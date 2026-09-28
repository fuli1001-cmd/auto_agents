"""Installation-owned runtime custody. History is not a live filesystem pin."""
import atexit
from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from .model import canonical, digest, require
from .store import KernelStore

_produced = {}


def identity(path):
    from ..artifact_store import _identity, _parents
    return {'inode': _identity(path), 'parents': _parents(path)}


def register(store, artifact, *, kind='runtime', directory=None, purpose=None):
    path = Path(directory or Path(artifact['path']).parent).absolute()
    require(path.is_relative_to(store.root) and path != store.root,
            'runtime_owner', 'Runtime directory is outside its installation')
    proof = identity(path)
    token = uuid4().hex if purpose else None
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM kernel_runtimes WHERE path=?', (str(path),)).fetchone()
        instance = old['id'] if old else artifact['artifact_id']
        if not old and db.execute('SELECT 1 FROM kernel_runtimes WHERE id=?', (instance,)).fetchone():
            instance = digest([artifact['artifact_id'], str(path)])
        require(not old or old['state'] != 'deleting', 'runtime_deleting', 'Runtime deletion is in progress')
        if old and old['state'] != 'deleted':
            require(old['path'] == str(path) and json.loads(old['inode']) == proof,
                    'runtime_changed', 'Registered runtime directory changed')
        db.execute('INSERT INTO kernel_runtimes(id,path,artifact,inode,kind,state) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET artifact=excluded.artifact,inode=excluded.inode,'
                   "state='ready',trash='',error='',freed=0",
                   (instance, str(path), canonical(artifact), canonical(proof), kind, 'ready'))
        if token:
            from ..artifact_store import process_identity
            db.execute('INSERT INTO kernel_runtime_uses VALUES(?,?,?,?)',
                       (token, instance, canonical(process_identity()), purpose))
    return token or instance


def acquire(store, artifact, purpose='process', *, parent=None):
    from ..artifact_store import process_identity
    token = uuid4().hex
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT id,state FROM kernel_runtimes WHERE path=?', (str(Path(artifact['path']).parent),)).fetchone()
        require(row is not None and row['state'] == 'ready', 'runtime_unavailable', 'Runtime is not available for use')
        db.execute('INSERT INTO kernel_runtime_uses VALUES(?,?,?,?)',
                   (token, row['id'], canonical({**process_identity(), 'parent': parent}), purpose))
    return token


def release(store, token, *, cleanup=True):
    with store.connect() as db:
        db.execute('DELETE FROM kernel_runtime_uses WHERE token=?', (token,))
    if cleanup: maintain(store)


@contextmanager
def using(store, artifact, purpose='process'):
    token = register(store, artifact, purpose=purpose)
    try: yield token
    finally: release(store, token)


def produced(root, artifact):
    """Existing producers acquire custody before publishing a new path."""
    control = next((p for p in [Path(root), *Path(root).parents]
                    if (p / 'control.sqlite3').is_file()), None)
    if control is None: return
    store = KernelStore(control)
    key = (str(control), artifact['path'])
    if key not in _produced:
        _produced[key] = register(store, artifact, purpose='producer')


def release_produced(store, artifact):
    token = _produced.pop((str(store.root), artifact['path']), None)
    if token: release(store, token)


def _exit():
    roots = set()
    for (root, _), token in list(_produced.items()):
        if not (Path(root) / 'control.sqlite3').is_file(): continue
        try:
            release(KernelStore(root), token, cleanup=False)
            roots.add(root)
        except Exception: pass  # The supervisor retries after process death.
    _produced.clear()
    for root in roots:
        try: maintain(KernelStore(root))
        except Exception: pass


atexit.register(_exit)


@contextmanager
def temporary(store, prefix):
    """A crash leaves an owned directory and a dead lease, not unknown trash."""
    base = store.root / 'runtime-staging'
    base.mkdir(exist_ok=True)
    path = base / (prefix + '-' + uuid4().hex)
    path.mkdir(mode=0o700)
    artifact = {'artifact_id': digest(str(path)), 'path': str(path), 'source': ''}
    token = register(store, artifact, kind='temporary', directory=path, purpose='build')
    try: yield path
    finally: release(store, token)


@contextmanager
def building(root):
    """Fence discovery/GC until a produced directory has its process lease."""
    control = next((p for p in [Path(root), *Path(root).parents]
                    if (p / 'control.sqlite3').is_file()), None)
    if control is None:
        yield
        return
    try:
        with (control / 'runtime-custody.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
    finally: maintain(KernelStore(control))


@contextmanager
def build_directory(root, parent, artifact_id):
    import tempfile
    control = next((p for p in [Path(root), *Path(root).parents]
                    if (p / 'control.sqlite3').is_file()), None)
    if control is None:
        with tempfile.TemporaryDirectory(prefix='.building-', dir=parent) as path: yield Path(path)
        return
    store = KernelStore(control)
    with store.connect() as db:
        row = db.execute('SELECT state FROM kernel_runtimes WHERE path=?', (str(Path(parent) / artifact_id),)).fetchone()
    require(row is None or row['state'] != 'deleting', 'runtime_deleting', 'Previous snapshot deletion must finish before rebuilding')
    with temporary(store, 'artifact') as path: yield path


def migrate(store):
    """Only authenticated manifests/owned Git worktrees grant deletion authority."""
    from ..repair_v2.runtime_artifact import verify
    reports = []
    paths = list((store.root / 'kernel-releases/runtime-artifacts').glob('*/manifest.json'))
    paths += list((store.root / 'kernel-engine').glob('*/runtime-artifacts/*/manifest.json'))
    paths += list((store.root / 'v2-transactions').glob('*/runtime-artifacts/*/manifest.json'))
    for path in paths:
        try:
            value = json.loads(path.read_text())
            artifact = {**value, 'path': str(path.parent / 'source'), 'version': 1}
            with store.connect() as db:
                old = db.execute('SELECT state FROM kernel_runtimes WHERE path=?', (str(path.parent),)).fetchone()
            if old: continue
            verify(artifact)
            register(store, artifact)
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            reports.append({'path': str(path.parent), 'result': 'retained', 'reason': str(error)})
    repository = store.root / 'engine.git'
    if repository.is_dir():
        result = subprocess.run(['git', '--git-dir=' + str(repository), 'worktree', 'list', '--porcelain'],
                                capture_output=True, text=True, timeout=10)
        require(result.returncode == 0, 'runtime_inventory', 'Cannot inspect installed controller worktrees')
        for block in result.stdout.split('\n\n'):
            fields = dict(line.split(' ', 1) for line in block.splitlines() if ' ' in line)
            path = Path(fields.get('worktree', '/missing'))
            if path.parent != store.root / 'runtimes' or not path.is_dir(): continue
            artifact = {'artifact_id': digest(['worktree', str(path)]), 'path': str(path),
                        'commit': fields.get('HEAD'), 'source': '', 'repository': str(repository)}
            with store.connect() as db:
                old = db.execute('SELECT id FROM kernel_runtimes WHERE path=?', (str(path),)).fetchone()
            if old: continue
            register(store, artifact, kind='worktree', directory=path)
    return reports


def external_users(store):
    """Protect legacy consumers and children whose parent exited first."""
    values = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            if proc.stat().st_uid != os.getuid(): continue
            values.append(str((proc / 'cwd').readlink()))
            values.extend((proc / 'cmdline').read_bytes().decode(errors='replace').split('\0'))
            values.extend((proc / 'environ').read_bytes().decode(errors='replace').split('\0'))
        except (FileNotFoundError, ProcessLookupError): continue
        except PermissionError:
            # Uninspectable same-user processes can still be consumers.
            raise RuntimeError('Cannot confirm runtime use by process ' + proc.name)
    if shutil.which('docker'):
        from ..repair_v2.docker import container_mounts
        values.extend({'mount': str(p)} for p in container_mounts())
    values.extend(storage_pins(store))
    return values


def storage_pins(store):
    """The shared storage API's explicit pins/leases still protect runtimes."""
    from ..artifact_store import ArtifactStore, alive
    registry = ArtifactStore()
    if not registry.path.is_file(): return []
    with registry.connect() as db:
        rows = db.execute("SELECT data FROM artifacts WHERE json_extract(data,'$.state')!='deleted' "
                          "AND (path LIKE ? OR ? LIKE path||'/%')",
                          (str(store.root) + '/%', str(store.root))).fetchall()
    protected = []
    for raw in rows:
        row = json.loads(raw[0])
        reason = ('storage_pin' if row.get('pin') else 'storage_reference' if row.get('references')
                  else 'storage_lease' if any(alive(owner) for owner in row.get('leases', [])) else '')
        if reason: protected.append({'mount': row['path'], 'reason': reason})
    return protected


def references(store, db):
    """Select live domain records first; do not pin every historical receipt."""
    roots = []
    for key in ('active_runtime', 'previous_runtime', 'trusted_verifier_runtime', 'runtime_manager_runtime'):
        row = db.execute('SELECT value FROM kernel_meta WHERE key=?', (key,)).fetchone()
        if row: roots.append((key, json.loads(row['value'])))
    operator = store.root / 'operator.json'
    if operator.is_file(): roots.append(('supervisor', json.loads(operator.read_text()).get('implementation_root')))
    for path in (store.root / 'runtimes').glob('*.source.json'):
        require(not path.is_symlink(), 'runtime_references', 'Source merge receipt cannot be a symlink')
        if json.loads(path.read_text()).get('pending', True):
            roots.append(('pending_source_merge', str(path.with_name(path.name[:-len('.source.json')]))))
    delivery = store.meta('runtime_delivery')
    if delivery and delivery.get('status') != 'complete': roots.append(('source-delivery', delivery))
    for row in db.execute('SELECT snapshot FROM kernel_streams'):
        state = json.loads(row['snapshot'])
        pending = {c['incident_id'] for c in state['continuations'].values() if c['status'] != 'consumed'}
        live = {key for key, task in state['tasks'].items() if task['status'] != 'completed'}
        live |= {incident['task_id'] for key, incident in state['incidents'].items()
                 if incident['status'] != 'resolved' or key in pending}
        roots += [('task:' + key, state['tasks'][key]) for key in live]
        # Recovery consumes the latest successful phase outputs and its recent
        # failure observations, not every engine version named in the journal.
        selected = {key for key, c in state['commands'].items() if c['status'] not in ('finished', 'cancelled')}
        for task_id in live:
            commands = sorted(((key, c) for key, c in state['commands'].items() if c['task_id'] == task_id),
                              key=lambda item: item[1]['sequence'])
            successful, failures = {}, []
            for key, command in commands:
                if (command.get('outcome') or {}).get('kind') == 'success': successful[command['phase']] = key
                elif command.get('outcome'): failures.append(key)
            selected.update(successful.values()); selected.update(failures[-3:])
        roots += [('command:' + key, state['commands'][key]) for key in selected]
        roots += [('incident:' + key, incident) for key, incident in state['incidents'].items()
                  if incident['status'] != 'resolved' or key in pending]
    roots += [('pending-effect', json.loads(row[0])) for row in db.execute(
        "SELECT payload FROM kernel_outbox WHERE state NOT IN ('finished','cancelled')")]
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'jobs' in tables:
        pending_jobs = {r[0] for r in db.execute("SELECT job FROM subscribers WHERE state NOT IN ('finished','cancelled')")} if 'subscribers' in tables else set()
        if 'outbox' in tables:
            pending_jobs |= {r[0] for r in db.execute("SELECT job FROM outbox WHERE state NOT IN ('published','invalidated','cancelled')")}
        for row in db.execute('SELECT id,state,payload,result FROM jobs'):
            if row['state'] == 'completed' and row['id'] not in pending_jobs: continue
            roots.append(('legacy-job:' + row['id'], [json.loads(row['payload']), json.loads(row['result'])]))
            for path in (store.root / 'jobs' / row['id']).glob('*.json'):
                if path.name.endswith('.output.json'): continue  # Raw provider output is not a custody record.
                require(not path.is_symlink() and path.stat().st_size <= 16 * 1024 * 1024,
                        'runtime_references', 'Legacy runtime reference needs explicit inspection', path=str(path))
                roots.append(('legacy-job:' + row['id'], json.loads(path.read_text())))
    if 'verifications' in tables:
        for row in db.execute("SELECT context,payload FROM verifications WHERE state IN ('queued','running')"):
            roots.append(('legacy-verification', json.loads(row['payload'])))
            context = db.execute('SELECT payload FROM verification_contexts WHERE id=?', (row['context'],)).fetchone()
            if context: roots.append(('legacy-verification', json.loads(context[0])))
    repair_bases = set()
    for path in (store.root / 'v2-transactions').glob('*/state.json'):
        from ..repair_v2.store import Store
        state = Store(path.parent).load()
        if state and state.get('status') not in ('complete', 'abandoned', 'skipped'):
            roots.append(('retained-repair', state))
            request = path.parent / 'request.json'
            require(request.is_file() and not request.is_symlink(), 'runtime_references', 'Retained repair request is unavailable')
            repair_bases.add(json.loads(request.read_text())['engine_base'])
    from ..artifact_store import alive
    for row in db.execute('SELECT token,owner FROM kernel_runtime_uses').fetchall():
        if not alive(json.loads(row['owner'])):
            db.execute('DELETE FROM kernel_runtime_uses WHERE token=?', (row['token'],))
    for row in db.execute('SELECT * FROM kernel_runtime_uses'):
        roots.append(('lease:' + row['purpose'], row['runtime']))
    for row in db.execute('SELECT payload FROM kernel_runtime_adoptions'):
        transaction = json.loads(row[0])
        if transaction.get('status') not in ('complete', 'failed', 'superseded') and alive(transaction['owner']):
            roots.append(('adoption', transaction))
    expanded, objects = set(), {}
    expanded.update(('retained_repair_base', commit) for commit in repair_bases)
    if 'jobs' in tables and db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed','blocked','cancelled') LIMIT 1").fetchone():
        expanded.add(('legacy_operation_in_progress', '__legacy_runtime_in_use__'))
    for reason, value in roots:
        seen = set()
        def walk(item):
            if isinstance(item, dict):
                if {'artifact_id', 'path', 'source'} <= item.keys():
                    walk(item['path'])
                    return
                for v in item.values(): walk(v)
            elif isinstance(item, list):
                for v in item: walk(v)
            elif isinstance(item, str):
                if item.startswith(str(store.root) + '/') or len(item) == 64:
                    expanded.add((reason, item))
                if len(item) == 64 and all(c in '0123456789abcdef' for c in item) and item not in seen:
                    seen.add(item)
                    require(len(seen) < 10000, 'runtime_references', 'Runtime reference traversal exceeded its bound')
                    if (store.root / 'kernel-objects' / item[:2] / item).is_file():
                        try:
                            if item not in objects: objects[item] = store.read(item)
                            value = objects[item]
                        except (UnicodeError, ValueError): return  # Opaque logs are not references.
                        walk(value)
        walk(value)
    return expanded


def _matches(value, artifact, directory):
    return (value in {artifact['artifact_id'], artifact.get('source') or '__none__'}
            or value == str(directory) or value.startswith(str(directory) + '/')
            or value == artifact['path'] or value.startswith(artifact['path'] + '/'))


def _reasons(refs, row, artifact, directory, observed):
    reasons = {why for why, value in refs
               if (value == row['id'] if why.startswith('lease:') else
                   _matches(value, artifact, directory)
                   or row['kind'] == 'worktree' and why == 'retained_repair_base' and value == artifact['commit']
                   or row['kind'] == 'worktree' and value == '__legacy_runtime_in_use__')}
    for value in observed:
        protected = ((Path(value['mount']) == directory or Path(value['mount']) in directory.parents
                      or directory in Path(value['mount']).parents) if isinstance(value, dict)
                     else str(directory) in value)
        if protected: reasons.add(value.get('reason', 'live_process_or_container') if isinstance(value, dict)
                                  else 'live_process_or_container')
    return reasons


def collect(store, *, users=None, deadline=float('inf')):
    from ..artifact_store import _allocated, _parent_fd, _remove_at
    reports = migrate(store)
    observed = external_users(store) if users is None else users
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        baseline = references(store, db)
        rows = db.execute("SELECT * FROM kernel_runtimes WHERE state!='deleted' ORDER BY id").fetchall()
    cursor = store.meta('runtime_cleanup_cursor', '')
    start = next((i for i, row in enumerate(rows) if row['id'] > cursor), 0)
    rows = rows[start:] + rows[:start]
    for row in rows:
        if time.monotonic() >= deadline: break
        try:
            artifact, directory = json.loads(row['artifact']), Path(row['path'])
            trash = Path(row['trash']) if row['trash'] else store.root / 'runtime-trash' / row['id']
            reasons = _reasons(baseline, row, artifact, directory, observed)
            if reasons:
                # Stale positive references can only defer deletion. Every
                # negative result is rechecked transactionally below, so new
                # consumers still win against the deletion fence.
                reports.append({'path': str(directory), 'result': 'retained', 'reason': ','.join(sorted(reasons))})
                continue
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                reasons = _reasons(references(store, db), row, artifact, directory, observed)
                if reasons:
                    reports.append({'path': str(directory), 'result': 'retained', 'reason': ','.join(sorted(reasons))})
                    continue
                if row['state'] != 'deleting':
                    require(identity(directory) == json.loads(row['inode']), 'runtime_changed', 'Runtime directory identity changed')
                    if row['kind'] == 'runtime':
                        from ..repair_v2.runtime_artifact import verify
                        verify(artifact)
                    if row['kind'] == 'worktree':
                        result = subprocess.run(['git', '-C', str(directory), 'status', '--porcelain', '--untracked-files=all'],
                                                capture_output=True, text=True, timeout=10)
                        require(result.returncode == 0 and not result.stdout.strip(), 'runtime_dirty', 'Retained worktree contains changes')
                        head = subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()
                        common = subprocess.check_output(['git', '-C', str(directory), 'rev-parse', '--path-format=absolute', '--git-common-dir'], text=True).strip()
                        require(head == artifact['commit'] and Path(common).resolve() == Path(artifact['repository']).resolve(),
                                'runtime_changed', 'Retained worktree Git identity changed')
                    size, measured = _allocated(directory, deadline)
                    db.execute("UPDATE kernel_runtimes SET state='deleting',trash=?,freed=?,error='' WHERE id=?",
                               (str(trash), size, row['id']))
            # The deletion fence is durable before changing the filesystem.
            if row['kind'] == 'worktree':
                if directory.exists():
                    require(identity(directory) == json.loads(row['inode']), 'runtime_changed', 'Worktree identity changed')
                    subprocess.run(['git', '--git-dir=' + artifact['repository'], 'worktree', 'remove', str(directory)],
                                   check=True, capture_output=True, timeout=30)
            else:
                trash.parent.mkdir(mode=0o700, exist_ok=True)
                if directory.exists():
                    proof = json.loads(row['inode'])
                    require(not trash.exists() and not trash.is_symlink(), 'runtime_changed', 'Deletion destination already exists')
                    target_proof = identity(trash.parent)
                    with _parent_fd(proof, directory) as fd, _parent_fd(target_proof, trash.parent) as parent_fd:
                        target_fd = os.open(trash.parent.name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                        try:
                            info = os.fstat(target_fd)
                            require([info.st_dev, info.st_ino] == target_proof['inode'], 'runtime_changed', 'Deletion parent changed')
                            os.rename(directory.name, trash.name, src_dir_fd=fd, dst_dir_fd=target_fd)
                        finally: os.close(target_fd)
                if trash.exists():
                    require(identity(trash)['inode'] == json.loads(row['inode'])['inode'],
                            'runtime_changed', 'Deletion directory identity changed')
                    proof = identity(trash)
                    with _parent_fd(proof, trash) as fd:
                        _remove_at(fd, trash.name, proof['inode'][0], deadline)
            with store.connect() as db:
                saved = db.execute('SELECT freed FROM kernel_runtimes WHERE id=?', (row['id'],)).fetchone()
                db.execute("UPDATE kernel_runtimes SET state='deleted',error='' WHERE id=?", (row['id'],))
            reports.append({'path': str(directory), 'result': 'deleted', 'freed_bytes': saved['freed'],
                            'size_complete': measured if row['state'] != 'deleting' else False})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            with store.connect() as db:
                db.execute('UPDATE kernel_runtimes SET error=? WHERE id=?', (str(error), row['id']))
            reports.append({'path': row['path'], 'result': 'deferred', 'reason': str(error)})
        finally: store.set_meta('runtime_cleanup_cursor', row['id'])
    return reports


def maintain(store, *, users=None, deadline=float('inf')):
    """One collector; cleanup errors never undo a committed adoption."""
    manager = store.meta('runtime_manager_runtime') or store.meta('active_runtime')
    if (users is None and manager and manager.get('path') and Path(manager['path']).resolve() != Path(__file__).resolve().parents[3]
            and (Path(manager['path']) / 'src/auto_agents/recovery/runtime_lifecycle.py').is_file()):
        return _current_manager_cleanup(store, manager, deadline)
    with (store.root / 'runtime-custody.lock').open('a+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return []
        from ..artifact_store import ArtifactStore
        registry = ArtifactStore()
        try:
            # Serialize explicit storage pins/reference acquisition with the
            # deletion decision. Both custodians agree before files disappear.
            with registry.locked(wait=0) if registry.path.is_file() else nullcontext():
                result = collect(store, users=users, deadline=deadline)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result = [{'result': 'deferred', 'reason': str(error)}]
        store.set_meta('runtime_cleanup', {'at': time.time(), 'items': result})
        return result


def _current_manager_cleanup(store, manager, deadline):
    """Old business processes never keep executing a superseded GC policy."""
    import sys
    token = None
    try:
        from ..repair_v2.runtime_artifact import verify
        verify(manager)
        token = register(store, manager, purpose='cleanup')
        script = ('import sys,json,time; sys.path.insert(0,' + repr(str(Path(manager['path']) / 'src'))
                  + '); from auto_agents.recovery.store import KernelStore; '
                  'from auto_agents.recovery.runtime_lifecycle import maintain; '
                  'print(json.dumps(maintain(KernelStore(sys.argv[1]),deadline=time.monotonic()+float(sys.argv[2]))))')
        seconds = max(0, min(30, deadline - time.monotonic()))
        result = subprocess.run([sys.executable, '-c', script, str(store.root), str(seconds)],
                                capture_output=True, text=True, timeout=seconds + 10)
        require(result.returncode == 0, 'runtime_cleanup', 'Current runtime cleanup did not finish')
        return json.loads(result.stdout)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return [{'result': 'deferred', 'reason': str(error)}]
    finally:
        if token: release(store, token, cleanup=False)


def owns_path(control, path):
    import sqlite3
    store = KernelStore(control, readonly=True)
    if not store.path.is_file(): return False
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='kernel_runtimes'").fetchone(): return False
        return any(Path(row[0]) == path or Path(row[0]) in path.parents for row in db.execute(
            "SELECT path FROM kernel_runtimes WHERE state!='deleted'"))


@contextmanager
def watch(store):
    """Supervisor compensation for SIGKILL and consumers ending after parents."""
    import threading
    from ..artifact_store import alive
    stop = threading.Event()
    def observe():
        previous, checked = None, 0.0
        while not stop.wait(.25):
            try:
                with store.connect() as db:
                    uses = [(r['token'], alive(json.loads(r['owner'])))
                            for r in db.execute('SELECT token,owner FROM kernel_runtime_uses')]
                    revisions = db.execute('SELECT coalesce(sum(revision),0) FROM kernel_streams').fetchone()[0]
                current = (uses, revisions)
                if current != previous or time.monotonic() - checked > 5:
                    maintain(store, deadline=time.monotonic() + 3)
                    previous, checked = current, time.monotonic()
            except Exception:
                # A durable deferred record or the next observation retries it.
                checked = 0
    thread = threading.Thread(target=observe, name='runtime-custody', daemon=True)
    thread.start()
    try: yield
    finally:
        stop.set()
        thread.join(timeout=5)
