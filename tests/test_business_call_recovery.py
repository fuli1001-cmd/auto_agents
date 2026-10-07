"""Confirmed provider failures must not poison later collab requests."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_agents.business_calls import provider
from auto_agents.business_state import BusinessStateError, BusinessStore
from auto_agents.config import create_session, load_session_state
from auto_agents.models import AgentRequest, AgentResult, AgentTermination, AgentUsage, ProvidersExhaustedError
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session


def test_unavailable_provider_does_not_leave_an_unknown_call(tmp_path, monkeypatch):
    root = tmp_path/'project'
    Orchestrator.init_project(root, 'demo', 'mock')
    orch = Orchestrator(root)
    orch.config.providers = {orch.config.active_provider: orch.config.providers[orch.config.active_provider]}
    monkeypatch.setattr(orch.adapter, 'available', lambda: False)
    execute = Mock()
    monkeypatch.setattr(orch.adapter, 'run', execute)
    request = AgentRequest('implement', 'deep', 'clarify the goal', root, root/'output',
        purpose='converse', attempt_id='converse-1',
        usage_context={'workflow_kind': 'collab', 'subject_id': 'session'})
    for identity in ['converse-1', 'converse-1', 'converse-2']:
        with pytest.raises(ProvidersExhaustedError, match='binary is unavailable') as caught:
            orch._call_with_failover(replace(request, attempt_id=identity))
        assert caught.value.result is None
        assert caught.value.category == 'unavailable'
        assert BusinessStore(root).pending() == []
    execute.assert_not_called()


@pytest.mark.parametrize('has_result', [False, True])
def test_exhaustion_is_settled_and_replayed_without_dispatch(tmp_path, has_result):
    owner = SimpleNamespace(project_root=tmp_path)
    request = AgentRequest('implement', 'deep', 'clarify the goal', tmp_path, tmp_path/'output',
        purpose='converse', attempt_id='converse-1',
        usage_context={'workflow_kind': 'collab', 'subject_id': 'session'})
    native = AgentResult(False, ['fake'], request.output_path, returncode=1,
        stderr='rate limit exceeded', termination=AgentTermination(reason='provider_error'),
        usage=AgentUsage(input_tokens=12)) if has_result else None
    failure = ProvidersExhaustedError('All providers exhausted', providers=['codex'],
                                      result=native, category='quota' if has_result else 'unavailable')
    execute = Mock(side_effect=failure)
    for _ in range(2):
        with pytest.raises(ProvidersExhaustedError) as caught:
            provider(owner, request, execute)
        assert caught.value.providers == ['codex']
        assert caught.value.category == failure.category
        assert caught.value.result == native
    assert execute.call_count == 1
    assert BusinessStore(tmp_path).pending() == []
    success = AgentResult(True, ['fake'], request.output_path, summary='clarified')
    assert provider(owner, replace(request, attempt_id='converse-2'), lambda _: success).ok


@pytest.mark.parametrize('failure', [RuntimeError('lost outcome'), KeyboardInterrupt()])
def test_unconfirmed_dispatch_remains_blocked(tmp_path, failure):
    request = AgentRequest('implement', 'deep', 'goal', tmp_path, tmp_path/'output',
        purpose='collab', attempt_id='first', usage_context={'subject_id': 'session'})
    owner = SimpleNamespace(project_root=tmp_path)
    with pytest.raises(type(failure)):
        provider(owner, request, Mock(side_effect=failure))
    execute = Mock()
    with pytest.raises(BusinessStateError) as caught:
        provider(owner, replace(request, attempt_id='second'), execute)
    assert caught.value.code == 'outcome_unknown'
    assert caught.value.details['pending_calls'][0]['id'] in str(caught.value)
    assert 'auto-agents reconcile-call' in str(caught.value)
    execute.assert_not_called()
    assert len(BusinessStore(tmp_path).pending()) == 1


def test_exhaustion_with_incomplete_cleanup_stays_unknown(tmp_path):
    request = AgentRequest('implement', 'deep', 'goal', tmp_path, tmp_path/'output',
        usage_context={'subject_id': 'session'})
    result = AgentResult(False, ['fake'], request.output_path, cleanup_incomplete=True)
    failure = ProvidersExhaustedError('All providers exhausted', providers=['codex'],
                                     result=result, category='cleanup')
    with pytest.raises(ProvidersExhaustedError):
        provider(SimpleNamespace(project_root=tmp_path), request, Mock(side_effect=failure))
    assert len(BusinessStore(tmp_path).pending()) == 1


def test_pending_call_stops_clarification_and_preserves_resume_phase(tmp_path, monkeypatch):
    root = tmp_path/'project'
    Orchestrator.init_project(root, 'demo', 'mock')
    orch = Orchestrator(root)
    state = create_session(root, 'collab')
    state.goal = 'Clarify the existing behavior'
    store = BusinessStore(root)
    store.reserve('old-call', 'old-session', 'plan', model=True)
    execute = Mock()
    monkeypatch.setattr(orch, '_call_with_failover_owned', execute)
    session = Session(orch, mode='collab')
    output = []
    monkeypatch.setattr(session, '_print', output.append)
    with pytest.raises(BusinessStateError) as caught:
        session._drive_local(state)
    assert caught.value.code == 'outcome_unknown'
    execute.assert_not_called()
    restored = load_session_state(root, state.session_id)
    assert restored.status == 'blocked'
    assert restored.resume_phase == 'conversing'
    assert restored.resolution == 'external_call_reconciliation_required'
    assert restored.consecutive_agent_errors == 0
    assert [item['action'] for item in restored.execution_log] == ['reconciliation_required']
    assert not any('Retrying clarification' in item or '(transient)' in item for item in output)
    assert store.pending()[0]['id'] == 'old-call'
