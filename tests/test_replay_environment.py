from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.repair_v2.replay_environment import (
    EnvironmentUnavailable, capture, commands, inventory, prefixes, prepare,
)
from auto_agents.repair_v2.store import atomic_json
from auto_agents.repair_v2.boundary_driver import check_environments, ReplayEnvironmentUnavailable


@pytest.fixture
def environment(tmp_path):
    prefix = tmp_path / 'project/.conda'
    (prefix / 'bin').mkdir(parents=True)
    (prefix / 'conda-meta').mkdir()
    (prefix / 'conda-meta/history').write_text('a real input fixture, not a replay-created marker')
    (prefix / 'bin/python3').write_bytes(b'original interpreter bytes')
    (prefix / 'bin/python').symlink_to(prefix / 'bin/python3')
    (prefix / 'lib').mkdir()
    (prefix / 'lib/package.py').write_text('original package bytes')
    return prefix


def test_capture_copies_real_inputs_without_links_to_live_environment(environment, tmp_path):
    before = inventory(environment)
    snapshot = capture(tmp_path / 'cache', environment)
    assert snapshot.root != environment
    assert (snapshot.root / 'bin/python').read_bytes() == b'original interpreter bytes'
    assert (snapshot.root / 'bin/python').resolve().is_relative_to(snapshot.root)
    assert (snapshot.root / 'bin/python3').stat().st_ino != (environment / 'bin/python3').stat().st_ino
    assert inventory(environment) == before
    snapshot.verify()


def test_environment_snapshot_is_reused_and_package_changes_invalidate_it(environment, tmp_path):
    first = capture(tmp_path / 'cache', environment)
    assert capture(tmp_path / 'cache', environment).root == first.root
    (environment / 'lib/package.py').write_text('updated package')
    with pytest.raises(EnvironmentUnavailable): first.verify()
    second = capture(tmp_path / 'cache', environment)
    assert second.identity != first.identity
    assert (first.root / 'lib/package.py').read_text() == 'original package bytes'
    second.verify()


def test_concurrent_capture_publishes_one_valid_immutable_snapshot(environment, tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(lambda _: capture(tmp_path / 'cache', environment), range(2)))
    assert snapshots[0].root == snapshots[1].root
    snapshots[0].verify()
    assert not list((tmp_path / 'cache').glob('preparing-*'))


def test_activation_credentials_are_not_copied_or_executed(environment, tmp_path):
    (environment / 'conda-meta/state').write_text('{"env_vars":{"API_KEY":"private"}}')
    scripts = environment / 'etc/conda/activate.d'; scripts.mkdir(parents=True)
    (scripts / 'secret.sh').write_text('export API_KEY=private')
    snapshot = capture(tmp_path / 'cache', environment)
    assert not (snapshot.root / 'conda-meta/state').exists()
    assert not (snapshot.root / 'etc/conda/activate.d/secret.sh').exists()
    assert snapshot.describe()['activation_state_included'] is False


def test_missing_environment_cannot_be_replaced_with_empty_conda_metadata(tmp_path):
    prefix = tmp_path / '.conda'; (prefix / 'conda-meta').mkdir(parents=True)
    with pytest.raises(EnvironmentUnavailable, match='实际的 Conda 环境不可用'):
        capture(tmp_path / 'cache', prefix)
    assert not (tmp_path / 'cache').exists()


def test_external_symlinks_and_modified_cache_are_rejected(environment, tmp_path):
    secret = tmp_path / 'outside'; secret.write_text('outside input')
    link = environment / 'lib/outside'; link.symlink_to(secret)
    with pytest.raises(EnvironmentUnavailable, match='外部'):
        capture(tmp_path / 'cache', environment)
    link.unlink()
    snapshot = capture(tmp_path / 'cache', environment)
    (snapshot.root / 'lib/package.py').write_text('tampered')
    with pytest.raises(EnvironmentUnavailable): capture(tmp_path / 'cache', environment)


def test_only_retained_owned_child_commands_supply_environment_inputs(tmp_path, environment):
    target = tmp_path / 'frozen'
    state = target / '.auto-agents/state'
    atomic_json(state / 'sessions/root/session_state.json', {'workflow_id': 'wf', 'active_handoff_id': 'wrapper'})
    atomic_json(state / 'handoffs/wrapper.json', {'workflow_id': 'wf', 'payload': {'resume_handoff_id': 'original'}})
    atomic_json(state / 'handoffs/original.json', {'workflow_id': 'wf', 'child': {'kind': 'fix', 'native_id': 'child'}})
    command = 'conda run -p ./.conda python -m pytest -q tests/test_current.py'
    atomic_json(state / 'sessions/child/session_state.json', {'workflow_id': 'wf', 'fix_verify_command': command})
    atomic_json(state / 'sessions/unrelated/session_state.json', {'workflow_id': 'other', 'fix_verify_command': 'conda run -p /unrelated python'})
    payload = {'project': str(environment.parent), 'invocation': {'session_id': 'root', 'workflow_id': 'wf'}}
    assert commands(target, payload) == [command]
    assert not (target / '.conda').exists()
    snapshots = prepare(tmp_path / 'cache', target, payload)
    assert [item.prefix for item in snapshots] == [environment]
    assert not (target / '.conda').exists()  # The frozen scene stays immutable.


def test_ready_run_task_supplies_direct_python_environment_without_old_sessions(tmp_path, environment):
    target = tmp_path / 'frozen'
    state = target / '.auto-agents/state'
    selected = './.conda/bin/python -m pytest -q tests/test_current.py::test_contract'
    atomic_json(state / 'run_state.json', {
        'run_id': 'run-current', 'resume_context': {'workflow_id': 'wf-current',
            'implementation_ready_tasks': {'task-current': True}},
        'tasks': [{'task_id': 'task-current', 'status': 'in_progress',
                   'verification_refs': ['tests/test_current.py::test_contract']},
                  {'task_id': 'unrelated', 'status': 'pending', 'verification_refs': ['tests/test_old.py']}]})
    atomic_json(target / '.auto-agents/config.json', {'gates': {'commands': [selected,
        'conda run -p /outside/.conda python -m pytest tests/test_old.py']}})
    bound = {'kind': 'run', 'native_id': 'run-current'}
    atomic_json(state / 'workflows/wf-current/workflow.json', {'root': bound, 'active_frame': bound})
    atomic_json(state / 'sessions/old/session_state.json', {'workflow_id': 'old',
        'fix_verify_command': 'conda run -p /outside/.conda python -m pytest tests/test_current.py'})
    payload = {'project': str(environment.parent), 'invocation': {'run_id': 'run-current', 'workflow_id': 'wf-current'}}
    before = (state / 'run_state.json').read_bytes()
    assert commands(target, payload) == [selected]
    assert commands(target, {**payload, 'invocation': {'run_id': 'run-current'}}) == [selected]
    assert [snapshot.prefix for snapshot in prepare(tmp_path / 'cache', target, payload)] == [environment]
    assert (state / 'run_state.json').read_bytes() == before and not (target / '.conda').exists()
    for invocation in [{'run_id': 'other', 'workflow_id': 'wf-current'},
                       {'run_id': 'run-current', 'workflow_id': 'other'}]:
        assert commands(target, {**payload, 'invocation': invocation}) == []


@pytest.mark.parametrize('command', [
    './.conda/bin/python -B -m pytest tests/test_current.py',
    'env FLAG=1 ./.conda/bin/python3.11 -m pytest tests/test_current.py',
])
def test_direct_python_prefix_is_bound_to_project(command, tmp_path):
    assert prefixes(command, tmp_path) == [tmp_path / '.conda']
    with pytest.raises(EnvironmentUnavailable):
        prefixes('/outside/.conda/bin/python -m pytest tests/test_current.py', tmp_path)


@pytest.mark.parametrize('command', [
    'conda run -p ./.conda python -m pytest',
    'env KEY=value conda run --no-capture-output --prefix=./.conda python -m pytest',
])
def test_explicit_prefix_parsing_preserves_the_retained_command(command, tmp_path):
    assert prefixes(command, tmp_path) == [tmp_path / '.conda']
    assert prefixes("python -c 'print(\"conda run -p /outside\")'", tmp_path) == []


@pytest.mark.parametrize('command', [
    'conda run -p /outside/.conda python', 'conda run -p ./src python',
    'conda run -p "$DYNAMIC" python', 'conda run -n shared python',
])
def test_unsupported_environment_selector_never_mounts_other_source(command, tmp_path):
    with pytest.raises(EnvironmentUnavailable): prefixes(command, tmp_path)


def test_trusted_driver_checks_the_captured_interpreter_without_site_or_activation(monkeypatch, environment):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'prefix': str(environment), 'version': [3, 11, 10]}))
    monkeypatch.setattr('auto_agents.repair_v2.boundary_driver.subprocess.run', run)
    result = check_environments({'replay_environments': [{'prefix': str(environment), 'digest': 'captured'}]})
    assert calls[0][:4] == [str(environment / 'bin/python'), '-I', '-S', '-c']
    assert result[0]['interpreter']['version'] == [3, 11, 10]
    monkeypatch.setattr('auto_agents.repair_v2.boundary_driver.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"prefix":"/wrong"}'))
    with pytest.raises(ReplayEnvironmentUnavailable):
        check_environments({'replay_environments': [{'prefix': str(environment)}]})


@pytest.mark.parametrize('tampered', [False, True])
def test_boundary_mounts_only_snapshot_and_keeps_real_recovery_failure(monkeypatch, environment, tmp_path, tampered):
    import threading
    from auto_agents.repair_v2.docker import DockerVerifier
    from auto_agents.repair_v2.workspace import git, source_identity
    source = tmp_path / 'engine'; source.mkdir(); git(source, 'init', '-q')
    (source / 'engine.py').write_text('source = 1\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'source')
    frozen = tmp_path / 'frozen'; frozen.mkdir()
    (frozen / 'keep.txt').write_text('frozen state')
    snapshot = capture(tmp_path / 'cache', environment)
    monkeypatch.setattr('auto_agents.repair_v2.replay_environment.prepare', lambda *a: [snapshot])
    calls = []
    def run(args, **kwargs):
        if args[:2] == ['docker', 'run']:
            calls.append(args)
            mount = f'type=bind,src={snapshot.root},dst={environment},readonly'
            assert mount in args
            assert not any(f'src={environment},' in arg for arg in args)
            output = next(arg for arg in args if arg.startswith('type=bind,src=') and arg.endswith(',dst=/result'))
            root = Path(output.split(',')[1].removeprefix('src='))
            atomic_json(root / 'boundary.json', {'ok': False, 'error': 'retained verification still rejects the child'})
            if tampered:
                (snapshot.root / 'lib/package.py').write_text('changed during verification')
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    verifier = DockerVerifier(tmp_path / 'verification', image='pinned')
    result = verifier.boundary(source_identity(source), source, frozen,
                               {'project': str(environment.parent)}, threading.Event())
    assert len(calls) == 1 and result['ok'] is False
    if tampered:
        assert result['infrastructure'] is True
    else:
        assert result['observed']['error'] == 'retained verification still rejects the child'
        assert result['environment_inputs'] == [snapshot.describe()]
    assert (frozen / 'keep.txt').read_text() == 'frozen state'
    assert (environment / 'lib/package.py').read_text() == 'original package bytes'
