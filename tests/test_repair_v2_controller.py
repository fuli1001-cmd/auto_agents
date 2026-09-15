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
