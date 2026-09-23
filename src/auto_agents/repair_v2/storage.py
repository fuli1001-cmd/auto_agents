"""Space admission and disposable working copies, separate from recovery evidence."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import threading
import time

from .types import RepairBlocked
from ..storage_admission import RESERVE_BYTES
_copy_lock = threading.Lock()


def _git_lock(root, path):
    """Git's transient metadata locks are never part of a source snapshot."""
    path = Path(path)
    return path.name.endswith('.lock') and path.is_relative_to(Path(root) / '.git')


def tree_bytes(root):
    """Estimate copied bytes without traversing links or reading file contents."""
    total = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not _git_lock(root, Path(parent) / name)]
        for name in files:
            path = Path(parent) / name
            if not _git_lock(root, path) and not path.is_symlink():
                total += path.stat().st_size
    return total


def require_space(root, additional=0):
    from ..storage_admission import DiskSpaceError, require_space as check
    try: check(root, additional)
    except DiskSpaceError as error:
        raise RepairBlocked(error.code, str(error)) from error


@contextmanager
def disposable_source(source, destination, *, cleanup=lambda: True):
    """Keep Git semantics but release the complete test copy on every exit."""
    destination = Path(destination)
    try:
        # Serialize admission/copy within this process so concurrent shards do
        # not all spend the same observed free bytes at once.
        with _copy_lock:
            require_space(destination, tree_bytes(source))
            # Concurrent read-only Git commands may refresh the index. Copying
            # their lock can race its removal or strand a stale lock in a shard.
            # Filter only this repository's metadata; source lockfiles and
            # missing ordinary inputs still follow the normal copy/error path.
            shutil.copytree(source, destination, symlinks=True,
                            ignore=lambda parent, names: [name for name in names
                                if _git_lock(source, Path(parent) / name)])
        yield destination
    finally:
        if cleanup() and destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)


@contextmanager
def execution_lease(base):
    """Kernel lease survives threads and becomes reclaimable after process death."""
    base = Path(base)
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (base / 'lease').open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def recover_executions(root, active_mounts, *, deadline=float('inf'), record=None):
    """Only remove disposable sources with a known lease and no Docker user.

    Retain logs/results, immutable snapshots and all unknown/legacy directories.
    Caller must obtain active mounts from a successful Docker inspection first.
    """
    recovered = []
    from ..artifact_store import _identity, _parents, _parent_fd, _remove_at, _allocated
    for base in (Path(root) / 'executions').glob('*'):
        if time.monotonic() >= deadline: break
        if base.is_symlink() or (base / 'lease').is_symlink() or not (base / 'lease').is_file():
            continue
        with (base / 'lease').open('a+b') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            for path in (base / 'source', base / 'target'):
                if time.monotonic() >= deadline: break
                if not path.is_dir() or path.is_symlink() or any(
                        p == path or path in p.parents or p in path.parents for p in active_mounts):
                    continue
                row = {'inode': _identity(path), 'parents': _parents(path)}
                size, complete = _allocated(path, min(deadline, time.monotonic() + .2))
                with _parent_fd(row, path) as fd:
                    _remove_at(fd, path.name, row['inode'][0], deadline)
                recovered.append(str(path))
                if record:
                    record({'path': str(path), 'kind': 'orphan_verification_copy', 'result': 'deleted',
                            'freed_bytes': size, 'size_complete': complete})
    return recovered
