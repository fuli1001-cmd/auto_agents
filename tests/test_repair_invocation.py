"""Repeating the user's collab command recovers without repair administration."""
import os
from types import SimpleNamespace

import pytest

from auto_agents.config import save_session_state
from auto_agents.models import SessionState
from auto_agents.repair_control import Supervisor
from auto_agents.self_repair import SelfRepairDecision, SelfRepairTriageResult
from test_repair_control import configuration


@pytest.mark.parametrize("rerun", ["retry", "updated", "already_fixed"])
@pytest.mark.parametrize("failure_mode", ["exception", "controlled"])
def test_same_collab_command_continues_after_blocked_repair(tmp_path, monkeypatch, rerun, failure_mode):
    from auto_agents import cli, repair_client
    project = tmp_path / "project"
    project.mkdir()
    state = SessionState(session_id="6a0cedd6674d", mode="collab", status="executing",
                         goal="finish the original collab", auto_approve=True)
    save_session_state(project, state)
    config = configuration(tmp_path)
    supervisor = Supervisor(config)
    invocation = 0
    submitted = []
    resumed = []
    autonomy = SimpleNamespace(mode="max", to_dict=lambda: {"mode": "max"})

    class Orchestrator:
        def __init__(self, project_root, **kwargs):
            from auto_agents.reporting import get_reporter
            import sys
            self.project_root = project_root
            self.config = SimpleNamespace(execution=SimpleNamespace(autonomy=autonomy))
            self.reporter = get_reporter(project_root, sys.stderr)

        def _ensure_agent_instructions_synced(self):
            pass

        def _set_active_provider(self, provider):
            assert provider == "codex"

    class Session:
        def __init__(self, orchestrator, **kwargs):
            assert kwargs["auto_approve"]

        def resume(self, session_id):
            resumed.append(session_id)
            if invocation == 2 and rerun == "already_fixed":
                state.status = "completed"
                return state
            if failure_mode == 'controlled':
                state.status, state.resolution = 'blocked', 'engine_bug'
                save_session_state(project, state)
                return state
            raise RuntimeError("engine bug prevents the retained collab from continuing")

    def control_rpc(config, request, fds=()):
        assert request["op"] not in {"cancel", "resume"}
        response = supervisor.dispatch({"version": 1, "_peer_pid": os.getpid(), **request},
                                       [os.dup(fd) for fd in fds])
        if request["op"] == "submit":
            job = supervisor.store.job(response["job"])
            assert job["state"] == "queued"  # Never echo the old terminal failure.
            submitted.append(job)
            assert job["payload"]["invocation"]["session_id"] == state.session_id
        if request["op"] == "status":
            job = response["job"]
            # The expensive repair worker and recovered workflow are simulated;
            # CLI parsing, lock transfer, registration and retry decisions are real.
            result = {"ok": invocation == 2,
                      "error": "no concrete failure evidence authorizes another design"}
            supervisor.store.transition(job["id"], "blocked" if invocation == 1 else "completed", result)
            with supervisor.store.connect() as db:
                db.execute("UPDATE subscribers SET state=? WHERE id=?",
                           ("blocked" if invocation == 1 else "finished", request["subscriber"]))
            response = supervisor.dispatch({"version": 1, "_peer_pid": os.getpid(), **request}, [])
            supervisor.tick()
        elif request["op"] == "finish":
            supervisor.tick()
        return response

    monkeypatch.setattr(cli, "Orchestrator", Orchestrator)
    monkeypatch.setattr("auto_agents.session.Session", Session)
    monkeypatch.setattr(cli, "_safe_notify", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_promote_pending_self_repairs", lambda *a: None)
    monkeypatch.setattr(cli, "_deferred_release_enabled", lambda *a: False)
    monkeypatch.setattr(cli, "_triage_terminal_run_error", lambda *a: SelfRepairTriageResult(
        SelfRepairDecision(True, fingerprint="engine-bug"), source="test", reason="engine fault"))
    monkeypatch.setattr(repair_client, "enabled", lambda: True)
    monkeypatch.setattr(repair_client, "configure", lambda *a: config)
    monkeypatch.setattr(repair_client, "ensure_supervisor", lambda *a: None)
    monkeypatch.setattr(repair_client, "rpc", control_rpc)
    monkeypatch.setattr(repair_client, "git", lambda *a: "new-code" if invocation == 2 and rerun == "updated" else "base")
    command = ["collab", "--project", str(project), "--provider", "codex",
               "--auto-approve", "--session", state.session_id]
    try:
        invocation = 1
        assert cli.main(command) == 3
        invocation = 2
        assert cli.main(command) == 0
        assert resumed == [state.session_id, state.session_id]
        if rerun == "already_fixed":
            assert len(submitted) == 1
        elif rerun == "retry":
            assert submitted[0]["id"] == submitted[1]["id"]
            assert submitted[1]["generation"] == submitted[0]["generation"] + 1
        else:
            assert submitted[0]["id"] != submitted[1]["id"]
            assert supervisor.store.job(submitted[0]["id"])["state"] == "cancelled"
        assert not supervisor.registrations
    finally:
        for registration in supervisor.registrations.values():
            for fd in registration["fds"]:
                os.close(fd)
