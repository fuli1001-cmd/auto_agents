"""Native control records are projections of the kernel, never competing stores."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from functools import wraps
import json
import os
from pathlib import Path
from uuid import uuid4

from .model import Event, KernelError, canonical, digest, require
from .store import KernelStore

_reading = ContextVar('kernel_projection_read', default=False)
_references = ContextVar('kernel_projection_references', default=None)
_depth = ContextVar('kernel_entry_depth', default=0)
MARKER = '.auto-agents/state/recovery-kernel.json'
NOT_MANAGED = object()


class Projection(dict):
    """Carry the read version with a record, not merely with its process."""
    def __init__(self, value, reference):
        super().__init__(value)
        self.reference = reference


def bind_model(model, payload):
    if isinstance(payload, Projection): model._kernel_reference = payload.reference
    return model


def save_model(path, model):
    reference = write_projection(path, model.to_dict(), expected=getattr(model, '_kernel_reference', ''))
    if reference: model._kernel_reference = reference
    return bool(reference)


def installation_root():
    explicit = os.environ.get('AUTO_AGENTS_RECOVERY_CONTROL')
    if explicit: return Path(explicit).resolve()
    if os.environ.get('AUTO_AGENTS_REPAIR_CONTROL_DISABLED') == '1': return None
    configured = os.environ.get('AUTO_AGENTS_REPAIR_CONTROL_CONFIG')
    if configured and Path(configured).is_file():
        return Path(json.loads(Path(configured).read_text())['root']).resolve()
    from ..repair_control import operator_root, digest as installation_digest
    source = str(Path(__file__).resolve().parents[3])
    binding = operator_root() / (installation_digest(source)[:24] + '.json')
    if binding.is_file(): return Path(json.loads(binding.read_text())['root']).resolve()
    return None


def admit_project(project):
    root = installation_root()
    if root is None: return
    store = KernelStore(root, readonly=True)
    if store.meta('mode') != 'active': return
    project = Path(project).resolve()
    if (project / MARKER).is_file():
        installed(project)
        return
    from .migration import inspect_project, apply_project, import_repairs
    store = KernelStore(root)
    manifest = inspect_project(project)
    require(manifest['ok'], 'migration_blocked', 'Project control data cannot be imported', errors=manifest['errors'])
    apply_project(store, manifest)
    import_repairs(store, root, project)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM kernel_meta WHERE key='activation'").fetchone()
        activation = json.loads(row['value']) if row else {'projects': []}
        activation['projects'] = sorted(set(activation['projects']) | {str(project)})
        db.execute("INSERT INTO kernel_meta VALUES('activation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canonical(activation),))
    activate_project(store, project)


def record_location(path):
    path = Path(path).absolute()
    parts = path.parts
    try: offset = parts.index('.auto-agents')
    except ValueError: return None
    if parts[offset + 1:offset + 2] != ('state',): return None
    project = Path(*parts[:offset]); relative = parts[offset + 2:]
    if relative == ('run_state.json',): return project, 'run'
    if len(relative) == 3 and relative[0] == 'sessions' and relative[2] in {'session_state.json', 'issue.json'}:
        return project, ('session:' if relative[2] == 'session_state.json' else 'issue:') + relative[1]
    if len(relative) == 3 and relative[0] == 'workflows' and relative[2] == 'workflow.json':
        return project, 'workflow:' + relative[1]
    if len(relative) == 4 and relative[0] == 'workflows' and relative[2] == 'events' and relative[3].endswith('.json'):
        return project, 'event:' + relative[1] + ':' + Path(relative[3]).stem
    if len(relative) == 2 and relative[0] == 'handoffs' and relative[1].endswith('.json'):
        return project, 'handoff:' + Path(relative[1]).stem
    return None


def installed(project):
    project = Path(project).resolve(); marker = project / MARKER
    if not marker.is_file(): return None
    require(not marker.is_symlink(), 'kernel_binding', 'Kernel binding cannot be a symbolic link')
    row = json.loads(marker.read_text())
    require(row.get('schema') == 1 and row.get('project') == str(project), 'kernel_binding', 'Kernel marker belongs to another project')
    store = KernelStore(row['control_root'], readonly=True)
    require(store.meta('mode') in {'active','draining'}, 'kernel_inactive', 'Project requires the unified recovery core')
    require(str(project) in store.meta('activation', {}).get('projects', []), 'kernel_binding', 'Project is not admitted by this core')
    return KernelStore(row['control_root'])


def pack(store, value):
    """Externalize large subtrees; original Python values round-trip exactly."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {'preimages', 'manifest', 'proof_sources'} and isinstance(item, (dict, list)):
                result[key] = {'$kernel_object': store.put(item)}
            else: result[key] = pack(store, item)
        return result
    if isinstance(value, list): return [pack(store, item) for item in value]
    if isinstance(value, str) and len(value) > 65536:
        return {'$kernel_object': store.put(value)}
    return value


def unpack(store, value):
    if isinstance(value, dict):
        if set(value) == {'$kernel_object'}: return store.read(value['$kernel_object'])
        return {key: unpack(store, item) for key, item in value.items()}
    if isinstance(value, list): return [unpack(store, item) for item in value]
    return value


def read_projection(path):
    if _reading.get(): return NOT_MANAGED
    location = record_location(path)
    if not location: return NOT_MANAGED
    project, name = location; store = installed(project)
    if store is None: return NOT_MANAGED
    if name == 'run':
        with store.connect() as db:
            current = db.execute("SELECT name FROM kernel_project_heads WHERE project=? AND kind='run'", (str(project),)).fetchone()
            names = [row['name'] for row in db.execute("SELECT name FROM kernel_bindings WHERE project=? AND name LIKE 'run:%'", (str(project),))]
        require(current is not None or len(names) <= 1, 'run_identity', 'Select an explicit retained run before resuming')
        if not names: return None
        name = current['name'] if current else names[0]
    stream = store.binding(project, name)
    if stream is None and name.startswith('event:'):
        stream = store.binding(project,'workflow:' + name.split(':',2)[1])
    if stream is None:
        require(not Path(path).exists(), 'migration_required', 'Unimported legacy record cannot enter the active core', path=str(path))
        return None
    state = store.load(stream); row = state['projections'].get(name)
    require(row is not None, 'projection', 'Control binding has no authoritative projection')
    refs = dict(_references.get() or {}); refs[str(Path(path).resolve())] = row['blob']; _references.set(refs)
    return Projection(unpack(store, store.read(row['blob'])), row['blob'])


def _stream(store, project, name, value):
    existing = store.binding(project, name)
    if existing: return existing
    if name.startswith('issue:'):
        stream = store.binding(project, 'session:' + name.split(':',1)[1])
        require(stream is not None, 'task_identity', 'Issue has no retained session owner')
        store.bind(project, name, stream)
        return stream
    wf = value.get('workflow_id') or value.get('resume_context', {}).get('workflow_id')
    stream = store.binding(project, 'workflow:' + wf) if wf else None
    if not stream:
        root = value.get('root') or {}
        native = root.get('native_id') or value.get('session_id') or value.get('run_id') or wf
        kind = root.get('kind') or value.get('mode') or 'run'
        if root:
            stream = store.binding(project, ('run:' if kind == 'run' else 'session:') + native)
        if not stream:
            require(bool(native), 'workflow_identity', 'A new control record needs an explicit owner')
            stream = 'wf:' + digest([str(project), kind, native])[:40]
            if not store.load(stream)['workflow_id']:
                store.apply(stream, 0, Event('register:' + digest(stream), 'workflow_registered', {
                    'workflow_id': stream, 'goal_id': 'goal:' + digest(stream)[:40], 'project': str(project)}))
    store.bind(project, name, stream)
    if not any(key.startswith('legacy-budget:') for key in store.load(stream)['imports']):
        from .migration import import_budgets
        import_budgets(store,store.root,project)
    return stream


def write_projection(path, value, *, expected=None):
    if _reading.get(): return False
    location = record_location(path)
    if not location: return False
    project, name = location; store = installed(project)
    if store is None: return False
    require(isinstance(value, dict), 'projection', 'Control projection must be structured')
    if name == 'run': name = 'run:' + str(value.get('run_id') or '')
    stream = _stream(store, project, name, value); state = store.load(stream)
    previous = state['projections'].get(name)
    if expected is None: expected = (_references.get() or {}).get(str(Path(path).resolve()))
    require(previous is None or previous['blob'] == expected, 'stale_projection', 'Control state changed since it was read', name=name)
    if previous is None: expected = None
    reference = store.put(pack(store, value))
    counters = {key: value[key] for key in ('current_attempt',) if type(value.get(key)) is int}
    event = Event('projection:' + uuid4().hex, 'projection_saved', {'name': name, 'blob': reference,
        'previous': expected, 'counters': counters, 'terminal': value.get('status') == 'completed'})
    store.apply(stream, state['revision'], event)
    refs = dict(_references.get() or {}); refs[str(Path(path).resolve())] = reference; _references.set(refs)
    # A crash here is harmless: readers use the database, not this view.
    from ..io_utils import _atomic_write
    display = {key: value.get(key) for key in ('session_id','run_id','workflow_id','mode','status','resolution',
        'current_attempt','active_handoff_id','parent_handoff_id') if key in value}
    display.update(kernel_schema=1, kernel_projection={'stream': stream, 'name': name, 'blob': reference})
    _atomic_write(Path(path), canonical(display) + '\n')
    return reference


def activate_project(store, project):
    """Called only after global adoption has committed under all project locks."""
    from ..io_utils import _atomic_write
    project = Path(project).resolve()
    require(store.meta('mode') == 'active' and str(project) in store.meta('activation', {}).get('projects', []),
            'kernel_inactive', 'Project activation is not committed')
    _atomic_write(project / MARKER, canonical({'schema': 1, 'project': str(project), 'control_root': str(store.root)}) + '\n')


def append_workflow_event(project, snapshot, payload, path):
    store = installed(project)
    if store is None: return False
    name = 'workflow:' + snapshot.workflow_id
    stream = store.binding(project,name)
    require(stream is not None,'workflow_identity','Event has no retained workflow')
    state = store.load(stream)
    event_name = 'event:' + snapshot.workflow_id + ':' + Path(path).stem
    reference = store.put(pack(store,snapshot.to_dict()))
    event_ref = store.put(payload)
    store.apply(stream,state['revision'],Event('native-event:' + payload['event_id'],'projection_batch_saved',{'entries':[
        {'name':name,'blob':reference,'previous':getattr(snapshot,'_kernel_reference',None),
         'terminal':snapshot.status == 'completed','counters':{}},
        {'name':event_name,'blob':event_ref,'previous':None,'terminal':True,'counters':{}}]}))
    snapshot._kernel_reference = reference
    store.bind(project,event_name,stream)
    from ..io_utils import _atomic_write
    _atomic_write(Path(path),canonical(payload) + '\n')
    return True


def workflow_events(project, workflow_id):
    store = installed(project)
    if store is None: return None
    stream = store.binding(project,'workflow:' + workflow_id)
    if stream is None: return []
    prefix = 'event:' + workflow_id + ':'
    records = [store.read(row['blob']) for name,row in store.load(stream)['projections'].items() if name.startswith(prefix)]
    return sorted(records,key=lambda row:row['sequence'])


def export_snapshot(source, destination):
    """Materialize frozen domain inputs without giving a sandbox a live core."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    store = installed(source)
    if store is None: return
    from ..io_utils import _atomic_write
    with store.connect() as db:
        streams = [r['stream'] for r in db.execute('SELECT DISTINCT stream FROM kernel_bindings WHERE project=?',(str(source),))]
        head = db.execute("SELECT name FROM kernel_project_heads WHERE project=? AND kind='run'",(str(source),)).fetchone()
    records = {name:row for stream in streams for name,row in store.load(stream)['projections'].items()}
    paths = []
    for name, row in records.items():
        kind, identity = name.split(':', 1)
        if kind == 'run' and head and name != head['name']: continue
        relative = {'run': 'run_state.json', 'session': f'sessions/{identity}/session_state.json',
                    'issue': f'sessions/{identity}/issue.json', 'workflow': f'workflows/{identity}/workflow.json',
                    'handoff': f'handoffs/{identity}.json'}.get(kind)
        if kind == 'event':
            workflow_id, filename = identity.split(':',1)
            relative = f'workflows/{workflow_id}/events/{filename}.json'
        if relative: paths.append(('.auto-agents/state/' + relative,row['blob']))
    for relative, reference in paths:
        _atomic_write(destination / relative,canonical(unpack(store,store.read(reference))) + '\n')
    (destination / MARKER).unlink(missing_ok=True)


def entry(method):
    """All public native entrypoints share one explicit projection context."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if _depth.get(): return method(self, *args, **kwargs)
        with ExitStack() as resources:
            root = installation_root()
            if root is not None:
                require(KernelStore(root, readonly=True).meta('mode') != 'draining',
                        'upgrade_draining', 'Core upgrade is draining operations; retry the original command after cutover')
            if root is not None and KernelStore(root, readonly=True).meta('mode') == 'active':
                from ..run_lock import ProjectRunLock, require_project_run_lock
                try: require_project_run_lock(self.project_root)
                except RuntimeError: resources.enter_context(ProjectRunLock(self.project_root))
            admit_project(self.project_root)
            store = installed(self.project_root)
            subject = None
            if method.__name__ in {'resume','resume_workflow'}:
                identity = args[0] if args else kwargs.get('session_id') or kwargs.get('workflow_id')
                if isinstance(identity,str): subject = ('session:' if method.__name__ == 'resume' else 'workflow:') + identity
            elif method.__name__ in {'run','resume_saved_run'} and store is not None:
                with store.connect() as db:
                    current = db.execute("SELECT name FROM kernel_project_heads WHERE project=? AND kind='run'",
                                         (str(Path(self.project_root).resolve()),)).fetchone()
                if current: subject = current['name']
            if subject and store is not None:
                owner = getattr(self,'orch',self)
                owner._kernel_subject = subject
                stream = store.binding(self.project_root,subject)
                if stream:
                    from .policy import automatic
                    automatic(store, stream)
                    state = store.load(stream)
                    if state['status'] in {'paused','cancelled'}:
                        store.apply(stream,state['revision'],Event('resume-entry:' + uuid4().hex,'workflow_resumed',{}))
            token = _references.set(dict(_references.get() or {}))
            depth = _depth.set(1)
            try: return method(self, *args, **kwargs)
            finally:
                _depth.reset(depth)
                _references.reset(token)
    return wrapped
