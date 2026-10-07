"""Coordinator-owned provenance for commits absent from the shared repository."""
from pathlib import Path

from .config import load_session_state
from .io_utils import read_json
from .local_io import atomic_json


def register_checkout(root, state, checkout):
    """Retain runtime identity independently of temporary-directory settings."""
    from .session_verification import fingerprint
    checkout = Path(checkout).resolve()
    info = checkout.stat()
    record = {'repository': str(Path(root).resolve()), 'session_id': state.session_id,
              'checkout': str(checkout), 'device': info.st_dev, 'inode': info.st_ino}
    atomic_json(Path(root) / '.auto-agents/state/custody' / (fingerprint(str(checkout)) + '.json'), record)
    track_checkout(root, state, checkout)


def track_checkout(root, state, checkout):
    from .artifact_runtime import track
    return track(Path(checkout).parent, 'recovery', project=root,
          metadata={'candidate_lifecycle': 1, 'session_id': state.session_id,
                    'workflow_id': state.workflow_id, 'checkout': str(checkout)},
          reference='session:' + str(Path(root).resolve()) + ':' + state.session_id)


def validate_checkout(root, state, checkout, *, owner_id=None):
    from .session_verification import fingerprint, ownership_error
    root, checkout = Path(root).resolve(), Path(checkout)
    if checkout.resolve() != checkout or not (checkout / '.git').is_dir() or (checkout / '.git').is_symlink():
        raise ownership_error(state, 'private source checkout was replaced')
    # Existing durable sessions predate external runtime registration. Retain
    # their original independent repositories without relocating their receipts.
    if checkout.is_relative_to(root / '.auto-agents/candidate-custody'):
        return
    info = checkout.stat()
    expected = {'repository': str(root), 'session_id': owner_id or state.session_id,
                'checkout': str(checkout), 'device': info.st_dev, 'inode': info.st_ino}
    stored = read_json(root / '.auto-agents/state/custody' / (fingerprint(str(checkout)) + '.json'), default={})
    if checkout.is_relative_to(root) or stored != expected:
        raise ownership_error(state, 'private source checkout is not registered to this session')


def _identity(root, state, handoff):
    from .session_candidate import _git, validate_receipt, completed_delivery
    from .gate_execution import discover_dependency_links
    from .session_verification import fingerprint, ownership_error, product_path
    custody = state.candidate_custody
    source = Path(custody['checkout'])
    validate_checkout(root, state, source)
    validate_receipt(state)
    revision = custody.get('delivered_revision') or custody.get('base_revision')
    head = _git(source, 'rev-parse', 'HEAD')
    if not revision or head not in {revision, custody.get('base_revision')}:
        raise ownership_error(state, 'source revision does not match retained custody')
    changes = set(_git(source, 'diff', '--name-only', '-z', 'HEAD').split('\0'))
    changes.update(_git(source, 'ls-files', '--others', '--exclude-standard', '-z').split('\0'))
    tracked = set(_git(source, 'ls-files', '-z').split('\0'))
    # The checkout installs links to the source repository's dependency
    # directories. Admit only those exact, untracked links; a changed link or
    # an ordinary product edit must still prevent a handoff.
    for relative, expected in discover_dependency_links(root).items():
        if relative not in changes or relative in tracked:
            continue
        link = source / relative
        try:
            if link.is_symlink() and link.readlink() == expected:
                changes.discard(relative)
        except OSError:
            pass
    if custody.get('delivered_revision'):
        completed_delivery(state)
    elif any(path and product_path(path) for path in changes):
        raise ownership_error(state, 'private source has uncommitted product changes; preserve them before handoff')
    return {'schema_version': 1, 'repository': str(root.resolve()),
            'checkout': str(source), 'revision': revision,
            'tree': _git(source, 'rev-parse', revision + '^{tree}'),
            'session_id': state.session_id, 'workflow_id': state.workflow_id,
            'handoff_id': handoff.handoff_id,
            'authorization_fingerprint': fingerprint(state.authorization_policy),
            'contract_revision': state.verification_binding.get('contract_revision') or custody.get('contract_revision', revision),
            'binding_fingerprint': state.verification_binding.get('binding_fingerprint', ''),
            'delivery_fingerprint': fingerprint(custody.get('consumed_delivery') or custody.get('receipt'))}


def register_source(root, state, handoff):
    """Ignore model-provided source fields; derive them from durable parent state."""
    from .session_verification import fingerprint
    root = Path(root).resolve()
    if handoff.target == 'resume':
        # A continuation references the original handoff's source. It never
        # registers a new implementation source under its own handoff ID.
        handoff.payload.pop('source_descriptor', None)
        return
    existing = handoff.payload.get('source_descriptor', {})
    if (isinstance(existing, dict) and existing.get('source_id') == fingerprint(
            {key: value for key, value in existing.items() if key != 'source_id'})
            and existing.get('handoff_id') == handoff.handoff_id
            and existing.get('session_id') == state.session_id
            and existing.get('repository') == str(root)
            and read_json(root / '.auto-agents/state/sources' / (existing['source_id'] + '.json'), default={}) == existing):
        return
    handoff.payload.pop('source_descriptor', None)
    if not state.candidate_custody:
        return
    source = _identity(root, state, handoff)
    source['source_id'] = fingerprint(source)
    atomic_json(root / '.auto-agents/state/sources' / (source['source_id'] + '.json'), source)
    handoff.payload['source_descriptor'] = source
    handoff.payload['head_before'] = source['revision']


def recover_resume_source(root, state, handoff, *, check_only=False):
    """Remove only the exact source registration minted by the old resume path."""
    from copy import deepcopy
    from .workflow_chain import WorkflowStore, WorkflowRef
    from .session_verification import fingerprint
    root = Path(root).resolve()
    proposed = handoff.payload.get('source_descriptor')
    if (handoff.target != 'resume' or not proposed or handoff.child is not None
            or handoff.status != 'prepared' or handoff.returned_at or handoff.result
            or state.active_handoff_id != handoff.handoff_id
            or handoff.workflow_id != state.workflow_id
            or handoff.parent != WorkflowRef(state.mode, state.session_id)):
        return False
    # Check every other chain authority before considering this one exact
    # controller registration. Arbitrary source disagreements stay rejected.
    trial = deepcopy(handoff)
    trial.payload.pop('source_descriptor')
    store = WorkflowStore(root)
    try:
        original = store.resolve_handoff_chain(trial,workflow_id=state.workflow_id)[-1]
        if original.child is None or original.child.kind != 'fix' or not state.candidate_custody:
            return False
        expected = _identity(root,state,handoff)
        expected['source_id'] = fingerprint(expected)
        child = load_session_state(root,original.child.native_id)
        registered = read_json(root/'.auto-agents/state/sources'/(expected['source_id']+'.json'),default={})
        inherited = {**expected,'handoff_id':original.handoff_id}
        inherited.pop('source_id')
        inherited['source_id'] = fingerprint(inherited)
        original_registration = read_json(root/'.auto-agents/state/sources'/(inherited['source_id']+'.json'),default={})
    except (OSError,ValueError,RuntimeError,KeyError,TypeError):
        return False
    if proposed != expected or registered != expected:
        return False
    if (original.payload.get('source_descriptor') != inherited
            or original_registration != inherited
            or child.source_descriptor != inherited or child.parent_handoff_id != original.handoff_id
            or child.workflow_id != state.workflow_id):
        return False
    if check_only:
        return True
    from .config import save_session_state
    state.execution_log.append({'action':'resume_source_registration_recovered',
        'handoff_id':handoff.handoff_id,'original_handoff_id':original.handoff_id,
        'previous_source_descriptor':dict(proposed)})
    if state.status == 'blocked' and state.resolution == 'verification_ownership':
        state.status,state.resolution,state.resume_phase = 'waiting_child','',''
    # Save the evidence and continuation phase first. If interrupted, the
    # normal chain guard still rejects the old wrapper until recovery retries.
    save_session_state(root,state)
    handoff.payload.pop('source_descriptor')
    store.save_handoff(handoff)
    return True


def resolve_source(root, state):
    from .session_candidate import _git
    from .session_verification import fingerprint, ownership_error
    from .workflow_chain import WorkflowStore
    root = Path(root).resolve()
    source = getattr(state, 'source_descriptor', {})
    if not source:
        return root
    if source.get('schema_version') != 1 or not all(isinstance(source.get(key), str) and source[key]
            for key in ('source_id', 'checkout', 'revision', 'tree', 'session_id', 'workflow_id', 'handoff_id')):
        raise ownership_error(state, 'private source descriptor is incomplete or unsupported')
    source_id = source.get('source_id', '')
    if source_id != fingerprint({k: v for k, v in source.items() if k != 'source_id'}):
        raise ownership_error(state, 'private source descriptor changed')
    try:
        stored = read_json(root / '.auto-agents/state/sources' / (source_id + '.json'), default={})
        handoff = WorkflowStore(root).load_handoff(state.parent_handoff_id)
    except (OSError, ValueError) as error:
        raise ownership_error(state, 'private source registration or handoff is unavailable') from error
    if (stored != source or source.get('repository') != str(root)
            or handoff.payload.get('source_descriptor') != source
            or source.get('handoff_id') != state.parent_handoff_id
            or source.get('workflow_id') != state.workflow_id
            or handoff.parent.native_id != source.get('session_id')
            or (handoff.child and handoff.child.native_id != state.session_id)
            or fingerprint(state.authorization_policy) != source.get('authorization_fingerprint')):
        raise ownership_error(state, 'private source is not authorized by this handoff')
    try:
        parent = load_session_state(root, source['session_id'])
    except (OSError, ValueError) as error:
        raise ownership_error(state, 'private source parent is unavailable') from error
    if parent.workflow_id != state.workflow_id:
        raise ownership_error(state, 'private source parent belongs to another workflow')
    from .execution_binding import validate_custody_binding
    if parent.verification_binding:
        validate_custody_binding(parent)
    custody = state.candidate_custody
    owned_copy = (custody.get('source_id') == source_id and custody.get('session_id') == state.session_id
                  and custody.get('repository') == str(root))
    path = Path(custody['checkout'] if owned_copy else source['checkout'])
    validate_checkout(root, state, path,
                      owner_id=state.session_id if owned_copy else source['session_id'])
    try:
        tree = _git(path, 'rev-parse', source['revision'] + '^{tree}')
        _git(path, 'cat-file', '-e', source['contract_revision'] + '^{commit}')
    except (OSError, RuntimeError) as error:
        raise ownership_error(state, 'retained private source is unavailable') from error
    if tree != source['tree']:
        raise ownership_error(state, 'retained private source tree changed')
    return path
