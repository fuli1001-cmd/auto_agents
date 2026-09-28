import json
import time
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest

from auto_agents.repair_v2.cleanup import labels, reap_containers
from auto_agents.repair_v2.storage import execution_lease
from auto_agents.repair_v2.store import digest
from auto_agents.repair_v2 import images


def test_only_orphaned_owned_containers_are_reaped(tmp_path):
    identity = 'a' * 32
    base = tmp_path / 'executions' / identity
    base.mkdir(parents=True); (base / 'lease').touch()
    container = {'Id': 'owned', 'Name': '/aav2-' + identity, 'Config': {'Labels': {
        'org.auto-agents.v2.root': digest(str(tmp_path.resolve())),
        'org.auto-agents.v2.kind': 'verification', 'org.auto-agents.v2.lease': identity}}}
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'ps': return 0, 'owned'
        if command[1] == 'inspect': return 0, json.dumps([container])
        return 0, ''
    with patch('auto_agents.repair_v2.docker.run', side_effect=run):
        with execution_lease(base):
            assert reap_containers(tmp_path, kind='verification') == []
        assert reap_containers(tmp_path, kind='verification') == ['owned']
    assert [c for c in calls if c[1] == 'rm'] == [['docker', 'rm', '-f', 'owned']]


def test_image_gc_keeps_pending_repairs_and_recent_images(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    for index in range(4): images.record('sha256:' + str(index), 'image:' + str(index))
    images.pin('sha256:0', tmp_path / 'pending-transaction')
    paths = sorted((images.registry() / 'images').glob('*.json'))
    for path in paths:
        row = json.loads(path.read_text()); row['used'] = time.time() - (100 - int(row['image'][-1])) * 86400
        path.write_text(json.dumps(row))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ['image', 'inspect']:
            if command[-1] == '{{.Id}}': return 0, 'sha256:' + command[3].split(':')[-1]
            return 0, json.dumps([{'Config': {'Labels': {'org.auto-agents.registry': images.owner()}}}])
        return 0, ''
    with patch('auto_agents.repair_v2.docker.run', side_effect=run), \
         patch('auto_agents.repair_v2.images.shutil.which', return_value='/usr/bin/docker'):
        assert images.maintain() == ['sha256:1']
    assert ['docker', 'image', 'rm', 'image:1'] in calls
    assert not any('volume' in c for c in calls)


def test_unavailable_optional_image_cleanup_does_not_fail_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    with patch('auto_agents.repair_v2.images.shutil.which', return_value=None):
        assert images.maintain() == []
    with patch('auto_agents.repair_v2.images.shutil.which', return_value='/usr/bin/docker'), \
         patch('auto_agents.repair_v2.docker.run', side_effect=OSError('daemon unavailable')):
        assert images.maintain() == []


def image_daemon(monkeypatch, *, retag=False):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ['image', 'inspect']:
            if command[-1] == '{{.Id}}':
                return 0, 'changed' if retag else 'sha256:' + command[3].split(':')[-1]
            return 0, json.dumps([{'Config': {'Labels': {'org.auto-agents.registry': images.owner()}}}])
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    monkeypatch.setattr(images.shutil, 'which', lambda _: '/usr/bin/docker')
    return calls


def test_recent_idle_images_are_bounded_but_pins_and_preparation_survive(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    for n in range(8): images.record('sha256:' + str(n), 'image:' + str(n))
    images.pin('sha256:0', tmp_path / 'pending')
    images.acquire('image:1')
    image_daemon(monkeypatch)
    assert set(images.maintain()) == {'sha256:2', 'sha256:3'}
    assert len(list((images.registry() / 'images').glob('*.json'))) == 6  # 4 idle + 2 protected


def test_image_budget_and_retention_are_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    root = images.registry()
    (root.parent / 'policy.json').write_text(json.dumps({'verifier_images': {
        'keep': 1, 'max_unused': 10, 'max_unused_bytes': 15, 'retention_days': 100}}))
    for n in range(3): images.record('sha256:' + str(n), 'image:' + str(n), size=10)
    image_daemon(monkeypatch)
    assert set(images.maintain()) == {'sha256:0', 'sha256:1'}


@pytest.mark.parametrize('value', [None, {'keep': -1}, {'max_unused': 1}, {'retention_days': True}])
def test_invalid_image_policy_reports_error_without_deletion(tmp_path, monkeypatch, value):
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    root = images.registry()
    (root.parent / 'policy.json').write_text(json.dumps({'verifier_images': value}))
    images.record('sha256:0', 'image:0')
    calls = image_daemon(monkeypatch)
    events = []
    assert images.maintain(record=events.append) == []
    assert events[0]['result'] == 'error' and not calls


def test_gc_does_not_delete_a_retargeted_tag(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    images.record('sha256:0', 'image:0')
    calls = image_daemon(monkeypatch, retag=True)
    assert images.maintain(keep=0, age_days=0) == []
    assert not any(c[1:3] == ['image', 'rm'] for c in calls)


def test_gc_retires_absent_records_but_preserves_daemon_failures(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    images.record('sha256:0', 'image:0')
    monkeypatch.setattr(images.shutil, 'which', lambda _: '/usr/bin/docker')
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', lambda *a, **k: (1, 'daemon unavailable'))
    images.maintain(keep=0, age_days=0)
    assert list((images.registry() / 'images').glob('*.json'))
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', lambda *a, **k: (1, 'No such image: sha256:0'))
    images.maintain(keep=0, age_days=0)
    assert not list((images.registry() / 'images').glob('*.json'))


@pytest.mark.parametrize('state', ['completed', 'cancelled', 'blocked'])
def test_only_authenticated_completed_recovery_releases_pin(tmp_path, monkeypatch, state):
    from auto_agents.repair_v2.store import Store
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'storage'))
    control = tmp_path / 'control'; transaction = control / 'v2-transactions' / ('a' * 64)
    transaction.mkdir(parents=True)
    result = {'recovery_protocol': 2, 'v2_transaction': str(transaction),
              'runtime_artifact': {'artifact_id': 'artifact'}, 'v2_receipt': {'reference': 'proof'}}
    with sqlite3.connect(control / 'control.sqlite3') as db:
        db.execute('CREATE TABLE jobs(id TEXT, generation INTEGER, state TEXT, result TEXT)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?)', ('job', 2, state, json.dumps(result)))
    Store(transaction).save({'status': 'complete', 'phase': 'recovered', 'receipt': 'proof',
                             'recovery_operation': 'operation', 'live_recovery': {
                                 'job': 'job', 'generation': 2, 'operation': 'operation', 'artifact_id': 'artifact'}})
    images.pin('sha256:0', transaction)
    images.release_completed(images.registry(), float('inf'))
    pin = json.loads(next((images.registry() / 'pins').glob('*.json')).read_text())
    assert pin['active'] == (state != 'completed')


@pytest.mark.parametrize('protection', ['', 'live', 'container', 'unknown'])
def test_abandoned_ephemeral_image_requires_provenance_and_no_users(tmp_path, monkeypatch, protection):
    from auto_agents.artifact_store import process_identity
    root = tmp_path / 'gone/v2-images'; identity = 'sha256:' + 'a' * 64
    tag = 'auto-agents-verifier:v2-' + 'b' * 24
    process = process_identity() if protection == 'live' else {'pid': 2147483647, 'ticks': '1', 'boot': 'old'}
    labels = {'org.auto-agents.purpose': 'repair-verifier-v2', 'org.auto-agents.ephemeral': 'true',
              'org.auto-agents.uid': str(os.getuid()), 'org.auto-agents.registry-path': str(root),
              'org.auto-agents.registry': digest(str(root)),
              **{'org.auto-agents.creator-' + k: str(v) for k, v in process.items()}}
    if protection == 'unknown': labels.pop('org.auto-agents.ephemeral')
    info = {'Id': identity, 'RepoTags': [tag], 'Created': '2020-01-01T00:00:00Z', 'Config': {'Labels': labels}}
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ['image', 'ls']: return 0, identity
        if command[1:3] == ['image', 'inspect']:
            return (0, identity) if command[-1] == '{{.Id}}' else (0, json.dumps([info]))
        if command[1] == 'ps': return 0, 'container' if protection == 'container' else ''
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    events = []
    images.reap_ephemeral(deadline=float('inf'), record=events.append)
    assert any(c[1:3] == ['image', 'rm'] for c in calls) == (not protection)
