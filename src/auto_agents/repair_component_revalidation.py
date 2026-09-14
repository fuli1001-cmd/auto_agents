"""Completed implementations take one delta review, not another planning cycle."""
from copy import deepcopy

from .repair_control import digest
from .repair_memory import read_record, save_record, remember_review
from . import repair_completion as completion


def _binding(runner, workspace, group, reference):
    from .repair_planning import finding_key
    return digest({'completion': reference, 'source': completion.snapshot(workspace),
        'context': completion.context(runner, workspace), 'policy': completion.policy(),
        'epoch': completion.epoch(runner, workspace), 'definition': completion.definition(group),
        'findings': {key: [finding_key(f), f.status] for key, f in runner._experiment.findings.items()}})


def _historical_scope(runner, workspace, group, proof):
    """A historical exclusion is review input, never a current waiver."""
    from .repair_planning import finding_key, _retained_scope, _scope_revalidation_reason, nonblocking_scope
    pending = []
    environment = digest(runner._full_suite_environment_fingerprint())
    paths = set(proof.get('dependencies', {}).get('files', {})) | set(group.get('touched_paths', []))
    for finding in runner._experiment.findings.values():
        if (finding.status not in {'confirmed', 'reopened'}
                or finding.disposition not in {'contract_violation', 'candidate_regression'}
                or finding.causal_obligation_id not in runner._experiment.contract_obligation_ids):
            continue
        owned = finding.finding_id in group.get('finding_ids', []) or finding.repair_group_id == group['group_id']
        if not owned and nonblocking_scope(runner._experiment, finding):
            continue
        related = owned or bool(paths.intersection(finding.affected_paths))
        unchanged = proof.get('findings', {}).get(finding.finding_id) == finding_key(finding)
        if related and (not unchanged or finding.status == 'reopened'):
            return None  # A new counterexample belongs in the ordinary repair path.
        if not owned or finding.finding_id in proof.get('resolved', []):
            continue
        row = finding.to_dict()
        old = runner._experiment.scope_decisions.get(finding.finding_id, {})
        if (old.get('verdict') not in {'not_applicable', 'follow_up'}
                or not _retained_scope(runner, old, row)):
            return None
        if _scope_revalidation_reason(runner, workspace, row, environment):
            pending.append(row)
    return pending


def prepare_completed(runner, workspace, group):
    """Restore only a no-writer plan; current tests and a delta review still gate it."""
    if not runner._acceleration_enabled() or getattr(runner, '_candidate_is_final_group', True):
        return None
    if getattr(runner, '_candidate_next_action', {}).get('kind') in {'repair_code', 'repair_verification'}:
        return None  # A diagnosis of a current failure takes precedence over old completion.
    proof, _ = completion.retained_proof(runner, group)
    if not proof or _historical_scope(runner, workspace, group, proof) is None:
        return None
    memory = completion._memory(runner, group)
    reference = memory['completion']
    binding = _binding(runner, workspace, group, reference)
    previous = read_record(runner, memory.get('delta_review', {}))
    if previous and previous.get('binding') == binding and previous.get('decision') != 'APPROVE':
        return None
    impact = completion.assess(runner, workspace, group)
    if (impact['state'] == 'completed'
            and getattr(runner, '_candidate_next_action', {}).get('kind') != 'revalidate_completed'):
        # Selection may have observed a transiently different context. Do not
        # turn a now-valid receipt into another review/test/candidate cycle.
        # Refresh prerequisites and final-integration rules before rescheduling.
        completion.refresh(runner, workspace)
        canonical = next(g for g in runner._experiment.finding_groups if g['group_id'] == group['group_id'])
        if canonical['status'] == 'completed':
            return {'decision': 'COMPLETED', 'completion': reference,
                    'reusable_checks': impact['reusable_checks'], 'affected_checks': []}
    memory['completion_assessment'] = {**impact, 'state': 'needs_revalidation',
        'implementation_completed': True, 'planning_reused': True}
    canonical = next(g for g in runner._experiment.finding_groups if g['group_id'] == group['group_id'])
    canonical['status'] = 'needs_revalidation'
    plan = deepcopy(proof['plan'])
    plan.update(group_id=group['group_id'], status='needs_revalidation', mode='verify_existing',
        finding_ids=list(group.get('finding_ids', [])),
        planning_receipt=plan.get('planning_receipt') or reference['id'],
        completion_revalidation={'receipt': reference, 'binding': binding,
            'checks_reused': len(impact['reusable_checks']), 'checks_retest': len(impact['affected_checks'])},
        retained_acceptance=list(dict.fromkeys([*plan.get('retained_acceptance', []), *proof['commands']])))
    plan.setdefault('quick_checks', proof['commands'][:1])
    runner._candidate_group = plan
    runner._experiment_store.save(runner._experiment)
    report = getattr(runner, '_report_candidate_phase', None)
    if report:
        report('component_revalidation_prepare', 'completed plan retained; only current evidence will be revalidated')
    return {'decision': 'REVALIDATE', 'request_id': plan['planning_receipt'], 'plan': plan,
            'reusable_checks': impact['reusable_checks'], 'affected_checks': impact['affected_checks']}


def revalidation_action(runner, record):
    """Recheck an updated completed source before diagnosing its old failure again."""
    from pathlib import Path
    from .git_ops import head_ref
    if (not record or not record.failure_evidence or getattr(runner, '_candidate_is_final_group', True)
            or not getattr(runner, '_acceleration_enabled', lambda: False)()):
        return None
    retained = getattr(runner, '_continuous_workspace', None)
    if not retained:
        return None
    workspace = Path(retained) / 'repair'
    if not workspace.is_dir() or not record.candidate_commit or head_ref(workspace) == record.candidate_commit:
        return None
    active = runner._candidate_group
    group = next((g for g in runner._experiment.finding_groups if g['group_id'] == active.get('group_id')), active)
    proof, _ = completion.retained_proof(runner, group)
    if not proof or _historical_scope(runner, workspace, group, proof) is None:
        return None
    return {'kind': 'revalidate_completed', 'evidence_ids': [e['evidence_id'] for e in record.failure_evidence if e.get('evidence_id')],
            'cause': 'completed source changed; recheck the retained failing acceptance before another diagnosis'}


def reject_prepared(runner, reason):
    group = runner._candidate_group
    marker = group['completion_revalidation']
    memory = completion._memory(runner, next(
        g for g in runner._experiment.finding_groups if g['group_id'] == group['group_id']))
    memory['delta_review'] = save_record(runner, 'completed_component_delta_review', {
        'binding': marker['binding'], 'decision': 'REJECT', 'reason': reason})
    runner._experiment_store.save(runner._experiment)


def review_completed(runner, workspace, phase):
    """Combine historical scope and implementation delta review in one read-only call."""
    from .repair_planning import (_validate_scope, _text, record_scope_decisions,
                                 PlanningBlocked, finding_key)
    from .repair_planning_input import source_delta
    from .verification_ledger import source_identity
    from .git_ops import head_ref
    from .self_repair import _VerificationResult
    active = runner._candidate_group
    marker = active.get('completion_revalidation')
    if not marker or phase == 'integration' or getattr(runner, '_candidate_is_final_group', True):
        return None
    group = next((g for g in runner._experiment.finding_groups if g['group_id'] == active['group_id']), active)
    proof, reason = completion.retained_proof(runner, group)
    memory = completion._memory(runner, group)
    reference = memory.get('completion')
    if (not proof or reference != marker['receipt']
            or _binding(runner, workspace, group, reference) != marker['binding']):
        return _VerificationResult(False, 'completed-component revalidation inputs changed: ' + reason)
    pending = _historical_scope(runner, workspace, group, proof)
    if pending is None:
        return None
    old = read_record(runner, memory.get('delta_review', {}))
    if old and old.get('binding') == marker['binding'] and old.get('decision') == 'APPROVE':
        runner._candidate_review_completed = True
        return _VerificationResult(True, 'completed-component delta review reused', payload=old['payload'])
    if old and old.get('binding') == marker['binding'] and old.get('decision') == 'REJECT':
        marker['rejected_delta'] = memory['delta_review']
        return None
    def location(ref):
        return str(runner._experiment_store.root / 'planning' / ref['id'] / 'memory.json')
    schedule = read_record(runner, proof['verification'])
    impact = memory.get('completion_assessment', {})
    acceptance = save_record(runner, 'completion_acceptance_index', {
        'commands': proof['commands'], 'reusable_checks': impact.get('reusable_checks', []),
        'affected_checks': impact.get('affected_checks', [])})
    contract = save_record(runner, 'completion_contract', {
        'original_request': getattr(runner, '_invocation_context', {}),
        'obligations': runner._repair_contract_payload(runner._experiment)})
    parts = completion.context_parts(runner, workspace)
    current_execution = completion.execution_binding(runner, workspace, parts=parts)
    old_execution = proof.get('execution_binding', {})
    changed = {key: sorted(name for name in set(old_execution.get(key, {})) | set(current_execution.get(key, {}))
                          if old_execution.get(key, {}).get(name) != current_execution.get(key, {}).get(name))
               for key in ('files', 'components')} if old_execution else {'legacy_binding': True}
    external = [f.to_dict() for f in runner._experiment.blocking_findings()
                if proof.get('findings', {}).get(f.finding_id) != finding_key(f)]
    external_ref = save_record(runner, 'completion_new_evidence', {'findings': external}) if external else None
    context = {'source': source_identity(workspace), 'source_commit': head_ref(workspace),
        'workspace': str(workspace), 'engine_base': runner._experiment.base_commit,
        'environment': digest(runner._full_suite_environment_fingerprint()),
        'contract_fingerprint': runner._experiment.contract_fingerprint,
        'planning_action': 'revalidate_completed_component',
        'component': {key: group.get(key) for key in ('group_id', 'title', 'contract_obligation_ids', 'touched_paths')},
        'completion_ref': location(reference), 'original_review_ref': location(proof['review']),
        'contract_ref': location(contract),
        'plan_location': {'file': location(reference), 'section': 'plan'},
        'source_delta': source_delta(workspace, schedule.get('source_commit')),
        'verification_impact': {'reason': impact.get('reason'), 'acceptance_index_ref': location(acceptance),
            'reusable_checks': len(impact.get('reusable_checks', [])),
            'affected_checks': len(impact.get('affected_checks', [])), 'changed_execution_inputs': changed},
        'runtime_capabilities': {**parts['capabilities'], 'production_namespace': parts['production_capabilities']},
        'quick_verification': getattr(runner, '_review_verification_binding', {}),
        'findings': pending,
        'previous_scope': {f['finding_id']: {key: runner._experiment.scope_decisions[f['finding_id']].get(key)
            for key in ('verdict', 'reason', 'disproof', 'evidence', 'request_id')} for f in pending},
        'new_evidence': {'count': len(external), 'reference': location(external_ref) if external_ref else None},
        'resolved_in_completed_review': proof.get('resolved', [])}
    # Full immutable receipts remain accessible, without rebuilding experiment
    # history, pending component plans or a duplicate copy of the completed plan.
    instruction = (
        'Revalidate this completed implementation, not its original repair design. The frozen contract, '
        'plan and prior independent approval are in contract_ref, completion_ref and original_review_ref. '
        'Inspect source_delta and current runtime/verification dependencies that can affect those obligations. '
        'Retain established mechanisms, scenarios and exact acceptance commands. Do not reconstruct unchanged '
        'history or rewrite the plan. Incomplete dependency closures and external inputs still require factual '
        'inspection; unchanged files or passing quick tests alone are not proof of independence. '
        'For each supplied historical finding, recheck its previous disproof in this same review. '
        'The decisions array must classify exactly the IDs in findings, once each. '
        'Do not repeat IDs from resolved_in_completed_review; use decisions:[] when findings is empty. '
        'Return JSON {decision:APPROVE|REJECT,reason,implementation_required:false|true,remaining_changes:[], '
        'findings:[],deferred_findings:[],decisions:[{finding_id,verdict:not_applicable|follow_up|unknown|required,'
        'reason,evidence:[...],disproof,obligation_id,trigger,consequence,support_basis}]}. '
        'APPROVE requires no implementation work or unresolved findings; safety/regression exclusions require '
        'concrete disproof. If a new defect, changed contract or unresolvable dependency requires broader work, '
        'return REJECT with the concrete reason. Full repair review will then handle it. '
        'This is an independent code/scope review, not test acceptance; the controller still executes affected checks.')
    from .repair_delta_protocol import approval, review_reply
    payload, request_id = review_reply(runner, workspace, instruction, context, proof, marker, memory)
    if _binding(runner, workspace, group, reference) != marker['binding']:
        raise PlanningBlocked('completed-component inputs changed during delta review', code='source_changed')
    approved = approval(payload) and bool(_text(payload.get('reason')))
    decisions = []
    if approved:
        try:
            decisions = _validate_scope(payload, pending, runner._experiment.contract_obligation_ids)
        except PlanningBlocked as error:
            approved = False
            payload['revalidation_protocol_error'] = error.detail
    approved = approved and all(row['verdict'] in {'not_applicable', 'follow_up'} for row in decisions)
    if approved:
        record_scope_decisions(runner, workspace, context, request_id, decisions)
        payload = {**payload, 'resolved_finding_ids': proof.get('resolved', []),
                   'completion_revalidated': reference, 'delta_request': request_id}
        remember_review(runner, workspace, active, payload)
        runner._candidate_review_completed = True
    memory['delta_review'] = save_record(runner, 'completed_component_delta_review', {
        'binding': marker['binding'], 'decision': 'APPROVE' if approved else 'REJECT',
        'request_id': request_id, 'payload': payload})
    runner._experiment_store.save(runner._experiment)
    if not approved:
        marker['rejected_delta'] = memory['delta_review']
        if pending:
            from .repair_planning import review_scope
            # A failed delta assessment must not leave an old exclusion acting
            # as a current waiver. Use the existing bounded scope protocol before
            # the full reviewer handles newly required work.
            review_scope(runner, workspace, pending)
        return None  # New work must go through the original full reviewer/admission path.
    return _VerificationResult(True, 'completed-component delta review=APPROVE: ' + payload['reason'], payload=payload)
