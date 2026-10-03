"""Power-loss recovery rechecks a retained engine candidate without rewriting it."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from auto_agents.recovery.engine import IsolatedEngineEffects
from auto_agents.recovery.model import Command, Outcome, OutcomeKind
from test_recovery_engine import runner
from test_recovery_kernel import scene


def test_explicit_resume_reconciles_dead_verifier_and_rechecks_without_reimplementation(scene, tmp_path, monkeypatch):
    repair, effects = runner(scene, tmp_path)
    execute = effects.execute
    interrupted = []
    def crash(command):
        if command.phase == 'verify' and not interrupted:
            interrupted.append(command.command_id)
            raise SystemExit('power loss during local verification')
        return execute(command)
    effects.execute = crash
    with pytest.raises(SystemExit): repair.run()
    before = repair.store.load('workflow')
    command = before['commands'][interrupted[0]]
    assert command.get('dispatch_identity')
    def reconcile(request):
        assert request.phase == 'verify' and not request.model_call
        return Outcome(OutcomeKind.ENVIRONMENT_BLOCKED, 'Interrupted without test result',
                       details={'verification_interrupted': True})
    effects.reconcile = reconcile
    assert repair.run(resume=True)['status'] == 'completed'
    after = repair.store.replay('workflow')
    assert effects.calls == ['plan', 'implement', 'verify', 'review']
    assert after['budget']['implementations'] == before['budget']['implementations'] == 1
    assert after['budget']['model_calls'] == before['budget']['model_calls'] + 1  # independent review only
    assert after['commands'][interrupted[0]]['outcome']['kind'] == 'environment_blocked'
    assert not after['commands'][interrupted[0]]['outcome']['evidence']
    checks = [c for c in after['commands'].values() if c['phase'] == 'verify']
    assert len(checks) == 2 and checks[0]['command_id'] != checks[1]['command_id']


@pytest.mark.parametrize('condition', ['valid', 'alive', 'missing_identity', 'changed_source', 'model_call'])
def test_isolated_reconciliation_requires_dead_owner_and_exact_retained_candidate(scene, tmp_path, monkeypatch, condition):
    store, original = scene
    contract = replace(original, kind='engine_repair')
    effects = object.__new__(IsolatedEngineEffects)
    effects.store, effects.stream, effects.contract = store, 'workflow', contract
    artifact = {'source': 'a'*64, 'path': str(tmp_path)}
    monkeypatch.setattr(effects, '_previous', lambda phase: {'artifact': artifact})
    monkeypatch.setattr(effects, 'source', lambda: 'b'*64 if condition == 'changed_source' else 'a'*64)
    monkeypatch.setattr('auto_agents.artifact_store.alive', lambda identity: condition == 'alive')
    monkeypatch.setattr('auto_agents.repair_v2.runtime_artifact.verify', lambda value: None)
    monkeypatch.setattr('auto_agents.repair_v2.workspace.source_identity', lambda path: 'a'*64)
    monkeypatch.setattr(effects, 'observed', lambda command, result, kind, reason:
        Outcome(kind, reason, details={'result_ref': store.put(result)}))
    command = Command('engine-check', 'workflow', contract.task_id, 'verify', 'a'*64,
                      contract.identity, 'b'*64, 'c'*64, 'check', condition == 'model_call')
    row = {**command.to_dict(), 'dispatch_identity': {'pid': 123, 'boot': 'old-boot', 'ticks': '12'}}
    if condition == 'missing_identity': row.pop('dispatch_identity')
    monkeypatch.setattr(store, 'load', lambda stream: {'commands': {command.command_id: row}})
    outcome = effects.reconcile(command)
    if condition != 'valid':
        assert outcome is None
    else:
        assert outcome.kind == OutcomeKind.ENVIRONMENT_BLOCKED and not outcome.evidence
        assert outcome.details['verification_interrupted'] is True
        result = store.read(outcome.details['result_ref'])
        assert result['candidate'] == artifact and result['progress_credit'] is False
