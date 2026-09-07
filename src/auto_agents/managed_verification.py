"""Managed verification CLI planning and trusted executor entrypoints."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shlex
import sys
from auto_agents import artifact_temp as tempfile
from types import SimpleNamespace

from .repair_control import digest, git, rpc


def selected_tests(root, targets):
    """Accept repository test paths/node IDs, never a shell command or flag."""
    root = Path(root).resolve()
    if not targets:
        return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))
    result = []
    for target in targets:
        if not isinstance(target, str) or target.startswith("-") or any(c in target for c in "\n\r\x00;&|`$<>"):
            raise ValueError("verification accepts test targets, not shell commands")
        name = target.split("::", 1)[0]
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
            raise ValueError("verification target is not a repository-local test file: " + target)
        result.append(target)
    return list(dict.fromkeys(result))


def explain(project, *, engine=False, level="affected", tests=(), changed_from=None):
    """No Orchestrator construction: explain cannot initialize project state."""
    project = Path(project).resolve()
    if engine:
        targets = selected_tests(project, tests)
        return {"ok": True, "read_only": True, "scope": "engine", "level": level,
                "targets": targets, "commands": [shlex.join(["python", "-m", "pytest", "-q", *targets])],
                "full_proof_required_before_engine_switch": True}
    from .config import load_project_config
    from .verification_selection import select_verification_steps
    config = load_project_config(project)
    changed = git(project, "diff", "--name-only", changed_from or "HEAD").splitlines()
    selection = select_verification_steps(config.gates.steps, project, config.gates,
        level="affected" if level == "focused" else level,
        changed_paths=[target.split("::", 1)[0] for target in tests] if tests else changed)
    return {"ok": True, "read_only": True, "scope": "project", "level": selection.level,
            "proof_ids": selection.proof_ids, "unmapped_paths": selection.unmapped_paths,
            "forced_release_reason": selection.forced_release_reason,
            "steps": [asdict(step) for step in selection.steps]}


def engine_runner(root, project, python, *, repository=None, fresh=False):
    from .models import AccelerationConfig, AutonomyConfig
    from .self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision
    execution = SimpleNamespace(acceleration=AccelerationConfig(), autonomy=AutonomyConfig())
    orchestrator = SimpleNamespace(config=SimpleNamespace(execution=execution))
    runner = AutoAgentsSelfRepairRunner(orchestrator, target_project_root=Path(project),
        error=RuntimeError("managed engine verification"), decision=SelfRepairDecision(False))
    runner.repo_root = Path(root)
    runner._engine_source_root = Path(repository or root)
    runner._real_project_root = Path(project)
    runner._verification_python_cache = python
    runner._verification_fresh = fresh
    return runner


def project_focused(orchestrator, targets, *, fresh=False, sandboxed=False):
    from .gates import run_gate_plan
    from .verification_selection import select_verification_steps
    targets = selected_tests(orchestrator.project_root, targets)
    if not targets:
        raise ValueError("focused verification requires test targets")
    steps = orchestrator.config.gates.steps
    selected = []
    for target in targets:
        file = target.split("::", 1)[0]
        owners = [step for step in steps if any(
            file == path.split("::", 1)[0] for path in step.targets)]
        if not owners:
            # Newly added Python tests inherit the project's existing pytest
            # environment, not the engine interpreter or an arbitrary command.
            owners = [step for step in steps if step.runner == "pytest"] if file.endswith(".py") else []
        if not owners:
            raise ValueError("test target has no configured verification runner: " + target)
        owner = owners[0]
        selected.append(replace(owner, targets=[target]))
    # Build commands through the same trusted plan builder used by gates.
    from .gates import resolve_gate_plan_from_verification_steps
    config = replace(orchestrator.config.gates, steps=selected, commands=[], parallel_groups=[])
    plan = resolve_gate_plan_from_verification_steps(selected, orchestrator.project_root)
    with orchestrator._gate_executor_context(plan.metadata) as executor:
        if executor is None:
            raise RuntimeError("managed focused verification requires configured isolation")
        local = getattr(executor, "local", executor)
        if sandboxed:
            local.sandbox_target = orchestrator.project_root
            local.result_cache.environment_fingerprint = digest([
                local.result_cache.environment_fingerprint, "credential-free-network-namespace-v1"])
            # Offline sandbox proofs must not be confused with gates that
            # explicitly depend on a live service or operator credentials.
            if any(step.operator_input_bindings or step.requires for step in selected):
                raise RuntimeError("this proof requires native environment verification, not model self-testing")
        previous = executor.use_result_cache if hasattr(executor, "use_result_cache") else None
        if fresh and previous is not None:
            executor.use_result_cache = False
        result = run_gate_plan(plan.commands, plan.parallel_groups, orchestrator.project_root,
            collect_all=False, gate_executor=executor,
            parallel_workers=orchestrator._gate_parallel_workers(),
            command_timeout_seconds=config.command_timeout_seconds)
    return {"ok": result.ok, "scope": "project", "level": "focused", "summary": result.summary,
            "commands": [asdict(command) for command in result.commands],
            "artifacts": {key: value for command in result.commands for key, value in command.artifacts.items()}}


def execute_engine(project, *, tests=(), level="focused", fresh=False, repository=None, python=None, real_project=None):
    from .root_cause import RootCauseCoordinator
    project = Path(project).resolve()
    if level == "release" and tests:
        raise ValueError("release proof cannot be narrowed with --test")
    targets = selected_tests(project, tests)
    if not targets:
        raise ValueError("engine verification collected no test targets")
    with tempfile.TemporaryDirectory(prefix="managed-engine-verify-") as temporary:
        root = Path(temporary) / "engine"
        RootCauseCoordinator._copy_diagnostic_tree(project, root)
        if level == "release" and git(root, "status", "--porcelain"):
            # Shards use Git worktrees: their ref must include the captured
            # dirty/untracked files, not just the user's previous HEAD.
            git(root, "add", "-A")
            if git(root, "diff", "--cached", "--quiet", check=False).returncode:
                git(root, "-c", "user.name=auto-agents-verification", "-c",
                    "user.email=verification@example.invalid", "commit", "-m", "verification: freeze source snapshot")
        runner = engine_runner(root, real_project or project, python or sys.executable, repository=repository or project, fresh=fresh)
        runner._verification_read_roots = [project, root]
        if level == "release":
            result = runner._run_full_suite_shards(root)
        else:
            result = runner._run_verification_commands([shlex.join(["python", "-m", "pytest", "-q", *targets])], root)
        return {"ok": result.ok and not result.recoverable, "scope": "engine", "level": level,
                "targets": targets, "verification": result.to_dict()}


def request_from_cli(args):
    context = getattr(args, "verification_context", "")
    if not context:
        if os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_WORKER"):
            raise PermissionError("model verification requires a bound supervisor context")
        return execute_engine(args.project, tests=args.test, level=args.level, fresh=args.fresh)
    configured, token = context.rsplit(":", 1)
    config = json.loads(Path(configured).read_text())
    reply = rpc(config, {"op": "verify-submit", "context": token, "tests": args.test,
                         "level": args.level, "fresh": args.fresh})
    import time
    while True:
        status = rpc(config, {"op": "verify-status", "context": token, "verification": reply["verification"]})
        if status["state"] in {"completed", "failed", "cancelled"}:
            return status.get("result", {"ok": False, "error": status["state"]})
        time.sleep(0.25)


def attach_context(orchestrator, request):
    """Only the host owning the workflow/repair worker can mint this token."""
    from .prompting import PromptBlock
    if request.purpose not in {"implement", "fix", "self_repair"} or request.prompt_metadata.get("verification_context"):
        return request
    acceleration = getattr(getattr(getattr(orchestrator, "config", None), "execution", None), "acceleration", None)
    if not acceleration or not acceleration.enabled:
        return request
    registration = getattr(orchestrator, "_repair_registration", None)
    configured = os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_CONFIG")
    if not registration and not configured:
        return request
    config = registration["config"] if registration else json.loads(Path(configured).read_text())
    contexts = getattr(orchestrator, "_managed_verification_contexts", {})
    key = (str(Path(request.cwd).resolve()), request.purpose)
    try:
        reply = contexts.get(key) or rpc(config, {"op": "verify-context", "job": os.environ.get("AUTO_AGENTS_REPAIR_JOB", ""),
            "subscriber": registration["subscriber"] if registration else "", "workspace": str(Path(request.cwd).resolve()),
            "engine": request.purpose == "self_repair", "runtime": str(Path(__file__).resolve().parents[2]),
            "python": sys.executable,
            "resource_environment": {key: value for key, value in os.environ.items() if key in {
                "AUTO_AGENTS_CLUSTER_HOME", "AUTO_AGENTS_WORKER_ROOT", "AUTO_AGENTS_WORKER_SLOTS", "AUTO_AGENTS_WORKER_CONFIG", "AUTO_AGENTS_VERIFICATION_ROOT"}}})
    except (OSError, RuntimeError):
        return request  # Legacy supervisor: ordinary self-tests are diagnostic only.
    contexts[key] = reply
    orchestrator._managed_verification_contexts = contexts
    token = str(Path(config["root"]) / "operator.json") + ":" + reply["context"]
    command = shlex.join([sys.executable, str(Path(__file__).resolve().parents[2] / "auto_agents.py"),
                         "verify", "--project", str(request.cwd), "--level", "focused",
                         "--verification-context", token])
    if request.purpose == "self_repair":
        command += " --engine"
    guide = ("Run authoritative focused tests through the trusted verification executor:\n"
             + command + " --test <repository test path or pytest node ID>\n"
             "Repeat --test for additional targets. Successful receipts are reused by later verification. "
             "Do not write proof/cache records. Raw shell tests are allowed for diagnosis only; "
             "their output cannot establish acceptance. Do not request full release verification while editing.")
    spec = request.prompt_spec
    if spec:
        spec = replace(spec, blocks=(*spec.blocks, PromptBlock(guide, "verification.managed")))
    return replace(request, prompt=str(request.prompt) + "\n" + guide, prompt_spec=spec,
                   prompt_metadata={**request.prompt_metadata, "verification_context": True})
