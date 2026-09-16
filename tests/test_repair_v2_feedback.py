from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from auto_agents.repair_v2.feedback import (
    assess_progress, diagnose, diagnostic_units, retain_unchecked,
)
from auto_agents.repair_v2.types import ReviewResult, ValidationResult, ValidationUnit
from test_repair_v2_controller import controller, job


OWNED = 'tests/test_value.py::test_value'
OTHER = 'tests/test_other.py::test_other'


def approval(snapshot='source'):
    payload = {'decision': 'APPROVE', 'findings': [],
               'coverage': [{'requirement': 'value', 'nodes': [OWNED]}]}
    return ReviewResult(True, snapshot, [], json.dumps(payload), payload['coverage'])


def historical_failures():
    # Shape of the retained incident: 33 related observation failures spread
    # across seven shards and two workspace checks in a separate shard.
    nodes = [f'tests/test_session_verification_ownership.py::test_resume[{i}]' for i in range(33)]
    rows = [{'unit': f'suite:ownership:{i}', 'failed': nodes[i:i + 5],
             'reason': "E       Failed: DID NOT RAISE <class 'ObservationBoundary'>"}
            for i in range(0, len(nodes), 5)]
    rows.append({'unit': 'suite:engine', 'failed': [
        'tests/test_engine_request.py::test_clean', 'tests/test_engine_request.py::test_dirty'],
        'reason': '>       assert snapshot(root) == before\nE       AssertionError: changed workspace'})
    return rows


def test_historical_35_failures_have_two_symptom_groups_without_losing_nodes():
    rows = historical_failures()
    original = deepcopy(rows)
    feedback = diagnose(rows)
    assert feedback['failed_nodes'] == 35 and len(feedback['groups']) == 2
    assert sorted(len(g['nodes']) for g in feedback['groups']) == [2, 33]
    assert all(g['hypothesis_only'] and len(g['representatives']) == 1 for g in feedback['groups'])
    assert rows == original
    units = [ValidationUnit('suite', 'python -m pytest -q tests',
                            tuple(n for row in rows for n in row['failed']), profile='sandbox')]
    selected = diagnostic_units(rows, units)
    assert len(selected) == 2
    assert all(u.fresh and u.profile == 'sandbox' for u in selected)
    assert {shlex.split(u.command)[-1] for u in selected} == {u.expected_nodes[0] for u in selected}


def test_structured_details_separate_fixture_failure_from_body_failure():
    rows = [{'failed': [OWNED, 'tests/test_value.py::test_second'], 'reason': 'truncated log',
             'failure_details': [{'nodeid': OWNED, 'phase': 'setup', 'message': 'OSError: disk full'},
                                 {'nodeid': 'tests/test_value.py::test_second', 'phase': 'call',
                                  'message': 'AssertionError: wrong result'}]}]
    assert {g['symptom'] for g in diagnose(rows)['groups']} == {'OSError: disk full', 'AssertionError: wrong result'}


def test_representatives_are_bounded_and_do_not_rewrite_custom_commands():
    rows = [{'failed': [f'tests/test_{i}.py::test_value'], 'reason': f'failure {i}'} for i in range(20)]
    units = [ValidationUnit(str(i), f'python -m pytest -q tests/test_{i}.py', tuple(row['failed']))
             for i, row in enumerate(rows)]
    assert len(diagnostic_units(rows, units)) == 8
    custom = [ValidationUnit('custom', 'conda run -p .conda python -m pytest -q tests', (OWNED,)),
              ValidationUnit('plugin', 'python -m pytest -p special tests', (OTHER,))]
    assert diagnostic_units([{'failed': [OWNED, OTHER]}], custom) == []


@pytest.mark.parametrize('proof', ['valid', 'missing', 'stale', 'unexecuted', 'conflicting', 'cancelled'])
def test_review_to_test_transition_needs_current_approval_and_behavioral_proof(proof):
    state = {'best_failure_keys': [['review', 'value', 'original counterexample']], 'resolved_failure_keys': []}
    failures = [{'failed': [OTHER]}]
    review = approval('old-source' if proof == 'stale' else 'source')
    passed = [OWNED]
    if proof == 'missing': review = None
    if proof == 'unexecuted': passed = []
    if proof == 'cancelled': review.ok = False
    if proof == 'conflicting': failures.append({'failed': [OWNED]})
    result = assess_progress(state, failures, 'source', passed_tests=passed, review=review)
    assert result['progressed'] is (proof == 'valid')
    assert bool(result['newly_verified']) is (proof == 'valid')


def test_passing_one_parameter_does_not_prove_a_failed_covered_test():
    result = assess_progress({'best_failure_keys': [['review', 'value', 'check']]},
        [{'failed': [OWNED + '[bad]']}], 'source', passed_tests=[OWNED + '[good]'], review=approval())
    assert not result['progressed']


def test_review_rewording_and_oscillation_cannot_repeat_resolution_credit():
    state = {'best_failure_keys': [['review', 'value', 'first wording']]}
    failures = [{'failed': [OTHER]}]
    first = assess_progress(state, failures, 'source', passed_tests=[OWNED], review=approval())
    assert first['newly_verified'] == [('review', 'value')]
    state.update(first)
    state['best_failure_keys'] = [['review', 'value', 'different wording']]
    again = assess_progress(state, failures, 'source', passed_tests=[OWNED], review=approval())
    assert not again['progressed'] and not again['newly_verified']


def test_partial_diagnostic_keeps_unchecked_test_and_review_obligations():
    rows = [{'failed': [OWNED, OTHER]}, {'requirement': 'value', 'check': 'still requires review'}]
    result = retain_unchecked(rows, [{'failed': [OWNED], 'reason': 'still failing'}], [])
    assert {n for row in result for n in row.get('failed', [])} == {OWNED, OTHER}
    assert any(row.get('requirement') == 'value' for row in result)
    assert not assess_progress({'best_failure_keys': [['test', OWNED], ['test', OTHER],
        ['review', 'value', 'still requires review']]}, result, 'source')['progressed']


@pytest.mark.parametrize('passing', [False, True])
def test_representatives_precede_full_acceptance_and_never_replace_it(job, passing):
    runner = controller(job)
    runner.run()
    identity = runner.state['snapshot']
    runner.checkpoint(status='active', phase='validate', failures=[{'failed': [OWNED]}])
    selected = ValidationUnit('mandatory', 'python -m pytest -q tests', (OWNED, OTHER))
    runner.units = lambda _: [selected]
    calls = []
    def validation(identity, root, units, cancel):
        calls.append(tuple(u.identity for u in units))
        ok = passing if units[0].identity.startswith('diagnostic:') else True
        return ValidationResult(ok, identity, checks=[{'ok': ok, 'passed': [OWNED, OTHER] if ok else []}],
            failures=[] if ok else [{'failed': [OWNED], 'reason': 'original failure remains'}])
    runner.verifier.validate = validation
    runner.review = lambda *args: (calls.append(('review',)) or approval(identity))
    assert runner.validate() is passing
    assert calls[0][0].startswith('diagnostic:')
    assert (('mandatory',) in calls) is passing and (('review',) in calls) is passing
    assert runner.state['status'] == ('ready' if passing else 'active')
    assert runner.store.read(runner.state['diagnostic_validation'])['snapshot'] == identity


@pytest.mark.parametrize('problem', ['cancel', 'infrastructure', 'snapshot'])
def test_diagnostic_interruptions_do_not_consume_search_budget(job, problem):
    runner = controller(job); runner.run()
    runner.checkpoint(status='active', phase='validate', failures=[{'failed': [OWNED]}])
    before = runner.state['stagnant']
    runner.units = lambda _: [ValidationUnit('mandatory', 'python -m pytest -q tests', (OWNED,))]
    runner.verifier.validate = lambda identity, *args: ValidationResult(False,
        'wrong' if problem == 'snapshot' else identity,
        cancelled=problem == 'cancel', infrastructure=problem == 'infrastructure')
    from auto_agents.repair_v2.types import RepairBlocked
    with pytest.raises(KeyboardInterrupt if problem == 'cancel' else RepairBlocked):
        runner.validate()
    assert runner.state['stagnant'] == before


def test_resume_during_diagnostics_does_not_repeat_implementation(job):
    runner = controller(job); runner.run()
    runner.checkpoint(status='stopped', phase='diagnose', failures=[{'failed': [OWNED]}])
    calls = list(runner.driver.calls)
    runner.units = lambda _: [ValidationUnit('mandatory', 'python -m pytest -q tests', (OWNED,))]
    assert runner.run()['status'] == 'ready'
    assert all(role == 'review' for role, _ in runner.driver.calls[len(calls):])


def legacy_progress_receipt(runner):
    runner.run()
    identity = runner.state['snapshot']
    failures = [{'failed': [OTHER], 'reason': 'new failure after old review issue was repaired'}]
    validation = ValidationResult(False, identity, checks=[{'ok': True, 'passed': [OWNED]}], failures=failures)
    runner.checkpoint(status='blocked', phase='implement', attempts=4, replans=1, stagnant=2,
        resume_token='old', failures=failures, best_failure_keys=[['review', 'value', 'old check']],
        blocker={'code': 'no_progress'}, validation=runner.store.artifact('validation', asdict(validation)),
        review=runner.store.artifact('review', asdict(approval(identity))))


def test_old_misclassified_stop_recovers_once_from_sealed_evidence(job):
    runner = controller(job); legacy_progress_receipt(runner)
    before = deepcopy(runner.state)
    runner.resume_token = 'new'
    calls = list(runner.driver.calls)
    assert runner.recover_verified_progress()
    after = runner.store.load()
    assert after['status'] == 'active' and after['phase'] == 'implement' and after['stagnant'] == 0
    for key in ('attempts', 'replans', 'calls', 'plan', 'sessions', 'failures', 'snapshot', 'validation', 'review'):
        assert after[key] == before[key]
    assert runner.driver.calls == calls
    assert after['resolved_failure_keys'] == [['review', 'value']]
    runner.checkpoint(status='blocked', blocker={'code': 'no_progress'})
    runner.resume_token = 'yet-another'
    assert not runner.recover_verified_progress()


def test_explicit_resume_can_correct_old_failure_then_complete_all_acceptance(job):
    runner = controller(job); legacy_progress_receipt(runner)
    runner.resume_token = 'explicit-resume'
    calls = list(runner.driver.calls)
    writer = runner.driver.run
    stages = []
    def repair(role, prompt, root, **kwargs):
        if role == 'implement':
            assert OTHER in prompt and 'failure_diagnosis' in prompt
            (Path(root) / 'counterexample_fixed').write_text('corrected')
            stages.append('implement')
        return writer(role, prompt, root, **kwargs)
    runner.driver.run = repair
    runner.units = lambda _: [ValidationUnit('mandatory', 'python -m pytest -q tests', (OWNED, OTHER))]
    def validate(identity, root, units, cancel):
        stages.append('diagnostic' if units[0].identity.startswith('diagnostic:') else 'full')
        assert (Path(root) / 'counterexample_fixed').read_text() == 'corrected'
        return ValidationResult(True, identity, checks=[{'ok': True, 'passed': [OWNED, OTHER]}])
    runner.verifier.validate = validate
    runner.regression = lambda identity, *args: (stages.append('regression') or {'ok': True, 'snapshot': identity})
    runner.boundary = lambda identity, *args: (stages.append('boundary') or {'ok': True, 'snapshot': identity})
    state = runner.run()
    assert state['status'] == 'ready' and state['attempts'] == 5 and state['replans'] == 1
    assert stages == ['implement', 'diagnostic', 'full', 'regression', 'boundary']
    assert runner.driver.calls[len(calls):] == [('implement', 'writer'), ('review', 'reviewer')]
    assert runner.store.read(state['receipt'])['snapshot'] == state['snapshot']


@pytest.mark.parametrize('problem', ['same_invocation', 'stale', 'no_proof', 'other_blocker', 'external', 'modified'])
def test_progress_migration_never_reopens_unproved_or_different_failures(job, problem):
    runner = controller(job); legacy_progress_receipt(runner)
    runner.resume_token = 'old' if problem == 'same_invocation' else 'new'
    if problem in ('stale', 'no_proof'):
        value = runner.store.read(runner.state['validation'])
        if problem == 'stale': value['snapshot'] = 'old-snapshot'
        else: value['checks'] = []
        runner.checkpoint(validation=runner.store.artifact('validation', value))
    if problem == 'other_blocker': runner.checkpoint(blocker={'code': 'read_only_violation'})
    if problem == 'external': runner.checkpoint(external_correction={'parent': 'manual-fix'})
    if problem == 'modified': (Path(runner.state['snapshot_path']) / 'source.py').write_text('changed')
    before = runner.store.load()
    assert not runner.recover_verified_progress()
    assert runner.store.load() == before


def test_native_pytest_preserves_structured_fixture_and_assertion_diagnostics(tmp_path):
    (tmp_path / 'test_failures.py').write_text('''import pytest
@pytest.fixture
def broken(): raise RuntimeError("fixture unavailable")
def test_setup(broken): pass
def test_body(): assert 1 == 2
''')
    source = Path(__file__).resolve().parents[1] / 'src'
    code = ('import sys,json,pytest\nsys.path.insert(0,' + repr(str(source)) + ')\n'
            'from auto_agents.repair_v2.pytest_driver import Evidence\n'
            'e=Evidence(); pytest.main(["-q","test_failures.py"],plugins=[e])\n'
            'print(json.dumps(e.failure_details))\n')
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    details = json.loads(result.stdout.splitlines()[-1])
    assert {d['phase'] for d in details} == {'setup', 'call'}
    assert {d['nodeid'] for d in details} == {'test_failures.py::test_setup', 'test_failures.py::test_body'}
    assert all(d['line'] and d['message'] and 'test_failures.py' in d['path'] for d in details)


def test_progress_display_distinguishes_diagnostics_from_full_acceptance():
    from auto_agents.repair_client import _repair_progress_message
    job = {'state': 'repairing', 'progress': {'engine': 'v2', 'phase': 'diagnose'}}
    assert '尚未开始完整验收' in _repair_progress_message(job, {'state': 'waiting'})
    job['progress'].update(phase='check_finished', unit='diagnostic:example', completed=1, total=2)
    text = _repair_progress_message(job, {'state': 'waiting'})
    assert '定向诊断' in text and '1/2' in text and '集中验收' not in text
