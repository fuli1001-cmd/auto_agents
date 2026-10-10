"""Scope feedback reaches blocked prototype work through the public CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from auto_agents.config import save_project_config
from auto_agents.control import ControlError, Store
from auto_agents.control.cli import decision_owner, main
from auto_agents.control.engine import Engine
from auto_agents.control.workspace import git
from auto_agents.models import ProjectConfig
from test_control_domains import Frontend
from test_control_engine import project


@pytest.mark.parametrize("native", [False, True])
def test_answer_revises_a_blocked_scope_without_a_pending_question_and_resumes_same_work(tmp_path, capsys, native):
    root = project(tmp_path)
    config = ProjectConfig("blocked scope feedback")
    config.frontend_design.mode = "off"
    save_project_config(root, config)
    store = Store(root)

    class Invalid(Frontend):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare":
                value = json.loads(result["text"])
                value["scope"]["changes"][0]["requirement_ids"] = []
                result["text"] = json.dumps(value)
            return result

    engine = Engine(root, store, config, transport=Invalid(), auto_approve=True, print_fn=lambda *a: None)
    blocked = engine.start("run", "Add retry", max_calls=12)
    assert blocked["status"] == "BLOCKED" and blocked["failure"]["code"] == "prototype_scope"
    assert not blocked["context"].get("input_requests") and not blocked["context"].get("waiting_for")
    operation = next(o for o in store.operations(blocked["id"]) if not o["consumed"])
    native_result = store.outcome(operation["id"])
    budget = store.workflow(blocked["workflow"])
    feedback = "Retain the original scope and preservation checks; bind REQ-001 to real new behavior checks."
    arguments = ["answer", "--project", str(root), "--session", blocked["id"], "--value", feedback, "--no-resume"]
    if native:
        source = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-m", "auto_agents", *arguments], cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(source / "src"), "AUTO_AGENTS_NO_SUPERVISOR": "1",
                 "AUTO_AGENTS_PINNED_RUNTIME": "fixture"}, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["ok"]
    else:
        assert main(arguments) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    ready = store.work(blocked["id"])
    assert ready["status"] == "READY" and ready["phase"] == "prototype_prepare" and not ready["failure"]
    assert ready["context"]["feedback"] == ready["context"]["operator_answers"][-1] == feedback
    assert "prototype_repairs" not in ready["context"]
    assert ready["contract"] == blocked["contract"] and ready["max_calls"] == blocked["max_calls"]
    assert ready["calls"] == blocked["calls"] == budget["calls"] == 4
    assert store.workflow(blocked["workflow"]) == budget
    assert store.outcome(operation["id"]) == native_result
    assert next(o for o in store.operations(blocked["id"]) if o["id"] == operation["id"])["consumed"]
    engine.transport = Frontend()
    resumed = engine.resume(blocked["id"])
    assert resumed["id"] == blocked["id"] and resumed["contract"] == blocked["contract"]
    assert resumed["status"] == "WAITING" and resumed["context"]["approval"] == "prototype", resumed["failure"]
    assert store.workflow(blocked["workflow"])["calls"] == 8
    assert store.workflow(blocked["workflow"])["max_calls"] == 12
    assert not any(o["state"] in {"UNKNOWN", "DISPATCHED"} for o in store.operations())


def blocked_work(tmp_path, *, phase="prototype_prepare", category="verification", code="prototype_scope", approval=False):
    root = project(tmp_path)
    save_project_config(root, ProjectConfig("feedback guards"))
    store = Store(root)
    work = store.create_workflow("run", "Original goal", git(root, "rev-parse", "HEAD"), max_calls=4)
    work = store.transition(work, "RUNNING", phase=phase)
    context = {"waiting_for": "approval", "approval": "prototype"} if approval else {}
    work = store.transition(work, "BLOCKED", context=context,
        failure={"category": category, "code": code, "message": "Original failure"})
    return root, store, work


@pytest.mark.parametrize("category,code,phase,approval", [
    ("budget", "budget", "prototype_prepare", False),
    ("reconciliation", "outcome_unknown", "prototype_prepare", False),
    ("migration", "migration_required", "prototype_prepare", False),
    ("engine", "engine_exception", "prototype_prepare", False),
    ("model", "approved_contract", "prototype_prepare", False),
    ("verification", "prototype_scope", "design", False),
    ("verification", "prototype_scope", "prototype_prepare", True),
])
def test_scope_feedback_cannot_clear_unrelated_failures_or_pending_approval(tmp_path, capsys, category, code, phase, approval):
    root, store, work = blocked_work(tmp_path, phase=phase, category=category, code=code, approval=approval)
    before = store.workflow(work["workflow"])
    assert main(["answer", "--project", str(root), "--session", work["id"],
                 "--value", "Keep the scope", "--no-resume"]) == 3
    capsys.readouterr()
    assert store.work(work["id"]) == work and store.workflow(work["workflow"]) == before


def test_scope_feedback_refuses_unconfirmed_operations_before_changing_input(tmp_path, capsys):
    root, store, work = blocked_work(tmp_path)
    work = store.transition(work, "READY")
    work = store.transition(work, "RUNNING")
    store.reserve(work, "model:prototype_prepare", 0, {}, model=True)
    work = store.transition(work, "BLOCKED", failure=work["failure"])
    operations = store.operations()
    budget = store.workflow(work["workflow"])
    assert main(["answer", "--project", str(root), "--session", work["id"],
                 "--value", "Keep the scope", "--no-resume"]) == 3
    assert "Reconcile existing prototype operations" in capsys.readouterr().err
    assert store.work(work["id"]) == work and store.operations() == operations
    assert store.workflow(work["workflow"]) == budget


def test_scope_feedback_does_not_ignore_a_nonexistent_question_id(tmp_path, capsys):
    root, store, work = blocked_work(tmp_path)
    assert main(["answer", "--project", str(root), "--session", work["id"], "--request-id", "absent",
                 "--value", "Keep the scope", "--no-resume"]) == 3
    capsys.readouterr()
    assert store.work(work["id"]) == work


@pytest.mark.parametrize("multiple", [False, True])
def test_root_scope_feedback_selects_only_one_owned_blocked_leaf(tmp_path, multiple):
    store = Store(tmp_path)
    parent = store.create_workflow("collab", "Original goal", "source")
    children = []
    for _ in range(2 if multiple else 1):
        child = store.child(store.work(parent["id"]), "run", "Prototype child", "source")
        child = store.transition(child, "RUNNING", phase="prototype_prepare")
        child = store.transition(child, "BLOCKED", failure={"category": "verification", "code": "prototype_scope"})
        children.append(child)
    parent = store.work(parent["id"])
    parent = store.transition(parent, "BLOCKED", context={"children": [c["id"] for c in children]},
        failure={"category": "verification", "code": "prototype_scope", "child_id": children[0]["id"]})
    if multiple:
        with pytest.raises(ControlError, match="one pending decision"):
            decision_owner(store, parent, "answer")
    else:
        assert decision_owner(store, parent, "answer")["id"] == children[0]["id"]


def test_feedback_after_candidate_failure_keeps_frozen_preservation_oracles(tmp_path, capsys):
    from test_control_prototype_contract import Iteration, setup

    root, engine = setup(tmp_path, Iteration(mutation=lambda html: html.replace("rgb(20,20,20)", "rgb(200,10,10)")))
    save_project_config(root, engine.config)
    work = engine.start("run", "Add retry")
    assert work["status"] == "BLOCKED" and work["phase"] == "prototype_verify", work["failure"]
    previous = work["context"]["prototype_scope"]
    calls = engine.store.workflow(work["workflow"])["calls"]
    assert main(["answer", "--project", str(root), "--session", work["id"],
                 "--value", "Correct only the original scoped issue and keep all preservation checks", "--no-resume"]) == 0
    capsys.readouterr()
    ready = engine.store.work(work["id"])
    assert ready["context"]["prototype_previous_scope"] == previous
    assert engine.store.workflow(work["workflow"])["calls"] == calls

    class Weakened(Iteration):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype_prepare":
                value = json.loads(result["text"])
                value["scope"]["checks"][0]["asserts"][0]["equals"] = "manual"
                result["text"] = json.dumps(value)
            return result

    engine.transport = Weakened()
    blocked = engine.resume(work["id"])
    assert blocked["status"] == "BLOCKED" and "weaken frozen behavior checks" in blocked["failure"]["message"]
