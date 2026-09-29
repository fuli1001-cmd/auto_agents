"""Settle explicit pre-execution provider rejections from immutable receipts.

Timeouts, connection loss, generated output and tool activity remain uncertain.
No model request is repeated by this module, and usage is never refunded.
"""
import json
import re
from dataclasses import asdict, is_dataclass

from .model import Event, Outcome, OutcomeKind, require


def request_rejection(result):
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
    for item in objects:
        if not isinstance(item, dict): continue
        error = item.get('error') or {}
        if (item.get('status') == 400 and isinstance(error, dict)
                and error.get('type') == 'invalid_request_error' and error.get('code') == 'invalid_json_schema'
                and error.get('param') in {'text.format.schema', 'response_format', 'response_format.json_schema.schema'}):
            return {'status': 400, 'code': error['code'], 'parameter': error['param'],
                    'message': str(error.get('message', 'Invalid output schema'))[:2000]}
    return None


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
        rejection = request_rejection(store.read(reference))
        if not rejection: continue
        result = Outcome(OutcomeKind.PROTOCOL_INVALID, 'Provider rejected the request before model execution',
                         details={**details, 'request_rejection': rejection})
        executor = Executor(store, {command['phase']: FunctionExecutor(
            lambda c: None, lambda c, result=result: result)})
        executor.reconcile(stream, command['command_id'])
        settled.append(command['command_id'])
    if record:
        for command_id, command in store.load(stream)['commands'].items():
            if (command.get('outcome') or {}).get('details', {}).get('request_rejection'):
                note(store, stream, command_id)
    return settled
