import json
from pathlib import Path
from auto_agents.control import Store
from auto_agents.control.engine import Engine
from auto_agents.models import ProjectConfig
from test_control_engine import project, Worker


class ParallelWorker(Worker):
    def run(self, ctx, phase, prompt, output, **kwargs):
        if phase == "plan":
            tasks = []
            for name in ("left", "right"):
                tasks.append(
                    {
                        "task_id": name,
                        "goal": "Implement " + name,
                        "paths": [name + ".py", "tests/test_" + name + ".py"],
                        "depends_on": [],
                        "requirement_ids": ["REQ-001"],
                        "checks": [
                            {
                                "command": "python -m pytest -q tests/test_"
                                + name
                                + ".py"
                            }
                        ],
                    }
                )
            data = {
                "tasks": tasks,
                "checks": [{"command": "python -m pytest -q tests"}],
            }
        elif phase == "implement":
            name = ctx.contract.inputs["planned_task"]
            root = ctx.workspace_root
            (root / (name + ".py")).write_text("VALUE = 1\n")
            (root / "tests").mkdir(exist_ok=True)
            (root / "tests" / ("test_" + name + ".py")).write_text(
                "from " + name + " import VALUE\ndef test_value(): assert VALUE == 1\n"
            )
            data = {"summary": "implemented " + name}
        else:
            return super().run(ctx, phase, prompt, output, **kwargs)
        self.calls.append((ctx.work_id, phase))
        output.write_text(json.dumps(data))
        return {"text": json.dumps(data), "provider": "fixture"}


def test_parallel_children_have_distinct_workspaces_and_merge_verified_results(
    tmp_path,
):
    root = project(tmp_path)
    store = Store(root)
    config = ProjectConfig("parallel")
    config.execution.parallel_tasks.workers = 2
    engine = Engine(
        root,
        store,
        config,
        transport=ParallelWorker(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    result = engine.start("run", "Implement two independent values")
    assert result["status"] == "COMPLETED", result["failure"]
    assert (root / "left.py").is_file() and (root / "right.py").is_file()
    children = [w for w in store.works() if w["parent"]]
    assert len(children) == 2 and all(w["status"] == "COMPLETED" for w in children)
    assert len({store.meta("workspace:" + w["id"])["source"] for w in children}) == 1


import pytest


@pytest.mark.parametrize("point", ["after_apply", "after_commit"])
def test_interrupted_integration_reuses_completed_children_without_model_calls(
    tmp_path, monkeypatch, point
):
    import auto_agents.control.workspace as workspace

    root = project(tmp_path)
    store = Store(root)
    config = ProjectConfig("integration")
    config.execution.parallel_tasks.workers = 2
    worker = ParallelWorker()
    engine = Engine(
        root,
        store,
        config,
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    original = workspace.git
    interrupted = False

    def crash(path, *args, **kw):
        nonlocal interrupted
        integration = "commit" in args and any(
            str(a).startswith("integrate: ") for a in args
        )
        if integration and not interrupted:
            interrupted = True
            if point == "after_commit":
                original(path, *args, **kw)
            raise RuntimeError("Injected process loss during local integration")
        return original(path, *args, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(workspace, "git", crash)
        with pytest.raises(RuntimeError, match="Injected process loss"):
            engine.start("run", "Implement two independent values")
    retained = store.work(store.meta("active_root"))
    children = [w for w in store.works() if w["parent"] == retained["id"]]
    assert len(children) == 2 and all(w["status"] == "COMPLETED" for w in children)
    implementations = [call for call in worker.calls if call[1] == "implement"]
    used = store.workflow(retained["workflow"])["calls"]
    result = engine.resume(retained["id"])
    assert result["status"] == "COMPLETED", result["failure"]
    assert implementations == [call for call in worker.calls if call[1] == "implement"]
    assert store.workflow(retained["workflow"])["calls"] >= used
    assert (
        (root / "left.py").read_text()
        == (root / "right.py").read_text()
        == "VALUE = 1\n"
    )
