"""Project-owned business records and call receipts, independent of maintenance.

Display JSON is a projection. SQLite transactions own records and reserve
external calls before dispatch. Legacy installation data is read only during
an explicit, backed-up migration.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid

NOT_MANAGED = object()
_subject = ContextVar('business_subject', default='')
_references = ContextVar('business_references', default=None)
_depth = ContextVar('business_entry_depth', default=0)
_record_owner = ContextVar('business_record_owner', default=None)
LEGACY_MARKER = '.auto-agents/state/recovery-kernel.json'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class BusinessStateError(RuntimeError):
    def __init__(self, code, reason, **details):
        super().__init__(reason)
        self.code, self.details = code, details


class Projection(dict):
    def __init__(self, value, reference):
        super().__init__(value)
        self.reference = reference


def record_location(path):
    path = Path(path).absolute()
    for parent in path.parents:
        if parent.name == 'state' and parent.parent.name == '.auto-agents':
            relative = path.relative_to(parent).as_posix()
            if (relative == 'run_state.json' or relative.startswith(('sessions/', 'workflows/', 'handoffs/'))
                    and path.suffix == '.json'):
                project = parent.parent.parent
                owner = _record_owner.get()
                if owner and project.resolve() == owner[1].resolve():
                    project = owner[0]
                return project, relative
    return None


class BusinessStore:
    def __init__(self, project, *, readonly=False):
        self.project = Path(project).resolve()
        self.root = self.project / '.auto-agents/state'
        self.path = self.root / 'business.sqlite3'
        self.readonly = readonly
        if readonly:
            return
        if (self.project / LEGACY_MARKER).exists():
            raise BusinessStateError('migration_required',
                'Legacy business state must be migrated with auto-agents migrate-state before execution')
        self.root.mkdir(parents=True, exist_ok=True)
        ignore = self.project / '.auto-agents/.gitignore'
        patterns = ['state/business.sqlite3*', 'state/run.lock*', 'state/run.processes*',
                    'state/resume-checkpoints/', 'state/legacy-archive/']
        existing = ignore.read_text() if ignore.exists() else ''
        missing = [item for item in patterns if item not in existing.splitlines()]
        if missing:
            ignore.write_text(existing.rstrip('\n') + '\n' + '\n'.join(missing) + '\n')
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS records(path TEXT PRIMARY KEY, payload TEXT NOT NULL, reference TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, subject TEXT NOT NULL, phase TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT, model INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect('file:' + str(self.path) + ('?mode=ro' if self.readonly else '?mode=rwc'),
                             uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if not self.readonly:
                db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def get(self, path):
        if not self.path.exists():
            return None
        with self.connect() as db:
            row = db.execute('SELECT payload,reference FROM records WHERE path=?', (path,)).fetchone()
        if row and hashlib.sha256(row['payload'].encode()).hexdigest() != row['reference']:
            raise BusinessStateError('record_integrity', 'Business record checksum differs', path=path)
        return Projection(json.loads(row['payload']), row['reference']) if row else None

    def save(self, path, value, expected=None):
        encoded = canonical(value)
        reference = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT reference FROM records WHERE path=?', (path,)).fetchone()
            if row and row['reference'] != expected:
                raise BusinessStateError('stale_projection', 'Business record changed after it was read', path=path)
            db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET '
                       'payload=excluded.payload,reference=excluded.reference', (path, encoded, reference))
        return reference

    def reserve(self, identity, subject, phase, *, model):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM calls WHERE id=?', (identity,)).fetchone()
            if row:
                return dict(row)
            if model and db.execute("SELECT 1 FROM calls WHERE state IN ('dispatched','unknown') AND model=1").fetchone():
                raise BusinessStateError('outcome_unknown', 'Reconcile the retained external request before another call')
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?)',
                       (identity, subject, phase, 'dispatched', None, int(model), time.time()))
        return None


    def settle(self, identity, result, *, state='finished'):
        with self.connect() as db:
            db.execute('UPDATE calls SET state=?,result=? WHERE id=?', (state, canonical(result), identity))

    def pending(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM calls WHERE state IN ('dispatched','unknown') AND model=1")]


def reference_key(path):
    location = record_location(path)
    if location:
        project, relative = location
        return str((project / '.auto-agents/state' / relative).resolve())
    return str(Path(path).resolve())



def bind_model(model, payload, path=None):
    if isinstance(payload, Projection):
        model._business_reference = payload.reference
        model._kernel_reference = payload.reference
        if path is not None:
            model._business_references = {reference_key(path): payload.reference}
    return model


def save_model(path, model):
    key = reference_key(path)
    references = dict(getattr(model, '_business_references', {}))
    # A model can be written to both a private checkout and its owning project.
    # References belong to the destination, not to the Python object as a whole.
    reference = write_projection(path, model.to_dict(), expected=references.get(key))
    if reference:
        references[key] = reference
        model._business_references = references
        model._business_reference = reference
        model._kernel_reference = reference
    return bool(reference)


def read_projection(path):
    location = record_location(path)
    if not location:
        return NOT_MANAGED
    project, relative = location
    if (project / LEGACY_MARKER).exists():
        raise BusinessStateError('migration_required', 'Migrate the legacy database before reading business projections')
    value = BusinessStore(project, readonly=True).get(relative)
    if value is None:
        return NOT_MANAGED
    refs = dict(_references.get() or {})
    refs[reference_key(path)] = value.reference
    _references.set(refs)
    return value


def write_projection(path, value, *, expected=None):
    location = record_location(path)
    if not location:
        return False
    project, relative = location
    if expected is None:
        expected = (_references.get() or {}).get(reference_key(path))
    reference = BusinessStore(project).save(relative, value, expected)
    refs = dict(_references.get() or {})
    refs[reference_key(path)] = reference
    _references.set(refs)
    from .io_utils import _atomic_write
    _atomic_write(Path(path), canonical(value) + '\n')
    from .supervision_api import observe_record
    observe_record(project, relative, value)
    return reference


def append_workflow_event(project, snapshot, payload, path):
    store = BusinessStore(project)
    relative = 'workflows/' + snapshot.workflow_id + '/workflow.json'
    event = Path(path).relative_to(store.root).as_posix()
    value = snapshot.to_dict()
    encoded = canonical(value)
    reference = hashlib.sha256(encoded.encode()).hexdigest()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT payload,reference FROM records WHERE path=?', (relative,)).fetchone()
        if previous and previous['reference'] != getattr(snapshot, '_kernel_reference', None):
            raise BusinessStateError('stale_projection', 'Workflow changed before event publication')
        if previous and payload['sequence'] != json.loads(previous['payload'])['event_sequence'] + 1:
            raise BusinessStateError('workflow_sequence', 'Workflow event is not the next durable sequence')
        db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET '
                   'payload=excluded.payload,reference=excluded.reference', (relative, encoded, reference))
        db.execute('INSERT INTO records VALUES(?,?,?)', (event, canonical(payload), digest(payload)))
    snapshot._business_reference = snapshot._kernel_reference = reference
    references = dict(getattr(snapshot, '_business_references', {}))
    references[str((store.root / relative).resolve())] = reference
    snapshot._business_references = references
    from .io_utils import _atomic_write
    _atomic_write(store.root / relative, encoded + '\n')
    _atomic_write(Path(path), canonical(payload) + '\n')
    return True


def workflow_events(project, workflow_id):
    store = BusinessStore(project, readonly=True)
    if not store.path.exists():
        return None
    with store.connect() as db:
        rows = db.execute('SELECT payload,reference FROM records WHERE path LIKE ?',
                          ('workflows/' + workflow_id + '/events/%',)).fetchall()
    for row in rows:
        if hashlib.sha256(row['payload'].encode()).hexdigest() != row['reference']:
            raise BusinessStateError('workflow_integrity', 'Workflow hash chain is invalid')
    records = sorted((json.loads(row['payload']) for row in rows), key=lambda row: row['sequence'])
    previous = ''
    for sequence, record in enumerate(records, 1):
        material = {k:v for k,v in record.items() if k != 'event_sha256'}
        expected = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if record['sequence'] != sequence or record['previous_event_sha256'] != previous or record['event_sha256'] != expected:
            raise BusinessStateError('workflow_integrity', 'Workflow hash chain is invalid')
        previous = expected
    head = store.get('workflows/' + workflow_id + '/workflow.json')
    if head and (head['event_sequence'] != len(records) or head['last_event_sha256'] != previous):
        raise BusinessStateError('workflow_integrity', 'Workflow snapshot disagrees with its journal')
    return records if rows else None


def installed(project):
    """Retired kernel dispatch is never admitted; records use BusinessStore."""
    return None


def entry(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if _depth.get():
            return method(self, *args, **kwargs)
        subject = ''
        if method.__name__ == 'resume' and args and isinstance(args[0], str):
            subject = 'session:' + args[0]
            getattr(self, 'orch', self)._kernel_subject = subject
        token = _subject.set(subject)
        depth = _depth.set(1)
        refs = _references.set(dict(_references.get() or {}))
        try:
            return method(self, *args, **kwargs)
        finally:
            _references.reset(refs)
            _depth.reset(depth)
            _subject.reset(token)
    return wrapped


def export_snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    store = BusinessStore(source, readonly=True)
    if store.path.exists():
        copied = destination / '.auto-agents/state/business.sqlite3'
        copied.unlink(missing_ok=True)
        with store.connect() as db:
            # SQLite backup includes call receipts as well as records. Copying
            # only JSON would lose dispatch identity and replay settled calls.
            with sqlite3.connect(copied) as target:
                db.backup(target)
            rows = list(db.execute('SELECT path,payload FROM records'))
        from .io_utils import _atomic_write
        for row in rows:
            _atomic_write(destination / '.auto-agents/state' / row['path'], row['payload'] + '\n')
    (destination / LEGACY_MARKER).unlink(missing_ok=True)


def _migrate(project, *, check=False, export=None):
    """Copy hydrated records and external-operation receipts, then switch locally."""
    project = Path(project).resolve()
    marker = project / LEGACY_MARKER
    if not marker.exists():
        return {'ok': True, 'status': 'already_local', 'project': str(project)}
    binding = json.loads(marker.read_text())
    if binding.get('project') != str(project):
        raise BusinessStateError('migration_owner', 'Legacy state belongs to another project')
    control = Path(binding['control_root'])
    db = sqlite3.connect('file:' + str(control / 'control.sqlite3') + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    streams = [json.loads(row['snapshot']) for row in db.execute(
        'SELECT snapshot FROM kernel_streams WHERE id IN (SELECT DISTINCT stream FROM kernel_bindings WHERE project=?)',
        (str(project),))]
    objects = set()
    def blob(identity):
        objects.add(identity)
        path = control / 'kernel-objects' / identity[:2] / identity
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != identity:
            raise BusinessStateError('migration_evidence', 'Legacy object checksum differs', object=identity)
        return json.loads(raw)
    def hydrate(value):
        if isinstance(value, dict):
            if set(value) == {'$kernel_object'}:
                return hydrate(blob(value['$kernel_object']))
            return {k: hydrate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [hydrate(v) for v in value]
        return value
    records, calls = {}, []
    for stream in streams:
        for name, row in stream['projections'].items():
            value = hydrate(blob(row['blob']))
            if name.startswith('session:'):
                relative = 'sessions/' + name.split(':', 1)[1] + '/session_state.json'
            elif name.startswith('issue:'):
                relative = 'sessions/' + name.split(':', 1)[1] + '/issue.json'
            elif name.startswith('workflow:'):
                relative = 'workflows/' + name.split(':', 1)[1] + '/workflow.json'
            elif name.startswith('handoff:'):
                relative = 'handoffs/' + name.split(':', 1)[1] + '.json'
            elif name.startswith('event:'):
                _, wf, event = name.split(':', 2)
                relative = 'workflows/' + wf + '/events/' + event + '.json'
            elif name.startswith('run:'):
                heads = db.execute('SELECT name FROM kernel_project_heads WHERE project=? AND kind=?',
                                   (str(project), 'run')).fetchone()
                if heads and heads['name'] != name:
                    continue
                relative = 'run_state.json'
            else:
                continue
            if relative in records and records[relative] != value:
                raise BusinessStateError('migration_ambiguous', 'Conflicting authoritative business records', path=relative)
            records[relative] = value
        for command in stream['commands'].values():
            task = stream['tasks'].get(command['task_id'], {})
            if task.get('contract', {}).get('kind') == 'engine_repair' or not command.get('model_call'):
                continue
            outcome = command.get('outcome') or {}
            reference = outcome.get('details', {}).get('native_result')
            result = hydrate(blob(reference)) if reference else None
            settled = command['status'] == 'finished'
            if settled and not reference:
                # Cancellation/refusal is a known terminal result, never a
                # pending external effect or permission to repeat that call.
                result = {'legacy_terminal': outcome, 'redispatch_allowed': False}
            calls.append((command['operation_key'], task.get('contract', {}).get('parent_task', command['task_id']),
                          command['phase'], 'finished' if settled else 'unknown',
                          canonical(result) if result is not None else None, 1, time.time()))
    report = {'ok': True, 'project': str(project), 'records': len(records), 'calls': len(calls),
              'identity': digest(records), 'pending_calls': sum(row[3] == 'unknown' for row in calls)}
    if check:
        db.close()
        if export is not None:
            destination = Path(export)
            (destination / LEGACY_MARKER).unlink(missing_ok=True)
            target_store = BusinessStore(destination)
            with target_store.connect() as target:
                target.executemany('INSERT OR REPLACE INTO records VALUES(?,?,?)',
                    [(key,canonical(value),digest(value)) for key,value in records.items()])
                target.executemany('INSERT OR IGNORE INTO calls VALUES(?,?,?,?,?,?,?)', calls)
            from .io_utils import _atomic_write
            for relative,value in records.items():
                _atomic_write(destination/'.auto-agents/state'/relative,canonical(value)+'\n')
        return report
    state = project / '.auto-agents/state'
    backup = state / 'legacy-archive' / uuid.uuid4().hex
    backup.mkdir(parents=True)
    with sqlite3.connect(backup / 'control.sqlite3') as target:
        db.backup(target)
    shutil.copy2(marker, backup / marker.name)
    for identity in objects:
        original = control / 'kernel-objects' / identity[:2] / identity
        archived = backup / 'kernel-objects' / identity[:2] / identity
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, archived)
    temporary = state / ('business-' + uuid.uuid4().hex + '.sqlite3')
    with sqlite3.connect(temporary) as target:
        target.executescript('CREATE TABLE records(path TEXT PRIMARY KEY,payload TEXT NOT NULL,reference TEXT NOT NULL);'
            'CREATE TABLE calls(id TEXT PRIMARY KEY,subject TEXT NOT NULL,phase TEXT NOT NULL,state TEXT NOT NULL,'
            'result TEXT,model INTEGER NOT NULL,created REAL NOT NULL);CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);')
        target.executemany('INSERT INTO records VALUES(?,?,?)',
                           [(key, canonical(value), digest(value)) for key, value in records.items()])
        target.executemany('INSERT OR IGNORE INTO calls VALUES(?,?,?,?,?,?,?)', calls)
        target.execute('INSERT INTO metadata VALUES(?,?)', ('legacy_import', canonical(report)))
    with temporary.open('rb') as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, state / 'business.sqlite3')
    marker.unlink()
    from .io_utils import _atomic_write
    for relative, value in records.items():
        _atomic_write(state / relative, canonical(value) + '\n')
    _atomic_write(backup / 'migration.json', canonical(report) + '\n')
    db.close()
    return {**report, 'status': 'migrated', 'backup': str(backup)}


def migrate(project, *, check=False):
    if check:
        return _migrate(project, check=True)
    # Fence both the retired and current project lock before reading authority.
    import fcntl
    import tempfile
    from contextlib import ExitStack
    from .run_lock import ProjectRunLock
    project=Path(project).resolve()
    legacy=Path(tempfile.gettempdir())/'auto-agents-run-locks'/(hashlib.sha256(str(project).encode()).hexdigest()+'.lock')
    with ExitStack() as stack:
        if legacy.exists():
            handle=stack.enter_context(legacy.open('r'))
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stack.enter_context(ProjectRunLock(project))
        return _migrate(project)
