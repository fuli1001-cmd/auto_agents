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




def test_cli_missing_session_does_not_initialize_or_triage_ambient_run(tmp_path):
    from unittest.mock import patch
    from auto_agents.control import Store
    from auto_agents.control.cli import main
    from auto_agents.models import ProjectConfig
    from auto_agents.config import save_project_config
    save_project_config(tmp_path,ProjectConfig('fixture'))
    store=Store(tmp_path)
    ambient=store.create_workflow('run','Unrelated goal','source')
    store.set_meta('active_root',ambient['id'])
    before=store.works()
    with patch('auto_agents.control.cli.Engine.resume') as resume,patch('auto_agents.control.cli.Engine.start') as start:
        code=main(['collab','--project',str(tmp_path),'--session','nonexistent','--auto-approve','--json'])
    assert code==3
    resume.assert_not_called();start.assert_not_called()
    assert store.works()==before and store.meta('active_root')==ambient['id']




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
    from unittest.mock import patch
    from auto_agents.control import Store
    from auto_agents.control.cli import main
    from auto_agents.models import ProjectConfig
    from auto_agents.config import save_project_config
    save_project_config(tmp_path,ProjectConfig('fixture'))
    store=Store(tmp_path)
    selected=store.create_workflow('collab','Original selected goal','source')
    selected=store.transition(selected,'BLOCKED',failure={'code':'state','category':'reconciliation','message':'Retained request'})
    ambient=store.create_workflow('run','Other run','source')
    store.set_meta('active_root',ambient['id'])
    with patch('auto_agents.control.cli.Engine.resume',return_value=selected) as resume:
        assert main(['collab','--project',str(tmp_path),'--session',selected['id'],'--json'])==3
    resume.assert_called_once_with(selected['id'])
    assert store.work(ambient['id'])==ambient
    assert store.contract(store.work(selected['id'])['contract']).goal=='Original selected goal'
