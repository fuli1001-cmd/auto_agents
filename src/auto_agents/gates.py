from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re
import shlex
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .models import CommandResult, GateParallelGroup, GateResult, VerificationStep


_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_VITEST_FAILED = re.compile(
    r"^\s*FAIL\s+(\S+\.(?:test|spec)\.[jt]sx?(?:\s+>\s+.+)?)$",
    re.MULTILINE,
)
_UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR):\s+(.+)$", re.MULTILINE)


@dataclass
class FailureExtraction:
    failure_ids: List[str]
    comparable: bool
    non_comparable_ids: List[str]


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
    return [command_from_verification_step(step, project_root=project_root) for step in steps]


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


def _run_command(command: str, cwd: Path) -> CommandResult:
    import os
    env = dict(os.environ)
    env["PYTEST_CURRENT_TEST"] = "auto_agents_gate_run"
    env["AUTO_AGENTS_TEST"] = "True"
    env["TESTING"] = "True"
    process = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env=env,
    )
    return CommandResult(
        command=command,
        ok=process.returncode == 0,
        returncode=process.returncode,
        stdout=process.stdout.strip(),
        stderr=process.stderr.strip(),
    )


def _run_parallel_commands(commands: Sequence[str], cwd: Path) -> List[CommandResult]:
    if not commands:
        return []
    if len(commands) == 1:
        return [_run_command(commands[0], cwd)]

    results: List[CommandResult] = [None] * len(commands)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        future_to_index = {
            executor.submit(_run_command, command, cwd): index
            for index, command in enumerate(commands)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return results


def run_commands(commands: Iterable[str], cwd: Path) -> GateResult:
    results: List[CommandResult] = []
    ok = True
    summary = "all commands passed"
    for command in commands:
        result = _run_command(command, cwd)
        results.append(result)
        if not result.ok:
            ok = False
            summary = _failure_summary(result)
            break
    return GateResult(ok=ok, commands=results, summary=summary)


def run_commands_collect_all(commands: Iterable[str], cwd: Path) -> GateResult:
    """Run *all* commands, collecting results even after failures.

    Unlike ``run_commands`` this does **not** short-circuit on the first
    failure — it executes every command so that callers can compare the
    full set of failures against a known baseline.
    """
    results: List[CommandResult] = []
    ok = True
    summaries: List[str] = []
    for command in commands:
        result = _run_command(command, cwd)
        results.append(result)
        if not result.ok:
            ok = False
            summaries.append(_failure_summary(result))
    summary = "all commands passed" if ok else "; ".join(summaries)
    return GateResult(ok=ok, commands=results, summary=summary)


def run_gate_plan(
    commands: Iterable[str],
    parallel_groups: Sequence[GateParallelGroup],
    cwd: Path,
    *,
    collect_all: bool,
) -> GateResult:
    results: List[CommandResult] = []
    summaries: List[str] = []
    ok = True

    for command in commands:
        result = _run_command(command, cwd)
        results.append(result)
        if result.ok:
            continue
        ok = False
        summaries.append(_failure_summary(result))
        if not collect_all:
            return GateResult(ok=False, commands=results, summary=summaries[0])

    for group in parallel_groups:
        group_results = _run_parallel_commands(group.commands, cwd)
        results.extend(group_results)
        failed = [result for result in group_results if not result.ok]
        if not failed:
            continue
        ok = False
        summaries.extend(_failure_summary(result) for result in failed)
        if not collect_all:
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
