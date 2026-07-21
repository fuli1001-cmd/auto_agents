from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from .process_supervision import (
    ACTIVE_PROCESSES,
    process_group_exists,
    process_identity_matches,
    process_start_ticks,
    read_process_control,
)


RUN_LOCK_FD_ENV = "AUTO_AGENTS_RUN_LOCK_FD"
RUN_LOCK_KEY_ENV = "AUTO_AGENTS_RUN_LOCK_KEY"
RUN_LOCK_TOKEN_ENV = "AUTO_AGENTS_RUN_TOKEN"


class RunAlreadyActiveError(RuntimeError):
    """Raised when another process already owns the target project's run lock."""


class ProjectRunLock:
    """Process lock for a target project, with explicit self-repair handoff support."""

    def __init__(self, project_root: Path, *, environ: Optional[Mapping[str, str]] = None) -> None:
        self.project_root = project_root.expanduser().resolve()
        self._environ = os.environ if environ is None else environ
        self.key = hashlib.sha256(str(self.project_root).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / "auto-agents-run-locks" / f"{self.key}.lock"
        self.control_path = self.path.with_suffix(".processes.json")
        self._fd: Optional[int] = None
        self.run_token = str(self._environ.get(RUN_LOCK_TOKEN_ENV, "")).strip() or uuid.uuid4().hex

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
            self._write_owner(inherited_fd)
            ACTIVE_PROCESSES.configure(self.project_root, self.run_token, self.control_path)
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

        self._fd = fd
        orphaned = _live_control_processes(self.control_path, expected_project=str(self.project_root))
        if orphaned:
            self.release()
            details = ", ".join(f"pid={item['pid']} pgid={item['pgid']}" for item in orphaned)
            raise RunAlreadyActiveError(
                f"orphaned auto_agents subprocesses are still active for {self.project_root} "
                f"({details}); run `python auto_agents.py stop --project {self.project_root}`"
            )
        self._write_owner(fd)
        ACTIVE_PROCESSES.configure(self.project_root, self.run_token, self.control_path)
        return self

    def _write_owner(self, fd: int) -> None:
        payload = {
            "version": 2,
            "pid": os.getpid(),
            "pid_start_ticks": process_start_ticks(os.getpid()),
            "project": str(self.project_root),
            "run_token": self.run_token,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, encoded)
        os.fsync(fd)

    def _inherited_fd(self) -> Optional[int]:
        if self._environ.get(RUN_LOCK_KEY_ENV) != self.key:
            return None
        inherited_token = str(self._environ.get(RUN_LOCK_TOKEN_ENV, "")).strip()
        if not inherited_token or inherited_token != self.run_token:
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
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            payload = json.loads(os.read(fd, 4096).decode("utf-8", errors="replace"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or str(payload.get("run_token", "")) != inherited_token:
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

    def owner_payload(self) -> dict[str, object]:
        return _read_json_path(self.path)

    def inherited_environment(self, base: Mapping[str, str]) -> dict[str, str]:
        env = dict(base)
        env[RUN_LOCK_FD_ENV] = str(self.fileno)
        env[RUN_LOCK_KEY_ENV] = self.key
        env[RUN_LOCK_TOKEN_ENV] = self.run_token
        return env

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        ACTIVE_PROCESSES.clear_configuration(remove_if_empty=True)
        os.close(fd)

    def __enter__(self) -> "ProjectRunLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def _read_json_path(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _lock_is_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _same_user_process(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}").stat().st_uid == os.getuid()
    except OSError:
        return False


def _live_control_processes(
    path: Path,
    *,
    expected_project: str,
    expected_token: str = "",
) -> list[dict[str, int]]:
    payload = read_process_control(path)
    if str(payload.get("project", "")) != expected_project:
        return []
    if expected_token and str(payload.get("run_token", "")) != expected_token:
        return []
    live: list[dict[str, int]] = []
    for item in payload.get("processes", []):
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid", 0))
            pgid = int(item.get("pgid", 0))
            start_ticks = int(item.get("start_ticks", 0))
        except (TypeError, ValueError):
            continue
        if (
            process_identity_matches(pid, start_ticks)
            and _same_user_process(pid)
            and process_group_exists(pgid)
        ):
            live.append({"pid": pid, "pgid": pgid, "start_ticks": start_ticks})
    return live


def runtime_status(project_root: Path) -> dict[str, object]:
    lock = ProjectRunLock(project_root, environ={})
    owner = lock.owner_payload()
    active = _lock_is_held(lock.path)
    token = str(owner.get("run_token", "")) if active else ""
    owner_pid = int(owner.get("pid", 0) or 0)
    owner_ticks = int(owner.get("pid_start_ticks", 0) or 0)
    owner_valid = bool(
        active
        and str(owner.get("project", "")) == str(lock.project_root)
        and process_identity_matches(owner_pid, owner_ticks)
        and _same_user_process(owner_pid)
    )
    processes = _live_control_processes(
        lock.control_path,
        expected_project=str(lock.project_root),
        expected_token=token,
    )
    control = read_process_control(lock.control_path)
    return {
        "active": bool(active and owner_valid),
        "owner_pid": owner_pid if owner_valid else 0,
        "owner_identity_valid": owner_valid,
        "active_process_groups": len(processes),
        "last_heartbeat_at": str(control.get("updated_at", "")),
        "cleanup_incomplete": bool(processes and not active),
    }


def _signal_owner(owner: dict[str, object], signum: int, project: str) -> bool:
    try:
        pid = int(owner.get("pid", 0))
        ticks = int(owner.get("pid_start_ticks", 0))
    except (TypeError, ValueError):
        return False
    if str(owner.get("project", "")) != project:
        return False
    if not process_identity_matches(pid, ticks) or not _same_user_process(pid):
        return False
    try:
        os.kill(pid, signum)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _signal_groups(processes: list[dict[str, int]], signum: int) -> list[int]:
    signaled: list[int] = []
    for item in processes:
        pid, pgid, ticks = item["pid"], item["pgid"], item["start_ticks"]
        if not process_identity_matches(pid, ticks) or not _same_user_process(pid):
            continue
        try:
            if os.getpgid(pid) != pgid:
                continue
            os.killpg(pgid, signum)
            signaled.append(pgid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return signaled


def _signal_trusted_process_groups(pgids: list[int], signum: int) -> list[int]:
    """Signal groups whose leaders were identity-validated earlier in this stop."""
    signaled: list[int] = []
    for pgid in sorted(set(pgids)):
        if not process_group_exists(pgid):
            continue
        try:
            os.killpg(pgid, signum)
            signaled.append(pgid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return signaled


def stop_project_run(
    project_root: Path,
    *,
    grace_seconds: float = 10.0,
    kill_grace_seconds: float = 5.0,
) -> tuple[dict[str, object], int]:
    lock = ProjectRunLock(project_root, environ={})
    project = str(lock.project_root)
    owner = lock.owner_payload()
    active = _lock_is_held(lock.path)
    token = str(owner.get("run_token", "")) if active else ""
    processes = _live_control_processes(
        lock.control_path,
        expected_project=project,
        expected_token=token,
    )
    if not active and not processes:
        try:
            lock.control_path.unlink()
        except FileNotFoundError:
            pass
        return {"ok": True, "status": "not_running", "forced": False}, 0

    signaled_groups = _signal_groups(processes, signal.SIGTERM)
    trusted_pgids = sorted(set(signaled_groups))
    owner_signaled = _signal_owner(owner, signal.SIGTERM, project) if active else False
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not _lock_is_held(lock.path) and not any(
            process_group_exists(pgid) for pgid in trusted_pgids
        ):
            try:
                lock.control_path.unlink()
            except FileNotFoundError:
                pass
            return {
                "ok": True,
                "status": "stopped",
                "forced": False,
                "owner_pid": int(owner.get("pid", 0) or 0),
                "terminated_process_groups": sorted(set(signaled_groups)),
            }, 0
        time.sleep(0.1)

    remaining_pgids = [pgid for pgid in trusted_pgids if process_group_exists(pgid)]
    killed_groups = _signal_trusted_process_groups(remaining_pgids, signal.SIGKILL)
    if active:
        _signal_owner(owner, signal.SIGKILL, project)
    deadline = time.monotonic() + max(0.0, kill_grace_seconds)
    while time.monotonic() < deadline:
        remaining_pgids = [
            pgid for pgid in remaining_pgids if process_group_exists(pgid)
        ]
        if not _lock_is_held(lock.path) and not remaining_pgids:
            try:
                lock.control_path.unlink()
            except FileNotFoundError:
                pass
            return {
                "ok": True,
                "status": "stopped",
                "forced": True,
                "owner_pid": int(owner.get("pid", 0) or 0),
                "terminated_process_groups": sorted(set(signaled_groups + killed_groups)),
            }, 0
        time.sleep(0.1)

    remaining_pgids = [
        pgid for pgid in remaining_pgids if process_group_exists(pgid)
    ]
    return {
        "ok": False,
        "status": "stop_incomplete",
        "forced": True,
        "owner_pid": int(owner.get("pid", 0) or 0),
        "owner_signaled": owner_signaled,
        "remaining_process_groups": remaining_pgids,
        "error": "validated processes remain alive, possibly in uninterruptible kernel I/O",
    }, 1
