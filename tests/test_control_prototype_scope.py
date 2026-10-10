"""Prototype tooling stays private without widening the product write scope."""

import json

import pytest

from auto_agents.control import ControlError, Store
from auto_agents.control.engine import Engine
from auto_agents.control.quality import stage_writes, workspace_guard
from auto_agents.control.workspace import git
from auto_agents.models import ProjectConfig
from test_control_domains import Frontend
from test_control_engine import project


@pytest.mark.parametrize("directory", [".tmp", ".tmp-tests"])
@pytest.mark.parametrize("action", ["create", "modify", "delete"])
@pytest.mark.parametrize("variant", [False, True])
def test_prototype_allows_untracked_tooling_within_its_artifact_scope(
    tmp_path, directory, action, variant
):
    root = project(tmp_path)
    temporary = root / directory / "prototype-runtime/browser.json"
    temporary.parent.mkdir(parents=True)
    if action != "create":
        temporary.write_text("previous browser state")
    before = workspace_guard(root)
    if action == "delete":
        temporary.unlink()
    else:
        temporary.write_text("current browser state")
    prefix = ".auto-agents/docs/frontend_prototype"
    if variant:
        prefix += "_variants/candidate"
    artifact = root / prefix / "home.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html>Prototype</html>")
    stage_writes(
        before, workspace_guard(root), "prototype", (prefix,) if variant else ()
    )


@pytest.mark.parametrize("directory", [".tmp", ".tmp-tests"])
@pytest.mark.parametrize("action", ["modify", "delete"])
def test_prototype_scratch_exception_preserves_tracked_inputs(
    tmp_path, directory, action
):
    root = project(tmp_path)
    path = root / directory / "source.py"
    path.parent.mkdir()
    path.write_text("original tracked input")
    git(root, "add", "-f", path.relative_to(root).as_posix())
    before = workspace_guard(root)
    if action == "delete":
        path.unlink()
    else:
        path.write_text("changed tracked input")
    with pytest.raises(ControlError) as error:
        stage_writes(before, workspace_guard(root), "prototype")
    assert error.value.code == "stage_scope"
    assert path.relative_to(root).as_posix() in error.value.details["paths"]


@pytest.mark.parametrize(
    "name",
    [
        "value.py",
        ".data/app.db",
        ".tmp/.env",
        ".tmp-tests/.env.local",
        ".tmp/.auto-agents/state/control.json",
        ".auto-agents/state/control.json",
        ".auto-agents/docs/architecture.md",
        ".tmp-tools/tool.py",
    ],
)
def test_prototype_scratch_exception_preserves_other_boundaries(tmp_path, name):
    root = project(tmp_path)
    before = workspace_guard(root)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("unauthorized change")
    with pytest.raises(ControlError) as error:
        stage_writes(before, workspace_guard(root), "prototype")
    assert error.value.code == "stage_scope"
    assert name in error.value.details["paths"]


@pytest.mark.parametrize("kind", ["tracked_target", "outside", "root_alias"])
def test_prototype_scratch_exception_rejects_escaping_links(tmp_path, kind):
    root = project(tmp_path)
    before = workspace_guard(root)
    private = root / ".tmp"
    if kind == "root_alias":
        private.symlink_to(tmp_path, target_is_directory=True)
    else:
        private.mkdir()
        target = (
            root / "value.py" if kind == "tracked_target" else tmp_path / "outside"
        )
        (private / "SingletonLock").symlink_to(target)
    with pytest.raises(ControlError) as error:
        stage_writes(before, workspace_guard(root), "prototype")
    assert error.value.code == "stage_scope"


@pytest.mark.parametrize("retained", [False, True])
def test_prototype_tooling_reaches_approval_without_delivery_or_redispatch(
    tmp_path, monkeypatch, retained
):
    from auto_agents.control import quality

    root = project(tmp_path)
    store = Store(root)

    class Tools(Frontend):
        def run(self, ctx, phase, *args, **kwargs):
            if phase == "prototype":
                self.calls.append((ctx.work_id, phase))
                for name in (
                    ".tmp/prototype-runtime/Crashpad/settings.dat",
                    ".tmp/prototype-tools/tool.py",
                    ".tmp/prototype/first-desktop.png",
                    ".tmp-tests/test_prototype.py",
                ):
                    path = ctx.workspace_root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("private prototype tooling")
            result = super().run(ctx, phase, *args, **kwargs)
            if phase == "prototype":
                reply = json.loads(result["text"])
                report = (
                    ctx.workspace_root
                    / ".auto-agents/docs/frontend_prototype/evidence/results.json"
                )
                report.parent.mkdir()
                report.write_text('{"standalone": true}')
                reply["artifacts"].append(report.relative_to(ctx.workspace_root).as_posix())
                manifest = report.parent.parent / "manifest.json"
                payload = json.loads(manifest.read_text())
                payload["artifacts"] = reply["artifacts"]
                manifest.write_text(json.dumps(payload))
                result["text"] = json.dumps(reply)
            return result

    worker = Tools()
    config = ProjectConfig("fixture")
    config.frontend_design.mode = "off"
    engine = Engine(
        root, store, config, transport=worker, auto_approve=True,
        print_fn=lambda *args: None,
    )
    guard = quality.stage_writes

    def previous_guard(before, after, phase, scope=()):
        if phase == "prototype":
            before = {**before, "private_runtime_paths": []}
            after = {**after, "private_runtime_paths": []}
        return guard(before, after, phase, scope)

    with monkeypatch.context() as previous:
        if retained:
            previous.setattr(quality, "stage_writes", previous_guard)
        work = engine.start("run", "Set value")
    operation = next(
        o for o in store.operations(work["id"]) if o["kind"] == "model:prototype"
    )
    native = store.outcome(operation["id"])
    if retained:
        assert work["status"] == "BLOCKED" and work["failure"]["code"] == "stage_scope"
        assert native["native_state"] == "confirmed"
        assert native["validation"]["state"] == "rejected"
        calls = list(worker.calls)
        work = engine.resume(work["id"])
        assert worker.calls == calls
        assert store.outcome(operation["id"])["payload"] == native["payload"]
    assert work["status"] == "WAITING", work["failure"]
    assert work["context"]["approval"] == "prototype"
    assert [phase for _, phase in worker.calls] == ["clarify", "prototype"]
    assert store.workflow(work["workflow"])["calls"] == 2
    assert store.outcome(operation["id"])["validation"]["state"] == "accepted"
    workspace = engine.context(work).workspace_root
    assert (workspace / ".tmp/prototype-tools/tool.py").is_file()
    assert not any(
        name.startswith((".tmp/", ".tmp-tests/"))
        for name in work["context"]["candidate"]["paths"]
    )
    assert not any(
        name.startswith((".tmp/", ".tmp-tests/"))
        for name in git(workspace, "ls-files").splitlines()
    )
    assert not (root / ".tmp").exists()
    assert (root / "value.py").read_text() == "VALUE = 0\n"

    from auto_agents.control.prototypes import approve
    from auto_agents.frontend_design import validate_frontend_design_artifacts

    calls = list(worker.calls)
    approved = approve(engine, work, "initial")
    assert approved["approved_variant"] == "initial"
    lock = json.loads(
        (workspace / ".auto-agents/docs/frontend_design.lock.json").read_text()
    )
    assert validate_frontend_design_artifacts(workspace, lock, require_approved=True) == []
    evidence_ref = ".auto-agents/docs/frontend_prototype/evidence/results.json"
    assert evidence_ref in lock["artifact_sha256"]
    assert worker.calls == calls and store.workflow(work["workflow"])["calls"] == 2
    (workspace / evidence_ref).write_text("changed after approval")
    errors = validate_frontend_design_artifacts(workspace, lock, require_approved=True)
    assert any("drifted" in error for error in errors)
