"""Domain capabilities remain ordinary work of the single controller."""

import json
import sys
from pathlib import Path
import pytest
from auto_agents.control import Store, ControlError
from auto_agents.control.engine import Engine
from auto_agents.control import prototypes, release, cleanup
from auto_agents.control.workspace import git
from auto_agents.models import ProjectConfig, VerificationStep
from auto_agents.config import save_project_config
from auto_agents.control.cli import main
from test_control_engine import project, Worker


class Frontend(Worker):
    def run(self, ctx, phase, *a, **kw):
        if phase == "prototype":
            prefix = ".auto-agents/docs/frontend_prototype"
            if ctx.contract.inputs.get("variant_only"):
                prefix += "_variants/" + ctx.contract.inputs["variant_only"]
            path = ctx.workspace_root / prefix
            path.mkdir(parents=True, exist_ok=True)
            (path / "home.html").write_text(
                '<html><meta name="viewport" content="width=device-width">'
                + ctx.contract.goal
                + "</html>"
            )
            manifest = {
                "pages": [
                    {
                        "id": "home",
                        "title": "Home",
                        "route": "/",
                        "requirement_ids": ["REQ-001"],
                        "html_ref": prefix + "/home.html",
                    }
                ],
                "index_ref": prefix + "/home.html",
                "viewports": ["1440x900"],
            }
            (path / "manifest.json").write_text(json.dumps(manifest))
            return {
                "text": json.dumps(
                    {"artifacts": [prefix + "/home.html", prefix + "/manifest.json"]}
                )
            }
        result = super().run(ctx, phase, *a, **kw)
        if phase == "clarify":
            value = json.loads(result["text"])
            value["frontend"] = True
            result["text"] = json.dumps(value)
        return result


def test_variant_approval_copies_complete_package_and_preserves_budget(
    tmp_path, capsys
):
    root = project(tmp_path)
    config = ProjectConfig("fixture")
    config.frontend_design.mode = "off"
    save_project_config(root, config)
    store = Store(root)
    engine = Engine(
        root,
        store,
        config,
        transport=Frontend(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    work = engine.start("run", "Set value")
    assert work["status"] == "WAITING" and work["context"]["approval"] == "prototype"
    additional = prototypes.generate(
        engine, work, "Alternative layout", name="Alternative"
    )
    assert additional["status"] == "WAITING", additional["failure"]
    rows = prototypes.candidates(engine, additional)
    selected = rows[-1]["id"]
    used = store.workflow(work["workflow"])["calls"]
    maximum = store.workflow(work["workflow"])["max_calls"]
    assert (
        main(
            [
                "approve",
                "--project",
                str(root),
                "--session",
                work["id"],
                "--variant",
                selected,
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    approved = store.work(work["id"])
    ctx = engine.context(approved)
    assert (
        "Alternative layout"
        in (
            ctx.workspace_root / ".auto-agents/docs/frontend_prototype/home.html"
        ).read_text()
    )
    manifest = json.loads(
        (
            ctx.workspace_root / ".auto-agents/docs/frontend_prototype/manifest.json"
        ).read_text()
    )
    assert (
        manifest["pages"][0]["html_ref"]
        == ".auto-agents/docs/frontend_prototype/home.html"
    )
    assert not list(
        (ctx.workspace_root / ".auto-agents/docs/frontend_prototype_variants").glob(
            "*/home.html"
        )
    )
    assert (
        store.workflow(work["workflow"])["calls"] == used
        and store.workflow(work["workflow"])["max_calls"] == maximum
    )
    from auto_agents.frontend_design import validate_frontend_design_artifacts

    lock = json.loads(
        (ctx.workspace_root / ".auto-agents/docs/frontend_design.lock.json").read_text()
    )
    assert not validate_frontend_design_artifacts(
        ctx.workspace_root, lock, require_approved=True
    )


def test_changed_candidate_requires_another_explicit_decision(tmp_path):
    root = project(tmp_path)
    config = ProjectConfig("fixture")
    config.frontend_design.mode = "off"
    store = Store(root)
    engine = Engine(
        root,
        store,
        config,
        transport=Frontend(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    work = engine.start("run", "Set value")
    ctx = engine.context(work)
    rows = prototypes.candidates(engine, work)
    work = store.transition(
        work, "WAITING", context={**work["context"], "variants": rows}
    )
    (ctx.workspace_root / ".auto-agents/docs/frontend_prototype/home.html").write_text(
        "different"
    )
    with pytest.raises(ControlError):
        prototypes.approve(engine, work, "initial")
    assert store.work(work["id"])["status"] == "WAITING"


def test_release_worker_uses_same_store_and_has_no_model_repair(tmp_path):
    root = project(tmp_path)
    config = ProjectConfig("fixture")
    (root / ".conda").symlink_to(sys.prefix, target_is_directory=True)
    config.gates.steps = [
        VerificationStep(
            runner="pytest",
            kind="test",
            targets=["tests/test_value.py"],
            levels=["release"],
            proof_id="release.value",
            command=sys.executable + " -m pytest -q tests/test_value.py",
        )
    ]
    config.gates.release_worker.auto_start = False
    save_project_config(root, config)
    store = Store(root)
    worker = Worker()
    engine = Engine(
        root,
        store,
        config,
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    work = engine.start("fix", "Set value")
    assert work["status"] == "COMPLETED", work["failure"]
    queued = store.work(work["result"]["release_work_id"])
    assert queued["status"] == "READY"
    revision = work["result"]["delivery"]["revision"]
    assert release.attest(store, revision)["ok"] is False
    calls = store.workflow(work["workflow"])["calls"]
    assert release.worker(root, once=True) == 0
    proof = store.work(queued["id"])
    assert proof["status"] == "COMPLETED", proof["failure"]
    assert proof["calls"] == 0 and store.workflow(work["workflow"])["calls"] == calls
    assert release.attest(store, revision)["ok"] is True
    cleanup.collect(store, git_gc=True)
    assert not list((root / ".auto-agents/state/owned/workspaces").iterdir())
