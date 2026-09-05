from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_agents.models import CommandResult, GateResult
from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision
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
