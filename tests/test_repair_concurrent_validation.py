"""Both verdicts are required, while review rejection cancels only verification."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import time

import pytest

from auto_agents.gates import run_commands
from auto_agents.repair_concurrent_validation import review_and_verify, verification_cancel
from auto_agents.self_repair import AutoAgentsSelfRepairRunner, _VerificationResult
from auto_agents.verification_pytest import Recorder
from test_repair_planning import setup
from test_repair_routing import git
from test_repair_verification_performance import runner, commands, passed


@pytest.mark.parametrize('quick_ok,review_ok,expanded_ok', [(False, True, True), (True, False, False), (True, True, False), (True, True, True)])
def test_candidate_entry_uses_quick_gate_then_one_concurrent_expansion(setup, monkeypatch, quick_ok, review_ok, expanded_ok):
    obj, _, plan, _ = setup
    obj._candidate_group.update(plan, planning_receipt='approved')
    expanded_started = threading.Event()
    review_started = threading.Event()
    calls = []
    def checks(worker, selected, root, *, parallel=False):
        calls.append('expanded' if parallel else 'quick')
        if parallel:
            expanded_started.set()
            assert review_started.wait(5)
        return _VerificationResult(expanded_ok if parallel else quick_ok, 'checks',
            returncodes=(0 if (expanded_ok if parallel else quick_ok) else 1,), payload={'source_commands': selected})
    def audit(*args, **kwargs):
        assert expanded_started.wait(5)
        review_started.set()
        return _VerificationResult(review_ok, 'audit')
    monkeypatch.setattr(AutoAgentsSelfRepairRunner, '_guarded_component_checks', checks)
    monkeypatch.setattr(obj, '_review_candidate', audit)
    result, review = obj._early_candidate_checks(obj.repo_root, obj._experiment.base_commit)
    assert calls == (['quick', 'expanded'] if quick_ok else ['quick'])
    assert result.ok == (quick_ok and expanded_ok)
    assert review.ok == (quick_ok and review_ok)
    assert bool(review.payload.get('early_review')) == quick_ok


@pytest.mark.parametrize('review_ok,tests_ok', [(True, True), (True, False), (False, True), (False, False)])
def test_both_stages_overlap_and_review_always_finishes(setup, monkeypatch, review_ok, tests_ok):
    obj, state, _, _ = setup
    started, reviewed = threading.Event(), threading.Event()
    roots = []
    def expanded(worker, root, *, record=True):
        assert worker is not obj and worker._experiment is not state and record is False
        roots.append(root)
        assert root != obj.repo_root
        worker._candidate_verification_plan = {'commands': ['check'], 'requests': []}
        worker._candidate_failure_evidence = [{'command': 'check', 'failure_kind': 'assertion'}] if not tests_ok else []
        # A stale worker state must never overwrite the foreground's new state.
        worker._experiment.attempt_count = 999
        started.set()
        assert reviewed.wait(5)
        return _VerificationResult(tests_ok, 'test result', commands=('check',), returncodes=(0 if tests_ok else 1,),
            payload={'source_commands': ['check'], 'command_timings': []})
    monkeypatch.setattr(AutoAgentsSelfRepairRunner, '_run_active_group_verification', expanded)
    def audit():
        assert started.wait(5)
        state.attempt_count = 7
        reviewed.set()
        return _VerificationResult(review_ok, 'review result')
    result, review = review_and_verify(obj, obj.repo_root, audit)
    assert review.ok == review_ok and result.ok == tests_ok
    assert state.attempt_count == 7 and obj._experiment_store.load().attempt_count == 7
    assert bool(obj._candidate_failure_evidence) == (not tests_ok)
    assert all(not root.exists() for root in roots)


@pytest.mark.parametrize('raises', [False, True])
def test_rejection_or_reviewer_exception_joins_cancelled_worker(setup, monkeypatch, raises):
    obj, _, _, _ = setup
    started, exited = threading.Event(), threading.Event()
    def expanded(worker, root, *, record=True):
        event = verification_cancel.get()
        assert event is not None
        worker._candidate_verification_plan = {'commands': ['slow'], 'requests': []}
        started.set()
        assert event.wait(5)
        exited.set()
        return _VerificationResult(False, 'cancelled', returncodes=(130,), termination_reasons=('cancelled',))
    monkeypatch.setattr(AutoAgentsSelfRepairRunner, '_run_active_group_verification', expanded)
    def audit():
        assert started.wait(5)
        if raises:
            raise ValueError('provider failed')
        return _VerificationResult(False, 'review rejects')
    if raises:
        with pytest.raises(ValueError, match='provider failed'):
            review_and_verify(obj, obj.repo_root, audit)
    else:
        expanded, review = review_and_verify(obj, obj.repo_root, audit)
        assert not expanded.ok and not review.ok
    assert exited.is_set() and verification_cancel.get() is None


def test_test_failure_does_not_cancel_reviewer(setup, monkeypatch):
    obj, _, _, _ = setup
    finished = threading.Event()
    def expanded(worker, root, *, record=True):
        worker._candidate_verification_plan = {'commands': ['bad'], 'requests': []}
        finished.set()
        return _VerificationResult(False, 'assertion failure')
    monkeypatch.setattr(AutoAgentsSelfRepairRunner, '_run_active_group_verification', expanded)
    def audit():
        assert finished.wait(5)
        assert verification_cancel.get() is None
        return _VerificationResult(True, 'review finished despite test failure')
    expanded, review = review_and_verify(obj, obj.repo_root, audit)
    assert not expanded.ok and review.ok


def test_environment_change_invalidates_concurrent_success(setup, monkeypatch):
    obj, _, _, _ = setup
    def expanded(worker, root, *, record=True):
        worker._candidate_verification_plan = {'commands': [], 'requests': []}
        return _VerificationResult(True, 'passed')
    monkeypatch.setattr(AutoAgentsSelfRepairRunner, '_run_active_group_verification', expanded)
    def audit():
        obj._full_suite_environment_fingerprint = lambda: ('changed',)
        return _VerificationResult(True, 'approved')
    expanded, _ = review_and_verify(obj, obj.repo_root, audit)
    assert not expanded.ok and expanded.payload['outcome'] == 'invalid'


def test_incomplete_cleanup_preserves_verification_checkout_and_blocks_success(setup, monkeypatch):
    import shutil
    from auto_agents.git_ops import remove_worktree
    obj, _, _, _ = setup
    roots = []
    def expanded(worker, root, *, record=True):
        roots.append(root)
        worker._candidate_verification_plan = {'commands': [], 'requests': []}
        return _VerificationResult(False, 'child cleanup incomplete', payload={'cleanup_incomplete': True})
    monkeypatch.setattr(AutoAgentsSelfRepairRunner, '_run_active_group_verification', expanded)
    try:
        result, _ = review_and_verify(obj, obj.repo_root, lambda: _VerificationResult(False, 'reject'))
        assert not result.ok and result.payload['cleanup_incomplete']
        assert roots[0].exists()
    finally:
        for root in roots:
            remove_worktree(obj.repo_root, root, force=True)
            shutil.rmtree(root.parent)


def test_command_scheduler_stops_dispatch_and_keeps_finished_failure(runner, monkeypatch):
    from auto_agents.repair_verification import run_component_checks
    selected = commands()
    event = threading.Event()
    calls = []
    def execute(items, root):
        calls.extend(items)
        event.set()
        return _VerificationResult(False, 'original assertion', commands=tuple(items), returncodes=(1,),
            payload={'source_commands': list(items), 'failure_evidence': [{'command': items[0], 'failures': ['assertion']} ]})
    monkeypatch.setattr(runner, '_run_verification_commands', execute)
    monkeypatch.setattr(runner, '_full_suite_shard_resources', lambda *args: (('exclusive',), True))
    token = verification_cancel.set(event)
    try:
        result = run_component_checks(runner, selected, runner.repo_root)
    finally:
        verification_cancel.reset(token)
    assert calls == selected[:1]
    assert not result.ok and result.payload['failure_evidence'][0]['failures']
    assert runner._failed_source_commands(result) == selected[:1]


def test_cancellation_does_not_create_a_sticky_code_failure():
    result = _VerificationResult(False, 'stopped', returncodes=(1, 130, 130),
        termination_reasons=('', 'cancelled', 'cancelled'),
        payload={'source_commands': ['failed', 'unfinished', 'partial'],
                 'failure_evidence': [{'command': 'partial', 'failures': [{'nodeid': 'failed_test'}]}]})
    assert AutoAgentsSelfRepairRunner._failed_source_commands(result) == ['failed', 'partial']


def test_stage_record_does_not_turn_cancellation_into_a_new_defect(setup):
    from auto_agents.repair_feedback import record_stage_failure
    obj, _, _, _ = setup
    result = _VerificationResult(False, 'cancelled', returncodes=(130,), termination_reasons=('cancelled',),
        payload={'source_commands': ['unfinished'], 'cancelled': True, 'failure_evidence': []})
    before = list(getattr(obj, '_candidate_failure_evidence', []))
    record_stage_failure(obj, 'focused_verification', result, 'candidate')
    assert getattr(obj, '_candidate_failure_evidence', []) == before
    result.payload['failure_evidence'] = [{'command': 'unfinished', 'failures': [{'nodeid': 'test_bad'}]}]
    record_stage_failure(obj, 'focused_verification', result, 'candidate')
    assert result.payload['failure_evidence'][0]['failures']
    assert obj._failed_source_commands(result) == ['unfinished']


def test_cancel_stops_live_process_group_and_does_not_start_next_command(tmp_path):
    ready = tmp_path / 'ready'
    later = tmp_path / 'must-not-run'
    event = threading.Event()
    code = 'import os,time;from pathlib import Path;Path(' + repr(str(ready)) + ').write_text(str(os.getpid()));time.sleep(60)'
    first = 'exec ' + shlex.join([sys.executable, '-c', code])
    second = shlex.join([sys.executable, '-c', 'from pathlib import Path;Path(' + repr(str(later)) + ').touch()'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_commands, [first, second], tmp_path, cancel_event=event)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and not future.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
        finally:
            event.set()
        result = future.result(timeout=10)
    assert len(result.commands) == 1 and result.commands[0].termination_reason == 'cancelled'
    assert not result.commands[0].cleanup_incomplete and not later.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)


def test_repair_runner_propagates_rejection_to_a_real_test_process(setup, tmp_path):
    obj, _, _, _ = setup
    ready = tmp_path / 'pytest-ready'
    (obj.repo_root / 'tests/test_slow.py').write_text(
        'def test_slow():\n    import os,time\n    from pathlib import Path\n'
        + '    Path(' + repr(str(ready)) + ').write_text(str(os.getpid()))\n    time.sleep(60)\n')
    git(obj.repo_root, 'add', '.')
    git(obj.repo_root, 'commit', '-qm', 'slow verification fixture')
    command = 'python -m pytest -q tests/test_slow.py::test_slow'
    obj._candidate_group.update(focused_tests=[command], planning_receipt='approved')
    def audit():
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), 'expanded pytest never started'
        return _VerificationResult(False, 'fix reviewed code')
    expanded, review = review_and_verify(obj, obj.repo_root, audit)
    assert not review.ok and not expanded.ok
    assert 'cancelled' in expanded.termination_reasons
    assert obj._failed_source_commands(expanded) == []
    assert not getattr(obj, '_candidate_failure_evidence', [])
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)


def test_cancelled_pytest_retains_failure_checkpoint_without_success_receipt(tmp_path):
    from types import SimpleNamespace
    output = tmp_path / 'result.json'
    recorder = Recorder(tmp_path, output)
    recorder.pytest_runtest_logreport(SimpleNamespace(nodeid='test_bad', when='call', duration=0.1,
        outcome='failed', failed=True, longreprtext='assert 1 == 2'))
    recorder.pytest_runtest_logstart('test_slow', None)
    partial = json.loads(Path(str(output) + '.progress.json').read_text())
    assert partial['failures'][0]['detail'] == 'assert 1 == 2'
    assert not output.exists() and 'passed' not in partial


def test_cancellation_interrupts_a_waiting_resource_lease(tmp_path, monkeypatch):
    from auto_agents.gate_execution import exclusive_resource_lease
    monkeypatch.setattr('auto_agents.gate_execution.auto_agents_state_root', lambda: tmp_path)
    event, started = threading.Event(), threading.Event()
    def wait_for_lease():
        started.set()
        with exclusive_resource_lease(['host:held'], worker_id='test', cancel_event=event):
            pytest.fail('held lease acquired')
    with exclusive_resource_lease(['host:held'], worker_id='test'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait_for_lease)
            assert started.wait(5)
            event.set()
            with pytest.raises(InterruptedError, match='cancelled'):
                future.result(timeout=5)
    with exclusive_resource_lease(['host:held'], worker_id='test'):
        pass
