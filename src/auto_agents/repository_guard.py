"""Exact, read-only observations used around untrusted repair operations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from .git_ops import (
    _capture_worktree_path,
    _git_bytes,
    _semantic_index_fingerprint,
    repository_path_is_excluded,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def capture_repository_guard(
    root: Path, *, ignore_run_artifacts: bool = False
) -> dict[str, object]:
    """Observe content and index semantics, never a truncated diagnostic diff.

    Only the explicitly requested run output directory is excluded. Product
    contracts, configuration and durable workflow state remain protected.
    """
    excluded = (".auto-agents/runs",) if ignore_run_artifacts else ()
    if not root.exists():
        return {"head": "", "index": "", "paths": {}}
    head = _git_bytes(root, "rev-parse", "--verify", "HEAD")
    tracked = _git_bytes(root, "ls-files", "-z")
    untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    tracked_names = set()
    if tracked.returncode or untracked.returncode:
        if (root / ".git").exists():
            raise RuntimeError("could not observe repository for repair protection")
        # Diagnosis is also valid before git/bootstrap has succeeded. Observe
        # that directory exactly instead of hashing git's constant error text.
        names = []
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name not in {
                ".git", "__pycache__", ".pytest_cache", ".conda", ".venv", "node_modules", ".data",
            } and not repository_path_is_excluded((Path(current) / name).relative_to(root).as_posix(), excluded)]
            names.extend((Path(current) / name).relative_to(root).as_posix() for name in files)
            names.extend((Path(current) / name).relative_to(root).as_posix() for name in directories if (Path(current) / name).is_symlink())
        index = ""
    else:
        tracked_names = {raw.decode("utf-8", errors="surrogateescape") for raw in tracked.stdout.split(b"\0") if raw}
        names = sorted(tracked_names | {
            raw.decode("utf-8", errors="surrogateescape") for raw in untracked.stdout.split(b"\0") if raw
        })
        index = _semantic_index_fingerprint(root, excluded)
    # Include all tracked paths: a clean path can become dirty during the call.
    paths: dict[str, str] = {}
    for name in names:
        if repository_path_is_excluded(name, excluded):
            continue
        # Untracked editor recovery files are not candidate work.
        if name.endswith((".swp", ".swo")) and ("/." in name or name.startswith(".")):
            if name not in tracked_names:
                continue
        snapshot = _capture_worktree_path(root, name)
        paths[name] = str(snapshot["fingerprint"])
    return {
        "head": head.stdout.decode().strip() if head.returncode == 0 else "",
        "index": index,
        "paths": paths,
    }


def guard_fingerprint(observation: Mapping[str, object]) -> str:
    return _digest(observation)


def changed_guard_paths(
    before: Mapping[str, object], after: Mapping[str, object]
) -> list[str]:
    old = dict(before.get("paths", {}))
    new = dict(after.get("paths", {}))
    changes = [name for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name)]
    if before.get("head") != after.get("head"):
        changes.append("<HEAD>")
    if before.get("index") != after.get("index"):
        changes.append("<index>")
    return changes
