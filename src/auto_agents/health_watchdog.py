from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

from .config import load_project_config, run_path
from .io_utils import read_json
from .process_supervision import process_identity_matches, process_start_ticks


WATCHDOG_SCHEMA_VERSION = 1
TERMINAL_HEALTH_STATUSES = {
    "completed",
    "paused",
    "waiting_user",
    "blocked",
    "failed",
    "stopped",
    "superseded",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(
        path.suffix + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def watchdog_control_path(project_root: Path, run_id: str) -> Path:
    return run_path(project_root, run_id) / "health" / "watchdog.json"


def watchdog_request_path(project_root: Path, run_id: str) -> Path:
    return run_path(project_root, run_id) / "health" / "watchdog-request.json"


def mark_watchdog_stop_intent(
    project_root: Path,
    run_id: str,
    *,
    reason: str,
) -> None:
    path = watchdog_control_path(project_root, run_id)
    payload = read_json(path, default={})
    control = dict(payload) if isinstance(payload, dict) else {}
    control.update(
        {
            "schema_version": WATCHDOG_SCHEMA_VERSION,
            "planned_stop": True,
            "planned_stop_reason": str(reason),
            "updated_at": _utc_now(),
        }
    )
    _atomic_json(path, control)


def mark_watchdog_supervisor_parent(
    project_root: Path,
    run_id: str,
    *,
    active: bool,
) -> None:
    path = watchdog_control_path(project_root, run_id)
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or not payload:
        return
    control = dict(payload)
    if active:
        control["supervisor_parent_pid"] = os.getpid()
        control["supervisor_parent_start_ticks"] = process_start_ticks(os.getpid())
    else:
        control["supervisor_parent_pid"] = 0
        control["supervisor_parent_start_ticks"] = 0
    control["updated_at"] = _utc_now()
    _atomic_json(path, control)


def watchdog_recovery_requested(project_root: Path, run_id: str) -> bool:
    payload = read_json(watchdog_request_path(project_root, run_id), default={})
    return bool(
        isinstance(payload, dict)
        and payload.get("action") == "restart"
        and not payload.get("handled", False)
    )


def start_run_watchdog(
    *,
    project_root: Path,
    run_id: str,
    run_token: str,
    restart_command: Sequence[str],
    auto_agents_entry: Path,
    autonomy_mode: str = "max",
) -> Optional[subprocess.Popen]:
    config = load_project_config(project_root).execution.health_watch
    if not config.enabled or not config.sidecar_enabled:
        return None
    path = watchdog_control_path(project_root, run_id)
    existing = read_json(path, default={})
    if isinstance(existing, dict):
        pid = int(existing.get("watchdog_pid", 0) or 0)
        ticks = int(existing.get("watchdog_start_ticks", 0) or 0)
        if (
            process_identity_matches(pid, ticks)
            and str(existing.get("run_token", "")) == str(run_token)
        ):
            return None
        if process_identity_matches(pid, ticks):
            _signal_verified(pid, ticks, signal.SIGTERM)
            _wait_for_exit(pid, ticks, 2.0)
    payload = {
        "schema_version": WATCHDOG_SCHEMA_VERSION,
        "project": str(project_root.expanduser().resolve()),
        "run_id": run_id,
        "run_token": run_token,
        "restart_command": [str(item) for item in restart_command],
        "restart_count": int(
            existing.get("restart_count", 0) if isinstance(existing, dict) else 0
        ),
        "autonomy_mode": str(autonomy_mode or "max"),
        "planned_stop": False,
        "created_at": _utc_now(),
        "created_epoch": time.time(),
        "updated_at": _utc_now(),
    }
    _atomic_json(path, payload)
    heartbeat_path = run_path(project_root, run_id) / "health" / "heartbeat.json"
    previous_heartbeat = read_json(heartbeat_path, default={})
    _atomic_json(
        heartbeat_path,
        {
            "schema_version": WATCHDOG_SCHEMA_VERSION,
            "run_id": run_id,
            "run_token": run_token,
            "owner_pid": os.getpid(),
            "owner_start_ticks": process_start_ticks(os.getpid()),
            "status": "starting",
            "previous_status": (
                str(previous_heartbeat.get("status", ""))
                if isinstance(previous_heartbeat, dict)
                else ""
            ),
            "reason": "watchdog bootstrap",
            "updated_at": _utc_now(),
            "updated_epoch": time.time(),
            "active_processes": [],
        },
    )
    process = subprocess.Popen(
        [
            os.fspath(Path(sys.executable).resolve()),
            os.fspath(auto_agents_entry.resolve()),
            "_watchdog",
            "--project",
            str(project_root.expanduser().resolve()),
            "--run-id",
            run_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    payload["watchdog_pid"] = process.pid
    payload["watchdog_start_ticks"] = process_start_ticks(process.pid)
    payload["updated_at"] = _utc_now()
    _atomic_json(path, payload)
    return process


def run_watchdog(project_root: Path, run_id: str) -> int:
    project = project_root.expanduser().resolve()
    config = load_project_config(project).execution.health_watch
    identity_supported = process_start_ticks(os.getpid()) > 0
    control_path = watchdog_control_path(project, run_id)
    heartbeat_path = run_path(project, run_id) / "health" / "heartbeat.json"
    request_path = watchdog_request_path(project, run_id)
    while True:
        control_payload = read_json(control_path, default={})
        if not isinstance(control_payload, dict):
            return 0
        if bool(control_payload.get("planned_stop", False)):
            return 0
        heartbeat = read_json(heartbeat_path, default={})
        if not isinstance(heartbeat, dict):
            heartbeat = {}
        status = str(heartbeat.get("status", ""))
        pending_request = read_json(request_path, default={})
        restart_pending = bool(
            isinstance(pending_request, dict)
            and pending_request.get("action") == "restart"
            and not pending_request.get("handled", False)
        )
        if status in TERMINAL_HEALTH_STATUSES and not restart_pending:
            return 0
        owner_pid = int(heartbeat.get("owner_pid", 0) or 0)
        owner_ticks = int(heartbeat.get("owner_start_ticks", 0) or 0)
        token_matches = str(heartbeat.get("run_token", "")) == str(
            control_payload.get("run_token", "")
        )
        owner_alive = bool(
            token_matches and process_identity_matches(owner_pid, owner_ticks)
        )
        supervisor_parent_alive = process_identity_matches(
            int(control_payload.get("supervisor_parent_pid", 0) or 0),
            int(control_payload.get("supervisor_parent_start_ticks", 0) or 0),
        )
        updated_epoch = float(heartbeat.get("updated_epoch", 0.0) or 0.0)
        if (
            not updated_epoch
            and time.time() - float(control_payload.get("created_epoch", 0.0) or 0.0)
            < config.heartbeat_timeout_seconds
        ):
            time.sleep(max(1.0, min(10.0, config.poll_seconds / 2.0)))
            continue
        stale = not updated_epoch or time.time() - updated_epoch >= config.heartbeat_timeout_seconds
        if not identity_supported:
            if stale:
                control_payload.update(
                    {
                        "status": "identity_probe_unsupported",
                        "updated_at": _utc_now(),
                    }
                )
                _atomic_json(control_path, control_payload)
                return 0
            time.sleep(max(1.0, min(10.0, config.poll_seconds / 2.0)))
            continue
        if owner_alive and not stale:
            time.sleep(max(1.0, min(10.0, config.poll_seconds / 2.0)))
            continue
        if not owner_alive and supervisor_parent_alive:
            control_payload.update(
                {
                    "status": "awaiting_supervisor_parent",
                    "updated_at": _utc_now(),
                }
            )
            _atomic_json(control_path, control_payload)
            time.sleep(max(1.0, min(10.0, config.poll_seconds / 2.0)))
            continue

        restart_count = int(control_payload.get("restart_count", 0) or 0)
        autonomy_mode = str(control_payload.get("autonomy_mode", "max") or "max")
        if autonomy_mode == "off":
            control_payload.update(
                {"status": "observation_only", "updated_at": _utc_now()}
            )
            _atomic_json(control_path, control_payload)
            return 0
        if restart_count >= config.max_sidecar_restarts_per_run:
            control_payload.update(
                {
                    "status": "restart_budget_exhausted",
                    "updated_at": _utc_now(),
                }
            )
            _atomic_json(control_path, control_payload)
            return 2
        _write_watchdog_diagnostic(
            project,
            run_id,
            reason=("owner_dead" if not owner_alive else "heartbeat_stale"),
            heartbeat=heartbeat,
            control=control_payload,
        )
        _atomic_json(
            request_path,
            {
                "schema_version": WATCHDOG_SCHEMA_VERSION,
                "action": "restart",
                "reason": "owner_dead" if not owner_alive else "heartbeat_stale",
                "owner_pid": owner_pid,
                "owner_start_ticks": owner_ticks,
                "requested_at": _utc_now(),
                "handled": False,
            },
        )
        if owner_alive and autonomy_mode != "max":
            control_payload.update(
                {"status": "stale_live_owner", "updated_at": _utc_now()}
            )
            _atomic_json(control_path, control_payload)
            time.sleep(max(1.0, min(10.0, config.poll_seconds / 2.0)))
            continue
        if owner_alive:
            deadline = time.monotonic() + config.sidecar_grace_seconds
            while time.monotonic() < deadline:
                refreshed = read_json(heartbeat_path, default={})
                if isinstance(refreshed, dict):
                    refreshed_epoch = float(refreshed.get("updated_epoch", 0.0) or 0.0)
                    if refreshed_epoch > updated_epoch:
                        try:
                            request_path.unlink()
                        except FileNotFoundError:
                            pass
                        break
                time.sleep(1.0)
            else:
                _signal_verified(owner_pid, owner_ticks, signal.SIGTERM)
                _wait_for_exit(owner_pid, owner_ticks, 10.0)
                if process_identity_matches(owner_pid, owner_ticks):
                    _signal_verified(owner_pid, owner_ticks, signal.SIGKILL)
                    _wait_for_exit(owner_pid, owner_ticks, 5.0)
            if process_identity_matches(owner_pid, owner_ticks):
                control_payload.update(
                    {"status": "owner_cleanup_incomplete", "updated_at": _utc_now()}
                )
                _atomic_json(control_path, control_payload)
                return 3
            if not request_path.exists():
                continue

        control_payload["restart_count"] = restart_count + 1
        control_payload["status"] = "restarting"
        control_payload["watchdog_pid"] = 0
        control_payload["watchdog_start_ticks"] = 0
        control_payload["updated_at"] = _utc_now()
        _atomic_json(control_path, control_payload)
        command = [str(item) for item in control_payload.get("restart_command", [])]
        if not command:
            return 4
        process = subprocess.Popen(
            command,
            cwd=str(project),
            close_fds=True,
            start_new_session=True,
        )
        request = read_json(request_path, default={})
        if isinstance(request, dict):
            request["handled"] = True
            request["restart_pid"] = process.pid
            request["handled_at"] = _utc_now()
            _atomic_json(request_path, request)
        return 0


def _write_watchdog_diagnostic(
    project_root: Path,
    run_id: str,
    *,
    reason: str,
    heartbeat: Dict[str, object],
    control: Dict[str, object],
) -> None:
    owner_pid = int(heartbeat.get("owner_pid", 0) or 0)
    proc_status = ""
    proc_stat = ""
    if owner_pid > 0:
        try:
            proc_status = Path(f"/proc/{owner_pid}/status").read_text(
                encoding="utf-8", errors="replace"
            )[:16_000]
        except OSError:
            pass
        try:
            proc_stat = Path(f"/proc/{owner_pid}/stat").read_text(
                encoding="utf-8", errors="replace"
            )[:4_000]
        except OSError:
            pass
    root = run_path(project_root, run_id) / "health" / "watchdog-diagnostics"
    _atomic_json(
        root / f"{int(time.time() * 1000)}.json",
        {
            "schema_version": WATCHDOG_SCHEMA_VERSION,
            "reason": reason,
            "captured_at": _utc_now(),
            "heartbeat": dict(heartbeat),
            "control": {
                key: value
                for key, value in control.items()
                if key != "restart_command"
            },
            "proc_status": proc_status,
            "proc_stat": proc_stat,
        },
    )


def _signal_verified(pid: int, start_ticks: int, signum: int) -> bool:
    if not process_identity_matches(pid, start_ticks):
        return False
    try:
        os.kill(pid, signum)
        return True
    except (OSError, PermissionError, ProcessLookupError):
        return False


def _wait_for_exit(pid: int, start_ticks: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not process_identity_matches(pid, start_ticks):
            return True
        time.sleep(0.1)
    return not process_identity_matches(pid, start_ticks)
