"""A resumed transaction checks current code before using an old plan."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.repair_v2 import integration
from auto_agents.repair_v2.source_refresh import prepare
from auto_agents.repair_v2.store import Store
from auto_agents.repair_v2.transaction import transaction_root
from auto_agents.repair_v2.workspace import git
from auto_agents.repair_control import Store as ControlStore
from test_repair_v2_controller import job, controller, Driver as SimpleDriver
from test_repair_v2_delivery import repair_request, Driver, Verifier, components


def stopped_request(request):
    driver, verifier = Driver(), Verifier()
    original = driver.run
    def stop(role, *args, **kwargs):
        if role == 'implement':
            raise KeyboardInterrupt()
        return original(role, *args, **kwargs)
    driver.run = stop
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        with pytest.raises(KeyboardInterrupt):
            integration.repair_entry(request)
    driver.run = original
    root = transaction_root(request['config'], request['job']['payload'])
    return root, driver, verifier


def renewed(request, *, update_base=True):
    public = ControlStore(request['config']['root'])
    payload = deepcopy(request['job']['payload'])
    if update_base:
        payload['base'] = git(request['config']['source_root'], 'rev-parse', 'HEAD')
    subscriber = public.register({'project': payload['project'], 'token': 'next-' + payload['base']})
    identity = public.submit(subscriber, payload)
    if identity == request['job']['id']:
        with public.connect() as db:
            db.execute('UPDATE jobs SET generation=generation+1 WHERE id=?', (identity,))
    return {'config': request['config'], 'job': public.job(identity)}


def execute(request, driver, verifier):
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        return integration.repair_entry(request)


def test_resume_imports_current_engine_and_preserves_partial_work_without_reimplementation(repair_request):
    root, driver, verifier = stopped_request(repair_request)
    before = deepcopy(Store(root).load())
    original_request = (root / 'request.json').read_bytes()
    original_selection = (root / 'source-selection.json').read_bytes()
    candidate = root / 'workspace/candidate'
    (candidate / 'retained.txt').write_text('interrupted candidate work')
    source = Path(repair_request['config']['source_root'])
    (source / 'source.py').write_text('value = 1\n')
    (source / 'new_engine.txt').write_text('current engine')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'Fix the current engine')
    current = git(source, 'rev-parse', 'HEAD')
    request = renewed(repair_request)
    calls = len(driver.calls)
    observed = []
    boundary = verifier.boundary
    def recheck(identity, snapshot, *args):
        git(snapshot, 'merge-base', '--is-ancestor', current, 'HEAD')
        observed.append('boundary')
        return boundary(identity, snapshot, *args)
    verifier.boundary = recheck
    result = execute(request, driver, verifier)
    assert result['ok'], result
    assert driver.calls[calls:] == ['review']
    assert observed == ['boundary']  # The early proof is reused by formal acceptance.
    assert (candidate / 'retained.txt').read_text() == 'interrupted candidate work'
    assert (root / 'request.json').read_bytes() == original_request
    assert (root / 'source-selection.json').read_bytes() == original_selection
    selected = json.loads((Path(request['config']['root']) / 'jobs' / request['job']['id'] / 'source-selection.json').read_text())
    assert selected['revision'] == current
    after = Store(root).load()
    assert after['plan'] == before['plan'] and after['attempts'] == before['attempts']
    assert after['calls'] == before['calls'] + 1
    assert integration.verify_receipt(result)


@pytest.mark.parametrize('confinement', [False, True, 'vitest'])
def test_resume_checks_environment_before_any_provider_or_full_suite(repair_request, confinement):
    from auto_agents.repair_v2.docker import replay_infrastructure_reason
    reason = replay_infrastructure_reason({'diagnostic': {
        'failure_kind': 'verification_confinement', 'detail': 'unshare failed: Operation not permitted',
    }}) if confinement else 'retained environment unavailable'
    if confinement == 'vitest':
        from test_repair_v2_replay_confinement import missing_vitest
        reason = replay_infrastructure_reason(missing_vitest())
    root, driver, verifier = stopped_request(repair_request)
    source = Path(repair_request['config']['source_root'])
    (source / 'source.py').write_text('value = 1\n')
    git(source, 'commit', '-qam', 'Current correction')
    request = renewed(repair_request)
    calls = list(driver.calls)
    driver.preflight = lambda *a: pytest.fail('provider was prepared before the recovery environment check')
    verifier.boundary = lambda identity, *a: {'ok': False, 'snapshot': identity, 'infrastructure': True,
                                            'reason': reason}
    result = execute(request, driver, verifier)
    assert not result['ok'] and result['error'] == reason
    assert driver.calls == calls and not verifier.calls
    assert Store(root).load()['source_refresh_check_pending']
    driver.preflight = lambda *a: None
    verifier.boundary = lambda identity, *a: {'ok': True, 'snapshot': identity}
    result = execute(renewed(request), driver, verifier)
    assert result['ok'], result
    assert driver.calls[len(calls):] == ['review']


def test_remaining_failure_uses_fresh_evidence_not_historical_plan(repair_request):
    root, driver, verifier = stopped_request(repair_request)
    state = Store(root).load()
    state['plan'] = Store(root).artifact('plan', {'text': 'OBSOLETE_WRITE_EVERYTHING', 'request': state['request_digest']})
    Store(root).save(state)
    source = Path(repair_request['config']['source_root'])
    (source / 'new_engine.txt').write_text('current source')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'Current engine still needs a correction')
    sequence = []
    def boundary(identity, snapshot, *args):
        assert (Path(snapshot) / 'new_engine.txt').exists()
        sequence.append('boundary')
        return {'ok': (Path(snapshot) / 'source.py').read_text() == 'value = 1\n',
                'snapshot': identity, 'observed': {'failure': 'CURRENT_REMAINING_FAILURE',
                    'engine_runtime': {'ok': True}, 'recovery_observation': {
                        'ok': False, 'parent_session_id': repair_request['job']['payload']['invocation']['session_id'],
                        'child_session_id': 'bound-child'}}}
    verifier.boundary = boundary
    original = driver.run
    def run(role, prompt, *args, **kwargs):
        if role == 'implement':
            assert sequence == ['boundary']
            assert 'CURRENT_REMAINING_FAILURE' in prompt and 'OBSOLETE_WRITE_EVERYTHING' not in prompt
        sequence.append(role)
        return original(role, prompt, *args, **kwargs)
    driver.run = run
    result = execute(renewed(repair_request), driver, verifier)
    assert result['ok'], result
    assert sequence[:2] == ['boundary', 'implement'] and 'plan' not in sequence


def test_source_refresh_cannot_move_the_original_regression_baseline(repair_request):
    from dataclasses import asdict
    from auto_agents.repair_v2.comparison import POLICY
    from auto_agents.repair_v2.store import digest
    from auto_agents.repair_v2.types import ValidationResult
    from test_goal_scoped_repair import report
    root, driver, verifier = stopped_request(repair_request)
    source = Path(repair_request['config']['source_root'])
    (source / 'source.py').write_text('value = 1\n')
    git(source, 'commit', '-qam', 'Current engine already restores the value')
    current = git(source, 'rev-parse', 'HEAD')
    old = json.loads((root / 'request.json').read_text())['engine_base']
    def validate(identity, snapshot, units, cancel):
        raw = report(identity)
        raw['checks'].append({'ok': True, 'passed': ['tests/test_value.py::test_value'], 'inputs': {'runtime': verifier.runtime}})
        return ValidationResult(**raw)
    verifier.validate = validate
    def compare(identity, snapshot, repository, base, validation, cancel):
        assert base == old and base != current
        assert git(repository, 'show', base + ':source.py') == 'value = 0'
        return {'policy': POLICY, 'ok': True, 'snapshot': identity, 'base': base,
                'validation_digest': digest(asdict(validation)), 'baseline_report': report('baseline'),
                'unchanged_tests': ['tests/test_old.py::test_old']}
    verifier.compare_baseline = compare
    calls = list(driver.calls)
    result = execute(renewed(repair_request), driver, verifier)
    assert result['ok'], result
    assert driver.calls[len(calls):] == ['review']
    assert integration.verify_receipt(result)['comparison_base'] == old


# Preserve the retained node ID while checking the corrected baseline contract.
test_existing_current_engine_failure_is_not_misclassified_against_obsolete_baseline = test_source_refresh_cannot_move_the_original_regression_baseline


def interrupted(job):
    driver = SimpleDriver()
    original = driver.run
    def run(role, *args, **kwargs):
        if role == 'implement': raise KeyboardInterrupt()
        return original(role, *args, **kwargs)
    driver.run = run
    runner = controller(job, driver)
    runner.resume_token = 'new-invocation'
    with pytest.raises(KeyboardInterrupt): runner.run()
    driver.run = original
    return controller(job, driver)


@pytest.mark.parametrize('outside', [False, True])
def test_merge_conflicts_are_resolved_before_rechecking_and_never_run_old_plan(job, outside):
    runner = interrupted(job)
    candidate = runner.workspace.candidate
    (candidate / 'source.py').write_text('value = 2\n')
    source = runner.workspace.source
    (source / 'source.py').write_text('value = 1\n')
    git(source, 'commit', '-qam', 'Current engine correction')
    parent = git(source, 'rev-parse', 'HEAD')
    assert prepare(runner, source, parent)
    before = Store(runner.store.root).load()['source_refresh_pending']['candidate_before']
    assert git(candidate, 'show', before + ':source.py') == 'value = 2'
    sequence = []
    original = runner.driver.run
    def run(role, prompt, root, **kwargs):
        if role == 'implement':
            assert prompt.startswith('Resolve ONLY') and 'PLAN:' not in prompt
            if outside: (Path(root) / 'unrelated.txt').write_text('outside merge scope')
        sequence.append(role)
        return original(role, prompt, root, **kwargs)
    runner.driver.run = run
    def boundary(identity, snapshot, cancel):
        sequence.append('boundary')
        git(snapshot, 'merge-base', '--is-ancestor', parent, 'HEAD')
        return {'ok': True, 'snapshot': identity}
    runner.boundary = boundary
    runner.preflight_boundary = True
    result = runner.run()
    if outside:
        assert result['blocker']['code'] == 'source_merge_scope'
        assert sequence == ['implement']
        assert runner.run() == result and sequence == ['implement']
    else:
        assert result['status'] == 'ready'
        assert sequence == ['implement', 'boundary', 'review']


def test_refresh_recovers_crash_after_git_merge_without_losing_history(job, monkeypatch):
    import auto_agents.repair_v2.source_refresh as refresh
    runner = interrupted(job)
    before = deepcopy(Store(runner.store.root).load())
    source = runner.workspace.source
    (source / 'source.py').write_text('value = 1\n'); git(source, 'commit', '-qam', 'Current correction')
    parent = git(source, 'rev-parse', 'HEAD')
    finish = refresh.finish
    monkeypatch.setattr(refresh, 'finish', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt): prepare(runner, source, parent)
    assert Store(runner.store.root).load()['source_refresh_pending']
    monkeypatch.setattr(refresh, 'finish', finish)
    assert prepare(runner, source, parent)
    result = runner.run()
    assert result['status'] == 'ready'
    assert result['attempts'] == before['attempts'] and result['plan'] == before['plan']


def test_current_engine_tests_are_preserved_without_reimposing_superseded_assertions(job):
    from dataclasses import replace
    from auto_agents.repair_v2.workspace import Workspace
    source = job[2].source
    (source / 'tests').mkdir()
    (source / 'tests/test_policy.py').write_text('def test_policy():\n    assert 6 == 6\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'Previous test contract')
    baseline = git(source, 'rev-parse', 'HEAD')
    request = replace(job[0], engine_base=baseline)
    runner = interrupted((request, job[1], Workspace(job[2].root, source, baseline)))
    (source / 'tests/test_policy.py').write_text('def test_policy():\n    assert 4 == 4\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'Current test contract')
    parent = git(source, 'rev-parse', 'HEAD')
    prepare(runner, source, parent)
    assert not runner.test_findings(runner.workspace.candidate)
    (runner.workspace.candidate / 'tests/test_policy.py').write_text('def test_policy():\n    pass\n')
    assert runner.test_findings(runner.workspace.candidate)


def test_source_advancing_during_an_interrupted_merge_is_also_imported(job):
    runner = interrupted(job)
    candidate, source = runner.workspace.candidate, runner.workspace.source
    (candidate / 'source.py').write_text('value = 2\n')
    (source / 'source.py').write_text('value = 1\n')
    git(source, 'commit', '-qam', 'First current correction')
    prepare(runner, source, git(source, 'rev-parse', 'HEAD'))
    (source / 'latest.txt').write_text('another current change')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'Source advanced while merge was stopped')
    latest = git(source, 'rev-parse', 'HEAD')
    prepare(runner, source, latest)
    def boundary(identity, snapshot, cancel):
        assert (Path(snapshot) / 'latest.txt').read_text() == 'another current change'
        git(snapshot, 'merge-base', '--is-ancestor', latest, 'HEAD')
        return {'ok': True, 'snapshot': identity}
    runner.boundary = boundary
    runner.preflight_boundary = True
    result = runner.run()
    assert result['status'] == 'ready'
    assert result['source_refreshed']['parent'] == latest


def test_committed_interrupted_writer_is_checked_before_another_implementation(job):
    runner = interrupted(job)
    candidate = runner.workspace.candidate
    (candidate / 'source.py').write_text('value = 1\n')
    runner.workspace.checkpoint()  # Crash before the implementation state checkpoint.
    state = Store(runner.store.root).load()
    state['active_call'] = {'role': 'implement', 'source': 'old-source'}
    Store(runner.store.root).save(state)
    assert prepare(runner, runner.workspace.source, runner.request.engine_base)
    calls = list(runner.driver.calls)
    assert runner.run()['status'] == 'ready'
    assert runner.driver.calls[len(calls):] == [('review', '')]
