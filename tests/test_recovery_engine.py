"""Engine repair routing uses actual kernel transitions and deterministic effects."""
from dataclasses import replace

import pytest

from auto_agents.recovery import Evidence, KernelError, Outcome, OutcomeKind
from auto_agents.recovery.engine import EngineRunner
from auto_agents.recovery.model import digest
from test_recovery_kernel import scene


class Effects:
    environment = 'e'*64

    def __init__(self, store, root, *, reject=0, protocol=False, crash=False):
        self.store, self.root, self.reject, self.protocol, self.crash = store, root, reject, protocol, crash
        self.calls = []
        self.version = 0

    def source(self): return digest(self.version)

    def execute(self, command):
        self.calls.append(command.phase)
        if self.crash: raise SystemExit('killed after dispatch')
        if command.phase == 'implement':
            self.version += 1
            (self.root/'candidate.py').write_text(f'VALUE = {self.version}\n')
        if command.phase == 'verify' and self.reject:
            self.reject -= 1
            return Outcome(OutcomeKind.CANDIDATE_REJECTED,'Required behavior still fails')
        if command.phase == 'review' and self.protocol:
            return Outcome(OutcomeKind.PROTOCOL_INVALID,'Unexpected change ID')
        proof = Evidence(self.store.put({'observed':self.version,'phase':command.phase}),command.task_id,
            command.source,command.contract,command.environment,'d'*64,command.phase,
            'preflight_recovered' if command.phase == 'review' else 'phase_completed')
        return Outcome(OutcomeKind.SUCCESS,'Verified phase',(proof,))


def runner(scene, tmp_path, **options):
    store, contract = scene
    store.set_meta('active_runtime',{'source':'f'*64})
    contract = replace(contract,task_id='engine',kind='engine_repair',completion='preflight_recovered',
                       phases=('plan','implement','verify','review'))
    effects = Effects(store,tmp_path,**options)
    return EngineRunner(store,'workflow',contract,effects), effects


def test_new_engine_incident_executes_stages_once_and_reuses_completion(scene,tmp_path):
    repair,effects = runner(scene,tmp_path)
    assert repair.run()['status'] == 'completed'
    assert effects.calls == ['plan','implement','verify','review']
    assert (tmp_path/'candidate.py').read_text() == 'VALUE = 1\n'
    before = repair.store.replay('workflow')
    assert repair.run()['status'] == 'completed'
    assert repair.store.replay('workflow') == before
    assert before['budget']['model_calls'] == 3


def test_engine_candidate_failure_returns_to_same_implementation_task(scene,tmp_path):
    repair,effects = runner(scene,tmp_path,reject=1)
    assert repair.run()['status'] == 'completed'
    assert effects.calls == ['plan','implement','verify','implement','verify','review']
    state = repair.store.replay('workflow')
    assert state['tasks']['engine']['attempts'] == 2
    assert state['budget']['model_calls'] == 4


def test_review_protocol_failure_gets_one_correction_and_never_reimplements(scene,tmp_path):
    repair,effects = runner(scene,tmp_path,protocol=True)
    state = repair.run()
    assert state['status'] == 'blocked'
    assert effects.calls == ['plan','implement','verify','review','review']
    assert state['failure']['kind'] == 'protocol_invalid'
    assert repair.store.replay('workflow')['budget']['model_calls'] == 4


def test_engine_unknown_effect_does_not_start_another_model_turn(scene,tmp_path):
    repair,effects = runner(scene,tmp_path,crash=True)
    with pytest.raises(SystemExit): repair.run()
    result = repair.run()
    assert result['status'] == 'blocked'
    assert result['failure']['kind'] == 'outcome_unknown'
    assert effects.calls == ['plan']
    assert repair.store.replay('workflow')['budget']['model_calls'] == 1


@pytest.mark.parametrize('code,domain',[('environment_blocked','environment'),('protocol_invalid','protocol'),
    ('outcome_unknown','reconciliation'),('candidate_rejected','product'),('no_progress','budget')])
def test_typed_failures_cannot_be_reinterpreted_as_engine_implementation(code,domain):
    from auto_agents.repair_v2.incidents import observation
    _, incident = observation({'session_id':'same-task','execution_log':[
        {'action':'execution_preflight_blocked','failure_kind':'kernel_' + code,'result':'Retained blocker'}]},
        {'project':'/project','session_id':'same-task'},'handoff')
    assert incident['domain'] == domain
