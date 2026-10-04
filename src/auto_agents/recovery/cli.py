"""Maintenance actions never start a legacy repair just to inspect state."""
import json
from pathlib import Path

from .authority import installation_root
from .model import Event, digest, require
from .store import KernelStore


def maintenance(args):
    root = installation_root()
    require(root is not None, 'installation', 'No registered recovery installation')
    if args.repair_action == 'abandon':
        require(not args.job and not args.project, 'transaction_selection', 'Use --transaction to select one stopped V2 repair')
        from ..repair_v2.retirement import abandon
        return abandon(root, args.transaction, args.reason)
    if args.repair_action == 'migrate':
        require(args.check, 'migration_mode', 'Use migrate --check; activation belongs to the verified upgrade transaction')
        from .migration import check
        return check(root, [args.project] if args.project else ())
    if args.repair_action == 'upgrade':
        require(bool(args.runtime), 'runtime', '--runtime is required')
        from .release import upgrade
        store = KernelStore(root, readonly=True)
        trusted_runtime = store.meta('trusted_verifier_runtime') if store.meta('active_runtime') else None
        trusted = (trusted_runtime or {}).get('path') or str(Path(__file__).resolve().parents[3])
        return upgrade(root, Path(args.runtime), trusted)
    store = KernelStore(root, readonly=True)
    if store.meta('mode') == 'active':
        if args.repair_action == 'status': return {'ok': True, **store.status()}
        return control(KernelStore(root), args)
    return None


def control(store, args):
    from uuid import uuid4
    selected = []
    for state in store.status()['workflows']:
        if args.project and state['project'] != str(Path(args.project).resolve()): continue
        if args.job and args.job != state['workflow_id'] and not any(
                args.job in (row['incident_id'], row['task_id']) or args.job in row.get('legacy_jobs', [row.get('legacy_job')])
                for row in state['incidents'].values()): continue
        selected.append(state)
    require(selected and (args.job or args.project), 'workflow_selection', 'Select a retained --job or --project')
    if args.repair_action == 'cancel':
        for state in selected:
            store.apply(state['workflow_id'], state['revision'], Event('cancel:' + uuid4().hex,
                'workflow_stopped', {'status':'cancelled'}))
        from ..run_lock import stop_project_run
        results = [stop_project_run(Path(project))[0] for project in sorted({s['project'] for s in selected})]
        return {'ok': all(r['ok'] for r in results), 'status':'cancelled', 'processes':results}
    require(len(selected) == 1, 'workflow_selection', 'Select one retained workflow with --job')
    state = selected[0]
    if args.repair_action == 'reverify':
        from .engine_correction import retain
        require(args.runtime and args.job, 'candidate_selection',
                'Reverification requires --job and a committed --runtime candidate')
        return retain(store, state['workflow_id'], args.job, Path(args.runtime))
    if args.repair_action == 'retry-publish':
        from .publication import retry
        return retry(store, state, args.job)
    require(args.repair_action == 'resume', 'operation', 'Unsupported maintenance operation')
    from .executor import Executor
    for command in state['commands'].values():
        if command['status'] in {'running','unknown'}:
            Executor(store, {}).reconcile(state['workflow_id'], command['command_id'])
    state = store.load(state['workflow_id'])
    if state['status'] != 'active':
        store.apply(state['workflow_id'], state['revision'], Event('resume:' + uuid4().hex, 'workflow_resumed', {}))
    project = Path(state['project'])
    from ..run_lock import ProjectRunLock
    from ..orchestrator import Orchestrator
    from ..session import Session
    from ..config import load_session_state
    with ProjectRunLock(project) as run_lock:
        orch = Orchestrator(project)
        from .engine_adoption import adopted
        engine = [i for i in state['incidents'].values() if i.get('payload_ref') and
                  (i['status'] == 'open' or not adopted(store, state['workflow_id'], i)
                   or any(c['incident_id'] == i['incident_id'] and c['status'] == 'ready'
                          for c in state['continuations'].values()))]
        if engine:
            require(len(engine) == 1,'incident_selection','Select the engine incident to resume')
            payload = store.read(engine[0]['payload_ref'])
            from ..cli import build_parser
            from .engine import submit
            resume_args = build_parser().parse_args(payload['resume_argv'][2:])
            orch._invocation_context = payload['invocation']
            code = submit(store,project,orch,payload,resume_args,run_lock)
            return {'ok':code == 0,'status':'blocked' if code else 'completed'}
        workflows = [name.split(':',1)[1] for name in state['projections'] if name.startswith('workflow:')]
        if workflows:
            from ..workflow_runtime import WorkflowCoordinator
            result = WorkflowCoordinator(orch).resume_workflow(workflows[0])
        else:
            subjects = [name.split(':',1)[1] for name in state['projections'] if name.startswith('session:')]
            if subjects:
                require(len(subjects) == 1, 'workflow_selection', 'Legacy session graph requires explicit recovery')
                saved = load_session_state(project, subjects[0])
                result = Session(orch, mode=saved.mode, auto_approve=saved.auto_approve).resume(subjects[0])
            else: result = orch.resume_saved_run()
    return {'ok': result.status == 'completed', 'status':result.status, 'workflow':state['workflow_id']}
