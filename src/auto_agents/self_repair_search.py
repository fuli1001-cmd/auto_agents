from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence

from .config import run_path
from .io_utils import read_json


SELF_REPAIR_EXPERIMENT_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(*parts: object, length: int = 24) -> str:
    payload = "\0".join(
        json.dumps(part, sort_keys=True, ensure_ascii=False, default=str)
        for part in parts
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:length]


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


def safe_repair_root(value: object) -> str:
    return (
        re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "repair")).strip("-")
        or "repair"
    )


@dataclass
class SelfRepairFinding:
    finding_id: str
    status: str = "observed"
    severity: str = "repairable"
    obligation_id: str = ""
    reason: str = ""
    counterexample: str = ""
    required_test: str = ""
    defer_until: str = ""
    evidence: list[str] = field(default_factory=list)
    introduced_by: str = ""
    resolved_by: str = ""
    reopened_by: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SelfRepairFinding":
        raw_evidence = payload.get("evidence", [])
        raw_reopened = payload.get("reopened_by", [])
        defer_until = str(payload.get("defer_until", "")).strip()
        if not defer_until and cls._looks_like_post_full_suite_finding(payload):
            # Migration for findings produced before the review protocol
            # explicitly distinguished code blockers from downstream proof.
            defer_until = "post_full_suite"
        return cls(
            finding_id=str(payload.get("finding_id", "")),
            status=str(payload.get("status", "observed")),
            severity=str(payload.get("severity", "repairable")),
            obligation_id=str(payload.get("obligation_id", "")),
            reason=str(payload.get("reason", "")),
            counterexample=str(payload.get("counterexample", "")),
            required_test=str(payload.get("required_test", "")),
            defer_until=defer_until,
            evidence=[
                str(item)
                for item in (raw_evidence if isinstance(raw_evidence, list) else [])
            ],
            introduced_by=str(payload.get("introduced_by", "")),
            resolved_by=str(payload.get("resolved_by", "")),
            reopened_by=[
                str(item)
                for item in (raw_reopened if isinstance(raw_reopened, list) else [])
            ],
            created_at=str(payload.get("created_at", "")) or _utc_now(),
            updated_at=str(payload.get("updated_at", "")) or _utc_now(),
        )

    @staticmethod
    def _looks_like_post_full_suite_finding(
        payload: Mapping[str, object],
    ) -> bool:
        text = " ".join(
            str(payload.get(key, ""))
            for key in (
                "obligation_id",
                "reason",
                "counterexample",
                "required_test",
            )
        ).casefold()
        return any(
            marker in text
            for marker in (
                "full-suite",
                "full suite",
                "full_suite",
                "candidate regression validation",
                "candidate-regression-proof",
                "candidate-only failure signature",
            )
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class SelfRepairCandidateRecord:
    candidate_id: str
    parent_candidate_id: str = "base"
    parent_ref: str = ""
    candidate_ref: str = ""
    candidate_commit: str = ""
    patch_fingerprint: str = ""
    strategy_fingerprint: str = ""
    status: str = "candidate_failed"
    validation_stage: str = "generation"
    validation_rank: int = 0
    passed_obligations: list[str] = field(default_factory=list)
    failed_obligations: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    resolved_finding_ids: list[str] = field(default_factory=list)
    fatal: bool = False
    infrastructure_failure: bool = False
    diff_line_count: int = 0
    summary: str = ""
    verification: str = ""
    created_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "SelfRepairCandidateRecord":
        fields = cls.__dataclass_fields__
        values = {key: value for key, value in payload.items() if key in fields}
        for key in (
            "passed_obligations",
            "failed_obligations",
            "finding_ids",
            "resolved_finding_ids",
        ):
            raw = values.get(key, [])
            values[key] = [
                str(item) for item in (raw if isinstance(raw, list) else [])
            ]
        return cls(**values)  # type: ignore[arg-type]

    @property
    def vector_fingerprint(self) -> str:
        return _stable_hash(
            sorted(set(self.passed_obligations)),
            sorted(set(self.failed_obligations)),
            sorted(set(self.resolved_finding_ids)),
            self.validation_rank,
            self.fatal,
        )

    def to_dict(self) -> Dict[str, object]:
        return {**asdict(self), "vector_fingerprint": self.vector_fingerprint}


@dataclass
class SelfRepairExperiment:
    experiment_id: str
    run_id: str
    root_fingerprint: str
    category: str
    base_commit: str
    evidence_fingerprint: str = ""
    status: str = "active"
    best_safe_candidate_id: str = "base"
    best_safe_ref: str = ""
    best_search_candidate_id: str = "base"
    best_search_ref: str = ""
    frontier: list[str] = field(default_factory=list)
    candidates: Dict[str, SelfRepairCandidateRecord] = field(default_factory=dict)
    findings: Dict[str, SelfRepairFinding] = field(default_factory=dict)
    obligations: Dict[str, Dict[str, object]] = field(default_factory=dict)
    strategy_history: list[str] = field(default_factory=list)
    attempt_count: int = 0
    consecutive_non_improvements: int = 0
    max_consecutive_non_improvements: int = 3
    max_frontier_candidates: int = 8
    last_progress_kind: str = ""
    current_candidate_id: str = ""
    infrastructure_failures: int = 0
    health_history: list[Dict[str, object]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        root_fingerprint: str,
        category: str,
        base_commit: str,
        expected_postconditions: Iterable[str] = (),
        evidence_fingerprint: str = "",
        max_consecutive_non_improvements: int = 3,
        max_frontier_candidates: int = 8,
    ) -> "SelfRepairExperiment":
        obligations: Dict[str, Dict[str, object]] = {}
        for index, text in enumerate(expected_postconditions, start=1):
            normalized = " ".join(str(text).split())
            if not normalized:
                continue
            obligation_id = f"root:{index}:{_stable_hash(normalized, length=10)}"
            obligations[obligation_id] = {
                "kind": "root_postcondition",
                "status": "open",
                "description": normalized,
                "source": "root_cause_diagnosis",
            }
        for obligation_id, description in {
            "safety:target_untouched": "candidate validation must not modify the target project",
            "safety:tests_not_weakened": "candidate must not delete, skip, or weaken tests",
            "safety:scope_guard": (
                "candidate changes remain within the generic auto_agents repair scope"
            ),
            "validation:focused": "focused candidate verification passes",
            "validation:boundary_replay": "sealed live-boundary replay crosses the original root",
            "validation:diagnosis_differential": (
                "diagnosis-specific base/candidate differential crosses the root boundary"
            ),
            "validation:full_suite": "full-suite differential introduces no new failure",
            "validation:final_review": (
                "proof-aware adversarial review approves the fully validated candidate"
            ),
        }.items():
            obligations.setdefault(
                obligation_id,
                {
                    "kind": "safety" if obligation_id.startswith("safety:") else "validation",
                    "status": "open",
                    "description": description,
                    "source": "engine",
                },
            )
        experiment_id = uuid.uuid4().hex[:12]
        base = SelfRepairCandidateRecord(
            candidate_id="base",
            parent_candidate_id="",
            parent_ref=base_commit,
            candidate_ref=base_commit,
            candidate_commit=base_commit,
            status="base",
            validation_stage="base",
            passed_obligations=[
                "safety:target_untouched",
                "safety:tests_not_weakened",
                "safety:scope_guard",
            ],
        )
        return cls(
            experiment_id=experiment_id,
            run_id=run_id,
            root_fingerprint=root_fingerprint,
            category=category,
            base_commit=base_commit,
            evidence_fingerprint=evidence_fingerprint,
            best_safe_ref=base_commit,
            best_search_ref=base_commit,
            candidates={"base": base},
            obligations=obligations,
            max_consecutive_non_improvements=max(
                1, int(max_consecutive_non_improvements)
            ),
            max_frontier_candidates=max(1, int(max_frontier_candidates)),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SelfRepairExperiment":
        raw_candidates = payload.get("candidates", {})
        raw_findings = payload.get("findings", {})
        raw_obligations = payload.get("obligations", {})
        for name, value in (
            ("candidates", raw_candidates),
            ("findings", raw_findings),
            ("obligations", raw_obligations),
        ):
            if not isinstance(value, Mapping):
                raise ValueError(f"self-repair experiment {name} must be an object")
        for name in ("frontier", "strategy_history", "health_history"):
            if not isinstance(payload.get(name, []), list):
                raise ValueError(f"self-repair experiment {name} must be a list")
        candidates = {
            str(key): SelfRepairCandidateRecord.from_dict(value)
            for key, value in raw_candidates.items()
            if isinstance(value, Mapping)
        }
        findings = {
            str(key): SelfRepairFinding.from_dict(value)
            for key, value in raw_findings.items()
            if isinstance(value, Mapping)
        }
        if "base" not in candidates:
            raise ValueError("self-repair experiment is missing its base candidate")
        return cls(
            experiment_id=str(payload.get("experiment_id", "")),
            run_id=str(payload.get("run_id", "")),
            root_fingerprint=str(payload.get("root_fingerprint", "")),
            category=str(payload.get("category", "")),
            base_commit=str(payload.get("base_commit", "")),
            evidence_fingerprint=str(payload.get("evidence_fingerprint", "")),
            status=str(payload.get("status", "active")),
            best_safe_candidate_id=str(payload.get("best_safe_candidate_id", "base")),
            best_safe_ref=str(payload.get("best_safe_ref", "")),
            best_search_candidate_id=str(payload.get("best_search_candidate_id", "base")),
            best_search_ref=str(payload.get("best_search_ref", "")),
            frontier=[str(item) for item in payload.get("frontier", []) or []],
            candidates=candidates,
            findings=findings,
            obligations={
                str(key): dict(value)
                for key, value in raw_obligations.items()
                if isinstance(value, Mapping)
            },
            strategy_history=[
                str(item) for item in payload.get("strategy_history", []) or []
            ],
            attempt_count=max(0, int(payload.get("attempt_count", 0) or 0)),
            consecutive_non_improvements=max(
                0, int(payload.get("consecutive_non_improvements", 0) or 0)
            ),
            max_consecutive_non_improvements=max(
                1, int(payload.get("max_consecutive_non_improvements", 3) or 3)
            ),
            max_frontier_candidates=max(
                1, int(payload.get("max_frontier_candidates", 8) or 8)
            ),
            last_progress_kind=str(payload.get("last_progress_kind", "")),
            current_candidate_id=str(payload.get("current_candidate_id", "")),
            infrastructure_failures=max(
                0, int(payload.get("infrastructure_failures", 0) or 0)
            ),
            health_history=[
                dict(item)
                for item in payload.get("health_history", []) or []
                if isinstance(item, Mapping)
            ][-64:],
            created_at=str(payload.get("created_at", "")) or _utc_now(),
            updated_at=str(payload.get("updated_at", "")) or _utc_now(),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": SELF_REPAIR_EXPERIMENT_SCHEMA_VERSION,
            **{
                key: value
                for key, value in asdict(self).items()
                if key not in {"candidates", "findings"}
            },
            "candidates": {
                key: value.to_dict() for key, value in self.candidates.items()
            },
            "findings": {
                key: value.to_dict() for key, value in self.findings.items()
            },
        }

    @staticmethod
    def _dominates(
        left: SelfRepairCandidateRecord,
        right: SelfRepairCandidateRecord,
    ) -> bool:
        if left.fatal:
            return False
        left_passed = set(left.passed_obligations)
        right_passed = set(right.passed_obligations)
        left_failed = set(left.failed_obligations)
        right_failed = set(right.failed_obligations)
        no_worse = bool(
            left_passed.issuperset(right_passed)
            and left_failed.issubset(right_failed)
            and left.validation_rank >= right.validation_rank
        )
        strictly_better = bool(
            left_passed != right_passed
            or left_failed != right_failed
            or left.validation_rank > right.validation_rank
        )
        return no_worse and strictly_better

    @staticmethod
    def _search_score(record: SelfRepairCandidateRecord) -> tuple[object, ...]:
        root_passed = sum(
            1 for item in record.passed_obligations if item.startswith("root:")
        )
        safety_failed = sum(
            1 for item in record.failed_obligations if item.startswith("safety:")
        )
        return (
            root_passed,
            -safety_failed,
            len(set(record.resolved_finding_ids)),
            record.validation_rank,
            len(set(record.passed_obligations)),
            -record.diff_line_count,
            record.candidate_id,
        )

    def _recompute_frontier(self) -> bool:
        previous = tuple(self.frontier)
        eligible = [
            record
            for candidate_id, record in self.candidates.items()
            if candidate_id != "base" and record.candidate_ref and not record.fatal
        ]
        vector_best: Dict[str, SelfRepairCandidateRecord] = {}
        for record in eligible:
            current = vector_best.get(record.vector_fingerprint)
            if current is None or self._search_score(record) > self._search_score(current):
                vector_best[record.vector_fingerprint] = record
        records = list(vector_best.values())
        frontier = [
            record
            for record in records
            if not any(
                other.candidate_id != record.candidate_id
                and self._dominates(other, record)
                for other in records
            )
        ]
        frontier.sort(key=self._search_score, reverse=True)
        frontier = frontier[: self.max_frontier_candidates]
        self.frontier = [record.candidate_id for record in frontier]
        search_candidates = [self.candidates["base"], *frontier]
        best_search = max(search_candidates, key=self._search_score)
        self.best_search_candidate_id = best_search.candidate_id
        self.best_search_ref = best_search.candidate_ref or self.base_commit
        safe_candidates = [
            record
            for record in search_candidates
            if not record.fatal
            and not any(
                item.startswith("safety:") for item in record.failed_obligations
            )
        ]
        best_safe = max(safe_candidates, key=self._search_score)
        self.best_safe_candidate_id = best_safe.candidate_id
        self.best_safe_ref = best_safe.candidate_ref or self.base_commit
        return tuple(self.frontier) != previous

    def register_candidate(
        self,
        record: SelfRepairCandidateRecord,
        *,
        findings: Sequence[SelfRepairFinding] = (),
    ) -> str:
        previous_max_rank = max(
            (item.validation_rank for item in self.candidates.values()), default=0
        )
        previous_vectors = {
            item.vector_fingerprint for item in self.candidates.values()
        }
        new_confirmed_findings = 0
        newly_active_finding_ids: list[str] = []
        for finding in findings:
            if not finding.finding_id:
                continue
            existing = self.findings.get(finding.finding_id)
            if existing is None:
                independently_actionable = bool(
                    finding.status == "confirmed"
                    or (
                        finding.counterexample.strip()
                        and finding.required_test.strip()
                        and finding.evidence
                    )
                )
                finding.status = (
                    "confirmed" if independently_actionable else "observed"
                )
                finding.introduced_by = finding.introduced_by or record.candidate_id
                finding.updated_at = _utc_now()
                self.findings[finding.finding_id] = finding
                if independently_actionable:
                    new_confirmed_findings += 1
                    newly_active_finding_ids.append(finding.finding_id)
                    self.obligations[f"finding:{finding.finding_id}"] = {
                        "kind": "review_finding",
                        "status": "open",
                        "description": finding.reason,
                        "source": finding.introduced_by,
                    }
            else:
                existing.updated_at = _utc_now()
                if existing.status in {"resolved", "invalidated"}:
                    existing.status = "reopened"
                    if record.candidate_id not in existing.reopened_by:
                        existing.reopened_by.append(record.candidate_id)
                    new_confirmed_findings += 1
                    newly_active_finding_ids.append(finding.finding_id)
                    self.obligations.setdefault(
                        f"finding:{finding.finding_id}",
                        {
                            "kind": "review_finding",
                            "description": finding.reason,
                            "source": finding.introduced_by,
                        },
                    )["status"] = "reopened"
                if finding.reason:
                    existing.reason = finding.reason
                if finding.counterexample:
                    existing.counterexample = finding.counterexample
                if finding.required_test:
                    existing.required_test = finding.required_test
        for finding_id in record.resolved_finding_ids:
            finding = self.findings.get(finding_id)
            if finding is not None:
                finding.status = "resolved"
                finding.resolved_by = record.candidate_id
                finding.updated_at = _utc_now()
            obligation = self.obligations.get(f"finding:{finding_id}")
            if obligation is not None:
                obligation["status"] = "resolved"
                obligation["resolved_by"] = record.candidate_id
            failure_id = f"finding:{finding_id}"
            record.failed_obligations = [
                item for item in record.failed_obligations if item != failure_id
            ]
            if failure_id not in record.passed_obligations:
                record.passed_obligations.append(failure_id)
        for finding_id in newly_active_finding_ids:
            failure_id = f"finding:{finding_id}"
            for historical in self.candidates.values():
                if finding_id in historical.resolved_finding_ids:
                    continue
                if failure_id not in historical.failed_obligations:
                    historical.failed_obligations.append(failure_id)
            if finding_id not in record.resolved_finding_ids:
                if failure_id not in record.failed_obligations:
                    record.failed_obligations.append(failure_id)
        self.candidates[record.candidate_id] = record
        self.attempt_count += 1
        self.current_candidate_id = ""
        if record.strategy_fingerprint:
            self.strategy_history.append(record.strategy_fingerprint)
            self.strategy_history = self.strategy_history[-64:]
        frontier_changed = self._recompute_frontier()
        progress_kind = ""
        if new_confirmed_findings:
            progress_kind = "diagnostic_progress"
        elif record.validation_rank > previous_max_rank:
            progress_kind = "validation_progress"
        elif (
            not record.fatal
            and record.vector_fingerprint not in previous_vectors
            and frontier_changed
        ):
            progress_kind = "frontier_progress"
        if record.infrastructure_failure:
            self.infrastructure_failures += 1
            progress_kind = "infrastructure_interruption"
        elif progress_kind:
            self.consecutive_non_improvements = 0
        else:
            self.consecutive_non_improvements += 1
            progress_kind = "no_progress"
        self.last_progress_kind = progress_kind
        self.updated_at = _utc_now()
        return progress_kind

    @property
    def patience_exhausted(self) -> bool:
        return (
            self.consecutive_non_improvements
            >= self.max_consecutive_non_improvements
        )

    def prompt_context(self) -> Dict[str, object]:
        open_findings = [
            item.to_dict()
            for item in self.findings.values()
            if item.status in {"confirmed", "reopened"}
        ]
        resolved_findings = [
            item.finding_id
            for item in self.findings.values()
            if item.status == "resolved"
        ]
        recent = sorted(
            (
                item
                for candidate_id, item in self.candidates.items()
                if candidate_id != "base"
            ),
            key=lambda item: item.created_at,
        )[-8:]
        return {
            "experiment_id": self.experiment_id,
            "root_fingerprint": self.root_fingerprint,
            "best_search_candidate_id": self.best_search_candidate_id,
            "frontier": list(self.frontier),
            "consecutive_non_improvements": self.consecutive_non_improvements,
            "patience_limit": self.max_consecutive_non_improvements,
            "open_confirmed_findings": open_findings,
            "resolved_findings_that_must_not_regress": resolved_findings,
            "recent_candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "parent_candidate_id": item.parent_candidate_id,
                    "status": item.status,
                    "validation_stage": item.validation_stage,
                    "passed_obligations": item.passed_obligations,
                    "failed_obligations": item.failed_obligations,
                    "finding_ids": item.finding_ids,
                    "strategy_fingerprint": item.strategy_fingerprint,
                    "summary": item.summary[-1200:],
                    "verification": item.verification[-1600:],
                }
                for item in recent
            ],
            "recent_health_anomalies": list(self.health_history[-8:]),
        }


class SelfRepairExperimentStore:
    def __init__(self, project_root: Path, run_id: str, root_fingerprint: str) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.run_id = str(run_id)
        self.safe_root = safe_repair_root(root_fingerprint)
        self.root = (
            run_path(self.project_root, self.run_id)
            / "self-repair"
            / self.safe_root
        )
        self.path = self.root / "experiment.json"

    def load(self) -> Optional[SelfRepairExperiment]:
        payload = read_json(self.path, default={})
        if not isinstance(payload, Mapping) or not payload.get("experiment_id"):
            if self.path.exists():
                raise RuntimeError(
                    f"persisted self-repair experiment is malformed: {self.path}"
                )
            return None
        try:
            return SelfRepairExperiment.from_dict(payload)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"persisted self-repair experiment is malformed: {error}"
            ) from error

    def save(self, experiment: SelfRepairExperiment) -> None:
        experiment.updated_at = _utc_now()
        _atomic_json(self.path, experiment.to_dict())

    def candidate_root(self, candidate_id: str) -> Path:
        return self.root / safe_repair_root(candidate_id)

    def write_candidate_artifact(
        self,
        candidate_id: str,
        name: str,
        payload: object,
    ) -> Path:
        path = self.candidate_root(candidate_id) / name
        _atomic_json(path, payload)
        return path

    def record_health(
        self,
        experiment: SelfRepairExperiment,
        *,
        status: str,
        detail: str = "",
    ) -> Dict[str, object]:
        strategies = list(experiment.strategy_history)
        anomaly = ""
        for cycle_length in range(1, 5):
            required = cycle_length * 3
            if len(strategies) < required:
                continue
            suffix = strategies[-required:]
            cycle = suffix[:cycle_length]
            if all(
                suffix[index : index + cycle_length] == cycle
                for index in range(0, required, cycle_length)
            ):
                anomaly = "strategy_oscillation"
                break
        if experiment.patience_exhausted:
            anomaly = "search_patience_exhausted"
        elif experiment.last_progress_kind == "infrastructure_interruption":
            anomaly = "infrastructure_interruption"
        payload: Dict[str, object] = {
            "schema_version": SELF_REPAIR_EXPERIMENT_SCHEMA_VERSION,
            "experiment_id": experiment.experiment_id,
            "root_fingerprint": experiment.root_fingerprint,
            "status": status,
            "detail": str(detail)[:2000],
            "anomaly": anomaly,
            "current_candidate_id": experiment.current_candidate_id,
            "best_search_candidate_id": experiment.best_search_candidate_id,
            "frontier": list(experiment.frontier),
            "consecutive_non_improvements": (
                experiment.consecutive_non_improvements
            ),
            "last_progress_kind": experiment.last_progress_kind,
            "updated_at": _utc_now(),
        }
        health_root = self.root / "health"
        _atomic_json(health_root / "heartbeat.json", payload)
        snapshots = health_root / "snapshots"
        sequence = experiment.attempt_count
        snapshot_label = safe_repair_root(
            f"{status}-{experiment.current_candidate_id or 'idle'}"
        )
        _atomic_json(
            snapshots / f"{sequence:08d}-{snapshot_label}.json",
            payload,
        )
        snapshot_paths = sorted(snapshots.glob("*.json"))
        for stale in snapshot_paths[:-50]:
            try:
                stale.unlink()
            except OSError:
                pass
        if anomaly:
            duplicate = bool(
                experiment.health_history
                and experiment.health_history[-1].get("anomaly") == anomaly
                and experiment.health_history[-1].get("current_candidate_id")
                == experiment.current_candidate_id
                and experiment.health_history[-1].get("last_progress_kind")
                == experiment.last_progress_kind
            )
            if not duplicate:
                experiment.health_history.append(payload)
            experiment.health_history = experiment.health_history[-64:]
            self.save(experiment)
        return payload

    def compact_success(self, experiment: SelfRepairExperiment) -> None:
        summaries = {
            candidate_id: {
                "candidate_id": record.candidate_id,
                "parent_candidate_id": record.parent_candidate_id,
                "status": record.status,
                "validation_stage": record.validation_stage,
                "validation_rank": record.validation_rank,
                "passed_obligations": record.passed_obligations,
                "failed_obligations": record.failed_obligations,
                "finding_ids": record.finding_ids,
                "resolved_finding_ids": record.resolved_finding_ids,
                "strategy_fingerprint": record.strategy_fingerprint,
            }
            for candidate_id, record in experiment.candidates.items()
        }
        _atomic_json(self.root / "candidate-summaries.json", summaries)
        for candidate_id in experiment.candidates:
            if candidate_id == "base":
                continue
            candidate_root = self.candidate_root(candidate_id)
            shutil.rmtree(candidate_root, ignore_errors=True)
