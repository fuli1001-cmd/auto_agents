import json
from pathlib import Path
import pytest
from auto_agents.control.cli import main
from auto_agents.control import Store
from auto_agents.control.engine import Engine
from auto_agents.models import ProjectConfig
from auto_agents.config import save_project_config
from test_control_engine import Worker, project


def test_public_init_status_and_storage_are_same_schema(tmp_path, capsys):
    root = tmp_path / "empty"
    assert main(["init", "--project", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == 2
    for command in (["status"], ["business-status"], ["storage", "maintain"]):
        assert (
            main(
                [*command, "--project", str(root), "--json"]
                if command[0] != "storage"
                else [*command, "--project", str(root)]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["ok"] is True
    with Store(root).connect() as db:
        assert "records" not in {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_frontend_prototype_is_mandatory_and_approval_preserves_caps(tmp_path, capsys):
    root = project(tmp_path)
    config = ProjectConfig("fixture")
    save_project_config(root, config)

    class Frontend(Worker):
        def run(self, ctx, phase, prompt, output, **kwargs):
            if phase == "prototype":
                path = (
                    ctx.workspace_root
                    / ".auto-agents/docs/frontend_prototype/home.html"
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    '<html><meta name="viewport" content="width=device-width">Prototype</html>'
                )
                manifest = {
                    "pages": [
                        {
                            "id": "home",
                            "title": "Home",
                            "route": "/",
                            "requirement_ids": ["REQ-001"],
                            "html_ref": ".auto-agents/docs/frontend_prototype/home.html",
                        }
                    ],
                    "index_ref": ".auto-agents/docs/frontend_prototype/home.html",
                    "viewports": ["1440x900"],
                }
                (path.parent / "manifest.json").write_text(json.dumps(manifest))
                data = {
                    "artifacts": [
                        ".auto-agents/docs/frontend_prototype/home.html",
                        ".auto-agents/docs/frontend_prototype/manifest.json",
                    ]
                }
                return {"text": json.dumps(data)}
            result = super().run(ctx, phase, prompt, output, **kwargs)
            if phase == "clarify":
                data = json.loads(result["text"])
                data["frontend"] = True
                result["text"] = json.dumps(data)
            return result

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
    calls = store.workflow(work["workflow"])["calls"]
    assert (
        main(
            [
                "approve",
                "--project",
                str(root),
                "--session",
                work["id"],
                "--gate",
                "prototype",
                "--json",
            ]
        )
        == 0
    )
    ready = store.work(work["id"])
    assert ready["status"] == "READY" and "prototype" in ready["context"]["approvals"]
    assert store.workflow(work["workflow"])["calls"] == calls
    assert ready["max_calls"] == work["max_calls"]


def test_unborn_project_delivers_without_sweeping_user_index(tmp_path):
    root = tmp_path / "new"
    root.mkdir()
    (root / "spec.md").write_text("Set a value")
    engine = Engine(
        root,
        Store(root),
        ProjectConfig("fixture"),
        transport=Worker(),
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    work = engine.start("fix", "Set value")
    assert work["status"] == "COMPLETED", work["failure"]
    from auto_agents.control.workspace import git

    assert "spec.md" not in git(root, "ls-files").splitlines()
    assert (root / "spec.md").read_text() == "Set a value"


@pytest.mark.parametrize("mode", ["run", "fix", "collab"])
def test_all_public_modes_share_the_controller(tmp_path, capsys, monkeypatch, mode):
    from auto_agents.control import cli

    root = project(tmp_path)
    save_project_config(root, ProjectConfig("fixture"))
    actual = Engine
    monkeypatch.setattr(
        cli, "Engine", lambda *a, **kw: actual(*a, **kw, transport=Worker())
    )
    code = main(
        [
            mode,
            "--project",
            str(root),
            "--goal",
            "Set value to one",
            "--auto-approve",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["work"]["status"] == "COMPLETED", result
    assert (root / "value.py").read_text() == "VALUE = 1\n"


def test_legacy_writer_cannot_create_a_second_state_machine(tmp_path):
    from auto_agents.business_state import BusinessStore, BusinessStateError

    store = Store(tmp_path)
    with pytest.raises(BusinessStateError):
        BusinessStore(tmp_path)
    with store.connect() as db:
        assert "records" not in {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
