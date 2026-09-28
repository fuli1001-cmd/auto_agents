"""Explicit, durable retirement of a stopped V2 repair, without losing evidence."""
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .store import Store, atomic_json, digest
from .transaction import transaction_lock
from .types import RepairBlocked


def verified_abandonment(root, db):
    """A marker alone never releases custody: check state and the control journal."""
    root = Path(root)
    marker = root / 'abandonment.json'
    if not marker.is_file() or marker.is_symlink(): return False
    value = json.loads(marker.read_text())
    if (value.get('schema') != 1 or value.get('transaction') != str(root)
            or not value.get('reason') or not value.get('jobs')): return False
    state = Store(root).load() or {}
    if state.get('status') != 'abandoned' or state.get('abandonment') != digest(value): return False
    Store(root).read(value['previous_state'])
    for job in value['jobs']:
        current = db.execute('SELECT state,generation FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if not current or tuple(current) != (job['target_state'], job['target_generation']): return False
        if db.execute("SELECT 1 FROM subscribers WHERE job=? AND state NOT IN ('finished','cancelled')",
                      (job['id'],)).fetchone(): return False
        if db.execute("SELECT 1 FROM outbox WHERE job=? AND state NOT IN ('published','invalidated','cancelled')",
                      (job['id'],)).fetchone(): return False
        events = db.execute("SELECT payload FROM events WHERE job=? AND kind='transaction_abandoned'",
                            (job['id'],)).fetchall()
        if not any(json.loads(row[0]) == value for row in events): return False
    return True


def abandon(control, identity, reason):
    if not re.fullmatch(r'[a-f0-9]{64}', identity or '') or not reason or not reason.strip():
        raise ValueError('abandon requires an exact --transaction ID and a nonempty --reason')
    from ..artifact_legacy import quiescent
    from ..artifact_store import _identity, _parents
    control = Path(control).absolute()
    root = control / 'v2-transactions' / identity
    _identity(root); _parents(root)
    _identity(control / 'control.sqlite3')
    store = Store(root)
    with transaction_lock(root, allow_abandoned=True), store.locked(), closing(
            sqlite3.connect((control / 'control.sqlite3').as_uri() + '?mode=rw', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        try:
            # Native work may still consume imported legacy evidence. Do not
            # close that custody while any kernel effect is unsettled.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='kernel_outbox'").fetchone():
                if db.execute("SELECT 1 FROM kernel_outbox WHERE state IN ('pending','running','unknown') LIMIT 1").fetchone():
                    raise RepairBlocked('transaction_busy', 'unsettled kernel work prevents retirement')
            marker = root / 'abandonment.json'
            state = store.load()
            if not state: raise RepairBlocked('transaction_state', 'transaction checkpoint is missing')
            if marker.exists():
                value = json.loads(marker.read_text())
                if value.get('schema') != 1 or value.get('transaction') != str(root) or not value.get('jobs'):
                    raise RepairBlocked('transaction_state', 'invalid retirement receipt')
            else:
                if state.get('status') == 'complete':
                    raise RepairBlocked('transaction_complete', 'completed recovery uses automatic pin release')
                jobs = []
                for row in db.execute('SELECT id,state,generation,result FROM jobs'):
                    binding = control / 'jobs' / row['id'] / 'v2-transaction.json'
                    bound = json.loads(binding.read_text()).get('root') if binding.is_file() else None
                    result = json.loads(row['result'])
                    if str(root) not in (bound, result.get('v2_transaction')): continue
                    if row['state'] not in ('blocked', 'cancelled', 'completed'):
                        raise RepairBlocked('transaction_busy', 'cancel the active repair before abandoning it')
                    jobs.append({'id': row['id'], 'state': row['state'], 'generation': row['generation'],
                                 'target_state': 'completed' if row['state'] == 'completed' else 'cancelled',
                                 'target_generation': row['generation'] + (row['state'] == 'blocked')})
                if not jobs: raise RepairBlocked('transaction_owner', 'no control job owns this transaction')
                value = {'schema': 1, 'transaction': str(root), 'reason': reason.strip(),
                         'at': time.time(), 'jobs': jobs, 'previous_state': store.artifact('retirement', state)}
            if not quiescent(root) or any(not quiescent(control / 'jobs' / job['id']) for job in value['jobs']):
                raise RepairBlocked('transaction_busy', 'repair processes are still stopping')
            # This is the crash fence. Every new V2 entry refuses this marker,
            # even if a crash precedes the DB commit or checkpoint update.
            atomic_json(marker, value)
            for job in value['jobs']:
                row = db.execute('SELECT state,generation FROM jobs WHERE id=?', (job['id'],)).fetchone()
                if not row or tuple(row) not in ((job['state'], job['generation']),
                                                 (job['target_state'], job['target_generation'])):
                    raise RepairBlocked('transaction_changed', 'job generation changed during retirement')
                db.execute('UPDATE jobs SET state=?,generation=?,updated=? WHERE id=?',
                           (job['target_state'], job['target_generation'], time.time(), job['id']))
                db.execute("UPDATE subscribers SET state='cancelled',updated=? WHERE job=? AND state!='finished'",
                           (time.time(), job['id']))
                db.execute("UPDATE outbox SET state='cancelled' WHERE job=? AND state NOT IN ('published','invalidated')",
                           (job['id'],))
                rows = db.execute("SELECT payload FROM events WHERE job=? AND kind='transaction_abandoned'", (job['id'],))
                if not any(json.loads(row[0]) == value for row in rows):
                    db.execute('INSERT INTO events(job,kind,payload,created) VALUES(?,?,?,?)',
                               (job['id'], 'transaction_abandoned', json.dumps(value), time.time()))
            db.commit()
            if state.get('status') != 'abandoned':
                store.transition(state, status='abandoned', phase='abandoned', abandonment=digest(value))
            if not verified_abandonment(root, db):
                raise RepairBlocked('transaction_state', 'retirement evidence does not match control state')
            from .images import release
            release(root)
            return {'ok': True, 'status': 'abandoned', 'transaction': identity,
                    'receipt': str(marker), 'evidence_retained': True}
        except BaseException:
            db.rollback()
            raise
