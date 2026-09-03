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


SELF_REPAIR_EXPERIMENT_SCHEMA_VERSION = 2


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
    disposition: str = "unclassified"
    causal_obligation_id: str = ""
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
            disposition=str(payload.get("disposition", "unclassified")),
            causal_obligation_id=str(payload.get("causal_obligation_id", "")),
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
    finding_group_id: str = ""
    net_progress: int = 0
    semantic_state_fingerprint: str = ""
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
    contract_status: str = "frozen"
    contract_fingerprint: str = ""
    contract_obligation_ids: list[str] = field(default_factory=list)
    repair_design: Dict[str, object] = field(default_factory=dict)
    repair_design_fingerprint: str = ""
    design_history: list[Dict[str, object]] = field(default_factory=list)
    finding_groups: list[Dict[str, object]] = field(default_factory=list)
    active_finding_group_id: str = ""
    completed_contract_obligation_ids: list[str] = field(default_factory=list)
    completed_finding_ids: list[str] = field(default_factory=list)
    strategy_blacklist: list[str] = field(default_factory=list)
    semantic_state_history: list[str] = field(default_factory=list)
    automatic_corrections: list[Dict[str, object]] = field(default_factory=list)
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
            "validation:proof_seal": (
                "deterministic sealing binds all proof to the immutable candidate"
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
        contract_obligation_ids = sorted(
            obligation_id
            for obligation_id in obligations
            if obligation_id.startswith(("root:", "safety:"))
        )
        contract_fingerprint = _stable_hash(
            [
                (
                    obligation_id,
                    obligations[obligation_id].get("description", ""),
                )
                for obligation_id in contract_obligation_ids
            ]
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
            contract_fingerprint=contract_fingerprint,
            contract_obligation_ids=contract_obligation_ids,
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
        for name in (
            "frontier",
            "strategy_history",
            "health_history",
            "contract_obligation_ids",
            "design_history",
            "finding_groups",
            "strategy_blacklist",
            "semantic_state_history",
            "automatic_corrections",
            "completed_contract_obligation_ids",
            "completed_finding_ids",
        ):
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
            contract_status=str(payload.get("contract_status", "frozen")),
            contract_fingerprint=str(payload.get("contract_fingerprint", "")),
            contract_obligation_ids=[
                str(item)
                for item in payload.get("contract_obligation_ids", []) or []
            ],
            repair_design=(
                dict(payload.get("repair_design", {}))
                if isinstance(payload.get("repair_design", {}), Mapping)
                else {}
            ),
            repair_design_fingerprint=str(
                payload.get("repair_design_fingerprint", "")
            ),
            design_history=[
                dict(item)
                for item in payload.get("design_history", []) or []
                if isinstance(item, Mapping)
            ][-32:],
            finding_groups=[
                dict(item)
                for item in payload.get("finding_groups", []) or []
                if isinstance(item, Mapping)
            ],
            active_finding_group_id=str(
                payload.get("active_finding_group_id", "")
            ),
            completed_contract_obligation_ids=[
                str(item)
                for item in payload.get("completed_contract_obligation_ids", []) or []
            ],
            completed_finding_ids=[
                str(item)
                for item in payload.get("completed_finding_ids", []) or []
            ],
            strategy_blacklist=[
                str(item) for item in payload.get("strategy_blacklist", []) or []
            ][-64:],
            semantic_state_history=[
                str(item)
                for item in payload.get("semantic_state_history", []) or []
            ][-128:],
            automatic_corrections=[
                dict(item)
                for item in payload.get("automatic_corrections", []) or []
                if isinstance(item, Mapping)
            ][-64:],
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

    def freeze_contract(self) -> bool:
        """Freeze causal obligations and quarantine legacy scope expansion."""

        changed = False
        if not self.contract_obligation_ids:
            self.contract_obligation_ids = sorted(
                obligation_id
                for obligation_id in self.obligations
                if obligation_id.startswith(("root:", "safety:"))
            )
            changed = True
        fingerprint = _stable_hash(
            [
                (
                    obligation_id,
                    self.obligations.get(obligation_id, {}).get(
                        "description", ""
                    ),
                )
                for obligation_id in self.contract_obligation_ids
            ]
        )
        if self.contract_fingerprint != fingerprint:
            self.contract_fingerprint = fingerprint
            self.repair_design = {}
            self.repair_design_fingerprint = ""
            self.finding_groups = []
            self.active_finding_group_id = ""
            changed = True
        self.contract_status = "frozen"

        contract_ids = set(self.contract_obligation_ids)
        quarantined: set[str] = set()
        for finding in self.findings.values():
            if finding.disposition != "unclassified":
                continue
            causal_id = finding.causal_obligation_id or finding.obligation_id
            if causal_id in contract_ids:
                finding.disposition = "contract_violation"
                finding.causal_obligation_id = causal_id
            else:
                finding.disposition = "unrelated_observation"
                if finding.status in {"confirmed", "reopened", "observed"}:
                    finding.status = "quarantined"
                quarantined.add(finding.finding_id)
                obligation = self.obligations.get(
                    f"finding:{finding.finding_id}"
                )
                if obligation is not None:
                    obligation["status"] = "quarantined"
            finding.updated_at = _utc_now()
            changed = True
        if quarantined:
            quarantined_obligations = {
                f"finding:{finding_id}" for finding_id in quarantined
            }
            for record in self.candidates.values():
                record.failed_obligations = [
                    item
                    for item in record.failed_obligations
                    if item not in quarantined_obligations
                ]
            self.automatic_corrections.append(
                {
                    "kind": "legacy_scope_contraction",
                    "quarantined_finding_ids": sorted(quarantined),
                    "at": _utc_now(),
                }
            )
            self.automatic_corrections = self.automatic_corrections[-64:]
        if changed:
            self._recompute_frontier()
        return changed

    def blocking_findings(self) -> list[SelfRepairFinding]:
        contract_ids = set(self.contract_obligation_ids)
        return [
            finding
            for finding in self.findings.values()
            if finding.status in {"confirmed", "reopened"}
            and finding.disposition == "contract_violation"
            and finding.causal_obligation_id in contract_ids
        ]

    def next_finding_group(self) -> Optional[Dict[str, object]]:
        completed = {
            str(item.get("group_id", ""))
            for item in self.finding_groups
            if str(item.get("status", "")) == "completed"
        }
        for group in self.finding_groups:
            group_id = str(group.get("group_id", ""))
            dependencies = {
                str(item) for item in group.get("depends_on", []) or []
            }
            if (
                group_id
                and group_id not in completed
                and dependencies.issubset(completed)
            ):
                self.active_finding_group_id = group_id
                return group
        self.active_finding_group_id = ""
        return None

    def mark_finding_group_completed(
        self,
        group_id: str,
        *,
        candidate_id: str,
    ) -> None:
        for group in self.finding_groups:
            if str(group.get("group_id", "")) != group_id:
                continue
            group["status"] = "completed"
            group["completed_by"] = candidate_id
            group["completed_at"] = _utc_now()
            self.completed_contract_obligation_ids = sorted(
                set(self.completed_contract_obligation_ids).union(
                    str(item)
                    for item in group.get("contract_obligation_ids", []) or []
                )
            )
            self.completed_finding_ids = sorted(
                set(self.completed_finding_ids).union(
                    str(item) for item in group.get("finding_ids", []) or []
                )
            )
            break
        self.next_finding_group()
        self.updated_at = _utc_now()

    def apply_automatic_correction(
        self,
        *,
        reason: str,
        candidate_id: str = "",
        strategy_fingerprint: str = "",
    ) -> None:
        """Reset design state after semantic non-progress without human routing."""

        if strategy_fingerprint:
            self.strategy_blacklist.append(strategy_fingerprint)
            self.strategy_blacklist = list(
                dict.fromkeys(self.strategy_blacklist)
            )[-64:]
        self.design_history.append(
            {
                "event": "automatic_correction",
                "reason": str(reason)[:2000],
                "candidate_id": candidate_id,
                "strategy_fingerprint": strategy_fingerprint,
                "at": _utc_now(),
            }
        )
        self.design_history = self.design_history[-32:]
        self.automatic_corrections.append(dict(self.design_history[-1]))
        self.automatic_corrections = self.automatic_corrections[-64:]
        self.repair_design = {}
        self.repair_design_fingerprint = ""
        self.finding_groups = []
        self.active_finding_group_id = ""
        self.best_search_candidate_id = self.best_safe_candidate_id
        self.best_search_ref = self.best_safe_ref or self.base_commit
        self.consecutive_non_improvements = 0
        self.status = "active"
        self.updated_at = _utc_now()

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
        candidate_regressions = sum(
            1
            for item in record.failed_obligations
            if item.startswith("candidate_regression:")
        )
        return (
            root_passed,
            -safety_failed,
            -candidate_regressions,
            record.net_progress,
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
                item.startswith(("safety:", "candidate_regression:"))
                for item in record.failed_obligations
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
        parent = self.candidates.get(
            record.parent_candidate_id,
            self.candidates["base"],
        )
        previous_blocking = {
            finding.finding_id for finding in self.blocking_findings()
        }
        candidate_regressions: list[str] = []
        contract_ids = set(self.contract_obligation_ids)
        for finding in findings:
            if not finding.finding_id:
                continue
            disposition = finding.disposition.strip().lower()
            causal_id = (
                finding.causal_obligation_id.strip()
                or finding.obligation_id.strip()
            )
            if disposition == "unclassified":
                disposition = (
                    "contract_violation"
                    if causal_id in contract_ids
                    else "unrelated_observation"
                )
            finding.disposition = disposition
            finding.causal_obligation_id = causal_id
            if disposition == "unrelated_observation":
                # Reviewer observations outside the frozen causal contract are
                # deliberately neither persisted nor scheduled.
                continue
            if disposition == "candidate_regression":
                candidate_regressions.append(finding.finding_id)
                failure_id = f"candidate_regression:{finding.finding_id}"
                if failure_id not in record.failed_obligations:
                    record.failed_obligations.append(failure_id)
                continue
            if disposition != "contract_violation" or causal_id not in contract_ids:
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
                    if causal_id not in record.failed_obligations:
                        record.failed_obligations.append(causal_id)
            else:
                existing.updated_at = _utc_now()
                if existing.status in {"resolved", "invalidated"}:
                    existing.status = "reopened"
                    if record.candidate_id not in existing.reopened_by:
                        existing.reopened_by.append(record.candidate_id)
                if finding.reason:
                    existing.reason = finding.reason
                if finding.counterexample:
                    existing.counterexample = finding.counterexample
                if finding.required_test:
                    existing.required_test = finding.required_test
                existing.disposition = disposition
                existing.causal_obligation_id = causal_id
                if causal_id not in record.failed_obligations:
                    record.failed_obligations.append(causal_id)
        for finding_id in record.resolved_finding_ids:
            finding = self.findings.get(finding_id)
            if finding is not None:
                finding.status = "resolved"
                finding.resolved_by = record.candidate_id
                finding.updated_at = _utc_now()
            failure_id = (
                finding.causal_obligation_id
                if finding is not None
                else f"finding:{finding_id}"
            )
            record.failed_obligations = [
                item for item in record.failed_obligations if item != failure_id
            ]
            if failure_id not in record.passed_obligations:
                record.passed_obligations.append(failure_id)
        current_blocking = {
            finding.finding_id for finding in self.blocking_findings()
        }
        resolved_blocking = len(previous_blocking - current_blocking)
        root_gain = len(
            {
                item for item in record.passed_obligations if item.startswith("root:")
            }
            - {
                item for item in parent.passed_obligations if item.startswith("root:")
            }
        )
        validation_gain = int(
            bool(record.candidate_ref)
            and record.validation_rank > parent.validation_rank
        )
        group_gain = int(
            record.status == "candidate_group_completed"
            and bool(record.finding_group_id)
            and record.finding_group_id != parent.finding_group_id
        )
        safety_gain = len(
            {
                item
                for item in parent.failed_obligations
                if item.startswith("safety:")
            }
            - {
                item
                for item in record.failed_obligations
                if item.startswith("safety:")
            }
        )
        record.net_progress = (
            root_gain
            + safety_gain
            + resolved_blocking
            + validation_gain
            + group_gain
            - len(candidate_regressions)
            - len(current_blocking - previous_blocking)
        )
        record.semantic_state_fingerprint = _stable_hash(
            sorted(
                item for item in record.passed_obligations if item.startswith("root:")
            ),
            sorted(current_blocking),
            record.validation_stage,
            record.strategy_fingerprint,
        )
        self.candidates[record.candidate_id] = record
        self.attempt_count += 1
        self.current_candidate_id = ""
        if record.strategy_fingerprint:
            self.strategy_history.append(record.strategy_fingerprint)
            self.strategy_history = self.strategy_history[-64:]
        if record.semantic_state_fingerprint:
            self.semantic_state_history.append(record.semantic_state_fingerprint)
            self.semantic_state_history = self.semantic_state_history[-128:]
        self._recompute_frontier()
        progress_kind = "net_progress" if record.net_progress > 0 else ""
        if record.infrastructure_failure:
            self.infrastructure_failures += 1
            progress_kind = "infrastructure_interruption"
        elif progress_kind and not candidate_regressions:
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
            for item in self.blocking_findings()
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
        )[-3:]
        return {
            "experiment_id": self.experiment_id,
            "root_fingerprint": self.root_fingerprint,
            "contract_fingerprint": self.contract_fingerprint,
            "contract_obligations": [
                {
                    "obligation_id": obligation_id,
                    "description": self.obligations.get(obligation_id, {}).get(
                        "description", ""
                    ),
                }
                for obligation_id in self.contract_obligation_ids
            ],
            "best_search_candidate_id": self.best_search_candidate_id,
            "consecutive_non_improvements": self.consecutive_non_improvements,
            "open_contract_findings": open_findings,
            "resolved_findings_that_must_not_regress": resolved_findings,
            "approved_repair_design": self.repair_design,
            "active_finding_group_id": self.active_finding_group_id,
            "prohibited_strategy_fingerprints": self.strategy_blacklist[-8:],
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
                    "net_progress": item.net_progress,
                    "summary": " ".join(item.summary.split())[-400:],
                }
                for item in recent
            ],
            "recent_automatic_corrections": self.automatic_corrections[-3:],
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
            anomaly = "net_progress_stalled"
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
