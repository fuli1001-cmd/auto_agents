"""Repair implementation worker; never imported by the stdlib supervisor."""
from __future__ import annotations

import json
import ast
import os
from pathlib import Path
import subprocess
import sys
import signal
import shutil
import re

# Direct script execution deliberately avoids auto_agents.cli initialization.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_agents.repair_control import Repository, Store, atomic_json, digest, git, publication_policy


def execute_selected_worker(request, runtime, python):
    """Keep control stable while loading repair logic from the selected engine."""
    runtime = Path(runtime).resolve()
    if Path(__file__).resolve().parents[2] == runtime:
        return
    request_path = request.get("_request_path")
    if not request_path:
        return  # Direct unit-level calls do not own a worker process.
    entry = runtime / "src/auto_agents/repair_worker.py"
    if not entry.is_file():
        raise RuntimeError("selected engine does not implement the repair worker protocol")
    atomic_json(Path(request_path), request)
    lock_fd = os.environ.get("AUTO_AGENTS_REPAIR_LOCK_FD")
    if lock_fd:
        os.set_inheritable(int(lock_fd), True)
    environment = {**os.environ, "PYTHONPATH": str(runtime / "src")}
    os.execve(python, [python, str(entry), request_path], environment)


def engine_environment(config, checkout):
    protocol = checkout / "src/auto_agents/repair_control.py"
    if not protocol.is_file() or not (checkout / "src/auto_agents/repair_client.py").is_file():
        raise RuntimeError("trusted runtime lacks the repair control protocol; publish the authorized installation before switching versions")
    versions = [node.value.value for node in ast.parse(protocol.read_text()).body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    if versions != [1]:
        raise RuntimeError("trusted runtime uses an incompatible repair control protocol")
    metadata = (checkout / "pyproject.toml").read_bytes()
    identity = digest([metadata.hex(), config["python"]])[:24]
    root = Path(config["root"]) / "environments" / identity
    python = root / "bin/python"
    receipt = root / "ready.json"
    if not receipt.exists():
        subprocess.run([config["python"], "-m", "venv", str(root)], check=True, timeout=60, capture_output=True)
        subprocess.run([str(python), "-m", "pip", "install", str(checkout), "pytest"],
                       check=True, timeout=300, capture_output=True)
        frozen = subprocess.run([str(python), "-m", "pip", "freeze"], check=True, text=True, capture_output=True, timeout=60).stdout
        atomic_json(receipt, {"identity": identity, "dependencies": frozen, "fingerprint": digest(frozen)})
    subprocess.run([str(python), "-c", "import pytest,regex"], check=True, capture_output=True, timeout=60)
    return str(python), json.loads(receipt.read_text())["fingerprint"]


def make_runner(payload, checkout, evidence, python):
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision
    from auto_agents.root_cause import RootCauseDiagnosis
    from auto_agents.repair_cases import RepairCase
    orchestrator = Orchestrator(evidence)
    orchestrator._invocation_context = dict(payload.get("invocation", {}))
    orchestrator._autonomy_mode = payload.get("autonomy", "max")
    if payload.get("provider"):
        orchestrator._set_active_provider(payload["provider"])
    diagnosis = RootCauseDiagnosis.from_dict(payload["diagnosis"]) if payload.get("diagnosis") else None
    runner = AutoAgentsSelfRepairRunner(orchestrator, target_project_root=evidence,
        error=RuntimeError(payload["error"]), decision=SelfRepairDecision(**payload["decision"]),
        diagnosis=diagnosis, repair_case=RepairCase.from_dict(payload["repair_case"]) if payload.get("repair_case") else None)
    runner.repo_root = checkout
    runner._verification_python_cache = python
    runner._engine_source_root = Path(payload.get("engine_root") or checkout)
    runner._replay_original_base = payload.get("base", "")
    runner._real_project_root = Path(payload["project"])
    config_path = os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_CONFIG")
    job_id = os.environ.get("AUTO_AGENTS_REPAIR_JOB")
    if config_path and job_id:
        store = Store(json.loads(Path(config_path).read_text())["root"])
        runner._control_phase_callback = lambda phase, details: store.event(job_id, phase, details)
    return runner


def import_legacy_experiment(payload, working, repository, directory):
    receipt = directory / "legacy-import.json"
    if receipt.exists():
        return
    from auto_agents.self_repair_search import safe_repair_root
    subject = ("session-" + payload["invocation"]["session_id"] if payload["invocation"].get("session_id")
               and not payload["invocation"].get("run_id") else payload["invocation"].get("run_id", ""))
    relative = Path(".auto-agents/runs") / subject / "self-repair" / safe_repair_root(payload["fingerprint"])
    original = Path(payload["project"]) / relative
    imported = False
    if subject and (original / "experiment.json").is_file():
        # Recreate frozen evidence for this invocation; retain candidate and
        # validation receipts for explicit revalidation, never trust old proof.
        shutil.copytree(original, working / relative, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("replay-checkpoint"))
        with repository.locked():
            git(repository.cache, "fetch", repository.config["source_root"],
                "refs/auto-agents/self-repair/*:refs/auto-agents/self-repair/*", check=False)
        imported = True
    atomic_json(receipt, {"imported": imported, "subject": subject})


def check_revision(runner, checkout, base):
    """Both behavior-specific differential AND original boundary are required."""
    runner._experiment_store, runner._experiment = runner._load_or_create_experiment()
    differential = runner._diagnosis_differential(base, checkout)
    if not differential.ok:
        # An explicit engine-work request may already be satisfied in both
        # installed and upstream versions. This is a no-change return, not
        # permission to approve a generated patch without negative proof.
        context = getattr(runner, "_invocation_context", {})
        diagnosis = getattr(runner, "diagnosis", None)
        if not context.get("engine_route") or diagnosis is None:
            return False, differential.summary
        from auto_agents.execution_binding import command_spans, executable_tokens
        commands = list(diagnosis.final.verification_commands)
        def pytest_command(command):
            try:
                args = executable_tokens(command)
                return len(command_spans(command)) == 1 and bool(args) and (
                    Path(args[0]).name == "pytest" or
                    bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(args[0]).name)) and args[1:3] == ["-m", "pytest"])
            except ValueError:
                return False
        if not commands or not all(pytest_command(command) for command in commands):
            return False, differential.summary
        positive = runner._run_verification_commands(commands, checkout)
        if not positive.ok or not positive.returncodes or any(code != 0 for code in positive.returncodes):
            return False, differential.summary
        if not re.search(r"\b[1-9][0-9]* passed\b", positive.summary):
            return False, differential.summary
    replay = runner._replay_candidate(checkout, git(checkout, "rev-parse", "HEAD"), "remote-reuse")
    return replay.ok, differential.summary + "\n" + replay.summary


def repair(request):
    from auto_agents.root_cause import RootCauseCoordinator
    from auto_agents.repository_guard import capture_repository_guard, guard_fingerprint
    config, job = request["config"], request["job"]
    payload = job["payload"]
    store = Store(config["root"])
    repository = Repository(config)
    prepared = request.get("prepared_runtime")
    revision, fresh = (prepared["revision"], prepared["fresh"]) if prepared else repository.fetch()
    store.event(job["id"], "remote_checked", {"revision": revision, "fresh": fresh})
    checkout = repository.worktree(revision, job["id"] + "-base-" + revision[:12])
    python, environment = ((prepared["python"], prepared["environment"]) if prepared else engine_environment(config, checkout))
    request["prepared_runtime"] = {"revision": revision, "fresh": fresh, "python": python, "environment": environment}
    execute_selected_worker(request, checkout, python)
    from auto_agents.verification_sandbox import check_verification_sandbox
    check_verification_sandbox(checkout, python, Path(payload["project"]))
    directory = Path(config["root"]) / "jobs" / job["id"]
    evidence = directory / "evidence"
    if not evidence.exists():
        RootCauseCoordinator._copy_diagnostic_tree(Path(payload["project"]), evidence)
        atomic_json(directory / "evidence.json", {"digest": guard_fingerprint(capture_repository_guard(evidence, ignore_run_artifacts=True))})
    # Work on a separate copy: the original frozen evidence never receives the
    # runner's experiment records or Orchestrator initialization writes.
    working = directory / "working-evidence"
    if not working.exists():
        RootCauseCoordinator._copy_diagnostic_tree(evidence, working)
    import_legacy_experiment(payload, working, repository, directory)
    base = payload["base"]
    repository.import_commit(config["source_root"], base)
    original = repository.worktree(base, job["id"] + "-original")
    verifier = make_runner(payload, original, working, python)
    fixed, proof = check_revision(verifier, checkout, base)
    if fixed:
        return {"ok": True, "status": "already_repaired", "commit": revision,
                "base": revision, "runtime": str(checkout), "python": python,
                "environment": environment, "proof": proof, "fresh": fresh}
    if payload.get("autonomy") != "max":
        return {"ok": False, "error": "latest revision did not prove recovery; guarded mode will not generate code", "proof": proof}
    runner = make_runner(payload, checkout, working, python)
    runner._latest_remote_check = proof
    runner._continuous_workspace = directory / "continuous"
    carry_continuous_work(runner, revision)
    result = runner.run()
    if not result.ok:
        return {"ok": False, "error": result.reason, "result": result.to_dict()}
    candidate = result.candidate_commit or result.commit_sha
    if not candidate:
        return {"ok": False, "error": "approved repair has no immutable candidate revision"}
    runtime = repository.worktree(candidate, job["id"] + "-approved-" + candidate[:12])
    return {"ok": True, "status": "repaired", "commit": candidate, "base": revision,
            "runtime": str(runtime), "python": python, "environment": environment,
            "proof": result.verification, "fresh": fresh, "result": result.to_dict()}


def carry_continuous_work(runner, revision):
    """Retain edits while bringing a restarted repair onto a newer upstream."""
    directory = Path(runner._continuous_workspace)
    receipt = directory / "base.json"
    previous = json.loads(receipt.read_text()).get("revision") if receipt.exists() else revision
    retained = directory / "repair"
    if retained.exists() and previous != revision:
        if git(retained, "status", "--porcelain"):
            git(retained, "add", "-A")
            git(retained, "commit", "-m", "chore: checkpoint retained repair before upstream integration")
        merged = git(retained, "merge", "--no-commit", "--no-ff", revision, check=False)
        if merged.returncode:
            paths = git(retained, "diff", "--name-only", "--diff-filter=U").splitlines()
            if not paths:
                raise RuntimeError("could not integrate upstream into retained repair")
            from auto_agents.self_repair import _SelfRepairGitConflict, _SelfRepairRemote
            original_root = runner.repo_root
            try:
                runner.repo_root = retained
                runner._resolve_remote_conflicts(_SelfRepairRemote("trusted", "master"),
                    _SelfRepairGitConflict("upstream changed while the repair was interrupted", paths))
            finally:
                runner.repo_root = original_root
    atomic_json(receipt, {"revision": revision})


def publish(request):
    config, job = request["config"], request["job"]
    config = publication_policy(config)
    repository = Repository(config)
    approved = job["result"]
    if approved.get("runtime"):
        execute_selected_worker(request, approved["runtime"], approved["python"])
    revision, fresh = repository.fetch()
    if not fresh:
        raise RuntimeError("publication requires a successful upstream refresh")
    candidate = approved["commit"]
    if git(repository.cache, "merge-base", "--is-ancestor", candidate, revision, check=False).returncode == 0:
        return {"ok": True, "commit": revision, "status": "already_published"}
    if revision != approved["base"]:
        directory = Path(config["root"]) / "jobs" / job["id"]
        receipt_key = digest([revision, approved["commit"], approved.get("environment", "")])
        receipt = directory / ("integrated-" + receipt_key + ".json")
        if receipt.exists():
            recorded = json.loads(receipt.read_text())
            candidate = recorded["commit"]
            repository.push(candidate)
            return {"ok": True, "commit": candidate, "status": "published"}
        name = job["id"] + "-publish-" + revision[:12] + "-" + approved["commit"][:12]
        root = Path(config["root"]) / "runtimes" / name
        if not root.exists():
            root = repository.worktree(revision, name)
        if git(root, "merge-base", "--is-ancestor", revision, "HEAD", check=False).returncode:
            raise RuntimeError("publication worktree does not descend from its recorded upstream")
        working = Path(config["root"]) / "jobs" / job["id"] / "working-evidence"
        runner = make_runner(job["payload"], root, working, approved["python"])
        # A different machine may already have published an equivalent fix.
        if git(root, "rev-parse", "HEAD") == revision and not git(root, "status", "--porcelain"):
            fixed, proof = check_revision(runner, root, job["payload"]["base"])
            if fixed:
                return {"ok": True, "commit": revision, "status": "already_published"}
        merged = git(root, "merge-base", "--is-ancestor", candidate, "HEAD", check=False).returncode == 0
        if not merged:
            merge_head = git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False)
            if merge_head.returncode:
                git(root, "merge", "--no-commit", "--no-ff", candidate, check=False)
            conflicts = git(root, "diff", "--name-only", "--diff-filter=U").splitlines()
            if conflicts:
                from auto_agents.self_repair import _SelfRepairGitConflict, _SelfRepairRemote
                runner._resolve_remote_conflicts(_SelfRepairRemote("trusted", config["ref"]),
                    _SelfRepairGitConflict("upstream advanced during verified repair publication", conflicts))
            else:
                git(root, "commit", "-m", f"fix: integrate verified engine repair {job['id']}")
        candidate = git(root, "rev-parse", "HEAD")
        weakening = runner._candidate_test_weakening_reason(root, revision)
        if weakening:
            raise RuntimeError("integrated publication weakened proof: " + weakening)
        passed, proof = check_revision(runner, root, job["payload"]["base"])
        suite = runner._run_verification_commands(["python -m pytest -q -p no:cacheprovider"], root)
        if not passed or not suite.ok:
            raise RuntimeError("integrated publication failed verification; retain local recovery version")
        atomic_json(receipt, {"commit": candidate, "upstream": revision, "proof": proof, "suite": suite.summary})
    if not config.get("publish", False):
        raise PermissionError("automatic publication is not authorized by the operator")
    repository.push(candidate)
    return {"ok": True, "commit": candidate, "status": "published"}


def validate_subscriber(request):
    from auto_agents.root_cause import RootCauseCoordinator
    config, job, subscriber = request["config"], request["job"], request["subscriber"]
    payload = subscriber["payload"]["repair"]
    execute_selected_worker(request, job["result"]["runtime"], job["result"]["python"])
    directory = Path(config["root"]) / "jobs" / job["id"] / ("subscriber-" + subscriber["id"])
    evidence = directory / "evidence"
    if not evidence.exists():
        RootCauseCoordinator._copy_diagnostic_tree(Path(subscriber["project"]), evidence)
    repository = Repository(config)
    repository.import_commit(config["source_root"], payload["base"])
    original = repository.worktree(payload["base"], "base-" + payload["base"][:20])
    runner = make_runner(payload, original, evidence, job["result"]["python"])
    passed, proof = check_revision(runner, Path(job["result"]["runtime"]), payload["base"])
    return {"ok": passed, "proof": proof}


def main():
    # Only verification children may inherit this marker. Project environment
    # input must never disable creation of the worker's network isolation.
    os.environ.pop("AUTO_AGENTS_VERIFICATION_SANDBOX", None)
    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_text())
    request["_request_path"] = str(request_path)
    operation = request["operation"]
    from auto_agents.process_supervision import ACTIVE_PROCESSES
    ACTIVE_PROCESSES.configure(request_path.parent, request["job"]["id"], request_path.parent / "processes.json")
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        result = (publish(request) if operation == "publish" else
                  validate_subscriber(request) if operation.startswith("validate-") else repair(request))
    except PermissionError as error:
        result = {"ok": False, "permission": True, "error": str(error)}
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}"[:2000]}
    except KeyboardInterrupt:
        result = {"ok": False, "error": "repair cancelled", "cancelled": True}
    finally:
        ACTIVE_PROCESSES.terminate_all()
    result["generation"] = request["job"]["generation"]
    atomic_json(request_path.with_name(request_path.name.replace("-request.json", "-result.json")), result)
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
