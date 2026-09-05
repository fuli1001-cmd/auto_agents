from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_agents.models import CommandResult, GateResult
from auto_agents.self_repair import (
    AutoAgentsSelfRepairRunner, SelfRepairDecision, _FullSuiteShard, _VerificationResult,
)
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairExperiment


def _runner(root: Path) -> AutoAgentsSelfRepairRunner:
    runner = AutoAgentsSelfRepairRunner(
        SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace())),
        target_project_root=root,
        error=RuntimeError("engine failure"),
        decision=SelfRepairDecision(True),
    )
    runner.repo_root = root
    return runner


def test_candidate_context_includes_bounded_redacted_verification_failure():
    experiment = SelfRepairExperiment.create(
        run_id="run-1", root_fingerprint="root", category="engine", base_commit="base",
    )
    experiment.candidates["failed"] = SelfRepairCandidateRecord(
        candidate_id="failed", status="candidate_verification_failed",
        summary="Implemented the requested fix.",
        verification=(
            "debug output\n" * 1000
            + "FAILED tests/test_engine.py::test_resume - AssertionError\n"
            + "api_key=secret-value\n"
        ),
    )

    context = experiment.prompt_context()["recent_candidates"][0]

    evidence = context.get("verification_failure", "")
    assert "FAILED tests/test_engine.py::test_resume" in evidence
    assert "secret-value" not in evidence
    assert len(evidence) <= 2400


def test_focused_failure_preserves_stdout_tail_even_when_stderr_has_warnings(tmp_path):
    runner = _runner(tmp_path)
    command = "python -m pytest -q tests/test_engine.py"
    failure = CommandResult(
        command=command, ok=False, returncode=1,
        stdout="setup output\n" * 500 + "FAILED tests/test_engine.py::test_resume - AssertionError\n",
        stderr="warning: optional tracing is unavailable\n",
    )
    with patch("auto_agents.self_repair.run_commands", return_value=GateResult(
        ok=False, commands=[failure], summary="failed",
    )), patch.object(runner, "_verification_python", return_value="python"):
        result = runner._run_verification_commands([command], tmp_path)

    assert not result.ok
    assert "FAILED tests/test_engine.py::test_resume" in result.summary
    assert "warning: optional tracing is unavailable" in result.summary


def test_full_suite_dispatches_ready_work_past_resource_waiters(tmp_path):
    runner = _runner(tmp_path)
    independent_started = threading.Event()
    overlapped = []
    shards = [
        _FullSuiteShard("shared-a", "tests/a.py", ("tests/a.py",), True, ("service:shared",)),
        _FullSuiteShard("shared-b", "tests/b.py", ("tests/b.py",), True, ("service:shared",)),
        _FullSuiteShard("independent", "tests/c.py", ("tests/c.py",), True),
    ]

    def execute(_root, shard):
        if shard.shard_id == "shared-a":
            overlapped.append(independent_started.wait(1))
        elif shard.shard_id == "independent":
            independent_started.set()
        return _VerificationResult(True, "passed")

    with patch.object(runner, "_collect_full_suite_shards", return_value=shards), \
         patch.object(runner, "_full_suite_checkpoint_key", return_value="suite"), \
         patch.object(runner, "_full_suite_proof_cache_lookup", return_value=None), \
         patch.object(runner, "_full_suite_proof_cache_store"), \
         patch.object(runner, "_execute_full_suite_shard", side_effect=execute), \
         patch("auto_agents.self_repair.os.cpu_count", return_value=4):
        result = runner._run_full_suite_shards(tmp_path)

    assert result.ok
    assert overlapped == [True]


def test_full_suite_differential_overlaps_base_and_candidate(tmp_path):
    (tmp_path / "tests").mkdir()
    runner = _runner(tmp_path)
    candidate_started = threading.Event()
    overlapped = []

    def base(_ref):
        overlapped.append(candidate_started.wait(1))
        return _VerificationResult(True, "base passed")

    def candidate(_root):
        candidate_started.set()
        return _VerificationResult(True, "candidate passed")

    with patch.object(runner, "_run_full_suite_at_ref", side_effect=base), \
         patch.object(runner, "_run_full_suite_shards", side_effect=candidate):
        result = runner._full_suite_differential("base", tmp_path)

    assert result.ok
    assert overlapped == [True]


def test_overlapping_full_suites_share_exclusive_resources_and_capacity(tmp_path):
    runner = _runner(tmp_path)
    active = 0
    maximum = 0
    lock = threading.Lock()
    start = threading.Barrier(2)
    shard = _FullSuiteShard(
        "exclusive", "tests/test_shared.py", ("tests/test_shared.py",),
        False, ("global:exclusive",),
    )

    def collect(_root):
        start.wait(timeout=3)
        return [shard]

    def execute(_root, _shard):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _VerificationResult(True, "passed")

    with patch.object(runner, "_collect_full_suite_shards", side_effect=collect), \
         patch.object(runner, "_full_suite_checkpoint_key", return_value="suite"), \
         patch.object(runner, "_full_suite_proof_cache_lookup", return_value=None), \
         patch.object(runner, "_full_suite_proof_cache_store"), \
         patch.object(runner, "_execute_full_suite_shard", side_effect=execute), \
         patch("auto_agents.self_repair.os.cpu_count", return_value=4), \
         ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner._run_full_suite_shards, tmp_path)
        second = pool.submit(runner._run_full_suite_shards, tmp_path)
        assert first.result(timeout=5).ok
        assert second.result(timeout=5).ok

    assert maximum == 1
