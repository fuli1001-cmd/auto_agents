"""The retained failed parent has a saved route but no active handoff."""
from copy import deepcopy
import json
import os
import subprocess
import sys

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_multilayer_engine_recovery import incident, replay
from test_engine_reference_recovery import ENGINE
from auto_agents.repair_control import digest


def saved_route(tmp_path):
    root, child, store, snapshot, original, active, route, old = incident(tmp_path)
    route = Session._fix_workflow_payload({'issue_seed': deepcopy(route['issue_seed'])})
    parent = load_session_state(root, 'parent')
    parent.status, parent.active_handoff_id, parent.resume_phase = 'failed', '', 'executing'
    parent.return_phase = 'after_child'
    parent.current_attempt, parent.attempt_epoch, parent.attempts_since_progress, parent.hard_ceiling = 2, 10, 1, 25
    parent.conversation.append({'role': 'assistant', 'content': 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'fix', **route})})
    save_session_state(root, parent)
    snapshot.active_handoff_id = ''
    store.save(snapshot)
    child.current_attempt, child.attempt_epoch, child.attempts_since_progress, child.hard_ceiling = 0, 0, 0, 15
    save_session_state(root, child)
    return root, parent, child, route


def test_saved_engine_route_preserves_distinct_parent_and_child_budgets(tmp_path):
    root, parent, child, route = saved_route(tmp_path)
    report = replay(root, route, tmp_path)
    assert report['ok'], report
    observation = report['recovery_observation']
    assert observation['parent_session_id'] == parent.session_id
    assert observation['parent_constraints_preserved'] and observation['child_constraints_preserved']
    assert observation['boundary_kind'] == 'implementation' and observation['preflight_rechecked']
    assert observation['diagnostic_provider_calls'] == 0
    for name, original in [('parent', parent), ('child', child)]:
        budget = observation[name + '_budget']
        assert budget['before'] == budget['after']
        assert budget['before']['current_attempt'] == original.current_attempt
        assert budget['before']['attempt_epoch'] == original.attempt_epoch
        assert budget['before']['attempts_since_progress'] == original.attempts_since_progress
        assert budget['before']['hard_ceiling'] == original.hard_ceiling
    saved = load_session_state(root, parent.session_id)
    assert saved.execution_log[:len(parent.execution_log)] == parent.execution_log
    assert not any(row.get('action') == 'attempt_epoch_started' for row in saved.execution_log[len(parent.execution_log):])


@pytest.mark.parametrize('mutation', ['user_message', 'ordinary_fix', 'missing_child', 'wrong_receipt'])
def test_pending_reply_is_not_itself_engine_authority(tmp_path, monkeypatch, mutation):
    root, parent, child, route = saved_route(tmp_path)
    if mutation == 'user_message': parent.conversation[-1]['role'] = 'user'
    elif mutation == 'ordinary_fix':
        parent.conversation[-1]['content'] = 'ROUTE_WORKFLOW v1: {"target":"fix","issue_seed":{}}'
    elif mutation == 'missing_child':
        route['issue_seed']['failed_handoff_id'] = 'missing'
        parent.conversation[-1]['content'] = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'fix', **route})
    monkeypatch.setattr('auto_agents.repair_client.engine_route', lambda *a: False)
    coordinator = WorkflowCoordinator(Orchestrator(root))
    before = parent.to_dict()
    if mutation == 'missing_child':
        from auto_agents.session_verification import SessionOwnershipError
        with pytest.raises(SessionOwnershipError): coordinator._pending_engine_resume(parent)
    else:
        assert coordinator._pending_engine_resume(parent) is False
    assert parent.to_dict() == before


def test_replay_rejects_parent_budget_change_even_after_child_entry(tmp_path):
    root, parent, child, route = saved_route(tmp_path)
    marker = tmp_path / 'receipt.json'
    marker.write_text(json.dumps({'route_digest': digest(route), 'engine_route': route}))
    program = '''
import sys,runpy
engine,project = sys.argv[1:]
sys.path.insert(0, engine + '/src')
from auto_agents import session
save = session.save_session_state
def corrupt(root, state):
    if state.session_id == 'parent' and state.status == 'waiting_child':
        state.attempt_epoch += 1
    return save(root, state)
session.save_session_state = corrupt
sys.argv = ['replay', engine, project, 'parent', 'collab']
runpy.run_path(engine + '/src/auto_agents/session_replay.py', run_name='__main__')
'''
    completed = subprocess.run([sys.executable, '-B', '-c', program, str(ENGINE), str(root)],
        capture_output=True, text=True, timeout=90, env={**os.environ,
            'AUTO_AGENTS_REPAIR_ROUTE_PROBE': str(marker), 'AUTO_AGENTS_REPAIR_CONTROL_DISABLED': '1',
            'AUTO_AGENTS_STORAGE_MAINTENANCE': 'off'})
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.splitlines()[-1])
    observed = report['recovery_observation']
    assert observed['boundary_kind'] == 'implementation' and observed['child_constraints_preserved']
    assert not observed['parent_constraints_preserved'] and not observed['retained_constraints']
    assert not report['ok']
