"""A repaired old reproduction must not force an endless proposal rewrite."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.repair_planning import prepare_component, _retained_review
from auto_agents.repair_probe_recovery import probe_id
from auto_agents.repair_probe_review import prepare, admit
from test_repair_planning import setup


@pytest.mark.parametrize('kind', ['already_fixed', 'planned_fix'])
def test_changed_historical_probe_needs_explicit_review_and_retains_failing_acceptance(setup, kind):
    runner, state, plan, calls = setup
    spec = plan['probes'][0]
    spec['expected'] = 'behavior_failure' if kind == 'already_fixed' else 'pass'
    spec['command'] = spec['command'].replace('pytest -q', 'pytest -x -q')
    initial = {'matches': True, 'outcome': spec['expected'], 'specification': deepcopy(spec)}
    with patch('auto_agents.repair_planning._probe', return_value=initial):
        first = prepare_component(runner, runner.repo_root)
    # Force a retained-plan revalidation, keeping the same strategy slot. This
    # previously demanded the old bug recur before approving a fix to other code.
    (runner.repo_root / 'source.py').write_text('value = 2\n')
    runner._candidate_group = deepcopy(state.finding_groups[0])
    changed = {**initial, 'matches': False, 'outcome': 'pass' if kind == 'already_fixed' else 'behavior_failure'}
    original = runner.target_orchestrator._call_with_failover
    def provider(request):
        assert request.stage == 'self_repair_plan_review'
        context = json.loads((request.output_path.parent / 'input.json').read_text())
        assert context['probe_baseline']['request_id'] == first['request_id']
        assert context['probe_results'] == [changed]
        result = original(request)
        payload = json.loads(result.summary)
        payload['probe_assessments'] = [dict(probe_id=probe_id(spec), disposition=kind,
            scenario_ids=['failure'], reason='inspected current mechanism against the original reproduction',
            evidence=['source.py:1', 'tests/test_contract.py:1'])]
        result.summary = json.dumps(payload)
        return result
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value=changed):
        second = prepare_component(runner, runner.repo_root)
    assert len(calls) == 3 and second['decision'] == 'APPROVE'
    assert second['plan']['probes'] == first['plan']['probes']
    assert second['probe_results'][0]['matches'] is False  # Never rewrite history.
    assert second['probe_acceptance_commands'] == ([spec['command']] if kind == 'planned_fix' else [])
    if kind == 'planned_fix':
        assert spec['command'] in runner._candidate_group['retained_acceptance']
    assert _retained_review(runner, second, second['component'])  # Old strategy entry was overwritten.
    if kind == 'planned_fix':
        stripped = {**second, 'probe_acceptance_commands': []}
        assert not _retained_review(runner, stripped, stripped['component'])
    assert not state.progress_credits and state.attempt_count == 0


@pytest.mark.parametrize('invalid', ['missing', 'duplicate', 'unknown_scenario', 'uncovered_node',
                                    'inconclusive', 'wrong_disposition', 'tampered_baseline', 'no_baseline'])
def test_changed_probe_cannot_be_silently_waived(setup, invalid):
    runner, state, plan, calls = setup
    spec = plan['probes'][0]
    initial = {'matches': True, 'outcome': 'pass', 'specification': deepcopy(spec)}
    with patch('auto_agents.repair_planning._probe', return_value=initial):
        receipt = prepare_component(runner, runner.repo_root)
    context = {'proposed_plan': receipt['plan'], 'probe_results': [
        {**initial, 'matches': False, 'outcome': 'behavior_failure'}]}
    group = state.finding_groups[0]
    prepare(runner, group, context)
    row = dict(probe_id=probe_id(spec), disposition='planned_fix', scenario_ids=['failure'],
               reason='planned correction', evidence=['source.py:1'])
    reply = {'probe_assessments': [row]}
    if invalid == 'missing':
        reply = {}
    elif invalid == 'duplicate':
        reply['probe_assessments'].append(deepcopy(row))
    elif invalid == 'unknown_scenario':
        row['scenario_ids'] = ['unrelated']
    elif invalid == 'uncovered_node':
        context['proposed_plan'] = deepcopy(context['proposed_plan'])
        context['proposed_plan']['scenarios'][0]['check'] = 'python -m pytest -q tests/test_elsewhere.py::test_other'
    elif invalid == 'inconclusive':
        context['probe_results'][0]['outcome'] = 'inconclusive'
    elif invalid == 'wrong_disposition':
        row['disposition'] = 'already_fixed'
    elif invalid == 'tampered_baseline':
        reference = context['probe_baseline']['reference']
        path = runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json'
        path.write_text('{}')
    else:
        context.pop('probe_baseline')
    assert admit(runner, group, context, reply) is None
    assert not state.progress_credits
