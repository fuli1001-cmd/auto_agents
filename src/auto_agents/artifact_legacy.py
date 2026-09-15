"""Reclaim positively identified build caches inside stopped repair copies.

This is deliberately not a recursive /tmp or project-directory cleaner. A
controller database, job state, process leases and Git all supply provenance.
"""
from contextlib import contextmanager, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from .artifact_store import _allocated, _identity, _parents, _parent_fd, _remove_at, alive, process_identity


COPIES = ('evidence', 'working-evidence', 'continuous/target-evidence')


def quiescent(directory):
    """Legacy records used both integer and string process birth ticks."""
    try:
        records = [json.loads(p.read_text()) for p in directory.glob('*-lease.json')]
        process_file = directory / 'processes.json'
        if process_file.exists(): records.extend(json.loads(process_file.read_text()).get('processes', []))
        for record in records:
            pid = int(record.get('pid', 0))
            if pid > 0:
                current = process_identity(pid)
                ticks = record.get('ticks', record.get('start_ticks'))
                if ticks is not None and not str(ticks).isdigit(): return False
                if ticks is None or str(ticks) == '0':
                    try: os.kill(pid, 0)
                    except ProcessLookupError: pass
                    else: return False
                elif alive({'pid': pid, 'ticks': str(ticks), 'boot': record.get('boot', current['boot'])}):
                    return False
            pgid = int(record.get('pgid', 0))
            if pgid > 0:
                try: os.killpg(pgid, 0)
                except ProcessLookupError: pass
                else: return False
        return True
    except (OSError, ValueError, TypeError, AttributeError): return False


def repair_roots(store, scope=None):
    rows = store.rows(scope)
    roots = {Path(r['metadata']['repair_root']).absolute() for r in rows if r['metadata'].get('repair_root')}
    # Explicit storage overrides isolate tests/embedded installations from the
    # user's default repair tree. Registered custom roots still participate.
    if scope is None or scope.startswith('repair:'):
        state_home = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
        default_store = (state_home / 'auto-agents/storage').absolute()
        if store.root == default_store or os.environ.get('AUTO_AGENTS_REPAIR_CONTROL_ROOT'):
            parent = Path(os.environ.get('AUTO_AGENTS_REPAIR_CONTROL_ROOT') or
                          str(Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) /
                              'auto-agents/repair-control')).absolute()
            if parent.is_dir() and not parent.is_symlink():
                if (parent / 'operator.json').is_file(): roots.add(parent)
                roots.update(p for p in parent.iterdir() if p.is_dir() and not p.is_symlink()
                             and (p / 'operator.json').is_file())
    accepted = []
    for root in sorted(roots):
        if scope and scope.startswith('repair:') and scope != 'repair:' and scope != 'repair:' + str(root): continue
        try:
            _identity(root); _parents(root)
            _identity(root / 'operator.json'); _identity(root / 'control.sqlite3')
            config = json.loads((root / 'operator.json').read_text())
            if Path(config.get('root', '')).absolute() == root:
                accepted.append(root)
        except (OSError, ValueError, TypeError): continue
    return accepted


@contextmanager
def repair_lock(root):
    path = root / 'repository.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid(): raise ValueError('unowned repair lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def git(root, *args):
    return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                           '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args],
                          capture_output=True, timeout=5)


def disposable_build(project, path, *, resuming=False):
    if path.name != '.next' or not path.is_dir() or path.is_symlink(): return False
    if not resuming and not any((path / name).exists() for name in ('cache', 'BUILD_ID', 'build-manifest.json')): return False
    # The working tree must be this diagnostic copy, never a Git repository
    # discovered by walking into a live parent project.
    top = git(project, 'rev-parse', '--show-toplevel')
    if top.returncode or Path(os.fsdecode(top.stdout).strip()).resolve() != project.resolve(): return False
    relative = path.relative_to(project).as_posix()
    tracked = git(project, 'ls-files', '-z', '--', relative)
    return not tracked.returncode and not tracked.stdout and git(project, 'check-ignore', '-q', '--', relative).returncode == 0


def enclosing_protection(store, path):
    with store.connect() as db:
        rows = [json.loads(r[0]) for r in db.execute(
            "SELECT data FROM artifacts WHERE json_extract(data,'$.state')!='deleted' AND "
            "(path=? OR substr(?,1,length(path)+1)=path||'/' OR substr(path,1,length(?)+1)=?||'/')",
            (str(path), str(path), str(path), str(path)))]
    for row in rows:
        if row['state'] == 'deleted': continue
        owner = Path(row['path'])
        if path == owner or owner in path.parents or path in owner.parents:
            if row['pin'] or row['references'] or any(alive(p) for p in row['leases']):
                return 'enclosing_artifact_in_use_or_pinned'
            if path == owner or path in owner.parents:
                return 'registered_resource_uses_normal_retention'
    return ''


def clean_build(store, db, directory, project, path, deadline, record):
    def retained(reason):
        record({'path': str(path), 'result': 'retained', 'reason': reason, 'freed_bytes': 0})
    reason = enclosing_protection(store, path)
    if reason: retained(reason); return
    row = {'path': str(path), 'inode': _identity(path), 'parents': _parents(path)}
    key = 'legacy-delete:' + hashlib.sha256(str(path).encode()).hexdigest()
    with store.connect() as index:
        saved = index.execute('SELECT data FROM maintenance WHERE key=?', (key,)).fetchone()
    resuming = saved is not None and json.loads(saved[0]) == row
    if not disposable_build(project, path, resuming=resuming):
        retained('unverified_build_output'); return
    size, measured = _allocated(path, min(deadline, time.monotonic() + .5))
    current = db.execute('SELECT state FROM jobs WHERE id=?', (directory.name,)).fetchone()
    if not current or current[0] not in ('cancelled', 'completed', 'failed') or not quiescent(directory):
        retained('job_became_active'); return
    with store.locked(), _parent_fd(row, path) as fd:
        if enclosing_protection(store, path) or not disposable_build(project, path, resuming=resuming):
            retained('ownership_changed'); return
        # Persist authorization before removing any marker. A killed cleaner
        # can finish this same inode after BUILD_ID/cache has already vanished.
        with store.connect(True) as index:
            index.execute('INSERT OR REPLACE INTO maintenance VALUES(?,?)', (key, json.dumps(row)))
        _remove_at(fd, path.name, row['inode'][0], deadline)
        with store.connect(True) as index:
            index.execute('DELETE FROM maintenance WHERE key=?', (key,))
    record({'path': str(path), 'result': 'deleted', 'kind': 'legacy_build_cache',
            'bytes': size, 'size_complete': measured, 'freed_bytes': size})


def clean_legacy(store, roots, deadline, record, scope=None):
    complete = True
    for root in roots:
        if time.monotonic() >= deadline: return False
        try:
            with repair_lock(root), closing(sqlite3.connect((root / 'control.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                jobs = db.execute("SELECT id,state FROM jobs ORDER BY updated,id").fetchall()
                if scope and scope.startswith('project:'):
                    jobs = [(job, state) for job, state in jobs if
                            json.loads(db.execute('SELECT payload FROM jobs WHERE id=?', (job,)).fetchone()[0]).get('project') == scope[8:]]
                cursor_key = 'legacy-cursor:' + hashlib.sha256(str(root).encode()).hexdigest()
                with store.connect() as index:
                    saved = index.execute('SELECT data FROM maintenance WHERE key=?', (cursor_key,)).fetchone()
                cursor = json.loads(saved[0]) if saved else None
                positions = {job: i for i, (job, _) in enumerate(jobs)}
                if cursor in positions:
                    offset = positions[cursor] + 1
                    jobs = jobs[offset:] + jobs[:offset]
                for job, state in jobs:
                    if time.monotonic() >= deadline: return False
                    if not re.fullmatch('[a-f0-9]{24}', job): continue
                    directory = root / 'jobs' / job
                    with store.connect(True) as index:
                        index.execute('INSERT OR REPLACE INTO maintenance VALUES(?,?)', (cursor_key, json.dumps(job)))
                    if not any((directory / suffix).is_dir() for suffix in COPIES): continue
                    if state not in ('cancelled', 'completed', 'failed') or not quiescent(directory):
                        record({'path': str(directory), 'result': 'retained', 'reason': 'active_or_recoverable_legacy_job',
                                'freed_bytes': 0, 'size_complete': False})
                        continue
                    record({'path': str(directory), 'result': 'retained', 'reason': 'legacy_candidate_and_recovery_evidence',
                            'freed_bytes': 0, 'size_complete': False})
                    for suffix in COPIES:
                        project = directory / suffix
                        if not project.is_dir() or project.is_symlink(): continue
                        for parent, directories, _ in os.walk(project, followlinks=False):
                            if time.monotonic() >= deadline: return False
                            directories[:] = [d for d in directories if d not in
                                ('.git', '.auto-agents', 'node_modules', '.venv', '.conda')
                                and not (Path(parent) / d).is_symlink()]
                            if '.next' not in directories: continue
                            directories.remove('.next')
                            path = Path(parent) / '.next'
                            try: clean_build(store, db, directory, project, path, deadline, record)
                            except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
                                complete = False
                                record({'path': str(path), 'result': 'deferred' if isinstance(error, TimeoutError) else 'error',
                                        'reason': str(error), 'freed_bytes': 0})
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
            complete = False
            record({'path': str(root), 'result': 'deferred' if isinstance(error, (TimeoutError, BlockingIOError)) else 'error',
                    'reason': str(error), 'freed_bytes': 0})
    return complete
