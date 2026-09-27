import pytest

from auto_agents.recovery import KernelStore, KernelError, Outcome, OutcomeKind
from auto_agents.recovery.executor import Executor, FunctionExecutor
from test_recovery_kernel import scene, emit, reserve, success


@pytest.mark.parametrize('boundary', ['reserved','dispatched','effect','receipt','settled'])
def test_crash_boundaries_never_duplicate_model_effect_or_budget(scene, monkeypatch, boundary):
    store, contract = scene
    command = reserve(store, contract, 'implement', 'writer', True)
    calls = []
    def effect(request):
        calls.append(request.command_id)
        return success(store, request)
    worker = Executor(store, {'implement': FunctionExecutor(effect)}, owner='first')
    if boundary == 'dispatched':
        emit(store, 'command_dispatched', {'command_id':command.command_id,'epoch':1,'owner':'first'})
    elif boundary == 'effect':
        emit(store, 'command_dispatched', {'command_id':command.command_id,'epoch':1,'owner':'first'})
        effect(command)
    elif boundary == 'receipt':
        original = worker._event
        def die(stream, kind, data, identity):
            if kind == 'command_finished': raise SystemExit('crash after durable receipt')
            return original(stream, kind, data, identity)
        monkeypatch.setattr(worker, '_event', die)
        with pytest.raises(SystemExit): worker.execute('workflow', command.command_id)
    elif boundary == 'settled': worker.execute('workflow', command.command_id)
    restarted = KernelStore(store.root)
    new = Executor(restarted, {'implement': FunctionExecutor(effect)}, owner='second')
    if boundary in {'dispatched','effect'}:
        with pytest.raises(KernelError, match='reconciled'): new.execute('workflow', command.command_id)
        assert new.reconcile('workflow', command.command_id).kind == OutcomeKind.OUTCOME_UNKNOWN
    elif boundary == 'receipt': assert new.reconcile('workflow', command.command_id).kind == OutcomeKind.SUCCESS
    else: assert new.execute('workflow', command.command_id).kind == OutcomeKind.SUCCESS
    assert len(calls) <= 1
    assert restarted.replay('workflow')['budget']['model_calls'] == 1


def test_expired_worker_cannot_poison_the_reconciled_receipt(scene):
    store, contract = scene
    command = reserve(store,contract,'implement','writer',True)
    emit(store,'command_dispatched',{'command_id':'writer','epoch':1,'owner':'old'})
    emit(store,'command_reconciled',{'command_id':'writer','epoch':2,'owner':'new',
                                   'receipt_ref':store.put({'external_job':'done'})})
    result = success(store,command)
    with pytest.raises(KernelError,match='Expired worker'):
        Executor(store,{},owner='old')._finish('workflow','writer',1,result)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM kernel_results').fetchone()[0] == 0
    Executor(store,{},owner='new')._finish('workflow','writer',2,result)
    assert store.replay('workflow')['commands']['writer']['status'] == 'finished'
