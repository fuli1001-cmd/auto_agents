from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable, List

from .models import CommandResult, GateResult


_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_UNITTEST_FAILED = re.compile(r"^(?:FAIL|ERROR):\s+(.+)$", re.MULTILINE)


def _failure_summary(result: CommandResult) -> str:
    details = result.stderr or result.stdout or f"exit code {result.returncode}"
    details = " ".join(details.split())
    return f"command failed: {result.command} ({details})"


def run_commands(commands: Iterable[str], cwd: Path) -> GateResult:
    results: List[CommandResult] = []
    ok = True
    summary = "all commands passed"
    for command in commands:
        process = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            cwd=str(cwd),
        )
        result = CommandResult(
            command=command,
            ok=process.returncode == 0,
            returncode=process.returncode,
            stdout=process.stdout.strip(),
            stderr=process.stderr.strip(),
        )
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
        process = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            cwd=str(cwd),
        )
        result = CommandResult(
            command=command,
            ok=process.returncode == 0,
            returncode=process.returncode,
            stdout=process.stdout.strip(),
            stderr=process.stderr.strip(),
        )
        results.append(result)
        if not result.ok:
            ok = False
            summaries.append(_failure_summary(result))
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
        unittest_ids = [item.strip() for item in _UNITTEST_FAILED.findall(combined) if item.strip()]
        if unittest_ids:
            failures.extend(unittest_ids)
            continue
        failures.append(f"cmd:{cmd_result.command}")
    return failures
