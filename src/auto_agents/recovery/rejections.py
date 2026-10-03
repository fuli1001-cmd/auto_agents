"""Settle explicit pre-execution provider rejections from immutable receipts.

Timeouts, connection loss, generated output and tool activity remain uncertain.
No model request is repeated by this module, and usage is never refunded.
"""
import json
import re
from dataclasses import asdict, is_dataclass

from .model import Event, Outcome, OutcomeKind, require


def _pre_execution_objects(result):
    value = result if isinstance(result, dict) else {key: getattr(result, key, None) for key in
        ('ok', 'cleanup_incomplete', 'stdout', 'summary', 'stderr', 'termination', 'usage')}
    if value.get('ok') or value.get('cleanup_incomplete') or value.get('stdout') or value.get('summary'):
        return None
    termination = value.get('termination') or {}
    if is_dataclass(termination): termination = asdict(termination)
    if not isinstance(termination, dict): return None
    if termination.get('reason') != 'provider_error' or termination.get('active_tool'):
        return None
    usage = value.get('usage') or {}
    if is_dataclass(usage): usage = asdict(usage)
    if not isinstance(usage, dict): return None
    if any(isinstance(v, (int, float)) and v > 0 for k, v in usage.items() if 'token' in k):
        return None
    text = value.get('stderr', '')
    if not isinstance(text, str) or len(text) > 1_048_576: return None
    objects = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r'(?m)^\s*(?=\{)', text):
        try: item, _ = decoder.raw_decode(text, match.end())
        except ValueError: continue
        if not isinstance(item, dict): continue
        event_type = item.get('type', '')
        if event_type and event_type not in {'thread.started', 'turn.started', 'turn.failed', 'error'}:
            return None
        objects.append(item)
        error = item.get('error')
        nested = item.get('message') or (error.get('message') if isinstance(error, dict) else None)
        if isinstance(nested, str):
            try: objects.append(json.loads(nested))
            except ValueError: pass
    return objects


def request_rejection(result):
    for item in _pre_execution_objects(result) or []:
        if not isinstance(item, dict): continue
        error = item.get('error') or {}
        if (item.get('status') == 400 and isinstance(error, dict)
                and error.get('type') == 'invalid_request_error' and error.get('code') == 'invalid_json_schema'
                and error.get('param') in {'text.format.schema', 'response_format', 'response_format.json_schema.schema'}):
            return {'status': 400, 'code': error['code'], 'parameter': error['param'],
                    'message': str(error.get('message', 'Invalid output schema'))[:2000]}
    return None


def quota_rejection(result):
    """Accept only a complete Codex turn rejected before any agent activity."""
    value = result if isinstance(result, dict) else asdict(result)
    attempts, metadata = value.get('usage_attempts') or [], value.get('prompt_metadata') or {}
    if not isinstance(attempts, list) or not isinstance(metadata, dict) or len(attempts) > 1 or metadata.get('resumed'):
        return None
    objects = _pre_execution_objects(value) or []
    if (not all(isinstance(item, dict) for item in objects)
            or [item.get('type') for item in objects] != ['thread.started', 'turn.started', 'error', 'turn.failed']):
        return None
    message = objects[2].get('message')
    error = objects[3].get('error')
    if (not isinstance(message, str) or not isinstance(error, dict) or error.get('message') != message
            or not re.match(r"^You[’']ve hit your usage limit\.", message)):
        return None
    return {'code': 'usage_limit', 'message': message[:2000]}


def quota_reason(rejection):
    return 'Provider quota exhausted before execution: ' + rejection['message']


def quota_blocker(project, failure):
    """Attribute the latest failed operation using its sealed provider receipt."""
    from .authority import installed
    store = installed(project)
    if store is None: return None
    evidence = failure.evidence
    stream = store.binding(project, evidence['kind'] + ':' + evidence['subject_id'])
    if not stream: return None
    commands = store.load(stream)['commands'].values()
    command = max(commands, key=lambda row: row['sequence'], default={})
    outcome = command.get('outcome') or {}
    details = outcome.get('details') or {}
    if (command.get('status') != 'finished' or not command.get('model_call')
            or outcome.get('kind') != 'environment_blocked' or details.get('subject') != evidence['subject_id']
            or not details.get('provider_quota') or not details.get('native_result')
            or details.get('post_source') != command.get('source')):
        return None
    rejection = quota_rejection(store.read(details['native_result']))
    if rejection != details['provider_quota']: return None
    return {'reason': quota_reason(rejection), 'result_ref': details['native_result'],
            'command_id': command['command_id']}


def note(store, stream, command_id):
    state = store.load(stream)
    if 'recovery' not in state: return
    command = state['commands'][command_id]
    details = (command.get('outcome') or {}).get('details') or {}
    if command['phase'] != 'diagnose' or command['status'] != 'finished' or not details.get('request_rejection'):
        return
    from .convergence import scope
    if any(row['command_id'] == command_id for row in scope(state, command['task_id']).get('rejected_requests', [])):
        return
    store.apply(stream, state['revision'], Event('request-rejected:' + command_id, 'recovery_request_rejected', {
        'command_id': command_id, 'result_ref': details['native_result'], 'rejection': details['request_rejection']}))


def reconcile(store, stream, *, record=True):
    from .executor import Executor, FunctionExecutor
    settled = []
    for command in list(store.load(stream)['commands'].values()):
        outcome = command.get('outcome') or {}
        details = outcome.get('details') or {}
        reference = details.get('native_result')
        if (command['status'] != 'unknown' or not command['model_call'] or not reference
                or details.get('post_source') != command['source']):
            continue
        native = store.read(reference)
        rejection = request_rejection(native)
        quota = quota_rejection(native)
        if rejection:
            result = Outcome(OutcomeKind.PROTOCOL_INVALID, 'Provider rejected the request before model execution',
                             details={**details, 'request_rejection': rejection})
        elif quota:
            result = Outcome(OutcomeKind.ENVIRONMENT_BLOCKED, quota_reason(quota),
                             details={**details, 'provider_quota': quota})
        else:
            continue
        executor = Executor(store, {command['phase']: FunctionExecutor(
            lambda c: None, lambda c, result=result: result)})
        executor.reconcile(stream, command['command_id'])
        settled.append(command['command_id'])
    if record:
        for command_id, command in store.load(stream)['commands'].items():
            if (command.get('outcome') or {}).get('details', {}).get('request_rejection'):
                note(store, stream, command_id)
    return settled
