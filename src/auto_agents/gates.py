from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, List

from .models import CommandResult, GateResult


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
