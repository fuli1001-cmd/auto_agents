"""Read failures from execution facts, independently of repair prose and budgets.

The legacy adapter is read-only. Its event identities survive restarts and
scene copies; successful execution closes the corresponding earlier failure.
"""
from dataclasses import asdict

from .store import digest
from .types import FailureIncident, RepairBlocked


VERIFY = {'verify', 'inventory_migration_verify', 'verification'}


def condition(row, phase):
    from .scope import failure_condition
    diagnostic = row.get('diagnostic') or {}
    return failure_condition({'phase': phase, 'code': row.get('failure_kind') or row.get('action'),
        'result': row.get('result') or row.get('reason'),
        'diagnostic': {k: diagnostic[k] for k in ('verification_ref', 'proof_id', 'contract_fingerprint',
            'session_id', 'workflow_id', 'handoff_id', 'task_scope') if k in diagnostic}})


def resolve_subject(target, root_state, route, workflow):
    from .scope import read_json, safe_id
    from ..execution_binding import route_sources
    sources = list(route_sources(route))
    children = {str(s['child_session_id']) for s in sources if s.get('child_session_id')}
    handoff = next((s.get('failed_handoff_id') or s.get('resume_handoff_id') for s in sources
                    if s.get('failed_handoff_id') or s.get('resume_handoff_id')),
                   root_state.get('active_handoff_id', ''))
    operation, visited = handoff, set()
    while handoff:
        if handoff in visited or len(visited) >= 64:
            raise RepairBlocked('scope_identity', '恢复交接存在循环或超出可核验的层数。')
        visited.add(handoff)
        row = read_json(target, f'.auto-agents/state/handoffs/{safe_id(handoff)}.json')
        if workflow and row.get('workflow_id') != workflow:
            raise RepairBlocked('scope_identity', '失败记录不属于当前工作流。')
        nested = row.get('payload', {}).get('resume_handoff_id')
        if nested:
            handoff = nested
            continue
        child = row.get('child') or {}
        if child.get('kind') in {'fix', 'collab', 'provider_resolve'}:
            children.add(child['native_id'])
        else:
            result = row.get('result') or {}
            diagnostic = result.get('diagnostic') or {}
            if diagnostic.get('session_id'):
                children.add(diagnostic['session_id'])
        operation = handoff
        break
    if len(children) > 1:
        raise RepairBlocked('scope_identity', '失败记录指向不同子流程，不能自动选择。')
    state = root_state
    if children:
        identity = safe_id(next(iter(children)))
        state = read_json(target, f'.auto-agents/state/sessions/{identity}/session_state.json')
        if workflow and state.get('workflow_id') != workflow:
            raise RepairBlocked('scope_identity', '子流程不属于当前工作流。')
        original = state.get('parent_handoff_id')
        if original:
            owner = read_json(target, f'.auto-agents/state/handoffs/{safe_id(original)}.json')
            child = owner.get('child') or {}
            if (owner.get('workflow_id') != state.get('workflow_id')
                    or child.get('native_id') != identity or child.get('kind') != state.get('mode')):
                raise RepairBlocked('scope_identity', '子流程与原交接的归属不一致。')
            declared = {s['original_handoff_id'] for s in sources if s.get('original_handoff_id')}
            if declared and declared != {original}:
                raise RepairBlocked('scope_identity', '声明的原交接与执行记录不一致。')
            operation = original
        elif handoff:
            raise RepairBlocked('scope_identity', '子流程缺少原交接记录。')
    return state, operation or state.get('active_execution_incident_id') or state.get('session_id', '')


def latest_failure(state):
    """Explicit result semantics; never match action-name substrings."""
    active = None
    for index, raw in enumerate(state.get('execution_log', [])):
        action = raw.get('action', '')
        row = raw
        if action == 'receipt_verification':
            # The preceding verify event is authoritative, not its duplicate.
            continue
        if action == 'child_returned':
            value = raw.get('result')
            if not isinstance(value, dict):
                continue
            row = value
            if row.get('status') not in {'failed', 'blocked'}:
                continue
        if action == 'engine_preflight_recheck':
            if active and active[2] == 'preflight':
                active = None
            continue
        if action in VERIFY and (row.get('ok') is True or row.get('result') == 'pass'):
            if active and active[2] == 'verification':
                active = None
            continue
        if action in {'receipt_completion', 'proof_review_approved'}:
            if action == 'receipt_completion' or active and active[2] == 'proof_review':
                active = None
            continue
        text = str(row.get('result') or row.get('reason') or '')
        if 'auto_agents engine self-repair required' in text:
            continue
        phase = ('preflight' if action == 'execution_preflight_blocked' else
                 'verification' if action in VERIFY else
                 'proof_review' if action in {'proof_review_rejected', 'proof_review_failed'} else 'execution')
        failed = (action in {'execution_preflight_blocked', 'quick_verify_fail', 'error', 'proof_review_rejected', 'proof_review_failed'}
                  or action in VERIFY and bool(row.get('failure_kind') or row.get('ok') is False or text)
                  or action == 'child_returned' and bool(row.get('failure_kind') or row.get('diagnostic')))
        if failed:
            same = active and condition(active[1], active[2]) == condition(raw, phase)
            active = (index, raw, phase, active[3] if same else index, active[4] + 1 if same else 1)
    return active


def observation(state, owner, operation):
    from .scope import failure_condition
    found = latest_failure(state)
    if found:
        index, raw, phase, anchor_index, revision = found
        row = {k: v for k, v in raw.items() if k != 'incident'}
        diagnostic = row.get('diagnostic') or {}
        code = row.get('failure_kind') or diagnostic.get('failure_kind') or row.get('action', 'execution_failed')
        check = diagnostic.get('verification_ref') or diagnostic.get('proof_id') or code
        event = {'path': (f".auto-agents/state/sessions/{state['session_id']}/session_state.json"
                          if state.get('session_id') else '.auto-agents/state/run_state.json'),
                 'pointer': f'/execution_log/{index}', 'sha256': digest(row)}
        observed = {'failure': failure_condition(row)}
        anchor = {k: v for k, v in state['execution_log'][anchor_index].items() if k != 'incident'}
        anchor_event = {'index': anchor_index, 'sha256': digest(anchor)}
    else:
        observed = {k: failure_condition(state[k]) for k in ('last_error', 'active_blocker') if state.get(k)}
        if not observed:
            return {}, None
        phase, code, check = 'execution', 'execution_failed', 'execution_failed'
        event = {'path': '', 'pointer': '', 'sha256': digest(observed)}
        anchor_event, revision = event, 1
    candidate = (state.get('candidate_custody') or {}).get('receipt', {}).get('fingerprint', '')
    identity = digest([owner, state.get('session_id'), operation, phase, code, check, anchor_event])
    record = asdict(FailureIncident(identity, {**owner, 'session_id': state.get('session_id'),
        'handoff_id': operation}, phase, code, check, event, candidate, revision=revision))
    if code in {'proof_review_required', 'proof_review_unavailable'}:
        record['domain'] = 'proof_review'
    elif code == 'owned_verification_failed':
        record['domain'] = 'product'
    elif candidate and code == 'verification_ownership' and check in state.get('candidate_paths', {}):
        record['domain'] = ('product' if check in state.get('verification_binding', {}).get('proof_control_paths', [])
                            else 'proof_review')
    elif code in {'verification_confinement', 'verification_infrastructure', 'verification_environment'}:
        record['domain'] = 'environment'
    return observed, record


def record(store, context):
    """Append a controller-owned incident snapshot without rewriting its source."""
    incident = context.get('incident')
    if incident:
        reference = store.artifact('incidents', incident)
        from .store import atomic_json
        atomic_json(store.root / 'incident.json', reference)
        return reference


def migrate(config, payload):
    """Index proved legacy resolutions without changing frozen contracts/history."""
    import json
    from pathlib import Path
    from .chain import workflow_key
    from .scope import context
    from .store import Store, atomic_json
    goal = workflow_key(payload)
    index = Store(Path(config['root']) / 'incident-index' / digest(goal))
    with index.locked():
        entries = {}
        resolved = {}
        for path in (Path(config['root']) / 'v2-transactions').glob('*/original-payload.json'):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            original = json.loads(path.read_text())
            if workflow_key(original) != goal:
                continue
            try:
                scoped = context(path.parent / 'target-evidence', original)
                incident = scoped.get('incident')
            except (OSError, ValueError, KeyError, TypeError, RepairBlocked):
                continue
            if not incident:
                continue
            entry = {'incident': incident['identity'], 'status': 'open'}
            saved = Store(path.parent).load() or {}
            proof = saved.get('live_recovery') or {}
            if saved.get('status') == 'complete' and saved.get('phase') == 'recovered' and proof:
                custody = saved.get('recovery_owner') or {}
                boundary = proof.get('boundary') or {}
                if (all(proof.get(k) and proof.get(k) == custody.get(k) for k in ('job', 'generation', 'artifact_id'))
                        and proof.get('operation') == saved.get('recovery_operation')
                        and boundary.get('session_id') == incident['owner']['session_id']
                        and boundary.get('original_handoff_id') == incident['owner']['handoff_id']):
                    entry.update(status='resolved', proof=digest(proof))
                    resolved[incident['identity']] = path.parent.name
            entries[path.parent.name] = entry
        for transaction, entry in entries.items():
            successor = resolved.get(entry['incident'])
            if successor and successor != transaction and entry['status'] == 'open':
                entry.update(status='superseded', successor=successor)
        value = {'version': 1, 'owner': goal, 'transactions': entries}
        old = index.load()
        if old != value:
            index.save(value)
        return value
