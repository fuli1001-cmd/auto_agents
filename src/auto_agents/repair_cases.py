from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .config import run_path
from .io_utils import read_json


REPAIR_CASE_SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_repair_fingerprint(*parts: object) -> str:
    normalized = "\0".join(" ".join(str(part or "").split()) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:24]


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


@dataclass
class RepairCase:
    case_id: str
    run_id: str
    source: str
    kind: str
    severity: str
    status: str = "open"
    stage: str = ""
    task_id: str = ""
    failure_scope: str = "run"
    symptom: str = ""
    fingerprint: str = ""
    root_fingerprint: str = ""
    execution_incident_id: str = ""
    progress_before: Dict[str, object] = field(default_factory=dict)
    progress_history: List[Dict[str, object]] = field(default_factory=list)
    activity_history: List[Dict[str, object]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    expected_postconditions: List[str] = field(default_factory=list)
    postcondition_claims: List[Dict[str, object]] = field(default_factory=list)
    postcondition_receipts: List[Dict[str, object]] = field(default_factory=list)
    authorization_policy: Dict[str, object] = field(default_factory=dict)
    owner_hint: str = "unknown"
    resume_checkpoint_ref: str = ""
    history: List[Dict[str, object]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.case_id:
            self.case_id = uuid.uuid4().hex[:12]
        if not self.fingerprint:
            self.fingerprint = stable_repair_fingerprint(
                self.source,
                self.kind,
                self.stage,
                self.task_id,
                self.symptom,
            )
        if not self.root_fingerprint:
            self.root_fingerprint = self.fingerprint

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RepairCase":
        fields = cls.__dataclass_fields__
        values = {key: value for key, value in payload.items() if key in fields}
        for key in ("postcondition_claims", "postcondition_receipts"):
            raw = values.get(key, [])
            values[key] = [
                dict(item)
                for item in (raw if isinstance(raw, list) else [])
                if isinstance(item, Mapping)
            ]
        raw_policy = values.get("authorization_policy", {})
        values["authorization_policy"] = (
            dict(raw_policy) if isinstance(raw_policy, Mapping) else {}
        )
        return cls(**values)  # type: ignore[arg-type]

    def to_dict(self) -> Dict[str, object]:
        return {"schema_version": REPAIR_CASE_SCHEMA_VERSION, **asdict(self)}


class RepairCaseStore:
    def __init__(self, project_root: Path, run_id: str) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.run_id = str(run_id)
        self.root = run_path(self.project_root, self.run_id) / "repair-cases"

    def path(self, case_id: str) -> Path:
        return self.root / f"{case_id}.json"

    def save(self, repair_case: RepairCase) -> None:
        repair_case.updated_at = _utc_now()
        _atomic_json(self.path(repair_case.case_id), repair_case.to_dict())

    def load(self, case_id: str) -> Optional[RepairCase]:
        payload = read_json(self.path(case_id), default={})
        if not isinstance(payload, dict) or not payload.get("case_id"):
            return None
        try:
            return RepairCase.from_dict(payload)
        except (TypeError, ValueError):
            return None

    def latest_open(self) -> Optional[RepairCase]:
        if not self.root.is_dir():
            return None
        matches: list[tuple[float, RepairCase]] = []
        for path in self.root.glob("*.json"):
            payload = read_json(path, default={})
            if not isinstance(payload, dict) or payload.get("status") in {
                "resolved",
                "dismissed",
            }:
                continue
            try:
                candidate = RepairCase.from_dict(payload)
                matches.append((path.stat().st_mtime, candidate))
            except (OSError, TypeError, ValueError):
                continue
        return max(matches, key=lambda item: item[0])[1] if matches else None


def terminal_repair_case(
    *,
    run_id: str,
    error: object,
    stage: str = "",
    execution_incident_id: str = "",
    owner_hint: str = "unknown",
) -> RepairCase:
    symptom = " ".join(str(error or "").split())
    return RepairCase(
        case_id=uuid.uuid4().hex[:12],
        run_id=run_id,
        source="terminal_error",
        kind=type(error).__name__,
        severity="blocking",
        stage=stage,
        symptom=symptom,
        execution_incident_id=execution_incident_id,
        owner_hint=owner_hint,
    )
