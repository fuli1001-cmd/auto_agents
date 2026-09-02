from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from auto_agents.config import bootstrap_project, create_session, save_session_state
from auto_agents.health_control import (
    HealthActionRecord,
    HealthActionStore,
    evidence_digest,
)
from auto_agents.health_watchdog import IndependentHealthAuditor
from auto_agents.io_utils import read_json, write_text
from auto_agents.session_health import (
    SESSION_PROGRESS_SCHEMA_VERSION,
    build_session_progress,
)


def _identity(state, run_token: str) -> dict[str, object]:
    payload = state.to_dict()
    progress = build_session_progress(payload)
    return {
        "run_token": run_token,
        "progress_schema_version": SESSION_PROGRESS_SCHEMA_VERSION,
        "state_digest": evidence_digest(payload),
        "progress_digest": evidence_digest(progress),
        "progress": progress,
    }


def test_progress_invalidates_pending_goal_stall_before_foreground_raise() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary) / "project"
        bootstrap_project(project, "demo")
        state = create_session(project, "fix")
        state.goal = "repair"
        state.status = "executing"
        save_session_state(project, state)
        old_identity = _identity(state, "token")
        store = HealthActionStore(project, "fix", state.session_id)
        store.append(
            HealthActionRecord(
                request_id="stale-goal-stall",
                action="diagnose",
                reason="health_anomaly:goal_stalled",
                source="health_sidecar",
                run_token="token",
                subject_id=state.session_id,
                evidence=old_identity,
            )
        )

        state.current_attempt = 1
        state.execution_log.append({"action": "fix", "attempt": 1})
        state.resolution = "commit_failed"
        state.status = "failed"
        save_session_state(project, state)

        assert store.next_pending(
            run_token="token",
            progress_identity=_identity(state, "token"),
        ) is None
        payload = read_json(store.path, default={})
        request = payload["requests"][0]
        assert request["state"] == "superseded"
        assert "progress advanced" in request["detail"]


def _session_auditor(project: Path):
    state = create_session(project, "fix")
    state.goal = "repair"
    state.status = "executing"
    save_session_state(project, state)
    manifest = {
        "run_token": "token",
        "workflow_kind": "fix",
        "subject_id": state.session_id,
        "process_phase": "fix",
    }
    auditor = IndependentHealthAuditor(project, manifest)
    auditor.previous_session = build_session_progress(state.to_dict())
    auditor.previous_session_activity = "prior-activity"
    auditor.last_session_progress_at = 0.0
    write_text(
        project
        / ".auto-agents"
        / "state"
        / "sessions"
        / state.session_id
        / "outputs"
        / "provider-progress.txt",
        "provider is active",
    )
    return state, manifest, auditor


def test_live_supervised_provider_defers_session_goal_stall() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary) / "project"
        bootstrap_project(project, "demo")
        state, manifest, auditor = _session_auditor(project)
        manifest["active_operation"] = {
            "kind": "provider",
            "label": "fix-1",
            "heartbeat_epoch": 1_000.0,
        }

        with patch("auto_agents.health_watchdog.time.time", return_value=1_000.0):
            auditor.observe_once(manifest)

        assert auditor.actions.next_pending(run_token="token") is None
        snapshot = read_json(
            project
            / ".auto-agents"
            / "state"
            / "sessions"
            / state.session_id
            / "health"
            / "auditor-snapshot.json",
            default={},
        )
        assert snapshot["active_operation_live"] is True


def test_inactive_unchanged_session_still_requests_goal_stall() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary) / "project"
        bootstrap_project(project, "demo")
        _state, manifest, auditor = _session_auditor(project)

        with patch("auto_agents.health_watchdog.time.time", return_value=1_000.0):
            auditor.observe_once(manifest)

        request = auditor.actions.next_pending(run_token="token")
        assert request is not None
        assert request["reason"] == "health_anomaly:goal_stalled"
