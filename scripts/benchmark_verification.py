"""Compare fresh and warm managed checks on one immutable source snapshot.

No model/API calls. Supply explicit offline test targets; the real source is
read-only during execution. Output is JSON so comparisons can be archived.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from auto_agents.managed_verification import engine_runner, selected_tests
from auto_agents.root_cause import RootCauseCoordinator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--test", action="append", required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    targets = selected_tests(project, args.test)
    with tempfile.TemporaryDirectory(prefix="verification-benchmark-") as temporary:
        os.environ["AUTO_AGENTS_VERIFICATION_ROOT"] = str(Path(temporary) / "proofs")
        os.environ["AUTO_AGENTS_WORKER_ROOT"] = str(Path(temporary) / "worker")
        os.environ["AUTO_AGENTS_CLUSTER_HOME"] = str(Path(temporary) / "cluster")
        root = Path(temporary) / "source"
        RootCauseCoordinator._copy_diagnostic_tree(project, root)
        runner = engine_runner(root, project, sys.executable, repository=project)
        runner._verification_read_roots = [project]
        results = []
        for fresh in (True, False, False):
            runner._verification_fresh = fresh
            started = time.monotonic()
            result = runner._run_verification_commands([shlex.join(["python", "-m", "pytest", "-q", *targets])], root)
            results.append({"fresh": fresh, "seconds": time.monotonic() - started,
                            "ok": result.ok, "certificate_hits": result.payload.get("certificate_hits", 0),
                            "proof_refs": result.payload.get("proof_refs", [])})
        print(json.dumps({"scope": "fixed isolated snapshot", "targets": targets, "runs": results}, indent=2))
        return 0 if all(row["ok"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
