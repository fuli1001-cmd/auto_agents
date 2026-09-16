from pathlib import Path

import pytest

from auto_agents.repair_v2 import docker
from auto_agents.repair_v2.types import RepairBlocked


def test_container_disappearing_during_observation_preserves_live_mounts(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command == ['docker', 'ps', '-aq']: return 0, 'gone\nlive\n'
        assert '{{json .Mounts}}' in command and '--type' in command
        if command[-2:] == ['gone', 'live']: return 1, '[]\nError: No such object: gone'
        if command[-1] == 'gone': return 1, 'Error: No such object: gone\n'
        return 0, '[{"Source":"/tmp/still-running"}]\n'
    monkeypatch.setattr(docker, 'run', run)
    assert docker.container_mounts() == [Path('/tmp/still-running')]
    assert len(commands) == 4


def test_normal_mount_observation_is_batched_and_requests_no_environment(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command == ['docker', 'ps', '-aq']: return 0, 'first\nsecond\n'
        assert command[-3:] == ['{{json .Mounts}}', 'first', 'second']
        return 0, '[{"Source":"/tmp/first"}]\n[{"Source":"/tmp/second"}]\n'
    monkeypatch.setattr(docker, 'run', run)
    assert docker.container_mounts() == [Path('/tmp/first'), Path('/tmp/second')]
    assert len(commands) == 2


def test_other_inspection_errors_do_not_authorize_execution_cleanup(monkeypatch):
    def run(command, **kwargs):
        if command == ['docker', 'ps', '-aq']: return 0, 'live\n'
        return 1, 'permission denied contacting Docker'
    monkeypatch.setattr(docker, 'run', run)
    with pytest.raises(RepairBlocked, match='permission denied'):
        docker.container_mounts()
