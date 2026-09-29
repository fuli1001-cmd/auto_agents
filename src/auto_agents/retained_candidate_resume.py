"""Revisit a stopped child's exact receipt before asking its parent for new work."""
from pathlib import Path

from .config import load_session_state, save_session_state
from .repair_control import digest
from .workflow_chain import WorkflowRef


def prepare(coordinator, parent, snapshot):
    if parent.active_handoff_id or parent.status not in {'failed', 'blocked'} or not parent.last_child_result_ref:
        return False
    from .recovery.authority import installed
    store = installed(coordinator.project_root)
    stream = store.binding(coordinator.project_root, 'session:' + parent.session_id) if store else None
    kernel = store.load(stream) if stream else {}
    recoverable = {'kernel_no_progress'}
    if 'recovery' in kernel:
        recoverable.update({'kernel_environment_blocked', 'kernel_protocol_invalid', 'verification_inconclusive'})
        recoverable.add('kernel_ownership_conflict')
        if (not any(command['status'] in {'reserved', 'running', 'unknown'} for command in kernel['commands'].values())
                and any(item.get('rejected_requests') for item in kernel['recovery']['scopes'].values())):
            recoverable.add('kernel_outcome_unknown')
    if parent.resolution not in recoverable | {'agent_errors_exhausted'}:
        return False
    reference = Path(parent.last_child_result_ref)
    if reference.parts[-4:] != ('.auto-agents', 'state', 'handoffs', reference.stem + '.json'):
        return False
    returned = coordinator.store.load_handoff(reference.stem)
    if (returned.workflow_id != snapshot.workflow_id or not returned.returned_at
            or returned.parent != WorkflowRef(parent.mode, parent.session_id)
            or returned.status != 'blocked' or returned.result.get('resolution') not in recoverable):
        return False
    original = coordinator._resolved_handoff_chain(returned, snapshot.workflow_id)[-1]
    child_id = (original.child.native_id if original.child and original.child.kind == 'fix'
                else coordinator._engine_child_id(original.payload, snapshot))
    if not child_id:
        return False
    child = load_session_state(coordinator.project_root, child_id)
    if child.resolution == 'kernel_outcome_unknown':
        from .recovery.convergence import scope
        if not scope(kernel, 'fix:' + child_id).get('rejected_requests'): return False
    owned = coordinator.store.load_handoff(child.parent_handoff_id)
    coordinator._validated_child_handoff(child, owned)
    if (owned.parent != returned.parent or child.resolution not in recoverable | {
            'proof_review_unavailable', 'proof_review_interrupted', 'proof_review_invalid'}):
        return False
    if child.resolution == 'kernel_ownership_conflict':
        from .recovery.scope_amendments import recover_writer
        if not recover_writer(coordinator.project_root, child): return False
    receipt = child.candidate_custody.get('receipt')
    if not receipt:
        return False
    # Identity/receipt checks run before a continuation is published. Existing
    # calls and retry credits stay consumed; only fresh verification can unlock
    # further model work. This also recovers legacy consumed engine returns.
    from .session_candidate import validate_receipt
    validate_receipt(child)
    if store is None:
        return False
    coordinator._preserve_engine_resume_budget = True
    runtime = store.meta('active_runtime') or {}
    key = digest([owned.handoff_id, receipt['fingerprint'], runtime.get('source')])
    stream = store.binding(coordinator.project_root, 'session:' + child_id)
    kernel = store.load(stream) if stream else {}
    if 'recovery' in kernel:
        from .recovery.convergence import scope
        item = scope(kernel, 'fix:' + child_id)
        key = digest([key, 2, item['latest'], item['diagnoses'], item['correction']])
    retry = coordinator.store.prepare_handoff(snapshot, parent=returned.parent, target='resume',
        goal=owned.goal, reason='Reverify the retained candidate after a stopped search',
        payload={'resume_handoff_id': owned.handoff_id}, handoff_id='hf-' + key[:12])
    if retry.returned_at:
        return False  # Same candidate/runtime cannot create another retry chain.
    parent.active_handoff_id = retry.handoff_id
    parent.status, parent.resolution, parent.return_phase = 'waiting_child', '', ''
    save_session_state(coordinator.project_root, parent)
    return True
