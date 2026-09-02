from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from .config import load_project_config, load_run_state, load_session_state, run_path
from .health_control import (
    HealthActionRecord,
    HealthActionStore,
    TERMINAL_PHASE,
    UNEXPECTED_EXIT_GRACE_SECONDS,
    evidence_digest,
    load_active_manifest,
    subject_health_root,
    utc_now,
    _atomic_json,
    _mutate_control,
)
from .health_watch import RunHealthEvaluator, capture_health_snapshot
from .io_utils import read_json
from .notifications import notify_flow_finished
from .process_supervision import process_identity_matches, process_start_ticks
from .session_health import (
    SESSION_PROGRESS_SCHEMA_VERSION,
    build_session_progress,
)


SIDECAR_SCHEMA_VERSION = 2


def watchdog_control_path(project_root: Path, run_id: str) -> Path:
    """Legacy diagnostic location retained for read compatibility."""
    return run_path(project_root, run_id) / "health" / "watchdog.json"


def start_health_sidecar(
    *,
    project_root: Path,
    run_token: str,
    auto_agents_entry: Path,
) -> Optional[subprocess.Popen]:
    """Launch one observer for the manifest-bound workflow; never restart it."""
    project = project_root.expanduser().resolve()
    manifest = load_active_manifest(project)
    if not manifest or str(manifest.get("run_token", "")) != str(run_token):
        return None
    if str(manifest.get("desired_state", "")) != "enabled":
        return None
    if not str(manifest.get("subject_id", "")).strip():
        return None
    pid = int(manifest.get("sidecar_pid", 0) or 0)
    ticks = int(manifest.get("sidecar_start_ticks", 0) or 0)
    if process_identity_matches(pid, ticks):
        return None
    process = subprocess.Popen(
        [
            os.fspath(Path(sys.executable).resolve()),
            os.fspath(auto_agents_entry.resolve()),
            "_health-sidecar",
            "--project",
            str(project),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    def record_sidecar(current: Dict[str, object]) -> Dict[str, object]:
        if str(current.get("run_token", "")) != str(run_token):
            return current
        current.update(
            sidecar_pid=process.pid,
            sidecar_start_ticks=process_start_ticks(process.pid),
            sidecar_started_at=utc_now(),
            updated_at=utc_now(),
            updated_epoch=time.time(),
        )
        return current

    _mutate_control(project, record_sidecar)
    return process


def request_sidecar_exit(project_root: Path, *, run_token: str, reason: str) -> None:
    """Record lifecycle intent. The observer exits by reading it; no signal is sent."""
    manifest = load_active_manifest(project_root, require_owner=False)
    if not manifest or str(manifest.get("run_token", "")) != str(run_token):
        return
    def record_terminal(current: Dict[str, object]) -> Dict[str, object]:
        if str(current.get("run_token", "")) != str(run_token):
            return current
        current.update(
            process_phase=TERMINAL_PHASE,
            terminal_reason=str(reason),
            updated_at=utc_now(),
            updated_epoch=time.time(),
        )
        return current

    _mutate_control(project_root, record_terminal)


class IndependentHealthAuditor:
    def __init__(self, project_root: Path, manifest: Dict[str, object]) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.run_token = str(manifest.get("run_token", ""))
        self.workflow_kind = str(manifest.get("workflow_kind", ""))
        self.subject_id = str(manifest.get("subject_id", ""))
        self.root = subject_health_root(
            self.project_root, self.workflow_kind, self.subject_id
        )
        self.config = load_project_config(self.project_root).execution.health_watch
        self.actions = HealthActionStore(
            self.project_root, self.workflow_kind, self.subject_id
        )
        self.evaluator = RunHealthEvaluator(self.config) if self.workflow_kind == "run" else None
        self.sequence = 0
        self.previous_session: Dict[str, object] = {}
        self.last_session_progress_at = time.time()
        self.previous_session_activity = ""
        self.session_activity_since_progress = False
        self.session_retry_pressure = 0
        self.self_repair_progress_digest = ""
        self.self_repair_progress_at = time.time()
        self.last_resume_epoch = 0

    def observe_once(self, manifest: Dict[str, object]) -> None:
        if self.workflow_kind == "run":
            self._observe_run(manifest)
        else:
            self._observe_session(manifest)

    def _observe_run(self, manifest: Dict[str, object]) -> None:
        state = load_run_state(self.project_root)
        if state.run_id != self.subject_id:
            self._request(
                "diagnose",
                "health_subject_disagreement",
                {"manifest_subject": self.subject_id, "raw_subject": state.run_id},
            )
            return
        health_control = (
            dict(state.health_control)
            if isinstance(state.health_control, dict)
            else {}
        )
        rewind_epoch = max(
            0, int(health_control.get("rewind_epoch", 0) or 0)
        )
        resume_epoch = max(
            0, int(health_control.get("resume_epoch", 0) or 0)
        )
        intervention_active = bool(
            health_control.get("intervention_active", False)
            or (state.active_repair_case_id and state.repair_phase)
        )
        self.sequence += 1
        snapshot = capture_health_snapshot(
            self.project_root,
            state,
            sequence=self.sequence,
            rewind_epoch=rewind_epoch,
        )
        lease = self.config.poll_seconds * 4
        smart = load_project_config(self.project_root).execution.smart_timeout
        lease = smart.stage_progress_lease_seconds.get(
            state.current_stage, smart.semantic_stall_seconds
        )
        anomaly = None
        if self.evaluator is not None:
            if resume_epoch > self.last_resume_epoch:
                self.evaluator.renew_progress_lease(snapshot.observed_epoch)
                self.last_resume_epoch = resume_epoch
            if intervention_active:
                # Diagnosis/quiescence is an explicit lifecycle, not ordinary
                # run progress.  Rebase while it is active so its elapsed time
                # cannot consume the resumed stage's goal lease or enqueue a
                # second run-level stall intervention.
                self.evaluator.rebase(snapshot)
            else:
                anomaly = self.evaluator.evaluate(
                    snapshot,
                    progress_lease_seconds=max(60, int(lease)),
                )
        active_operation = manifest.get("active_operation", {})
        active_operation = (
            dict(active_operation) if isinstance(active_operation, dict) else {}
        )
        operation_heartbeat = float(
            active_operation.get("heartbeat_epoch", 0.0) or 0.0
        )
        active_operation_live = bool(
            str(active_operation.get("kind", "")).strip()
            and operation_heartbeat
            and time.time() - operation_heartbeat
            <= max(5.0, float(self.config.heartbeat_timeout_seconds))
        )
        audit = {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "source": "independent_health_auditor",
            "run_token": self.run_token,
            "process_phase": str(manifest.get("process_phase", "")),
            "snapshot": snapshot.to_dict(),
            "intervention_active": intervention_active,
            "resume_epoch": resume_epoch,
            "active_operation": active_operation,
            "active_operation_live": active_operation_live,
            "updated_at": utc_now(),
        }
        _atomic_json(self.root / "auditor-snapshot.json", audit)
        self._compare_main_progress(
            snapshot.progress.digest,
            snapshot.sequence,
            evidence_digest(state.to_dict()),
        )
        if anomaly is not None:
            self._request(
                "diagnose",
                f"health_anomaly:{anomaly.kind}",
                {
                    "anomaly": anomaly.to_dict(),
                    "snapshot": snapshot.to_dict(),
                },
                sequence=snapshot.sequence,
            )
        if str(manifest.get("process_phase", "")) == "self_repair":
            if active_operation_live:
                # A self-repair provider call or validation operation has its own
                # bounded timeout/progress supervision.  The run-state vector is
                # intentionally stable while that isolated work is in flight, so
                # the control-channel heartbeat is the relevant liveness signal.
                self.self_repair_progress_at = time.time()
            if snapshot.progress.digest != self.self_repair_progress_digest:
                self.self_repair_progress_digest = snapshot.progress.digest
                self.self_repair_progress_at = time.time()
            self_repair_lease = max(
                60.0,
                float(lease) * float(self.config.goal_stall_lease_multiplier),
            )
            if (
                not active_operation_live
                and time.time() - self.self_repair_progress_at >= self_repair_lease
            ):
                self._request(
                    "diagnose",
                    "self_repair_stagnation",
                    {"snapshot": snapshot.to_dict(), "lease": self_repair_lease},
                    sequence=snapshot.sequence,
                )
        else:
            self.self_repair_progress_digest = ""
            self.self_repair_progress_at = time.time()

    def _compare_main_progress(
        self, independent_digest: str, sequence: int, state_digest: str
    ) -> None:
        main = read_json(self.root / "summary.json", default={})
        if not isinstance(main, dict):
            return
        main_digest = str(main.get("progress_digest", ""))
        same_raw_state = bool(
            state_digest and str(main.get("state_digest", "")) == state_digest
        )
        if same_raw_state and main_digest and main_digest != independent_digest:
            self._request(
                "diagnose",
                "health_observer_disagreement",
                {
                    "main_progress_digest": main_digest,
                    "independent_progress_digest": independent_digest,
                    "main_sequence": int(main.get("sequence", 0) or 0),
                },
                sequence=sequence,
            )

    def _observe_session(self, manifest: Dict[str, object]) -> None:
        state = load_session_state(self.project_root, self.subject_id)
        raw = state.to_dict()
        durable = build_session_progress(raw)
        digest = evidence_digest(durable)
        state_digest = evidence_digest(raw)
        session_dir = self.root.parent
        file_activity = []
        for path in sorted(session_dir.glob("**/*")):
            if not path.is_file() or self.root in path.parents:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            file_activity.append((str(path.relative_to(session_dir)), stat.st_size, stat.st_mtime_ns))
        activity_digest = evidence_digest(
            {
                "execution_entries": len(state.execution_log),
                "attempt": state.current_attempt,
                "agent_errors": state.consecutive_agent_errors,
                "files": file_activity,
            }
        )
        prior = self.previous_session
        progressed = bool(
            not prior
            or int(durable["conversation_entries"]) > int(prior.get("conversation_entries", 0))
            or int(durable["execution_entries"]) > int(prior.get("execution_entries", 0))
            or int(durable["attempt"]) > int(prior.get("attempt", 0))
            or bool(durable["resolution_set"]) and not bool(prior.get("resolution_set", False))
            or str(durable["status"]) != str(prior.get("status", ""))
            or bool(durable["diff"]) and str(durable["diff"]) != str(prior.get("diff", ""))
        )
        regressed = bool(
            prior
            and (
                int(durable["conversation_entries"]) < int(prior.get("conversation_entries", 0))
                or int(durable["execution_entries"]) < int(prior.get("execution_entries", 0))
            )
        )
        if progressed:
            self.last_session_progress_at = time.time()
            self.session_activity_since_progress = False
            self.session_retry_pressure = 0
        elif self.previous_session_activity and activity_digest != self.previous_session_activity:
            self.session_activity_since_progress = True
            prior_attempt = int(prior.get("attempt", 0) or 0)
            self.session_retry_pressure += max(0, state.current_attempt - prior_attempt)
        self.previous_session_activity = activity_digest
        self.previous_session = durable
        active_operation = manifest.get("active_operation", {})
        active_operation = (
            dict(active_operation) if isinstance(active_operation, dict) else {}
        )
        operation_heartbeat = float(
            active_operation.get("heartbeat_epoch", 0.0) or 0.0
        )
        active_operation_live = bool(
            str(active_operation.get("kind", "")).strip()
            and operation_heartbeat
            and time.time() - operation_heartbeat
            <= max(5.0, float(self.config.heartbeat_timeout_seconds))
        )
        if active_operation_live:
            self.last_session_progress_at = time.time()
            self.session_activity_since_progress = False
        self.sequence += 1
        audit = {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "source": "independent_health_auditor",
            "run_token": self.run_token,
            "process_phase": str(manifest.get("process_phase", "")),
            "sequence": self.sequence,
            "state_digest": state_digest,
            "progress_schema_version": SESSION_PROGRESS_SCHEMA_VERSION,
            "progress_digest": digest,
            "progress": durable,
            "activity_digest": activity_digest,
            "active_operation": active_operation,
            "active_operation_live": active_operation_live,
            "updated_at": utc_now(),
        }
        _atomic_json(self.root / "auditor-snapshot.json", audit)
        main = read_json(self.root / "summary.json", default={})
        if (
            isinstance(main, dict)
            and str(main.get("run_token", "")) == self.run_token
            and str(main.get("progress_schema_version", ""))
            == str(SESSION_PROGRESS_SCHEMA_VERSION)
            and state_digest
            and str(main.get("state_digest", "")) == state_digest
            and str(main.get("progress_digest", ""))
            and str(main.get("progress_digest", "")) != digest
        ):
            self._request(
                "diagnose",
                "health_observer_disagreement",
                {
                    "main_progress_digest": str(main.get("progress_digest", "")),
                    "independent_progress_digest": digest,
                    "run_token": self.run_token,
                    "progress_schema_version": SESSION_PROGRESS_SCHEMA_VERSION,
                    "state_digest": state_digest,
                    "sequence": self.sequence,
                },
                sequence=self.sequence,
            )
        if regressed:
            self._request("diagnose", "health_session_regression", audit, sequence=self.sequence)
        if self.session_retry_pressure >= 3:
            self._request(
                "diagnose",
                "health_anomaly:resource_degraded",
                audit,
                sequence=self.sequence,
            )
        actions = [
            str(item.get("action", ""))
            for item in state.execution_log[-12:]
            if isinstance(item, dict) and str(item.get("action", ""))
        ]
        for cycle_length in range(1, 5):
            required = cycle_length * max(2, int(self.config.oscillation_repeat_limit))
            if len(actions) < required:
                continue
            suffix = actions[-required:]
            cycle = suffix[:cycle_length]
            if all(
                suffix[index : index + cycle_length] == cycle
                for index in range(0, required, cycle_length)
            ):
                self._request(
                    "diagnose",
                    "health_anomaly:oscillating",
                    {**audit, "control_cycle": cycle},
                    sequence=self.sequence,
                )
                break
        stall_lease = max(
            60.0,
            float(self.config.poll_seconds)
            * float(self.config.goal_stall_lease_multiplier),
        )
        if (
            str(manifest.get("process_phase", "")) == "self_repair"
            and time.time() - self.last_session_progress_at >= stall_lease
        ):
            self._request(
                "diagnose",
                "self_repair_stagnation",
                audit,
                sequence=self.sequence,
            )
        elif (
            self.session_activity_since_progress
            and not active_operation_live
            and time.time() - self.last_session_progress_at >= stall_lease
        ):
            self._request(
                "diagnose",
                "health_anomaly:goal_stalled",
                audit,
                sequence=self.sequence,
            )

    def _request(
        self,
        action: str,
        reason: str,
        evidence: object,
        *,
        sequence: int = 0,
    ) -> Dict[str, object]:
        evidence_map = dict(evidence) if isinstance(evidence, dict) else {}
        anomaly = evidence_map.get("anomaly", {})
        anomaly_map = dict(anomaly) if isinstance(anomaly, dict) else {}
        snapshot = evidence_map.get("snapshot", {})
        snapshot_map = dict(snapshot) if isinstance(snapshot, dict) else {}
        progress = snapshot_map.get("progress", {})
        progress_map = dict(progress) if isinstance(progress, dict) else {}
        stable_context = (
            str(anomaly_map.get("root_fingerprint", ""))
            or str(progress_map.get("digest", ""))
            or str(evidence_map.get("progress_digest", ""))
            or str(evidence_map.get("independent_progress_digest", ""))
            or evidence_digest(evidence_map)
        )
        return self.actions.append(
            HealthActionRecord(
                request_id=uuid.uuid4().hex[:12],
                action=action,
                reason=reason,
                source="health_sidecar",
                run_token=self.run_token,
                subject_id=self.subject_id,
                observation_sequence=sequence,
                evidence_digest=evidence_digest(evidence),
                dedupe_key=evidence_digest(
                    {"action": action, "reason": reason, "context": stable_context}
                ),
                evidence=dict(evidence) if isinstance(evidence, dict) else {},
            )
        )

    def record_owner_crash(self, manifest: Dict[str, object]) -> None:
        evidence = {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "kind": "unexpected_owner_exit",
            "project": str(self.project_root),
            "workflow_kind": self.workflow_kind,
            "subject_id": self.subject_id,
            "run_token": self.run_token,
            "owner_pid": int(manifest.get("owner_pid", 0) or 0),
            "owner_start_ticks": int(manifest.get("owner_start_ticks", 0) or 0),
            "last_process_phase": str(manifest.get("process_phase", "")),
            "observed_at": utc_now(),
            "pending_manual_resume": True,
        }
        _atomic_json(self.root / "unexpected-owner-exit.json", evidence)
        self._request("pending_manual_resume", "unexpected_owner_exit", evidence)
        notification_path = self.root / "unexpected-owner-exit-notified.json"
        prior = read_json(notification_path, default={})
        if not isinstance(prior, dict) or str(prior.get("run_token", "")) != self.run_token:
            sent = notify_flow_finished(
                self.project_root,
                workflow=self.workflow_kind,
                status="failed",
                identifier=self.subject_id,
                detail=(
                    "The foreground owner exited unexpectedly. No restart was attempted; "
                    "the next user-started command will validate whether it can resume."
                ),
                paths=(self.root / "unexpected-owner-exit.json",),
            )
            _atomic_json(
                notification_path,
                {"run_token": self.run_token, "sent": sent, "updated_at": utc_now()},
            )


def run_health_sidecar(project_root: Path) -> int:
    project = project_root.expanduser().resolve()
    manifest = load_active_manifest(project, require_owner=False)
    if not manifest:
        return 0
    token = str(manifest.get("run_token", ""))
    if str(manifest.get("desired_state", "")) != "enabled":
        return 0
    auditor = IndependentHealthAuditor(project, manifest)
    poll = max(5.0, float(auditor.config.poll_seconds))
    next_audit_at = 0.0
    while True:
        current = load_active_manifest(project, require_owner=False)
        if not current or str(current.get("run_token", "")) != token:
            return 0
        if str(current.get("desired_state", "")) != "enabled":
            return 0
        phase = str(current.get("process_phase", ""))
        owner_pid = int(current.get("owner_pid", 0) or 0)
        owner_ticks = int(current.get("owner_start_ticks", 0) or 0)
        alive = process_identity_matches(owner_pid, owner_ticks)
        if phase == TERMINAL_PHASE:
            try:
                auditor.observe_once(current)
            except (FileNotFoundError, RuntimeError, ValueError):
                pass
            return 0
        if not alive:
            time.sleep(UNEXPECTED_EXIT_GRACE_SECONDS)
            revalidated = load_active_manifest(project, require_owner=False)
            if (
                revalidated
                and str(revalidated.get("run_token", "")) == token
                and str(revalidated.get("process_phase", "")) != TERMINAL_PHASE
                and not process_identity_matches(owner_pid, owner_ticks)
            ):
                auditor.record_owner_crash(revalidated)
            return 0
        if time.monotonic() >= next_audit_at:
            try:
                auditor.observe_once(current)
            except Exception as error:
                _atomic_json(
                    auditor.root / "auditor-error.json",
                    {
                        "schema_version": SIDECAR_SCHEMA_VERSION,
                        "error": str(error)[:2000],
                        "updated_at": utc_now(),
                    },
                )
            next_audit_at = time.monotonic() + poll
        time.sleep(1.0)


# Compatibility names for callers from releases before the unified control plane.
def run_watchdog(project_root: Path, run_id: str = "") -> int:
    return run_health_sidecar(project_root)


def mark_watchdog_stop_intent(project_root: Path, run_id: str, *, reason: str) -> None:
    manifest = load_active_manifest(project_root, require_owner=False)
    if manifest:
        request_sidecar_exit(
            project_root,
            run_token=str(manifest.get("run_token", "")),
            reason=reason,
        )


def start_run_watchdog(
    *,
    project_root: Path,
    run_id: str,
    run_token: str,
    auto_agents_entry: Path,
) -> Optional[subprocess.Popen]:
    return start_health_sidecar(
        project_root=project_root,
        run_token=run_token,
        auto_agents_entry=auto_agents_entry,
    )
