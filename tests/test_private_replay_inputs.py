"""Private replay inputs keep committed trees and reject changed registrations."""
import json
from pathlib import Path

import pytest

from auto_agents.repair_v2.private_replay import prepare
from auto_agents.repair_v2.store import atomic_json
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.repair_v2.workspace import git
from auto_agents.session_verification import fingerprint


@pytest.fixture
def private_source(tmp_path):
    target = tmp_path / 'frozen'; target.mkdir()
    private = tmp_path / 'private'; private.mkdir()
    git(private, 'init', '-q'); (private / 'value.py').write_text('VALUE = 1\n')
    git(private, 'add', '-A'); git(private, 'commit', '-qm', 'Committed delivery')
    revision = git(private, 'rev-parse', 'HEAD')
    auth = {'mode': 'auto'}
    source = {'repository': str(target), 'checkout': str(private), 'session_id': 'parent',
        'workflow_id': 'wf', 'handoff_id': 'original', 'revision': revision, 'contract_revision': revision,
        'tree': git(private, 'rev-parse', 'HEAD^{tree}'), 'authorization_fingerprint': fingerprint(auth)}
    source['source_id'] = fingerprint(source)
    state = target / '.auto-agents/state'
    atomic_json(state / 'sources' / (source['source_id'] + '.json'), source)
    atomic_json(state / 'sessions/child/session_state.json', {'session_id': 'child', 'workflow_id': 'wf',
        'parent_handoff_id': 'original', 'source_descriptor': source, 'authorization_policy': auth})
    atomic_json(state / 'handoffs/original.json', {'child': {'kind': 'fix', 'native_id': 'child'},
                                                'payload': {'source_descriptor': source}})
    info = private.stat()
    registration = state / 'custody' / (fingerprint(str(private)) + '.json')
    atomic_json(registration, {'repository': str(target), 'checkout': str(private), 'session_id': 'parent',
                             'device': info.st_dev, 'inode': info.st_ino})
    payload = {'project': str(target), 'invocation': {'engine_route': {'failed_handoff_id': 'original'}}}
    return target, private, payload, registration


def test_git_snapshot_is_independent_and_cannot_hide_modified_inputs(private_source, tmp_path):
    target, private, payload, _ = private_source
    captured = prepare(tmp_path / 'cache', target, payload)
    repo = Path(captured[0]['root'])
    assert repo != private and not (repo / 'objects/info/alternates').exists()
    assert git(repo, 'show', captured[0]['revision'] + ':value.py') == 'VALUE = 1'
    assert prepare(tmp_path / 'cache', target, payload) == captured
    (repo / 'unexpected').write_text('changed')
    with pytest.raises(RepairBlocked, match='snapshot changed'):
        prepare(tmp_path / 'cache', target, payload)


def test_private_capture_refuses_replaced_checkout_registration(private_source, tmp_path):
    target, _, payload, registration = private_source
    record = json.loads(registration.read_text()); record['inode'] += 1; atomic_json(registration, record)
    with pytest.raises(RepairBlocked, match='identity changed'):
        prepare(tmp_path / 'cache', target, payload)
