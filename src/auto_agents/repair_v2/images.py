"""Shared image custody with durable pins for unfinished repair transactions."""
from contextlib import contextmanager
import fcntl
import json
import os
from datetime import datetime, timezone
import re
import shutil
import sqlite3
import stat
from pathlib import Path
import time

from ..artifact_store import storage_root
from .store import atomic_json, digest


def registry():
    root = storage_root() / 'v2-images'
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return root


def owner():
    return digest(str(registry().resolve()))


@contextmanager
def locked(root=None):
    root = Path(root) if root is not None else registry()
    from ..artifact_store import _identity, _parents
    _identity(root); _parents(root)
    fd = os.open(root / 'registry.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid(): raise ValueError('unowned image registry lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally: os.close(fd)


def record(image, tag, *, size=0, status='ready'):
    with locked() as root:
        path = root / 'images' / (digest(image) + '.json')
        previous = json.loads(path.read_text()) if path.exists() else {}
        # A failed revalidation must not demote a previously usable shared image.
        if previous.get('status', 'ready') == 'ready' and previous and status != 'ready': return
        atomic_json(root / 'images' / (digest(image) + '.json'),
                    {'image': image, 'tag': tag, 'used': time.time(), 'bytes': size, 'status': status})
        if status == 'ready':
            for path in (root / 'builds').glob('*.json'):
                if json.loads(path.read_text()).get('tag') == tag: path.unlink()


def pin(image, transaction):
    with locked() as root:
        if (Path(transaction) / 'abandonment.json').exists():
            raise ValueError('abandoned repair transaction cannot acquire an image pin')
        atomic_json(root / 'pins' / (digest(str(Path(transaction).resolve())) + '.json'),
                    {'image': image, 'transaction': str(transaction), 'active': True})


def release(transaction):
    with locked() as root:
        path = root / 'pins' / (digest(str(Path(transaction).resolve())) + '.json')
        if path.exists():
            value = json.loads(path.read_text()); value['active'] = False
            atomic_json(path, value)


def acquire(tag):
    """Protect preparation and all later container launches by this process."""
    from ..artifact_store import process_identity
    process = process_identity()
    with locked() as root:
        path = root / 'leases' / (digest([tag, process]) + '.json')
        created = not path.exists()
        atomic_json(path, {'tag': tag, 'process': process})
        return created


@contextmanager
def preparation(tag):
    """Serialize the same tool image across verification roots, including tests."""
    from ..artifact_store import process_identity
    root = registry()
    locks = root / 'preparations'; locks.mkdir(mode=0o700, exist_ok=True)
    from ..artifact_store import _identity, _parents
    _identity(locks); _parents(locks)
    fd = os.open(locks / (digest(tag) + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid(): raise ValueError('unowned preparation lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        created = acquire(tag)
        try:
            yield
        except BaseException:
            # The journal remains if the daemon or filesystem is unavailable.
            # Maintenance recovers it after the process exits.
            try:
                with locked(root):
                    for path in (root / 'images').glob('*.json'):
                        value = json.loads(path.read_text())
                        if value['tag'] == tag and value.get('status') == 'preparing':
                            value['status'] = 'failed'; atomic_json(path, value)
                    if created:
                        (root / 'leases' / (digest([tag, process_identity()]) + '.json')).unlink(missing_ok=True)
                maintain(registry_root=root)
            except (OSError, ValueError, KeyError, TypeError): pass
            raise
    finally:
        os.close(fd)


def begin_build(tag, temporary):
    from ..artifact_store import process_identity
    with locked() as root:
        atomic_json(root / 'builds' / (digest(temporary) + '.json'),
                    {'tag': tag, 'temporary': temporary, 'created': time.time(), 'process': process_identity()})


def register_tag(tag, *, status):
    from .docker import run
    from .types import RepairBlocked
    code, text = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}} {{.Size}}'], timeout=15)
    if code: raise RepairBlocked('image_unavailable', text)
    identity, size = text.strip().split()
    record(identity, tag, size=int(size), status=status)
    return identity


def recover_builds(root, deadline, report):
    """A durable pre-import intent closes the tag-created/record-missing gap."""
    from ..artifact_store import alive
    from .docker import run
    for path in (root / 'builds').glob('*.json'):
        if time.monotonic() >= deadline: return
        value = json.loads(path.read_text())
        if alive(value['process']): continue
        code, text = run(['docker', 'image', 'inspect', value['tag']], timeout=15)
        if code:
            # Temporary building tags are handled by the normal orphan sweep.
            if 'no such image' in text.lower() and time.time() - value['created'] > 86400: path.unlink()
            continue
        info = json.loads(text)[0]; labels = info.get('Config', {}).get('Labels') or {}
        if (labels.get('org.auto-agents.registry') != digest(str(root.resolve()))
                or labels.get('org.auto-agents.purpose') != 'repair-verifier-v2'
                or any(labels.get('org.auto-agents.creator-' + k) != str(v) for k, v in value['process'].items())):
            report(value['tag'], 'retained', 'build_identity_changed'); continue
        target = root / 'images' / (digest(info['Id']) + '.json')
        if not target.exists():
            atomic_json(target, {'image': info['Id'], 'tag': value['tag'], 'used': value['created'],
                                 'bytes': info['Size'], 'status': 'failed'})
        path.unlink()


def release_process():
    """Explicit teardown for disposable test runtimes; never releases pins."""
    from ..artifact_store import process_identity
    root = storage_root() / 'v2-images'
    if not root.is_dir(): return
    with locked(root):
        for path in (root / 'leases').glob('*.json'):
            if json.loads(path.read_text())['process'] == process_identity(): path.unlink()


def policy(root):
    """Limits apply only to idle images; pins and live users always win."""
    values = {'keep': 2, 'retention_days': 14, 'max_unused': 4, 'max_unused_bytes': 8 << 30}
    path = root.parent / 'policy.json'
    if path.exists():
        document = json.loads(path.read_text())
        if not isinstance(document, dict): raise ValueError('storage policy must be an object')
        configured = document.get('verifier_images', {})
        if not isinstance(configured, dict): raise ValueError('verifier_images policy must be an object')
        for key in values:
            value = configured.get(key, values[key])
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError('invalid verifier_images policy: ' + key)
            values[key] = value
    if values['max_unused'] < values['keep']:
        raise ValueError('verifier_images.max_unused must be >= keep')
    return values


def release_completed(root, deadline):
    """Reconcile a crash after durable recovery, including publish-disabled runs.

    Cancelled, missing and merely superseded transactions remain pinned. The
    recovery receipt and control DB must agree; an isolated JSON flag is not
    evidence of delivery.
    """
    from contextlib import closing
    from .recovery import completed_result
    from .transaction import transaction_lock
    from .types import RepairBlocked
    from ..artifact_store import _identity, _parents
    for path in (root / 'pins').glob('*.json'):
        if time.monotonic() >= deadline: return
        value = json.loads(path.read_text())
        if not value.get('active', True): continue
        transaction = Path(value['transaction'])
        if transaction.parent.name != 'v2-transactions' or not transaction.is_dir(): continue
        _identity(transaction); _parents(transaction)
        control = transaction.parent.parent / 'control.sqlite3'
        if not control.is_file(): continue
        _identity(control)
        # Nonblocking: cleanup must never wait behind a running repair.
        try:
            with transaction_lock(transaction, allow_abandoned=True), closing(sqlite3.connect(control.as_uri() + '?mode=ro', uri=True)) as db:
                from .retirement import verified_abandonment
                if verified_abandonment(transaction, db):
                    value['active'] = False
                    atomic_json(path, value)
                    continue
                rows = db.execute("SELECT id,generation,result FROM jobs WHERE state='completed'").fetchall()
                for identity, generation, result in rows:
                    result = json.loads(result)
                    if result.get('v2_transaction') != str(transaction): continue
                    if completed_result({'id': identity, 'generation': generation, 'result': result}):
                        value['active'] = False
                        atomic_json(path, value)
                        break
        except (OSError, ValueError, KeyError, sqlite3.Error, RepairBlocked):
            continue


def _maintain(*, keep=None, age_days=None, deadline=None, registry_root=None, record=None):
    """Do not touch unknown images, active containers, pins or Docker volumes."""
    from .docker import run
    if not shutil.which('docker'): return []
    if registry_root is not None and not Path(registry_root).is_dir(): return []
    deadline = time.monotonic() + 10 if deadline is None else deadline
    removed = []
    def report(image, result, reason=''):
        if record: record({'kind': 'image', 'id': image, 'result': result, 'reason': reason,
                           'freed_bytes': 0, 'size_complete': False})
    with locked(registry_root) as root:
        registry_owner = digest(str(root.resolve()))
        limits = policy(root)
        keep = limits['keep'] if keep is None else keep
        age_days = limits['retention_days'] if age_days is None else age_days
        try:
            release_completed(root, deadline)
            recover_builds(root, deadline, report)
            pins = [json.loads(p.read_text()) for p in (root / 'pins').glob('*.json')]
            records = [(p, json.loads(p.read_text())) for p in (root / 'images').glob('*.json')]
        except (OSError, ValueError):
            report(str(root), 'error', 'image_registry_unreadable'); return []
        protected = {p['image'] for p in pins if p.get('active', True)}
        from ..artifact_store import alive
        leased = set()
        for path in (root / 'leases').glob('*.json'):
            value = json.loads(path.read_text())
            if alive(value['process']): leased.add(value['tag'])
            else: path.unlink()
        records.sort(key=lambda item: item[1]['used'], reverse=True)
        newest = [row for _, row in records if row['image'] not in protected and row['tag'] not in leased
                  and row.get('status', 'ready') == 'ready'][:keep]
        protected.update(row['image'] for row in newest)
        unused, unused_bytes = len(newest), sum(row.get('bytes', 0) for row in newest)
        for path, value in records:
            if time.monotonic() >= deadline:
                report(value['image'], 'deferred', 'image_cleanup_budget'); break
            if value['image'] in protected or value['tag'] in leased:
                report(value['image'], 'retained', 'image_in_use_or_retained'); continue
            unused += 1
            # Size is captured when the producer registers the image. Legacy
            # records still obey the count limit until next reused.
            unused_bytes += value.get('bytes', 0)
            pressure = (unused > limits['max_unused']
                        or unused_bytes > limits['max_unused_bytes'])
            if (value.get('status', 'ready') == 'ready' and not pressure
                    and time.time() - value['used'] < age_days * 86400):
                report(value['image'], 'retained', 'image_retention'); continue
            code, raw = run(['docker', 'image', 'inspect', value['image']], timeout=15)
            if code:
                absent = 'no such image' in raw.lower()
                if absent: path.unlink()
                report(value['image'], 'absent' if absent else 'error', 'image_inspection_failed')
                continue
            info = json.loads(raw)[0]
            labels = info.get('Config', {}).get('Labels') or {}
            if labels.get('org.auto-agents.registry') != registry_owner:
                report(value['image'], 'retained', 'image_owner_unknown'); continue
            code, active = run(['docker', 'ps', '-aq', '--filter', 'ancestor=' + value['image']], timeout=15)
            if code or active.strip():
                report(value['image'], 'error' if code else 'retained', 'image_consumers_unknown' if code else 'image_has_containers')
                continue
            code, current = run(['docker', 'image', 'inspect', value['tag'], '--format', '{{.Id}}'], timeout=10)
            if code or current.strip() != value['image']:
                report(value['image'], 'retained', 'image_tag_changed'); continue
            code, _ = run(['docker', 'image', 'rm', value['tag']], timeout=30)
            if code == 0:
                path.unlink(); removed.append(value['image'])
                report(value['image'], 'deleted')
            else: report(value['image'], 'error', 'image_removal_failed')
        if time.monotonic() < deadline:
            code, text = run(['docker', 'image', 'ls', '--filter', 'label=org.auto-agents.registry=' + registry_owner,
                              '--format', '{{.Repository}}:{{.Tag}}'], timeout=10)
            if code == 0:
                for tag in text.splitlines():
                    if time.monotonic() >= deadline: break
                    if not re.fullmatch(r'auto-agents-verifier:building-[a-f0-9]{32}', tag): continue
                    code, raw = run(['docker', 'image', 'inspect', tag], timeout=10)
                    if code: continue
                    value = json.loads(raw)[0]
                    try: created = datetime.strptime(value['Created'][:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
                    except (ValueError, KeyError, TypeError): continue
                    if (datetime.now(timezone.utc) - created).total_seconds() < 86400: continue
                    code, active = run(['docker', 'ps', '-aq', '--filter', 'ancestor=' + tag], timeout=10)
                    if not code and not active.strip():
                        code, _ = run(['docker', 'image', 'rm', tag], timeout=20)
                        if not code:
                            removed.append(value['Id']); report(value['Id'], 'deleted')
                        else: report(value['Id'], 'error', 'image_removal_failed')
    return removed


def maintain(*, keep=None, age_days=None, deadline=None, registry_root=None, record=None):
    # Housekeeping must never turn accepted delivery into a repair failure.
    try: return _maintain(keep=keep, age_days=age_days, deadline=deadline, registry_root=registry_root, record=record)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, sqlite3.Error) as error:
        if record: record({'kind': 'image', 'result': 'error', 'reason': str(error), 'freed_bytes': 0})
        return []


def reap_ephemeral(*, deadline, record):
    """Maintain owned namespaces using explicit producer provenance.

    Called only after the storage cleaner verifies a local Docker endpoint.
    Normal namespaces require their original registry and all its pin records.
    Only explicitly disposable test namespaces may outlive their registry.
    """
    from .docker import run
    from ..artifact_store import alive, _identity, _parents
    code, raw = run(['docker', 'image', 'ls', '--filter', 'label=org.auto-agents.purpose=repair-verifier-v2',
                     '--filter', 'label=org.auto-agents.uid=' + str(os.getuid()),
                     '--format', '{{.ID}}'], timeout=10)
    if code:
        record({'kind': 'image', 'result': 'deferred', 'reason': 'ephemeral_inventory_unavailable', 'freed_bytes': 0})
        return
    visited = set()
    for identity in dict.fromkeys(raw.split()):
        if time.monotonic() >= deadline:
            record({'kind': 'image', 'result': 'deferred', 'reason': 'image_cleanup_budget', 'freed_bytes': 0})
            break
        try:
            code, raw = run(['docker', 'image', 'inspect', identity], timeout=10)
            if code: continue
            info = json.loads(raw)[0]; labels = info.get('Config', {}).get('Labels') or {}
            ephemeral = labels.get('org.auto-agents.ephemeral') == 'true'
            if ((not ephemeral and labels.get('org.auto-agents.origin-version') != '1')
                    or labels.get('org.auto-agents.uid') != str(os.getuid())
                    or labels.get('org.auto-agents.purpose') != 'repair-verifier-v2'): continue
            root = Path(labels['org.auto-agents.registry-path'])
            if (not root.is_absolute() or '..' in root.parts or root.name != 'v2-images'
                    or digest(str(root)) != labels.get('org.auto-agents.registry')): continue
            creator = {key: labels['org.auto-agents.creator-' + key] for key in ('pid', 'ticks', 'boot')}
            creator['pid'] = int(creator['pid'])
            if creator['pid'] <= 0 or alive(creator): continue
            created = datetime.strptime(info['Created'][:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created).total_seconds() < 86400: continue
            try:
                root.lstat()
                exists = True
            except FileNotFoundError:
                exists = False
                # Permission failures and symbolic ancestors do not prove a
                # registry disappeared. Missing temporary parents are normal.
                for parent in root.parents:
                    try: mode = parent.lstat().st_mode
                    except FileNotFoundError: continue
                    if not stat.S_ISDIR(mode): raise ValueError('unsafe registry ancestor')
            if exists:
                _identity(root); _parents(root)
                if root not in visited:
                    visited.add(root)
                    maintain(keep=0 if ephemeral else None, age_days=1 if ephemeral else None,
                             deadline=deadline, registry_root=root, record=record)
                # An unregistered image in an existing namespace could still
                # belong to a failed preparation. Never bypass its pins.
                continue
            if not ephemeral:
                record({'kind': 'image', 'id': identity, 'result': 'retained',
                        'reason': 'image_registry_missing_requires_recovery', 'freed_bytes': 0})
                continue
            # No registry survives, and the explicitly disposable producer has
            # exited. Docker still vetoes removal of images with any container.
            code, containers = run(['docker', 'ps', '-aq', '--filter', 'ancestor=' + info['Id']], timeout=10)
            if code or containers.strip(): continue
            for tag in info.get('RepoTags') or []:
                if not re.fullmatch(r'auto-agents-verifier:(v2-[a-f0-9]{24}|building-[a-f0-9]{32})', tag): continue
                # Recheck tag identity: a mutable tag is never deletion authority.
                code, current = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], timeout=10)
                if code or current.strip() != info['Id']: continue
                code, _ = run(['docker', 'image', 'rm', tag], timeout=20)
                record({'kind': 'image', 'id': info['Id'], 'result': 'error' if code else 'deleted',
                        'reason': 'expired_ephemeral_namespace', 'freed_bytes': 0, 'size_complete': False})
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            record({'kind': 'image', 'id': identity, 'result': 'retained',
                    'reason': 'ephemeral_identity_unverified: ' + str(error), 'freed_bytes': 0})
