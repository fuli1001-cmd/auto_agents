import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from auto_agents.models import AgentResult, AgentTermination
from auto_agents.recovery import Outcome, OutcomeKind
from auto_agents.recovery.convergence import decision, scope, diagnosis_usage
from auto_agents.recovery.native import provider_outcome
from auto_agents.recovery.observations import diagnosis_schema
from auto_agents.recovery.rejections import request_rejection, reconcile
from test_recovery_convergence_policy import scene, operation, verify, write
from test_recovery_kernel import finish


def rejected():
    error = {'type': 'error', 'status': 400, 'error': {'type': 'invalid_request_error',
        'code': 'invalid_json_schema', 'param': 'text.format.schema',
        'message': "'uniqueItems' is not permitted"}}
    return {'ok': False, 'returncode': -1, 'cleanup_incomplete': False, 'stdout': '', 'summary': '',
            'usage': None, 'termination': {'reason': 'provider_error', 'active_tool': ''},
            'stderr': '\n'.join([json.dumps({'type': 'thread.started'}), json.dumps({'type': 'turn.started'}),
                                 json.dumps({'type': 'error', 'message': json.dumps(error)})])}


def test_schema_uses_supported_wire_keywords_and_local_rules_keep_uniqueness():
    schema = diagnosis_schema(['test'], 'observation')
    assert 'uniqueItems' not in json.dumps(schema)
    assert 'minLength' not in json.dumps(schema)
    assert set(schema['required']) == set(schema['properties'])
    assert schema['additionalProperties'] is False


@pytest.mark.parametrize('change', [
    {'summary': 'Partial model reply'}, {'stdout': 'Partial stream'}, {'cleanup_incomplete': True},
    {'usage': {'output_tokens': 1}}, {'termination': {'reason': 'connection_lost'}},
    {'termination': {'reason': 'provider_error', 'active_tool': 'shell'}},
])
def test_uncertain_or_effectful_calls_are_not_known_rejections(change):
    assert request_rejection({**rejected(), **change}) is None


def test_tool_activity_or_different_error_is_not_schema_rejection():
    value = rejected()
    value['stderr'] += '\n' + json.dumps({'type': 'item.started', 'item': {'type': 'command_execution'}})
    assert request_rejection(value) is None
    value = rejected()
    value['stderr'] = value['stderr'].replace('invalid_json_schema', 'server_error')
    assert request_rejection(value) is None


def test_recorded_codex_error_is_a_protocol_failure_not_unknown_effect(tmp_path):
    value = rejected()
    result = AgentResult(False, [], tmp_path/'result', returncode=-1, stderr=value['stderr'],
                         termination=AgentTermination(reason='provider_error'))
    assert provider_outcome(SimpleNamespace(), result)[0] == OutcomeKind.PROTOCOL_INVALID


def test_rejection_reconciliation_preserves_usage_and_has_one_format_correction(scene):
    store, contract = scene
    for i in range(2): write(store, contract, 'w-' + str(i))
    verify(store, contract, 'observed', {'test': 'failed'})
    commands = []
    for i in range(2):
        command = operation(store, contract, 'diagnose', 'diagnosis-' + str(i), model=True)
        reference = store.put(rejected())
        finish(store, command, Outcome(OutcomeKind.OUTCOME_UNKNOWN, 'Old adapter classified provider error',
               details={'native_result': reference, 'post_source': command.source}))
        before = deepcopy(store.load('workflow')['budget'])
        assert reconcile(store, 'workflow') == [command.command_id]
        after = store.replay('workflow')
        assert after['budget'] == before
        assert after['commands'][command.command_id]['status'] == 'finished'
        assert after['commands'][command.command_id]['outcome']['kind'] == 'protocol_invalid'
        commands.append(command.command_id)
        assert decision(after, 'fix', 'diagnose', command.source)['allowed'] == (i == 0)
    item = scope(after, 'fix')
    assert len(item['rejected_requests']) == 2
    assert diagnosis_usage(item)[1] is True
    assert reconcile(store, 'workflow') == []


def test_reconciliation_does_not_clear_unknown_when_source_changed(scene):
    store, contract = scene
    for i in range(2): write(store, contract, 'w-' + str(i))
    verify(store, contract, 'observed', {'test': 'failed'})
    command = operation(store, contract, 'diagnose', 'diagnosis', model=True)
    finish(store, command, Outcome(OutcomeKind.OUTCOME_UNKNOWN, 'Source changed',
        details={'native_result': store.put(rejected()), 'post_source': 'f'*64}))
    before = store.replay('workflow')
    assert reconcile(store, 'workflow') == []
    assert store.replay('workflow') == before


def test_request_format_recovery_reuses_failed_evidence_without_reverification(tmp_path, monkeypatch):
    from auto_agents.config import load_session_state
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.recovery import native
    from auto_agents.recovery.policy import enable, resume_rejected_diagnosis
    from test_recovery_native import activate
    from test_session_verification_ownership import project
    root, initial = project(tmp_path)
    store = activate(root, tmp_path/'control', monkeypatch)
    stream = store.binding(root, 'session:' + initial.session_id)
    enable(store, stream)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    state = load_session_state(root, initial.session_id)
    session._current_state = state
    monkeypatch.setattr(native, '_source', lambda *args: 'a'*64)
    for i in range(2):
        native.perform(session, 'implement', 'write-' + str(i), lambda: {'ok': True},
                       lambda r: (OutcomeKind.SUCCESS, 'Implementation completed'), model=True)
    native.perform(session, 'verify', 'failed', lambda: {'ok': False,
        'verification_checks': [{'id': 'tests/test_owned.py::test_owned', 'status': 'failed'}]},
        lambda r: (OutcomeKind.CANDIDATE_REJECTED, 'Retained failure'))
    from auto_agents.recovery.model import KernelError
    with pytest.raises(KernelError):
        native.perform(session, 'diagnose', 'old-adapter', lambda: rejected(),
            lambda r: (OutcomeKind.OUTCOME_UNKNOWN, 'Legacy classification'), model=True)
    reconcile(store, stream)
    before = deepcopy(store.load(stream)['budget'])
    assert not resume_rejected_diagnosis(session, state)
    store.set_meta('active_runtime', {'source': 'e'*64})
    assert resume_rejected_diagnosis(session, state)
    assert state.status == 'executing'
    assert store.replay(stream)['budget'] == before
    monkeypatch.setattr(native, '_source', lambda *args: 'b'*64)
    assert not resume_rejected_diagnosis(session, state)
