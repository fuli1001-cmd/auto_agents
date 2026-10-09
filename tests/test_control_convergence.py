"""Recovery uses retained facts, never a second execution to reconstruct them."""

import json
import sqlite3
import sys
from dataclasses import replace

import pytest
from auto_agents.control import Store, ControlError, Contract
from auto_agents.control.engine import Engine
from auto_agents.control.effects import Verifier, compile_checks
from auto_agents.control.types import ExecutionContext
from auto_agents.control.api import status
from auto_agents.control.workspace import git
from auto_agents.models import ProjectConfig
from test_control_engine import Worker, project


@pytest.mark.parametrize("tamper", [False, True])
def test_native_result_revalidated_without_redispatch_or_refunding(
    tmp_path, monkeypatch, tamper
):
    from auto_agents.control import quality

    root = project(tmp_path)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    original = quality.stage_writes

    def reject(before, after, phase, scope=()):
        if phase == "implement":
            raise ControlError(
                "stage_scope",
                "Old validator rejects an allowed output",
                category="model",
            )
        return original(before, after, phase, scope)

    monkeypatch.setattr(quality, "stage_writes", reject)
    blocked = engine.start("fix", "Set value to one", max_calls=6)
    assert blocked["status"] == "BLOCKED"
    op = store.operations(blocked["id"])[-1]
    native = store.outcome(op["id"])
    assert (
        native["native_state"] == "confirmed"
        and native["validation"]["state"] == "rejected"
    )
    assert json.loads(native["payload"]["text"])["summary"] == "Fixed value"
    assert op["state"] == "FAILED"
    monkeypatch.setattr(quality, "stage_writes", original)
    if tamper:
        (engine.context(blocked).workspace_root / "value.py").write_text("VALUE = 99\n")
    result = engine.resume(blocked["id"])
    if tamper:
        assert (
            result["status"] == "BLOCKED"
            and result["failure"]["category"] == "reconciliation"
        )
        assert store.workflow(blocked["workflow"])["calls"] == 2
    else:
        assert result["status"] == "COMPLETED", result["failure"]
        assert store.workflow(blocked["workflow"])["calls"] == 3
        assert (root / "value.py").read_text() == "VALUE = 1\n"
        assert store.outcome(op["id"])["validation"]["state"] == "accepted"
    assert [p for _, p in worker.calls].count("implement") == 1
    assert store.outcome(op["id"])["payload"] == native["payload"]
    assert store.contract(result["contract"]).goal == "Set value to one"


def test_known_verification_replays_results_not_the_effectful_test(
    tmp_path, monkeypatch
):
    from auto_agents.control import proof

    root = project(tmp_path)
    store = Store(root)

    class Counted(Worker):
        def run(self, ctx, phase, *a, **kw):
            result = super().run(ctx, phase, *a, **kw)
            if phase == "review":
                self.observed_count = (ctx.workspace_root / ".tmp/count").read_text()
            if phase == "implement":
                (ctx.workspace_root / "tests/test_value.py").write_text(
                    "from pathlib import Path\nfrom value import VALUE\n"
                    "def test_value():\n p=Path('.tmp/count');p.parent.mkdir(exist_ok=True)\n"
                    " p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n assert VALUE == 1\n"
                )
            return result

    worker = Counted()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    count = proof.executed_count
    monkeypatch.setattr(proof, "executed_count", lambda text: 0)
    blocked = engine.start("fix", "Set value")
    workspace = engine.context(blocked).workspace_root
    assert blocked["failure"]["code"] == "proof"
    assert (workspace / ".tmp/count").read_text() == "1"
    operation = next(
        o for o in store.operations(blocked["id"]) if o["kind"] == "verify"
    )
    assert store.outcome(operation["id"])["native_state"] == "confirmed"
    monkeypatch.setattr(proof, "executed_count", count)
    result = engine.resume(blocked["id"])
    assert result["status"] == "COMPLETED", result["failure"]
    assert worker.observed_count == "1"
    assert store.outcome(operation["id"])["validation"]["state"] == "accepted"


def test_temporary_verification_script_cannot_rewrite_itself(tmp_path):
    root = project(tmp_path)
    private = root / ".tmp-tests"
    private.mkdir()
    (private / "test_self.py").write_text(
        "from pathlib import Path\ndef test_self():\n Path(__file__).write_text('def test_self(): pass\\n')\n"
    )
    context = ExecutionContext(
        root,
        root,
        "fixture",
        Contract("wf", "fixture", "collab", "Observe", "source"),
        "fixture",
        verification_workspace="owned_runtime",
    )
    with pytest.raises(ControlError, match="frozen test inputs"):
        Verifier().run(
            context,
            compile_checks(
                [sys.executable + " -m pytest -q .tmp-tests/test_self.py"], root
            ),
        )


def test_schema_two_upgrade_preserves_identity_and_readers_do_not_migrate(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    work = store.create_workflow(
        "collab", "验收已有功能，不新增产品需求", "source", max_calls=5
    )
    with store.connect(True) as db:
        db.execute("DROP TABLE operation_outcomes")
        db.execute("DROP TABLE contract_policies")
        db.execute("DELETE FROM control_meta WHERE key='control_features'")
    before = status(root)
    reader = Store(root, readonly=True)
    assert (
        reader.policy(reader.contract(work["contract"])).kind == "existing_acceptance"
    )
    assert not reader.has_features()
    store.ensure_features()
    store.policy(store.contract(work["contract"]), persist=True)
    after = status(root)
    assert (
        before["identity"] == after["identity"]
        and before["records"] == after["records"]
    )
    assert store.work(work["id"])["contract"] == work["contract"]
    assert store.workflow(work["workflow"])["calls"] == 0
    assert list((root / ".auto-agents/state/schema-upgrades").glob("*.sqlite3"))


def test_unknown_native_result_never_retries_or_switches_provider(tmp_path):
    root = project(tmp_path)
    store = Store(root)

    class Unknown(Worker):
        def run(self, ctx, phase, *a, **kw):
            self.calls.append((ctx.work_id, phase))
            raise ControlError(
                "outcome_unknown",
                "CLI ended without a confirmed terminal result",
                category="reconciliation",
            )

    worker = Unknown()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        print_fn=lambda *a: None,
    )
    result = engine.start("fix", "Set value")
    assert store.outcome(store.operations()[0]["id"])["native_state"] == "unknown"
    assert engine.resume(result["id"])["status"] == "BLOCKED"
    assert len(worker.calls) == 1 and store.workflow(result["workflow"])["calls"] == 1


def test_original_no_source_permission_cannot_be_overridden_by_model(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        print_fn=lambda *a: None,
    )
    result = engine.start("collab", "验收已有功能，不新增产品需求，不修改产品源码。")
    assert result["status"] == "BLOCKED" and result["failure"]["code"] == "scope_policy"
    assert len(store.works()) == 1 and [p for _, p in worker.calls] == ["diagnose"]
    assert (root / "value.py").read_text() == "VALUE = 0\n"


def test_mode_phase_and_wait_ownership_rejected_before_persistence(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Bounded fix", "source")
    with pytest.raises(ControlError, match="mode"):
        store.transition(work, "RUNNING", phase="prototype")
    with pytest.raises(ControlError, match="binding"):
        store.transition(work, "WAITING", context={"waiting_for": "user"})
    other = store.create_workflow("fix", "Other root", "source")
    with pytest.raises(ControlError, match="another parent"):
        store.transition(
            work, "WAITING", context={"waiting_for": "child", "child": other["id"]}
        )
    assert store.work(work["id"]) == work


def test_historical_development_cannot_resume_after_its_source_changed(tmp_path):
    from test_control_acceptance_scope import retained_development_route

    _, store, engine, parent, child, database = retained_development_route(tmp_path)
    (database.parents[1] / "value.py").write_text("VALUE = 2\n")
    before = database.read_bytes()
    result = engine.resume(parent["id"])
    assert result["failure"]["code"] == "scope_policy" and result["status"] == "BLOCKED"
    assert not engine.transport.calls and database.read_bytes() == before


@pytest.mark.parametrize("bad", ["saved_tasks", "old_prototype", "broad_suite"])
def test_acceptance_repairs_need_real_evidence_and_targeted_regressions(tmp_path, bad):
    root = project(tmp_path)
    for name in (
        ".auto-agents/docs/acceptance/failure.json",
        ".auto-agents/docs/requirements.md",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "Observed failure"
            if "acceptance/" in name
            else "Historical planned feature"
        )
    git(root, "add", ".auto-agents/docs")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "retained acceptance evidence",
    )
    store = Store(root)

    class Invalid(Worker):
        def run(self, ctx, phase, *a, **kw):
            self.calls.append((ctx.work_id, phase))
            issue = {
                "summary": "Acceptance blocker",
                "reproduction": ["Recorded browser action failed"],
                "paths": ["value.py"],
                "checks": [{"command": "python -m pytest -q tests/test_value.py"}],
                "evidence_refs": [".auto-agents/docs/acceptance/failure.json"],
            }
            if bad == "saved_tasks":
                issue["task_ids"] = ["historical-task"]
            elif bad == "old_prototype":
                issue["evidence_refs"] = [".auto-agents/docs/requirements.md"]
            else:
                issue["checks"] = [{"command": "python -m pytest -q tests"}]
            return {"text": json.dumps({"action": "fix", "issue": issue})}

    worker = Invalid()
    result = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        print_fn=lambda *a: None,
    ).start("collab", "验收已有功能，不新增产品需求")
    assert result["status"] == "BLOCKED" and len(store.works()) == 1
    assert all(phase == "diagnose" for _, phase in worker.calls)
    assert (root / "value.py").read_text() == "VALUE = 0\n"


def test_crash_after_terminal_receipt_recovers_locally_without_another_call(
    tmp_path, monkeypatch
):
    root = project(tmp_path)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    settle = store.settle
    interrupted = False

    def crash(identity, result, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return settle(identity, result, **kwargs)

    monkeypatch.setattr(store, "settle", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.start("fix", "Set value")
    work = store.works()[0]
    operation = store.operations()[0]
    assert (
        operation["state"] == "DISPATCHED"
        and store.outcome(operation["id"])["native_state"] == "confirmed"
    )
    store.orphan_calls()
    assert store.operations()[0]["state"] == "UNKNOWN"
    store.transition(
        work,
        "BLOCKED",
        failure={"category": "reconciliation", "code": "outcome_unknown"},
    )
    monkeypatch.setattr(store, "settle", settle)
    result = engine.resume(work["id"])
    assert result["status"] == "COMPLETED", result["failure"]
    assert [phase for _, phase in worker.calls].count("classify") == 1
    assert store.workflow(work["workflow"])["calls"] == 3


def test_future_feature_schema_is_rejected_without_rewriting_it(tmp_path):
    store = Store(tmp_path)
    store.create_workflow("fix", "Goal", "source")
    with store.connect(True) as db:
        db.execute("UPDATE control_meta SET value='99' WHERE key='control_features'")
    with pytest.raises(ControlError, match="unsupported control features"):
        Store(tmp_path).ensure_features()
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT value FROM control_meta WHERE key='control_features'"
            ).fetchone()[0]
            == "99"
        )


def test_legacy_failed_receipt_without_native_result_stays_blocked(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        print_fn=lambda *a: None,
    )
    source = engine.workspaces.snapshot()
    work = store.create_workflow("fix", "Goal", source)
    running = store.transition(work, "RUNNING")
    op = store.reserve(running, "model:classify", 0, {}, model=True)
    failure = ControlError(
        "stage_scope", "Old validator rejected output", category="model"
    ).to_dict()
    store.settle(op["id"], failure, state="FAILED")
    store.transition(running, "BLOCKED", failure=failure)
    result = engine.resume(work["id"])
    assert (
        result["status"] == "BLOCKED"
        and result["failure"]["details"]["recovery"] == "legacy_unavailable"
    )
    assert not worker.calls and store.workflow(work["workflow"])["calls"] == 1


def test_validation_cannot_replace_the_original_native_payload(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "Goal", "source")
    running = store.transition(work, "RUNNING")
    op = store.reserve(running, "model:classify", 0, {}, model=True)
    operation = store.operations()[0]
    store.record_native(
        op["id"],
        {"text": "original"},
        {"contract": work["contract"], "input_hash": operation["input_hash"]},
    )
    with pytest.raises(ControlError, match="replace the native response"):
        store.validate_outcome(op["id"], result={"text": "fabricated"})
    assert store.operations()[0]["state"] == "DISPATCHED"
    assert store.outcome(op["id"])["payload"]["text"] == "original"


def test_invalid_acceptance_protocol_does_not_repeat_creation(tmp_path):
    root = project(tmp_path)
    store = Store(root)

    class Invalid(Worker):
        def run(self, ctx, phase, *args, **kwargs):
            self.calls.append((ctx.work_id, phase))
            if phase == "diagnose":
                return {"text": '{"action":"acceptance"}'}
            if phase == "acceptance":
                data = ctx.workspace_root / ".data/project"
                data.parent.mkdir()
                assert not data.exists()
                data.write_text("created once")
                return {"text": "invalid terminal JSON"}
            raise AssertionError(phase)

    worker = Invalid()
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        print_fn=lambda *a: None,
    )
    failed = engine.start("collab", "验收已有功能，不新增产品需求")
    assert failed["failure"]["category"] == "model"
    assert engine.resume(failed["id"])["status"] == "BLOCKED"
    assert [p for _, p in worker.calls] == ["diagnose", "acceptance"]
    assert (
        engine.context(failed).workspace_root / ".data/project"
    ).read_text() == "created once"


def test_nested_package_dependency_bindings_are_protected_and_reused(tmp_path):
    from auto_agents.control.quality import workspace_guard, stage_writes

    root = project(tmp_path)
    app = root / "apps/client"
    app.mkdir(parents=True)
    (app / "package.json").write_text('{"dependencies":{"next":"15.2.4"}}')
    deps = app / "node_modules"
    deps.mkdir()
    (deps / "library").write_text("shared dependency")
    (root / ".gitignore").write_text("node_modules/\n")
    git(root, "add", "apps/client/package.json", ".gitignore")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "nested app",
    )
    store = Store(root)
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Worker(),
        print_fn=lambda *a: None,
    )
    work = store.create_workflow("fix", "Goal", engine.workspaces.snapshot())
    workspace = engine.context(work).workspace_root
    binding = workspace / "apps/client/node_modules"
    assert binding.is_symlink() and binding.resolve() == deps.resolve()
    before = workspace_guard(workspace)
    binding.unlink()
    with pytest.raises(ControlError, match="protected"):
        stage_writes(before, workspace_guard(workspace), "implement")
    assert (deps / "library").read_text() == "shared dependency"


def test_retained_native_result_survives_snapshot_relocation(tmp_path, monkeypatch):
    from auto_agents.control import api, quality

    root = project(tmp_path)
    store = Store(root)
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Worker(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    original = quality.stage_writes

    def reject(before, after, phase, scope=()):
        if phase == "implement":
            raise ControlError("stage_scope", "Old rule", category="model")
        return original(before, after, phase, scope)

    monkeypatch.setattr(quality, "stage_writes", reject)
    failed = engine.start("fix", "Set value")
    host = api.status(root)["identity"]
    copy = tmp_path / "snapshot"
    api.snapshot(root, copy)
    monkeypatch.setattr(quality, "stage_writes", original)
    transport = Worker()
    isolated = Engine(
        copy,
        Store(copy),
        ProjectConfig("fixture"),
        transport=transport,
        offline=True,
        print_fn=lambda *a: None,
    )
    result = isolated.resume(failed["id"])
    assert result["failure"]["category"] == "offline" and result["phase"] == "review", (
        result["failure"]
    )
    assert not transport.calls and api.status(root)["identity"] == host


@pytest.mark.parametrize("extension", ["mjs", "cjs"])
def test_browser_helper_sources_cannot_be_mutable_check_outputs(tmp_path, extension):
    with pytest.raises(ControlError, match="sources cannot be mutable"):
        compile_checks(
            [
                {
                    "command": "python -m pytest tests/test_browser.py",
                    "outputs": [".tmp-tests/browser." + extension],
                }
            ],
            tmp_path,
        )


def test_incomplete_readonly_review_is_corrected_without_reimplementation(tmp_path):
    root = project(tmp_path)
    store = Store(root)

    class MissingFlag(Worker):
        def run(self, ctx, phase, *a, **kw):
            result = super().run(ctx, phase, *a, **kw)
            if phase == "review" and [p for _, p in self.calls].count("review") == 1:
                reply = json.loads(result["text"])
                del reply["test_changes_valid"]
                result["text"] = json.dumps(reply)
            return result

    worker = MissingFlag()
    result = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    ).start("fix", "Set value", max_calls=6)
    assert result["status"] == "COMPLETED", result["failure"]
    assert [phase for _, phase in worker.calls].count("implement") == 1
    assert [phase for _, phase in worker.calls].count("review") == 2
    assert len([o for o in store.operations() if o["kind"] == "verify"]) == 1


def test_tracked_build_named_inputs_stay_frozen_during_acceptance(tmp_path):
    from auto_agents.control.quality import workspace_guard, stage_writes, source_seal

    root = project(tmp_path)
    path = root / "web/.next/fixture.bundle"
    path.parent.mkdir(parents=True)
    path.write_text("explicit tracked input")
    git(root, "add", "-f", "web/.next/fixture.bundle")
    before = workspace_guard(root)
    seal = source_seal(root)
    path.write_text("unexpected modification")
    assert source_seal(root) != seal
    with pytest.raises(ControlError):
        stage_writes(before, workspace_guard(root), "acceptance")
