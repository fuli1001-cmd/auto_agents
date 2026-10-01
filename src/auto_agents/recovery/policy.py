"""Effect adapter for the pure convergence policy; never alter historical events."""
import json
from pathlib import Path

from .model import Command, Event, KernelError, digest, require
from .convergence import decision, scope, failures, record_diagnosis
from .observations import observation, diagnosis_schema, compact


def apply(store, stream, kind, data, identity):
    return store.apply(stream, store.load(stream)['revision'], Event(identity, kind, data))


def enable(store, stream):
    if 'recovery' not in store.load(stream):
        apply(store, stream, 'recovery_policy_activated', {'version': 2}, 'recovery-policy-2:' + digest(stream))
    from .rejections import reconcile
    reconcile(store, stream)
    snapshot = store.load(stream)
    for identity, call in snapshot['recovery']['auxiliary'].items():
        if call['status'] != 'reserved': continue
        with store.connect() as db:
            saved = db.execute('SELECT receipt FROM kernel_results WHERE command_id=?', ('aux:' + identity,)).fetchone()
        if saved:
            apply(store, stream, 'recovery_auxiliary_finished', {'id': identity, 'result_ref': saved['receipt']},
                  'aux-finish:' + identity)
    # Import only executor-produced observations. Old prose is retained as
    # context, never upgraded into a passed check by the migration.
    latest = {}
    from .convergence import scope_id
    for row in sorted(snapshot['commands'].values(), key=lambda c: c['sequence']):
        if (row['phase'] == 'verify' and row['status'] == 'finished'
                and row['sequence'] < snapshot['recovery']['activation_revision']):
            latest[scope_id(snapshot, row['task_id'])] = row
    for row in latest.values():
        if scope(snapshot, row['task_id'])['latest']: continue
        ref = row['outcome']['details'].get('native_result') or row['outcome']['details'].get('result_ref')
        if not ref: continue
        result = store.read(ref)
        if not isinstance(result, dict): continue
        command = Command(**{key: row[key] for key in Command.__dataclass_fields__})
        full = observation(command, result, verifier=row['runtime'])
        value = compact(full)
        if not value['checks']: continue
        store.put(full)
        apply(store, stream, 'recovery_observation_imported',
              {'command_id': row['command_id'], 'result_ref': ref, 'observation': value},
              'recovery-import:' + digest([stream, row['command_id'], ref]))


def automatic(store, stream):
    runtime = store.meta('active_runtime') or {}
    # Only a verified adopted runtime advertises this capability. Legacy
    # embedders can explicitly enable the policy using the same event.
    path = runtime.get('path')
    if path and Path(path).resolve() == Path(__file__).resolve().parents[3]:
        enable(store, stream)


def reserve(store, stream, command):
    snapshot = store.load(stream)
    if 'recovery' in snapshot and command.model_call:
        existing = snapshot['recovery']['permits'].get(command.command_id)
        if existing is None:
            selected = decision(snapshot, command.task_id, command.phase, command.source)
            code = selected['reason'] if selected['reason'] in {'outcome_unknown', 'user_budget_exhausted', 'review_evidence'} else 'no_progress'
            require(selected['allowed'], code, 'Recovery action requires new verified evidence', **selected)
            apply(store, stream, 'recovery_permit_issued', {'command': command.to_dict(), 'decision': selected},
                  command.command_id + ':permit')
    return apply(store, stream, 'command_reserved', command.to_dict(), command.command_id + ':reserve')


def observation_summary(item):
    observed = item['observations'].get(item['latest'], {})
    checks = observed.get('checks', {})
    failed = failures(item)
    return {'identity': item['latest'], 'source': observed.get('source'), 'reason': observed.get('reason', '')[:4000],
            'basis': observed.get('basis', 'verification'),
            'complete': observed.get('complete', False), 'check_count': len(checks),
            'failure_count': len(failed), 'failures': [checks[key] for key in failed[:20]],
            'full_evidence': '.auto-agents/recovery-evidence/' + item['latest'] + '.json'}


def materialize_observation(root, snapshot, task_id, store):
    item = scope(snapshot, task_id)
    if not item['latest']: return
    from ..io_utils import write_json
    path = Path(root)/'.auto-agents/recovery-evidence'/ (item['latest'] + '.json')
    observed = item['observations'][item['latest']]
    write_json(path, store.read(observed['details_ref']))
    contract = snapshot['tasks'][task_id]['contract']
    write_json(path.with_name(item['latest'] + '-contract.json'), {
        'contract': contract, 'goal': store.read(contract['goal_ref']), 'issue': store.read(contract['issue_ref'])})


def diagnosis_input(snapshot, task_id, store=None):
    item = scope(snapshot, task_id)
    keys = failures(item)[:20]
    schema = diagnosis_schema(keys, item['latest'])
    from .convergence import diagnosis_usage
    payload = {'observation': item['latest'], 'verification': observation_summary(item),
               'previous_hypotheses': item['hypotheses'], 'diagnoses_remaining': max(0, 2 - diagnosis_usage(item)[0]),
               'contract': snapshot['tasks'][task_id]['contract'], 'response_schema': schema}
    if store is not None:
        contract = payload['contract']
        payload.update(goal=json.dumps(store.read(contract['goal_ref']), ensure_ascii=False)[:4000],
                       issue=json.dumps(store.read(contract['issue_ref']), ensure_ascii=False)[:4000],
                       complete_contract='.auto-agents/recovery-evidence/' + item['latest'] + '-contract.json')
    prompt = ('Diagnose this retained verification failure or independent review counterexample. '
              'Review findings are information to investigate, not proof that a correction works. '
              'Do not modify files or make external service calls. '
              'Use the concrete failed checks to identify a falsifiable cause and the smallest source correction. '
              'Keep all original verification obligations. Return exactly one JSON object matching response_schema. '
              'Read the complete evidence and original contract when the summaries are insufficient. '
              'Name every relative source path needed by the correction, including regression tests; '
              'additional product paths require independent necessity review before delivery. '
              'Use observed failure IDs. Do not repeat a disproved hypothesis.\n'
              + json.dumps(payload, ensure_ascii=False))
    return prompt, schema


def parse_diagnosis(snapshot, command, text):
    from copy import deepcopy
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise KernelError('diagnosis_invalid', 'Diagnosis must return one JSON object') from error
    # Run exactly the same validator before settling the command. Invalid
    # responses are durable protocol failures, not uncertain external effects.
    record_diagnosis(deepcopy(snapshot), command.to_dict(), value)
    return value


def result_details(store, stream, command, result):
    if 'recovery' not in store.load(stream): return {}
    if command.phase == 'review' and (result.get('kind') == 'candidate_rejected' or result.get('ok') is False):
        from .observations import review_observation
        value = review_observation(command, result, verifier=command.runtime)
    elif command.phase == 'verify':
        value = observation(command, result, verifier=command.runtime)
    else: return {}
    if value is None: return {}
    return {'verification_observation': compact(value), 'observation_ref': store.put(value)}


def correction_context(snapshot, task_id):
    item = scope(snapshot, task_id)
    return json.dumps({'current_verification': observation_summary(item),
                       'approved_correction': (item['correction'] or {}).get('proposal'),
                       'protected_checks': item['protected'][:80], 'protected_count': len(item['protected'])}, ensure_ascii=False)


def correction_paths(snapshot, task_id, before, after):
    item = scope(snapshot, task_id)
    proposal = (item['correction'] or {}).get('proposal')
    if not proposal and item['hypotheses']: proposal = item['hypotheses'][-1]['proposal']
    if not proposal: return []
    paths = [p.rstrip('/') for p in proposal['paths']]
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
                  and not any(path == allowed or path.startswith(allowed + '/') for allowed in paths))


def retained_failure_commands(session, state, commands):
    """Prior failures only select existing managed commands; they grant no credit."""
    if not getattr(session, '_recovery_policy_active', False): return []
    import re
    rows = [row for row in state.execution_log if row.get('action') == 'receipt_verification'
            and row.get('verification', {}).get('ok') is False]
    if not rows: return []
    prior = rows[-1]['verification']
    refs = list((prior.get('diagnostic') or {}).get('failure_ids', []))
    if not refs and 'new failure(s) introduced:' in prior.get('reason', ''):
        refs = re.findall(r'[\w./-]+\.py(?:::[\w.-]+)+', prior['reason'])
    baseline = set(getattr(state, 'baseline_failures', []))
    refs = [ref for ref in refs if '.py::' in ref and ref not in baseline][:10]
    if not refs: return []
    from fnmatch import fnmatch
    from ..verification_context import current_context
    from ..pytest_selection import selected_nodes
    from ..session_verification import _mandatory_refs
    from ..execution_binding import RunnerContextError
    candidates = []
    for index, command in enumerate(dict.fromkeys(commands)):
        selected = set()
        try:
            for invocation in current_context(session, state).invocations(command):
                if invocation.runner != 'pytest': continue
                targets = invocation.repository_targets or ()
                if not any(ref == target or ref.startswith(target + '::') or ref.startswith(target + '[')
                           or ref.startswith(target.rstrip('/') + '/')
                           or fnmatch(ref.split('::', 1)[0], target) for ref in refs for target in targets):
                    continue
                selected.update(selected_nodes(session, state, invocation,
                                expected_missing=_mandatory_refs(state))[0])
        except (RunnerContextError, OSError, ValueError):
            continue
        covered = {ref for ref in refs if any(node == ref or node.startswith(ref + '[') for node in selected)}
        if covered: candidates.append((len(selected) / len(covered), index, command, covered))
    remaining, chosen = set(refs), []
    for _, _, command, covered in sorted(candidates):
        if remaining & covered:
            chosen.append(command)
            remaining -= covered
    return chosen


def session_stop(session, state):
    """None means legacy policy; an empty string means recovery can continue."""
    from .authority import installed
    root = getattr(session, '_custody_control_root', None) or session.project_root
    store = installed(root)
    if store is None: return None
    stream = store.binding(root, 'session:' + state.session_id)
    if not stream: return None
    snapshot = store.load(stream)
    if 'recovery' not in snapshot: return None
    task_id = state.mode + ':' + state.session_id
    if task_id not in snapshot['tasks']: return ''
    session._recovery_policy_active = True
    from .native import _source
    phase = 'implement' if state.mode == 'fix' else 'route' if state.mode == 'collab' else 'research'
    selected = decision(snapshot, task_id, phase, _source(session, state))
    if selected['allowed'] or selected['action'] == 'diagnose': return ''
    state.resolution = 'kernel_no_progress'
    return 'Recovery stopped: ' + selected['reason'] + '; retained verification and candidate remain available.'


def resume_rejected_diagnosis(session, state):
    """A schema-only rejection does not invalidate its unchanged failed candidate."""
    from .authority import installed
    root = getattr(session, '_custody_control_root', None) or session.project_root
    store = installed(root)
    if store is None: return False
    stream = store.binding(root, 'session:' + state.session_id)
    if not stream: return False
    snapshot = store.load(stream)
    if 'recovery' not in snapshot: return False
    task_id = state.mode + ':' + state.session_id
    if task_id not in snapshot['tasks']: return False
    item = scope(snapshot, task_id)
    if not item.get('rejected_requests'): return False
    session._recovery_policy_active = True
    from .native import _source
    source = _source(session, state)
    observed = item['observations'].get(item['latest'], {})
    if observed.get('source') != source or observed.get('complete'): return False
    selected = decision(snapshot, task_id, 'implement', source)
    if selected['action'] != 'diagnose' and not (selected['allowed'] and selected['reason'] == 'evidence_bound_correction'):
        return False
    rejected = snapshot['commands'][item['rejected_requests'][-1]['command_id']]
    if rejected['source'] != source or rejected['runtime'] == (store.meta('active_runtime') or {}).get('source'):
        return False
    session._receipt_retry_feedback = 'The previous diagnosis request was explicitly rejected before model execution. Use the retained failed checks with the corrected request protocol.'
    state.status, state.resolution = 'executing', ''
    session._save(state)
    return True


def auxiliary_call(orchestrator, request, execute):
    """Account independent read-only roles without attaching business proofs."""
    from dataclasses import asdict
    from uuid import uuid4
    from .authority import installed
    from ..models import AgentResult, AgentUsage, AgentTermination
    root = getattr(orchestrator, '_kernel_project', None) or orchestrator.project_root
    store = installed(root)
    invocation = getattr(orchestrator, '_invocation_context', {}) or {}
    native = request.usage_context.get('subject_id') or invocation.get('session_id') or invocation.get('run_id')
    if store is None or not native: return execute(request)
    name = ('session:' if request.usage_context.get('subject_id') or invocation.get('session_id') else 'run:') + native
    stream = store.binding(root, name)
    if stream is None or 'recovery' not in store.load(stream): return execute(request)
    snapshot = store.load(stream)
    subjects = [key for key in snapshot['tasks'] if key.split(':', 1)[-1] == native
                and not snapshot['tasks'][key]['contract'].get('parent_task')]
    require(len(subjects) == 1, 'kernel_binding', 'Independent review has no unambiguous original task')
    anchor = digest([subjects[0], request.stage,
                     request.logical_call_id.rsplit(':', 1)[0] if request.logical_call_id else
                     invocation.get('controlled_failure') or invocation.get('engine_route') or request.purpose])
    # The root-cause coordinator binds the original evidence, source and policy
    # in this call ID. Temporary snapshot paths must not cause paid redispatch
    # or exhaust a different incident's bounded role allowance.
    identity = (digest([anchor, request.logical_call_id])
                if request.stage in {'self_repair_investigator', 'self_repair_reviewer', 'self_repair_arbiter'}
                and request.logical_call_id.startswith('root-cause:') else
                digest([anchor, request.logical_call_id, request.attempt_id, str(request.prompt)]))
    def commit(kind, data, key):
        for attempt in range(5):
            try: return apply(store, stream, kind, data, key)
            except KernelError as error:
                if error.code != 'stale_transition' or attempt == 4: raise
    prior = snapshot['recovery']['auxiliary'].get(identity)
    if prior is not None:
        require(prior['status'] == 'finished', 'outcome_unknown', 'Independent review outcome needs reconciliation')
        plain = store.read(prior['result_ref'])
        return AgentResult(**{**plain, 'output_path': Path(plain['output_path']),
            'usage': AgentUsage(**plain['usage']) if plain.get('usage') else None,
            'termination': AgentTermination(**plain['termination']) if plain.get('termination') else None})
    commit('recovery_auxiliary_reserved', {'id': identity, 'anchor': anchor, 'owner': subjects[0],
        'lease': uuid4().hex,
        'stage': request.stage, 'request_ref': store.put({'prompt': str(request.prompt), 'schema': request.response_schema})},
        'aux-reserve:' + identity)
    # Crashes leave a reserved call; they cannot silently cause a paid retry.
    result = execute(request)
    plain = asdict(result)
    plain['output_path'] = str(plain['output_path'])
    reference = store.put(plain)
    with store.connect() as db:
        db.execute('INSERT INTO kernel_results VALUES(?,?)', ('aux:' + identity, reference))
    commit('recovery_auxiliary_finished', {'id': identity, 'result_ref': reference}, 'aux-finish:' + identity)
    return result
