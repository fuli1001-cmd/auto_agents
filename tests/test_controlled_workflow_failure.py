import json

import pytest

from auto_agents import cli
from auto_agents.config import load_run_state, save_run_state, save_session_state
from auto_agents.models import SessionState
from auto_agents.self_repair import SelfRepairDecision, SelfRepairJudgment, SelfRepairTriageResult
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_session_verification_ownership import project


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


def decision(owner):
    eligible = owner == 'auto_agents'
    return SelfRepairTriageResult(SelfRepairDecision(eligible, reason='Source-backed ownership decision',
                                                   category='controlled_failure', fingerprint='current-failure'),
        source='root_cause', reason='Source-backed ownership decision',
        judgment=SelfRepairJudgment('SELF_REPAIR' if eligible else 'BLOCK', owner, eligible, eligible,
                                   0.99, 'controlled_failure', 'Source-backed ownership decision', ['source:line']))


@pytest.mark.parametrize('command', ['collab', 'fix', 'resume', 'new_collab'])
@pytest.mark.parametrize('status', ['blocked', 'failed'])
@pytest.mark.parametrize('owner', ['auto_agents', 'target_project', 'unknown'])
def test_controlled_results_are_diagnosed_and_only_approved_engine_failures_dispatch(
        tmp_path, monkeypatch, command, status, owner):
    root, state, protected = terminal_fixture(tmp_path, monkeypatch, mode='fix' if command == 'fix' else 'collab', status=status)
    observed, repaired = [], []
    triage = decision(owner)

    def investigate(project_root, orchestrator, error):
        observed.append(error)
        assert project_root == root
        context = orchestrator._invocation_context
        assert context['session_id'] == state.session_id and context['run_id'] == ''
        assert context['workflow_id'] == state.workflow_id
        assert context['controlled_failure'] == error.evidence
        assert error.evidence['acceptance']['result']['summary'].startswith('Configured correction ceiling')
        assert error.evidence['acceptance']['directory'] == '/retained/acceptance-evidence'
        assert error.evidence['goal'] == state.goal
        return triage

    def repair(project_root, orchestrator, error, approval, args, lock, diagnosis=None):
        from auto_agents.run_lock import require_project_run_lock
        repaired.append(error)
        assert approval.eligible and require_project_run_lock(root) is lock
        assert args.session == state.session_id and args.auto_approve
        assert orchestrator._invocation_context['command'] == state.mode
        parsed = cli.build_parser().parse_args(cli._run_command_for_self_repair_resume(args)[2:])
        assert parsed.command == state.mode and parsed.session == state.session_id and parsed.auto_approve
        return 0

    monkeypatch.setattr(cli, '_triage_terminal_run_error', investigate)
    monkeypatch.setattr(cli, '_auto_repair_auto_agents_and_resume', repair)
    argv = (['resume', '--workflow', state.workflow_id] if command == 'resume' else
            ['collab', '--auto-approve'] if command == 'new_collab' else
            [command, '--session', state.session_id, '--auto-approve'])
    code = cli.main([*argv, '--project', str(root), '--no-health-watch'])
    assert code == (0 if owner == 'auto_agents' else 3 if status == 'blocked' else 1)
    assert len(observed) == 1 and len(repaired) == (1 if owner == 'auto_agents' else 0)
    saved = json.loads((root / '.auto-agents/state/sessions/terminal/terminal-triage.json').read_text())
    assert saved['owner'] == owner and saved['failure']['status'] == status
    assert saved['triage']['decision']['eligible'] == (owner == 'auto_agents')
    assert {p: (root / p).read_bytes() for p in protected} == protected
    assert (state.attempt_epoch, state.current_attempt, state.attempts_since_progress, state.hard_ceiling) == (7, 5, 3, 25)


def test_controlled_run_resume_uses_the_returned_run_and_records_ownership(tmp_path, monkeypatch):
    root, _, _ = terminal_fixture(tmp_path, monkeypatch)
    run = load_run_state(root)
    workflow = WorkflowStore(root).create_root(WorkflowRef('run', run.run_id))
    run.resume_context['workflow_id'] = workflow.workflow_id
    run.last_error = 'Provider did not return a usable result'
    save_run_state(root, run)
    before = (root / '.auto-agents/state/run_state.json').read_bytes()
    monkeypatch.setattr(WorkflowCoordinator, 'resume_workflow', lambda *a: run)
    def adjudicate(orchestrator, **kwargs):
        assert kwargs['state'].run_id == run.run_id
        assert not orchestrator._invocation_context['session_id']
        assert orchestrator._invocation_context['controlled_failure']['kind'] == 'run'
        return decision('external_provider')
    monkeypatch.setattr(cli, 'adjudicate_auto_agents_error', adjudicate)
    assert cli.main(['resume', '--project', str(root), '--workflow', workflow.workflow_id, '--no-health-watch']) == 3
    from auto_agents.config import run_path
    saved = json.loads((run_path(root, run.run_id) / 'outputs/terminal-triage.json').read_text())
    assert saved['owner'] == 'external_provider'
    assert (root / '.auto-agents/state/run_state.json').read_bytes() == before


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


@pytest.mark.parametrize('owner', ['auto_agents', 'target_project'])
def test_controlled_failure_reaches_independent_root_cause_review(tmp_path, owner):
    from pathlib import Path
    from auto_agents.controlled_failure import capture
    from auto_agents.repair_cases import terminal_repair_case
    from test_root_cause import RootCauseCoordinatorTests, _report
    failure = capture(SessionState('accept', mode='collab', status='blocked',
        goal='Inspect the existing product', resolution='acceptance_blocked',
        acceptance_execution={'result': {'status': 'blocked', 'summary': 'Configured ceiling 4 exceeds agent default 3'}}))
    case = terminal_repair_case(run_id='session-accept', error=failure, stage='collab', owner_hint='unknown')
    case.failure_scope = 'session'
    case.invocation_context = {'session_id': 'accept', 'run_id': '', 'command': 'collab',
                               'controlled_failure': failure.evidence}
    coordinator, provider, target = RootCauseCoordinatorTests()._coordinator(tmp_path, [
        {**_report(role='investigator', verdict='ROOT_CAUSE', owner=owner), 'failure_scope': 'session'},
        {**_report(role='reviewer', verdict='AGREE', owner=owner), 'failure_scope': 'session'},
    ], repair_case=case)
    coordinator.state, coordinator.error = None, failure
    unrelated = (target / '.auto-agents/state/run_state.json').read_bytes()
    result = coordinator.run()
    assert result.repair_approved == (owner == 'auto_agents')
    assert [request.stage for request in provider.requests] == ['self_repair_investigator', 'self_repair_reviewer']
    assert all(request.sandbox_mode == 'read-only' for request in provider.requests)
    assert all('not a verified ownership decision' in request.prompt for request in provider.requests)
    evidence = json.loads(Path(result.evidence_path).read_text())
    assert not evidence['run_state']
    assert evidence['repair_case']['invocation_context']['controlled_failure'] == failure.evidence
    assert (target / '.auto-agents/state/run_state.json').read_bytes() == unrelated


@pytest.mark.parametrize('status', ['paused', 'waiting_user', 'waiting_child'])
def test_non_failure_session_states_do_not_trigger_diagnosis(tmp_path, monkeypatch, status):
    root, state, _ = terminal_fixture(tmp_path, monkeypatch, status=status)
    monkeypatch.setattr(cli, '_triage_terminal_run_error', lambda *a: pytest.fail('non-terminal failure was diagnosed'))
    assert cli.main(['collab', '--project', str(root), '--session', state.session_id, '--no-health-watch']) == 3
    assert not (root / '.auto-agents/state/sessions/terminal/terminal-triage.json').exists()


def test_unavailable_diagnosis_preserves_original_failure_without_recursive_triage(tmp_path, monkeypatch):
    root, state, protected = terminal_fixture(tmp_path, monkeypatch)
    called = []
    def unavailable(*args):
        called.append(True)
        raise RuntimeError('diagnosis service unavailable')
    monkeypatch.setattr(cli, '_triage_terminal_run_error', unavailable)
    monkeypatch.setattr(cli, '_auto_repair_auto_agents_and_resume', lambda *a, **kw: pytest.fail('repair without diagnosis'))
    assert cli.main(['collab', '--project', str(root), '--session', state.session_id, '--no-health-watch']) == 3
    assert len(called) == 1
    saved = json.loads((root / '.auto-agents/state/sessions/terminal/terminal-triage.json').read_text())
    assert saved['owner'] == 'unknown' and saved['diagnosis_error'] == 'diagnosis service unavailable'
    assert {p: (root / p).read_bytes() for p in protected} == protected


def test_controlled_session_diagnosis_excludes_the_unrelated_saved_run(tmp_path, monkeypatch):
    root, state, protected = terminal_fixture(tmp_path, monkeypatch)
    diagnoses = []
    def adjudicate(orchestrator, **kwargs):
        diagnoses.append(kwargs)
        assert kwargs['state'] is None
        assert orchestrator._invocation_context['controlled_failure']['subject_id'] == state.session_id
        return decision('target_project')
    monkeypatch.setattr(cli, 'adjudicate_auto_agents_error', adjudicate)
    assert cli.main(['collab', '--project', str(root), '--session', state.session_id, '--no-health-watch']) == 3
    assert len(diagnoses) == 1
    assert {p: (root / p).read_bytes() for p in protected} == protected
