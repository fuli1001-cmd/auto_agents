"""Evidence-driven repair routing and monotonic, restart-safe diagnosis history."""
from copy import deepcopy

from .repair_control import digest
from .repair_memory import read_record
from .repair_work import memory, work_item, work_id, transition

MAX_HYPOTHESES = 3
MAX_DIAGNOSIS_RECOVERIES = 2


def case_identity(finding):
    # Finding labels, commit IDs and source line references are bookkeeping.
    # Keep the actual counterexample and full invocation semantics in the key.
    from .repair_schedule import canonical_commands
    test = finding.get('required_test', '')
    family = digest([finding.get('causal_obligation_id'), sorted(finding.get('scenario_ids', [])),
                     sorted(finding.get('affected_paths', []))])
    case = digest([' '.join(str(finding.get('counterexample', '')).split()),
                   canonical_commands([test])[0] if test else [], finding.get('disposition')])
    return family, case


def observe_review(runner, group, reference):
    record = read_record(runner, reference)
    if not record or record.get('kind') != 'code_review' or not record.get('source_commit'):
        return
    item = work_item(runner._experiment, group)
    if item is None:
        return
    for finding in record.get('result', {}).get('findings', []):
        if (finding.get('causal_obligation_id') not in group.get('contract_obligation_ids', [])
                or not finding.get('counterexample') or not finding.get('affected_paths')):
            continue
        family_id, case_id = case_identity(finding)
        family = item['families'].setdefault(family_id, {'cases': {}, 'review_sources': {},
            'paths': finding['affected_paths'], 'scenarios': finding.get('scenario_ids', [])})
        case = family['cases'].setdefault(case_id, {'finding_ids': [], 'observations': {},
            'counterexample': finding['counterexample'], 'required_test': finding.get('required_test', '')})
        if finding['finding_id'] not in case['finding_ids']:
            case['finding_ids'].append(finding['finding_id'])
        # Re-reading the same rejected source does not count as another repair.
        case['observations'].setdefault(record['source_commit'], reference)
        family['review_sources'].setdefault(record['source_commit'], reference)
    runner._experiment_store.save(runner._experiment)


def route(runner, record, fallback):
    """Select work from authenticated review facts as well as command failures.

    A routing decision is not a plan approval or a verification certificate.
    Scope, plan authority and all candidate/final gates still apply afterward.
    """
    group = getattr(runner, '_candidate_group', {})
    if not record or not group.get('group_id') or not hasattr(runner, '_experiment_store') or not hasattr(runner, '_experiment'):
        return fallback
    if fallback.get('kind') in {'blocked', 'prepare_environment', 'diagnose_execution'}:
        return fallback
    state = runner._experiment
    reference = memory(runner, group).get('code_review', {})
    reviewed = read_record(runner, reference)
    if (not reviewed or reviewed.get('kind') != 'code_review'
            or reviewed.get('source_commit') != record.candidate_commit
            or reviewed.get('contract') != state.contract_fingerprint
            or not isinstance(reviewed.get('component'), dict)
            or work_id(state, reviewed['component']) != work_id(state, group)):
        return fallback
    from .repair_planning import finding_key, _implementation_bindings
    actual = {f.finding_id: f for f in state.blocking_findings()
              if f.finding_id in group.get('finding_ids', []) or f.repair_group_id == group['group_id']}
    findings = [row for row in reviewed.get('result', {}).get('findings', [])
                if row.get('finding_id') in actual and finding_key(row) == finding_key(actual[row['finding_id']])]
    if not findings:
        return fallback
    receipt = next((row for row in state.planning_receipts.values()
                    if row.get('request_id') == reviewed.get('component', {}).get('planning_receipt')
                    and row.get('decision') == 'APPROVE'), None)
    covered = bool(receipt and _implementation_bindings(runner, receipt, findings) is not None)
    action = {**fallback, 'kind': 'repair_code' if covered else 'amend_plan',
        'evidence_ids': list(dict.fromkeys([*fallback.get('evidence_ids', []), reference['id']])),
        'review_reference': reference, 'review_findings': deepcopy(findings),
        'cause': 'independent review identifies a covered correction' if covered else
                 'independent review identifies a changed mechanism, scope or unproved scenario mapping',
        'completion': 'the independent counterexamples, compatibility controls and complete retained acceptance pass'}
    item = work_item(state, group)
    recurrence = []
    history = []
    for finding in findings:
        family_id, case_id = case_identity(finding)
        family = item['families'].get(family_id, {})
        case = family.get('cases', {}).get(case_id, {})
        observations = case.get('observations', {})
        if len(observations) < 2:
            continue
        key = family_id + ':' + case_id
        diagnosis = item['diagnoses'].get(key, {})
        history.append({'family': family_id, 'case': case_id, 'counterexample': finding['counterexample'],
                        'reviewed_sources': list(observations), 'diagnosis': diagnosis.get('result')})
        disproved = diagnosis.get('status') == 'complete' and len(observations) > diagnosis['observed_count']
        if disproved and diagnosis.get('hypotheses', 1) >= MAX_HYPOTHESES:
            action.update(kind='blocked', cause='the retained counterexample disproved all bounded diagnostic '
                'hypotheses; preserve their evidence instead of repeating a failed correction', counterexample_history=history)
            break
        if diagnosis.get('status') != 'complete' or disproved:
            if not disproved and diagnosis.get('recoveries', 0) >= MAX_DIAGNOSIS_RECOVERIES:
                action.update(kind='blocked', cause='bounded diagnosis recovery exhausted for the retained counterexample',
                              counterexample_history=history)
                break
            recurrence.append(key)
    else:
        if recurrence:
            action.update(kind='diagnose_failure', recurrence_cases=recurrence, counterexample_history=history,
                          cause='the same counterexample survived distinct corrections; identify the disproved '
                                'assumption and the smallest discriminating check before another writer')
    transition(runner, group, 'route', [record.candidate_id, reference, fallback, action['kind']],
               {'action': action['kind'], 'candidate_id': record.candidate_id,
                'finding_ids': [f['finding_id'] for f in findings], 'cause': action['cause']})
    return action


def reserve_diagnosis(runner, action):
    if not action.get('recurrence_cases'):
        return
    item = work_item(runner._experiment, runner._candidate_group)
    if item is None:
        return
    for key in action.get('recurrence_cases', []):
        family, case = key.split(':')
        observed = len(item['families'][family]['cases'][case]['observations'])
        entry = item['diagnoses'].setdefault(key, {'calls': 0, 'hypotheses': 0, 'recoveries': 0, 'history': []})
        if entry.get('status') == 'complete':
            entry['history'].append({'result': entry['result'], 'reference': entry['reference'],
                                     'observed_count': entry['observed_count']})
            entry['recoveries'] = 0
        if entry['recoveries'] >= MAX_DIAGNOSIS_RECOVERIES:
            raise RuntimeError('bounded recurring-failure diagnosis is exhausted')
        if entry['recoveries'] == 0:
            entry['hypotheses'] += 1
        entry.update(status='running', calls=entry['calls'] + 1, recoveries=entry['recoveries'] + 1,
                     observed_count=observed)
    runner._experiment_store.save(runner._experiment)


def finish_diagnosis(runner, action, result, reference):
    if not action.get('recurrence_cases'):
        return
    item = work_item(runner._experiment, runner._candidate_group)
    if item is None:
        return
    for key in action.get('recurrence_cases', []):
        entry = item['diagnoses'][key]
        proposed = [result.get('invalidated_assumption'), result.get('next_check')]
        if any(proposed == [old['result'].get('invalidated_assumption'), old['result'].get('next_check')]
               for old in entry.get('history', [])):
            return False
    for key in action.get('recurrence_cases', []):
        entry = item['diagnoses'][key]
        entry.update(status='complete', result=deepcopy(result), reference=reference)
    runner._experiment_store.save(runner._experiment)
    return True


def summary(experiment):
    items = list(getattr(experiment, 'work_items', {}).values())
    cases = [case for item in items for family in item['families'].values() for case in family['cases'].values()]
    return {'work_items': len(items), 'definition_versions': sum(len(item['definitions']) for item in items),
            'decisions': sum(len(item['transitions']) for item in items), 'counterexamples': len(cases),
            'recurring_counterexamples': sum(len(case['observations']) > 1 for case in cases),
            'diagnosis_calls': sum(d.get('calls', 0) for item in items for d in item['diagnoses'].values())}


def admit_duplicate_validation(runner, fingerprint):
    """A restored implementation may need fresh proof, not another code diff.

    Allow one full revalidation for an independently retained plan or a restored
    completed source. This does not reuse a failed check or grant progress.
    Repeated unchanged failures still hit the duplicate guard.
    """
    state, group = runner._experiment, runner._candidate_group
    canonical = next((g for g in state.finding_groups if g.get('group_id') == group.get('group_id')), group)
    approved = any(row.get('decision') == 'APPROVE' and row.get('request_id') == group.get('planning_receipt')
                   for row in state.planning_receipts.values())
    eligible = approved and group.get('mode') == 'verify_existing'
    if not eligible:
        from .repair_completion import retained_proof
        proof, _ = retained_proof(runner, canonical)
        eligible = bool(proof and any(record.patch_fingerprint == fingerprint
            and record.status == 'candidate_group_completed'
            and work_id(state, {'group_id': record.finding_group_id}) == work_id(state, group)
            for record in state.candidates.values()))
    if not eligible:
        return False
    key = digest([fingerprint, group.get('planning_receipt'), state.contract_fingerprint,
                  runner._full_suite_environment_fingerprint(), group.get('retained_acceptance', [])])
    used = memory(runner, group).setdefault('duplicate_revalidations', {})
    if key in used:
        return False
    used[key] = {'source_fingerprint': fingerprint, 'planning_receipt': group.get('planning_receipt')}
    transition(runner, group, 'duplicate_revalidation', key, {'action': 'revalidate',
        'cause': 'restored or independently revalidated source still requires current complete acceptance'})
    return True
