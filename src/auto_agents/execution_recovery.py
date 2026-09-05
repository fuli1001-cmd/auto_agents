from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from .config import run_path
from .io_utils import read_json, write_json
from .models import AgentResult, CommandResult, RunState


INCIDENT_SCHEMA_VERSION = 5
PARALLEL_LANE_FAILURE_SCHEMA_VERSION = 1
INCIDENT_ACTIONS = {
    "RETRY",
    "RECOVER_TARGET",
    "REPAIR_INFRASTRUCTURE",
    "REWIND_PLAN",
    "REWIND_CLARIFY",
    "SELF_REPAIR",
    "ASK_USER",
    "STOP",
}
INCIDENT_OWNERS = {
    "target_project",
    "verification_contract",
    "verification_infrastructure",
    "execution_environment",
    "requirements",
    "external_provider",
    "auto_agents",
    "user_input",
    "unknown",
}
BASELINE_FAILURE_IDENTITY_INCIDENT_KIND = "gate_baseline_failure_identity_unresolved"
BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY = "baseline_failure_identity"
CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND = (
    "gate_current_verification_contract_invalid"
)
CURRENT_VERIFICATION_CONTRACT_SNAPSHOT_KEY = "current_verification_contract"
_WATCH_PATTERN = re.compile(
    r"(?:^|\s)(?:--watch(?:All)?\b|watch\b|pytest-watch\b|vitest(?!\s+run\b)(?:\s+watch)?\b)",
    re.IGNORECASE,
)
_SECRET_PATTERN = re.compile(
    r"(?i)(token|password|secret|api[_-]?key)(\s*[:=]\s*)([^\s]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s]+)")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: object, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"[truncated]\n{text[-limit:]}"


def redact_incident_text(value: object) -> str:
    text = _SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        str(value or ""),
    )
    return _BEARER_PATTERN.sub(lambda match: f"{match.group(1)}<redacted>", text)


def _normalized_failure_ids(value: object) -> List[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return sorted(
        {
            str(item).strip()
            for item in value
            if str(item).strip()
        }
    )


@dataclass
class ParallelLaneFailure:
    """Durable failure handoff from an isolated task lane to its collector."""

    task: Dict[str, object]
    operation: str
    owner: str
    automatic_retryable: bool
    resumable: bool
    reason: str
    redacted_evidence: str = ""
    current_failure_ids: List[str] = field(default_factory=list)
    baseline_failure_ids: List[str] = field(default_factory=list)
    new_failure_ids: List[str] = field(default_factory=list)
    owned_failure_ids: List[str] = field(default_factory=list)
    failure_class: str = ""
    baseline_comparison_comparable: bool = True
    base_ref: str = ""
    checkpoint: Dict[str, object] = field(default_factory=dict)
    command_incident: Dict[str, object] = field(default_factory=dict)
    implementation_completed: bool = False
    created_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ParallelLaneFailure":
        raw_task = data.get("task", {})
        raw_checkpoint = data.get("checkpoint", {})
        raw_incident = data.get("command_incident", {})
        owner = str(data.get("owner", "unknown")).strip() or "unknown"
        if owner not in INCIDENT_OWNERS:
            owner = "unknown"
        return cls(
            task=dict(raw_task) if isinstance(raw_task, Mapping) else {},
            operation=str(data.get("operation", "unknown")).strip() or "unknown",
            owner=owner,
            automatic_retryable=bool(data.get("automatic_retryable", False)),
            resumable=bool(data.get("resumable", True)),
            reason=redact_incident_text(data.get("reason", "")).strip(),
            redacted_evidence=_compact(
                redact_incident_text(data.get("redacted_evidence", ""))
            ),
            current_failure_ids=_normalized_failure_ids(
                data.get("current_failure_ids", [])
            ),
            baseline_failure_ids=_normalized_failure_ids(
                data.get("baseline_failure_ids", [])
            ),
            new_failure_ids=_normalized_failure_ids(
                data.get("new_failure_ids", [])
            ),
            owned_failure_ids=_normalized_failure_ids(
                data.get("owned_failure_ids", [])
            ),
            failure_class=str(data.get("failure_class", "")).strip(),
            baseline_comparison_comparable=bool(
                data.get("baseline_comparison_comparable", True)
            ),
            base_ref=str(data.get("base_ref", "")).strip(),
            checkpoint=(
                dict(raw_checkpoint)
                if isinstance(raw_checkpoint, Mapping)
                else {}
            ),
            command_incident=(
                dict(raw_incident) if isinstance(raw_incident, Mapping) else {}
            ),
            implementation_completed=bool(
                data.get("implementation_completed", False)
            ),
            created_at=str(data.get("created_at", "")).strip() or _utc_now(),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": PARALLEL_LANE_FAILURE_SCHEMA_VERSION,
            "kind": "parallel_lane_failure",
            "task": dict(self.task),
            "operation": self.operation,
            "owner": self.owner,
            "automatic_retryable": self.automatic_retryable,
            "resumable": self.resumable,
            "reason": redact_incident_text(self.reason).strip(),
            "redacted_evidence": _compact(
                redact_incident_text(self.redacted_evidence)
            ),
            "current_failure_ids": _normalized_failure_ids(
                self.current_failure_ids
            ),
            "baseline_failure_ids": _normalized_failure_ids(
                self.baseline_failure_ids
            ),
            "new_failure_ids": _normalized_failure_ids(self.new_failure_ids),
            "owned_failure_ids": _normalized_failure_ids(
                self.owned_failure_ids
            ),
            "failure_class": self.failure_class,
            "baseline_comparison_comparable": (
                self.baseline_comparison_comparable
            ),
            "base_ref": self.base_ref,
            "checkpoint": dict(self.checkpoint),
            "command_incident": dict(self.command_incident),
            "implementation_completed": self.implementation_completed,
            "created_at": self.created_at,
        }


@dataclass
class IncidentDiagnosis:
    owner: str
    action: str
    confidence: float
    reason: str
    evidence: List[str] = field(default_factory=list)
    cause_status: str = "unknown"
    source: str = "deterministic"
    failure_domain: str = "unknown"
    mutation_domain: str = "unknown"
    expected_postconditions: List[str] = field(default_factory=list)

    def valid(self) -> bool:
        return (
            self.owner in INCIDENT_OWNERS
            and self.action in INCIDENT_ACTIONS
            and 0.0 <= float(self.confidence) <= 1.0
            and bool(self.reason.strip())
            and self.cause_status in {"confirmed", "suspected", "unknown"}
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ExecutionIncident:
    incident_id: str
    run_id: str
    source: str
    kind: str
    stage: str
    context: str
    command: str = ""
    task_id: str = ""
    baseline: bool = False
    termination_reason: str = ""
    returncode: int = 0
    elapsed_seconds: float = 0.0
    timeout_seconds: float = 0.0
    idle_timeout_seconds: float = 0.0
    last_activity_seconds: float = 0.0
    activity_kind: str = ""
    cleanup_incomplete: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    infrastructure_cause_id: str = ""
    cause_status: str = "unknown"
    runtime_profile: str = ""
    process_snapshot: Dict[str, object] = field(default_factory=dict)
    head_ref: str = ""
    worktree_fingerprint: str = ""
    incident_fingerprint: str = ""
    root_incident_id: str = ""
    root_cause_fingerprint: str = ""
    origin_command: str = ""
    evidence_fingerprint: str = ""
    budget_epoch: int = 0
    occurrence_count: int = 1
    recovery_round: int = 0
    status: str = "open"
    diagnosis: Dict[str, object] = field(default_factory=dict)
    repair_history: List[Dict[str, object]] = field(default_factory=list)
    recovery_policy_version: int = INCIDENT_SCHEMA_VERSION
    history: List[Dict[str, object]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ExecutionIncident":
        fields = cls.__dataclass_fields__
        values = {key: value for key, value in data.items() if key in fields}
        if "recovery_policy_version" not in values:
            values["recovery_policy_version"] = 1
        return cls(**values)

    def to_dict(self) -> Dict[str, object]:
        return {"schema_version": INCIDENT_SCHEMA_VERSION, **asdict(self)}

    def summary(self) -> Dict[str, object]:
        return {
            "incident_id": self.incident_id,
            "source": self.source,
            "kind": self.kind,
            "stage": self.stage,
            "context": self.context,
            "task_id": self.task_id,
            "incident_fingerprint": self.incident_fingerprint,
            "root_incident_id": self.root_incident_id,
            "root_cause_fingerprint": self.root_cause_fingerprint,
            "origin_command": self.origin_command,
            "evidence_fingerprint": self.evidence_fingerprint,
            "budget_epoch": self.budget_epoch,
            "occurrence_count": self.occurrence_count,
            "recovery_round": self.recovery_round,
            "status": self.status,
            "diagnosis": dict(self.diagnosis),
            "repair_history": list(self.repair_history),
            "recovery_policy_version": self.recovery_policy_version,
            "updated_at": self.updated_at,
        }


class ExecutionIncidentStore:
    def __init__(self, project_root: Path, run_id: str) -> None:
        self.project_root = project_root.resolve()
        self.run_id = str(run_id)
        self.root = run_path(self.project_root, self.run_id) / "recovery_incidents"

    def path(self, incident_id: str) -> Path:
        return self.root / f"{incident_id}.json"

    def load(self, incident_id: str) -> Optional[ExecutionIncident]:
        payload = read_json(self.path(incident_id), default={})
        if not isinstance(payload, dict) or not payload.get("incident_id"):
            return None
        try:
            return ExecutionIncident.from_dict(payload)
        except (TypeError, ValueError):
            return None

    def save(self, incident: ExecutionIncident, state: RunState) -> None:
        incident.updated_at = _utc_now()
        write_json(self.path(incident.incident_id), incident.to_dict())
        summaries = [
            entry for entry in state.execution_incidents
            if str(entry.get("incident_id", "")) != incident.incident_id
        ]
        summaries.append(incident.summary())
        state.execution_incidents = summaries[-50:]
        state.active_execution_incident_id = (
            "" if incident.status == "resolved" else incident.incident_id
        )

    def active(self, state: RunState) -> Optional[ExecutionIncident]:
        incident_id = state.active_execution_incident_id.strip()
        incident = self.load(incident_id) if incident_id else None
        if (
            incident is not None
            and incident.kind == "gate_reported_infrastructure_error"
            and incident.status == "needs_human"
            and incident.recovery_policy_version < INCIDENT_SCHEMA_VERSION
        ):
            # Policy v4 could consume a round by re-verifying an already
            # in-progress recovery task without running implementation again.
            # Return that ineffective final round to the incident budget once
            # when upgrading the persisted incident.
            previous_policy = incident.recovery_policy_version
            if previous_policy < 5 and incident.recovery_round > 0:
                incident.recovery_round -= 1
            incident.status = "open"
            incident.recovery_policy_version = INCIDENT_SCHEMA_VERSION
            incident.history.append(
                {
                    "event": "legacy_reopen",
                    "previous_policy_version": previous_policy,
                    "reason": (
                        "policy v5 requires a fresh implementation attempt for every "
                        "managed target recovery round"
                    ),
                }
            )
            self.save(incident, state)
        return incident


def _fingerprint_payload(payload: Dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def command_incident(
    *,
    run_id: str,
    stage: str,
    context: str,
    result: CommandResult,
    baseline: bool = False,
    task_id: str = "",
    head_ref: str = "",
    worktree_fingerprint: str = "",
    idle_timeout_seconds: float = 0.0,
) -> ExecutionIncident:
    command = redact_incident_text(result.command)
    stdout_tail = _compact(redact_incident_text(result.stdout))
    stderr_tail = _compact(redact_incident_text(result.stderr))
    combined_output = f"{stdout_tail}\n{stderr_tail}".lower()
    infrastructure_cause_id = ""
    cause_status = "unknown"
    if any(
        token in combined_output
        for token in ("socket path too long", "singletonsocket", "enametoolong")
    ):
        infrastructure_cause_id = "unix_socket_path_too_long"
        cause_status = "confirmed"
    repair_history = [
        dict(item)
        for item in result.infrastructure_attempts
        if str(item.get("event", "")) == "managed_infrastructure_repair"
    ]
    runtime_profile = next(
        (
            str(item.get("runtime_profile", ""))
            for item in reversed(repair_history)
            if str(item.get("runtime_profile", ""))
        ),
        "",
    )
    baseline_identity = result.process_snapshot.get(
        BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY,
        {},
    )
    baseline_identity_unresolved = bool(
        baseline
        and isinstance(baseline_identity, dict)
        and str(baseline_identity.get("status", "")).strip().lower()
        == "unresolved"
    )
    current_contract = result.process_snapshot.get(
        CURRENT_VERIFICATION_CONTRACT_SNAPSHOT_KEY,
        {},
    )
    current_contract_invalid = bool(
        not baseline
        and isinstance(current_contract, dict)
        and str(current_contract.get("status", "")).strip().lower()
        == "target_not_found"
    )
    if baseline_identity_unresolved:
        kind = BASELINE_FAILURE_IDENTITY_INCIDENT_KIND
    elif current_contract_invalid:
        kind = CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND
    elif result.infrastructure_failure_id:
        kind = "gate_reported_infrastructure_error"
    elif result.infrastructure_error:
        kind = "gate_infrastructure_error"
    else:
        kind = "gate_stall" if result.termination_reason == "stalled" else "gate_timeout"
    if (
        not baseline_identity_unresolved
        and not current_contract_invalid
        and not result.infrastructure_error
        and result.termination_reason not in {"timeout", "stalled"}
    ):
        kind = f"gate_{result.termination_reason or 'abnormal_exit'}"
    if baseline_identity_unresolved or current_contract_invalid:
        cause_status = "confirmed"
    identity = {
        "source": "gate",
        "kind": kind,
        "stage": stage,
        "command": " ".join(command.split()),
        "termination_reason": result.termination_reason,
        "infrastructure_failure_id": result.infrastructure_failure_id,
        "infrastructure_cause_id": infrastructure_cause_id,
    }
    if kind != "gate_reported_infrastructure_error":
        identity["context"] = context
    incident_fp = _fingerprint_payload(identity)
    root_identity = {
        "source": "gate",
        "kind": kind,
        "stage": stage,
        "command": " ".join(command.split()),
        "termination_reason": result.termination_reason,
        "infrastructure_failure_id": result.infrastructure_failure_id,
        "infrastructure_cause_id": infrastructure_cause_id,
    }
    root_cause_fp = _fingerprint_payload(root_identity)
    evidence_fp = _fingerprint_payload(
        {
            "incident": incident_fp,
            "output": _fingerprint_payload(
                {"stdout": stdout_tail, "stderr": stderr_tail}
            ),
            "head": head_ref,
            "worktree": worktree_fingerprint,
            "process": result.process_snapshot,
            "infrastructure_attempts": result.infrastructure_attempts,
        }
    )
    return ExecutionIncident(
        incident_id=uuid.uuid4().hex[:12],
        run_id=run_id,
        source="gate",
        kind=kind,
        stage=stage,
        context=context,
        command=command,
        task_id=task_id,
        baseline=baseline,
        termination_reason=result.termination_reason,
        returncode=result.returncode,
        elapsed_seconds=result.duration_seconds,
        timeout_seconds=result.timeout_seconds,
        idle_timeout_seconds=float(idle_timeout_seconds),
        last_activity_seconds=result.last_activity_seconds,
        activity_kind=result.activity_kind,
        cleanup_incomplete=result.cleanup_incomplete,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        infrastructure_cause_id=infrastructure_cause_id,
        cause_status=cause_status,
        runtime_profile=runtime_profile,
        process_snapshot={
            **dict(result.process_snapshot),
            "infrastructure_failure_id": result.infrastructure_failure_id,
            "infrastructure_attempts": list(result.infrastructure_attempts),
        },
        head_ref=head_ref,
        worktree_fingerprint=worktree_fingerprint,
        incident_fingerprint=incident_fp,
        root_cause_fingerprint=root_cause_fp,
        origin_command=command,
        evidence_fingerprint=evidence_fp,
        repair_history=repair_history,
    )


def provider_incident(
    *,
    run_id: str,
    stage: str,
    provider: str,
    result: AgentResult,
    head_ref: str = "",
    worktree_fingerprint: str = "",
) -> Optional[ExecutionIncident]:
    termination = result.termination
    if termination is None:
        return None
    reason = termination.reason
    stderr_tail = _compact(redact_incident_text(result.stderr or result.summary))
    incident_fp = _fingerprint_payload(
        {
            "source": "provider",
            "provider": provider,
            "stage": stage,
            "reason": reason,
            "active_tool": termination.active_tool,
        }
    )
    evidence_fp = _fingerprint_payload(
        {
            "incident": incident_fp,
            "output": _fingerprint_payload({"stderr": stderr_tail}),
            "head": head_ref,
            "worktree": worktree_fingerprint,
        }
    )
    return ExecutionIncident(
        incident_id=uuid.uuid4().hex[:12],
        run_id=run_id,
        source="provider",
        kind=f"provider_{reason}",
        stage=stage,
        context=f"provider:{provider}",
        termination_reason=reason,
        returncode=result.returncode,
        elapsed_seconds=termination.elapsed_seconds,
        last_activity_seconds=termination.last_provider_activity_seconds,
        activity_kind=termination.active_tool,
        stderr_tail=stderr_tail,
        head_ref=head_ref,
        worktree_fingerprint=worktree_fingerprint,
        incident_fingerprint=incident_fp,
        evidence_fingerprint=evidence_fp,
    )


def baseline_identity_is_immutable_only(incident: ExecutionIncident) -> bool:
    marker = incident.process_snapshot.get(
        BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY,
        {},
    )
    return bool(
        isinstance(marker, dict)
        and marker.get("immutable_baseline_only") is True
    )


def deterministic_diagnosis(incident: ExecutionIncident) -> Optional[IncidentDiagnosis]:
    if incident.cleanup_incomplete:
        return IncidentDiagnosis(
            owner="unknown",
            action="STOP",
            confidence=1.0,
            reason="process-group cleanup is incomplete; recovery cannot mutate safely",
            evidence=["cleanup_incomplete=true"],
        )
    if incident.source == "provider":
        return IncidentDiagnosis(
            owner="external_provider",
            action="RETRY",
            confidence=0.95,
            reason="provider supervision termination uses the existing resume/failover route",
            evidence=[f"termination_reason={incident.termination_reason}"],
        )
    worker_allocation = incident.process_snapshot.get("worker_allocation", {})
    if (
        incident.kind == "gate_infrastructure_error"
        and isinstance(worker_allocation, dict)
        and str(worker_allocation.get("status", "")).strip()
    ):
        reason = redact_incident_text(
            worker_allocation.get("user_message", "") or incident.stderr_tail
        ).strip()
        workers = worker_allocation.get("workers", [])
        evidence = (
            [
                (
                    f"worker={item.get('worker_id', 'unknown')} "
                    f"status={item.get('status', 'unknown')} "
                    "reasons="
                    + ", ".join(
                        str(value) for value in item.get("reasons", [])
                    )
                )
                for item in workers
                if isinstance(item, dict)
            ]
            if isinstance(workers, list)
            else []
        )
        return IncidentDiagnosis(
            owner="verification_infrastructure",
            action="REPAIR_INFRASTRUCTURE",
            confidence=1.0,
            reason=reason or "No eligible worker can run the verification command.",
            evidence=evidence or [
                f"worker_allocation_status={worker_allocation.get('status')}"
            ],
            cause_status="confirmed",
            failure_domain="worker_pool",
            mutation_domain="execution_environment",
            expected_postconditions=[
                "at least one worker has the required total slots and capabilities",
                "worker connectivity and runtime probes succeed before the gate is rerun",
            ],
        )
    baseline_identity = incident.process_snapshot.get(
        BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY,
        {},
    )
    if incident.kind == BASELINE_FAILURE_IDENTITY_INCIDENT_KIND:
        if not baseline_identity_is_immutable_only(incident):
            return None
        contract = (
            str(baseline_identity.get("contract", "")).strip()
            if isinstance(baseline_identity, dict)
            else ""
        )
        return IncidentDiagnosis(
            owner="auto_agents",
            action="SELF_REPAIR",
            confidence=1.0,
            reason=(
                "the immutable verification baseline failed without a stable "
                "semantic identity; changing current target HEAD cannot affect "
                "that baseline and must not be routed to target recovery"
            ),
            evidence=[f"contract={contract or 'unspecified'}"],
            cause_status="confirmed",
            failure_domain="baseline_snapshot",
            mutation_domain="auto_agents_engine",
            expected_postconditions=[
                "baseline execution yields stable identities or typed not-applicable",
                "current target HEAD is not changed merely to repair an immutable baseline",
            ],
        )
    current_contract = incident.process_snapshot.get(
        CURRENT_VERIFICATION_CONTRACT_SNAPSHOT_KEY,
        {},
    )
    if (
        incident.kind == CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND
        and isinstance(current_contract, dict)
        and str(current_contract.get("status", "")).strip().lower()
        == "target_not_found"
    ):
        baseline_observation = current_contract.get("baseline_observation", {})
        baseline_status = (
            str(baseline_observation.get("status", "")).strip()
            if isinstance(baseline_observation, dict)
            else ""
        )
        return IncidentDiagnosis(
            owner="verification_contract",
            action="RECOVER_TARGET",
            confidence=1.0,
            reason=(
                "the exact current pytest command references a target that does "
                "not resolve in the current project snapshot"
            ),
            evidence=[
                "current_selector_status=target_not_found",
                f"baseline_selector_status={baseline_status or 'not_observed'}",
            ],
            cause_status="confirmed",
            failure_domain="current_verification_contract",
            mutation_domain="target_project",
            expected_postconditions=[
                "the configured current verification target resolves exactly",
                "the retained source-task worktree is preserved during recovery",
            ],
        )
    marker = incident.process_snapshot.get("reported_infrastructure_marker", {})
    repair_scope = (
        str(marker.get("repair_scope", "")).strip().lower()
        if isinstance(marker, dict)
        else ""
    )
    if (
        incident.kind == "gate_reported_infrastructure_error"
        and repair_scope in {"target_project", "verification_contract"}
    ):
        return IncidentDiagnosis(
            owner=repair_scope,
            action="RECOVER_TARGET",
            confidence=1.0,
            reason=(
                "the standard infrastructure marker explicitly assigns the repair "
                f"surface to {repair_scope}"
            ),
            evidence=[f"repair_scope={repair_scope}"],
            cause_status="confirmed",
        )
    if (
        incident.kind == "gate_reported_infrastructure_error"
        and repair_scope == "execution_environment"
    ):
        return IncidentDiagnosis(
            owner="execution_environment",
            action="REPAIR_INFRASTRUCTURE",
            confidence=1.0,
            reason=(
                "the standard infrastructure marker explicitly assigns the repair "
                "surface to the execution environment"
            ),
            evidence=["repair_scope=execution_environment"],
            cause_status="confirmed",
        )
    if incident.termination_reason == "launch_error":
        return IncidentDiagnosis(
            owner="verification_contract",
            action="REWIND_PLAN",
            confidence=0.9,
            reason="the configured verification command could not be launched",
            evidence=[incident.stderr_tail or "launch_error"],
        )
    if _WATCH_PATTERN.search(incident.command):
        return IncidentDiagnosis(
            owner="verification_contract",
            action="REWIND_PLAN",
            confidence=0.95,
            reason="verification command appears to use a non-terminating watch mode",
            evidence=[incident.command],
        )
    if incident.baseline and incident.termination_reason in {"timeout", "stalled"}:
        return IncidentDiagnosis(
            owner="target_project",
            action="RECOVER_TARGET",
            confidence=0.85,
            reason="a clean-head baseline command stalled and requires pre-baseline repair",
            evidence=[
                f"context={incident.context}",
                f"termination_reason={incident.termination_reason}",
            ],
        )
    if incident.termination_reason in {"timeout", "stalled"}:
        return IncidentDiagnosis(
            owner="target_project",
            action="RECOVER_TARGET",
            confidence=0.8,
            reason="a target-project verification command exceeded its progress budget",
            evidence=[f"context={incident.context}"],
        )
    return None


def parse_incident_diagnosis(raw: str) -> IncidentDiagnosis:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("incident diagnosis must be a JSON object")
    raw_reason = payload.get("reason", "")
    nested_cause_status = ""
    if isinstance(raw_reason, Mapping):
        nested_cause_status = (
            str(raw_reason.get("cause_status", "")).strip().lower()
        )
        reason = next(
            (
                str(raw_reason.get(key, "")).strip()
                for key in ("causation", "reason", "message", "summary")
                if str(raw_reason.get(key, "")).strip()
            ),
            json.dumps(raw_reason, ensure_ascii=False, sort_keys=True),
        )
    else:
        reason = str(raw_reason).strip()
    raw_evidence = payload.get("evidence", [])
    evidence: List[str] = []
    if isinstance(raw_evidence, Mapping):
        for group, values in raw_evidence.items():
            if isinstance(values, list):
                evidence.extend(
                    f"{group}: {str(item).strip()}"
                    for item in values
                    if str(item).strip()
                )
            elif str(values).strip():
                evidence.append(f"{group}: {str(values).strip()}")
    elif isinstance(raw_evidence, list):
        evidence = [
            str(item).strip() for item in raw_evidence if str(item).strip()
        ]
    elif str(raw_evidence).strip():
        evidence = [str(raw_evidence).strip()]
    cause_status = str(payload.get("cause_status", "")).strip().lower()
    if not cause_status:
        cause_status = nested_cause_status or "unknown"
    diagnosis = IncidentDiagnosis(
        owner=str(payload.get("owner", "unknown")),
        action=str(payload.get("action", "ASK_USER")).upper(),
        confidence=float(payload.get("confidence", 0.0)),
        reason=reason,
        evidence=evidence,
        cause_status=cause_status,
        source="provider",
    )
    if not diagnosis.valid():
        raise ValueError("incident diagnosis contains an invalid owner, action, confidence, or reason")
    return diagnosis


def recovery_task_marker(
    incident_id: str,
    command: str,
    *,
    recovery_round: int = 0,
) -> Dict[str, object]:
    return {
        "kind": "execution_incident",
        "execution_incident_id": incident_id,
        "verification_command": redact_incident_text(command),
        "initial_recovery_round": max(0, int(recovery_round)),
        "result": "scheduled",
    }


def is_execution_incident_recovery_task(task: object) -> bool:
    history = getattr(task, "recovery_history", [])
    return any(
        isinstance(entry, dict)
        and str(entry.get("kind", "")) == "execution_incident"
        and bool(str(entry.get("execution_incident_id", "")).strip())
        for entry in history
    )


def latest_execution_incident_id(entries: Iterable[Dict[str, object]]) -> str:
    for entry in reversed(list(entries)):
        incident_id = str(entry.get("incident_id", "")).strip()
        if incident_id:
            return incident_id
    return ""
