from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .config import run_path
from .io_utils import read_json, read_text, write_json
from .models import AgentRequest, AgentResult, RunState, SelfRepairDiagnosisConfig


ROOT_CAUSE_SCHEMA_VERSION = 1
ROOT_CAUSE_OWNERS = {
    "auto_agents",
    "execution_environment",
    "target_project",
    "external_provider",
    "requirements",
    "user_input",
    "verification_contract",
    "verification_infrastructure",
    "unknown",
}
_TEXT_LIMIT = 80_000
_DIFF_LIMIT = 60_000
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|access[_-]?token|"
    r"refresh[_-]?token)(\s*[=:]\s*)([^\s,}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass
class RootCauseEvidence:
    kind: str
    ref: str
    claim: str

    @classmethod
    def from_dict(cls, payload: object) -> "RootCauseEvidence":
        if not isinstance(payload, Mapping):
            raise ValueError("root-cause evidence entry must be an object")
        kind = str(payload.get("kind", "")).strip()
        ref = str(payload.get("ref", "")).strip()
        claim = str(payload.get("claim", "")).strip()
        if not kind or not ref or not claim:
            raise ValueError("root-cause evidence requires kind, ref, and claim")
        return cls(kind=kind, ref=ref, claim=claim)


@dataclass
class RootCauseReport:
    role: str
    verdict: str
    owner: str
    confidence: float
    category: str
    generic: bool
    safe_to_repair: bool
    causal_chain: List[str]
    evidence: List[RootCauseEvidence]
    rejected_hypotheses: List[str] = field(default_factory=list)
    reproduction_commands: List[str] = field(default_factory=list)
    reproduction_outcome: str = ""
    proposed_fix_scope: List[str] = field(default_factory=list)
    verification_commands: List[str] = field(default_factory=list)
    resume_strategy: str = ""

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["schema_version"] = ROOT_CAUSE_SCHEMA_VERSION
        return payload

    @classmethod
    def from_dict(
        cls,
        payload: object,
        *,
        role: str,
    ) -> "RootCauseReport":
        if not isinstance(payload, Mapping):
            raise ValueError("root-cause report must be an object")
        if "safe_to_self_repair" in payload:
            decision = str(payload.get("decision", "")).strip().upper()
            owner = str(payload.get("owner", "")).strip().lower()
            legacy_evidence = [
                RootCauseEvidence(
                    kind="legacy_judgment",
                    ref="self-repair-triage",
                    claim=str(item).strip(),
                )
                for item in payload.get("evidence", []) or []
                if str(item).strip()
            ]
            return cls(
                role=role,
                verdict=(
                    "AGREE"
                    if role == "reviewer"
                    else "FINAL"
                    if role == "arbiter"
                    else "ROOT_CAUSE"
                ),
                owner=owner,
                confidence=float(payload.get("confidence", 0.0) or 0.0),
                category=str(payload.get("category", "")).strip(),
                generic=bool(payload.get("generic", False)),
                safe_to_repair=bool(payload.get("safe_to_self_repair", False)),
                causal_chain=[str(payload.get("reason", "")).strip()],
                evidence=legacy_evidence,
                resume_strategy=(
                    "repair_and_resume" if decision == "SELF_REPAIR" else "block"
                ),
            )

        report_role = str(payload.get("role", role)).strip().lower()
        if report_role != role:
            raise ValueError(f"root-cause report role must be {role}")
        verdict = str(payload.get("verdict", "")).strip().upper()
        allowed_verdicts = {
            "investigator": {"ROOT_CAUSE"},
            "reviewer": {"AGREE", "DISAGREE", "UNKNOWN"},
            "arbiter": {"FINAL", "UNKNOWN"},
        }[role]
        if verdict not in allowed_verdicts:
            raise ValueError(
                f"root-cause {role} verdict must be one of: "
                + ", ".join(sorted(allowed_verdicts))
            )
        owner = str(payload.get("owner", "")).strip().lower()
        if owner not in ROOT_CAUSE_OWNERS:
            raise ValueError("root-cause owner is invalid")
        confidence = payload.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise ValueError("root-cause confidence must be between 0 and 1")
        category = str(payload.get("category", "")).strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", category):
            raise ValueError("root-cause category must be stable snake_case")
        causal_chain = [
            str(item).strip()
            for item in payload.get("causal_chain", []) or []
            if str(item).strip()
        ]
        evidence = [
            RootCauseEvidence.from_dict(item)
            for item in payload.get("evidence", []) or []
        ]
        if not causal_chain or not evidence:
            raise ValueError("root-cause report requires causal_chain and evidence")
        return cls(
            role=role,
            verdict=verdict,
            owner=owner,
            confidence=float(confidence),
            category=category,
            generic=bool(payload.get("generic", False)),
            safe_to_repair=bool(payload.get("safe_to_repair", False)),
            causal_chain=causal_chain,
            evidence=evidence,
            rejected_hypotheses=[
                str(item).strip()
                for item in payload.get("rejected_hypotheses", []) or []
                if str(item).strip()
            ],
            reproduction_commands=[
                str(item).strip()
                for item in payload.get("reproduction_commands", []) or []
                if str(item).strip()
            ],
            reproduction_outcome=str(
                payload.get("reproduction_outcome", "")
            ).strip(),
            proposed_fix_scope=[
                str(item).strip()
                for item in payload.get("proposed_fix_scope", []) or []
                if str(item).strip()
            ],
            verification_commands=[
                str(item).strip()
                for item in payload.get("verification_commands", []) or []
                if str(item).strip()
            ],
            resume_strategy=str(payload.get("resume_strategy", "")).strip(),
        )


@dataclass
class RootCauseDiagnosis:
    diagnosis_id: str
    evidence_path: str
    investigator: RootCauseReport
    reviewer: RootCauseReport
    final: RootCauseReport
    arbiter: Optional[RootCauseReport]
    repair_approved: bool
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": ROOT_CAUSE_SCHEMA_VERSION,
            "diagnosis_id": self.diagnosis_id,
            "evidence_path": self.evidence_path,
            "investigator": self.investigator.to_dict(),
            "reviewer": self.reviewer.to_dict(),
            "arbiter": self.arbiter.to_dict() if self.arbiter else None,
            "final": self.final.to_dict(),
            "repair_approved": self.repair_approved,
            "reason": self.reason,
        }


def _compact(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[-limit:]


def _redact_text(value: str) -> str:
    text = _BEARER_RE.sub("Bearer [REDACTED]", str(value or ""))
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}[REDACTED]"
        ),
        text,
    )


def _redact_payload(value: object) -> object:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(
                r"(?i)(api[_-]?key|authorization|password|secret|"
                r"access[_-]?token|refresh[_-]?token)",
                key_text,
            ):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = _redact_payload(item)
        return redacted
    return value


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return _compact(
        result.stdout if result.returncode == 0 else result.stderr,
        _DIFF_LIMIT,
    )


def repository_diagnostic_state(root: Path) -> Dict[str, object]:
    return {
        "root": str(root),
        "head": _run_git(root, "rev-parse", "HEAD").strip(),
        "status": _run_git(root, "status", "--short", "--untracked-files=all"),
        "unstaged_diff": _run_git(root, "diff", "--no-ext-diff", "--binary"),
        "staged_diff": _run_git(
            root,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--binary",
        ),
    }


def repository_guard_fingerprint(
    root: Path,
    *,
    ignore_run_artifacts: bool = False,
) -> str:
    state = repository_diagnostic_state(root)
    if ignore_run_artifacts:
        status_lines = [
            line
            for line in str(state["status"]).splitlines()
            if ".auto-agents/runs/" not in line
        ]
        state["status"] = "\n".join(status_lines)
    return hashlib.sha256(
        json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _compact_run_state(
    state: Optional[RunState],
    *,
    error_text: str = "",
) -> Dict[str, object]:
    if state is None:
        return {}
    payload = state.to_dict()
    keys = (
        "run_id",
        "status",
        "current_stage",
        "pending_approval",
        "approved_gates",
        "last_error",
        "rejected_stage",
        "rejection_reason",
        "resume_context",
        "last_recovery_route",
        "active_blocker",
        "active_execution_incident_id",
        "execution_incidents",
        "agent_attempts",
    )
    compact = {key: payload.get(key) for key in keys}
    tasks = [
        item
        for item in payload.get("tasks", []) or []
        if isinstance(item, Mapping)
    ]
    preferred_ids = set(
        re.findall(
            r"(?:\bTask\s+|\bimplement-)([A-Za-z0-9_-]+)",
            error_text,
        )
    )
    selected_tasks = [
        item
        for item in tasks
        if str(item.get("task_id", "")) in preferred_ids
        or str(item.get("parent_task_id", "")) in preferred_ids
    ]
    for item in tasks[-8:]:
        if item not in selected_tasks:
            selected_tasks.append(item)
    compact["tasks"] = [
        {
            "task_id": item.get("task_id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "task_origin": item.get("task_origin"),
            "parent_task_id": item.get("parent_task_id"),
            "review_summary": item.get("review_summary"),
            "review_history": list(item.get("review_history", []) or [])[-4:],
            "verify_history": list(item.get("verify_history", []) or [])[-4:],
            "recovery_history": list(item.get("recovery_history", []) or [])[-4:],
            "verification_refs": item.get("verification_refs", []),
        }
        for item in selected_tasks
    ]
    return compact


class TerminalEvidenceCollector:
    def __init__(
        self,
        *,
        auto_agents_root: Path,
        target_root: Path,
        error: object,
        state: Optional[RunState],
        traceback_text: str,
        heuristic: Mapping[str, object],
        runtime_evidence: Mapping[str, object],
    ) -> None:
        self.auto_agents_root = auto_agents_root
        self.target_root = target_root
        self.error = error
        self.state = state
        self.traceback_text = traceback_text
        self.heuristic = dict(heuristic)
        self.runtime_evidence = dict(runtime_evidence)

    def collect(self) -> tuple[str, Path, Dict[str, object]]:
        run_id = (
            self.state.run_id
            if self.state is not None and self.state.run_id.strip()
            else "uninitialized"
        )
        diagnosis_id = uuid.uuid4().hex[:12]
        root = run_path(self.target_root, run_id) / "root-cause" / diagnosis_id
        root.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, object] = {
            "schema_version": ROOT_CAUSE_SCHEMA_VERSION,
            "diagnosis_id": diagnosis_id,
            "run_id": run_id,
            "error_type": type(self.error).__name__,
            "error": _compact(str(self.error or ""), _TEXT_LIMIT),
            "traceback": _compact(self.traceback_text, _TEXT_LIMIT),
            "heuristic_hint": self.heuristic,
            "run_state": _compact_run_state(
                self.state,
                error_text=str(self.error or ""),
            ),
            "run_log": _compact(
                read_text(run_path(self.target_root, run_id) / "run.log"),
                _TEXT_LIMIT,
            ),
            "active_execution_incident": self._active_execution_incident(
                run_id
            ),
            "requirements_audit_findings": self._requirements_audit_findings(),
            "attempt_timeline": self._attempt_timeline(run_id),
            "attempt_recovery_checkpoints": self._attempt_checkpoints(run_id),
            "runtime_capability_evidence": self.runtime_evidence,
            "target_repository": repository_diagnostic_state(self.target_root),
            "auto_agents_repository": repository_diagnostic_state(
                self.auto_agents_root
            ),
        }
        evidence_path = root / "evidence.json"
        redacted = _redact_payload(payload)
        assert isinstance(redacted, dict)
        write_json(evidence_path, redacted)
        return diagnosis_id, root, redacted

    def _attempt_timeline(self, run_id: str) -> List[Dict[str, object]]:
        attempts_root = (
            run_path(self.target_root, run_id)
            / "outputs"
            / "provider-attempts"
        )
        result: List[Dict[str, object]] = []
        if not attempts_root.is_dir():
            return result
        paths = sorted(
            attempts_root.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
        )
        for path in paths[-24:]:
            payload = read_json(path, default={})
            if not isinstance(payload, Mapping):
                continue
            result.append(
                {
                    "path": str(path),
                    "attempt_id": payload.get("attempt_id"),
                    "stage": payload.get("stage"),
                    "status": payload.get("status"),
                    "elapsed_seconds": payload.get("elapsed_seconds"),
                    "cwd": payload.get("cwd"),
                    "events": list(payload.get("events", []) or [])[-80:],
                }
            )
        return result

    def _active_execution_incident(
        self,
        run_id: str,
    ) -> Dict[str, object]:
        incident_id = (
            self.state.active_execution_incident_id
            if self.state is not None
            else ""
        )
        if not incident_id:
            return {}
        payload = read_json(
            run_path(self.target_root, run_id)
            / "recovery_incidents"
            / f"{incident_id}.json",
            default={},
        )
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _requirements_audit_findings(self) -> str:
        report = read_text(
            self.target_root
            / ".auto-agents"
            / "docs"
            / "requirements_audit.md"
        )
        referenced = {
            item.upper()
            for item in re.findall(
                r"\bREQ-\d+\b",
                str(self.error or ""),
                flags=re.IGNORECASE,
            )
        }
        if not referenced:
            return _compact(report, _TEXT_LIMIT)
        selected = []
        for section in re.finditer(
            r"(?ms)^## (?P<id>REQ-\d+): [^\n]+\n.*?(?=^## REQ-\d+: |\Z)",
            report,
        ):
            if section.group("id").upper() in referenced:
                selected.append(section.group(0).strip())
        return _compact("\n\n".join(selected), _TEXT_LIMIT)

    def _attempt_checkpoints(self, run_id: str) -> List[Dict[str, object]]:
        checkpoint_root = (
            run_path(self.target_root, run_id) / "attempt-checkpoints"
        )
        result: List[Dict[str, object]] = []
        if not checkpoint_root.is_dir():
            return result
        for manifest in sorted(checkpoint_root.glob("*/manifest.json")):
            payload = read_json(manifest, default={})
            if isinstance(payload, Mapping):
                result.append(
                    {
                        "path": str(manifest.parent),
                        **dict(payload),
                    }
                )
        return result


class RootCauseCoordinator:
    def __init__(
        self,
        orchestrator: object,
        *,
        auto_agents_root: Path,
        target_root: Path,
        error: object,
        state: Optional[RunState],
        traceback_text: str,
        heuristic: Mapping[str, object],
        runtime_evidence: Mapping[str, object],
        config: SelfRepairDiagnosisConfig,
    ) -> None:
        self.orchestrator = orchestrator
        self.auto_agents_root = auto_agents_root
        self.target_root = target_root
        self.error = error
        self.state = state
        self.traceback_text = traceback_text
        self.heuristic = dict(heuristic)
        self.runtime_evidence = dict(runtime_evidence)
        self.config = config
        self.diagnostic_auto_root = auto_agents_root
        self.diagnostic_target_root = target_root

    def run(self) -> RootCauseDiagnosis:
        diagnosis_id, artifacts, evidence = TerminalEvidenceCollector(
            auto_agents_root=self.auto_agents_root,
            target_root=self.target_root,
            error=self.error,
            state=self.state,
            traceback_text=self.traceback_text,
            heuristic=self.heuristic,
            runtime_evidence=self.runtime_evidence,
        ).collect()
        before_auto = repository_guard_fingerprint(self.auto_agents_root)
        before_target = repository_guard_fingerprint(
            self.target_root,
            ignore_run_artifacts=True,
        )
        with tempfile.TemporaryDirectory(
            prefix="auto-agents-root-cause-snapshot-"
        ) as tmp:
            snapshot_root = Path(tmp)
            self.diagnostic_auto_root = snapshot_root / "auto_agents"
            self.diagnostic_target_root = snapshot_root / "target"
            self._copy_diagnostic_tree(
                self.auto_agents_root,
                self.diagnostic_auto_root,
            )
            self._copy_diagnostic_tree(
                self.target_root,
                self.diagnostic_target_root,
            )
            diagnostic_evidence = self._replace_repository_roots(evidence)
            investigator = self._invoke(
                role="investigator",
                evidence=diagnostic_evidence,
                artifacts=artifacts,
                prior=None,
                timeout=self.config.investigator_timeout_seconds,
            )
            reviewer = self._invoke(
                role="reviewer",
                evidence=diagnostic_evidence,
                artifacts=artifacts,
                prior=investigator,
                timeout=self.config.reviewer_timeout_seconds,
            )
            arbiter: Optional[RootCauseReport] = None
            if not self._reports_agree(investigator, reviewer):
                arbiter = self._invoke(
                    role="arbiter",
                    evidence=diagnostic_evidence,
                    artifacts=artifacts,
                    prior={
                        "investigator": investigator,
                        "reviewer": reviewer,
                    },
                    timeout=self.config.arbiter_timeout_seconds,
                )
        self._assert_originals_unchanged(before_auto, before_target)
        final = arbiter or investigator
        threshold = (
            self.config.arbiter_confidence_threshold
            if arbiter is not None
            else self.config.confidence_threshold
        )
        repair_approved = self._repair_approved(
            final,
            reviewer,
            threshold=threshold,
            arbitrated=arbiter is not None,
        )
        reason = (
            "root-cause evidence consensus approved automatic auto_agents repair"
            if repair_approved
            else "root-cause investigation did not prove a safe generic auto_agents repair"
        )
        diagnosis = RootCauseDiagnosis(
            diagnosis_id=diagnosis_id,
            evidence_path=str(artifacts / "evidence.json"),
            investigator=investigator,
            reviewer=reviewer,
            arbiter=arbiter,
            final=final,
            repair_approved=repair_approved,
            reason=reason,
        )
        write_json(artifacts / "diagnosis.json", diagnosis.to_dict())
        return diagnosis

    @staticmethod
    def _copy_diagnostic_tree(source: Path, destination: Path) -> None:
        ignored_names = {
            ".git",
            ".conda",
            ".venv",
            "node_modules",
            "build",
            "dist",
            ".tmp-tests",
            ".data",
            ".cache",
            "coverage",
            "media",
            "__pycache__",
        }

        def ignore(current: str, names: List[str]) -> List[str]:
            current_path = Path(current)
            ignored = [name for name in names if name in ignored_names]
            if current_path.name == ".auto-agents":
                for generated in ("runs", "history"):
                    if generated in names:
                        ignored.append(generated)
            return ignored

        shutil.copytree(
            source,
            destination,
            ignore=ignore,
            symlinks=True,
        )

    def _replace_repository_roots(
        self,
        value: object,
    ) -> object:
        if isinstance(value, str):
            return (
                value.replace(
                    str(self.auto_agents_root),
                    str(self.diagnostic_auto_root),
                )
                .replace(
                    str(self.target_root),
                    str(self.diagnostic_target_root),
                )
            )
        if isinstance(value, list):
            return [self._replace_repository_roots(item) for item in value]
        if isinstance(value, Mapping):
            return {
                str(key): self._replace_repository_roots(item)
                for key, item in value.items()
            }
        return value

    def _invoke(
        self,
        *,
        role: str,
        evidence: Mapping[str, object],
        artifacts: Path,
        prior: object,
        timeout: int,
    ) -> RootCauseReport:
        output_path = artifacts / f"{role}.json"
        request = AgentRequest(
            stage=f"self_repair_{role}",
            effort=self._effort(),
            prompt=self._prompt(role=role, evidence=evidence, prior=prior),
            cwd=self.diagnostic_auto_root,
            output_path=output_path,
            attempt_id=f"root-cause-{role}",
            sandbox_mode="read-only",
            timeout_seconds=timeout,
            stream_output=(
                self.orchestrator._stream_agent_output_callback(
                    f"root-cause-{role}"
                )
                if bool(getattr(self.orchestrator, "_print_agent_output", False))
                and hasattr(
                    self.orchestrator,
                    "_stream_agent_output_callback",
                )
                else None
            ),
        )
        result: AgentResult = self.orchestrator._call_with_failover(request)
        if hasattr(self.orchestrator, "_emit_agent_output"):
            self.orchestrator._emit_agent_output(
                f"root-cause-{role}",
                result,
            )
        if not result.ok:
            raise RuntimeError(
                result.stderr or result.summary or f"root-cause {role} agent failed"
            )
        tool_count = self._diagnostic_tool_count(artifacts, request.attempt_id)
        if tool_count > self.config.max_dynamic_commands:
            raise RuntimeError(
                f"root-cause {role} exceeded the diagnostic command/tool limit: "
                f"{tool_count}>{self.config.max_dynamic_commands}"
            )
        raw = (result.summary or result.stdout or read_text(output_path)).strip()
        report = RootCauseReport.from_dict(_extract_json(raw), role=role)
        write_json(output_path, report.to_dict())
        return report

    @staticmethod
    def _diagnostic_tool_count(
        artifacts: Path,
        attempt_id: str,
    ) -> int:
        report_root = artifacts / "provider-attempts"
        if not report_root.is_dir():
            return 0
        count = 0
        safe_attempt = re.sub(r"[^A-Za-z0-9_.-]+", "-", attempt_id)
        for path in report_root.glob(f"{safe_attempt}-*.json"):
            payload = read_json(path, default={})
            if not isinstance(payload, Mapping):
                continue
            count += sum(
                1
                for event in payload.get("events", []) or []
                if isinstance(event, Mapping)
                and str(event.get("kind", "")) == "tool_completed"
            )
        return count

    def _effort(self) -> str:
        config = getattr(self.orchestrator, "config", None)
        efforts = getattr(config, "efforts", {}) if config is not None else {}
        return str(efforts.get("self_repair", "max")).strip() or "max"

    def _prompt(
        self,
        *,
        role: str,
        evidence: Mapping[str, object],
        prior: object,
    ) -> str:
        role_instruction = {
            "investigator": (
                "Independently investigate the true causal chain. Inspect both repositories, "
                "the Git index/worktree state, retry attempts, logs, and relevant source. "
                "Run bounded non-mutating focused diagnostics when useful."
            ),
            "reviewer": (
                "Act as an independent adversarial reviewer. Recheck evidence and source, "
                "try to falsify the investigator's ownership and causal chain, and report "
                "AGREE, DISAGREE, or UNKNOWN."
            ),
            "arbiter": (
                "Resolve the investigator/reviewer disagreement using concrete source or "
                "runtime invariant evidence. Return FINAL only when ownership is proven; "
                "otherwise return UNKNOWN."
            ),
        }[role]
        schema = {
            "schema_version": 1,
            "role": role,
            "verdict": (
                "ROOT_CAUSE"
                if role == "investigator"
                else "AGREE|DISAGREE|UNKNOWN"
                if role == "reviewer"
                else "FINAL|UNKNOWN"
            ),
            "owner": "one allowed owner",
            "confidence": 0.0,
            "category": "stable_snake_case",
            "generic": False,
            "safe_to_repair": False,
            "causal_chain": ["cause -> mechanism -> terminal symptom"],
            "evidence": [
                {
                    "kind": "source|runtime|git|test",
                    "ref": "path:line or artifact",
                    "claim": "fact",
                }
            ],
            "rejected_hypotheses": [],
            "reproduction_commands": [],
            "reproduction_outcome": "",
            "proposed_fix_scope": [],
            "verification_commands": [],
            "resume_strategy": "repair_and_resume|target_recovery|block",
        }
        return "\n".join(
            [
                f"You are the {role} in a terminal root-cause investigation.",
                role_instruction,
                f"auto_agents repository snapshot: {self.diagnostic_auto_root}",
                f"target project snapshot: {self.diagnostic_target_root}",
                "Both repositories are read-only. Do not edit files, install dependencies, "
                + (
                    "or run commands with external side effects."
                    if self.config.network_enabled
                    else "use network access, or run commands with external side effects."
                ),
                f"Use no more than {self.config.max_dynamic_commands} diagnostic commands; "
                f"each command must finish within {self.config.command_timeout_seconds} seconds.",
                "Separate ownership of the visible symptom from ownership of the mechanism "
                "that made the run terminal. A target-project change can expose an "
                "auto_agents retry, restore, routing, or lifecycle defect.",
                "Do not infer auto_agents ownership from prose alone. Cite inspectable source "
                "or a reproduced runtime invariant/counterfactual. Cite repository-relative "
                "source paths so the evidence remains valid after the snapshots are deleted.",
                "Return exactly one JSON object matching this schema:",
                json.dumps(schema, ensure_ascii=False, indent=2),
                "PRIOR_REPORTS:",
                json.dumps(_report_payload(prior), ensure_ascii=False, indent=2),
                "TERMINAL_EVIDENCE:",
                json.dumps(evidence, ensure_ascii=False, indent=2),
            ]
        )

    def _assert_originals_unchanged(
        self,
        before_auto: str,
        before_target: str,
    ) -> None:
        after_auto = repository_guard_fingerprint(self.auto_agents_root)
        after_target = repository_guard_fingerprint(
            self.target_root,
            ignore_run_artifacts=True,
        )
        if before_auto != after_auto or before_target != after_target:
            raise RuntimeError(
                "root-cause diagnostic mutation invariant failed: a read-only "
                "diagnostic agent modified an original repository"
            )

    @staticmethod
    def _reports_agree(
        investigator: RootCauseReport,
        reviewer: RootCauseReport,
    ) -> bool:
        return (
            reviewer.verdict == "AGREE"
            and reviewer.owner == investigator.owner
            and reviewer.category == investigator.category
        )

    @staticmethod
    def _has_concrete_auto_agents_evidence(report: RootCauseReport) -> bool:
        source_evidence = any(
            item.kind in {"source", "runtime", "test", "git"}
            and (
                "auto_agents" in item.ref
                or "src/" in item.ref
                or item.kind in {"runtime", "test"}
            )
            for item in report.evidence
        )
        return source_evidence and bool(
            report.reproduction_outcome or len(report.causal_chain) >= 2
        )

    def _repair_approved(
        self,
        final: RootCauseReport,
        reviewer: RootCauseReport,
        *,
        threshold: float,
        arbitrated: bool,
    ) -> bool:
        if (
            final.owner != "auto_agents"
            or final.confidence < threshold
            or not final.generic
            or not final.safe_to_repair
            or not self._has_concrete_auto_agents_evidence(final)
        ):
            return False
        if arbitrated:
            return final.verdict == "FINAL"
        return (
            reviewer.verdict == "AGREE"
            and reviewer.owner == "auto_agents"
            and reviewer.confidence >= self.config.confidence_threshold
            and reviewer.generic
            and reviewer.safe_to_repair
            and self._has_concrete_auto_agents_evidence(reviewer)
        )


def _extract_json(raw: str) -> Dict[str, object]:
    candidate = str(raw or "").strip()
    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("root-cause agent output did not contain a JSON object")
        candidate = candidate[start : end + 1]
    payload = json.loads(candidate)
    if not isinstance(payload, dict):
        raise ValueError("root-cause agent output must be a JSON object")
    return payload


def _report_payload(value: object) -> object:
    if isinstance(value, RootCauseReport):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {
            str(key): _report_payload(item)
            for key, item in value.items()
        }
    return None
