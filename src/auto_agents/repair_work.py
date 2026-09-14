"""Stable controller-owned work identities; content hashes only version evidence.

Mutable file scopes are never entity identities. Legacy memory is imported as
evidence, with ownership checked before any authorization-bearing reference is
adopted. The normal plan, review and completion validators still decide reuse.
"""
from copy import deepcopy

from .repair_control import digest

VERSION = 1


def work_id(experiment, group):
    owner = group.get('group_id')
    if not isinstance(owner, str) or not owner:
        return ''
    known = [key for key, item in getattr(experiment, 'work_items', {}).items()
             if item.get('contract') == getattr(experiment, 'contract_fingerprint', '')
             and owner in item.get('aliases', [item.get('group_id')])]
    if len(known) > 1:
        raise ValueError('ambiguous controller work alias')
    if known:
        return known[0]
    return 'work:' + digest([VERSION, getattr(experiment, 'experiment_id', ''),
                             getattr(experiment, 'contract_fingerprint', ''), owner])


def work_item(experiment, group):
    identity = work_id(experiment, group)
    if not identity:
        return None
    if not hasattr(experiment, 'work_items'):
        experiment.work_items = {}
    definition = {key: deepcopy(group.get(key, [])) for key in (
        'contract_obligation_ids', 'touched_paths', 'depends_on', 'focused_tests')}
    if identity not in experiment.work_items:
        live = {g.get('group_id') for g in getattr(experiment, 'finding_groups', [])}
        aliases = [item for item in experiment.work_items.values()
                   if item.get('contract') == getattr(experiment, 'contract_fingerprint', '')
                   and not live.intersection(item.get('aliases', [item['group_id']]))
                   and digest(definition) in item['definitions']]
        if group['group_id'] in live and len(aliases) == 1:
            # A unique controller-observed rename retains work and budgets.
            # Approval still validates the complete definition and dependencies.
            record = aliases[0]
            record.setdefault('aliases', [record['group_id']]).append(group['group_id'])
            identity = record['work_id']
    record = experiment.work_items.setdefault(identity, {
        'version': VERSION, 'work_id': identity, 'group_id': group['group_id'],
        'contract': getattr(experiment, 'contract_fingerprint', ''), 'memory': {}, 'aliases': [group['group_id']],
        'definitions': {}, 'families': {}, 'transitions': {}, 'diagnoses': {},
    })
    if (record.get('work_id') != identity or group['group_id'] not in record.get('aliases', [record.get('group_id')])
            or record.get('contract') != getattr(experiment, 'contract_fingerprint', '')):
        raise ValueError('repair work identity does not match its controller owner')
    record['definitions'].setdefault(digest(definition), definition)
    return record


def _owned(runner, reference, group, kind):
    from .repair_memory import read_record
    record = read_record(runner, reference)
    if not record or record.get('kind') != kind:
        return None
    owner = record.get('component')
    owner = owner.get('group_id') if isinstance(owner, dict) else owner
    if work_id(runner._experiment, {'group_id': owner}) != work_id(runner._experiment, group):
        return None
    if record.get('contract', runner._experiment.contract_fingerprint) != runner._experiment.contract_fingerprint:
        return None
    return record


def memory(runner, group):
    from .repair_memory import component_key
    state = runner._experiment
    item = work_item(state, group)
    if item is None:
        return state.component_memory.setdefault(component_key(group), {})
    values = item['memory']
    if item.get('legacy_imported'):
        return values
    canonical = next((g for g in getattr(state, 'finding_groups', []) if g.get('group_id') == group['group_id']), group)
    # Hints and acceptance inventories can add work, never certify it. Preserve
    # the complete original inventory when execution paths later change.
    for current in (canonical, group):
        old = state.component_memory.get(component_key(current), {})
        for key in ('acceptance_inventory', 'check_timings'):
            if isinstance(old.get(key), dict):
                values.setdefault(key, {}).update(deepcopy(old[key]))
            elif isinstance(old.get(key), list):
                values[key] = list(dict.fromkeys([*values.get(key, []), *old[key]]))
    kinds = {'latest_revision': 'plan_revision', 'code_review': 'code_review',
             'completion': 'component_completion'}
    for key, kind in kinds.items():
        matches = []
        for old in state.component_memory.values():
            reference = old.get(key)
            if not reference:
                continue
            record = _owned(runner, reference, canonical, kind)
            if record:
                path = runner._experiment_store.root / 'planning' / record['id'] / 'memory.json'
                matches.append((path.stat().st_mtime_ns, record['id'], reference))
        if matches:
            values[key] = deepcopy(max(matches, key=lambda row: row[:2])[2])
    item['legacy_imported'] = True
    return values


def stored_memory(experiment, group):
    """Read scheduling hints without creating state in concurrent verifiers."""
    from .repair_memory import component_key
    item = getattr(experiment, 'work_items', {}).get(work_id(experiment, group), {})
    return item.get('memory', {}) if item else getattr(experiment, 'component_memory', {}).get(component_key(group), {})


def transition(runner, group, kind, evidence, payload):
    """Idempotent event application: replay/restart never replenishes a budget."""
    from .repair_memory import save_record, read_record
    item = work_item(runner._experiment, group)
    if item is None:
        return None
    identity = digest([kind, evidence])
    previous = item['transitions'].get(identity)
    if previous:
        record = read_record(runner, previous)
        if not record or record.get('work_id') != item['work_id']:
            raise ValueError('repair transition evidence is missing or invalid')
        return record
    reference = save_record(runner, 'repair_transition', {
        'work_id': item['work_id'], 'event': kind, 'evidence': evidence, 'payload': payload,
        'sequence': len(item['transitions']) + 1,
    })
    item['transitions'][identity] = reference
    runner._experiment_store.save(runner._experiment)
    callback = getattr(runner, '_control_phase_callback', None)
    if callback:
        callback('repair_transition', {'work_id': item['work_id'], 'event': kind, 'receipt': reference['id'], **payload})
    return read_record(runner, reference)


def link_definition(runner, group):
    """Expose a stable reference without accepting an identity from model JSON."""
    item = work_item(runner._experiment, group)
    return {'work_id': item['work_id'], 'group_id': group['group_id'],
            'definition_versions': len(item['definitions'])} if item else {}
