"""External expectations for the unified authority, not self-generated replies."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from auto_agents.recovery import Command, Contract, Event, Evidence, KernelError, KernelStore, Outcome, OutcomeKind
from auto_agents.recovery.executor import Executor, FunctionExecutor
from auto_agents.recovery.model import digest
from auto_agents.recovery.protocol import ReviewManifest
from auto_agents.recovery.reducer import decide


@pytest.fixture
def scene(tmp_path):
    store = KernelStore(tmp_path / 'control')
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path / 'project'), 'model_call_limit': 4}))
    ref = store.put({'original': 'contract'})
    contract = Contract('goal', 'task', 'fix', ref, ref, ('Keep unrelated work',),
        ('tests/test_value.py::test_value',), ('value.py',), ref, 'candidate_delivered',
        ('implement', 'verify', 'review', 'deliver'))
    store.apply('workflow', 1, Event('bind', 'task_bound', {'contract': contract.to_dict()}))
    return store, contract


def emit(store, kind, data, identity=None):
    state = store.load('workflow')
    return store.apply('workflow', state['revision'], Event(identity or 'event:' + str(state['revision']), kind, data))


def reserve(store, contract, phase, identity, model=False):
    command = Command(identity, 'workflow', contract.task_id, phase, 'a'*64, contract.identity,
                      'b'*64, 'c'*64, identity, model)
    emit(store, 'command_reserved', command.to_dict())
    return command


def success(store, command, predicate='phase_completed'):
    proof = Evidence(store.put({'executed': command.command_id}), command.task_id, command.source,
        command.contract, command.environment, 'd'*64, command.phase, predicate)
    return Outcome(OutcomeKind.SUCCESS, 'Observed the declared result', (proof,))


def finish(store, command, outcome):
    emit(store, 'command_dispatched', {'command_id': command.command_id, 'epoch': 1, 'owner': 'worker'})
    return emit(store, 'command_finished', {'command_id': command.command_id, 'epoch': 1,
                                          'owner': 'worker', 'outcome': outcome.to_dict()})


def test_full_cycle_is_replayable_and_duplicate_receipts_do_not_spend(scene):
    store, contract = scene
    for phase in contract.phases:
        command = reserve(store, contract, phase, phase, model=phase in {'implement', 'review'})
        result = finish(store, command, success(store, command, 'candidate_delivered' if phase == 'deliver' else 'phase_completed'))
    assert result['tasks']['task']['status'] == 'completed'
    assert result['budget']['model_calls'] == 2
    assert result['budget']['implementations'] == 1
    assert store.replay('workflow') == result
    before = result['revision']
    duplicate = Event('receipt-repeat', 'command_finished', {'command_id': 'deliver', 'epoch': 1,
        'owner': 'worker', 'outcome': success(store, command, 'candidate_delivered').to_dict()})
    with pytest.raises(KernelError, match='settled'): store.apply('workflow', before, duplicate)
    assert store.load('workflow')['revision'] == before


def test_failed_candidate_keeps_identity_and_uses_another_bounded_attempt(scene):
    store, contract = scene
    emit(store, 'candidate_retained', {'task_id': 'task', 'candidate_id': 'candidate',
        'source': 'source-1', 'base': 'base', 'receipt': store.put({'candidate': 1})})
    command = reserve(store, contract, 'implement', 'first', True)
    finish(store, command, success(store, command))
    command = reserve(store, contract, 'verify', 'check')
    state = finish(store, command, Outcome(OutcomeKind.CANDIDATE_REJECTED, 'Required test is missing',
                                         details={'missing': ['tests/test_value.py::test_value']}))
    assert state['tasks']['task']['candidate']['candidate_id'] == 'candidate'
    assert state['tasks']['task']['phase'] == 'implement'
    command = reserve(store, contract, 'implement', 'second', True)
    assert store.load('workflow')['budget']['model_calls'] == 2
    assert store.load('workflow')['tasks']['task']['attempts'] == 2


def test_two_stalls_one_diagnosis_then_two_stalls_stop_across_restart(scene):
    store, contract = scene
    for index in range(2):
        implementation = reserve(store, contract, 'implement', 'implement:' + str(index))
        finish(store, implementation, success(store, implementation))
        verification = reserve(store, contract, 'verify', 'verify:' + str(index))
        finish(store, verification, Outcome(OutcomeKind.CANDIDATE_REJECTED, 'Same required check fails'))
    assert store.load('workflow')['budget']['diagnosis_due']
    diagnosis = replace(contract, task_id='diagnose', completion='phase_completed', phases=('diagnose',))
    emit(store, 'task_bound', {'contract':diagnosis.to_dict()})
    command = reserve(store, diagnosis, 'diagnose', 'bounded-diagnosis', True)
    finish(store, command, success(store, command))
    emit(store, 'task_resumed', {'task_id':'task','evidence_ref':store.put({'diagnosis':'bounded-diagnosis'})})
    store = KernelStore(store.root)
    for index in range(2, 4):
        implementation = reserve(store, contract, 'implement', 'implement:' + str(index))
        finish(store, implementation, success(store, implementation))
        verification = reserve(store, contract, 'verify', 'verify:' + str(index))
        finish(store, verification, Outcome(OutcomeKind.CANDIDATE_REJECTED, 'Same required check fails'))
    snapshot = store.replay('workflow')
    assert snapshot['budget']['stagnant'] == 2
    assert snapshot['budget']['rediagnoses'] == 1
    assert not snapshot['budget']['diagnosis_due']
    with pytest.raises(KernelError, match='cannot acquire'):
        emit(store, 'task_resumed', {'task_id':'task','evidence_ref':store.put({'restart':True})})
    # A new engine task cannot evade the same exhausted workflow budget, and
    # admission explains the retained failure without reserving another call.
    engine = replace(contract, task_id='engine', kind='engine_repair', phases=('plan',))
    emit(store, 'task_bound', {'contract': engine.to_dict()})
    before = store.replay('workflow')
    with pytest.raises(KernelError) as failure:
        reserve(store, engine, 'plan', 'engine-plan', True)
    assert failure.value.code == 'no_progress'
    assert failure.value.details['last_rejection']['command_id'] == 'verify:3'
    assert failure.value.details['last_rejection']['reason'] == 'Same required check fails'
    assert store.replay('workflow') == before


def test_protocol_two_fences_stale_runtime_before_idempotent_event_lookup(scene):
    from auto_agents.recovery.rpc import dispatch
    store, contract = scene
    store.set_meta('epoch', 2)
    event = Event('pause', 'workflow_stopped', {'status':'paused'})
    request = {'version':2,'op':'kernel-event','stream':'workflow','revision':2,'epoch':2,'event':event.to_dict()}
    assert dispatch(store, request)['state']['status'] == 'paused'
    store.set_meta('epoch', 3)
    with pytest.raises(KernelError, match='generation changed'): dispatch(store, request)


def test_cancel_retires_only_undispatched_outbox_and_keeps_usage(scene):
    store, contract = scene
    reserve(store, contract, 'implement', 'not-yet-sent', True)
    emit(store, 'workflow_stopped', {'status':'cancelled'})
    snapshot = store.replay('workflow')
    assert snapshot['commands']['not-yet-sent']['status'] == 'cancelled'
    assert snapshot['budget']['model_calls'] == 1
    assert store.status()['operations'] == []


def test_closed_incident_survives_version_adoption(scene):
    store, contract = scene
    emit(store, 'incident_opened', {'incident_id': 'preflight', 'task_id': 'task',
        'occurrence_ref': store.put({'failed': 'preflight'}),
        'failure': Outcome(OutcomeKind.ENGINE_DEFECT, 'Preflight defect', details={'counterexample': 'fixture'}).to_dict()})
    proof = Evidence(store.put({'recovered': True}), 'task', 'a'*64, contract.identity, 'b'*64, 'd'*64, 'verify', 'preflight_recovered')
    emit(store, 'incident_resolved', {'incident_id': 'preflight', 'proof': proof.to_dict(),
        'continuation_id': 'continue', 'next_task_id': 'task'})
    emit(store, 'adoption_requested', {'adoption_id': 'version-two', 'runtime': 'f'*64,
        'source': 'e'*64, 'environment': 'b'*64, 'contract': contract.identity})
    adoption = replace(proof, source='e'*64, phase='adopt', predicate='version_adopted')
    emit(store, 'adoption_verified', {'adoption_id': 'version-two', 'proofs': [adoption.to_dict()]})
    state = emit(store, 'adoption_activated', {'adoption_id': 'version-two'})
    assert state['incidents']['preflight']['status'] == 'resolved'
    assert state['continuations']['continue']['status'] == 'ready'
    assert state['budget']['model_calls'] == 0
    with pytest.raises(KernelError): emit(store, 'incident_opened', state['incidents']['preflight'])
    assert store.replay('workflow') == state


def test_unknown_model_outcome_is_reconciled_without_second_dispatch(scene):
    store, contract = scene; calls = []
    command = reserve(store, contract, 'implement', 'write', True)
    def crash(request):
        calls.append(request.command_id)
        raise KeyboardInterrupt()
    worker = Executor(store, {'implement': FunctionExecutor(crash)})
    with pytest.raises(KeyboardInterrupt): worker.execute('workflow', 'write')
    assert store.load('workflow')['commands']['write']['status'] == 'unknown'
    with pytest.raises(KernelError, match='reconciled'): worker.execute('workflow', 'write')
    recovered = Executor(store, {'implement': FunctionExecutor(crash, lambda request: success(store, request))})
    assert recovered.reconcile('workflow', 'write').kind == OutcomeKind.SUCCESS
    assert calls == ['write'] and store.load('workflow')['budget']['model_calls'] == 1
    assert store.replay('workflow')['tasks']['task']['phase'] == 'verify'


def test_stale_owner_and_mixed_snapshot_proof_cannot_commit(scene):
    store, contract = scene; command = reserve(store, contract, 'implement', 'write', True)
    emit(store, 'command_dispatched', {'command_id': 'write', 'epoch': 1, 'owner': 'actual'})
    outcome = success(store, command)
    with pytest.raises(KernelError, match='Expired'):
        emit(store, 'command_finished', {'command_id': 'write', 'epoch': 1, 'owner': 'other', 'outcome': outcome.to_dict()})
    mixed = replace(outcome, evidence=(replace(outcome.evidence[0], source='e'*64),))
    with pytest.raises(KernelError, match='different inputs'):
        emit(store, 'command_finished', {'command_id': 'write', 'epoch': 1, 'owner': 'actual', 'outcome': mixed.to_dict()})


def test_budget_event_and_outbox_are_one_transaction(scene):
    store, contract = scene; state = store.load('workflow')
    command = Command('one', 'workflow', 'task', 'implement', 'a'*64, contract.identity, 'b'*64, 'c'*64, 'same', True)
    event = Event('reserve-once', 'command_reserved', command.to_dict())
    def concurrent(_):
        return KernelStore(store.root).apply('workflow', state['revision'], event)
    with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(concurrent, range(4)))
    assert all(r['budget']['model_calls'] == 1 for r in results)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM kernel_outbox').fetchone()[0] == 1
    with pytest.raises(KernelError):
        store.apply('workflow', state['revision'], Event('other', 'command_reserved', replace(command, command_id='two').to_dict()))
    assert store.load('workflow')['budget']['model_calls'] == 1


def test_projection_cannot_reset_counter_or_reopen_completion(scene):
    store, _ = scene; first = store.put({'status': 'completed'})
    emit(store, 'projection_saved', {'name': 'session:child', 'blob': first, 'counters': {'current_attempt': 3}, 'terminal': True})
    second = store.put({'status': 'executing'})
    for counters, terminal in [({'current_attempt': 0}, True), ({'current_attempt': 3}, False)]:
        with pytest.raises(KernelError): emit(store, 'projection_saved', {'name': 'session:child', 'previous': first,
            'blob': second, 'counters': counters, 'terminal': terminal})


def test_journal_tamper_is_detected(scene):
    store, _ = scene
    with store.connect() as db: db.execute("UPDATE kernel_events SET checksum='bad' WHERE revision=1")
    with pytest.raises(KernelError, match='integrity'): store.replay('workflow')


def test_large_blob_is_streamed_and_content_verified(tmp_path, monkeypatch):
    store = KernelStore(tmp_path / 'control'); source = tmp_path / 'large'
    source.write_bytes(b'x' * (34 * 1024 * 1024))
    original = Path.read_bytes
    def guarded(path):
        assert path != source
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', guarded)
    reference = store.put_file(source)
    assert store.verify_blob(reference).stat().st_size == source.stat().st_size
    with store.blob_path(reference).open('r+b') as stream: stream.write(b'y')
    with pytest.raises(KernelError, match='digest'): store.verify_blob(reference)


def test_recorded_zero_thirteen_review_has_precise_diagnostics():
    manifest = ReviewManifest('source', 'base', 'contract', ('required',), {})
    # Deliberately external identifiers; never derive this reply from changes().
    reply = {'decision': 'APPROVE', 'findings': [], 'coverage': [],
             'change_coverage': [{'change': 'file.py:+' + str(i), 'requirement': 'required',
                                  'reason': 'Upstream', 'evidence': 'fixture'} for i in range(13)]}
    with pytest.raises(KernelError) as failure: manifest.validate(json.dumps(reply))
    assert failure.value.details['expected_count'] == 0
    assert failure.value.details['actual_count'] == 13
    schema = manifest.schema({'properties': {}, 'required': []})
    assert schema['properties']['change_coverage']['maxItems'] == 0
    assert 'allowed_change_ids' in manifest.correction('reply', json.dumps(reply), failure.value)
    assert manifest.validate(json.dumps({**reply, 'change_coverage': []}))['decision'] == 'APPROVE'


@pytest.mark.parametrize('kind', ['collab','fix','run','provider_resolve'])
def test_all_business_modes_share_contract_rules(scene, kind):
    store, contract = scene
    other = replace(contract, task_id=kind, kind=kind)
    state = emit(store, 'task_bound', {'contract': other.to_dict()})
    assert state['tasks'][kind]['contract_id'] == other.identity
    invalid = other.to_dict(); invalid.pop('authorization_ref')
    with pytest.raises(KernelError): Contract.read(invalid)
