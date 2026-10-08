import json
from pathlib import Path
import pytest
from auto_agents.control import Store, Status
from auto_agents.control.engine import Engine
from auto_agents.control.api import status, snapshot
from auto_agents.models import ProjectConfig
from test_control_engine import project, Worker


def test_snapshot_is_one_repository_and_exact_state_not_all_history(tmp_path):
    root = project(tmp_path)
    store = Store(root)
    engine = Engine(
        root,
        store,
        ProjectConfig("test"),
        transport=Worker(),
        auto_approve=False,
        print_fn=lambda *a: None,
    )
    work = engine.start("run", "Set value")
    before = status(root)
    destination = tmp_path / "copy"
    receipt = snapshot(root, destination)
    assert receipt["state"]["identity"] == before["identity"]
    assert (destination / ".auto-agents/state/owned/objects.git").is_dir()
    assert not (destination / ".auto-agents/state/owned/workspaces").exists()
    copied = Store(destination)
    assert copied.work(work["id"])["contract"] == store.work(work["id"])["contract"]
    assert len(copied.operations()) == len(store.operations())


def test_successful_stage_transition_and_receipt_consumption_are_atomic(tmp_path):
    store = Store(tmp_path)
    work = store.create_workflow("fix", "goal", "source")
    work = store.transition(work, "RUNNING")
    op = store.reserve(work, "classify", 0, {}, model=True)
    store.settle(op["id"], {"reply": "complete"})
    with pytest.raises(Exception):
        store.transition(work, "READY", phase="implement", operation="missing")
    assert store.work(work["id"])["status"] == "RUNNING"
    assert not store.operations(work["id"])[0]["consumed"]
    store.transition(work, "READY", phase="implement", operation=op["id"])
    assert store.operations(work["id"])[0]["consumed"]


def test_snapshot_preserves_uncommitted_bytes_and_original_head(tmp_path):
    from auto_agents.control.workspace import git

    root = project(tmp_path)
    store = Store(root)
    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Worker(),
        auto_approve=False,
        print_fn=lambda *a: None,
    )
    work = engine.start("run", "Set value")
    ctx = engine.context(work)
    head = git(ctx.workspace_root, "rev-parse", "HEAD")
    (ctx.workspace_root / "value.py").write_text("VALUE = 2\n")
    destination = tmp_path / "snapshot"
    snapshot(root, destination)
    copied = Store(destination)
    isolated = Engine(
        destination,
        copied,
        ProjectConfig("fixture"),
        transport=Worker(),
        print_fn=lambda *a: None,
    )
    materialized = isolated.context(copied.work(work["id"])).workspace_root
    assert git(materialized, "rev-parse", "HEAD") == head
    assert (materialized / "value.py").read_text() == "VALUE = 2\n"
    assert git(materialized, "diff", "--name-only") == "value.py"


def test_checkpoint_binds_actual_child_and_never_restarts_a_fresh_goal(tmp_path):
    from auto_agents.control.api import checkpoint
    from auto_agents.control.types import ControlError

    root = project(tmp_path)
    store = Store(root)

    class Failure(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "implement":
                raise ControlError("bug", "engine error", category="engine")
            return super().run(ctx, phase, *a, **kw)

    engine = Engine(
        root,
        store,
        ProjectConfig("fixture"),
        transport=Failure(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    work = engine.start("collab", "Set value")
    fault = checkpoint(
        root,
        ["collab", "--project", str(root), "--goal", "Set value"],
        work["id"],
        ControlError("bug", "engine error", category="engine"),
    )
    token = json.loads(Path(fault["resume_token"]).read_text())
    assert token["work_id"] == work["failure"]["child_id"]
    assert token["argv"] == ["collab", "--project", str(root), "--session", work["id"]]
