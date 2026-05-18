from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import subprocess
from pathlib import Path
from typing import Iterable, List, Sequence

from .models import CommandResult, GateParallelGroup, GateResult


_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_VITEST_FAILED = re.compile(
    r"^\s*FAIL\s+(\S+\.(?:test|spec)\.[jt]sx?(?:\s+>\s+.+)?)$",
    re.MULTILINE,
)
_UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR):\s+(.+)$", re.MULTILINE)


def _failure_summary(result: CommandResult) -> str:
    details = result.stderr or result.stdout or f"exit code {result.returncode}"
    details = " ".join(details.split())
    return f"command failed: {result.command} ({details})"


def _run_command(command: str, cwd: Path) -> CommandResult:
    process = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        cwd=str(cwd),
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


def extract_failure_ids(gate_result: GateResult) -> List[str]:
    """Extract a list of unique failure identifiers from gate results.

    For pytest commands the function tries to pull individual ``FAILED``
    test node IDs from the output.  For other commands it falls back to
    the command string itself as the failure identifier.
    """
    failures: List[str] = []
    for cmd_result in gate_result.commands:
        if cmd_result.ok:
            continue
        combined = f"{cmd_result.stdout}\n{cmd_result.stderr}"
        pytest_ids = _PYTEST_FAILED.findall(combined)
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
        failures.append(f"cmd:{cmd_result.command}")
    return failures
