"""Existing-behavior acceptance keeps its route, private runtime and receipts."""

import json
import sqlite3
import subprocess
import sys

import pytest

from auto_agents.control import ControlError, Store
from auto_agents.control.engine import Engine
from auto_agents.control.quality import stage_writes, workspace_guard
from auto_agents.control.workspace import git, private_runtime_files
from auto_agents.models import ProjectConfig
from test_control_engine import Worker, project


GOAL = "对当前已有功能执行真实端到端验收，不新增产品需求，不重新设计或规划项目。"
REPORT = ".auto-agents/docs/acceptance/result.txt"


class AcceptanceWorker(Worker):
    def run(self, ctx, phase, prompt, output, **kwargs):
        if phase not in {"diagnose", "acceptance"}:
            return super().run(ctx, phase, prompt, output, **kwargs)
        self.calls.append((ctx.work_id, phase))
        if phase == "diagnose":
            assert "Do not route run" in prompt
            # Exercise a mistaken model route even after the scope instruction.
            result = {"action": "run", "spec": "Execute the existing browser goal"}
        else:
            root = ctx.workspace_root
            database = root / ".data/app.db"
            database.parent.mkdir(exist_ok=True)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE projects (title TEXT)")
                db.execute("INSERT INTO projects VALUES ('one acceptance project')")
            temporary = root / ".tmp-tests/test_acceptance.py"
            temporary.parent.mkdir()
            temporary.write_text(
                "import sqlite3, unittest\n"
                "from value import VALUE\n"
                "class Acceptance(unittest.TestCase):\n"
                " def test_existing_runtime(self):\n"
                "  self.assertEqual(VALUE, 0)\n"
                "  with sqlite3.connect('file:.data/app.db?mode=ro', uri=True) as db:\n"
                "   self.assertEqual(db.execute('SELECT title FROM projects').fetchall(), "
                "[('one acceptance project',)])\n"
            )
            report = root / REPORT
            report.parent.mkdir(parents=True)
            report.write_text("Observed the existing behavior and one private project")
            result = {
                "accepted": True,
                "evidence_refs": [REPORT],
                "checks": [{"command": sys.executable + " -m unittest discover -s .tmp-tests -p test_acceptance.py -v"}],
            }
        output.write_text(json.dumps(result))
        return {"text": json.dumps(result), "provider": "fixture"}


@pytest.mark.parametrize("isolation", [False, True])
def test_acceptance_only_cannot_become_a_development_run(tmp_path, isolation):
    root = project(tmp_path)
    database = root / ".data/app.db"
    database.parent.mkdir()
    database.write_bytes(b"original operator database")
    config = ProjectConfig("fixture")
    config.gates.isolation.enabled = isolation
    store = Store(root)
    worker = AcceptanceWorker()
    engine = Engine(root, store, config, transport=worker, auto_approve=True,
                    print_fn=lambda *a: None)
    done = engine.start("collab", GOAL)

    assert done["status"] == "COMPLETED", done["failure"]
    assert [phase for _, phase in worker.calls] == ["diagnose", "acceptance", "acceptance_review"]
    assert len(store.works()) == 1
    assert (root / "value.py").read_text() == "VALUE = 0\n"
    assert database.read_bytes() == b"original operator database"
    assert done["result"]["verification"]["checks"][0]["executed"] == 1
    assert done["result"]["delivery"]["paths"] == [REPORT]
    assert not (root / ".tmp-tests").exists()
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE event='diagnostic_route_corrected'").fetchone()[0] == 1


def test_collab_can_still_delegate_requested_development(tmp_path):
    root = project(tmp_path)
    store = Store(root)

    class Development(Worker):
        def run(self, ctx, phase, *args, **kwargs):
            if phase == "diagnose":
                self.calls.append((ctx.work_id, phase))
                return {"text": json.dumps({"action": "run", "spec": "Set value to one"})}
            return super().run(ctx, phase, *args, **kwargs)

    worker = Development()
    result = Engine(root, store, ProjectConfig("fixture"), transport=worker,
                    auto_approve=True, print_fn=lambda *a: None).start("collab", "Build a new value feature")
    assert result["status"] == "COMPLETED", result["failure"]
    assert any(work["mode"] == "run" for work in store.works())
    assert "clarify" in [phase for _, phase in worker.calls]
    assert (root / "value.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("phase", ["clarify", "prototype", "implement", "acceptance"])
def test_private_database_writes_are_limited_to_acceptance(tmp_path, phase):
    root = project(tmp_path)
    before = workspace_guard(root)
    database = root / ".data/app.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE initialized (id INTEGER)")
    after = workspace_guard(root)
    if phase == "acceptance":
        stage_writes(before, after, phase, (".auto-agents/docs/acceptance/",))
    else:
        with pytest.raises(ControlError) as error:
            stage_writes(before, after, phase)
        assert error.value.code == "stage_scope"


@pytest.mark.parametrize("name", [
    "value.py", ".env", ".data/.env", ".auto-agents/config.json",
    ".auto-agents/operator/input.json", ".auto-agents/state/workspace-control.json",
])
def test_acceptance_still_protects_source_credentials_and_control_paths(tmp_path, name):
    root = project(tmp_path)
    before = workspace_guard(root)
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed")
    with pytest.raises(ControlError) as error:
        stage_writes(before, workspace_guard(root), "acceptance", (".auto-agents/docs/acceptance/",))
    assert error.value.code == "stage_scope"


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "tracked"])
def test_runtime_exceptions_do_not_authorize_links_or_tracked_files(tmp_path, kind):
    root = project(tmp_path)
    data = root / ".data"
    data.mkdir()
    target = data / "app.db"
    if kind == "tracked":
        target.write_text("tracked source")
        git(root, "add", "-f", ".data/app.db")
    before = workspace_guard(root)
    if kind == "file_link":
        target.symlink_to(root / "value.py")
    elif kind == "directory_link":
        data.rmdir()
        data.symlink_to(tmp_path, target_is_directory=True)
    else:
        target.write_text("changed tracked source")
    with pytest.raises(ControlError) as error:
        stage_writes(before, workspace_guard(root), "acceptance", (".auto-agents/docs/acceptance/",))
    assert error.value.code == "stage_scope"


@pytest.mark.parametrize("kind", ["chrome_lock", "chrome_cookie", "local_directory", "local_file", "local_dangling"])
def test_acceptance_and_prototype_allow_private_tool_metadata_and_local_aliases(tmp_path, kind):
    root = project(tmp_path)
    private = root / ".tmp-tests/profile"
    private.mkdir(parents=True)
    before = workspace_guard(root)
    if kind == "chrome_lock":
        link = private / "SingletonLock"
        link.symlink_to("LAPTOP-UE3M8TAU-25904")
    elif kind == "chrome_cookie":
        link = private / "SingletonCookie"
        link.symlink_to("1234567890")
    else:
        target = private / "test_case0"
        if kind == "local_directory":
            target.mkdir()
        elif kind == "local_file":
            target.write_text("private runtime")
        link = private / "test_casecurrent"
        link.symlink_to(target, target_is_directory=kind == "local_directory")
    assert link.relative_to(root).as_posix() in private_runtime_files(root)
    stage_writes(before, workspace_guard(root), "acceptance", (".auto-agents/docs/acceptance/",))
    stage_writes(before, workspace_guard(root), "prototype")


@pytest.mark.parametrize("kind", ["outside", "tracked_target", "fake_chrome_lock"])
def test_runtime_links_still_cannot_alias_external_or_tracked_source(tmp_path, kind):
    root = project(tmp_path)
    private = root / ".tmp-tests/profile"
    private.mkdir(parents=True)
    if kind == "tracked_target":
        target = private / "source.py"
        target.write_text("TRACKED = True\n")
        git(root, "add", "-f", str(target.relative_to(root)))
    else:
        target = tmp_path / "external"
        target.mkdir()
    before = workspace_guard(root)
    link = private / ("SingletonLock" if kind == "fake_chrome_lock" else "alias")
    link.symlink_to(target)
    with pytest.raises(ControlError):
        stage_writes(before, workspace_guard(root), "acceptance", (".auto-agents/docs/acceptance/",))


def test_actual_pytest_tmp_path_aliases_are_private_acceptance_runtime(tmp_path):
    root = project(tmp_path)
    private = root / ".tmp-tests"
    private.mkdir()
    (private / "test_temp.py").write_text("def test_temp(tmp_path):\n (tmp_path/'output').write_text('observed')\n")
    before = workspace_guard(root)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", ".tmp-tests/test_temp.py", "--basetemp=.tmp-tests/pytest-runtime"],
        cwd=root, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((private / "pytest-runtime").glob("*current"))
    stage_writes(before, workspace_guard(root), "acceptance", (".auto-agents/docs/acceptance/",))


NEXT_TYPES = ('/// <reference types="next" />\n'
              '/// <reference types="next/image-types/global" />\n\n'
              '// NOTE: This file should not be edited\n'
              '// see https://nextjs.org/docs/app/api-reference/config/typescript for more information.\n')


def next_fixture(root, *, next_package=True, ignored=True):
    app = root / "frontend"
    app.mkdir()
    (app / "package.json").write_text(json.dumps({"dependencies": {"next": "15.2.4"} if next_package else {}}))
    if ignored:
        (root / ".gitignore").write_text("next-env.d.ts\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "frontend fixture")
    return app


@pytest.mark.parametrize("case", ["generated", "tracked", "source_content", "not_ignored", "not_next"])
def test_next_generated_declarations_are_runtime_only_when_untracked_and_ignored(tmp_path, case):
    root = project(tmp_path)
    app = next_fixture(root, next_package=case != "not_next", ignored=case != "not_ignored")
    path = app / "next-env.d.ts"
    if case == "tracked":
        path.write_text("// tracked declaration source\n")
        git(root, "add", "-f", "frontend/next-env.d.ts")
    before = workspace_guard(root)
    path.write_text("export const changed = true;\n" if case == "source_content" else NEXT_TYPES)
    if case == "generated":
        stage_writes(before, workspace_guard(root), "acceptance", (".auto-agents/docs/acceptance/",))
        with pytest.raises(ControlError):
            stage_writes(before, workspace_guard(root), "prototype")
    else:
        with pytest.raises(ControlError):
            stage_writes(before, workspace_guard(root), "acceptance", (".auto-agents/docs/acceptance/",))


def test_tool_runtime_does_not_enter_acceptance_delivery(tmp_path):
    root = project(tmp_path)
    next_fixture(root)
    store = Store(root)

    class Tools(AcceptanceWorker):
        def run(self, ctx, phase, *args, **kwargs):
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "acceptance":
                (ctx.workspace_root / "frontend/next-env.d.ts").write_text(NEXT_TYPES)
                private = ctx.workspace_root / ".tmp-tests"
                (private / "profile").mkdir()
                (private / "profile/SingletonLock").symlink_to("fixture-host-12345")
                (private / "pytest-case0").mkdir()
                (private / "pytest-casecurrent").symlink_to(private / "pytest-case0", target_is_directory=True)
            return result

    result = Engine(root, store, ProjectConfig("fixture"), transport=Tools(), auto_approve=True,
                    print_fn=lambda *a: None).start("collab", GOAL)
    assert result["status"] == "COMPLETED", result["failure"]
    assert result["result"]["delivery"]["paths"] == [REPORT]
    assert not (root / "frontend/next-env.d.ts").exists() and not (root / ".tmp-tests").exists()
    assert (root / "value.py").read_text() == "VALUE = 0\n"


@pytest.mark.parametrize("tamper", [False, True])
def test_failed_acceptance_retains_existing_project_and_diagnoses_before_retry(tmp_path, tamper):
    root = project(tmp_path)
    store = Store(root)

    class Blocked(Worker):
        def run(self, ctx, phase, prompt, output, **kwargs):
            self.calls.append((ctx.work_id, phase))
            if phase == "diagnose":
                if any(p == "acceptance" for _, p in self.calls):
                    assert "snapshot digest mismatch" in prompt and "project-existing" in prompt
                    assert "Do not repeat project creation" in prompt
                    result = {"action": "need_user", "question": "Review the evidenced provider prerequisite"}
                else:
                    result = {"action": "acceptance"}
            elif phase == "acceptance":
                database = ctx.workspace_root / ".data/app.db"
                database.parent.mkdir()
                with sqlite3.connect(database) as db:
                    db.execute("CREATE TABLE projects (id TEXT)")
                    db.execute("INSERT INTO projects VALUES ('project-existing')")
                report = ctx.workspace_root / REPORT
                report.parent.mkdir(parents=True)
                report.write_text("snapshot digest mismatch; existing project is preserved")
                result = {"accepted": False, "reason": "snapshot digest mismatch", "evidence_refs": [REPORT],
                          "external_ids": {"project_id": "project-existing"}}
            else:
                raise AssertionError(phase)
            return {"text": json.dumps(result)}

    worker = Blocked()
    engine = Engine(root, store, ProjectConfig("fixture"), transport=worker, print_fn=lambda *a: None)
    failed = engine.start("collab", GOAL, max_calls=6)
    assert failed["status"] == "BLOCKED" and failed["failure"]["message"] == "snapshot digest mismatch"
    assert failed["failure"]["details"]["external_ids"]["project_id"] == "project-existing"
    assert store.operations(failed["id"])[-1]["consumed"]
    workspace = engine.context(failed).workspace_root
    database = workspace / ".data/app.db"
    before = database.read_bytes()
    if tamper:
        (workspace / REPORT).write_text("changed after failure")
        with pytest.raises(ControlError) as error:
            engine.resume(failed["id"])
        assert error.value.category == "reconciliation"
        assert len(worker.calls) == 2
    else:
        resumed = engine.resume(failed["id"])
        assert resumed["status"] == "WAITING" and resumed["phase"] == "diagnose"
        assert resumed["contract"] == failed["contract"]
        assert [p for _, p in worker.calls] == ["diagnose", "acceptance", "diagnose"]
    assert database.read_bytes() == before
    assert store.workflow(failed["workflow"])["max_calls"] == 6


def retained_development_route(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    engine = Engine(root, store, ProjectConfig("fixture"), transport=Worker(),
                    print_fn=lambda *a: None)
    source = engine.workspaces.snapshot()
    parent = store.create_workflow("collab", GOAL, source, max_calls=12)
    # Seed an actually obsolete route; the current admission policy must reject it.
    from unittest.mock import patch
    from auto_agents.control.policy import WorkflowPolicy
    with patch.object(WorkflowPolicy, "route", return_value=None):
        child = store.child(parent, "run", "Perform the browser acceptance", source)
    workspace = engine.context(child).workspace_root
    docs = workspace / ".auto-agents/docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "requirements.md").write_text("Unrequested development requirements")
    database = workspace / ".data/app.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE receipts (external_id TEXT)")
        db.execute("INSERT INTO receipts VALUES ('keep-existing-request')")
    child = store.transition(child, "RUNNING", phase="prototype")
    operation = store.reserve(child, "model:prototype", 0, {"original": True}, model=True)
    failure = ControlError("stage_scope", "Worker changed protected runtime or operator paths",
                           category="model", details={"paths": [".data/app.db"]}).to_dict()
    store.settle(operation["id"], failure, state="FAILED")
    child = store.transition(child, "BLOCKED", failure=failure)
    parent = store.transition(store.work(parent["id"]), "BLOCKED",
                              failure={**failure, "child_id": child["id"]})
    return root, store, engine, parent, child, database


def test_retained_wrong_route_recovers_original_goal_without_erasing_receipts(tmp_path):
    root, store, engine, parent, child, database = retained_development_route(tmp_path)
    workflow = store.workflow(parent["workflow"])
    operations = store.operations(child["id"])
    before = database.read_bytes()
    recovered = engine.recover_acceptance_route(parent)

    assert recovered["status"] == "READY" and recovered["phase"] == "acceptance"
    assert recovered["contract"] == parent["contract"]
    assert not recovered["failure"] and "child" not in recovered["context"]
    assert store.work(child["id"])["status"] == "CANCELLED"
    assert store.workflow(parent["workflow"]) == workflow
    assert store.operations(child["id"]) == operations
    assert database.read_bytes() == before
    assert recovered["context"]["retained_acceptance_work"][0]["work_id"] == child["id"]
    assert not (engine.context(recovered).workspace_root / ".auto-agents/docs/requirements.md").exists()
    assert not (root / ".data").exists()
    # Idempotent preparation and ordinary resume cannot return to prototype.
    assert engine.recover_acceptance_route(recovered) == recovered
    engine.offline = True
    stopped = engine.resume(parent["id"])
    assert stopped["phase"] == "acceptance" and stopped["failure"]["category"] == "offline"
    assert not engine.transport.calls


def test_resume_reroutes_the_blocked_child_without_an_external_retry(tmp_path):
    _, store, engine, parent, child, database = retained_development_route(tmp_path)
    before = database.read_bytes()
    calls = store.workflow(parent["workflow"])["calls"]
    engine.offline = True
    result = engine.resume(parent["id"])
    assert result["phase"] == "acceptance" and result["failure"]["category"] == "offline"
    assert store.work(child["id"])["status"] == "CANCELLED"
    assert store.workflow(parent["workflow"])["calls"] == calls
    assert database.read_bytes() == before and not engine.transport.calls


@pytest.mark.parametrize("blocker", ["unknown", "product_change", "protected_change", "budget"])
def test_recovery_does_not_hide_unknown_results_or_real_scope_violations(tmp_path, blocker):
    _, store, engine, parent, child, database = retained_development_route(tmp_path)
    if blocker == "unknown":
        child = store.transition(child, "READY")
        child = store.transition(child, "RUNNING")
        op = store.reserve(child, "model:review", 0, {}, model=True)
        store.settle(op["id"], {"result": "unknown"}, state="UNKNOWN")
        child = store.transition(child, "BLOCKED")
    elif blocker == "product_change":
        (database.parents[1] / "value.py").write_text("VALUE = 2\n")
    else:
        failure = (ControlError("stage_scope", "Changed credentials", category="model",
                                details={"paths": [".env"]}) if blocker == "protected_change"
                   else ControlError("call_budget", "Budget exhausted", category="budget"))
        child = store.transition(child, "BLOCKED", failure=failure.to_dict())
    assert engine.recover_acceptance_route(parent) == parent
    assert store.work(child["id"])["status"] == "BLOCKED"
    assert database.exists()


def test_acceptance_blocker_cannot_escalate_to_development_through_fix(tmp_path):
    root = project(tmp_path)
    store = Store(root)

    class Escalating(Worker):
        def run(self, ctx, phase, *args, **kwargs):
            self.calls.append((ctx.work_id, phase))
            if phase == "diagnose":
                result = {"action": "fix", "issue": {"summary": "Observed acceptance blocker"}}
            elif phase == "classify":
                assert "Do not choose run_iteration" in args[0]
                result = {"decision": "run_iteration", "summary": "Implement a new feature"}
            else:
                raise AssertionError(phase)
            return {"text": json.dumps(result)}

    worker = Escalating()
    result = Engine(root, store, ProjectConfig("fixture"), transport=worker,
                    print_fn=lambda *a: None).start("collab", GOAL)
    assert result["status"] == "BLOCKED"
    assert all(work["mode"] != "run" for work in store.works())
    assert (root / "value.py").read_text() == "VALUE = 0\n"
