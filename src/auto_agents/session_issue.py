"""Read routed fix authority from control state, never from a source clone."""
from pathlib import Path

from .io_utils import read_json
from .session_verification import ownership_error, SessionOwnershipError


def routed_issue(session, state, *, _retained_issue=None):
    if state.mode != 'fix' or not state.parent_handoff_id:
        return None
    from .workflow_chain import WorkflowStore
    root = Path(getattr(session, '_custody_control_root', session.project_root)).resolve()
    path = root / '.auto-agents/state/sessions' / state.session_id / 'issue.json'
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ownership_error(state, 'routed fix issue leaves its control repository')
    if not state.workflow_id and not state.source_descriptor and not state.verification_binding:
        # Older unbound fix conversations carried a local issue reference,
        # before coordinator-owned workflow identities existed.
        legacy = read_json(path, default={})
        if not legacy:
            return None
        if (not isinstance(legacy,dict) or legacy.get('issue_id') != 'issue-' + state.session_id
                or legacy.get('source_handoff_id') != state.parent_handoff_id):
            raise ownership_error(state, 'legacy fix issue belongs to another session or handoff')
        return legacy
    try:
        handoff = WorkflowStore(root).load_handoff(state.parent_handoff_id)
        issue = read_json(path, default={}) if _retained_issue is None else _retained_issue
    except (OSError, ValueError, TypeError) as error:
        raise ownership_error(state, 'authoritative routed fix issue is unavailable') from error
    child = handoff.child
    if (handoff.workflow_id != state.workflow_id
            or child is None or child.kind != 'fix' or child.native_id != state.session_id
            or handoff.payload.get('child_session_id', state.session_id) != state.session_id
            or state.source_descriptor != dict(handoff.payload.get('source_descriptor', {}))
            or not isinstance(issue, dict)):
        raise ownership_error(state, 'routed fix issue belongs to another session or handoff')
    seed = handoff.payload.get('issue_seed', {})
    if not isinstance(seed, dict):
        raise ownership_error(state, 'routed fix handoff has no readable issue seed')
    if not seed and not issue:
        return None  # Legacy task-owned handoffs may have no separate brief.
    if (issue.get('issue_id') != 'issue-' + state.session_id
            or issue.get('source_handoff_id') != handoff.handoff_id):
        raise ownership_error(state, 'routed fix issue belongs to another session or handoff')
    for key in ('verification_scope', 'task_id', 'task_ids', 'requirement_ids', 'retained_task_relation'):
        if issue.get(key) != seed.get(key):
            raise ownership_error(state, 'routed fix issue authority conflicts with original handoff', field=key)
    command = seed.get('verification_command', '')
    if (not isinstance(command, str)
            or issue.get('verification_command', '') != command.strip()
            or handoff.payload.get('verification_command', command) != command):
        raise ownership_error(state, 'routed fix issue command conflicts with original handoff')
    # The immutable handoff supplies the reproduction and obligations. Local
    # classification prose is evidence, not a replacement authorization.
    authoritative = {**seed, 'issue_id': issue['issue_id'],
                     'source_handoff_id': handoff.handoff_id,
                     'reported_goal': handoff.goal, 'verification_command': command.strip()}
    authoritative['summary'] = seed.get('summary') or issue.get('summary', '')
    return authoritative


def authoritative_command(session, state):
    """Use the sealed command or original routed command as a single contract."""
    issue = routed_issue(session, state)
    binding = state.verification_binding
    command = binding.get('fix_verify_command') if binding else None
    routed = issue.get('verification_command', '') if issue is not None else ''
    if command and routed and command != routed:
        raise ownership_error(state, 'bound command conflicts with original routed issue')
    return command or routed


def validate_classification(session, state, disposition):
    """Reject conflicting commands before materializing an issue or mutation."""
    issue = routed_issue(session, state)
    command = authoritative_command(session,state)
    proposed = disposition.get('verification_command', '')
    if command and (not isinstance(proposed,str) or proposed and proposed.strip() != command):
        raise ownership_error(state, 'classification conflicts with authoritative fix verification command',
                              original_command=command, proposed_command=str(proposed).strip())
    if issue is not None:
        if disposition.get('source_handoff_id', state.parent_handoff_id) != state.parent_handoff_id:
            raise ownership_error(state, 'classification changes the original fix handoff')
        for key in ('task_id', 'task_ids', 'requirement_ids', 'verification_scope'):
            if key in disposition and disposition[key] != issue.get(key):
                raise ownership_error(state, 'classification changes routed fix authority', field=key)
    return issue


def recover_classification_command(session, state, *, check_only=False):
    """Reclassify only a proven pre-writer overwrite or rejected proposal."""
    binding = state.verification_binding
    original = binding.get('fix_verify_command') if binding else None
    rejected = state.execution_log[-1] if state.execution_log else {}
    diagnostic = rejected.get('diagnostic', {})
    protocol_rejection = bool(original and original == state.fix_verify_command
        and rejected.get('action') == 'execution_preflight_blocked'
        and rejected.get('failure_kind') == 'verification_ownership'
        and rejected.get('result') == 'classification conflicts with authoritative fix verification command'
        and diagnostic.get('original_command') == original
        and diagnostic.get('proposed_command') and diagnostic['proposed_command'] != original
        and diagnostic.get('session_id') == state.session_id
        and diagnostic.get('handoff_id') == state.parent_handoff_id
        and diagnostic.get('workflow_id') == state.workflow_id)
    if (state.mode != 'fix' or state.status != 'blocked'
            or state.resolution != 'verification_ownership' or not state.source_descriptor
            or not original or (original == state.fix_verify_command and not protocol_rejection)):
        return False
    if (state.current_attempt or state.candidate_paths or state.lineage_changed_paths
            or state.persistence_actions or state.candidate_custody.get('receipt')
            or state.candidate_custody.get('delivered_revision')):
        raise ownership_error(state, 'classified command cannot be recovered after implementation')
    forbidden = {'fix', 'commit', 'receipt_writer_result', 'receipt_verification',
                 'receipt_completion', 'candidate_superseded', 'implementation_attempts_retained'}
    if any(row.get('action') in forbidden for row in state.execution_log):
        raise ownership_error(state, 'classified command recovery has prior writer evidence')
    import json
    root = Path(getattr(session, '_custody_control_root', session.project_root)).resolve()
    issue_path = root / '.auto-agents/state/sessions' / state.session_id / 'issue.json'
    canonical_issue = read_json(issue_path, default={})
    repair_issue = None
    try:
        issue = routed_issue(session, state)
    except SessionOwnershipError:
        # The old private-clone writer could update the canonical record while
        # leaving the controller's original projection untouched. Recover only
        # that exact pre-writer corruption, with a still-authenticated original
        # physical projection, handoff and sealed verification binding.
        if (not isinstance(canonical_issue, dict)
                or canonical_issue.get('issue_id') != 'issue-' + state.session_id
                or canonical_issue.get('source_handoff_id') != state.parent_handoff_id
                or canonical_issue.get('verification_command') != state.fix_verify_command
                or canonical_issue.get('decision') != 'fix'):
            raise
        try:
            original_issue = json.loads(issue_path.read_text())
        except (OSError,ValueError) as projection_error:
            raise ownership_error(state, 'original routed issue projection is unavailable') from projection_error
        issue = routed_issue(session,state,_retained_issue=original_issue)
        repair_issue = original_issue
    if (issue is None or issue.get('verification_command') != original
            or binding.get('task_scope', {}).get('mode') != 'focused_fix'):
        raise ownership_error(state, 'classified command recovery sources disagree')
    from copy import deepcopy
    from .session_source import resolve_source
    from .session_verification import _validate_binding_identity, product_path
    trial = deepcopy(state)
    trial.fix_verify_command = original
    _validate_binding_identity(session, trial)
    source = resolve_source(Path(session.project_root), trial)
    from .session_candidate import _git
    if _git(source, 'rev-parse', 'HEAD') != state.source_descriptor['revision']:
        raise ownership_error(state, 'classified command recovery checkout advanced beyond original source')
    changed = _git(source, 'diff', '--name-only', '-z', 'HEAD').split('\0')
    changed += _git(source, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
    from .gate_execution import discover_dependency_links
    dependencies = discover_dependency_links(Path(session.project_root))
    for name in changed:
        if not name or not product_path(name):
            continue
        link = source / name
        if name in dependencies and link.is_symlink() and link.readlink() == dependencies[name]:
            continue
        raise ownership_error(state, 'classified command recovery has uncommitted product edits', path=name)
    if check_only:
        return True
    previous = state.fix_verify_command
    if repair_issue is not None:
        from .config import save_session_state
        # Persist the overwritten record as evidence before replacing it. A
        # crash at any subsequent boundary leaves a retryable preflight.
        state.execution_log.append({'action':'routed_issue_projection_recovery',
            'source_handoff_id':state.parent_handoff_id,'previous_issue':dict(canonical_issue)})
        save_session_state(root,state)
        from .business_state import write_projection
        write_projection(issue_path,repair_issue,expected=getattr(canonical_issue,'reference',None))
    state.fix_verify_command = original
    state.status, state.resolution, state.resume_phase = 'conversing', '', ''
    state.execution_log.append({'action': 'routed_issue_classification_recovered',
        'previous_command': previous, 'restored_command': original,
        'source_handoff_id': state.parent_handoff_id,
        **({'rejected_classification':dict(rejected)} if protocol_rejection else {})})
    from .config import save_session_state
    save_session_state(session.project_root, state)
    return True
