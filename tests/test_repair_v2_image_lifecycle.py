import json
import os

import pytest

from auto_agents.repair_v2 import images
from auto_agents.repair_v2.store import digest
from auto_agents.repair_v2.types import RepairBlocked
from test_repair_v2_cleanup import image_daemon


def test_validation_failure_is_registered_and_reclaimed_without_waiting_for_ttl(monkeypatch):
    calls = image_daemon(monkeypatch)
    with pytest.raises(RepairBlocked):
        with images.preparation('image:0'):
            images.record('sha256:0', 'image:0', status='preparing')
            raise RepairBlocked('image_validation_failed', 'missing runtime')
    assert ['docker', 'image', 'rm', 'image:0'] in calls
    assert not list((images.registry() / 'images').glob('*.json'))
    assert not list((images.registry() / 'leases').glob('*.json'))


def test_failed_revalidation_does_not_demote_or_remove_ready_shared_image(monkeypatch):
    images.record('sha256:0', 'image:0')
    calls = image_daemon(monkeypatch)
    with pytest.raises(RepairBlocked):
        with images.preparation('image:0'):
            images.record('sha256:0', 'image:0', status='preparing')
            raise RepairBlocked('image_validation_failed', 'transient failure')
    assert not any(c[1:3] == ['image', 'rm'] for c in calls)
    assert json.loads(next((images.registry() / 'images').glob('*.json')).read_text())['status'] == 'ready'


@pytest.mark.parametrize('checkpoint', ['tag_only', 'preparing', 'ready', 'wrong_creator'])
def test_crashed_build_is_recovered_but_success_and_replaced_images_survive(monkeypatch, checkpoint):
    root = images.registry(); tag = 'image:0'
    dead = {'pid': 2147483647, 'ticks': '1', 'boot': 'previous-boot'}
    images.begin_build(tag, 'temporary')
    journal = next((root / 'builds').glob('*.json'))
    value = json.loads(journal.read_text()); value['process'] = dead; journal.write_text(json.dumps(value))
    if checkpoint in ('preparing', 'ready'):
        images.record('sha256:0', tag, status=checkpoint)
    labels = {'org.auto-agents.registry': images.owner(), 'org.auto-agents.purpose': 'repair-verifier-v2',
              **{'org.auto-agents.creator-' + k: str(v) for k, v in dead.items()}}
    if checkpoint == 'wrong_creator': labels['org.auto-agents.creator-pid'] = '123'
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ['image', 'inspect']:
            if command[-1] == '{{.Id}}': return 0, 'sha256:0'
            return 0, json.dumps([{'Id': 'sha256:0', 'Size': 100, 'Config': {'Labels': labels}}])
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    monkeypatch.setattr(images.shutil, 'which', lambda _: '/usr/bin/docker')
    removed = images.maintain()
    assert bool(removed) == (checkpoint in ('tag_only', 'preparing'))


@pytest.mark.parametrize('existing', [True, False])
def test_normal_foreign_namespace_requires_its_custody_registry(tmp_path, monkeypatch, existing):
    root = tmp_path / 'foreign/v2-images'
    if existing: root.mkdir(parents=True)
    identity = 'sha256:' + '1' * 64
    labels = {'org.auto-agents.purpose': 'repair-verifier-v2', 'org.auto-agents.origin-version': '1',
              'org.auto-agents.ephemeral': 'false', 'org.auto-agents.uid': str(os.getuid()),
              'org.auto-agents.registry-path': str(root), 'org.auto-agents.registry': digest(str(root)),
              'org.auto-agents.creator-pid': '2147483647', 'org.auto-agents.creator-ticks': '1',
              'org.auto-agents.creator-boot': 'previous-boot'}
    info = {'Id': identity, 'Created': '2020-01-01T00:00:00Z', 'Config': {'Labels': labels},
            'RepoTags': ['auto-agents-verifier:v2-' + '2' * 24]}
    def run(command, **kwargs):
        if command[1:3] == ['image', 'ls']: return 0, identity
        if command[1:3] == ['image', 'inspect']: return 0, json.dumps([info])
        pytest.fail('normal missing registry must never authorize deletion')
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    maintained = []
    monkeypatch.setattr(images, 'maintain', lambda **kwargs: maintained.append(kwargs))
    events = []
    images.reap_ephemeral(deadline=float('inf'), record=events.append)
    assert bool(maintained) == existing
    if existing: assert maintained[0]['keep'] is None and maintained[0]['registry_root'] == root
    else: assert events[-1]['reason'] == 'image_registry_missing_requires_recovery'
