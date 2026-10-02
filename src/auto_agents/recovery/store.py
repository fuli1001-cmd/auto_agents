"""SQLite authority with atomic event/projection/budget/outbox commits."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .model import Event, KernelError, canonical, checksum, digest, require
from .reducer import decide, initial
from .journal_storage import FORMAT, PROJECTION, pack, unpack, projection, stored_projection


def _matches_json(db, table, column, rowid, encoded):
    """Compare all retained bytes without binding another full projection."""
    if hasattr(db, 'blobopen'):
        # SQLite's incremental blob reader also supports TEXT columns. Reading
        # them as UTF-8 bytes preserves canonical JSON's exact comparison.
        with db.blobopen(table, column, rowid, readonly=True) as stored:
            if len(stored) != len(encoded): return False
            for offset in range(0, len(encoded), 1024 * 1024):
                if stored.read(1024 * 1024) != encoded[offset:offset + 1024 * 1024]:
                    return False
            return True
    # Python 3.9/3.10 do not expose SQLite's incremental reader.
    row = db.execute(f'SELECT {column}=? FROM {table} WHERE rowid=?',
                     (encoded.decode(), rowid)).fetchone()
    return bool(row and row[0] == 1)


class KernelStore:
    def __init__(self, root, *, readonly=False):
        self.root = Path(root).resolve()
        self.path = self.root / 'control.sqlite3'
        self.readonly = readonly
        if readonly: return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS kernel_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kernel_streams(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kernel_events(
 stream TEXT NOT NULL, revision INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
 previous TEXT NOT NULL, checksum TEXT NOT NULL, envelope TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(stream, revision));
CREATE TABLE IF NOT EXISTS kernel_outbox(
 id TEXT PRIMARY KEY, stream TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '', receipt TEXT);
CREATE TABLE IF NOT EXISTS kernel_bindings(
 project TEXT NOT NULL, name TEXT NOT NULL, stream TEXT NOT NULL,
 PRIMARY KEY(project, name));
CREATE TABLE IF NOT EXISTS kernel_migrations(
 id TEXT PRIMARY KEY, manifest TEXT NOT NULL, state TEXT NOT NULL, imported_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kernel_results(
 command_id TEXT PRIMARY KEY, receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kernel_project_heads(
 project TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
 PRIMARY KEY(project, kind));
CREATE TABLE IF NOT EXISTS kernel_runtimes(
 id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, artifact TEXT NOT NULL,
 inode TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, trash TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', freed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kernel_runtime_uses(
 token TEXT PRIMARY KEY, runtime TEXT NOT NULL, owner TEXT NOT NULL, purpose TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kernel_runtime_adoptions(
 id TEXT PRIMARY KEY, payload TEXT NOT NULL);
''')
            if not db.execute("SELECT 1 FROM kernel_meta WHERE key='kernel_storage_format'").fetchone():
                legacy = db.execute('SELECT 1 FROM kernel_events LIMIT 1').fetchone()
                db.execute("INSERT INTO kernel_meta VALUES('kernel_storage_format',?)", ('1' if legacy else str(FORMAT),))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect('file:' + str(self.path) + ('?mode=ro' if self.readonly else '?mode=rwc'),
                                     uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            if not self.readonly:
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA synchronous=FULL')
            with connection: yield connection
        finally: connection.close()

    def load(self, stream):
        if not self.path.exists(): return initial()
        with self.connect() as db:
            row = db.execute('SELECT snapshot FROM kernel_streams WHERE id=?', (stream,)).fetchone()
        return json.loads(row['snapshot']) if row else initial()

    def put(self, value):
        return self.put_bytes(canonical(value).encode())

    def put_bytes(self, content):
        require(not self.readonly, 'readonly', 'Read-only store cannot publish evidence')
        name = hashlib.sha256(content).hexdigest()
        directory = self.root / 'kernel-objects' / name[:2]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / name
        if target.exists():
            require(self.read_bytes(name) == content, 'blob', 'Evidence object changed')
            return name
        fd, temporary = tempfile.mkstemp(prefix='.object-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            # No existing object is overwritten, including in concurrent writers.
            try: os.link(temporary, target)
            except FileExistsError: self.read_bytes(name)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        finally: Path(temporary).unlink(missing_ok=True)
        return name

    def put_file(self, source):
        """Stream large candidate preimages without embedding them in state."""
        require(not self.readonly, 'readonly', 'Read-only store cannot publish evidence')
        directory = self.root / 'kernel-objects'; directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.stream-', dir=directory)
        value = hashlib.sha256()
        try:
            with Path(source).open('rb') as incoming, os.fdopen(fd, 'wb') as output:
                before = os.fstat(incoming.fileno())
                for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                    value.update(chunk); output.write(chunk)
                after = os.fstat(incoming.fileno())
                require((before.st_ino, before.st_size, before.st_mtime_ns) ==
                        (after.st_ino, after.st_size, after.st_mtime_ns), 'source_changed', 'Evidence changed while sealing')
                output.flush(); os.fsync(output.fileno())
            name = value.hexdigest(); folder = directory / name[:2]; folder.mkdir(exist_ok=True)
            try: os.link(temporary, folder / name)
            except FileExistsError: self.verify_blob(name)
            descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            return name
        finally: Path(temporary).unlink(missing_ok=True)

    def blob_path(self, name):
        checksum(name)
        path = self.root / 'kernel-objects' / name[:2] / name
        require(not path.is_symlink() and path.resolve().is_relative_to(self.root), 'blob', 'Evidence path escaped its store')
        return path

    def verify_blob(self, name):
        path = self.blob_path(name); value = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
        require(value.hexdigest() == name, 'blob', 'Evidence content does not match its digest')
        return path

    def read_bytes(self, name): return self.verify_blob(name).read_bytes()
    def read(self, name): return json.loads(self.read_bytes(name))

    def apply(self, stream, expected_revision, event, *, expected_epoch=None):
        require(not self.readonly, 'readonly', 'Read-only store cannot change state')
        # A sealed duplicate is independent of expired diagnostic attachments.
        # Epoch and draining fences still precede returning its recorded state.
        with self.connect() as db:
            if db.execute('SELECT 1 FROM kernel_events WHERE event_id=?', (event.event_id,)).fetchone():
                db.execute('BEGIN IMMEDIATE')
                if event.kind == 'command_reserved':
                    mode = db.execute("SELECT value FROM kernel_meta WHERE key='mode'").fetchone()
                    require(mode is None or json.loads(mode[0]) != 'draining',
                            'upgrade_draining', 'New operations are fenced during core adoption')
                if expected_epoch is not None:
                    epoch = db.execute("SELECT value FROM kernel_meta WHERE key='epoch'").fetchone()
                    require(expected_epoch == (json.loads(epoch[0]) if epoch else 0),
                            'stale_epoch', 'Runtime generation changed; worker must rebind')
                previous = db.execute('SELECT stream,revision,envelope,result FROM kernel_events WHERE event_id=?',
                                      (event.event_id,)).fetchone()
                require(previous['stream'] == stream and unpack(previous['envelope']) == canonical(event.to_dict()),
                        'event_collision', 'Event identity was reused with different input')
                if not previous['result'].startswith(PROJECTION): return json.loads(previous['result'])
                return self.replay(stream, through=previous['revision'])
        # Referenced proofs are sealed before a transaction is made visible.
        data = event.data
        proofs = [*data.get('proofs', []), *data.get('outcome', {}).get('evidence', [])]
        if data.get('proof'): proofs.append(data['proof'])
        for proof in proofs: self.verify_blob(proof['blob'])
        details = data.get('outcome', {}).get('details', {})
        if details.get('verification_observation') is not None:
            from .observations import compact, compact_for_storage
            value = self.read(details['observation_ref'])
            require(details['verification_observation'] in (value, compact(value), compact_for_storage(value)),
                    'verification_observation', 'Executor observation changed after sealing')
        if event.kind == 'recovery_observation_imported':
            from .observations import observation, compact, compact_for_storage
            from .model import Command
            row = self.load(stream)['commands'][data['command_id']]
            command = Command(**{key: row[key] for key in Command.__dataclass_fields__})
            result = self.read(data['result_ref'])
            compact_observation = compact_for_storage if data['observation'].get('compact_version') == 2 else compact
            require(data['observation'] == compact_observation(observation(command, result, verifier=row['runtime'],
                    baseline_aware=data['observation'].get('baseline_aware', False))),
                    'verification_observation', 'Imported observation is not the retained executor result')
        if event.kind == 'recovery_auxiliary_finished': self.verify_blob(data['result_ref'])
        if event.kind == 'recovery_request_rejected':
            from .rejections import request_rejection
            require(request_rejection(self.read(data['result_ref'])) == data['rejection'],
                    'request_rejection', 'Rejection does not match the retained provider response')
        if event.kind == 'recovery_scope_change_requested' or details.get('scope_amendment_required'):
            result = self.read(data['result_ref'] if event.kind == 'recovery_scope_change_requested' else details['native_result'])
            require(result.get('ok') is True and not result.get('cleanup_incomplete'),
                    'scope_review_required', 'Scope amendment requires a completed writer result')
        if event.kind == 'projection_saved': self.verify_blob(data['blob'])
        if event.kind == 'projection_batch_saved':
            for row in data['entries']: self.verify_blob(row['blob'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.kind == 'command_reserved':
                mode = db.execute("SELECT value FROM kernel_meta WHERE key='mode'").fetchone()
                require(mode is None or json.loads(mode['value']) != 'draining',
                        'upgrade_draining', 'New operations are fenced during core adoption')
            if expected_epoch is not None:
                row = db.execute("SELECT value FROM kernel_meta WHERE key='epoch'").fetchone()
                require(expected_epoch == (json.loads(row['value']) if row else 0),
                        'stale_epoch', 'Runtime generation changed; worker must rebind')
            previous = db.execute('SELECT stream,revision,envelope,result FROM kernel_events WHERE event_id=?', (event.event_id,)).fetchone()
            envelope = canonical(event.to_dict())
            if previous:
                require(previous['stream'] == stream and unpack(previous['envelope']) == envelope,
                        'event_collision', 'Event identity was reused with different input')
                if not previous['result'].startswith(PROJECTION): return json.loads(previous['result'])
                current = db.execute('SELECT revision,snapshot FROM kernel_streams WHERE id=?', (stream,)).fetchone()
                if current and current['revision'] == previous['revision']:
                    state = json.loads(current['snapshot'])
                    require(projection(canonical(state).encode()) == previous['result'],
                            'journal', 'Event projection does not match replay')
                    return state
                return self.replay(stream, through=previous['revision'])
            row = db.execute('SELECT snapshot,revision FROM kernel_streams WHERE id=?', (stream,)).fetchone()
            state = json.loads(row['snapshot']) if row else initial()
            require(state['revision'] == expected_revision, 'stale_transition', 'Concurrent transition won; reload state')
            state, commands = decide(state, event)
            prior = db.execute('SELECT checksum FROM kernel_events WHERE stream=? ORDER BY revision DESC LIMIT 1', (stream,)).fetchone()
            previous_hash = prior['checksum'] if prior else ''
            record_hash = digest([stream, state['revision'], previous_hash, event.to_dict()])
            encoded = canonical(state)
            storage = db.execute("SELECT value FROM kernel_meta WHERE key='kernel_storage_format'").fetchone()
            compact = storage and json.loads(storage[0]) >= FORMAT
            db.execute('INSERT INTO kernel_events VALUES(?,?,?,?,?,?,?)',
                       (stream, state['revision'], event.event_id, previous_hash, record_hash,
                        pack(envelope) if compact else envelope,
                        projection(encoded.encode()) if compact else encoded))
            db.execute('INSERT INTO kernel_streams VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,snapshot=excluded.snapshot',
                       (stream, state['revision'], encoded))
            if event.kind == 'projection_saved' and data['name'].startswith('run:'):
                current = db.execute("SELECT name FROM kernel_project_heads WHERE project=? AND kind='run'", (state['project'],)).fetchone()
                require(current is None or current['name'] == data['name'] or data.get('previous') is None,
                        'run_identity', 'An old run cannot replace the selected current run')
                db.execute("INSERT INTO kernel_project_heads VALUES(?,'run',?) ON CONFLICT(project,kind) DO UPDATE SET name=excluded.name",
                           (state['project'],data['name']))
            for command in commands:
                db.execute("INSERT INTO kernel_outbox(id,stream,payload,state) VALUES(?,?,?,'pending')",
                           (command['command_id'], stream, canonical(command)))
            if event.kind == 'command_dispatched':
                updated = db.execute("UPDATE kernel_outbox SET state='running',epoch=?,owner=? WHERE id=? AND state='pending'",
                                     (data['epoch'], data['owner'], data['command_id']))
                require(updated.rowcount == 1, 'dispatch_duplicate', 'Outbox dispatch already claimed')
            if event.kind == 'command_finished':
                outcome = data['outcome']
                db.execute('UPDATE kernel_outbox SET state=?,receipt=? WHERE id=?',
                           ('unknown' if outcome['kind'] == 'outcome_unknown' else 'finished', canonical(outcome), data['command_id']))
            if event.kind == 'command_reconciled':
                db.execute('UPDATE kernel_outbox SET epoch=?,owner=? WHERE id=?',
                           (data['epoch'],data['owner'],data['command_id']))
            if event.kind == 'workflow_stopped' and data['status'] == 'cancelled':
                db.execute("UPDATE kernel_outbox SET state='cancelled' WHERE stream=? AND state='pending'", (stream,))
            return state

    def replay(self, stream, *, through=None):
        from .reducer import _decide_owned
        state, previous = initial(), ''
        with self.connect() as db:
            # Keep one private state and read every full retained projection in
            # bounded chunks. Neither event nor projection checks are skipped.
            for row in db.execute('SELECT rowid,revision,envelope,previous,checksum FROM kernel_events '
                                  'WHERE stream=? AND (? IS NULL OR revision<=?) ORDER BY revision', (stream, through, through)):
                raw = json.loads(unpack(row['envelope'])); event = Event(**raw)
                require(row['revision'] == state['revision'] + 1 and row['previous'] == previous
                        and row['checksum'] == digest([stream, row['revision'], previous, raw]),
                        'journal', 'Recovery journal integrity failure')
                state, _ = _decide_owned(state, event)
                encoded = canonical(state).encode()
                expected = stored_projection(db, row['rowid'])
                require(hashlib.sha256(encoded).hexdigest() == expected if expected else
                        _matches_json(db, 'kernel_events', 'result', row['rowid'], encoded),
                        'journal', 'Event projection does not match replay')
                del encoded
                previous = row['checksum']
            encoded = canonical(state).encode()
            if through is not None:
                require(state['revision'] == through, 'journal', 'Recovery journal revision is missing')
                return json.loads(encoded)
            final = db.execute('SELECT rowid FROM kernel_streams WHERE id=?', (stream,)).fetchone()
            require(_matches_json(db, 'kernel_streams', 'snapshot', final['rowid'], encoded)
                    if final else state == initial(),
                    'projection', 'Stored state differs from event replay')
        return json.loads(encoded)

    def bind(self, project, name, stream):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT stream FROM kernel_bindings WHERE project=? AND name=?', (str(Path(project).resolve()), name)).fetchone()
            require(row is None or row['stream'] == stream, 'binding', 'Control record belongs to another workflow')
            db.execute('INSERT OR IGNORE INTO kernel_bindings VALUES(?,?,?)', (str(Path(project).resolve()), name, stream))

    def binding(self, project, name):
        with self.connect() as db:
            row = db.execute('SELECT stream FROM kernel_bindings WHERE project=? AND name=?', (str(Path(project).resolve()), name)).fetchone()
        return row['stream'] if row else None

    def meta(self, key, default=None):
        if not self.path.exists(): return default
        with self.connect() as db:
            try: row = db.execute('SELECT value FROM kernel_meta WHERE key=?', (key,)).fetchone()
            except sqlite3.OperationalError: return default
        return json.loads(row['value']) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute('INSERT INTO kernel_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, canonical(value)))

    def status(self):
        with self.connect() as db:
            streams = [json.loads(row['snapshot']) for row in db.execute('SELECT snapshot FROM kernel_streams ORDER BY id')]
            active = [dict(row) for row in db.execute("SELECT id,stream,state,epoch FROM kernel_outbox WHERE state NOT IN ('finished','cancelled')")]
            runtimes = ([dict(row) for row in db.execute('SELECT id,path,kind,state,error,freed FROM kernel_runtimes')]
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='kernel_runtimes'").fetchone() else [])
        return {'schema': 1, 'mode': self.meta('mode', 'staged'), 'active_runtime': self.meta('active_runtime'),
                'workflows': streams, 'operations': active,
                'runtimes': runtimes, 'runtime_cleanup': self.meta('runtime_cleanup')}

    def frontier(self):
        with self.connect() as db:
            return digest({row['id']:row['revision'] for row in db.execute('SELECT id,revision FROM kernel_streams')})
