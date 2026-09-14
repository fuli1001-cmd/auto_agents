"""Stage-level regressions from the non-converging c93/c94 repair history."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.repair_memory import (
    component_key, latest_revision, read_record, remember_review, remember_revision, save_record,
)
from auto_agents.repair_planning import PlanningBlocked, prepare_component
from auto_agents.repair_schedule import verification_plan
from auto_agents.self_repair_search import SelfRepairFinding
from test_repair_planning import setup
from test_repair_routing import git


def add_finding(runner, plan, *, identity='candidate-regression', path='source.py'):
    state = runner._experiment
    group = state.finding_groups[0]
    item = SelfRepairFinding(identity, status='confirmed', disposition='candidate_regression',
        causal_obligation_id=group['contract_obligation_ids'][0], repair_group_id=group['group_id'],
        affected_paths=[path], required_test=plan['quick_checks'][0],
        reason='candidate violates retained recovery behavior',
        counterexample='owned context is lost on resume', evidence=[path + ':1'])
    state.findings[item.finding_id] = item
    group['finding_ids'] = [item.finding_id]
    return item


def review(runner, group, item, **changes):
    remember_review(runner, runner.repo_root, group, {'decision': 'REJECT', 'findings': [
        {**item.to_dict(), 'repair_kind': 'implementation', 'scenario_ids': ['recovery'], **changes}]})


@pytest.mark.parametrize('scope', ['expanded', 'narrowed'])
def test_actual_execution_scope_review_reuses_plan_after_restart(setup, scope):
    runner, state, plan, calls = setup
    plan['touched_paths'].append('source.py')
    if scope == 'narrowed':
        state.finding_groups[0]['touched_paths'] += ['source.py', 'unrelated.py']
        runner._candidate_group = deepcopy(state.finding_groups[0])
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}) as probe:
        first = prepare_component(runner, runner.repo_root)
        execution = deepcopy(runner._candidate_group)
        acceptance = verification_plan(state, execution)['commands']
        assert component_key(execution) != component_key(state.finding_groups[0])
        item = add_finding(runner, plan)
        (runner.repo_root / 'source.py').write_text('value = 2\n')
        git(runner.repo_root, 'add', '.')
        git(runner.repo_root, 'commit', '-qm', 'candidate with covered regression')
        review(runner, execution, item)
        runner._experiment = runner._experiment_store.load()
        runner._candidate_group = deepcopy(runner._experiment.finding_groups[0])
        with patch('auto_agents.repair_planning.review_scope'):
            second = prepare_component(runner, runner.repo_root)
    assert second['request_id'] == first['request_id']
    assert len(calls) == 2 and probe.call_count == 1
    assert runner._candidate_group['finding_scenario_bindings'] == {item.finding_id: ['recovery']}
    assert set(acceptance).issubset(verification_plan(runner._experiment, runner._candidate_group)['commands'])
    assert not runner._experiment.progress_credits


@pytest.mark.parametrize('change', ['new_path', 'same_id_new_evidence'])
@pytest.mark.parametrize('history', ['current', 'legacy'])
def test_uncovered_finding_goes_directly_to_amendment_and_independent_review(setup, change, history):
    runner, state, plan, calls = setup
    with (patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}),
          patch('auto_agents.repair_planning.review_scope')):
        if change == 'same_id_new_evidence':
            item = add_finding(runner, plan, path='tests/test_contract.py')
            plan['scenarios'][0]['finding_ids'] = [item.finding_id]
        first = prepare_component(runner, runner.repo_root)
        execution = deepcopy(runner._candidate_group)
        if history == 'legacy':
            previous = latest_revision(runner, state.finding_groups[0])
            previous.pop('finding_keys', None)
            remember_revision(runner, state.finding_groups[0], previous)
        original_scenarios = {row['scenario_id'] for row in first['plan']['scenarios']}
        if change == 'new_path':
            item = add_finding(runner, plan)
            plan['touched_paths'].append('source.py')
        else:
            item.counterexample = 'resume preserves context but binds a different owner'
        review(runner, execution, item, repair_kind='plan_gap' if change == 'same_id_new_evidence' else 'implementation')
        plan['scenarios'][0]['finding_ids'] = [item.finding_id]
        plan['implementation_steps'] = ['preserve the original owner while restoring context']
        runner._candidate_group = deepcopy(state.finding_groups[0])
        second = prepare_component(runner, runner.repo_root)
    assert [request.stage for request, _ in calls] == [
        'self_repair_component_plan', 'self_repair_plan_review',
        'self_repair_component_plan', 'self_repair_plan_review']
    assert calls[2][1]['planning_action'] == 'amend_plan'
    assert calls[2][1]['finding_delta']['added_or_changed'] == [item.finding_id]
    assert second['request_id'] != first['request_id']
    assert original_scenarios.issubset({row['scenario_id'] for row in second['plan']['scenarios']})
    assert not state.progress_credits and state.attempt_count == 0


def test_reviewed_implementation_regression_cannot_reenter_verify_existing(setup):
    runner, state, plan, calls = setup
    original = runner.target_orchestrator._call_with_failover
    def provider(request):
        result = original(request)
        if request.stage == 'self_repair_plan_review':
            payload = json.loads(result.summary)
            payload.update(implementation_required=False, remaining_changes=[])
            result.summary = json.dumps(payload)
        return result
    runner.target_orchestrator._call_with_failover = provider
    with (patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}),
          patch('auto_agents.repair_planning.review_scope')):
        first = prepare_component(runner, runner.repo_root)
        assert runner._candidate_group['mode'] == 'verify_existing'
        item = add_finding(runner, plan, path='tests/test_contract.py')
        review(runner, runner._candidate_group, item)
        runner._candidate_next_action = {'kind': 'implement', 'evidence_ids': []}
        runner._candidate_group = deepcopy(state.finding_groups[0])
        second = prepare_component(runner, runner.repo_root)
    assert second['request_id'] == first['request_id'] and len(calls) == 2
    assert runner._candidate_group['mode'] == 'implement'
    assert not state.progress_credits


def test_full_plan_reply_cannot_drop_retained_scenarios_during_amendment(setup):
    runner, state, plan, calls = setup
    with (patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}) as probe,
          patch('auto_agents.repair_planning.review_scope')):
        first = prepare_component(runner, runner.repo_root)
        item = add_finding(runner, plan)
        plan['touched_paths'].append('source.py')
        plan['scenarios'][0]['finding_ids'] = [item.finding_id]
        plan['implementation_steps'] = ['fix the active regression and retain recovery']
        runner._candidate_group = deepcopy(state.finding_groups[0])
        original = runner.target_orchestrator._call_with_failover
        def provider(request):
            result = original(request)
            if len(calls) == 3:
                payload = json.loads(result.summary)
                payload['scenarios'] = [s for s in payload['scenarios'] if s['kind'] != 'recovery']
                payload['not_applicable'] = {'recovery': 'claimed unrelated to this local correction'}
                result.summary = json.dumps(payload)
            return result
        runner.target_orchestrator._call_with_failover = provider
        second = prepare_component(runner, runner.repo_root)
    assert [request.stage for request, _ in calls[2:]] == [
        'self_repair_component_plan', 'self_repair_component_plan', 'self_repair_plan_review']
    assert calls[3][1]['feedback'][0]['code'] == 'acceptance_removed'
    assert probe.call_count == 2  # The weakened proposal never reaches probes/review.
    assert {s['scenario_id'] for s in first['plan']['scenarios']}.issubset(
        {s['scenario_id'] for s in second['plan']['scenarios']})
    assert not state.progress_credits


@pytest.mark.parametrize('invalid', [
    'other_group', 'other_plan', 'tampered', 'environment', 'plan_gap',
    'unknown_scenario', 'changed_counterexample', 'malformed_scenarios', 'malformed_scope', 'wrong_record_kind',
])
def test_invalid_review_cannot_authorize_reuse_or_borrow_a_legacy_review(setup, invalid):
    runner, state, plan, calls = setup
    plan['touched_paths'].append('source.py')
    with (patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}),
          patch('auto_agents.repair_planning.review_scope')):
        first = prepare_component(runner, runner.repo_root)
        execution = deepcopy(runner._candidate_group)
        item = add_finding(runner, plan)
        # A formerly usable canonical record must not hide an invalid current
        # execution-scope record or a later incompatible review conclusion.
        review(runner, state.finding_groups[0], item)
        changes = {}
        if invalid == 'other_group':
            execution['group_id'] = state.finding_groups[1]['group_id']
        elif invalid == 'other_plan':
            execution['planning_receipt'] = 'another-approval'
        elif invalid == 'plan_gap':
            changes['repair_kind'] = 'plan_gap'
        elif invalid == 'unknown_scenario':
            changes['scenario_ids'] = ['not-an-approved-scenario']
        elif invalid == 'malformed_scenarios':
            changes['scenario_ids'] = [{'scenario_id': 'recovery'}]
        elif invalid == 'changed_counterexample':
            changes['counterexample'] = 'a different failure from the active evidence'
        elif invalid == 'environment':
            runner._full_suite_environment_fingerprint = lambda: ('foreign-review-environment',)
        review(runner, execution, item, **changes)
        runner._full_suite_environment_fingerprint = lambda: ('provisioned',)
        if invalid in {'malformed_scope', 'wrong_record_kind'}:
            memory = state.component_memory[component_key(execution)]
            record = read_record(runner, memory['code_review'])
            if invalid == 'malformed_scope':
                record['component']['touched_paths'] = None
            else:
                record['kind'] = 'unreviewed_draft'
            memory['code_review'] = save_record(runner, record['kind'], record)
        if invalid == 'tampered':
            reference = state.component_memory[component_key(execution)]['code_review']
            artifact = runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json'
            artifact.write_text(artifact.read_text().replace('REJECT', 'APPROVE'))
            assert read_record(runner, reference) is None
        plan['scenarios'][0]['finding_ids'] = [item.finding_id]
        plan['implementation_steps'] = ['amend recovery after inspecting the current evidence']
        runner._candidate_group = deepcopy(state.finding_groups[0])
        second = prepare_component(runner, runner.repo_root)
    assert second['request_id'] != first['request_id']
    assert [request.stage for request, _ in calls[2:]] == [
        'self_repair_component_plan', 'self_repair_plan_review']
    assert not runner._candidate_group.get('finding_scenario_bindings')
    assert not state.progress_credits


@pytest.mark.parametrize('phase', ['queued', 'review', 'reviewing', 'exhausted'])
def test_legacy_pending_review_migrates_to_amendment_without_renewing_budgets(setup, phase):
    runner, state, plan, calls = setup
    with (patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}),
          patch('auto_agents.repair_planning.review_scope')):
        first = prepare_component(runner, runner.repo_root)
        item = add_finding(runner, plan)
        plan['touched_paths'].append('source.py')
        plan['scenarios'][0]['finding_ids'] = [item.finding_id]
        plan['implementation_steps'] = ['amend recovery for the newly observed failure']
        runner._candidate_group = deepcopy(state.finding_groups[0])
        with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                prepare_component(runner, runner.repo_root)
        key = next(key for key, row in state.repair_episodes.items() if row['status'] == 'active')
        episode = state.repair_episodes[key]
        episode.update(phase=phase, resume_phase='review', semantic_attempts=1,
                       pending_round=phase != 'queued', format_corrections=1, round_format_calls=1)
        if phase == 'exhausted':
            episode.update(status='blocked', semantic_attempts=3, pending_round=False)
        runner._experiment_store.save(state)
        runner._experiment = runner._experiment_store.load()
        runner._candidate_group = deepcopy(runner._experiment.finding_groups[0])
        if phase == 'exhausted':
            with pytest.raises(PlanningBlocked):
                prepare_component(runner, runner.repo_root)
            assert len(calls) == 2
        else:
            second = prepare_component(runner, runner.repo_root)
            assert second['request_id'] != first['request_id']
            assert [request.stage for request, _ in calls[2:]] == [
                'self_repair_component_plan', 'self_repair_plan_review']
            assert calls[2][1]['planning_action'] == 'amend_plan'
        restored = runner._experiment_store.load().repair_episodes[key]
        expected_attempts = 3 if phase == 'exhausted' else 1 if phase == 'review' else 2
        assert restored['semantic_attempts'] == expected_attempts
        assert restored['format_corrections'] == 1
        assert not runner._experiment.progress_credits
