from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_control import atomic_json
from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision, SelfRepairResult
from auto_agents.self_repair_search import (
    SelfRepairCandidateRecord, SelfRepairExperiment, SelfRepairExperimentStore, SelfRepairFinding,
)


def experiment():
    return SelfRepairExperiment.create(run_id="run", root_fingerprint="root", category="engine",
                                       base_commit="base", expected_postconditions=["preserve retained verification"])


def test_rebuilding_old_parent_does_not_repeatedly_credit_the_same_resolutions():
    state = experiment()
    obligation = next(key for key in state.contract_obligation_ids if key.startswith("root:"))
    parent = SelfRepairCandidateRecord("parent", candidate_ref="parent", validation_rank=75)
    state.register_candidate(parent, findings=[SelfRepairFinding(
        key, status="confirmed", disposition="contract_violation", causal_obligation_id=obligation,
    ) for key in ["a", "b"]])
    outcomes = []
    for number in range(4):
        candidate = SelfRepairCandidateRecord(
            f"candidate-{number}", parent_candidate_id="parent", candidate_ref=f"ref-{number}",
            validation_rank=75, status="candidate_review_rejected", resolved_finding_ids=["a", "b"],
        )
        outcomes.append(state.register_candidate(candidate, findings=[SelfRepairFinding(
            "guard", status="confirmed", disposition="contract_violation",
            causal_obligation_id="safety:tests_not_weakened",
        )]))
        assert state.best_search_candidate_id == "parent"
    assert outcomes[0] == "net_progress"
    assert outcomes[1:] == ["no_progress"] * 3
    assert state.patience_exhausted


def test_deep_design_retains_workspace_but_still_calls_the_planner(tmp_path):
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace()),
                                                       _call_with_failover=lambda request: (_ for _ in ()).throw(RuntimeError(request.stage))),
                                        target_project_root=tmp_path, error=RuntimeError(), decision=SelfRepairDecision(True),
                                        diagnosis=SimpleNamespace(to_dict=lambda: {}))
    runner.repo_root = tmp_path
    runner._continuous_workspace = tmp_path / "continuous"
    runner._experiment = experiment()
    runner._experiment_store = SelfRepairExperimentStore(tmp_path, "run", "root")
    with runner._candidate_workspace() as workspace:
        (workspace / "retained.py").write_text("newer work\n")
        atomic_json(workspace / "fallback.json", {"reason": "deepen diagnosis"})
    with runner._candidate_workspace() as same:
        assert same == workspace and (same / "retained.py").read_text() == "newer work\n"
    with patch.object(runner, "_compact_diagnosis_payload", return_value={}):
        with pytest.raises(RuntimeError, match="self_repair_design_review"):
            runner._ensure_approved_repair_design(runner._experiment)


@pytest.mark.parametrize("continuous", [False, True])
def test_rejected_components_stop_after_one_redesign_without_accepted_progress(tmp_path, continuous):
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace())),
                                        target_project_root=tmp_path, error=RuntimeError(), decision=SelfRepairDecision(True))
    runner.repo_root = tmp_path
    state = experiment()
    store = SelfRepairExperimentStore(tmp_path, "run", "root")
    runner._experiment, runner._experiment_store = state, store
    if continuous:
        runner._continuous_workspace = tmp_path / "continuous"
    calls = []
    revision = ["base"]
    def design(_state):
        _state.finding_groups = [{"group_id": "component", "status": "pending"}]
        _state.active_finding_group_id = "component"
        return True
    def candidate(**kwargs):
        calls.append(kwargs["attempt"])
        return SelfRepairResult(
            revision[0] != "base", "approved_candidate" if revision[0] != "base" else "candidate_review_rejected",
            "still rejected", candidate_id=f"c{kwargs['attempt']}", candidate_ref=f"ref{kwargs['attempt']}",
            candidate_commit=f"commit{kwargs['attempt']}", finding_group_id="component",
        )
    with (
        patch.object(runner, "_load_or_create_experiment", return_value=(store, state)),
        patch("auto_agents.self_repair.head_ref", side_effect=lambda _: revision[0]),
        patch.object(runner, "_ensure_approved_repair_design", side_effect=design),
        patch.object(runner, "_migrate_recoverable_candidate_to_pending"),
        patch.object(runner, "_latest_pending_validation_ref", return_value=""),
        patch.object(runner, "_resume_pending_validation_candidate", return_value=None),
        patch.object(runner, "_automatic_contract_reanalysis", return_value=False),
        patch.object(runner, "_run_candidate", side_effect=candidate),
    ):
        result = runner._run_search()
        assert not result.ok and result.status == "search_stalled"
        assert len(calls) == 6
        assert state.status == "stalled" and store.load().candidates
        before = len(calls)
        assert runner._run_search().status == "search_stalled"
        assert len(calls) == before  # Restarting unchanged search cannot grant another budget.
        revision[0] = "new-engine"
        assert runner._run_search().ok
        assert len(calls) == before + 1


def test_review_rejections_are_bounded_even_with_different_finding_ids_and_positive_scores():
    state = experiment()
    state.active_finding_group_id = "component"
    for n in range(3):
        state.candidates[str(n)] = SelfRepairCandidateRecord(
            str(n), status="candidate_review_rejected", finding_group_id="component", net_progress=n + 5,
            finding_ids=[f"new-name-{n}"],
        )
    assert state.review_patience_exhausted
    state.apply_automatic_correction(reason="split the component", progress_anchor=state.accepted_progress_anchor())
    state.active_finding_group_id = "component"
    assert not state.review_patience_exhausted


def test_accepted_component_allows_a_new_correction_window():
    state = experiment()
    before = state.accepted_progress_anchor()
    state.candidates["accepted"] = SelfRepairCandidateRecord(
        "accepted", status="candidate_group_completed", component_receipts={"first": "commit"},
    )
    assert state.accepted_progress_anchor() != before
