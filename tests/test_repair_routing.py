"""Regression owners and unchanged component proofs survive serial handoffs."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision, _VerificationResult
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairExperiment, SelfRepairExperimentStore, SelfRepairFinding


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def make_runner(tmp_path):
    auto, target = tmp_path / 'auto', tmp_path / 'target'
    for root in (auto, target):
        root.mkdir()
        git(root, 'init', '-q')
        git(root, 'config', 'user.name', 'Test')
        git(root, 'config', 'user.email', 'test@example.invalid')
        (root / 'source.py').write_text('value = 1\n')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'initial')
    reply = {'decision': 'APPROVE', 'reason': 'reviewed', 'findings': [], 'resolved_finding_ids': []}
    orchestrator = SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace(), efforts={}),
        _call_with_failover=lambda request: AgentResult(True, [], request.output_path, summary=json.dumps(reply)))
    diagnosis = SimpleNamespace(to_dict=lambda: {}, final=SimpleNamespace(expected_postconditions=['retain custody']))
    runner = AutoAgentsSelfRepairRunner(orchestrator, target_project_root=target,
        error=RuntimeError(), decision=SelfRepairDecision(True), diagnosis=diagnosis)
    runner.repo_root = auto
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='repair',
        base_commit=git(auto, 'rev-parse', 'HEAD'), expected_postconditions=['retain custody'])
    obligation = next(key for key in state.contract_obligation_ids if key.startswith('root:'))
    state.finding_groups = [dict(group_id=name, status='pending', depends_on=[], finding_ids=[],
        touched_paths=['source.py'], contract_obligation_ids=[obligation], focused_tests=['test -f source.py'])
        for name in ('custody', 'inventory')]
    state.active_finding_group_id = 'custody'
    runner._candidate_group = state.finding_groups[0].copy()
    runner._experiment = state
    runner._experiment_store = SelfRepairExperimentStore(target, 'run', 'root')
    runner._experiment_store.save(state)
    return runner, state, reply


@pytest.mark.parametrize('unready', [False, True])
def test_review_keeps_regression_blocking_but_routes_ready_owner_across_restart(tmp_path, unready):
    runner, state, reply = make_runner(tmp_path)
    (runner.repo_root / 'source.py').write_text('value = 2\n')
    if unready:
        state.finding_groups[1]['depends_on'] = ['custody']
    finding = dict(finding_id='migration-breaks-custody', disposition='candidate_regression', severity='hard',
        causal_obligation_id=state.finding_groups[0]['contract_obligation_ids'][0],
        affected_paths=['source.py'], reason='inventory migration changes the retained identity',
        counterexample='existing candidate cannot resume', required_test='tests/test_contract.py::test_resume',
        evidence=['source.py:1'], defer_until='inventory')
    reply['findings'] = [finding]
    review = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60)
    assert not review.ok  # An APPROVE verdict with a deferred regression is still blocked.
    normalized = review.payload['findings'][0]
    assert normalized['defer_until'] == ''
    assert normalized['repair_group_id'] == ('custody' if unready else 'inventory')
    failed = SelfRepairCandidateRecord('failed', candidate_ref='failed', finding_group_id='custody',
        status='candidate_review_rejected', review_completed=True)
    state.register_candidate(failed, findings=[SelfRepairFinding.from_dict(normalized)])
    assert not failed.progress_keys
    assert 'candidate_regression:migration-breaks-custody' in failed.failed_obligations
    runner._experiment_store.save(state)
    restored = runner._experiment_store.load()
    assert restored.next_finding_group()['group_id'] == normalized['repair_group_id']
    assert restored.blocking_findings()[0].finding_id == finding['finding_id']
    assert restored.findings[finding['finding_id']].disposition == 'candidate_regression'
    assert restored.best_safe_candidate_id != 'failed'
    from auto_agents.repair_schedule import verification_plan
    assert 'python -m pytest -q tests/test_contract.py::test_resume' in verification_plan(
        restored, restored.next_finding_group())['commands']

    # A re-review must not relabel an existing regression as a contract achievement.
    runner._experiment = restored
    runner._candidate_group = restored.next_finding_group().copy()
    reply['findings'][0]['disposition'] = 'contract_violation'
    repeated = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60)
    assert not repeated.ok
    assert repeated.payload['findings'][0]['disposition'] == 'candidate_regression'

    unreviewed = SelfRepairCandidateRecord('unreviewed', parent_candidate_id='failed',
        candidate_ref='unreviewed', resolved_finding_ids=[finding['finding_id']])
    restored.register_candidate(unreviewed)
    assert restored.blocking_findings()
    assert restored.best_safe_candidate_id != 'unreviewed'
    repaired = SelfRepairCandidateRecord('repaired', parent_candidate_id='failed', candidate_ref='repaired',
        review_completed=True, resolved_finding_ids=[finding['finding_id']],
        verified_check_ids=['tests/test_contract.py::test_resume'])
    restored.register_candidate(repaired)
    assert not restored.blocking_findings()
    assert not repaired.progress_keys  # Fixing the introduced regression earns no new achievement.
    if not unready:
        restored.mark_finding_group_completed('inventory', candidate_id='repaired')
        assert restored.next_finding_group()['group_id'] == 'custody'


def test_known_owner_uses_one_strategy_adjustment_without_replanning_or_new_credit(tmp_path):
    runner, state, _ = make_runner(tmp_path)
    finding = SelfRepairFinding('migration', disposition='candidate_regression', repair_group_id='inventory',
        causal_obligation_id=state.finding_groups[0]['contract_obligation_ids'][0], status='confirmed')
    failed = SelfRepairCandidateRecord('failed', candidate_ref='failed', finding_group_id='custody')
    state.register_candidate(failed, findings=[finding])
    state.consecutive_non_improvements = 3
    design = state.finding_groups.copy()
    before = state.accepted_progress_anchor()
    assert runner._use_regression_owner_correction(state, failed)
    assert state.active_finding_group_id == 'inventory'
    assert state.finding_groups == design
    assert state.consecutive_non_improvements == 0
    assert state.accepted_progress_anchor() == before
    assert not state.progress_credits
    assert not runner._use_regression_owner_correction(state, failed)
    assert len(state.automatic_corrections) == 1
    restored = runner._experiment_store.load()
    assert restored.next_finding_group()['group_id'] == 'inventory'
    assert not runner._use_regression_owner_correction(restored, failed)


def test_regression_priority_follows_retained_finding_when_design_renames_owner(tmp_path):
    runner, state, _ = make_runner(tmp_path)
    state.findings['migration'] = SelfRepairFinding('migration', disposition='candidate_regression',
        repair_group_id='old-owner', status='confirmed',
        causal_obligation_id=state.finding_groups[0]['contract_obligation_ids'][0])
    state.finding_groups[1]['finding_ids'] = ['migration']
    assert state.next_finding_group()['group_id'] == 'inventory'


def test_unanchored_regression_cannot_redirect_or_expand_the_frozen_contract(tmp_path):
    runner, state, reply = make_runner(tmp_path)
    (runner.repo_root / 'source.py').write_text('value = 2\n')
    reply['findings'] = [dict(finding_id='local-regression', disposition='candidate_regression',
        affected_paths=['source.py'], defer_until='inventory', reason='local breakage')]
    review = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60)
    assert not review.ok
    finding = review.payload['findings'][0]
    assert finding['repair_group_id'] == 'custody'
    record = SelfRepairCandidateRecord('local', candidate_ref='local', finding_group_id='custody')
    state.register_candidate(record, findings=[SelfRepairFinding.from_dict(finding)])
    assert not state.findings
    assert state.next_finding_group()['group_id'] == 'custody'
    assert 'candidate_regression:local-regression' in record.failed_obligations


def test_existing_regression_owner_is_not_bounced_back_by_overlapping_paths(tmp_path):
    runner, state, _ = make_runner(tmp_path)
    state.finding_groups[0]['touched_paths'] = ['capture.py']
    state.finding_groups[1]['touched_paths'] = ['binding.py']
    obligation = state.finding_groups[1]['contract_obligation_ids'][0]
    state.findings['migration'] = SelfRepairFinding('migration', disposition='candidate_regression',
        status='confirmed', causal_obligation_id=obligation, repair_group_id='inventory')
    assert runner._regression_repair_group(dict(finding_id='migration', causal_obligation_id=obligation,
        affected_paths=['capture.py', 'binding.py']), state.finding_groups[1], state) == 'inventory'


def test_unchanged_source_can_verify_next_component_without_recrediting_same_scope(tmp_path):
    runner, state, reply = make_runner(tmp_path)
    runner._candidate_is_final_group = False
    runner._continuous_workspace = tmp_path / 'continuous'
    runner._continuous_workspace.mkdir()
    git(runner.repo_root, 'worktree', 'add', '--detach', str(runner._continuous_workspace / 'repair'), state.base_commit)
    ok = _VerificationResult(True, 'passed', returncodes=(0,))
    seen = set()
    with (patch.object(runner, '_build_prompt', return_value='report existing proof without editing'),
          patch.object(runner, '_run_active_group_verification', return_value=ok) as verify,
          patch.object(runner, '_candidate_boundary_checks', return_value=(ok, ok)),
          patch.object(runner, '_review_candidate', return_value=ok)):
        def attempt(number):
            return runner._run_candidate(experiment_id=state.experiment_id, attempt=number,
                deadline=None, prior_failures=[], seen_fingerprints=seen)
        first = attempt(1)
        assert first.status == 'candidate_group_completed'
        assert first.diff_line_count == 0
        runner._candidate_group = state.finding_groups[1].copy()
        second = attempt(2)
        assert second.status == 'candidate_group_completed'
        assert second.patch_fingerprint == first.patch_fingerprint
        assert second.candidate_commit == first.candidate_commit == state.base_commit
        third = attempt(3)
        assert third.status == 'candidate_duplicate'
        assert verify.call_count == 2
    # Regrouping identical obligations/checks cannot manufacture new progress.
    records = []
    for result in (first, second):
        record = SelfRepairCandidateRecord(result.candidate_id, candidate_ref=result.candidate_ref,
            candidate_commit=result.candidate_commit, finding_group_id=result.finding_group_id,
            status=result.status, review_completed=True)
        state.register_candidate(record)
        records.append(record)
    assert records[0].progress_keys
    assert not records[1].progress_keys
