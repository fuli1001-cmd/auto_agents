from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import load_run_state, run_path
from .git_ops import head_ref, worktree_fingerprint
from .io_utils import read_json
from .models import HealthWatchConfig, RunState, STAGE_ORDER, SmartTimeoutConfig, TaskSpec
from .process_supervision import ACTIVE_PROCESSES, process_start_ticks
from .repair_cases import RepairCase, RepairCaseStore, stable_repair_fingerprint


HEALTH_SCHEMA_VERSION = 1
EVENT_LOG_MAX_BYTES = 10 * 1024 * 1024
EVENT_LOG_ROTATIONS = 3
SNAPSHOT_RETENTION = 50


class HealthSelfRepairRequired(RuntimeError):
    def __init__(self, repair_case: RepairCase, triage: object) -> None:
        self.repair_case = repair_case
        self.triage = triage
        super().__init__(repair_case.symptom or repair_case.kind)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


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


def _proof_identity(task: TaskSpec, proof: Mapping[str, object]) -> str:
    return _json_hash(
        {
            "task_lineage": task.parent_task_id or task.task_id,
            "requirement_id": str(proof.get("requirement_id", "")),
            "oracle_index": proof.get("oracle_index"),
            "acceptance_oracle": str(proof.get("acceptance_oracle", "")),
            "contract": str(proof.get("requirement_contract_sha256", "")),
        }
    )[:24]


@dataclass(frozen=True)
class ProgressVector:
    durable_atoms: Tuple[str, ...]
    unresolved_roots: Tuple[str, ...]
    root_occurrences: Tuple[Tuple[str, int], ...]
    completed_stages: Tuple[str, ...]
    done_lineages: Tuple[str, ...]
    verified_proofs: Tuple[str, ...]

    @property
    def digest(self) -> str:
        return _json_hash(
            {
                "durable_atoms": self.durable_atoms,
                "unresolved_roots": self.unresolved_roots,
                "completed_stages": self.completed_stages,
                "done_lineages": self.done_lineages,
                "verified_proofs": self.verified_proofs,
            }
        )

    def to_dict(self) -> Dict[str, object]:
        return {**asdict(self), "digest": self.digest}


@dataclass(frozen=True)
class HealthSnapshot:
    sequence: int
    observed_at: str
    observed_epoch: float
    run_id: str
    run_status: str
    stage: str
    task_id: str
    progress: ProgressVector
    activity_digest: str
    head_ref: str
    worktree_fingerprint: str
    active_tool_count: int
    active_tool: str
    retry_pressure: int
    control_fingerprint: str
    control_history: Tuple[str, ...]
    rewind_epoch: int

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["progress"] = self.progress.to_dict()
        return payload


@dataclass(frozen=True)
class HealthAnomaly:
    kind: str
    severity: str
    stage: str
    root_fingerprint: str
    reason: str
    expected_postconditions: Tuple[str, ...]
    failure_scope: str = "run"
    task_id: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class HealthActionRequest:
    action: str
    anomaly: HealthAnomaly
    repair_case_id: str
    created_at: str = field(default_factory=utc_now)


def build_progress_vector(state: RunState) -> ProgressVector:
    stages = tuple(
        stage for stage in STAGE_ORDER if stage in state.stage_summaries
    )
    done_lineages: set[str] = set()
    verified_proofs: set[str] = set()
    durable_atoms: set[str] = set()
    for stage in stages:
        durable_atoms.add(f"stage:{stage}")
    for task in state.tasks:
        lineage = task.parent_task_id or task.task_id
        if task.status == "done":
            done_lineages.add(lineage)
            durable_atoms.add(f"task:{lineage}:done")
        for proof in task.requirement_proofs:
            if not isinstance(proof, Mapping):
                continue
            if str(proof.get("status", "")).strip() != "verified":
                continue
            identity = _proof_identity(task, proof)
            verified_proofs.add(identity)
            durable_atoms.add(f"proof:{identity}")

    unresolved_roots: set[str] = set()
    root_occurrences: Dict[str, int] = {}
    for entry in state.execution_incidents:
        if not isinstance(entry, Mapping):
            continue
        root = str(
            entry.get("root_cause_fingerprint")
            or entry.get("incident_fingerprint", "")
        ).strip()
        if not root:
            continue
        root_occurrences[root] = max(
            root_occurrences.get(root, 0),
            int(entry.get("occurrence_count", 1) or 1),
        )
        if str(entry.get("status", "")) != "resolved":
            unresolved_roots.add(root)
    blocker = state.active_blocker if isinstance(state.active_blocker, dict) else {}
    blocker_root = str(
        blocker.get("fingerprint") or blocker.get("category", "")
    ).strip()
    if blocker_root and state.status == "blocked":
        unresolved_roots.add(blocker_root)
    return ProgressVector(
        durable_atoms=tuple(sorted(durable_atoms)),
        unresolved_roots=tuple(sorted(unresolved_roots)),
        root_occurrences=tuple(sorted(root_occurrences.items())),
        completed_stages=stages,
        done_lineages=tuple(sorted(done_lineages)),
        verified_proofs=tuple(sorted(verified_proofs)),
    )


def _latest_provider_report(run_root: Path) -> Dict[str, object]:
    matches: list[tuple[float, Path]] = []
    for path in run_root.glob("outputs/provider-attempts/*.json"):
        try:
            matches.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not matches:
        return {}
    payload = read_json(max(matches)[1], default={})
    return payload if isinstance(payload, dict) else {}


def _activity_payload(project_root: Path, state: RunState) -> Dict[str, object]:
    root = run_path(project_root, state.run_id)
    report = _latest_provider_report(root)
    run_log = root / "run.log"
    try:
        log_size = run_log.stat().st_size
        log_mtime = run_log.stat().st_mtime_ns
    except OSError:
        log_size = 0
        log_mtime = 0
    recovery_events = sum(len(task.recovery_history) for task in state.tasks)
    verify_events = sum(len(task.verify_history) for task in state.tasks)
    return {
        "head": _safe_head(project_root),
        "worktree": _safe_worktree(project_root),
        "agent_attempts": state.agent_attempts,
        "retry_pressure": sum(int(value) for value in state.agent_attempts.values())
        + recovery_events,
        "recovery_events": recovery_events,
        "verify_events": verify_events,
        "provider_updated_at": report.get("updated_at", ""),
        "provider_workspace": report.get("workspace_fingerprint", ""),
        "provider_output": report.get("output_fingerprint", ""),
        "provider_events": report.get("events", [])[-5:],
        "run_log_size": log_size,
        "run_log_mtime": log_mtime,
    }


def _safe_head(project_root: Path) -> str:
    try:
        return head_ref(project_root)
    except Exception:
        return ""


def _safe_worktree(project_root: Path) -> str:
    try:
        return worktree_fingerprint(project_root)
    except Exception:
        return ""


def capture_health_snapshot(
    project_root: Path,
    state: RunState,
    *,
    sequence: int,
    now: Optional[float] = None,
    control_fingerprint: str = "",
    control_history: Sequence[str] = (),
    rewind_epoch: int = 0,
) -> HealthSnapshot:
    observed = time.time() if now is None else float(now)
    activity = _activity_payload(project_root, state)
    report = _latest_provider_report(run_path(project_root, state.run_id))
    active_task = next(
        (task.task_id for task in state.tasks if task.status == "in_progress"),
        "",
    )
    return HealthSnapshot(
        sequence=sequence,
        observed_at=datetime.fromtimestamp(observed, timezone.utc).isoformat(),
        observed_epoch=observed,
        run_id=state.run_id,
        run_status=state.status,
        stage=state.current_stage,
        task_id=active_task,
        progress=build_progress_vector(state),
        activity_digest=_json_hash(activity),
        head_ref=str(activity.get("head", "")),
        worktree_fingerprint=str(activity.get("worktree", "")),
        active_tool_count=int(report.get("active_tool_count", 0) or 0),
        active_tool=str(report.get("active_tool", "")),
        retry_pressure=int(activity.get("retry_pressure", 0) or 0),
        control_fingerprint=control_fingerprint,
        control_history=tuple(str(item) for item in control_history),
        rewind_epoch=max(0, int(rewind_epoch)),
    )


class RunHealthEvaluator:
    def __init__(self, config: HealthWatchConfig) -> None:
        self.config = config
        self.previous: Optional[HealthSnapshot] = None
        self.last_progress_at = 0.0
        self.activity_since_progress = False
        self.control_history: Deque[str] = deque(maxlen=64)
        self.rewind_epoch = 0
        self._last_rewind_seen = 0
        self.retry_pressure_without_progress = 0

    def record_control(self, fingerprint: str, *, rewind: bool = False) -> None:
        normalized = " ".join(str(fingerprint).split())
        if normalized:
            self.control_history.append(normalized)
        if rewind:
            self.rewind_epoch += 1

    def evaluate(
        self,
        snapshot: HealthSnapshot,
        *,
        progress_lease_seconds: float,
    ) -> Optional[HealthAnomaly]:
        previous = self.previous
        if snapshot.control_history:
            self.control_history = deque(snapshot.control_history, maxlen=64)
        if snapshot.rewind_epoch > self.rewind_epoch:
            self.rewind_epoch = snapshot.rewind_epoch
        self.previous = snapshot
        if previous is None:
            self.last_progress_at = snapshot.observed_epoch
            self._last_rewind_seen = self.rewind_epoch
            return None
        if snapshot.run_status in {
            "completed",
            "paused",
            "waiting_user",
            "blocked",
            "failed",
        }:
            self.last_progress_at = snapshot.observed_epoch
            self.activity_since_progress = False
            return None

        prior_atoms = set(previous.progress.durable_atoms)
        current_atoms = set(snapshot.progress.durable_atoms)
        rewind_declared = self.rewind_epoch != self._last_rewind_seen
        self._last_rewind_seen = self.rewind_epoch
        if rewind_declared:
            self.last_progress_at = snapshot.observed_epoch
            self.activity_since_progress = False
        elif not prior_atoms.issubset(current_atoms):
            removed = sorted(prior_atoms - current_atoms)
            root = stable_repair_fingerprint("regression", snapshot.stage, *removed)
            return HealthAnomaly(
                kind="regressing",
                severity="confirmed",
                stage=snapshot.stage,
                root_fingerprint=root,
                reason="durable progress atoms disappeared without a declared rewind: "
                + ", ".join(removed[:8]),
                expected_postconditions=(
                    "durable progress is monotonic outside a declared rewind epoch",
                    "removed proof/task/stage atoms are restored or explicitly superseded",
                ),
                failure_scope=("task_lineage" if snapshot.task_id else "run"),
                task_id=snapshot.task_id,
            )

        progressed = bool(current_atoms - prior_atoms) or (
            len(snapshot.progress.unresolved_roots)
            < len(previous.progress.unresolved_roots)
        )
        if progressed:
            self.last_progress_at = snapshot.observed_epoch
            self.activity_since_progress = False
            self.retry_pressure_without_progress = 0
        elif snapshot.activity_digest != previous.activity_digest:
            self.activity_since_progress = True
        if snapshot.retry_pressure > previous.retry_pressure and not progressed:
            self.retry_pressure_without_progress += (
                snapshot.retry_pressure - previous.retry_pressure
            )

        if (
            self.retry_pressure_without_progress >= 3
            and snapshot.active_tool_count == 0
        ):
            root = stable_repair_fingerprint(
                "resource_degraded", snapshot.stage, snapshot.progress.digest
            )
            return HealthAnomaly(
                kind="resource_degraded",
                severity="confirmed",
                stage=snapshot.stage,
                root_fingerprint=root,
                reason=(
                    "retry/recovery pressure increased three times without durable progress"
                ),
                expected_postconditions=(
                    "retry pressure stops increasing without progress",
                    "provider or infrastructure recovery produces a durable progress delta",
                ),
                failure_scope=("task_lineage" if snapshot.task_id else "stage"),
                task_id=snapshot.task_id,
            )

        churn = self._recovery_churn(snapshot, previous)
        if churn is not None:
            return churn
        oscillation = self._oscillation(snapshot)
        if oscillation is not None:
            return oscillation
        stalled_for = snapshot.observed_epoch - self.last_progress_at
        hard_lease = max(
            60.0,
            float(progress_lease_seconds)
            * float(self.config.goal_stall_lease_multiplier),
        )
        if (
            self.activity_since_progress
            and snapshot.active_tool_count == 0
            and stalled_for >= hard_lease
        ):
            root = stable_repair_fingerprint(
                "goal_stalled", snapshot.stage, snapshot.progress.digest
            )
            return HealthAnomaly(
                kind="goal_stalled",
                severity="confirmed",
                stage=snapshot.stage,
                root_fingerprint=root,
                reason=(
                    "activity continued without durable goal progress for "
                    f"{int(stalled_for)} seconds"
                ),
                expected_postconditions=(
                    "the progress vector gains a durable atom or resolves an active root",
                    "activity-only workspace/output changes do not renew the goal lease",
                ),
                failure_scope=("task_lineage" if snapshot.task_id else "run"),
                task_id=snapshot.task_id,
            )
        return None

    def _recovery_churn(
        self,
        snapshot: HealthSnapshot,
        previous: HealthSnapshot,
    ) -> Optional[HealthAnomaly]:
        previous_counts = dict(previous.progress.root_occurrences)
        for root, count in snapshot.progress.root_occurrences:
            if count < self.config.recovery_churn_limit:
                continue
            if count <= previous_counts.get(root, 0):
                continue
            if snapshot.progress.digest != previous.progress.digest:
                continue
            fingerprint = stable_repair_fingerprint("recovery_churn", root)
            return HealthAnomaly(
                kind="recovery_churn",
                severity="confirmed",
                stage=snapshot.stage,
                root_fingerprint=fingerprint,
                reason=(
                    f"root {root} reached {count} recovery occurrences without "
                    "durable postcondition progress"
                ),
                expected_postconditions=(
                    f"root cause {root} is resolved or its postcondition changes",
                    "recovery occurrence budgets are not reset by task-id or context churn",
                ),
                failure_scope=("task_lineage" if snapshot.task_id else "run"),
                task_id=snapshot.task_id,
            )
        return None

    def _oscillation(self, snapshot: HealthSnapshot) -> Optional[HealthAnomaly]:
        values = list(self.control_history)
        repeat_limit = self.config.oscillation_repeat_limit
        for cycle_length in range(1, 5):
            required = cycle_length * repeat_limit
            if len(values) < required:
                continue
            suffix = values[-required:]
            cycle = suffix[:cycle_length]
            if all(
                suffix[index : index + cycle_length] == cycle
                for index in range(0, required, cycle_length)
            ):
                root = stable_repair_fingerprint(
                    "oscillating", snapshot.stage, snapshot.progress.digest, *cycle
                )
                return HealthAnomaly(
                    kind="oscillating",
                    severity="confirmed",
                    stage=snapshot.stage,
                    root_fingerprint=root,
                    reason=(
                        f"the same control-flow cycle repeated {repeat_limit} times "
                        "without durable progress"
                    ),
                    expected_postconditions=(
                        "the repeated control-flow cycle is broken",
                        "the next recovery route changes the durable progress vector",
                    ),
                    failure_scope=("task_lineage" if snapshot.task_id else "stage"),
                    task_id=snapshot.task_id,
                )
        return None


class RunHealthSupervisor:
    def __init__(
        self,
        project_root: Path,
        run_id: str,
        *,
        config: HealthWatchConfig,
        smart_timeout: SmartTimeoutConfig,
        autonomy_mode: str,
        run_token: str = "",
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.run_id = str(run_id)
        self.config = config
        self.smart_timeout = smart_timeout
        self.autonomy_mode = str(autonomy_mode or "max")
        self.run_token = str(run_token)
        self.root = run_path(self.project_root, self.run_id) / "health"
        self.evaluator = RunHealthEvaluator(config)
        self._control_events: "queue.Queue[Tuple[str, bool]]" = queue.Queue()
        self._actions: "queue.Queue[HealthActionRequest]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sequence = 0
        self._interventions: Dict[str, int] = {}
        self._last_anomaly_root = ""
        self._last_anomaly_at = 0.0
        self._terminal_status = ""
        self._terminal_reason = ""
        self._last_summary: Dict[str, object] = {}
        self._recent_snapshots: Deque[Dict[str, object]] = deque(maxlen=16)

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._restore_recent_snapshots()
        self._thread = threading.Thread(
            target=self._run,
            name=f"auto-agents-health-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def _restore_recent_snapshots(self) -> None:
        summary = read_json(self.root / "summary.json", default={})
        if isinstance(summary, dict):
            raw_interventions = summary.get("interventions", {})
            if isinstance(raw_interventions, dict):
                self._interventions = {
                    str(key): max(0, int(value or 0))
                    for key, value in raw_interventions.items()
                }
            self._last_anomaly_root = str(
                summary.get("active_repair_root", "")
            )
            self._last_anomaly_at = float(
                summary.get("last_anomaly_at_epoch", 0.0) or 0.0
            )
        directory = self.root / "snapshots"
        paths = sorted(directory.glob("*.json"))[-16:] if directory.is_dir() else []
        for path in paths:
            payload = read_json(path, default={})
            if not isinstance(payload, Mapping):
                continue
            snapshot = health_snapshot_from_dict(payload)
            if snapshot is None:
                continue
            lease = self.smart_timeout.stage_progress_lease_seconds.get(
                snapshot.stage,
                self.smart_timeout.semantic_stall_seconds,
            )
            self.evaluator.evaluate(
                snapshot,
                progress_lease_seconds=max(60, int(lease)),
            )
            self._sequence = max(self._sequence, snapshot.sequence)
            self._recent_snapshots.append(snapshot.to_dict())
        heartbeat = read_json(self.root / "heartbeat.json", default={})
        previous_terminal = bool(
            isinstance(heartbeat, dict)
            and str(
                heartbeat.get("previous_status")
                or heartbeat.get("status", "")
            )
            in {
                "completed",
                "paused",
                "waiting_user",
                "blocked",
                "failed",
                "stopped",
            }
        )
        if self.evaluator.previous is not None and previous_terminal:
            self.evaluator.last_progress_at = time.time()
            self.evaluator.activity_since_progress = False

    def stop(self, status: str = "stopped", reason: str = "") -> None:
        self._terminal_status = str(status)
        self._terminal_reason = str(reason)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.config.poll_seconds + 1.0))
        self._thread = None
        # Persist terminal state only after the worker has stopped. A final
        # in-flight tick must never overwrite blocked/completed with healthy and
        # make the sidecar kill a live root-cause or self-repair process as stale.
        self._write_heartbeat(status=status, reason=reason)

    def record_control_event(
        self,
        kind: str,
        *,
        stage: str = "",
        task_id: str = "",
        root_fingerprint: str = "",
        action: str = "",
        rewind: bool = False,
    ) -> None:
        value = "|".join(
            (
                str(kind),
                str(stage),
                str(task_id),
                str(root_fingerprint),
                str(action),
            )
        )
        self._control_events.put((value, bool(rewind)))

    def pop_action(self) -> Optional[HealthActionRequest]:
        try:
            return self._actions.get_nowait()
        except queue.Empty:
            return None

    def quiesce_requested(self) -> bool:
        return not self._actions.empty()

    @property
    def summary(self) -> Dict[str, object]:
        return dict(self._last_summary)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._tick()
            except Exception as error:
                self._append_event(
                    {"kind": "health_monitor_error", "detail": str(error)[:1000]}
                )
                self._write_heartbeat(status="degraded", reason=str(error)[:500])
            remaining = max(0.1, self.config.poll_seconds - (time.monotonic() - started))
            self._stop.wait(remaining)

    def _tick(self) -> None:
        self._write_heartbeat(status="checking")
        while True:
            try:
                value, rewind = self._control_events.get_nowait()
            except queue.Empty:
                break
            prior_digest = (
                self.evaluator.previous.progress.digest
                if self.evaluator.previous is not None
                else ""
            )
            value = f"{value}|goal={prior_digest}"
            self.evaluator.record_control(value, rewind=rewind)
            self._append_event(
                {"kind": "control", "fingerprint": value, "rewind": rewind}
            )
        state = load_run_state(self.project_root)
        if state.run_id != self.run_id:
            self._terminal_status = "superseded"
            self._terminal_reason = f"active run changed to {state.run_id}"
            self._write_heartbeat(
                status=self._terminal_status,
                reason=self._terminal_reason,
            )
            self._stop.set()
            return
        self._sequence += 1
        control_fingerprint = _json_hash(list(self.evaluator.control_history))
        snapshot = capture_health_snapshot(
            self.project_root,
            state,
            sequence=self._sequence,
            control_fingerprint=control_fingerprint,
            control_history=tuple(self.evaluator.control_history),
            rewind_epoch=self.evaluator.rewind_epoch,
        )
        self._recent_snapshots.append(snapshot.to_dict())
        lease = self.smart_timeout.stage_progress_lease_seconds.get(
            state.current_stage,
            self.smart_timeout.semantic_stall_seconds,
        )
        anomaly = self.evaluator.evaluate(
            snapshot,
            progress_lease_seconds=max(60, int(lease)),
        )
        self._write_snapshot(snapshot)
        health = "healthy"
        if snapshot.active_tool_count:
            health = "slow"
        if anomaly is not None:
            health = anomaly.kind
            self._handle_anomaly(anomaly, snapshot)
        self._last_summary = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": health,
            "sequence": snapshot.sequence,
            "stage": snapshot.stage,
            "last_progress_at_epoch": self.evaluator.last_progress_at,
            "last_progress_at": (
                datetime.fromtimestamp(
                    self.evaluator.last_progress_at,
                    timezone.utc,
                ).isoformat()
                if self.evaluator.last_progress_at
                else ""
            ),
            "progress_digest": snapshot.progress.digest,
            "activity_digest": snapshot.activity_digest,
            "active_tool_count": snapshot.active_tool_count,
            "active_repair_root": self._last_anomaly_root,
            "last_anomaly_at_epoch": self._last_anomaly_at,
            "interventions": dict(self._interventions),
            "updated_at": utc_now(),
        }
        _atomic_json(self.root / "summary.json", self._last_summary)
        self._write_heartbeat(status=health)

    def _handle_anomaly(
        self,
        anomaly: HealthAnomaly,
        snapshot: HealthSnapshot,
    ) -> None:
        root = anomaly.root_fingerprint
        count = self._interventions.get(root, 0)
        exhausted = count >= self.config.max_interventions_per_root
        active_case = RepairCaseStore(
            self.project_root, self.run_id
        ).latest_open()
        if (
            active_case is not None
            and active_case.root_fingerprint == root
            and active_case.status
            in {
                "open",
                "self_repair",
                "self_repairing",
                "resuming",
                "boundary_verified",
            }
        ):
            return
        cooldown = max(60.0, float(self.config.poll_seconds) * 4.0)
        if (
            root == self._last_anomaly_root
            and time.time() - self._last_anomaly_at < cooldown
        ) or count > self.config.max_interventions_per_root:
            return
        self._last_anomaly_root = root
        self._last_anomaly_at = time.time()
        self._append_event({"kind": "anomaly", "anomaly": anomaly.to_dict()})
        repair_case = RepairCase(
            case_id=uuid.uuid4().hex[:12],
            run_id=self.run_id,
            source="health_watch",
            kind=anomaly.kind,
            severity=anomaly.severity,
            stage=anomaly.stage,
            task_id=anomaly.task_id,
            failure_scope=anomaly.failure_scope,
            symptom=anomaly.reason,
            fingerprint=root,
            root_fingerprint=root,
            progress_before=snapshot.progress.to_dict(),
            progress_history=list(self._recent_snapshots),
            activity_history=[
                {
                    "observed_at": snapshot.observed_at,
                    "activity_digest": snapshot.activity_digest,
                }
            ],
            evidence_refs=[
                str(self.root / "summary.json"),
                str(self.root / "events.jsonl"),
                str(self.root / "snapshots" / f"{snapshot.sequence:08d}.json"),
            ],
            expected_postconditions=list(anomaly.expected_postconditions),
            status=("needs_human" if exhausted else "open"),
        )
        RepairCaseStore(self.project_root, self.run_id).save(repair_case)
        if self.autonomy_mode == "off":
            return
        self._interventions[root] = count + 1
        self._actions.put(
            HealthActionRequest(
                action=("exhausted" if exhausted else "diagnose"),
                anomaly=anomaly,
                repair_case_id=repair_case.case_id,
            )
        )

    def _write_snapshot(self, snapshot: HealthSnapshot) -> None:
        directory = self.root / "snapshots"
        path = directory / f"{snapshot.sequence:08d}.json"
        _atomic_json(path, snapshot.to_dict())
        paths = sorted(directory.glob("*.json"))
        protected: set[str] = set()
        cases_root = run_path(self.project_root, self.run_id) / "repair-cases"
        if cases_root.is_dir():
            for case_path in cases_root.glob("*.json"):
                payload = read_json(case_path, default={})
                if not isinstance(payload, dict):
                    continue
                for reference in payload.get("evidence_refs", []) or []:
                    protected.add(str(Path(str(reference)).resolve()))
        for stale in paths[:-SNAPSHOT_RETENTION]:
            if str(stale.resolve()) in protected:
                continue
            try:
                stale.unlink()
            except OSError:
                pass

    def _write_heartbeat(self, *, status: str, reason: str = "") -> None:
        if self._terminal_status:
            status = self._terminal_status
            reason = self._terminal_reason or reason
        payload = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_token": self.run_token,
            "owner_pid": os.getpid(),
            "owner_start_ticks": process_start_ticks(os.getpid()),
            "status": status,
            "reason": reason,
            "updated_at": utc_now(),
            "updated_epoch": time.time(),
            "active_processes": [asdict(item) for item in ACTIVE_PROCESSES.snapshot()],
        }
        _atomic_json(self.root / "heartbeat.json", payload)

    def _append_event(self, payload: Mapping[str, object]) -> None:
        path = self.root / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_events(path)
        entry = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "observed_at": utc_now(),
            **dict(payload),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _rotate_events(path: Path) -> None:
        try:
            if path.stat().st_size < EVENT_LOG_MAX_BYTES:
                return
        except OSError:
            return
        oldest = path.with_suffix(path.suffix + f".{EVENT_LOG_ROTATIONS}")
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        for index in range(EVENT_LOG_ROTATIONS - 1, 0, -1):
            source = path.with_suffix(path.suffix + f".{index}")
            destination = path.with_suffix(path.suffix + f".{index + 1}")
            if source.exists():
                os.replace(source, destination)
        if path.exists():
            os.replace(path, path.with_suffix(path.suffix + ".1"))


def health_snapshot_from_dict(
    payload: Mapping[str, object],
) -> Optional[HealthSnapshot]:
    raw_progress = payload.get("progress", {})
    if not isinstance(raw_progress, Mapping):
        return None
    progress = ProgressVector(
        durable_atoms=tuple(str(item) for item in raw_progress.get("durable_atoms", [])),
        unresolved_roots=tuple(
            str(item) for item in raw_progress.get("unresolved_roots", [])
        ),
        root_occurrences=tuple(
            (str(item[0]), int(item[1]))
            for item in raw_progress.get("root_occurrences", [])
            if isinstance(item, (list, tuple)) and len(item) == 2
        ),
        completed_stages=tuple(
            str(item) for item in raw_progress.get("completed_stages", [])
        ),
        done_lineages=tuple(
            str(item) for item in raw_progress.get("done_lineages", [])
        ),
        verified_proofs=tuple(
            str(item) for item in raw_progress.get("verified_proofs", [])
        ),
    )
    return HealthSnapshot(
        sequence=int(payload.get("sequence", 0) or 0),
        observed_at=str(payload.get("observed_at", "")),
        observed_epoch=float(payload.get("observed_epoch", 0.0) or 0.0),
        run_id=str(payload.get("run_id", "")),
        run_status=str(payload.get("run_status", "pending")),
        stage=str(payload.get("stage", "")),
        task_id=str(payload.get("task_id", "")),
        progress=progress,
        activity_digest=str(payload.get("activity_digest", "")),
        head_ref=str(payload.get("head_ref", "")),
        worktree_fingerprint=str(payload.get("worktree_fingerprint", "")),
        active_tool_count=int(payload.get("active_tool_count", 0) or 0),
        active_tool=str(payload.get("active_tool", "")),
        retry_pressure=int(payload.get("retry_pressure", 0) or 0),
        control_fingerprint=str(payload.get("control_fingerprint", "")),
        control_history=tuple(
            str(item) for item in payload.get("control_history", []) or []
        ),
        rewind_epoch=int(payload.get("rewind_epoch", 0) or 0),
    )


def replay_health_events(
    snapshots: Sequence[Mapping[str, object]],
    *,
    config: Optional[HealthWatchConfig] = None,
    progress_lease_seconds: float = 60.0,
) -> List[HealthAnomaly]:
    """Replay serialized snapshots for deterministic repair boundary tests."""
    evaluator = RunHealthEvaluator(config or HealthWatchConfig())
    anomalies: List[HealthAnomaly] = []
    for payload in snapshots:
        snapshot = health_snapshot_from_dict(payload)
        if snapshot is None:
            continue
        anomaly = evaluator.evaluate(
            snapshot, progress_lease_seconds=progress_lease_seconds
        )
        if anomaly is not None:
            anomalies.append(anomaly)
    return anomalies
