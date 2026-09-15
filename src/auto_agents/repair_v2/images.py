"""Shared image custody with durable pins for unfinished repair transactions."""
from contextlib import contextmanager
import fcntl
import json
from datetime import datetime, timezone
import re
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
def locked():
    root = registry()
    with (root / 'registry.lock').open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield root


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


def maintain(*, keep=2, age_days=14):
    """Do not touch unknown images, active containers, pins or Docker volumes."""
    from .docker import run
    removed = []
    with locked() as root:
        try:
            pins = [json.loads(p.read_text()) for p in (root / 'pins').glob('*.json')]
            records = [(p, json.loads(p.read_text())) for p in (root / 'images').glob('*.json')]
        except (OSError, ValueError): return []
        protected = {p['image'] for p in pins if p.get('active', True)}
        records.sort(key=lambda item: item[1]['used'], reverse=True)
        protected.update(row['image'] for _, row in records[:keep])
        deadline = time.monotonic() + 10
        for path, value in records:
            if time.monotonic() >= deadline: break
            if value['image'] in protected or time.time() - value['used'] < age_days * 86400: continue
            code, raw = run(['docker', 'image', 'inspect', value['image']], timeout=15)
            if code: continue
            info = json.loads(raw)[0]
            labels = info.get('Config', {}).get('Labels') or {}
            if labels.get('org.auto-agents.registry') != owner(): continue
            code, active = run(['docker', 'ps', '-aq', '--filter', 'ancestor=' + value['image']], timeout=15)
            if code or active.strip(): continue
            code, _ = run(['docker', 'image', 'rm', value['tag']], timeout=30)
            if code == 0:
                path.unlink(); removed.append(value['image'])
        if time.monotonic() < deadline:
            code, text = run(['docker', 'image', 'ls', '--filter', 'label=org.auto-agents.registry=' + owner(),
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
                        if not code: removed.append(value['Id'])
    return removed
