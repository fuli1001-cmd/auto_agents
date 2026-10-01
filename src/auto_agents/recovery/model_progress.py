"""Explain a stopped search from retained outcomes without granting retry credit."""
from .model import require


def diagnostic(state):
    budget = state['budget']
    result = {'workflow_id': state['workflow_id'],
              'stagnant': budget['stagnant'], 'rediagnoses': budget['rediagnoses'],
              'diagnosis_due': budget['diagnosis_due'],
              'next_action': 'correct_retained_candidate_and_reverify'}
    rejected = [command for command in state['commands'].values()
                if (command.get('outcome') or {}).get('kind') == 'candidate_rejected']
    if rejected:
        command = max(rejected, key=lambda row: row['sequence'])
        task = state['tasks'][command['task_id']]
        result['last_rejection'] = {
            'command_id': command['command_id'],
            'task_id': task['contract'].get('parent_task') or command['task_id'],
            'source': command['source'], 'reason': command['outcome']['reason'],
            'result_ref': command['outcome'].get('details', {}).get('native_result')
                          or command['outcome'].get('details', {}).get('result_ref'),
        }
    return result


def require_progress(state, phase):
    budget = state['budget']
    if budget['stagnant'] >= 2 and not (phase == 'diagnose' and budget['diagnosis_due']):
        require(False, 'no_progress', 'Verified progress is required before more model work',
                **diagnostic(state))


def stopped_search(project, failure):
    """Only suppress terminal model triage for an authoritative exhausted search."""
    if failure.evidence['resolution'] not in {'kernel_no_progress', 'agent_errors_exhausted'}:
        return None
    from .authority import installed
    store = installed(project)
    if store is None:
        return None
    evidence = failure.evidence
    stream = store.binding(project, evidence['kind'] + ':' + evidence['subject_id'])
    if stream is None:
        return None
    state = store.load(stream)
    if 'recovery' in state:
        from .convergence import scope_id, failures
        scopes = state['recovery']['scopes']
        task_id = evidence['mode'] + ':' + evidence['subject_id']
        key = scope_id(state, task_id) if task_id in state['tasks'] else None
        candidates = [(key, value) for key, value in scopes.items() if value['latest']
                      and not value['observations'][value['latest']]['complete']]
        if key in scopes:
            item = scopes[key]
        elif candidates:
            key, item = max(candidates, key=lambda pair: max(
                (c['sequence'] for c in state['commands'].values()
                 if scope_id(state, c['task_id']) == pair[0]), default=0))
        else:
            return {'workflow_id': state['workflow_id'], 'policy': 2,
                    'next_action': 'inspect_recovery_evidence', 'reason': evidence['reason'][:4000]}
        result = {'workflow_id': state['workflow_id'], 'policy': 2, 'scope': key,
                  'stagnant': item['stalled'], 'rediagnoses': item['diagnoses'],
                  'diagnosis_due': False, 'next_action': 'inspect_recovery_evidence',
                  'observation': item['latest'], 'reason': evidence['reason'][:4000]}
        value = item['observations'].get(item['latest'], {})
        if not value.get('complete') and failures(item):
            result['last_rejection'] = {'reason': value['reason'][:4000], 'command_id': value['command_id'],
                                       'source': value['source'], 'task_id': value['task_id']}
        elif item['routes']:
            result['next_action'] = 'inspect_route_evidence'
            result['routes'] = dict(item['routes'])
            deferred = next((row for row in reversed(evidence.get('recent_execution', []))
                             if row.get('action') == 'run_route_deferred'), None)
            if deferred:
                result['reason'] = str(deferred.get('result', ''))[:4000]
        return result
    if state['budget']['stagnant'] < 2 or state['budget']['diagnosis_due']:
        return None
    return diagnostic(state)
