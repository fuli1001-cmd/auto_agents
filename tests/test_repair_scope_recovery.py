"""Scope recovery must survive process death without weakening external-input checks."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.models import AgentResult
from auto_agents.repair_planning import PlanningBlocked, review_scope
from test_repair_planning import setup


def scenario(runner, state, *, external=False):
    (runner.repo_root / 'source.py').write_text(
        'def read_value(path): return path.read_text()\n' if external else 'VALUE = 1\n')
    finding = {'finding_id': 'owned', 'disposition': 'contract_violation',
        'causal_obligation_id': state.contract_obligation_ids[0], 'affected_paths': ['source.py'],
        'evidence': ['source.py:1']}
    row = {'finding_id': 'owned', 'verdict': 'not_applicable', 'reason': 'independently checked',
        'evidence': ['source.py:1'], 'disproof': 'the current input preserves ownership'}
    return finding, row


def provider_for(runner, row, calls):
    def provider(request):
        calls.append(request)
        return AgentResult(True, [], request.output_path, summary=json.dumps({'decisions': [deepcopy(row)]}))
    runner.target_orchestrator._call_with_failover = provider


def reload(runner):
    runner._experiment = runner._experiment_store.load()
    return runner._experiment


def receipt(runner):
    return next((key, value) for key, value in runner._experiment.planning_receipts.items()
                if key.startswith('scope:'))


def test_successful_external_rechecks_do_not_exhaust_diagnosis_budget(setup):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state, external=True)
    provider_for(runner, row, calls)
    for _ in range(6):
        review_scope(runner, runner.repo_root, [finding])
        reload(runner)
    key, saved = receipt(runner)
    assert len(calls) == 6  # Each opaque external input is independently inspected.
    assert runner._experiment.planning_attempts[key] == 6
    assert saved['diagnosis_calls'] == 1
    assert saved['scope_call']['mode'] == 'revalidation'
    context = json.loads((calls[-1].output_path.parent / 'input.json').read_text())
    assert context['scope_unresolved_dependencies']['owned']
    assert not runner._experiment.progress_credits


@pytest.mark.parametrize('tamper', [False, True])
def test_legacy_three_attempts_require_authentic_completed_decisions(setup, tamper):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state, external=True)
    provider_for(runner, row, calls)
    review_scope(runner, runner.repo_root, [finding])
    key, saved = receipt(runner)
    old_request = state.scope_decisions['owned']['request_id']
    # Exact old incident shape: the exhausted invocation removed its draft but
    # retained the validated scope decisions and immutable review artifacts.
    state.planning_receipts[key] = {'prior_requests': [old_request]}
    state.planning_attempts[key] = 3
    if tamper:
        path = runner._experiment_store.root / 'planning' / old_request / 'result.json'
        path.write_text('{"decisions": []}')
    runner._experiment_store.save(state)
    reload(runner)
    if tamper:
        with pytest.raises(PlanningBlocked) as failure:
            review_scope(runner, runner.repo_root, [finding])
        assert failure.value.detail['code'] == 'scope_diagnosis_exhausted'
        assert failure.value.detail['actual']['unresolved_dependencies']['owned']
        assert len(calls) == 1
    else:
        review_scope(runner, runner.repo_root, [finding])
        _, saved = receipt(runner)
        assert saved['diagnosis_calls'] == 3
        assert runner._experiment.planning_attempts[key] == 4
        assert runner._experiment.scope_decisions['owned']['request_id'] != old_request
        assert len(calls) == 2 and not runner._experiment.progress_credits


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('boundary', ['reservation', 'dispatch'])
def test_repeated_process_death_has_separate_durable_bounded_recovery(setup, legacy, boundary):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state, external=True)
    if legacy:
        provider_for(runner, row, calls)
        review_scope(runner, runner.repo_root, [finding])
        key, saved = receipt(runner)
        state.planning_receipts[key] = {}
        state.planning_attempts[key] = 3
        runner._experiment_store.save(state)
    def interrupted(request):
        calls.append(request)
        raise KeyboardInterrupt
    runner.target_orchestrator._call_with_failover = interrupted
    from contextlib import nullcontext
    reserve_calls = []
    def reserved(*args):
        reserve_calls.append(args)
        raise KeyboardInterrupt
    with (patch('auto_agents.repair_planning._invoke', side_effect=reserved)
          if boundary == 'reservation' else nullcontext()):
        for _ in range(3):
            with pytest.raises(KeyboardInterrupt):
                review_scope(runner, runner.repo_root, [finding])
            reload(runner)
    for _ in range(2):
        with pytest.raises(PlanningBlocked) as failure:
            review_scope(runner, runner.repo_root, [finding])
        assert failure.value.detail['code'] == 'scope_recovery_exhausted'
        reload(runner)
    _, saved = receipt(runner)
    assert saved['scope_call']['recoveries'] == 2
    assert saved['diagnosis_calls'] == (3 if legacy else 1)
    assert len(calls) + len(reserve_calls) == (4 if legacy else 3)


@pytest.mark.parametrize('crash', ['result', 'draft', 'validated'])
def test_admitted_result_survives_crash_before_scope_decisions_are_saved(setup, crash):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state)
    provider_for(runner, row, calls)
    if crash == 'result':
        from auto_agents.repair_planning import _invoke
        def interrupted(*args):
            _invoke(*args)
            raise KeyboardInterrupt
        context = patch('auto_agents.repair_planning._invoke', side_effect=interrupted)
    else:
        save = runner._experiment_store.save
        def interrupted(value):
            save(value)
            if not any(key.startswith('scope:') for key in value.planning_receipts):
                return
            _, saved = receipt(runner)
            if ((crash == 'draft' and saved.get('draft'))
                    or (crash == 'validated' and saved.get('decision') == 'scope_validated')):
                raise KeyboardInterrupt
        context = patch.object(runner._experiment_store, 'save', side_effect=interrupted)
    with context, pytest.raises(KeyboardInterrupt):
        review_scope(runner, runner.repo_root, [finding])
    reload(runner)
    review_scope(runner, runner.repo_root, [finding])
    key, saved = receipt(runner)
    assert len(calls) == 1
    assert runner._experiment.planning_attempts[key] == 1
    assert saved['scope_call']['status'] == 'validated'


@pytest.mark.parametrize('artifact', ['raw', 'tampered', 'input', 'external', 'redacted'])
def test_recovery_rechecks_unadmitted_or_unproved_output(setup, artifact):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state, external=artifact == 'external')
    if artifact == 'redacted':
        row['secret'] = 'redaction must not become recovered executable evidence'
    provider_for(runner, row, calls)
    from auto_agents.repair_planning import _invoke
    def interrupted(*args):
        _invoke(*args)
        _, saved = receipt(runner)
        directory = runner._experiment_store.root / 'planning' / saved['scope_call']['request_id']
        if artifact == 'raw':
            (directory / 'result.json').rename(directory / 'output.json')
        elif artifact == 'tampered':
            (directory / 'result.json').write_text('{"decisions": []}')
        elif artifact == 'input':
            (directory / 'input.json').write_text('{}')
        raise KeyboardInterrupt
    with patch('auto_agents.repair_planning._invoke', side_effect=interrupted), pytest.raises(KeyboardInterrupt):
        review_scope(runner, runner.repo_root, [finding])
    reload(runner)
    row.update(verdict='required', obligation_id=finding['causal_obligation_id'],
        trigger='external input changed', consequence='foreign work lost', support_basis='original custody requirement')
    review_scope(runner, runner.repo_root, [finding])
    assert len(calls) == 2
    assert runner._experiment.scope_decisions['owned']['verdict'] == 'required'
    if artifact == 'external':
        context = json.loads((calls[-1].output_path.parent / 'input.json').read_text())
        assert context['recovered_scope']['result']['decisions'][0]['verdict'] == 'not_applicable'


@pytest.mark.parametrize('changed', ['source', 'environment'])
def test_completed_output_does_not_cross_changed_execution_inputs(setup, changed):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state)
    provider_for(runner, row, calls)
    from auto_agents.repair_planning import _invoke
    def interrupted(*args):
        _invoke(*args)
        raise KeyboardInterrupt
    with patch('auto_agents.repair_planning._invoke', side_effect=interrupted), pytest.raises(KeyboardInterrupt):
        review_scope(runner, runner.repo_root, [finding])
    reload(runner)
    if changed == 'source':
        (runner.repo_root / 'source.py').write_text('VALUE = 2\n')
    else:
        runner._full_suite_environment_fingerprint = lambda: ('changed',)
    review_scope(runner, runner.repo_root, [finding])
    assert len(calls) == 2


def test_unknown_revalidation_cannot_reopen_legacy_exhaustion(setup):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state, external=True)
    provider_for(runner, row, calls)
    review_scope(runner, runner.repo_root, [finding])
    key, saved = receipt(runner)
    state.planning_receipts[key] = {}
    state.planning_attempts[key] = 3
    row['verdict'] = 'unknown'
    with pytest.raises(PlanningBlocked, match='insufficient'):
        review_scope(runner, runner.repo_root, [finding])
    reload(runner)
    with pytest.raises(PlanningBlocked, match='diagnosis exhausted'):
        review_scope(runner, runner.repo_root, [finding])
    assert len(calls) == 2


def test_completed_format_output_is_recovered_without_buying_more_format_slots(setup):
    from auto_agents.repair_planning import _invoke
    from test_repair_incremental import scope_fixture
    runner, state, _, calls = setup
    finding, row = scope_fixture(state)
    def provider(request):
        calls.append(request)
        value = row if request.stage == 'self_repair_scope_review' else {
            **row, 'obligation_id': finding['causal_obligation_id']}
        return AgentResult(True, [], request.output_path, summary=json.dumps({'decisions': [value]}))
    runner.target_orchestrator._call_with_failover = provider
    def interrupted(*args):
        result = _invoke(*args)
        if args[2] == 'self_repair_scope_format':
            raise KeyboardInterrupt
        return result
    with patch('auto_agents.repair_planning._invoke', side_effect=interrupted), pytest.raises(KeyboardInterrupt):
        review_scope(runner, runner.repo_root, [finding])
    reload(runner)
    review_scope(runner, runner.repo_root, [finding])
    _, saved = receipt(runner)
    assert len(calls) == 2 and saved['format_calls'] == 1
    assert runner._experiment.scope_decisions[finding['finding_id']]['obligation_id'] == finding['causal_obligation_id']


def test_interrupted_format_does_not_freeze_an_external_decision(setup):
    runner, state, _, calls = setup
    finding, row = scenario(runner, state, external=True)
    row.update(verdict='required', obligation_id='wrong', trigger='external input',
        consequence='foreign write', support_basis='original requirement')
    def provider(request):
        calls.append(request)
        if request.stage == 'self_repair_scope_format':
            raise KeyboardInterrupt
        if len(calls) > 1:
            context = json.loads((request.output_path.parent / 'input.json').read_text())
            assert context['scope_unresolved_dependencies']['owned']
            row['obligation_id'] = finding['causal_obligation_id']
        return AgentResult(True, [], request.output_path, summary=json.dumps({'decisions': [row]}))
    runner.target_orchestrator._call_with_failover = provider
    with pytest.raises(KeyboardInterrupt):
        review_scope(runner, runner.repo_root, [finding])
    reload(runner)
    review_scope(runner, runner.repo_root, [finding])
    assert [call.stage for call in calls] == ['self_repair_scope_review', 'self_repair_scope_format',
                                            'self_repair_scope_review']
    _, saved = receipt(runner)
    assert saved['scope_call']['recoveries'] == 1
    assert saved['format_calls'] == 1
