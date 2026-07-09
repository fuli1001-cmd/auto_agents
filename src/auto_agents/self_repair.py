from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .git_ops import changed_paths, commit_all
from .io_utils import write_text
from .models import AgentRequest, AgentResult, RunState


SELF_REPAIR_LAST_FINGERPRINT_ENV = "AUTO_AGENTS_SELF_REPAIR_LAST_FINGERPRINT"
SELF_REPAIR_REPEAT_COUNT_ENV = "AUTO_AGENTS_SELF_REPAIR_REPEAT_COUNT"
SELF_REPAIR_DISABLED_ENV = "AUTO_AGENTS_SELF_REPAIR_DISABLED"
SELF_REPAIR_VERIFY_ENV = "AUTO_AGENTS_SELF_REPAIR_VERIFY"
SELF_REPAIR_MAX_CONSECUTIVE_SAME_ERROR = 3


@dataclass
class SelfRepairDecision:
    eligible: bool
    category: str = ""
    reason: str = ""
    fingerprint: str = ""
    repeat_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class SelfRepairResult:
    ok: bool
    status: str
    reason: str
    category: str = ""
    commit_sha: str = ""
    summary: str = ""
    verification: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def auto_agents_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def self_repair_repeat_count(env: Optional[dict[str, str]] = None) -> int:
    raw = (env or os.environ).get(SELF_REPAIR_REPEAT_COUNT_ENV, "0")
    try:
        return max(0, int(str(raw).strip() or "0"))
    except ValueError:
        return 0


def classify_auto_agents_error(
    error: object,
    *,
    state: Optional[RunState] = None,
    env: Optional[dict[str, str]] = None,
) -> SelfRepairDecision:
    values = env or os.environ
    if str(values.get(SELF_REPAIR_DISABLED_ENV, "")).strip().lower() in {"1", "true", "yes"}:
        return SelfRepairDecision(False, reason="self repair is disabled by environment")

    text = str(error or "")
    if not text.strip():
        return SelfRepairDecision(False, reason="empty error")

    lowered = text.lower()
    if "provider research is blocked" in lowered:
        return SelfRepairDecision(False, reason="provider_research blocker has its own recovery path")
    if "preflight validation failed" in lowered:
        return SelfRepairDecision(False, reason="target project preflight failure")
    if "review rejected the task" in lowered:
        return SelfRepairDecision(False, reason="target task review failure")
    if "all providers exhausted" in lowered:
        return SelfRepairDecision(False, reason="provider availability failure")

    if (
        "verification scope mismatch: new failures are outside this task's owned test/proof surface"
        in lowered
    ):
        return _with_repetition_guard(
            SelfRepairDecision(
                True,
                category="verification_scope_mismatch",
                reason=(
                    "task verification stopped on an auto_agents gate-scope classification; "
                    "this is eligible for generic orchestrator repair"
                ),
            ),
            text,
            values,
        )

    if (
        "requirements audit failed:" in lowered
        and "automatic recovery is unsafe" in lowered
        and "forbidden pattern" in lowered
        and "immutable input specification" in lowered
    ):
        return _with_repetition_guard(
            SelfRepairDecision(
                True,
                category="requirements_audit_immutable_input_scope",
                reason=(
                    "requirements audit blocked on immutable input specifications; "
                    "this is eligible for generic audit-scope repair in auto_agents"
                ),
            ),
            text,
            values,
        )

    if (
        "stage clarify modified files outside its ownership during clarify-conv-" in lowered
        and "allowed scope:" in lowered
        and (
            ".auto-agents/docs/project_brief.md" in text
            or ".auto-agents/state/requirements_trace.json" in text
        )
    ):
        return _with_repetition_guard(
            SelfRepairDecision(
                True,
                category="clarify_conversation_mutation_scope",
                reason=(
                    "clarify conversation mutated requirements-owned artifacts before the "
                    "clarify generation step; this is eligible for generic orchestrator repair"
                ),
            ),
            text,
            values,
        )

    if _looks_like_auto_agents_traceback(text):
        return _with_repetition_guard(
            SelfRepairDecision(
                True,
                category="auto_agents_traceback",
                reason="exception traceback points at auto_agents runtime code",
            ),
            text,
            values,
        )

    if state is not None and str(state.current_stage).strip() in {"implement", "verify"}:
        if "generated verification commands are invalid" in lowered:
            return _with_repetition_guard(
                SelfRepairDecision(
                    True,
                    category="generated_verification_contract",
                    reason="auto_agents generated invalid verification command configuration",
                ),
                text,
                values,
            )

    return SelfRepairDecision(False, reason="error is not classified as auto_agents-owned")


def self_repair_error_fingerprint(error: object, category: str) -> str:
    normalized = " ".join(str(error or "").lower().split())
    normalized = re.sub(r"/[^\s:]+/\.auto-agents/[^\s]+", "<auto-agents-path>", normalized)
    normalized = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", normalized)
    payload = f"{category}\0{normalized}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()[:24]


def _with_repetition_guard(
    decision: SelfRepairDecision,
    error_text: str,
    env: Optional[dict[str, str]],
) -> SelfRepairDecision:
    fingerprint = self_repair_error_fingerprint(error_text, decision.category)
    previous_fingerprint = str((env or os.environ).get(SELF_REPAIR_LAST_FINGERPRINT_ENV, "")).strip()
    previous_count = self_repair_repeat_count(env)
    repeat_count = previous_count + 1 if previous_fingerprint == fingerprint else 1
    if repeat_count >= SELF_REPAIR_MAX_CONSECUTIVE_SAME_ERROR:
        return SelfRepairDecision(
            False,
            category=decision.category,
            reason=(
                "same self-repair error repeated "
                f"{SELF_REPAIR_MAX_CONSECUTIVE_SAME_ERROR} consecutive times without repair"
            ),
            fingerprint=fingerprint,
            repeat_count=repeat_count,
        )
    decision.fingerprint = fingerprint
    decision.repeat_count = repeat_count
    return decision


def _looks_like_auto_agents_traceback(text: str) -> bool:
    if "Traceback (most recent call last):" not in text:
        return False
    return bool(re.search(r'File ".*(?:src/)?auto_agents/[^"]+\.py"', text))


def self_repair_verify_commands(env: Optional[dict[str, str]] = None) -> list[str]:
    configured = str((env or os.environ).get(SELF_REPAIR_VERIFY_ENV, "")).strip()
    if configured:
        return [configured]
    return [
        "python -m pytest -q tests/test_project_validation.py -k 'self_repair or legacy_efforts or provider_resolve'",
        "python -m pytest -q tests/test_retry_flow.py -k 'scope or verification_scope or recovery'",
    ]


class AutoAgentsSelfRepairRunner:
    def __init__(
        self,
        target_orchestrator: object,
        *,
        target_project_root: Path,
        error: object,
        decision: SelfRepairDecision,
        print_agent_output: bool = False,
    ) -> None:
        self.target_orchestrator = target_orchestrator
        self.target_project_root = target_project_root
        self.error = error
        self.decision = decision
        self.print_agent_output = print_agent_output
        self.repo_root = auto_agents_repo_root()

    def run(self) -> SelfRepairResult:
        dirty_before = changed_paths(self.repo_root)
        if dirty_before:
            preview = ", ".join(dirty_before[:8])
            return SelfRepairResult(
                ok=False,
                status="failed",
                category=self.decision.category,
                reason=(
                    "auto_agents working tree is not clean before self-repair; "
                    f"changed paths: {preview}"
                ),
            )

        prompt = self._build_prompt()
        prompt_path, output_path = self._artifact_paths()
        write_text(prompt_path, prompt)
        effort = self._effort()
        request = AgentRequest(
            stage="self_repair",
            effort=effort,
            prompt=prompt,
            cwd=self.repo_root,
            output_path=output_path,
            stream_output=(
                self.target_orchestrator._stream_agent_output_callback("self-repair")
                if self.print_agent_output
                and hasattr(self.target_orchestrator, "_stream_agent_output_callback")
                else None
            ),
        )
        result: AgentResult = self.target_orchestrator._call_with_failover(request)
        if hasattr(self.target_orchestrator, "_emit_agent_output"):
            self.target_orchestrator._emit_agent_output("self-repair", result)
        if not result.ok:
            return SelfRepairResult(
                ok=False,
                status="failed",
                category=self.decision.category,
                reason=self._agent_failure_detail(result),
                summary=result.summary or result.stdout,
            )

        summary = (result.summary or result.stdout).strip()
        dirty_after = changed_paths(self.repo_root)
        if not dirty_after:
            return SelfRepairResult(
                ok=False,
                status="failed",
                category=self.decision.category,
                reason="self-repair agent completed without changing auto_agents",
                summary=summary,
            )

        verification = self._run_verification()
        if not verification.ok:
            return SelfRepairResult(
                ok=False,
                status="failed",
                category=self.decision.category,
                reason="self-repair verification failed",
                summary=summary,
                verification=verification.summary,
            )

        commit_message = self._commit_message(summary)
        commit_sha = commit_all(self.repo_root, commit_message)
        return SelfRepairResult(
            ok=True,
            status="completed",
            category=self.decision.category,
            reason=self.decision.reason,
            commit_sha=commit_sha,
            summary=summary,
            verification=verification.summary,
        )

    def _effort(self) -> str:
        config = getattr(self.target_orchestrator, "config", None)
        efforts = getattr(config, "efforts", {}) if config is not None else {}
        return str(efforts.get("self_repair", "max")).strip() or "max"

    def _artifact_paths(self) -> tuple[Path, Path]:
        root = Path(tempfile.gettempdir()) / "auto-agents-self-repair" / uuid.uuid4().hex[:12]
        prompt_path = root / "prompt.txt"
        output_path = root / "output.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        return prompt_path, output_path

    def _build_prompt(self) -> str:
        state_payload = {}
        try:
            from .config import load_run_state

            state_payload = load_run_state(self.target_project_root).to_dict()
        except Exception:
            state_payload = {}

        lines = [
            f"auto_agents repository root: {self.repo_root}",
            f"Target project root (read-only evidence): {self.target_project_root}",
            f"Self-repair category: {self.decision.category}",
            f"Classifier reason: {self.decision.reason}",
            "",
            "Original run error:",
            str(self.error).strip(),
            "",
            "Target run state excerpt:",
            json.dumps(_compact_run_state(state_payload), indent=2, ensure_ascii=False),
            "",
            "Task:",
            "Fix auto_agents itself with a generic orchestrator change.",
            "",
            "Hard scope rules:",
            "- Modify only the auto_agents repository.",
            "- Do not modify the target project.",
            "- Do not hard-code the target project path, task id, spec path, or one-off failure strings.",
            "- Implement a general fix for the auto_agents behavior that produced this error.",
            "- Add or update focused auto_agents tests that prove the generic behavior.",
            "- Preserve existing public CLI behavior except for the new self-repair recovery path.",
            "",
            "Verification expectation:",
            "- Run focused pytest checks for auto_agents before declaring success.",
            "",
            "Final response:",
            "- Briefly summarize the root cause and generic fix.",
            "- Include exactly one COMMIT_MESSAGE line under 72 chars.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _agent_failure_detail(result: AgentResult) -> str:
        parts = []
        if result.stderr:
            parts.append(f"stderr={result.stderr}")
        if result.stdout:
            parts.append(f"stdout={result.stdout[:500]}")
        if result.summary and result.summary != result.stdout:
            parts.append(f"summary={result.summary[:500]}")
        return "; ".join(parts) if parts else "self-repair agent failed without output"

    def _run_verification(self) -> "_VerificationResult":
        summaries = []
        for command in self_repair_verify_commands():
            process = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                cwd=str(self.repo_root),
                timeout=900,
            )
            detail = (process.stderr or process.stdout or "").strip()
            summaries.append(
                f"$ {command}\nexit={process.returncode}\n{detail[:1200]}".strip()
            )
            if process.returncode != 0:
                return _VerificationResult(False, "\n\n".join(summaries))
        return _VerificationResult(True, "\n\n".join(summaries))

    def _commit_message(self, summary: str) -> str:
        for match in re.finditer(r"^COMMIT_MESSAGE:\s*(.+)$", summary, flags=re.MULTILINE):
            subject = _clean_commit_subject(match.group(1))
            if subject:
                return f"fix: {subject}" if not subject.lower().startswith("fix:") else subject
        return "fix: repair auto_agents self-recovery"


@dataclass
class _VerificationResult:
    ok: bool
    summary: str


def _compact_run_state(payload: dict[str, object]) -> dict[str, object]:
    if not payload:
        return {}
    keys = [
        "run_id",
        "status",
        "current_stage",
        "pending_approval",
        "last_error",
        "rejected_stage",
        "rejection_reason",
        "resume_context",
    ]
    compact = {key: payload.get(key) for key in keys if key in payload}
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        compact["tasks"] = [
            {
                "task_id": item.get("task_id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "review_summary": item.get("review_summary"),
                "verify_history": item.get("verify_history", [])[-3:],
            }
            for item in tasks[-5:]
            if isinstance(item, dict)
        ]
    return compact


def _clean_commit_subject(value: str) -> str:
    subject = " ".join(value.replace("`", "").split()).strip(" .,:;!?")
    if not subject:
        return ""
    if re.search(r"(?:^|\s)(?:/|\./|\.\./|[A-Za-z]:[\\/])\S+", subject):
        return ""
    return subject[:72].rstrip(" .,:;!?")


def append_self_repair_history(
    decision: SelfRepairDecision,
    env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    merged = dict(os.environ if env is None else env)
    if decision.fingerprint:
        merged[SELF_REPAIR_LAST_FINGERPRINT_ENV] = decision.fingerprint
        merged[SELF_REPAIR_REPEAT_COUNT_ENV] = str(max(1, decision.repeat_count))
    return merged
