"""Crash recovery for scope calls; factual refreshes are not failed diagnoses."""
from copy import deepcopy
import json
import re

from .repair_control import digest
from .repair_feedback import sanitize_evidence

MAX_INTERRUPTION_RECOVERIES = 2


def _binding(context):
    from .repair_planning import finding_key
    return digest({**{key: context.get(key) for key in (
        'source', 'source_commit', 'environment', 'engine_base', 'contract_fingerprint',
        'runtime_capabilities', 'probe_results')},
        'findings': [finding_key(f) for f in context['findings']]})


def _prior_decisions(runner, context):
    from .repair_planning import POLICY_VERSION, _retained_scope, finding_key
    state = runner._experiment
    return all(
        old.get('verdict') in {'required', 'not_applicable', 'follow_up'}
        and old.get('policy') == POLICY_VERSION
        and old.get('contract') == state.contract_fingerprint
        and old.get('finding_key') == finding_key(finding)
        and _retained_scope(runner, old, finding)
        for finding in context['findings']
        for old in [state.scope_decisions.get(finding['finding_id'], {})])


def _completed_result(runner, call, context):
    """Only controller-admitted output can be recovered, never a raw provider file."""
    identity = call.get('request_id', '')
    if not re.fullmatch('[a-f0-9]{32}', identity):
        return None
    directory = runner._experiment_store.root / 'planning' / identity
    paths = [directory / name for name in ('request.json', 'input.json', 'result.json')]
    try:
        if directory.is_symlink() or directory.parent.is_symlink() or any(p.is_symlink() for p in paths):
            return None
        request, incoming, result = [json.loads(p.read_text()) for p in paths]
        if (request.get('stage') not in {'self_repair_scope_review', 'self_repair_scope_format'}
                or request.get('request_id') != identity
                or request.get('scope_input_digest') != digest(incoming)
                or request.get('scope_result_digest') != digest(result)
                or request.get('scope_result_exact') is not True
                or call.get('binding') != _binding(context)
                or request.get('scope_binding') != _binding(context)):
            return None
        if request['stage'] == 'self_repair_scope_format':
            from .repair_planning import _scope_format_changed
            if _scope_format_changed(incoming.get('previous_scope'), result,
                                     incoming.get('feedback', {}).get('field', '')):
                return None
        return result, identity
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def recover_scope_result(runner, workspace, context, key):
    from .repair_planning import _scope_dependencies
    saved = runner._experiment.planning_receipts[key]
    completed = _completed_result(runner, saved.get('scope_call', {}), context)
    if not completed:
        return None
    context['recovered_scope'] = {'result': completed[0], 'request_id': completed[1],
        'instruction': 'Recheck unresolved inputs independently; this draft is not an authorization.'}
    rows = completed[0].get('decisions')
    if not isinstance(rows, list):
        return None
    decisions = {row.get('finding_id'): row for row in rows if isinstance(row, dict)}
    if (context.get('scope_dependencies_complete') and all(
            _scope_dependencies(workspace, context, finding,
                decisions.get(finding['finding_id'], {})).get('complete')
            for finding in context['findings'])):
        return completed
    return None


def _exhausted(saved, context, key, message, code):
    from .repair_planning import PlanningBlocked
    reasons = {identity: row.get('reason') for identity, row in context.get('scope_revalidation', {}).items()}
    unresolved = context.get('scope_unresolved_dependencies', {})
    if reasons:
        finding, reason = next(iter(reasons.items()))
        message += f'; {finding}: {reason}'
        if unresolved.get(finding):
            item = unresolved[finding][0]
            message += '; ' + ':'.join(str(item.get(k, '')) for k in ('path', 'line', 'reason'))
    return PlanningBlocked(message, code=code,
        actual={'diagnosis_calls': saved.get('diagnosis_calls', 0),
                'call': saved.get('scope_call'), 'revalidation_reasons': reasons,
                'unresolved_dependencies': unresolved},
        constraint='complete the retained scope evidence or correct the reported input; restarting does not reset attempts',
        evidence=key)


def scope_call(runner, workspace, context, key, instruction):
    """Reserve a logical round before dispatch; transport retries keep that round."""
    from .repair_planning import MAX_PLAN_REVIEWS, _invoke
    from .repair_probe_recovery import MAX_PROBE_CORRECTIONS
    state = runner._experiment
    saved = state.planning_receipts.setdefault(key, {})
    # Keep the historical total verbatim. Old completed receipts authorize one
    # fresh factual review, not a reset of an exhausted diagnosis window.
    saved.setdefault('diagnosis_calls', state.planning_attempts.get(key, 0))
    call = saved.get('scope_call')
    if call and call.get('status') != 'validated':
        completed = recover_scope_result(runner, workspace, context, key)
        if completed:
            return completed
        if call.get('recoveries', 0) >= MAX_INTERRUPTION_RECOVERIES:
            raise _exhausted(saved, context, key, 'scope call recovery exhausted; retained evidence requires inspection',
                             'scope_recovery_exhausted')
        call.setdefault('interrupted_requests', []).append(call.get('request_id'))
        call['recoveries'] = call.get('recoveries', 0) + 1
        call.update(status='reserved', binding=_binding(context), request_id='')
    else:
        prior = _prior_decisions(runner, context)
        # Once a factual recheck returns unknown, the previous completed receipt
        # cannot authorize another refresh. Unknown outcomes use the diagnosis budget.
        mode = 'revalidation' if prior else 'diagnosis'
        extra = min(saved.get('probe_corrections', 0), MAX_PROBE_CORRECTIONS)
        if mode == 'diagnosis' and saved['diagnosis_calls'] >= MAX_PLAN_REVIEWS + extra:
            raise _exhausted(saved, context, key, 'scope diagnosis exhausted its bounded attempts',
                             'scope_diagnosis_exhausted')
        if mode == 'diagnosis':
            saved['diagnosis_calls'] += 1
        if call:
            saved.setdefault('scope_call_history', []).append(deepcopy(call))
        call = {'mode': mode, 'binding': _binding(context), 'status': 'reserved', 'recoveries': 0}
        saved['scope_call'] = call
    state.planning_attempts[key] = state.planning_attempts.get(key, 0) + 1
    runner._experiment_store.save(state)
    # _invoke binds the immutable request before calling the provider. A crash
    # between reservation and request creation still spends a recovery slot.
    context['scope_key'] = key
    try:
        return _invoke(runner, workspace, 'self_repair_scope_review', instruction, context)
    except BaseException as error:
        call['status'] = 'failed' if isinstance(error, Exception) else 'interrupted'
        call['failure'] = {'type': type(error).__name__, 'detail': getattr(error, 'detail', {})}
        try:
            runner._experiment_store.save(state)
        except OSError:
            pass  # ENOSPC may prevent this update; the durable running call is still recoverable.
        raise


def bind_request(runner, context, identity, projected):
    key = context.get('scope_key')
    if key:
        call = runner._experiment.planning_receipts[key]['scope_call']
        call.update(request_id=identity['request_id'], stage=identity['stage'], status='running')
        identity['scope_input_digest'] = digest(projected)
        identity['scope_binding'] = _binding(context)
        runner._experiment_store.save(runner._experiment)


def admit_result(identity, payload):
    if 'scope_input_digest' in identity:
        identity['scope_result_digest'] = digest(sanitize_evidence(payload))
        # A redacted executable probe is not the command the reviewer supplied.
        identity['scope_result_exact'] = digest(payload) == identity['scope_result_digest']
