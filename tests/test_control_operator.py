"""Operator CLI actions retain the single controller's authority."""

import json
from auto_agents.control import Store
from auto_agents.control.engine import Engine
from auto_agents.control.cli import main
from auto_agents.control.workspace import git
from auto_agents.config import save_project_config
from auto_agents.models import ProjectConfig, PersistenceTargetConfig
from test_control_engine import project, Worker


def test_answer_continues_owned_root_without_refunding_calls(
    tmp_path, monkeypatch, capsys
):
    from auto_agents.control import cli

    root = project(tmp_path)
    config = ProjectConfig("operator")
    save_project_config(root, config)
    store = Store(root)

    class Question(Worker):
        def run(self, ctx, phase, *a, **kw):
            if phase == "classify" and not store.work(ctx.work_id)["context"].get(
                "operator_answers"
            ):
                return {
                    "text": json.dumps(
                        {"decision": "need_user", "question": "Confirm required value"}
                    )
                }
            return super().run(ctx, phase, *a, **kw)

    worker = Question()
    actual = Engine
    monkeypatch.setattr(
        cli, "Engine", lambda *a, **kw: actual(*a, **kw, transport=worker)
    )
    engine = actual(
        root,
        store,
        config,
        transport=worker,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    waiting = engine.start("fix", "Set value", max_calls=7)
    assert waiting["status"] == "WAITING"
    used = store.workflow(waiting["workflow"])["calls"]
    assert (
        main(
            [
                "answer",
                "--project",
                str(root),
                "--session",
                waiting["id"],
                "--value",
                "yes",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    completed = store.work(waiting["id"])
    assert completed["status"] == "COMPLETED"
    assert store.workflow(completed["workflow"])["max_calls"] == 7
    assert store.workflow(completed["workflow"])["calls"] > used


def persistence_fixture(tmp_path):
    root = project(tmp_path)
    config = ProjectConfig("persistence")
    config.persistence.targets = [
        PersistenceTargetConfig(
            target_id="local-test",
            environment="test",
            kind="local_file",
            locator={"path": ".data/app.db"},
        )
    ]
    save_project_config(root, config)
    docs = root / ".auto-agents/docs"
    docs.mkdir(parents=True)
    change = {
        "strategy": "initial_schema",
        "decision_id": "PERSIST-001",
        "target_ids": ["REQ-001"],
        "to_version": "1",
        "migration_artifacts": ["migrations/0001.py"],
        "legacy_fixture_refs": [],
    }
    task = {
        "task_id": "task-1",
        "title": "Initial schema",
        "goal": "Initial schema",
        "description": "Create schema",
        "acceptance": ["Schema is explicit"],
        "status": "pending",
        "commit_message": "feat: schema",
        "persistence_change": change,
    }
    (docs / "requirements_trace.json").write_text(
        json.dumps(
            {
                "version": 1,
                "requirements": [],
                "persistence_decisions": [
                    {
                        "id": "PERSIST-001",
                        "target_ids": ["REQ-001"],
                        "strategy": "initial_schema",
                        "source": "Legacy generated plan",
                        "status": "active",
                    }
                ],
            }
        )
    )
    (docs / "task_plan.json").write_text(
        json.dumps({"persistence_contract_version": 1, "tasks": [task]})
    )
    store = Store(root)
    engine = Engine(root, store, config, transport=Worker(), print_fn=lambda *a: None)
    source = engine.workspaces.snapshot(allow_dirty=True)
    work = store.create_workflow(
        "run", "Preserve original schema goal", source, max_calls=9
    )
    work = store.transition(
        work,
        "BLOCKED",
        phase="implement",
        context={"tasks": [task]},
        failure={
            "code": "persistence",
            "category": "operator",
            "message": "Bind legacy metadata",
        },
    )
    return root, store, engine, work


def test_persistence_rebind_updates_owned_docs_without_flat_state_or_calls(
    tmp_path, capsys
):
    root, store, engine, work = persistence_fixture(tmp_path)
    before = (root / ".auto-agents/docs/task_plan.json").read_bytes()
    assert (
        main(
            [
                "persistence-rebind",
                "--project",
                str(root),
                "--decision",
                "PERSIST-001",
                "--target",
                "local-test",
            ]
        )
        == 0
    )
    capsys.readouterr()
    retained = store.work(work["id"])
    workspace = engine.context(retained).workspace_root
    assert retained["context"]["tasks"][0]["persistence_change"]["target_ids"] == [
        "local-test"
    ]
    assert json.loads(
        (workspace / ".auto-agents/docs/requirements_trace.json").read_text()
    )["persistence_decisions"][0]["target_ids"] == ["local-test"]
    assert retained["contract"] == work["contract"] and retained["status"] == "BLOCKED"
    assert retained["calls"] == 0 and store.workflow(work["workflow"])["max_calls"] == 9
    assert (root / ".auto-agents/docs/task_plan.json").read_bytes() == before
    assert not (root / ".auto-agents/state/run_state.json").exists()
    assert all(o["consumed"] for o in store.operations())


def test_persistence_rebind_invalid_target_keeps_docs_and_ledger(tmp_path, capsys):
    root, store, engine, work = persistence_fixture(tmp_path)
    workspace = engine.context(work).workspace_root
    before = (workspace / ".auto-agents/docs/requirements_trace.json").read_bytes()
    assert (
        main(
            [
                "persistence-rebind",
                "--project",
                str(root),
                "--decision",
                "PERSIST-001",
                "--target",
                "foreign",
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert (
        workspace / ".auto-agents/docs/requirements_trace.json"
    ).read_bytes() == before
    assert (
        not store.operations()
        and store.work(work["id"])["revision"] == work["revision"]
    )


def test_persistence_upgrade_preserves_goal_status_budget_and_database(
    tmp_path, capsys
):
    root, store, engine, work = persistence_fixture(tmp_path)
    database = root / ".data/app.db"
    database.parent.mkdir()
    database.write_bytes(b"operator database")
    assert (
        main(
            [
                "persistence-upgrade-contract",
                "--project",
                str(root),
                "--decision-policy",
                "PERSIST-001:initialize:not_applicable",
            ]
        )
        == 0
    )
    capsys.readouterr()
    retained = store.work(work["id"])
    trace = json.loads(
        (
            engine.context(retained).workspace_root
            / ".auto-agents/docs/requirements_trace.json"
        ).read_text()
    )
    assert trace["persistence_contract_version"] == 2
    assert trace["persistence_decisions"][0]["storage_transition"] == "initialize"
    assert (
        retained["status"] == work["status"]
        and retained["contract"] == work["contract"]
    )
    assert retained["calls"] == 0 and store.workflow(work["workflow"])["max_calls"] == 9
    assert database.read_bytes() == b"operator database"
    assert not (root / ".auto-agents/state/run_state.json").exists()
