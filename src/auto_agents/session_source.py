"""Coordinator-owned provenance for commits absent from the shared repository."""
from pathlib import Path

from .config import load_session_state
from .io_utils import read_json
from .repair_control import atomic_json


def _identity(root, state, handoff):
    from .session_candidate import _git, validate_receipt, completed_delivery
    from .session_verification import fingerprint, ownership_error, product_path
    custody = state.candidate_custody
    source = Path(custody['checkout'])
    managed = root / '.auto-agents' / 'candidate-custody'
    if source.resolve() != source or not source.is_relative_to(managed.resolve()):
        raise ownership_error(state, 'source checkout is outside managed custody')
    validate_receipt(state)
    revision = custody.get('delivered_revision') or custody.get('base_revision')
    head = _git(source, 'rev-parse', 'HEAD')
    if not revision or head not in {revision, custody.get('base_revision')}:
        raise ownership_error(state, 'source revision does not match retained custody')
    changes = set(_git(source, 'diff', '--name-only', '-z', 'HEAD').split('\0'))
    changes.update(_git(source, 'ls-files', '--others', '--exclude-standard', '-z').split('\0'))
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
    custody = state.candidate_custody
    owned_copy = (custody.get('source_id') == source_id and custody.get('session_id') == state.session_id
                  and custody.get('repository') == str(root))
    path = Path(custody['checkout'] if owned_copy else source['checkout'])
    if path.resolve() != path or not path.is_relative_to(root / '.auto-agents/candidate-custody'):
        raise ownership_error(state, 'private source checkout was replaced')
    try:
        tree = _git(path, 'rev-parse', source['revision'] + '^{tree}')
        _git(path, 'cat-file', '-e', source['contract_revision'] + '^{commit}')
    except (OSError, RuntimeError) as error:
        raise ownership_error(state, 'retained private source is unavailable') from error
    if tree != source['tree']:
        raise ownership_error(state, 'retained private source tree changed')
    return path
