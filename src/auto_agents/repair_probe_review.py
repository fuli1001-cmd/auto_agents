"""Historical reproduction expectations are not current repair acceptance.

An independent reviewer may reconcile changed *behavioral* observations on a
retained plan. This never turns infrastructure errors into evidence, rewrites
the old expected outcome, or excuses a failing post-implementation check.
"""
from .repair_probe_recovery import probe_id
from .repair_test_refs import pytest_targets


def baseline(runner, plan, group, request_id=None, reference=None):
    from .repair_planning import _component_signature, _review_reuse_failure
    from .repair_memory import read_record
    if reference is not None:
        record = read_record(runner, reference)
        if not record or record.get('kind') != 'probe_baseline':
            return None
        candidates = [record.get('receipt', {})]
    else:
        candidates = reversed(list(runner._experiment.planning_receipts.values()))
    for receipt in candidates:
        if (receipt.get('decision') != 'APPROVE'
                or request_id is not None and receipt.get('request_id') != request_id
                or receipt.get('contract') != runner._experiment.contract_fingerprint
                or receipt.get('component_signature') != _component_signature(group)
                or receipt.get('plan', {}).get('probes') != plan.get('probes')
                or not receipt.get('probe_results')
                or not all(p.get('matches') is True for p in receipt['probe_results'])):
            continue
        if not _review_reuse_failure(runner, receipt, receipt.get('component', group)):
            return {'request_id': receipt['request_id'], 'source_commit': receipt.get('source_commit')}
    return None


def prepare(runner, group, context):
    probes = context['probe_results']
    if all(p.get('matches') for p in probes):
        return
    original = baseline(runner, context['proposed_plan'], group)
    if original:
        from .repair_memory import save_record
        from .repair_control import digest
        from .repair_feedback import sanitize_evidence
        receipt = next(r for r in runner._experiment.planning_receipts.values()
                       if r.get('request_id') == original['request_id'])
        # Preserve the original receipt before its strategy slot is updated.
        # Redacted executable evidence cannot stand in for an original digest.
        if digest(receipt) == digest(sanitize_evidence(receipt)):
            context['probe_baseline'] = {**original, 'reference': save_record(
                runner, 'probe_baseline', {'receipt': receipt})}
    context['changed_probe_observations'] = [
        {'probe_id': probe_id(p['specification']), 'index': index,
         'expected_on_original_source': p['specification']['expected'], 'observed_now': p['outcome']}
        for index, p in enumerate(probes) if not p.get('matches')]


INSTRUCTION = (
    'Inspect changed_probe_observations when present. A retained negative reproduction can now pass '
    'because an earlier candidate fixed it; do not demand that repaired code reproduce the old bug. '
    'When probe_baseline identifies an authenticated independent prior approval, explicitly reconcile '
    'each changed observation with probe_assessments:[{probe_id,disposition:already_fixed|planned_fix,'
    'scenario_ids:[...],evidence:[...],reason}]. already_fixed is only for a formerly failing reproduction '
    'that now passes. planned_fix is only for a current behavioral pytest failure that the proposed '
    'plan concretely repairs; name the existing scenarios whose checks cover those test nodes. '
    'That exact failed command becomes mandatory post-implementation acceptance. Setup, missing '
    'dependencies, cancellation and inconclusive probes cannot be reconciled this way. '
    'A changed observation outside the plan needs REVISE. Keep original expected outcomes as historical '
    'evidence. Reconciliation authorizes a plan, never a repaired candidate. Without changed observations '
    'return probe_assessments=null.'
)


def admit(runner, group, context, review):
    """Return mandatory additional checks, or None when evidence is insufficient."""
    from .repair_planning import _texts, _text
    probes = context.get('probe_results', [])
    if all(p.get('matches') is True for p in probes):
        return []
    plan = context['proposed_plan']
    original = context.get('probe_baseline', {})
    observed = (baseline(runner, plan, group, original['request_id'], original.get('reference'))
                if original.get('request_id') and original.get('reference') else None)
    if not observed or {**observed, 'reference': original['reference']} != original:
        return None
    pending = {probe_id(p['specification']): p for p in probes if not p.get('matches')}
    decisions = review.get('probe_assessments')
    if (not isinstance(decisions, list) or len(decisions) != len(pending)
            or any(not isinstance(row, dict) or not _text(row.get('probe_id')) for row in decisions)
            or {row['probe_id'] for row in decisions} != set(pending)):
        return None
    scenarios = {s['scenario_id']: s for s in plan['scenarios']}
    commands = []
    for row in decisions:
        probe = pending[row['probe_id']]
        ids = row.get('scenario_ids')
        if (not _texts(ids) or not set(ids).issubset(scenarios)
                or not _texts(row.get('evidence')) or not _text(row.get('reason'))):
            return None
        expected, observed = probe['specification']['expected'], probe.get('outcome')
        if row.get('disposition') == 'already_fixed':
            if (expected, observed) != ('behavior_failure', 'pass'):
                return None
        elif row.get('disposition') == 'planned_fix':
            if (expected, observed) != ('pass', 'behavior_failure'):
                return None
            command = probe['specification']['command']
            targets = pytest_targets(command, prose=False)
            checks = {node for identity in ids for node in pytest_targets(scenarios[identity]['check'], prose=False)}
            if not targets or any(not any(node == check or node.startswith(check + '::')
                                           or node.startswith(check + '[') for check in checks) for node in targets):
                return None
            commands.append(command)
        else:
            return None
    return list(dict.fromkeys(commands))
