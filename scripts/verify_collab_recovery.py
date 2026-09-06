"""Compare the committed engine and working candidate on private project copies."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--baseline-ref", default="HEAD")
    args = parser.parse_args()
    engine = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(engine / "src"))
    from auto_agents.repository_guard import capture_repository_guard, changed_guard_paths
    from auto_agents.root_cause import RootCauseCoordinator
    from auto_agents.session_recovery import plan_session_recovery

    project = args.project.resolve()
    before = capture_repository_guard(project, ignore_run_artifacts=True)
    output = Path(tempfile.mkdtemp(prefix="auto-agents-collab-validation-"))
    baseline_engine = output / "base-engine"
    subprocess.run(["git", "clone", "--shared", "--quiet", str(engine), str(baseline_engine)], check=True)
    subprocess.run(["git", "checkout", "--detach", "--quiet", args.baseline_ref], cwd=baseline_engine, check=True)
    results = {"output_root": str(output), "session_id": args.session, "command": "collab"}
    for name, implementation in (("base", baseline_engine), ("candidate", engine)):
        target = output / name / "target"
        RootCauseCoordinator._copy_diagnostic_tree(project, target)
        if name == "candidate":
            plan = plan_session_recovery(target, args.session, "collab")
            results["recovery_plan"] = {"workflow_id": plan.workflow_id, "event_sequence": plan.event_sequence, "files": len(plan.files)}
        try:
            probe = subprocess.run(
                [sys.executable, str(engine / "src/auto_agents/session_replay.py"), str(implementation), str(target), args.session, "collab"],
                cwd=target, capture_output=True, text=True, timeout=90,
            )
            (output / f"{name}.stdout.txt").write_text(probe.stdout)
            (output / f"{name}.stderr.txt").write_text(probe.stderr)
            try:
                results[name] = json.loads(probe.stdout.splitlines()[-1])
            except (ValueError, IndexError):
                results[name] = {"ok": False, "error": probe.stderr[-2000:]}
        except subprocess.TimeoutExpired:
            results[name] = {"ok": False, "error": "isolated resume probe timed out"}
    results["live_changed_paths"] = changed_guard_paths(before, capture_repository_guard(project, ignore_run_artifacts=True))
    results["ok"] = bool(not results["base"].get("ok") and results["candidate"].get("ok") and not results["live_changed_paths"])
    (output / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
