"""Offline discovery retains installed project packages, never installs substitutes."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from auto_agents.repair_v2.replay_environment import (
    EnvironmentUnavailable, capture, node_prefixes, prepare,
)
from auto_agents.repair_v2.boundary_driver import check_environments
from auto_agents.repair_v2.store import atomic_json


@pytest.fixture
def node_scene(tmp_path):
    project, frozen = tmp_path / 'project', tmp_path / 'frozen'
    packages = project / 'node_modules'
    (packages / 'vitest').mkdir(parents=True)
    (packages / '.bin').mkdir()
    (packages / 'vitest/package.json').write_text('{"name":"vitest","version":"5.0.0"}')
    (packages / 'vitest/vitest.mjs').write_text('retained installed runner\n')
    (packages / '.bin/vitest').symlink_to('../vitest/vitest.mjs')
    atomic_json(frozen / '.auto-agents/state/sessions/child/session_state.json',
                {'session_id': 'child', 'workflow_id': 'wf', 'mode': 'fix'})
    atomic_json(frozen / '.auto-agents/config.json', {'gates': {'steps': [
        {'proof_id': 'retained.frontend', 'runner': 'vitest', 'targets': ['workbench/owned.test.tsx']}]}})
    payload = {'project': str(project), 'invocation': {'session_id': 'child', 'workflow_id': 'wf'}}
    return project, frozen, packages, payload


def test_replay_captures_installed_packages_at_the_original_path(node_scene, tmp_path):
    project, frozen, packages, payload = node_scene
    (packages / '.npmrc').write_text('private npm token')
    (packages / '.env.local').write_text('private provider credentials')
    snapshots = prepare(tmp_path / 'cache', frozen, payload)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.prefix == packages and snapshot.root != packages
    assert snapshot.describe()['kind'] == 'node-dependencies'
    assert snapshot.describe()['credential_files_excluded'] is True
    assert (snapshot.root / '.bin/vitest').resolve().is_relative_to(snapshot.root)
    assert (snapshot.root / '.bin/vitest').read_bytes() == (packages / '.bin/vitest').read_bytes()
    assert not (snapshot.root / '.npmrc').exists() and not (snapshot.root / '.env.local').exists()
    assert not (frozen / 'node_modules').exists()
    snapshot.verify()
    assert capture(tmp_path / 'cache/node', packages, kind='node-dependencies').root == snapshot.root


@pytest.mark.parametrize('changed', ['source', 'snapshot'])
def test_dependency_changes_reject_the_snapshot(node_scene, tmp_path, changed):
    _, _, packages, _ = node_scene
    snapshot = capture(tmp_path / 'cache', packages, kind='node-dependencies')
    root = packages if changed == 'source' else snapshot.root
    (root / 'vitest/vitest.mjs').write_text('changed after capture')
    with pytest.raises(EnvironmentUnavailable):
        snapshot.verify()


def test_node_inputs_follow_frozen_runner_declarations_not_live_config(node_scene):
    project, frozen, packages, payload = node_scene
    atomic_json(project / '.auto-agents/config.json', {'gates': {'steps': []}})
    assert node_prefixes(frozen, payload) == [packages]
    assert node_prefixes(frozen, {**payload, 'invocation': {'session_id': 'unrelated'}}) == []
    atomic_json(frozen / '.auto-agents/config.json', {'gates': {'steps': []}})
    atomic_json(project / '.auto-agents/config.json', {'gates': {'steps': [{'runner': 'vitest'}]}})
    assert node_prefixes(frozen, payload) == []


def test_workbench_dependencies_follow_retained_lockfile(node_scene):
    project, frozen, packages, payload = node_scene
    (project / 'workbench').mkdir()
    packages.rename(project / 'workbench/node_modules')
    (frozen / 'workbench').mkdir()
    (frozen / 'workbench/package-lock.json').write_text('{}')
    assert node_prefixes(frozen, payload) == [project / 'workbench/node_modules']
    assert node_prefixes(frozen, {**payload, 'invocation': {
        'session_id': 'child', 'workflow_id': 'different-workflow'}}) == []


@pytest.mark.parametrize('external', [True, False])
def test_node_links_cannot_capture_files_outside_the_project_or_dependency_tree(node_scene, tmp_path, external):
    project, frozen, packages, payload = node_scene
    outside = tmp_path / 'outside'; outside.mkdir()
    (outside / 'secret').write_text('outside input')
    if external:
        packages.rename(project / 'saved')
        packages.symlink_to(outside, target_is_directory=True)
    else:
        (packages / 'escape').symlink_to(outside / 'secret')
    with pytest.raises(EnvironmentUnavailable, match='外部'):
        prepare(tmp_path / 'cache', frozen, payload)


def test_node_capture_excludes_credential_directories(node_scene, tmp_path):
    _, _, packages, _ = node_scene
    (packages / '.env.private').mkdir()
    (packages / '.env.private/token').write_text('private')
    snapshot = capture(tmp_path / 'cache', packages, kind='node-dependencies')
    assert not (snapshot.root / '.env.private').exists()


def test_missing_node_inputs_fail_closed_without_creating_replacements(node_scene, tmp_path):
    project, frozen, packages, payload = node_scene
    packages.rename(project / 'saved-packages')
    with pytest.raises(EnvironmentUnavailable, match='node_modules'):
        prepare(tmp_path / 'cache', frozen, payload)
    assert not packages.exists() and not (frozen / 'node_modules').exists()


def test_existing_node_directory_without_vitest_is_not_a_usable_environment(node_scene, tmp_path):
    _, frozen, packages, payload = node_scene
    (packages / 'vitest/package.json').unlink()
    with pytest.raises(EnvironmentUnavailable, match='缺少 Vitest'):
        prepare(tmp_path / 'cache', frozen, payload)


def test_node_dependency_check_does_not_launch_a_python_or_package_script(node_scene, monkeypatch):
    _, _, packages, _ = node_scene
    monkeypatch.setattr('auto_agents.repair_v2.boundary_driver.subprocess.run',
                        lambda *a, **k: pytest.fail('dependency admission must not execute package scripts'))
    record = {'kind': 'node-dependencies', 'prefix': str(packages), 'digest': 'bound'}
    assert check_environments({'replay_environments': [record]}) == [record]


@pytest.mark.parametrize('outcome', ['missing_runner', 'timeout', 'missing_report', 'invalid_report', 'cancelled'])
def test_controller_mounts_node_snapshot_and_stops_on_missing_runner(node_scene, tmp_path, monkeypatch, outcome):
    import threading
    from auto_agents.repair_v2.docker import DockerVerifier
    from auto_agents.repair_v2.workspace import git, source_identity
    from test_repair_v2_replay_confinement import missing_vitest
    project, frozen, packages, payload = node_scene
    source = tmp_path / 'engine'; source.mkdir(); git(source, 'init', '-q')
    (source / 'engine.py').write_text('engine = 1\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'source')
    snapshot = capture(tmp_path / 'cache', packages, kind='node-dependencies')
    monkeypatch.setattr('auto_agents.repair_v2.replay_environment.prepare', lambda *a: [snapshot])
    def run(args, **kwargs):
        if args[:2] == ['docker', 'run']:
            assert f'type=bind,src={snapshot.root},dst={packages},readonly' in args
            assert not any(f'src={packages},' in arg for arg in args)
            mount = next(arg for arg in args if arg.endswith(',dst=/result'))
            output = Path(mount.split(',')[1].removeprefix('src='))
            request = json.loads((output / 'request.json').read_text())
            assert request['replay_environments'] == [snapshot.describe()]
            if outcome == 'missing_runner':
                atomic_json(output / 'boundary.json', missing_vitest())
            elif outcome == 'timeout':
                kwargs['observation']['termination'] = 'timeout'
                atomic_json(output / 'boundary.json', {'ok': True})
            elif outcome == 'invalid_report':
                atomic_json(output / 'boundary.json', {'ok': 'true'})
            elif outcome == 'cancelled':
                kwargs['observation']['termination'] = 'cancelled'
                kwargs['cancel'].set()
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    result = DockerVerifier(tmp_path / 'verification', image='pinned').boundary(
        source_identity(source), source, frozen, payload, threading.Event())
    assert result['ok'] is False and result['infrastructure'] is True
    assert '已停止自动代码修复' in result['reason']
    if outcome == 'timeout': assert result['timed_out'] is True
    if outcome == 'cancelled': assert result['cancelled'] is True
    assert not (frozen / 'node_modules').exists()
    snapshot.verify()


def test_vitest_failure_keeps_bounded_redacted_process_evidence(tmp_path, monkeypatch):
    from auto_agents import session_verification as verification
    from auto_agents.execution_binding import RunnerContextError, test_invocations
    command = "npm exec -- vitest run workbench/owned.test.tsx -t '(?:保留字段)'"
    invocation, = test_invocations(command)
    monkeypatch.setattr('auto_agents.session_candidate._clone', lambda root, revision, destination: destination.mkdir())
    @contextmanager
    def isolated(argv, *args, **kwargs):
        yield argv
    monkeypatch.setattr('auto_agents.verification_sandbox.verification_argv', isolated)
    monkeypatch.setattr(verification.subprocess, 'run', lambda argv, **kwargs:
        subprocess.CompletedProcess(argv, 17, 'api_key=private-value\n',
                                    'x' * 4000 + '\nError: missing retained package password=secret-value'))
    with pytest.raises(RunnerContextError) as rejected:
        verification._retained_vitest_discovery(SimpleNamespace(project_root=tmp_path), tmp_path, 'retained', invocation)
    diagnostic = rejected.value.diagnostic
    assert diagnostic['failure_kind'] == 'discovery' and diagnostic['returncode'] == 17
    assert diagnostic['command'] == diagnostic['original_command'] == command
    assert len(diagnostic['stdout_tail']) <= 2000 and len(diagnostic['stderr_tail']) <= 2000
    assert 'private-value' not in diagnostic['stdout_tail'] and 'secret-value' not in diagnostic['stderr_tail']
    assert 'missing retained package' in diagnostic['stderr_tail']
