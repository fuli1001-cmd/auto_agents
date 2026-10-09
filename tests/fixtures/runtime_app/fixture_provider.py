"""A deterministic local CLI worker, exercising production confinement."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

prompt = sys.stdin.read()
inputs = json.loads(prompt.split("Owned inputs:\n", 1)[1].split("\n\n", 1)[0])
phase = os.environ["AUTO_AGENTS_STAGE"]
prefix = ".auto-agents/docs/acceptance/runtime/"
check = sys.executable + " -m pytest -q tests/test_capability.py"


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if phase == "diagnose":
    if inputs.get("acceptance_blocker"):
        result = {
            "action": "fix",
            "issue": {
                "summary": "Repair the evidenced capability reference",
                "reproduction": [
                    "The existing browser project shows capability reference mismatch"
                ],
                "paths": ["capability.json"],
                "checks": [{"command": check}],
                "evidence_refs": inputs["acceptance_blocker"]["evidence_refs"],
            },
        }
    else:
        result = {
            "action": "run",
            "spec": "Execute browser acceptance of existing behavior",
        }
elif phase == "classify":
    result = {"decision": "fix"}
elif phase == "implement":
    Path("capability.json").write_text('{"ready":true}\n')
    result = {"summary": "Corrected the local fixture capability reference"}
elif phase in {"review", "acceptance_review"}:
    result = {
        "decision": "pass",
        "test_changes_valid": True,
        "reason": "Verified the retained project and exact checks",
        "evidence_refs": [prefix + "observation.json"]
        if phase == "acceptance_review"
        else [],
    }
elif phase == "acceptance":
    Path(".tmp-tests").mkdir(exist_ok=True)
    Path(prefix).mkdir(parents=True, exist_ok=True)
    api, frontend = port(), port()
    environment = {
        **os.environ,
        "AGT_API_BASE": "http://127.0.0.1:" + str(api),
        "NEXT_TELEMETRY_DISABLED": "1",
        "RAYON_NUM_THREADS": "1",
        "UV_THREADPOOL_SIZE": "2",
    }
    processes = []
    try:
        for command, name in [
            ([sys.executable, "backend.py", str(api)], "backend"),
            (["node", "next-server.cjs", str(frontend)], "frontend"),
        ]:
            log = Path(".tmp-tests/" + name + ".log").open("w")
            processes.append(
                subprocess.Popen(command, env=environment, stdout=log, stderr=log)
            )
            log.close()
        url = "http://127.0.0.1:" + str(frontend)
        for _ in range(300):
            try:
                urllib.request.urlopen(url + "/api/state", timeout=2).close()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("Fixture services did not become ready")
        mode = "resume" if inputs.get("previous_acceptance") else "create"
        browser = subprocess.run(
            ["node", "browser.cjs", url, mode],
            env=environment,
            capture_output=True,
            text=True,
            timeout=100,
        )
        if browser.returncode:
            raise RuntimeError(browser.stdout + browser.stderr)
        observed = json.loads(browser.stdout)
        test = Path(".tmp-tests/test_observe.py")
        test.write_text("""import json,sqlite3
from pathlib import Path
def test_retained_project(tmp_path):
 obs=json.loads(Path('.auto-agents/docs/acceptance/runtime/observation.json').read_text())
 with sqlite3.connect('file:.data/projects.sqlite3?mode=ro',uri=True) as db:
  assert db.execute('SELECT id FROM projects').fetchall()==[('project-1',)]
  assert db.execute("SELECT count(*) FROM requests WHERE kind='/create'").fetchone()[0]==1
  assert db.execute("SELECT count(*) FROM requests WHERE kind='/submit'").fetchone()[0]==(1 if obs['ready'] else 0)
 assert Path('.auto-agents/docs/acceptance/runtime/browser.png').read_bytes().startswith(b'\\x89PNG')
""")
        command = (
            sys.executable
            + " -m pytest -q .tmp-tests/test_observe.py --basetemp=.tmp-tests/pytest-observe --junitxml="
            + prefix
            + "checks.xml"
        )
        executed = subprocess.run(
            command.split(), capture_output=True, text=True, timeout=20
        )
        if executed.returncode:
            raise RuntimeError(executed.stdout + executed.stderr)
        result = {
            "accepted": bool(observed["task_id"]),
            "reason": "same project completed"
            if observed["task_id"]
            else "capability reference mismatch",
            "external_ids": {
                "project_id": observed["project_id"],
                "provider_task_id": observed["task_id"],
            },
            "evidence_refs": [
                prefix + "observation.json",
                prefix + "browser.png",
                prefix + "checks.xml",
            ],
            "checks": [{"command": command, "outputs": [prefix + "checks.xml"]}],
        }
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
else:
    raise AssertionError(phase)
print(json.dumps(result))
