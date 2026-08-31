from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from .io_utils import read_json
from .process_supervision import process_identity_matches, process_start_ticks


CONTROL_SCHEMA_VERSION = 1
CONTROL_POLL_SECONDS = 1.0
CONTROL_ACK_TIMEOUT_SECONDS = 15.0
UNEXPECTED_EXIT_GRACE_SECONDS = 3.0
ACTIVE_PHASES = {
    "run",
    "fix",
    "collab",
    "triage",
    "self_repair",
    "resuming",
    "finalizing",
}
TERMINAL_PHASE = "terminal"


def utc_now() -> str:
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


def _project(project_root: Path) -> Path:
    return project_root.expanduser().resolve()


def control_path(project_root: Path) -> Path:
    return _project(project_root) / ".auto-agents" / "state" / "health-watch-control.json"


def _mutate_control(
    project_root: Path,
    update: Callable[[Dict[str, object]], Dict[str, object]],
) -> Dict[str, object]:
    path = control_path(project_root)
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = read_json(path, default={})
        payload = update(dict(current) if isinstance(current, dict) else {})
        _atomic_json(path, payload)
        return payload
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def subject_health_root(
    project_root: Path,
    workflow_kind: str,
    subject_id: str,
) -> Path:
    project = _project(project_root)
    if workflow_kind == "run":
        return project / ".auto-agents" / "runs" / subject_id / "health"
    return project / ".auto-agents" / "state" / "sessions" / subject_id / "health"


def action_store_path(project_root: Path, workflow_kind: str, subject_id: str) -> Path:
    return subject_health_root(project_root, workflow_kind, subject_id) / "actions.json"


def _valid_owner(payload: Dict[str, object]) -> bool:
    try:
        pid = int(payload.get("owner_pid", 0) or 0)
        ticks = int(payload.get("owner_start_ticks", 0) or 0)
    except (TypeError, ValueError):
        return False
    return pid > 0 and process_identity_matches(pid, ticks)


def load_active_manifest(project_root: Path, *, require_owner: bool = True) -> Dict[str, object]:
    payload = read_json(control_path(project_root), default={})
    manifest = dict(payload) if isinstance(payload, dict) else {}
    if int(manifest.get("schema_version", 0) or 0) != CONTROL_SCHEMA_VERSION:
        return {}
    if str(manifest.get("project", "")) != str(_project(project_root)):
        return {}
    if not str(manifest.get("run_token", "")).strip():
        return {}
    if require_owner and not _valid_owner(manifest):
        return {}
    return manifest


@dataclass(frozen=True)
class HealthActionRecord:
    request_id: str
    action: str
    reason: str
    source: str
    run_token: str
    subject_id: str
    observation_sequence: int = 0
    evidence_digest: str = ""
    dedupe_key: str = ""
    evidence: Optional[Dict[str, object]] = None
    state: str = "pending"
    created_at: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "request_id": self.request_id,
            "action": self.action,
            "reason": self.reason,
            "source": self.source,
            "run_token": self.run_token,
            "subject_id": self.subject_id,
            "observation_sequence": self.observation_sequence,
            "evidence_digest": self.evidence_digest,
            "dedupe_key": self.dedupe_key or self.evidence_digest,
            "evidence": dict(self.evidence or {}),
            "state": self.state,
            "created_at": self.created_at or utc_now(),
            "updated_at": utc_now(),
        }


class HealthActionStore:
    """Small durable mailbox between the independent auditor and the owner."""

    STATES = {"pending", "claimed", "completed", "rejected", "superseded"}

    def __init__(self, project_root: Path, workflow_kind: str, subject_id: str) -> None:
        self.path = action_store_path(project_root, workflow_kind, subject_id)
        self.lock_path = self.path.with_suffix(".lock")
        self._lock = threading.Lock()

    def _acquire_file_lock(self) -> int:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    @staticmethod
    def _release_file_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load(self) -> Dict[str, object]:
        payload = read_json(self.path, default={})
        if not isinstance(payload, dict):
            payload = {}
        requests = payload.get("requests", [])
        if not isinstance(requests, list):
            requests = []
        return {"schema_version": 1, "requests": requests}

    def append(self, record: HealthActionRecord) -> Dict[str, object]:
        with self._lock:
            fd = self._acquire_file_lock()
            try:
                payload = self._load()
                requests = list(payload["requests"])
                identity = (
                    record.action,
                    record.reason,
                    record.run_token,
                    record.subject_id,
                    record.dedupe_key or record.evidence_digest,
                )
                for existing in requests:
                    if not isinstance(existing, dict):
                        continue
                    existing_identity = (
                        str(existing.get("action", "")),
                        str(existing.get("reason", "")),
                        str(existing.get("run_token", "")),
                        str(existing.get("subject_id", "")),
                        str(existing.get("dedupe_key", ""))
                        or str(existing.get("evidence_digest", "")),
                    )
                    if existing_identity == identity and str(existing.get("state", "")) in {
                        "pending",
                        "claimed",
                    }:
                        return dict(existing)
                item = record.to_dict()
                requests.append(item)
                active = [
                    item
                    for item in requests
                    if isinstance(item, dict)
                    and str(item.get("state", "")) in {"pending", "claimed"}
                ]
                terminal = [item for item in requests if item not in active]
                payload["requests"] = terminal[-200:] + active
                _atomic_json(self.path, payload)
                return item
            finally:
                self._release_file_lock(fd)

    def next_pending(self, *, run_token: str) -> Optional[Dict[str, object]]:
        payload = self._load()
        for item in payload["requests"]:
            if (
                isinstance(item, dict)
                and str(item.get("state", "")) == "pending"
                and str(item.get("run_token", "")) == run_token
            ):
                return dict(item)
        return None

    def transition(self, request_id: str, state: str, *, detail: str = "") -> bool:
        if state not in self.STATES:
            raise ValueError(f"unsupported health action state: {state}")
        with self._lock:
            fd = self._acquire_file_lock()
            try:
                payload = self._load()
                changed = False
                for item in payload["requests"]:
                    if isinstance(item, dict) and str(item.get("request_id", "")) == request_id:
                        item["state"] = state
                        item["updated_at"] = utc_now()
                        if detail:
                            item["detail"] = detail[:2000]
                        changed = True
                        break
                if changed:
                    _atomic_json(self.path, payload)
                return changed
            finally:
                self._release_file_lock(fd)


class HealthControlChannel:
    """Always-on, low-cost runtime control plane for one foreground workflow."""

    def __init__(
        self,
        project_root: Path,
        *,
        workflow_kind: str,
        run_token: str,
        enabled: bool,
        on_enable: Callable[[Dict[str, object]], None],
        on_disable: Callable[[Dict[str, object]], None],
    ) -> None:
        self.project_root = _project(project_root)
        self.workflow_kind = workflow_kind
        self.run_token = run_token
        self.on_enable = on_enable
        self.on_disable = on_disable
        self._enabled = bool(enabled)
        self._subject_id = ""
        self._phase = workflow_kind
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._generation = 1

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def subject_id(self) -> str:
        return self._subject_id

    @property
    def process_phase(self) -> str:
        return self._phase

    def start(self, subject_id: str = "") -> None:
        self._subject_id = str(subject_id)
        self._publish(initial=True)
        self._thread = threading.Thread(
            target=self._run,
            name=f"auto-agents-health-control-{self.workflow_kind}",
            daemon=True,
        )
        self._thread.start()

    def bind_subject(self, subject_id: str) -> None:
        normalized = str(subject_id).strip()
        if normalized and normalized != self._subject_id:
            self._subject_id = normalized
            self._publish()

    def set_phase(self, phase: str) -> None:
        normalized = str(phase).strip()
        if normalized and normalized != self._phase:
            self._phase = normalized
            self._publish()

    def set_sidecar(self, pid: int, start_ticks: int) -> None:
        def update(payload: Dict[str, object]) -> Dict[str, object]:
            if str(payload.get("run_token", "")) != self.run_token:
                return payload
            payload.update(
                sidecar_pid=max(0, int(pid)),
                sidecar_start_ticks=max(0, int(start_ticks)),
                updated_at=utc_now(),
                updated_epoch=time.time(),
            )
            return payload

        _mutate_control(self.project_root, update)

    def close(self, *, reason: str = "") -> None:
        self._phase = TERMINAL_PHASE
        self._publish(terminal_reason=reason)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _current(self) -> Dict[str, object]:
        payload = read_json(control_path(self.project_root), default={})
        if not isinstance(payload, dict):
            return {}
        if str(payload.get("run_token", "")) != self.run_token:
            return {}
        return dict(payload)

    def _publish(self, *, initial: bool = False, terminal_reason: str = "") -> None:
        def update(existing: Dict[str, object]) -> Dict[str, object]:
            prior = (
                existing
                if str(existing.get("run_token", "")) == self.run_token
                else {}
            )
            return self._build_manifest(
                prior, initial=initial, terminal_reason=terminal_reason
            )

        _mutate_control(self.project_root, update)

    def _build_manifest(
        self,
        prior: Dict[str, object],
        *,
        initial: bool,
        terminal_reason: str,
    ) -> Dict[str, object]:
        pending_command = bool(
            prior
            and int(prior.get("generation", 0) or 0)
            > int(prior.get("applied_generation", 0) or 0)
        )
        desired = (
            str(prior.get("desired_state", ""))
            if pending_command
            else "enabled" if self._enabled else "disabled"
        )
        generation = int(prior.get("generation", self._generation) or self._generation)
        if initial:
            generation = max(1, generation)
        self._generation = generation
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "project": str(self.project_root),
            "workflow_kind": self.workflow_kind,
            "subject_id": self._subject_id,
            "run_token": self.run_token,
            "owner_pid": os.getpid(),
            "owner_start_ticks": process_start_ticks(os.getpid()),
            "process_phase": self._phase,
            "desired_state": desired,
            "applied_state": "enabled" if self._enabled else "disabled",
            "generation": generation,
            "applied_generation": (
                int(prior.get("applied_generation", 0) or 0)
                if pending_command
                else generation
            ),
            "created_at": str(prior.get("created_at", "")) or utc_now(),
            "updated_at": utc_now(),
            "updated_epoch": time.time(),
            "terminal_reason": terminal_reason,
            "sidecar_pid": int(prior.get("sidecar_pid", 0) or 0),
            "sidecar_start_ticks": int(prior.get("sidecar_start_ticks", 0) or 0),
        }

    def _run(self) -> None:
        while not self._stop.wait(CONTROL_POLL_SECONDS):
            payload = self._current()
            if not payload:
                continue
            desired = str(payload.get("desired_state", "enabled")) == "enabled"
            generation = int(payload.get("generation", 0) or 0)
            applied = int(payload.get("applied_generation", 0) or 0)
            if generation <= applied and desired == self._enabled:
                continue
            try:
                if desired:
                    self.on_enable(payload)
                else:
                    self.on_disable(payload)
            except Exception as error:
                def record_error(current: Dict[str, object]) -> Dict[str, object]:
                    if str(current.get("run_token", "")) != self.run_token:
                        return current
                    current.update(
                        apply_error=str(error)[:1000],
                        updated_at=utc_now(),
                        updated_epoch=time.time(),
                    )
                    return current

                _mutate_control(self.project_root, record_error)
                continue
            self._enabled = desired
            def acknowledge(current: Dict[str, object]) -> Dict[str, object]:
                if str(current.get("run_token", "")) != self.run_token:
                    return current
                if int(current.get("generation", 0) or 0) < generation:
                    return current
                current.update(
                    applied_state="enabled" if desired else "disabled",
                    applied_generation=generation,
                    apply_error="",
                    updated_at=utc_now(),
                    updated_epoch=time.time(),
                )
                return current

            _mutate_control(self.project_root, acknowledge)


def request_health_state(
    project_root: Path,
    *,
    enabled: bool,
    timeout_seconds: float = CONTROL_ACK_TIMEOUT_SECONDS,
) -> Dict[str, object]:
    payload = load_active_manifest(project_root)
    if not payload:
        raise RuntimeError(f"no active auto_agents workflow for {_project(project_root)}")
    generation = int(payload.get("generation", 0) or 0) + 1
    token = str(payload.get("run_token", ""))
    def request(current: Dict[str, object]) -> Dict[str, object]:
        if str(current.get("run_token", "")) != token:
            raise RuntimeError("active workflow changed while requesting health-watch state")
        current.update(
            desired_state="enabled" if enabled else "disabled",
            generation=generation,
            command_requested_at=utc_now(),
            updated_at=utc_now(),
            updated_epoch=time.time(),
        )
        return current

    _mutate_control(project_root, request)
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        current = load_active_manifest(project_root)
        if not current:
            raise RuntimeError("active workflow exited before applying the health-watch command")
        if (
            int(current.get("applied_generation", 0) or 0) >= generation
            and str(current.get("applied_state", ""))
            == ("enabled" if enabled else "disabled")
        ):
            return {"ok": True, **current}
        error = str(current.get("apply_error", "")).strip()
        if error:
            raise RuntimeError(f"health-watch command was not applied: {error}")
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for the active workflow to apply health-watch state")


def health_watch_status(project_root: Path) -> Dict[str, object]:
    manifest = load_active_manifest(project_root)
    if not manifest:
        stale = read_json(control_path(project_root), default={})
        return {
            "ok": True,
            "active": False,
            "project": str(_project(project_root)),
            "last_manifest": stale if isinstance(stale, dict) else {},
        }
    return {"ok": True, "active": True, **manifest}


def evidence_digest(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()
