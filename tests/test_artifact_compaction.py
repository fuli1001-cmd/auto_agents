import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from auto_agents.artifact_compaction import compact_database, deduplicate_blob, compact_repair_copies
from auto_agents.recovery.journal_storage import unpack
from test_artifact_storage import store


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'failure': '保留当前失败。' * 90_000, 'checks': ['required']}
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE release_jobs(job_id TEXT PRIMARY KEY,status TEXT,failure_payload TEXT,reason TEXT,attempts INT,updated_at REAL); '
                         'CREATE TABLE release_worker(singleton INT,status TEXT);')
        db.execute('INSERT INTO release_jobs VALUES(?,?,?,?,?,?)',
                   ('old', 'superseded', json.dumps(payload, ensure_ascii=False), 'obsolete' * 100_000, 7, 10))
        db.execute('INSERT INTO release_jobs VALUES(?,?,?,?,?,?)',
                   ('pending', 'needs_user', json.dumps(payload, ensure_ascii=False), 'current', 9, 20))
    return payload


def test_sqlite_compaction_keeps_current_failure_and_accounting_exact(tmp_path):
    path = tmp_path / 'release_jobs.sqlite3'
    payload = database(path)
    before = path.stat().st_size
    freed = compact_database(path, time.monotonic() + 30)
    assert freed > before * .8
    with sqlite3.connect(path) as db:
        old = db.execute("SELECT failure_payload,attempts FROM release_jobs WHERE job_id='old'").fetchone()
        pending = db.execute("SELECT failure_payload,attempts,updated_at FROM release_jobs WHERE job_id='pending'").fetchone()
        assert old == ('{}', 7)
        assert json.loads(unpack(pending[0])) == payload and pending[1:] == (9, 20)
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_only_verified_hash_named_blobs_are_deduplicated(tmp_path):
    data = b'immutable checkpoint bytes' * 10_000
    name = hashlib.sha256(data).hexdigest()
    first, second = tmp_path / 'first' / name, tmp_path / 'second' / name
    for path in (first, second): path.parent.mkdir(); path.write_bytes(data)
    known = {}
    assert deduplicate_blob(first, known) == 0
    assert deduplicate_blob(second, known) > 0
    assert first.stat().st_ino == second.stat().st_ino and first.read_bytes() == data
    first.unlink()
    assert second.read_bytes() == data
    wrong = tmp_path / 'wrong' / name; wrong.parent.mkdir(); wrong.write_bytes(b'changed')
    assert deduplicate_blob(wrong, known) == 0 and wrong.read_bytes() == b'changed'
    ordinary = tmp_path / 'source.py'; ordinary.write_bytes(data)
    assert deduplicate_blob(ordinary, known) == 0
