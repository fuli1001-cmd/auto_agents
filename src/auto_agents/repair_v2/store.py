"""One locked checkpoint and append-only journal per repair transaction."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


class Store:
    def __init__(self, root, callback=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.callback = callback

    @contextmanager
    def locked(self):
        with (self.root / 'job.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: yield
            finally: fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self):
        path = self.root / 'state.json'
        if not path.exists(): return None
        envelope = json.loads(path.read_text())
        if digest(envelope['state']) != envelope.get('digest'):
            raise ValueError('repair checkpoint digest mismatch')
        return envelope['state']

    def save(self, state):
        atomic_json(self.root / 'state.json', {'state': state, 'digest': digest(state)})

    def event(self, kind, **details):
        row = {'at': datetime.now(timezone.utc).isoformat(), 'kind': kind, **details}
        with (self.root / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
            stream.flush(); os.fsync(stream.fileno())
        if self.callback: self.callback(kind, details)

    def artifact(self, name, value):
        identity = digest(value)
        relative = Path('artifacts') / name / (identity + '.json')
        atomic_json(self.root / relative, value)
        return {'path': str(relative), 'digest': identity}

    def read(self, reference):
        path = self.root / reference['path']
        if not path.resolve().is_relative_to(self.root.resolve()) or path.is_symlink():
            raise ValueError('artifact path escapes repair storage')
        value = json.loads(path.read_text())
        if digest(value) != reference['digest']: raise ValueError('repair artifact digest mismatch')
        return value
