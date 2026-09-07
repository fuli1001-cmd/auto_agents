"""Load a verified runtime in a fresh interpreter, preserving workflow identity."""
import json
import os
from pathlib import Path
import sys
import subprocess


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    subscriber, result = request["subscriber"], request["result"]
    runtime = Path(result["runtime"]).resolve()
    loaded = subprocess.run(["git", "-C", str(runtime), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    changed = subprocess.run(["git", "-C", str(runtime), "diff", "HEAD", "--exit-code"],
                             capture_output=True, timeout=60)
    if loaded != result["commit"] or changed.returncode:
        raise RuntimeError("approved runtime changed before workflow launch")
    sys.path.insert(0, str(runtime / "src"))
    from auto_agents.run_lock import ProjectRunLock
    from auto_agents.config import load_run_state
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.cli import main as cli_main
    project = Path(subscriber["project"])
    payload = subscriber["payload"]["repair"]
    inherited = int(os.environ["AUTO_AGENTS_RUN_LOCK_FD"])
    preparation_fd = os.dup(inherited)
    preparation_env = {**os.environ, "AUTO_AGENTS_RUN_LOCK_FD": str(preparation_fd)}
    with ProjectRunLock(project, environ=preparation_env) as lock:
        invocation = payload["invocation"]
        if invocation.get("run_id"):
            state = load_run_state(project)
            if state.run_id != invocation["run_id"] or state.active_blocker.get("fingerprint") != payload["fingerprint"]:
                raise RuntimeError("live run no longer matches the approved recovery")
            Orchestrator(project).mark_self_repair_applied(result["commit"], verification=result["proof"])
        argv = payload["resume_argv"]
    return cli_main(argv[2:])


if __name__ == "__main__":
    path = Path(sys.argv[1])
    try:
        code = main()
    finally:
        output = path.with_name(path.stem + "-result.json")
        temporary = output.with_suffix(".tmp")
        with temporary.open("w") as handle:
            json.dump({"exit_code": locals().get("code", 3)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    raise SystemExit(code)
