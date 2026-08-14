from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Optional


class ForegroundActivity:
    """Advisory lease giving interactive sessions priority over release work."""

    def __init__(self, project_root: Path) -> None:
        root = Path(project_root).expanduser().resolve()
        key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / "auto-agents-foreground" / f"{key}.lock"
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise RuntimeError("another foreground auto_agents session is active") from error
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        os.close(fd)


def foreground_active(project_root: Path) -> bool:
    lease = ForegroundActivity(project_root)
    if not lease.path.exists():
        return False
    fd = os.open(lease.path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
