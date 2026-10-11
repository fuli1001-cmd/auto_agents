"""Only an explicit operator command can add model-call allowance."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from auto_agents.control import ControlError, Store
from auto_agents.control.cli import main
from auto_agents.control.engine import Engine
from auto_agents.control.types import digest
from auto_agents.models import ProjectConfig
from test_control_engine import Worker, project


def budget_events(store, identity):
    with store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM events WHERE work=? AND event='budget_extended' ORDER BY id", (identity,),
        )]


def test_extension_preserves_used_calls_unknown_receipts_contract_and_retry_allowance(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("run", "Keep the exact goal", "source")
    work = store.transition(work, "RUNNING", phase="prototype_prepare", context={
        "frontend": True, "prototype_repairs": 2, "protocol_retries": 2,
        "provider": "codex-fuli0110", "operator_answers": [],
    })
    for ordinal in range(13):
        op = store.reserve(work, "model:prototype_prepare", ordinal, {"ordinal": ordinal}, model=True)
        store.settle(op["id"], {"ordinal": ordinal}, state="UNKNOWN" if ordinal == 12 else "CONFIRMED")
        if ordinal != 12:
            store.consume(op["id"])
    store.record_native(op["id"], {"terminal": "unconfirmed"}, {
        "contract": work["contract"], "input_hash": digest({"ordinal": 12}),
    }, state="unknown")
    work = store.transition(work, "BLOCKED", failure={"category": "reconciliation", "code": "outcome_unknown"})
    before_ops, before_native = store.operations(work["id"]), store.outcome(op["id"])
    before_contract, before_workflow = store.contract(work["contract"]), store.workflow(work["workflow"])

    result = store.extend_budget(work, 30, reason="User authorized a total limit of 30")
    updated = store.work(work["id"])
    assert result["calls"] == 13 and result["max_calls"] == 30 and result["remaining_calls"] == 17
    assert result["status"] == "BLOCKED" and not result["reopened"]
    assert updated["context"] == work["context"] and updated["failure"] == work["failure"]
    assert store.operations(work["id"]) == before_ops and store.outcome(op["id"]) == before_native
    assert store.contract(updated["contract"]) == before_contract
    assert store.workflow(work["workflow"]) == before_workflow
    assert budget_events(store, work["id"])[0] == {
        "previous_max_calls": 15, "max_calls": 30, "calls": 13,
        "contract": work["contract"], "reason": "User authorized a total limit of 30", "reopened": False,
    }


@pytest.mark.parametrize("limit", [0, -1, True, 30.5, "30", None, 2**63, 10])
def test_invalid_or_lower_limits_cannot_change_accounting(tmp_path, limit):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Keep goal", "source")
    with pytest.raises(ControlError):
        store.extend_budget(work, limit, reason="Explicit decision")
    assert store.work(work["id"]) == work and not budget_events(store, work["id"])


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_budget_extension_needs_an_auditable_reason(tmp_path, reason):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Keep goal", "source")
    with pytest.raises(ControlError):
        store.extend_budget(work, 30, reason=reason)
    assert store.work(work["id"]) == work


@pytest.mark.parametrize("status", ["RUNNING", "COMPLETED", "CANCELLED"])
def test_running_or_terminal_work_cannot_receive_an_operator_extension(tmp_path, status):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Keep goal", "source")
    work = store.transition(work, "RUNNING")
    if status != "RUNNING":
        work = store.transition(work, status)
    with pytest.raises(ControlError):
        store.extend_budget(work, 30, reason="Explicit decision")
    assert store.work(work["id"]) == work


def test_stale_or_readonly_authority_cannot_extend_budget(tmp_path):
    store = Store(tmp_path)
    stale = store.create_workflow("fix", "Keep goal", "source")
    current = store.transition(stale, "RUNNING")
    current = store.transition(current, "BLOCKED", failure={"code": "environment"})
    with pytest.raises(ControlError, match="changed after it was read"):
        store.extend_budget(stale, 30, reason="Explicit decision")
    with pytest.raises(ControlError, match="Read-only"):
        Store(tmp_path, readonly=True).extend_budget(current, 30, reason="Explicit decision")
    assert store.work(current["id"]) == current


def test_extension_and_its_audit_roll_back_together(tmp_path, monkeypatch):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Keep goal", "source")
    original = store._event
    def reject_audit(db, identity, event, data):
        if event == "budget_extended":
            raise RuntimeError("Audit unavailable")
        original(db, identity, event, data)
    monkeypatch.setattr(store, "_event", reject_audit)
    with pytest.raises(RuntimeError, match="Audit unavailable"):
        store.extend_budget(work, 30, reason="Explicit decision")
    assert store.work(work["id"]) == work and not budget_events(store, work["id"])


@pytest.mark.parametrize("failure", [
    {"category": "verification", "code": "prototype_scope"},
    {"category": "migration", "code": "migration_required"},
    {"category": "budget", "code": "acceptance_budget"},
    {"category": "budget", "code": "budget", "child_id": "owned-child"},
])
def test_extension_does_not_clear_other_blockers(tmp_path, failure):
    store = Store(tmp_path)
    work = store.create_workflow("run", "Keep goal", "source")
    work = store.transition(work, "RUNNING", phase="prototype_prepare", context={"frontend": True})
    work = store.transition(work, "BLOCKED", failure=failure)
    result = store.extend_budget(work, 30, reason="Explicit decision")
    assert result["status"] == "BLOCKED" and store.work(work["id"])["failure"] == failure


def test_extension_preserves_pending_approval_and_other_work_budgets(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("run", "Keep goal", "source")
    work = store.transition(work, "RUNNING", phase="prototype", context={"frontend": True})
    context = {"frontend": True, "waiting_for": "approval", "approval": "prototype"}
    work = store.transition(work, "WAITING", context=context)
    other = store.create_workflow("fix", "Independent work", "source")
    result = store.extend_budget(work, 30, reason="Explicit decision")
    assert result["status"] == "WAITING" and store.work(work["id"])["context"] == context
    assert store.work(other["id"]) == other


def test_extending_a_parent_preserves_owned_child_limits(tmp_path):
    store = Store(tmp_path)
    parent = store.create_workflow("run", "Keep root goal", "source")
    parent = store.transition(parent, "RUNNING", phase="plan")
    child = store.child(parent, "fix", "Owned child goal", "source")
    parent = store.work(parent["id"])
    store.extend_budget(parent, 30, reason="Extend this parent only")
    assert store.work(child["id"]) == child
    assert store.work(parent["id"])["context"] == parent["context"]


def test_budget_extension_does_not_manufacture_business_progress(tmp_path):
    from auto_agents.control.observer import step_history
    store = Store(tmp_path)
    work = store.create_workflow("run", "Keep root goal", "source")
    work = store.transition(work, "RUNNING", phase="prototype_prepare")
    for _ in range(3):
        store.event(work["id"], "step_entered", {"step_id": work["id"] + ":prototype_prepare", "phase": "prototype_prepare", "work_id": work["id"], "progress_seq": 0})
    work = store.transition(work, "BLOCKED", failure={"category": "verification", "code": "prototype_scope"})
    before = step_history(store, work["workflow"])
    store.extend_budget(work, 30, reason="Extend financial allowance only")
    assert step_history(store, work["workflow"]) == before


@pytest.mark.parametrize("workflow_limit", [None, 1])
def test_exhausted_work_resumes_only_when_all_original_limits_allow_it(tmp_path, workflow_limit):
    root = project(tmp_path)
    config = ProjectConfig("fixture")
    config.execution.session_limits.hard_ceiling["fix"] = 1
    worker = Worker()
    engine = Engine(root, Store(root), config, transport=worker, print_fn=lambda *args: None)
    work = engine.start("fix", "Set value", max_calls=workflow_limit)
    assert work["status"] == "BLOCKED" and work["failure"]["code"] == "budget"
    calls = work["calls"]
    result = engine.store.extend_budget(work, 30, reason="Explicit decision")
    assert result["reopened"] == (workflow_limit is None)
    assert len(worker.calls) == calls == 1
    resumed = engine.resume(work["id"])
    assert (resumed["status"] == "COMPLETED") == (workflow_limit is None), resumed["failure"]
    assert engine.store.workflow(work["workflow"])["max_calls"] == workflow_limit
    assert engine.store.work(work["id"])["calls"] == len(worker.calls)
    assert engine.store.contract(resumed["contract"]).goal == "Set value"


@pytest.mark.parametrize("subprocess_cli", [False, True])
def test_public_budget_extension_is_explicit_idempotent_and_dispatches_no_models(tmp_path, capsys, monkeypatch, subprocess_cli):
    from auto_agents.control import cli
    root = project(tmp_path)
    store = Store(root)
    work = store.create_workflow("run", "Keep goal", "source")
    args = ["extend-budget", "--project", str(root), "--session", work["id"], "--limit", "30", "--reason", "User authorized 30 calls"]
    monkeypatch.setattr(cli, "Engine", lambda *args, **kwargs: pytest.fail("Budget extension dispatched the engine"))
    if subprocess_cli:
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "AUTO_AGENTS_NO_SUPERVISOR": "1", "AUTO_AGENTS_PINNED_RUNTIME": "fixture"}
        completed = subprocess.run([sys.executable, "-m", "auto_agents", *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        result = json.loads(completed.stdout)
    else:
        assert main(args) == 0
        result = json.loads(capsys.readouterr().out)
    assert result["calls"] == 0 and result["max_calls"] == 30 and result["remaining_calls"] == 30
    current = store.work(work["id"])
    assert main(args) == 0
    duplicate = json.loads(capsys.readouterr().out)
    assert not duplicate["changed"] and store.work(work["id"]) == current
    assert len(budget_events(store, work["id"])) == 1 and not store.operations(work["id"])


def test_ordinary_transitions_still_cannot_increase_budget(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Keep goal", "source")
    with pytest.raises(ControlError, match="cannot change identity, contract or budget"):
        store.transition(work, "READY", max_calls=30)
    assert store.work(work["id"]) == work


def test_budget_command_uses_the_public_entrypoint_without_selecting_an_old_runtime(tmp_path, capsys, monkeypatch):
    from auto_agents import bootstrap
    root = project(tmp_path)
    store = Store(root)
    work = store.create_workflow("fix", "Keep goal", "source")
    monkeypatch.setattr(bootstrap, "select_runtime", lambda *args: pytest.fail("Budget command selected another runtime"))
    assert bootstrap.main(["extend-budget", "--project", str(root), "--session", work["id"], "--limit", "30", "--reason", "User authorized 30 calls"]) == 0
    assert json.loads(capsys.readouterr().out)["max_calls"] == 30
