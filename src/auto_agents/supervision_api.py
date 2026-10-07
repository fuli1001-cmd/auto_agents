"""Small public observation/checkpoint protocol; maintenance is optional."""
from contextvars import ContextVar
from pathlib import Path
import argparse
import json
import os
import shutil
import threading
import time
import traceback
import uuid
import subprocess

from .business_state import BusinessStore, BusinessStateError, canonical, digest, export_snapshot, migrate, _migrate, LEGACY_MARKER

_active = ContextVar('business_observer', default=None)


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w') as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Observer:
    def __init__(self, project, argv):
        self.project, self.argv = Path(project).resolve(), list(argv)
        self.output = os.environ.get('AUTO_AGENTS_OBSERVATION_FILE')
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.value = {'schema': 1, 'project': str(self.project), 'status': 'running', 'phase': 'startup',
                      'step_id': 'startup', 'progress_seq': 0, 'steps': [], 'waiting_for': '',
                      'operation': '', 'subject': '', 'milestones': []}
        self.value['root_subject'] = next((self.argv[i+1] for i,arg in enumerate(self.argv[:-1])
                                          if arg == '--session'), '')
        revision=os.environ.get('AUTO_AGENTS_PINNED_RUNTIME')
        module_source=Path(__file__).resolve().parents[2]
        if not revision and (module_source/'.git').exists():
            probe=subprocess.run(['git','-C',str(module_source),'rev-parse','HEAD'],capture_output=True,text=True)
            if probe.returncode==0:revision=probe.stdout.strip()
        self.value['runtime_revision']=revision
        self.thread = None
        self.offline = os.environ.get('AUTO_AGENTS_OFFLINE_RESUME') == '1'
        self.expected_step = os.environ.get('AUTO_AGENTS_EXPECTED_STEP', '')
        store = BusinessStore(self.project, readonly=True)
        if store.path.exists():
            with store.connect() as db:
                row = db.execute("SELECT value FROM metadata WHERE key='progress'").fetchone()
            if row:
                progress = json.loads(row['value'])
                self.value.update(progress_seq=progress['sequence'], milestones=progress['milestones'])
            else:
                # Historical accepted work is a starting vector, never fresh
                # credit awarded by the first save after process replacement.
                initial=[]
                with store.connect() as db:
                    paths=[item['path'] for item in db.execute('SELECT path FROM records')]
                for path in paths:
                    initial.extend(accepted_milestones(path,store.get(path)))
                self.value.update(progress_seq=len(set(initial)),milestones=sorted(set(initial)))

    def __enter__(self):
        self.token = _active.set(self)
        self.publish()
        if self.output:
            self.thread = threading.Thread(target=self.heartbeat, daemon=True)
            self.thread.start()
        return self

    def heartbeat(self):
        while not self.stopped.wait(30):
            self.publish()

    def publish(self):
        with self.lock:
            if self.output:
                atomic(self.output, {**self.value, 'heartbeat_at': time.time()})

    def step(self, phase, subject='', *, external=False, counted=True):
        from .business_calls import OfflineBoundary
        step = str(subject) + ':' + phase
        with self.lock:
            self.value.update(phase=phase, step_id=step, subject=subject, operation=phase, waiting_for='')
            row = {'step_id': step, 'progress_seq': self.value['progress_seq']}
            if not counted:
                row['counted'] = False
            self.value['steps'] = [*self.value['steps'][-31:], row]
            self.publish()
        if counted and self.offline and not self.value.get('waiting_for'):
            repeated = sum(row.get('counted', True) and row['step_id'] == step
                           and row['progress_seq'] == self.value['progress_seq']
                           for row in self.value['steps'])
            if repeated >= 3:
                from .engine_fault import EngineFault
                raise EngineFault('Repeated business step without verified progress', step_id=step)
        if external and self.offline:
            raise OfflineBoundary({'phase': phase, 'subject': subject, 'step_id': step})

    def fault(self, error):
        from .engine_fault import EngineFault
        from .diagnostic_redaction import sanitize
        from .gates import GateCommandInfrastructureError
        step = self.value['step_id']
        category = 'engine' if isinstance(error, EngineFault) or isinstance(error, (KeyError, AttributeError, TypeError)) else 'unknown'
        if isinstance(error, BusinessStateError):
            category = 'reconciliation' if error.code == 'outcome_unknown' else 'state'
        evidence = dict(getattr(error, 'evidence', {}))
        if isinstance(error, GateCommandInfrastructureError):
            # A project test's unavailable browser/server is not evidence that
            # editing the supervising engine will repair it.
            category = 'verification'
            result = error.result
            if result is not None:
                evidence['verification'] = {
                    'command': sanitize(result.command), 'returncode': result.returncode,
                    'failure_id': result.infrastructure_failure_id,
                    'capability': result.infrastructure_capability,
                    'contract': result.infrastructure_contract,
                    'repair_scope': result.infrastructure_repair_scope,
                    'marker': result.process_snapshot.get('reported_infrastructure_marker', {}),
                    'stdout': sanitize(result.stdout[:8000] + '\n' + result.stdout[-8000:]),
                    'stderr': sanitize(result.stderr[:8000] + '\n' + result.stderr[-8000:]),
                }
        state = self.project / '.auto-agents/state'
        checkpoint = state / 'resume-checkpoints' / (uuid.uuid4().hex + '.json')
        records = status(self.project)
        token = {'schema': 1, 'project': str(self.project), 'argv': self.argv,
                 'step_id': step, 'state_identity': records['identity'], 'subject': self.value['subject']}
        root_id = self.value.get('root_subject') or next(
            (self.argv[i+1] for i,arg in enumerate(self.argv[:-1]) if arg=='--session'), '')
        if root_id and self.argv and self.argv[0] in {'collab', 'fix', 'provider-resolve'} and '--session' not in self.argv:
            token['argv'] = [*self.argv, '--session', root_id]
        target = root_id or self.value['subject']
        root_record = records['records'].get('sessions/' + root_id + '/session_state.json', {})
        handoff_id = root_record.get('active_handoff_id')
        if handoff_id:
            handoff = records['records'].get('handoffs/' + handoff_id + '.json', {})
            child = handoff.get('child') or {}
            if handoff.get('parent', {}).get('native_id') == root_id and child.get('native_id'):
                target = child['native_id']
        token['target_subject'] = target
        protected = ('goal','authorization_policy','goal_execution_environment','source_descriptor',
                     'workflow_id','parent_handoff_id','max_attempts','hard_ceiling')
        token['protected'] = {name:{key:value.get(key) for key in protected}
                              for name,value in records['records'].items() if name.endswith('session_state.json')}
        atomic(checkpoint, token)
        self.value.update(status='failed', fault={'category': category, 'step_id': step,
            'type': type(error).__name__, 'message': sanitize(str(error)), 'traceback': sanitize(traceback.format_exc()),
            'resume_token': str(checkpoint), 'evidence': evidence})
        diagnostics = checkpoint.with_suffix('.diagnostics.json')
        self.value['fault']['diagnostics_path'] = str(diagnostics)
        atomic(diagnostics, self.value['fault'])
        self.publish()
        return self.value['fault']

    def finish(self, code):
        if self.value['status'] != 'failed':
            records=status(self.project)['records']
            root=self.value.get('root_subject') or next(
                (self.argv[i+1] for i,arg in enumerate(self.argv[:-1]) if arg=='--session'),self.value['subject'])
            completed=(records.get('run_state.json',{}).get('status')=='completed' if self.argv and self.argv[0]=='run'
                       else any(value.get('session_id')==root and value.get('status')=='completed' for value in records.values()))
            self.value['status'] = 'completed' if code == 0 and completed else 'stopped'
            self.value['returncode'] = code
            self.publish()

    def __exit__(self, kind, error, tb):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
        _active.reset(self.token)


def operation_boundary(project, phase, subject='', *, external=False, counted=True):
    observer = _active.get()
    if observer and Path(project).resolve() == observer.project:
        observer.step(phase, subject, external=external, counted=counted)


def milestone(project, identity):
    observer = _active.get()
    if observer and Path(project).resolve() == observer.project:
        with observer.lock:
            if identity not in observer.value['milestones']:
                observer.value['milestones'].append(identity)
                observer.value['progress_seq'] += 1
                store = BusinessStore(project)
                with store.connect() as db:
                    db.execute("INSERT INTO metadata VALUES('progress',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (canonical({'sequence':observer.value['progress_seq'],'milestones':observer.value['milestones']}),))
                observer.publish()


def accepted_milestones(relative,value):
    if relative.endswith('session_state.json') and value.get('status')=='completed':
        return ['completed:'+value['session_id']]
    if relative!='run_state.json':return []
    run_id=value.get('run_id','')
    values=['stage:'+run_id+':'+stage for stage,status in value.get('stage_statuses',{}).items() if status=='done']
    values += ['task:'+run_id+':'+task['task_id'] for task in value.get('tasks',[]) if task.get('status')=='done']
    if value.get('status')=='completed':values.append('run-completed:'+run_id)
    return values


def observe_record(project, relative, value):
    observer = _active.get()
    if not observer or Path(project).resolve() != observer.project:
        return
    if relative.endswith('session_state.json'):
        phase = value.get('status', '')
        waiting = phase if phase in {'waiting_user', 'paused'} else ''
        observer.value.update(waiting_for=waiting, subject=value.get('session_id', observer.value['subject']))
        if phase == 'completed':
            milestone(project, 'completed:' + value['session_id'])
        observer.publish()
    elif relative == 'run_state.json':
        run_id = value.get('run_id', '')
        observer.value.update(waiting_for=value.get('status') if value.get('status') in {'waiting_user','paused'} else '')
        for identity in accepted_milestones(relative,value):
            milestone(project,identity)
        observer.publish()


def status(project):
    project = Path(project).resolve()
    store = BusinessStore(project, readonly=True)
    records = {}
    pending = []
    if store.path.exists():
        with store.connect() as db:
            paths = [row['path'] for row in db.execute('SELECT path FROM records')]
        records = {path: dict(store.get(path)) for path in paths}
        pending = store.pending()
    else:
        for pattern in ('sessions/*/session_state.json', 'workflows/*/workflow.json', 'handoffs/*.json', 'run_state.json'):
            for path in store.root.glob(pattern):
                records[path.relative_to(store.root).as_posix()] = json.loads(path.read_text())
    return {'schema': 1, 'ok': True, 'project': str(project), 'identity': digest(records),
            'records': records, 'pending_external': pending}


def snapshot(project, destination):
    project, destination = Path(project).resolve(), Path(destination).resolve()
    if destination == project or project in destination.parents:
        raise ValueError('Snapshot destination must be outside the live project')
    if destination.exists():
        raise ValueError('Snapshot destination already exists')
    # Dependencies and business secrets are never recursively exported.
    ignore_patterns = shutil.ignore_patterns('.env', '.env.*', 'node_modules', '.conda', '.venv', '__pycache__',
                                    '.next', 'legacy-archive', 'business.sqlite3*', '*.sqlite3-wal', '*.sqlite3-shm')
    def ignored(directory,names):
        excluded=set(ignore_patterns(directory,names))
        if Path(directory).name=='.auto-agents':excluded.add('operator')
        return excluded
    shutil.copytree(project, destination, ignore=ignored, symlinks=True)
    if (project/'.git').exists():
        tracked=subprocess.run(['git','-C',str(project),'ls-files','-z'],capture_output=True,text=True,check=True).stdout
        for name in tracked.split('\0'):
            relative=Path(name)
            if '.next' in relative.parts and (project/relative).is_file():
                copied=destination/relative;copied.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(project/relative,copied)
    if (project / LEGACY_MARKER).exists():
        _migrate(project, check=True, export=destination)
    else:
        export_snapshot(project, destination)
    exported = status(destination)
    # Native business candidates may live outside the project. Keep their
    # logical paths and copy only repositories admitted by native custody.
    from .session_source import validate_checkout
    from .session_verification import fingerprint
    from .models import SessionState
    checkouts={}
    for name,record in exported['records'].items():
        if not name.endswith('session_state.json'):continue
        state=SessionState.from_dict(record)
        sources=[(record.get('candidate_custody',{}).get('checkout'),state.session_id)]
        descriptor=record.get('source_descriptor',{})
        sources.append((descriptor.get('checkout'),descriptor.get('session_id')))
        for path,owner in sources:
            if not path:continue
            original=Path(path)
            if not original.exists() or original.is_relative_to(project) or str(original) in checkouts:continue
            validate_checkout(project,state,original,owner_id=owner)
            relative=Path('.auto-agents/state/offline-checkouts')/fingerprint(str(original))/'project'
            copied=destination/relative
            copied.parent.mkdir(parents=True,exist_ok=True)
            shutil.copytree(original,copied,ignore=ignored,symlinks=True)
            custody=destination/'.auto-agents/state/custody'/(fingerprint(str(original))+'.json')
            registered=json.loads(custody.read_text())
            registered.update(device=copied.stat().st_dev,inode=copied.stat().st_ino)
            atomic(custody,registered)
            checkouts[str(original)]=relative.as_posix()
    atomic(destination/'.auto-agents/state/offline-checkouts.json',checkouts)
    for checkout in [destination,*[destination/path for path in checkouts.values()]]:
        config_file=checkout/'.auto-agents/config.json'
        if not config_file.is_file():continue
        config=json.loads(config_file.read_text())
        for provider in config.get('providers', {}).values():
            # Account secrets stay in the selected native CLI's private HOME.
            provider['environment'] = {}
        atomic(config_file, config)
    # The records and receipts are already in the copied database. Returning
    # every historical candidate preimage again can produce hundreds of MB
    # on stdout and exhaust both the exporter and its supervising process.
    return {'ok': True, 'project': str(project), 'snapshot': str(destination),
            'state': {**{key: value for key, value in exported.items() if key != 'records'},
                      'project': str(project)},
            'record_count': len(exported['records'])}


def offline_isolated():
    return (os.environ.get('AUTO_AGENTS_OFFLINE_RESUME')=='1'
            and os.environ.get('AUTO_AGENTS_WATCH_ISOLATED')=='1'
            and Path('/.dockerenv').exists())


def resume_check(project, resume_token):
    from .business_calls import OfflineBoundary
    from .cli_impl import main
    project = Path(project).resolve()
    # This command runs only inside the supervisor's credential-free sandbox.
    if not offline_isolated():
        raise ValueError('resume-check requires the offline isolation profile')
    token = json.loads(Path(resume_token).read_text())
    if token.get('schema') != 1 or not token.get('step_id') or not token.get('argv'):
        raise ValueError('Invalid resume checkpoint')
    if str(project) != token.get('project'):
        raise ValueError('Resume checkpoint belongs to another project')
    current = status(project)
    if current['pending_external']:
        return {'ok': False, 'blocked_step_cleared':False, 'external_calls':0,
                'category': 'reconciliation', 'reason': 'External outcomes are unresolved'}
    if current['identity'] != token['state_identity']:
        return {'ok': False, 'blocked_step_cleared':False, 'external_calls':0,
                'category': 'state', 'reason': 'Resume checkpoint changed'}
    args = list(token['argv'])
    if '--project' in args:
        args[args.index('--project') + 1] = str(project)
    with Observer(project, args) as observer:
        try:
            observer.step('resume' if '--session' in args or args[0]=='resume' else 'start',
                          next((args[i+1] for i,a in enumerate(args[:-1]) if a=='--session'),'run'))
            code = main(args)
        except OfflineBoundary as boundary:
            # A boundary reached before the old failing step is not recovery.
            cleared = token['step_id'] != boundary.operation['step_id'] and token['step_id'] in {
                row['step_id'] for row in observer.value['steps']}
            cleared = cleared and (not token.get('target_subject') or
                                   boundary.operation['subject'].split(':')[-1] == token['target_subject'])
            after = status(project)['records']
            preserved = all(name in after and all(after[name].get(key) == value for key,value in fields.items())
                            for name,fields in token.get('protected',{}).items())
            cleared = cleared and preserved
            return {'ok': cleared, 'blocked_step_cleared': cleared, 'external_calls': 0,
                    'next_operation': boundary.operation, 'steps': observer.value['steps'],
                    'retained_constraints':preserved}
        except Exception as error:
            from .gates import GateCommandInfrastructureError
            category=('state' if isinstance(error,BusinessStateError) else
                      'verification' if isinstance(error,GateCommandInfrastructureError) else 'engine')
            return {'ok': False, 'blocked_step_cleared':False, 'external_calls':0,
                    'category': category, 'type': type(error).__name__,
                    'reason': str(error), 'traceback': traceback.format_exc(), 'steps':observer.value['steps']}
    after = status(project)['records']
    preserved = all(name in after and all(after[name].get(key) == value for key,value in fields.items())
                    for name,fields in token.get('protected',{}).items())
    target = token.get('target_subject')
    completed = any((value.get('session_id') == target or value.get('run_id') == target)
                    and value.get('status') == 'completed'
                    for value in after.values()) if target and target != 'run' else any(
                        value.get('status') == 'completed' for name,value in after.items() if name == 'run_state.json')
    cleared = code == 0 and completed and preserved
    if cleared and target:
        from .models import SessionState
        from .session_acceptance import completed as accepted
        from types import SimpleNamespace
        for value in after.values():
            if value.get('session_id') == target and value.get('acceptance_execution'):
                cleared = accepted(SimpleNamespace(project_root=project),SessionState.from_dict(value))
    return {'ok': cleared, 'blocked_step_cleared': cleared, 'external_calls': 0,
            'retained_constraints': preserved, 'returncode': code}


def command(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['business-status', 'snapshot', 'resume-check', 'migrate-state', 'checkpoint', 'reconcile-call', 'quiesce'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--output')
    parser.add_argument('--resume-token')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--observation')
    parser.add_argument('--invocation')
    parser.add_argument('--call')
    parser.add_argument('--result')
    parser.add_argument('--confirm-cancelled', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'quiesce':
        from .run_lock import ProjectRunLock, _live_control_processes, _signal_groups, process_group_exists
        import signal
        lock=ProjectRunLock(Path(args.project))
        if lock._inherited_fd() is None:
            raise ValueError('quiesce requires the owning project lock descriptor')
        token=os.environ['AUTO_AGENTS_RUN_TOKEN']
        processes=_live_control_processes(lock.control_path,expected_project=str(lock.project_root),expected_token=token)
        groups=_signal_groups(processes,signal.SIGTERM)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and any(process_group_exists(group) for group in groups):
            time.sleep(.1)
        for group in groups:
            if process_group_exists(group):
                try:os.killpg(group,signal.SIGKILL)
                except ProcessLookupError:pass
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and any(process_group_exists(group) for group in groups):
            time.sleep(.1)
        value={'ok':not any(process_group_exists(group) for group in groups),'groups':len(groups)}
    elif args.action == 'business-status':
        value = status(args.project)
    elif args.action == 'snapshot':
        if not args.output:
            parser.error('--output is required')
        value = snapshot(args.project, args.output)
    elif args.action == 'resume-check':
        value = resume_check(args.project, args.resume_token)
    elif args.action == 'reconcile-call':
        if not args.call or bool(args.result) == args.confirm_cancelled:
            parser.error('reconcile-call requires --call and exactly one of --result or --confirm-cancelled')
        from .run_lock import ProjectRunLock
        from .models import AgentResult
        with ProjectRunLock(Path(args.project)):
            store = BusinessStore(args.project)
            with store.connect() as db:
                row = db.execute('SELECT state,model FROM calls WHERE id=?',(args.call,)).fetchone()
            if not row or not row['model'] or row['state'] not in {'unknown','dispatched'}:
                raise ValueError('Only an unresolved business model call can be reconciled')
            if args.confirm_cancelled:
                result = {'legacy_terminal':{'reason':'Operator confirmed the original request was cancelled'},
                          'redispatch_allowed':False}
            else:
                receipt = json.loads(Path(args.result).read_text())
                if receipt.get('operation_id') != args.call:
                    raise ValueError('Confirmed result belongs to another operation')
                result = receipt['result']
                AgentResult(**result)
                if type(result.get('ok')) is not bool or not isinstance(result.get('output_path'),str):
                    raise ValueError('Invalid confirmed native result')
            store.settle(args.call,result)
        value = {'ok':True,'operation_id':args.call,'status':'reconciled'}
    elif args.action == 'checkpoint':
        if not args.observation or not args.invocation:
            parser.error('checkpoint requires --observation and --invocation')
        observation = json.loads(Path(args.observation).read_text())
        if observation.get('project') != str(Path(args.project).resolve()):
            raise ValueError('Observation belongs to another project')
        invocation = json.loads(Path(args.invocation).read_text())
        with Observer(args.project, invocation) as observer:
            observer.value.update(observation)
            from .engine_fault import EngineFault
            fault = observer.fault(EngineFault('Repeated execution without progress',
                                              step_id=observation['step_id'],evidence={'monitor':'control_cycle'}))
        value = {'ok': True, 'fault': fault}
    else:
        value = migrate(args.project, check=args.check)
    print(canonical(value))
    return 0 if value.get('ok') else 3


class BusinessTelemetry:
    """Existing business phase callers emit observations, never launch a sidecar."""
    enabled = False
    def __init__(self, project_root, **kwargs):
        self.project_root = Path(project_root)
        self.enabled = bool(kwargs.get('enabled', True))
        self.subject_id = ''
    def start(self, subject_id=''): pass
    def bind_subject(self, subject_id):
        self.subject_id = subject_id
        observer = _active.get()
        if observer and self.project_root.resolve() == observer.project and subject_id:
            with observer.lock:
                if not observer.value.get('root_subject'):
                    observer.value['root_subject'] = subject_id
                observer.value['subject'] = subject_id
                observer.publish()
    def set_phase(self, phase):
        # Returning from a child is a phase transition, not another execution
        # of the same business operation. Actual call/verification boundaries
        # retain their repetition checks and never earn progress from routing.
        operation_boundary(self.project_root, phase, self.subject_id, counted=False)
    def set_active_operation(self, *args, **kwargs): pass
    def close(self, **kwargs): pass
    def check_action(self): return None


def gate_boundary(function):
    from functools import wraps
    @wraps(function)
    def wrapped(*args, **kwargs):
        return function(*args, **kwargs)
    return wrapped


def boundary_event(*args, **kwargs):
    return True
