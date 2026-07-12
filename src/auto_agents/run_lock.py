from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional


RUN_LOCK_FD_ENV = "AUTO_AGENTS_RUN_LOCK_FD"
RUN_LOCK_KEY_ENV = "AUTO_AGENTS_RUN_LOCK_KEY"


class RunAlreadyActiveError(RuntimeError):
    """Raised when another process already owns the target project's run lock."""


class ProjectRunLock:
    """Process lock for a target project, with explicit self-repair handoff support."""

    def __init__(self, project_root: Path, *, environ: Optional[Mapping[str, str]] = None) -> None:
        self.project_root = project_root.expanduser().resolve()
        self._environ = os.environ if environ is None else environ
        self.key = hashlib.sha256(str(self.project_root).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / "auto-agents-run-locks" / f"{self.key}.lock"
        self._fd: Optional[int] = None

    @property
    def fileno(self) -> int:
        if self._fd is None:
            raise RuntimeError("project run lock is not acquired")
        return self._fd

    def acquire(self) -> "ProjectRunLock":
        if self._fd is not None:
            return self

        inherited_fd = self._inherited_fd()
        if inherited_fd is not None:
            self._fd = inherited_fd
            return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            owner = self._read_owner(fd)
            os.close(fd)
            detail = f" ({owner})" if owner else ""
            raise RunAlreadyActiveError(
                f"another auto_agents run is already active for {self.project_root}{detail}"
            ) from error

        payload = {
            "pid": os.getpid(),
            "project": str(self.project_root),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
        self._fd = fd
        return self

    def _inherited_fd(self) -> Optional[int]:
        if self._environ.get(RUN_LOCK_KEY_ENV) != self.key:
            return None
        raw_fd = str(self._environ.get(RUN_LOCK_FD_ENV, "")).strip()
        if not raw_fd:
            return None
        try:
            fd = int(raw_fd)
            inherited_stat = os.fstat(fd)
            path_stat = self.path.stat()
        except (OSError, ValueError):
            return None
        if (inherited_stat.st_dev, inherited_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            return None
        return fd

    @staticmethod
    def _read_owner(fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
            payload = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        pid = payload.get("pid")
        started_at = payload.get("started_at")
        fields = []
        if pid is not None:
            fields.append(f"pid={pid}")
        if started_at:
            fields.append(f"started_at={started_at}")
        return ", ".join(fields)

    def inherited_environment(self, base: Mapping[str, str]) -> dict[str, str]:
        env = dict(base)
        env[RUN_LOCK_FD_ENV] = str(self.fileno)
        env[RUN_LOCK_KEY_ENV] = self.key
        return env

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        os.close(fd)

    def __enter__(self) -> "ProjectRunLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
