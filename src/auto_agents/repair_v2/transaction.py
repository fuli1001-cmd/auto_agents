"""Stable repair identity across CLI invocations and source upgrades."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re

from .store import Store, atomic_json, digest
from .types import RepairBlocked, RepairRequest


def intent(payload):
    from ..repair_control import contract_identity, workflow_identity
    return {'project': str(Path(payload['project']).resolve()),
            'workflow': workflow_identity(payload),
            'contract': contract_identity({**payload, 'contract': payload.get('contract', {})}),
            'route': payload.get('invocation', {}).get('engine_route'),
            'symptom': payload.get('symptom_key') or payload.get('fingerprint'),
            'autonomy': payload.get('autonomy')}


def transaction_root(config, payload):
    directory = Path(config['root']) / 'v2-transactions'
    expected = directory / digest(intent(payload))
    scope = _scope_identity(payload)
    if scope is not None:
        matches = []
        from .chain import workflow_key
        index = Path(config['root']) / 'incident-index' / digest(workflow_key(payload)) / 'state.json'
        retired = {}
        if index.exists():
            retired = Store(index.parent).load().get('transactions', {})
        for path in directory.glob('*/original-payload.json'):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            if retired.get(path.parent.name, {}).get('status') in {'resolved', 'superseded'}:
                continue
            original = json.loads(path.read_text())
            old = _scope_identity(original)
            if old is None or (original.get('scope_receipt') or {}).get('context', {}).get('policy') != 'goal-scope-v2':
                # Legacy identity is established from its retained scene, not
                # a paraphrased issue or the current project's ambient run.
                try:
                    from .scope import context
                    old = _context_identity(context(path.parent / 'target-evidence', original))
                except (OSError, ValueError, KeyError, TypeError, RepairBlocked):
                    continue
            if old == scope:
                saved = Store(path.parent).load() or {}
                if saved.get('status') != 'complete':
                    matches.append((path.stat().st_mtime_ns, path.parent))
        if matches:
            return max(matches, key=lambda row: row[0])[1]
        return directory / digest({'goal_scope': scope})
    invocation = payload.get('invocation', {})
    if invocation.get('session_id') and invocation.get('workflow_id'):
        legacy_payload = {**payload, 'invocation': {**invocation, 'workflow_id': ''}}
        legacy = directory / digest(intent(legacy_payload))
        # Prefer the original transaction even if the old bug already created
        # a second directory on resume. Keep both directories intact; the
        # original plan, candidate and budget remain the recovery authority.
        if (legacy.exists() or legacy.is_symlink()) and _matches_legacy_session(legacy, payload):
            return legacy
    return expected


def _context_identity(context):
    if (not isinstance(context, dict) or context.get('policy') not in {'goal-scope-v1', 'goal-scope-v2'}
            or not context.get('owner') or not context.get('goal_version') or not context.get('blocker')):
        return None
    return {key: context[key] for key in ('owner', 'goal_version', 'blocker')}


def _scope_identity(payload):
    record = payload.get('scope_receipt') or {}
    if record.get('proposal', {}).get('decision') != 'required' or not record.get('witnesses'):
        return None
    return _context_identity(record.get('context'))


def _matches_scope(root, payload):
    current = _scope_identity(payload)
    if current is None:
        return False
    original = json.loads((Path(root) / 'original-payload.json').read_text())
    if json.loads((Path(root) / 'intent.json').read_text()).get('digest') != digest(intent(original)):
        raise RepairBlocked('transaction_identity_unresolved', 'frozen intent no longer matches its original payload')
    previous = _scope_identity(original)
    if previous is None or (original.get('scope_receipt') or {}).get('context', {}).get('policy') != 'goal-scope-v2':
        from .scope import context
        previous = _context_identity(context(Path(root) / 'target-evidence', original))
    return previous == current


def _matches_legacy_session(root, payload):
    """Prove the missing ID from frozen evidence, never from the live project."""
    invocation = payload.get('invocation', {})
    session_id = str(invocation.get('session_id') or '')
    workflow_id = str(invocation.get('workflow_id') or '')
    if not workflow_id or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', session_id):
        return False
    root = Path(root)
    def read(relative):
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('legacy identity evidence escapes its transaction')
        return json.loads(path.read_text())
    try:
        if root.is_symlink():
            raise ValueError('legacy transaction is a symbolic link')
        original = read('original-payload.json')
        original_intent = intent(original)
        if read('intent.json').get('digest') != digest(original_intent):
            raise ValueError('legacy intent does not match its frozen payload')
        if original.get('invocation', {}).get('workflow_id'):
            return False
        restored = {**original, 'invocation': {**original.get('invocation', {}), 'workflow_id': workflow_id}}
        if digest(intent(restored)) != digest(intent(payload)):
            return False
        state = read(Path('target-evidence/.auto-agents/state/sessions') / session_id / 'session_state.json')
        if not state.get('workflow_id'):
            raise ValueError('frozen session has no workflow identity')
        return (state.get('session_id') == session_id and state.get('workflow_id') == workflow_id
                and state.get('mode') == str(invocation.get('command', '')).replace('-', '_'))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        # Missing/corrupt evidence must not silently restart a stopped search
        # with a fresh candidate and budget.
        raise RepairBlocked('transaction_identity_unresolved',
                            f'cannot establish the saved session identity in {root}: {error}') from error


@contextmanager
def transaction_lock(root):
    root = Path(root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (root / 'transaction.lock').open('a+b') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RepairBlocked('transaction_busy', 'the same repair is already running') from error
        yield


def frozen_request(root, payload, create):
    """Neither a fresh job UUID nor a new engine base resets the repair budget."""
    root = Path(root)
    marker = root / 'intent.json'
    frozen = root / 'request.json'
    expected = digest(intent(payload))
    if marker.exists():
        if (json.loads(marker.read_text()).get('digest') != expected
                and not _matches_scope(root, payload) and not _matches_legacy_session(root, payload)):
            raise RepairBlocked('request_changed', 'repair intent differs from its frozen contract')
        if not frozen.is_file():
            raise RepairBlocked('incomplete_request', 'frozen repair request is missing')
        return RepairRequest.from_dict(json.loads(frozen.read_text()))
    request = create()
    scoped = (payload.get('scope_receipt') or {}).get('context') or {}
    if scoped.get('policy') == 'goal-scope-v2' and scoped.get('incident'):
        from dataclasses import replace
        incident = scoped['incident']
        request = replace(request, incident_id=incident['identity'], incident_revision=incident['revision'],
                          contract_revision=digest([request.goal, [a.__dict__ for a in request.acceptance]]))
    atomic_json(frozen, request.to_dict())
    atomic_json(root / 'original-payload.json', payload)
    atomic_json(marker, {'digest': expected})
    return request


def bind_controller(root, config, owner):
    """Pin one controller per job generation; retries retain transaction budgets."""
    from .workspace import git, source_identity
    implementation = Path(config.get('implementation_root') or config['source_root']).resolve()
    identity = {'root': str(implementation), 'commit': git(implementation, 'rev-parse', 'HEAD'),
                'source': source_identity(implementation)}
    path = Path(root) / 'controllers' / (digest(owner) + '.json')
    if path.exists():
        saved = json.loads(path.read_text())
        pinned = Path(saved['root'])
        if source_identity(pinned) != saved['source']:
            raise RepairBlocked('controller_changed', 'pinned repair controller was modified')
        return saved
    atomic_json(path, identity)
    return identity
