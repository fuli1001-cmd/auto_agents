"""Shrink positively owned stopped repair copies without removing their inputs."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import uuid4

from .artifact_store import _identity, _parents

DATABASES = {'release_jobs.sqlite3', 'gate_baseline_cache.sqlite3', 'requirements_audit_cache.sqlite3'}


def copy_snapshot_file(source, target, copy_function):
    source, target = Path(source), Path(target)
    if ('checkpoint_blobs' in source.parts and not source.is_symlink()
            and re.fullmatch('[a-f0-9]{64}', source.name)):
        value = hashlib.sha256()
        with source.open('rb') as incoming:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b''): value.update(chunk)
        if value.hexdigest() == source.name:
            temporary = target.with_name('.immutable-' + uuid4().hex)
            try:
                os.link(source, temporary, follow_symlinks=False)
                os.replace(temporary, target)
                return str(target)
            except OSError:
                pass  # Cross-filesystem snapshots still preserve the bytes.
            finally:
                temporary.unlink(missing_ok=True)
    return copy_function(source, target)


def compact_database(path, deadline):
    """Keep active failures exact; retire only superseded release prose."""
    from .recovery.journal_storage import pack, unpack
    path = Path(path)
    _identity(path); _parents(path)
    before = path.stat().st_size
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)) as db:
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if path.name == 'release_jobs.sqlite3':
            if not {'release_jobs', 'release_worker'} <= tables: return 0
            with db:
                db.execute('BEGIN IMMEDIATE')
                for identity, status, failure, reason in db.execute(
                        'SELECT job_id,status,failure_payload,reason FROM release_jobs').fetchall():
                    retained = '{}' if status == 'superseded' else pack(unpack(failure))
                    summary = reason[:1200]
                    if retained != failure or summary != reason:
                        db.execute('UPDATE release_jobs SET failure_payload=?,reason=? WHERE job_id=?',
                                   (retained, summary, identity))
        elif not (tables & {'command_entries', 'file_matches', 'gate_proof_certificates'}):
            return 0
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and checkpoint[0]: return 0
        free = db.execute('PRAGMA freelist_count').fetchone()[0]
        if free > 32:
            db.execute('VACUUM')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('compacted SQLite failed integrity check')
    return max(0, before - path.stat().st_size)


def deduplicate_blob(path, known):
    """Only immutable, hash-named checkpoint blobs may share an inode."""
    path = Path(path)
    if path.is_symlink() or not re.fullmatch('[a-f0-9]{64}', path.name): return 0
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid(): return 0
    if hashlib.sha256(path.read_bytes()).hexdigest() != path.name: return 0
    key = (path.name, before.st_dev, before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode))
    original = known.get(key)
    if original is None or not original.exists():
        known[key] = path
        return 0
    prior = original.stat()
    if prior.st_ino == before.st_ino: return 0
    _parents(path); _parents(original)
    temporary = path.with_name('.dedup-' + uuid4().hex)
    try:
        os.link(original, temporary, follow_symlinks=False)
        current = path.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError('immutable blob changed during deduplication')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return before.st_blocks * 512 if before.st_nlink == 1 else 0


def compact_repair_copies(store, roots, deadline, record):
    try:
        return _compact_repair_copies(store, roots, deadline, record)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        record({'result':'deferred','reason':'copy_compaction: '+str(error),'freed_bytes':0})
        return False


def _compact_repair_copies(store, roots, deadline, record):
    from .artifact_legacy import quiescent, repair_lock, enclosing_protection
    from .repair_v2.docker import container_mounts
    present = False
    for root in roots:
        for pattern in ('*/evidence', '*/working-evidence', '*/continuous/target-evidence', '*/subscriber-*/evidence'):
            if any(_has_compactable_data(copy / '.auto-agents/state')
                   for copy in (root / 'jobs').glob(pattern)):
                present = True; break
        if present: break
    if not present: return True
    try: mounts = container_mounts()
    except (OSError, RuntimeError):
        record({'result': 'deferred', 'reason': 'copy_compaction_consumers_unknown', 'freed_bytes': 0})
        return False
    known, complete = {}, True
    for root in roots:
        if time.monotonic() >= deadline: return False
        with repair_lock(root), closing(sqlite3.connect((root / 'control.sqlite3').as_uri() + '?mode=rw',
                                                      uri=True, timeout=.1)) as db:
            jobs = db.execute("SELECT id FROM jobs WHERE state IN ('completed','cancelled','failed') ORDER BY updated").fetchall()
            cursor_key = 'compact-copy-cursor:' + str(root)
            with store.connect() as registry:
                cursor = registry.execute('SELECT data FROM maintenance WHERE key=?', (cursor_key,)).fetchone()
            ids = [r[0] for r in jobs]
            if cursor and json.loads(cursor[0]) in ids:
                offset = ids.index(json.loads(cursor[0])) + 1
                ids = ids[offset:] + ids[:offset]
            for identity in ids:
                if time.monotonic() >= deadline: return False
                if not re.fullmatch('[a-f0-9]{24}', identity): continue
                directory = root / 'jobs' / identity
                if directory.is_symlink() or not directory.is_dir() or not quiescent(directory): continue
                if any(m == directory or directory in m.parents or m in directory.parents for m in mounts): continue
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    state = db.execute('SELECT state FROM jobs WHERE id=?', (identity,)).fetchone()
                    if not state or state[0] not in ('completed','cancelled','failed'): continue
                    # Diagnostic copies and subscriber copies have producer
                    # provenance through this exact control job, never a name
                    # match elsewhere in /tmp or a user's live repository.
                    candidates = [directory / 'evidence', directory / 'working-evidence',
                                  directory / 'continuous/target-evidence']
                    candidates += list(directory.glob('subscriber-*/evidence'))
                    for copy in candidates:
                        if not copy.is_dir() or copy.is_symlink(): continue
                        protection = enclosing_protection(store, copy)
                        if protection and protection != 'registered_resource_uses_normal_retention': continue
                        state_root = copy / '.auto-agents/state'
                        for path in state_root.glob('**/*.sqlite3'):
                            if time.monotonic() >= deadline: return False
                            if path.name not in DATABASES or path.is_symlink(): continue
                            try:
                                freed = compact_database(path, deadline)
                                record({'path':str(path), 'kind':'sqlite', 'result':'compacted', 'freed_bytes':freed})
                            except (OSError, ValueError, sqlite3.Error) as error:
                                complete = False
                                record({'path':str(path), 'result':'deferred', 'reason':str(error), 'freed_bytes':0})
                        for path in (state_root / 'checkpoint_blobs').glob('*/*'):
                            if time.monotonic() >= deadline: return False
                            try:
                                freed = deduplicate_blob(path, known)
                                if freed: record({'path':str(path),'kind':'immutable_blob','result':'deduplicated','freed_bytes':freed})
                            except (OSError, ValueError) as error:
                                complete = False
                                record({'path':str(path),'result':'deferred','reason':str(error),'freed_bytes':0})
                with store.connect(True) as registry:
                    registry.execute('INSERT OR REPLACE INTO maintenance VALUES(?,?)', (cursor_key, json.dumps(identity)))
    return complete


def _has_compactable_data(state_root):
    """Query consumers only when there is database/blob work to protect."""
    if any(path.name in DATABASES and path.is_file() and not path.is_symlink()
           for path in state_root.glob('**/*.sqlite3')):
        return True
    return any(path.is_file() and not path.is_symlink()
               for path in (state_root / 'checkpoint_blobs').glob('*/*'))
