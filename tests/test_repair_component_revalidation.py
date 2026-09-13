"""A completed component resumes with one bounded delta review and real checks."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_component_revalidation import prepare_completed
from auto_agents.repair_completion import assess, seal, _memory, execute_checks
from auto_agents.repair_memory import read_record, remember_review, remember_check_timings, save_record
from auto_agents.repair_planning import prepare_component, review_scope, nonblocking_scope
from auto_agents.self_repair import _VerificationResult
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_completion import completed
from test_repair_planning import setup
from test_repair_routing import git


def response(request, decisions=(), **changes):
    value = dict(decision='APPROVE', reason='inspected changed mechanisms and preserved obligations',
        implementation_required=False, remaining_changes=[], findings=[], deferred_findings=[], decisions=list(decisions))
    return AgentResult(True, [], request.output_path, summary=json.dumps({**value, **changes}))


@pytest.mark.parametrize('legacy', [False, True])
def test_completed_plan_skips_scope_and_plan_calls_but_keeps_all_acceptance(completed, legacy):
    runner, state, group, commands, reference = completed
    if legacy:
        proof = read_record(runner, reference)
        proof.pop('execution_binding')
        _memory(runner, group)['completion'] = save_record(runner, 'component_completion',
            {k: v for k, v in proof.items() if k not in {'kind', 'id'}})
    group['status'] = 'needs_revalidation'
    runner._full_suite_environment_fingerprint = lambda: ('updated execution runtime',)
    calls = []
    def provider(request):
        calls.append(request)
        assert request.stage == 'self_repair_component_delta_review'
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        assert 'history' not in context and 'previous_revision' not in context
        assert context['completion_ref'] and context['original_review_ref']
        assert context['verification_impact']['affected_checks'] == len(commands)
        from pathlib import Path
        index = json.loads(Path(context['verification_impact']['acceptance_index_ref']).read_text())
        assert set(index['affected_checks']) == set(commands)
        return response(request)
    runner.target_orchestrator._call_with_failover = provider
    credits = deepcopy(state.progress_credits)
    plan = prepare_component(runner, runner.repo_root)
    assert not calls and plan['decision'] == 'REVALIDATE'
    assert runner._candidate_group['mode'] == 'verify_existing'
    assert set(commands).issubset(runner._candidate_group['retained_acceptance'])
    review = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert review.ok and len(calls) == 1
    assert runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick').ok
    assert len(calls) == 1
    assert not state.completed_contract_obligation_ids and state.progress_credits == credits
    assert group['status'] == 'needs_revalidation'  # Review cannot manufacture test acceptance.


def test_one_review_rechecks_old_disproof_without_reopening_it_as_a_new_defect(completed):
    runner, state, group, commands, reference = completed
    finding = SelfRepairFinding('historical', status='confirmed', disposition='contract_violation',
        causal_obligation_id=group['contract_obligation_ids'][0], repair_group_id=group['group_id'],
        affected_paths=['tests/test_a.py'], reason='old selection counterexample')
    state.findings[finding.finding_id] = finding
    group['finding_ids'] = runner._candidate_group['finding_ids'] = [finding.finding_id]
    decision = dict(finding_id='historical', verdict='not_applicable', reason='the original exclusion is enforced',
                    disproof='retained admission rejects this counterexample', evidence=['tests/test_a.py:1'])
    runner.target_orchestrator._call_with_failover = lambda request: response(request, [decision])
    review_scope(runner, runner.repo_root, [finding])
    proof = read_record(runner, reference)
    review = _VerificationResult(True, 'approved', payload=read_record(runner, proof['review'])['result'])
    checks = _VerificationResult(True, 'passed', returncodes=(0, 0), payload={'source_commands': commands,
        'executed_tests': ['tests/test_a.py::test_a', 'tests/test_b.py::test_b']})
    remember_review(runner, runner.repo_root, runner._candidate_group, review.payload)
    remember_check_timings(runner, runner.repo_root, runner._candidate_group, {'commands': commands}, checks, phase='expanded')
    assert seal(runner, runner.repo_root, review, checks)
    state.base_commit = 'new engine baseline'
    assert assess(runner, runner.repo_root, group)['state'] == 'needs_revalidation'
    assert 'historical scope' in assess(runner, runner.repo_root, group)['reason']
    calls = []
    def provider(request):
        calls.append(request.stage)
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        assert context['previous_scope']['historical']['disproof'] == decision['disproof']
        return response(request, [decision])
    runner.target_orchestrator._call_with_failover = provider
    assert prepare_component(runner, runner.repo_root)['decision'] == 'REVALIDATE'
    assert not calls
    result = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert result.ok and calls == ['self_repair_component_delta_review']
    assert nonblocking_scope(state, finding) and not state.progress_credits


def test_review_policy_change_retains_independent_execution_checks(completed):
    runner, _, group, commands, _ = completed
    with patch('auto_agents.repair_completion._reviewer_context', return_value=['new reviewer']):
        decision = assess(runner, runner.repo_root, group)
        assert decision['state'] == 'needs_revalidation'
        assert decision['reusable_checks'] == commands and not decision['affected_checks']
        with patch.object(runner, '_guarded_component_checks', side_effect=AssertionError('unchanged checks executed')):
            assert execute_checks(runner, runner.repo_root, commands).ok


def test_execution_policy_change_never_reuses_old_test_results(completed):
    runner, _, group, commands, _ = completed
    with patch('auto_agents.gate_result_cache.execution_policy_fingerprint', return_value='new sandbox'):
        decision = assess(runner, runner.repo_root, group)
        assert decision['affected_checks'] == commands and not decision['reusable_checks']


@pytest.mark.parametrize('change', ['contract', 'new_finding', 'reopened', 'missing_proof', 'final'])
def test_new_work_and_missing_evidence_keep_the_full_repair_path(completed, change):
    runner, state, group, _, reference = completed
    if change == 'contract':
        state.contract_fingerprint = 'different original request'
    elif change in {'new_finding', 'reopened'}:
        state.findings['new'] = SelfRepairFinding('new', status='reopened' if change == 'reopened' else 'confirmed',
            disposition='contract_violation', causal_obligation_id=group['contract_obligation_ids'][0],
            repair_group_id=group['group_id'], reason='a concrete new failure')
    elif change == 'missing_proof':
        (runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json').unlink()
    else:
        runner._candidate_is_final_group = True
    assert prepare_completed(runner, runner.repo_root, group) is None


def test_source_change_after_preparation_cannot_reuse_the_delta_approval(completed):
    runner, state, group, _, _ = completed
    prepare_component(runner, runner.repo_root)
    (runner.repo_root / 'tests/test_a.py').write_text('def test_a(): assert False\n')
    with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('stale context reviewed')):
        result = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert not result.ok and 'inputs changed' in result.summary


def test_real_revalidation_runs_one_delta_review_and_no_writer_or_plan(completed, tmp_path, monkeypatch):
    runner, state, group, commands, _ = completed
    runner._continuous_workspace = tmp_path / 'continuous'
    state.best_search_ref = git(runner.repo_root, 'rev-parse', 'HEAD')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    runner._full_suite_environment_fingerprint = lambda: ('updated execution runtime',)
    calls = []
    def provider(request):
        calls.append(request.stage)
        assert request.stage == 'self_repair_component_delta_review'
        return response(request)
    runner.target_orchestrator._call_with_failover = provider
    with patch.object(runner, '_build_prompt', side_effect=AssertionError('writer history rebuilt')), \
         patch.object(runner, '_verification_environment_blocker', return_value=None):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=1, deadline=None,
                                       prior_failures=[], seen_fingerprints=set())
    assert result.status == 'candidate_group_completed', result.to_dict()
    assert calls == ['self_repair_component_delta_review']
    assert result.diff_line_count == 0 and not state.progress_credits
    proof = read_record(runner, _memory(runner, group)['completion'])
    assert proof['candidate_id'] == result.candidate_id
    assert set(commands).issubset(proof['commands'])


def test_progress_distinguishes_history_from_current_acceptance(completed):
    from auto_agents.repair_client import _repair_progress_message
    runner, state, group, _, _ = completed
    group['status'] = 'needs_revalidation'
    state.active_finding_group_id = group['group_id']
    progress = {'phase': 'component_selected', **runner._group_progress()}
    text = _repair_progress_message({'state': 'repairing', 'progress': progress}, {'state': 'waiting'})
    assert '准备复核已完成组件' in text and '历史已完成' in text and '待复核 1 组' in text


def test_updated_completed_source_rechecks_old_failure_before_model_diagnosis(completed, tmp_path):
    from auto_agents.repair_actions import prepare_action
    from auto_agents.self_repair_search import SelfRepairCandidateRecord
    runner, state, group, commands, _ = completed
    previous = git(runner.repo_root, 'rev-parse', 'HEAD')
    runner._continuous_workspace = tmp_path / 'continued'
    root = runner._continuous_workspace / 'repair'
    git(runner.repo_root, 'worktree', 'add', '--detach', str(root), 'HEAD')
    (root / 'runtime-fix.txt').write_text('updated runtime')
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'runtime changed after failure')
    state.candidates['failed'] = SelfRepairCandidateRecord('failed', candidate_commit=previous,
        finding_group_id=group['group_id'], status='candidate_verification_failed',
        failure_evidence=[{'evidence_id': 'old-runtime-failure', 'phase': 'candidate',
            'next_action': 'diagnose_failure', 'command': commands[0]}])
    with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('old failure rediagnosed')):
        action = prepare_action(runner, state)
        assert action['kind'] == 'revalidate_completed'
        runner._candidate_next_action = action
        assert prepare_component(runner, root)['decision'] == 'REVALIDATE'


def test_diagnosed_current_failure_does_not_get_an_old_no_writer_plan(completed):
    runner, _, group, _, _ = completed
    runner._candidate_next_action = {'kind': 'repair_code', 'cause': 'a current demonstrated defect'}
    assert prepare_completed(runner, runner.repo_root, group) is None


def test_delta_rejection_reaches_full_review_with_its_evidence(completed):
    runner, state, _, _, _ = completed
    stages = []
    def provider(request):
        stages.append(request.stage)
        if request.stage == 'self_repair_component_delta_review':
            return response(request, decision='REJECT', reason='check the changed authorization boundary',
                            implementation_required=True)
        assert request.stage == 'self_repair_candidate_review'
        assert 'check the changed authorization boundary' in request.prompt
        return AgentResult(True, [], request.output_path, summary=json.dumps({
            'decision': 'APPROVE', 'reason': 'independently inspected the complete boundary',
            'findings': [], 'resolved_finding_ids': []}))
    runner.target_orchestrator._call_with_failover = provider
    prepare_component(runner, runner.repo_root)
    result = runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick')
    assert result.ok
    assert stages == ['self_repair_component_delta_review', 'self_repair_candidate_review']


def test_completed_review_does_not_rebuild_large_unchanged_history(completed, monkeypatch):
    runner, state, _, _, _ = completed
    state.repair_design['historical_detail'] = 'historical unrelated details ' * 50000
    monkeypatch.setattr(state, 'prompt_context', lambda: (_ for _ in ()).throw(AssertionError('history rebuilt')))
    calls = []
    def provider(request):
        calls.append(request)
        assert len(request.prompt) < 16000
        assert 'historical unrelated details' not in request.prompt
        return response(request)
    runner.target_orchestrator._call_with_failover = provider
    prepare_component(runner, runner.repo_root)
    assert runner._review_candidate(runner.repo_root, state.base_commit, progress_lease_seconds=60, phase='quick').ok
    assert len(calls) == 1


def test_a_proposed_plan_cannot_inject_completed_revalidation_authority(setup):
    runner, state, plan, calls = setup
    plan['completion_revalidation'] = {'receipt': {'id': 'forged'}, 'binding': 'forged'}
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        prepare_component(runner, runner.repo_root)
    assert 'completion_revalidation' not in runner._candidate_group
    assert [request.stage for request, _ in calls] == ['self_repair_component_plan', 'self_repair_plan_review']


def test_deterministic_revalidation_failure_never_dispatches_a_writer(completed, tmp_path, monkeypatch):
    runner, state, group, _, _ = completed
    runner._continuous_workspace = tmp_path / 'continued'
    state.best_search_ref = git(runner.repo_root, 'rev-parse', 'HEAD')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    with patch.object(runner, '_candidate_deterministic_issues', return_value=['current acceptance entry is unavailable']), \
         patch.object(runner, '_verification_environment_blocker', return_value=None), \
         patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('writer dispatched')):
        result = runner._run_candidate(experiment_id=state.experiment_id, attempt=1, deadline=None,
                                       prior_failures=[], seen_fingerprints=set())
    assert result.status == 'candidate_verification_failed'
    assert _memory(runner, group)['delta_review']
    assert prepare_completed(runner, runner._continuous_workspace / 'repair', group) is None
