"""Browser acceptance cannot silently adopt the project's development backlog."""
import json

import pytest

from auto_agents.collab_goal_scope import acceptance_only
from auto_agents.config import load_run_state, save_session_state
from auto_agents.models import SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_session import _make_project, _confirm_collab_state
from test_workflow_chain import _commit_baseline


GOAL = '对当前已有功能执行真实端到端验收，通过前端创建视频，不新增产品需求，不重新设计或规划项目。'


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', '1')
    root = _make_project(str(tmp_path))
    _commit_baseline(root)
    coordinator = WorkflowCoordinator(Orchestrator(root), auto_approve=True)
    snapshot = coordinator.store.create_root(WorkflowRef('collab', 'parent'))
    state = _confirm_collab_state(SessionState('parent', mode='collab', status='executing',
        workflow_id=snapshot.workflow_id, goal=GOAL, auto_approve=True), 'real')
    save_session_state(root, state)
    session = Session(coordinator.orch, mode='collab', auto_approve=True, coordinator=coordinator)
    return root, coordinator, snapshot, state, session


@pytest.mark.parametrize('goal, expected', [
    (GOAL, True),
    ('Run acceptance of existing behavior, no new features or planning.', True),
    ('Build a new video application with five new features.', False),
    ('Implement the approved missing recovery capability.', False),
    ('Review existing behavior and propose new product requirements.', False),
])
def test_scope_comes_from_explicit_user_goal(goal, expected):
    assert acceptance_only(goal) is expected


@pytest.mark.parametrize('reason', ['Missing recovery button', 'Previously approved feature is unfinished'])
def test_missing_capability_claim_cannot_start_full_run(scene, monkeypatch, reason):
    root, coordinator, snapshot, state, session = scene
    before = load_run_state(root).to_dict()
    monkeypatch.setattr(coordinator, 'prepare_run_route', lambda *a: pytest.fail('Run preflight'))
    result = session._prepare_workflow_handoff(state, target='run', reason=reason,
        payload={'spec_seed': {'steps': ['Implement a whole recovery subsystem'],
                 'approved_contract_refs': ['An old approved prototype']}})
    assert result.status == 'executing' and not result.active_handoff_id
    assert state.execution_log[-1]['action'] == 'collab_acceptance_scope_deferred'
    assert 'focused fix' in state.conversation[-1]['content']
    assert not list((root / 'specs/iterations').glob('*.md'))
    assert load_run_state(root).to_dict() == before


def test_resume_chain_cannot_restore_development_for_acceptance(scene):
    root, coordinator, snapshot, state, session = scene
    old = coordinator.store.prepare_handoff(snapshot, parent=snapshot.root, target='run',
        goal=state.goal, reason='Old development detour', payload={'spec_seed': {'scope': 'feature'}})
    coordinator.store.bind_child(snapshot, old, WorkflowRef('run', load_run_state(root).run_id))
    wrapper = coordinator.store.prepare_handoff(snapshot, parent=snapshot.root, target='resume',
        goal=state.goal, reason='Old resume', payload={'resume_handoff_id': old.handoff_id})
    session._prepare_workflow_handoff(state, target='resume', reason='Continue',
        payload={'resume_handoff_id': wrapper.handoff_id})
    assert not state.active_handoff_id
    assert state.execution_log[-1]['action'] == 'collab_acceptance_scope_deferred'


@pytest.mark.parametrize('seed', [{}, {'verification_scope': {'mode': 'focused_fix'}, 'task_ids': ['owned']}])
def test_repairs_cannot_adopt_planned_tasks(scene, seed):
    root, coordinator, snapshot, state, session = scene
    session._prepare_workflow_handoff(state, target='fix', reason='Specific acceptance blocker',
        payload={'issue_seed': seed})
    assert not state.active_handoff_id
    assert 'focused_fix' in state.conversation[-1]['content']


def test_old_contract_alone_is_not_reproduction_evidence(scene):
    root, coordinator, snapshot, state, session = scene
    session._prepare_workflow_handoff(state, target='fix', reason='Old requirement is incomplete',
        payload={'issue_seed': {'verification_scope': {'mode': 'focused_fix'},
            'approved_contract_refs': ['An old prototype'], 'summary': 'Build missing recovery'}})
    assert not state.active_handoff_id
    assert 'concrete observed failure' in state.conversation[-1]['content']


def test_focused_fix_for_observed_blocker_remains_available(scene, monkeypatch):
    root, coordinator, snapshot, state, session = scene
    monkeypatch.setattr(session, '_ensure_baseline', lambda *a: None)
    session._prepare_workflow_handoff(state, target='fix', reason='Browser action fails',
        payload={'issue_seed': {'summary': 'Repair existing browser action',
            'verification_scope': {'mode': 'focused_fix'},
            'reproduction': ['Recorded browser click returns a failure'],
            'verification_command': 'python -m pytest -q tests/test_browser_action.py'}})
    assert state.status == 'waiting_child'
    assert coordinator.store.load_handoff(state.active_handoff_id).target == 'fix'


@pytest.mark.parametrize('target', ['acceptance', 'run'])
def test_existing_acceptance_entry_retains_goal_and_saved_development(scene, target):
    root, coordinator, snapshot, state, session = scene
    before = load_run_state(root).to_dict()
    session._prepare_workflow_handoff(state, target=target, reason='Observe existing browser behavior',
        payload={'spec_seed': {'scope': 'existing_behavior_real_acceptance_only',
            'steps': ['Inspect code revision, current UI and recovery eligibility']}})
    assert not state.active_handoff_id and state.acceptance_execution['phase'] == 'pending'
    assert state.acceptance_execution['inputs']['goal'] == GOAL
    assert load_run_state(root).to_dict() == before
    prompt = session._build_collab_prompt(state, '')
    assert 'running service code/configuration' in prompt
    assert 'Historical specs or prototypes are context' in prompt
