"""Trusted subprocess for a supervisor-bound verification request."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_agents import artifact_temp as tempfile
from auto_agents.repair_control import atomic_json
from auto_agents.managed_verification import execute_engine, selected_tests


def execute_project(payload):
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.managed_verification import project_focused
    workspace = Path(payload["workspace"])
    selected_tests(workspace, payload["tests"])
    return project_focused(Orchestrator(workspace), payload["tests"], fresh=payload.get("fresh", False), sandboxed=True)


def main():
    path = Path(sys.argv[1])
    payload = json.loads(path.read_text())
    from auto_agents.artifact_runtime import activate, track
    activate(project=payload.get("source") if not payload.get("engine") else None,
             process_control=path.parent / "processes.json")
    track(path.parent, "evidence")
    from auto_agents.process_supervision import ACTIVE_PROCESSES
    ACTIVE_PROCESSES.configure(path.parent, path.parent.name, path.parent / "processes.json")
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        workspace = Path(payload["workspace"])
        info = workspace.stat()
        if workspace.resolve() != workspace or [info.st_dev, info.st_ino] != payload["inode"]:
            raise RuntimeError("verification workspace was replaced")
        if payload["engine"]:
            result = execute_engine(workspace, tests=payload["tests"], level=payload["level"],
                fresh=payload.get("fresh", False), repository=payload["repository"], python=payload["python"], real_project=payload["source"])
        else:
            result = execute_project(payload)
    except KeyboardInterrupt:
        result = {"ok": False, "error": "verification cancelled"}
    except Exception as error:
        from auto_agents.verification_dependencies import VerificationDependencyError
        result = error.to_result() if isinstance(error, VerificationDependencyError) else {
            "ok": False, "error": f"{type(error).__name__}: {error}"[:2000]}
    finally:
        ACTIVE_PROCESSES.terminate_all()
    atomic_json(path.with_name("result.json"), result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
