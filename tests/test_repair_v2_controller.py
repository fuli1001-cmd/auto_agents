from pathlib import Path
import subprocess
import threading

import pytest

from auto_agents.repair_v2 import Acceptance, AgentReply, Controller, RepairRequest, ValidationResult, ValidationUnit
from auto_agents.repair_v2.controller import review_result
from auto_agents.repair_v2.store import Store
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.repair_v2.workspace import Workspace, git, source_identity


@pytest.fixture
def job(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'source.py').write_text('value = 0\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'baseline')
    request = RepairRequest('job', git(repo, 'rev-parse', 'HEAD'), 'restore the value',
        (Acceptance('value', 'value must be 1'),), 'fake')
    store = Store(tmp_path / 'state')
    workspace = Workspace(tmp_path / 'work', repo, request.engine_base)
    return request, store, workspace


class Driver:
    def __init__(self, *, fail=False): self.calls, self.fail = [], fail
    def run(self, role, prompt, root, *, session, schema, progress, cancel):
        self.calls.append((role, session))
        progress({'session': 'writer' if role != 'review' else 'reviewer'})
        if role == 'plan': return AgentReply(True, 'Set value to 1. Cover requirement value.', 'writer')
        if role == 'implement':
            (Path(root) / 'source.py').write_text('value = ' + ('0' if self.fail else '1') + '\n')
            return AgentReply(True, 'implemented', 'writer')
        return AgentReply(True, '{"decision":"APPROVE","findings":[],"coverage":[{"requirement":"value",'
                          '"nodes":["tests/test_value.py::test_value"]}]}', 'reviewer')


class Verifier:
    def __init__(self): self.calls = []
    def prepare(self): pass
    def validate(self, identity, root, units, cancel):
        self.calls.append(identity)
        ok = (Path(root) / 'source.py').read_text() == 'value = 1\n'
        return ValidationResult(ok, identity, checks=[{'passed': ['tests/test_value.py::test_value']}],
                                failures=[] if ok else [{'unit': 'value', 'reason': 'value is not 1'}])


def controller(job, driver=None, verifier=None):
    request, store, workspace = job
    return Controller(request, store, workspace, driver or Driver(), verifier or Verifier(),
                      units=lambda _: [ValidationUnit('value', 'python -m pytest -q tests')])


def test_success_has_three_calls_one_candidate_and_no_group_state(job):
    runner = controller(job)
    state = runner.run()
    assert state['status'] == 'ready' and state['attempts'] == 1 and state['calls'] == 3
    assert runner.driver.calls == [('plan', ''), ('implement', 'writer'), ('review', '')]
    assert runner.store.read(state['receipt'])['snapshot'] == state['snapshot']
    assert (runner.workspace.source / 'source.py').read_text() == 'value = 0\n'
    assert runner.run() == state and len(runner.driver.calls) == 3
    assert 'finding_groups' not in state


def test_bounded_rediagnosis_does_not_reset_on_restart(job):
    runner = controller(job, Driver(fail=True))
    state = runner.run()
    assert state['status'] == 'blocked' and state['blocker']['code'] == 'no_progress'
    assert state['attempts'] == 4 and state['replans'] == 1
    calls = len(runner.driver.calls)
    assert controller(job, runner.driver).run()['status'] == 'blocked'
    assert len(runner.driver.calls) == calls


def test_cancelled_implementation_keeps_plan_and_resumes_writer(job):
    driver = Driver(); original = driver.run
    def interrupt(role, *args, **kwargs):
        if role == 'implement': raise KeyboardInterrupt()
        return original(role, *args, **kwargs)
    driver.run = interrupt
    with pytest.raises(KeyboardInterrupt): controller(job, driver).run()
    assert job[1].load()['status'] == 'stopped' and job[1].load()['plan']
    driver.run = original
    assert controller(job, driver).run()['status'] == 'ready'
    assert [r for r, _ in driver.calls].count('plan') == 1


def test_plan_cannot_modify_candidate(job):
    driver = Driver(); original = driver.run
    def mutate(role, prompt, root, **kwargs):
        result = original(role, prompt, root, **kwargs)
        if role == 'plan': (Path(root) / 'source.py').write_text('value = 2\n')
        return result
    driver.run = mutate
    state = controller(job, driver).run()
    assert state['blocker']['code'] == 'read_only_violation'


def test_tests_and_review_bind_the_same_frozen_source_and_run_concurrently(job):
    rendezvous = threading.Barrier(2)
    driver, verifier = Driver(), Verifier()
    original_driver, original_verifier = driver.run, verifier.validate
    observed = []
    def review(role, prompt, root, **kwargs):
        if role == 'review':
            observed.append(source_identity(root)); rendezvous.wait(timeout=5)
        return original_driver(role, prompt, root, **kwargs)
    def verify(identity, root, units, cancel):
        observed.append(identity); rendezvous.wait(timeout=5)
        return original_verifier(identity, root, units, cancel)
    driver.run, verifier.validate = review, verify
    result = controller(job, driver, verifier).run()
    assert observed == [result['snapshot'], result['snapshot']]


@pytest.mark.parametrize('text', ['{"decision":"REJECT","findings":[]}', '{"decision":"APPROVE"}',
    '{"decision":"APPROVE","findings":[{"requirement":"unknown"}]}', 'not a result'])
def test_malformed_review_cannot_authorize_delivery(text):
    with pytest.raises(RepairBlocked): review_result(text, 'snapshot', {'value'})


def test_corrupt_checkpoint_never_resumes(job):
    runner = controller(job); runner.run()
    path = runner.store.root / 'state.json'
    path.write_text(path.read_text().replace('"ready"', '"complete"'))
    with pytest.raises(ValueError, match='digest'): runner.run()


def test_missing_executed_coverage_is_saved_as_a_failed_validation(job):
    verifier = Verifier(); original = verifier.validate
    def no_coverage(*args):
        result = original(*args); result.checks = []; return result
    verifier.validate = no_coverage
    runner = controller(job, verifier=verifier)
    state = runner.run()
    saved = runner.store.read(state['validation'])
    assert state['status'] == 'blocked' and not saved['ok']
    assert saved['failures'][0]['missing'] == ['tests/test_value.py::test_value']


def test_saved_ready_source_is_checked_again_before_delivery(job):
    runner = controller(job); state = runner.run()
    (Path(state['snapshot_path']) / 'source.py').write_text('changed after review')
    with pytest.raises(RepairBlocked, match='no longer matches'):
        runner.run()


def test_review_format_correction_cannot_drop_blocking_findings(job):
    driver = Driver(); original = driver.run
    calls = 0
    def review(role, *args, **kwargs):
        nonlocal calls
        if role != 'review': return original(role, *args, **kwargs)
        calls += 1
        if calls == 1:
            return AgentReply(True, '{"decision":"REJECT","findings":[{"requirement":"value","reason":"wrong",'
                              '"counterexample":"value is zero","check":""}]}', 'reviewer')
        return original(role, *args, **kwargs)
    driver.run = review
    state = controller(job, driver).run()
    assert state['status'] == 'blocked' and state['blocker']['code'] == 'review_semantics_changed'


def test_source_context_never_claims_omitted_files_are_complete(job):
    import json
    from auto_agents.repair_v2.context import source_context
    request, _, workspace = job
    root = workspace.prepare()
    full = json.loads(source_context(root, request.engine_base))
    assert full['complete_repository_contents']
    (root / 'large.txt').write_text('x' * 40000)
    small = json.loads(source_context(root, request.engine_base))
    assert not small['complete_repository_contents'] and 'large.txt' not in small['files']


def test_failure_oscillation_cannot_keep_renewing_progress_budget(job):
    verifier = Verifier()
    count = 0
    def oscillate(identity, root, units, cancel):
        nonlocal count
        count += 1
        failures = [{'unit': 'check', 'failed': ['A', 'B'] if count % 2 else ['A'], 'reason': 'same unresolved defect'}]
        return ValidationResult(False, identity, failures=failures)
    verifier.validate = oscillate
    state = controller(job, verifier=verifier).run()
    assert state['status'] == 'blocked' and state['blocker']['code'] == 'no_progress'
    assert state['attempts'] == 6 and state['replans'] == 1


def test_failed_tests_cancel_slow_review_without_cancelling_the_repair(job):
    import time
    driver, verifier = Driver(), Verifier()
    original = driver.run
    def slow_review(role, *args, **kwargs):
        if role != 'review': return original(role, *args, **kwargs)
        deadline = time.monotonic() + 5
        while not kwargs['cancel'].is_set():
            if time.monotonic() > deadline: pytest.fail('review was not cancelled')
            time.sleep(0.01)
        return AgentReply(False, error='cancelled', interrupted=True)
    driver.run = slow_review
    verifier.validate = lambda identity, *args: ValidationResult(False, identity, failures=[{'unit': 'known', 'reason': 'failed'}])
    runner = controller(job, driver, verifier)
    state = runner.run()
    assert state['blocker']['code'] == 'no_progress' and state['attempts'] == 4
    assert not runner.cancel.is_set()


def test_guarded_mode_never_generates_or_modifies_candidate_code(job):
    request, store, workspace = job
    driver = Driver()
    runner = Controller(request, store, workspace, driver, Verifier(),
                        units=lambda _: [], allow_implementation=False)
    state = runner.run()
    assert state['blocker']['code'] == 'guarded_mode'
    assert all(role == 'review' for role, _ in driver.calls)
    assert (workspace.candidate / 'source.py').read_text() == 'value = 0\n'


def test_missing_native_session_recovers_once_without_discarding_plan(job):
    driver = Driver(); original = driver.run
    missing = False
    def run(role, *args, **kwargs):
        nonlocal missing
        if role == 'implement' and kwargs['session'] and not missing:
            missing = True
            return AgentReply(False, error='thread not found', missing_session=True)
        return original(role, *args, **kwargs)
    driver.run = run
    runner = controller(job, driver)
    result = runner.run()
    assert result['status'] == 'ready' and result['session_recoveries'] == {'implement': 1}
    assert [role for role, _ in driver.calls].count('plan') == 1
    assert result['attempts'] == 1
