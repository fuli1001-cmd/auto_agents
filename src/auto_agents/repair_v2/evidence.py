"""Bind all copied recovery inputs, including Git-ignored workflow state."""
import hashlib
import os
from pathlib import Path

from .store import digest
from .workspace import git


def identity(root):
    root = Path(root)
    records = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in [*files, *(n for n in directories if (Path(current) / n).is_symlink())]:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                records[relative] = ['link', os.readlink(path)]
            elif path.is_file():
                value = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
                records[relative] = [value.hexdigest(), path.stat().st_mode & 0o777]
    return digest(records)


def dissociate(root):
    root = Path(root)
    if (root / '.git').is_dir():
        git(root, 'repack', '-a', '-d')
        (root / '.git/objects/info/alternates').unlink(missing_ok=True)
