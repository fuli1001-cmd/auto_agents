from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional


DEFAULT_TERMINATE_GRACE_SECONDS = 5.0
DEFAULT_KILL_GRACE_SECONDS = 5.0
DEFAULT_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
PROCESS_CONTROL_VERSION = 1


class RunInterruptedError(BaseException):
    """Control-flow exception raised when a run receives SIGINT or SIGTERM.

    Inheriting from ``BaseException`` keeps shutdown requests out of broad
    application-error and self-repair handlers, matching ``KeyboardInterrupt``.
    """

    def __init__(self, signum: int) -> None:
        self.signum = int(signum)
        try:
            label = signal.Signals(signum).name
        except ValueError:
            label = str(signum)
        super().__init__(f"run interrupted by {label}")

    @property
    def exit_code(self) -> int:
        return 128 + self.signum


@dataclass
class ManagedProcessRecord:
    pid: int
    pgid: int
    start_ticks: int
    kind: str
    started_at: str
    heartbeat_at: str


@dataclass
class ProcessTerminationResult:
    returncode: Optional[int]
    cleanup_incomplete: bool
    term_sent: bool
    kill_sent: bool


@dataclass
class SupervisedCommandResult:
    stdout: str
    stderr: str
    returncode: int
    duration_seconds: float
    termination_reason: str = ""
    timeout_seconds: float = 0.0
    cleanup_incomplete: bool = False
    last_activity_seconds: float = 0.0
    activity_kind: str = ""
    process_snapshot: Dict[str, object] = field(default_factory=dict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_start_ticks(pid: int) -> int:
    """Return Linux /proc start ticks, or zero when the process is unavailable."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        # The command name is parenthesized and may contain spaces.  Fields after
        # the final ')' start at proc field 3; starttime is field 22.
        remainder = raw.rsplit(")", 1)[1].strip().split()
        return int(remainder[19])
    except (OSError, ValueError, IndexError):
        return 0


def process_identity_matches(pid: int, start_ticks: int) -> bool:
    return bool(pid > 0 and start_ticks > 0 and process_start_ticks(pid) == start_ticks)


def process_group_exists(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_group_exit(process: subprocess.Popen, pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        process.poll()
        if not process_group_exists(pgid):
            return True
        time.sleep(0.05)
    process.poll()
    return not process_group_exists(pgid)


def terminate_process_group(
    process: subprocess.Popen,
    *,
    pgid: Optional[int] = None,
    terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> ProcessTerminationResult:
    """Terminate a process group without ever performing an unbounded wait."""
    target_pgid = int(pgid or process.pid)
    term_sent = False
    kill_sent = False
    if process_group_exists(target_pgid):
        try:
            os.killpg(target_pgid, signal.SIGTERM)
            term_sent = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if not _wait_for_group_exit(process, target_pgid, terminate_grace_seconds):
        try:
            os.killpg(target_pgid, signal.SIGKILL)
            kill_sent = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
        _wait_for_group_exit(process, target_pgid, kill_grace_seconds)
    return ProcessTerminationResult(
        returncode=process.poll(),
        cleanup_incomplete=process_group_exists(target_pgid),
        term_sent=term_sent,
        kill_sent=kill_sent,
    )


class ActiveProcessRegistry:
    """Thread-safe process-group registry with an optional on-disk control file."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: Dict[int, ManagedProcessRecord] = {}
        self._control_path: Optional[Path] = None
        self._project = ""
        self._run_token = ""

    def configure(self, project: Path, run_token: str, control_path: Path) -> None:
        with self._lock:
            self._project = str(project.expanduser().resolve())
            self._run_token = str(run_token)
            self._control_path = control_path
            self._write_locked()

    def clear_configuration(self, *, remove_if_empty: bool = True) -> None:
        with self._lock:
            if remove_if_empty and not self._records and self._control_path is not None:
                try:
                    self._control_path.unlink()
                except FileNotFoundError:
                    pass
            self._control_path = None
            self._project = ""
            self._run_token = ""

    def register(self, process: subprocess.Popen, *, kind: str) -> ManagedProcessRecord:
        pid = int(process.pid)
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            # All supervised processes are launched with start_new_session=True,
            # so their initial process-group ID is their PID. Very short-lived
            # commands can exit between Popen returning and this lookup.
            pgid = pid
        record = ManagedProcessRecord(
            pid=pid,
            pgid=pgid,
            start_ticks=process_start_ticks(pid),
            kind=str(kind or "subprocess"),
            started_at=_utc_now(),
            heartbeat_at=_utc_now(),
        )
        with self._lock:
            self._records[pid] = record
            self._write_locked()
        return record

    def heartbeat(self, pid: int) -> None:
        with self._lock:
            record = self._records.get(int(pid))
            if record is None:
                return
            record.heartbeat_at = _utc_now()
            self._write_locked()

    def unregister(self, pid: int, *, preserve_if_alive: bool = False) -> None:
        with self._lock:
            record = self._records.get(int(pid))
            if record is None:
                return
            if preserve_if_alive and process_group_exists(record.pgid):
                record.heartbeat_at = _utc_now()
            else:
                self._records.pop(int(pid), None)
            self._write_locked()

    def terminate_all(self) -> None:
        with self._lock:
            records = list(self._records.values())
        for record in records:
            if not process_identity_matches(record.pid, record.start_ticks):
                continue
            try:
                os.killpg(record.pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def snapshot(self) -> list[ManagedProcessRecord]:
        with self._lock:
            return list(self._records.values())

    def _write_locked(self) -> None:
        if self._control_path is None or not self._run_token:
            return
        payload = {
            "version": PROCESS_CONTROL_VERSION,
            "project": self._project,
            "run_token": self._run_token,
            "updated_at": _utc_now(),
            "processes": [asdict(item) for item in self._records.values()],
        }
        self._control_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self._control_path.name}.", dir=str(self._control_path.parent)
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._control_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


ACTIVE_PROCESSES = ActiveProcessRegistry()


def _bounded_output(file_obj, limit: int = DEFAULT_OUTPUT_LIMIT_BYTES) -> str:
    try:
        size = os.fstat(file_obj.fileno()).st_size
        offset = max(0, size - limit)
        raw = os.pread(file_obj.fileno(), min(size, limit), offset)
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if offset:
        text = f"[output truncated to last {limit} bytes]\n{text}"
    return text


def _process_group_snapshot(pgid: int) -> Dict[str, object]:
    """Return a cheap activity snapshot for every visible member of *pgid*."""
    members = []
    cpu_ticks = 0
    try:
        proc_entries = Path("/proc").iterdir()
    except OSError:
        proc_entries = []
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            fields = raw.rsplit(")", 1)[1].strip().split()
            # fields starts at proc field 3: state, ppid, pgrp, ... utime, stime.
            if int(fields[2]) != pgid:
                continue
            ticks = int(fields[11]) + int(fields[12])
            cpu_ticks += ticks
            members.append({"pid": int(entry.name), "state": fields[0], "cpu_ticks": ticks})
        except (OSError, ValueError, IndexError):
            continue
    members.sort(key=lambda item: int(item["pid"]))
    return {"pgid": pgid, "cpu_ticks": cpu_ticks, "members": members}


def run_supervised_shell_command(
    command: str,
    *,
    cwd: Path,
    env: Optional[Mapping[str, str]] = None,
    timeout_seconds: float,
    adaptive_timeout_enabled: bool = False,
    idle_timeout_seconds: float = 0.0,
    kind: str = "gate",
    cancel_event: Optional[threading.Event] = None,
    heartbeat_seconds: float = 60.0,
    progress: Optional[Callable[[str, float], None]] = None,
    on_start: Optional[Callable[[int, int], None]] = None,
    diagnostic_output: object = None,
) -> SupervisedCommandResult:
    """Run one shell command with an absolute ceiling and optional activity lease."""
    started = time.monotonic()
    if diagnostic_output is None:
        from .reporting import find_reporter
        reporter = find_reporter(cwd)
        if reporter is not None:
            diagnostic_output = reporter.capture(kind=kind, cwd=str(cwd))
    if diagnostic_output is not None:
        try:
            diagnostic_output.start(command, env or {}, cwd=str(cwd), capture_mode="file",
                                    timeout_seconds=timeout_seconds)
        except Exception:
            pass

    def capture_files(stdout_file, stderr_file, **metadata: object) -> None:
        if diagnostic_output is not None:
            try:
                diagnostic_output.file("stdout", stdout_file)
                diagnostic_output.file("stderr", stderr_file)
                diagnostic_output.finish(**metadata)
            except Exception:
                pass
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, \
            tempfile.TemporaryFile(mode="w+b") as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=str(cwd),
                env=dict(env) if env is not None else None,
                start_new_session=True,
            )
        except OSError as error:
            capture_files(stdout_file, stderr_file, status="launch_error", error=str(error), returncode=127,
                          traceback=traceback.format_exc())
            return SupervisedCommandResult(
                stdout="",
                stderr=str(error),
                returncode=127,
                duration_seconds=time.monotonic() - started,
                termination_reason="launch_error",
                timeout_seconds=float(timeout_seconds),
            )

        pgid = process.pid
        termination_reason = ""
        cleanup_incomplete = False
        post_exit_cleanup: Dict[str, object] = {}
        try:
            record = ACTIVE_PROCESSES.register(process, kind=kind)
            pgid = record.pgid
            if on_start is not None:
                on_start(process.pid, pgid)
            deadline = started + max(0.001, float(timeout_seconds))
            idle_budget = max(0.001, float(idle_timeout_seconds or timeout_seconds))
            last_activity = started
            activity_kind = "started"
            last_output_sizes = (0, 0)
            process_snapshot = _process_group_snapshot(pgid)
            last_cpu_ticks = int(process_snapshot.get("cpu_ticks", 0))
            next_heartbeat = started + max(0.1, heartbeat_seconds)
            next_activity_probe = started
            while process.poll() is None:
                now = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    termination_reason = "cancelled"
                    break
                if now >= deadline:
                    termination_reason = "timeout"
                    break
                if now >= next_activity_probe:
                    try:
                        output_sizes = (
                            os.fstat(stdout_file.fileno()).st_size,
                            os.fstat(stderr_file.fileno()).st_size,
                        )
                    except OSError:
                        output_sizes = last_output_sizes
                    snapshot = _process_group_snapshot(record.pgid)
                    cpu_ticks = int(snapshot.get("cpu_ticks", 0))
                    if output_sizes != last_output_sizes:
                        last_activity = now
                        activity_kind = "output"
                    elif cpu_ticks != last_cpu_ticks:
                        last_activity = now
                        activity_kind = "cpu"
                    last_output_sizes = output_sizes
                    last_cpu_ticks = cpu_ticks
                    process_snapshot = snapshot
                    next_activity_probe = now + min(0.5, max(0.05, idle_budget / 4.0))
                if adaptive_timeout_enabled and now - last_activity >= idle_budget:
                    termination_reason = "stalled"
                    break
                if now >= next_heartbeat:
                    ACTIVE_PROCESSES.heartbeat(process.pid)
                    if progress is not None:
                        progress("heartbeat", now - started)
                    next_heartbeat = now + max(0.1, heartbeat_seconds)
                time.sleep(min(0.1, max(0.0, deadline - now)))
            if termination_reason:
                if cancel_event is not None:
                    cancel_event.set()
                terminated = terminate_process_group(process, pgid=record.pgid)
                cleanup_incomplete = terminated.cleanup_incomplete
            returncode = process.poll()
            if returncode is None:
                returncode = 124 if termination_reason in {"timeout", "stalled"} else 130
            elif not termination_reason and process_group_exists(record.pgid):
                residual_snapshot = _process_group_snapshot(record.pgid)
                terminated = terminate_process_group(process, pgid=record.pgid)
                cleanup_incomplete = terminated.cleanup_incomplete
                post_exit_cleanup = {
                    "required": True,
                    "term_sent": terminated.term_sent,
                    "kill_sent": terminated.kill_sent,
                    "cleanup_incomplete": cleanup_incomplete,
                    "residual_members": residual_snapshot.get("members", []),
                }
                process_snapshot = {
                    **residual_snapshot,
                    "post_exit_cleanup": post_exit_cleanup,
                }
        except BaseException as error:
            terminated = terminate_process_group(process, pgid=pgid)
            cleanup_incomplete = terminated.cleanup_incomplete
            capture_files(stdout_file, stderr_file, status="interrupted", error=str(error),
                          traceback=traceback.format_exc())
            raise
        finally:
            ACTIVE_PROCESSES.unregister(
                process.pid,
                preserve_if_alive=cleanup_incomplete,
            )

        capture_files(stdout_file, stderr_file, returncode=int(returncode),
                      termination_reason=termination_reason, cleanup_incomplete=cleanup_incomplete)
        stdout = _bounded_output(stdout_file).strip()
        stderr = _bounded_output(stderr_file).strip()
        elapsed = time.monotonic() - started
        if termination_reason in {"timeout", "stalled"}:
            diagnostic = (
                f"command timed out after {float(timeout_seconds):g}s"
                if termination_reason == "timeout"
                else f"command stalled with no observed activity for {idle_budget:g}s"
            )
            if cleanup_incomplete:
                diagnostic += "; process group cleanup is incomplete"
            stderr = f"{stderr}\n{diagnostic}".strip()
        elif termination_reason == "cancelled":
            diagnostic = "command cancelled by run shutdown"
            if cleanup_incomplete:
                diagnostic += "; process group cleanup is incomplete"
            stderr = f"{stderr}\n{diagnostic}".strip()
        elif post_exit_cleanup and cleanup_incomplete:
            stderr = (
                f"{stderr}\ncommand exited but residual process group cleanup is incomplete"
            ).strip()
        return SupervisedCommandResult(
            stdout=stdout,
            stderr=stderr,
            returncode=int(returncode),
            duration_seconds=elapsed,
            termination_reason=termination_reason,
            timeout_seconds=float(timeout_seconds),
            cleanup_incomplete=cleanup_incomplete,
            last_activity_seconds=max(0.0, last_activity - started),
            activity_kind=activity_kind,
            process_snapshot=process_snapshot,
        )


def read_process_control(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
