"""Policy 2: pure, replayable evidence decisions and single-use model permits.

Historical policy-1 events are deliberately untouched. Activation is an explicit
event; this module never performs effects or rewrites a historical budget.
"""
from copy import deepcopy
from pathlib import PurePosixPath

from .model import digest, require
from .observations import validate, compact

VERSION = 2


def owner(state, task_id):
    task = state['tasks'][task_id]
    while task['contract'].get('parent_task'):
        task_id = task['contract']['parent_task']
        task = state['tasks'][task_id]
    return task_id


def scope_id(state, task_id):
    contract = state['tasks'][owner(state, task_id)]['contract']
    # Ephemeral operation IDs and handoff aliases cannot mint a new search.
    roots = ['engine'] if contract['kind'] == 'engine_repair' else contract['source_scope']
    return digest([contract['goal_id'], contract['kind'], roots, sorted(contract['required_checks'])])


def blank_scope():
    return {'observations': {}, 'latest': '', 'seen_failures': [], 'credited': [], 'protected': [],
            'stalled': 0, 'diagnoses': 0, 'attempts': 0, 'hypotheses': [], 'correction': None,
            'routes': {}, 'reviews': {}, 'completed_manifests': [], 'revision': 0}


def scope(state, task_id):
    return state['recovery']['scopes'].get(scope_id(state, task_id), blank_scope())


def failures(item):
    observed = item['observations'].get(item['latest'], {})
    baseline = set(observed.get('baseline_failures', []))
    return sorted(key for key, check in observed.get('checks', {}).items()
                  if check['status'] == 'failed' and key not in baseline)


def decision(state, task_id, phase, source):
    item = scope(state, task_id)
    base = {'version': VERSION, 'scope': scope_id(state, task_id), 'scope_revision': item['revision'],
            'owner': owner(state, task_id), 'phase': phase, 'source': source,
            'observation': item['latest'], 'action': phase, 'allowed': True, 'reason': 'owned phase',
            'stalled': item['stalled'], 'diagnoses_remaining': max(0, 2 - item['diagnoses'])}
    budget = state['budget']
    if budget['limit'] is not None and budget['model_calls'] >= budget['limit']:
        return {**base, 'allowed': False, 'reason': 'user_budget_exhausted'}
    if any(c['status'] in {'running', 'unknown', 'reserved'} for c in state['commands'].values()):
        return {**base, 'allowed': False, 'reason': 'outcome_unknown'}
    if any(c['status'] == 'reserved' for c in state['recovery']['auxiliary'].values()):
        return {**base, 'allowed': False, 'reason': 'outcome_unknown'}
    observed = item['observations'].get(item['latest'], {})
    kind = state['tasks'][owner(state, task_id)]['contract']['kind']
    if kind == 'run' and phase not in {'implement', 'diagnose'}:
        return {**base, 'frontier': digest([source, state['recovery']['frontier'], phase])}
    if phase == 'diagnose':
        allowed = bool(failures(item)) and item['diagnoses'] < 2 and item['stalled'] >= 2
        return {**base, 'allowed': allowed, 'reason': 'bounded_diagnosis' if allowed else 'no_distinct_hypothesis'}
    if phase == 'implement':
        correction = item['correction']
        permitted = bool(correction and correction['observation'] == item['latest']
                         and correction['source'] == source and not correction['used'])
        if item['stalled'] >= 2 and not permitted:
            return {**base, 'allowed': False,
                    'action': 'diagnose' if failures(item) and item['diagnoses'] < 2 else 'blocked',
                    'reason': 'diagnosis_required' if failures(item) and item['diagnoses'] < 2 else 'no_progress'}
        return {**base, 'reason': 'evidence_bound_correction' if permitted else 'bounded_implementation'}
    if phase == 'review':
        allowed = observed.get('complete') is True and observed.get('source') == source
        allowed = allowed and item['reviews'].get(item['latest'], 0) < 2
        return {**base, 'allowed': allowed, 'reason': 'verified_candidate_review' if allowed else 'review_evidence'}
    if phase not in {'verify', 'deliver', 'acceptance', 'reconcile'}:
        # Read-only route/plan calls have a bounded allowance for the current
        # causal frontier. A changed prompt alone cannot renew it.
        frontier = digest([source, state['recovery']['frontier'], phase])
        return {**base, 'frontier': frontier, 'allowed': item['routes'].get(frontier, 0) < 2,
                'reason': 'bounded_route'}
    return base


def consume(state, command):
    recovery = state['recovery']
    permit = recovery['permits'].get(command.command_id)
    require(permit is not None and not permit['consumed'], 'recovery_permit', 'Model work needs an unused permit')
    expected = decision(state, command.task_id, command.phase, command.source)
    require(expected['allowed'] and permit['decision'] == expected
            and permit['command'] == command.to_dict(), 'recovery_permit', 'Permit inputs changed before reservation')
    permit['consumed'] = True
    item = recovery['scopes'].setdefault(expected['scope'], blank_scope())
    if command.phase == 'implement':
        item['attempts'] += 1
        # A writer that never reaches verification also consumes its search.
        item['stalled'] += 1
        if item['correction']: item['correction']['used'] = True
    elif command.phase == 'diagnose': item['diagnoses'] += 1
    elif command.phase == 'review':
        key = item['latest']; item['reviews'][key] = item['reviews'].get(key, 0) + 1
    else:
        key = expected['frontier']; item['routes'][key] = item['routes'].get(key, 0) + 1
    item['revision'] += 1


def observe(state, command, observed):
    validate(observed, command)
    observed = compact(observed)
    recovery = state['recovery']
    item = recovery['scopes'].setdefault(scope_id(state, command['task_id']), blank_scope())
    identity = digest(observed)
    if identity in item['observations']: return
    previous = item['observations'].get(item['latest'])
    old_failures = set(item['seen_failures'])
    passed = {key for key, check in observed['checks'].items() if check['status'] == 'passed'}
    failed = {key for key, check in observed['checks'].items() if check['status'] == 'failed'}
    comparable = (previous is not None and previous['manifest'] == observed['manifest']
                  and previous['environment'] == observed['environment']
                  and previous['verifier'] == observed['verifier'])
    eligible = set(observed.get('progress_checks', []))
    if previous: eligible &= set(previous.get('progress_checks', []))
    closed = (passed & old_failures & eligible) - set(item['credited'])
    preserved = set(item['protected']) <= passed
    previously_failed = {key for key, check in (previous or {}).get('checks', {}).items() if check['status'] == 'failed'}
    new_regressions = set(observed.get('regressions', [])) - previously_failed
    progressed = comparable and preserved and not new_regressions and bool(closed)
    completed = (observed['complete'] and bool(observed['checks'])
                 and observed['manifest'] not in item['completed_manifests'])
    if progressed:
        item['credited'] = sorted(set(item['credited']) | closed)
        item.update(stalled=0, diagnoses=0)
    if completed:
        item['completed_manifests'].append(observed['manifest'])
        item.update(stalled=0, diagnoses=0)
    # First successes become obligations to preserve, not retry credits.
    if previous is None or progressed or observed['complete']:
        item['protected'] = sorted(set(item['protected']) | passed)
    item['seen_failures'] = sorted(old_failures | failed)
    item['observations'][identity] = deepcopy(observed)
    item['latest'] = identity
    item['correction'] = None
    item['revision'] += 1
    if progressed or observed['complete']:
        recovery['frontier'] = digest([recovery['frontier'], identity])


def record_diagnosis(state, command, proposal):
    item = state['recovery']['scopes'][scope_id(state, command['task_id'])]
    require(isinstance(proposal, dict) and set(proposal) == {
        'observation', 'hypothesis', 'failure_ids', 'paths', 'expected_result'},
        'diagnosis_invalid', 'Diagnosis must identify an observation, hypothesis, failures, paths and expected result')
    require(proposal['observation'] == item['latest'] and item['latest'],
            'diagnosis_invalid', 'Diagnosis is not bound to current verification')
    require(isinstance(proposal['hypothesis'], str) and proposal['hypothesis'].strip()
            and isinstance(proposal['expected_result'], str) and proposal['expected_result'].strip(),
            'diagnosis_invalid', 'Diagnosis has no falsifiable hypothesis or expected result')
    keys, paths = proposal['failure_ids'], proposal['paths']
    require(isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys)
            and set(keys) <= set(failures(item)), 'diagnosis_invalid', 'Diagnosis names an unobserved failure')
    require(isinstance(paths, list) and paths and all(isinstance(p, str) and p not in {'', '.', './'} and
            not PurePosixPath(p).is_absolute() and '..' not in PurePosixPath(p).parts
            and '.git' not in PurePosixPath(p).parts and not p.startswith('.auto-agents/') for p in paths),
            'diagnosis_invalid', 'Correction must name relative source paths, not control records')
    hypothesis = ' '.join(proposal['hypothesis'].casefold().split())
    signature = digest([sorted(keys), hypothesis, sorted(paths), proposal['expected_result'].strip()])
    require(hypothesis not in [h['hypothesis'] for h in item['hypotheses']]
            and signature not in [h['signature'] for h in item['hypotheses']],
            'no_distinct_hypothesis', 'The same hypothesis cannot authorize another correction')
    item['hypotheses'].append({'hypothesis': hypothesis, 'signature': signature, 'proposal': deepcopy(proposal)})
    item['correction'] = {'observation': item['latest'], 'source': command['source'],
                          'proposal': deepcopy(proposal), 'used': False}
    item['revision'] += 1


def activate(state):
    require('recovery' not in state, 'recovery_policy', 'Recovery policy is already activated')
    state['recovery'] = {'version': VERSION, 'scopes': {}, 'permits': {}, 'frontier': '', 'auxiliary': {},
                         'activation_revision': state['revision'] + 1,
                         'legacy_budget': deepcopy(state['budget'])}
    # Count retained consumption by owner, without changing the global ledger.
    for command in sorted(state['commands'].values(), key=lambda c: c['sequence']):
        key = scope_id(state, command['task_id'])
        item = state['recovery']['scopes'].setdefault(key, blank_scope())
        outcome = command.get('outcome') or {}
        if command['phase'] == 'implement': item['attempts'] += 1
        if outcome.get('kind') == 'candidate_rejected': item['stalled'] += 1
        if command['phase'] == 'diagnose' and command['model_call']: item['diagnoses'] += 1
        if command['phase'] in {'verify', 'deliver', 'acceptance'} and outcome.get('kind') == 'success':
            item.update(stalled=0, diagnoses=0)


def event(state, kind, data):
    if kind == 'recovery_policy_activated':
        require(data == {'version': VERSION}, 'recovery_policy', 'Unsupported recovery policy')
        activate(state)
    elif kind == 'recovery_permit_issued':
        from .model import Command
        command = Command(**data['command'])
        expected = decision(state, command.task_id, command.phase, command.source)
        require(expected == data['decision'] and expected['allowed'], 'recovery_permit', 'Decision does not authorize this command')
        require(command.command_id not in state['recovery']['permits'], 'recovery_permit', 'Permit already exists')
        state['recovery']['permits'][command.command_id] = {**deepcopy(data), 'consumed': False}
    elif kind == 'recovery_observation_imported':
        command = state['commands'][data['command_id']]
        require(command['status'] == 'finished' and command['phase'] == 'verify'
                and data['result_ref'] == (command['outcome']['details'].get('native_result')
                                           or command['outcome']['details'].get('result_ref')),
                'verification_observation', 'Historical observation has no executor result')
        observe(state, command, data['observation'])
    elif kind == 'recovery_auxiliary_reserved':
        auxiliary = state['recovery']['auxiliary']
        require(state['status'] == 'active' and data['id'] not in auxiliary and data.get('anchor'),
                'recovery_permit', 'Auxiliary request already has an owner or workflow is stopped')
        require(data['owner'] in state['tasks'], 'recovery_permit', 'Auxiliary call has no original task')
        require(not any(c['status'] in {'reserved', 'running', 'unknown'} for c in state['commands'].values()),
                'outcome_unknown', 'Native operation must settle before auxiliary diagnosis')
        require(sum(row['anchor'] == data['anchor'] for row in auxiliary.values()) < 2,
                'no_progress', 'Auxiliary role exhausted its bounded evidence review')
        require(not any(row['anchor'] == data['anchor'] and row['status'] == 'reserved' for row in auxiliary.values()),
                'outcome_unknown', 'Prior auxiliary role needs reconciliation')
        budget = state['budget']
        for used, limit in ((budget['model_calls'], budget['limit']),
                            (budget['repair_model_calls'], budget['repair_limits']['model_calls'])):
            require(limit is None or used < limit, 'budget_exhausted', 'Explicit model call limit reached')
        budget['model_calls'] += 1
        budget['repair_model_calls'] += 1
        auxiliary[data['id']] = {**data, 'status': 'reserved'}
    elif kind == 'recovery_auxiliary_finished':
        row = state['recovery']['auxiliary'].get(data['id'])
        require(row is not None and row['status'] == 'reserved' and data.get('result_ref'),
                'recovery_permit', 'Auxiliary result has no open reservation')
        row.update(status='finished', result_ref=data['result_ref'])
    else:
        require(False, 'recovery_policy', 'Unknown recovery event')
