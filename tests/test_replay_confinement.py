"""Recovery must be able to launch, rather than bypass, its nested verifier."""
from pathlib import Path
import threading

import pytest

from auto_agents.repair_v2 import docker
from auto_agents.repair_v2.store import atomic_json
from auto_agents.repair_v2.workspace import git, source_identity


@pytest.fixture
def boundary_launch(tmp_path, monkeypatch):
    source = tmp_path / 'engine'
    source.mkdir()
    git(source, 'init', '-q')
    (source / 'engine.py').write_text('value = 1\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'retained engine')
    frozen = tmp_path / 'frozen'
    frozen.mkdir()
    (frozen / 'retained.txt').write_bytes(b'original evidence')
    monkeypatch.setattr('auto_agents.repair_v2.replay_environment.prepare', lambda *args: [])

    def launch(observed):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ['docker', 'run']:
                output = next(arg for arg in argv if arg.startswith('type=bind,src=')
                              and arg.endswith(',dst=/result'))
                result = Path(output.split(',')[1].removeprefix('src='))
                atomic_json(result / 'boundary.json', observed)
            return 0, ''
        monkeypatch.setattr(docker, 'run', run)
        verifier = docker.DockerVerifier(tmp_path / 'verification', image='pinned')
        result = verifier.boundary(source_identity(source), source, frozen,
            {'project': str(tmp_path / 'logical-project')}, threading.Event())
        assert (frozen / 'retained.txt').read_bytes() == b'original evidence'
        return result, calls, frozen
    return launch


def test_boundary_supports_nested_confinement_without_exposing_live_inputs(boundary_launch):
    result, calls, frozen = boundary_launch({'ok': False, 'error': 'child still blocked'})
    command = next(argv for argv in calls if argv[:2] == ['docker', 'run'])
    capabilities = [command[i + 1] for i, value in enumerate(command) if value == '--cap-add']
    assert set(capabilities) == {'SYS_ADMIN', 'SYS_PTRACE'}
    assert 'seccomp=unconfined' in command
    assert '--privileged' not in command
    assert command[command.index('--network') + 1] == 'none'
    assert '--read-only' in command
    assert '--user' in command and '--pids-limit' in command and '--memory' in command
    mounts = [command[i + 1] for i, value in enumerate(command) if value == '--mount']
    assert any(mount.endswith(',dst=/work,readonly') for mount in mounts)
    assert any(mount.endswith(',dst=/opt/repair/session_replay.py,readonly') for mount in mounts)
    assert all(str(frozen) not in mount and 'docker.sock' not in mount for mount in mounts)
    assert not any('AUTO_AGENTS_VERIFICATION_SANDBOX=' in arg for arg in command)
    assert result['ok'] is False and result['observed']['error'] == 'child still blocked'
    assert calls[-1][:3] == ['docker', 'rm', '-f']


@pytest.mark.parametrize('failure_kind', ['verification_confinement', 'verification_ownership'])
def test_boundary_preserves_failure_and_attributes_confinement_to_environment(boundary_launch, failure_kind):
    failure = {'action': 'execution_preflight_blocked', 'failure_kind': 'verification_ownership',
               'result': 'retained preflight rejected', 'retry_fix': False,
               'diagnostic': {'failure_kind': failure_kind,
                              'detail': 'unshare: unshare failed: Operation not permitted'}}
    observed = {'ok': False, 'status': 'next_provider_boundary',
                'error': 'bound child did not pass its retained recovery preflight',
                'recovery_observation': {'current_failure': failure, 'preflight_rechecked': False}}
    result, _, _ = boundary_launch(observed)
    assert result['ok'] is False
    assert result['observed'] == observed
    assert bool(result.get('infrastructure')) is (failure_kind == 'verification_confinement')
    if failure_kind == 'verification_confinement':
        assert result['reason'] == failure['result']
