"""Terminal diagnosis follows its subject rather than another scope's history."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auto_agents import cli
from auto_agents.controlled_failure import capture
from auto_agents.models import SessionState
from auto_agents.recovery.convergence import scope_id
from auto_agents.recovery.model_progress import stopped_search
from test_recovery_convergence_policy import scene, operation, verify
from test_recovery_kernel import emit, finish, success


@pytest.mark.parametrize('completed_child', [False, True])
def test_route_exhaustion_never_reports_another_candidate_as_current_failure(scene, tmp_path, monkeypatch, completed_child):
    store, contract = scene
    verify(store, contract, 'old-failure', {'owned-test': 'failed'}, reason='Old candidate failure')
    if completed_child:
        verify(store, contract, 'accepted', {'owned-test': 'failed'}, ok=True,
               reason='Known baseline output from a successful verification')
    parent = replace(contract, task_id='collab:parent', kind='collab', phases=('route',),
                     completion='goal_accepted')
    emit(store, 'task_bound', {'contract': parent.to_dict()})
    for key in ['first-route', 'second-route']:
        command = operation(store, parent, 'route', key, model=True)
        finish(store, command, success(store, command))
    root = tmp_path / 'project'
    store.bind(root, 'session:parent', 'workflow')
    from auto_agents.recovery import authority
    monkeypatch.setattr(authority, 'installed', lambda *a: store)
    refusal = 'run unused remains pending; no new run handoff was created'
    state = SessionState('parent', mode='collab', status='failed', resolution='kernel_no_progress',
        goal='Resume the original project', execution_log=[{'action': 'run_route_deferred', 'result': refusal},
        {'action': 'collab', 'result': 'Investigated run admission'}])
    before = deepcopy(store.replay('workflow'))
    diagnostic = stopped_search(root, capture(state))
    assert diagnostic['scope'] == scope_id(before, parent.task_id)
    assert diagnostic['next_action'] == 'inspect_route_evidence'
    assert diagnostic['reason'] == refusal
    assert 'last_rejection' not in diagnostic
    reporter = Mock(language='zh')
    orch = SimpleNamespace(reporter=reporter)
    monkeypatch.setattr(cli, '_triage_terminal_run_error', lambda *a: pytest.fail('New model diagnosis'))
    cli._triage_controlled_workflow_result(root, orch, state,
        SimpleNamespace(command='collab', auto_approve=True, full_verify=False), None, Mock())
    text = '\n'.join(call.args[0] for call in reporter.text.call_args_list)
    assert refusal in text
    assert '候选验证仍未通过' not in text and '请修正保留候选' not in text
    record = json.loads((root / '.auto-agents/state/sessions/parent/terminal-triage.json').read_text())
    assert 'Old candidate failure' not in record['triage']['reason']
    assert 'successful verification' not in record['triage']['reason']
    assert store.replay('workflow') == before
