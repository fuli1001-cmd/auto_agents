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
    return True  # Archived legacy repairs are not mutated by normal maintenance.


def _has_compactable_data(state_root):
    """Query consumers only when there is database/blob work to protect."""
    if any(path.name in DATABASES and path.is_file() and not path.is_symlink()
           for path in state_root.glob('**/*.sqlite3')):
        return True
    return any(path.is_file() and not path.is_symlink()
               for path in (state_root / 'checkpoint_blobs').glob('*/*'))
