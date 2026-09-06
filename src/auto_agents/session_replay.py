"""Subprocess probe of a session's deterministic resume boundary.

The engine root is an explicit argument so the identical driver can exercise
both the base revision and its candidate. No model call is executed by a probe.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    engine, target, session_id, mode = sys.argv[1:5]
    sys.path.insert(0, str(Path(engine) / "src"))
    from auto_agents.config import load_session_state
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.workflow_runtime import WorkflowCoordinator

    class NextProviderBoundary(BaseException):
        pass

    def no_provider(*args, **kwargs):
        raise NextProviderBoundary()

    project = Path(target)
    try:
        # Older revisions have no recovery preflight; exercise their real load.
        import auto_agents.cli as cli
        prepare = getattr(cli, "_prepare_explicit_session", None)
        if prepare is not None:
            prepare(project, session_id, mode)
        else:
            load_session_state(project, session_id)
        orchestrator = Orchestrator(project)
        orchestrator._call_with_failover = no_provider
        session = Session(orchestrator, mode=mode, auto_approve=True)
        coordinator = WorkflowCoordinator(orchestrator, auto_approve=True)
        session._coordinator = coordinator
        session._coordinator_managed = True
        state = session.resume(session_id)
        payload = {"ok": state.status == "completed", "status": state.status,
                   "error": state.resolution, "session_id": state.session_id}
    except NextProviderBoundary:
        payload = {"ok": True, "status": "next_provider_boundary", "session_id": session_id}
    except Exception as error:
        payload = {"ok": False, "status": "failed", "error": str(error), "error_type": type(error).__name__}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
