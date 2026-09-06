from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import pytest

from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision, SelfRepairResult, _VerificationResult
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairExperiment, SelfRepairFinding, SelfRepairExperimentStore


def test_no_collection_or_import_error_cannot_prove_a_repair():
    for result in (
        _VerificationResult(False, "74 deselected", returncodes=(5,)),
        _VerificationResult(False, "ERROR collecting test_new.py", returncodes=(2,)),
        _VerificationResult(False, "ModuleNotFoundError", returncodes=(1,)),
        _VerificationResult(False, "process failed", returncodes=(1,)),
    ):
        assert not AutoAgentsSelfRepairRunner._is_behavioral_failure(result)
    assert AutoAgentsSelfRepairRunner._is_behavioral_failure(_VerificationResult(False, "FAILED test_old.py::test_resume - AssertionError", returncodes=(1,)))


def test_candidate_tests_are_applied_even_when_base_collects_nothing(tmp_path):
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(), target_project_root=tmp_path, error=RuntimeError(), decision=SelfRepairDecision(True), diagnosis=SimpleNamespace(final=SimpleNamespace(verification_commands=["python -m pytest -q tests/test_repair.py"])))
    runner.repo_root = tmp_path
    baseline = _VerificationResult(False, "no tests", returncodes=(5,))
    candidate = _VerificationResult(True, "1 passed", returncodes=(0,))
    with patch.object(runner, "_run_verification_at_ref", return_value=baseline), patch.object(runner, "_run_verification_commands", return_value=candidate), patch.object(runner, "_run_test_only_base_differential", return_value=baseline) as hybrid:
        result = runner._diagnosis_differential("base", tmp_path)
    hybrid.assert_called_once()
    assert not result.ok
    assert result.payload["outcome"] == "invalid"


def _experiment():
    return SelfRepairExperiment.create(run_id="run", root_fingerprint="root", category="recovery", base_commit="base", expected_postconditions=["actual recovery completes"])


def test_resolving_one_counterexample_does_not_close_shared_obligation():
    experiment = _experiment()
    root = next(key for key in experiment.obligations if key.startswith("root:"))
    first = SelfRepairCandidateRecord("c1", candidate_ref="c1", failed_obligations=[root])
    experiment.register_candidate(first, findings=[SelfRepairFinding(finding_id=name, status="confirmed", disposition="contract_violation", causal_obligation_id=root) for name in ("a", "b")])
    second = SelfRepairCandidateRecord("c2", parent_candidate_id="c1", candidate_ref="c2", failed_obligations=[root], resolved_finding_ids=["a"])
    experiment.register_candidate(second)
    assert root in second.failed_obligations
    assert root not in second.passed_obligations
    assert second.finding_states == {"a": "resolved", "b": "confirmed"}


def test_new_findings_do_not_make_old_unverified_parent_look_perfect():
    experiment = _experiment()
    root = next(key for key in experiment.obligations if key.startswith("root:"))
    c4 = SelfRepairCandidateRecord("c4", candidate_ref="c4", status="candidate_group_completed", finding_group_id="ownership", validation_rank=60)
    experiment.register_candidate(c4)
    c7 = SelfRepairCandidateRecord("c7", parent_candidate_id="c4", candidate_ref="c7", status="candidate_review_rejected", validation_rank=40)
    experiment.register_candidate(c7, findings=[SelfRepairFinding(finding_id="a", status="confirmed", disposition="contract_violation", causal_obligation_id=root)])
    c8 = SelfRepairCandidateRecord("c8", parent_candidate_id="c7", candidate_ref="c8", status="candidate_review_rejected", validation_rank=40, resolved_finding_ids=["a"])
    experiment.register_candidate(c8, findings=[SelfRepairFinding(finding_id="b", status="confirmed", disposition="contract_violation", causal_obligation_id=root)])
    assert experiment.best_search_candidate_id == "c8"
    assert c8.component_receipts == c4.component_receipts
    assert not any(item.startswith("root:") for item in c4.passed_obligations)


def test_group_completion_does_not_manufacture_root_proof(tmp_path):
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(), target_project_root=tmp_path, error=RuntimeError(), decision=SelfRepairDecision(True))
    runner._experiment = _experiment()
    runner._experiment_store = SelfRepairExperimentStore(tmp_path, "run", "root")
    root = next(key for key in runner._experiment.obligations if key.startswith("root:"))
    runner._candidate_group = {"group_id": "ownership", "contract_obligation_ids": [root]}
    result = SelfRepairResult(False, "candidate_group_completed", "component verified", candidate_id="c1", candidate_ref="c1", finding_group_id="ownership", validation_rank=60)
    with patch.object(runner, "_record_candidate_result"):
        runner._register_search_result(result)
    assert root not in runner._experiment.candidates["c1"].passed_obligations


def test_snapshot_is_frozen_despite_live_state_changes(tmp_path):
    from auto_agents.repair_snapshot import freeze_target
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    state = project / ".auto-agents/state/run_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"run_id":"run","status":"blocked"}')
    for args in (["config", "user.email", "test@example.com"], ["config", "user.name", "Test"], ["add", "-A"], ["commit", "-qm", "initial"]):
        subprocess.run(["git", *args], cwd=project, check=True)
    root, digest = freeze_target(project, tmp_path / "experiment", {"command": "collab"})
    state.write_text('{"run_id":"run","status":"pending"}')
    same_root, same_digest = freeze_target(project, tmp_path / "experiment", {"command": "collab"})
    assert (same_root, same_digest) == (root, digest)
    assert '"blocked"' in (root / ".auto-agents/state/run_state.json").read_text()


def test_interrupt_preserves_untracked_binary_candidate_before_worktree_cleanup(tmp_path):
    auto = tmp_path / "auto"
    target = tmp_path / "target"
    for root in (auto, target):
        root.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "test@example.com"], ["config", "user.name", "Test"], ["commit", "--allow-empty", "-qm", "initial"]):
            subprocess.run(["git", *args], cwd=root, check=True)
    original_worktree = []
    def interrupted(request):
        original_worktree.append(request.cwd)
        (request.cwd / "binary.dat").write_bytes(b"\x00\xffretained")
        raise KeyboardInterrupt()
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(config=SimpleNamespace(efforts={}), _call_with_failover=interrupted), target_project_root=target, error=RuntimeError(), decision=SelfRepairDecision(True))
    runner.repo_root = auto
    runner._experiment = _experiment()
    runner._experiment.base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=auto, text=True).strip()
    runner._experiment.best_search_ref = runner._experiment.base_commit
    runner._experiment_store = SelfRepairExperimentStore(target, "run", "root")
    with pytest.raises(KeyboardInterrupt):
        runner._run_candidate(experiment_id=runner._experiment.experiment_id, attempt=1, deadline=None, prior_failures=[], seen_fingerprints=set())
    assert not original_worktree[0].exists()
    saved = list(runner._experiment_store.root.glob("c*/partial-candidate.diff"))
    assert len(saved) == 1
    restored = tmp_path / "restored"
    subprocess.run(["git", "worktree", "add", "-q", str(restored), runner._experiment.base_commit], cwd=auto, check=True)
    assert runner._resume_interrupted_candidate(restored, base_head=runner._experiment.base_commit)
    assert (restored / "binary.dat").read_bytes() == b"\x00\xffretained"


def test_provider_continuation_is_bound_to_parent_design_and_component(tmp_path):
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(), target_project_root=tmp_path, error=RuntimeError(), decision=SelfRepairDecision(True))
    runner._experiment = _experiment()
    runner._candidate_group = {"group_id": "component"}
    parent = SelfRepairCandidateRecord("c1", candidate_ref="c1", provider_session_id="provider-session", provider_kind="codex", provider_prompt_hash="compatible", provider_context_fingerprint=runner._provider_continuation_context())
    runner._experiment.candidates["c1"] = parent
    runner._experiment.best_search_candidate_id = "c1"
    assert runner._provider_continuation() == {"resume_session_id": "provider-session", "resume_provider": "codex", "resume_prompt_hash": "compatible"}
    runner._candidate_group = {"group_id": "other-component"}
    assert runner._provider_continuation() == {}


def test_verification_failure_before_review_does_not_claim_review_passed():
    result = SelfRepairResult(False, "candidate_verification_failed", "focused check failed before review", validation_rank=50)
    passed, failed = AutoAgentsSelfRepairRunner._milestone_obligations(result)
    assert "validation:adversarial_review" not in passed
    assert "validation:focused" in failed
