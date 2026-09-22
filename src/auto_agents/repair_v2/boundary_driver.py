"""Trusted offline resume probe, executed inside a disposable Docker container."""
import json
import hashlib
import os
from pathlib import Path
import runpy
import sys
import subprocess


DIAGNOSTIC_BINDING_CATEGORY = 'diagnostic_evidence_reference_binding_gap'
CONTINUATION_CATEGORIES = {'iteration_plan_scope_mismatch', 'provider_reference_freshness_validity_conflation',
                           DIAGNOSTIC_BINDING_CATEGORY}


class ReplayEnvironmentUnavailable(RuntimeError):
    pass


def diagnostic_continuation_complete(observed, original):
    """Reject older clearance-only reports even if their top-level flag is true."""
    proof = observed.get('recovery_observation') or {}
    submission = proof.get('submission_receipt') or {}
    entry = proof.get('implementation_entry') or {}
    runtime = observed.get('engine_runtime') or {}
    workflow = original.get('resume_context', {}).get('workflow_id')
    run = original.get('run_id')
    return bool(
        observed.get('ok') is True and proof.get('ok') is True
        and proof.get('implementation_entered') is True and proof.get('entry_event_ref')
        and entry.get('type') == 'implementation.entered' and entry.get('event_id')
        and runtime.get('ok') is True and runtime.get('commit')
        and entry.get('data', {}).get('engine_runtime', {}).get('repository_head') == runtime.get('commit')
        and submission.get('engine_commit') == runtime.get('commit')
        and submission.get('accepted') is True and submission.get('job_state') == 'queued' and submission.get('job_id')
        and submission.get('scope_receipt_digest') and submission.get('event', {}).get('event_id')
        and run and workflow
        and all(row.get('run_id') == run and row.get('workflow_id') == workflow
                for row in (observed, proof, submission, entry.get('data', {})))
    )


def check_environments(request):
    result = []
    program = 'import json,sys;print(json.dumps({"prefix":sys.prefix,"version":list(sys.version_info[:3])}))'
    for environment in request.get('replay_environments', []):
        prefix = Path(environment['prefix'])
        if environment.get('kind') == 'node-dependencies':
            if prefix.name != 'node_modules' or not prefix.is_dir():
                raise ReplayEnvironmentUnavailable('隔离恢复中的 Node 验证依赖不可用：' + str(prefix))
            result.append(environment)
            continue
        try:
            completed = subprocess.run([str(prefix / 'bin/python'), '-I', '-S', '-c', program],
                                       capture_output=True, text=True, timeout=20)
            observed = json.loads(completed.stdout)
            if completed.returncode or Path(observed['prefix']).resolve() != prefix.resolve():
                raise ValueError('interpreter does not belong to the captured prefix')
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            raise ReplayEnvironmentUnavailable('隔离验证环境中的 Python 无法启动或环境归属不匹配：' + str(prefix)) from error
        result.append({**environment, 'interpreter': observed})
    return result


def emit(payload):
    Path('/result/boundary.json').write_text(json.dumps(payload, ensure_ascii=False))
    print(json.dumps(payload, ensure_ascii=False))


def requires_child_recovery(route):
    sources = [route]
    while sources:
        source = sources.pop()
        if source.get('failed_handoff_id') or source.get('child_session_id'):
            return True
        sources.extend(source[key] for key in ('issue_seed', 'spec_seed', 'fix_disposition')
                       if isinstance(source.get(key), dict))
    return False


def run_plan_contract(plan):
    """Delivery obligations that reconciliation/entry must not rewrite."""
    tasks = []
    for task in plan.get('tasks', []):
        row = {key: task.get(key, []) for key in (
            'task_id', 'acceptance', 'requirement_ids', 'requirement_proofs', 'verification_refs')}
        # TaskSpec's existing storage contract serializes migration declarations
        # as strings. Compare that representation without dropping any entries.
        row['expected_test_migrations'] = [str(item) for item in task.get('expected_test_migrations', [])]
        tasks.append(row)
    return {
        'tasks': tasks,
        'verification_steps': plan.get('verification_steps', []),
        'verification_commands': plan.get('verification_commands', []),
    }


def run_input_hashes(target, original):
    from auto_agents.config import requirements_trace_path
    spec = Path(original.resume_context['spec_file'])
    if not spec.is_absolute():
        spec = target / spec
    return {
        'spec_file': str(spec.resolve()),
        'spec_sha256': hashlib.sha256(spec.read_bytes()).hexdigest(),
        'requirements_trace_sha256': hashlib.sha256(requirements_trace_path(target).read_bytes()).hexdigest(),
    }


def observe_run_continuation(orchestrator, original, original_plan, request, runtime, frozen_inputs,
                             submission=None):
    """Observe the retained workflow at implementation or its next real prerequisite.

    The caller retains its provider fence and disposable original project. No
    workflow stage, admission guard or task loader is replaced by this observer.
    """
    from auto_agents.config import load_run_state, load_task_plan, requirements_trace_path, task_plan_path
    from auto_agents.workflow_chain import WorkflowStore
    from auto_agents.workflow_runtime import WorkflowCoordinator

    class ContinuationBoundaryObserved(BaseException):
        pass

    target = orchestrator.project_root
    context = original.resume_context
    if request.get('invocation', {}).get('run_id') != original.run_id:
        raise RuntimeError('original run identity does not match the recovery request')
    workflow_id = str(context.get('workflow_id') or '')
    requested_workflow = request.get('invocation', {}).get('workflow_id')
    if not workflow_id or (requested_workflow and requested_workflow != workflow_id):
        raise RuntimeError('original run workflow identity is missing or mismatched')
    snapshot = WorkflowStore(target).load(workflow_id)
    if (snapshot.root.kind != 'run' or snapshot.root.native_id != original.run_id
            or snapshot.active_frame.kind != 'run' or snapshot.active_frame.native_id != original.run_id):
        raise RuntimeError('retained workflow is not positioned at the original run')
    if not runtime.get('ok') or runtime.get('commit') != request['commit']:
        raise RuntimeError('continuation runtime does not match the verified candidate')
    spec = Path(context['spec_file'])
    if not spec.is_absolute():
        spec = target / spec
    spec = spec.resolve()
    spec_hash = hashlib.sha256(spec.read_bytes()).hexdigest()
    trace_path = requirements_trace_path(target)
    trace_before = trace_path.read_bytes()
    if run_input_hashes(target, original) != frozen_inputs:
        raise RuntimeError('original run inputs changed during reconciliation')
    diagnostic = original.active_blocker.get('category') == DIAGNOSTIC_BINDING_CATEGORY
    require_admission = original.active_blocker.get('category') == 'provider_reference_freshness_validity_conflation'
    requeued = []
    if diagnostic:
        if (not isinstance(submission, dict) or submission.get('accepted') is not True
                or submission.get('job_state') != 'queued' or not submission.get('job_id')
                or not submission.get('subscriber_id') or not submission.get('scope_receipt_digest')
                or not submission.get('diagnosis_digest') or not submission.get('request_digest')
                or not submission.get('event_ref') or not submission.get('event', {}).get('event_id')
                or submission.get('run_id') != original.run_id
                or submission.get('workflow_id') != workflow_id
                or submission.get('engine_commit') != request['commit']):
            raise RuntimeError('retained diagnostic recovery lacks supervisor-accepted submission evidence')
        submitted_event = submission['event']
        submitted_path = Path('.auto-agents/runs') / original.run_id / 'events.jsonl'
        if (submission['event_ref'] != submitted_path.as_posix()
                or submitted_event.get('type') != 'repair.submitted'
                or submitted_event.get('subject_id') != original.run_id
                or any(submitted_event.get('data', {}).get(key) != submission.get(key) for key in (
                    'job_id', 'subscriber_id', 'run_id', 'workflow_id', 'engine_commit',
                    'scope_receipt_digest', 'diagnosis_digest'))):
            raise RuntimeError('submission receipt conflicts with its persisted acknowledgment')
        with (target / submitted_path).open() as stream:
            matches = [row for line in stream if line.strip()
                       if (row := json.loads(line)).get('event_id') == submitted_event['event_id']]
        if matches != [submitted_event]:
            raise RuntimeError('submission receipt acknowledgment is missing or changed')
        prepared = load_run_state(target)
        if prepared.pending_approval or prepared.active_input_request_id or prepared.pending_input_requests:
            raise RuntimeError('retained diagnostic recovery is awaiting human input')
        route = prepared.last_recovery_route.get('diagnostic_evidence_repair', {})
        repaired = route.get('repaired_blocker', {})
        requeued = repaired.get('requeued_task_ids', [])
        blocked_ids = {task['task_id'] for task in original_plan['tasks'] if task.get('status') == 'blocked'}
        if (route.get('outcome') != 'admission_retry_ready' or route.get('run_id') != original.run_id
                or route.get('workflow_id') != workflow_id
                or repaired.get('category') != DIAGNOSTIC_BINDING_CATEGORY
                or repaired.get('self_repair_commit') != request['commit']
                or repaired.get('prepared_self_repair_commit') != request['commit']
                or not isinstance(requeued, list) or not requeued
                or any(not isinstance(task_id, str) or task_id not in blocked_ids for task_id in requeued)
                or len(set(requeued)) != len(requeued)):
            raise RuntimeError('diagnostic recovery requeued tasks outside the retained repair contract')
    required_references = {b['reference'] for b in orchestrator.provider_research_blockers(
        requirement_ids=orchestrator._current_provider_research_requirement_ids(original))} if require_admission else set()
    accepted_events = ({'implementation.entered'} if require_admission or diagnostic
                       else {'implementation.entered', 'provider_research.required'})
    event_path = target / '.auto-agents/runs' / original.run_id / 'events.jsonl'
    offset = event_path.stat().st_size if event_path.exists() else 0
    emit_event = orchestrator.reporter.event

    def observe(kind, data, **options):
        emit_event(kind, data, **options)
        if diagnostic and kind == 'provider_research.required':
            raise RuntimeError('diagnostic recovery reached a prerequisite, not implementation entry')
        if kind in accepted_events:
            raise ContinuationBoundaryObserved()

    orchestrator.reporter.event = observe
    try:
        try:
            WorkflowCoordinator(orchestrator).resume_workflow(workflow_id)
        except ContinuationBoundaryObserved:
            pass
    finally:
        orchestrator.reporter.event = emit_event

    state = load_run_state(target)
    plan = load_task_plan(target)
    events = ([json.loads(line) for line in event_path.read_bytes()[offset:].splitlines()]
              if event_path.exists() else [])
    entries = [event for event in events if event.get('type') in accepted_events]
    if len(entries) != 1:
        raise RuntimeError('original workflow did not persist a fresh implementation entry or prerequisite boundary')
    entry = entries[0]
    data = entry.get('data', {})
    implemented = entry.get('type') == 'implementation.entered'
    stage = 'implement' if implemented else 'provider_research'
    prerequisites = data.get('prerequisites', [])
    actual_prerequisites = (orchestrator.provider_research_blockers(
        requirement_ids=orchestrator._current_provider_research_requirement_ids(state))
        if not implemented else [])
    task_ids = [task['task_id'] for task in original_plan['tasks']]
    pending_ids = [task['task_id'] for task in original_plan['tasks']
                   if task.get('status') == 'pending' or task['task_id'] in requeued]
    plan_hash = hashlib.sha256(task_plan_path(target).read_bytes()).hexdigest()
    expected_module = Path(runtime['runtime_root']) / 'src/auto_agents/orchestrator.py'
    engine = data.get('engine_runtime', {})
    preserved = (run_plan_contract(plan) == run_plan_contract(original_plan)
                 and trace_path.read_bytes() == trace_before
                 and hashlib.sha256(spec.read_bytes()).hexdigest() == spec_hash
                 and state.agent_attempts == original.agent_attempts)
    if diagnostic:
        expected_status = {task['task_id']: 'pending' if task['task_id'] in requeued else task.get('status', 'pending')
                           for task in original_plan['tasks']}
        histories = {task.task_id: (task.verify_history, task.review_history) for task in original.tasks}
        preserved = (preserved and {task.task_id: task.status for task in state.tasks} == expected_status
                     and state.localized_blockers == original.localized_blockers
                     and state.task_failure_checkpoints == original.task_failure_checkpoints
                     and {task.task_id: (task.verify_history, task.review_history) for task in state.tasks} == histories)
    entered = (
        state.run_id == original.run_id and state.resume_context.get('workflow_id') == workflow_id
        and state.current_stage == ('implement' if implemented else 'plan') and state.status not in {'blocked', 'failed', 'paused', 'waiting_user'}
        and not state.active_blocker and 'plan' in state.stage_summaries
        and (('provider_research' in state.stage_summaries) if implemented else
             ('provider_research' not in state.stage_summaries and bool(prerequisites)
              and prerequisites == actual_prerequisites))
        and [task.task_id for task in state.tasks] == task_ids
        and bool(pending_ids) and data.get('pending_task_ids') == pending_ids
        and data.get('task_ids') == task_ids and data.get('run_id') == original.run_id
        and data.get('workflow_id') == workflow_id and data.get('spec_file') == str(spec)
        and data.get('spec_sha256') == spec_hash and data.get('plan_sha256') == plan_hash
        and entry.get('event_id') and entry.get('subject_id') == original.run_id
        and entry.get('stage_id') == stage
        and engine.get('repository_head') == request['commit']
        and engine.get('orchestrator_module') == str(expected_module.resolve())
        and engine.get('orchestrator_sha256') == hashlib.sha256(expected_module.read_bytes()).hexdigest()
    )
    admission = None
    if require_admission:
        from auto_agents.config import provider_references_lock_path
        admissions = [event for event in events if event.get('type') == 'provider_references.admitted']
        if len(admissions) != 1:
            raise RuntimeError('original workflow lacks a fresh provider-review admission receipt')
        admission = admissions[0]
        bound = admission.get('data', {})
        paths = bound.get('document_sha256', {})
        valid_paths = isinstance(paths, dict) and all(
            isinstance(path, str) and not Path(path).is_absolute() and '..' not in Path(path).parts
            and path.startswith('.auto-agents/docs/provider_references/')
            and (target / path).resolve().is_relative_to(target.resolve()) for path in paths)
        valid_admission = (
            admission.get('event_id') and admission.get('subject_id') == original.run_id
            and admission.get('stage_id') == 'provider_research'
            and bound.get('run_id') == original.run_id and bound.get('workflow_id') == workflow_id
            and bound.get('lock_sha256') == hashlib.sha256(provider_references_lock_path(target).read_bytes()).hexdigest()
            and valid_paths and set(bound.get('references', [])) == set(paths)
            and required_references.issubset(paths)
            and all(hashlib.sha256((target / path).read_bytes()).hexdigest() == checksum for path, checksum in paths.items())
            and events.index(admission) < events.index(entry)
            and not orchestrator.provider_research_blockers(
                requirement_ids=orchestrator._current_provider_research_requirement_ids(state))
        )
        if not valid_admission:
            raise RuntimeError('provider-review receipt does not attest the original run admission')
    if not preserved or not entered:
        raise RuntimeError('original workflow continuation failed identity or preservation checks')
    return {
        'ok': True, 'run_id': state.run_id, 'workflow_id': workflow_id,
        'status': state.status, 'current_stage': state.current_stage,
        'engine_runtime': runtime,
        'recovery_observation': {
            'ok': True, 'boundary_kind': 'implementation' if implemented else 'provider_research', 'run_id': state.run_id,
            'workflow_id': workflow_id, 'implementation_entry': entry if implemented else None,
            'continuation_entry': entry, 'implementation_entered': implemented,
            'provider_admission': admission,
            'continuation_status': 'implementation_entered' if implemented else 'prerequisite_required',
            'prerequisites': prerequisites,
            'entry_event_ref': str(event_path.relative_to(target)),
            'spec_sha256': spec_hash, 'accepted_plan_sha256': plan_hash,
            'requirements_trace_sha256': frozen_inputs['requirements_trace_sha256'],
            'accepted_task_ids': task_ids, 'retained_constraints': True,
            'oracle_proof_count': sum(len(task.get('requirement_proofs', [])) for task in plan['tasks']),
            'verification_step_count': len(plan.get('verification_steps', [])),
            'submission_receipt': submission,
        },
    }


def main():
    home = Path(os.environ['HOME'])
    home.mkdir(parents=True, exist_ok=True)
    (home / '.gitconfig').write_text('[user]\n name = auto-agents verification\n email = verification@localhost\n')
    request = json.loads(Path('/result/request.json').read_text())
    environment_inputs = check_environments(request)
    invocation = request.get('invocation', {})
    sys.path.insert(0, '/work/src')
    target = Path(request.get('_replay_project', '/target'))
    case = request.get('repair_case') or {}
    bound_child = requires_child_recovery(invocation.get('engine_route') or {})
    if bound_child and not invocation.get('session_id'):
        raise RuntimeError('bound child recovery requires the retained session entrypoint')
    scoped_run = False
    if invocation.get('run_id'):
        from auto_agents.config import load_run_state
        scoped_run = load_run_state(target).active_blocker.get('category') in CONTINUATION_CATEGORIES
    if case.get('progress_history') and not bound_child and not scoped_run:
        from auto_agents.health_watch import replay_health_events
        items = replay_health_events(case['progress_history'], progress_lease_seconds=60)
        emit({'ok': True, 'status': 'health_trajectory_replayed',
              'anomalies': [item.to_dict() for item in items]})
        return
    if invocation.get('session_id'):
        if invocation.get('engine_route'):
            import hashlib
            def digest(value):
                return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            marker = target / '.auto-agents/engine-route-probe.json'
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({'route_digest': digest(invocation['engine_route']),
                                          'engine_route': invocation['engine_route'],
                                          'engine_commit': request['commit']}))
            os.environ['AUTO_AGENTS_REPAIR_ROUTE_PROBE'] = str(marker)
        sys.argv = ['session_replay', '/work', str(target), invocation['session_id'],
                    invocation.get('command', 'collab').replace('provider-resolve', 'fix')]
        # This copy of the harness belongs to the pinned controller, not /work.
        namespace = runpy.run_path('/opt/repair/session_replay.py', run_name='repair_boundary_harness')
        observed = namespace['main']()
        observed['environment_inputs'] = environment_inputs
        emit(observed)
        return
    if invocation.get('run_id'):
        from auto_agents.config import load_run_state, load_task_plan, save_run_state
        from auto_agents.orchestrator import Orchestrator
        original = load_run_state(target)
        original_plan = load_task_plan(target)
        if original.run_id != invocation['run_id']:
            raise RuntimeError('frozen run identity changed')
        blocked = {t.task_id for t in original.tasks if t.status in ('blocked', 'failed')}
        before = dict(original.active_blocker)
        if not before and not blocked:
            raise RuntimeError('frozen run has no original blocked boundary')
        try:
            frozen_inputs = (run_input_hashes(target, original)
                             if before.get('category') in CONTINUATION_CATEGORIES else None)
        except FileNotFoundError as error:
            raise ReplayEnvironmentUnavailable('retained continuation input is absent: ' + str(error)) from error
        orchestrator = Orchestrator(target)
        def forbidden(*args, **kwargs):
            raise RuntimeError('offline boundary verification must not invoke providers')
        orchestrator._call_with_failover = forbidden
        submission = None
        if before.get('category') == DIAGNOSTIC_BINDING_CATEGORY:
            identity = runpy.run_path('/opt/repair/repair_runtime_identity.py')
            runtime = identity['observe_engine'](Path('/work'), expected_commit=request['commit'])
            if not runtime.get('ok'):
                raise RuntimeError('submission runtime does not match the verified candidate')
            replay = runpy.run_path(str(Path(__file__).with_name('diagnostic_replay.py')))
            try:
                diagnosis, _ = replay['retained_diagnosis'](target, request)
            except FileNotFoundError as error:
                raise ReplayEnvironmentUnavailable(str(error)) from error
            submission = replay['observe_submission'](orchestrator, original, diagnosis, request, runtime, Path('/result'))
        state = orchestrator.mark_self_repair_applied(request['commit'])
        changed = orchestrator._resume_blocked_run(state)
        save_run_state(target, state)
        after = state.active_blocker or {}
        remains = {t.task_id for t in state.tasks if t.status in ('blocked', 'failed')}
        same = bool(after and any(after.get(k) and after.get(k) == before.get(k)
                                  for k in ('fingerprint', 'category')))
        ok = not same and (bool(blocked - remains) if blocked else bool(changed))
        observed = {'ok': ok, 'run_id': state.run_id, 'status': state.status,
                    'remaining_blocked': sorted(remains), 'same_blocker': same}
        if ok and before.get('category') in CONTINUATION_CATEGORIES:
            # Keep the existing reconciliation checks, then prove the stronger
            # postcondition through the saved workflow. This probe is pinned by
            # the controller, independently of the candidate engine imports.
            identity = runpy.run_path('/opt/repair/repair_runtime_identity.py')
            runtime = identity['observe_engine'](Path('/work'), expected_commit=request['commit'])
            observed.update(observe_run_continuation(
                orchestrator, original, original_plan, request, runtime, frozen_inputs, submission=submission
            ))
        emit(observed)
        return
    # Explicit engine-only repairs have no project session to resume. Their
    # requested behavior is checked by mandatory tests and independent review.
    if invocation.get('engine_route'):
        emit({'ok': True, 'status': 'engine_only', 'resume_required': False})
        return
    raise RuntimeError('repair lacks a supported original resume boundary')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit({'ok': False, 'error': str(error), 'error_type': type(error).__name__,
              'infrastructure': isinstance(error, ReplayEnvironmentUnavailable)})
        raise SystemExit(1)
