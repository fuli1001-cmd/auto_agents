import json

import pytest

from auto_agents import cli
from auto_agents.config import load_run_state, save_run_state, save_session_state
from auto_agents.models import SessionState
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_session_verification_ownership import project


@pytest.fixture(autouse=True)
def disable_external_notifications(monkeypatch):
    monkeypatch.setenv('WECHAT_WEBHOOK_URL', '')


def terminal_fixture(tmp_path, monkeypatch, *, mode='collab', status='blocked'):
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', '1')
    root, _ = project(tmp_path)
    workflow = WorkflowStore(root).create_root(WorkflowRef(mode, 'terminal'))
    state = SessionState('terminal', mode=mode, status=status, workflow_id=workflow.workflow_id,
                         goal='Observe existing behavior with bounded real calls', auto_approve=True,
                         resolution='acceptance_blocked', attempt_epoch=7, current_attempt=5,
                         attempts_since_progress=3, hard_ceiling=25,
                         acceptance_execution={'phase': 'blocked', 'identity': 'retained-input',
                             'directory': '/retained/acceptance-evidence',
                             'result': {'status': 'blocked', 'summary': 'Configured correction ceiling 4 exceeds agent default 3',
                                        'evidence': ['report.json']}})
    save_session_state(root, state)
    run = load_run_state(root)
    run.status, run.last_error = 'blocked', 'run stopped by user'
    run.resume_context['workflow_id'] = 'unrelated-workflow'
    save_run_state(root, run)
    protected = {p: (root / p).read_bytes() for p in ['.auto-agents/state/run_state.json',
                  '.auto-agents/state/sessions/terminal/session_state.json']}
    monkeypatch.setattr(Session, 'resume', lambda *a: state)
    monkeypatch.setattr(Session, 'offer_resume_or_new', lambda *a: state)
    monkeypatch.setattr(WorkflowCoordinator, 'resume_workflow', lambda *a: state)
    return root, state, protected








def test_rejected_review_and_explicit_user_limits_survive_evidence_capture():
    from auto_agents.controlled_failure import capture
    state = SessionState('review', mode='collab', status='blocked', resolution='acceptance_review_rejected',
        conversation=[{'role': 'user', 'content': 'At most two paid corrective calls.'}],
        acceptance_execution={'phase': 'blocked', 'result': {'status': 'passed', 'summary': 'All done'},
            'review': {'approved': False, 'reason': 'No actual playback evidence; api_key=hidden-secret'},
            'inputs': {'request': {'continuation_constraints': ['Agent-derived cap 3']}}})
    failure = capture(state)
    assert 'No actual playback evidence' in str(failure) and 'All done' not in str(failure)
    assert 'hidden-secret' not in json.dumps(failure.evidence) + str(failure)
    assert failure.evidence['user_instructions'] == ['At most two paid corrective calls.']
    assert failure.evidence['derived_acceptance_plan']['continuation_constraints'] == ['Agent-derived cap 3']


def test_agent_error_capture_keeps_cause_before_terminal_summary():
    from auto_agents.controlled_failure import capture
    state = SessionState('stopped', mode='collab', status='failed', resolution='agent_errors_exhausted',
        execution_log=[{'action': 'agent_error', 'result': 'No verified progress; preserve the candidate'},
                       {'action': 'session_stopped', 'result': 'agent_errors_exhausted'}])
    assert capture(state).evidence['reason'] == 'No verified progress; preserve the candidate'
