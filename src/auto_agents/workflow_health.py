from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

from .config import load_run_state
from .health_control import (
    HealthActionStore,
    HealthControlChannel,
    evidence_digest,
    subject_health_root,
    utc_now,
    _atomic_json,
)
from .health_watchdog import start_health_sidecar
from .io_utils import read_json
from .process_supervision import process_start_ticks


logger = logging.getLogger(__name__)


class WorkflowHealthRuntime:
    """Coordinator/facade for telemetry, control, sidecar, and action intake."""

    def __init__(
        self,
        project_root: Path,
        *,
        workflow_kind: str,
        run_token: str,
        enabled: bool,
        auto_agents_entry: Path,
        orchestrator: object = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.workflow_kind = workflow_kind
        self.run_token = str(run_token)
        self.auto_agents_entry = auto_agents_entry
        self.orchestrator = orchestrator
        self._subject_id = ""
        self._enabling = False
        self._available = True
        self.channel = HealthControlChannel(
            self.project_root,
            workflow_kind=workflow_kind,
            run_token=self.run_token,
            enabled=enabled,
            on_enable=self._enable,
            on_disable=self._disable,
        )

    @property
    def enabled(self) -> bool:
        return self.channel.enabled

    def start(self, subject_id: str = "") -> None:
        self._subject_id = str(subject_id).strip()
        try:
            self.channel.start(self._subject_id)
            if self.channel.enabled and self._subject_id:
                self._enable({})
        except Exception as error:
            self._available = False
            logger.warning("health control channel could not start: %s", error)

    def bind_subject(self, subject_id: str) -> None:
        normalized = str(subject_id).strip()
        if not normalized or not self._available:
            return
        changed = normalized != self._subject_id
        self._subject_id = normalized
        try:
            self.channel.bind_subject(normalized)
            self._publish_phase(self.channel.process_phase)
            if changed and self.channel.enabled and not self._enabling:
                self._enable({})
        except Exception as error:
            logger.warning("health subject/sidecar could not be bound: %s", error)

    def set_phase(self, phase: str) -> None:
        if not self._available:
            return
        try:
            self.channel.set_phase(phase)
            self._publish_phase(phase)
        except Exception as error:
            logger.warning("health process phase could not be published: %s", error)

    def close(self, *, reason: str = "") -> None:
        try:
            if self.orchestrator is not None:
                stop = getattr(self.orchestrator, "stop_health_supervision", None)
                if callable(stop):
                    stop(status="stopped", reason=reason)
            if self._available:
                self.channel.close(reason=reason)
                self._publish_phase("terminal", reason=reason)
        except Exception as error:
            logger.warning("health runtime could not close cleanly: %s", error)

    def publish_session(self, state: object) -> None:
        subject_id = str(getattr(state, "session_id", ""))
        if not subject_id:
            return
        self.bind_subject(subject_id)
        if not self._available or not self.channel.enabled:
            return
        payload = state.to_dict()
        root = subject_health_root(
            self.project_root, self.workflow_kind, subject_id
        )
        progress = {
            "goal_set": bool(str(payload.get("goal", "")).strip()),
            "conversation_entries": len(payload.get("conversation", []) or []),
            "execution_entries": len(payload.get("execution_log", []) or []),
            "attempt": int(payload.get("current_attempt", 0) or 0),
            "resolution_set": bool(str(payload.get("resolution", "")).strip()),
            "status": str(payload.get("status", "")),
            "diff": str(payload.get("last_diff_hash", "")),
            "verify": str(payload.get("last_verify_sig", "")),
            "workflow_id": str(payload.get("workflow_id", "")),
            "active_handoff_id": str(payload.get("active_handoff_id", "")),
            "return_phase": str(payload.get("return_phase", "")),
        }
        try:
            _atomic_json(
                root / "summary.json",
                {
                    "schema_version": 1,
                    "source": "in_process_telemetry",
                    "subject_id": subject_id,
                    "run_token": self.run_token,
                    "progress_digest": evidence_digest(progress),
                    "state_updated_at": str(payload.get("updated_at", "")),
                    "progress": progress,
                    "owner_pid": os.getpid(),
                    "owner_start_ticks": process_start_ticks(os.getpid()),
                    "updated_at": utc_now(),
                    "updated_epoch": time.time(),
                },
            )
        except OSError as error:
            logger.warning("session health telemetry could not be published: %s", error)

    def pending_session_action(self) -> Optional[Dict[str, object]]:
        if not self._available or not self.channel.enabled or not self._subject_id:
            return None
        store = HealthActionStore(
            self.project_root, self.workflow_kind, self._subject_id
        )
        return store.next_pending(run_token=self.run_token)

    def _publish_phase(self, phase: str, *, reason: str = "") -> None:
        if not self._subject_id:
            return
        root = subject_health_root(
            self.project_root, self.workflow_kind, self._subject_id
        )
        path = root / "heartbeat.json"
        current = read_json(path, default={})
        payload = dict(current) if isinstance(current, dict) else {}
        payload.update(
            schema_version=1,
            subject_id=self._subject_id,
            run_id=(self._subject_id if self.workflow_kind == "run" else ""),
            run_token=self.run_token,
            owner_pid=os.getpid(),
            owner_start_ticks=process_start_ticks(os.getpid()),
            process_phase=str(phase),
            status=str(payload.get("status", "")) or "observing",
            reason=reason or str(payload.get("reason", "")),
            updated_at=utc_now(),
            updated_epoch=time.time(),
        )
        _atomic_json(path, payload)

    def complete_session_action(self, request_id: str, *, detail: str = "") -> None:
        if not self._subject_id:
            return
        HealthActionStore(
            self.project_root, self.workflow_kind, self._subject_id
        ).transition(request_id, "completed", detail=detail)

    def _enable(self, _manifest: Dict[str, object]) -> None:
        if self._enabling:
            return
        self._enabling = True
        try:
            self._enable_inner()
        finally:
            self._enabling = False

    def _enable_inner(self) -> None:
        if self.orchestrator is not None:
            config = getattr(
                getattr(getattr(self.orchestrator, "config", None), "execution", None),
                "health_watch",
                None,
            )
            if config is not None:
                config.enabled = True
            if self.workflow_kind == "run" and self._subject_id:
                try:
                    state = load_run_state(self.project_root)
                    start = getattr(self.orchestrator, "_start_health_supervision", None)
                    if callable(start):
                        start(state)
                except (FileNotFoundError, RuntimeError, ValueError):
                    pass
        if self._subject_id:
            process = start_health_sidecar(
                project_root=self.project_root,
                run_token=self.run_token,
                auto_agents_entry=self.auto_agents_entry,
            )
            if process is not None:
                self.channel.set_sidecar(
                    process.pid, process_start_ticks(process.pid)
                )

    def _disable(self, _manifest: Dict[str, object]) -> None:
        if self.orchestrator is None:
            return
        config = getattr(
            getattr(getattr(self.orchestrator, "config", None), "execution", None),
            "health_watch",
            None,
        )
        if config is not None:
            config.enabled = False
        stop = getattr(self.orchestrator, "stop_health_supervision", None)
        if callable(stop):
            stop(status="disabled", reason="health-watch disabled at runtime")
