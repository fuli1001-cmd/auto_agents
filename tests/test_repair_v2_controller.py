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


@pytest.mark.parametrize('weaken', [False, True])
def test_integration_with_lagging_upstream_protects_frozen_base_tests(job, weaken):
    from dataclasses import replace
    request, store, workspace = job
    repo = workspace.source
    (repo / 'tests').mkdir()
    test = repo / 'tests/test_value.py'
    test.write_text('def test_value():\n    assert 1 == 1\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'old upstream tests')
    upstream = git(repo, 'rev-parse', 'HEAD')
    test.write_text('def test_value():\n    assert 2 == 2\n')
    git(repo, 'commit', '-qam', 'test evolution before repair')
    base = git(repo, 'rev-parse', 'HEAD')
    runner = controller((replace(request, engine_base=base), store,
                         Workspace(workspace.root, repo, base)))
    accepted = runner.run()
    assert accepted['status'] == 'ready'
    calls = list(runner.driver.calls)
    runner.integrate(repo, [base, upstream])
    if weaken:
        (runner.workspace.candidate / 'tests/test_value.py').write_text('def test_value():\n    pass\n')
        assert not runner.audit(runner.workspace.candidate)
        assert {f['baseline'] for f in runner.state['failures']} == {base}
    else:
        assert runner.run()['status'] == 'ready'
        assert runner.driver.calls == calls
    assert runner.state['attempts'] == accepted['attempts']


@pytest.mark.parametrize('diverged', [False, True])
def test_integration_still_protects_new_upstream_tests_after_merge(job, diverged):
    runner = controller(job)
    assert runner.run()['status'] == 'ready'
    repo = runner.workspace.source
    (repo / 'tests').mkdir()
    (repo / 'tests/test_upstream.py').write_text('def test_upstream():\n    assert 3 == 3\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'new upstream regression')
    upstream = git(repo, 'rev-parse', 'HEAD')
    if diverged:
        git(repo, 'checkout', '--detach', runner.request.engine_base)
        (repo / 'local.txt').write_text('independent local work\n')
        git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'diverged local source')
    local = git(repo, 'rev-parse', 'HEAD')
    runner.integrate(repo, [local, upstream])
    root = runner.workspace.candidate
    (root / 'tests/test_upstream.py').write_text('def test_upstream():\n    pass\n')
    assert not runner.audit(root)
    assert any(f['baseline'] == upstream and f['path'] == 'tests/test_upstream.py'
               for f in runner.state['failures'])


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
    # A disappearing failure without an executed passing check is no progress.
    assert state['attempts'] == 4 and state['replans'] == 1


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


def protected_job(job):
    from dataclasses import replace
    request, store, workspace = job
    tests = workspace.source / 'tests'; tests.mkdir()
    (tests / 'test_value.py').write_text('def test_value():\n    assert value == 1\n')
    git(workspace.source, 'add', '.'); git(workspace.source, 'commit', '-qm', 'retain regression')
    request = replace(request, engine_base=git(workspace.source, 'rev-parse', 'HEAD'))
    workspace.base = request.engine_base
    return request, store, workspace


def test_test_audit_returns_all_failures_to_same_writer_before_formal_acceptance(job):
    job = protected_job(job)
    driver, verifier = Driver(), Verifier()
    original, turns = driver.run, []
    def run(role, prompt, root, **kwargs):
        if role == 'implement':
            turns.append(prompt)
            if len(turns) == 1:
                (Path(root) / 'tests/test_value.py').write_text('def test_value():\n    assert True\n')
            else:
                assert "value == 1" in prompt and 'test_value.py' in prompt
                (Path(root) / 'tests/test_value.py').write_text('def test_value():\n    assert value == 1\n')
        return original(role, prompt, root, **kwargs)
    driver.run = run
    state = controller(job, driver, verifier).run()
    assert state['status'] == 'ready' and state['attempts'] == 2
    assert driver.calls == [('plan', ''), ('implement', 'writer'), ('implement', 'writer'), ('review', '')]
    assert len(verifier.calls) == 1


def test_unchanged_test_audit_failure_consumes_persistent_no_progress_budget(job):
    job = protected_job(job)
    driver = Driver(); original = driver.run
    def run(role, prompt, root, **kwargs):
        if role == 'implement':
            (Path(root) / 'tests/test_value.py').write_text('def test_value():\n    assert True\n')
        return original(role, prompt, root, **kwargs)
    driver.run = run
    verifier = Verifier()
    state = controller(job, driver, verifier).run()
    assert state['blocker']['code'] == 'no_progress' and state['attempts'] == 4
    assert state['replans'] == 1 and verifier.calls == []
    calls = list(driver.calls)
    resumed = controller(job, driver, verifier); resumed.resume_token = 'new-job'
    assert resumed.run()['status'] == 'blocked' and driver.calls == calls


def test_imported_test_issue_is_visible_to_initial_plan_and_implementation(job):
    job = protected_job(job)
    job[2].prepare()
    (job[2].candidate / 'tests/test_value.py').write_text('def test_value():\n    assert True\n')
    driver = Driver(); original = driver.run
    def run(role, prompt, root, **kwargs):
        if role in ('plan', 'implement'):
            assert 'missing_assertions' in prompt and 'value == 1' in prompt
        if role == 'implement':
            (Path(root) / 'tests/test_value.py').write_text('def test_value():\n    assert value == 1\n')
        return original(role, prompt, root, **kwargs)
    driver.run = run
    assert controller(job, driver).run()['status'] == 'ready'


def test_completed_implementation_resumes_at_audit_after_interruption(job):
    runner = controller(job)
    def interrupt(root): raise KeyboardInterrupt()
    runner.audit = interrupt
    with pytest.raises(KeyboardInterrupt): runner.run()
    saved = job[1].load()
    assert saved['phase'] == 'audit' and saved['attempts'] == 1
    resumed = controller(job, runner.driver).run()
    assert resumed['status'] == 'ready'
    assert runner.driver.calls == [('plan', ''), ('implement', 'writer'), ('review', '')]


def test_old_terminal_audit_resumes_retained_source_without_repeating_agent_turn(job):
    job = protected_job(job)
    runner = controller(job)
    def old_audit(root):
        # Reproduce the state emitted before the audit checkpoint was added.
        runner.checkpoint(phase='implement', attempts=0)
        raise RepairBlocked('tests_weakened', 'old syntax-only audit rejected the candidate')
    runner.audit = old_audit
    state = runner.run()
    assert state['status'] == 'blocked' and state['calls'] == 2
    assert controller(job, runner.driver).run()['status'] == 'blocked'
    resumed = controller(job, runner.driver); resumed.resume_token = 'explicit-new-job:1'
    state = resumed.run()
    assert state['status'] == 'ready' and state['attempts'] == 1
    assert runner.driver.calls == [('plan', ''), ('implement', 'writer'), ('review', '')]
    assert resumed.run()['attempts'] == 1


def test_guarded_mode_does_not_run_weakened_tests(job):
    job = protected_job(job)
    job[2].prepare()
    (job[2].candidate / 'tests/test_value.py').write_text('def test_value():\n    assert True\n')
    runner = controller(job); runner.allow_implementation = False
    state = runner.run()
    assert state['blocker']['code'] == 'guarded_mode'
    assert runner.driver.calls == [] and runner.verifier.calls == []


def test_codex_output_fragments_do_not_each_write_durable_progress(job):
    import json
    driver = Driver(); original = driver.run
    def run(role, prompt, root, **kwargs):
        if role == 'plan':
            for event in ('item/commandExecution/outputDelta', 'item/agentMessage/delta', 'assistant.delta'):
                kwargs['progress']({'session': 'writer', 'event': event})
            kwargs['progress']({'session': 'writer', 'event': 'item/completed'})
        return original(role, prompt, root, **kwargs)
    driver.run = run
    runner = controller(job, driver)
    assert runner.run()['status'] == 'ready'
    events = [json.loads(line) for line in (runner.store.root / 'events.jsonl').read_text().splitlines()]
    progress = [e for e in events if e['kind'] == 'agent_progress']
    assert any(e.get('event') == 'item/completed' for e in progress)
    assert not any(e.get('event', '').lower().endswith('delta') for e in progress)


def test_exhausted_repair_accepts_committed_correction_without_more_implementation(job):
    runner = controller(job, Driver(fail=True))
    blocked = dict(runner.run())
    repo = runner.workspace.source
    (repo / 'source.py').write_text('value = 1\n')
    git(repo, 'commit', '-qam', 'Correct the retained failure')
    runner.resume_token = 'new-invocation'
    assert runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    recovered = runner.store.load()
    for field in ('attempts', 'calls', 'stagnant', 'replans', 'plan', 'sessions'):
        assert recovered[field] == blocked[field]
    assert recovered['phase'] == 'audit'
    calls = len(runner.driver.calls)
    final = runner.run()
    assert final['status'] == 'ready'
    assert runner.driver.calls[calls:] == [('review', 'reviewer')]
    assert final['attempts'] == blocked['attempts']
    assert runner.store.read(final['receipt'])['snapshot'] != blocked['snapshot']


def test_subscriber_counterexample_accepts_new_committed_correction_without_extra_retry(job):
    runner = controller(job, Driver(fail=True))
    runner.run()
    repo = runner.workspace.source
    (repo / 'source.py').write_text('value = 1\n')
    git(repo, 'commit', '-qam', 'First external correction')
    runner.resume_token = 'first-correction'
    assert runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    assert runner.run()['status'] == 'ready'
    # validate_subscriber revokes acceptance and retains the concrete live
    # counterexample as active/implement, even for an exhausted writer budget.
    runner.checkpoint(status='active', phase='implement', failures=[{
        'unit': 'subscriber-boundary', 'reason': 'relocated receipt was not consumed'}])
    before = runner.store.load()
    (repo / 'resume.py').write_text('relocated_receipt_supported = True\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'Correct the subscriber counterexample')
    correction = git(repo, 'rev-parse', 'HEAD')
    assert not runner.recover_corrected_source(repo, correction)  # Same invocation.
    runner.resume_token = 'subscriber-correction'
    assert runner.recover_corrected_source(repo, correction)
    recovered = runner.store.load()
    for field in ('attempts', 'calls', 'stagnant', 'replans', 'plan', 'sessions', 'failures'):
        assert recovered[field] == before[field]
    assert recovered['phase'] == 'audit'
    observed = []
    def boundary(identity, snapshot, cancel):
        observed.append(identity)
        return {'ok': (Path(snapshot) / 'resume.py').read_text() == 'relocated_receipt_supported = True\n',
                'snapshot': identity}
    runner.boundary = boundary
    calls = len(runner.driver.calls)
    final = runner.run()
    assert final['status'] == 'ready'
    assert observed == [final['snapshot']]
    assert runner.driver.calls[calls:] == [('review', 'reviewer')]
    assert final['attempts'] == before['attempts']


def test_exhausted_repair_ignores_same_invocation_and_source_identical_commit(job):
    runner = controller(job, Driver(fail=True))
    runner.resume_token = 'old-invocation'
    runner.run()
    blocked = runner.store.load()
    repo = runner.workspace.source
    git(repo, 'commit', '--allow-empty', '-qm', 'Metadata only')
    commit = git(repo, 'rev-parse', 'HEAD')
    assert not runner.recover_corrected_source(repo, commit)
    runner.resume_token = 'new-invocation'
    assert not runner.recover_corrected_source(repo, commit)
    unchanged = runner.store.load()
    assert unchanged.pop('correction_pending', None) is None
    assert unchanged == blocked
    calls = len(runner.driver.calls)
    assert runner.run()['status'] == 'blocked'
    assert len(runner.driver.calls) == calls


def test_failed_external_correction_cannot_buy_more_writer_calls(job):
    runner = controller(job, Driver(fail=True))
    blocked = dict(runner.run())
    repo = runner.workspace.source
    (repo / 'source.py').write_text('value = 2\n')
    git(repo, 'commit', '-qam', 'Insufficient correction')
    runner.resume_token = 'new-invocation'
    assert runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    calls = len(runner.driver.calls)
    final = runner.run()
    assert final['status'] == 'blocked' and final['blocker']['code'] == 'no_progress'
    assert final['blocker']['message'].startswith('Acceptance failed at value;')
    assert runner.driver.calls[calls:] == [('review', 'reviewer')]
    assert final['attempts'] == blocked['attempts'] and final['replans'] == blocked['replans']
    runner.resume_token = 'third-invocation'
    assert not runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    assert runner.run()['status'] == 'blocked'
    assert runner.driver.calls[calls:] == [('review', 'reviewer')]


def test_external_correction_preserves_merge_conflicts_without_spending_budget(job):
    runner = controller(job, Driver(fail=True))
    blocked = runner.run()
    (runner.workspace.candidate / 'source.py').write_text('value = 3\n')
    runner.workspace.checkpoint()
    repo = runner.workspace.source
    (repo / 'source.py').write_text('value = 1\n')
    git(repo, 'commit', '-qam', 'Conflicting correction')
    runner.resume_token = 'new-invocation'
    assert not runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    final = runner.store.load()
    assert final['status'] == 'blocked' and final['calls'] == blocked['calls']
    assert git(runner.workspace.candidate, 'diff', '--name-only', '--diff-filter=U') == 'source.py'
    runner.resume_token = 'third-invocation'
    assert not runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    assert '<<<<<<<' in (runner.workspace.candidate / 'source.py').read_text()


def test_external_correction_does_not_reopen_other_blockers(job):
    runner = controller(job)
    runner.run()
    runner.checkpoint(status='blocked', blocker={'code': 'read_only_violation', 'message': 'untrusted edit'})
    before = runner.store.load()
    runner.resume_token = 'new-invocation'
    assert not runner.recover_corrected_source('/does/not/exist', 'not-a-commit')
    assert runner.store.load() == before


def test_external_correction_recovers_crash_after_git_merge(job, monkeypatch):
    runner = controller(job, Driver(fail=True))
    blocked = dict(runner.run())
    repo = runner.workspace.source
    (repo / 'source.py').write_text('value = 1\n')
    git(repo, 'commit', '-qam', 'Correction surviving a controller crash')
    runner.resume_token = 'new-invocation'
    save = runner.store.save
    def crash(state):
        if state.get('external_correction'): raise OSError('simulated disk failure after merge')
        save(state)
    with monkeypatch.context() as m:
        m.setattr(runner.store, 'save', crash)
        with pytest.raises(OSError, match='disk failure'):
            runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    assert (runner.workspace.candidate / 'source.py').read_text() == 'value = 1\n'
    assert runner.store.load()['status'] == 'blocked'
    assert runner.store.load()['correction_pending']
    resumed = controller(job, runner.driver)
    resumed.resume_token = 'next-invocation'
    # The imported commit is local and durable, even if its origin disappears.
    assert resumed.recover_corrected_source('/missing/origin', 'unused')
    final = resumed.run()
    assert final['status'] == 'ready' and final['attempts'] == blocked['attempts']
    assert not final['correction_pending']


def test_corrected_source_still_requires_original_boundary_acceptance(job):
    runner = controller(job, Driver(fail=True))
    blocked = dict(runner.run())
    repo = runner.workspace.source
    (repo / 'source.py').write_text('value = 1\n')
    git(repo, 'commit', '-qam', 'Pass tests but not recovery boundary')
    runner.resume_token = 'new-invocation'
    assert runner.recover_corrected_source(repo, git(repo, 'rev-parse', 'HEAD'))
    observed = []
    def boundary(identity, source, cancel):
        observed.append(identity)
        return {'ok': False, 'snapshot': identity, 'observed': 'child recovery still fails'}
    runner.boundary = boundary
    assert runner.run()['status'] == 'blocked'
    assert observed and runner.state['failures'][0]['unit'] == 'original-boundary'
    assert runner.state['attempts'] == blocked['attempts']


def test_acceptance_runs_previous_counterexamples_first_without_omitting_tests(job):
    runner = controller(job)
    runner.state = {'failures': [
        {'failed': ['tests/test_last.py::test_crash[x]']},
        {'requirement': 'value', 'check': 'Recheck test_scope[blocking] after forwarding kwargs',
         'reason': 'Regression in tests/test_middle.py'},
    ]}
    units = [ValidationUnit('unrelated', 'python -m pytest -q tests/test_first.py',
                            expected_nodes=('tests/test_first.py::test_first',)),
             ValidationUnit('related', 'python -m pytest -q tests/test_middle.py',
                            expected_nodes=('tests/test_middle.py::test_other',)),
             ValidationUnit('counterexample', 'python -m pytest -q tests/test_middle.py',
                            expected_nodes=('tests/test_middle.py::test_scope[blocking]',)),
             ValidationUnit('failed', 'python -m pytest -q tests/test_last.py',
                            expected_nodes=('tests/test_last.py::test_crash[x]',)),
             ValidationUnit('required', 'python -m compileall src')]
    ordered = runner.prioritize_failures(units)
    assert [unit.identity for unit in ordered] == ['counterexample', 'failed', 'related', 'unrelated', 'required']
    assert set(ordered) == set(units) and len(ordered) == len(units)
    runner.state['failures'] = []
    assert runner.prioritize_failures(units) == units
