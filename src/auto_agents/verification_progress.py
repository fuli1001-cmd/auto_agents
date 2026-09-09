"""Stage observations written by the trusted worker, outside candidate storage."""
import fcntl
import json
import os
from pathlib import Path

from .repair_control import atomic_json


def publish(checkpoints):
    configured = os.environ.get('AUTO_AGENTS_VERIFICATION_PROGRESS')
    if not configured or not checkpoints:
        return
    path = Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            existing = {}
        values = set(existing.get('checkpoints', [])) | set(checkpoints)
        atomic_json(path, {'context': path.stem, 'checkpoints': sorted(values)})
