from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import run_path
from .io_utils import read_json, write_json
from .models import AgentResult, CommandResult, RunState


INCIDENT_SCHEMA_VERSION = 5
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


@dataclass
class IncidentDiagnosis:
    owner: str
    action: str
    confidence: float
    reason: str
    evidence: List[str] = field(default_factory=list)
    cause_status: str = "unknown"
    source: str = "deterministic"

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
    if result.infrastructure_failure_id:
        kind = "gate_reported_infrastructure_error"
    elif result.infrastructure_error:
        kind = "gate_infrastructure_error"
    else:
        kind = "gate_stall" if result.termination_reason == "stalled" else "gate_timeout"
    if (
        not result.infrastructure_error
        and result.termination_reason not in {"timeout", "stalled"}
    ):
        kind = f"gate_{result.termination_reason or 'abnormal_exit'}"
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
        root_cause_fingerprint=incident_fp,
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
    diagnosis = IncidentDiagnosis(
        owner=str(payload.get("owner", "unknown")),
        action=str(payload.get("action", "ASK_USER")).upper(),
        confidence=float(payload.get("confidence", 0.0)),
        reason=str(payload.get("reason", "")).strip(),
        evidence=[
            str(item).strip() for item in payload.get("evidence", [])
            if str(item).strip()
        ],
        cause_status=str(payload.get("cause_status", "unknown")).strip().lower(),
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
