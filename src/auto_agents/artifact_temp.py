"""tempfile-compatible producer hooks for all orchestration stages."""
import tempfile as _tempfile
from .artifact_runtime import track, release


def __getattr__(name):
    return getattr(_tempfile, name)


class TemporaryDirectory(_tempfile.TemporaryDirectory):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._artifact_id = track(self.name)

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            release(self._artifact_id)


def mkdtemp(*args, **kwargs):
    path = _tempfile.mkdtemp(*args, **kwargs)
    prefix = str(kwargs.get("prefix", ""))
    # Long-lived approved runtimes/partial candidates require an explicit owner
    # to dispose them; a vanished creating process isn't proof of obsolescence.
    kind = "recovery" if any(x in prefix for x in ("approved-runtime", "self-repair-worktree", "replay-checkpoint")) else "scratch"
    track(path, kind)
    return path


def mkstemp(*args, **kwargs):
    fd, path = _tempfile.mkstemp(*args, **kwargs)
    track(path)
    return fd, path
