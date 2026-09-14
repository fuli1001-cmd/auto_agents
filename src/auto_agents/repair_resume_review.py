"""Refresh retained scope and a retained proposal in one independent request.

Only previously admitted scope decisions may enter this path. They are hints
for a read-only proposal review, never current exclusions or write authority.
The returned scope decisions and the ordinary plan approval must both validate.
"""
from copy import deepcopy

from .repair_control import digest


def prepare_refresh(runner, workspace, group, findings):
    from .repair_memory import latest_revision
    from .repair_planning import (POLICY_VERSION, PlanningBlocked, finding_key,
        _retained_scope, _scope_revalidation_reason, validate_plan)
    state = runner._experiment
    values = [f.to_dict() for f in findings]
    environment = digest(runner._full_suite_environment_fingerprint())
    reasons = {f['finding_id']: _scope_revalidation_reason(runner, workspace, f, environment)
               for f in values}
    pending = [f for f in values if reasons[f['finding_id']]]
    if not pending:
        return None
    for finding in values:
        old = state.scope_decisions.get(finding['finding_id'], {})
        if (old.get('policy') != POLICY_VERSION or old.get('contract') != state.contract_fingerprint
                or old.get('finding_key') != finding_key(finding)
                or old.get('verdict') not in {'required', 'not_applicable', 'follow_up'}
                or not _retained_scope(runner, old, finding)):
            return None
    previous = latest_revision(runner, group)
    if not previous or previous.get('status') not in {'APPROVE', 'draft', 'recovered_draft', 'legacy_draft'}:
        return None
    if previous.get('status') == 'APPROVE':
        approval = next((r for r in state.planning_receipts.values()
                         if r.get('request_id') == previous.get('reviewer_request')), {})
        if approval.get('engine_base') == state.base_commit and approval.get('environment') == environment:
            # Ordinary covered corrections can reuse their plan after a scope
            # refresh. Do not turn that one scope call into a larger plan audit.
            return None
    required = [f for f in values if state.scope_decisions[f['finding_id']]['verdict'] == 'required']
    proposed_group = {**group, 'finding_ids': [f['finding_id'] for f in required]}
    try:
        validate_plan(previous.get('draft'), proposed_group, set(state.contract_obligation_ids))
    except PlanningBlocked:
        return None
    return {'findings': required, 'pending': pending, 'reasons': reasons}


def scope_context(runner, workspace, context, pending, reasons):
    from .repair_planning import _scope_dependencies
    from .repair_planning_input import source_delta
    state = runner._experiment
    context['scope_revalidation'] = {f['finding_id']: {
        'previous': state.scope_decisions.get(f['finding_id']),
        'reason': reasons[f['finding_id']]} for f in pending}
    manifests = {f['finding_id']: _scope_dependencies(workspace, context, f,
        state.scope_decisions.get(f['finding_id'], {})) for f in pending}
    context['scope_dependencies_complete'] = all(value.get('complete') for value in manifests.values())
    context['scope_unresolved_dependencies'] = {key: value.get('unresolved', [])
        for key, value in manifests.items() if not value.get('complete')}
    deltas = {}
    for row in context['scope_revalidation'].values():
        commit = (row.get('previous') or {}).get('source_commit')
        if commit:
            if commit not in deltas:
                deltas[commit] = source_delta(workspace, commit)
            row['source_delta'] = deltas[commit]


INSTRUCTION = (
    'This request also refreshes the previously independent scope decisions in scope_findings. '
    'They are provisional until you explicitly reassess every supplied ID. Return decisions:[{finding_id,'
    'verdict:required|not_applicable|follow_up|unknown,obligation_id,trigger,consequence,support_basis,'
    'evidence:[...],reason,disproof}] in addition to the plan verdict. '
    'Start from each previous disproof and its actual source delta. Inspect unresolved external inputs; '
    'a source delta alone does not prove them unchanged. Required safety and introduced regressions '
    'cannot be excluded without concrete disproof. Use unknown if evidence is insufficient. '
    'A required verdict must use that finding causal_obligation_id. '
    'Audit the retained plan and these facts together; do not reconstruct unrelated historical tasks. '
)


def admit_refresh(runner, workspace, context, request_id, review):
    from .repair_planning import _validate_scope, record_scope_decisions, PlanningBlocked
    pending = context.get('scope_findings')
    if not pending:
        return False
    decisions = _validate_scope(review, pending, runner._experiment.contract_obligation_ids)
    original = {f['finding_id']: f for f in pending}
    scope = {**context, 'findings': deepcopy(pending)}
    record_scope_decisions(runner, workspace, scope, request_id, decisions)
    if any(row['verdict'] == 'unknown' for row in decisions):
        raise PlanningBlocked('retained scope refresh is unresolved; no code change is authorized',
                              code='scope_unresolved', evidence=request_id)
    required = {f['finding_id'] for f in context['findings']}
    # A newly required observation must enter ordinary planning. No approval
    # over the provisional finding set can authorize that changed scope.
    return any((row['verdict'] == 'required') != (row['finding_id'] in required)
               for row in decisions if row['finding_id'] in original)
