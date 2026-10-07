"""Durable business call receipts without maintenance scheduling or retry credit."""
from dataclasses import asdict, is_dataclass
from pathlib import Path
import hashlib
import json

from .business_state import BusinessStore, BusinessStateError, _subject, canonical, digest


class OfflineBoundary(BaseException):
    """An offline resume reached the next external operation without dispatch."""
    def __init__(self, operation):
        self.operation = operation


def context(owner, usage=None):
    # The retired kernel has no dispatch context. Business receipts are local.
    return None


def _location(owner, usage=None):
    usage = usage or {}
    root = (getattr(owner, '_custody_control_root', None) or getattr(owner, '_kernel_project', None)
            or getattr(owner, 'project_root', None))
    subject = usage.get('subject_id') or getattr(owner, '_kernel_subject', '') or _subject.get()
    state = getattr(owner, '_current_state', None)
    if state is not None:
        subject = getattr(state, 'session_id', '') or getattr(state, 'run_id', '') or subject
    if root and not subject:
        # Auxiliary business calls (preflight, rules, prototype) belong to the
        # current durable run even when their caller has no _current_state.
        store = BusinessStore(root, readonly=True)
        current = store.get('run_state.json')
        if current:
            subject = current.get('run_id', '')
    return Path(root).resolve() if root else None, str(subject)


def perform(owner, phase, key, function, classify=None, *, usage=None, model=False,
            bind=False, completion=False, read_only=False):
    if bind:
        raise BusinessStateError('business_contract', 'Business dispatch must use its native execution contract')
    root, subject = _location(owner, usage)
    if root is None:
        return function()
    if not subject:
        if model:
            raise BusinessStateError('business_identity', 'External calls require an explicit business subject')
        return function()
    from .supervision_api import operation_boundary
    if not model and phase != 'deliver':
        operation_boundary(root, phase, subject)
        return function()
    identity = str(key) if model else digest([subject, phase, key])
    store = BusinessStore(root)
    # Reading an already settled result performs no external operation. Offline
    # recovery must be able to consume it before reaching the original fault.
    with store.connect() as db:
        retained = db.execute('SELECT * FROM calls WHERE id=?', (identity,)).fetchone()
    if retained and retained['state'] == 'finished':
        operation_boundary(root, phase + ':' + identity, subject)
        return json.loads(retained['result'])
    operation_boundary(root, phase + ':' + identity, subject, external=True)
    prior = store.reserve(identity, subject, phase, model=model)
    if prior:
        if prior['state'] != 'finished':
            raise store.unconfirmed_call_error([
                {name: prior[name] for name in ('id', 'subject', 'phase')}
            ])
        return json.loads(prior['result'])
    from .models import ProvidersExhaustedError
    try:
        result = function()
        serialized = asdict(result) if is_dataclass(result) else result
        # Validate serializability before publishing the receipt.
        canonical(serialized)
        store.settle(identity, serialized)
        return result
    except ProvidersExhaustedError as error:
        # Exhaustion is a confirmed failure: adapters returned terminal
        # results, or no executable was available to dispatch. Preserve the
        # exception so replay retains the same contract without another call.
        if error.result is not None and error.result.cleanup_incomplete:
            store.settle(identity, None, state='unknown')
        else:
            store.settle(identity, {'providers_exhausted': {
                'message': str(error), 'providers': error.providers,
                'category': error.category,
                'result': asdict(error.result) if error.result is not None else None,
            }})
        raise
    except BaseException:
        store.settle(identity, None, state='unknown' if model or phase == 'deliver' else 'interrupted')
        raise


def provider(orchestrator, request, execute):
    from .models import ProvidersExhaustedError
    root, subject = _location(orchestrator, request.usage_context)
    phase = ('acceptance' if 'acceptance' in request.purpose else 'review' if 'review' in request.purpose
             else 'implement' if request.purpose in {'fix', 'implement'} else 'route' if request.purpose == 'collab'
             else 'research' if request.usage_context.get('workflow_kind') == 'provider_resolve' else 'plan')
    attachments = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in request.attachments]
    key = digest([request.attempt_id or str(request.output_path), str(request.prompt), request.purpose,
                  request.response_schema, attachments])
    kind = request.usage_context.get('workflow_kind') or getattr(getattr(orchestrator, '_current_state', None), 'mode', '')
    if not kind:
        kind = 'run' if str(subject).startswith('run:') else 'collab'
    native = subject.split(':', 1)[-1]
    identity = digest([kind, native, phase, key])
    result = perform(orchestrator, phase, identity, lambda: execute(request),
                     usage=request.usage_context, model=True)
    if isinstance(result, dict):
        if 'providers_exhausted' in result:
            failure = result['providers_exhausted']
            native = _agent_result(failure['result']) if failure['result'] is not None else None
            raise ProvidersExhaustedError(failure['message'], providers=failure['providers'],
                                         result=native, category=failure['category'])
        if result.get('legacy_terminal'):
            raise BusinessStateError('settled_terminal', result['legacy_terminal'].get('reason', 'The original request was cancelled'))
        return _agent_result(result)
    return result


def _agent_result(result):
    from .models import AgentResult, AgentTermination, AgentUsage
    value = dict(result)
    value['output_path'] = Path(value['output_path'])
    if isinstance(value.get('termination'), dict):
        value['termination'] = AgentTermination(**value['termination'])
    if isinstance(value.get('usage'), dict):
        value['usage'] = AgentUsage(**value['usage'])
    return AgentResult(**value)


def verification(owner, scope, execute):
    # Native verification receipts already bind source, commands and environment.
    root, subject = _location(owner)
    if root:
        from .supervision_api import operation_boundary
        operation_boundary(root, 'verify:' + scope, subject)
    result = execute()
    if root and result.get('ok'):
        from .supervision_api import milestone
        milestone(root, 'verification:' + subject + ':' + scope)
    return result


def delivery(owner, state, execute):
    # Native custody performs its own immutable receipt and delivery checks.
    from .supervision_api import operation_boundary
    root, subject = _location(owner)
    operation_boundary(root, 'deliver', subject, external=True)
    return execute()


def acceptance(owner, state):
    if state.status == 'completed':
        from .session_acceptance import completed
        if state.acceptance_execution and not completed(owner, state):
            raise BusinessStateError('acceptance_incomplete', 'Business completion lacks valid acceptance evidence')


def run_completion(owner, state):
    from .supervision_api import milestone
    if state.status == 'completed':
        milestone(owner.project_root, 'run-completed:' + state.run_id)


def recovered_route(orchestrator, payload):
    from .engine_fault import request_engine_repair
    return request_engine_repair(orchestrator, payload)


def review_candidate(owner, state, verification):
    """Native business checks stay in the business engine.

    Independent engine-patch review belongs to the external supervisor; this
    compatibility boundary must not add new paid calls to ordinary fix tasks.
    """
    return {'ok': bool(verification.get('ok')), 'reason': verification.get('reason', '')}


def session_stop(session, state):
    return None


def resume_rejected_diagnosis(session, state):
    return False


def recover_writer(project, state):
    return False


def retained_failure_commands(session, state, commands):
    return []


def legacy_scope(snapshot, task_id):
    return {}
