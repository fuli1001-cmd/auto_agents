"""Regression coverage for the approved-plan failure observed in repair c73."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_feedback import sanitize_evidence
from auto_agents.repair_planning import PlanningBlocked, _retained_review, prepare_component
from auto_agents.repair_schedule import quick_verification_plan, verification_plan
from auto_agents.self_repair_search import SelfRepairCandidateRecord
from test_repair_planning import setup
from test_repair_routing import git


def redacted_probe(plan):
    plan['probes'] = [{'command': 'python -B -c \'from types import SimpleNamespace; '
                      'marker = SimpleNamespace(); marker.path, marker.token = "path", "owned"\'',
                      'expected': 'pass', 'purpose': 'execution marker protocol'}]


def probe(_runner, _root, spec):
    return {'specification': deepcopy(spec), 'matches': True, 'outcome': 'pass'}


def test_redacted_probe_approval_survives_last_round_code_change_and_reload(setup):
    runner, state, plan, calls = setup
    redacted_probe(plan)
    original = runner.target_orchestrator._call_with_failover
    rounds = []
    def provider(request):
        result = original(request)
        if request.stage == 'self_repair_plan_review':
            rounds.append(request)
            if len(rounds) < 3:
                result.summary = json.dumps({'decision': 'REVISE', 'issues': ['old issue'],
                                             'reason': 'needs correction', 'scenario_ids': []})
        return result
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', side_effect=probe):
        approved = prepare_component(runner, runner.repo_root)
        evidence = runner._experiment_store.root / 'planning' / approved['request_id']
        assert '<redacted>' in (evidence / 'input.json').read_text()
        assert '<redacted>' not in approved['plan']['probes'][0]['command']
        (runner.repo_root / 'tests/test_contract.py').write_text('def test_contract(): assert 1 == 1\n')
        git(runner.repo_root, 'add', '.')
        git(runner.repo_root, 'commit', '-qm', 'implement approved plan')
        runner._experiment = runner._experiment_store.load()
        runner._candidate_group = deepcopy(runner._experiment.finding_groups[0])
        assert prepare_component(runner, runner.repo_root)['request_id'] == approved['request_id']
    episode = next(iter(runner._experiment.repair_episodes.values()))
    assert len(calls) == 6 and episode['semantic_attempts'] == 3
    assert episode['feedback'] == [] and episode['status'] == 'approved'
    assert not state.progress_credits


def test_equal_redacted_projections_cannot_hide_changed_executable_evidence(setup):
    runner, state, plan, calls = setup
    redacted_probe(plan)
    with patch('auto_agents.repair_planning._probe', side_effect=probe):
        receipt = prepare_component(runner, runner.repo_root)
    changed = deepcopy(receipt)
    changed['plan']['probes'][0]['command'] = changed['plan']['probes'][0]['command'].replace('"path"', '"other"')
    assert sanitize_evidence(changed['plan']) == sanitize_evidence(receipt['plan'])
    assert _retained_review(runner, receipt, receipt['component'])
    assert not _retained_review(runner, changed, changed['component'])
    path = runner._experiment_store.root / 'planning' / receipt['request_id'] / 'input.json'
    incoming = json.loads(path.read_text())
    incoming['feedback'] = ['tampered evidence']
    path.write_text(json.dumps(incoming))
    assert not _retained_review(runner, receipt, receipt['component'])


@pytest.mark.parametrize('outcome', ['approve', 'reject', 'interrupt'])
def test_legacy_approval_at_exhaustion_gets_one_durable_review_recovery(setup, outcome):
    runner, state, plan, calls = setup
    redacted_probe(plan)
    with patch('auto_agents.repair_planning._probe', side_effect=probe):
        first = prepare_component(runner, runner.repo_root)
    request_path = runner._experiment_store.root / 'planning' / first['request_id'] / 'request.json'
    request = json.loads(request_path.read_text())
    request.pop('review_binding')
    request_path.write_text(json.dumps(request))
    episode = next(iter(state.repair_episodes.values()))
    episode.update(status='blocked', semantic_attempts=3, feedback=[{'reason': 'superseded rejection'}])
    runner._experiment_store.save(state)
    runner._experiment = runner._experiment_store.load()
    original = runner.target_orchestrator._call_with_failover
    recoveries = []
    def provider(request):
        assert request.stage == 'self_repair_plan_review'
        recoveries.append(request)
        if outcome == 'interrupt':
            raise KeyboardInterrupt
        if outcome == 'reject':
            return AgentResult(True, [], request.output_path, summary=json.dumps({
                'decision': 'REVISE', 'reason': 'new actionable conflict', 'issues': ['current conflict'], 'scenario_ids': []}))
        return original(request)
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', side_effect=probe):
        if outcome == 'approve':
            new = prepare_component(runner, runner.repo_root)
            assert new['request_id'] != first['request_id']
        else:
            with pytest.raises(KeyboardInterrupt if outcome == 'interrupt' else PlanningBlocked) as failure:
                prepare_component(runner, runner.repo_root)
            if outcome == 'reject':
                assert 'current conflict' in str(failure.value)
                assert 'superseded rejection' not in str(failure.value)
        runner._experiment = runner._experiment_store.load()
        with patch.object(runner.target_orchestrator, '_call_with_failover', side_effect=AssertionError('extra call')):
            if outcome == 'approve':
                assert prepare_component(runner, runner.repo_root)['request_id'] == new['request_id']
            else:
                with pytest.raises(PlanningBlocked):
                    prepare_component(runner, runner.repo_root)
    episode = next(iter(runner._experiment.repair_episodes.values()))
    assert len(recoveries) == 1 and episode['semantic_attempts'] == 3
    assert episode['approval_recovery_used'] and not runner._experiment.progress_credits


def test_new_engine_audits_latest_draft_without_importing_older_legacy_plan(setup):
    runner, state, plan, calls = setup
    with patch('auto_agents.repair_planning._probe', side_effect=probe):
        first = prepare_component(runner, runner.repo_root)
        (runner.repo_root / 'source.py').write_text('value = 2\n')
        git(runner.repo_root, 'add', '.')
        git(runner.repo_root, 'commit', '-qm', 'updated engine')
        state.base_commit = git(runner.repo_root, 'rev-parse', 'HEAD')
        with patch('auto_agents.repair_memory.import_legacy_draft', side_effect=AssertionError('stale migration')):
            second = prepare_component(runner, runner.repo_root)
    assert len(calls) == 3 and second['request_id'] != first['request_id']
    assert calls[-1][1]['planning_action'] == 'review_retained_plan'
    assert calls[-1][1]['source_delta']['changed_paths'] == ['source.py']


def test_latest_failed_atomic_acceptance_runs_before_quick_review(setup):
    runner, state, plan, calls = setup
    active = {**runner._candidate_group, **plan}
    cohort = 'python -m pytest -q tests/test_contract.py::test_first tests/test_contract.py::test_second'
    active['retained_acceptance'] = [cohort]
    ignored = 'python -m pytest -q tests/other.py::test_other'
    state.candidates['c1'] = SelfRepairCandidateRecord('c1', finding_group_id=active['group_id'],
        failure_evidence=[{'command': cohort}, {'command': ignored},
                          {'command': plan['quick_checks'][0], 'phase': 'baseline'}])
    quick = quick_verification_plan(state, active)
    assert quick['commands'][0] == cohort
    assert ignored not in quick['commands']
    assert plan['quick_checks'][0] in quick['commands']
    assert set(verification_plan(state, active)['commands']).issubset(quick['acceptance_inventory'])
    assert any('failed acceptance' in reason for reason in quick['budget_exceptions'])


def test_amendment_working_input_keeps_active_feedback_and_indexes_background(setup):
    runner, state, plan, calls = setup
    original = runner.target_orchestrator._call_with_failover
    draft_count = 0
    review_count = 0
    def provider(request):
        nonlocal draft_count, review_count
        incoming = json.loads((request.output_path.parent / 'input.json').read_text())
        working = json.loads((request.output_path.parent / 'working_input.json').read_text())
        if request.stage == 'self_repair_component_plan':
            draft_count += 1
            if draft_count == 2:
                assert working['feedback'] == incoming['feedback']
                assert 'draft' not in working['previous_revision']
                assert working['previous_revision']['draft_ref']['section'] == 'previous_revision.draft'
                assert working['previous_revision']['step_index'][0]['step_id'] == incoming['previous_revision']['step_ids'][0]
                assert working['history']['complete_input_ref'].endswith('input.json')
                return AgentResult(True, [], request.output_path, summary=json.dumps({'amendment': {
                    'parent_revision': incoming['previous_revision']['id'],
                    'replace_steps': {incoming['previous_revision']['step_ids'][0]: 'cover the missing interaction'}}}))
        if request.stage == 'self_repair_plan_review':
            review_count += 1
            if review_count == 1:
                return AgentResult(True, [], request.output_path, summary=json.dumps({
                    'decision': 'REVISE', 'reason': 'missing interaction', 'issues': ['current interaction'], 'scenario_ids': []}))
            assert working['plan_delta']['changed_fields'] == ['implementation_steps']
            assert working['plan_delta']['changed_scenario_ids'] == []
            assert set(working['plan_delta']['retained_scenario_ids']) == {'failure', 'compatibility', 'recovery', 'interaction'}
        return original(request)
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', side_effect=probe):
        prepare_component(runner, runner.repo_root)
    assert draft_count == review_count == 2


def test_incomplete_scope_revalidation_receives_previous_disproof_and_actual_delta(setup):
    from auto_agents.repair_planning import review_scope
    runner, state, plan, calls = setup
    finding = {'finding_id': 'dynamic', 'disposition': 'contract_violation',
               'causal_obligation_id': state.contract_obligation_ids[0],
               'affected_paths': ['source.py'], 'evidence': ['source.py:1']}
    (runner.repo_root / 'source.py').write_text('def read_value(path): return path.read_text()\n')
    git(runner.repo_root, 'add', '.')
    git(runner.repo_root, 'commit', '-qm', 'dynamic dependency')
    requests = []
    def provider(request):
        working = json.loads((request.output_path.parent / 'working_input.json').read_text())
        requests.append(working)
        return AgentResult(True, [], request.output_path, summary=json.dumps({'decisions': [{
            'finding_id': 'dynamic', 'verdict': 'not_applicable',
            'obligation_id': finding['causal_obligation_id'], 'reason': 'inspected current input',
            'disproof': 'current source handles the supported trigger', 'evidence': ['source.py:1']}]}))
    runner.target_orchestrator._call_with_failover = provider
    review_scope(runner, runner.repo_root, [finding])
    (runner.repo_root / 'source.py').write_text('def read_value(path): return path.read_text().strip()\n')
    review_scope(runner, runner.repo_root, [finding])
    row = requests[-1]['scope_revalidation']['dynamic']
    assert 'incomplete' in row['reason']
    assert row['previous']['disproof'] == 'current source handles the supported trigger'
    assert row['source_delta']['changed_paths'] == ['source.py']
    assert len(requests) == 2  # A delta summary is not proof that permits skipping review.
