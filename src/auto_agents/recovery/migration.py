"""One-way, sealed legacy import. Reading old state is not an execution path."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .model import Contract, Event, KernelError, canonical, digest, require


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def read(path, root):
    path, root = Path(path), Path(root).resolve()
    require(not path.is_symlink() and path.resolve().is_relative_to(root), 'migration_path', 'Control input leaves its repository')
    value = json.loads(path.read_text())
    require(isinstance(value, dict), 'migration_record', 'Control record must be an object')
    return value


def registered_projects(control):
    path = Path(control) / 'control.sqlite3'
    if not path.exists(): return []
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
        try: rows = db.execute('SELECT DISTINCT project FROM subscribers').fetchall()
        except sqlite3.OperationalError: rows = []
        try:
            activation = db.execute("SELECT value FROM kernel_meta WHERE key='activation'").fetchone()
            rows += [(p,) for p in json.loads(activation[0]).get('projects', [])] if activation else []
        except sqlite3.OperationalError: pass
    return sorted({str(Path(row[0]).resolve()) for row in rows if row[0]})


def inspect_project(project):
    project = Path(project).resolve()
    state = project / '.auto-agents/state'
    files, records, errors = {}, [], []
    if not project.is_dir(): errors.append({'path': str(project), 'code': 'missing_project', 'reason': 'Registered project is unavailable'})
    patterns = [('workflow', 'workflows/*/workflow.json'), ('session', 'sessions/*/session_state.json'),
                ('issue', 'sessions/*/issue.json'), ('handoff', 'handoffs/*.json'), ('run', 'run_state.json'),
                ('proof_review','proof-reviews/*/state.json'), ('event','workflows/*/events/*.json')]
    for kind, pattern in patterns:
        for path in sorted(state.glob(pattern)):
            name = path.relative_to(project).as_posix()
            try:
                before = file_digest(path); value = read(path, project)
                require(file_digest(path) == before, 'migration_changed', 'Input changed while inspecting')
                files[name] = before
                records.append({'kind': kind, 'path': name, 'sha256': before,
                    'identity': value.get('workflow_id') if kind == 'workflow' else
                                value.get('session_id') if kind == 'session' else
                                value.get('run_id') if kind == 'run' else value.get('handoff_id', ''),
                    'status': value.get('status', '')})
            except (OSError, ValueError, KernelError) as error:
                errors.append({'path': name, 'code': getattr(error, 'code', 'unreadable'), 'reason': str(error)})
    material = {'schema': 1, 'project': str(project), 'files': files, 'records': records, 'errors': errors}
    return {**material, 'identity': digest(material), 'ok': not errors}


def check(control, projects=()):
    selected = sorted({str(Path(p).resolve()) for p in projects} or set(registered_projects(control)))
    result = [inspect_project(project) for project in selected]
    active = []
    path = Path(control) / 'control.sqlite3'
    if path.exists():
        with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
            try:
                active = [{'job': row[0], 'state': row[1]} for row in db.execute(
                    "SELECT id,state FROM jobs WHERE state IN ('repairing','validating','resuming')")]
            except sqlite3.OperationalError: pass
    from ..repair_control import alive
    for pattern in ('jobs/*/*-lease.json', 'verifications/*/lease.json'):
        for lease_path in Path(control).glob(pattern):
            try:
                lease = read(lease_path, control)
                if alive(lease.get('pid', 0), lease.get('ticks', '')):
                    active.append({'lease': str(lease_path.relative_to(control)), 'state':'live_process'})
            except (OSError, ValueError, KernelError):
                active.append({'lease': str(lease_path.relative_to(control)), 'state':'unreadable_lease'})
    return {'schema': 1, 'ok': all(p['ok'] for p in result) and not active,
            'projects': result, 'active_operations': active, 'mutated_live_state': False}


def _event(store, stream, kind, data, key):
    event = Event('import:' + digest([stream, kind, key]), kind, data)
    return store.apply(stream, store.load(stream)['revision'], event)


def _contracts(kind):
    if kind == 'fix': return ('prepare', 'implement', 'verify', 'review', 'deliver'), 'candidate_delivered'
    if kind == 'provider_resolve': return ('prepare', 'research', 'verify', 'review', 'deliver'), 'candidate_delivered'
    if kind == 'run': return ('prepare', 'clarify', 'requirements', 'architecture', 'plan', 'implement', 'verify', 'review', 'deliver', 'acceptance'), 'goal_accepted'
    return ('prepare', 'clarify', 'route', 'acceptance'), 'goal_accepted'


def apply_project(store, manifest):
    """Import into a staging store. Does not activate it or rewrite project files."""
    project = Path(manifest['project'])
    require(inspect_project(project)['identity'] == manifest['identity'], 'migration_changed', 'Project changed since migration check')
    require(manifest['ok'], 'migration_blocked', 'Malformed control records require recovery before import')
    from .authority import installed
    active = installed(project)
    if active is not None:
        require(active.root == store.root, 'migration_owner', 'Project is owned by another recovery core')
        with store.connect() as db:
            streams = [r['stream'] for r in db.execute('SELECT DISTINCT stream FROM kernel_bindings WHERE project=?', (str(project),))]
        for stream in streams: store.replay(stream)
        with store.connect() as db:
            db.execute('INSERT OR IGNORE INTO kernel_migrations VALUES(?,?,?,?)',
                       (manifest['identity'], canonical(manifest), 'imported', time.time()))
        return [{'stream': stream, 'already_managed': True} for stream in streams]
    rows = {row['path']: (row, read(project / row['path'], project)) for row in manifest['records']}
    streams, imported = {}, []
    workflows = {value['workflow_id']: value for row, value in rows.values() if row['kind'] == 'workflow'}
    for workflow_id, workflow in workflows.items():
        events = sorted([value for row,value in rows.values() if row['kind'] == 'event' and value.get('workflow_id') == workflow_id],
                        key=lambda value:value.get('sequence',0))
        previous = ''
        for index, event in enumerate(events,1):
            material = {key:value for key,value in event.items() if key != 'event_sha256'}
            actual = hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            require(event.get('sequence') == index and event.get('previous_event_sha256') == previous
                    and event.get('event_sha256') == actual,'migration_journal','Legacy workflow journal is inconsistent')
            previous = actual
        require(workflow.get('event_sequence',0) == len(events) and workflow.get('last_event_sha256','') == previous,
                'migration_journal','Legacy workflow snapshot does not match its retained journal')
    for row, value in rows.values():
        if row['kind'] not in {'session', 'run'}: continue
        kind = value.get('mode', 'fix') if row['kind'] == 'session' else 'run'
        if kind == 'provider-resolve': kind = 'provider_resolve'
        require(kind in {'collab', 'fix', 'run', 'provider_resolve'}, 'migration_kind', 'Unsupported business entrypoint')
        native = value.get('session_id') or value.get('run_id')
        require(bool(native), 'migration_identity', 'Native task has no identity')
        wf = value.get('workflow_id') or value.get('resume_context', {}).get('workflow_id') or kind + ':' + native
        stream = 'wf:' + digest([str(project), wf])[:40]
        goal_id = 'goal:' + digest([str(project), wf])[:40]
        task_id = kind + ':' + native
        if not store.load(stream)['workflow_id']:
            _event(store, stream, 'workflow_registered', {'workflow_id': stream, 'goal_id': goal_id,
                'project': str(project)}, manifest['identity'])
        if manifest['identity'] not in store.load(stream)['imports']:
            reference = store.put(manifest)
            _event(store, stream, 'legacy_imported', {'manifest': manifest['identity'], 'record': reference}, manifest['identity'])
        streams[wf] = stream
        raw_ref = store.put_file(project / row['path'])
        root = workflows.get(wf, {}).get('root', {})
        goal = value.get('goal')
        if not goal and kind == 'run':
            specification = value.get('spec_file') or value.get('resume_context', {}).get('spec_file') or 'spec.md'
            spec = Path(specification)
            if not spec.is_absolute(): spec = project / spec
            if spec.is_file() and spec.resolve().is_relative_to(project): goal = spec.read_text()
        goal = goal or 'Retained run ' + native
        issue_name = '.auto-agents/state/sessions/' + native + '/issue.json'
        issue = rows.get(issue_name, ({}, {}))[1]
        command = value.get('fix_verify_command') or issue.get('verification_command')
        checks = [command] if command else list(value.get('verification_binding', {}).get('required_commands', []))
        if not checks:
            try:
                from ..config import load_project_config
                checks = [target for step in load_project_config(project).gates.steps for target in step.targets]
            except (OSError, ValueError, RuntimeError): pass
        if not checks: checks = ['retained:' + raw_ref]
        phases, completion = _contracts(kind)
        contract = Contract(goal_id, task_id, kind, store.put({'goal': goal, 'root': root}),
            store.put(issue or {'retained_state': raw_ref}), tuple(issue.get('constraints', [])), tuple(checks),
            (str(project),), store.put(value.get('authorization_policy') or {'mode': 'interactive'}),
            completion, phases, tuple(value.get('verification_binding', {}).get('task_scope', {}).get('task_ids', [])))
        _event(store, stream, 'task_bound', {'contract': contract.to_dict()}, [manifest['identity'], task_id])
        usage_marker = 'legacy-task-usage:' + digest([stream, task_id])
        if usage_marker not in store.load(stream)['imports']:
            attempts = int(value.get('current_attempt', 0))
            _event(store, stream, 'legacy_imported', {'manifest': usage_marker, 'record': raw_ref,
                'usage': {'model_calls': attempts, 'implementations': attempts if kind == 'fix' else 0}}, usage_marker)
        custody = value.get('candidate_custody') or {}; receipt = custody.get('receipt')
        failure = None
        if receipt:
            try:
                from ..models import SessionState
                from ..session_candidate import validate_receipt
                validate_receipt(SessionState.from_dict(value))
                _event(store, stream, 'candidate_retained', {'task_id': task_id,
                    'candidate_id': receipt['fingerprint'], 'source': receipt['source_revision'],
                    'base': custody['base_revision'], 'receipt': store.put(receipt)}, [manifest['identity'], task_id])
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                failure = {'kind': 'evidence_invalid', 'reason': str(error), 'evidence': [], 'details': {}, 'schema': 1}
        phase = 'verify' if receipt and 'verify' in phases else value.get('current_stage', 'prepare')
        if phase not in phases: phase = 'prepare'
        status = 'blocked' if failure else 'completed' if value.get('status') == 'completed' else 'blocked' if value.get('status') in {'paused','waiting_user','waiting_child'} else 'ready'
        _event(store, stream, 'task_restored', {'task_id': task_id, 'manifest': manifest['identity'],
            'legacy_ref': raw_ref, 'phase': phase, 'status': status, 'failure': failure,
            'attempts': int(value.get('current_attempt', 0))}, [manifest['identity'], task_id])
        counters = {k: value[k] for k in ('current_attempt',) if type(value.get(k)) is int}
        projection_ref = store.put({**value, 'status':'blocked', 'resolution':'migration_evidence_invalid'}) if failure else raw_ref
        _event(store, stream, 'projection_saved', {'name': row['kind'] + ':' + native,
            'blob': projection_ref, 'counters': counters, 'terminal': status == 'completed'}, [manifest['identity'], row['path']])
        store.bind(project, row['kind'] + ':' + native, stream)
        imported.append({'stream': stream, 'task_id': task_id, 'status': status})
    # Handoffs, issues and workflow graphs are immutable control projections too.
    for row, value in rows.values():
        if row['kind'] in {'session', 'run'}: continue
        wf = value.get('workflow_id')
        if row['kind'] == 'issue':
            sid = Path(row['path']).parent.name
            stream = store.binding(project, 'session:' + sid)
        else: stream = streams.get(wf)
        if stream is None: continue
        name = row['kind'] + ':' + (value.get('workflow_id') if row['kind'] == 'workflow' else
            Path(row['path']).parent.name if row['kind'] == 'issue' else value.get('handoff_id', Path(row['path']).stem))
        if row['kind'] == 'event': name = 'event:' + wf + ':' + Path(row['path']).stem
        reference = store.put_file(project / row['path'])
        _event(store, stream, 'projection_saved', {'name': name, 'blob': reference, 'counters': {},
            'terminal': False}, [manifest['identity'], row['path']])
        store.bind(project, name, stream)
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO kernel_migrations VALUES(?,?,?,?)',
                   (manifest['identity'], canonical(manifest), 'imported', time.time()))
    return imported


def import_repairs(store, control, project):
    """Closed engine incidents come from immutable live ACKs, not current phase."""
    path = Path(control) / 'control.sqlite3'
    if not path.exists():
        import_budgets(store,control,project)
        return []
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        try: jobs = db.execute('SELECT * FROM jobs ORDER BY updated').fetchall()
        except sqlite3.OperationalError: jobs = []
    imported = []
    for job in jobs:
        payload, result = json.loads(job['payload']), json.loads(job['result'])
        if payload.get('project') != str(Path(project).resolve()): continue
        invocation = payload.get('invocation', {})
        native = invocation.get('session_id') or invocation.get('run_id')
        stream = store.binding(project, ('session:' if invocation.get('session_id') else 'run:') + str(native))
        if not stream: continue
        marker = 'legacy-job:' + job['id']
        reference = store.put(dict(job))
        transaction = Path(result.get('v2_transaction', '/__unavailable__'))
        saved = {}
        try:
            from ..repair_v2.store import Store
            # Read only: do not construct the legacy store, which creates dirs.
            envelope = read(transaction / 'state.json', Path(control))
            from ..repair_v2.store import digest as legacy_digest
            require(legacy_digest(envelope['state']) == envelope['digest'], 'migration', 'Corrupt legacy checkpoint')
            saved = envelope['state']
        except (OSError, ValueError, KeyError, KernelError): pass
        _event(store, stream, 'legacy_imported', {'manifest': marker, 'record': reference}, marker)
        usage_key = 'legacy-usage:' + digest(str(transaction))
        if saved and usage_key not in store.load(stream)['imports']:
            _event(store, stream, 'legacy_imported', {'manifest': usage_key, 'record': reference,
                'usage': {'model_calls': int(saved.get('calls', 0)), 'implementations': int(saved.get('attempts', 0)),
                          'repair_model_calls':int(saved.get('calls',0)),
                          'repair_implementations':int(saved.get('attempts',0)), 'repair_transactions':1}}, usage_key)
        closed = False; recovery_ref = None
        try:
            ack = read(transaction / 'live-recovery.json', Path(control))
            require(ack['job'] == job['id'] and ack['generation'] == job['generation']
                    and ack['commit'] == result['commit'] and ack['artifact_id'] == result['runtime_artifact']['artifact_id'],
                    'migration', 'Legacy ACK binding mismatch')
            from ..repair_v2.integration import verify_receipt
            verify_receipt(result, expected_root=transaction)
            require(bool(result.get('engine_full_proof', {}).get('recovered')), 'migration', 'Recovery not confirmed')
            recovery_ref = store.put(ack); closed = True
        except (OSError, ValueError, KeyError, RuntimeError): pass
        state = store.load(stream)
        task_id = next((k for k in state['tasks'] if k.endswith(':' + str(native))), None)
        if not task_id: continue
        # A job/version is a delivery attempt, not a new failure occurrence.
        route = payload.get('boundary', {}).get('route_digest')
        identity = 'incident:' + digest([stream, native, route or payload.get('symptom_key') or
                                        payload.get('fingerprint') or str(transaction)])[:40]
        if identity in state['incidents']:
            if job['id'] not in state['incidents'][identity].get('legacy_jobs', []):
                _event(store, stream, 'incident_alias_imported', {'incident_id': identity,
                    'manifest': marker, 'legacy_job': job['id'], 'resolution_ref': recovery_ref,
                    'record_ref': reference, 'runtime': result.get('runtime_artifact')}, marker)
        else:
            _event(store, stream, 'incident_restored', {'incident_id': identity, 'task_id': task_id,
            'manifest': marker, 'record_ref': reference, 'status': 'resolved' if closed else 'open',
            'resolution_ref': recovery_ref, 'legacy_job': job['id'], 'legacy_jobs': [job['id']],
            'route_digest': payload.get('boundary', {}).get('route_digest', ''),
            'runtime': result.get('runtime_artifact'), 'previous': None}, marker)
        imported.append({'job': job['id'], 'incident': identity,
                         'resolved': store.load(stream)['incidents'][identity]['status'] == 'resolved'})
    import_budgets(store,control,project)
    return imported


def import_budgets(store, control, project):
    """Chain counters and proof-review reservations cannot disappear at import."""
    from ..repair_v2.store import digest as legacy_digest
    totals = {}
    for path in sorted((Path(control)/'repair-chains').glob('*/state.json')):
        envelope = read(path,control)
        require(legacy_digest(envelope['state']) == envelope['digest'],'migration_budget','Corrupt repair-chain checkpoint')
        saved = envelope['state']; owner = saved.get('identity',{})
        if owner.get('project') != str(Path(project).resolve()): continue
        stream = store.binding(project,owner.get('subject',''))
        if not stream: continue
        entry = totals.setdefault(stream,{'calls':0,'implementations':0,'transactions':set(),'limits':{},'records':[],'reviews':{}})
        transactions = saved.get('transactions',{})
        entry['calls'] += sum(int(t.get('model_calls',0)) for t in transactions.values())
        for key,count in saved.get('proof_reviews',{}).items(): entry['reviews'][key] = max(entry['reviews'].get(key,0),count)
        entry['implementations'] += sum(int(t.get('implementations',0)) for t in transactions.values())
        entry['transactions'].update(t['canonical'] for t in transactions.values())
        entry['records'].append(store.put_file(path))
        if saved.get('explicit_limits'):
            for key,value in saved.get('limits',{}).items():
                if value is not None: entry['limits'][key] = min(entry['limits'].get(key,value),value)
    for path in sorted((Path(project)/'.auto-agents/state/proof-reviews').glob('*/state.json')):
        envelope = read(path,project)
        require(legacy_digest(envelope['state']) == envelope['digest'],'migration_budget','Corrupt proof-review checkpoint')
        saved = envelope['state']; owner = saved.get('owner',{})
        if owner.get('project') != str(Path(project).resolve()): continue
        stream = store.binding(project,owner.get('subject',''))
        if not stream: continue
        entry = totals.setdefault(stream,{'calls':0,'implementations':0,'transactions':set(),'limits':{},'records':[],'reviews':{}})
        entry['reviews'][path.parent.name] = max(entry['reviews'].get(path.parent.name,0),int(saved.get('model_calls',0)))
        entry['records'].append(store.put_file(path))
    operator = Path(control)/'operator.json'
    policy = {}
    if operator.is_file():
        from ..repair_control import operator_policy
        policy = operator_policy(read(operator,control))
    with store.connect() as db:
        streams = [r['stream'] for r in db.execute('SELECT DISTINCT stream FROM kernel_bindings WHERE project=?',(str(Path(project).resolve()),))]
    for stream in streams:
        entry = totals.get(stream,{'calls':0,'implementations':0,'transactions':set(),'limits':{},'records':[],'reviews':{}})
        limits = policy.get('repair_chain_limits',entry['limits'])
        value = {'usage':{'repair_model_calls':entry['calls'] + sum(entry['reviews'].values()),'repair_implementations':entry['implementations'],
                          'repair_transactions':len(entry['transactions'])},'limits':limits,'records':entry['records'],
                 'policy':store.put(policy)}
        marker = 'legacy-budget:' + digest([stream,value])
        _event(store,stream,'legacy_imported',{'manifest':marker,'record':store.put(value)},marker)
        _event(store,stream,'legacy_budget_bound',{'manifest':marker,'usage':value['usage'],'limits':limits},marker)
