from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.models import CommandResult, GateParallelGroup, GateResult
from auto_agents.orchestrator import Orchestrator


class _PassingRetryExecutor:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []

    def run(self, command: str, *, lane: str = "", **_kwargs) -> CommandResult:
        self.commands.append((command, lane))
        return CommandResult(command=command, ok=True, returncode=0)

    def priority(self, _command: str) -> tuple[object, ...]:
        return ()

    def estimated_duration(self, _command: str) -> None:
        return None

    def required_slots(self, _command: str) -> int:
        return 1


def test_parallel_failure_that_passes_serially_is_quarantined(tmp_path: Path) -> None:
    project = tmp_path / "project"
    Orchestrator.init_project(project, "demo", "mock")
    orchestrator = Orchestrator(project)
    executor = _PassingRetryExecutor()
    command = "check-a"
    initial = GateResult(
        ok=False,
        commands=[
            CommandResult(
                command=command,
                ok=False,
                returncode=1,
                stderr="shared resource collision",
            )
        ],
        summary="failed",
    )

    recovered = orchestrator._serial_fallback_for_parallel_failures(
        initial,
        [],
        [GateParallelGroup(name="tests", commands=[command])],
        executor,
        context="test",
    )

    assert recovered.ok
    assert executor.commands == [(command, "")]
    assert command in orchestrator._parallel_gate_quarantine
    assert recovered.commands[0].process_snapshot["parallel_fallback"][
        "initial_returncode"
    ] == 1


def test_parallel_fallback_resumes_undispatched_tail_in_parallel(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    Orchestrator.init_project(project, "demo", "mock")
    orchestrator = Orchestrator(project)
    executor = _PassingRetryExecutor()
    failed = "check-failed"
    undispatched = "check-undispatched"
    initial = GateResult(
        ok=False,
        commands=[
            CommandResult(
                command=failed,
                ok=False,
                returncode=1,
                stderr="shared resource collision",
            )
        ],
        summary="failed",
    )

    recovered = orchestrator._serial_fallback_for_parallel_failures(
        initial,
        [],
        [
            GateParallelGroup(
                name="tests",
                commands=[failed, undispatched],
            )
        ],
        executor,
        context="test",
    )

    assert recovered.ok
    assert executor.commands == [
        (failed, ""),
        (undispatched, ""),
    ]
    assert failed in orchestrator._parallel_gate_quarantine

    reloaded = Orchestrator(project)
    assert failed in reloaded._parallel_gate_quarantine
