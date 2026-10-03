"""Acceptance owns execution/review calls without resetting diagnostic budgets."""
from copy import deepcopy

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.models import AgentRequest, AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.recovery.convergence import decision, scope
from auto_agents.recovery.model import KernelError
from auto_agents.recovery.native import provider, _source
from auto_agents.recovery.policy import enable, session_stop
from auto_agents.workflow_chain import WorkflowStore, WorkflowRef
from test_session_acceptance import setup_acceptance
from test_recovery_native import activate


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root, state, _, _ = setup_acceptance(tmp_path, monkeypatch)
    workflow = WorkflowStore(root).create_root(WorkflowRef('collab', state.session_id))
    state.workflow_id = workflow.workflow_id
    state.acceptance_execution = {'phase': 'pending'}
    save_session_state(root, state)
    store = activate(root, tmp_path / 'control', monkeypatch)
    stream = store.binding(root, 'session:' + state.session_id)
    enable(store, stream)
    orch = Orchestrator(root)
    usage = {'workflow_kind': 'collab', 'subject_id': state.session_id}
    def call(purpose, key, execute=None):
        request = AgentRequest(key, 'balanced', 'Inspect the existing goal ' + key,
            root, tmp_path / key, purpose=purpose, attempt_id=key,
            sandbox_mode='read-only' if purpose != 'acceptance_execute' else 'workspace-write',
            usage_context=usage)
        return provider(orch, request, execute or (lambda r: AgentResult(True, [], r.output_path, summary='Observed existing value')))
    call('collab', 'route-one')
    call('collab', 'route-two')
    return root, state, store, stream, orch, call


def test_acceptance_execute_and_review_have_real_permits_after_route_exhaustion(scene):
    root, state, store, stream, orch, call = scene
    task = 'collab:' + state.session_id
    snapshot = store.load(stream)
    assert not decision(snapshot, task, 'route', _source(orch, state))['allowed']
    before_routes = deepcopy(scope(snapshot, task)['routes'])
    before_calls = snapshot['budget']['model_calls']
    orch._custody_control_root = root
    assert session_stop(orch, state) == ''
    assert call('acceptance_execute', 'execute').ok
    state = load_session_state(root, state.session_id)
    state.acceptance_execution['phase'] = 'reviewing'
    save_session_state(root, state)
    assert session_stop(orch, state) == ''
    assert call('acceptance_review', 'review').ok
    final = store.replay(stream)
    assert final['budget']['model_calls'] == before_calls + 2
    assert scope(final, task)['routes'] == before_routes
    assert all(c['phase'] == 'acceptance' for c in final['commands'].values()
               if c['operation_key'] not in {snapshot['commands'][k]['operation_key'] for k in snapshot['commands']})
    state.acceptance_execution['phase'] = 'blocked'
    assert 'bounded_route' in session_stop(orch, state)


def test_acceptance_keeps_unknown_effect_and_user_budget_fences(scene):
    root, state, store, stream, orch, call = scene
    task = 'collab:' + state.session_id
    snapshot = store.load(stream)
    snapshot['budget']['limit'] = snapshot['budget']['model_calls']
    assert decision(snapshot, task, 'acceptance', _source(orch, state))['reason'] == 'user_budget_exhausted'
    snapshot = store.load(stream)
    next(iter(snapshot['commands'].values()))['status'] = 'unknown'
    assert decision(snapshot, task, 'acceptance', _source(orch, state))['reason'] == 'outcome_unknown'


def test_acceptance_review_mutation_cannot_be_cached_as_approval(scene):
    root, state, store, stream, orch, call = scene
    def mutation(request):
        (root / 'value.py').write_text('UNREVIEWED = 2\n')
        return AgentResult(True, [], request.output_path, summary='{"approved":true,"reason":"Pretended review"}')
    with pytest.raises(KernelError, match='Read-only review changed'):
        call('acceptance_review', 'mutating-review', mutation)
    final = store.replay(stream)
    command = max(final['commands'].values(), key=lambda row: row['sequence'])
    assert command['phase'] == 'acceptance' and command['outcome']['kind'] == 'ownership_conflict'
    assert not command['outcome']['evidence']
