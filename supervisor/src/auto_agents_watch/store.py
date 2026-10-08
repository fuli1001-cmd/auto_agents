"""One maintenance state machine and durable pre-dispatch counters."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import time
import uuid

STATES = {'RUNNING', 'REPAIRING', 'VERIFYING', 'RESTARTING', 'DONE', 'STOPPED'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('AUTO_AGENTS_WATCH_ROOT') or
            Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'auto-agents-watch')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'maintenance.sqlite3'
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS attempts(job TEXT,number INTEGER,payload TEXT NOT NULL,PRIMARY KEY(job,number));')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db: yield db
        finally:
            db.close()

    def create(self, argv, project, engine):
        identity = uuid.uuid4().hex
        value = {'id': identity, 'argv': list(argv), 'project': str(Path(project).resolve()),
                 'cwd':os.getcwd(),
                 'engine': str(Path(engine).resolve()), 'state': 'RUNNING', 'attempts': 0, 'model_calls': 0,
                 'no_progress': 0, 'best_passed': [], 'created': time.time(), 'publication': 'not_requested'}
        with self.connect() as db:
            db.execute('INSERT INTO jobs VALUES(?,?)', (identity, json.dumps(value)))
        (self.root / 'jobs' / identity).mkdir(parents=True)
        return value

    def get(self, identity):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (identity,)).fetchone()
        if row is None: raise ValueError('Unknown maintenance job: ' + identity)
        return json.loads(row[0])

    def successor(self, previous, argv):
        """Replace retired inputs, without granting another maintenance budget."""
        identity = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = json.loads(db.execute('SELECT payload FROM jobs WHERE id=?', (previous['id'],)).fetchone()[0])
            if current.get('active_call'):
                raise RuntimeError('Reconcile the previous model call before replacing maintenance inputs')
            if current.get('superseded_by'):
                return json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',
                    (current['superseded_by'],)).fetchone()[0])
            if current['state'] != 'STOPPED':
                raise RuntimeError('Only a stopped maintenance job can be replaced')
            value = {'id': identity, 'argv': list(argv), 'project': current['project'], 'engine': current['engine'],
                     'cwd': os.getcwd(), 'state': 'RUNNING', 'created': time.time(), 'publication': 'not_requested',
                     'predecessor': current['id'],
                     'maintenance_started': current.get('maintenance_started') or current['created'],
                     'cancel_requested': current.get('cancel_requested',False)}
            for key, default in [('attempts',0), ('model_calls',0), ('no_progress',0), ('best_passed',[])]:
                value[key] = current.get(key,default)
            # Old faults, candidates, checkpoints and accepted runtime pointers
            # stay in the predecessor. The successor observes current business.
            (self.root/'jobs'/identity).mkdir(parents=True)
            current['superseded_by'] = identity
            db.execute('INSERT INTO jobs VALUES(?,?)', (identity,json.dumps(value)))
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(current),current['id']))
        return value

    def list(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM jobs')]

    def save(self, job, state=None, **changes):
        if state:
            if state not in STATES: raise ValueError('Invalid maintenance state')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
            current = {**previous, **changes, 'updated':time.time()}
            if state: current['state'] = state
            if previous.get('cancel_requested'):
                current['cancel_requested'] = True
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(current), job['id']))
        job.update(current)

    def reserve(self, job, role, *, max_calls=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = json.loads(db.execute('SELECT payload FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
            if current.get('cancel_requested'):
                raise RuntimeError('Maintenance cancelled; another model dispatch refused')
            if current.get('active_call'):
                raise RuntimeError('Reconcile the previous model call before another dispatch')
            if max_calls is not None and current['model_calls'] >= max_calls:
                raise RuntimeError('Configured model-call limit reached')
            current['model_calls'] += 1
            if role == 'implement': current['attempts'] += 1
            current['active_call'] = {'role':role,'number':current['model_calls'],'dispatched_at':time.time()}
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(current), job['id']))
        job.update(current)

    def settle(self, job, result):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
            call = previous.get('active_call')
            if not call or call != job.get('active_call'):
                raise RuntimeError('Maintenance call changed before settlement')
            db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',
                       (job['id'], call['number'], json.dumps({'call':call,'result':result})))
            previous['active_call'] = None
            db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(previous),job['id']))
        job.update(previous)

    def acknowledge_resume(self, job):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
            current.update(cancel_requested=False,last_resume_requested=time.time())
            db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(current),job['id']))
        job.update(current)

    def progress(self, job, checks, limit=2):
        if any(row.get('status') in {'unknown','environment','not_run'} for row in checks):
            raise RuntimeError('Verification prerequisites or outcomes are unresolved')
        passed = {row['id'] for row in checks if row['status'] == 'passed'}
        best = set(job['best_passed'])
        improved = best < passed
        self.save(job, best_passed=sorted(passed) if improved else sorted(best),
                  no_progress=0 if improved else job['no_progress'] + 1)
        return improved, job['no_progress'] < limit
