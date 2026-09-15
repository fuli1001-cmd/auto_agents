import json
import time
from pathlib import Path
from unittest.mock import patch

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
            return 0, json.dumps([{'Config': {'Labels': {'org.auto-agents.registry': images.owner()}}}])
        return 0, ''
    with patch('auto_agents.repair_v2.docker.run', side_effect=run):
        assert images.maintain() == ['sha256:1']
    assert ['docker', 'image', 'rm', 'image:1'] in calls
    assert not any('volume' in c for c in calls)
