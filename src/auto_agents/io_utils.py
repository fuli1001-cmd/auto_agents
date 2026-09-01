from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_write(path, payload)


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    _atomic_write(path, content)


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        write_text(path, content)


def _atomic_write(path: Path, content: str) -> None:
    """Durably replace *path* without exposing a partially-written file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_mode = path.stat().st_mode & 0o7777
    except OSError:
        target_mode = 0o644
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), target_mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()
