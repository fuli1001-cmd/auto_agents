from __future__ import annotations

from pathlib import Path

from auto_agents.gate_timing import GateTimingStore
from auto_agents.gates import GateCommandMetadata
from auto_agents.models import CommandResult


def _result(duration: float, *, ok: bool = True) -> CommandResult:
    return CommandResult(
        command="pytest tests/test_demo.py",
        ok=ok,
        returncode=0 if ok else 1,
        duration_seconds=duration,
    )


def test_gate_timing_uses_median_of_latest_seven_successes(
    tmp_path: Path,
) -> None:
    store = GateTimingStore(
        tmp_path,
        cache_path=tmp_path / "gate-cache.sqlite3",
        environment_fingerprint="distributed-v1",
    )
    metadata = GateCommandMetadata(resource_class="normal", cpu_slots=1)

    for duration in range(1, 9):
        store.record(
            "pytest tests/test_demo.py",
            _result(float(duration)),
            metadata,
        )

    assert store.estimate("pytest tests/test_demo.py", metadata) == 5.0


def test_gate_timing_excludes_failures_and_separates_resource_signatures(
    tmp_path: Path,
) -> None:
    store = GateTimingStore(
        tmp_path,
        cache_path=tmp_path / "gate-cache.sqlite3",
        environment_fingerprint="distributed-v1",
    )
    normal = GateCommandMetadata(resource_class="normal", cpu_slots=1)
    heavy = GateCommandMetadata(resource_class="heavy", cpu_slots=2)
    store.record("pytest tests/test_demo.py", _result(12.0), normal)
    store.record(
        "pytest tests/test_demo.py",
        _result(1.0, ok=False),
        normal,
    )

    assert store.estimate("pytest tests/test_demo.py", normal) == 12.0
    assert store.estimate("pytest tests/test_demo.py", heavy) is None

    other_environment = GateTimingStore(
        tmp_path,
        cache_path=tmp_path / "gate-cache.sqlite3",
        environment_fingerprint="local-v1",
    )
    assert other_environment.estimate(
        "pytest tests/test_demo.py", normal
    ) is None
