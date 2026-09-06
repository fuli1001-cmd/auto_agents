from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from auto_agents.models import SessionState
from auto_agents.session_recovery import (
    SessionRecoveryError, apply_session_recovery, plan_session_recovery,
)
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore


def _blob(root: Path, value: dict) -> Path:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = root / ".auto-agents/state/checkpoint_blobs" / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _fixture(root: Path) -> tuple[dict, Path]:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    store = WorkflowStore(root)
    snapshot = store.create_root(WorkflowRef("collab", "session-1"), workflow_id="wf-1", activate=False)
    session = SessionState(
        session_id="session-1", mode="collab", workflow_id="wf-1", status="executing",
        goal="continue the original goal", updated_at=snapshot.created_at,
    ).to_dict()
    _blob(root, session)
    _blob(root, snapshot.to_dict())
    event_path = next((store.workflow_root("wf-1") / "events").glob("*.json"))
    event_blob = _blob(root, json.loads(event_path.read_bytes()))
    for p in (event_path, store.snapshot_path("wf-1")):
        p.unlink()
    return session, event_blob


def test_restoration_preserves_root_and_goal_and_does_not_touch_ambient_run(tmp_path):
    session, _ = _fixture(tmp_path)
    run = tmp_path / ".auto-agents/state/run_state.json"
    run.write_text('{"run_id":"unrelated","status":"blocked"}')
    product = tmp_path / "product.py"
    product.write_text("dirty user work\n")
    plan = plan_session_recovery(tmp_path, "session-1", "collab")
    assert not (tmp_path / ".auto-agents/state/sessions/session-1/session_state.json").exists()
    apply_session_recovery(tmp_path, plan)
    apply_session_recovery(tmp_path, plan)
    restored = json.loads((tmp_path / ".auto-agents/state/sessions/session-1/session_state.json").read_bytes())
    assert restored == session
    assert product.read_text() == "dirty user work\n"
    assert json.loads(run.read_bytes()) == {"run_id": "unrelated", "status": "blocked"}
    assert len(WorkflowStore(tmp_path).events("wf-1")) == 1


def test_missing_or_corrupt_event_cannot_restore_empty_session(tmp_path):
    _, event_blob = _fixture(tmp_path)
    event_blob.write_text("{}")
    with pytest.raises(SessionRecoveryError, match="no complete recovery boundary"):
        plan_session_recovery(tmp_path, "session-1", "collab")
    assert not (tmp_path / ".auto-agents/state/sessions/session-1").exists()


def test_restoration_refuses_different_existing_state_before_any_write(tmp_path):
    _fixture(tmp_path)
    plan = plan_session_recovery(tmp_path, "session-1", "collab")
    path = tmp_path / ".auto-agents/state/workflows/wf-1/workflow.json"
    path.write_text('{"workflow_id":"wf-1","status":"completed"}')
    with pytest.raises(SessionRecoveryError, match="overwrite"):
        apply_session_recovery(tmp_path, plan)
    assert not (tmp_path / ".auto-agents/state/sessions/session-1").exists()


def test_unrelated_workflow_and_wrong_mode_never_supply_recovery(tmp_path):
    _fixture(tmp_path)
    with pytest.raises(SessionRecoveryError):
        plan_session_recovery(tmp_path, "other-session", "collab")
    with pytest.raises(SessionRecoveryError, match="not fix"):
        plan_session_recovery(tmp_path, "session-1", "fix")


def test_cli_resume_command_retains_collab_identity_and_options(tmp_path):
    from argparse import Namespace
    from auto_agents.cli import _run_command_for_self_repair_resume
    args = Namespace(command="collab", project=str(tmp_path), session="session-1", provider="codex", auto_approve=True)
    command = _run_command_for_self_repair_resume(args)
    assert command[2] == "collab"
    assert command[command.index("--session") + 1] == "session-1"
    assert command[command.index("--provider") + 1] == "codex"
    assert "--auto-approve" in command


def test_cli_missing_session_does_not_initialize_or_triage_ambient_run(tmp_path):
    from unittest.mock import patch
    from auto_agents.cli import main
    from auto_agents.models import RunState
    _fixture(tmp_path)
    path = tmp_path / ".auto-agents/state/run_state.json"
    raw = json.dumps(RunState("unrelated", status="blocked", active_blocker={"owner": "auto_agents", "category": "old_failure"}).to_dict())
    path.write_text(raw)
    with patch("auto_agents.cli.Orchestrator") as orchestrator, patch("auto_agents.cli._triage_terminal_run_error") as triage:
        code = main(["collab", "--project", str(tmp_path), "--session", "nonexistent", "--auto-approve"])
    assert code == 3
    orchestrator.assert_not_called()
    triage.assert_not_called()
    assert path.read_text() == raw


def test_session_triage_ignores_run_outside_its_handoff_chain(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch
    from auto_agents.cli import _triage_terminal_run_error
    from auto_agents.models import RunState
    from auto_agents.self_repair import SelfRepairDecision, SelfRepairTriageResult
    session, _ = _fixture(tmp_path)
    path = tmp_path / ".auto-agents/state/run_state.json"
    path.write_text(json.dumps(RunState("other-run", status="blocked").to_dict()))
    orchestrator = SimpleNamespace(_invocation_context={"session_id": "session-1", "command": "collab", "workflow_id": "wf-1"})
    result = SelfRepairTriageResult(SelfRepairDecision(False), source="test", reason="test")
    with patch("auto_agents.cli.adjudicate_auto_agents_error", return_value=result) as diagnose:
        _triage_terminal_run_error(tmp_path, orchestrator, RuntimeError("session failure"))
    assert diagnose.call_args.kwargs["state"] is None
    assert orchestrator._invocation_context["run_id"] == ""


def test_recovery_can_use_committed_history_without_checkpoint_blobs(tmp_path):
    _fixture(tmp_path)
    initial = plan_session_recovery(tmp_path, "session-1", "collab")
    apply_session_recovery(tmp_path, initial)
    for args in (["config", "user.email", "test@example.com"], ["config", "user.name", "Test"],
                 ["add", ".auto-agents/state/sessions", ".auto-agents/state/workflows"],
                 ["commit", "-qm", "durable workflow"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    for relative in initial.files:
        (tmp_path / relative).unlink()
    for path in (tmp_path / ".auto-agents/state/checkpoint_blobs").glob("*/*"):
        path.unlink()
    recovered = plan_session_recovery(tmp_path, "session-1", "collab")
    assert recovered.files == initial.files
    assert all(source.startswith("git:") for source in recovered.sources.values())


def test_old_interruption_cannot_retarget_explicit_session(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from auto_agents.cli import _reconcile_session_interruption
    session, _ = _fixture(tmp_path)
    apply_session_recovery(tmp_path, plan_session_recovery(tmp_path, "session-1", "collab"))
    store = WorkflowStore(tmp_path)
    store.create_root(WorkflowRef("run", "other-run"), workflow_id="wf-other")
    coordinator = SimpleNamespace(store=store, reconcile_interruption=Mock())
    state = SessionState.from_dict(session)
    _reconcile_session_interruption(coordinator, {"owner": {"subject_id": "other-run"}}, state)
    coordinator.reconcile_interruption.assert_not_called()
    assert store.active().workflow_id == "wf-other"
    payload = {"owner": {"subject_id": "session-1"}}
    _reconcile_session_interruption(coordinator, payload, state)
    coordinator.reconcile_interruption.assert_called_once_with(payload)
    assert store.active().workflow_id == "wf-1"
