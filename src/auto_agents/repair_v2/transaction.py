"""Stable repair identity across CLI invocations and source upgrades."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path

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
    return Path(config['root']) / 'v2-transactions' / digest(intent(payload))


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
        if json.loads(marker.read_text()).get('digest') != expected:
            raise RepairBlocked('request_changed', 'repair intent differs from its frozen contract')
        if not frozen.is_file():
            raise RepairBlocked('incomplete_request', 'frozen repair request is missing')
        return RepairRequest.from_dict(json.loads(frozen.read_text()))
    request = create()
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
