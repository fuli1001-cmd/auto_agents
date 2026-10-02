"""Verified, quiescent conversion of legacy full projections to event digests."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .journal_storage import FORMAT, PROJECTION, compact_state, digest_result, pack, unpack
from .model import Event, canonical, digest, require
from .store import KernelStore


def compact_history(store, *, installed=True, callback=None):
    from .runtime_manager import adoption_lock
    from ..artifact_store import alive
    from ..run_lock import ProjectRunLock
    if installed:
        runtime = store.meta('active_runtime') or {}
        require((Path(runtime.get('path', '/__missing__')) / 'src/auto_agents/recovery/journal_storage.py').is_file(),
                'storage_runtime', 'Adopt the storage-capable runtime before converting history')
    with adoption_lock(store), ExitStack() as locks:
        for project in sorted(store.meta('activation', {}).get('projects', [])):
            locks.enter_context(ProjectRunLock(Path(project)))
        with store.connect() as db:
            owners = [json.loads(r[0]) for r in db.execute(
                "SELECT owner FROM kernel_runtime_uses WHERE purpose IN ('business','verifier')")]
            require(not any(alive(owner) for owner in owners), 'storage_busy', 'Live consumers prevent history compaction')
            require(not db.execute("SELECT 1 FROM kernel_outbox WHERE state IN ('pending','running','unknown')").fetchone(),
                    'storage_busy', 'Unsettled effects prevent history compaction')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
                require(not db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed','blocked','cancelled','failed')").fetchone(),
                        'storage_busy', 'Active legacy repair prevents history compaction')
        if store.meta('kernel_storage_format') == FORMAT:
            return {'ok': True, 'result': 'already_compact', 'freed_bytes': 0}
        prior = store.meta('mode', 'staged')
        before_size, before_frontier = store.path.stat().st_size, store.frontier()
        store.set_meta('mode', 'draining')
        try:
            with tempfile.TemporaryDirectory(prefix='kernel-compact-', dir=store.root) as temporary:
                staged = KernelStore(temporary)
                with store.connect() as source, staged.connect() as target:
                    source.execute('BEGIN')
                    for row in source.execute('SELECT * FROM kernel_streams'):
                        target.execute('INSERT INTO kernel_streams VALUES(?,?,?)', tuple(row))
                    rows = source.execute('SELECT rowid,stream,revision,event_id,previous,checksum,envelope '
                                          'FROM kernel_events ORDER BY stream,revision')
                    count, last_notice = 0, 0.
                    for row in rows:
                        target.execute('INSERT INTO kernel_events VALUES(?,?,?,?,?,?,?)',
                            (row['stream'], row['revision'], row['event_id'], row['previous'], row['checksum'],
                             pack(unpack(row['envelope'])), PROJECTION + digest_result(source, row['rowid'])))
                        count += 1
                        if callback and time.monotonic() - last_notice >= 5:
                            callback({'phase': 'convert', 'events': count})
                            last_notice = time.monotonic()
                with staged.connect() as db:
                    identities = [r[0] for r in db.execute('SELECT id FROM kernel_streams')]
                comparisons = {}
                for identity in identities:
                    if callback: callback({'phase': 'verify', 'stream': identity})
                    original = staged.replay(identity)
                    shortened = compact_state(original)
                    unchanged = ('budget', 'operations', 'continuations', 'adoptions', 'imports',
                                 'workflow_id', 'goal_id', 'project', 'status', 'projections')
                    require(all(original.get(key) == shortened.get(key) for key in unchanged),
                            'storage_compaction', 'Compaction changed an authority or accounting field')
                    if original != shortened:
                        staged.apply(identity, original['revision'], Event(
                            'storage:' + digest([identity, original['revision'], FORMAT]), 'history_compacted',
                            {'format': FORMAT, 'before': digest(original), 'after': digest(shortened)}))
                    final = staged.replay(identity)
                    comparisons[identity] = {'before_revision': original['revision'], 'after_revision': final['revision'],
                                             'budget': original['budget'], 'state_digest': digest(final)}
                require(store.frontier() == before_frontier, 'storage_changed', 'History advanced during conversion')
                evidence = store.put({'schema': 1, 'format': FORMAT, 'before_frontier': before_frontier,
                                      'after_frontier': staged.frontier(), 'streams': comparisons,
                                      'events': count, 'before_bytes': before_size})
                # Replace rows in-place under SQLite's write transaction. Keeping
                # the database inode avoids racing existing supervisor readers.
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    frontier = digest({r['id']: r['revision'] for r in db.execute('SELECT id,revision FROM kernel_streams')})
                    require(frontier == before_frontier, 'storage_changed', 'History changed before compaction commit')
                    db.execute('ATTACH DATABASE ? AS compacted', (str(staged.path),))
                    db.execute('DELETE FROM kernel_events')
                    db.execute('INSERT INTO kernel_events SELECT * FROM compacted.kernel_events')
                    db.execute('DELETE FROM kernel_streams')
                    db.execute('INSERT INTO kernel_streams SELECT * FROM compacted.kernel_streams')
                    for key, value in [('kernel_storage_format', FORMAT), ('journal_compaction_receipt', evidence)]:
                        db.execute('INSERT INTO kernel_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                   (key, canonical(value)))
                    for stream in identities:
                        state = staged.load(stream)
                        for identity, command in state['commands'].items():
                            if command.get('outcome') and command['status'] in ('finished','cancelled'):
                                db.execute("UPDATE kernel_outbox SET receipt=? WHERE id=? AND state IN ('finished','cancelled')",
                                           (canonical(command['outcome']), identity))
                with store.connect() as db:
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    db.execute('VACUUM')
            return {'ok': True, 'result': 'compacted', 'events': count, 'receipt': evidence,
                    'before_bytes': before_size, 'after_bytes': store.path.stat().st_size,
                    'freed_bytes': max(0, before_size - store.path.stat().st_size)}
        finally:
            if store.meta('mode') == 'draining': store.set_meta('mode', prior)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Verify and compact the installed recovery journal')
    parser.add_argument('--control', required=True)
    options = parser.parse_args()
    result = compact_history(KernelStore(options.control), callback=lambda value: print(json.dumps(value), flush=True))
    print(json.dumps(result), flush=True)
