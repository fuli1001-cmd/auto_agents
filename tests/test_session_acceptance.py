import json
from pathlib import Path
import sqlite3

import pytest

from auto_agents.config import load_run_state, load_session_state, save_run_state, save_session_state
from auto_agents.models import AgentResult, SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_session import _confirm_collab_state
from test_session_verification_ownership import project


def setup_acceptance(tmp_path, monkeypatch, *, legacy=False, outcome='passed', approved=True):
    root, _ = project(tmp_path)
    run = load_run_state(root)
    run.status, run.last_error = 'blocked', 'run stopped by user'
    save_run_state(root, run)
    state = _confirm_collab_state(SessionState(session_id='accept', mode='collab', status='executing',
        goal='Check the existing value without adding features', auto_approve=True), 'real')
    route = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'run' if legacy else 'acceptance',
        'spec_seed': {'scope': 'existing_behavior_real_acceptance_only', 'acceptance': ['Inspect the existing value']}})
    if legacy:
        state.conversation = [{'role': 'agent', 'content': route}, {'role': 'orchestrator', 'content': 'run remains blocked'},
            {'role': 'agent', 'content': 'NEED_USER_ASSIST v1: {"decision_class":"goal_choice","question":"Resume the stopped task?"}'}]
        state.execution_log = [{'action': 'run_route_deferred', 'result': 'run remains blocked'}, {'action': 'collab'}]
        state.status, state.resume_phase, state.resolution = 'paused', 'executing', 'interrupted_by_user'
    save_session_state(root, state)
    preserved = {p: (root / p).read_bytes() for p in ['.auto-agents/state/run_state.json', '.auto-agents/state/task_plan.json', 'value.py']}
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        if request.purpose == 'collab':
            current = load_session_state(root, state.session_id)
            reply = ('GOAL_ACHIEVED: Observed the existing value'
                     if current.acceptance_execution.get('phase') == 'blocked' else route)
        elif request.purpose == 'acceptance_execute':
            assert request.sandbox_mode == 'workspace-write'
            directory = request.cwd / '.auto-agents/state/sessions/accept/acceptance'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'observation.txt').write_text((request.cwd / 'value.py').read_text())
            if outcome == 'mutation':
                (request.cwd / 'value.py').write_text('MUTATED\n')
            reply = json.dumps({'status': 'passed' if outcome == 'mutation' else outcome,
                                'summary': 'Observed the existing value', 'evidence': ['observation.txt']})
        elif request.purpose == 'acceptance_review':
            assert request.sandbox_mode == 'read-only'
            reply = json.dumps({'approved': approved, 'reason': 'Checked the actual observation against the goal'})
        else:
            pytest.fail('unexpected stage: ' + request.purpose)
        return AgentResult(True, [], request.output_path, summary=reply)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    monkeypatch.setattr(WorkflowCoordinator, 'prepare_run_route', lambda *a: pytest.fail('ambient run preflight'))
    monkeypatch.setattr(Orchestrator, 'run', lambda *a, **kw: pytest.fail('resumed stopped development'))
    return root, state, calls, preserved


@pytest.mark.parametrize('legacy', [False, True])
def test_acceptance_independent_of_stopped_run_and_reuses_completed_result(tmp_path, monkeypatch, legacy):
    root, state, calls, preserved = setup_acceptance(tmp_path, monkeypatch, legacy=legacy)
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'completed', result.execution_log[-3:]
    assert calls == ([] if legacy else ['collab']) + ['acceptance_execute', 'acceptance_review']
    before = list(calls)
    resumed = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert resumed.status == 'completed' and calls == before
    assert {p: (root / p).read_bytes() for p in preserved} == preserved


@pytest.mark.parametrize('outcome,approved', [('mutation', True), ('blocked', True), ('passed', False)])
def test_acceptance_does_not_pass_defects_mutations_or_rejected_evidence(tmp_path, monkeypatch, outcome, approved):
    root, state, calls, preserved = setup_acceptance(tmp_path, monkeypatch, legacy=True, outcome=outcome, approved=approved)
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'blocked'
    assert {p: (root / p).read_bytes() for p in preserved} == preserved
    if outcome != 'passed':
        assert 'acceptance_review' not in calls


def test_restart_after_execution_reuses_result_before_review(tmp_path, monkeypatch):
    root, state, calls, preserved = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    provider = Orchestrator._call_with_failover
    def interrupted(self, request):
        if request.purpose == 'acceptance_review':
            raise KeyboardInterrupt()
        return provider(self, request)
    with monkeypatch.context() as patch:
        patch.setattr(Orchestrator, '_call_with_failover', interrupted)
        result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'paused'
    resumed = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert resumed.status == 'completed'
    assert calls == ['acceptance_execute', 'acceptance_review']


def test_legacy_acceptance_survives_real_resume_bookkeeping(tmp_path, monkeypatch):
    root, state, calls, preserved = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    state.provider_continuations = {'collab': {'provider_session_id': 'retained-diagnostic-session'}}
    save_session_state(root, state)
    def obsolete_question(prompt):
        pytest.fail('Replayed the obsolete scope-expansion question: ' + prompt)
    result = Session(Orchestrator(root, user_input_fn=obsolete_question), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'completed'
    assert calls == ['acceptance_execute', 'acceptance_review']
    actions = [row['action'] for row in result.execution_log]
    assert 'provider_continuation_invalidated' in actions and 'attempt_epoch_started' in actions
    assert {p: (root / p).read_bytes() for p in preserved} == preserved


@pytest.mark.parametrize('change', ['unrelated_refusal', 'different_route', 'answered_question'])
def test_legacy_recovery_requires_the_unanswered_acceptance_boundary(tmp_path, monkeypatch, change):
    from auto_agents.session_acceptance import recover_deferred
    root, state, _, _ = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    state.execution_log.extend({'action': 'attempt_epoch_started'} for _ in range(10))
    if change == 'unrelated_refusal':
        state.conversation[-2]['content'] = 'A different issue needs a user decision'
    elif change == 'different_route':
        state.conversation[-3]['content'] = 'ROUTE_WORKFLOW v1: {"target":"run","spec_seed":{"scope":"new_feature"}}'
    else:
        state.conversation.append({'role': 'user', 'content': 'I choose a new goal'})
    assert not recover_deferred(Session(Orchestrator(root), mode='collab'), state)
    assert not state.acceptance_execution


def test_completed_acceptance_with_changed_evidence_cannot_be_reused(tmp_path, monkeypatch):
    root, state, calls, _ = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'completed'
    (root / '.auto-agents/state/sessions/accept/acceptance/observation.txt').write_text('changed')
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status != 'completed'
    assert calls == ['acceptance_execute', 'acceptance_review']


def test_acceptance_user_question_survives_interruption_without_model_retry(tmp_path, monkeypatch):
    root, state, calls, _ = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    original = Orchestrator._call_with_failover
    asked = []
    def provider(self, request):
        if request.purpose == 'acceptance_execute' and not asked:
            asked.append(True)
            return AgentResult(True, [], request.output_path, summary=json.dumps({
                'status': 'needs_user', 'summary': 'Need an observation only the user can make',
                'decision_class': 'external_observation', 'question': '浏览器是否显示返回值为 0？'}))
        return original(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    def interrupt(prompt):
        raise KeyboardInterrupt()
    result = Session(Orchestrator(root, user_input_fn=interrupt), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'paused' and calls == []
    questions = []
    def answer(prompt):
        questions.append(prompt)
        assert calls == []
        return '是，显示为 0。'
    result = Session(Orchestrator(root, user_input_fn=answer), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'completed'
    assert len(questions) == 1 and '浏览器是否显示' in questions[0]
    assert calls == ['acceptance_execute', 'acceptance_review']


def test_cli_failure_details_stay_in_diagnostics(tmp_path, monkeypatch, capsys):
    from auto_agents import cli
    root, state, _, _ = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    def failure(*args):
        raise RuntimeError('internal detail /private/worktree/hf-123')
    monkeypatch.setattr(Session, 'resume', failure)
    monkeypatch.setattr(cli, '_triage_terminal_run_error', lambda *args: None)
    assert cli.main(['collab', '--project', str(root), '--session', state.session_id,
                     '--auto-approve', '--no-health-watch']) == 1
    output = capsys.readouterr()
    assert 'internal detail' not in output.err + output.out
    assert 'Failed' in output.err or '执行失败' in output.err


def test_cli_acceptance_uses_completed_child_and_preserves_stopped_run(tmp_path, monkeypatch, capsys):
    from auto_agents.cli import main
    from test_engine_child_recovery import configure_local_writer, parent_workflow, REAL_PROVIDER_CALL
    root, child = project(tmp_path)
    exclude = root / '.git/info/exclude'
    exclude.write_text(exclude.read_text() + '\n.env\n.data/\n')
    (root / '.env').write_text('RUNTIME_DB=.data/runtime.db\nTOKEN=retained-test-secret\n')
    (root / '.data').mkdir()
    with sqlite3.connect(root / '.data/runtime.db') as connection:
        connection.execute('CREATE TABLE receipts (operation TEXT)')
        connection.execute("INSERT INTO receipts VALUES ('already-submitted')")
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')")
    parent_workflow(root, child)
    parent = load_session_state(root, 'parent')
    parent.goal = 'Observe that the existing product returns value 1; no development.'
    save_session_state(root, parent)
    run = load_run_state(root)
    run.status, run.last_error = 'blocked', 'run stopped by user'
    save_run_state(root, run)
    protected = {p: (root / p).read_bytes() for p in ['.auto-agents/state/run_state.json', '.auto-agents/state/task_plan.json', 'value.py', '.env', '.data/runtime.db']}
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        if request.purpose == 'fix':
            return REAL_PROVIDER_CALL(self, request)
        if request.purpose == 'collab':
            reply = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'acceptance', 'spec_seed': {'acceptance': ['Observe value 1']}})
        elif request.purpose == 'acceptance_execute':
            assert request.cwd != root
            assert (request.cwd / 'value.py').read_text() == 'VALUE = 1\n'
            assert not (request.cwd / '.env').exists()
            assert not (request.cwd / '.data/runtime.db').exists()
            context, _ = json.JSONDecoder().raw_decode(request.prompt.split('Runtime context (controller-owned): ', 1)[1])
            assert context == {'code_root': str(request.cwd), 'runtime_source_root': str(root)}
            assert 'process-local environment variables' in request.prompt
            assert 'retained-test-secret' not in request.prompt
            runtime = Path(context['runtime_source_root'])
            values = dict(line.split('=', 1) for line in (runtime / '.env').read_text().splitlines())
            database = runtime / values['RUNTIME_DB']
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                assert connection.execute('SELECT operation FROM receipts').fetchall() == [('already-submitted',)]
            directory = request.cwd / '.auto-agents/state/sessions/parent/acceptance'
            (directory / 'value.txt').write_text((request.cwd / 'value.py').read_text())
            reply = json.dumps({'status': 'passed', 'summary': 'Observed value 1', 'evidence': ['value.txt']})
        else:
            assert request.purpose == 'acceptance_review'
            reply = json.dumps({'approved': True, 'reason': 'The observation establishes the existing goal'})
        return AgentResult(True, [], request.output_path, summary=reply)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    assert main(['collab', '--project', str(root), '--session', 'parent', '--auto-approve', '--no-health-watch']) == 0
    assert calls == ['fix', 'collab', 'acceptance_execute', 'acceptance_review']
    assert load_session_state(root, child.session_id).current_attempt == 1
    assert load_session_state(root, 'parent').status == 'completed'
    assert {p: (root / p).read_bytes() for p in protected} == protected
    output = capsys.readouterr()
    assert 'ROUTE_WORKFLOW' not in output.err and 'candidate_custody' not in output.out
    assert str(root) not in output.err


@pytest.mark.parametrize('entrypoint', ['session', 'workflow'])
def test_blocked_acceptance_resume_diagnoses_before_retry_and_retains_evidence(tmp_path, monkeypatch, capsys, entrypoint):
    from auto_agents.cli import main
    root, state, calls, protected = setup_acceptance(tmp_path, monkeypatch, legacy=True, outcome='blocked')
    first = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert first.status == 'blocked'
    prior = Path(first.acceptance_execution['directory'])
    prior_bytes = (prior / 'observation.txt').read_bytes()
    epoch, attempts, count = first.attempt_epoch, first.attempts_since_progress, first.current_attempt
    saved_result = first.acceptance_execution['result']
    def provider(self, request):
        calls.append(request.purpose)
        current = load_session_state(root, state.session_id)
        if request.purpose == 'collab':
            assert current.acceptance_execution['result'] == saved_result
            assert current.attempt_epoch == epoch
            assert current.attempts_since_progress == attempts + 1
            assert current.current_attempt == count + 1
            assert str(prior) in request.prompt and 'Observed the existing value' in request.prompt
            assert 'Do not repeat generation or paid operations during diagnosis' in request.prompt
            reply = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'acceptance', 'spec_seed': {'reason': 'Use the existing runtime binding'}})
        elif request.purpose == 'acceptance_execute':
            assert calls[-2] == 'collab'
            saved = current.acceptance_execution
            assert saved['inputs']['prior_evidence_directories'] == [str(prior)]
            directory = Path(saved['directory'])
            assert directory != prior
            (directory / 'observation.txt').write_text('Observed after correcting the runtime binding')
            reply = json.dumps({'status': 'passed', 'summary': 'Observed the existing value', 'evidence': ['observation.txt']})
        else:
            assert request.purpose == 'acceptance_review'
            reply = json.dumps({'approved': True, 'reason': 'Checked the actual observation'})
        return AgentResult(True, [], request.output_path, summary=reply)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    argv = (['collab', '--session', state.session_id, '--auto-approve'] if entrypoint == 'session' else
            ['resume', '--workflow', first.workflow_id])
    assert main([*argv, '--project', str(root), '--no-health-watch']) == 0
    result = load_session_state(root, state.session_id)
    assert result.status == 'completed'
    assert calls == ['acceptance_execute', 'collab', 'collab', 'acceptance_execute', 'acceptance_review']
    assert (prior / 'observation.txt').read_bytes() == prior_bytes
    assert {p: (root / p).read_bytes() for p in protected} == protected
    assert main([*argv, '--project', str(root), '--no-health-watch']) == 0
    assert len(calls) == 5


def test_acceptance_recovery_does_not_reopen_ownership_blocker(tmp_path, monkeypatch):
    root, state, calls, _ = setup_acceptance(tmp_path, monkeypatch, legacy=True, outcome='blocked')
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    result.resolution = 'verification_ownership'
    save_session_state(root, result)
    resumed = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert resumed.status == 'blocked' and resumed.resolution == 'verification_ownership'
    assert calls == ['acceptance_execute', 'collab']


@pytest.mark.parametrize('outcome', ['blocked', 'passed'])
def test_acceptance_recovery_cannot_replace_evidence_review_with_completion_marker(tmp_path, monkeypatch, outcome):
    root, state, calls, _ = setup_acceptance(tmp_path, monkeypatch, legacy=True, outcome=outcome, approved=False)
    first = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert first.status == 'blocked'
    saved = first.acceptance_execution
    def provider(self, request):
        calls.append(request.purpose)
        assert request.purpose == 'collab'
        return AgentResult(True, [], request.output_path, summary='GOAL_ACHIEVED: The product works now')
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    monkeypatch.setattr(Session, '_run_verify', lambda *a, **kw: pytest.fail('ordinary gates cannot replace acceptance evidence'))
    resumed = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert resumed.status == 'blocked'
    assert resumed.acceptance_execution == saved
    assert resumed.execution_log[-1]['action'] == 'acceptance_completion_rejected'
    assert calls[-1] == 'collab'


def test_blocked_acceptance_routes_fix_and_rechecks_delivered_candidate_without_restart(tmp_path, monkeypatch):
    from auto_agents.cli import main
    from test_engine_child_recovery import configure_local_writer, REAL_PROVIDER_CALL

    root, state, calls, protected = setup_acceptance(tmp_path, monkeypatch)
    child = load_session_state(root, 'owned-child')
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')")
    prior = root / '.auto-agents/state/sessions/accept/acceptance'
    verify = './.conda/bin/python -m pytest -q tests/test_owned.py::test_owned'
    issue = {'summary': 'Repair the observed value', 'reproduction': ['Observe VALUE = 0'],
             'expected': 'VALUE = 1', 'actual': 'VALUE = 0',
             'verification_scope': {'mode': 'focused_fix'}, 'verification_command': verify}
    acceptance_count = 0
    def provider(self, request):
        nonlocal acceptance_count
        calls.append(request.purpose)
        current = load_session_state(root, state.session_id)
        if request.purpose == 'collab':
            if not current.acceptance_execution or current.last_child_result_ref:
                if current.last_child_result_ref:
                    assert request.cwd != root
                    assert (request.cwd / 'value.py').read_text() == 'VALUE = 1\n'
                    assert current.acceptance_execution['result']['status'] == 'blocked'
                    assert (prior / 'observation.txt').read_text() == 'VALUE = 0\n'
                reply = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'acceptance',
                    'spec_seed': {'steps': ['Observe the existing value']}})
            else:
                assert current.acceptance_execution['result']['status'] == 'blocked'
                assert current.acceptance_execution['recovery_started'] is True
                assert current.current_attempt == 3 and current.attempts_since_progress == 3
                assert current.attempt_epoch == 1
                assert 'route target="fix"' in request.prompt
                reply = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'fix', 'issue_seed': issue})
        elif request.purpose == 'fix_converse':
            reply = 'FIX_DISPOSITION v1: ' + json.dumps({'decision': 'fix', **issue})
        elif request.purpose == 'fix':
            assert request.cwd != root
            return REAL_PROVIDER_CALL(self, request)
        elif request.purpose == 'acceptance_execute':
            acceptance_count += 1
            saved = current.acceptance_execution
            directory = Path(saved['directory'])
            value = (request.cwd / 'value.py').read_text()
            if acceptance_count == 1:
                assert value == 'VALUE = 0\n'
            else:
                assert acceptance_count == 2 and value == 'VALUE = 1\n'
                assert saved['inputs']['prior_evidence_directories'] == [str(prior)]
                assert directory != prior
            (directory / 'observation.txt').write_text(value)
            reply = json.dumps({'status': 'blocked' if acceptance_count == 1 else 'passed',
                                'summary': 'Observed value 0' if acceptance_count == 1 else 'Observed value 1',
                                'evidence': ['observation.txt']})
        else:
            assert request.purpose == 'acceptance_review'
            reply = json.dumps({'approved': True, 'reason': 'Actual observation proves value 1'})
        return AgentResult(True, [], request.output_path, summary=reply)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    monkeypatch.setattr(Orchestrator, '_prompt_user', lambda *a, **kw: pytest.fail('unexpected approval'))
    argv = ['collab', '--project', str(root), '--session', state.session_id, '--auto-approve', '--no-health-watch']
    assert main(argv) == 0
    result = load_session_state(root, state.session_id)
    assert result.status == 'completed'
    assert calls == ['collab', 'acceptance_execute', 'collab', 'fix_converse', 'fix',
                     'collab', 'acceptance_execute', 'acceptance_review']
    assert (prior / 'observation.txt').read_text() == 'VALUE = 0\n'
    assert sum(row['action'] == 'acceptance_recovery_started' for row in result.execution_log) == 1
    assert {p: (root / p).read_bytes() for p in protected} == protected
    assert main(argv) == 0
    assert len(calls) == 8


def test_repeated_acceptance_blockers_consume_existing_budget(tmp_path, monkeypatch):
    root, state, calls, protected = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    state.hard_ceiling = 5
    save_session_state(root, state)
    directories = []
    def provider(self, request):
        calls.append(request.purpose)
        current = load_session_state(root, state.session_id)
        if request.purpose == 'collab':
            assert current.acceptance_execution['phase'] == 'blocked'
            reply = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'acceptance',
                'spec_seed': {'steps': ['Recheck the existing goal']}})
        else:
            assert request.purpose == 'acceptance_execute'
            directory = Path(current.acceptance_execution['directory'])
            directories.append(directory)
            (directory / 'blocker.txt').write_text('Still blocked')
            reply = json.dumps({'status': 'blocked', 'summary': 'Still blocked', 'evidence': ['blocker.txt']})
        return AgentResult(True, [], request.output_path, summary=reply)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'blocked' and result.resolution == 'acceptance_blocked'
    assert result.attempts_since_progress == result.current_attempt == 5
    assert len(calls) == 5 and calls.count('acceptance_execute') == 3
    assert len(set(directories)) == 3
    assert all((directory / 'blocker.txt').read_text() == 'Still blocked' for directory in directories)
    assert {p: (root / p).read_bytes() for p in protected} == protected


@pytest.mark.parametrize('reply', [
    'ROUTE_WORKFLOW v1 {"target":"acceptance","spec_seed":{"steps":["Inspect"]}}',
    'ROUTE_WORKFLOW v1: {"target":"acceptance",',
])
def test_malformed_route_gets_complete_format_feedback(tmp_path, monkeypatch, reply):
    root, state, _, _ = setup_acceptance(tmp_path, monkeypatch)
    session = Session(Orchestrator(root), mode='collab')
    routed, error = session._route_collab_workflow_reply(state, reply)
    assert routed is None and 'colon after v1 is required' in error
    assert 'ROUTE_WORKFLOW v1: {' in error
    assert not state.active_handoff_id and not state.acceptance_execution
    for prompt in (session._build_collab_prompt(state, ''), session._build_converse_prompt(state)):
        assert 'The colon after v1 is required' in prompt
        assert 'ROUTE_WORKFLOW v1: {"target":"acceptance"' in prompt
