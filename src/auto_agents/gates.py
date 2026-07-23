from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import os
import re
import shlex
import threading
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Protocol, Sequence

from .models import (
    DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS,
    CommandResult,
    GateParallelGroup,
    GateResult,
    VerificationStep,
)
from .process_supervision import run_supervised_shell_command


_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_VITEST_FAILED = re.compile(
    r"^\s*FAIL\s+(\S+\.(?:test|spec)\.[jt]sx?(?:\s+>\s+.+)?)$",
    re.MULTILINE,
)
_UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR):\s+(.+)$", re.MULTILINE)
GateProgressCallback = Callable[[str, str, float], None]


class GateCommandExecutor(Protocol):
    def priority(self, command: str) -> tuple[int, str]: ...

    def run(
        self,
        command: str,
        *,
        lane: str = "",
        timeout_seconds: float,
        adaptive_timeout_enabled: bool,
        idle_timeout_seconds: float,
        cancel_event: Optional[threading.Event] = None,
        progress: Optional[GateProgressCallback] = None,
    ) -> CommandResult: ...


@dataclass
class FailureExtraction:
    failure_ids: List[str]
    comparable: bool
    non_comparable_ids: List[str]


@dataclass
class GateCommandMetadata:
    resource_class: str = "normal"
    requires: List[str] = field(default_factory=list)
    exclusive_resources: List[str] = field(default_factory=list)
    artifact_globs: List[str] = field(default_factory=list)


@dataclass
class ResolvedGatePlan:
    commands: List[str]
    parallel_groups: List[GateParallelGroup]
    cache_scopes: dict[str, str]
    raw_command_count: int
    metadata: dict[str, GateCommandMetadata] = field(default_factory=dict)

    @property
    def unique_command_count(self) -> int:
        return len(self.commands) + sum(
            len(group.commands) for group in self.parallel_groups
        )

    @property
    def duplicates_removed(self) -> int:
        return max(0, self.raw_command_count - self.unique_command_count)


class GateCommandTimeoutError(RuntimeError):
    """A baseline gate could not produce a finite, cacheable result."""

    def __init__(
        self,
        message: str,
        *,
        result: Optional[CommandResult] = None,
        context: str = "",
        baseline: bool = False,
        task_id: str = "",
    ) -> None:
        super().__init__(message)
        self.result = result
        self.context = context
        self.baseline = baseline
        self.task_id = task_id


def first_terminated_command(gate_result: GateResult) -> Optional[CommandResult]:
    return next(
        (item for item in gate_result.commands if item.termination_reason),
        None,
    )


def _pytest_failure_ids(output: str) -> List[str]:
    ids: List[str] = []
    for item in _PYTEST_FAILED.findall(output):
        candidate = item.strip()
        if candidate.startswith("("):
            continue
        if ".py" not in candidate and "::" not in candidate:
            continue
        ids.append(candidate)
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("FAILED "):
            continue
        if not re.search(r"\s+(?:FAILED|ERROR)(?:\s+\[\s*\d+%\])?$", candidate):
            continue
        candidate = re.sub(r"\s+(?:FAILED|ERROR)(?:\s+\[\s*\d+%\])?$", "", candidate).strip()
        if ".py" not in candidate or "::" not in candidate:
            continue
        if candidate not in ids:
            ids.append(candidate)
    return ids


def _failure_summary(result: CommandResult) -> str:
    if result.termination_reason == "timeout":
        suffix = "; process group cleanup incomplete" if result.cleanup_incomplete else ""
        return (
            f"command timed out after {result.timeout_seconds:g}s: "
            f"{result.command}{suffix}"
        )
    if result.termination_reason == "stalled":
        suffix = "; process group cleanup incomplete" if result.cleanup_incomplete else ""
        return f"command stalled without observable activity: {result.command}{suffix}"
    if result.termination_reason:
        return f"command {result.termination_reason}: {result.command}"
    details = result.stderr or result.stdout or f"exit code {result.returncode}"
    details = " ".join(details.split())
    return f"command failed: {result.command} ({details})"


def command_from_verification_step(step: VerificationStep, project_root: Optional[Path] = None) -> str:
    runner = step.runner.strip().lower()
    kind = step.kind.strip().lower() or "test"
    targets = [item.strip() for item in step.targets if item.strip()]
    args = [item.strip() for item in step.args if item.strip()]
    if kind == "test" and runner == "pytest":
        local_python = project_root / ".conda" / "bin" / "python" if project_root is not None else None
        if local_python is not None and local_python.exists():
            parts = ["./.conda/bin/python", "-m", "pytest", "-q"]
        else:
            parts = ["conda", "run", "-p", "./.conda", "python", "-m", "pytest", "-q"]
        parts.extend(args)
        parts.extend(targets or ["tests"])
        return " ".join(parts)
    if kind == "test" and runner == "vitest":
        parts = ["npm", "exec", "--", "vitest", "run"]
        parts.extend(args)
        parts.extend(targets)
        return " ".join(parts)
    raise ValueError(f"unsupported verification step runner: {step.runner or '<empty>'}")


def expand_pytest_directory_steps(
    steps: Sequence[VerificationStep],
    project_root: Path,
) -> List[VerificationStep]:
    expanded: List[VerificationStep] = []
    for step in steps:
        runner = step.runner.strip().lower()
        kind = step.kind.strip().lower() or "test"
        if kind != "test" or runner != "pytest":
            expanded.append(step)
            continue

        raw_targets = [item.strip() for item in step.targets if item.strip()] or ["tests"]
        seen_targets: set[str] = set()
        for target in raw_targets:
            test_files = _pytest_files_for_target(project_root, target)
            if not test_files:
                if target not in seen_targets:
                    expanded.append(
                        VerificationStep(
                            kind=step.kind,
                            runner=step.runner,
                            targets=[target],
                            args=list(step.args),
                            parallel_safe=step.parallel_safe,
                            cadence=step.cadence,
                            cache_scope=step.cache_scope,
                            resource_class=step.resource_class,
                            requires=list(step.requires),
                            exclusive_resources=list(step.exclusive_resources),
                            artifact_globs=list(step.artifact_globs),
                        )
                    )
                    seen_targets.add(target)
                continue
            for test_file in test_files:
                if test_file in seen_targets:
                    continue
                expanded.append(
                    VerificationStep(
                        kind=step.kind,
                        runner=step.runner,
                        targets=[test_file],
                        args=list(step.args),
                        parallel_safe=step.parallel_safe,
                        cadence=step.cadence,
                        cache_scope=step.cache_scope,
                        resource_class=step.resource_class,
                        requires=list(step.requires),
                        exclusive_resources=list(step.exclusive_resources),
                        artifact_globs=list(step.artifact_globs),
                    )
                )
                seen_targets.add(test_file)
    return expanded


def _pytest_files_for_target(project_root: Path, target: str) -> List[str]:
    if "::" in target:
        return []
    root = project_root.resolve()
    candidate = Path(target)
    resolved = candidate if candidate.is_absolute() else root / candidate
    if not resolved.is_dir():
        return []
    files = {
        path.resolve()
        for pattern in ("test_*.py", "*_test.py")
        for path in resolved.rglob(pattern)
        if path.is_file()
    }
    out: List[str] = []
    for path in sorted(files):
        try:
            out.append(path.relative_to(root).as_posix())
        except ValueError:
            continue
    return out


def commands_from_verification_steps(
    steps: Sequence[VerificationStep],
    project_root: Optional[Path] = None,
) -> List[str]:
    commands: List[str] = []
    seen: set[str] = set()
    for step in steps:
        command = command_from_verification_step(step, project_root=project_root)
        if command in seen:
            continue
        seen.add(command)
        commands.append(command)
    return commands


def resolve_gate_plan_from_verification_steps(
    steps: Sequence[VerificationStep],
    project_root: Optional[Path] = None,
    *,
    phase: str = "final",
) -> ResolvedGatePlan:
    if phase not in {"implement", "final"}:
        raise ValueError(f"unsupported gate plan phase: {phase}")

    occurrences: dict[str, List[VerificationStep]] = {}
    order: List[str] = []
    raw_count = 0
    for step in steps:
        cadence = step.cadence.strip().lower() or "implement_and_final"
        if phase == "implement" and cadence == "final_only":
            continue
        command = command_from_verification_step(step, project_root=project_root)
        raw_count += 1
        if command not in occurrences:
            occurrences[command] = []
            order.append(command)
        occurrences[command].append(step)

    sequential: List[str] = []
    grouped: dict[str, List[str]] = {}
    runner_order: List[str] = []
    cache_scopes: dict[str, str] = {}
    metadata: dict[str, GateCommandMetadata] = {}
    for command in order:
        command_steps = occurrences[command]
        parallel_safe = all(step.parallel_safe for step in command_steps)
        cache_scope = (
            "run_context"
            if any(
                (step.cache_scope.strip().lower() or "run_context") != "source"
                for step in command_steps
            )
            else "source"
        )
        cache_scopes[command] = cache_scope
        resource_class = (
            "heavy"
            if any(
                step.resource_class.strip().lower() == "heavy"
                or any(
                    requirement.strip().lower() in {"chrome", "browser", "ffmpeg"}
                    for requirement in step.requires
                )
                for step in command_steps
            )
            else "normal"
        )
        metadata[command] = GateCommandMetadata(
            resource_class=resource_class,
            requires=list(
                dict.fromkeys(
                    requirement.strip()
                    for step in command_steps
                    for requirement in step.requires
                    if requirement.strip()
                )
            ),
            exclusive_resources=list(
                dict.fromkeys(
                    resource.strip()
                    for step in command_steps
                    for resource in step.exclusive_resources
                    if resource.strip()
                )
            ),
            artifact_globs=list(
                dict.fromkeys(
                    pattern.strip()
                    for step in command_steps
                    for pattern in step.artifact_globs
                    if pattern.strip()
                )
            ),
        )
        if not parallel_safe:
            sequential.append(command)
            continue
        runner = command_steps[0].runner.strip().lower() or "test"
        if runner not in grouped:
            grouped[runner] = []
            runner_order.append(runner)
        grouped[runner].append(command)

    groups = [
        GateParallelGroup(name=f"steps-{runner}", commands=grouped[runner])
        for runner in runner_order
        if grouped[runner]
    ]
    return ResolvedGatePlan(
        commands=sequential,
        parallel_groups=groups,
        cache_scopes=cache_scopes,
        raw_command_count=raw_count,
        metadata=metadata,
    )


def gate_plan_from_verification_steps(
    steps: Sequence[VerificationStep],
    project_root: Optional[Path] = None,
) -> tuple[List[str], List[GateParallelGroup]]:
    """Build a gate plan without inferring concurrency safety.

    Only steps explicitly marked ``parallel_safe`` enter a parallel group.
    Unmarked and legacy steps remain sequential.
    """
    plan = resolve_gate_plan_from_verification_steps(
        steps,
        project_root,
        phase="final",
    )
    return plan.commands, plan.parallel_groups


def build_failure_identity_diagnostic_command(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    if not parts:
        return ""

    pytest_index = -1
    if "pytest" in parts:
        pytest_index = parts.index("pytest")
    else:
        for index in range(len(parts) - 2):
            if parts[index + 1] == "-m" and parts[index + 2] == "pytest":
                pytest_index = index + 2
                break
    if pytest_index >= 0:
        filtered = [part for part in parts if part not in {"-q", "--quiet", "-qq"}]
        for flag in ("-x", "-vv", "-rA", "--tb=short", "console_output_style=classic"):
            if flag in filtered:
                filtered = [part for part in filtered if part != flag]
        if "-o" in filtered:
            for index in range(len(filtered) - 1):
                if filtered[index] == "-o" and filtered[index + 1] == "console_output_style=classic":
                    del filtered[index:index + 2]
                    break
        insert_at = filtered.index("pytest") + 1 if "pytest" in filtered else pytest_index + 1
        filtered[insert_at:insert_at] = ["-vv", "-rA", "--tb=short", "-o", "console_output_style=classic"]
        return shlex.join(filtered)

    if "vitest" in parts:
        filtered = [part for part in parts if part not in {"--reporter=verbose"}]
        insert_at = filtered.index("vitest") + 1
        filtered[insert_at:insert_at] = ["--reporter=verbose"]
        return shlex.join(filtered)

    return ""


def _run_command(
    command: str,
    cwd: Path,
    *,
    timeout_seconds: float = DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS,
    adaptive_timeout_enabled: bool = False,
    idle_timeout_seconds: float = DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS,
    cancel_event: Optional[threading.Event] = None,
    progress: Optional[GateProgressCallback] = None,
) -> CommandResult:
    if cancel_event is not None and cancel_event.is_set():
        return CommandResult(
            command=command,
            ok=False,
            returncode=130,
            stderr="command cancelled because a peer gate command timed out",
            termination_reason="cancelled",
            timeout_seconds=float(timeout_seconds),
        )
    env = dict(os.environ)
    env["PYTEST_CURRENT_TEST"] = "auto_agents_gate_run"
    env["AUTO_AGENTS_TEST"] = "True"
    env["TESTING"] = "True"
    if progress is not None:
        progress("start", command, 0.0)
    process = run_supervised_shell_command(
        command,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        adaptive_timeout_enabled=adaptive_timeout_enabled,
        idle_timeout_seconds=idle_timeout_seconds,
        kind="gate",
        cancel_event=cancel_event,
        progress=(
            (lambda event, elapsed: progress(event, command, elapsed))
            if progress is not None
            else None
        ),
    )
    result = CommandResult(
        command=command,
        ok=process.returncode == 0 and not process.termination_reason,
        returncode=process.returncode,
        stdout=process.stdout,
        stderr=process.stderr,
        duration_seconds=process.duration_seconds,
        termination_reason=process.termination_reason,
        timeout_seconds=process.timeout_seconds,
        cleanup_incomplete=process.cleanup_incomplete,
        last_activity_seconds=process.last_activity_seconds,
        activity_kind=process.activity_kind,
        process_snapshot=process.process_snapshot,
    )
    if progress is not None:
        progress("finish", command, result.duration_seconds)
    return result


def _run_parallel_commands(
    commands: Sequence[str],
    cwd: Path,
    *,
    max_workers: int = 2,
    timeout_seconds: float = DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS,
    adaptive_timeout_enabled: bool = False,
    idle_timeout_seconds: float = DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS,
    progress: Optional[GateProgressCallback] = None,
    gate_executor: Optional[GateCommandExecutor] = None,
    cancel_on_failure: bool = False,
) -> List[CommandResult]:
    if not commands:
        return []
    if len(commands) == 1:
        return [
            (
                gate_executor.run(
                    commands[0],
                    timeout_seconds=timeout_seconds,
                    adaptive_timeout_enabled=adaptive_timeout_enabled,
                    idle_timeout_seconds=idle_timeout_seconds,
                    progress=progress,
                )
                if gate_executor is not None
                else _run_command(
                    commands[0], cwd, timeout_seconds=timeout_seconds,
                    adaptive_timeout_enabled=adaptive_timeout_enabled,
                    idle_timeout_seconds=idle_timeout_seconds, progress=progress
                )
            )
        ]

    results: List[CommandResult] = [None] * len(commands)  # type: ignore[list-item]
    cancel_event = threading.Event()
    ordered_commands = list(enumerate(commands))
    if gate_executor is not None:
        ordered_commands.sort(key=lambda item: gate_executor.priority(item[1]))
    with ThreadPoolExecutor(max_workers=max(1, min(len(commands), max_workers))) as pool:
        future_to_index = {
            pool.submit(
                (
                    gate_executor.run
                    if gate_executor is not None
                    else _run_command
                ),
                command,
                **(
                    {
                        "timeout_seconds": timeout_seconds,
                        "adaptive_timeout_enabled": adaptive_timeout_enabled,
                        "idle_timeout_seconds": idle_timeout_seconds,
                        "cancel_event": cancel_event,
                        "progress": progress,
                    }
                    if gate_executor is not None
                    else {
                        "cwd": cwd,
                        "timeout_seconds": timeout_seconds,
                        "adaptive_timeout_enabled": adaptive_timeout_enabled,
                        "idle_timeout_seconds": idle_timeout_seconds,
                        "cancel_event": cancel_event,
                        "progress": progress,
                    }
                ),
            ): index
            for index, command in ordered_commands
        }
        for future in as_completed(future_to_index):
            try:
                result = future.result()
                results[future_to_index[future]] = result
                if cancel_on_failure and not result.ok:
                    cancel_event.set()
            except BaseException:
                cancel_event.set()
                raise
    return results


def run_commands(
    commands: Iterable[str],
    cwd: Path,
    *,
    command_timeout_seconds: float = DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS,
    adaptive_timeout_enabled: bool = False,
    command_idle_timeout_seconds: float = DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS,
    progress: Optional[GateProgressCallback] = None,
) -> GateResult:
    results: List[CommandResult] = []
    ok = True
    summary = "all commands passed"
    for command in commands:
        result = _run_command(
            command, cwd, timeout_seconds=command_timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=command_idle_timeout_seconds, progress=progress
        )
        results.append(result)
        if not result.ok:
            ok = False
            summary = _failure_summary(result)
            break
    return GateResult(ok=ok, commands=results, summary=summary)


def run_commands_collect_all(
    commands: Iterable[str],
    cwd: Path,
    *,
    command_timeout_seconds: float = DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS,
    adaptive_timeout_enabled: bool = False,
    command_idle_timeout_seconds: float = DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS,
    progress: Optional[GateProgressCallback] = None,
) -> GateResult:
    """Run *all* commands, collecting results even after failures.

    Unlike ``run_commands`` this does **not** short-circuit on the first
    failure — it executes every command so that callers can compare the
    full set of failures against a known baseline.
    """
    results: List[CommandResult] = []
    ok = True
    summaries: List[str] = []
    for command in commands:
        result = _run_command(
            command, cwd, timeout_seconds=command_timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=command_idle_timeout_seconds, progress=progress
        )
        results.append(result)
        if not result.ok:
            ok = False
            summaries.append(_failure_summary(result))
            if result.termination_reason:
                break
    summary = "all commands passed" if ok else "; ".join(summaries)
    return GateResult(ok=ok, commands=results, summary=summary)


def run_gate_plan(
    commands: Iterable[str],
    parallel_groups: Sequence[GateParallelGroup],
    cwd: Path,
    *,
    collect_all: bool,
    parallel_workers: int = 2,
    command_timeout_seconds: float = DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS,
    adaptive_timeout_enabled: bool = False,
    command_idle_timeout_seconds: float = DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS,
    progress: Optional[GateProgressCallback] = None,
    gate_executor: Optional[GateCommandExecutor] = None,
) -> GateResult:
    results: List[CommandResult] = []
    summaries: List[str] = []
    ok = True

    for command in commands:
        result = (
            gate_executor.run(
                command,
                lane="serial",
                timeout_seconds=command_timeout_seconds,
                adaptive_timeout_enabled=adaptive_timeout_enabled,
                idle_timeout_seconds=command_idle_timeout_seconds,
                progress=progress,
            )
            if gate_executor is not None
            else _run_command(
                command, cwd, timeout_seconds=command_timeout_seconds,
                adaptive_timeout_enabled=adaptive_timeout_enabled,
                idle_timeout_seconds=command_idle_timeout_seconds, progress=progress
            )
        )
        results.append(result)
        if result.ok:
            continue
        ok = False
        summaries.append(_failure_summary(result))
        if result.termination_reason or not collect_all:
            return GateResult(ok=False, commands=results, summary="; ".join(summaries))

    for group in parallel_groups:
        group_results = _run_parallel_commands(
            group.commands,
            cwd,
            max_workers=parallel_workers,
            timeout_seconds=command_timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=command_idle_timeout_seconds,
            progress=progress,
            gate_executor=gate_executor,
            cancel_on_failure=not collect_all,
        )
        results.extend(group_results)
        failed = [result for result in group_results if not result.ok]
        if not failed:
            continue
        ok = False
        reportable = [
            result
            for result in failed
            if result.termination_reason != "cancelled"
        ] or failed[:1]
        summaries.extend(_failure_summary(result) for result in reportable)
        if any(result.termination_reason for result in failed) or not collect_all:
            break

    summary = "all commands passed" if ok else "; ".join(summaries)
    return GateResult(ok=ok, commands=results, summary=summary)


def extract_failure_info(gate_result: GateResult) -> FailureExtraction:
    """Extract a list of unique failure identifiers from gate results.

    For pytest commands the function tries to pull individual ``FAILED``
    test node IDs from the output.  If a command fails without test-level
    failures, the result is intentionally marked non-comparable so retry
    logic cannot treat a command-level failure as the same test failure.
    """
    failures: List[str] = []
    non_comparable: List[str] = []
    for cmd_result in gate_result.commands:
        if cmd_result.ok:
            continue
        if cmd_result.termination_reason:
            prefix = (
                "cmd-timeout"
                if cmd_result.termination_reason == "timeout"
                else "cmd-stalled"
                if cmd_result.termination_reason == "stalled"
                else "cmd-terminated"
            )
            non_comparable.append(f"{prefix}:{cmd_result.command}")
            continue
        combined = f"{cmd_result.stdout}\n{cmd_result.stderr}"
        pytest_ids = _pytest_failure_ids(combined)
        if pytest_ids:
            failures.extend(pytest_ids)
            continue
        vitest_ids = [item.strip() for item in _VITEST_FAILED.findall(combined) if item.strip()]
        if vitest_ids:
            failures.extend(vitest_ids)
            continue
        unittest_ids = [item.strip() for item in _UNITTEST_FAILED.findall(combined) if item.strip()]
        if unittest_ids:
            failures.extend(unittest_ids)
            continue
        non_comparable.append(f"cmd:{cmd_result.command}")
    comparable = bool(failures) and not non_comparable
    return FailureExtraction(
        failure_ids=failures if comparable else failures + non_comparable,
        comparable=comparable,
        non_comparable_ids=non_comparable,
    )


def extract_failure_ids(gate_result: GateResult) -> List[str]:
    return extract_failure_info(gate_result).failure_ids
