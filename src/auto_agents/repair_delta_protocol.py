"""Recover malformed delta replies without repeating the substantive review."""
from copy import deepcopy
import re

from .repair_memory import read_record, save_record


def approval(payload):
    return (payload.get('decision') == 'APPROVE' and payload.get('implementation_required') is False
            and payload.get('remaining_changes') == [] and payload.get('findings') == []
            and payload.get('deferred_findings', []) == [] and not payload.get('revalidation_protocol_error'))


def validate_reply(runner, payload, context, proof):
    from .repair_planning import PlanFormatError, PlanningBlocked, _text, _validate_scope, finding_key
    for key, valid in (
        ('decision', payload.get('decision') in ('APPROVE', 'REJECT')),
        ('reason', _text(payload.get('reason'))),
        ('implementation_required', isinstance(payload.get('implementation_required'), bool)),
        *((key, isinstance(payload.get(key, [] if key == 'deferred_findings' else None), list))
          for key in ('remaining_changes', 'findings', 'deferred_findings', 'decisions')),
    ):
        if not valid:
            raise PlanFormatError('invalid delta review field: ' + key, field=key,
                                  actual=payload.get(key), constraint='the declared delta review schema')
    if not approval(payload):
        return payload  # A substantive rejection still takes the full repair path.
    pending = context['findings']
    ids = {f['finding_id'] for f in pending}
    rows = payload['decisions']
    extras = [row for row in rows if isinstance(row, dict) and row.get('finding_id') not in ids]
    if extras:
        if any(row.get('verdict') in ('required', 'unknown') for row in extras):
            return {**payload, 'revalidation_protocol_error': {
                'code': 'delta_scope_conflict',
                'message': 'extra scope rows report required or unresolved work; full review must inspect them'}}
        # Some reviewers repeat resolved findings from the immutable old receipt.
        # Project only grounded nonblocking repetitions of that exact evidence.
        # Unknown IDs, new/changed defects, duplicate decisions and contradictory
        # verdicts remain errors; no additional scope waiver is recorded.
        findings = runner._experiment.findings
        redundant = []
        for row in extras:
            identity = row.get('finding_id')
            finding = findings.get(identity) if isinstance(identity, str) else None
            if (finding is None or identity not in proof.get('resolved', [])
                    or proof.get('findings', {}).get(identity) != finding_key(finding)
                    or finding.status == 'reopened' or row.get('verdict') != 'not_applicable'
                    or not _text(row.get('disproof'))):
                break
            redundant.append(finding.to_dict())
        else:
            _validate_scope({'decisions': extras}, redundant, runner._experiment.contract_obligation_ids)
            payload = {**payload, 'decisions': [row for row in rows if row not in extras]}
    try:
        _validate_scope(payload, pending, runner._experiment.contract_obligation_ids)
    except PlanFormatError:
        raise
    except PlanningBlocked as error:
        return {**payload, 'revalidation_protocol_error': error.detail}
    return payload


def format_changed(before, after, field):
    """A correction cannot discard or revise any unreported factual decision."""
    if before is None:
        return False  # Invalid JSON has no admitted substantive fields yet.
    left, right = deepcopy(before), deepcopy(after)
    match = re.fullmatch(r'decisions\[(\d+)\]\.(\w+)', field)
    try:
        if match:
            index, key = int(match[1]), match[2]
            left['decisions'][index].pop(key, None)
            right['decisions'][index].pop(key, None)
        elif field == 'decisions':
            old_rows, new_rows = left.pop('decisions', None), right.pop('decisions', None)
            if isinstance(old_rows, list) and (
                    not isinstance(new_rows, list) or any(row not in new_rows for row in old_rows)):
                return True
        else:
            left.pop(field, None)
            right.pop(field, None)
    except (KeyError, IndexError, TypeError, AttributeError):
        return True
    return left != right


def review_reply(runner, workspace, instruction, context, proof, marker, memory):
    from .repair_planning import _invoke, PlanFormatError, PlanningBlocked, MAX_FORMAT_CORRECTIONS
    from .repair_component_revalidation import _binding
    group = next(g for g in runner._experiment.finding_groups if g['group_id'] == context['component']['group_id'])

    def guard():
        if _binding(runner, workspace, group, marker['receipt']) != marker['binding']:
            raise PlanningBlocked('completed-component inputs changed during delta review', code='source_changed')

    guard()
    saved = memory.get('delta_reply', {})
    if saved.get('binding') != marker['binding']:
        saved = {'binding': marker['binding'], 'format_calls': 0}
        memory['delta_reply'] = saved
    if saved.get('blocked'):
        raise PlanningBlocked(saved['blocked'], code=saved.get('blocked_code', 'delta_format_exhausted'),
                              evidence=saved.get('request_id', ''))
    draft = read_record(runner, saved.get('draft', {}))
    if saved.get('draft') and draft is None:
        raise PlanningBlocked('retained delta reply is missing or invalid', code='delta_reply_invalid')
    payload, request_id = (draft.get('payload'), draft.get('request_id', '')) if draft else (None, '')

    def retain():
        saved['draft'] = save_record(runner, 'delta_review_reply', {'payload': payload, 'request_id': request_id})
        saved['request_id'] = request_id
        runner._experiment_store.save(runner._experiment)

    if draft is None and not saved.get('format_error'):
        try:
            payload, request_id = _invoke(runner, workspace, 'self_repair_component_delta_review', instruction, context)
            guard()
            retain()
        except PlanFormatError as error:
            saved['format_error'] = error.detail
            runner._experiment_store.save(runner._experiment)
    while True:
        guard()
        if payload is not None:
            try:
                return validate_reply(runner, payload, context, proof), request_id
            except PlanFormatError as error:
                saved['format_error'] = error.detail
        if saved['format_calls'] >= MAX_FORMAT_CORRECTIONS:
            saved['blocked'] = 'delta review format corrections exhausted; the substantive review is retained'
            runner._experiment_store.save(runner._experiment)
            raise PlanningBlocked(saved['blocked'], code='delta_format_exhausted', evidence=request_id,
                                  actual=saved.get('format_error'))
        saved['format_calls'] += 1
        runner._experiment_store.save(runner._experiment)  # Cancellation still consumes this slot.
        field = saved['format_error']['field']
        correction = {key: context[key] for key in ('source', 'source_commit', 'workspace', 'environment',
            'engine_base', 'contract_fingerprint', 'component', 'findings')}
        correction.update(previous_review=payload, feedback=saved['format_error'],
            original_review_ref=str(runner._experiment_store.root / 'planning' / request_id / 'input.json')
                                if request_id else saved['format_error'].get('evidence', ''),
            required_obligation_ids={f['finding_id']: f.get('causal_obligation_id') for f in context['findings']})
        previous = payload
        try:
            payload, request_id = _invoke(runner, workspace, 'self_repair_component_delta_format',
                'Correct only the reported output field of this completed delta review. Return the full JSON. '
                'Preserve all other decisions, evidence, disproof and implementation requirements. '
                'Do not repeat the code or scope review. Classify exactly the supplied findings once each; '
                'an empty findings list requires decisions:[]. Do not discard a new or required defect. '
                'If a correction would change a substantive decision, report that conflict.', correction)
        except PlanFormatError as error:
            saved['format_error'] = error.detail
            payload = previous
            continue
        guard()
        if format_changed(previous, payload, field):
            saved['blocked'] = 'delta format correction changed unreported substantive fields'
            saved['blocked_code'] = 'delta_semantics_changed'
            runner._experiment_store.save(runner._experiment)
            raise PlanningBlocked(saved['blocked'], code='delta_semantics_changed', evidence=request_id)
        retain()
