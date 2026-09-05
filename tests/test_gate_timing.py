from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


def test_gate_timing_includes_finite_failures_and_separates_resource_signatures(
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

    assert store.estimate("pytest tests/test_demo.py", normal) == 6.5
    assert store.estimate("pytest tests/test_demo.py", heavy) is None

    other_environment = GateTimingStore(
        tmp_path,
        cache_path=tmp_path / "gate-cache.sqlite3",
        environment_fingerprint="local-v1",
    )
    assert other_environment.estimate(
        "pytest tests/test_demo.py", normal
    ) is None
    assert other_environment.estimate_any_environment(
        "pytest tests/test_demo.py", normal
    ) == 6.5


def test_parallel_quarantine_is_environment_scoped_and_persistent(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "gate-cache.sqlite3"
    first = GateTimingStore(
        tmp_path,
        cache_path=cache,
        environment_fingerprint="worker-a",
    )
    first.quarantine_parallel_command("pytest tests/test_shared.py")

    reloaded = GateTimingStore(
        tmp_path,
        cache_path=cache,
        environment_fingerprint="worker-a",
    )
    other = GateTimingStore(
        tmp_path,
        cache_path=cache,
        environment_fingerprint="worker-b",
    )

    assert reloaded.quarantined_commands() == {
        "pytest tests/test_shared.py"
    }
    assert other.quarantined_commands() == set()


def test_batch_estimates_match_individual_results_with_one_connection(tmp_path):
    store = GateTimingStore(tmp_path, cache_path=tmp_path / "cache.sqlite3", environment_fingerprint="env")
    normal = GateCommandMetadata(resource_class="normal", cpu_slots=1)
    heavy = GateCommandMetadata(resource_class="heavy", cpu_slots=2)
    commands = {f"check-{index}": normal for index in range(300)}
    for command in ("check-0", "check-299"):
        for duration in (1.0, 3.0, 8.0):
            store.record(command, _result(duration), normal)
    store.record("heavy", _result(12.0), heavy)
    commands["heavy"] = normal

    with patch.object(store, "_connect", wraps=store._connect) as connect:
        estimates = store.estimate_many(commands)

    assert connect.call_count == 1
    assert estimates["check-0"] == estimates["check-299"] == 3.0
    assert estimates["check-1"] is None
    assert estimates["heavy"] is None
    assert len(estimates) == len(commands)
    other = GateTimingStore(tmp_path, cache_path=store.cache_path, environment_fingerprint="other")
    assert other.estimate_many({"check-0": normal}) == {"check-0": None}
