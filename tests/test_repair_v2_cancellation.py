import json
from pathlib import Path
import sys
import threading
import time

import pytest

from auto_agents.repair_v2.docker import DockerVerifier, run
from auto_agents.repair_v2.types import AgentReply, ValidationResult, ValidationUnit
from auto_agents.repair_v2.workspace import git, source_identity
from auto_agents.repair_v2.store import digest
from test_repair_v2_controller import job, controller, Driver, Verifier


def test_cancel_before_launch_never_creates_a_process(tmp_path, monkeypatch):
    cancel = threading.Event(); cancel.set()
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: pytest.fail('cancelled process was launched'))
    observed = {}
    code, output = run(['unused'], cancel=cancel, observation=observed)
    assert code == 130 and 'cancelled' in output
    assert observed['started'] is False and observed['termination'] == 'cancelled'


def test_timeout_has_a_distinct_cause_from_controller_cancellation():
    observed = {}
    code, _ = run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.05, observation=observed)
    assert code == 130 and observed['started']
    assert observed['termination'] == 'timeout'


@pytest.mark.parametrize('state,exit_code,infrastructure', [({}, -15, False), ({'OOMKilled': True}, -9, True), ({}, 125, True)])
def test_cancellation_with_missing_container_does_not_mask_real_oom_or_launch_error(
        tmp_path, monkeypatch, state, exit_code, infrastructure):
    source = tmp_path / 'source'; source.mkdir(); git(source, 'init', '-q')
    (source / 'source.py').write_text('value = 1\n')
    identity = source_identity(source)
    base = tmp_path / 'execution'; output = base / 'result'; output.mkdir(parents=True)
    cancel = threading.Event()
    def observed_run(command, **kwargs):
        if command[1] == 'run':
            kwargs['observation'].update(started=True, termination='cancelled', exit_code=exit_code)
            cancel.set(); return 130, ''
        if command[1] == 'inspect': return (0, json.dumps(state)) if state else (1, 'No such object')
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', observed_run)
    verifier = DockerVerifier(tmp_path / 'verify', image='fixed')
    result = verifier._execute(identity, source, ValidationUnit('unit', 'true'), cancel, '', base / 'cache.json',
                               {'complete': False}, 'test', base, source, output, {'clear': True})
    assert result['cancelled'] and not result['ok']
    assert result['infrastructure'] is infrastructure and result['excerpt'].strip()
    if state.get('OOMKilled'): assert 'memory' in result['excerpt']


def test_cancelled_batches_and_real_infrastructure_use_one_failure_selection(tmp_path, monkeypatch):
    verifier = DockerVerifier(tmp_path / 'verify', image='fixed', workers=1)
    for infrastructure in (False, True):
        cancel = threading.Event()
        def execute(*args):
            cancel.set()
            return {'unit': 'test', 'command': 'true', 'ok': False, 'cancelled': True, 'returncode': 130,
                    'infrastructure': infrastructure, 'excerpt': 'out of memory' if infrastructure else '',
                    'failed': [], 'missing': []}
        monkeypatch.setattr(verifier, 'execute', execute)
        result = verifier.validate('source', tmp_path, [ValidationUnit('test', 'true')], cancel)
        assert result.cancelled and not result.ok
        assert result.infrastructure is infrastructure
        assert bool(result.failures) is infrastructure


def rejection():
    return json.dumps({'decision': 'REJECT', 'coverage': [], 'findings': [{
        'requirement': 'value', 'reason': 'required behavior is missing',
        'counterexample': 'a required input fails', 'check': 'exercise that input'}]})


def test_review_rejection_cancels_tests_then_returns_to_same_implementation(job):
    driver, verifier = Driver(), Verifier()
    original = driver.run
    rejected = False
    def review(role, *args, **kwargs):
        nonlocal rejected
        if role == 'review' and not rejected:
            rejected = True; driver.calls.append((role, kwargs['session']))
            return AgentReply(True, rejection(), 'reviewer')
        return original(role, *args, **kwargs)
    driver.run = review
    original_validate = verifier.validate
    calls = 0
    def validation(identity, root, units, cancel):
        nonlocal calls
        calls += 1
        if calls == 1:
            until = time.monotonic() + 5
            while not cancel.is_set():
                assert time.monotonic() < until
                time.sleep(.01)
            return ValidationResult(False, identity, cancelled=True)
        return original_validate(identity, root, units, cancel)
    verifier.validate = validation
    state = controller(job, driver, verifier).run()
    assert state['status'] == 'ready' and state['attempts'] == 2
    assert [role for role, _ in driver.calls].count('plan') == 1
    assert [session for role, session in driver.calls if role == 'implement'] == ['writer', 'writer']


def test_infrastructure_failure_never_displays_an_empty_array(job):
    verifier = Verifier()
    verifier.validate = lambda identity, *a: ValidationResult(False, identity, infrastructure=True)
    state = controller(job, verifier=verifier).run()
    assert state['blocker']['code'] == 'verification_infrastructure'
    assert state['blocker']['message'] != '[]'
    assert 'without structured diagnostics' in state['blocker']['message']


def test_legacy_cancelled_review_recovers_without_repeating_tests_review_or_plan(job):
    runner = controller(job); state = runner.run()
    original_plan, original_sessions = state['plan'], dict(state['sessions'])
    identity = state['snapshot']
    state.update(status='blocked', phase='validate', resume_token='old', attempts=2, stagnant=1,
                 blocker={'code': 'verification_infrastructure', 'message': '[]'},
                 failures=[{'failed': ['old-test']}], best_failure_keys=[['test', 'old-test']])
    state['validation'] = runner.store.artifact('validation', {'snapshot': identity, 'cancelled': True,
        'infrastructure': True, 'failures': [], 'checks': [
            {'ok': True, 'passed': ['old-test']}, {'ok': False, 'returncode': 130, 'infrastructure': True}]})
    state['review'] = runner.store.artifact('review', {'snapshot': identity, 'text': rejection()})
    state['review_input'] = digest([state['request_digest'], identity, state.get('verification_runtime'), state['failures']])
    runner.store.save(state)
    calls = list(runner.driver.calls)
    resumed = controller(job, runner.driver); resumed.resume_token = 'new'
    def before_writer(root): raise KeyboardInterrupt()
    resumed.implement = before_writer
    resumed.verifier.validate = lambda *a: pytest.fail('known rejected source was retested')
    with pytest.raises(KeyboardInterrupt): resumed.run()
    saved = runner.store.load()
    assert saved['phase'] == 'implement' and saved['stagnant'] == 0 and saved['attempts'] == 2
    assert saved['failures'][0]['requirement'] == 'value'
    assert saved['plan'] == original_plan and saved['sessions'] == original_sessions
    assert runner.driver.calls == calls


def test_verified_test_fixes_do_not_give_unlimited_credit_to_failure_oscillation(job):
    verifier = Verifier()
    calls = 0
    def alternate(identity, *args):
        nonlocal calls
        calls += 1
        failed, passed = ('A', 'B') if calls % 2 else ('B', 'A')
        return ValidationResult(False, identity,
            checks=[{'ok': True, 'passed': [passed, 'tests/test_value.py::test_value']}],
            failures=[{'unit': 'test', 'failed': [failed]}])
    verifier.validate = alternate
    state = controller(job, verifier=verifier).run()
    assert state['blocker']['code'] == 'no_progress'
    assert state['attempts'] == 7 and state['replans'] == 1
    assert {tuple(key) for key in state['resolved_failure_keys']} == {('test', 'A'), ('test', 'B')}
