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
import time

# Direct script execution deliberately avoids auto_agents.cli initialization.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_agents.repair_control import Repository, Store, atomic_json, digest, git, operator_policy, publication_policy
from auto_agents.repair_runtime import RuntimeCompatibilityError, require_runtime, verify_runtime


def execute_selected_worker(request, runtime, python):
    """Keep control stable while loading repair logic from the selected engine."""
    runtime = Path(runtime).resolve()
    payload = request.get("job", {}).get("payload", {})
    if payload.get("invocation", {}).get("engine_route") and not (runtime / "src/auto_agents/repair_contract.py").is_file():
        raise RuntimeError("selected engine lacks explicit-request acceptance proof; refusing legacy repair fallback")
    if Path(__file__).resolve().parents[2] == runtime:
        return
    request_path = request.get("_request_path")
    if not request_path:
        return  # Direct unit-level calls do not own a worker process.
    entry = runtime / "src/auto_agents/repair_worker.py"
    if not entry.is_file():
        raise RuntimeError("selected engine does not implement the repair worker protocol")
    request["runtime_compatibility"] = verify_runtime(runtime, python)
    atomic_json(Path(request_path), request)
    lock_fd = os.environ.get("AUTO_AGENTS_REPAIR_LOCK_FD")
    if lock_fd:
        os.set_inheritable(int(lock_fd), True)
    environment = {**os.environ, "PYTHONPATH": str(runtime / "src")}
    os.execve(python, [python, str(entry), request_path], environment)


def engine_environment(config, checkout):
    from auto_agents.repair_environment_log import EnvironmentSetupLog, sanitize
    log = EnvironmentSetupLog(config)
    protocol = checkout / "src/auto_agents/repair_control.py"
    if not protocol.is_file() or not (checkout / "src/auto_agents/repair_client.py").is_file():
        raise RuntimeError("trusted runtime lacks the repair control protocol; publish the authorized installation before switching versions")
    versions = [node.value.value for node in ast.parse(protocol.read_text()).body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    if versions != [1]:
        raise RuntimeError("trusted runtime uses an incompatible repair control protocol")
    require_runtime(checkout, phase="environment")
    metadata = (checkout / "pyproject.toml").read_bytes()
    identity = digest([metadata.hex(), config["python"]])[:24]
    root = Path(config["root"]) / "environments" / identity
    python = root / "bin/python"
    receipt = root / "ready.json"
    from auto_agents.artifact_runtime import track
    from auto_agents.artifact_store import ArtifactStore
    root.mkdir(parents=True, exist_ok=True)
    artifact = track(root, "environment" if receipt.exists() else "incomplete",
                     scope="repair:" + config["root"], metadata={"repair_root": config["root"]})
    if not receipt.exists():
        package_cache = Path(config["root"]) / "package-cache" / "pip"
        package_cache.mkdir(parents=True, exist_ok=True)
        track(package_cache, "cache", scope="repair:" + config["root"], metadata={"repair_root": config["root"]})
        log.run([config["python"], "-m", "venv", str(root)], timeout=60)
        log.run([str(python), "-m", "pip", "install", str(checkout), "pytest"],
                timeout=300, env={**os.environ, "PIP_CACHE_DIR": str(package_cache)})
        frozen = log.run([str(python), "-m", "pip", "freeze"], text=True, timeout=60).stdout
        atomic_json(receipt, {"identity": identity, "dependencies": sanitize(frozen), "fingerprint": digest(frozen)})
        if artifact:
            ArtifactStore().promote(artifact, "environment")
    log.run([str(python), "-c", "import pytest,regex"], timeout=60)
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
    if payload.get("invocation", {}).get("engine_route"):
        from auto_agents.repair_contract import EngineRequestContract
        diagnosis = EngineRequestContract.from_dict(payload.get("request_contract", {}), payload["invocation"]["engine_route"])
    else:
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
        root = json.loads(Path(config_path).read_text())["root"]
        store = Store(root)
        runner._repair_control_binding = {"root": root, "job": job_id, "generation": store.job(job_id)["generation"]}
        generation = runner._repair_control_binding['generation']
        runner._control_phase_callback = lambda phase, details: store.event(job_id, phase, {**details, 'generation': generation})
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
    context = getattr(runner, "_invocation_context", {})
    if context.get("engine_route"):
        # An explicit engine-work request may already be satisfied in both
        # installed and upstream versions. This is a no-change return, not
        # permission to approve a generated patch without negative proof.
        diagnosis = getattr(runner, "diagnosis", None)
        if diagnosis is None:
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
        if re.search(r"\b[1-9][0-9]* (?:skipped|xfailed|xpassed|deselected)\b", positive.summary):
            return False, "engine request acceptance checks were not all exercised\n" + positive.summary
    elif not differential.ok:
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
    base = payload["base"]
    directory = Path(config["root"]) / 'jobs' / job['id']
    selection_path = directory / 'source-selection.json'
    if prepared and prepared.get('source_selection'):
        selection = prepared['source_selection']
        if (not selection_path.is_file() or json.loads(selection_path.read_text()) != selection
                or selection['revision'] != prepared['revision'] or selection['requested_revision'] != base):
            raise RuntimeError('prepared engine selection does not match its durable receipt')
    else:
        prepared = None  # A legacy cached remote is not a new source selection.
        started = time.monotonic()
        store.event(job['id'], 'phase_started', {'phase': 'engine_source_sync'})
        try:
            selection = repository.select_source(base)
        finally:
            store.event(job['id'], 'phase_finished', {'phase': 'engine_source_sync',
                        'duration_seconds': time.monotonic() - started})
    revision, fresh = selection['revision'], selection['fresh']
    store.event(job["id"], "remote_checked", {"revision": selection['remote_revision'], "fresh": fresh})
    store.event(job["id"], "runtime_selected", {"requested_revision": base,
                "selected_revision": revision, "contains_requested": True, "fresh": fresh,
                'relation': selection['relation'], 'remote_revision': selection['remote_revision']})
    checkout = repository.worktree(revision, job["id"] + "-base-" + revision[:12])
    python, environment = ((prepared["python"], prepared["environment"]) if prepared else engine_environment(config, checkout))
    if not prepared:
        if request.get('_request_path'):
            verify_runtime(checkout, python)
        repository.update_source(selection['snapshot'], revision)
        atomic_json(selection_path, selection)
        if selection.get('merge_receipt'):
            atomic_json(Path(selection['merge_receipt']), {'pending': False, 'commit': revision})
    request["prepared_runtime"] = {"revision": revision, "fresh": fresh, "python": python,
                                   "environment": environment, 'source_selection': selection}
    execute_selected_worker(request, checkout, python)
    if config.get('repair_engine') == 'v2':
        from auto_agents.repair_v2.integration import repair as unified_repair
        return unified_repair(request, checkout, python, environment, revision)
    from auto_agents.verification_sandbox import check_verification_sandbox
    check_verification_sandbox(checkout, python, Path(payload["project"]))
    from auto_agents.verification_input_trace import check_input_tracing
    trace_owner = check_input_tracing(checkout, python, Path(payload['project']))
    store.event(job['id'], 'verification_input_trace_ready', trace_owner)
    evidence = directory / "evidence"
    if not evidence.exists():
        RootCauseCoordinator._copy_diagnostic_tree(Path(payload["project"]), evidence)
        atomic_json(directory / "evidence.json", {"digest": guard_fingerprint(capture_repository_guard(evidence, ignore_run_artifacts=True))})
    # Work on a separate copy: the original frozen evidence never receives the
    # runner's experiment records or Orchestrator initialization writes.
    working = directory / "working-evidence"
    if not working.exists():
        RootCauseCoordinator._copy_diagnostic_tree(evidence, working)
    from auto_agents.artifact_runtime import track
    for snapshot in (evidence, working):
        track(snapshot, "recovery", scope="repair:" + config["root"],
              metadata={"repair_root": config["root"], "disposable": True})
    request_contract = {}
    if payload.get("invocation", {}).get("engine_route"):
        from auto_agents.repair_contract import prepare_contract
        store.event(job["id"], "request_contract_planning", {"revision": revision})
        request_contract = prepare_contract(payload, revision, checkout, working, directory).to_dict()
        payload = {**payload, "request_contract": request_contract}
        store.event(job["id"], "request_contract_ready", {"revision": revision})
    import_legacy_experiment(payload, working, repository, directory)
    original = repository.worktree(base, job["id"] + "-original")
    verifier = make_runner(payload, original, working, python)
    fixed, proof = check_revision(verifier, checkout, base)
    if fixed:
        full = verifier._full_suite_differential(base, checkout)
        if not full.ok or full.recoverable:
            return {"ok": False, "error": "upstream behavior passed but full engine proof is incomplete or failed",
                    "proof": proof + "\n" + full.summary}
        return {"ok": True, "status": "already_repaired", "commit": revision,
                'source_delivery_needed': True,
                "base": revision, "runtime": str(checkout), "python": python,
                "environment": environment, "proof": proof + "\n" + full.summary, "fresh": fresh, "request_contract": request_contract,
                "engine_full_proof": {"policy": 1, "commit": revision, "environment": environment, "ok": True}}
    if payload.get("autonomy") != "max":
        return {"ok": False, "error": "latest revision did not prove recovery; guarded mode will not generate code", "proof": proof}
    from auto_agents.repair_restart import import_cancelled_repair
    import_cancelled_repair(store, job, working, repository, revision=revision)
    runner = make_runner(payload, checkout, working, python)
    restart_receipt = directory / "prior-repair-import.json"
    if restart_receipt.exists():
        runner._inherited_candidate_ids = set(json.loads(restart_receipt.read_text()).get("candidate_ids", []))
    runner._latest_remote_check = proof
    runner._continuous_workspace = directory / "continuous"
    carry_continuous_work(runner, revision)
    result = runner.run()
    if not result.ok:
        return {"ok": False, "status": getattr(result, "status", "failed"), "next_action": getattr(result, "next_action", {}),
                **getattr(runner, "_verification_dependency_failure", {}),
                "error": result.reason, "result": result.to_dict()}
    candidate = result.candidate_commit or result.commit_sha
    if not candidate:
        return {"ok": False, "error": "approved repair has no immutable candidate revision"}
    runtime = repository.worktree(candidate, job["id"] + "-approved-" + candidate[:12])
    return {"ok": True, "status": "repaired", "commit": candidate, "base": revision,
            'source_delivery_needed': True,
            "runtime": str(runtime), "python": python, "environment": environment,
            "proof": result.verification, "fresh": fresh, "result": result.to_dict(), "request_contract": request_contract,
            "engine_full_proof": {"policy": 1, "commit": candidate, "environment": environment, "ok": True}}


def carry_continuous_work(runner, revision):
    """Retain edits while bringing a restarted repair onto a newer upstream."""
    directory = Path(runner._continuous_workspace)
    receipt = directory / "base.json"
    retained = directory / "repair"

    def finish_merge():
        paths = git(retained, "diff", "--name-only", "--diff-filter=U").splitlines()
        if paths:
            from auto_agents.self_repair import _SelfRepairGitConflict, _SelfRepairRemote
            original_root = runner.repo_root
            try:
                runner.repo_root = retained
                runner._resolve_remote_conflicts(_SelfRepairRemote("trusted", "master"),
                    _SelfRepairGitConflict("upstream changed while the repair was interrupted", paths))
            finally:
                runner.repo_root = original_root
        if git(retained, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git(retained, "add", "-A")
            git(retained, "commit", "--no-edit")

    if retained.exists() and git(retained, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        finish_merge()
    if (retained.exists()
            and git(retained, "merge-base", "--is-ancestor", revision, "HEAD", check=False).returncode != 0):
        if git(retained, "status", "--porcelain"):
            git(retained, "add", "-A")
            git(retained, "commit", "-m", "chore: checkpoint retained repair before upstream integration")
        merged = git(retained, "merge", "--no-commit", "--no-ff", revision, check=False)
        if merged.returncode:
            paths = git(retained, "diff", "--name-only", "--diff-filter=U").splitlines()
            if not paths:
                raise RuntimeError("could not integrate upstream into retained repair")
        finish_merge()
    atomic_json(receipt, {"revision": revision})


def publish(request):
    config, job = request["config"], request["job"]
    config = operator_policy(config)
    repository = Repository(config)
    approved = job["result"]
    if approved.get("runtime"):
        execute_selected_worker(request, approved["runtime"], approved["python"])
    revision, fresh = repository.fetch()
    snapshot = repository.delivery_snapshot(job['id'])
    repository.import_commit(config['source_root'], snapshot['commit'])
    candidate = approved['commit']
    directory = Path(config['root']) / 'jobs' / job['id']
    # A verified integration remains reusable after local delivery followed by a
    # network failure. New local/remote commits invalidate this cache naturally.
    receipt = directory / 'verified-delivery.json'
    recorded = json.loads(receipt.read_text()) if receipt.exists() else {}
    reusable = (recorded.get('approved') == candidate and recorded.get('upstream') == revision
                and recorded.get('environment') == approved.get('environment', '')
                and recorded.get('branch') == snapshot['branch']
                and snapshot['commit'] in {recorded.get('local'), recorded.get('commit')})
    if reusable:
        candidate = recorded['commit']
    elif not all(git(repository.cache, 'merge-base', '--is-ancestor', parent, candidate,
                     check=False).returncode == 0 for parent in (revision, snapshot['commit'])):
        name = 'delivery-' + digest([job['id'], revision, candidate, snapshot])[:24]
        root = repository.root / 'runtimes' / name
        if not root.exists():
            root = repository.worktree(revision, name)
        working = directory / 'working-evidence'
        from auto_agents.repair_contract import with_request_contract
        runner = make_runner(with_request_contract(job['payload'], approved), root, working, approved['python'])
        for parent in (candidate, snapshot['commit']):
            if git(root, 'merge-base', '--is-ancestor', parent, 'HEAD', check=False).returncode == 0:
                continue
            if git(root, 'rev-parse', '--verify', 'MERGE_HEAD', check=False).returncode:
                git(root, 'merge', '--no-commit', '--no-ff', parent, check=False)
            conflicts = git(root, 'diff', '--name-only', '--diff-filter=U').splitlines()
            if conflicts:
                from auto_agents.self_repair import _SelfRepairGitConflict, _SelfRepairRemote
                runner._resolve_remote_conflicts(_SelfRepairRemote('trusted', config['ref']),
                    _SelfRepairGitConflict('engine source advanced during repair delivery', conflicts))
            if not git(root, 'rev-parse', '--verify', 'MERGE_HEAD', check=False).returncode:
                git(root, 'add', '-A')
                git(root, 'commit', '-m', f"fix: integrate verified engine repair {job['id']}")
            if git(root, 'merge-base', '--is-ancestor', parent, 'HEAD', check=False).returncode:
                raise RuntimeError('repair delivery merge did not retain its input history')
        candidate = git(root, 'rev-parse', 'HEAD')
        if git(root, 'status', '--porcelain'):
            raise RuntimeError('repair delivery integration has uncommitted changes')
        if git(root, 'diff', approved['commit'], candidate, '--', 'pyproject.toml'):
            python, _ = engine_environment(config, root)
            runner = make_runner(with_request_contract(job['payload'], approved), root, working, python)
        weakening = runner._candidate_test_weakening_reason(root, approved['commit'])
        if weakening:
            raise RuntimeError('integrated publication weakened proof: ' + weakening)
        passed, proof = check_revision(runner, root, job['payload']['base'])
        suite = runner._run_full_suite_shards(root)
        if not passed or not suite.ok or getattr(suite, 'recoverable', False):
            raise RuntimeError('integrated publication failed verification; retain local recovery version')
        if git(root, 'rev-parse', 'HEAD') != candidate or git(root, 'status', '--porcelain'):
            raise RuntimeError('repair delivery source changed during verification')
        atomic_json(receipt, {'approved': approved['commit'], 'upstream': revision,
            'local': snapshot['commit'], 'branch': snapshot['branch'], 'commit': candidate,
            'environment': approved.get('environment', ''), 'proof': proof, 'suite': suite.summary})
    repository.record_delivery(job['id'], snapshot, candidate)
    if not fresh:
        raise RuntimeError('publication requires a successful upstream refresh')
    contained = git(repository.cache, 'merge-base', '--is-ancestor', candidate, revision, check=False).returncode == 0
    if not contained:
        publication_policy(config)
        repository.push(candidate)
    return {'ok': True, 'commit': candidate, 'status': 'already_published' if contained else 'published',
            'source_delivery': {'commit': candidate, 'branch': snapshot['branch']}}


def validate_subscriber(request):
    from auto_agents.root_cause import RootCauseCoordinator
    config, job, subscriber = request["config"], request["job"], request["subscriber"]
    payload = subscriber["payload"]["repair"]
    from auto_agents.repair_contract import with_request_contract
    payload = with_request_contract(payload, job["result"])
    execute_selected_worker(request, job["result"]["runtime"], job["result"]["python"])
    directory = Path(config["root"]) / "jobs" / job["id"] / ("subscriber-" + subscriber["id"])
    evidence = directory / "evidence"
    if not evidence.exists():
        RootCauseCoordinator._copy_diagnostic_tree(Path(subscriber["project"]), evidence)
    repository = Repository(config)
    repository.import_commit(config["source_root"], payload["base"])
    original = repository.worktree(payload["base"], "base-" + payload["base"][:20])
    runner = make_runner(payload, original, evidence, job["result"]["python"])
    receipt = job["result"].get("engine_full_proof", {})
    expected = {"policy": 1, "commit": job["result"]["commit"], "environment": job["result"]["environment"], "ok": True}
    if receipt != expected:
        full = runner._full_suite_differential(payload["base"], Path(job["result"]["runtime"]))
        if not full.ok or full.recoverable:
            return {"ok": False, "proof": full.summary}
    passed, proof = check_revision(runner, Path(job["result"]["runtime"]), payload["base"])
    return {"ok": passed, "proof": proof, "engine_full_proof": expected}


def main():
    # Only verification children may inherit this marker. Project environment
    # input must never disable creation of the worker's network isolation.
    os.environ.pop("AUTO_AGENTS_VERIFICATION_SANDBOX", None)
    request_path = Path(sys.argv[1])
    request = json.loads(request_path.read_text())
    from auto_agents.artifact_runtime import activate, schedule, track
    activate(scope="repair:" + request["config"]["root"], process_control=request_path.parent / "processes.json")
    schedule()
    track(request_path.parent / "repair.log", "log", scope="repair:" + request["config"]["root"],
          metadata={"repair_root": request["config"]["root"]})
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
    except RuntimeCompatibilityError as error:
        result = error.to_result()
        result["runtime_compatibility"]["controller_revision"] = request["config"].get("implementation_revision", "")
    except PermissionError as error:
        from auto_agents.repair_environment_log import failure_result
        result = {**failure_result(error), "permission": True}
    except Exception as error:
        from auto_agents.repair_environment_log import failure_result
        from auto_agents.verification_dependencies import VerificationDependencyError
        result = error.to_result() if isinstance(error, VerificationDependencyError) else failure_result(error)
    except KeyboardInterrupt:
        result = {"ok": False, "error": "repair cancelled", "cancelled": True}
    finally:
        ACTIVE_PROCESSES.terminate_all()
    result["generation"] = request["job"]["generation"]
    atomic_json(request_path.with_name(request_path.name.replace("-request.json", "-result.json")), result)
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
