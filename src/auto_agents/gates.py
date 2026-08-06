from __future__ import annotations

from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass, field, replace
import fnmatch
import os
import re
import shlex
import threading
import time
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, List, Optional, Protocol, Sequence

from .gate_timing import GateTimingStore
from .models import (
    DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS,
    CommandResult,
    GateParallelGroup,
    GateResult,
    InfrastructureFailureMarker,
    VerificationStep,
)
from .process_supervision import run_supervised_shell_command


_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_VITEST_FAILED = re.compile(
    r"^\s*FAIL\s+(\S+\.(?:test|spec)\.[jt]sx?(?:\s+>\s+.+)?)$",
    re.MULTILINE,
)
_UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR):\s+(.+)$", re.MULTILINE)
_STANDARD_INFRA_FAILURE = re.compile(
    r"^AUTO_AGENTS_INFRA_FAILURE\s+id=(?P<id>[a-z][a-z0-9_-]{1,63})\b"
    r"(?:\s+capability=(?P<capability>[a-z][a-z0-9_-]{1,31}))?"
    r"(?:\s+contract=(?P<contract>[a-z][a-z0-9_-]{1,31}))?"
    r"(?:\s+repair_scope=(?P<repair_scope>"
    r"target_project|verification_contract|execution_environment|unknown))?"
    r"(?=$|[\s:])",
    re.IGNORECASE,
)
_BUILTIN_INFRA_MARKERS = (
    (
        "browser_verification_infrastructure_failed",
        re.compile(
            r"^(?:browser_verification_infrastructure_failed|"
            r"BrowserVerificationInfrastructureError)(?=$|[\s:])"
        ),
    ),
)
_BROWSER_ARTIFACT_PUBLICATION_CONFLICT = re.compile(
    r"(?:BrowserArtifactPublicationConflictError|"
    r"browser_artifact_publication_conflict:)"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIAGNOSTIC_PREFIX = re.compile(
    r"^(?:(?:E|ERROR|Error|FAIL|FAILED)\s*[:>]?\s+|"
    r"(?:[>→×✖❯])\s*)"
)
GateProgressCallback = Callable[[str, str, float], None]


class GateCommandExecutor(Protocol):
    def priority(self, command: str) -> tuple[object, ...]: ...

    def estimated_duration(self, command: str) -> Optional[float]: ...

    def required_slots(self, command: str) -> int: ...

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
    cpu_slots: int = 0
    memory_mb: int = 0
    memory_reserve_mb: int = 0
    memory_guard: str = "off"
    requires: List[str] = field(default_factory=list)
    exclusive_resources: List[str] = field(default_factory=list)
    dynamic_ports: List[str] = field(default_factory=list)
    artifact_globs: List[str] = field(default_factory=list)
    serial_reason: str = ""
    cache_scope: str = "run_context"
    result_cache_scope: str = "off"


@dataclass
class ResolvedGatePlan:
    commands: List[str]
    parallel_groups: List[GateParallelGroup]
    cache_scopes: dict[str, str]
    raw_command_count: int
    metadata: dict[str, GateCommandMetadata] = field(default_factory=dict)
    result_cache_scopes: dict[str, str] = field(default_factory=dict)

    @property
    def unique_command_count(self) -> int:
        return len(self.commands) + sum(
            len(group.commands) for group in self.parallel_groups
        )

    @property
    def duplicates_removed(self) -> int:
        return max(0, self.raw_command_count - self.unique_command_count)


class GateCommandExecutionError(RuntimeError):
    """A gate command failed for reasons that require incident recovery."""

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


class GateCommandTimeoutError(GateCommandExecutionError):
    """A baseline gate could not produce a finite, cacheable result."""


class GateCommandInfrastructureError(GateCommandExecutionError):
    """A test reported that verification infrastructure could not run."""


def first_terminated_command(gate_result: GateResult) -> Optional[CommandResult]:
    return next(
        (item for item in gate_result.commands if item.termination_reason),
        None,
    )


def first_infrastructure_command(
    gate_result: GateResult,
) -> Optional[CommandResult]:
    return next(
        (item for item in gate_result.commands if item.infrastructure_error),
        None,
    )


def _normalized_diagnostic_line(raw: str) -> str:
    line = _ANSI_ESCAPE.sub("", raw).strip()
    while line:
        stripped = _DIAGNOSTIC_PREFIX.sub("", line, count=1)
        if stripped == line:
            break
        line = stripped.strip()
    return line


def _diagnostic_output_lines(result: CommandResult) -> List[str]:
    lines: List[str] = []
    for raw in f"{result.stdout}\n{result.stderr}".splitlines():
        line = _normalized_diagnostic_line(raw)
        if not line:
            continue
        if _STANDARD_INFRA_FAILURE.match(line) or any(
            pattern.match(line) for _, pattern in _BUILTIN_INFRA_MARKERS
        ):
            lines.append(line)
    return lines


def classify_reported_infrastructure_failure(
    result: CommandResult,
    markers: Sequence[InfrastructureFailureMarker] = (),
) -> CommandResult:
    """Promote an explicit test-reported infrastructure failure.

    Tests may use the stable ``AUTO_AGENTS_INFRA_FAILURE id=<id>`` protocol.
    A small built-in compatibility set recognizes existing browser verification
    failures, while project configuration can add literal diagnostic markers.
    """
    if result.ok or result.infrastructure_error or result.termination_reason:
        return result
    full_output = f"{result.stdout}\n{result.stderr}"
    lines = _diagnostic_output_lines(result)
    if not lines and not markers:
        return result
    diagnostic = "\n".join(lines)
    standard = next(
        (
            match
            for line in lines
            if (match := _STANDARD_INFRA_FAILURE.match(line)) is not None
        ),
        None,
    )
    failure_id = standard.group("id").lower() if standard else ""
    provenance_source = "standard" if standard else ""
    matched_line = standard.group(0) if standard else ""
    if standard:
        result.infrastructure_capability = (
            standard.group("capability") or ""
        ).lower()
        result.infrastructure_contract = (
            standard.group("contract") or ""
        ).lower()
        result.infrastructure_repair_scope = (
            standard.group("repair_scope") or ""
        ).lower()
    if not failure_id:
        for marker_id, pattern in _BUILTIN_INFRA_MARKERS:
            for line in lines:
                builtin = pattern.match(line)
                if builtin:
                    failure_id = marker_id
                    provenance_source = "builtin"
                    matched_line = builtin.group(0)
                    break
            if failure_id:
                break
    if not failure_id:
        lowered = full_output.lower()
        for marker in markers:
            if marker.contains.lower() in lowered:
                failure_id = marker.marker_id
                provenance_source = "configured"
                matched_line = marker.contains
                break
    if failure_id:
        result.infrastructure_error = True
        result.infrastructure_failure_id = failure_id
        result.process_snapshot = {
            **dict(result.process_snapshot),
            "reported_infrastructure_marker": {
                "source": provenance_source,
                "id": failure_id,
                "capability": result.infrastructure_capability,
                "contract": result.infrastructure_contract,
                "repair_scope": result.infrastructure_repair_scope,
                "matched": matched_line,
            },
        }
    return result


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
    *,
    max_batches_per_step: int = 8,
) -> List[VerificationStep]:
    expanded: List[VerificationStep] = []
    for step in steps:
        runner = step.runner.strip().lower()
        kind = step.kind.strip().lower() or "test"
        if kind != "test" or runner != "pytest":
            expanded.append(step)
            continue
        if step.max_batches == 1:
            # Preserve the directory target so the runner's own discovery,
            # ignore configuration, and session fixtures remain authoritative.
            expanded.append(step)
            continue

        exclude_patterns, remaining_args = _pytest_exclude_patterns(step.args)
        raw_targets = [item.strip() for item in step.targets if item.strip()] or ["tests"]
        seen_targets: set[str] = set()
        discovered_targets: list[str] = []
        fallback_targets: list[str] = []
        for target in raw_targets:
            test_files = _pytest_files_for_target(
                project_root,
                target,
                exclude_patterns=exclude_patterns,
            )
            if not test_files:
                if target not in seen_targets:
                    fallback_targets.append(target)
                    seen_targets.add(target)
                continue
            for test_file in test_files:
                if test_file in seen_targets:
                    continue
                discovered_targets.append(test_file)
                seen_targets.add(test_file)
        expanded.extend(
            replace(step, targets=[target], args=list(step.args))
            for target in fallback_targets
        )
        expanded.extend(
            replace(step, targets=batch, args=list(remaining_args))
            for batch in _balanced_target_batches(
                discovered_targets,
                project_root,
                max_batches=step.max_batches or max_batches_per_step,
                target_weights=_historical_target_weights(
                    step,
                    discovered_targets,
                    remaining_args,
                    project_root,
                ),
            )
        )
    return expanded


def _pytest_exclude_patterns(args: Sequence[str]) -> tuple[list[str], list[str]]:
    patterns: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = str(args[index]).strip()
        if arg in {"--ignore", "--ignore-glob"} and index + 1 < len(args):
            patterns.append(str(args[index + 1]).strip())
            index += 2
            continue
        if arg.startswith(("--ignore=", "--ignore-glob=")):
            patterns.append(arg.split("=", 1)[1].strip())
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return patterns, remaining


def _vitest_exclude_patterns(args: Sequence[str]) -> tuple[list[str], list[str]]:
    patterns: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(args):
        arg = str(args[index]).strip()
        if arg == "--exclude" and index + 1 < len(args):
            patterns.append(str(args[index + 1]).strip())
            index += 2
            continue
        if arg.startswith("--exclude="):
            patterns.append(arg.split("=", 1)[1].strip())
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return patterns, remaining


def _vitest_files_for_target(
    project_root: Path,
    target: str,
    *,
    exclude_patterns: Sequence[str] = (),
) -> List[str]:
    root = project_root.resolve()
    candidate = Path(target)
    resolved = candidate if candidate.is_absolute() else root / candidate
    if not resolved.is_dir():
        return []
    suffix = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$", re.IGNORECASE)
    out: list[str] = []
    for path in sorted(item for item in resolved.rglob("*") if item.is_file()):
        if not suffix.search(path.name):
            continue
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if any(
            fnmatch.fnmatch(relative, pattern)
            or relative == pattern.removeprefix("./")
            for pattern in exclude_patterns
            if pattern
        ):
            continue
        out.append(relative)
    return out


def _balanced_target_batches(
    targets: Sequence[str],
    project_root: Path,
    *,
    max_batches: int,
    target_weights: Optional[dict[str, float]] = None,
) -> list[list[str]]:
    """Bound process fan-out while spreading likely-expensive files.

    File size is only a stable first-run proxy. Once commands have timing
    history, the gate scheduler still orders the resulting batches by measured
    duration. Keeping the number of batches bounded avoids repeating expensive
    runner startup and session fixture work once per test file.
    """
    unique_targets = list(dict.fromkeys(targets))
    if not unique_targets:
        return []
    batch_count = min(len(unique_targets), max(1, int(max_batches)))
    if batch_count == len(unique_targets):
        return [[target] for target in unique_targets]

    weighted: list[tuple[float, str]] = []
    for target in unique_targets:
        weight = max(1.0, float((target_weights or {}).get(target, 0.0)))
        if weight <= 1.0 and target not in (target_weights or {}):
            path = project_root / target
            try:
                weight = float(max(1, path.stat().st_size))
            except OSError:
                weight = 1.0
        weighted.append((weight, target))
    weighted.sort(key=lambda item: (-item[0], item[1]))

    batches: list[list[str]] = [[] for _ in range(batch_count)]
    weights = [0] * batch_count
    for weight, target in weighted:
        index = min(range(batch_count), key=lambda item: (weights[item], item))
        batches[index].append(target)
        weights[index] += weight
    return [sorted(batch) for batch in batches if batch]


def _historical_target_weights(
    step: VerificationStep,
    targets: Sequence[str],
    args: Sequence[str],
    project_root: Path,
) -> dict[str, float]:
    store = GateTimingStore(project_root)
    estimates: dict[str, float] = {}
    sizes: dict[str, int] = {}
    for target in targets:
        command = command_from_verification_step(
            replace(step, targets=[target], args=list(args)),
            project_root=project_root,
        )
        estimate = store.estimate_any_environment(command, step)
        if estimate is not None and estimate > 0:
            estimates[target] = estimate
        try:
            sizes[target] = max(1, (project_root / target).stat().st_size)
        except OSError:
            sizes[target] = 1
    if not estimates:
        return {target: float(size) for target, size in sizes.items()}

    typical_estimate = median(estimates.values())
    typical_size = max(1.0, float(median(sizes.values())))
    weights = dict(estimates)
    for target, size in sizes.items():
        if target in weights:
            continue
        size_factor = max(
            0.25,
            min(4.0, (float(size) / typical_size) ** 0.5),
        )
        weights[target] = typical_estimate * size_factor
    return weights


def expand_vitest_directory_steps(
    steps: Sequence[VerificationStep],
    project_root: Path,
    *,
    max_batches_per_step: int = 8,
) -> List[VerificationStep]:
    expanded: list[VerificationStep] = []
    for step in steps:
        runner = step.runner.strip().lower()
        kind = step.kind.strip().lower() or "test"
        if kind != "test" or runner != "vitest":
            expanded.append(step)
            continue
        if step.max_batches == 1:
            # Vitest config can exclude files that are visible on disk. A
            # single batch must retain directory discovery instead of turning
            # excluded files into explicit CLI filters.
            expanded.append(step)
            continue
        exclude_patterns, remaining_args = _vitest_exclude_patterns(step.args)
        raw_targets = [item.strip() for item in step.targets if item.strip()]
        if not raw_targets:
            expanded.append(step)
            continue
        seen_targets: set[str] = set()
        discovered_targets: list[str] = []
        fallback_targets: list[str] = []
        for target in raw_targets:
            test_files = _vitest_files_for_target(
                project_root,
                target,
                exclude_patterns=exclude_patterns,
            )
            if not test_files:
                if target not in seen_targets:
                    fallback_targets.append(target)
                    seen_targets.add(target)
                continue
            for test_file in test_files:
                if test_file in seen_targets:
                    continue
                discovered_targets.append(test_file)
                seen_targets.add(test_file)
        expanded.extend(
            replace(step, targets=[target], args=list(step.args))
            for target in fallback_targets
        )
        expanded.extend(
            replace(step, targets=batch, args=list(remaining_args))
            for batch in _balanced_target_batches(
                discovered_targets,
                project_root,
                max_batches=step.max_batches or max_batches_per_step,
                target_weights=_historical_target_weights(
                    step,
                    discovered_targets,
                    remaining_args,
                    project_root,
                ),
            )
        )
    return expanded


def expand_verification_directory_steps(
    steps: Sequence[VerificationStep],
    project_root: Path,
    *,
    max_batches_per_step: int = 8,
) -> List[VerificationStep]:
    return expand_vitest_directory_steps(
        expand_pytest_directory_steps(
            steps,
            project_root,
            max_batches_per_step=max_batches_per_step,
        ),
        project_root,
        max_batches_per_step=max_batches_per_step,
    )


def _pytest_files_for_target(
    project_root: Path,
    target: str,
    *,
    exclude_patterns: Sequence[str] = (),
) -> List[str]:
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
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if any(
            fnmatch.fnmatch(relative, pattern)
            or relative == pattern.removeprefix("./")
            for pattern in exclude_patterns
            if pattern
        ):
            continue
        out.append(relative)
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
    result_cache_scopes: dict[str, str] = {}
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
        result_cache_order = {"observed_inputs": 0, "candidate": 1, "off": 2}
        result_cache_scope = max(
            (
                step.result_cache_scope.strip().lower() or "candidate"
                for step in command_steps
            ),
            key=lambda value: result_cache_order.get(value, 2),
        )
        result_cache_scopes[command] = result_cache_scope
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
        memory_guard_order = {"off": 0, "advisory": 1, "required": 2}
        memory_guard = max(
            (
                (step.memory_guard.strip().lower() or "off")
                for step in command_steps
            ),
            key=lambda value: memory_guard_order.get(value, 0),
        )
        metadata[command] = GateCommandMetadata(
            resource_class=resource_class,
            cpu_slots=max((step.cpu_slots for step in command_steps), default=0),
            memory_mb=max((step.memory_mb for step in command_steps), default=0),
            memory_reserve_mb=max(
                (step.memory_reserve_mb for step in command_steps),
                default=0,
            ),
            memory_guard=memory_guard,
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
            dynamic_ports=list(
                dict.fromkeys(
                    name.strip()
                    for step in command_steps
                    for name in step.dynamic_ports
                    if name.strip()
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
            serial_reason=next(
                (
                    step.serial_reason.strip()
                    for step in command_steps
                    if step.serial_reason.strip()
                ),
                "",
            ),
            cache_scope=cache_scope,
            result_cache_scope=result_cache_scope,
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
        result_cache_scopes=result_cache_scopes,
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


def _is_browser_artifact_publication_conflict(result: CommandResult) -> bool:
    if result.ok or result.termination_reason or result.infrastructure_error:
        return False
    return bool(
        _BROWSER_ARTIFACT_PUBLICATION_CONFLICT.search(
            f"{result.stdout}\n{result.stderr}"
        )
    )


def _run_with_browser_artifact_publication_confirmation(
    command: str,
    runner: Callable[[], CommandResult],
    *,
    progress: Optional[GateProgressCallback],
) -> CommandResult:
    first = runner()
    if not _is_browser_artifact_publication_conflict(first):
        return first
    if progress is not None:
        progress(
            "confirmation_retry",
            f"browser_artifact_publication_conflict command={command}",
            first.duration_seconds,
        )
    confirmed = runner()
    confirmed.duration_seconds += first.duration_seconds
    confirmed.process_snapshot = {
        **dict(confirmed.process_snapshot),
        "browser_artifact_publication_confirmation": {
            "attempts": 2,
            "first_returncode": first.returncode,
            "confirmed_ok": confirmed.ok,
            "confirmed_returncode": confirmed.returncode,
        },
    }
    return confirmed


def _run_command_once(
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
    return _run_with_browser_artifact_publication_confirmation(
        command,
        lambda: _run_command_once(
            command,
            cwd,
            timeout_seconds=timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=idle_timeout_seconds,
            cancel_event=cancel_event,
            progress=progress,
        ),
        progress=progress,
    )


def _run_executor_command(
    gate_executor: GateCommandExecutor,
    command: str,
    *,
    lane: str = "",
    timeout_seconds: float,
    adaptive_timeout_enabled: bool,
    idle_timeout_seconds: float,
    cancel_event: Optional[threading.Event] = None,
    progress: Optional[GateProgressCallback] = None,
) -> CommandResult:
    return _run_with_browser_artifact_publication_confirmation(
        command,
        lambda: gate_executor.run(
            command,
            lane=lane,
            timeout_seconds=timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=idle_timeout_seconds,
            cancel_event=cancel_event,
            progress=progress,
        ),
        progress=progress,
    )


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
                _run_executor_command(
                    gate_executor,
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
                    _run_executor_command
                    if gate_executor is not None
                    else _run_command
                ),
                *(
                    (gate_executor, command)
                    if gate_executor is not None
                    else (command,)
                ),
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
    if gate_executor is not None:
        return _run_overlapped_gate_plan(
            list(commands),
            parallel_groups,
            collect_all=collect_all,
            parallel_workers=parallel_workers,
            command_timeout_seconds=command_timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            command_idle_timeout_seconds=command_idle_timeout_seconds,
            progress=progress,
            gate_executor=gate_executor,
        )
    return _run_phased_gate_plan(
        commands,
        parallel_groups,
        cwd,
        collect_all=collect_all,
        parallel_workers=parallel_workers,
        command_timeout_seconds=command_timeout_seconds,
        adaptive_timeout_enabled=adaptive_timeout_enabled,
        command_idle_timeout_seconds=command_idle_timeout_seconds,
        progress=progress,
    )


@dataclass(frozen=True)
class _ScheduledGateCommand:
    command: str
    lane: str
    command_index: int
    group_index: int = -1
    slots: int = 1


def _executor_capacity(
    gate_executor: GateCommandExecutor,
    parallel_workers: int,
) -> int:
    capacity = getattr(gate_executor, "capacity", None)
    if callable(capacity):
        try:
            return max(1, int(capacity()))
        except (TypeError, ValueError):
            pass
    return max(1, int(parallel_workers))


def _executor_required_slots(
    gate_executor: GateCommandExecutor,
    command: str,
    capacity: int,
) -> int:
    required_slots = getattr(gate_executor, "required_slots", None)
    if callable(required_slots):
        try:
            return min(capacity, max(1, int(required_slots(command))))
        except (TypeError, ValueError):
            pass
    return 1


def _executor_estimated_duration(
    gate_executor: GateCommandExecutor,
    command: str,
) -> Optional[float]:
    estimated_duration = getattr(gate_executor, "estimated_duration", None)
    if not callable(estimated_duration):
        return None
    try:
        estimate = estimated_duration(command)
        return max(0.0, float(estimate)) if estimate is not None else None
    except (TypeError, ValueError):
        return None


def _ordered_parallel_commands(
    gate_executor: GateCommandExecutor,
    commands: Sequence[str],
) -> list[tuple[int, str]]:
    ordered = list(enumerate(commands))
    ordered.sort(
        key=lambda item: (
            gate_executor.priority(item[1]),
            item[0],
        )
    )
    return ordered


def _run_overlapped_gate_plan(
    commands: Sequence[str],
    parallel_groups: Sequence[GateParallelGroup],
    *,
    collect_all: bool,
    parallel_workers: int,
    command_timeout_seconds: float,
    adaptive_timeout_enabled: bool,
    command_idle_timeout_seconds: float,
    progress: Optional[GateProgressCallback],
    gate_executor: GateCommandExecutor,
) -> GateResult:
    slot_capacity = _executor_capacity(gate_executor, parallel_workers)
    job_capacity = max(1, min(int(parallel_workers), slot_capacity))
    pending_groups = [
        _ordered_parallel_commands(gate_executor, group.commands)
        for group in parallel_groups
    ]
    serial_results: dict[int, CommandResult] = {}
    parallel_results: dict[tuple[int, int], CommandResult] = {}
    running: dict[Future[CommandResult], _ScheduledGateCommand] = {}
    cancel_event = threading.Event()
    serial_next = 0
    group_index = 0
    stop_dispatch = False
    fatal_error: Optional[BaseException] = None
    scheduler_started = time.monotonic()
    last_accounted = scheduler_started
    occupied_slot_seconds = 0.0
    overlap_seconds = 0.0

    def account_running_time() -> None:
        nonlocal last_accounted, occupied_slot_seconds, overlap_seconds
        now = time.monotonic()
        elapsed = now - last_accounted
        occupied_slot_seconds += sum(item.slots for item in running.values()) * elapsed
        if (
            any(item.lane == "serial" for item in running.values())
            and any(item.lane != "serial" for item in running.values())
        ):
            overlap_seconds += elapsed
        last_accounted = now

    def group_is_running(index: int) -> bool:
        return any(item.group_index == index for item in running.values())

    def advance_completed_groups() -> None:
        nonlocal group_index
        while (
            group_index < len(pending_groups)
            and not pending_groups[group_index]
            and not group_is_running(group_index)
        ):
            group_index += 1

    def submit(
        pool: ThreadPoolExecutor,
        scheduled: _ScheduledGateCommand,
    ) -> None:
        account_running_time()
        estimate = _executor_estimated_duration(
            gate_executor, scheduled.command
        )
        if progress is not None:
            progress(
                (
                    "dispatch_serial"
                    if scheduled.lane == "serial"
                    else "dispatch_parallel"
                ),
                scheduled.command,
                estimate or 0.0,
            )
        future = pool.submit(
            _run_executor_command,
            gate_executor,
            scheduled.command,
            lane=scheduled.lane,
            timeout_seconds=command_timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=command_idle_timeout_seconds,
            cancel_event=cancel_event,
            progress=progress,
        )
        running[future] = scheduled

    if progress is not None:
        progress(
            "scheduler_start",
            (
                f"mode=overlap slots={slot_capacity} "
                f"jobs={job_capacity} serial={len(commands)} "
                f"groups={len(parallel_groups)}"
            ),
            0.0,
        )

    with ThreadPoolExecutor(max_workers=job_capacity) as pool:
        while True:
            advance_completed_groups()
            made_progress = False
            if not stop_dispatch:
                used_slots = sum(item.slots for item in running.values())
                serial_running = any(
                    item.lane == "serial" for item in running.values()
                )
                if (
                    serial_next < len(commands)
                    and not serial_running
                    and len(running) < job_capacity
                ):
                    command = commands[serial_next]
                    slots = _executor_required_slots(
                        gate_executor, command, slot_capacity
                    )
                    if used_slots + slots <= slot_capacity:
                        submit(
                            pool,
                            _ScheduledGateCommand(
                                command=command,
                                lane="serial",
                                command_index=serial_next,
                                slots=slots,
                            ),
                        )
                        serial_next += 1
                        used_slots += slots
                        made_progress = True

                if group_index < len(pending_groups):
                    pending = pending_groups[group_index]
                    while pending and len(running) < job_capacity:
                        selected_position = next(
                            (
                                position
                                for position, (_index, command) in enumerate(pending)
                                if used_slots
                                + _executor_required_slots(
                                    gate_executor, command, slot_capacity
                                )
                                <= slot_capacity
                            ),
                            None,
                        )
                        if selected_position is None:
                            # A wide command at the head group's frontier may
                            # not fit beside a command already running from
                            # that group. Backfill otherwise-idle slots from a
                            # later independent group, but preserve the normal
                            # group barrier when the head group has no pending
                            # work and is merely draining.
                            later_selection = next(
                                (
                                    (later_group, position)
                                    for later_group in range(
                                        group_index + 1,
                                        len(pending_groups),
                                    )
                                    for position, (_index, command) in enumerate(
                                        pending_groups[later_group]
                                    )
                                    if used_slots
                                    + _executor_required_slots(
                                        gate_executor,
                                        command,
                                        slot_capacity,
                                    )
                                    <= slot_capacity
                                ),
                                None,
                            )
                            if later_selection is not None:
                                later_group, position = later_selection
                                command_index, command = pending_groups[
                                    later_group
                                ].pop(position)
                                slots = _executor_required_slots(
                                    gate_executor, command, slot_capacity
                                )
                                submit(
                                    pool,
                                    _ScheduledGateCommand(
                                        command=command,
                                        lane="",
                                        command_index=command_index,
                                        group_index=later_group,
                                        slots=slots,
                                    ),
                                )
                                used_slots += slots
                                made_progress = True
                                continue
                            break
                        command_index, command = pending.pop(selected_position)
                        slots = _executor_required_slots(
                            gate_executor, command, slot_capacity
                        )
                        submit(
                            pool,
                            _ScheduledGateCommand(
                                command=command,
                                lane="",
                                command_index=command_index,
                                group_index=group_index,
                                slots=slots,
                            ),
                        )
                        used_slots += slots
                        made_progress = True

            advance_completed_groups()
            all_serial_done = serial_next >= len(commands)
            all_groups_done = group_index >= len(pending_groups)
            if not running and (
                stop_dispatch or (all_serial_done and all_groups_done)
            ):
                break
            if not running and not made_progress:
                raise RuntimeError(
                    "gate scheduler could not dispatch any remaining command"
                )
            if made_progress and len(running) < job_capacity:
                continue

            completed, _pending = wait(
                tuple(running),
                return_when=FIRST_COMPLETED,
            )
            account_running_time()
            for future in completed:
                scheduled = running.pop(future)
                try:
                    result = future.result()
                except BaseException as error:
                    fatal_error = error
                    stop_dispatch = True
                    cancel_event.set()
                    continue
                if scheduled.lane == "serial":
                    serial_results[scheduled.command_index] = result
                else:
                    parallel_results[
                        (scheduled.group_index, scheduled.command_index)
                    ] = result
                if not result.ok and (
                    not collect_all
                    or result.termination_reason
                    or result.infrastructure_error
                ):
                    stop_dispatch = True

    account_running_time()
    scheduler_elapsed = time.monotonic() - scheduler_started
    if progress is not None:
        utilization = (
            occupied_slot_seconds / (slot_capacity * scheduler_elapsed)
            if scheduler_elapsed > 0
            else 0.0
        )
        progress(
            "scheduler_finish",
            (
                f"mode=overlap slots={slot_capacity} "
                f"overlap_seconds={overlap_seconds:.3f} "
                f"slot_utilization={utilization:.3f}"
            ),
            scheduler_elapsed,
        )
    if fatal_error is not None:
        raise fatal_error

    results = [
        serial_results[index]
        for index in range(len(commands))
        if index in serial_results
    ]
    for current_group, group in enumerate(parallel_groups):
        results.extend(
            parallel_results[(current_group, command_index)]
            for command_index in range(len(group.commands))
            if (current_group, command_index) in parallel_results
        )
    failed = [result for result in results if not result.ok]
    reportable = [
        result for result in failed if result.termination_reason != "cancelled"
    ] or failed[:1]
    summaries = [_failure_summary(result) for result in reportable]
    ok = not failed and len(results) == (
        len(commands) + sum(len(group.commands) for group in parallel_groups)
    )
    return GateResult(
        ok=ok,
        commands=results,
        summary="all commands passed" if ok else "; ".join(summaries),
    )


def _run_phased_gate_plan(
    commands: Iterable[str],
    parallel_groups: Sequence[GateParallelGroup],
    cwd: Path,
    *,
    collect_all: bool,
    parallel_workers: int,
    command_timeout_seconds: float,
    adaptive_timeout_enabled: bool,
    command_idle_timeout_seconds: float,
    progress: Optional[GateProgressCallback],
) -> GateResult:
    results: List[CommandResult] = []
    summaries: List[str] = []
    ok = True

    for command in commands:
        result = _run_command(
            command,
            cwd,
            timeout_seconds=command_timeout_seconds,
            adaptive_timeout_enabled=adaptive_timeout_enabled,
            idle_timeout_seconds=command_idle_timeout_seconds,
            progress=progress,
        )
        results.append(result)
        if result.ok:
            continue
        ok = False
        summaries.append(_failure_summary(result))
        if result.termination_reason or result.infrastructure_error or not collect_all:
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
            gate_executor=None,
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
        if (
            any(
                result.termination_reason or result.infrastructure_error
                for result in failed
            )
            or not collect_all
        ):
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
        if cmd_result.infrastructure_error:
            failure_id = (
                cmd_result.infrastructure_failure_id
                or cmd_result.termination_reason
                or "unknown"
            )
            non_comparable.append(
                f"infra:{failure_id}:{cmd_result.command}"
            )
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
