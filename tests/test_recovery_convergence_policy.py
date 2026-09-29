"""Behavioral tests for evidence-based recovery using the real journal/outbox."""
from copy import deepcopy
from dataclasses import replace

import pytest

from auto_agents.recovery import Command, Contract, Event, Evidence, KernelError, KernelStore, Outcome, OutcomeKind
from auto_agents.recovery.convergence import decision, scope, failures
from auto_agents.recovery.model import digest
from auto_agents.recovery.observations import observation
from auto_agents.recovery.policy import enable, reserve
from test_recovery_kernel import finish, success, emit


@pytest.fixture
def scene(tmp_path):
    store = KernelStore(tmp_path/'control')
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path/'project')}))
    ref = store.put({'goal': 'repair the original behavior'})
    contract = Contract('goal', 'fix', 'fix', ref, ref, ('preserve tests',),
                        ('tests/test_value.py',), ('project',), ref, 'candidate_delivered',
                        ('implement', 'verify', 'review', 'deliver'))
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    enable(store, 'workflow')
    return store, contract


def operation(store, contract, phase, key, *, source='a'*64, model=False):
    child = replace(contract, task_id=key, parent_task=contract.task_id,
                    phases=(phase,), completion='phase_completed')
    emit(store, 'task_bound', {'contract': child.to_dict()})
    command = Command('cmd-' + key, 'workflow', child.task_id, phase, source, child.identity,
                      'b'*64, 'c'*64, key, model)
    reserve(store, 'workflow', command)
    return command


def write(store, contract, key, **kw):
    command = operation(store, contract, 'implement', key, model=True, **kw)
    finish(store, command, success(store, command))


def verify(store, contract, key, checks, *, ok=False, environment=None, source='a'*64, progress_checks=None, regressions=()):
    command = operation(store, contract, 'verify', key, source=source)
    result = {'ok': ok, 'reason': 'real verification', 'verification_manifest': 'fixed-obligations',
              'verification_checks': [{'id': k, 'status': v, 'command': 'python -m pytest tests/test_value.py'}
                                      for k, v in checks.items()]}
    if progress_checks is not None: result['progress_checks'] = progress_checks
    result['regression_ids'] = list(regressions)
    value = observation(command, result, verifier=command.runtime)
    if environment: value['environment'] = environment
    details = {'verification_observation': value, 'observation_ref': store.put(value),
               'native_result': store.put(result)}
    outcome = Outcome(OutcomeKind.SUCCESS if ok else OutcomeKind.CANDIDATE_REJECTED,
                      'Required checks', success(store, command).evidence if ok else (), details)
    return finish(store, command, outcome)


def diagnose(store, contract, key, hypothesis='Shared diagnostic construction omits the neighbor facts'):
    command = operation(store, contract, 'diagnose', key, model=True)
    item = scope(store.load('workflow'), contract.task_id)
    proposal = {'observation': item['latest'], 'hypothesis': hypothesis,
                'failure_ids': failures(item), 'paths': ['value.py'], 'expected_result': 'All observed checks pass'}
    result = success(store, command)
    finish(store, command, replace(result, details={'recovery_diagnosis': proposal}))


def test_partial_verified_progress_allows_more_than_four_candidates(scene):
    store, contract = scene
    checks = {str(i): 'failed' for i in range(7)}
    verify(store, contract, 'initial', checks)
    for i in range(7):
        write(store, contract, 'write-' + str(i))
        checks[str(i)] = 'passed'
        verify(store, contract, 'verify-' + str(i), checks, ok=i == 6)
    item = scope(store.replay('workflow'), 'fix')
    assert item['stalled'] == 0 and len(item['credited']) == 7
    assert store.load('workflow')['budget']['implementations'] == 7


def test_two_failed_candidates_need_diagnosis_and_exactly_one_correction(scene):
    store, contract = scene
    for i in range(2):
        write(store, contract, 'write-' + str(i))
        verify(store, contract, 'verify-' + str(i), {'a': 'failed', 'b': 'passed'})
    before = store.load('workflow')['budget'].copy()
    with pytest.raises(KernelError, match='new verified evidence'):
        write(store, contract, 'forbidden')
    assert store.load('workflow')['budget'] == before
    diagnose(store, contract, 'diagnosis')
    write(store, contract, 'correction')
    with pytest.raises(KernelError): write(store, contract, 'second-correction')
    assert store.replay('workflow')['budget']['model_calls'] == 4


def test_new_error_and_source_changes_do_not_reset_diagnostic_budget(scene):
    store, contract = scene
    for i in range(2):
        write(store, contract, 'write-' + str(i))
        verify(store, contract, 'verify-' + str(i), {'a': 'failed'})
    for i in range(2):
        diagnose(store, contract, 'diagnose-' + str(i), hypothesis='Hypothesis ' + str(i))
        write(store, contract, 'correction-' + str(i))
        verify(store, contract, 'again-' + str(i), {'a': 'failed', 'new-' + str(i): 'failed'})
    snapshot = store.replay('workflow')
    assert not decision(snapshot, 'fix', 'implement', 'd'*64)['allowed']
    assert not decision(snapshot, 'fix', 'diagnose', 'd'*64)['allowed']
    assert scope(snapshot, 'fix')['diagnoses'] == 2


def test_regressions_and_skips_do_not_manufacture_progress(scene):
    store, contract = scene
    verify(store, contract, 'initial', {'a': 'failed', 'b': 'passed'})
    write(store, contract, 'write')
    verify(store, contract, 'regressed', {'a': 'passed', 'b': 'failed'})
    assert scope(store.load('workflow'), 'fix')['credited'] == []
    verify(store, contract, 'skipped', {'a': 'passed', 'b': 'skipped'})
    assert scope(store.replay('workflow'), 'fix')['stalled'] == 1


def test_reclosing_same_failure_does_not_earn_progress_twice(scene):
    store, contract = scene
    verify(store, contract, 'initial', {'a': 'failed'})
    write(store, contract, 'first')
    verify(store, contract, 'closed', {'a': 'passed'})
    write(store, contract, 'second')
    verify(store, contract, 'regression', {'a': 'failed'})
    verify(store, contract, 'closed-again', {'a': 'passed'})
    assert scope(store.replay('workflow'), 'fix')['stalled'] == 1


def test_task_alias_and_restart_cannot_renew_search(scene):
    store, contract = scene
    for i in range(2): write(store, contract, 'write-' + str(i))
    alias = replace(contract, task_id='new-fix')
    emit(store, 'task_bound', {'contract': alias.to_dict()})
    reopened = KernelStore(store.root)
    enable(reopened, 'workflow')
    assert scope(reopened.replay('workflow'), alias.task_id)['attempts'] == 2
    with pytest.raises(KernelError): write(reopened, alias, 'alias-write')


def test_product_stall_does_not_prevent_independent_engine_work(scene):
    store, contract = scene
    for i in range(2): write(store, contract, 'write-' + str(i))
    engine = replace(contract, task_id='engine', kind='engine_repair')
    emit(store, 'task_bound', {'contract': engine.to_dict()})
    write(store, engine, 'engine-write')
    assert store.replay('workflow')['budget']['model_calls'] == 3
    assert scope(store.load('workflow'), 'fix')['stalled'] == 2


def test_permit_cannot_be_rebound_or_used_after_evidence_changes(scene):
    store, contract = scene
    child = replace(contract, task_id='pending', phases=('implement',), parent_task='fix')
    emit(store, 'task_bound', {'contract': child.to_dict()})
    command = Command('cmd', 'workflow', 'pending', 'implement', 'a'*64, child.identity,
                      'b'*64, 'c'*64, 'key', True)
    selected = decision(store.load('workflow'), 'pending', 'implement', command.source)
    emit(store, 'recovery_permit_issued', {'command': command.to_dict(), 'decision': selected})
    verify(store, contract, 'new-observation', {'a': 'failed'})
    before = store.replay('workflow')
    with pytest.raises(KernelError): emit(store, 'command_reserved', command.to_dict())
    assert store.replay('workflow') == before


def test_partial_progress_cannot_authorize_delivery_review(scene):
    store, contract = scene
    verify(store, contract, 'initial', {'a': 'failed', 'b': 'failed'})
    write(store, contract, 'write')
    verify(store, contract, 'partial', {'a': 'passed', 'b': 'failed'})
    assert not decision(store.load('workflow'), 'fix', 'review', 'a'*64)['allowed']
    verify(store, contract, 'full', {'a': 'passed', 'b': 'passed'}, ok=True)
    assert decision(store.replay('workflow'), 'fix', 'review', 'a'*64)['allowed']


def test_diagnosis_cannot_invent_failure_or_change_control_files(scene):
    from auto_agents.recovery.policy import parse_diagnosis
    import json
    store, contract = scene
    for i in range(2): write(store, contract, 'w-' + str(i))
    verify(store, contract, 'observed', {'a': 'failed'})
    command = operation(store, contract, 'diagnose', 'diagnose', model=True)
    item = scope(store.load('workflow'), 'fix')
    value = {'observation': item['latest'], 'hypothesis': 'repair', 'failure_ids': ['invented'],
             'paths': ['value.py'], 'expected_result': 'passes'}
    with pytest.raises(KernelError): parse_diagnosis(store.load('workflow'), command, json.dumps(value))
    value.update(failure_ids=['a'], paths=['.auto-agents/state/session.json'])
    with pytest.raises(KernelError): parse_diagnosis(store.load('workflow'), command, json.dumps(value))


def test_legacy_activation_preserves_exact_replay_and_usage(tmp_path):
    from test_recovery_kernel import reserve as old_reserve
    store = KernelStore(tmp_path/'old')
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': '/project'}))
    ref = store.put({})
    contract = Contract('goal', 'task', 'fix', ref, ref, (), ('test',), ('project',), ref,
                        'phase_completed', ('implement', 'verify'))
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    for i in range(2):
        cmd = old_reserve(store, contract, 'implement', 'write-' + str(i), True)
        finish(store, cmd, success(store, cmd))
        cmd = old_reserve(store, contract, 'verify', 'verify-' + str(i))
        finish(store, cmd, Outcome(OutcomeKind.CANDIDATE_REJECTED, 'old failure'))
    old = deepcopy(store.replay('workflow'))
    enable(store, 'workflow')
    new = store.replay('workflow')
    assert new['budget'] == old['budget'] == new['recovery']['legacy_budget']
    assert new['commands'] == old['commands']
    assert not decision(new, 'task', 'implement', 'a'*64)['allowed']


def test_engine_repairs_after_bounded_diagnosis_without_resetting_product_budget(scene, tmp_path):
    from test_recovery_engine import Effects, runner
    store, contract = scene
    repair, original = runner(scene, tmp_path, reject=2)
    execute = original.execute
    def effects(command):
        result = execute(command)
        if command.phase == 'verify':
            raw = {'ok': result.kind == OutcomeKind.SUCCESS, 'verification_manifest': 'engine-contract',
                   'verification_checks': [{'id': 'mandatory-test', 'status':
                       'passed' if result.kind == OutcomeKind.SUCCESS else 'failed'}]}
            observed = observation(command, raw, verifier=command.runtime)
            result = replace(result, details={'verification_observation': observed,
                'observation_ref': store.put(observed), 'result_ref': store.put(raw)})
        elif command.phase == 'diagnose':
            item = scope(store.load('workflow'), command.task_id)
            result = replace(result, details={'recovery_diagnosis': {'observation': item['latest'],
                'hypothesis': 'The candidate has not updated the required value',
                'failure_ids': failures(item), 'paths': ['candidate.py'], 'expected_result': 'mandatory-test passes'}})
        return result
    original.execute = effects
    assert repair.run()['status'] == 'completed'
    assert original.calls == ['plan', 'implement', 'verify', 'implement', 'verify', 'diagnose', 'implement', 'verify', 'review']
    state = store.replay('workflow')
    assert state['budget']['model_calls'] == 6 and state['budget']['implementations'] == 3
    assert state['budget']['stagnant'] == 0  # legacy counter never rewritten


def test_check_observation_cannot_be_attached_to_other_inputs(scene):
    store, contract = scene
    with pytest.raises(KernelError, match='different execution inputs'):
        verify(store, contract, 'wrong-environment', {'a': 'passed'}, environment='d'*64)


def test_correction_must_stay_within_declared_paths(scene):
    from auto_agents.recovery.policy import correction_paths
    store, contract = scene
    for i in range(2): write(store, contract, 'w-' + str(i))
    verify(store, contract, 'observation', {'a': 'failed'})
    diagnose(store, contract, 'diagnosis')
    assert correction_paths(store.load('workflow'), 'fix', {'value.py': 'old', 'foreign.py': 'old'},
                            {'value.py': 'new', 'foreign.py': 'new'}) == ['foreign.py']


def test_candidate_added_tests_cannot_renew_implementation_credit(scene):
    store, contract = scene
    verify(store, contract, 'initial', {'original': 'failed', 'added': 'failed'}, progress_checks=['original'])
    write(store, contract, 'write')
    verify(store, contract, 'added-passed', {'original': 'failed', 'added': 'passed'}, progress_checks=['original'])
    item = scope(store.replay('workflow'), 'fix')
    assert item['stalled'] == 1 and item['credited'] == []


def test_declared_call_limit_wins_over_progress_and_diagnosis(tmp_path):
    store = KernelStore(tmp_path/'limited')
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': '/project', 'model_call_limit': 1}))
    ref = store.put({})
    contract = Contract('goal', 'fix', 'fix', ref, ref, (), ('test',), ('project',), ref,
                        'candidate_delivered', ('implement', 'verify', 'review', 'deliver'))
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    enable(store, 'workflow')
    write(store, contract, 'write')
    verify(store, contract, 'verified', {'test': 'passed'}, ok=True)
    with pytest.raises(KernelError) as error: write(store, contract, 'extra')
    assert error.value.code == 'user_budget_exhausted'
    assert store.replay('workflow')['budget']['model_calls'] == 1


def test_later_broad_check_regression_prevents_false_partial_progress(scene):
    store, contract = scene
    verify(store, contract, 'targeted', {'a': 'failed'})
    write(store, contract, 'write')
    verify(store, contract, 'broad', {'a': 'passed', 'b': 'failed'}, regressions=['b'])
    item = scope(store.replay('workflow'), 'fix')
    assert item['credited'] == [] and item['stalled'] == 1
