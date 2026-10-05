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

from .local_io import digest, git


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
            "selection_reasons": selection.selection_reasons,
            "steps": [asdict(step) for step in selection.steps]}




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
    import subprocess
    project = Path(project).resolve()
    if level == "release" and tests:
        raise ValueError("release proof cannot be narrowed")
    targets = selected_tests(project, tests)
    if not targets:
        raise ValueError("No engine test targets")
    result = subprocess.run([python or sys.executable,"-m","pytest","-q",*targets],cwd=project,
        env={**os.environ,"PYTHONPATH":str(project/"src"),"AUTO_AGENTS_NO_SUPERVISOR":"1"},capture_output=True,text=True)
    return {"ok":result.returncode==0,"scope":"engine","level":level,"targets":targets,
            "verification":{"returncode":result.returncode,"stdout":result.stdout,"stderr":result.stderr}}


def request_from_cli(args):
    if getattr(args,"verification_context",""):
        raise ValueError("Legacy verification context retired")
    return execute_engine(args.project,tests=args.test,level=args.level,fresh=args.fresh)


def attach_context(orchestrator, request):
    return request
