"""Reap only controller-labelled containers whose kernel lease has ended."""
import fcntl
import json
from pathlib import Path
import re

from .store import digest


def labels(root, lease, *, kind):
    return ['--label', 'org.auto-agents.v2.root=' + digest(str(Path(root).resolve())),
            '--label', 'org.auto-agents.v2.kind=' + kind,
            '--label', 'org.auto-agents.v2.lease=' + lease]


def reap_containers(root, *, kind):
    from .docker import run
    root = Path(root).resolve()
    owner = digest(str(root))
    code, listing = run(['docker', 'ps', '-aq', '--filter', 'label=org.auto-agents.v2.root=' + owner], timeout=15)
    if code or not listing.strip(): return []
    code, text = run(['docker', 'inspect', *listing.split()], timeout=15)
    if code: return []
    removed = []
    for container in json.loads(text):
        meta = container.get('Config', {}).get('Labels') or {}
        if meta.get('org.auto-agents.v2.root') != owner or meta.get('org.auto-agents.v2.kind') != kind:
            continue
        lease = meta.get('org.auto-agents.v2.lease', '')
        if kind == 'verification':
            if not re.fullmatch('[a-f0-9]{32}', lease): continue
            if container.get('Name', '').lstrip('/') != 'aav2-' + lease: continue
            base = root / 'executions' / lease
        elif kind == 'provider':
            if lease not in ('writer', 'reviewer'): continue
            if not re.fullmatch('aa-agent-[a-f0-9]{32}', container.get('Name', '').lstrip('/')): continue
            base = root / 'leases' / lease
        else: continue
        path = base / 'lease'
        if base.is_symlink() or path.is_symlink() or not path.is_file(): continue
        with path.open('a+b') as handle:
            try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: continue
            code, _ = run(['docker', 'rm', '-f', container['Id']], timeout=15)
            if code == 0: removed.append(container['Id'])
    return removed
