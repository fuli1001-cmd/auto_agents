from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, Mapping, Optional

from .config import load_run_state, load_session_state
from .health_control import (
    HealthActionStore,
    HealthControlChannel,
    subject_health_root,
    utc_now,
    _atomic_json,
)
from .health_watchdog import start_health_sidecar
from .io_utils import read_json
from .process_supervision import process_start_ticks
from .session_health import (
    SESSION_PROGRESS_SCHEMA_VERSION,
    build_session_progress_identity,
)


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
        fresh_health_boundary: bool = False,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.workflow_kind = workflow_kind
        self.run_token = str(run_token)
        self.auto_agents_entry = auto_agents_entry
        self.orchestrator = orchestrator
        self.fresh_health_boundary = bool(fresh_health_boundary)
        self._subject_id = ""
        self._enabling = False
        self._available = True
        self._published_subjects: set[str] = set()
        self._rebased_subjects: set[str] = set()
        self._sidecar_launch_attempted_generation: Optional[str] = None
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
            if self.orchestrator is not None:
                prepare = getattr(
                    self.orchestrator,
                    "_prepare_project_config_for_supervision",
                    None,
                )
                if callable(prepare):
                    prepare()
                health_config = getattr(
                    getattr(
                        getattr(self.orchestrator, "config", None),
                        "execution",
                        None,
                    ),
                    "health_watch",
                    None,
                )
                if health_config is not None:
                    # Reapply command-scoped --no-health-watch after a reload.
                    health_config.enabled = self.channel.enabled
            self._rebase_subject_actions(self._subject_id)
            self.channel.start(self._subject_id)
            baseline_ready = bool(
                self.workflow_kind == "run" or not self.fresh_health_boundary
            )
            if self.channel.enabled and self._subject_id and baseline_ready:
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
            self._rebase_subject_actions(normalized)
            self.channel.bind_subject(normalized)
            self._publish_phase(self.channel.process_phase)
            baseline_ready = bool(
                self.workflow_kind == "run"
                or not self.fresh_health_boundary
                or normalized in self._published_subjects
            )
            if (
                changed
                and baseline_ready
                and self.channel.enabled
                and not self._enabling
            ):
                self._enable({})
        except Exception as error:
            logger.warning("health subject/sidecar could not be bound: %s", error)

    def _rebase_subject_actions(self, subject_id: str) -> None:
        if (
            not self.fresh_health_boundary
            or not subject_id
            or subject_id in self._rebased_subjects
        ):
            return
        HealthActionStore(
            self.project_root, self.workflow_kind, subject_id
        ).supersede_obsolete(
            run_token=self.run_token,
            detail="superseded at the self-repair health boundary",
        )
        self._rebased_subjects.add(subject_id)

    def set_phase(self, phase: str) -> None:
        if not self._available:
            return
        try:
            self.channel.set_phase(phase)
            self._publish_phase(phase)
        except Exception as error:
            logger.warning("health process phase could not be published: %s", error)

    def set_active_operation(self, kind: str = "", label: str = "") -> None:
        if not self._available:
            return
        try:
            self.channel.set_active_operation(kind, label)
        except Exception as error:
            logger.warning("health active operation could not be published: %s", error)

    def _session_progress_identity(self, payload: Mapping[str, object]) -> Dict[str, object]:
        return build_session_progress_identity(
            payload,
            run_token=self.run_token,
        )

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
        identity = self._session_progress_identity(payload)
        progress = dict(identity["progress"])
        try:
            _atomic_json(
                root / "summary.json",
                {
                    "schema_version": 1,
                    "source": "in_process_telemetry",
                    "subject_id": subject_id,
                    "run_token": self.run_token,
                    "progress_schema_version": SESSION_PROGRESS_SCHEMA_VERSION,
                    "progress_digest": identity["progress_digest"],
                    "state_digest": identity["state_digest"],
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
            return
        self._published_subjects.add(subject_id)
        HealthActionStore(
            self.project_root, self.workflow_kind, subject_id
        ).supersede_stale_progress(
            run_token=self.run_token,
            progress_identity=identity,
        )
        if (
            self.fresh_health_boundary
            and self.channel.enabled
            and not self._enabling
            and self._sidecar_launch_attempted_generation is None
        ):
            try:
                self._enable({})
            except Exception as error:
                logger.warning("health sidecar could not start after rebase: %s", error)

    def pending_session_action(self) -> Optional[Dict[str, object]]:
        if not self._available or not self.channel.enabled or not self._subject_id:
            return None
        store = HealthActionStore(
            self.project_root, self.workflow_kind, self._subject_id
        )
        if self.fresh_health_boundary:
            store.supersede_obsolete(
                run_token=self.run_token,
                detail="superseded at the self-repair health boundary",
            )
        try:
            state = load_session_state(self.project_root, self._subject_id)
        except (OSError, RuntimeError, FileNotFoundError, ValueError):
            return None
        identity = self._session_progress_identity(state.to_dict())
        return store.next_pending(
            run_token=self.run_token,
            progress_identity=identity,
        )

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
            if self.fresh_health_boundary:
                if (
                    self.workflow_kind != "run"
                    and self._subject_id not in self._published_subjects
                ):
                    return
                if self._sidecar_launch_attempted_generation is not None:
                    return
                # A health generation owns at most one observer launch attempt.
                # Record it before spawning so an exception or dead replacement
                # cannot be hidden by a later publication starting another one.
                self._sidecar_launch_attempted_generation = self.run_token
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
