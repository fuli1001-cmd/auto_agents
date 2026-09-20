"""Controller-owned replay isolation and infrastructure failure attribution."""
from copy import deepcopy
from pathlib import Path
import threading

import pytest

from auto_agents.repair_v2.docker import DockerVerifier, replay_infrastructure_reason
from auto_agents.repair_v2.store import atomic_json
from auto_agents.repair_v2.workspace import git, source_identity
from test_repair_v2_controller import job, controller


def confinement():
    return {'ok': False, 'recovery_observation': {'current_failure': {
        'failure_kind': 'verification_ownership',
        'result': 'confinement is unavailable',
        'diagnostic': {'failure_kind': 'verification_confinement',
                       'phase': 'verification_preflight',
                       'detail': 'unshare: unshare failed: Operation not permitted'}}}}


def missing_vitest():
    return {'ok': False, 'recovery_observation': {'current_failure': {
        'failure_kind': 'verification_ownership', 'result': 'retained Vitest discovery failed',
        'diagnostic': {'failure_kind': 'discovery', 'phase': 'runner_discovery', 'returncode': 1,
                       'stderr_tail': 'workbench Vitest is not installed; run npm install in ./workbench first\n'}}}}


@pytest.mark.parametrize('session', [False, True])
@pytest.mark.parametrize('failed_environment', [False, True])
def test_boundary_uses_controller_profile_and_preserves_failures(tmp_path, monkeypatch, session, failed_environment):
    source = tmp_path / 'engine'; source.mkdir(); git(source, 'init', '-q')
    (source / 'engine.py').write_text('value = 1\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'source')
    target = tmp_path / 'frozen'; target.mkdir()
    (target / 'keep').write_text('original state')
    observed = confinement() if failed_environment else {'ok': False, 'error': 'required proof missing'}
    monkeypatch.setattr('auto_agents.repair_v2.replay_environment.prepare', lambda *a: [])
    calls = []
    def run(args, **kwargs):
        if args[:2] == ['docker', 'run']:
            calls.append(args)
            assert args[args.index('--network') + 1] == 'none'
            assert '--read-only' in args and '--user' in args
            assert args[args.index('--cap-drop') + 1] == 'ALL'
            assert 'no-new-privileges' in args
            assert '--privileged' not in args and '--cap-add' not in args
            assert ('seccomp=unconfined' in args) == session
            assert not any(f'src={target},' in arg or f'src={source},' in arg for arg in args)
            mount = next(arg for arg in args if arg.endswith(',dst=/result'))
            output = Path(mount.split(',')[1].removeprefix('src='))
            atomic_json(output / 'boundary.json', observed)
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    verifier = DockerVerifier(tmp_path / 'verification', image='pinned')
    result = verifier.boundary(source_identity(source), source, target, {
        'project': str(tmp_path / 'project'), 'isolation_profile': 'privileged',
        'invocation': {'session_id': 'retained'} if session else {},
    }, threading.Event())
    assert len(calls) == 1 and result['ok'] is False
    assert result['observed'] == observed
    assert result.get('infrastructure', False) == failed_environment
    assert result['isolation_profile'] == ('session' if session else 'standard')
    if failed_environment:
        assert 'Operation not permitted' in result['reason']
    assert (target / 'keep').read_text() == 'original state'


@pytest.mark.parametrize('observed', [
    {'error_type': 'ConfinementPreflightError', 'error': 'metadata supervisor failed'},
    {'diagnostic': {'failure_kind': 'verification_confinement', 'detail': 'metadata supervisor failed'}},
    confinement(),
])
def test_structured_confinement_causes_survive_ownership_wrapping(observed):
    before = deepcopy(observed)
    assert '已停止自动代码修复' in replay_infrastructure_reason(observed)
    assert observed == before


@pytest.mark.parametrize('kind,detail', [
    ('verification_ownership', 'required verification reference has no executable proof'),
    ('discovery', 'retained Vitest discovery failed'),
    ('assertion', 'expected text: confinement is unavailable; Operation not permitted'),
])
def test_semantic_failures_and_error_text_are_not_environment_evidence(kind, detail):
    observed = confinement()
    failure = observed['recovery_observation']['current_failure']
    failure['diagnostic'] = {'failure_kind': kind}
    failure['result'] = detail
    assert replay_infrastructure_reason(observed) == ''


@pytest.mark.parametrize('early', [False, True])
@pytest.mark.parametrize('failure', [confinement, missing_vitest])
def test_confinement_stops_controller_without_reimplementation_or_replanning(job, early, failure):
    runner = controller(job)
    runner.preflight_boundary = early
    runner.boundary = lambda identity, *a: {
        'ok': False, 'snapshot': identity, 'observed': failure(),
        'infrastructure': True, 'reason': replay_infrastructure_reason(failure()),
    }
    assert '已停止自动代码修复' in replay_infrastructure_reason(failure())
    state = runner.run()
    assert state['status'] == 'blocked'
    assert state['blocker']['code'] == 'verification_infrastructure'
    assert state['attempts'] == 1 and state['replans'] == 0
    assert sum(role == 'implement' for role, _ in runner.driver.calls) == 1
    calls = list(runner.driver.calls)
    assert runner.run()['status'] == 'blocked'
    assert runner.driver.calls == calls


@pytest.mark.parametrize('change', [
    {'phase': 'test_execution'}, {'failure_kind': 'assertion'}, {'returncode': 0},
    {'stderr_tail': "Error: Cannot find module './product-component'"},
    {'stderr_tail': 'AssertionError: workbench Vitest is not installed; run npm install in ./workbench first'},
])
def test_missing_vitest_classification_requires_discovery_exit_evidence(change):
    observed = missing_vitest()
    observed['recovery_observation']['current_failure']['diagnostic'].update(change)
    assert replay_infrastructure_reason(observed) == ''


def test_replay_isolation_changes_invalidate_verification_runtime(tmp_path, monkeypatch):
    from auto_agents.repair_v2 import docker
    monkeypatch.setattr(docker.shutil, 'which', lambda *a: '/usr/bin/docker')
    monkeypatch.setattr(docker, 'reap_containers', lambda *a, **k: None)
    monkeypatch.setattr(docker, 'container_mounts', lambda: [])
    def run(args, **kwargs):
        if args[1] == 'info': return 0, 'linux'
        if args[1] == 'image': return 0, 'sha256:pinned'
        if args[1] == 'version': return 0, 'fixed-version'
        pytest.fail(str(args))
    monkeypatch.setattr(docker, 'run', run)
    verifier = DockerVerifier(tmp_path / 'verification', image='pinned')
    verifier.prepare()
    previous = verifier.runtime
    monkeypatch.setattr(docker, 'REPLAY_ISOLATION', {
        **docker.REPLAY_ISOLATION, 'session': docker.REPLAY_ISOLATION['standard'],
    })
    verifier.prepare()
    assert verifier.runtime != previous
