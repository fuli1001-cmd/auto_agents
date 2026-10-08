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
    state = _confirm_collab_state(SessionState('parent', mode='collab', status='executing', workflow_id=snapshot.workflow_id, goal=GOAL, auto_approve=True), 'real')
    save_session_state(root, state)
    session = Session(coordinator.orch, mode='collab', auto_approve=True, coordinator=coordinator)
    return (root, coordinator, snapshot, state, session)

@pytest.mark.parametrize('goal, expected', [(GOAL, True), ('Run acceptance of existing behavior, no new features or planning.', True), ('Build a new video application with five new features.', False), ('Implement the approved missing recovery capability.', False), ('Review existing behavior and propose new product requirements.', False)])
def test_scope_comes_from_explicit_user_goal(goal, expected):
    assert acceptance_only(goal) is expected
