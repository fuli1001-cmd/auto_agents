"""Collect unreferenced kernel objects; keep authority, pending work and proofs."""
import json
import os
from pathlib import Path
import re
import time
from contextlib import contextmanager

from .model import require
from .upgrade import MANDATORY_CHECKS


@contextmanager
def _collection_lock(store):
    from .runtime_manager import adoption_lock
    from ..artifact_store import alive
    with adoption_lock(store):
        with store.connect() as db:
            require(not any(alive(json.loads(r[0])) for r in db.execute(
                "SELECT owner FROM kernel_runtime_uses WHERE purpose IN ('business','verifier')")),
                'storage_busy', 'Live consumers prevent evidence collection')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'jobs' in tables:
                require(not db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed','blocked','cancelled','failed')").fetchone(),
                        'storage_busy', 'Active legacy repair prevents evidence collection')
        prior = store.meta('mode', 'staged')
        store.set_meta('mode', 'draining')
        try: yield
        finally:
            if store.meta('mode') == 'draining': store.set_meta('mode', prior)


def collect_objects(store, *, grace_seconds=600, deadline=None):
    from ..artifact_store import alive
    reachable, visited, pending = set(), set(), []
    def expired(): return deadline is not None and time.monotonic() >= deadline
    def walk(value, keep_logs=False):
        if isinstance(value, dict):
            gate = value.get('ok') is True and value.get('check') in MANDATORY_CHECKS
            for key, item in value.items():
                if key == 'log' and gate and not keep_logs: continue
                walk(item, keep_logs)
        elif isinstance(value, list):
            for item in value: walk(item, keep_logs)
        elif isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value):
            path = store.root / 'kernel-objects' / value[:2] / value
            if path.is_file() and not path.is_symlink():
                reachable.add(value)
                if (value, keep_logs) not in visited: pending.append((value, keep_logs))
    with _collection_lock(store):
        with store.connect() as db:
            require(not any(alive(json.loads(r[0])) for r in db.execute(
                "SELECT owner FROM kernel_runtime_uses WHERE purpose IN ('business','verifier')")),
                'storage_busy', 'Live consumers prevent evidence collection')
            require(not db.execute("SELECT 1 FROM kernel_outbox WHERE state IN ('pending','running','unknown')").fetchone(),
                    'storage_busy', 'Unsettled effects prevent evidence collection')
            for row in db.execute('SELECT key,value FROM kernel_meta'):
                walk(json.loads(row['value']), row['key'] in ('activation_receipt','journal_compaction_receipt'))
            for row in db.execute('SELECT snapshot FROM kernel_streams'): walk(json.loads(row[0]))
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('kernel_results','kernel_migrations','jobs','subscribers','outbox',
                          'verification_contexts','verifications'):
                if table not in tables: continue
                columns = {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}
                for column in columns & {'payload','result','receipt','manifest'}:
                    for row in db.execute('SELECT ' + column + ' FROM ' + table):
                        if not row[0]: continue
                        try: walk(json.loads(row[0]))
                        except (ValueError, TypeError): walk(row[0])
        # Retained legacy checkpoints can refer to objects outside the current
        # native projections, including incomplete repair and delivery receipts.
        files = list((store.root / 'jobs').glob('*/*.json'))
        files += list((store.root / 'v2-transactions').glob('*/state.json'))
        files += list((store.root / 'kernel-engine').glob('*/state.json'))
        operator = store.root / 'operator.json'
        if operator.exists(): files.append(operator)
        for path in files:
            if expired(): return {'ok':False,'result':'deferred','reason':'evidence_mark_budget','freed_bytes':0}
            if path.is_symlink() or path.name.endswith('.output.json'): continue
            try: walk(json.loads(path.read_text()))
            except (ValueError, UnicodeError): pass
        while pending:
            if expired(): return {'ok':False,'result':'deferred','reason':'evidence_mark_budget','freed_bytes':0}
            identity, keep_logs = pending.pop()
            if (identity, keep_logs) in visited: continue
            visited.add((identity, keep_logs))
            with store.blob_path(identity).open('rb') as stream: first = stream.read(1)
            if first not in (b'{', b'[', b'"'): continue
            try: walk(store.read(identity), keep_logs)
            except (ValueError, UnicodeError): pass  # Opaque outputs have no object references.
        freed, removed, now = 0, 0, time.time()
        for path in (store.root / 'kernel-objects').glob('*/*'):
            if expired(): break
            if (path.is_symlink() or not re.fullmatch('[a-f0-9]{64}', path.name)
                    or path.parent.name != path.name[:2] or path.name in reachable): continue
            info = path.stat()
            if info.st_uid != os.getuid() or now - info.st_mtime < grace_seconds: continue
            path.unlink()
            freed += info.st_blocks * 512 if info.st_nlink == 1 else 0
            removed += 1
        report = {'ok': True, 'removed_objects': removed, 'retained_objects':len(reachable), 'freed_bytes':freed}
        store.set_meta('kernel_object_collection', report)
        return report
