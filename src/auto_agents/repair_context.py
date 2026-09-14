"""Task-sized working packets with immutable, complete evidence references."""
from copy import deepcopy

from .repair_memory import save_record
from .repair_work import link_definition


def writer_packet(runner, search, group, background):
    if not group.get('planning_receipt') or not hasattr(runner, '_experiment_store'):
        return search, group, None
    from .repair_review_protocol import controls_for
    reference = save_record(runner, 'writer_context', {'search': search, 'component': group, **background})
    path = str(runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json')
    def ref(section):
        return {'complete_context': path, 'section': section,
                'retrieve_when': 'the affected mechanism or an unresolved dependency requires it'}
    active_ids = set(group.get('finding_ids', []))
    selected = {key: deepcopy(search[key]) for key in (
        'experiment_id', 'contract_fingerprint', 'contract_obligations', 'next_action',
        'resolved_findings_that_must_not_regress') if key in search}
    selected.update(work=link_definition(runner, group), complete_context=path,
                    counterexample_controls=controls_for(runner._experiment, group))
    selected['open_contract_findings'] = [deepcopy(f) for f in search.get('open_contract_findings', [])
        if f.get('finding_id') in active_ids or f.get('repair_group_id') == group.get('group_id')]
    for name in ('parent_review', 'previous_review'):
        if name in search:
            prior = search[name]
            selected[name] = {**{key: deepcopy(prior[key]) for key in (
                'candidate_id', 'candidate_commit', 'status', 'resolved_finding_ids', 'result_path') if key in prior},
                'findings': [deepcopy(f) for f in prior.get('findings', []) if f.get('finding_id') in active_ids],
                'complete_review': ref('search.' + name)}
    action = selected.get('next_action', {})
    # Do not send three copies of the same full review in one prompt.
    if action.get('review_findings'):
        selected['current_review_findings'] = action.pop('review_findings')
    compact_group = deepcopy(group)
    local = action.get('kind') == 'repair_code' and bool(group.get('finding_scenario_bindings'))
    if local:
        compact_group['implementation_steps'] = [
            'Correct the current independently reviewed counterexamples within the approved paths. '
            'Retain other mechanisms; inspect the complete plan for affected interactions.']
        compact_group['complete_implementation_steps'] = ref('component.implementation_steps')
        relevant = {scenario for ids in group.get('finding_scenario_bindings', {}).values() for scenario in ids}
        compact_group['scenarios'] = [row for row in group.get('scenarios', [])
                                      if row['scenario_id'] in relevant or row.get('quick_required')]
        compact_group['remaining_scenarios'] = ref('component.scenarios')
    for name in ('focused_tests', 'retained_acceptance', 'probes'):
        if name in compact_group:
            compact_group[name] = ref('component.' + name)
    return selected, compact_group, {name: ref(name) for name in background}


def review_packet(group, context, full_input, *, incremental):
    if not incremental or not full_input:
        return group
    value = deepcopy(group)
    for name in ('implementation_steps', 'focused_tests', 'retained_acceptance', 'probes'):
        if name in value:
            value[name] = {'complete_review_input': full_input, 'section': 'component.' + name,
                           'retrieve_when': 'inspect changed behavior, a missing dependency or an affected retained decision'}
    # Keep every trigger, expected result and acceptance check visible. Review
    # may retain an unchanged scenario only with its original bound evidence.
    value['scenarios'] = [{key: row[key] for key in (
        'scenario_id', 'kind', 'trigger', 'expected', 'check', 'quick_check', 'obligation_ids', 'finding_ids') if key in row}
        for row in group.get('scenarios', [])]
    return value
