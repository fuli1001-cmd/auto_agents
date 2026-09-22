from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.repair_v2.scope import ScopeGuard, changes, context
from auto_agents.repair_v2.store import Store, atomic_json, digest
from auto_agents.repair_v2.types import RepairBlocked, ValidationResult
from auto_agents.repair_v2.comparison import POLICY, accepted, signatures, unchanged, verify
from auto_agents.scope_decisions import Decisions, choose
from test_repair_v2_controller import job


@pytest.fixture
def scene(tmp_path):
    project = tmp_path / 'project'
    source = tmp_path / 'engine'
    source.mkdir()
    (source / 'engine.py').write_text('value = 0\n')
    atomic_json(project / '.auto-agents/state/sessions/root/session_state.json', {
        'session_id': 'root', 'mode': 'collab', 'goal': 'Produce the existing real video',
        'last_error': 'the existing video step failed'})
    payload = {'project': str(project), 'invocation': {'session_id': 'root'}, 'error': 'video failed'}
    proposal = {'decision': 'required', 'blocked_step': 'existing video', 'consequence': 'cannot produce the video',
                'evidence_refs': ['engine.py'], 'recovery_check': 'existing video resumes'}
    return project, source, payload, proposal


def test_necessity_receipt_reuses_evidence_but_rejects_changed_source(scene, tmp_path):
    project, source, payload, proposal = scene
    guard = ScopeGuard(tmp_path / 'scope', payload, project, source)
    ref = guard.admit(proposal)
    assert guard.admit(None) == ref
    copied = ScopeGuard(tmp_path / 'worker', payload, project, source)
    assert copied.import_receipt(guard.store.read(ref))
    assert copied.admit(None) == ref
    (source / 'engine.py').write_text('value = 2\n')
    assert copied.current() is None
    assert copied.import_receipt(guard.store.read(ref)) is None


def test_skip_creates_no_issue_receipt_or_backlog(scene, tmp_path):
    project, source, payload, proposal = scene
    guard = ScopeGuard(tmp_path / 'scope', payload, project, source)
    with pytest.raises(RepairBlocked) as failure:
        guard.admit({'decision': 'skip', 'unrelated_problem': 'legacy cleanup'})
    assert failure.value.code == 'scope_skipped'
    assert not list((tmp_path / 'scope').iterdir())


def test_missing_or_foreign_evidence_cannot_authorize_an_implementation(scene, tmp_path):
    project, source, payload, proposal = scene
    guard = ScopeGuard(tmp_path / 'scope', payload, project, source)
    for refs in ([], ['../private'], ['/etc/passwd'], ['missing.py'], ['.env']):
        with pytest.raises(RepairBlocked): guard.admit({**proposal, 'evidence_refs': refs})
    assert guard.current() is None


def test_scope_identity_survives_rewording_provider_and_restart(scene, tmp_path):
    from auto_agents.repair_v2.transaction import frozen_request, transaction_root
    from auto_agents.repair_v2.types import Acceptance, RepairRequest
    project, source, payload, proposal = scene
    guard = ScopeGuard(tmp_path / 'input', payload, project, source)
    payload['scope_receipt'] = guard.store.read(guard.admit(proposal))
    payload.update(base='base', provider='codex', fingerprint='old words')
    config = {'root': str(tmp_path / 'control')}
    root = transaction_root(config, payload)
    request = RepairRequest('same', 'base', 'video', (Acceptance('video', 'video works'),), 'codex')
    request = frozen_request(root, payload, lambda: request)
    Store(root).save({'status': 'blocked', 'calls': 4, 'attempts': 1})
    changed = {**payload, 'fingerprint': 'new words', 'provider': 'another', 'base': 'new-base'}
    assert transaction_root(config, changed) == root
    assert frozen_request(root, changed, lambda: pytest.fail('request was recreated')) == request
    assert Store(root).load()['calls'] == 4


def test_default_limits_do_not_stop_the_fourth_repair_or_33rd_call(scene, tmp_path):
    from auto_agents.repair_v2.chain import RepairChain
    project, source, payload, proposal = scene
    config = {'root': str(tmp_path / 'control')}
    for index in range(5):
        chain = RepairChain(config, payload, tmp_path / str(index))
        chain.admit()
        for _ in range(8): chain.reserve('implement')
    assert chain.context()['used'] == {'transactions': 5, 'implementations': 40, 'model_calls': 40}
    assert all(v is None for v in chain.context()['limits'].values())


def report(snapshot='candidate', message='assert old_value == 2'):
    node = 'tests/test_old.py::test_old'
    return {'ok': False, 'snapshot': snapshot, 'cancelled': False, 'infrastructure': False,
        'checks': [{'unit': 'suite:old', 'ok': False, 'returncode': 1, 'source_unchanged': True,
            'failed': [node], 'missing': [node], 'collected': [node], 'skipped': [], 'call_failed': [node],
            'failure_details': [{'nodeid': node, 'phase': 'call', 'message': message}]}],
        'failures': [{'unit': 'suite:old', 'failed': [node], 'reason': message}]}


def test_old_failure_is_accepted_without_falsifying_raw_report(tmp_path):
    current, baseline = report(), report('baseline')
    proof = {'policy': POLICY, 'ok': True, 'snapshot': 'candidate', 'base': 'base',
             'validation_digest': digest(current), 'baseline_report': baseline,
             'unchanged_tests': ['tests/test_old.py::test_old']}
    assert verify(proof, current, base='base')
    store = Store(tmp_path)
    receipt = {'validation': store.artifact('validation', current), 'comparison': store.artifact('comparison', proof)}
    assert accepted(store, receipt, 'base')
    assert store.read(receipt['validation']) == current and not current['ok']
    assert not verify({**proof, 'base': 'different'}, current, base='base')
    assert not verify(proof, report(message='a new failure'), base='base')


@pytest.mark.parametrize('change', ['message', 'phase', 'collection', 'timeout', 'skip', 'required', 'count'])
def test_old_failure_comparison_does_not_hide_new_or_incomplete_results(change):
    old, current = report('baseline'), report()
    check = current['checks'][0]
    if change == 'message': check['failure_details'][0]['message'] = 'assert new_value == 4'
    if change == 'phase': check['failure_details'][0]['phase'] = 'setup'
    if change == 'collection': check['collected'] = []
    if change == 'timeout': check['timed_out'] = True
    if change == 'skip': check['skipped'] = ['tests/test_else.py::test_else']
    if change == 'required': check['unit'] = 'required:contract'
    if change == 'count': check['failed'].append('tests/test_new.py::test_new')
    assert not unchanged(current, old)


def test_each_actual_diff_hunk_needs_review_coverage(tmp_path):
    from auto_agents.repair_v2.controller import review_result
    text = json.dumps({'decision': 'APPROVE', 'findings': [],
        'coverage': [{'requirement': 'video', 'nodes': ['tests/test_video.py::test_video']}],
        'change_coverage': [{'change': 'one', 'requirement': 'video', 'reason': 'restore video', 'evidence': 'test_video'}]})
    with pytest.raises(RepairBlocked): review_result(text, 'candidate', {'video'}, {'one': 'needed', 'two': 'unrelated'})
    assert review_result(text, 'candidate', {'video'}, {'one': 'needed'}).ok


def test_user_choice_is_durable_explicit_and_not_repeated(scene):
    project, source, payload, proposal = scene
    ctx = context(project, payload)
    decisions = Decisions(project)
    request = decisions.create(ctx, {'question': '需要启用一项已停止的步骤。', 'suggestion': '只启用生成步骤。'})
    replies = iter(['', '1'])
    orch = SimpleNamespace(_prompt_user=lambda *a, **k: next(replies))
    assert choose(orch, request) == ('approve', '')
    assert decisions.read(request['id'])['status'] == 'pending'
    answered = decisions.answer(request['id'], 1, 'approve', goal_version=ctx['goal_version'])
    assert decisions.answer(request['id'], 1, 'approve') == answered
    assert choose(SimpleNamespace(_prompt_user=lambda *a, **k: pytest.fail('asked twice')), answered) == ('approve', '')
    with pytest.raises(ValueError): decisions.answer(request['id'], 1, 'keep')
    with pytest.raises(ValueError): decisions.answer(request['id'], 2, 'approve')


def test_detach_and_empty_input_do_not_approve(scene):
    project, source, payload, proposal = scene
    decisions = Decisions(project)
    request = decisions.create(context(project, payload), {'question': '改变目标？', 'suggestion': '增加一项功能。'})
    assert choose(SimpleNamespace(_prompt_user=lambda *a, **k: k['default']), request) is None
    assert decisions.read(request['id'])['status'] == 'pending'
    assert decisions.pending('session:root') == [request]
    with pytest.raises(ValueError): decisions.answer(request['id'], 1, 'approve', goal_version='changed')


def test_declined_expansion_cannot_be_reworded_into_a_fresh_prompt(scene):
    project, source, payload, proposal = scene
    ctx = context(project, payload)
    decisions = Decisions(project)
    first = decisions.create(ctx, {'question': '扩大？', 'suggestion': '恢复旧任务'})
    answered = decisions.answer(first['id'], 1, 'keep')
    assert decisions.create(ctx, {'question': '再试？', 'suggestion': '恢复之前的任务'}) == answered
    assert len(list(decisions.root.glob('*.json'))) == 1


@pytest.fixture
def control(scene, tmp_path):
    import os
    from auto_agents.repair_control import Supervisor, VERSION
    from auto_agents.run_lock import ProjectRunLock
    from test_repair_control import configuration, registration, failure
    project, source, payload, proposal = scene
    supervisor = Supervisor(configuration(tmp_path))
    with ProjectRunLock(project, environ={}) as lock:
        identity = supervisor.register({'payload': registration(project, lock.run_token)},
                                       [os.dup(lock.fileno)])['subscriber']
        incoming = {**failure(project), **payload}
        job = supervisor.store.submit(identity, incoming)
        supervisor.store.transition(job, 'completed', {'ok': True, 'engine': 'v2'})
        with supervisor.store.connect() as db:
            db.execute("UPDATE subscribers SET state='resuming' WHERE id=?", (identity,))
        question = Decisions(project).create(context(project, incoming),
                                            {'question': '需要恢复已停止的一步。', 'suggestion': '只恢复视频生成。'})
        def request(op, **values):
            return supervisor.dispatch({'version': VERSION, 'op': op, 'subscriber': identity,
                                        'decision': question['id'], '_peer_pid': os.getpid(), **values}, [])
        # `version` is the control protocol field; decision revision has its
        # own field and cannot overwrite the RPC protocol version.
        try:
            yield supervisor, identity, question, request
        finally:
            for entry in supervisor.registrations.values():
                for fd in entry['fds']: os.close(fd)


def test_foreground_choice_releases_waiting_child_without_new_repair(control, scene):
    supervisor, identity, question, request = control
    request('request-decision')
    assert supervisor.store.subscriptions()[0]['state'] == 'waiting_user'
    assert not supervisor.workers
    result = request('answer-decision', decision_version=1, answer='approve')
    assert not result['resume_original']
    assert supervisor.store.subscriptions()[0]['state'] == 'resuming'
    assert Decisions(scene[0]).read(question['id'])['answer'] == 'approve'


def test_background_or_stale_reply_cannot_authorize_goal_change(control, scene):
    supervisor, identity, question, request = control
    request('request-decision')
    with pytest.raises(RuntimeError): request('answer-decision', decision_version=1, answer='approve', _peer_pid=-1)
    with pytest.raises(ValueError): request('answer-decision', decision_version=2, answer='approve')
    assert Decisions(scene[0]).read(question['id'])['status'] == 'pending'


def test_business_process_cannot_claim_foreground_by_omitting_environment(control, scene, monkeypatch):
    import os
    supervisor, identity, question, request = control
    request('request-decision')
    previous = supervisor.store.subscriptions()[0]['payload']['foreground']
    registration = supervisor.registrations[identity]
    payload = {**registration['payload'], 'pid': 987654321, 'ticks': 'managed',
               'foreground': {'pid': 987654321, 'ticks': 'managed'}}
    supervisor.resumes[identity] = SimpleNamespace(pid=payload['pid'], poll=lambda: None)
    monkeypatch.setattr('auto_agents.repair_control.alive', lambda *a: True)
    supervisor.register({'payload': payload, '_peer_pid': payload['pid'], 'environment': {}},
                        [os.dup(registration['fds'][0])])
    assert supervisor.store.subscriptions()[0]['payload']['foreground'] == previous
    with pytest.raises(RuntimeError):
        request('answer-decision', decision_version=1, answer='approve', _peer_pid=payload['pid'])


def test_detached_wait_keeps_question_and_releases_project_lock(control, scene):
    supervisor, identity, question, request = control
    request('request-decision')
    request('detach-decision')
    assert request('decision-status')['detached']
    supervisor.tick()
    assert identity not in supervisor.registrations
    assert Decisions(scene[0]).read(question['id'])['status'] == 'pending'


def test_mixed_old_and_new_failures_only_feed_new_failure_back_to_writer():
    from auto_agents.repair_v2.comparison import matched, relevant_failures
    current, baseline = report(), report('base')
    new = deepcopy(current['checks'][0])
    new['unit'] = 'suite:new'
    new['failed'] = new['missing'] = new['collected'] = new['call_failed'] = ['tests/test_new.py::test_new']
    new['failure_details'] = [{'nodeid': new['failed'][0], 'phase': 'call', 'message': 'new bug'}]
    current['checks'].append(new)
    current['failures'].append({'unit': 'suite:new', 'failed': new['failed'], 'reason': 'new bug'})
    proof = {'policy': POLICY, 'ok': False, 'snapshot': current['snapshot'], 'validation_digest': digest(current),
             'baseline_report': baseline, 'unchanged_tests': ['tests/test_old.py::test_old']}
    assert not verify(proof, current)
    assert relevant_failures(current['failures'], matched(proof, current)) == [current['failures'][1]]


def test_waiting_repair_never_dispatches_another_model_on_restart(job, scene):
    from auto_agents.repair_v2.types import AgentReply
    from test_repair_v2_controller import controller, Driver
    project, source, payload, proposal = scene
    driver = Driver()
    def waiting(role, *args, **kwargs):
        driver.calls.append((role, ''))
        assert role == 'plan'
        return AgentReply(True, 'REPAIR_SCOPE v1: ' + json.dumps({'decision': 'needs_user',
            'question': '完成当前任务需要增加一项功能。', 'suggestion': '仅增加视频恢复能力。'}))
    driver.run = waiting
    runner = controller(job, driver)
    runner.scope = ScopeGuard(job[1].root, payload, project, source)
    first = deepcopy(runner.run())
    assert first['status'] == 'waiting_user' and first['attempts'] == 0
    assert runner.run() == first
    assert driver.calls == [('plan', '')]


def test_old_accepted_candidate_only_supplements_scope_without_reimplementation(job, scene):
    from test_repair_v2_controller import controller, Driver
    project, source, payload, proposal = scene
    original = controller(job)
    saved = deepcopy(original.run())
    driver = original.driver
    run = driver.run
    def scoped(role, prompt, root, **kwargs):
        result = run(role, prompt, root, **kwargs)
        if role == 'plan':
            result.text += '\nREPAIR_SCOPE v1: ' + json.dumps({**proposal, 'evidence_refs': ['source.py']})
        if role == 'review':
            value = json.loads(result.text)
            value['change_coverage'] = [{'change': key, 'requirement': 'value', 'reason': 'restore the blocked step',
                                        'evidence': 'test_value'} for key in changes(root, job[0].engine_base)]
            result.text = json.dumps(value)
        return result
    driver.run = scoped
    resumed = controller(job, driver)
    resumed.scope = ScopeGuard(job[1].root, payload, project, job[2].source)
    result = resumed.run()
    assert result['status'] == 'ready'
    assert result['attempts'] == saved['attempts']
    assert result['plan'] == saved['plan']
    assert [role for role, _ in driver.calls].count('implement') == 1
    assert job[1].read(result['receipt'])['scope']


def test_approval_continues_same_cli_with_inherited_lock_and_unchanged_original_goal(scene, monkeypatch):
    import os
    from auto_agents.scope_decisions import resume_original
    from auto_agents.run_lock import ProjectRunLock
    from auto_agents.config import load_session_state
    project, source, payload, proposal = scene
    decisions = Decisions(project)
    question = decisions.create(context(project, payload), {'question': '需要增加一项能力。', 'suggestion': '只增加恢复能力。'})
    decisions.answer(question['id'], 1, 'approve')
    observed = []
    def main(argv):
        observed.append(argv)
        os.fstat(int(os.environ['AUTO_AGENTS_RUN_LOCK_FD']))
        state = load_session_state(project, 'root')
        assert state.goal == 'Produce the existing real video'
        assert state.status == 'executing'
        assert any(row.get('scope_decision') == question['id'] for row in state.conversation)
        return 0
    monkeypatch.setattr('auto_agents.cli.main', main)
    old = os.environ.get('AUTO_AGENTS_RUN_LOCK_FD')
    with ProjectRunLock(project, environ={}) as lock:
        assert resume_original(SimpleNamespace(), project, question['id'],
            SimpleNamespace(provider='codex', auto_approve=True), lock) == 0
    assert observed == [['collab', '--project', str(project), '--session', 'root', '--provider', 'codex', '--auto-approve']]
    assert os.environ.get('AUTO_AGENTS_RUN_LOCK_FD') == old


def test_unrelated_inherited_changes_are_preserved_not_assigned_to_repair(tmp_path):
    from auto_agents.repair_v2.workspace import git
    repo = tmp_path / 'repo'; repo.mkdir(); git(repo, 'init', '-q')
    (repo / 'engine.py').write_text('value = 0\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'unrelated.py').write_text('independent = True\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'independent upstream')
    parent = git(repo, 'rev-parse', 'HEAD')
    (repo / 'engine.py').write_text('value = 1\n')
    actual = changes(repo, base, [parent])
    assert len(actual) == 1 and 'engine.py' in next(iter(actual.values()))
    assert (repo / 'unrelated.py').read_text() == 'independent = True\n'


def test_pending_answer_checks_actual_current_goal_not_just_client_version(scene):
    project, source, payload, proposal = scene
    decisions = Decisions(project)
    question = decisions.create(context(project, payload), {'question': '调整目标？', 'suggestion': '增加一项能力。'})
    path = project / '.auto-agents/state/sessions/root/session_state.json'
    state = json.loads(path.read_text()); state['goal'] = 'A different user goal'; atomic_json(path, state)
    with pytest.raises(ValueError, match='任务目标已变化'):
        decisions.answer(question['id'], 1, 'approve')
    assert decisions.read(question['id'])['status'] == 'pending'


def test_run_choice_uses_existing_clarification_and_keeps_original_goal(tmp_path):
    from auto_agents.config import save_run_state, load_run_state
    from auto_agents.models import RunState, TaskSpec
    from auto_agents.scope_decisions import resume_run_choice, apply_run_choice
    state = RunState('run-one', tasks=[TaskSpec(task_id='task-one', title='Generate video', description='Existing video',
                                               acceptance=['video plays'])])
    save_run_state(tmp_path, state)
    payload = {'project': str(tmp_path), 'invocation': {'run_id': 'run-one'}, 'error': 'blocked'}
    ctx = context(tmp_path, payload)
    decisions = Decisions(tmp_path)
    question = decisions.create(ctx, {'question': '需要增加恢复能力。', 'suggestion': '只增加视频恢复。'})
    def rewind(state, stage):
        state.current_stage, state.status, state.tasks = stage, 'pending', []
    orch = SimpleNamespace(project_root=tmp_path, _prompt_user=lambda *a, **k: '1', _rewind_state_from_stage=rewind)
    assert resume_run_choice(orch) is None
    updated = load_run_state(tmp_path)
    assert updated.current_stage == 'clarify' and updated.rejected_stage == 'clarify'
    assert question['id'] in updated.resume_context['scope_decisions']
    assert context(tmp_path, payload)['original_goal'] == ctx['original_goal']
    orch._rewind_state_from_stage = lambda *a: pytest.fail('rewound twice')
    apply_run_choice(orch, question['id'])


@pytest.mark.parametrize('volatile', [False, True])
def test_real_pytest_baseline_comparison_preserves_old_failure_and_required_success(tmp_path, volatile):
    import shlex
    import subprocess
    import sys
    from uuid import uuid4
    from auto_agents.repair_v2.comparison import compare
    from auto_agents.repair_v2.types import ValidationUnit
    from auto_agents.repair_v2.workspace import git, source_identity
    repo = tmp_path / 'repo'; repo.mkdir(); git(repo, 'init', '-q')
    (repo / 'tests').mkdir()
    (repo / 'toy_source.py').write_text('old = 0\nrequired = 0\n')
    (repo / 'tests/test_toy.py').write_text(
        'from toy_source import old, required\ndef test_old(): assert old == 1\ndef test_required(): assert required == 1\n')
    if volatile:
        (repo / 'tests/test_toy.py').write_text(
            'from toy_source import old, required\nfrom tempfile import TemporaryDirectory\n'
            'def test_old():\n'
            '    with TemporaryDirectory(prefix="auto-agents-session-replay-", dir="/tmp") as root:\n'
            '        result = {"old": old, "error": "Diagnostics: " + root + "/target/.auto-agents/state/sessions/child/logs/diagnostics.json"}\n'
            '        assert result.get("old") == 1, result\n'
            'def test_required(): assert required == 1\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'toy_source.py').write_text('old = 0\nrequired = 1\n')
    script = '''import sys, json
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import pytest
from auto_agents.repair_v2.pytest_driver import Evidence
evidence = Evidence()
code = int(pytest.main(json.loads(sys.argv[2]) + ['-p', 'no:cacheprovider'], plugins=[evidence]))
Path(sys.argv[3]).write_text(json.dumps({**vars(evidence), 'returncode': code}))
'''
    class LocalVerifier:
        runtime = 'local-pytest'
        root = tmp_path / 'verification'
        def validate(self, identity, source, units, cancel, **kwargs):
            checks, failures = [], []
            for unit in units:
                report_path = self.root / (uuid4().hex + '.json')
                args = shlex.split(unit.command)[3:]
                subprocess.run([sys.executable, '-c', script, str(Path(__file__).resolve().parents[1] / 'src'),
                                json.dumps(args), str(report_path)], cwd=source, capture_output=True, check=True, timeout=30)
                observed = json.loads(report_path.read_text())
                check = {**observed, 'ok': observed['returncode'] == 0, 'unit': unit.identity, 'command': unit.command,
                         'missing': [], 'source_unchanged': source_identity(source) == identity}
                checks.append(check)
                if not check['ok']:
                    failures.append({'unit': unit.identity, 'failed': observed['failed']})
            return ValidationResult(not failures, identity, checks, failures)
    verifier = LocalVerifier(); verifier.root.mkdir()
    identity = source_identity(repo)
    current = verifier.validate(identity, repo, [ValidationUnit('suite:toy', 'python -m pytest -q tests')], None)
    assert not current.ok
    assert 'tests/test_toy.py::test_required' in current.checks[0]['passed']
    compared = compare(verifier, identity, repo, repo, base, current, None)
    assert compared['ok'] and verify(compared, asdict(current), base=base)
    assert current.failures[0]['failed'] == ['tests/test_toy.py::test_old']


def test_diagnostic_scope_retains_temporary_json_evidence_after_restart(scene, tmp_path):
    from auto_agents.root_cause import RootCauseDiagnosis, RootCauseReport
    project, source, payload, proposal = scene
    evidence = project / '.auto-agents/runs/run/root-cause/diagnosis/evidence.json'
    atomic_json(evidence, {'attempt_timeline': [{'outcome': 'health_quiesce'}]})
    (project / 'spec.md').write_text('the original goal')
    original_refs = ['target:spec.md:5', 'source:engine.py:1:2',
                     '.root-cause-evidence.json#/attempt_timeline/0']
    from test_root_cause import _report
    report = RootCauseReport.from_dict({**_report(role='investigator', verdict='ROOT_CAUSE'),
        'necessity': {**proposal, 'evidence_refs': original_refs}}, role='investigator')
    reviewer = RootCauseReport.from_dict(_report(role='reviewer', verdict='AGREE'), role='reviewer')
    diagnosis = RootCauseDiagnosis('diagnosis', str(evidence), report, reviewer, report, None, True, '')
    # Deserialization also covers cached diagnoses with the old citation format.
    diagnosis = RootCauseDiagnosis.from_dict(diagnosis.to_dict())
    guard = ScopeGuard(tmp_path / 'scope', payload, project, source)
    ref = guard.admit(diagnosis.scope_necessity(project))
    assert diagnosis.final.necessity['evidence_refs'] == original_refs
    saved = guard.store.read(ref)
    assert [row['origin'] for row in saved['witnesses']] == ['target', 'source', 'target']
    assert saved['witnesses'][-1]['pointer'] == '/attempt_timeline/0'
    restarted = ScopeGuard(tmp_path / 'scope', payload, project, source)
    assert restarted.current() == ref
    from auto_agents.root_cause import RootCauseCoordinator
    frozen = tmp_path / 'frozen'
    RootCauseCoordinator._copy_diagnostic_tree(project, frozen)
    assert not (frozen / evidence.relative_to(project)).exists()
    worker = ScopeGuard(tmp_path / 'worker', payload, frozen, source)
    assert worker.import_receipt(saved)
    retained = frozen / saved['witnesses'][-1]['path']
    atomic_json(retained, {'attempt_timeline': [{'outcome': 'different failure'}]})
    assert worker.current() is None


@pytest.mark.parametrize('ref', ['target:../private', 'source:/etc/passwd',
                                'target:.env', 'target:.auto-agents/operator/policy.json',
                                'source:spec.md'])
def test_qualified_evidence_keeps_path_and_origin_checks(scene, ref):
    from auto_agents.repair_v2.scope import witnesses
    project, source, _, _ = scene
    (project / 'spec.md').write_text('only exists in target')
    with pytest.raises(RepairBlocked):
        witnesses([ref], project, source)


@pytest.mark.parametrize('pointer', ['/missing', '/items/-1', '/items/01', '/items/2',
                                    '/items/0/value/missing', '/bad~2escape'])
def test_diagnostic_json_pointer_rejects_invalid_or_missing_values(scene, pointer):
    from auto_agents.repair_v2.scope import witnesses
    project, source, _, _ = scene
    atomic_json(project / 'evidence.json', {'items': [{'value': 1}]})
    with pytest.raises(RepairBlocked):
        witnesses([{'origin': 'target', 'path': 'evidence.json', 'pointer': pointer}], project, source)


@pytest.fixture
def diagnostic_scope(scene):
    """Synthetic evidence with the retained incident's identities and citation shape."""
    from auto_agents.root_cause import RootCauseDiagnosis, RootCauseReport
    from test_root_cause import _report

    project, source, _, proposal = scene
    run, workflow = '82288622684f', 'wf-cea9506a7499'
    task = {'task_id': 'task-pp-07', 'title': 'Planning provider safety', 'status': 'blocked',
            'commit_sha': '', 'acceptance': ['Verify, review, publish and integrate'],
            'verification_refs': [
                'tests/test_text_protocol_boundary_api.py::TextProtocolBoundaryTests::'
                'test_req_284_planning_progress_wire_is_positive_typed_and_deduplicated',
                'tests/test_text_protocol_boundary_api.py::TextProtocolBoundaryTests::'
                'test_req_284_planning_safety_body_precedes_http_without_unchanged_retry']}
    atomic_json(project / '.auto-agents/state/run_state.json', {
        'run_id': run, 'current_stage': 'implement', 'status': 'blocked',
        'goal': 'Synthetic planning recovery goal', 'last_error': 'publication failed on ignored .conda',
        'resume_context': {'workflow_id': workflow}, 'tasks': [task]})
    atomic_json(project / '.auto-agents/state/task_plan.json', {'tasks': [task]})
    atomic_json(project / f'.auto-agents/state/workflows/{workflow}/workflow.json', {
        'workflow_id': workflow, 'status': 'blocked',
        'root': {'kind': 'run', 'native_id': run},
        'active_frame': {'kind': 'run', 'native_id': run}})
    (project / 'spec.md').write_text('Synthetic planning recovery goal')
    (project / 'unrelated.txt').write_text('unrelated unfinished work')
    payload = {'project': str(project), 'invocation': {'run_id': run, 'workflow_id': workflow}}
    evidence = project / f'.auto-agents/runs/{run}/root-cause/diagnosis/evidence.json'
    contents = {'repair_case': {'run_id': run, 'stage': 'implement', 'synthetic': True},
                'traceback': ['synthetic admission traceback'], 'run_log': ['publication still pending'],
                'items': [{'a/b': {'~key': 1}}]}
    atomic_json(evidence, contents)
    report = RootCauseReport.from_dict({**_report(role='investigator', verdict='ROOT_CAUSE'),
        'necessity': {**proposal, 'blocked_step': 'Publish task-pp-07',
            'consequence': 'Publication repair cannot be submitted',
            'recovery_check': 'Submit the existing repair; publication and integration remain required',
            'evidence_refs': ['target:spec.md:5', 'source:engine.py:1:2',
                {'origin': 'source', 'path': '.root-cause-evidence.json#/repair_case'}]}},
        role='investigator')
    reviewer = RootCauseReport.from_dict(_report(role='reviewer', verdict='AGREE'), role='reviewer')
    diagnosis = RootCauseDiagnosis('diagnosis', str(evidence), report, reviewer, report, None, True, '')
    diagnosis = RootCauseDiagnosis.from_dict(json.loads(json.dumps(diagnosis.to_dict())))
    return project, source, payload, diagnosis, evidence, contents


def test_structured_diagnostic_citations_survive_scope_admission_and_restart(diagnostic_scope, tmp_path):
    from auto_agents.root_cause import RootCauseCoordinator, RootCauseDiagnosis
    project, source, payload, diagnosis, evidence, contents = diagnostic_scope
    refs = diagnosis.final.necessity['evidence_refs']
    refs.extend([
        '.root-cause-evidence.json#/traceback',
        'source:.root-cause-evidence.json#/run_log',
        'target:.root-cause-evidence.json#/repair_case',
        {'origin': 'target', 'path': '.root-cause-evidence.json', 'pointer': '/repair_case',
         'sha256': digest(contents['repair_case']), 'snapshot': digest(contents['repair_case'])},
        {'origin': 'source', 'path': '.root-cause-evidence.json#/items/0/a~1b/~0key',
         'pointer': '/items/0/a~1b/~0key', 'sha256': digest(1)},
    ])
    diagnosis = RootCauseDiagnosis.from_dict(json.loads(json.dumps(diagnosis.to_dict())))
    original = deepcopy(diagnosis.to_dict())
    # Neither a source nor a target snapshot alias may substitute foreign content.
    for root in (project, source):
        atomic_json(root / '.root-cause-evidence.json', {'repair_case': 'foreign evidence'})
    guard = ScopeGuard(tmp_path / 'scope', payload, project, source)
    bound = diagnosis.scope_necessity(project)
    ref = guard.admit(bound)
    saved = guard.store.read(ref)
    assert diagnosis.to_dict() == original
    assert bound['evidence_refs'][:2] == refs[:2]
    assert [row['origin'] for row in saved['witnesses']] == ['target', 'source'] + ['target'] * 6
    assert [row['pointer'] for row in saved['witnesses'][2:]] == [
        '/repair_case', '/traceback', '/run_log', '/repair_case', '/repair_case', '/items/0/a~1b/~0key']
    assert saved['witnesses'][2]['sha256'] == digest(contents['repair_case'])
    assert bound['evidence_refs'][-2]['sha256'] == digest(contents['repair_case'])
    assert bound['evidence_refs'][-2]['snapshot'] == digest(contents['repair_case'])
    retained = project / saved['witnesses'][2]['path']
    assert json.loads(retained.read_text()) == contents
    assert retained.name == digest(contents) + '.json'
    assert len(list(retained.parent.glob('*.json'))) == 1
    before = retained.stat().st_mtime_ns
    assert diagnosis.scope_necessity(project) == bound
    assert retained.stat().st_mtime_ns == before
    for root in (project, source):
        (root / '.root-cause-evidence.json').unlink()
    frozen = tmp_path / 'frozen'
    RootCauseCoordinator._copy_diagnostic_tree(project, frozen)
    assert not (frozen / evidence.relative_to(project)).exists()
    evidence.unlink()
    restarted = ScopeGuard(tmp_path / 'scope', payload, project, source)
    assert restarted.current() == ref
    assert restarted.admit(None) == ref
    worker = ScopeGuard(tmp_path / 'worker', payload, frozen, source)
    imported = worker.import_receipt(saved)
    assert imported is not None
    assert worker.current() == imported
    assert worker.admit(None) == imported
    assert worker.store.read(imported) == saved
    assert diagnosis.to_dict() == original
    atomic_json(frozen / saved['witnesses'][2]['path'], {**contents, 'repair_case': {'run_id': 'foreign'}})
    assert worker.current() is None
    assert worker.import_receipt(saved) is None


@pytest.mark.parametrize('ref', [
    '.root-cause-evidence.json#/repair_case',
    'source:.root-cause-evidence.json#/repair_case',
    'target:.root-cause-evidence.json#/repair_case',
    {'origin': 'source', 'path': '.root-cause-evidence.json#/repair_case'},
    {'origin': 'target', 'path': '.root-cause-evidence.json#/repair_case'},
    {'origin': 'source', 'path': '.root-cause-evidence.json', 'pointer': '/repair_case'},
    {'origin': 'target', 'path': '.root-cause-evidence.json#/repair_case', 'pointer': '/repair_case'},
    '.root-cause-evidence.json',
    'source:.root-cause-evidence.json',
    {'origin': 'target', 'path': '.root-cause-evidence.json', 'pointer': ''},
])
def test_diagnostic_scope_binds_object_and_qualified_references(diagnostic_scope, tmp_path, ref):
    from auto_agents.root_cause import RootCauseDiagnosis
    project, source, payload, diagnosis, _, contents = diagnostic_scope
    diagnosis.final.necessity['evidence_refs'] = [deepcopy(ref)]
    diagnosis = RootCauseDiagnosis.from_dict(json.loads(json.dumps(diagnosis.to_dict())))
    original = deepcopy(diagnosis.to_dict())
    guard = ScopeGuard(tmp_path / 'scope', payload, project, source)
    receipt = guard.store.read(guard.admit(diagnosis.scope_necessity(project)))
    witness, = receipt['witnesses']
    assert witness['origin'] == 'target'
    assert witness['path'] == f'.auto-agents/state/repair-scope-evidence/{digest(contents)}.json'
    assert witness['pointer'] == ('/repair_case' if '/repair_case' in str(ref) else '')
    assert diagnosis.to_dict() == original


@pytest.mark.parametrize('ref', [
    {'origin': 'foreign', 'path': '.root-cause-evidence.json#/repair_case'},
    {'origin': [], 'path': '.root-cause-evidence.json#/repair_case'},
    {'path': '.root-cause-evidence.json#/repair_case'},
    'foreign:.root-cause-evidence.json#/repair_case',
    {'origin': 'source', 'path': '.root-cause-evidence.json#/repair_case', 'pointer': '/run_log'},
    {'origin': 'target', 'path': '.root-cause-evidence.json#/repair_case', 'pointer': ''},
    {'origin': 'source', 'path': '.root-cause-evidence.json', 'pointer': None},
    {'origin': 'target', 'path': '.root-cause-evidence.json#/repair_case', 'sha256': '0' * 64},
    {'origin': 'source', 'path': '.root-cause-evidence.json#/repair_case', 'snapshot': '0' * 64},
    {'origin': 'source', 'path': '.root-cause-evidence.json#/repair_case', 'sha256': 'invalid'},
    {'origin': 'target', 'path': '.root-cause-evidence.json#/repair_case', 'snapshot': None},
    'source:.root-cause-evidence.json#repair_case',
    'target:.root-cause-evidence.json#/missing',
    'source:.root-cause-evidence.json#/items/-1',
    'target:.root-cause-evidence.json#/items/01',
    'source:.root-cause-evidence.json#/items/2',
    'target:.root-cause-evidence.json#/bad~2escape',
    'source:.root-cause-evidence.json#/items/0/a~1b/~0key/missing',
    {'origin': 'target', 'path': '../.root-cause-evidence.json#/repair_case'},
    {'origin': 'source', 'path': '/tmp/.root-cause-evidence.json#/repair_case'},
])
def test_diagnostic_scope_rejects_invalid_reference_constraints(diagnostic_scope, tmp_path, ref):
    project, source, payload, diagnosis, _, _ = diagnostic_scope
    diagnosis.final.necessity['evidence_refs'] = [deepcopy(ref)]
    original = deepcopy(diagnosis.to_dict())
    guard = ScopeGuard(tmp_path / 'scope', payload, project, source)
    with pytest.raises(RepairBlocked) as failure:
        guard.admit(diagnosis.scope_necessity(project))
    assert failure.value.code == 'scope_evidence'
    assert guard.current() is None
    assert diagnosis.to_dict() == original


@pytest.mark.parametrize('location', ['foreign', 'symlink', 'retained_symlink', 'retained_tamper'])
def test_diagnostic_scope_rejects_foreign_or_changed_retention(diagnostic_scope, tmp_path, location):
    project, _, _, diagnosis, evidence, contents = diagnostic_scope
    outside = tmp_path / 'foreign.json'
    atomic_json(outside, contents)
    retained = project / '.auto-agents/state/repair-scope-evidence' / (digest(contents) + '.json')
    if location == 'foreign':
        diagnosis.evidence_path = str(outside)
    elif location == 'symlink':
        evidence.unlink()
        evidence.symlink_to(outside)
    elif location == 'retained_symlink':
        retained.parent.mkdir(parents=True)
        retained.symlink_to(outside)
    else:
        atomic_json(retained, {'repair_case': 'changed'})
    before = outside.read_bytes()
    with pytest.raises(RepairBlocked) as failure:
        diagnosis.scope_necessity(project)
    assert failure.value.code == 'scope_evidence'
    assert outside.read_bytes() == before
    if location == 'retained_tamper':
        assert json.loads(retained.read_text()) == {'repair_case': 'changed'}


@pytest.mark.parametrize('valid', [True, False])
def test_diagnostic_scope_submission_preserves_bound_run(diagnostic_scope, tmp_path, monkeypatch, valid):
    from auto_agents import repair_client
    from auto_agents.self_repair import SelfRepairDecision
    project, source, payload, diagnosis, evidence, _ = diagnostic_scope
    if not valid:
        diagnosis.final.necessity['evidence_refs'][-1]['pointer'] = '/conflicting'
    original = deepcopy(diagnosis.to_dict())
    protected = [project / name for name in (
        '.auto-agents/state/run_state.json', '.auto-agents/state/task_plan.json',
        '.auto-agents/state/workflows/wf-cea9506a7499/workflow.json', 'unrelated.txt')]
    before = {path: path.read_bytes() for path in protected}
    registration = {'config': {'root': str(tmp_path / 'control')}, 'subscriber': 'original'}
    autonomy = SimpleNamespace(mode='max', to_dict=lambda: {'mode': 'max'})
    orchestrator = SimpleNamespace(_repair_registration=registration,
        _invocation_context=deepcopy(payload['invocation']), record_run_blocker=lambda **kwargs: None,
        config=SimpleNamespace(active_provider='codex', execution=SimpleNamespace(autonomy=autonomy)))
    argv = ['python', 'auto_agents.py', 'run', '--project', str(project)]
    monkeypatch.setattr('auto_agents.cli._run_command_for_self_repair_resume', lambda args: argv)
    monkeypatch.setattr('auto_agents.self_repair.auto_agents_repo_root', lambda: source)
    monkeypatch.setattr('auto_agents.process_supervision.ACTIVE_PROCESSES.terminate_all', lambda: None)
    monkeypatch.setattr('auto_agents.process_supervision.ACTIVE_PROCESSES.snapshot', lambda: [])
    monkeypatch.setattr(repair_client, 'git', lambda *args: 'engine-base')
    submitted = []

    class Submitted(BaseException):
        pass

    def transport(config, request):
        assert request['op'] == 'submit'
        submitted.append(request['payload'])
        raise Submitted()

    monkeypatch.setattr(repair_client, 'rpc', transport)
    def submit():
        repair_client.submit_and_wait(project, orchestrator, RuntimeError('publication failed on ignored .conda'),
            SelfRepairDecision(True, category='publication', fingerprint='original'),
            SimpleNamespace(command='run', provider='codex', autonomy=None), SimpleNamespace(), diagnosis=diagnosis)

    with pytest.raises(Submitted if valid else RepairBlocked):
        submit()
    assert {path: path.read_bytes() for path in protected} == before
    assert diagnosis.to_dict() == original
    if valid:
        request, = submitted
        assert request['invocation'] == payload['invocation']
        assert request['resume_argv'] == argv
        assert request['diagnosis'] == original
        assert request['boundary']['stage'] == 'implement'
        assert request['scope_receipt']['context']['owner']['subject'] == 'run:82288622684f'
        assert request['scope_receipt']['context']['owner']['workflow_id'] == 'wf-cea9506a7499'
        assert request['scope_receipt']['context']['original_goal'] == 'Synthetic planning recovery goal'
        worker = ScopeGuard(tmp_path / 'worker', request, project, source)
        assert worker.import_receipt(request['scope_receipt']) is not None
        # A restarted submitter must reuse its validated receipt before trying
        # to bind citations from diagnostic files that no longer exist.
        evidence.unlink()
        with pytest.raises(Submitted):
            submit()
        assert len(submitted) == 2
        assert submitted[1]['scope_receipt'] == request['scope_receipt']
        assert submitted[1]['invocation'] == request['invocation']
        retained = project / request['scope_receipt']['witnesses'][-1]['path']
        atomic_json(retained, {'repair_case': 'changed'})
        with pytest.raises(RepairBlocked) as failure:
            submit()
        assert failure.value.code == 'scope_evidence'
        assert len(submitted) == 2
        assert {path: path.read_bytes() for path in protected} == before
        assert diagnosis.to_dict() == original
    else:
        assert submitted == []
