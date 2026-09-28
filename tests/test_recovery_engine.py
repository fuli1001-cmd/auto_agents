"""Engine repair routing uses actual kernel transitions and deterministic effects."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_agents.recovery import Evidence, KernelError, Outcome, OutcomeKind
from auto_agents.recovery.engine import EngineRunner
from auto_agents.recovery.model import digest
from test_recovery_kernel import scene


def test_submit_loads_real_dependencies_before_checking_task_admission(tmp_path):
    """Exercise the lazy-import entrypoint, not only the injected effect runner."""
    from auto_agents.recovery.engine import submit

    with pytest.raises(KernelError) as failure:
        submit(None, tmp_path, SimpleNamespace(),
               {'invocation': {'command': 'collab', 'session_id': 'retained'}},
               SimpleNamespace(command='collab'), None)
    assert failure.value.code == 'kernel_binding'


@pytest.mark.parametrize('with_progress', [False, True])
def test_isolated_engine_initializes_environment_and_diagnostic_evidence(scene, tmp_path, monkeypatch, with_progress):
    from auto_agents.recovery.engine import IsolatedEngineEffects
    from auto_agents import config, repair_dependencies, repair_worker
    from auto_agents.repair_v2 import docker, providers, scope, workspace

    store, contract = scene
    root = tmp_path / 'repair'
    source = tmp_path / 'source'
    trusted = tmp_path / 'trusted'
    store.set_meta('trusted_verifier_runtime', {'path': str(trusted)})
    payload = {
        'project': str(tmp_path / 'project'), 'base': 'a' * 40, 'provider': 'test-provider',
        'error': 'Retained engine failure',
        'diagnosis': {'final': {'expected_postconditions': ['Resume the retained session']}},
        'invocation': {'command': 'collab', 'session_id': 'retained'},
    }
    frozen = root / 'target-evidence/.auto-agents/state/sessions/retained/session_state.json'
    frozen.parent.mkdir(parents=True)
    frozen.write_text(json.dumps({'session_id': 'retained', 'goal': 'Finish the original acceptance'}))
    provider = SimpleNamespace(kind='codex', binary='codex')
    monkeypatch.setattr(config, 'load_project_config', lambda project: SimpleNamespace(
        providers={'test-provider': provider}, efforts={}))
    environment = Mock(return_value=('/private/bin/python', 'environment-fingerprint'))
    dependencies = Mock()
    monkeypatch.setattr(repair_worker, 'engine_environment', environment)
    monkeypatch.setattr(repair_dependencies, 'prepare_verification_dependency', dependencies)
    verifier = Mock(runtime={'image': 'verified'}, image='verified')
    verifier_factory = Mock(return_value=verifier)
    monkeypatch.setattr(docker, 'DockerVerifier', verifier_factory)
    candidate = root / 'workspace/candidate'
    monkeypatch.setattr(workspace, 'Workspace', Mock(return_value=Mock(prepare=Mock(return_value=candidate))))
    sandbox_factory = Mock()
    driver_factory = Mock()
    monkeypatch.setattr(providers, 'AgentSandbox', sandbox_factory)
    monkeypatch.setattr(providers, 'NativeDriver', driver_factory)
    monkeypatch.setattr(scope, 'ScopeGuard', Mock())

    progress = Mock() if with_progress else None
    effects = IsolatedEngineEffects(store, 'workflow', contract, payload, root, source, progress)

    environment.assert_called_once()
    assert environment.call_args.args[1] == trusted
    dependencies.assert_called_once_with(environment.call_args.args[0], '/private/bin/python', 'vitest')
    verifier_factory.assert_called_once_with(store.root / 'kernel-verification',
                                            python='/private/bin/python', codex_binary='codex')
    verifier.prepare.assert_called_once_with()
    evidence = sandbox_factory.call_args.kwargs['evidence']
    assert json.loads((evidence / 'original/.auto-agents/state/sessions/retained/session_state.json').read_text())['goal'] == 'Finish the original acceptance'
    assert effects.evidence_context['original_goal'] == 'Finish the original acceptance'
    assert effects.candidate == candidate
    assert effects.driver is driver_factory.return_value
    if progress is not None:
        assert verifier.callback == progress.check


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
