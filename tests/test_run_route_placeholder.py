"""Unused run identities admit retained routes without adopting other work."""
import json

import pytest

from auto_agents.config import load_run_state, save_run_state, save_session_state
from auto_agents.io_utils import write_json
from auto_agents.models import RunState, SessionState, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_session import _make_project, _confirm_collab_state
from test_workflow_chain import _commit_baseline


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', '1')
    monkeypatch.setenv('WECHAT_WEBHOOK_URL', '')
    root = _make_project(str(tmp_path))
    current = load_run_state(root)
    save_run_state(root, RunState(current.run_id))
    write_json(root / '.auto-agents/state/task_plan.json', {'tasks': []})
    _commit_baseline(root)
    coordinator = WorkflowCoordinator(Orchestrator(root), auto_approve=True)
    snapshot = coordinator.store.create_root(WorkflowRef('collab', 'parent'))
    payload = {'spec_seed': {'title': 'Resume original project', 'goal': 'Resume original project',
        'gap': 'Browser recovery entry is missing', 'capability': 'Resume from browser',
        'acceptance': ['Resume preserves project identity'], 'non_goals': [],
        'evidence': [], 'open_decisions': []},
        'goal_execution_environment': {'mode': 'real', 'confirmed': True},
        'authorization_policy': {'allow_real_provider_calls': True}}
    handoff = coordinator.store.prepare_handoff(snapshot, parent=snapshot.root, target='run',
        goal='Resume original project', reason='Required recovery entry', payload=payload)
    return root, coordinator, snapshot, handoff


def test_unstarted_run_is_bound_once_without_completed_archive(scene, monkeypatch):
    root, coordinator, snapshot, handoff = scene
    original = load_run_state(root).run_id
    calls = []
    def execute(**kwargs):
        current = load_run_state(root)
        calls.append(current.run_id)
        assert current.status == 'pending' and current.tasks == []
        assert current.resume_context['parent_handoff_id'] == handoff.handoff_id
        assert current.resume_context['goal_execution_environment'] == handoff.payload['goal_execution_environment']
        assert current.resume_context['authorization_policy'] == handoff.payload['authorization_policy']
        assert kwargs['spec_file'].read_text().find('Resume original project') >= 0
        current.status = 'completed'
        save_run_state(root, current)
        return current
    monkeypatch.setattr(coordinator.orch, 'run', execute)
    monkeypatch.setattr(coordinator.orch, '_start_new_iteration', lambda *a, **kw: pytest.fail('Archived unused run'))
    before = (root / '.auto-agents/state/run_state.json').read_bytes()
    assert coordinator.prepare_run_route()[0]
    assert (root / '.auto-agents/state/run_state.json').read_bytes() == before
    assert coordinator._drive_run_child(handoff, snapshot)['status'] == 'completed'
    assert handoff.child == WorkflowRef('run', original)
    assert coordinator._drive_run_child(handoff, snapshot)['status'] == 'completed'
    assert calls == [original]


def test_restart_after_context_save_reuses_same_run_and_spec(scene, monkeypatch):
    root, coordinator, snapshot, handoff = scene
    with monkeypatch.context() as patch:
        patch.setattr(coordinator.store, 'bind_child', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            coordinator._drive_run_child(handoff, snapshot)
    current = load_run_state(root)
    spec = root / current.resume_context['spec_file']
    retained = spec.read_bytes()
    monkeypatch.setattr(coordinator.orch, 'run', lambda **kw: current)
    coordinator._drive_run_child(handoff, snapshot)
    assert handoff.child == WorkflowRef('run', current.run_id)
    assert spec.read_bytes() == retained
    assert len(list((root / 'specs/iterations').glob('*.md'))) == 1


@pytest.mark.parametrize('field,value', [
    ('status', 'paused'), ('current_stage', 'plan'), ('pending_approval', 'clarify'),
    ('approved_gates', ['clarify']), ('tasks', [TaskSpec('owned', 'Owned task', 'Owned task', ['Owned check'])]),
    ('stage_summaries', {'clarify': 'started'}), ('agent_attempts', {'clarify': 1}),
    ('resume_context', {'workflow_id': 'other'}), ('last_error', 'stopped by user'),
    ('pending_input_requests', [{'id': 'waiting'}]), ('active_blocker', {'owner': 'user'}),
    ('health_control', {'action': 'stop'}),
])
def test_existing_run_evidence_blocks_adoption(scene, field, value):
    root, coordinator, snapshot, handoff = scene
    current = load_run_state(root)
    setattr(current, field, value)
    save_run_state(root, current)
    before = (root / '.auto-agents/state/run_state.json').read_bytes()
    assert not coordinator.prepare_run_route()[0]
    assert coordinator._drive_run_child(handoff, snapshot)['resolution'] == 'active_run_conflict'
    assert (root / '.auto-agents/state/run_state.json').read_bytes() == before
    assert handoff.child is None


@pytest.mark.parametrize('owner', ['plan', 'workflow', 'handoff', 'run_files'])
def test_ownership_outside_run_projection_blocks_adoption(scene, owner):
    root, coordinator, snapshot, handoff = scene
    current = load_run_state(root)
    base = root / '.auto-agents/state'
    if owner == 'plan':
        write_json(base / 'task_plan.json', {'tasks': [{'id': 'owned'}]})
    elif owner == 'workflow':
        coordinator.store.create_root(WorkflowRef('run', current.run_id))
    elif owner == 'handoff':
        coordinator.store.bind_child(snapshot, handoff, WorkflowRef('run', current.run_id))
    else:
        path = root / '.auto-agents/runs' / current.run_id
        path.mkdir(parents=True)
    assert not coordinator.prepare_run_route()[0]


@pytest.mark.parametrize('later_user', [False, True])
def test_replay_controller_refused_run_without_new_model_call(scene, monkeypatch, later_user):
    root, coordinator, snapshot, handoff = scene
    coordinator.store.record_result(snapshot, handoff, status='blocked', result={'status': 'blocked'})
    coordinator.store.consume_result(snapshot, handoff, operation_id='previous-return')
    state = _confirm_collab_state(SessionState('parent', mode='collab', status='executing',
        workflow_id=snapshot.workflow_id, goal=handoff.goal, auto_approve=True), 'real')
    route = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'run', 'reason': handoff.reason,
        'spec_seed': handoff.payload['spec_seed']})
    refusal = f'run {load_run_state(root).run_id} remains pending; no new run handoff was created'
    state.conversation = [{'role': 'user', 'content': state.goal}, {'role': 'agent', 'content': route},
        {'role': 'orchestrator', 'content': refusal}, {'role': 'agent', 'content': 'Engine investigation failed'},
        {'role': 'orchestrator', 'content': 'Cannot read blocking evidence'}]
    state.execution_log = [{'action': 'run_route_deferred', 'result': refusal}, {'action': 'collab'}]
    acceptance = {'phase': 'blocked', 'identity': 'existing-acceptance',
                  'result': {'status': 'blocked', 'summary': 'Browser recovery is missing'}}
    state.acceptance_execution = dict(acceptance)
    if later_user:
        state.conversation.append({'role': 'user', 'content': 'Do a different task'})
    save_session_state(root, state)
    session = Session(coordinator.orch, mode='collab', auto_approve=True, coordinator=coordinator)
    monkeypatch.setattr(coordinator.orch, '_call_with_failover', lambda *a: pytest.fail('New model call'))
    result = (coordinator.recover_unstarted_run_route(session, state) if later_user else
              session._drive_local_owned(state))
    if later_user:
        assert result is None and not state.active_handoff_id
    else:
        assert result.status == 'waiting_child' and state.active_handoff_id
        assert state.execution_log[-1]['action'] == 'run_route_placeholder_reconciled'
        assert coordinator.recover_unstarted_run_route(session, state) is None
    assert state.acceptance_execution == acceptance
