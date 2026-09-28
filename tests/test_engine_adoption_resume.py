"""Cold routes finish accepted engine delivery without another model request."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_agents import repair_client
from auto_agents.orchestrator import Orchestrator
from auto_agents.recovery import engine, native, runtime_manager as manager
from auto_agents.recovery.model import Contract, Event, Evidence, KernelError, Outcome, OutcomeKind, digest
from auto_agents.recovery.runtime_source import capture
from auto_agents.recovery.upgrade import MANDATORY_CHECKS, activate as activate_runtime, independent_verify
from test_recovery_native import activate
from test_runtime_adoption import installation
from test_session_verification_ownership import project


@pytest.fixture
def pending(installation, tmp_path, monkeypatch):
    store, source = installation
    # This fixture tests adoption, not the host's WSL disk observation bridge.
    monkeypatch.setattr('auto_agents.storage_admission.windows_backing_roots', lambda: ())
    package = source / 'src/auto_agents'; package.mkdir(parents=True)
    for name in ('workflow_chain.py', 'workflow_runtime.py', 'session.py', 'session_verification.py'):
        (package / name).write_text('# fixture runtime\n')
    base = capture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    candidate = capture(store, source)
    (source / 'value.py').write_text('VALUE = 1\n')
    (source / 'removed.py').write_text('UNRELATED = True\n')
    root, child = project(tmp_path)
    activate(root, store.root, monkeypatch)
    store.set_meta('active_runtime', base)
    store.set_meta('trusted_verifier', 'a' * 64)
    (store.root / 'operator.json').write_text(json.dumps({'source_root': str(source)}))
    monkeypatch.delenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', raising=False)
    monkeypatch.setattr(native, '__file__', str(Path(base['path']) / 'src/auto_agents/recovery/native.py'))
    stream = store.binding(root, 'session:' + child.session_id)
    route = {'target_repository': str(source), 'issue_seed': {
        'summary': 'Repair the engine value', 'required_behavior': ['The engine value becomes two']}}
    payload = {'project': str(root), 'base': base['commit'], 'autonomy': 'max', 'provider': 'fixture',
        'invocation': {'command': 'fix', 'session_id': child.session_id, 'auto_approve': True, 'engine_route': route},
        'error': 'Original engine defect', 'symptom_key': 'original-symptom', 'fingerprint': 'original-fingerprint',
        'contract': {'expected_postconditions': ['The engine value becomes two']},
        'boundary': {'route_digest': repair_client.digest(route)}}
    identity = 'engine:' + digest([stream, payload['symptom_key'], payload['contract']])[:40]
    identifier = identity + ':incident'
    working = store.root / 'kernel-engine' / identity
    (working / 'target-evidence').mkdir(parents=True)
    (working / 'original-payload.json').write_text(json.dumps(payload))
    from auto_agents.repair_v2.evidence import identity as evidence_identity
    (working / 'target.json').write_text(json.dumps({'digest': evidence_identity(working / 'target-evidence')}))
    ref = store.put(payload)
    contract = Contract(store.load(stream)['goal_id'], identity + ':repair', 'engine_repair',
        store.put({'goal': child.goal}), ref, (), ('The engine value becomes two',), (base['path'],),
        store.put({'autonomy': 'max'}), 'preflight_recovered', ('plan', 'implement', 'verify', 'review'))
    store.apply(stream, store.load(stream)['revision'], Event(identity + ':bind', 'task_bound',
                                                            {'contract': contract.to_dict()}))
    class Effects:
        environment = 'e' * 64
        def source(self): return candidate['source']
        def execute(self, command):
            result = store.put({'artifact': candidate} if command.phase == 'implement' else {'ok': True})
            proof = Evidence(result, command.task_id, command.source, command.contract, command.environment,
                             'd' * 64, command.phase,
                             'preflight_recovered' if command.phase == 'review' else 'phase_completed')
            return Outcome(OutcomeKind.SUCCESS, 'Checked', (proof,), {'result_ref': result})
    completed = engine.EngineRunner(store, stream, contract, Effects()).run()
    def emit(kind, data, suffix):
        return store.apply(stream, store.load(stream)['revision'], Event(identity + ':' + suffix, kind, data))
    emit('incident_opened', {'incident_id': identifier, 'task_id': contract.task_id, 'occurrence_ref': ref,
        'payload_ref': ref, 'route_digest': repair_client.digest(route),
        'failure': Outcome(OutcomeKind.ENGINE_DEFECT, 'Original defect', details={'counterexample': ref}).to_dict()}, 'incident')
    emit('incident_resolved', {'incident_id': identifier, 'proof': completed['proofs']['review'][0],
        'continuation_id': identity + ':continue', 'next_task_id': 'fix:' + child.session_id,
        'required_runtime': candidate['source']}, 'resolved')
    orch = Orchestrator(root)
    orch._invocation_context = payload['invocation']
    args = SimpleNamespace(command='fix', project=str(root), session=child.session_id,
                           provider='fixture', auto_approve=True)
    def adopt(control, path):
        runtime = capture(store, path)
        receipt = independent_verify(store, runtime, {k: lambda _: {'ok': True} for k in MANDATORY_CHECKS}, 'a' * 64)
        activate_runtime(store, runtime, receipt)
    monkeypatch.setattr(manager, 'adopt_source', adopt)
    monkeypatch.setattr(engine, 'IsolatedEngineEffects', lambda *a, **k: pytest.fail('Completed repair must not call a model'))
    monkeypatch.setattr('auto_agents.cli_impl.adjudicate_auto_agents_error',
                        lambda *a, **k: pytest.fail('Pending adoption must not diagnose again'))
    # No installed supervisor is part of this private fixture.
    monkeypatch.setattr(manager, 'sync_supervisor', lambda store: None)
    return SimpleNamespace(store=store, source=source, base=base, candidate=candidate, root=root,
        child=child, stream=stream, identifier=identifier, identity=identity, route=route,
        payload=payload, orch=orch, args=args, adopt=adopt)


class Handoff(BaseException):
    pass


def resume_pending(pending, monkeypatch, *, fail_adoption=False):
    p = pending
    with pytest.raises(repair_client.EngineRepairRequired) as raised:
        repair_client.engine_route(p.orch, p.route)
    assert raised.value.resume_incident == (p.stream, p.identifier)
    from auto_agents.cli_impl import _triage_terminal_run_error, _auto_repair_auto_agents_and_resume
    triage = _triage_terminal_run_error(p.root, p.orch, raised.value)
    assert triage.decision.eligible
    execution = []
    def execve(binary, command, environment):
        execution.append((command, environment))
        raise Handoff()
    monkeypatch.setattr(engine.os, 'execve', execve)
    if fail_adoption:
        monkeypatch.setattr(manager, 'adopt_source', Mock(side_effect=KernelError('runtime_verification', 'rejected')))
        assert _auto_repair_auto_agents_and_resume(p.root, p.orch, raised.value, triage.decision, p.args, Mock()) == 3
    else:
        with pytest.raises(Handoff):
            _auto_repair_auto_agents_and_resume(p.root, p.orch, raised.value, triage.decision, p.args, Mock())
    return execution


def test_cold_cli_finishes_retained_delivery_and_accepts_merged_runtime(pending, monkeypatch):
    p = pending
    before = p.store.load(p.stream)
    execution = resume_pending(p, monkeypatch)
    state = p.store.replay(p.stream)
    assert state['commands'] == before['commands']
    assert state['budget'] == before['budget']
    assert state['incidents'] == before['incidents']
    assert state['continuations'][p.identity + ':continue']['status'] == 'consumed'
    runtime = p.store.meta('active_runtime')
    assert runtime['source'] != p.candidate['source']
    assert (p.source / 'value.py').read_text() == 'VALUE = 2\n'
    assert (p.source / 'removed.py').read_text() == 'UNRELATED = True\n'
    assert execution[0][1]['AUTO_AGENTS_RUNTIME_ID'] == runtime['artifact_id']
    assert execution[0][0][execution[0][0].index('--session') + 1] == p.child.session_id
    monkeypatch.setattr(repair_client, '__file__', str(Path(runtime['path']) / 'src/auto_agents/repair_client.py'))
    fresh = Orchestrator(p.root)
    fresh._invocation_context = p.payload['invocation']
    assert repair_client.engine_route(fresh, p.route) is True
    assert p.store.load(p.stream) == state


def test_failed_adoption_keeps_ready_continuation_and_retries_without_model_calls(pending, monkeypatch):
    p = pending
    before = p.store.load(p.stream)
    with monkeypatch.context() as patch:
        assert resume_pending(p, patch, fail_adoption=True) == []
    assert p.store.meta('active_runtime') == p.base
    assert p.store.load(p.stream) == before
    resume_pending(p, monkeypatch)
    assert p.store.load(p.stream)['budget'] == before['budget']


def test_later_adopted_runtime_must_preserve_the_accepted_patch(pending, monkeypatch):
    from auto_agents.recovery.engine_adoption import adopted
    p = pending
    resume_pending(p, monkeypatch)
    incident = p.store.load(p.stream)['incidents'][p.identifier]
    (p.source / 'later.txt').write_text('Unrelated later change\n')
    p.adopt(p.store.root, p.source)
    assert adopted(p.store, p.stream, incident)
    (p.source / 'value.py').write_text('VALUE = 1\n')
    p.adopt(p.store.root, p.source)
    assert not adopted(p.store, p.stream, incident)


def test_pending_adoption_rejects_a_different_route_before_submission(pending):
    from auto_agents.recovery.engine_adoption import retained_payload
    p = pending
    error = repair_client.EngineRepairRequired({'target_repository': str(p.source), 'issue_seed': {'summary': 'Other'}},
                                               resume_incident=(p.stream, p.identifier))
    with pytest.raises(KernelError, match='another route'):
        retained_payload(p.store, p.root, p.orch, error)


def test_restart_after_adoption_before_continuation_reuses_the_verified_runtime(pending, monkeypatch):
    from auto_agents.recovery import engine_adoption
    p = pending
    original = engine_adoption.record
    def interrupted(*args):
        original(*args)
        raise SystemExit('after adoption proof, before continuation')
    with monkeypatch.context() as patch:
        patch.setattr(engine_adoption, 'record', interrupted)
        with pytest.raises(SystemExit, match='before continuation'):
            resume_pending(p, patch)
    runtime = p.store.meta('active_runtime')
    assert p.store.load(p.stream)['continuations'][p.identity + ':continue']['status'] == 'ready'
    monkeypatch.setattr(native, '__file__', str(Path(runtime['path']) / 'src/auto_agents/recovery/native.py'))
    monkeypatch.setattr(manager, 'adopt_source', lambda *args: pytest.fail('Already adopted runtime must be reused'))
    resume_pending(p, monkeypatch)
    assert p.store.meta('active_runtime') == runtime
    assert p.store.load(p.stream)['continuations'][p.identity + ':continue']['status'] == 'consumed'
