import json

import pytest

from auto_agents.recovery import Event, KernelError, KernelStore
from auto_agents.recovery.journal_maintenance import compact_history
from auto_agents.recovery.journal_storage import ENCODING, PROJECTION, pack, unpack
from auto_agents.recovery.observations import compact, compact_for_storage


def history(tmp_path, *, legacy=False):
    store = KernelStore(tmp_path / 'control')
    if legacy: store.set_meta('kernel_storage_format', 1)
    store.apply('workflow', 0, Event('create', 'workflow_registered', {
        'workflow_id': 'workflow', 'goal_id': 'goal', 'project': str(tmp_path / 'project')}))
    blob = store.put({'receipt': 'original'})
    payload = '历史验证日志。' * 80_000
    events = []
    for index in range(18):
        event = Event('projection:' + str(index), 'projection_saved', {
            'name': 'evidence', 'blob': blob, 'previous': blob if index else None, 'description': payload})
        events.append(event)
        store.apply('workflow', index + 1, event)
    return store, events


def test_new_history_stores_digests_and_compressed_events(tmp_path):
    store, events = history(tmp_path)
    assert store.path.stat().st_size < 5_000_000
    with store.connect() as db:
        row = db.execute('SELECT envelope,result FROM kernel_events WHERE revision=2').fetchone()
    assert row['result'].startswith(PROJECTION)
    assert row['envelope'].startswith(ENCODING)
    assert json.loads(unpack(row['envelope'])) == events[0].to_dict()
    assert store.replay('workflow') == store.load('workflow')


def test_duplicate_old_event_returns_original_revision_without_spending(tmp_path):
    store, events = history(tmp_path)
    before = store.load('workflow')
    first = store.apply('workflow', 0, events[0])
    assert first['revision'] == 2
    assert store.load('workflow') == before
    last = store.apply('workflow', 0, events[-1])
    assert last == before


def test_migration_preserves_history_and_physically_shrinks_database(tmp_path):
    store, events = history(tmp_path, legacy=True)
    before, before_bytes = store.load('workflow'), store.path.stat().st_size
    assert before_bytes > 25_000_000
    report = compact_history(store, installed=False)
    assert report['ok'] and report['freed_bytes'] > before_bytes * .8
    assert store.path.stat().st_size < before_bytes * .2
    assert KernelStore(store.root).replay('workflow') == before
    assert store.apply('workflow', 0, events[0])['revision'] == 2
    assert compact_history(store, installed=False)['result'] == 'already_compact'


def test_corrupt_legacy_projection_aborts_without_changing_original_database(tmp_path):
    store, _ = history(tmp_path, legacy=True)
    before = store.load('workflow')
    store.set_meta('mode', 'active')
    with store.connect() as db:
        db.execute("UPDATE kernel_events SET result='{}' WHERE revision=5")
    with pytest.raises(KernelError, match='projection does not match'):
        compact_history(store, installed=False)
    assert store.meta('mode') == 'active' and store.meta('kernel_storage_format') == 1
    assert store.load('workflow') == before


def test_compressed_event_corruption_is_detected():
    retained = pack(json.dumps({'reason': '大日志' * 100_000}, ensure_ascii=False))
    parts = retained.split(':', 2)
    with pytest.raises(KernelError, match='integrity failure'):
        unpack(parts[0] + ':' + '0' * 64 + ':' + parts[2])


def test_new_compaction_bounds_previously_referenced_output_without_changing_legacy_replay():
    value = {'version': 1, 'command_id': 'command', 'manifest': 'manifest',
             'details_ref': 'a' * 64, 'reason': 'large reason ' * 100_000, 'checks': {
                 'failed': {'id': 'failed', 'status': 'failed', 'baseline': True, 'detail': 'detail ' * 100_000},
                 'passed': {'id': 'passed', 'status': 'passed', 'command': 'large command'}}}
    assert compact(value) is value
    reduced = compact_for_storage(value)
    assert len(reduced['reason']) == 1200 and len(reduced['checks']['failed']['detail']) == 600
    assert reduced['details_ref'] == value['details_ref']
    assert reduced['checks']['failed']['baseline'] and reduced['checks']['passed']['status'] == 'passed'
    assert compact(reduced) == reduced
