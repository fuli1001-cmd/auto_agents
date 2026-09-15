"""Shared image custody with durable pins for unfinished repair transactions."""
from contextlib import contextmanager
import fcntl
import json
import os
from datetime import datetime, timezone
import re
import shutil
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


def record(image, tag):
    with locked() as root:
        atomic_json(root / 'images' / (digest(image) + '.json'),
                    {'image': image, 'tag': tag, 'used': time.time()})


def pin(image, transaction):
    with locked() as root:
        atomic_json(root / 'pins' / (digest(str(Path(transaction).resolve())) + '.json'),
                    {'image': image, 'transaction': str(transaction), 'active': True})


def release(transaction):
    with locked() as root:
        path = root / 'pins' / (digest(str(Path(transaction).resolve())) + '.json')
        if path.exists():
            value = json.loads(path.read_text()); value['active'] = False
            atomic_json(path, value)


def _maintain(*, keep=2, age_days=14, deadline=None, registry_root=None, record=None):
    """Do not touch unknown images, active containers, pins or Docker volumes."""
    from .docker import run
    if not shutil.which('docker'): return []
    if registry_root is not None and not Path(registry_root).is_dir(): return []
    removed = []
    def report(image, result, reason=''):
        if record: record({'kind': 'image', 'id': image, 'result': result, 'reason': reason,
                           'freed_bytes': 0, 'size_complete': False})
    with locked(registry_root) as root:
        registry_owner = digest(str(root.resolve()))
        try:
            pins = [json.loads(p.read_text()) for p in (root / 'pins').glob('*.json')]
            records = [(p, json.loads(p.read_text())) for p in (root / 'images').glob('*.json')]
        except (OSError, ValueError):
            report(str(root), 'error', 'image_registry_unreadable'); return []
        protected = {p['image'] for p in pins if p.get('active', True)}
        records.sort(key=lambda item: item[1]['used'], reverse=True)
        protected.update(row['image'] for _, row in records[:keep])
        deadline = time.monotonic() + 10 if deadline is None else deadline
        for path, value in records:
            if time.monotonic() >= deadline:
                report(value['image'], 'deferred', 'image_cleanup_budget'); break
            if value['image'] in protected or time.time() - value['used'] < age_days * 86400:
                report(value['image'], 'retained', 'image_in_use_or_retained'); continue
            code, raw = run(['docker', 'image', 'inspect', value['image']], timeout=15)
            if code:
                report(value['image'], 'absent' if 'no such image' in raw.lower() else 'error', 'image_inspection_failed')
                continue
            info = json.loads(raw)[0]
            labels = info.get('Config', {}).get('Labels') or {}
            if labels.get('org.auto-agents.registry') != registry_owner:
                report(value['image'], 'retained', 'image_owner_unknown'); continue
            code, active = run(['docker', 'ps', '-aq', '--filter', 'ancestor=' + value['image']], timeout=15)
            if code or active.strip():
                report(value['image'], 'error' if code else 'retained', 'image_consumers_unknown' if code else 'image_has_containers')
                continue
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


def maintain(*, keep=2, age_days=14, deadline=None, registry_root=None, record=None):
    # Housekeeping must never turn accepted delivery into a repair failure.
    try: return _maintain(keep=keep, age_days=age_days, deadline=deadline, registry_root=registry_root, record=record)
    except (OSError, ValueError, KeyError, TypeError) as error:
        if record: record({'kind': 'image', 'result': 'error', 'reason': str(error), 'freed_bytes': 0})
        return []
