import json, sqlite3
from pathlib import Path
from auto_agents.control import Store
from auto_agents.control.migration import migrate
from auto_agents.control.workspace import git


def test_once_import_preserves_goal_unknown_calls_and_blocks_unproved_completion(
    tmp_path,
):
    from auto_agents.business_state import BusinessStore

    root = tmp_path / "project"
    root.mkdir()
    git(root, "init")
    (root / "value.py").write_text("VALUE=0\n")
    git(root, "add", "value.py")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-m",
        "baseline",
    )
    old = BusinessStore(root)
    old.save(
        "sessions/original/session_state.json",
        {
            "session_id": "original",
            "mode": "fix",
            "goal": "original goal",
            "status": "verifying",
            "fix_verify_command": "python -m pytest -q tests/test_value.py",
            "current_attempt": 2,
        },
    )
    old.save(
        "workflows/wf-retained/workflow.json",
        {
            "workflow_id": "wf-retained",
            "root": {"kind": "fix", "native_id": "original"},
        },
    )
    old.reserve("first-call", "original", "classify", model=True)
    old.settle("first-call", {"ok": True, "summary": "saved reply"})
    old.reserve("unknown-call", "original", "classify", model=True)
    before = migrate(root, check=True)
    assert before["status"] == "blocked" and any(
        x["code"] == "outcome_unknown" for x in before["issues"]
    )
    result = migrate(root)
    assert result["migrated"] and Path(result["backup"], "business.sqlite3").exists()
    store = Store(root)
    assert store.work("wf-retained")["id"] == "original"
    assert store.contract(store.work("original")["contract"]).goal == "original goal"
    assert len(store.operations()) == 2
    assert store.operations()[-1]["state"] == "UNKNOWN"
    assert store.workflow(store.work("original")["workflow"])["calls"] >= 2
    assert migrate(root)["status"] == "already_current"


def test_missing_original_verification_target_is_migrated_as_blocked(tmp_path):
    from auto_agents.business_state import BusinessStore
    from test_control_engine import project

    root = project(tmp_path)
    old = BusinessStore(root)
    command = "python -m pytest -q tests/missing_original_contract.py"
    old.save(
        "sessions/retained/session_state.json",
        {
            "session_id": "retained",
            "mode": "fix",
            "goal": "Original user goal and constraints",
            "status": "verifying",
            "auto_approve": True,
            "fix_verify_command": command,
            "hard_ceiling": 7,
            "current_attempt": 3,
        },
    )
    report = migrate(root, check=True)
    assert any(
        x["code"].startswith("verification_target_missing:") for x in report["issues"]
    )
    result = migrate(root)
    assert result["migrated"] and result["status"] == "migrated_with_blocks"
    store = Store(root)
    work = store.work("retained")
    contract = store.contract(work["contract"])
    assert work["status"] == "BLOCKED" and work["failure"]["category"] == "migration"
    assert (
        contract.goal
        == contract.inputs["parent_goal"]
        == "Original user goal and constraints"
    )
    assert (
        contract.checks[0].command == command
        and contract.authorization["auto_approve"] is True
    )
    assert work["calls"] == 3 and work["max_calls"] == 7


def test_future_regression_target_does_not_block_preimplementation_import(tmp_path):
    from auto_agents.business_state import BusinessStore
    from test_control_engine import project

    root = project(tmp_path)
    old = BusinessStore(root)
    command = "python -m pytest -q tests/new_regression.py"
    old.save(
        "sessions/future/session_state.json",
        {
            "session_id": "future",
            "mode": "fix",
            "goal": "Add the requested regression for the bounded fix",
            "status": "conversing",
            "fix_verify_command": command,
            "hard_ceiling": 5,
            "current_attempt": 1,
        },
    )
    report = migrate(root, check=True)
    assert not any(
        x["code"].startswith("verification_target_missing:") for x in report["issues"]
    )
    migrate(root)
    store = Store(root)
    work = store.work("future")
    assert work["status"] == "READY" and work["phase"] == "classify"
    assert work["context"]["planned_test_targets"] == ["tests/new_regression.py"]
    assert store.contract(work["contract"]).checks[0].command == command
    assert work["calls"] == 1 and work["max_calls"] == 5


def test_private_received_candidate_keeps_shared_baseline_and_full_delta(tmp_path):
    from auto_agents.business_state import BusinessStore
    from test_control_engine import project

    root = project(tmp_path)
    base = git(root, "rev-parse", "HEAD")
    private = root / ".auto-agents/candidate-custody/parent/project"
    private.parent.mkdir(parents=True)
    import subprocess

    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(root), str(private)],
        check=True,
        capture_output=True,
    )
    (private / "value.py").write_text("VALUE = 1\n")
    git(private, "add", "value.py")
    git(
        private,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "received child",
    )
    received = git(private, "rev-parse", "HEAD")
    old = BusinessStore(root)
    old.save(
        "sessions/parent/session_state.json",
        {
            "session_id": "parent",
            "mode": "collab",
            "goal": "Original user acceptance",
            "status": "conversing",
            "candidate_custody": {
                "checkout": str(private),
                "base_revision": received,
                "contract_revision": base,
                "consumed_delivery": {
                    "revision": received,
                    "receipt_fingerprint": "retained",
                },
            },
        },
    )
    migrate(root)
    store = Store(root)
    work = store.work("parent")
    contract = store.contract(work["contract"])
    assert contract.source == base and contract.inputs["delivery_head"] == base
    assert work["context"]["candidate"]["source"] == base
    assert "value.py" in work["context"]["candidate"]["paths"]
    assert (root / "value.py").read_text() == "VALUE = 0\n"


def test_import_never_replaces_owned_documents_with_foreign_global_plan(tmp_path):
    from auto_agents.business_state import BusinessStore
    from test_control_engine import project

    root = project(tmp_path)
    docs = root / ".auto-agents/docs"
    docs.mkdir(parents=True)
    owned = {"tasks": [{"task_id": "owned", "title": "Original owned contract"}]}
    (docs / "task_plan.json").write_text(json.dumps(owned))
    old = BusinessStore(root)
    old.save(
        "task_plan.json",
        {"tasks": [{"task_id": "foreign", "title": "Historical different work"}]},
    )
    old.save(
        "sessions/owned/session_state.json",
        {
            "session_id": "owned",
            "mode": "fix",
            "goal": "Original fix",
            "status": "conversing",
            "fix_verify_command": "python -m pytest -q tests/new.py",
        },
    )
    migrate(root)
    store = Store(root)
    work = store.work("owned")
    path = (
        Path(store.meta("workspace:" + work["id"])["path"])
        / ".auto-agents/docs/task_plan.json"
    )
    assert json.loads(path.read_text()) == owned
