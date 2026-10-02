import time

import pytest

from auto_agents.recovery import Event, KernelError, KernelStore
from auto_agents.recovery.evidence_maintenance import collect_objects


def test_collection_preserves_transitive_authority_and_current_upgrade_logs(tmp_path):
    store = KernelStore(tmp_path / 'control')
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path / 'project')}))
    leaf = store.put({'receipt': 'required'})
    record = store.put({'evidence': leaf})
    store.apply('workflow', 1, Event('projection', 'projection_saved', {'name': 'evidence', 'blob': record}))
    junk = store.put({'old': 'unreferenced output'})
    old_log = store.put_bytes(b'old successful gate output')
    old_gate = store.put({'ok': True, 'check': 'all_entrypoints', 'log': old_log})
    current_log = store.put_bytes(b'current upgrade replay log')
    current = store.put({'ok': True, 'check': 'journal_replay', 'log': current_log})
    store.set_meta('verified_upgrades', [old_gate, current])
    store.set_meta('activation_receipt', current)
    report = collect_objects(store, grace_seconds=0)
    assert report['removed_objects'] == 2
    assert store.read(leaf) == {'receipt': 'required'} and store.read(record)['evidence'] == leaf
    assert store.read_bytes(current_log) == b'current upgrade replay log'
    assert not store.blob_path(junk).exists() and not store.blob_path(old_log).exists()
    assert store.replay('workflow') == store.load('workflow')


def test_mark_timeout_does_not_delete_any_objects(tmp_path):
    store = KernelStore(tmp_path / 'control')
    ref = store.put({'private': 'record'})
    store.set_meta('retained', ref)
    unused = store.put({'unused': 'log'})
    report = collect_objects(store, grace_seconds=0, deadline=time.monotonic() - 1)
    assert not report['ok'] and report['freed_bytes'] == 0
    assert store.blob_path(ref).exists() and store.blob_path(unused).exists()


def test_sealed_duplicate_survives_expired_attachment_and_keeps_epoch_fence(tmp_path):
    store = KernelStore(tmp_path / 'control')
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path / 'project')}))
    ref = store.put({'historical': 'projection'})
    event = Event('projection', 'projection_saved', {'name':'evidence', 'blob':ref})
    saved = store.apply('workflow', 1, event)
    store.blob_path(ref).unlink()
    store.set_meta('epoch', 2)
    with pytest.raises(KernelError, match='Runtime generation changed'):
        store.apply('workflow', 0, event, expected_epoch=1)
    assert store.apply('workflow', 0, event, expected_epoch=2) == saved
