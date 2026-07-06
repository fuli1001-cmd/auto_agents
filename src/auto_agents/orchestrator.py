from __future__ import annotations

import ast
import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, TextIO, Tuple

from .adapters import CodexAdapter, CopilotCliAdapter, AntigravityAdapter, MockAdapter, ShellAdapter
from .agent_instructions import (
    GENERATED_AGENT_INSTRUCTION_PATHS,
    LEGACY_GENERATED_AGENT_INSTRUCTION_PATHS,
    AgentInstructionSyncResult,
    ensure_agent_instructions_synced,
    load_current_normalized_project_rules,
    normalized_project_rules_current,
    normalized_project_rules_identifier_feedback,
    parse_normalized_project_rules_output,
    project_rules_are_meaningful,
    project_rules_source_sha256,
    sync_agent_instructions,
    write_normalized_project_rules,
)
from .repomap import RepoMapBuilder, RepoMapResult
from .config import (
    archived_run_state_path,
    archived_task_plan_path,
    bootstrap_project,
    docs_dir,
    gate_baseline_cache_path,
    load_project_config,
    load_run_state,
    load_task_plan,
    provider_references_dir,
    provider_references_lock_path,
    run_path,
    requirements_audit_path,
    requirements_trace_path,
    review_path,
    run_artifact_paths,
    run_state_path,
    save_project_config,
    save_run_state,
    save_task_plan,
    task_plan_path,
    write_run_prompt,
)
from .gates import (
    build_failure_identity_diagnostic_command,
    commands_from_verification_steps,
    extract_failure_ids,
    extract_failure_info,
    expand_pytest_directory_steps,
    run_gate_plan,
    run_commands,
    run_commands_collect_all,
)
from .gate_baseline_cache import GateBaselineCache
from .git_ops import abort_cherry_pick, add_worktree, changed_entries, changed_files, changed_paths, cherry_pick_no_commit, commit_all, commit_all_except, commit_changed_paths, ensure_repo, hard_reset_clean, head_ref, is_repo, remove_worktree, worktree_fingerprint
from .io_utils import read_json, read_text, write_json, write_text
from .logging_utils import attach_run_file_logger, build_run_logger, log_timing
from .models import (
    APPROVAL_ORDER,
    APPROVAL_BY_STAGE,
    AgentResult,
    AgentRequest,
    AgentUsage,
    DOCUMENT_LANGUAGE_OPTIONS,
    ProviderConfig,
    ProjectConfig,
    GateParallelGroup,
    RunState,
    STAGE_ORDER,
    TaskSpec,
    VerificationStep,
)
from .provider_limits import ParallelTuningStore, provider_limit
from .requirements import (
    external_doc_requirements,
    format_requirement_context,
    forbidden_pattern_findings,
    historical_verified_proofs_by_requirement,
    load_archived_done_task_payloads,
    load_provider_references_lock,
    load_requirements_trace,
    normalize_generated_task_plan_statuses,
    provider_reference_status,
    preserve_task_plan_negative_oracle_clauses,
    run_requirements_audit,
    task_is_fully_historically_covered,
    requirements_for_task,
    validate_done_task_requirement_proofs,
    validate_requirements_trace_payload,
    verified_proofs_by_requirement_from_task_payloads,
)
from .validation import (
    PYTEST_VALUE_OPTIONS,
    _unwrap_conda_run,
    validate_required_document,
    validate_task_dependencies,
    validate_task_plan_with_requirements,
    validate_verification_command_paths,
    validation_report,
)

_FAILOVER_PATTERN = re.compile(
    r"rate.limit|usage.limit|\b429\b|quota|too many requests|capacity|unavailable"
    r"|service.unavailable|not.found|No such file|ENOENT"
    r"|no.last.agent.message|wrote.empty.content|empty.response"
    r"|connection.error|connect.error|timed?\s*out|stalled",
    re.IGNORECASE,
)
_FAILOVER_TIMEOUT_PATTERN = re.compile(r"timed?\s*out|stalled", re.IGNORECASE)
_FAILOVER_QUOTA_PATTERN = re.compile(
    r"rate.limit|usage.limit|\b429\b|quota|too many requests|capacity",
    re.IGNORECASE,
)
_PARALLEL_PROVIDER_PRESSURE_PATTERN = re.compile(
    r"\b(?:"
    r"rate[-\s]?limit(?:ed|s|ing)?"
    r"|usage[-\s]?limit(?:ed|s|ing)?"
    r"|429"
    r"|quota"
    r"|too many requests"
    r"|throttl(?:e|ed|ing)?"
    r"|provider availability"
    r"|all providers exhausted"
    r"|timed?\s*out"
    r"|timeout"
    r"|stall(?:ed|ing)?"
    r")\b",
    re.IGNORECASE,
)


class Orchestrator:
    MAX_SPLIT_DEPTH = 2
    SPLIT_TASK_MARKER = "SPLIT_TASK:"
    ARBITER_MIN_REVIEW_FAILS = 2

    def __init__(
        self,
        project_root: Path,
        agent_output_stream: Optional[TextIO] = None,
        user_input_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config = load_project_config(self.project_root)
        self.adapter = self._build_adapter(self.config)
        self.agent_output_stream = agent_output_stream or sys.stderr
        self.logger = build_run_logger(self.agent_output_stream)
        self._print_agent_output = False
        self._active_spec_file: Optional[Path] = None
        self._user_input_fn = user_input_fn
        self._allow_dirty_tree = False
        # Run-level failover memory (in-memory only, never persisted)
        self._last_successful_provider: Optional[str] = None
        self._failed_providers: Set[str] = set()
        self._current_provider: str = self.config.active_provider
        self._repo_map_builder: Optional[RepoMapBuilder] = None
        self._last_repo_map_result: Optional[RepoMapResult] = None
        self._task_proof_evidence_cache: Dict[Tuple[str, str], Dict[str, object]] = {}
        self._parallel_tuning = ParallelTuningStore(self.project_root)
        self._max_tasks_remaining: Optional[int] = None
        self._task_budget_exhausted = False
        self._active_run_log_path: Optional[Path] = None
        self._gate_baseline_cache = GateBaselineCache(
            self.project_root,
            cache_path=gate_baseline_cache_path(self.project_root),
        )
        # Snapshot of active/deferred REQ IDs captured BEFORE a clarify
        # iteration generation; used by _clarify_validation_feedback to
        # detect silent deletion of existing requirements.
        self._clarify_pre_trace_ids: Set[str] = set()

    def _attach_run_logger(self, run_id: str) -> None:
        if not str(run_id).strip():
            return
        path = run_path(self.project_root, run_id) / "run.log"
        self._active_run_log_path = attach_run_file_logger(self.logger, path)

    def _failed_verification_log_dir(self) -> Path:
        return self.project_root / ".auto-agents" / "failed-verification-logs"

    def _cleanup_failed_verification_logs(self) -> None:
        shutil.rmtree(self._failed_verification_log_dir(), ignore_errors=True)

    def _persist_failed_verification_log(self, raw_output: str, *, label: str) -> str:
        if not raw_output.strip():
            return ""
        log_dir = self._failed_verification_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "verification"
        path = log_dir / f"{safe_label}-{uuid.uuid4().hex[:8]}.log"
        write_text(path, raw_output.rstrip() + "\n")
        try:
            return str(path.relative_to(self.project_root))
        except ValueError:
            return str(path)

    def _run_verify_failure_identity_diagnostic(self, verify_gate: GateResult) -> Optional[GateResult]:
        commands: List[str] = []
        for result in verify_gate.commands:
            if result.ok:
                continue
            diagnostic_command = build_failure_identity_diagnostic_command(result.command)
            if diagnostic_command and diagnostic_command not in commands:
                commands.append(diagnostic_command)
        if not commands:
            return None
        return run_commands_collect_all(commands, self.project_root)

    @staticmethod
    def init_project(
        project_root: Path,
        name: str,
        provider_kind: Optional[str] = None,
        doc_language: str = "en",
    ) -> Path:
        root = bootstrap_project(project_root, name, doc_language=doc_language)
        # Keep backward compatibility for API callers still passing provider_kind,
        # while CLI-level provider selection now happens at run time.
        if provider_kind:
            config = load_project_config(root)
            if provider_kind not in config.providers:
                if provider_kind == "mock":
                    config.providers[provider_kind] = ProviderConfig(
                        kind="mock",
                        binary="mock",
                        profile_map={"balanced": "mock", "deep": "mock", "max": "mock"},
                        extra_args=[],
                        cwd_flag="",
                        prompt_via_stdin=True,
                        output_flag="-o",
                    )
                elif provider_kind == "codex":
                    config.providers[provider_kind] = ProviderConfig(
                        kind="codex",
                        binary="codex",
                        profile_map={"balanced": "balanced", "deep": "deep", "max": "max"},
                        extra_args=[],
                        cwd_flag="-C",
                        prompt_via_stdin=True,
                        output_flag="-o",
                    )
                else:
                    config.providers[provider_kind] = ProviderConfig(
                        kind=provider_kind,
                        binary=provider_kind,
                        profile_map={"balanced": "balanced", "deep": "deep", "max": "max"},
                        extra_args=[],
                        cwd_flag="",
                        prompt_via_stdin=True,
                        output_flag="-o",
                    )
            config.active_provider = provider_kind
            save_project_config(root, config)
        ensure_repo(root, auto_init=True)
        sync_agent_instructions(root)
        return root

    def approve(self, gate: Optional[str] = None) -> RunState:
        state = load_run_state(self.project_root)
        inferred_gate = ""
        if not gate:
            if state.pending_approval:
                inferred_gate = state.pending_approval
            elif state.status == "paused":
                candidate = APPROVAL_BY_STAGE.get(state.current_stage, "")
                if candidate in self.config.approvals.enabled:
                    inferred_gate = candidate
        active_gate = gate or inferred_gate
        if not active_gate:
            raise RuntimeError("No approval gate could be inferred. Pass --gate explicitly.")
        if active_gate not in self.config.approvals.enabled:
            raise RuntimeError(f"Unknown approval gate: {active_gate}")
        if active_gate not in state.approved_gates:
            state.approved_gates.append(active_gate)
        if state.pending_approval == active_gate:
            state.pending_approval = ""
            state.status = "pending"
        elif not state.pending_approval and inferred_gate == active_gate and state.status == "paused":
            state.status = "pending"
        save_run_state(self.project_root, state)
        return state

    def reject(self, gate: Optional[str] = None, reason: str = "") -> RunState:
        state = load_run_state(self.project_root)
        inferred_gate = ""
        if not gate:
            if state.pending_approval:
                inferred_gate = state.pending_approval
            elif state.status == "paused":
                candidate = APPROVAL_BY_STAGE.get(state.current_stage, "")
                if candidate in self.config.approvals.enabled:
                    inferred_gate = candidate
        active_gate = gate or inferred_gate
        if not active_gate:
            raise RuntimeError("No pending gate to reject. Pass --gate explicitly.")

        stage_by_approval = {v: k for k, v in APPROVAL_BY_STAGE.items()}
        target_stage = stage_by_approval.get(active_gate)
        if not target_stage:
            raise RuntimeError(f"Cannot determine stage for gate: {active_gate}")

        # Reset the rejected stage and all downstream stage outputs so run()
        # can rebuild the pipeline from the right point.
        self._rewind_state_from_stage(state, target_stage)

        # Remove the rejected approval and any downstream approvals
        # (e.g. reject architecture should also drop release).
        approval_index = APPROVAL_ORDER.index(active_gate)
        downstream_approvals = set(APPROVAL_ORDER[approval_index:])
        state.approved_gates = [g for g in state.approved_gates if g not in downstream_approvals]
        state.pending_approval = ""
        state.status = "pending"
        state.rejection_reason = reason
        state.rejected_stage = target_stage
        save_run_state(self.project_root, state)
        return state

    def _rewind_state_from_stage(self, state: RunState, target_stage: str) -> None:
        target_index = STAGE_ORDER.index(target_stage)
        for stage in STAGE_ORDER[target_index:]:
            state.stage_summaries.pop(stage, None)
        state.stage_summaries.pop("requirements_audit", None)
        state.stage_summaries.pop("implement_baseline_ref", None)
        state.pending_approval = ""
        state.status = "pending"
        state.current_stage = target_stage
        state.last_error = ""
        if target_index <= STAGE_ORDER.index("plan"):
            state.plan_task_replacements = {}
        if target_index <= STAGE_ORDER.index("implement"):
            state.implement_verify_baseline_failures = []
            state.implement_verify_baseline_ref = ""

    def _normalize_legacy_requirements_audit_resume(self, state: RunState) -> bool:
        last_error = state.last_error.strip()
        exhausted_recovery = last_error.startswith("requirements audit failed after ")
        if not exhausted_recovery and not last_error.startswith("requirements audit failed:"):
            return False
        has_stale_verify = "verify" in state.stage_summaries
        has_stale_audit = "requirements_audit" in state.stage_summaries
        if not exhausted_recovery and not (has_stale_verify or has_stale_audit):
            return False
        self._rewind_state_from_stage(state, "verify")
        state.rejected_stage = ""
        state.rejection_reason = ""
        state.agent_attempts.pop("requirements_audit_recovery", None)
        return True

    @staticmethod
    def _is_requirements_audit_recovery_task(task: Optional[TaskSpec]) -> bool:
        if task is None:
            return False
        if not str(task.task_id).startswith("fix-rejection-"):
            return False
        if str(task.title).strip() != "Fix issues after release rejection":
            return False
        description = str(task.description)
        return (
            "requirements audit failed" in description
            and ".auto-agents/docs/requirements_audit.md" in description
        )

    def _normalize_blocked_requirements_audit_recovery_resume(self, state: RunState) -> bool:
        tasks = list(state.tasks)
        if not tasks:
            try:
                payload = load_task_plan(self.project_root)
            except Exception:
                return False
            raw_tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
            if not isinstance(raw_tasks, list) or not raw_tasks:
                return False
            try:
                tasks = [TaskSpec.from_dict(item) for item in raw_tasks if isinstance(item, dict)]
            except Exception:
                return False
            if not tasks:
                return False
        recovery_tasks = [
            task
            for task in tasks
            if task.status != "done" and self._is_requirements_audit_recovery_task(task)
        ]
        if not recovery_tasks:
            return False

        audit_result = run_requirements_audit(
            self.project_root, tasks, current_spec=self._active_spec_file
        )
        if bool(audit_result.get("ok")):
            return False

        target_stage, hard_failures = self._requirements_audit_route(audit_result)
        if hard_failures or not target_stage or target_stage == "implement":
            return False

        state.tasks = [
            task
            for task in tasks
            if task not in recovery_tasks
        ]
        self._persist_tasks(state.tasks)
        self._rewind_state_from_stage(state, target_stage)
        state.rejection_reason = self._build_requirements_audit_feedback(audit_result, target_stage)
        state.rejected_stage = target_stage
        if self._sanitize_persisted_audit_feedback(state, audit_result) and state.tasks:
            self._persist_tasks(state.tasks)
        return True

    def _normalize_historically_covered_iteration_resume(self, state: RunState) -> bool:
        if not self._is_iteration_run(state):
            return False
        tasks = list(state.tasks)
        if not tasks:
            try:
                tasks = self._load_tasks_from_plan()
            except Exception:
                return False
        if not tasks:
            return False

        trace = load_requirements_trace(self.project_root)
        historical_proofs = historical_verified_proofs_by_requirement(self.project_root, trace)
        if not historical_proofs:
            return False

        retired_ids = {
            task.task_id
            for task in tasks
            if task_is_fully_historically_covered(task, trace, historical_proofs)
        }
        if not retired_ids:
            return False

        retained = [
            task
            for task in tasks
            if task.task_id not in retired_ids and task.parent_task_id.strip() not in retired_ids
        ]
        remaining_ids = {task.task_id for task in retained if task.task_id.strip()}
        changed = len(retained) != len(tasks)
        for task in retained:
            filtered_depends_on = [dependency for dependency in task.depends_on if dependency in remaining_ids]
            if filtered_depends_on != task.depends_on:
                task.depends_on = filtered_depends_on
                changed = True
        if not changed:
            return False

        state.tasks = retained
        self._persist_tasks(retained)
        state.last_error = ""
        if state.rejected_stage == "implement":
            state.rejected_stage = ""
            state.rejection_reason = ""
        if not retained:
            state.stage_summaries["implement"] = (
                "Historical coverage normalization removed stale implementation-only tasks; "
                "no current implementation tasks remain."
            )
            state.current_stage = "verify"
        return True

    @staticmethod
    def _normalize_audit_blocker_path(path: object) -> str:
        normalized = str(path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    @classmethod
    def _forbidden_pattern_owner_stage(cls, blocker: Dict[str, object]) -> str:
        path = cls._normalize_audit_blocker_path(blocker.get("path"))
        if path in {
            ".auto-agents/docs/project_brief.md",
            ".auto-agents/state/requirements_trace.json",
        }:
            return "clarify"
        if path == ".auto-agents/docs/architecture.md":
            return "design"
        if path == ".auto-agents/state/task_plan.json":
            return "plan"
        if (
            path.startswith(".auto-agents/docs/provider_references/")
            or path == ".auto-agents/state/provider_references.lock.json"
        ):
            return "provider_research"
        if path.startswith(".auto-agents/state/"):
            return "verify"
        return "implement"

    @classmethod
    def _unsafe_forbidden_pattern_recovery_reason(cls, blocker: Dict[str, object]) -> str:
        path = cls._normalize_audit_blocker_path(blocker.get("path"))
        if path in {
            ".auto-agents/docs/requirements_audit.md",
            ".auto-agents/docs/review.md",
        }:
            return (
                f"{path} is an orchestrator diagnostic report, not implementation-owned "
                "source-of-truth"
            )
        if path == "spec.md" or path.startswith("specs/"):
            return f"{path} is an immutable input specification"
        return ""

    @classmethod
    def _audit_issue_route(cls, blocker: Dict[str, object]) -> Tuple[Optional[str], str]:
        kind = str(blocker.get("kind", "")).strip()
        message = str(blocker.get("message", "")).strip() or "requirements audit blocker"
        if kind == "forbidden_pattern":
            unsafe_reason = cls._unsafe_forbidden_pattern_recovery_reason(blocker)
            if unsafe_reason:
                return None, f"{message}; automatic recovery is unsafe because {unsafe_reason}"
            return cls._forbidden_pattern_owner_stage(blocker), ""
        if kind == "task_coverage":
            return "plan", ""
        if kind == "provider_reference":
            reference = str(blocker.get("reference", "")).strip()
            ref_status = str(blocker.get("reference_status", "")).strip() or "missing"
            if ref_status == "missing" and reference:
                return "provider_research", ""
            if not reference:
                return None, f"{message}; provider_reference is missing from the requirement record"
            return None, (
                f"{message}; automatic recovery is unsafe because the provider reference "
                f"requires external resolution ({ref_status})"
            )
        if kind in {"oracle_proof_missing", "oracle_proof_invalid"}:
            return "plan", ""
        return None, f"{message}; no automatic recovery route is defined for blocker kind '{kind or 'unknown'}'"

    @staticmethod
    def _audit_blocker_feedback(blocker: Dict[str, object]) -> str:
        kind = str(blocker.get("kind", "")).strip()
        if kind == "forbidden_pattern":
            path = str(blocker.get("path", "")).strip() or "unknown path"
            unsafe_reason = Orchestrator._unsafe_forbidden_pattern_recovery_reason(blocker)
            if unsafe_reason:
                return f"forbidden pattern found in {path} (not auto-fixable: {unsafe_reason})"
            owner_stage = Orchestrator._forbidden_pattern_owner_stage(blocker)
            return f"forbidden pattern found in {path} (owned by {owner_stage})"
        return str(blocker.get("message", "")).strip() or "requirements audit blocker"

    def _requirements_audit_route(self, audit_result: Dict[str, object]) -> Tuple[Optional[str], List[str]]:
        target_stage: Optional[str] = None
        hard_failures: List[str] = []
        for issue in audit_result.get("issues", []):
            if not isinstance(issue, dict) or str(issue.get("result", "")).strip() != "fail":
                continue
            req_id = str(issue.get("requirement_id", "")).strip() or "(unknown requirement)"
            blockers = issue.get("blockers", [])
            if not isinstance(blockers, list):
                continue
            for blocker in blockers:
                if not isinstance(blocker, dict):
                    hard_failures.append(f"{req_id}: invalid audit blocker payload")
                    continue
                candidate, hard_failure = self._audit_issue_route(blocker)
                if hard_failure:
                    hard_failures.append(f"{req_id}: {hard_failure}")
                    continue
                if candidate is None:
                    continue
                if target_stage is None or STAGE_ORDER.index(candidate) < STAGE_ORDER.index(target_stage):
                    target_stage = candidate
        return target_stage, hard_failures

    @staticmethod
    def _sanitize_text_for_patterns(text: str, compiled_patterns: List[re.Pattern[str]]) -> Tuple[str, bool]:
        if not text:
            return text, False
        updated = text
        changed = False
        replacement = "[forbidden pattern omitted; see .auto-agents/docs/requirements_audit.md]"
        for pattern in compiled_patterns:
            updated, count = pattern.subn(replacement, updated)
            if count:
                changed = True
        return updated, changed

    def _sanitize_persisted_audit_feedback(self, state: RunState, audit_result: Dict[str, object]) -> bool:
        compiled_patterns: List[re.Pattern[str]] = []
        for issue in audit_result.get("issues", []):
            if not isinstance(issue, dict):
                continue
            blockers = issue.get("blockers", [])
            if not isinstance(blockers, list):
                continue
            for blocker in blockers:
                if not isinstance(blocker, dict):
                    continue
                if str(blocker.get("kind", "")).strip() != "forbidden_pattern":
                    continue
                raw = str(blocker.get("pattern", "")).strip()
                if not raw:
                    continue
                try:
                    compiled_patterns.append(re.compile(raw))
                except re.error:
                    continue
        if not compiled_patterns:
            return False

        changed = False
        for task in state.tasks:
            task.review_summary, task_changed = self._sanitize_text_for_patterns(task.review_summary, compiled_patterns)
            changed = changed or task_changed
            sanitized_history: List[Dict[str, object]] = []
            for entry in task.review_history:
                if not isinstance(entry, dict):
                    sanitized_history.append(entry)
                    continue
                updated_entry = dict(entry)
                summary = str(updated_entry.get("summary", ""))
                updated_summary, entry_changed = self._sanitize_text_for_patterns(summary, compiled_patterns)
                if entry_changed:
                    updated_entry["summary"] = updated_summary
                    changed = True
                sanitized_history.append(updated_entry)
            task.review_history = sanitized_history

        for cache_entry in state.task_review_cache.values():
            if not isinstance(cache_entry, dict):
                continue
            summary = str(cache_entry.get("summary", ""))
            updated_summary, entry_changed = self._sanitize_text_for_patterns(summary, compiled_patterns)
            if entry_changed:
                cache_entry["summary"] = updated_summary
                changed = True

        for key, value in list(state.stage_summaries.items()):
            updated_value, entry_changed = self._sanitize_text_for_patterns(str(value), compiled_patterns)
            if entry_changed:
                state.stage_summaries[key] = updated_value
                changed = True

        state.rejection_reason, entry_changed = self._sanitize_text_for_patterns(state.rejection_reason, compiled_patterns)
        changed = changed or entry_changed
        state.last_error, entry_changed = self._sanitize_text_for_patterns(state.last_error, compiled_patterns)
        changed = changed or entry_changed
        return changed

    def _requirements_audit_recovery_limit(self) -> int:
        return max(
            self._max_attempts("implement"),
            self._max_attempts("plan"),
            self._max_attempts("provider_research"),
        )

    def _verify_gate_recovery_limit(self) -> int:
        return self._max_attempts("implement")

    def _requirements_audit_recovery_scope_instruction(self, target_stage: str) -> str:
        if target_stage == "plan":
            return (
                f"Fix only the task-planning source of truth at {task_plan_path(self.project_root)}. "
                "Do not edit input specs, project code, tests, README.md, "
                ".auto-agents/docs/requirements_audit.md, .auto-agents/docs/review.md, "
                "project_brief.md, architecture.md, or requirements_trace.json to make the plan pass."
            )
        if target_stage == "clarify":
            return (
                f"Fix only the requirements source of truth at {docs_dir(self.project_root) / 'project_brief.md'} "
                f"and {requirements_trace_path(self.project_root)}. Do not edit input specs, project code, "
                "tests, README.md, or .auto-agents diagnostic reports to make the audit pass."
            )
        if target_stage == "design":
            return (
                f"Fix only the architecture source of truth at {docs_dir(self.project_root) / 'architecture.md'}. "
                "Do not edit input specs, project code, tests, README.md, requirements trace, or "
                ".auto-agents diagnostic reports to make the audit pass."
            )
        if target_stage == "provider_research":
            return (
                "Fix only provider-reference source files under "
                f"{provider_references_dir(self.project_root)} and {provider_references_lock_path(self.project_root)}. "
                "Do not edit input specs, project code, tests, README.md, task plans, or "
                ".auto-agents diagnostic reports to make the audit pass."
            )
        if target_stage == "readme":
            return (
                "Fix only README.md. Do not edit input specs, project code, tests, task plans, requirements "
                "trace, architecture docs, or .auto-agents diagnostic reports to make the audit pass."
            )
        if target_stage == "implement":
            return (
                "Fix the implementation-owned source files and tests that caused the finding. Do not edit "
                ".auto-agents docs/state files directly; for requirement proof updates, use the task's "
                "ORACLE_PROOF_UPDATES mechanism."
            )
        return (
            "Fix the source-of-truth file owned by the routed stage; do not satisfy this by only editing "
            "tests, excluding the flagged path, editing input specs, or asserting that the current failure "
            "is expected."
        )

    def _build_requirements_audit_feedback(self, audit_result: Dict[str, object], target_stage: str) -> str:
        report_path = str(audit_result.get("path", requirements_audit_path(self.project_root)))
        lines = [
            f"The requirements audit failed. Use {report_path} as the source of truth.",
            f"Recovery route: rerun from {target_stage}.",
            "Address every failing mandatory requirement before continuing.",
            self._requirements_audit_recovery_scope_instruction(target_stage),
        ]
        for issue in audit_result.get("issues", []):
            if not isinstance(issue, dict) or str(issue.get("result", "")).strip() != "fail":
                continue
            req_id = str(issue.get("requirement_id", "")).strip() or "(unknown requirement)"
            lines.append(f"- {req_id}:")
            blockers = issue.get("blockers", [])
            if isinstance(blockers, list):
                for blocker in blockers[:4]:
                    if not isinstance(blocker, dict):
                        continue
                    lines.append(f"  - {self._audit_blocker_feedback(blocker)}")
        lines.append(
            "Do not copy forbidden pattern literals verbatim into persisted summaries; refer back to the audit report path instead."
        )
        return "\n".join(lines)

    def _handle_requirements_audit_failure(self, state: RunState, audit_result: Dict[str, object]) -> bool:
        target_stage, hard_failures = self._requirements_audit_route(audit_result)
        report_path = str(audit_result.get("path", requirements_audit_path(self.project_root)))
        if hard_failures:
            detail = "\n".join(f"- {entry}" for entry in hard_failures[:8])
            state.status = "failed"
            state.last_error = (
                f"requirements audit failed: {report_path}\n"
                "Automatic recovery is unsafe for at least one blocker:\n"
                f"{detail}"
            )
            return False
        if not target_stage:
            state.status = "failed"
            state.last_error = f"requirements audit failed: {report_path}"
            return False

        attempts = int(state.agent_attempts.get("requirements_audit_recovery", 0)) + 1
        limit = self._requirements_audit_recovery_limit()
        if attempts > limit:
            state.status = "failed"
            state.last_error = (
                f"requirements audit failed after {limit} automatic recovery attempt(s): {report_path}"
            )
            return False

        state.agent_attempts["requirements_audit_recovery"] = attempts
        self._rewind_state_from_stage(state, target_stage)
        state.rejection_reason = self._build_requirements_audit_feedback(audit_result, target_stage)
        state.rejected_stage = target_stage
        if self._sanitize_persisted_audit_feedback(state, audit_result) and state.tasks:
            self._persist_tasks(state.tasks)
        return True

    def _build_verify_gate_recovery_feedback(
        self,
        verify_gate: GateResult,
        raw_output: str,
        raw_log_path: str,
        *,
        attempt: int,
        limit: int,
    ) -> str:
        extraction = extract_failure_info(verify_gate)
        reason = verify_gate.summary.strip() or "full verification failed"
        failure_ids = self._normalize_verify_failure_ids(extraction.failure_ids, reason)
        retry_feedback = self._build_verify_retry_feedback(
            {
                "reason": reason,
                "current_failure_ids": failure_ids,
                "new_failure_ids": failure_ids,
                "baseline_failure_ids": [],
                "raw_output": raw_output,
                "raw_log_path": raw_log_path,
                "comparable_failures": extraction.comparable,
            }
        )
        guidance = self._verify_failure_recovery_guidance(
            failure_ids=failure_ids,
            reason=reason,
            implicated_paths=list(retry_feedback.get("implicated_paths", [])),
            comparable=extraction.comparable,
        )
        instructions = [
            f"Full verification failed after the implementation stage (automatic recovery attempt {attempt}/{limit}).",
            "Recovery route: rerun implement with a dedicated recovery task.",
            "Determine from the active requirements, task plan, failing tests, and touched code whether the root cause is implementation code, stale tests, or both.",
            "Fix implementation code when product behavior is wrong; update repository tests only when they are stale or contradict active requirements/acceptance oracles.",
            "If the failure exposes an unclear product decision that cannot be resolved from requirements or nearby contracts, leave a concise blocker explaining the ambiguity instead of guessing.",
        ]
        if guidance:
            instructions.extend(["", "Automated triage guidance:", guidance])
        return self._format_retry_feedback(
            "full_verification",
            reason="\n".join(instructions),
            verification_summary=str(retry_feedback.get("verification_summary", "")),
            implicated_paths=list(retry_feedback.get("implicated_paths", [])),
            raw_excerpts=list(retry_feedback.get("raw_excerpts", [])),
        )

    def _handle_verify_gate_failure(
        self,
        state: RunState,
        verify_gate: GateResult,
        *,
        raw_output: str,
        raw_log_path: str,
    ) -> bool:
        attempts = int(state.agent_attempts.get("verify_recovery", 0)) + 1
        limit = self._verify_gate_recovery_limit()
        if attempts > limit:
            feedback = self._build_verify_gate_recovery_feedback(
                verify_gate,
                raw_output,
                raw_log_path,
                attempt=limit,
                limit=limit,
            )
            self._rewind_state_from_stage(state, "clarify")
            state.rejected_stage = "clarify"
            state.rejection_reason = (
                f"Automatic full verification recovery was exhausted after {limit} attempt(s). "
                "Use the clarify conversation to ask the user which behavior is intended when "
                "the active requirements, implementation, and repository tests do not clearly "
                "agree.\n\n"
                f"{feedback}"
            )
            state.agent_attempts.pop("verify_recovery", None)
            return True

        feedback = self._build_verify_gate_recovery_feedback(
            verify_gate,
            raw_output,
            raw_log_path,
            attempt=attempts,
            limit=limit,
        )
        state.agent_attempts["verify_recovery"] = attempts
        self._rewind_state_from_stage(state, "implement")
        state.rejected_stage = "implement"
        state.rejection_reason = feedback
        return True

    def run(
        self,
        spec_file: Path,
        auto_approve: bool = False,
        allow_dirty_tree: bool = False,
        max_tasks: Optional[int] = None,
        skip_validate: bool = False,
        print_agent_output: bool = False,
        doc_language: Optional[str] = None,
        provider_kind: Optional[str] = None,
    ) -> RunState:
        ensure_repo(self.project_root, auto_init=self.config.git.auto_init_repo)
        self._ensure_agent_instructions_synced()
        self._print_agent_output = print_agent_output
        self._allow_dirty_tree = allow_dirty_tree
        try:
            self._cleanup_failed_verification_logs()
            if provider_kind is not None:
                self._set_active_provider(provider_kind)
            if doc_language is not None:
                self._set_document_language(doc_language)
            self._max_tasks_remaining = max_tasks
            self._task_budget_exhausted = False
            state = load_run_state(self.project_root)
            self._attach_run_logger(state.run_id)
            if self._normalize_legacy_requirements_audit_resume(state):
                save_run_state(self.project_root, state)
            resolved_spec_file = spec_file.expanduser().resolve()
            self._active_spec_file = resolved_spec_file
            self._capture_resume_context(
                state,
                spec_file=resolved_spec_file,
                auto_approve=auto_approve,
                allow_dirty_tree=allow_dirty_tree,
                max_tasks=max_tasks,
                skip_validate=skip_validate,
                print_agent_output=print_agent_output,
                provider_kind=provider_kind,
                doc_language=doc_language,
            )
            if self._normalize_historically_covered_iteration_resume(state):
                save_run_state(self.project_root, state)
            if self._normalize_blocked_requirements_audit_recovery_resume(state):
                save_run_state(self.project_root, state)
            self._ensure_preconditions(state, spec_file=spec_file, skip_validate=skip_validate)

            if state.status == "completed":
                self.logger.info("Project execution is already completed. Do you want to start a new iteration for further development? [y/N]")
                user_conf = self._prompt_user("").strip().lower()
                if user_conf in ("y", "yes"):
                    state = self._start_new_iteration(state)
                    self._attach_run_logger(state.run_id)
                    save_run_state(self.project_root, state)
                else:
                    return state

            if state.pending_approval:
                if auto_approve:
                    if state.pending_approval not in state.approved_gates:
                        state.approved_gates.append(state.pending_approval)
                    state.pending_approval = ""
                    state.status = "pending"
                    save_run_state(self.project_root, state)
                else:
                    state.status = "paused"
                    return state

            while True:
                pending = self._pending_stages(state)
                if not pending:
                    break
                stage = pending[0]
                self._emit_stage_start(stage)
                try:
                    with log_timing(self.logger, f"stage:{stage}"):
                        if stage == "implement":
                            state = self._run_implementation_loop(state, max_tasks=max_tasks)
                        elif stage == "provider_research":
                            state = self._run_provider_research(state, spec_file)
                        elif stage == "verify":
                            state = self._run_verify(state)
                        elif stage == "readme":
                            state = self._run_readme(state, spec_file)
                        else:
                            state = self._run_agent_stage(stage, state, spec_file, auto_approve=auto_approve)
                except RuntimeError as error:
                    state.status = "failed"
                    state.last_error = str(error)
                    save_run_state(self.project_root, state)
                    raise

                save_run_state(self.project_root, state)
                if stage == "implement" and self._task_budget_exhausted:
                    state.status = "pending"
                    save_run_state(self.project_root, state)
                    return state
                pending_gate = APPROVAL_BY_STAGE.get(stage)
                if pending_gate and pending_gate in self.config.approvals.enabled and stage in state.stage_summaries:
                    if auto_approve or pending_gate in state.approved_gates:
                        if pending_gate not in state.approved_gates:
                            state.approved_gates.append(pending_gate)
                        state.pending_approval = ""
                        save_run_state(self.project_root, state)
                    else:
                        state.pending_approval = pending_gate
                        state.status = "paused"
                        save_run_state(self.project_root, state)
                        return state

            if self._verify_stage_failed(state):
                state.status = "failed"
                state.last_error = "cannot finalize run while verify summary indicates failure"
                save_run_state(self.project_root, state)
                raise RuntimeError(state.last_error)
            state.status = "completed"
            save_run_state(self.project_root, state)
            self._commit_if_dirty("chore: finalize run state")
            return state
        finally:
            self._print_agent_output = False
            self._active_spec_file = None
            self._allow_dirty_tree = False
            self._max_tasks_remaining = None
            self._task_budget_exhausted = False

    @staticmethod
    def _json_payload_equal(left: object, right: object) -> bool:
        return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
            right,
            sort_keys=True,
            ensure_ascii=False,
        )

    def _write_archive_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = read_json(path, default=None)
            if self._json_payload_equal(existing, payload):
                return
            raise RuntimeError(f"archive already exists with different content: {path}")
        write_json(path, payload)

    def _archive_completed_run(self, state: RunState) -> Tuple[Path, Path]:
        task_archive = archived_task_plan_path(self.project_root, state.run_id)
        state_archive = archived_run_state_path(self.project_root, state.run_id)
        run_state_payload = read_json(run_state_path(self.project_root), default=state.to_dict())
        self._write_archive_json(task_archive, load_task_plan(self.project_root))
        self._write_archive_json(state_archive, run_state_payload)
        return task_archive, state_archive

    def _start_new_iteration(self, state: RunState) -> RunState:
        previous_run_id = state.run_id
        task_archive, _state_archive = self._archive_completed_run(state)
        context = dict(state.resume_context)
        context["previous_run_id"] = previous_run_id
        context["previous_task_plan_archive"] = str(task_archive)

        state.run_id = uuid.uuid4().hex[:12]
        state.status = "pending"
        state.current_stage = "clarify"
        state.pending_approval = ""
        state.approved_gates = []
        state.tasks = []
        state.stage_summaries = {}
        state.agent_attempts = {}
        state.task_review_cache = {}
        state.implement_verify_baseline_failures = []
        state.implement_verify_baseline_ref = ""
        state.plan_task_replacements = {}
        state.last_error = ""
        state.rejection_reason = ""
        state.rejected_stage = ""
        state.resume_context = context
        save_task_plan(self.project_root, {"tasks": []})
        return state

    def _ensure_preconditions(self, state: RunState, spec_file: Path, skip_validate: bool) -> None:
        if not spec_file.exists():
            state.status = "failed"
            state.last_error = f"spec file does not exist: {spec_file}"
            save_run_state(self.project_root, state)
            raise RuntimeError(state.last_error)

        if skip_validate:
            return

        report = validation_report(self.project_root)
        if report["ok"]:
            return

        error_lines = [f"- {item}" for item in report["errors"]]
        if report["warnings"]:
            error_lines.extend(f"- warning: {item}" for item in report["warnings"])
        message = "preflight validation failed:\n" + "\n".join(error_lines)
        state.status = "failed"
        state.last_error = message
        save_run_state(self.project_root, state)
        raise RuntimeError(message)

    def _build_adapter(self, config: ProjectConfig):
        if config.provider.kind == "codex":
            return CodexAdapter(config.provider)
        if config.provider.kind == "copilot-cli":
            return CopilotCliAdapter(config.provider)
        if config.provider.kind == "antigravity":
            return AntigravityAdapter(config.provider)
        if config.provider.kind == "mock":
            return MockAdapter()
        return ShellAdapter(config.provider)

    @staticmethod
    def _is_iteration_run(state: RunState) -> bool:
        if any(task.status == "done" for task in state.tasks):
            return True
        return bool(str(state.resume_context.get("previous_run_id", "")).strip())

    def _previous_task_plan_archive_for_prompt(self) -> str:
        try:
            state = load_run_state(self.project_root)
        except (FileNotFoundError, KeyError, ValueError):
            return ""
        archive = str(state.resume_context.get("previous_task_plan_archive", "")).strip()
        if not archive:
            return ""
        return archive

    def _run_agent_stage(self, stage: str, state: RunState, spec_file: Path, auto_approve: bool = False) -> RunState:
        if stage == "clarify":
            return self._run_interactive_clarify(state, spec_file)

        is_iteration = self._is_iteration_run(state)
        prior_tasks = list(state.tasks)
        if stage == "plan":
            self._plan_prior_done_task_payloads = self._done_task_payloads(prior_tasks)
        prompt = self._build_prompt(stage=stage, spec_file=spec_file, is_iteration=is_iteration)

        if state.rejected_stage == stage and state.rejection_reason:
            prompt += f"\n\nThe previous output was rejected. Please address this feedback:\n{state.rejection_reason}\n"
            state.rejected_stage = ""
            state.rejection_reason = ""

        validator_map = {
            "design": self._design_validation_feedback,
            "plan": self._plan_validation_feedback,
        }
        validator = validator_map.get(stage)
        effort = None
        if stage == "design":
            analysis = self._analyze_spec(spec_file)
            effort = self._effort_for_spec_stage(stage, str(analysis["kind"]))
        try:
            result = self._run_agent_with_retries(
                state=state,
                stage=stage,
                stage_key=stage,
                prompt=prompt,
                validation_feedback=validator,
                effort=effort,
            )
        finally:
            if stage == "plan":
                self._plan_prior_done_task_payloads = []
        state.current_stage = stage
        state.stage_summaries[stage] = result.summary.strip()
        state.last_error = ""
        if stage == "plan":
            self._merge_prior_done_tasks_into_generated_plan(prior_tasks)
            self._apply_generated_verification_config()
            state.tasks = self._load_tasks_from_plan()
            state.plan_task_replacements = self._derive_plan_task_replacements(prior_tasks, state.tasks)
            self._emit_plan_task_count(state.tasks)
        return state

    def _ensure_agent_instructions_synced(self) -> AgentInstructionSyncResult:
        if not project_rules_are_meaningful(self.project_root):
            return ensure_agent_instructions_synced(self.project_root)

        source_sha = project_rules_source_sha256(self.project_root)
        if normalized_project_rules_current(self.project_root):
            return ensure_agent_instructions_synced(self.project_root)

        normalized = load_current_normalized_project_rules(self.project_root)
        if normalized is None or not normalized_project_rules_current(self.project_root):
            normalized = self._normalize_project_rules_with_llm(source_sha)
        return sync_agent_instructions(self.project_root, normalized_rules=normalized)

    def _normalize_project_rules_with_llm(self, source_sha: str) -> Dict[str, List[str]]:
        source_path = self.project_root / ".auto-agents" / "project-rules.md"
        source_text = read_text(source_path).strip()
        if not source_text:
            rules = {
                "hard_rules": [],
                "workflow_contracts": [],
                "engineering_validation": [],
                "testing_contracts": [],
            }
            write_normalized_project_rules(
                self.project_root,
                source_sha256=source_sha,
                rules=rules,
            )
            return rules

        prompt = "\n".join([
            "Normalize the human-readable project rules into strict JSON for agent instruction generation.",
            "",
            "Return ONLY a JSON object with this exact shape:",
            "{",
            '  "rules": {',
            '    "hard_rules": ["short imperative rules that must always hold"],',
            '    "workflow_contracts": ["state-machine, product flow, and API behavior contracts"],',
            '    "engineering_validation": ["deterministic validation and artifact integrity rules"],',
            '    "testing_contracts": ["test expectation and regression coverage rules"]',
            "  }",
            "}",
            "",
            "Rules:",
            "- Preserve precise product semantics and enum values.",
            "- Preserve source identifiers, state names, stage names, API names, enum values, and quoted terms verbatim.",
            "- Never replace a source identifier with a similar implementation term or inferred synonym.",
            "- Do not copy background explanation, examples, headings, or process diagrams unless they are actual rules.",
            "- Do not convert rationale or descriptive context into migration, deletion, or implementation tasks.",
            "- Prefer rules that constrain default paths, state transitions, enum values, gates, artifact validity, and test expectations.",
            "- Keep each item concise and standalone; avoid pronouns like 'this step' or 'these two'.",
            "- Do not invent rules not supported by the source.",
            "- If a category has no rules, use an empty array.",
            "",
            f"Source file: {source_path}",
            "",
            "Source markdown:",
            source_text,
        ])
        effort = self.config.efforts.get(
            "sync-agent-instructions",
            self.config.efforts.get("plan", "deep"),
        )
        result = self._run_agent_with_retries(
            state=None,
            stage="sync-agent-instructions",
            stage_key="sync-agent-instructions",
            prompt=prompt,
            validation_feedback=lambda result: self._normalized_project_rules_validation_feedback(
                result,
                source_text=source_text,
            ),
            effort=effort,
        )
        rules = parse_normalized_project_rules_output(result.summary)
        write_normalized_project_rules(
            self.project_root,
            source_sha256=source_sha,
            rules=rules,
        )
        return rules

    @staticmethod
    def _normalized_project_rules_validation_feedback(
        result: AgentResult,
        *,
        source_text: str = "",
    ) -> Optional[str]:
        try:
            rules = parse_normalized_project_rules_output(result.summary)
        except ValueError as error:
            return str(error)
        if source_text.strip():
            return normalized_project_rules_identifier_feedback(
                source_text=source_text,
                rules=rules,
            )
        return None

    def _run_interactive_clarify(self, state: RunState, spec_file: Path) -> RunState:
        from .config import conversation_history_path
        import json
        
        history_path = conversation_history_path(self.project_root, state.run_id)
        history = []
        if history_path.exists():
            try:
                history = json.loads(read_text(history_path))
            except Exception:
                pass

        post_rejection = False
        if state.rejected_stage == "clarify" and state.rejection_reason:
            history.append({
                "role": "user",
                "content": (
                    "The previous requirements output was rejected. Treat this as additional user feedback.\n"
                    "Use the existing conversation and generated files as context, and revise only the affected requirements.\n"
                    f"Feedback:\n{state.rejection_reason}"
                )
            })
            state.rejected_stage = ""
            state.rejection_reason = ""
            post_rejection = True

        def _history_role(msg: object) -> str:
            if not isinstance(msg, dict):
                return ""
            role = str(msg.get("role", "")).strip().lower()
            if role == "assistant":
                return "agent"
            return role

        # Detect crash-resume: the last agent message has READY_TO_GENERATE
        # but the brief was never generated (we wouldn't be here otherwise).
        # Instead of discarding the conversation, re-prompt the user.
        resume_to_confirm = False
        if not post_rejection and history:
            for msg in reversed(history):
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role", "")).lower()
                if role in ("agent", "assistant"):
                    if "READY_TO_GENERATE" in str(msg.get("content", "")):
                        resume_to_confirm = True
                    break

        confirmed_generation = False

        def _record_clarify_feedback(user_reply: str) -> None:
            feedback = user_reply.strip()
            if feedback:
                history.append({"role": "user", "content": user_reply})
            else:
                history.append({
                    "role": "user",
                    "content": (
                        "I am not ready to generate the project brief yet. "
                        "Please continue clarifying the requirements with me."
                    ),
                })
            write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))
            self.logger.info("\nAgent is thinking, please wait...")

        if resume_to_confirm:
            # Show the agent's last reply (minus the marker) and re-ask.
            last_agent_content = ""
            for msg in reversed(history):
                if isinstance(msg, dict) and str(msg.get("role", "")).lower() in ("agent", "assistant"):
                    last_agent_content = str(msg.get("content", ""))
                    break
            display = last_agent_content.replace("READY_TO_GENERATE", "").strip()
            if display:
                self.logger.info("\n[Resuming previous conversation]")
                self.logger.info("\nAgent:")
                self.logger.info(display)
            self.logger.info("\nAgent is ready to generate project_brief.md.")
            user_conf = self._prompt_user("Confirm generation? (y/n) [y]: ", default="y")
            if user_conf.strip().lower() not in ("n", "no"):
                confirmed_generation = True
            else:
                user_reply = self._prompt_user("Please provide your thoughts: ", multiline=True)
                _record_clarify_feedback(user_reply)
        else:
            # Resume interrupted conversation: if trailing history entries
            # are from the agent (e.g. process crashed before user reply was
            # saved), replay the last substantive agent message and collect
            # a fresh user reply.
            if history and _history_role(history[-1]) == "agent":
                trailing = []
                while history and _history_role(history[-1]) == "agent":
                    trailing.insert(0, history.pop())
                replay_msg = None
                for msg in trailing:
                    if not isinstance(msg, dict):
                        continue
                    content = str(msg.get("content", ""))
                    if "READY_TO_GENERATE" not in content:
                        replay_msg = {"role": "agent", "content": content}
                        break
                if replay_msg:
                    history.append(replay_msg)
                    self.logger.info("\n[Resuming previous conversation]")
                    self.logger.info("\nAgent:")
                    self.logger.info(replay_msg["content"])
                    user_reply = self._prompt_user("\nYour reply: ", multiline=True)
                    if user_reply.strip():
                        history.append({"role": "user", "content": user_reply})
                    else:
                        history.append({"role": "user", "content": "I have nothing to add. Please proceed to generate if you are ready."})
                write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))

        if not confirmed_generation:
            self.logger.info("Entering interactive clarify session, please wait for the agent to analyze the spec...")

            max_rounds = 15
            rounds = 0

            while rounds < max_rounds:
                rounds += 1
                prompt_lines = [
                    f"Project root: {self.project_root}",
                    "Read the input spec from: " + str(spec_file),
                    "Clarify will later generate both project_brief.md and .auto-agents/state/requirements_trace.json.",
                    "During these clarify conversation turns, do NOT modify repository files yet; ask questions and reason only.",
                    "As you discuss requirements, identify concrete mandatory requirements, non-goals, acceptance oracles, forbidden patterns, and any external provider docs needed.",
                    "If the project repository already contains an active codebase and a history of completed tasks, please review them to understand the current progress before discussing the next features.",
                    "You are an expert product manager analyzing the spec.",
                    "Your goal is to extract the target scope, requirements, constraints, and non-goals.",
                    "Ask the user questions to clarify the requirements if needed.",
                    "If the spec is already well-defined, ask for confirmation.",
                    self._document_language_instruction(),
                    "Only output 'READY_TO_GENERATE' on a line by itself at the very end when ALL of the following are true: "
                    "(1) you have explicitly answered every question in the user's most recent message, "
                    "(2) the user's last reply does not contain any unanswered questions or requests for clarification, and "
                    "(3) you have gathered sufficient information to write the project brief. "
                    "Do NOT output 'READY_TO_GENERATE' if the user asked anything that you have not yet fully answered.",
                    "\n--- Conversation History ---",
                ]
                if post_rejection:
                    prompt_lines.extend([
                        "This is a revision pass after a requirements rejection.",
                        "Do not restart discovery unless the rejection feedback requires it.",
                        "Use the existing conversation and generated artifacts as context, and focus on correcting the rejected parts.",
                        f"Review the existing project brief if present: {docs_dir(self.project_root) / 'project_brief.md'}",
                        f"Review the existing requirements trace if present: {requirements_trace_path(self.project_root)}",
                    ])

                for msg in history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    prompt_lines.append(f"\n[{role.upper()}]:\n{content}")

                prompt = "\n".join(prompt_lines)

                effort = self._effort_for_spec_stage("clarify", str(self._analyze_spec(spec_file)["kind"]))

                result = self._run_agent_with_retries(
                    state=state,
                    stage="clarify",
                    stage_key=f"clarify-conv-{len(history)}",
                    prompt=prompt,
                    effort=effort,
                )

                reply = result.summary.strip()
                if not reply:
                    reply = result.stdout.strip()

                history.append({"role": "agent", "content": reply})
                write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))

                if "READY_TO_GENERATE" in reply:
                    display_reply = reply.replace("READY_TO_GENERATE", "").strip()
                    if display_reply:
                        self.logger.info("\nAgent:")
                        self.logger.info(display_reply)
                    if post_rejection:
                        confirmed_generation = True
                        post_rejection = False
                        break
                    self.logger.info("\nAgent is ready to generate project_brief.md.")
                    user_conf = self._prompt_user("Confirm generation? (y/n) [y]: ", default="y")

                    if user_conf.strip().lower() not in ("n", "no"):
                        confirmed_generation = True
                        break
                    else:
                        user_reply = self._prompt_user("Please provide your thoughts: ", multiline=True)
                        _record_clarify_feedback(user_reply)
                        continue

                # After rejection, show the agent's response (stripping the
                # READY_TO_GENERATE marker) and force user interaction so the
                # user can review how the agent addressed the feedback.
                display_reply = reply.replace("READY_TO_GENERATE", "").strip() if post_rejection else reply
                post_rejection = False

                self.logger.info("\nAgent:")
                self.logger.info(display_reply)

                user_reply = self._prompt_user("\nYour reply: ", multiline=True)

                if user_reply.strip():
                    history.append({"role": "user", "content": user_reply})
                else:
                    history.append({"role": "user", "content": "I have nothing to add. Please proceed to generate if you are ready."})

                write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))
                self.logger.info("\nAgent is thinking, please wait...")

        if not confirmed_generation:
            raise RuntimeError(
                "Clarify ended without explicit confirmation to generate project_brief.md."
            )

        # Generate the actual project brief
        self.logger.info("\nGenerating project_brief.md, please wait...")
        is_iteration = self._is_iteration_run(state)
        # Snapshot active/deferred REQ IDs so _clarify_validation_feedback can
        # detect silent deletion (iteration must use status='superseded' rather
        # than removal). Empty set on first run.
        if is_iteration:
            self._clarify_pre_trace_ids = self._active_or_deferred_req_ids(
                load_requirements_trace(self.project_root)
            )
        else:
            self._clarify_pre_trace_ids = set()
        generate_prompt = self._build_prompt(stage="clarify", spec_file=spec_file, is_iteration=is_iteration)
        if history:
            generate_prompt += "\n\n--- Conversation History ---\n"
            for msg in history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                generate_prompt += f"\n[{role.upper()}]:\n{content}"
            generate_prompt += "\n\nBased on the spec and conversation above, output the required project brief."
        
        effort = self._effort_for_spec_stage("clarify", str(self._analyze_spec(spec_file)["kind"]))
        result = self._run_agent_with_retries(
            state=state,
            stage="clarify",
            stage_key="clarify-generate",
            prompt=generate_prompt,
            validation_feedback=self._clarify_validation_feedback,
            effort=effort,
        )
        state.current_stage = "clarify"
        state.stage_summaries["clarify"] = result.summary.strip()
        state.last_error = ""
        return state

    def _effort_for_spec_stage(self, stage: str, spec_kind: str) -> str:
        """Choose effort for clarify/design based on spec type.

        When the input spec is already a detailed design document, clarify and
        design are mostly extraction/normalization work and can use the cheaper
        balanced effort.  When the spec is a rough idea, deeper reasoning is
        needed to synthesize requirements and architecture from scratch.
        """
        configured = self.config.efforts.get(stage, "deep")
        if configured not in ("deep", "balanced"):
            return configured
        if spec_kind == "design":
            return "balanced"
        return "deep"

    def _prompt_user(self, prompt: str, default: str = "", multiline: bool = False) -> str:
        if self._user_input_fn:
            return self._user_input_fn(prompt)
        if "unittest" in sys.modules:
            return default
        if sys.stdin.isatty():
            if multiline:
                self.logger.info(prompt + " (Press Ctrl+D or Ctrl+Z to submit):")
                try:
                    text = sys.stdin.read()
                except EOFError:
                    text = ""
                except UnicodeDecodeError:
                    # stdin encoding doesn't match actual bytes; re-read from
                    # the underlying binary buffer with lossy UTF-8 decoding.
                    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
                # Reopen stdin from the terminal so subsequent reads work.
                self._reopen_stdin_from_tty()
                # Fix surrogate escapes from Windows console encoding mismatches
                return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
            else:
                return self._read_single_line_input(prompt, default)
        if not multiline and self._reopen_stdin_from_tty():
            return self._read_single_line_input(prompt, default)
        return default

    def _reopen_stdin_from_tty(self) -> bool:
        try:
            tty = "/dev/tty" if os.path.exists("/dev/tty") else "CON"
            sys.stdin = open(tty, "r", encoding="utf-8", errors="surrogateescape")
            return True
        except OSError:
            return False

    def _read_single_line_input(self, prompt: str, default: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            if self._reopen_stdin_from_tty():
                try:
                    return input(prompt)
                except EOFError:
                    return default
            return default

    @staticmethod
    def _review_fingerprint(summary: str) -> str:
        """Normalize a review summary and hash it for stable fingerprinting.

        Strips surrounding whitespace, lowercases, collapses runs of
        whitespace/punctuation, and SHA-256 hashes the normalized form so
        semantically identical blocker lists produce identical fingerprints
        across retries.
        """
        text = (summary or "").strip().lower()
        if not text:
            return ""
        normalized = re.sub(r"[\s\W_]+", " ", text).strip()
        if not normalized:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _build_arbiter_prompt(self, task: TaskSpec, last_review: str) -> str:
        history_lines: List[str] = []
        for idx, entry in enumerate(task.review_history[-6:], start=1):
            if not isinstance(entry, dict):
                continue
            summary = str(entry.get("summary", "")).strip()
            attempt = entry.get("attempt", idx)
            if summary:
                history_lines.append(f"--- review attempt {attempt} ---\n{summary}")
        verify_lines: List[str] = []
        for entry in task.verify_history[-4:]:
            if not isinstance(entry, dict):
                continue
            attempt = entry.get("attempt", "?")
            ids = entry.get("failure_ids") or []
            outcome = entry.get("outcome", "")
            ids_str = ", ".join(str(x) for x in (ids or [])[:8]) if isinstance(ids, list) else ""
            verify_lines.append(f"attempt {attempt} ({outcome}): {ids_str}")
        try:
            paths = changed_paths(self.project_root)
        except Exception:
            paths = []
        task_brief = {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
            "acceptance": list(task.acceptance),
            "requirement_ids": list(task.requirement_ids),
            "split_depth": int(task.split_depth),
            "parent_task_id": task.parent_task_id,
        }
        prompt_parts = [
            "You are the SCOPE ARBITER. Your sole job is to decide whether the current task is",
            "too coupled / too large to land in one implement+review cycle, given the failure",
            "history below. You are NOT reviewing code correctness; the review agent already did",
            "that. You judge task SIZING.",
            "This stage is read-only. Do not modify any repository files.",
            "",
            "Decide ONE of:",
            "  - CONTINUE: the task is the right size; the implementer just needs another",
            "    attempt with sharper guidance. Pick this when the same root cause keeps coming",
            "    back due to a fixable mistake (missing one call site, wrong file, lint error).",
            "  - SPLIT: the task spans too many independent slices to converge. Pick this when",
            "    multiple distinct subsystems / layers / acceptance criteria fail repeatedly,",
            "    or when each retry trades one blocker for another in different code regions.",
            "",
            "OUTPUT FORMAT (strict, machine-parsed):",
            "  Line 1: 'DECISION: CONTINUE' or 'DECISION: SPLIT' (uppercase, exact).",
            "  Line 2: 'RATIONALE: <one or two sentences>'.",
            "  Lines 3+: when DECISION is SPLIT, add 'SPLIT_AXIS:' followed by 2-4 bullet",
            "  points, each naming one coherent slice the parent task should be split into",
            "  (e.g. '- backend: stale-flag propagation in regen entrypoints',",
            "  '- API: query-side filtering of stale results').",
            "  No other text. No code fences. No preamble.",
            "",
            f"Task brief:\n{json.dumps(task_brief, indent=2, ensure_ascii=False)}",
            "",
            "Most recent review verdict (latest first):",
            last_review.strip() or "(no current review summary)",
        ]
        if history_lines:
            prompt_parts.extend(["", "Prior review history:", *history_lines])
        if verify_lines:
            prompt_parts.extend(["", "Verify history:", *verify_lines])
        if paths:
            prompt_parts.extend([
                "",
                f"Files touched in current attempt ({len(paths)}):",
                *[f"  - {p}" for p in paths[:30]],
            ])
        if int(task.split_depth) >= self.MAX_SPLIT_DEPTH:
            prompt_parts.extend([
                "",
                f"NOTE: split_depth={task.split_depth} is at MAX_SPLIT_DEPTH={self.MAX_SPLIT_DEPTH}.",
                "Further SPLIT will be rejected. Prefer CONTINUE unless splitting is the only viable path.",
            ])
        return "\n".join(prompt_parts)

    @staticmethod
    def _parse_arbiter_decision(text: str) -> Dict[str, object]:
        decision = ""
        rationale = ""
        split_axis: List[str] = []
        section = ""
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("DECISION:"):
                value = line.split(":", 1)[1].strip().upper()
                if value.startswith("SPLIT"):
                    decision = "SPLIT"
                elif value.startswith("CONTINUE"):
                    decision = "CONTINUE"
                section = ""
                continue
            if upper.startswith("RATIONALE:"):
                rationale = line.split(":", 1)[1].strip()
                section = "rationale"
                continue
            if upper.startswith("SPLIT_AXIS") or upper.startswith("SPLIT AXIS"):
                section = "split_axis"
                tail = line.split(":", 1)[1].strip() if ":" in line else ""
                if tail:
                    split_axis.append(tail.lstrip("-* ").strip())
                continue
            if section == "split_axis" and (line.startswith("-") or line.startswith("*")):
                split_axis.append(line.lstrip("-* ").strip())
            elif section == "rationale" and not rationale:
                rationale = line
        return {
            "decision": decision,
            "rationale": rationale,
            "split_axis": [item for item in split_axis if item],
        }

    def _run_scope_arbiter(
        self,
        run_id: str,
        task: TaskSpec,
        last_review: str,
    ) -> Dict[str, object]:
        """Invoke the arbiter agent and return a parsed decision dict.

        Always returns a dict with keys: decision ('SPLIT'|'CONTINUE'|''),
        rationale, split_axis (list), raw (raw agent text), error (str if any).
        Errors and parse failures are mapped to CONTINUE so the loop never
        gets stuck waiting on the arbiter.
        """
        prompt = self._build_arbiter_prompt(task, last_review)
        effort = self.config.efforts.get("arbiter", "balanced")
        try:
            result = self._run_agent_with_retries(
                state=None,
                stage="arbiter",
                stage_key=f"arbiter-{task.task_id}",
                prompt=prompt,
                run_id=run_id,
                effort=effort,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "decision": "CONTINUE",
                "rationale": f"arbiter invocation failed: {exc}",
                "split_axis": [],
                "raw": "",
                "error": str(exc),
            }
        raw = (result.summary or "").strip()
        parsed = self._parse_arbiter_decision(raw)
        if parsed["decision"] not in {"SPLIT", "CONTINUE"}:
            parsed["decision"] = "CONTINUE"
            if not parsed.get("rationale"):
                parsed["rationale"] = "arbiter output unparseable; defaulting to CONTINUE"
        parsed["raw"] = raw
        parsed["error"] = ""
        return parsed

    def _worktree_change_snapshot(self) -> Dict[str, str]:
        snapshot: Dict[str, str] = {}
        for status, path in changed_entries(self.project_root, ignored_prefixes=()):
            if path.startswith(".antigravitycli/"):
                continue
            hasher = hashlib.sha256()
            hasher.update(status.encode("utf-8"))
            hasher.update(b"\0")
            file_path = self.project_root / path
            if file_path.is_file():
                hasher.update(file_path.read_bytes())
            elif file_path.exists():
                hasher.update(b"[dir]")
            else:
                hasher.update(b"[missing]")
            snapshot[path] = hasher.hexdigest()
        return snapshot

    @staticmethod
    def _is_ephemeral_tooling_artifact(
        path: str,
        *,
        tracked: bool = False,
        include_untracked_build_lib: bool = True,
    ) -> bool:
        normalized = str(path).replace("\\", "/").lower()
        if normalized.endswith(".tsbuildinfo"):
            return True
        if (
            include_untracked_build_lib
            and not tracked
            and (normalized.startswith("build/lib/") or normalized.startswith("build/lib."))
        ):
            return True
        return False

    def _cleanup_ephemeral_tooling_artifacts(self, *, include_untracked_build_lib: bool = True) -> None:
        for _, path in changed_entries(self.project_root, ignored_prefixes=()):
            if path.startswith(".antigravitycli/"):
                continue
            file_path = self.project_root / path
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path],
                cwd=str(self.project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            ).returncode == 0
            if not self._is_ephemeral_tooling_artifact(
                path,
                tracked=tracked,
                include_untracked_build_lib=include_untracked_build_lib,
            ):
                continue
            if tracked:
                restore = subprocess.run(
                    ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", path],
                    cwd=str(self.project_root),
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                )
                if restore.returncode != 0:
                    raise RuntimeError(restore.stderr.strip() or f"git restore failed for {path}")
                continue
            if file_path.is_dir():
                shutil.rmtree(file_path)
            elif file_path.exists() or file_path.is_symlink():
                file_path.unlink()

    @staticmethod
    def _snapshot_delta_paths(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
        return sorted(
            path for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )

    @staticmethod
    def _changed_path_preview(paths: Iterable[str], limit: int = 5) -> str:
        normalized = [str(path).strip() for path in paths if str(path).strip()]
        if not normalized:
            return ""
        preview = ", ".join(normalized[:limit])
        if len(normalized) > limit:
            preview += f", +{len(normalized) - limit} more"
        return preview

    def _capture_auto_agents_restore_point(self, restore_root: Path) -> None:
        restore_relatives = [
            ".auto-agents/.gitignore",
            ".auto-agents/config.json",
            ".auto-agents/state",
            ".auto-agents/docs",
            ".auto-agents/history",
            "spec.md",
            "specs",
        ]
        if self._active_spec_file is not None:
            try:
                restore_relatives.append(self._relative_repo_path(self._active_spec_file))
            except ValueError:
                pass
        for relative in restore_relatives:
            source = self.project_root / relative
            target = restore_root / relative
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.exists() or source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)

    def _restore_paths_from_restore_point(self, paths: Iterable[str], restore_root: Path) -> None:
        for relative in sorted({str(path).strip() for path in paths if str(path).strip()}):
            target = self.project_root / relative
            source = restore_root / relative
            if target.is_dir() and not source.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.exists() or source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)

    def _is_implement_restorable_scope_violation_path(self, path: str) -> bool:
        normalized = str(path).replace("\\", "/").strip()
        if not normalized:
            return False
        if normalized.startswith(".auto-agents/"):
            return True
        if normalized == "spec.md" or normalized.startswith("specs/"):
            return True
        if self._active_spec_file is not None:
            try:
                return normalized == self._relative_repo_path(self._active_spec_file)
            except ValueError:
                return False
        return False

    def _relative_repo_path(self, path: Path) -> str:
        return str(path.relative_to(self.project_root)).replace("\\", "/")

    def _stage_mutation_policy(
        self,
        *,
        stage: str,
        stage_key: str,
        run_id: str,
    ) -> Tuple[List[str], Callable[[str], bool]]:
        run_prefix = f".auto-agents/runs/{run_id}/"
        brief_path = self._relative_repo_path(docs_dir(self.project_root) / "project_brief.md")
        architecture_path = self._relative_repo_path(docs_dir(self.project_root) / "architecture.md")
        trace_path = self._relative_repo_path(requirements_trace_path(self.project_root))
        plan_path = self._relative_repo_path(task_plan_path(self.project_root))
        readme_path = "README.md"
        provider_lock_path = self._relative_repo_path(provider_references_lock_path(self.project_root))
        provider_refs_prefix = self._relative_repo_path(provider_references_dir(self.project_root)).rstrip("/") + "/"
        run_state_rel = self._relative_repo_path(run_state_path(self.project_root))
        auto_gitignore_rel = ".auto-agents/.gitignore"
        protected_input_specs = {"spec.md"}
        if self._active_spec_file is not None:
            try:
                protected_input_specs.add(self._relative_repo_path(self._active_spec_file))
            except ValueError:
                pass

        def is_implementation_owned_path(path: str) -> bool:
            return (
                not path.startswith(".auto-agents/")
                and not path.startswith("specs/")
                and path not in protected_input_specs
            )

        if stage == "clarify":
            if stage_key == "clarify-generate":
                allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel, brief_path, trace_path]
                return allowed, lambda path: path.startswith(run_prefix) or path in {run_state_rel, auto_gitignore_rel, brief_path, trace_path}
            allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel]
            return allowed, lambda path: path.startswith(run_prefix) or path in {run_state_rel, auto_gitignore_rel}

        if stage == "design":
            allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel, architecture_path]
            return allowed, lambda path: path.startswith(run_prefix) or path in {run_state_rel, auto_gitignore_rel, architecture_path}

        if stage == "plan":
            allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel, plan_path]
            return allowed, lambda path: path.startswith(run_prefix) or path in {run_state_rel, auto_gitignore_rel, plan_path}

        if stage == "provider_research":
            allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel, provider_lock_path, f"{provider_refs_prefix}**"]
            return allowed, (
                lambda path: path.startswith(run_prefix)
                or path == run_state_rel
                or path == auto_gitignore_rel
                or path == provider_lock_path
                or path.startswith(provider_refs_prefix)
            )

        if stage == "readme":
            if stage_key.startswith("readme-propose"):
                allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel]
                return allowed, lambda path: path.startswith(run_prefix) or path in {run_state_rel, auto_gitignore_rel}
            allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel, readme_path]
            return allowed, lambda path: path.startswith(run_prefix) or path in {run_state_rel, auto_gitignore_rel, readme_path}

        if stage == "implement":
            if stage_key.startswith("implement-repair-"):
                allowed = [
                    f"{run_prefix}**",
                    run_state_rel,
                    auto_gitignore_rel,
                    plan_path,
                    provider_lock_path,
                    f"{provider_refs_prefix}**",
                    "any non-.auto-agents project path except input specs (spec.md, specs/**, active spec file)",
                ]
                return allowed, (
                    lambda path: path.startswith(run_prefix)
                    or path in {run_state_rel, auto_gitignore_rel, plan_path, provider_lock_path}
                    or path.startswith(provider_refs_prefix)
                    or is_implementation_owned_path(path)
                )
            allowed = [
                f"{run_prefix}**",
                run_state_rel,
                auto_gitignore_rel,
                "any non-.auto-agents project path except input specs (spec.md, specs/**, active spec file)",
            ]
            return allowed, (
                lambda path: path.startswith(run_prefix)
                or path in {run_state_rel, auto_gitignore_rel}
                or is_implementation_owned_path(path)
            )

        allowed = [f"{run_prefix}**", run_state_rel, auto_gitignore_rel]
        return allowed, lambda path: path.startswith(run_prefix) or path in {run_state_rel, auto_gitignore_rel}

    def _assert_stage_mutation_scope(
        self,
        *,
        stage: str,
        stage_key: str,
        run_id: str,
        before_snapshot: Dict[str, str],
    ) -> None:
        violation = self._stage_mutation_scope_violation(
            stage=stage,
            stage_key=stage_key,
            run_id=run_id,
            before_snapshot=before_snapshot,
        )
        if violation is None:
            return
        offending, allowed_scope = violation
        raise RuntimeError(
            f"stage {stage} modified files outside its ownership during {stage_key}. "
            f"Changed paths: {self._changed_path_preview(offending)}. "
            f"Allowed scope: {'; '.join(allowed_scope)}."
        )

    def _stage_mutation_scope_violation(
        self,
        *,
        stage: str,
        stage_key: str,
        run_id: str,
        before_snapshot: Dict[str, str],
    ) -> Optional[Tuple[List[str], List[str]]]:
        after_snapshot = self._worktree_change_snapshot()
        delta_paths = [
            path
            for path in self._snapshot_delta_paths(before_snapshot, after_snapshot)
            if not self._is_orchestrator_diagnostic_path(path)
        ]
        if not delta_paths:
            return None
        allowed_scope, is_allowed = self._stage_mutation_policy(
            stage=stage,
            stage_key=stage_key,
            run_id=run_id,
        )
        offending = [path for path in delta_paths if not is_allowed(path)]
        if not offending:
            return None
        return offending, allowed_scope

    @staticmethod
    def _is_orchestrator_diagnostic_path(path: str) -> bool:
        return (
            path.startswith(".auto-agents/failed-verification-logs/")
            or path == ".auto-agents/docs/requirements_audit.md"
        )

    def _run_gate_commands(self, *, collect_all: bool, context: str):
        self._apply_generated_verification_config()
        before_snapshot = self._worktree_change_snapshot()
        commands = self._default_gate_commands()
        with log_timing(self.logger, f"gate:{context} commands={len(commands)} groups={len(self.config.gates.parallel_groups)}"):
            gate = run_gate_plan(
                commands,
                self.config.gates.parallel_groups,
                self.project_root,
                collect_all=collect_all,
            )
        self._cleanup_ephemeral_tooling_artifacts()
        after_snapshot = self._worktree_change_snapshot()
        changed = self._snapshot_delta_paths(before_snapshot, after_snapshot)
        reason = ""
        if changed:
            reason = (
                f"{context} modified tracked or unignored files: "
                f"{self._changed_path_preview(changed)}"
            )
        return gate, reason

    def _run_gate_commands_for_commands(
        self,
        commands: List[str],
        *,
        collect_all: bool,
        context: str,
    ):
        self._apply_generated_verification_config()
        before_snapshot = self._worktree_change_snapshot()
        with log_timing(self.logger, f"gate:{context} commands={len(commands)}"):
            gate = run_gate_plan(
                commands,
                [],
                self.project_root,
                collect_all=collect_all,
            )
        self._cleanup_ephemeral_tooling_artifacts()
        after_snapshot = self._worktree_change_snapshot()
        changed = self._snapshot_delta_paths(before_snapshot, after_snapshot)
        reason = ""
        if changed:
            reason = (
                f"{context} modified tracked or unignored files: "
                f"{self._changed_path_preview(changed)}"
            )
        return gate, reason

    def _default_gate_commands(self) -> List[str]:
        return (
            commands_from_verification_steps(self.config.gates.steps, self.project_root)
            if self.config.gates.steps
            else self.config.gates.commands
        )

    def _implement_touched_code(self, task: Optional[TaskSpec] = None) -> bool:
        """Return True if the last implement step touched any non-orchestrator file."""
        try:
            paths = changed_paths(
                self.project_root,
                ignored_prefixes=(".auto-agents/",),
            )
        except TypeError:
            paths = [p for p in changed_paths(self.project_root) if not p.startswith(".auto-agents/")]
        if paths:
            return True
        if not self._is_repair_task(task):
            return False
        provider_refs_prefix = (
            self._relative_repo_path(provider_references_dir(self.project_root)).rstrip("/") + "/"
        )
        provider_lock_path = self._relative_repo_path(
            provider_references_lock_path(self.project_root)
        )
        plan_path = self._relative_repo_path(task_plan_path(self.project_root))
        auto_agent_paths = [
            path
            for path in changed_paths(
                self.project_root,
                ignored_prefixes=(".antigravitycli/",),
            )
            if path == plan_path
            or path == provider_lock_path
            or path.startswith(provider_refs_prefix)
        ]
        return bool(auto_agent_paths)

    @staticmethod
    def _extract_oracle_proof_updates(text: str) -> Tuple[List[Dict[str, object]], str]:
        marker = "ORACLE_PROOF_UPDATES"
        if marker not in text:
            return [], ""

        fenced = re.search(
            rf"{marker}\s*:\s*```(?:json)?\s*(.*?)\s*```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            raw_json = fenced.group(1).strip()
        else:
            inline = re.search(
                rf"{marker}\s*:\s*(\[[\s\S]*?\]|\{{[\s\S]*?\}})(?=\s*(?:\n[A-Z][A-Z0-9_ -]*:|\Z))",
                text,
                flags=re.IGNORECASE,
            )
            if not inline:
                return [], "ORACLE_PROOF_UPDATES marker found but no JSON object or array followed it"
            raw_json = inline.group(1).strip()

        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as error:
            return [], f"ORACLE_PROOF_UPDATES is not valid JSON: {error}"
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return [], "ORACLE_PROOF_UPDATES must be a JSON object or array of objects"
        updates: List[Dict[str, object]] = []
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                return [], f"ORACLE_PROOF_UPDATES[{index}] must be an object"
            updates.append(item)
        return updates, ""

    @staticmethod
    def _proof_oracle_index(value: object) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_oracle_proof_type(value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return value
        normalized = raw.lower().replace("-", "_")
        doc_aliases = {"doc", "docs", "documentation", "local_doc", "local_docs"}
        if normalized in doc_aliases:
            return "mixed"
        parts = [
            part.strip()
            for part in re.split(r"[+,/|&\s]+", normalized)
            if part.strip()
        ]
        if len(parts) <= 1:
            return value
        allowed_or_doc = {
            "deterministic_test",
            "integration_test",
            "runtime_evidence",
            "human_review",
            "judge_model",
            "benchmark",
            "mixed",
            *doc_aliases,
        }
        if all(part in allowed_or_doc for part in parts):
            return "mixed"
        return value

    def _apply_oracle_proof_updates_from_text(self, task: TaskSpec, text: str) -> Tuple[bool, str]:
        updates, parse_error = self._extract_oracle_proof_updates(text)
        if parse_error:
            return False, parse_error
        if not updates:
            return False, ""
        if not task.requirement_proofs:
            return False, (
                "ORACLE_PROOF_UPDATES was provided, but the current task has no existing "
                "requirement_proofs entries to update"
            )

        allowed_keys = {
            "requirement_id",
            "oracle_index",
            "acceptance_oracle",
            "proof_type",
            "oracle_strength",
            "evidence_boundary",
            "evidence_refs",
            "forbidden_proxy_oracles",
            "proxy_oracles",
            "status",
        }
        list_keys = {"evidence_refs", "forbidden_proxy_oracles", "proxy_oracles"}
        updated_proofs = [dict(proof) for proof in task.requirement_proofs if isinstance(proof, dict)]
        errors: List[str] = []

        for update_index, update in enumerate(updates, start=1):
            prefix = f"ORACLE_PROOF_UPDATES[{update_index}]"
            local_errors: List[str] = []
            if "proof_type" in update:
                update = dict(update)
                update["proof_type"] = self._normalize_oracle_proof_type(update.get("proof_type"))
            unknown = sorted(set(update) - allowed_keys)
            if unknown:
                errors.append(f"{prefix} contains unsupported field(s): {', '.join(unknown)}")
                continue
            req_id = str(update.get("requirement_id", "")).strip()
            oracle_index = self._proof_oracle_index(update.get("oracle_index"))
            if not req_id:
                errors.append(f"{prefix}.requirement_id must be a non-empty string")
                continue
            if oracle_index is None:
                errors.append(f"{prefix}.oracle_index must be an integer")
                continue
            if str(update.get("status", "")).strip() != "verified":
                errors.append(f"{prefix}.status must be 'verified'")
                continue

            match = next(
                (
                    proof
                    for proof in updated_proofs
                    if str(proof.get("requirement_id", "")).strip() == req_id
                    and self._proof_oracle_index(proof.get("oracle_index")) == oracle_index
                ),
                None,
            )
            if match is None:
                errors.append(
                    f"{prefix} does not match an existing proof on task {task.task_id}: "
                    f"{req_id} oracle #{oracle_index}"
                )
                continue
            for key in list_keys:
                if key not in update:
                    continue
                value = update.get(key)
                if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                    local_errors.append(f"{prefix}.{key} must be a list of non-empty strings")
                elif key == "evidence_refs" and not value:
                    local_errors.append(f"{prefix}.{key} must be a non-empty list of strings")
            for key in ("proof_type", "oracle_strength", "evidence_boundary"):
                if key in update and (not isinstance(update.get(key), str) or not str(update.get(key)).strip()):
                    local_errors.append(f"{prefix}.{key} must be a non-empty string")
            if local_errors:
                errors.extend(local_errors)
                continue
            for key in allowed_keys - {"requirement_id", "oracle_index"}:
                if key in update:
                    match[key] = update[key]

        if errors:
            return False, "Invalid ORACLE_PROOF_UPDATES:\n" + "\n".join(f"- {item}" for item in errors)
        task.requirement_proofs = updated_proofs
        return True, ""

    def _task_completion_proof_findings(self, task: TaskSpec) -> List[dict]:
        plan = load_task_plan(self.project_root)
        strict_plan = False
        if isinstance(plan, dict):
            try:
                strict_plan = int(plan.get("oracle_proof_schema_version") or 0) >= 1
            except (TypeError, ValueError):
                strict_plan = False
        if not strict_plan and not task.requirement_proofs:
            return []
        trace = load_requirements_trace(self.project_root)
        return validate_done_task_requirement_proofs(task, trace)

    @staticmethod
    def _format_requirement_proof_findings(task: TaskSpec, findings: List[dict]) -> str:
        lines = [
            f"task {task.task_id} cannot be marked done because requirement_proofs are not verified.",
            "Emit a valid ORACLE_PROOF_UPDATES JSON block with concrete evidence_refs for each finding.",
        ]
        for item in findings[:12]:
            req_id = str(item.get("requirement_id", "")).strip() or "(unknown requirement)"
            oracle_index = str(item.get("oracle_index", "")).strip()
            oracle_text = f" oracle #{oracle_index}" if oracle_index else ""
            lines.append(f"- {req_id}{oracle_text}: {item.get('message', 'invalid proof')}")
        if len(findings) > 12:
            lines.append(f"- ... {len(findings) - 12} more proof finding(s)")
        return "\n".join(lines)

    @staticmethod
    def _verify_failure_looks_like_oracle_proof_state(text: str) -> bool:
        normalized = str(text or "").lower()
        if not normalized:
            return False
        return any(
            token in normalized
            for token in (
                "requirement_proofs",
                "oracle proof",
                "requirements_audit_state",
                "proof is not verified",
                "has no valid verified proof",
            )
        )

    def _build_split_rejection_reason(
        self,
        task: TaskSpec,
        trigger: str,
        fingerprint: str,
        last_review: str,
        verify_history: List[Dict[str, object]],
        arbiter: Optional[Dict[str, object]] = None,
    ) -> str:
        child_depth = int(task.split_depth) + 1
        verify_summary: List[str] = []
        for entry in verify_history[-4:]:
            if not isinstance(entry, dict):
                continue
            ids = entry.get("failure_ids") or []
            if isinstance(ids, list) and ids:
                verify_summary.append(
                    f"attempt {entry.get('attempt', '?')}: " + ", ".join(str(x) for x in ids[:8])
                )
        lines = [
            f"{self.SPLIT_TASK_MARKER} {task.task_id}",
            "",
            f"Task '{task.task_id}' ({task.title}) has triggered scope-overflow rollback.",
            f"Trigger: {trigger}.",
            f"Blocker fingerprint: {fingerprint or '(empty-diff)'}",
            "",
            "SPLIT MODE INSTRUCTIONS — follow these EXACTLY when updating "
            ".auto-agents/state/task_plan.json:",
            f"  1. Do NOT modify any task with status 'done'.",
            f"  2. Locate the offending task (task_id='{task.task_id}') in the plan.",
            "  3. Replace it IN-PLACE with 2–4 smaller pending sub-tasks, each delivering one",
            "     coherent testable slice (backend change, single API endpoint, single UI",
            "     surface, or test migration) with 2–4 acceptance criteria and a concise",
            "     description. Preserve the surrounding task order.",
            f"  4. Set 'parent_task_id' = '{task.task_id}' on each child task.",
            f"  5. Set 'split_depth' = {child_depth} on each child task.",
            "  6. When the original scope required tests to be updated, populate each child's",
            "     'expected_test_migrations' with the test ids/names it is allowed to change",
            "     (e.g. 'tests.test_foo.test_bar') so regression gating knows those are",
            "     intentional.",
            "  7. Keep all other pending/blocked tasks untouched unless their scope is now",
            "     covered by the split children (in which case remove the duplicate).",
            "  8. Ensure every child still carries requirement_ids that cover the parent's",
            "     requirement_ids.",
            "",
            "Repeating review blockers that forced this rollback:",
            last_review.strip() or "(no review summary captured)",
        ]
        if verify_summary:
            lines.append("")
            lines.append("Recent verification failures:")
            for entry in verify_summary:
                lines.append(f"  - {entry}")
        if arbiter and isinstance(arbiter, dict) and arbiter.get("decision") == "SPLIT":
            rationale = str(arbiter.get("rationale", "")).strip()
            split_axis = arbiter.get("split_axis") or []
            lines.append("")
            lines.append("Scope arbiter verdict: SPLIT")
            if rationale:
                lines.append(f"  Rationale: {rationale}")
            if isinstance(split_axis, list) and split_axis:
                lines.append("  Suggested split axes (use as guidance, not as a rigid prescription):")
                for axis in split_axis[:6]:
                    lines.append(f"    - {axis}")
        return "\n".join(lines)

    def _run_implementation_loop(self, state: RunState, max_tasks: Optional[int]) -> RunState:
        tasks = self._load_implementation_tasks(state)
        state.current_stage = "implement"
        state.tasks = tasks
        save_run_state(self.project_root, state)

        if state.rejected_stage == "implement" and state.rejection_reason:
            import time
            is_full_verify_recovery = "Failure type: full_verification" in state.rejection_reason
            tasks.append(
                TaskSpec(
                    task_id=f"fix-rejection-{int(time.time()*1000)}",
                    title=(
                        "Fix full verification failure"
                        if is_full_verify_recovery
                        else "Fix issues after release rejection"
                    ),
                    description=(
                        "Full verification failed after all planned tasks were implemented."
                        if is_full_verify_recovery
                        else "The release was rejected."
                    )
                    + f"\n\nFeedback:\n{state.rejection_reason}\n\nPlease fix these issues.",
                    acceptance=[
                        "Feedback is fully addressed",
                        "Business code and repository tests are aligned with active requirements",
                        "Tests pass",
                    ]
                )
            )
            state.rejected_stage = ""
            state.rejection_reason = ""

        state.tasks = tasks
        self._commit_planning_baseline_if_needed(tasks)
        self._ensure_implement_verify_baseline(state, tasks)
        if self.config.execution.parallel_tasks.enabled:
            return self._run_parallel_implementation_loop(state, tasks, max_tasks)
        return self._run_sequential_implementation_loop(state, tasks, max_tasks)

    def _load_implementation_tasks(self, state: RunState) -> List[TaskSpec]:
        plan_tasks = self._load_tasks_from_plan()
        if not state.tasks:
            return plan_tasks
        state_tasks = [task.to_dict() for task in state.tasks]
        plan_payload = [task.to_dict() for task in plan_tasks]
        if state_tasks == plan_payload:
            return state.tasks
        state.tasks = plan_tasks
        save_run_state(self.project_root, state)
        return plan_tasks

    def _run_sequential_implementation_loop(
        self,
        state: RunState,
        tasks: List[TaskSpec],
        max_tasks: Optional[int],
    ) -> RunState:
        processed = 0
        for task in tasks:
            if task.status == "done":
                continue
            if not self._has_task_budget(max_tasks, processed):
                self._task_budget_exhausted = True
                break
            rewind_state = self._execute_task_in_main_worktree(state, tasks, task)
            if rewind_state is not None:
                return rewind_state
            processed += 1
            self._consume_task_budget()

        state.tasks = tasks
        state.current_stage = "implement"
        state.stage_summaries["implement"] = f"Completed {sum(task.status == 'done' for task in tasks)} tasks."
        state.last_error = ""
        return state

    def _has_task_budget(self, local_max_tasks: Optional[int], local_processed: int) -> bool:
        if self._max_tasks_remaining is not None:
            return self._max_tasks_remaining > 0
        return local_max_tasks is None or local_processed < local_max_tasks

    def _remaining_task_budget(self, local_max_tasks: Optional[int], local_processed: int, ready_count: int) -> int:
        if self._max_tasks_remaining is not None:
            return min(ready_count, max(0, self._max_tasks_remaining))
        if local_max_tasks is None:
            return ready_count
        return min(ready_count, max(0, local_max_tasks - local_processed))

    def _consume_task_budget(self) -> None:
        if self._max_tasks_remaining is not None:
            self._max_tasks_remaining = max(0, self._max_tasks_remaining - 1)

    def _run_parallel_implementation_loop(
        self,
        state: RunState,
        tasks: List[TaskSpec],
        max_tasks: Optional[int],
    ) -> RunState:
        fallback_reason = self._parallel_execution_fallback_reason(tasks)
        if fallback_reason:
            if self.config.execution.parallel_tasks.strict:
                raise RuntimeError(fallback_reason)
            self.logger.info(f"[parallel-tasks] fallback to sequential: {fallback_reason}")
            return self._run_sequential_implementation_loop(state, tasks, max_tasks)

        processed = 0
        current_workers = self._parallel_worker_count()
        self._log_parallel_worker_resolution(current_workers)
        while True:
            if not self._has_task_budget(max_tasks, processed):
                self._task_budget_exhausted = True
                break
            ready = self._ready_parallel_tasks(tasks)
            if not ready:
                break
            remaining = self._remaining_task_budget(max_tasks, processed, len(ready))
            if remaining <= 0:
                self._task_budget_exhausted = True
                break
            batch_size = min(current_workers, remaining)
            batch = ready[:batch_size]
            if len(batch) < 2:
                self.logger.info(
                    "[parallel-tasks] ready=%s batch=%s; executing sequentially task=%s",
                    len(ready),
                    len(batch),
                    batch[0].task_id,
                )
                rewind_state = self._execute_task_in_main_worktree(state, tasks, batch[0])
                if rewind_state is not None:
                    return rewind_state
                processed += 1
                self._consume_task_budget()
                continue

            self._require_clean_tree_excluding_agent_instructions()
            deferred = self._deferred_parallel_task_reasons(tasks)
            self.logger.info(
                "[parallel-tasks] ready=%s deferred=%s workers=%s batch=%s tasks=%s",
                len(ready),
                len(deferred),
                current_workers,
                len(batch),
                ",".join(task.task_id for task in batch),
            )
            if deferred:
                preview = "; ".join(deferred[:6])
                if len(deferred) > 6:
                    preview += f"; +{len(deferred) - 6} more"
                self.logger.info("[parallel-tasks] deferred reasons=%s", preview)
            with log_timing(self.logger, f"parallel-batch workers={len(batch)}"):
                results = self._run_parallel_task_batch(state, tasks, batch)
            provider_pressure_result: Optional[Tuple[TaskSpec, Dict[str, object]]] = None
            failed_results: List[Tuple[TaskSpec, Dict[str, object]]] = []
            integrated_paths: Set[str] = set()
            for task in batch:
                result = results[task.task_id]
                if not result["ok"]:
                    if self._parallel_result_is_provider_pressure(result):
                        provider_pressure_result = (task, result)
                        break
                    self._apply_parallel_task_failure_snapshot(task, dict(result["task"]))
                    task.status = "blocked"
                    failed_results.append((task, result))
                    continue

                result_changed_paths = {
                    str(path).strip()
                    for path in result.get("changed_paths", [])
                    if str(path).strip()
                }
                overlapping_paths = integrated_paths & result_changed_paths
                if overlapping_paths:
                    preview = ", ".join(sorted(overlapping_paths)[:4])
                    if len(overlapping_paths) > 4:
                        preview += f", +{len(overlapping_paths) - 4} more"
                    self.logger.info(
                        "[parallel-tasks] defer integration task=%s reason=overlapping-worker-paths paths=%s",
                        task.task_id,
                        preview,
                    )
                    continue

                self._apply_parallel_task_snapshot(task, dict(result["task"]))
                commit_sha = self._integrate_parallel_task_result(task, tasks, str(result["commit_sha"]))
                task.commit_sha = commit_sha
                integrated_paths.update(result_changed_paths)
                self._warm_clean_head_verify_baseline(
                    state,
                    failure_ids=result.get("verify_current_failure_ids", []),
                )
                processed += 1
                self._consume_task_budget()
            if provider_pressure_result is not None:
                task, result = provider_pressure_result
                if (
                    self.config.execution.parallel_tasks.workers != "auto"
                    or not self.config.execution.parallel_tasks.adaptive
                ):
                    raise RuntimeError(
                        "parallel task execution hit provider pressure with fixed workers; "
                        f"task={task.task_id} reason={result['reason']}"
                    )
                current_workers = self._record_parallel_pressure(current_workers)
                self.logger.info(
                    "[parallel-tasks] provider pressure task=%s workers=%s reason=%s",
                    task.task_id,
                    current_workers,
                    str(result["reason"])[:200],
                )
            elif failed_results:
                scheduled_recovery = False
                for failed_task, result in failed_results:
                    if self._schedule_repair_tasks_for_failure(state, tasks, failed_task, result):
                        scheduled_recovery = True
                if scheduled_recovery:
                    continue
                self._persist_tasks(tasks)
                for failed_task, result in failed_results:
                    self._emit_task_blocked(failed_task, str(result["reason"]))
                raise RuntimeError(self._format_parallel_batch_failure_error(failed_results))
            else:
                current_workers = self._record_parallel_success(current_workers)
                continue

            if current_workers < 2:
                raise RuntimeError("parallel task execution paused due to provider pressure; retry later")

        state.tasks = tasks
        state.current_stage = "implement"
        state.stage_summaries["implement"] = f"Completed {sum(task.status == 'done' for task in tasks)} tasks."
        state.last_error = ""
        return state

    def _execute_task_in_main_worktree(
        self,
        state: RunState,
        tasks: List[TaskSpec],
        task: TaskSpec,
    ) -> Optional[RunState]:
        if task.status == "blocked":
            payload = self._task_recovery_payload_from_history(task, state)
            if self._schedule_repair_tasks_for_failure(state, tasks, task, payload):
                return state

        resume_existing = task.status == "in_progress" or self._should_resume_task(state, task)
        allow_dirty_retry = task.status == "blocked"
        allow_dirty_repair = self._is_repair_task(task)
        if (resume_existing or allow_dirty_retry) and task.status != "in_progress":
            task.status = "in_progress"
            self._persist_tasks(tasks)

        if (
            not (
                resume_existing
                or allow_dirty_retry
                or allow_dirty_repair
                or self._allow_dirty_tree
            )
            and self.config.gates.require_clean_git_before_task
        ):
            self._require_clean_tree_for_task(task)

        if task.status == "pending":
            task.status = "in_progress"
            self._persist_tasks(tasks)

        if self._ensure_task_verify_baseline(task):
            self._persist_tasks(tasks)

        gate_result = self._execute_task_with_retries(state, task, resume_existing=resume_existing)
        if not gate_result["ok"]:
            rewind_stage = str(gate_result.get("rewind_to_stage", "")).strip()
            if rewind_stage:
                rewind_state = self._handle_review_stage_rewind(
                    state,
                    task,
                    tasks,
                    gate_result,
                    rewind_stage,
                )
                if rewind_state is not None:
                    return rewind_state
            if gate_result.get("rewind_to_plan"):
                rewind_state = self._handle_scope_overflow_rewind(
                    state, task, tasks, gate_result
                )
                if rewind_state is not None:
                    return rewind_state
            if self._schedule_repair_tasks_for_failure(state, tasks, task, gate_result):
                return state
            task.status = "blocked"
            task.review_summary = str(gate_result["review"])
            self._persist_tasks(tasks)
            self._emit_task_blocked(task, str(gate_result["reason"]))
            raise RuntimeError(
                self._format_task_failure_error(
                    task,
                    reason=str(gate_result["reason"]),
                    review_summary=task.review_summary,
                )
            )

        task.status = "done"
        task.review_summary = str(gate_result["review"])
        commit_message = task.commit_message or self.config.git.commit_message_template.format(
            task_id=task.task_id,
            title=task.title,
        )
        self._persist_tasks(tasks)
        save_run_state(self.project_root, state)
        task.commit_sha = commit_all(self.project_root, commit_message)
        self._warm_clean_head_verify_baseline(
            state,
            failure_ids=gate_result.get("verify_current_failure_ids", []),
        )
        return None

    def _parallel_execution_fallback_reason(self, tasks: List[TaskSpec]) -> str:
        if self._parallel_worker_count() < 2:
            return "parallel task execution requires at least 2 workers"
        if any(task.status not in {"pending", "done"} for task in tasks):
            return "parallel task execution only supports fresh pending/done task sets; resume and blocked retries stay sequential"
        raw_plan = load_task_plan(self.project_root)
        raw_tasks = raw_plan.get("tasks", []) if isinstance(raw_plan, dict) else []
        missing_depends_on = []
        for index, item in enumerate(raw_tasks, start=1):
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "pending")) == "done":
                continue
            if "depends_on" not in item:
                missing_depends_on.append(str(item.get("task_id") or f"task #{index}"))
        if missing_depends_on:
            preview = ", ".join(missing_depends_on[:5])
            return (
                "parallel task execution requires planner-generated depends_on on each non-done task; "
                f"missing for {preview}"
            )
        dependency_errors = validate_task_dependencies(
            raw_tasks,
            require_depends_on_for_pending=self.config.execution.parallel_tasks.strict,
        )
        if dependency_errors:
            return dependency_errors[0]
        try:
            self._require_clean_tree_excluding_agent_instructions()
        except RuntimeError as error:
            return str(error)
        return ""

    def _parallel_tuning_key(self) -> str:
        provider = self.config.providers.get(self.config.active_provider, self.config.provider)
        profile = provider.profile_map.get(self.config.efforts.get("implement", "deep"), "")
        return f"{provider.kind}:{provider.subscription_tier}:{profile}"

    def _parallel_worker_count(self) -> int:
        config = self.config.execution.parallel_tasks
        workers = config.workers
        if isinstance(workers, int):
            return max(1, workers)
        provider = self.config.providers.get(self.config.active_provider, self.config.provider)
        limit = provider_limit(provider)
        tuned = self._parallel_tuning.get_workers(self._parallel_tuning_key())
        initial = tuned if tuned is not None else limit.initial_workers
        return max(1, min(initial, limit.worker_ceiling, config.max_auto_workers))

    def _log_parallel_worker_resolution(self, current_workers: int) -> None:
        config = self.config.execution.parallel_tasks
        if config.workers != "auto":
            return
        provider = self.config.providers.get(self.config.active_provider, self.config.provider)
        limit = provider_limit(provider)
        tuned = self._parallel_tuning.get_workers(self._parallel_tuning_key())
        tuned_label = str(tuned) if tuned is not None else "none"
        self.logger.info(
            "[parallel-tasks] auto mode resolved workers=%s tier=%s tuned=%s ceiling=%s max_auto_workers=%s",
            current_workers,
            provider.subscription_tier,
            tuned_label,
            limit.worker_ceiling,
            config.max_auto_workers,
        )

    def _record_parallel_success(self, current_workers: int) -> int:
        config = self.config.execution.parallel_tasks
        if config.workers != "auto" or not config.adaptive:
            return current_workers
        provider = self.config.providers.get(self.config.active_provider, self.config.provider)
        limit = provider_limit(provider)
        next_workers = min(current_workers + 1, limit.worker_ceiling, config.max_auto_workers)
        self._parallel_tuning.put_workers(self._parallel_tuning_key(), next_workers, event="success")
        return next_workers

    def _record_parallel_pressure(self, current_workers: int) -> int:
        config = self.config.execution.parallel_tasks
        if config.workers != "auto" or not config.adaptive:
            return current_workers
        next_workers = max(1, current_workers // 2)
        self._parallel_tuning.put_workers(self._parallel_tuning_key(), next_workers, event="provider_pressure")
        return next_workers

    @staticmethod
    def _parallel_result_is_provider_pressure(result: Dict[str, object]) -> bool:
        failure_ids = result.get("failure_ids", [])
        if isinstance(failure_ids, list) and any(str(item).strip() for item in failure_ids):
            return False
        proof_evidence = result.get("proof_evidence")
        if isinstance(proof_evidence, dict) and proof_evidence and not bool(proof_evidence.get("ok", True)):
            return False
        reason = str(result.get("reason", "")).strip()
        if not reason:
            return False
        return _PARALLEL_PROVIDER_PRESSURE_PATTERN.search(reason) is not None

    def _ready_parallel_tasks(self, tasks: List[TaskSpec]) -> List[TaskSpec]:
        completed = {task.task_id for task in tasks if task.status == "done"}
        return [
            task
            for task in tasks
            if task.status == "pending"
            and all(dependency in completed for dependency in task.depends_on)
        ]

    def _deferred_parallel_task_reasons(self, tasks: List[TaskSpec]) -> List[str]:
        completed = {task.task_id for task in tasks if task.status == "done"}
        reasons: List[str] = []
        for task in tasks:
            if task.status != "pending":
                continue
            missing = [dependency for dependency in task.depends_on if dependency not in completed]
            if missing:
                reasons.append(f"{task.task_id}: waiting for {', '.join(missing[:4])}")
        return reasons

    def _parallel_worktree_root(self) -> Path:
        configured = self.config.execution.parallel_tasks.worktree_root.strip()
        if not configured:
            return (self.project_root.parent / f".{self.project_root.name}-auto-agents-worktrees").resolve()
        root = Path(configured)
        if not root.is_absolute():
            root = (self.project_root.parent / root).resolve()
        return root

    def _run_parallel_task_batch(
        self,
        state: RunState,
        tasks: List[TaskSpec],
        batch: List[TaskSpec],
    ) -> Dict[str, Dict[str, object]]:
        batch_tasks = [TaskSpec.from_dict(task.to_dict()) for task in tasks]
        state_snapshot = RunState.from_dict(state.to_dict())
        state_snapshot.tasks = batch_tasks
        results: Dict[str, Dict[str, object]] = {}
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            future_map = {
                executor.submit(
                    self._run_task_in_worktree,
                    state_snapshot,
                    [TaskSpec.from_dict(item.to_dict()) for item in batch_tasks],
                    task.task_id,
                ): task.task_id
                for task in batch
            }
            for future in as_completed(future_map):
                task_id = future_map[future]
                results[task_id] = future.result()
        return results

    def _run_task_in_worktree(
        self,
        state: RunState,
        tasks: List[TaskSpec],
        task_id: str,
    ) -> Dict[str, object]:
        base_ref = head_ref(self.project_root) or "HEAD"
        worktree_path = self._parallel_worktree_root() / state.run_id / task_id
        worktree_created = False
        try:
            add_worktree(self.project_root, worktree_path, ref=base_ref)
            worktree_created = True
            worker = self.__class__(
                worktree_path,
                agent_output_stream=self.agent_output_stream,
                user_input_fn=self._user_input_fn,
            )
            worker._print_agent_output = self._print_agent_output
            worker._allow_dirty_tree = self._allow_dirty_tree
            worker_state = RunState.from_dict(state.to_dict())
            worker_tasks = [TaskSpec.from_dict(item.to_dict()) for item in tasks]
            worker_state.tasks = worker_tasks
            save_run_state(worktree_path, worker_state)
            worker._persist_tasks(worker_tasks)
            worker_task = next(task for task in worker_tasks if task.task_id == task_id)
            if worker._ensure_task_verify_baseline(worker_task):
                worker._persist_tasks(worker_tasks)
            gate_result = worker._execute_task_with_retries(worker_state, worker_task, resume_existing=False)
            if not gate_result["ok"]:
                worker_task.status = "blocked"
                worker_task.review_summary = str(gate_result["review"])
                return {
                    "ok": False,
                    "task": worker_task.to_dict(),
                    "reason": str(gate_result["reason"]),
                    "review": str(gate_result["review"]),
                    "failure_ids": list(gate_result.get("failure_ids", [])),
                    "comparable_failures": bool(gate_result.get("comparable_failures", True)),
                    "proof_evidence": (
                        gate_result.get("proof_evidence")
                        if isinstance(gate_result.get("proof_evidence"), dict)
                        else {}
                    ),
                }

            worker_task.status = "done"
            worker_task.review_summary = str(gate_result["review"])
            worker._persist_tasks(worker_tasks)
            commit_message = worker_task.commit_message or worker.config.git.commit_message_template.format(
                task_id=worker_task.task_id,
                title=worker_task.title,
            )
            worker_commit_sha = commit_all_except(
                worktree_path,
                commit_message,
                exclude_prefixes=(".auto-agents", ".antigravitycli"),
            )
            worker_changed_paths = commit_changed_paths(worktree_path, worker_commit_sha)
            return {
                "ok": True,
                "task": worker_task.to_dict(),
                "reason": "",
                "review": str(gate_result["review"]),
                "commit_sha": worker_commit_sha,
                "changed_paths": worker_changed_paths,
                "verify_current_failure_ids": list(gate_result.get("verify_current_failure_ids", [])),
            }
        except Exception as error:
            task = next((item for item in tasks if item.task_id == task_id), None)
            task_payload = task.to_dict() if task is not None else {"task_id": task_id}
            return {
                "ok": False,
                "task": task_payload,
                "reason": f"parallel worktree execution failed: {error}",
                "review": "",
            }
        finally:
            if worktree_created:
                remove_worktree(self.project_root, worktree_path, force=True)

    def _copy_parallel_task_snapshot_fields(self, task: TaskSpec, payload: Dict[str, object]) -> None:
        updated = TaskSpec.from_dict(payload)
        task.description = updated.description
        task.acceptance = list(updated.acceptance)
        task.requirement_ids = list(updated.requirement_ids)
        task.depends_on = list(updated.depends_on)
        task.review_summary = updated.review_summary
        task.scope_boundaries = updated.scope_boundaries
        task.review_history = list(updated.review_history)
        task.verify_history = list(updated.verify_history)
        task.verify_baseline_failures = list(updated.verify_baseline_failures)
        task.verify_baseline_ref = updated.verify_baseline_ref
        task.expected_test_migrations = list(updated.expected_test_migrations)
        task.requirement_proofs = list(updated.requirement_proofs)
        task.verification_refs = list(updated.verification_refs)
        task.scratchpad = updated.scratchpad
        task.arbitration_history = list(updated.arbitration_history)
        task.recovery_history = list(updated.recovery_history)

    def _apply_parallel_task_snapshot(self, task: TaskSpec, payload: Dict[str, object]) -> None:
        self._copy_parallel_task_snapshot_fields(task, payload)
        task.status = "done"

    def _apply_parallel_task_failure_snapshot(self, task: TaskSpec, payload: Dict[str, object]) -> None:
        self._copy_parallel_task_snapshot_fields(task, payload)
        updated = TaskSpec.from_dict(payload)
        task.status = updated.status if updated.status in {"pending", "in_progress", "blocked"} else "blocked"

    def _format_parallel_batch_failure_error(self, failures: List[Tuple[TaskSpec, Dict[str, object]]]) -> str:
        if not failures:
            return "parallel task batch failed"
        parts = []
        for task, result in failures:
            reason = str(result.get("reason", "")).strip() or "unknown failure"
            parts.append(f"{task.task_id}: {reason}")
        return "parallel task batch failed: " + " | ".join(parts)

    def _task_recovery_payload_from_history(self, task: TaskSpec, state: RunState) -> Dict[str, object]:
        failure_ids: List[str] = []
        comparable = True
        for entry in reversed(task.verify_history):
            if not isinstance(entry, dict) or str(entry.get("decision", "")) != "fail":
                continue
            raw_ids = entry.get("failure_ids", [])
            if isinstance(raw_ids, list):
                failure_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
            comparable = bool(entry.get("comparable_failures", True))
            break
        reason = task.review_summary.strip() or state.last_error.strip()
        return {
            "reason": reason,
            "review": task.review_summary,
            "failure_ids": failure_ids,
            "comparable_failures": comparable,
        }

    def _recovery_signature(self, failure_ids: List[str], reason: str = "") -> str:
        # NOTE: the recovery round counter groups failures by this signature. It must be
        # stable across rounds for the same set of failing verification refs, otherwise
        # recovery_config.max_rounds is never reached and repair tasks spawn unbounded.
        # The free-form review `reason` varies every round, so it is intentionally excluded.
        payload = {
            "failure_ids": sorted(failure_ids),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def _candidate_repair_refs(self, task: TaskSpec, result: Dict[str, object]) -> List[str]:
        comparable = bool(result.get("comparable_failures", True))
        raw_ids = result.get("failure_ids", [])
        failure_ids = [str(item).strip() for item in raw_ids if str(item).strip()] if isinstance(raw_ids, list) else []
        if not comparable:
            return []
        refs: List[str] = []
        for failure_id in failure_ids:
            if failure_id.startswith("reason:") or failure_id.startswith("cmd:"):
                continue
            if self._build_task_proof_evidence_command_for_ref(failure_id):
                refs.append(failure_id)
        if refs:
            return refs
        proof_evidence = result.get("proof_evidence")
        if isinstance(proof_evidence, dict):
            for raw_ref in proof_evidence.get("failed_refs", []) or []:
                ref = str(raw_ref).strip()
                if ref and self._build_task_proof_evidence_command_for_ref(ref):
                    refs.append(ref)
        return refs

    @staticmethod
    def _group_repair_refs(refs: List[str], max_refs_per_group: int, max_groups: int) -> List[List[str]]:
        grouped: Dict[str, List[str]] = {}
        ordered_paths: List[str] = []
        for ref in refs:
            path, _selector = Orchestrator._split_evidence_ref(ref)
            key = path.replace("\\", "/").strip() or ref
            if key not in grouped:
                grouped[key] = []
                ordered_paths.append(key)
            if ref not in grouped[key]:
                grouped[key].append(ref)
        groups: List[List[str]] = []
        for path in ordered_paths:
            items = grouped[path]
            for index in range(0, len(items), max_refs_per_group):
                groups.append(items[index : index + max_refs_per_group])
                if len(groups) >= max_groups:
                    return groups
        return groups

    @staticmethod
    def _repair_task_id(parent_task_id: str, round_number: int, index: int) -> str:
        safe_parent = re.sub(r"[^a-zA-Z0-9_-]+", "-", parent_task_id).strip("-").lower() or "task"
        return f"repair-{safe_parent}-r{round_number}-{index}"

    def _schedule_repair_tasks_for_failure(
        self,
        state: RunState,
        tasks: List[TaskSpec],
        task: TaskSpec,
        result: Dict[str, object],
    ) -> bool:
        recovery_config = self.config.execution.recovery
        if not recovery_config.enabled:
            return False
        if task.parent_task_id or task.task_id.startswith("repair-"):
            return False
        existing_open_repairs = [
            item for item in tasks
            if item.parent_task_id == task.task_id and item.status != "done"
        ]
        if existing_open_repairs:
            self.logger.info(
                "[recovery] parent=%s waits for existing repair tasks=%s",
                task.task_id,
                ",".join(item.task_id for item in existing_open_repairs),
            )
            task.status = "pending"
            for repair in existing_open_repairs:
                if repair.task_id not in task.depends_on:
                    task.depends_on.append(repair.task_id)
            self._persist_tasks(tasks)
            state.tasks = tasks
            save_run_state(self.project_root, state)
            return True

        refs = self._candidate_repair_refs(task, result)
        if not refs:
            return False
        reason = str(result.get("reason", "")).strip()
        signature = self._recovery_signature(refs, reason)
        prior_rounds = [
            entry for entry in task.recovery_history
            if isinstance(entry, dict) and str(entry.get("signature", "")) == signature
        ]
        round_number = len(prior_rounds) + 1
        if round_number > recovery_config.max_rounds:
            self.logger.info(
                "[recovery] exhausted parent=%s signature=%s rounds=%s reason=%s",
                task.task_id,
                signature,
                len(prior_rounds),
                reason[:300],
            )
            task.recovery_history.append({
                "signature": signature,
                "round": round_number,
                "result": "exhausted",
                "reason": reason,
                "failure_ids": refs,
            })
            return False

        groups = self._group_repair_refs(
            refs,
            max_refs_per_group=recovery_config.max_refs_per_repair_task,
            max_groups=recovery_config.max_repair_tasks_per_round,
        )
        if not groups:
            return False
        existing_ids = {item.task_id for item in tasks}
        repair_tasks: List[TaskSpec] = []
        for index, group in enumerate(groups, start=1):
            repair_id = self._repair_task_id(task.task_id, round_number, index)
            suffix = 2
            while repair_id in existing_ids:
                repair_id = f"{self._repair_task_id(task.task_id, round_number, index)}-{suffix}"
                suffix += 1
            existing_ids.add(repair_id)
            path, _selector = self._split_evidence_ref(group[0])
            title = f"Repair {task.task_id} proof evidence"
            if path:
                title = f"{title}: {path.rsplit('/', 1)[-1]}"
            repair_tasks.append(
                TaskSpec(
                    task_id=repair_id,
                    title=title,
                    description=(
                        f"Repair failed verification evidence for parent task {task.task_id}.\n\n"
                        f"Failure reason:\n{reason}\n\n"
                        "Verification refs:\n" + "\n".join(f"- {ref}" for ref in group)
                    ),
                    acceptance=[
                        "All verification_refs on this repair task pass.",
                        f"The fix remains scoped to failed evidence for parent task {task.task_id}.",
                        "No parent requirement_proofs, acceptance criteria, or forbidden proxy oracle constraints are weakened.",
                    ],
                    requirement_ids=[],
                    depends_on=[],
                    status="pending",
                    commit_message=f"fix({task.task_id}): repair proof evidence",
                    parent_task_id=task.task_id,
                    split_depth=int(task.split_depth) + 1,
                    verification_refs=list(group),
                )
            )

        insert_at = tasks.index(task)
        tasks[insert_at:insert_at] = repair_tasks
        task.status = "pending"
        task.commit_sha = ""
        for repair in repair_tasks:
            if repair.task_id not in task.depends_on:
                task.depends_on.append(repair.task_id)
        task.recovery_history.append({
            "signature": signature,
            "round": round_number,
            "result": "scheduled",
            "reason": reason,
            "failure_ids": refs,
            "repair_task_ids": [repair.task_id for repair in repair_tasks],
        })
        self._persist_tasks(tasks)
        state.tasks = tasks
        state.current_stage = "implement"
        state.last_error = ""
        save_run_state(self.project_root, state)
        self.logger.info(
            "[recovery] scheduled parent=%s round=%s repairs=%s refs=%s",
            task.task_id,
            round_number,
            ",".join(repair.task_id for repair in repair_tasks),
            len(refs),
        )
        return True

    def _integrate_parallel_task_result(
        self,
        task: TaskSpec,
        tasks: List[TaskSpec],
        worker_commit_sha: str,
    ) -> str:
        pre_integration_ref = head_ref(self.project_root) or "HEAD"
        try:
            cherry_pick_no_commit(self.project_root, worker_commit_sha)
        except RuntimeError as error:
            cleanup_notes: List[str] = []
            abort_error = abort_cherry_pick(self.project_root)
            if abort_error:
                cleanup_notes.append(f"cherry-pick abort failed: {abort_error}")
            if not hard_reset_clean(self.project_root, pre_integration_ref):
                cleanup_notes.append(f"failed to restore pre-integration ref {pre_integration_ref}")
            task.status = "blocked"
            self._persist_tasks(tasks)
            reason = str(error)
            if cleanup_notes:
                reason = f"{reason}; cleanup notes: {'; '.join(cleanup_notes)}"
            self._emit_task_blocked(task, f"merge failed: {reason}")
            raise RuntimeError(
                self._format_task_failure_error(
                    task,
                    reason=f"parallel integration failed while merging worker commit: {reason}",
                    review_summary=task.review_summary,
                )
            )
        self._persist_tasks(tasks)
        commit_message = task.commit_message or self.config.git.commit_message_template.format(
            task_id=task.task_id,
            title=task.title,
        )
        return commit_all(self.project_root, commit_message)

    def _handle_review_stage_rewind(
        self,
        state: RunState,
        task: TaskSpec,
        tasks: List[TaskSpec],
        gate_result: Dict[str, object],
        target_stage: str,
    ) -> Optional[RunState]:
        if target_stage not in STAGE_ORDER or STAGE_ORDER.index(target_stage) >= STAGE_ORDER.index("implement"):
            return None

        baseline_ref = (
            task.verify_baseline_ref
            or state.implement_verify_baseline_ref
            or state.stage_summaries.get("implement_baseline_ref", "")
        )
        rewind_ref = self._git_ref_from_verify_baseline_ref(baseline_ref) or "HEAD"
        if not hard_reset_clean(self.project_root, rewind_ref):
            raise RuntimeError(
                "review-stage rewind failed to restore the baseline before "
                f"returning task {task.task_id} to {target_stage}. Resolved git ref: {rewind_ref}."
            )

        task.status = "pending"
        task.review_summary = str(gate_result.get("review", ""))
        task.commit_sha = ""
        self._persist_tasks(tasks)

        state.tasks = tasks
        reason_lines = [
            str(gate_result.get("rewind_reason", "")).strip()
            or f"review feedback points to a {target_stage}-owned artifact",
            "",
            "Review feedback:",
            str(gate_result.get("review", "")).strip(),
        ]
        self._rewind_state_from_stage(state, target_stage)
        state.rejected_stage = target_stage
        state.rejection_reason = "\n".join(line for line in reason_lines if line is not None).strip()
        state.last_error = f"review rejected task {task.task_id}; rewinding to {target_stage}"
        save_run_state(self.project_root, state)
        self._emit_task_blocked(
            task,
            f"review rejected the task; rewinding to {target_stage}",
        )
        return state

    def _handle_scope_overflow_rewind(
        self,
        state: RunState,
        task: TaskSpec,
        tasks: List[TaskSpec],
        gate_result: Dict[str, object],
    ) -> Optional[RunState]:
        """Route a scope-overflow task back to the plan stage for splitting.

        Returns a state to bubble up (plan rewind) or None when rewind is
        refused (e.g. split-depth cap reached) and the caller should fall
        through to the normal blocked-task path.
        """
        if int(task.split_depth) >= self.MAX_SPLIT_DEPTH:
            return None

        baseline_ref = (
            task.verify_baseline_ref
            or state.implement_verify_baseline_ref
            or state.stage_summaries.get("implement_baseline_ref", "")
        )
        rewind_ref = self._git_ref_from_verify_baseline_ref(baseline_ref) or "HEAD"
        if not hard_reset_clean(self.project_root, rewind_ref):
            raise RuntimeError(
                "scope-overflow rewind failed to restore the baseline before "
                f"splitting task {task.task_id}. Resolved git ref: {rewind_ref}."
            )

        task.status = "pending"
        task.review_summary = str(gate_result.get("review", ""))
        task.commit_sha = ""
        self._persist_tasks(tasks)

        state.tasks = tasks
        reason = self._build_split_rejection_reason(
            task,
            trigger=str(gate_result.get("split_trigger", "")),
            fingerprint=str(gate_result.get("split_fingerprint", "")),
            last_review=str(gate_result.get("review", "")),
            verify_history=list(task.verify_history),
            arbiter=gate_result.get("arbiter") if isinstance(gate_result.get("arbiter"), dict) else None,
        )
        self._rewind_state_from_stage(state, "plan")
        state.rejected_stage = "plan"
        state.rejection_reason = reason
        state.last_error = f"scope_overflow: {gate_result.get('split_trigger', '')}"[:500]
        save_run_state(self.project_root, state)
        self._emit_task_blocked(
            task,
            f"scope_overflow → rewinding to plan for split (depth {task.split_depth} → "
            f"{task.split_depth + 1})",
        )
        return state

    def _require_clean_tree_for_task(self, task: TaskSpec) -> None:
        try:
            self._require_clean_tree_excluding_agent_instructions()
        except RuntimeError as error:
            if str(error) != "working tree is not clean":
                raise

            changed = self._changed_paths_excluding_agent_instructions()
            preview = ", ".join(changed[:5])
            if len(changed) > 5:
                preview += f", +{len(changed) - 5} more"
            if not preview:
                preview = "(unable to determine changed paths)"

            raise RuntimeError(
                "working tree is not clean before "
                f"task {task.task_id}. Changed paths: {preview}. "
                "Commit or stash those changes first, disable "
                "gates.require_clean_git_before_task, or rerun with --allow-dirty-tree."
            ) from error

    @staticmethod
    def _is_repair_task(task: TaskSpec) -> bool:
        return task.task_id.strip().startswith("repair-")

    def _require_clean_tree_excluding_agent_instructions(self) -> None:
        changed = self._changed_paths_excluding_agent_instructions()
        if changed:
            raise RuntimeError("working tree is not clean")

    def _changed_paths_excluding_agent_instructions(self) -> List[str]:
        ignored = list(GENERATED_AGENT_INSTRUCTION_PATHS) + list(LEGACY_GENERATED_AGENT_INSTRUCTION_PATHS)
        if not head_ref(self.project_root):
            ignored.extend(["README.md", ".gitignore"])
        return changed_paths(self.project_root, ignored_prefixes=(".auto-agents/", *ignored))

    def _run_task_verify(
        self,
        task: Optional[TaskSpec] = None,
        *,
        state: Optional[RunState] = None,
    ) -> Dict[str, object]:
        task_commands = self._build_task_verify_commands(task)
        quick_failure = self._quick_verify_failure(task_commands if task_commands else None)
        if quick_failure:
            return {
                "ok": False,
                "reason": quick_failure,
                "failure_ids": self._normalize_verify_failure_ids([], quick_failure),
                "current_failure_ids": self._normalize_verify_failure_ids([], quick_failure),
                "baseline_failure_ids": list(task.verify_baseline_failures) if task is not None else [],
                "new_failure_ids": self._normalize_verify_failure_ids([], quick_failure),
                "raw_output": quick_failure,
                "comparable_failures": False,
            }
        task_scope_label = self._task_verify_command_scope_label(task)
        if task_commands:
            verify_gate, mutation_error = self._run_gate_commands_for_commands(
                task_commands,
                collect_all=True,
                context=(
                    f"task verification commands ({task.task_id})"
                    if task is not None
                    else "task verification commands"
                ),
            )
        else:
            verify_gate, mutation_error = self._run_gate_commands(
                collect_all=task is not None,
                context="task verification commands" if task is not None else "verification commands",
            )
        if mutation_error:
            failure_ids = self._normalize_verify_failure_ids([], mutation_error)
            return {
                "ok": False,
                "reason": mutation_error,
                "failure_ids": failure_ids,
                "current_failure_ids": failure_ids,
                "baseline_failure_ids": list(task.verify_baseline_failures) if task is not None else [],
                "new_failure_ids": failure_ids,
                "raw_output": mutation_error,
                "comparable_failures": False,
            }
        extraction = extract_failure_info(verify_gate)
        diagnostic_identity_only = False
        raw_output = self._gate_raw_output(verify_gate)
        if not verify_gate.ok and not extraction.comparable:
            diagnostic_gate = self._run_verify_failure_identity_diagnostic(verify_gate)
            if diagnostic_gate is not None:
                diagnostic_extraction = extract_failure_info(diagnostic_gate)
                diagnostic_raw_output = self._gate_raw_output(diagnostic_gate)
                if diagnostic_raw_output.strip():
                    raw_output = (
                        f"{raw_output.rstrip()}\n\n=== Failure Identity Diagnostic ===\n"
                        f"{diagnostic_raw_output.strip()}\n"
                    ).strip()
                if diagnostic_extraction.comparable and diagnostic_extraction.failure_ids:
                    extraction = diagnostic_extraction
                    diagnostic_identity_only = True
        current_failure_ids = (
            self._normalize_verify_failure_ids(extraction.failure_ids, verify_gate.summary)
            if extraction.failure_ids
            else []
        )
        baseline_failure_ids = (
            self._normalize_verify_failure_ids(task.verify_baseline_failures, verify_gate.summary)
            if task is not None and task.verify_baseline_failures
            else []
        )
        new_failure_ids = (
            sorted(set(current_failure_ids) - set(baseline_failure_ids))
            if extraction.comparable
            else list(current_failure_ids)
        )
        if task is not None and task.expected_test_migrations:
            allowed_migrations = {str(item) for item in task.expected_test_migrations}
            new_failure_ids = [fid for fid in new_failure_ids if fid not in allowed_migrations]
        raw_log_path = ""
        if not verify_gate.ok:
            raw_log_path = self._persist_failed_verification_log(raw_output, label="task-verify")
        baseline_only_reason = ""
        if (
            task is not None
            and extraction.comparable
            and not diagnostic_identity_only
            and current_failure_ids
            and not new_failure_ids
        ):
            baseline_only_reason = (
                f"task baseline only: {len(current_failure_ids)} pre-existing failure(s) remain"
            )
        if not verify_gate.ok and not baseline_only_reason:
            effective_failure_ids = new_failure_ids or current_failure_ids
            is_repair_task = self._is_repair_task(task)
            retryable_missing_owned_evidence = (
                is_repair_task
                and self._all_failures_are_missing_owned_pytest_evidence_refs(
                    task,
                    effective_failure_ids,
                )
            )
            retryable_owned_evidence = (
                is_repair_task
                and (
                    retryable_missing_owned_evidence
                    or self._all_failures_are_owned_pytest_evidence_refs(
                        task,
                        effective_failure_ids,
                    )
                )
            )
            if diagnostic_identity_only:
                reason = (
                    "verification failed before a stable full failure summary was emitted; "
                    f"identity diagnostic captured: {', '.join(current_failure_ids[:10])}"
                )
                if raw_log_path:
                    reason = f"{reason}; raw log: {raw_log_path}"
            elif not extraction.comparable:
                reason = (
                    "non-comparable verification failure: failed command did not yield stable "
                    "test-case failure ids"
                )
                if raw_log_path:
                    reason = f"{reason}; raw log: {raw_log_path}"
            elif task is not None and new_failure_ids:
                reason = (
                    f"{len(new_failure_ids)} new verification failure(s) vs task baseline: "
                    + ", ".join(new_failure_ids[:10])
                )
            else:
                reason = verify_gate.summary
            out_of_scope_reason = self._task_verify_contract_scope_reason(
                task,
                new_failure_ids or effective_failure_ids,
                task_scope_label=task_scope_label,
            )
            if out_of_scope_reason:
                return {
                    "ok": False,
                    "reason": out_of_scope_reason,
                    "failure_ids": effective_failure_ids,
                    "current_failure_ids": current_failure_ids,
                    "baseline_failure_ids": baseline_failure_ids,
                    "new_failure_ids": new_failure_ids or effective_failure_ids,
                    "raw_output": raw_output,
                    "raw_log_path": raw_log_path,
                    "comparable_failures": extraction.comparable,
                    "retryable_missing_owned_evidence_refs": retryable_missing_owned_evidence,
                    "retryable_owned_evidence_failure_refs": retryable_owned_evidence,
                    "contract_scope_issue": True,
                }
            return {
                "ok": False,
                "reason": reason,
                "failure_ids": effective_failure_ids,
                "current_failure_ids": current_failure_ids,
                "baseline_failure_ids": baseline_failure_ids,
                "new_failure_ids": new_failure_ids or effective_failure_ids,
                "raw_output": raw_output,
                "raw_log_path": raw_log_path,
                "comparable_failures": extraction.comparable,
                "retryable_missing_owned_evidence_refs": retryable_missing_owned_evidence,
                "retryable_owned_evidence_failure_refs": retryable_owned_evidence,
            }
        stale_plan_audit = self._run_stale_plan_coupled_test_audit(task, state=state)
        if stale_plan_audit:
            stale_failure_ids = self._normalize_verify_failure_ids(
                stale_plan_audit.get("failure_ids", []),
                str(stale_plan_audit.get("reason", "")),
            )
            return {
                "ok": False,
                "reason": str(stale_plan_audit["reason"]),
                "failure_ids": stale_failure_ids,
                "current_failure_ids": stale_failure_ids,
                "baseline_failure_ids": baseline_failure_ids,
                "new_failure_ids": stale_failure_ids,
                "raw_output": str(stale_plan_audit.get("raw_output", "")),
            }
        stale_status_audit = self._run_task_status_coupled_test_audit(
            task,
            expected_status="done",
        )
        if stale_status_audit:
            stale_failure_ids = self._normalize_verify_failure_ids(
                stale_status_audit.get("failure_ids", []),
                str(stale_status_audit.get("reason", "")),
            )
            return {
                "ok": False,
                "reason": str(stale_status_audit["reason"]),
                "failure_ids": stale_failure_ids,
                "current_failure_ids": stale_failure_ids,
                "baseline_failure_ids": baseline_failure_ids,
                "new_failure_ids": stale_failure_ids,
                "raw_output": str(stale_status_audit.get("raw_output", "")),
            }
        requirements_audit_check = self._run_task_requirements_audit_recovery_check(task, state)
        if requirements_audit_check:
            audit_failure_ids = self._normalize_verify_failure_ids(
                requirements_audit_check.get("failure_ids", []),
                str(requirements_audit_check.get("reason", "")),
            )
            return {
                "ok": False,
                "reason": str(requirements_audit_check["reason"]),
                "failure_ids": audit_failure_ids,
                "current_failure_ids": audit_failure_ids,
                "baseline_failure_ids": baseline_failure_ids,
                "new_failure_ids": audit_failure_ids,
                "raw_output": str(requirements_audit_check.get("raw_output", "")),
            }
        proof_evidence = self._run_task_proof_evidence(task)
        if proof_evidence is not None and not bool(proof_evidence.get("ok")):
            failure_ids = self._normalize_verify_failure_ids(
                proof_evidence.get("failure_ids", []),
                str(proof_evidence.get("reason", "")),
            )
            return {
                "ok": False,
                "reason": str(proof_evidence.get("reason", "")),
                "failure_ids": failure_ids,
                "current_failure_ids": failure_ids,
                "baseline_failure_ids": baseline_failure_ids,
                "new_failure_ids": failure_ids,
                "raw_output": str(proof_evidence.get("raw_output", "")),
                "proof_evidence": proof_evidence,
            }
        return {
            "ok": True,
            "reason": baseline_only_reason or verify_gate.summary,
            "failure_ids": [],
            "current_failure_ids": current_failure_ids,
            "baseline_failure_ids": baseline_failure_ids,
            "new_failure_ids": [],
            "raw_output": raw_output,
            "raw_log_path": raw_log_path,
            "comparable_failures": extraction.comparable,
            "proof_evidence": proof_evidence,
        }

    _REQUIREMENTS_AUDIT_EVIDENCE_MARKERS = (
        "test_requirements_audit_state",
        ".auto-agents/docs/requirements_audit",
    )

    @staticmethod
    def _task_depends_on_requirements_audit(task: Optional[TaskSpec]) -> bool:
        if task is None:
            return False
        for ref in Orchestrator._task_planned_evidence_refs(task):
            normalized = str(ref).replace("\\", "/").lower()
            if any(
                marker in normalized
                for marker in Orchestrator._REQUIREMENTS_AUDIT_EVIDENCE_MARKERS
            ):
                return True
        return False

    def _requirements_audit_gate(
        self,
        task: Optional[TaskSpec],
        state: Optional[RunState],
    ) -> Optional[Tuple[List[str], set]]:
        """Return (requirement_ids, assume_done_task_ids) for a planner-generated
        requirements-audit gap task (or a repair of one), or None when the task's
        verification does not depend on the requirements audit.

        The audit-gap task's proof asserts on the generated requirements_audit.md, but
        requirement proofs only count once the owning task is done. Treating the owner (and,
        for a repair, its parent) as done lets the gate recompute the true audit state, which
        both breaks the completion deadlock and makes the gate impossible to satisfy by
        weakening the asserting test.
        """
        if task is None or not self._task_depends_on_requirements_audit(task):
            return None
        tasks = state.tasks if state is not None and state.tasks else self._load_tasks_from_plan()
        tasks_by_id = {t.task_id: t for t in tasks}
        owner = task
        assumed = {task.task_id}
        if not task.requirement_ids and task.parent_task_id:
            parent = tasks_by_id.get(task.parent_task_id)
            if parent is not None and parent.requirement_ids:
                owner = parent
                assumed.add(parent.task_id)
        if not owner.requirement_ids:
            return None
        return list(owner.requirement_ids), assumed

    def _run_task_requirements_audit_recovery_check(
        self,
        task: Optional[TaskSpec],
        state: Optional[RunState],
    ) -> Optional[Dict[str, object]]:
        tasks = state.tasks if state is not None and state.tasks else self._load_tasks_from_plan()
        # Legacy release-rejection recovery task: the full requirement ledger must pass.
        if self._is_requirements_audit_recovery_task(task):
            audit_result = run_requirements_audit(
                self.project_root, tasks, current_spec=self._active_spec_file
            )
            if bool(audit_result.get("ok")):
                return None
            failed_requirements = [
                str(issue.get("requirement_id", "")).strip()
                for issue in audit_result.get("issues", [])
                if isinstance(issue, dict) and str(issue.get("result", "")).strip() == "fail"
            ]
            failed_requirements = [item for item in failed_requirements if item]
            return {
                "reason": f"requirements audit still failed: {audit_result['path']}",
                "failure_ids": failed_requirements,
                "raw_output": str(audit_result.get("report", "")),
            }
        # Planner-generated audit-gap task (or its repair): deterministic, un-gameable gate
        # scoped to the task's bound requirements.
        gate = self._requirements_audit_gate(task, state)
        if gate is None:
            return None
        gate_requirement_ids, assume_done = gate
        audit_result = run_requirements_audit(
            self.project_root,
            tasks,
            current_spec=self._active_spec_file,
            assume_done_task_ids=assume_done,
        )
        failed_requirements = {
            str(issue.get("requirement_id", "")).strip()
            for issue in audit_result.get("issues", [])
            if isinstance(issue, dict) and str(issue.get("result", "")).strip() == "fail"
        }
        gate_failures = [rid for rid in gate_requirement_ids if rid in failed_requirements]
        if not gate_failures:
            return None
        return {
            "reason": (
                "requirements audit still fails for this task's bound requirement(s) "
                f"{', '.join(gate_failures)} even with the task treated as done. Fix the real "
                "proof evidence and source-of-truth so the audit passes; do not weaken the "
                f"asserting test. See {audit_result['path']}."
            ),
            "failure_ids": gate_failures,
            "raw_output": str(audit_result.get("report", "")),
        }

    @staticmethod
    def _task_requirement_evidence_refs(task: Optional[TaskSpec]) -> List[str]:
        if task is None:
            return []
        refs: List[str] = []
        for proof in task.requirement_proofs:
            if not isinstance(proof, dict):
                continue
            if str(proof.get("status", "")).strip() != "verified":
                continue
            for raw_ref in proof.get("evidence_refs", []) or []:
                ref = str(raw_ref).strip()
                if ref and ref not in refs:
                    refs.append(ref)
        return refs

    @staticmethod
    def _task_planned_evidence_refs(task: Optional[TaskSpec]) -> List[str]:
        if task is None:
            return []
        refs: List[str] = []
        for raw_ref in task.verification_refs:
            ref = str(raw_ref).strip()
            if ref and ref not in refs:
                refs.append(ref)
        for proof in task.requirement_proofs:
            if not isinstance(proof, dict):
                continue
            for raw_ref in proof.get("evidence_refs", []) or []:
                ref = str(raw_ref).strip()
                if ref and ref not in refs:
                    refs.append(ref)
        return refs

    @staticmethod
    def _looks_like_pytest_evidence_ref(ref: str) -> bool:
        normalized = str(ref).strip()
        if not normalized:
            return False
        path, _ = Orchestrator._split_evidence_ref(normalized)
        normalized_path = path.replace("\\", "/").strip()
        file_name = normalized_path.rsplit("/", 1)[-1].lower()
        return (
            " " not in normalized_path
            and normalized_path.endswith(".py")
            and (
                file_name.startswith("test_")
                or file_name.endswith("_test.py")
                or "/tests/" in f"/{normalized_path}"
            )
        )

    @staticmethod
    def _looks_like_python_evidence_ref(ref: str) -> bool:
        path, _ = Orchestrator._split_evidence_ref(ref)
        return path.replace("\\", "/").strip().endswith(".py")

    @staticmethod
    def _looks_like_supporting_evidence_ref(ref: str) -> bool:
        path, _ = Orchestrator._split_evidence_ref(ref)
        normalized = path.replace("\\", "/").strip()
        if not normalized or " " in normalized:
            return False
        return normalized.endswith(
            (
                ".py",
                ".md",
                ".json",
                ".toml",
                ".yaml",
                ".yml",
                ".ts",
                ".tsx",
                ".js",
                ".jsx",
            )
        )

    @staticmethod
    def _split_evidence_ref(ref: str) -> Tuple[str, str]:
        normalized = str(ref).strip()
        if "::" not in normalized:
            return normalized, ""
        path, selector = normalized.split("::", 1)
        return path.strip(), selector.strip()

    @staticmethod
    def _looks_like_vitest_evidence_ref(ref: str) -> bool:
        path, _ = Orchestrator._split_evidence_ref(ref)
        lowered = path.lower()
        return lowered.endswith(
            (
                ".test.js",
                ".test.jsx",
                ".test.ts",
                ".test.tsx",
                ".spec.js",
                ".spec.jsx",
                ".spec.ts",
                ".spec.tsx",
            )
        )

    def _proof_evidence_cache_key(self, task: TaskSpec) -> Tuple[str, str]:
        refs_payload = json.dumps(
            self._task_requirement_evidence_refs(task),
            ensure_ascii=True,
            sort_keys=True,
        )
        digest = hashlib.sha256(refs_payload.encode("utf-8")).hexdigest()
        return (task.task_id, f"{worktree_fingerprint(self.project_root)}:{digest}")

    def _proof_verification_command_templates(self) -> List[str]:
        commands: List[str] = []
        try:
            payload = load_task_plan(self.project_root)
        except Exception:
            payload = {}
        raw_steps = payload.get("verification_steps", [])
        if isinstance(raw_steps, list):
            steps = [
                VerificationStep.from_dict(dict(item))
                for item in raw_steps
                if isinstance(item, dict)
            ]
            try:
                step_commands = commands_from_verification_steps(steps, self.project_root)
            except ValueError:
                step_commands = []
            for command in step_commands:
                if command and command not in commands:
                    commands.append(command)
        for raw_command in payload.get("verification_commands", []) or []:
            command = str(raw_command).strip()
            if command and command not in commands:
                commands.append(command)
        try:
            gate_step_commands = commands_from_verification_steps(self.config.gates.steps, self.project_root)
        except ValueError:
            gate_step_commands = []
        for command in gate_step_commands:
            if command and command not in commands:
                commands.append(command)
        for raw_command in self.config.gates.commands:
            command = str(raw_command).strip()
            if command and command not in commands:
                commands.append(command)
        return commands

    def _rewrite_pytest_command_targets(
        self,
        command: str,
        targets: List[str],
    ) -> Optional[str]:
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if not parts:
            return None
        inner = _unwrap_conda_run(parts)
        prefix = parts[: len(parts) - len(inner)] if inner and len(inner) < len(parts) else []
        if not inner:
            return None
        executable = Path(inner[0]).name
        runner: List[str]
        args: List[str]
        if executable in {"pytest", "py.test"}:
            runner = [inner[0]]
            args = inner[1:]
        elif (
            len(inner) >= 3
            and Path(inner[0]).name in {"python", "python3"}
            and inner[1] == "-m"
            and inner[2] == "pytest"
        ):
            runner = inner[:3]
            args = inner[3:]
        else:
            return None

        preserved_args: List[str] = []
        index = 0
        option_parsing_done = False
        while index < len(args):
            arg = args[index]
            if arg == "--":
                preserved_args.append(arg)
                preserved_args.extend(args[index + 1 :])
                break
            if not option_parsing_done and arg.startswith("-"):
                preserved_args.append(arg)
                if arg in PYTEST_VALUE_OPTIONS and "=" not in arg and index + 1 < len(args):
                    preserved_args.append(args[index + 1])
                    index += 2
                    continue
                index += 1
                continue
            option_parsing_done = True
            index += 1

        return shlex.join([*prefix, *runner, *preserved_args, *targets])

    def _build_task_proof_evidence_command(self, evidence_refs: List[str]) -> str:
        for command in self._proof_verification_command_templates():
            rewritten = self._rewrite_pytest_command_targets(command, evidence_refs)
            if rewritten:
                return rewritten
        quoted_refs = " ".join(shlex.quote(ref) for ref in evidence_refs)
        conda_python = self.project_root / ".conda" / "bin" / "python"
        if conda_python.exists():
            return f"./.conda/bin/python -m pytest -q {quoted_refs}"
        conda_meta = self.project_root / ".conda" / "conda-meta"
        if conda_meta.exists():
            return f"conda run -p ./.conda python -m pytest -q {quoted_refs}"
        return f"{shlex.quote(sys.executable)} -m pytest -q {quoted_refs}"

    def _find_package_root_for_evidence_ref(self, ref: str) -> Optional[Path]:
        path, _ = self._split_evidence_ref(ref)
        if not path:
            return None
        candidate = Path(path)
        candidate = candidate if candidate.is_absolute() else (self.project_root / candidate)
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.project_root)
        except ValueError:
            return None
        current = candidate.parent if candidate.suffix else candidate
        while True:
            if (current / "package.json").exists():
                return current
            if current == self.project_root:
                return None
            parent = current.parent
            if parent == current:
                return None
            current = parent

    @staticmethod
    def _load_package_manifest(package_root: Path) -> Dict[str, object]:
        try:
            with (package_root / "package.json").open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _package_test_script_uses_vitest(cls, package_root: Path) -> bool:
        manifest = cls._load_package_manifest(package_root)
        scripts = manifest.get("scripts")
        if not isinstance(scripts, dict):
            return False
        test_script = str(scripts.get("test", "")).strip().lower()
        return "vitest" in test_script

    @classmethod
    def _package_supports_vitest(cls, package_root: Path) -> bool:
        manifest = cls._load_package_manifest(package_root)
        for key in ("devDependencies", "dependencies"):
            deps = manifest.get(key)
            if isinstance(deps, dict) and "vitest" in deps:
                return True
        if cls._package_test_script_uses_vitest(package_root):
            return True
        return any(
            (package_root / name).exists()
            for name in (
                "vitest.config.ts",
                "vitest.config.js",
                "vitest.config.mts",
                "vitest.config.mjs",
                "vitest.config.cts",
                "vitest.config.cjs",
            )
        )

    def _build_vitest_evidence_command(self, ref: str) -> Optional[str]:
        package_root = self._find_package_root_for_evidence_ref(ref)
        if package_root is None or not self._package_supports_vitest(package_root):
            return None
        path, selector = self._split_evidence_ref(ref)
        candidate = Path(path)
        candidate = candidate if candidate.is_absolute() else (self.project_root / candidate)
        candidate = candidate.resolve()
        try:
            target = candidate.relative_to(package_root)
            package_prefix = package_root.relative_to(self.project_root)
        except ValueError:
            return None
        command: List[str] = ["npm"]
        if package_prefix != Path("."):
            command.extend(["--prefix", str(package_prefix)])
        if self._package_test_script_uses_vitest(package_root):
            command.extend(["test", "--", str(target)])
        else:
            command.extend(["exec", "--", "vitest", "run", str(target)])
        if selector:
            command.extend(["-t", selector])
        return shlex.join(command)

    def _build_task_proof_evidence_command_for_ref(self, ref: str) -> Optional[str]:
        if self._looks_like_pytest_evidence_ref(ref):
            return self._build_task_proof_evidence_command([ref])
        if self._looks_like_vitest_evidence_ref(ref):
            return self._build_vitest_evidence_command(ref)
        return None

    def _build_task_verify_commands(self, task: Optional[TaskSpec]) -> List[str]:
        if task is None:
            return []
        commands: List[str] = []
        for ref in self._task_planned_evidence_refs(task):
            command = self._build_task_proof_evidence_command_for_ref(ref)
            if command and command not in commands:
                commands.append(command)
        return commands

    def _task_verify_command_scope_label(self, task: Optional[TaskSpec]) -> str:
        refs = self._task_planned_evidence_refs(task)
        executable = [
            ref for ref in refs
            if self._build_task_proof_evidence_command_for_ref(ref)
        ]
        if not executable:
            return ""
        preview = ", ".join(executable[:4])
        if len(executable) > 4:
            preview = f"{preview}, ..."
        return preview

    def _canonical_project_evidence_ref(self, ref: str) -> str:
        path, selector = self._split_evidence_ref(ref)
        normalized_path = path.replace("\\", "/").strip()
        if normalized_path:
            candidate = Path(normalized_path)
            if candidate.is_absolute():
                try:
                    normalized_path = str(
                        candidate.resolve().relative_to(self.project_root.resolve())
                    )
                except ValueError:
                    normalized_path = str(candidate)
            else:
                normalized_path = normalized_path.removeprefix("./")
        if selector:
            return f"{normalized_path}::{selector.strip()}"
        return normalized_path

    def _extract_pytest_not_found_ref(self, failure_id: str) -> str:
        text = str(failure_id).strip()
        match = re.search(r"\bnot found:\s+(?P<ref>\S+\.py(?:::[^\s]+)*)", text)
        if not match:
            return ""
        return self._canonical_project_evidence_ref(match.group("ref").strip())

    @staticmethod
    def _is_repair_task(task: Optional[TaskSpec]) -> bool:
        if task is None:
            return False
        return bool(task.parent_task_id.strip() or task.task_id.startswith("repair-"))

    def _owned_pytest_evidence_refs(self, task: Optional[TaskSpec]) -> Set[str]:
        if task is None:
            return set()
        return {
            self._canonical_project_evidence_ref(ref)
            for ref in self._task_planned_evidence_refs(task)
            if self._looks_like_pytest_evidence_ref(ref)
        }

    def _all_failures_are_owned_pytest_evidence_refs(
        self,
        task: Optional[TaskSpec],
        failure_ids: Iterable[str],
    ) -> bool:
        owned_refs = self._owned_pytest_evidence_refs(task)
        if not owned_refs:
            return False
        normalized_failures = [
            self._canonical_project_evidence_ref(failure_id)
            for failure_id in failure_ids
            if str(failure_id).strip()
        ]
        return bool(normalized_failures) and all(
            failure_id in owned_refs for failure_id in normalized_failures
        )

    def _all_failures_are_missing_owned_pytest_evidence_refs(
        self,
        task: Optional[TaskSpec],
        failure_ids: Iterable[str],
    ) -> bool:
        owned_refs = self._owned_pytest_evidence_refs(task)
        if not owned_refs:
            return False
        missing_refs = [
            self._extract_pytest_not_found_ref(failure_id)
            for failure_id in failure_ids
            if str(failure_id).strip()
        ]
        return bool(missing_refs) and all(ref in owned_refs for ref in missing_refs)

    def _run_task_proof_evidence(self, task: Optional[TaskSpec]) -> Optional[Dict[str, object]]:
        if task is None:
            return None
        evidence_refs = self._task_requirement_evidence_refs(task)
        if not evidence_refs:
            return None

        cache_key = self._proof_evidence_cache_key(task)
        cached = self._task_proof_evidence_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        command_pairs: List[Tuple[str, str]] = []
        unsupported_refs: List[str] = []
        supporting_refs: List[str] = []
        for ref in evidence_refs:
            command = self._build_task_proof_evidence_command_for_ref(ref)
            if not command:
                if self._looks_like_supporting_evidence_ref(ref):
                    supporting_refs.append(ref)
                else:
                    unsupported_refs.append(ref)
                continue
            command_pairs.append((ref, command))
        if unsupported_refs or not command_pairs:
            failed_refs = list(unsupported_refs or evidence_refs)
            result = {
                "ok": False,
                "reason": (
                    "owned proof evidence_refs are not executable verification targets: "
                    + ", ".join(failed_refs)
                ),
                "summary": (
                    f"Unsupported owned proof evidence refs ({len(failed_refs)}): "
                    + ", ".join(failed_refs)
                ),
                "evidence_refs": evidence_refs,
                "passed_refs": [],
                "failed_refs": failed_refs,
                "failure_ids": failed_refs,
                "command": "",
                "raw_output": "",
                "supporting_refs": supporting_refs,
            }
            self._task_proof_evidence_cache[cache_key] = dict(result)
            return result

        commands = [command for _, command in command_pairs]
        with log_timing(self.logger, f"proof-evidence commands={len(commands)}"):
            gate_result = run_gate_plan(
                [],
                [GateParallelGroup(name="proof-evidence", commands=commands)],
                self.project_root,
                collect_all=True,
            )
        raw_output = self._gate_raw_output(gate_result)
        failed_refs = [
            ref
            for (ref, _command), command_result in zip(command_pairs, gate_result.commands)
            if not command_result.ok
        ]
        passed_refs = [ref for ref in evidence_refs if ref not in failed_refs]
        command = commands[0] if len(commands) == 1 else "\n".join(commands)
        if gate_result.ok:
            result = {
                "ok": True,
                "reason": "",
                "summary": (
                    f"Owned proof evidence passed ({len(evidence_refs)} refs): "
                    + ", ".join(evidence_refs)
                ),
                "evidence_refs": evidence_refs,
                "passed_refs": passed_refs,
                "failed_refs": [],
                "failure_ids": [],
                "command": command,
                "raw_output": raw_output,
                "supporting_refs": supporting_refs,
            }
        else:
            failed_label = ", ".join(failed_refs[:6]) if failed_refs else gate_result.summary
            result = {
                "ok": False,
                "reason": (
                    f"owned proof evidence failed: {failed_label}"
                    if failed_label
                    else "owned proof evidence failed"
                ),
                "summary": (
                    f"Owned proof evidence failed ({len(failed_refs) or len(evidence_refs)} refs): "
                    + failed_label
                ),
                "evidence_refs": evidence_refs,
                "passed_refs": passed_refs,
                "failed_refs": failed_refs or list(evidence_refs),
                "failure_ids": failed_refs or list(evidence_refs),
                "command": command,
                "raw_output": raw_output or gate_result.summary,
                "supporting_refs": supporting_refs,
            }
        self._task_proof_evidence_cache[cache_key] = dict(result)
        return result

    def _task_verify_baseline_ref(self) -> str:
        return f"{head_ref(self.project_root)}:{worktree_fingerprint(self.project_root)}"

    @staticmethod
    def _git_ref_from_verify_baseline_ref(baseline_ref: str) -> str:
        candidate = str(baseline_ref or "").strip()
        if not candidate:
            return ""
        head, sep, _ = candidate.partition(":")
        if sep:
            if re.fullmatch(r"[0-9a-f]{7,40}", head, flags=re.IGNORECASE):
                return head
            if not head:
                return ""
        return candidate

    def _ensure_task_verify_baseline(self, task: TaskSpec) -> bool:
        baseline_ref = self._task_verify_baseline_ref()
        task_commands = self._build_task_verify_commands(task)
        if task.verify_baseline_ref == baseline_ref:
            return False
        task.verify_baseline_ref = baseline_ref
        if not task_commands:
            return True
        cached_failures = self._gate_baseline_cache.get(
            baseline_ref,
            task_commands,
            collect_all=True,
            parallel_groups=[],
        )
        if cached_failures is not None:
            task.verify_baseline_failures = list(cached_failures)
            return True
        gate, mutation_error = self._run_gate_commands_for_commands(
            task_commands,
            collect_all=True,
            context=f"task baseline verification commands ({task.task_id})",
        )
        if mutation_error:
            raise RuntimeError(mutation_error)
        failures = (
            self._normalize_verify_failure_ids(extract_failure_ids(gate), gate.summary)
            if not gate.ok
            else []
        )
        task.verify_baseline_failures = list(failures)
        self._gate_baseline_cache.put(
            baseline_ref,
            task_commands,
            collect_all=True,
            failure_ids=failures,
            summary=gate.summary,
            parallel_groups=[],
        )
        return True

    def _ensure_implement_verify_baseline(
        self,
        state: RunState,
        tasks: Iterable[TaskSpec],
    ) -> bool:
        baseline_ref = self._task_verify_baseline_ref()
        changed = False
        if state.implement_verify_baseline_ref != baseline_ref:
            state.implement_verify_baseline_ref = baseline_ref
            gate_commands = self._default_gate_commands()
            if not gate_commands:
                state.implement_verify_baseline_failures = []
            else:
                cached_failures = self._gate_baseline_cache.get(
                    baseline_ref,
                    gate_commands,
                    collect_all=True,
                    parallel_groups=self.config.gates.parallel_groups,
                )
                if cached_failures is not None:
                    state.implement_verify_baseline_failures = list(cached_failures)
                else:
                    gate, mutation_error = self._run_gate_commands(
                        collect_all=True,
                        context="implement verify baseline commands",
                    )
                    if mutation_error:
                        raise RuntimeError(mutation_error)
                    failures = (
                        self._normalize_verify_failure_ids(extract_failure_ids(gate), gate.summary)
                        if not gate.ok
                        else []
                    )
                    state.implement_verify_baseline_failures = list(failures)
                    self._gate_baseline_cache.put(
                        baseline_ref,
                        gate_commands,
                        collect_all=True,
                        failure_ids=failures,
                        summary=gate.summary,
                        parallel_groups=self.config.gates.parallel_groups,
                    )
            changed = True
        baseline_failures = list(state.implement_verify_baseline_failures)
        for task in tasks:
            if task.status == "done":
                continue
            if task.verify_baseline_failures != baseline_failures:
                task.verify_baseline_failures = list(baseline_failures)
                changed = True
        return changed

    def _warm_clean_head_verify_baseline(
        self,
        state: RunState,
        *,
        failure_ids: Iterable[str],
    ) -> None:
        baseline_ref = self._task_verify_baseline_ref()
        failure_list = [str(item).strip() for item in failure_ids if str(item).strip()]
        normalized = self._normalize_verify_failure_ids(failure_list, "") if failure_list else []
        state.implement_verify_baseline_ref = baseline_ref
        state.implement_verify_baseline_failures = list(normalized)
        gate_commands = (
            commands_from_verification_steps(self.config.gates.steps, self.project_root)
            if self.config.gates.steps
            else self.config.gates.commands
        )
        if not gate_commands:
            return
        self._gate_baseline_cache.put(
            baseline_ref,
            gate_commands,
            collect_all=True,
            failure_ids=normalized,
            summary="warm clean-head baseline",
            parallel_groups=self.config.gates.parallel_groups,
        )

    @staticmethod
    def _gate_raw_output(gate_result) -> str:
        sections: List[str] = []
        for cmd_result in gate_result.commands:
            if cmd_result.ok:
                continue
            sections.append(f"$ {cmd_result.command}")
            if cmd_result.stdout:
                sections.append(cmd_result.stdout)
            if cmd_result.stderr:
                sections.append(cmd_result.stderr)
        return "\n".join(section for section in sections if section).strip()

    @staticmethod
    def _truncate_feedback_text(text: str, limit: int = 400) -> str:
        compact = " ".join(text.split()).strip()
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."

    @staticmethod
    def _truncate_feedback_excerpt(text: str, limit: int = 900) -> str:
        excerpt = text.strip()
        if len(excerpt) <= limit:
            return excerpt
        return excerpt[: limit - 3].rstrip() + "..."

    def _extract_verify_implicated_paths(self, raw_output: str) -> List[str]:
        if not raw_output.strip():
            return []
        project_root = self.project_root.resolve()
        paths: List[str] = []
        for raw_path in re.findall(r'File "([^"]+)"', raw_output):
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = (project_root / candidate).resolve()
            else:
                candidate = candidate.resolve()
            try:
                relative = candidate.relative_to(project_root)
            except ValueError:
                continue
            normalized = str(relative)
            if normalized not in paths:
                paths.append(normalized)
        for raw_path in re.findall(r"(?:(?<=^)|(?<=[\s`'\"(]))((?:tests?|__tests__)/[^\s:`'\")]+)", raw_output, re.MULTILINE):
            candidate = (project_root / raw_path).resolve()
            if not candidate.exists():
                continue
            try:
                relative = candidate.relative_to(project_root)
            except ValueError:
                continue
            normalized = str(relative)
            if normalized not in paths:
                paths.append(normalized)
        return paths[:8]

    @staticmethod
    def _extract_verify_root_causes(raw_output: str) -> List[str]:
        if not raw_output.strip():
            return []
        pattern = re.compile(
            r"^\s*(?:AssertionError|RuntimeError|TypeError|ValueError|KeyError|IndexError|"
            r"StopIteration|AttributeError|NameError|ImportError|ModuleNotFoundError|"
            r"sqlite3\.[A-Za-z]+Error|OSError|SyntaxError): .+$",
            re.MULTILINE,
        )
        causes: List[str] = []
        for match in pattern.findall(raw_output):
            normalized = match.strip()
            if normalized not in causes:
                causes.append(normalized)
        return causes[:4]

    @classmethod
    def _extract_verify_excerpts(cls, raw_output: str) -> List[str]:
        if not raw_output.strip():
            return []
        lines = raw_output.splitlines()
        excerpt_starts = [
            index
            for index, line in enumerate(lines)
            if re.match(r"^(?:FAIL|ERROR):\s+|^FAILED\s+\S+", line)
        ]
        if not excerpt_starts:
            excerpt_starts = [
                max(0, index - 2)
                for index, line in enumerate(lines)
                if re.search(
                    r"(?:AssertionError|RuntimeError|TypeError|ValueError|KeyError|IndexError|"
                    r"StopIteration|AttributeError|NameError|ImportError|ModuleNotFoundError|"
                    r"sqlite3\.[A-Za-z]+Error|OSError|SyntaxError):",
                    line,
                )
            ]
        excerpts: List[str] = []
        for position, start in enumerate(excerpt_starts[:3]):
            end = excerpt_starts[position + 1] if position + 1 < len(excerpt_starts) else len(lines)
            excerpt = "\n".join(lines[start:min(end, start + 14)]).strip()
            excerpt = cls._truncate_feedback_excerpt(excerpt, limit=900)
            if excerpt and excerpt not in excerpts:
                excerpts.append(excerpt)
        return excerpts[:3]

    def _build_verify_retry_feedback(
        self,
        verify_result: Dict[str, object],
    ) -> Dict[str, object]:
        reason = str(verify_result.get("reason", "")).strip()
        current_failure_ids = self._normalize_verify_failure_ids(
            verify_result.get("current_failure_ids", []),
            reason,
        )
        new_failure_ids = [
            str(item).strip() for item in verify_result.get("new_failure_ids", []) if str(item).strip()
        ]
        baseline_failure_ids = [
            str(item).strip()
            for item in verify_result.get("baseline_failure_ids", [])
            if str(item).strip()
        ]
        raw_output = str(verify_result.get("raw_output", "")).strip()
        raw_log_path = str(verify_result.get("raw_log_path", "")).strip()
        summary_lines: List[str] = []
        if not bool(verify_result.get("comparable_failures", True)):
            summary_lines.append(
                "Failure identity is non-comparable; retry early-stop comparison is disabled."
            )
        if raw_log_path:
            summary_lines.append(f"Full failed verification log: {raw_log_path}")
        if baseline_failure_ids:
            baseline_remaining = sorted(set(current_failure_ids) & set(baseline_failure_ids))
            if new_failure_ids:
                summary_lines.append(
                    f"New failures vs task baseline ({len(new_failure_ids)}): "
                    + ", ".join(new_failure_ids[:6])
                )
            if baseline_remaining:
                summary_lines.append(
                    f"Pre-existing baseline failures still present ({len(baseline_remaining)}): "
                    + ", ".join(baseline_remaining[:6])
                )
            resolved_failures = sorted(set(baseline_failure_ids) - set(current_failure_ids))
            if resolved_failures:
                summary_lines.append(
                    f"Baseline failures resolved in this attempt ({len(resolved_failures)}): "
                    + ", ".join(resolved_failures[:6])
                )
        elif current_failure_ids:
            summary_lines.append(
                f"Failing checks ({len(current_failure_ids)}): " + ", ".join(current_failure_ids[:6])
            )
        root_causes = self._extract_verify_root_causes(raw_output)
        if root_causes:
            summary_lines.append("Likely root causes:")
            summary_lines.extend(f"  - {item}" for item in root_causes)
        implicated_paths = self._extract_verify_implicated_paths(raw_output)
        guidance = self._verify_failure_recovery_guidance(
            failure_ids=(new_failure_ids or current_failure_ids),
            reason=reason,
            implicated_paths=implicated_paths,
            comparable=bool(verify_result.get("comparable_failures", True)),
        )
        if guidance:
            summary_lines.append("Recovery guidance:")
            summary_lines.extend(f"  - {line}" for line in guidance.splitlines())
        raw_excerpts = self._extract_verify_excerpts(raw_output)
        if not raw_excerpts and reason:
            raw_excerpts = [self._truncate_feedback_text(reason, limit=900)]
        return {
            "verification_summary": "\n".join(summary_lines).strip(),
            "implicated_paths": implicated_paths,
            "raw_excerpts": raw_excerpts,
        }

    def _verify_failure_recovery_guidance(
        self,
        *,
        failure_ids: Iterable[str],
        reason: str,
        implicated_paths: Iterable[str],
        comparable: bool,
    ) -> str:
        ids = [str(item).strip() for item in failure_ids if str(item).strip()]
        paths = [str(item).strip().replace("\\", "/") for item in implicated_paths if str(item).strip()]
        combined = " ".join([reason, *ids, *paths]).lower()
        lines: List[str] = []

        if not comparable:
            lines.append(
                "Failure identity is unstable; inspect command output, environment setup, and test discovery before changing product behavior."
            )

        stale_markers = (
            "stale-plan-coupled-test:",
            "stale-task-status-test:",
            "update the repository tests",
            "stale test",
            "stale status",
            "retired task id",
        )
        if any(marker in combined for marker in stale_markers):
            lines.append(
                "This looks like a stale repository test; align the affected test with the current task plan and active requirements."
            )

        if ids:
            id_paths = [
                self._split_evidence_ref(item)[0].replace("\\", "/")
                for item in ids
                if not item.startswith("reason:") and not item.startswith("cmd:")
            ]
            if id_paths and all(self._is_test_path(path) for path in id_paths):
                lines.append(
                    "Failing IDs are test/proof paths; inspect whether assertions encode outdated expectations before changing implementation."
                )

        if paths and any(self._is_test_path(path) for path in paths):
            lines.append(
                "Implicated paths include tests; compare their assertions with active acceptance oracles and nearby contract tests."
            )
        if paths and any(not self._is_test_path(path) for path in paths):
            lines.append(
                "Implicated paths include product code; fix implementation when it violates the current public contract."
            )

        if not lines:
            lines.append(
                "Classify the failure as implementation bug, stale test, or mixed by comparing the failing assertion with active requirements."
            )
        lines.append(
            "If requirements and existing tests conflict and no active oracle resolves it, stop and surface a clarification blocker instead of guessing."
        )
        return "\n".join(lines)

    def _run_provider_research(self, state: RunState, spec_file: Path) -> RunState:
        del spec_file
        trace = load_requirements_trace(self.project_root)
        docs_required = external_doc_requirements(trace)
        if not docs_required:
            summary = "No provider research required by active requirements."
            write_text(self._stage_output_path(state.run_id, "provider_research"), summary + "\n")
            state.current_stage = "provider_research"
            state.stage_summaries["provider_research"] = summary
            state.last_error = ""
            return state

        lock = load_provider_references_lock(self.project_root)
        unresolved = []
        for requirement in docs_required:
            reference = str(requirement.get("provider_reference", "")).strip()
            status = provider_reference_status(lock, reference)
            if not self._is_resolved_provider_reference_status(status):
                unresolved.append(requirement)
        if not unresolved:
            summary = "Provider references already verified; research reused from local lock."
            write_text(self._stage_output_path(state.run_id, "provider_research"), summary + "\n")
            state.current_stage = "provider_research"
            state.stage_summaries["provider_research"] = summary
            state.last_error = ""
            return state

        provider_references_dir(self.project_root).mkdir(parents=True, exist_ok=True)
        prompt = self._build_provider_research_prompt(unresolved)
        if state.rejected_stage == "provider_research" and state.rejection_reason:
            prompt += (
                "\n\nThe previous provider research output was rejected. Please address this feedback:\n"
                f"{state.rejection_reason}\n"
            )
            state.rejected_stage = ""
            state.rejection_reason = ""
        result = self._run_agent_with_retries(
            state=state,
            stage="provider_research",
            stage_key="provider_research",
            prompt=prompt,
            validation_feedback=self._provider_research_validation_feedback,
            effort=self.config.efforts.get("provider_research", "deep"),
        )
        still_blocked = [
            f"{item['requirement_id']}: {item['reference'] or '(missing)'} is {item['status']}"
            for item in self.provider_research_blockers()
        ]
        if still_blocked:
            detail = "\n".join(f"- {item}" for item in still_blocked)
            raise RuntimeError(
                "provider research is blocked; provide official docs, defer the requirement, "
                "choose another provider, or explicitly approve assumptions before resuming.\n"
                f"{detail}"
            )
        state.current_stage = "provider_research"
        state.stage_summaries["provider_research"] = result.summary.strip()
        state.last_error = ""
        return state

    @staticmethod
    def _is_resolved_provider_reference_status(status: str) -> bool:
        return status in {"verified", "assumption_approved", "deferred"}

    @staticmethod
    def is_provider_research_blocked_error(message: str) -> bool:
        return message.strip().startswith("provider research is blocked;")

    def provider_research_blockers(self) -> List[Dict[str, str]]:
        trace = load_requirements_trace(self.project_root)
        lock = load_provider_references_lock(self.project_root)
        blockers: List[Dict[str, str]] = []
        for requirement in external_doc_requirements(trace):
            req_id = str(requirement.get("id", "")).strip() or "(unknown requirement)"
            reference = str(requirement.get("provider_reference", "")).strip()
            if not reference:
                blockers.append(
                    {
                        "requirement_id": req_id,
                        "reference": "",
                        "status": "missing",
                        "reason": "missing provider_reference in requirements trace",
                    }
                )
                continue
            ref_path = self.project_root / reference
            status = provider_reference_status(lock, reference)
            normalized_status = status or "missing"
            if not ref_path.exists():
                blockers.append(
                    {
                        "requirement_id": req_id,
                        "reference": reference,
                        "status": "missing",
                        "reason": f"missing provider reference file {reference}",
                    }
                )
                continue
            if not self._is_resolved_provider_reference_status(normalized_status):
                blockers.append(
                    {
                        "requirement_id": req_id,
                        "reference": reference,
                        "status": normalized_status,
                        "reason": f"{reference} is {normalized_status}",
                    }
                )
        return blockers

    def provider_research_resolution_report(self, state: Optional[RunState] = None) -> Dict[str, object]:
        state = state or load_run_state(self.project_root)
        blockers = self.provider_research_blockers()
        if not self.is_provider_research_blocked_error(state.last_error):
            return {
                "eligible": False,
                "reason": "Current run is not blocked by provider_research.",
                "run_id": state.run_id,
                "last_error": state.last_error,
                "blockers": blockers,
            }
        if not blockers:
            return {
                "eligible": False,
                "reason": "Provider references no longer have unresolved blockers.",
                "run_id": state.run_id,
                "last_error": state.last_error,
                "blockers": [],
            }
        return {
            "eligible": True,
            "reason": "",
            "run_id": state.run_id,
            "last_error": state.last_error,
            "blockers": blockers,
        }

    def build_provider_resolve_goal(self, state: Optional[RunState] = None) -> str:
        report = self.provider_research_resolution_report(state)
        if not report["eligible"]:
            raise RuntimeError(str(report["reason"]))
        lines = [
            "Recover the blocked provider_research stage for the current run.",
            f"Run ID: {report['run_id']}",
            "Current blockers:",
        ]
        for blocker in report["blockers"]:
            if not isinstance(blocker, dict):
                continue
            lines.append(
                f"- {blocker.get('requirement_id')}: {blocker.get('reference') or '(missing)'} "
                f"is {blocker.get('status')} ({blocker.get('reason')})"
            )
        lines.extend(
            [
                "",
                "Discuss the unblock path with the user, update only provider-research artifacts, "
                "and reach a locally valid provider reference state so the pipeline can resume.",
            ]
        )
        return "\n".join(lines)

    def _capture_resume_context(
        self,
        state: RunState,
        *,
        spec_file: Path,
        auto_approve: bool,
        allow_dirty_tree: bool,
        max_tasks: Optional[int],
        skip_validate: bool,
        print_agent_output: bool,
        provider_kind: Optional[str],
        doc_language: Optional[str],
    ) -> None:
        previous_run_id = str(state.resume_context.get("previous_run_id", "")).strip()
        previous_task_plan_archive = str(
            state.resume_context.get("previous_task_plan_archive", "")
        ).strip()
        state.resume_context = {
            "spec_file": str(spec_file),
            "auto_approve": bool(auto_approve),
            "allow_dirty_tree": bool(allow_dirty_tree),
            "max_tasks": int(max_tasks) if max_tasks is not None else None,
            "skip_validate": bool(skip_validate),
            "print_agent_output": bool(print_agent_output),
            "provider_kind": str(provider_kind).strip() if provider_kind else "",
            "doc_language": str(doc_language).strip() if doc_language else "",
        }
        if previous_run_id:
            state.resume_context["previous_run_id"] = previous_run_id
        if previous_task_plan_archive:
            state.resume_context["previous_task_plan_archive"] = previous_task_plan_archive

    def resume_saved_run(self) -> RunState:
        state = load_run_state(self.project_root)
        context = dict(state.resume_context)
        spec_file = Path(str(context.get("spec_file") or (self.project_root / "spec.md")))
        raw_max_tasks = context.get("max_tasks")
        max_tasks = int(raw_max_tasks) if raw_max_tasks not in (None, "") else None
        provider_kind = str(context.get("provider_kind", "")).strip() or None
        doc_language = str(context.get("doc_language", "")).strip() or None
        return self.run(
            spec_file=spec_file,
            auto_approve=bool(context.get("auto_approve", False)),
            allow_dirty_tree=bool(context.get("allow_dirty_tree", False)),
            max_tasks=max_tasks,
            skip_validate=bool(context.get("skip_validate", False)),
            print_agent_output=bool(context.get("print_agent_output", False)),
            provider_kind=provider_kind,
            doc_language=doc_language,
        )

    def _run_task_review(
        self,
        run_id: str,
        task: TaskSpec,
        verify_reason: str = "",
        proof_evidence: Optional[Dict[str, object]] = None,
        state: Optional[RunState] = None,
    ) -> Dict[str, object]:
        review_effort = self._review_effort_for_task(task)
        plan_migration_context = self._build_task_plan_migration_context(state, task)
        review_prompt = self._build_task_prompt(
            task,
            "review",
            review_context=self._build_review_context(
                verify_reason=verify_reason,
                proof_evidence=proof_evidence,
            ),
            plan_migration_context=plan_migration_context,
        )
        review_result = self._run_agent_with_retries(
            state=None,
            stage="review",
            stage_key=f"review-{task.task_id}",
            prompt=review_prompt,
            validation_feedback=self._review_validation_feedback,
            run_id=run_id,
            effort=review_effort,
        )
        decision, summary = self._parse_review_decision(review_result.summary)
        write_text(review_path(self.project_root), summary + "\n")
        self._emit_task_review_result(task, decision, summary)
        if decision != "pass":
            return {"ok": False, "review": summary, "reason": "review rejected the task"}
        return {"ok": True, "review": summary}

    def _review_effort_for_task(self, task: TaskSpec) -> str:
        default_effort = self.config.efforts.get("review", "balanced")
        if default_effort != "balanced":
            return default_effort

        if task.review_summary.strip():
            return "deep"

        paths = self._changed_paths_excluding_agent_instructions()
        has_test_changes = any(self._is_test_path(path) for path in paths)
        non_test_paths = [path for path in paths if not self._is_test_path(path)]
        if not non_test_paths:
            return "balanced"
        if not has_test_changes:
            return "deep"
        if len(non_test_paths) > 3:
            return "deep"
        if any(self._is_high_risk_review_path(path) for path in non_test_paths):
            return "deep"

        estimated_lines = 0
        for path in non_test_paths:
            file_path = self.project_root / path
            if not file_path.is_file():
                continue
            try:
                with file_path.open("r", encoding="utf-8") as handle:
                    estimated_lines += sum(1 for _ in handle)
            except UnicodeDecodeError:
                return "deep"
            if estimated_lines > 240:
                return "deep"
        return "balanced"

    @staticmethod
    def _is_test_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        if normalized.startswith("tests/"):
            return True
        if normalized.endswith(("_test.py", ".spec.js", ".spec.ts", ".test.js", ".test.ts", ".test.tsx", ".test.jsx")):
            return True
        return False

    @staticmethod
    def _is_high_risk_review_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        high_risk_names = {
            "pyproject.toml",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "poetry.lock",
            "requirements.txt",
            "dockerfile",
            "compose.yml",
            "docker-compose.yml",
        }
        if normalized in high_risk_names:
            return True
        return normalized.startswith(".github/")

    def _git_text(self, *args: str) -> str:
        process = subprocess.run(
            ["git", *args],
            cwd=str(self.project_root),
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        if process.returncode != 0:
            return ""
        return process.stdout.strip()

    @staticmethod
    def _format_proof_evidence_summary(proof_evidence: Optional[Dict[str, object]]) -> str:
        if not isinstance(proof_evidence, dict):
            return ""
        summary = str(proof_evidence.get("summary", "")).strip()
        if not summary:
            return ""
        lines = [summary]
        command = str(proof_evidence.get("command", "")).strip()
        if command:
            lines.append(f"Command: {command}")
        passed_refs = [
            str(item).strip()
            for item in proof_evidence.get("passed_refs", [])
            if str(item).strip()
        ]
        failed_refs = [
            str(item).strip()
            for item in proof_evidence.get("failed_refs", [])
            if str(item).strip()
        ]
        if passed_refs:
            lines.append("Passed refs: " + ", ".join(passed_refs))
        if failed_refs:
            lines.append("Failed refs: " + ", ".join(failed_refs))
        return "\n".join(lines)

    def _build_review_context(
        self,
        verify_reason: str = "",
        *,
        proof_evidence: Optional[Dict[str, object]] = None,
        max_diff_chars: int = 20000,
    ) -> str:
        entries = changed_entries(self.project_root)
        lines = [
            "Review the current task by prioritizing the diff context below before exploring unrelated files.",
        ]
        if verify_reason.strip():
            lines.extend(["Local verification summary:", verify_reason.strip()])
        proof_summary = self._format_proof_evidence_summary(proof_evidence)
        if proof_summary:
            lines.extend(["Current owned proof evidence:", proof_summary])
        if entries:
            lines.append("Changed files:")
            lines.extend(f"- {path}" for _, path in entries[:40])
            if len(entries) > 40:
                lines.append(f"- ... {len(entries) - 40} more files")

        diff_stat = self._git_text("diff", "--stat", "--", ".", ":(exclude).auto-agents")
        if diff_stat:
            lines.extend(["Diff stat:", diff_stat])

        diff_excerpt = self._git_text("diff", "--no-ext-diff", "--unified=3", "--", ".", ":(exclude).auto-agents")
        if diff_excerpt:
            if len(diff_excerpt) > max_diff_chars:
                diff_excerpt = diff_excerpt[:max_diff_chars].rstrip() + "\n... [diff truncated]"
            lines.extend(["Diff excerpt:", diff_excerpt])

        untracked_paths = [path for status, path in entries if status == "??"]
        if untracked_paths:
            lines.append("Untracked file excerpts:")
            remaining_chars = max_diff_chars
            for path in untracked_paths[:10]:
                file_path = self.project_root / path
                if not file_path.is_file():
                    continue
                try:
                    snippet = file_path.read_text(encoding="utf-8")[: min(800, remaining_chars)]
                except UnicodeDecodeError:
                    lines.append(f"```text\n# {path}\n[binary or non-utf8 file omitted]\n```")
                    continue
                if not snippet.strip():
                    continue
                lines.append(f"```text\n# {path}\n{snippet.rstrip()}\n```")
                remaining_chars -= len(snippet)
                if remaining_chars <= 0:
                    lines.append("[untracked excerpts truncated]")
                    break
        return "\n".join(lines)

    def _quick_verify_failure_details(
        self,
        commands: Optional[Iterable[str]] = None,
    ) -> Optional[Tuple[str, bool]]:
        conda_meta = self.project_root / ".conda" / "conda-meta"
        command_list = list(commands) if commands is not None else list(self.config.gates.commands)
        shell_tokens = {"|", "||", "&&", ";", "$(", "`"}
        shell_builtins = {
            ":",
            ".",
            "alias",
            "bg",
            "cd",
            "echo",
            "eval",
            "exec",
            "exit",
            "export",
            "fg",
            "printf",
            "pwd",
            "read",
            "set",
            "shift",
            "test",
            "times",
            "trap",
            "true",
            "type",
            "ulimit",
            "umask",
            "unalias",
            "unset",
            "wait",
        }

        command_path_errors = validate_verification_command_paths(
            command_list,
            self.project_root,
            "verification commands" if commands is not None else "gates.commands",
        )
        if command_path_errors:
            return command_path_errors[0], False

        for command in command_list:
            stripped = command.strip()
            if not stripped:
                continue
            if (".conda/conda-meta" in stripped or "conda run -p ./.conda" in stripped) and not conda_meta.exists():
                return "expected a project-local conda environment at ./.conda/conda-meta before verification", True
            if any(token in stripped for token in shell_tokens):
                continue
            try:
                parts = shlex.split(stripped)
            except ValueError:
                continue
            if not parts:
                continue
            executable = parts[0]
            if executable in shell_builtins:
                continue
            if "/" in executable:
                candidate = (self.project_root / executable).resolve() if executable.startswith(".") else Path(executable)
                if not candidate.exists():
                    return f"verification command is not runnable: {command}", True
                continue
            if shutil.which(executable) is None:
                return f"verification command is not runnable: {command}", True
        return None

    def _quick_verify_failure(self, commands: Optional[Iterable[str]] = None) -> Optional[str]:
        failure = self._quick_verify_failure_details(commands)
        if failure is None:
            return None
        return failure[0]

    @staticmethod
    def _format_retry_feedback(
        failure_type: str,
        reason: str = "",
        review_summary: str = "",
        review_history: Optional[List[Dict[str, object]]] = None,
        verification_summary: str = "",
        proof_evidence_summary: str = "",
        implicated_paths: Optional[List[str]] = None,
        raw_excerpts: Optional[List[str]] = None,
    ) -> str:
        lines = [f"- Failure type: {failure_type}"]
        if reason:
            rendered_reason = reason
            if verification_summary.strip() or raw_excerpts:
                rendered_reason = Orchestrator._truncate_feedback_text(reason, limit=500)
            lines.append(f"- Reason: {rendered_reason}")
        if verification_summary.strip():
            lines.extend(["- Verification triage:", verification_summary.strip()])
        if proof_evidence_summary.strip():
            lines.extend(["- Current proof evidence:", proof_evidence_summary.strip()])
        if implicated_paths:
            lines.append(f"- Implicated paths: {', '.join(implicated_paths[:8])}")
        if raw_excerpts:
            lines.append("- Key verify evidence:")
            for index, excerpt in enumerate(raw_excerpts[:3], start=1):
                lines.append(f"  --- Excerpt {index} ---")
                for raw_line in excerpt.splitlines():
                    lines.append(f"  {raw_line}")
        if review_history and len(review_history) > 1:
            lines.append("- Review history (oldest first):")
            for i, entry in enumerate(review_history):
                is_latest = i == len(review_history) - 1
                status = "[CURRENT - must fix]" if is_latest else "[ADDRESSED in later attempt]"
                lines.append(f"  --- Attempt {entry.get('attempt', '?')} {status} ---")
                lines.append(f"  {entry.get('summary', '').strip()}")
        elif review_summary.strip():
            lines.extend(["- Review summary:", review_summary.strip()])
        return "\n".join(lines)

    @staticmethod
    def _task_requirement_proof_keys(task: TaskSpec) -> Set[Tuple[str, int]]:
        keys: Set[Tuple[str, int]] = set()
        for proof in task.requirement_proofs:
            if not isinstance(proof, dict):
                continue
            req_id = str(proof.get("requirement_id", "")).strip()
            oracle_index = Orchestrator._proof_oracle_index(proof.get("oracle_index"))
            if req_id and oracle_index is not None:
                keys.add((req_id, oracle_index))
        return keys

    @staticmethod
    def _review_feedback_oracle_refs(text: str) -> Set[Tuple[str, int]]:
        refs: Set[Tuple[str, int]] = set()
        if not text:
            return refs
        req_pattern = r"(REQ-\d+)"
        patterns = [
            rf"{req_pattern}.{{0,120}}?oracle_index\s*[:=#]?\s*(\d+)",
            rf"{req_pattern}.{{0,120}}?acceptance\s+oracle\s*#?\s*(\d+)",
            rf"{req_pattern}.{{0,120}}?oracle\s*#\s*(\d+)",
            rf"{req_pattern}.{{0,120}}?第\s*(\d+)\s*条.{{0,40}}?oracle",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL):
                try:
                    refs.add((match.group(1), int(match.group(2))))
                except (TypeError, ValueError):
                    continue
        return refs

    @staticmethod
    def _review_feedback_evidence_refs(text: str) -> Set[str]:
        refs: Set[str] = set()
        if not text:
            return refs
        pattern = re.compile(
            r"((?:[\w.-]+/)+[^\s`'\"()]+\.(?:py|(?:test|spec)\.[jt]sx?)(?:::[^\n`'\"]+)?)"
        )
        for match in pattern.findall(text):
            normalized = str(match).strip().rstrip(".,)")
            if normalized:
                refs.add(normalized)
        return refs

    @staticmethod
    def _review_feedback_paths(text: str) -> Set[str]:
        refs: Set[str] = set()
        if not text:
            return refs
        pattern = re.compile(
            r"((?:\.?[\w.-]+/)+[^\s`'\"()]+\.(?:py|md|json|toml|ya?ml|tsx?|jsx?))"
        )
        for match in pattern.findall(text):
            normalized = Orchestrator._normalize_audit_blocker_path(match.strip().rstrip(".,):;"))
            if normalized:
                refs.add(normalized)
        return refs

    @classmethod
    def _review_feedback_rewind_stage(cls, text: str) -> str:
        owner_order = ["clarify", "design", "plan", "provider_research"]
        owners: Set[str] = set()
        for path in cls._review_feedback_paths(text):
            owner = cls._forbidden_pattern_owner_stage({"path": path})
            if owner in owner_order:
                owners.add(owner)
        for owner in owner_order:
            if owner in owners:
                return owner
        return ""

    @staticmethod
    def _review_feedback_is_obsolete_for_task(task: TaskSpec, text: str) -> bool:
        current_keys = Orchestrator._task_requirement_proof_keys(task)
        if not current_keys:
            return False
        referenced_keys = Orchestrator._review_feedback_oracle_refs(text)
        return bool(referenced_keys) and referenced_keys.isdisjoint(current_keys)

    @staticmethod
    def _review_feedback_is_resolved_by_current_evidence(
        text: str,
        proof_evidence: Optional[Dict[str, object]],
    ) -> bool:
        if not text or not isinstance(proof_evidence, dict) or not bool(proof_evidence.get("ok")):
            return False
        passed_refs = {
            str(item).strip()
            for item in proof_evidence.get("passed_refs", [])
            if str(item).strip()
        }
        if not passed_refs:
            return False
        referenced_refs = Orchestrator._review_feedback_evidence_refs(text)
        return bool(referenced_refs) and referenced_refs.issubset(passed_refs)

    @staticmethod
    def _format_task_review_retry_feedback(
        task: TaskSpec,
        *,
        reason: str,
        review_summary: str = "",
        review_history: Optional[List[Dict[str, object]]] = None,
        proof_evidence: Optional[Dict[str, object]] = None,
    ) -> str:
        review_history = review_history or []
        if Orchestrator._review_feedback_is_obsolete_for_task(task, review_summary):
            return Orchestrator._format_retry_feedback(
                "review_rejected",
                reason=(
                    reason
                    + "\nPersisted review feedback was omitted because it only references "
                    "requirement oracle proof(s) that are not present in this task's current "
                    "requirement_proofs. The current Task JSON proof ownership is authoritative."
                ),
                proof_evidence_summary=Orchestrator._format_proof_evidence_summary(proof_evidence),
            )
        if Orchestrator._review_feedback_is_resolved_by_current_evidence(review_summary, proof_evidence):
            return Orchestrator._format_retry_feedback(
                "review_rejected",
                reason=(
                    reason
                    + "\nPersisted review feedback was omitted because its cited evidence_refs now "
                    "pass on the current worktree."
                ),
                proof_evidence_summary=Orchestrator._format_proof_evidence_summary(proof_evidence),
            )

        filtered_history = [
            entry
            for entry in review_history
            if not Orchestrator._review_feedback_is_obsolete_for_task(
                task,
                str(entry.get("summary", "")) if isinstance(entry, dict) else "",
            )
            and not Orchestrator._review_feedback_is_resolved_by_current_evidence(
                str(entry.get("summary", "")) if isinstance(entry, dict) else "",
                proof_evidence,
            )
        ]
        if len(filtered_history) != len(review_history):
            reason = (
                reason
                + "\nSome older review feedback was omitted because it references requirement "
                "oracle proof(s) outside this task's current requirement_proofs or cites "
                "evidence_refs that now pass on the current worktree."
            )

        return Orchestrator._format_retry_feedback(
            "review_rejected",
            reason=reason,
            review_history=filtered_history,
            review_summary=review_summary,
            proof_evidence_summary=Orchestrator._format_proof_evidence_summary(proof_evidence),
        )

    def _cached_review_result(self, state: RunState, task: TaskSpec, fingerprint: str) -> Optional[Dict[str, object]]:
        cache_entry = state.task_review_cache.get(task.task_id, {})
        if cache_entry.get("fingerprint") != fingerprint:
            return None
        if cache_entry.get("decision") != "pass":
            return None
        summary = cache_entry.get("summary", "").strip()
        if not summary:
            return None
        write_text(review_path(self.project_root), summary + "\n")
        return {"ok": True, "review": summary}

    def _store_task_review_cache(self, state: RunState, task: TaskSpec, fingerprint: str, summary: str) -> None:
        state.task_review_cache[task.task_id] = {
            "fingerprint": fingerprint,
            "decision": "pass",
            "summary": summary.strip(),
        }
        save_run_state(self.project_root, state)

    def _run_verify(self, state: RunState) -> RunState:
        verify_gate, mutation_error = self._run_gate_commands(
            collect_all=False,
            context="verify stage commands",
        )
        if mutation_error:
            raise RuntimeError(mutation_error)
        lines = ["# Verify", "", f"Result: {'pass' if verify_gate.ok else 'fail'}", ""]
        for item in verify_gate.commands:
            lines.append(f"- `{item.command}` -> {'ok' if item.ok else 'failed'}")
        summary = "\n".join(lines) + "\n"
        output_path = self._stage_output_path(state.run_id, "verify")
        write_text(output_path, summary)
        state.current_stage = "verify"
        state.stage_summaries["verify"] = summary.strip()
        state.last_error = ""
        if not verify_gate.ok:
            state.status = "failed"
            raw_output = self._gate_raw_output(verify_gate)
            raw_log_path = self._persist_failed_verification_log(raw_output, label="verify-stage")
            gate_commands = (
                commands_from_verification_steps(self.config.gates.steps, self.project_root)
                if self.config.gates.steps
                else self.config.gates.commands
            )
            if gate_commands:
                self._gate_baseline_cache.put(
                    self._task_verify_baseline_ref(),
                    gate_commands,
                    collect_all=False,
                    failure_ids=self._normalize_verify_failure_ids(
                        extract_failure_ids(verify_gate),
                        verify_gate.summary,
                    ),
                    summary=verify_gate.summary,
                    parallel_groups=self.config.gates.parallel_groups,
                )
            if self._verify_failure_looks_like_oracle_proof_state(f"{verify_gate.summary}\n{raw_output}"):
                tasks = state.tasks or self._load_tasks_from_plan()
                state.tasks = tasks
                audit_result = run_requirements_audit(
                    self.project_root, tasks, current_spec=self._active_spec_file
                )
                if not bool(audit_result["ok"]):
                    state.stage_summaries.pop("verify", None)
                    if self._handle_requirements_audit_failure(state, audit_result):
                        self._emit_stage_verify_result(
                            "fail",
                            f"requirements audit failed: {audit_result['path']}",
                            route=state.rejected_stage,
                        )
                        save_run_state(self.project_root, state)
                        return state
            if self._handle_verify_gate_failure(
                state,
                verify_gate,
                raw_output=raw_output,
                raw_log_path=raw_log_path,
            ):
                route = state.rejected_stage
                routed_summary = (
                    "full verification failed; routing to clarify for user guidance"
                    if route == "clarify"
                    else "full verification failed; routing to implement recovery"
                )
                self._emit_stage_verify_result(
                    "fail",
                    routed_summary,
                    route=route,
                )
                save_run_state(self.project_root, state)
                return state
            self._emit_stage_verify_result("fail", state.last_error or summary.strip())
            raise RuntimeError(state.last_error or "verify stage failed")
        if self.config.gates.commands:
            self._gate_baseline_cache.put(
                self._task_verify_baseline_ref(),
                self.config.gates.commands,
                collect_all=False,
                failure_ids=[],
                summary=verify_gate.summary,
                parallel_groups=self.config.gates.parallel_groups,
            )
        tasks = state.tasks or self._load_tasks_from_plan()
        state.tasks = tasks
        audit_result = run_requirements_audit(
            self.project_root, tasks, current_spec=self._active_spec_file
        )
        audit_ok = bool(audit_result["ok"])
        audit_report = str(audit_result["report"])
        if not audit_ok:
            state.stage_summaries.pop("verify", None)
            if self._handle_requirements_audit_failure(state, audit_result):
                self._emit_stage_verify_result(
                    "fail",
                    f"requirements audit failed: {audit_result['path']}",
                    route=state.rejected_stage,
                )
                save_run_state(self.project_root, state)
                return state
            self._emit_stage_verify_result("fail", state.last_error)
            raise RuntimeError(state.last_error)
        if "No requirements are currently tracked." not in audit_report:
            state.stage_summaries["requirements_audit"] = audit_report.strip()
        state.agent_attempts.pop("requirements_audit_recovery", None)
        state.agent_attempts.pop("verify_recovery", None)
        return state

    def _load_tasks_from_plan(self) -> List[TaskSpec]:
        payload = load_task_plan(self.project_root)
        tasks = [TaskSpec.from_dict(item) for item in payload.get("tasks", [])]
        if not tasks:
            raise RuntimeError(f"No tasks found in {task_plan_path(self.project_root)}")
        return tasks

    def _sync_allowed_repair_task_plan_edits(
        self,
        state: RunState,
        task: TaskSpec,
    ) -> None:
        if not self._is_repair_task(task):
            return
        plan_rel = self._relative_repo_path(task_plan_path(self.project_root))
        if plan_rel not in changed_paths(
            self.project_root,
            ignored_prefixes=(".antigravitycli/",),
        ):
            return
        try:
            loaded_tasks = self._load_tasks_from_plan()
        except Exception:
            return
        current_index = next(
            (
                index
                for index, candidate in enumerate(loaded_tasks)
                if candidate.task_id == task.task_id
            ),
            None,
        )
        if current_index is not None:
            self._copy_parallel_task_snapshot_fields(
                task,
                loaded_tasks[current_index].to_dict(),
            )
            loaded_tasks[current_index] = task
        if state.tasks:
            state.tasks[:] = loaded_tasks
        else:
            state.tasks = loaded_tasks

    def _run_readme(self, state: RunState, spec_file: Path) -> RunState:
        import json as _json
        from .config import run_path as _run_path

        history_file = _run_path(self.project_root, state.run_id) / "readme_conversation.json"
        history: List[Dict[str, str]] = []
        if history_file.exists():
            try:
                history = _json.loads(read_text(history_file))
            except Exception:
                pass

        # --- conversation loop: propose topics, collect feedback, repeat ---
        max_rounds = 10
        round_num = 0

        # First round (or resume): generate initial proposal if no history yet
        if not history:
            self.logger.info("Entering README preparation, please wait for the agent to analyze the project...")

            proposal_prompt = self._build_readme_proposal_prompt(spec_file, history)
            result = self._run_agent_with_retries(
                state=state,
                stage="readme",
                stage_key="readme-propose",
                prompt=proposal_prompt,
            )
            reply = (result.summary or result.stdout).strip()
            history.append({"role": "agent", "content": reply})
            write_text(history_file, _json.dumps(history, indent=2, ensure_ascii=False))
        elif history[-1].get("role") == "user":
            # Resuming after crash: user gave feedback but agent hasn't replied yet.
            # Fall through to the loop which will send a new proposal round.
            pass

        while round_num < max_rounds:
            round_num += 1
            # Show the latest agent message
            last_agent_msg = ""
            for msg in reversed(history):
                if msg.get("role") == "agent":
                    last_agent_msg = msg.get("content", "")
                    break

            self.logger.info("\nAgent:")
            self.logger.info(last_agent_msg)

            answer = self._prompt_user(
                "\nDo you have anything to add or modify? (y/n) [n]: ", default="n"
            ).strip().lower()
            if answer not in ("y", "yes"):
                break

            user_input = self._prompt_user("Please describe what to add or change: ", multiline=True).strip()
            if not user_input:
                break
            history.append({"role": "user", "content": user_input})
            write_text(history_file, _json.dumps(history, indent=2, ensure_ascii=False))

            self.logger.info("\nAgent is updating the plan, please wait...")
            proposal_prompt = self._build_readme_proposal_prompt(spec_file, history)
            result = self._run_agent_with_retries(
                state=state,
                stage="readme",
                stage_key=f"readme-propose-{round_num}",
                prompt=proposal_prompt,
            )
            reply = (result.summary or result.stdout).strip()
            history.append({"role": "agent", "content": reply})
            write_text(history_file, _json.dumps(history, indent=2, ensure_ascii=False))

        # Collect all user messages as extra instructions for generation
        user_extras = [msg["content"] for msg in history if msg.get("role") == "user"]

        # --- generation phase ---
        self.logger.info("\nGenerating README.md, please wait...")
        prompt = self._build_prompt(stage="readme", spec_file=spec_file)
        if user_extras:
            prompt += "\n\nAdditional user instructions for the README:\n" + "\n".join(user_extras)

        result = self._run_agent_with_retries(
            state=state,
            stage="readme",
            stage_key="readme",
            prompt=prompt,
            validation_feedback=self._readme_validation_feedback,
        )
        state.current_stage = "readme"
        state.stage_summaries["readme"] = result.summary.strip()
        state.last_error = ""
        save_run_state(self.project_root, state)
        self._commit_if_dirty("docs: update README")
        return state

    @staticmethod
    def _derive_plan_task_replacements(
        previous_tasks: Iterable[TaskSpec],
        current_tasks: Iterable[TaskSpec],
    ) -> Dict[str, List[str]]:
        previous_ids = {task.task_id for task in previous_tasks if task.task_id.strip()}
        current_list = [task for task in current_tasks if task.task_id.strip()]
        current_ids = {task.task_id for task in current_list}
        replacements: Dict[str, List[str]] = {}
        for task in current_list:
            parent_id = task.parent_task_id.strip()
            if not parent_id or parent_id not in previous_ids or parent_id in current_ids:
                continue
            replacements.setdefault(parent_id, []).append(task.task_id)
        return {
            retired_id: sorted(set(children))
            for retired_id, children in replacements.items()
        }

    @staticmethod
    def _looks_like_test_path(path: str) -> bool:
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized or normalized.startswith(".auto-agents/"):
            return False
        lower = normalized.lower()
        parts = [part for part in lower.split("/") if part]
        filename = parts[-1] if parts else lower
        if any(part in {"tests", "test", "__tests__"} for part in parts[:-1]):
            return True
        return (
            filename.startswith("test_")
            or filename.endswith("_test.py")
            or filename.endswith("_spec.py")
            or ".test." in filename
            or ".spec." in filename
        )

    def _repository_test_paths(self) -> List[str]:
        output = self._git_text("ls-files", "-co", "--exclude-standard")
        paths: List[str] = []
        for raw_line in output.splitlines():
            path = raw_line.strip().replace("\\", "/")
            if not self._looks_like_test_path(path):
                continue
            file_path = self.project_root / path
            if file_path.is_file():
                paths.append(path)
        return sorted(set(paths))

    def _task_plan_replacements_for_retired_id(self, state: RunState, retired_id: str) -> List[str]:
        replacements = [
            task.task_id
            for task in state.tasks
            if task.parent_task_id.strip() == retired_id and task.task_id.strip()
        ]
        mapped = state.plan_task_replacements.get(retired_id, [])
        return sorted(set([*mapped, *replacements]))

    def _task_retired_plan_ids(self, state: RunState, task: TaskSpec) -> List[str]:
        current_ids = {item.task_id for item in state.tasks if item.task_id.strip()}
        retired_ids: Set[str] = set()
        parent_id = task.parent_task_id.strip()
        if parent_id and parent_id not in current_ids:
            retired_ids.add(parent_id)
        for retired_id, replacements in state.plan_task_replacements.items():
            if task.task_id in replacements:
                retired_ids.add(retired_id)
        return sorted(retired_ids)

    def _text_references_task_id(self, content: str, task_id: str) -> bool:
        if not task_id.strip():
            return False
        pattern = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(task_id)}(?![A-Za-z0-9_-])")
        return any(
            not self._task_id_match_is_allowed_parent_reference(content, match)
            for match in pattern.finditer(content)
        )

    @staticmethod
    def _task_id_match_is_allowed_parent_reference(
        content: str,
        match: re.Match[str],
    ) -> bool:
        prefix = content[max(0, match.start() - 160): match.start()]
        allowed_prefix_patterns = (
            re.compile(r"(?:['\"]parent_task_id['\"]|parent_task_id)\s*:\s*['\"]?$"),
            re.compile(r"\[\s*['\"]parent_task_id['\"]\s*\]\s*,\s*['\"]?$"),
            re.compile(r"\[\s*['\"]parent_task_id['\"]\s*\]\s*(?:==|!=)\s*['\"]?$"),
            re.compile(r"\.parent_task_id\s*,\s*['\"]?$"),
            re.compile(r"\.parent_task_id\s*(?:==|!=)\s*['\"]?$"),
        )
        return any(pattern.search(prefix) for pattern in allowed_prefix_patterns)

    @staticmethod
    def _ast_string_literal(node: Optional[ast.AST]) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return ""

    @classmethod
    def _dict_string_literal_items(cls, node: ast.AST) -> Dict[str, str]:
        if not isinstance(node, ast.Dict):
            return {}
        items: Dict[str, str] = {}
        for key, value in zip(node.keys, node.values):
            rendered_key = cls._ast_string_literal(key)
            rendered_value = cls._ast_string_literal(value)
            if rendered_key and rendered_value:
                items[rendered_key] = rendered_value
        return items

    @classmethod
    def _python_test_task_status_literals(cls, content: str, task_id: str) -> List[str]:
        if not task_id.strip():
            return []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        statuses: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            literal_items = cls._dict_string_literal_items(node)
            direct_task_id = literal_items.get("task_id", "")
            direct_status = literal_items.get("status", "")
            if direct_task_id == task_id and direct_status:
                statuses.append(direct_status)
            for key, value in zip(node.keys, node.values):
                if cls._ast_string_literal(key) != task_id:
                    continue
                nested_status = cls._dict_string_literal_items(value).get("status", "")
                if nested_status:
                    statuses.append(nested_status)
        return sorted(set(statuses))

    @classmethod
    def _text_task_status_literals(cls, content: str, task_id: str) -> List[str]:
        if not task_id.strip():
            return []
        statuses: List[str] = []
        patterns = (
            re.compile(
                rf"['\"]{re.escape(task_id)}['\"]\s*:\s*\{{[\s\S]{{0,600}}?['\"]status['\"]\s*:\s*['\"](pending|in_progress|blocked|done)['\"]",
                re.MULTILINE,
            ),
            re.compile(
                rf"['\"]task_id['\"]\s*:\s*['\"]{re.escape(task_id)}['\"][\s\S]{{0,300}}?['\"]status['\"]\s*:\s*['\"](pending|in_progress|blocked|done)['\"]",
                re.MULTILINE,
            ),
        )
        for pattern in patterns:
            statuses.extend(match.group(1) for match in pattern.finditer(content))
        return sorted(set(statuses))

    @classmethod
    def _test_file_task_status_literals(cls, relative_path: str, content: str, task_id: str) -> List[str]:
        if relative_path.endswith(".py"):
            statuses = cls._python_test_task_status_literals(content, task_id)
            if statuses:
                return statuses
        return cls._text_task_status_literals(content, task_id)

    def _build_task_plan_migration_context(self, state: Optional[RunState], task: TaskSpec) -> str:
        active_state = state or load_run_state(self.project_root)
        retired_ids = self._task_retired_plan_ids(active_state, task)
        if not retired_ids:
            return ""
        lines = [
            "PLAN MIGRATION CONTEXT:",
            "This task is responsible for keeping plan-coupled repository tests aligned with the current task plan.",
        ]
        for retired_id in retired_ids:
            replacements = self._task_plan_replacements_for_retired_id(active_state, retired_id)
            replacement_text = ", ".join(replacements) if replacements else task.task_id
            lines.append(
                f"- Retired task ID `{retired_id}` was replaced by: {replacement_text}."
            )
        lines.append(
            "If repository tests still reference the retired task IDs or rely on the pre-split plan shape, update those tests in this task."
        )
        return "\n".join(lines)

    def _build_task_status_migration_context(
        self,
        task: TaskSpec,
        *,
        expected_status: str = "done",
    ) -> str:
        if not task.task_id.strip() or not expected_status.strip():
            return ""

        lines = [
            "TASK STATUS MIGRATION CONTEXT:",
            (
                f"If this task succeeds, the orchestrator will persist task `{task.task_id}` "
                f"with status `{expected_status}`."
            ),
            (
                "If repository tests still assert an older status for this task, update them in the same task."
            ),
            "Internal .auto-agents state files are orchestrator-owned run snapshots, not implementation targets.",
        ]

        findings: List[str] = []
        for relative_path in self._repository_test_paths():
            content = read_text(self.project_root / relative_path)
            if not content.strip() or not self._text_references_task_id(content, task.task_id):
                continue
            statuses = self._test_file_task_status_literals(relative_path, content, task.task_id)
            stale_statuses = [status for status in statuses if status != expected_status]
            if not stale_statuses:
                continue
            findings.append(
                f"- {relative_path}: currently asserts `{task.task_id}` as {', '.join(sorted(set(stale_statuses)))}."
            )

        if findings:
            lines.append("Repository tests currently look stale for this status transition:")
            lines.extend(findings[:8])
            if len(findings) > 8:
                lines.append(f"- ... {len(findings) - 8} more stale task-status assertion(s)")
        return "\n".join(lines)

    def _run_stale_plan_coupled_test_audit(
        self,
        task: Optional[TaskSpec],
        *,
        state: Optional[RunState] = None,
    ) -> Optional[Dict[str, object]]:
        if task is None:
            return None
        active_state = state or load_run_state(self.project_root)
        retired_ids = self._task_retired_plan_ids(active_state, task)
        if not retired_ids:
            return None

        findings: List[Dict[str, object]] = []
        for relative_path in self._repository_test_paths():
            content = read_text(self.project_root / relative_path)
            if not content.strip():
                continue
            for retired_id in retired_ids:
                if not self._text_references_task_id(content, retired_id):
                    continue
                findings.append(
                    {
                        "path": relative_path,
                        "retired_id": retired_id,
                        "replacement_ids": self._task_plan_replacements_for_retired_id(active_state, retired_id),
                    }
                )

        if not findings:
            return None

        lines = [
            "Stale plan-coupled tests still reference retired task IDs for this task's re-plan scope.",
            "Update the repository tests so they match the current task plan before continuing.",
        ]
        failure_ids: List[str] = []
        seen_failures: Set[str] = set()
        for item in findings[:12]:
            retired_id = str(item["retired_id"])
            path = str(item["path"])
            replacement_ids = [str(value) for value in item.get("replacement_ids", []) if str(value).strip()]
            replacement_text = ", ".join(replacement_ids) if replacement_ids else task.task_id
            lines.append(
                f"- {path}: replace retired task ID `{retired_id}` with current task-plan references ({replacement_text})."
            )
            failure_id = f"stale-plan-coupled-test:{path}:{retired_id}"
            if failure_id not in seen_failures:
                failure_ids.append(failure_id)
                seen_failures.add(failure_id)
        if len(findings) > 12:
            lines.append(f"- ... {len(findings) - 12} more stale test reference(s)")

        reason = "\n".join(lines)
        return {
            "reason": reason,
            "failure_ids": failure_ids,
            "raw_output": reason,
        }

    def _run_task_status_coupled_test_audit(
        self,
        task: Optional[TaskSpec],
        *,
        expected_status: str,
    ) -> Optional[Dict[str, object]]:
        if task is None or not task.task_id.strip() or not expected_status.strip():
            return None

        findings: List[Dict[str, object]] = []
        for relative_path in self._repository_test_paths():
            content = read_text(self.project_root / relative_path)
            if not content.strip() or not self._text_references_task_id(content, task.task_id):
                continue
            statuses = self._test_file_task_status_literals(relative_path, content, task.task_id)
            stale_statuses = [status for status in statuses if status != expected_status]
            if not stale_statuses:
                continue
            findings.append({"path": relative_path, "statuses": stale_statuses})

        if not findings:
            return None

        lines = [
            f"Plan-coupled repository tests still expect task `{task.task_id}` to have a stale status.",
            f"A successful implementation attempt for this task must leave it at status `{expected_status}`.",
            "Update the repository tests so they match the current task plan before continuing.",
        ]
        failure_ids: List[str] = []
        seen_failures: Set[str] = set()
        for item in findings[:12]:
            path = str(item["path"])
            stale_statuses = [str(value) for value in item.get("statuses", []) if str(value).strip()]
            status_text = ", ".join(sorted(set(stale_statuses)))
            lines.append(
                f"- {path}: task `{task.task_id}` should now be asserted as `{expected_status}`, not `{status_text}`."
            )
            for stale_status in stale_statuses:
                failure_id = f"stale-task-status-test:{path}:{task.task_id}:{stale_status}"
                if failure_id in seen_failures:
                    continue
                failure_ids.append(failure_id)
                seen_failures.add(failure_id)
        if len(findings) > 12:
            lines.append(f"- ... {len(findings) - 12} more stale task-status assertion(s)")

        reason = "\n".join(lines)
        return {
            "reason": reason,
            "failure_ids": failure_ids,
            "raw_output": reason,
        }

    def _build_readme_proposal_prompt(self, spec_file: Path, history: List[Dict[str, str]] = None) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        plan = task_plan_path(self.project_root)
        lang_instruction = self._readme_language_instruction()
        lines = [
            f"Project root: {self.project_root}",
            f"Read the input spec: {spec_file}",
            f"Read the project brief: {brief}",
            f"Read the architecture doc: {architecture}",
            f"Read the task plan: {plan}",
            "You are about to write a README for this project.",
            "This proposal round is read-only. Do not modify README.md or any other repository file yet.",
            "List the topics / sections you plan to include in the README, with a short description of each.",
            "Do NOT write the README yet. Only outline the planned sections.",
            lang_instruction,
        ]
        if history:
            lines.append("\n--- Conversation History ---")
            for msg in history:
                role = msg.get("role", "user").upper()
                content = msg.get("content", "")
                lines.append(f"\n[{role}]:\n{content}")
            lines.append("\nBased on the conversation above, present the UPDATED list of planned README sections.")
        return "\n".join(lines)

    def _build_provider_research_prompt(self, requirements: List[dict]) -> str:
        trace_path = requirements_trace_path(self.project_root)
        lock_path = provider_references_lock_path(self.project_root)
        references_dir = provider_references_dir(self.project_root)
        req_context = format_requirement_context(requirements)
        lines = [
            f"Project root: {self.project_root}",
            f"Requirements trace: {trace_path}",
            f"Provider reference directory: {references_dir}",
            f"Provider reference lock: {lock_path}",
            "This stage researches external provider protocols once, before implementation tasks run.",
            "Use available browsing or network tools when local notes are insufficient, but restrict sources to official provider documentation.",
            "Only use official provider documentation or user-provided protocol notes already in the repository.",
            "Do not use blogs, forum answers, random SDK examples, or unofficial mirrors as source of truth.",
            "Do not implement product code in this stage.",
            "Only modify provider reference markdown files under .auto-agents/docs/provider_references/ and .auto-agents/state/provider_references.lock.json.",
            "Do not modify project code, tests, README.md, or task-planning artifacts in this stage.",
            "For each requirement below, create or update the provider_reference markdown file named in the trace.",
            "Each reference must include: Status, Retrieved at, Official sources, Authentication, Request, Response, Errors, Contract Test Requirements, Unknowns / Ambiguities.",
            "If official docs are unavailable or ambiguous, write a blocked/needs_user_input reference with the exact missing information and recovery options.",
            "Update provider_references.lock.json with one entry per provider reference. Each entry must include path, status, retrieved_at, source_urls, and notes.",
            "Allowed lock statuses: verified, blocked, needs_user_input, ambiguous, deferred, temporary_stub, assumption_approved.",
            "Final response: 3 short bullets summarizing references created or blockers found.",
            "",
            req_context,
        ]
        return "\n".join(lines)

    def _persist_tasks(self, tasks: Iterable[TaskSpec]) -> None:
        current_payload = load_task_plan(self.project_root)
        payload = []
        for task in tasks:
            item = task.to_dict()
            item.pop("commit_sha", None)
            payload.append(item)
        next_payload = {"tasks": payload}
        if isinstance(current_payload.get("oracle_proof_schema_version"), int):
            next_payload["oracle_proof_schema_version"] = current_payload["oracle_proof_schema_version"]
        if isinstance(current_payload.get("test_strategy"), str) and current_payload["test_strategy"].strip():
            next_payload["test_strategy"] = current_payload["test_strategy"].strip()
        verification_steps = current_payload.get("verification_steps")
        if isinstance(verification_steps, list) and verification_steps:
            next_payload["verification_steps"] = [
                item for item in verification_steps if isinstance(item, dict)
            ]
        verification_commands = current_payload.get("verification_commands")
        if isinstance(verification_commands, list) and verification_commands:
            next_payload["verification_commands"] = [
                str(item).strip() for item in verification_commands if str(item).strip()
            ]
        save_task_plan(self.project_root, next_payload)

    @staticmethod
    def _done_task_payloads(tasks: Iterable[TaskSpec]) -> List[dict]:
        payloads: List[dict] = []
        seen_task_ids: Set[str] = set()
        for task in tasks:
            if task.status != "done":
                continue
            item = task.to_dict()
            item.pop("commit_sha", None)
            task_id = str(item.get("task_id", "")).strip()
            if task_id and task_id in seen_task_ids:
                continue
            if task_id:
                seen_task_ids.add(task_id)
            payloads.append(item)
        return payloads

    def _merge_prior_done_tasks_into_generated_plan(self, prior_tasks: Iterable[TaskSpec]) -> None:
        prior_done = self._done_task_payloads(prior_tasks)
        if not prior_done:
            return

        payload = load_task_plan(self.project_root)
        raw_tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(raw_tasks, list):
            return

        trace = load_requirements_trace(self.project_root)
        prior_proofs = verified_proofs_by_requirement_from_task_payloads(prior_done, trace)
        prior_by_id = {
            str(task.get("task_id", "")).strip(): task
            for task in prior_done
            if str(task.get("task_id", "")).strip()
        }

        retained: List[dict] = []
        dropped_task_ids: List[str] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                retained.append(item)
                continue
            task_id = str(item.get("task_id", "")).strip()
            if task_id and task_id in prior_by_id:
                continue
            if str(item.get("status", "")).strip() != "done":
                try:
                    task = TaskSpec.from_dict(item)
                except (KeyError, TypeError, ValueError):
                    task = None
                if task is not None and task_is_fully_historically_covered(task, trace, prior_proofs):
                    if task_id:
                        dropped_task_ids.append(task_id)
                    continue
            retained.append(item)

        next_tasks = list(prior_by_id.values()) + retained
        if next_tasks == raw_tasks:
            return

        next_payload = dict(payload)
        next_payload["tasks"] = next_tasks
        save_task_plan(self.project_root, next_payload)
        if dropped_task_ids:
            self.logger.info(
                "[plan] pruned current-run duplicate tasks already covered by done proof: "
                + ", ".join(dropped_task_ids)
            )

    def _stage_output_path(self, run_id: str, stage: str) -> Path:
        _, output_path = run_artifact_paths(self.project_root, run_id, stage)
        return output_path

    def _build_prompt(self, stage: str, spec_file: Path, is_iteration: bool = False) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        plan = task_plan_path(self.project_root)
        requirements_trace = requirements_trace_path(self.project_root)
        archived_plan = self._previous_task_plan_archive_for_prompt()
        analysis = self._analyze_spec(spec_file)
        spec_kind = str(analysis["kind"])
        spec_context = self._spec_context_line(analysis)
        common = [
            f"Project root: {self.project_root}",
            "Work only inside this repository.",
            "Keep outputs concise and file-driven.",
            "Do not restate large documents in your final response.",
            "Do not modify the system-wide environment or install global packages.",
            f"Primary input spec: {spec_file}",
            spec_context,
        ]

        if stage == "clarify":
            lines = common + [
                f"Read the input spec from: {spec_file}",
                f"Update this file in place: {brief}",
                f"Write the requirements trace at: {requirements_trace}",
                "Keep the brief compact and focused on the target scope.",
                "Only update project_brief.md and requirements_trace.json in this stage; do not modify project code, tests, or other repository documents.",
                "Preserve the exact top-level and section headings already present in the file.",
                "The requirements trace is the downstream execution contract. It must be valid JSON with version=1 and a requirements list.",
                "Every active requirement must have id, text, source, status, priority, acceptance_oracles, oracle_type, oracle_strength, evidence_boundary, forbidden_proxy_oracles, forbidden_patterns, external_docs_required, provider_reference, and notes fields.",
                "Use stable IDs like REQ-001. Mark hard requirements as priority='mandatory'. Use status='active', 'deferred', or 'superseded'.",
                "If a requirement needs an external provider protocol or official API docs, set external_docs_required=true and provider_reference to a local path under .auto-agents/docs/provider_references/.",
                "Use oracle_type to name the primary proof mechanism (for example deterministic_test, integration_test, runtime_evidence, judge_model, benchmark, human_review, or mixed). Use oracle_strength to record the minimum acceptable fidelity (proxy, behavioral, semantic, or human). Use evidence_boundary to say where proof must come from (internal_state, system_boundary, or external_side_effect). Record any checks that must NOT be treated as sufficient in forbidden_proxy_oracles.",
                "For requirements that remove, forbid, or replace old behavior, add precise forbidden_patterns regexes for stale terms or old semantic claims so requirements audit can scan code, tests, and docs. Prefer narrow patterns that catch positive stale claims without matching the new negative requirement text.",
                self._clarify_spec_instruction(spec_kind),
                self._document_language_instruction(),
            ]
            if not is_iteration:
                lines.append(
                    "This is the FIRST clarify run; the requirements trace is empty. "
                    "Populate it with the requirements derived from the input spec."
                )
            if is_iteration:
                historical_plan_line = (
                    f"Review the archived completed task plan at {archived_plan} to understand what has already been completed."
                    if archived_plan
                    else f"Review the existing task plan at {task_plan_path(self.project_root)} to understand what has already been completed."
                )
                lines.extend([
                    f"This is an ITERATION run. The project already has completed work and an existing brief at {brief}.",
                    "IMPORTANT: Do NOT discard or rewrite the existing content of the brief.",
                    "ADD or UPDATE sections relevant to the new iteration scope while preserving existing content.",
                    "Extend existing sections in place rather than appending a separate duplicate block at the end.",
                    historical_plan_line,
                    "The existing requirements_trace.json is a CUMULATIVE contract across iterations; downstream task plans reference REQ IDs by value.",
                    "Do NOT delete existing REQ entries and do NOT renumber or reuse REQ IDs from the existing trace.",
                    "Mark requirements that are no longer in scope as status='superseded' (preserve id/text/source/acceptance_oracles) instead of removing them.",
                    "For new iteration scope, append entries with new IDs that continue the existing numbering (e.g., if the highest existing ID is REQ-029, the next new one is REQ-030).",
                    "Only add a brand-new requirement when it cannot be expressed as an update to an existing active or deferred entry.",
                ])
            lines.append("Final response: 3 short bullets summarizing the clarified scope.")
            return "\n".join(lines)

        if stage == "design":
            lines = common + [
                f"Read the input spec: {spec_file}",
                f"Read the current project brief: {brief}",
                f"Read the requirements trace: {requirements_trace}",
                f"Update this file in place: {architecture}",
                "Record only top-level architecture decisions and major risks.",
                "Only update architecture.md in this stage. Do not modify project code, tests, README.md, or .auto-agents state files.",
                "Preserve the exact top-level and section headings already present in the file.",
                "Architecture.md must not contradict any active mandatory requirement, acceptance oracle, forbidden proxy oracle, or forbidden_patterns entry in requirements_trace.json.",
                "For each new or updated active mandatory requirement that changes architecture semantics, remove or rewrite stale architecture text and document the current contract.",
                self._design_spec_instruction(spec_kind),
                self._document_language_instruction(),
            ]
            if is_iteration:
                historical_plan_line = (
                    f"Review the archived completed task plan at {archived_plan} to understand what has already been completed."
                    if archived_plan
                    else f"Review the existing task plan at {task_plan_path(self.project_root)} to understand what has already been completed."
                )
                lines.extend([
                    f"This is an ITERATION run. The project already has completed work and an existing architecture at {architecture}.",
                    "IMPORTANT: Do NOT discard or rewrite the existing architecture decisions.",
                    "ADD or UPDATE sections relevant to the new iteration scope while preserving existing content.",
                    historical_plan_line,
                    "Compare the brief's current iteration requirements against the existing architecture content.",
                    "If the architecture describes a capability as already implemented but the brief's iteration scope explicitly asks for it as new or upgraded scope, ADD a subsection or bullet under the relevant heading that describes the GAP between what exists and what the new iteration requires.",
                    "Do NOT assume that existing architecture descriptions are accurate for the new iteration — the brief's iteration scope takes precedence over existing architecture claims about what is already real or complete.",
                ])
            lines.append("Final response: 3 short bullets summarizing the design.")
            return "\n".join(lines)

        if stage == "plan":
            lines = common + [
                f"Read the input spec: {spec_file}",
                f"Read: {brief}",
                f"Read: {architecture}",
                f"Read the requirements trace: {requirements_trace}",
                f"Replace this JSON file with a task plan of minimal verifiable feature slices: {plan}",
                f"Only update {plan} in this stage.",
                "Do not modify project code, tests, README.md, input specs under specs/, "
                ".auto-agents/docs/requirements_audit.md, .auto-agents/docs/review.md, "
                "project_brief.md, architecture.md, requirements_trace.json, or any other "
                "repository files to make the plan pass.",
                "At the root of the JSON, also define test_strategy and verification_steps.",
                "At the root of the JSON, set oracle_proof_schema_version to 1 for all new plans.",
                "Every new non-done task must include requirement_ids listing the requirements it covers.",
                "Every task that covers requirement_ids must include requirement_proofs. Each proof must include requirement_id, oracle_index (1-based) or exact acceptance_oracle, proof_type, oracle_strength, evidence_boundary, evidence_refs, status='planned', and forbidden_proxy_oracles copied from the bound requirement.",
                "All active mandatory requirements in requirements_trace.json must be covered by either archived verified done-task proof or at least one current task requirement_ids entry unless the requirement is explicitly deferred or superseded.",
                "All active mandatory requirement acceptance_oracles must also be covered by either archived verified done-task proof or at least one current task requirement_proofs entry; requirement_ids alone are not sufficient coverage.",
                "If an acceptance_oracle covers docs or architecture semantics, its evidence_refs must include an executable test that reads/asserts those docs and a supporting ref to the affected document, such as .auto-agents/docs/architecture.md.",
                "Task acceptance criteria must preserve the bound requirement's concrete acceptance_oracles; do not weaken direct/API/protocol requirements into naming or configuration-only checks.",
                "For negative contract requirements such as 'must not contain', '不得', '不包含', or '不返回', preserve every concrete field/path/API token from the requirement in the task acceptance. For example, a requirement that forbids `tasks[].result` is NOT covered by only omitting `retry_trace`.",
                "Preserve each bound requirement's oracle_type, oracle_strength, evidence_boundary, and forbidden_proxy_oracles when slicing tasks. Requirements that demand semantic or human-strength proof are NOT satisfied by proxy checks, internal-state-only checks, config-only checks, or metadata/log snapshots. Requirements that demand system_boundary or external_side_effect evidence are NOT covered unless the task acceptance requires proof at that boundary.",
                "If a requirement has external_docs_required=true, create at least one implementation task that consumes its provider_reference and tests against that protocol reference.",
                "Choose the smallest practical automated verification strategy for this stack.",
                "If this is a Python project, require a project-local conda env at ./.conda.",
                "If tests or runtime helpers need mutable local artifacts (for example sqlite DBs, temp configs, fixtures, caches, or downloaded samples), place them under ignored temp/data paths such as ./.tmp/, ./.tmp-tests/, or ./.data/ rather than tracked repo-root files.",
                "Choose the number of tasks based on project complexity rather than an arbitrary cap.",
                "Keep each task small enough to implement, review, and verify independently, but do not split into trivial housekeeping-only tasks.",
                "Avoid oversized tasks that bundle multiple loosely related features together.",
                "Prefer tasks that each deliver one coherent, testable capability or technical slice.",
                "For Python verification, use verification_steps entries with kind='test' and runner='pytest'; do not use unittest as the planned runner. Prefer one target per test file when test files already exist; auto_agents may expand directory targets such as ['tests'] into per-file pytest steps before running gates.",
                "For JavaScript/TypeScript verification, use verification_steps entries with kind='test', runner='vitest'.",
                "Do not generate free-form shell verification commands for test steps; auto_agents derives the runnable command from verification_steps.",
                "For non-Python projects, keep all dependency installation and tooling local to the repository and avoid global installs.",
                self._plan_spec_instruction(spec_kind),
                self._plan_language_instruction(),
                "If future implementation will require test updates, encode that need in task scope, acceptance, and expected_test_migrations. Do NOT pre-edit repository tests in this planning stage.",
                "CRITICAL — COVERAGE VERIFICATION: when determining whether a done task covers a brief requirement, you MUST compare the requirement against the task's ACCEPTANCE CRITERIA and REVIEW SUMMARY, not its title or description alone. A task titled 'Real X Integration' does NOT cover a requirement for actual real-model output if its acceptance criteria only verify adapter switching, infrastructure patterns, or fixture/stub results rather than actual external API calls producing real output.",
                "If the brief explicitly states that a capability must be 'real' / 'production' / '真实' / '公网', verify that the done task's acceptance criteria confirm actual external API calls producing real output — not just adapter infrastructure or fixture-based testing.",
                "Before generating the task list, produce a COVERAGE ANALYSIS in your final summary response (NOT in the JSON file): for each key requirement in the brief's current iteration scope, state which done task covers it (citing the specific acceptance criterion that proves delivery) or mark it as UNCOVERED. Any UNCOVERED requirement MUST result in a new task.",
                "Each task must contain task_id, title, description, acceptance, status, commit_message.",
                "A good plan may contain only a few tasks for a small target or many tasks for a broad target, as long as the slicing remains disciplined.",
                "",
                "TASK SPLITTING — ANTI-PATTERNS (avoid these):",
                "1. God Task: a single active task with >5 acceptance criteria or a description exceeding ~500 characters. Split by feature slice.",
                "2. Cross-cutting Bundle: acceptance criteria that span multiple unrelated subsystems (e.g. 'set up DB schema AND implement API AND write CLI'). Each subsystem should be its own task.",
                "3. Infra + Feature Combo: mixing infrastructure setup (dependencies, CI, env config) with business logic in one task. Split infra into a prerequisite task.",
                "4. Vague Acceptance: criteria like 'code is clean' or 'follows best practices'. Every criterion must be objectively verifiable by a test or a command.",
                "5. False Coverage: concluding a done task covers a new requirement based on its title, while its acceptance criteria only verify infrastructure, adapters, or fixture results — not the actual capability the brief demands. Always verify coverage by reading acceptance criteria, not titles. Especially dangerous when the brief uses terms like 'real' / 'production' / '真实' / '公网' — these signal that adapter-level or fixture-level delivery is insufficient.",
                "",
                "TASK SPLITTING — STRATEGIES:",
                "1. Vertical Slice: each task delivers one user-facing or API-facing capability end-to-end (route, logic, test).",
                "2. Dependency-first: extract shared setup (DB schema, env, config) into an early task, then layer feature tasks on top.",
                "3. Test-boundary: if a single task would require tests in 3+ unrelated test files, it likely needs splitting.",
                "4. Acceptance count rule: aim for 2-4 acceptance criteria per active task. If a task has 6-7 criteria, add scope_boundaries explaining why it remains one coherent slice. Tasks with more than 7 criteria are invalid and must be split.",
                "",
                "Final response: 3 short bullets summarizing the plan.",
            ]
            if is_iteration:
                if archived_plan:
                    lines.extend([
                        f"Review the archived completed task plan at: {archived_plan}",
                        f"Also review the current active task plan at: {task_plan_path(self.project_root)}",
                        "Use the archived plan only as read-only history for coverage analysis; archived done tasks with verified requirement_proofs already count as historical coverage.",
                        "Do NOT copy archived done tasks back into the active task_plan.json.",
                        "If the current active task_plan.json already contains done tasks from this run, preserve those done tasks and do NOT generate replacement tasks for requirement_proofs they already verified.",
                        "The active task_plan.json for this iteration must contain only tasks for the current iteration scope.",
                        "When archived completed tasks are present, cross-reference the brief and architecture against those done tasks to identify ONLY the scope not yet covered. Do NOT create regression-lock or baseline-preservation tasks solely for capabilities already delivered and verified by archived completed tasks.",
                    ])
                else:
                    lines.extend([
                        "Review .auto-agents/state/task_plan.json if it exists. DO NOT overwrite or delete existing completed tasks. APPEND new tasks to the end of the JSON array for the new features.",
                        "When existing completed tasks are present, cross-reference the brief and architecture against those done tasks to identify ONLY the scope not yet covered. Do NOT create tasks for capabilities already delivered by completed tasks.",
                    ])
            if self.config.execution.parallel_tasks.enabled:
                lines[lines.index("Each task must contain task_id, title, description, acceptance, status, commit_message.")] = (
                    "Each task must contain task_id, title, description, acceptance, status, commit_message, depends_on."
                )
                lines.insert(
                    lines.index("A good plan may contain only a few tasks for a small target or many tasks for a broad target, as long as the slicing remains disciplined."),
                    "Experimental parallel task execution is enabled for this project. The planner—not the user—must generate depends_on for every non-done task. Use [] when a task has no prerequisites, and only reference existing task_id values that are true prerequisites.",
                )
            return "\n".join(lines)

        if stage == "readme":
            readme = self.project_root / "README.md"
            lines = common + [
                f"Read the input spec: {spec_file}",
                f"Read the project brief: {brief}",
                f"Read the architecture doc: {architecture}",
                f"Read the task plan and verification strategy: {plan}",
                f"Update this file in place: {readme}",
                "Write a practical README for the finished project, not for auto_agents itself.",
                "Only update README.md in this final README generation step. Do not modify project code, tests, or .auto-agents planning/state files.",
                "The README MUST include ALL of the following sections (in any order, using appropriate headings):",
                "  1. Project overview / introduction",
                "  2. Currently implemented features (list what has actually been built so far)",
                "  3. Installation / prerequisites",
                "  4. Configuration",
                "  5. Usage",
                "  6. Architecture",
                "Base commands on files and entrypoints that actually exist in the repository.",
                "Prefer concise sections, bullets, and runnable command examples.",
                self._readme_spec_instruction(spec_kind),
                self._readme_language_instruction(),
                "Final response: 3 short bullets summarizing the README update.",
            ]
            return "\n".join(lines)

        raise RuntimeError(f"Unsupported stage: {stage}")

    def _analyze_spec(self, spec_file: Path) -> Dict[str, object]:
        content = read_text(spec_file).strip()
        lowered = content.lower()
        normalized = re.sub(r"[^a-z0-9]+", " ", lowered)

        def has_any(*patterns: str) -> bool:
            return any(pattern in lowered for pattern in patterns)

        def has_any_regex(*patterns: str) -> bool:
            return any(re.search(pattern, lowered) is not None for pattern in patterns)

        def count_matches(patterns: Iterable[str]) -> int:
            return sum(1 for pattern in patterns if pattern in normalized)

        has_problem = has_any("## problem", "# problem", "problem statement", "target audience", "user pain", "pain point")
        has_scope = has_any("## mvp scope", "# mvp scope", "mvp", "scope", "requirements", "feature list", "use case")
        has_constraints = has_any("## constraints", "# constraints", "constraints", "assumptions", "budget", "timeline", "compliance")
        has_modules = has_any("## core modules", "# core modules", "architecture", "module", "component", "service", "layer")
        has_data_flow = has_any("## data flow", "# data flow", "data flow", "sequence", "workflow", "request flow")
        has_interfaces = has_any("## interfaces", "# interfaces") or has_any_regex(
            r"\bapi\b",
            r"\binterface\b",
            r"\bendpoint\b",
            r"\bschema\b",
            r"\bcontract\b",
        )
        has_verification = has_any("test strategy", "verification", "acceptance criteria", "qa", "validation", "test plan")

        idea_score = count_matches(
            (
                "problem",
                "audience",
                "user",
                "mvp",
                "scope",
                "non goals",
                "goal",
                "use case",
            )
        )
        design_score = count_matches(
            (
                "architecture",
                "system boundary",
                "module",
                "component",
                "data flow",
                "interface",
                "api",
                "database",
                "schema",
                "deployment",
                "risk",
                "verification",
                "test strategy",
            )
        )

        if design_score >= 4 and (has_modules or has_data_flow or has_interfaces):
            kind = "design"
        elif idea_score >= 3 and design_score <= 2 and not (has_data_flow or has_interfaces):
            kind = "idea"
        else:
            kind = "mixed"

        evidence = []
        if has_problem:
            evidence.append("problem")
        if has_scope:
            evidence.append("scope")
        if has_constraints:
            evidence.append("constraints")
        if has_modules:
            evidence.append("modules")
        if has_data_flow:
            evidence.append("data flow")
        if has_interfaces:
            evidence.append("interfaces")
        if has_verification:
            evidence.append("verification")
        if not evidence:
            evidence.append("general requirements")

        return {
            "kind": kind,
            "idea_score": idea_score,
            "design_score": design_score,
            "has_problem": has_problem,
            "has_scope": has_scope,
            "has_constraints": has_constraints,
            "has_modules": has_modules,
            "has_data_flow": has_data_flow,
            "has_interfaces": has_interfaces,
            "has_verification": has_verification,
            "evidence": evidence,
        }

    @staticmethod
    def _spec_context_line(analysis: Dict[str, object]) -> str:
        evidence = ", ".join(str(item) for item in analysis.get("evidence", [])[:4])
        return (
            f"Detected spec profile: {analysis.get('kind', 'mixed')} "
            f"(signals: {evidence})."
        )

    @staticmethod
    def _clarify_spec_instruction(spec_kind: str) -> str:
        if spec_kind == "design":
            return (
                "This spec is architecture-heavy. Extract only product intent, target scope, non-goals, and "
                "constraints into the brief. Do not duplicate full architecture details here."
            )
        if spec_kind == "mixed":
            return (
                "This spec mixes product intent and design detail. Preserve the core requirements in the brief "
                "and leave implementation structure for the architecture document."
            )
        return "Treat the spec as early product intent and turn it into a crisp project brief."

    @staticmethod
    def _design_spec_instruction(spec_kind: str) -> str:
        if spec_kind == "design":
            return (
                "Treat the input spec as the primary architecture source. Normalize it into this template, "
                "preserve concrete decisions, and only fill small gaps conservatively."
            )
        if spec_kind == "mixed":
            return (
                "Preserve explicit architectural decisions from the input spec and use the brief only to fill "
                "missing context or constraints."
            )
        return "Use the brief as the source of truth and derive a practical architecture from it."

    @staticmethod
    def _plan_spec_instruction(spec_kind: str) -> str:
        if spec_kind == "design":
            return "Honor the architecture decisions already present in the input spec unless they clearly conflict with the brief."
        if spec_kind == "mixed":
            return "Prefer the explicit design decisions in the input spec and use the brief and architecture doc to resolve gaps."
        return "Decompose the target scope into the smallest practical feature slices implied by the brief and architecture."

    @staticmethod
    def _readme_spec_instruction(spec_kind: str) -> str:
        if spec_kind == "design":
            return "Use the input spec to preserve important architecture terminology and constraints in the final README."
        if spec_kind == "mixed":
            return "Use the input spec to preserve both the intended product scope and the key architecture choices."
        return "Use the input spec mainly as product context and describe the implemented repository state faithfully."

    def _get_repo_map_builder(self) -> Optional[RepoMapBuilder]:
        """Lazy-construct the repo map builder, honoring the enabled flag.

        Returns None if disabled so callers can skip the work entirely and
        keep prompts byte-identical to the pre-RepoMap behavior.
        """
        config = getattr(self.config, "repo_map", None)
        if config is None or not config.enabled:
            return None
        if self._repo_map_builder is None:
            self._repo_map_builder = RepoMapBuilder(self.project_root, config)
        return self._repo_map_builder

    def _build_repo_map_section(
        self,
        task: "TaskSpec",
        *,
        stage: str,
        extra_anchor_paths: Iterable[str] = (),
    ) -> str:
        """Return the repo map text to append to a task prompt, or "" if disabled/empty."""
        builder = self._get_repo_map_builder()
        if builder is None:
            self._last_repo_map_result = None
            return ""
        config = self.config.repo_map
        budget = config.review_budget_tokens if stage in ("review", "fix") else config.budget_tokens
        with log_timing(self.logger, f"repo-map:{stage}"):
            result = builder.build(
                task,
                budget_tokens=budget,
                extra_anchor_paths=list(extra_anchor_paths),
            )
        self._last_repo_map_result = result
        return result.text or ""

    def _build_task_prompt(
        self,
        task: TaskSpec,
        stage: str,
        review_context: str = "",
        plan_migration_context: str = "",
    ) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        task_json = json.dumps(task.to_dict(), indent=2, ensure_ascii=False)
        requirement_context = format_requirement_context(requirements_for_task(self.project_root, task))
        task_status_migration_context = self._build_task_status_migration_context(task)
        common = [
            f"Project root: {self.project_root}",
            f"Project brief: {brief}",
            f"Architecture: {architecture}",
            f"Requirements trace: {requirements_trace_path(self.project_root)}",
            "Work only on the current task.",
            "Keep changes scoped and testable.",
            f"Task JSON:\n{task_json}",
            "If Task JSON includes requirement_proofs, those entries define the current task's "
            "owned requirement-oracle proof pairs. Acceptance oracles from the same requirement "
            "that are absent from this task's requirement_proofs may be covered by other tasks; "
            "do not implement, review-fail, or emit ORACLE_PROOF_UPDATES for those absent pairs.",
            "If Task JSON includes verification_refs, those refs are the current repair task's "
            "owned executable proof surface and must pass before the repair can complete.",
        ]
        if task.verification_refs:
            common.extend(
                [
                    "Verification refs command for this task:",
                    self._build_task_proof_evidence_command(task.verification_refs),
                    "When checking verification_refs manually, use the command above or the "
                    "project's configured verification command rewritten to those refs. Do not "
                    "substitute a bare global pytest executable or a different Python environment.",
                ]
            )
        if requirement_context:
            common.extend(["", requirement_context])
        if plan_migration_context.strip():
            common.extend(["", plan_migration_context.strip()])
        if task_status_migration_context.strip():
            common.extend(["", task_status_migration_context.strip()])

        if stage == "implement":
            lines = common + [
                "Implement only this feature slice.",
                "If local verification exposes a tightly coupled regression in files you touched or in paths explicitly implicated by retry feedback, fix it in the same attempt even if it sits slightly outside the nominal task slice.",
                "The current task's owned acceptance criteria and owned requirement proof entries are hard requirements, not optional background.",
                "Honor each owned oracle contract exactly: the implementation and tests must meet or exceed each requirement's oracle_strength, collect proof at the required evidence_boundary, and avoid every forbidden proxy oracle listed in the requirement context.",
                "When the task has requirement_proofs, do NOT edit .auto-agents/state/task_plan.json directly. Instead, include an ORACLE_PROOF_UPDATES JSON block in your final response.",
                "Each ORACLE_PROOF_UPDATES entry must update an existing current-task proof by requirement_id and oracle_index, set status='verified', and include concrete evidence_refs plus proof_type/oracle_strength/evidence_boundary/proxy_oracles when relevant.",
                "Do not submit proof updates for proxy evidence listed in forbidden_proxy_oracles, for final-status-only checks, or for config/metadata-only checks when the requirement demands behavioral/system-boundary proof.",
                "Example final response block:\nORACLE_PROOF_UPDATES:\n```json\n[{\"requirement_id\":\"REQ-001\",\"oracle_index\":1,\"status\":\"verified\",\"proof_type\":\"integration_test\",\"oracle_strength\":\"behavioral\",\"evidence_boundary\":\"system_boundary\",\"evidence_refs\":[\"tests/test_public_api.py::test_behavior\"],\"proxy_oracles\":[]}]\n```",
                "If Task JSON and bound requirements conflict, preserve the bound requirements and mention the conflict in the final summary.",
                "You MUST also write or update tests that verify the acceptance criteria in the Task JSON.",
                "When plan migration context is present, you MUST also migrate any repository tests that still reference retired task IDs or pre-split task-plan structure covered by this task.",
                "When task status migration context is present, migrate only repository tests that assert stale task status. Do not edit orchestrator-owned .auto-agents state snapshots to force that transition early.",
                "Tests should validate observable behavior (API contracts, input/output, side-effects), not internal implementation details.",
                "Before adding or changing tests, inspect nearby repository tests for the same API fields, state-machine outputs, and public payload keys. Preserve existing semantic distinctions unless the task explicitly changes the contract.",
                "Do not collapse layered semantics in assertions. Distinguish internal failure reasons/error codes from outward-facing state labels, next-action hints, and user-action flags unless the repository already defines them as the same contract.",
                "Python proof tests must be deterministic under the project's configured verification command. Do not rely on pytest-only or unittest-only ambient state; explicitly configure test adapters, environment variables, and dependency injection needed by the test.",
                "Python tests must not contact real external services by accident. Use explicit fakes/mocks or test adapters for object storage, providers, databases, and network clients.",
                "Use per-test unique temp paths for mutable artifacts such as sqlite databases, object-storage roots, caches, and generated fixtures so repeated, resumed, or mixed-runner verification cannot reuse stale state.",
                "For external provider integrations, use the listed provider_reference files as the source of truth. Do not search for alternate docs or invent protocol details unless the reference is marked insufficient; stop and report missing documentation instead.",
                "For protocol/direct-integration tasks, add contract tests that verify outbound request shape, auth/header behavior, response normalization, and forbidden legacy payloads where applicable.",
                "If this is a Python project, create and use a project-local conda env at ./.conda and install packages only inside it.",
                "Do not use '.conda' as a generic directory, pip target, virtualenv, or venv path. It must remain a real conda prefix created with 'conda create -p ./.conda ...', including '.conda/conda-meta'.",
                "Keep mutable local test/runtime artifacts (for example sqlite DBs, temp configs, fixtures, and caches) under ignored temp/data paths such as ./.tmp/, ./.tmp-tests/, or ./.data/ instead of tracked repo-root files.",
                "For any other stack, keep dependencies and tool state local to the repository and never rely on global installs.",
                "Do not modify input specification files (spec.md, specs/**, or the active spec file). They are run inputs, not implementation targets.",
                "Do not modify .auto-agents state files except through ORACLE_PROOF_UPDATES or when explicitly requested.",
                "Do not modify .auto-agents docs/state/config files or planning artifacts directly as part of implementation. Keep orchestrator-owned files untouched.",
                "Final response: 3 short bullets describing what changed.",
            ]
            prompt = "\n".join(lines)
            repo_map = self._build_repo_map_section(task, stage="implement")
            if repo_map:
                prompt = f"{prompt}\n\n{repo_map}"
            return prompt

        if stage == "review":
            lines = common + [
                "Review the current uncommitted changes for correctness, regressions, and missing tests.",
                "The Task JSON acceptance criteria and current-task requirement_proofs are in scope. A task passes only if both Task JSON and the owned requirement proof entries are satisfied.",
                "ORACLE PROOF AUDIT: Review every current-task requirement_proofs entry. Fail unless each owned acceptance oracle has verified proof with concrete evidence_refs, the proof_type/oracle_strength/evidence_boundary meet or exceed the requirement, and proxy_oracles do not include anything listed in forbidden_proxy_oracles.",
                "TEST AUDIT: Separately evaluate whether the tests truly cover the acceptance criteria "
                "from the Task JSON. Check that tests validate observable behavior (API contracts, "
                "input/output, side-effects) rather than internal implementation details. "
                "If the tests only pass by mocking/faking internal state instead of exercising real "
                "public interfaces, that is a 'DECISION: fail' issue.",
                "Also fail if tests collapse distinct semantics for neighboring public fields. Check whether reason/error-code fields, public state labels, next-action hints, and waiting/manual-action flags are asserted consistently with adjacent repository tests and the implementation contract.",
                "Also fail if a new or edited test contradicts nearby existing tests for the same public payload fields without an explicit contract change in the task scope.",
                "For Python projects, fail tests that depend on runner-specific ambient state, shared fixed "
                "sqlite/temp paths, or real external services instead of explicit test fakes/adapters.",
                "If plan migration context lists retired task IDs, stale repository tests that still reference those retired IDs or the pre-split task-plan structure are also a 'DECISION: fail' issue.",
                "If task status migration context is present, review stale repository test assertions only. Do NOT fail solely because orchestrator-owned .auto-agents state snapshots still show `in_progress` during review.",
                "SCOPE RULE: Your review scope is bounded by the acceptance criteria in the Task JSON plus the bound requirements and acceptance oracles above. "
                "A 'DECISION: fail' is warranted ONLY when the implementation does not satisfy one or more "
                "task acceptance criteria, bound requirement oracles, introduces a regression in existing tests, or leaves the codebase in a "
                "non-buildable state. Architectural concerns, future robustness improvements, and suggestions "
                "beyond the stated acceptance criteria and bound requirements should be noted as '[NON-BLOCKING]' advisory notes, "
                "NOT as failure reasons.",
                "When issuing 'DECISION: fail', you MUST cite the specific acceptance criterion (by index or text) "
                "or requirement ID/oracle/proof entry that is not satisfied. If no acceptance criterion or requirement oracle is violated but you have advisory concerns, "
                "issue 'DECISION: pass' with those concerns listed as '[NON-BLOCKING]' notes.",
                "For external provider integrations, verify the code and tests against the provider_reference file. Fail if the implementation invents protocol fields, reuses a legacy private gateway payload, or tests only mock an internal gateway contract.",
                "Also fail when the implementation uses a weaker oracle than the requirement allows (for example: proxy-only checks for semantic/human requirements, internal-state-only checks for system_boundary/external_side_effect requirements, or any check explicitly listed in forbidden_proxy_oracles).",
                "Also fail if a negative requirement was weakened by dropping a concrete forbidden field/path/API token from the task acceptance or proof. Example: if the requirement says default project detail must not include `tasks[].result`, tests that only prove `retry_trace` was removed are insufficient.",
                "Use the supplied changed-file and diff context first. Only inspect the rest of the repository when the diff is insufficient.",
                "This stage is read-only. Do not modify any repository files; return only the review result.",
                "Return only the review result. Do not include any preamble, file path note, or tool narration.",
                "The first non-empty line must be exactly 'DECISION: pass' or 'DECISION: fail'.",
                self._review_language_instruction(),
                "After the first line, provide a short review summary.",
            ]
            if task.scope_boundaries.strip():
                lines.append(
                    f"SCOPE BOUNDARIES (explicitly out-of-scope for this task, do NOT fail for these): "
                    f"{task.scope_boundaries.strip()}"
                )
            if task.review_history:
                lines.append(
                    "This is a RETRY review. Your PRIMARY job is to verify that the issues from the previous "
                    "review have been addressed. You may note newly discovered issues, but 'DECISION: fail' "
                    "should only be issued if (a) previous issues remain unresolved, or (b) the fix introduced "
                    "a clear regression. Do NOT fail for newly-discovered scope-expansion concerns that were "
                    "not raised in the previous review."
                )
            if review_context.strip():
                lines.extend(["", review_context.strip()])
            prompt = "\n".join(lines)
            repo_map = self._build_repo_map_section(task, stage="review")
            if repo_map:
                prompt = f"{prompt}\n\n{repo_map}"
            return prompt

        raise RuntimeError(f"Unsupported task stage: {stage}")

    def _execute_task_with_retries(
        self,
        state: RunState,
        task: TaskSpec,
        resume_existing: bool = False,
    ) -> Dict[str, object]:
        max_attempts = self._max_attempts("implement")
        feedback = ""
        current_proof_evidence = self._run_task_proof_evidence(task) if task.review_summary.strip() else None
        if task.review_summary.strip():
            feedback = self._format_task_review_retry_feedback(
                task,
                reason="review rejected the task",
                review_history=task.review_history,
                review_summary=task.review_summary,
                proof_evidence=current_proof_evidence,
            )
        last_reason = "task failed without a recorded reason"
        last_review = ""
        last_failure_ids: List[str] = []
        last_comparable_failures = True
        last_proof_evidence: Optional[Dict[str, object]] = None

        review_fingerprints: List[str] = []
        for entry in task.review_history:
            if isinstance(entry, dict):
                fp = self._review_fingerprint(str(entry.get("summary", "")))
                if fp:
                    review_fingerprints.append(fp)
        # Seed the arbiter trigger counter from persisted history so resumed
        # blocked tasks consult the arbiter on their FIRST fresh review fail
        # instead of waiting for new fails to accumulate from zero.
        prior_review_fails = len([
            entry for entry in task.review_history if isinstance(entry, dict)
        ])
        empty_diff_streak = 0
        overflow_trigger = ""
        overflow_fingerprint = ""
        overflow_arbiter: Optional[Dict[str, object]] = None

        for attempt in range(1, max_attempts + 1):
            state.current_stage = "implement"
            if resume_existing and attempt == 1:
                result = None
            else:
                self._emit_task_activity(task, "implement", attempt)
                implement_prompt = self._build_task_prompt(
                    task,
                    "implement",
                    plan_migration_context=self._build_task_plan_migration_context(state, task),
                )
                if feedback:
                    implement_prompt = (
                        f"{implement_prompt}\n\nPrevious attempt issues:\n{feedback}\n\n"
                        "CRITICAL: Before writing or modifying any code, you MUST first output a step-by-step "
                        "'Fix Plan' in Markdown detailing how you will address all the issues above. "
                        "Use the structured verification triage and evidence below to identify the root causes. "
                        "Do not dismiss tightly coupled regressions in explicitly implicated paths as out of scope. "
                        "Then, proceed to implement the plan."
                    )
                result = self._run_agent_with_retries(
                    state=state,
                    stage="implement",
                    stage_key=f"implement-{task.task_id}",
                    prompt=implement_prompt,
                )
                if not result.ok:
                    last_reason = result.stderr or result.summary or "implementation failed"
                    feedback = self._format_retry_feedback(
                        "implementation_command",
                        reason=last_reason,
                    )
                    continue

                self._sync_allowed_repair_task_plan_edits(state, task)

                proof_updates_applied, proof_update_error = self._apply_oracle_proof_updates_from_text(
                    task,
                    result.summary or result.stdout,
                )
                if proof_update_error:
                    last_reason = proof_update_error
                    feedback = self._format_retry_feedback(
                        "oracle_proof_update",
                        reason=last_reason,
                    )
                    continue
                if proof_updates_applied:
                    proof_findings = self._task_completion_proof_findings(task)
                    if proof_findings:
                        last_reason = self._format_requirement_proof_findings(task, proof_findings)
                        feedback = self._format_retry_feedback(
                            "oracle_proof_update",
                            reason=last_reason,
                        )
                        continue
                    self._persist_tasks(state.tasks if state.tasks else [task])

                if not self._implement_touched_code(task) and not proof_updates_applied:
                    empty_diff_streak += 1
                    last_reason = (
                        "implement step produced no code changes outside .auto-agents/ "
                        f"(empty-diff streak={empty_diff_streak})"
                    )
                    feedback = self._format_retry_feedback(
                        "implementation_command",
                        reason=last_reason,
                    )
                    if empty_diff_streak >= 2:
                        overflow_trigger = (
                            "empty-diff streak: implement produced no code changes on "
                            f"{empty_diff_streak} consecutive attempts"
                        )
                        break
                    continue
                empty_diff_streak = 0

            self._emit_task_activity(task, "verify", attempt)
            task_commands = self._build_task_verify_commands(task)
            quick_failure = self._quick_verify_failure_details(task_commands if task_commands else None)
            if quick_failure:
                last_reason, retryable = quick_failure
                failure_ids = self._normalize_verify_failure_ids([], last_reason)
                verify_analysis = self._analyze_verify_failure(task, failure_ids, comparable=False)
                last_failure_ids = list(failure_ids)
                last_comparable_failures = False
                last_proof_evidence = None
                verify_stats = str(verify_analysis["stats"])
                self._record_verify_result(
                    task,
                    attempt,
                    "fail",
                    last_reason,
                    failure_ids,
                    comparable_failures=False,
                )
                feedback = self._format_retry_feedback(
                    "pre_verify_check",
                    reason=last_reason,
                )
                self._emit_task_verify_result(task, "fail", last_reason, stats=verify_stats)
                if not retryable:
                    break
                if bool(verify_analysis["stop_retry"]):
                    if bool(verify_analysis.get("non_comparable")):
                        last_reason = self._format_non_comparable_verify_failure_reason(last_reason)
                    else:
                        last_reason = self._format_repeated_verify_failure_reason(
                            last_reason,
                            first_attempt=verify_analysis["first_attempt"],
                            repeat=verify_analysis["repeat"],
                        )
                    break
                continue

            verify_result = self._run_task_verify(task, state=state)
            if not verify_result["ok"]:
                last_reason = str(verify_result["reason"])
                failure_ids = self._normalize_verify_failure_ids(
                    verify_result.get("failure_ids", []),
                    last_reason,
                )
                comparable_failures = bool(verify_result.get("comparable_failures", True))
                last_failure_ids = list(failure_ids)
                last_comparable_failures = comparable_failures
                last_proof_evidence = (
                    verify_result.get("proof_evidence")
                    if isinstance(verify_result.get("proof_evidence"), dict)
                    else None
                )
                verify_analysis = self._analyze_verify_failure(
                    task,
                    failure_ids,
                    comparable=comparable_failures,
                )
                if (
                    (
                        bool(verify_result.get("retryable_missing_owned_evidence_refs"))
                        or bool(verify_result.get("retryable_owned_evidence_failure_refs"))
                    )
                    and bool(verify_analysis["stop_retry"])
                ):
                    verify_analysis = dict(verify_analysis)
                    verify_analysis["stop_retry"] = False
                    stats = str(verify_analysis["stats"]).replace(
                        " action=stop-unchanged-set",
                        "",
                    )
                    verify_analysis["stats"] = (
                        f"{stats} action=continue-owned-evidence-repair"
                    )
                verify_stats = str(verify_analysis["stats"])
                self._record_verify_result(
                    task,
                    attempt,
                    "fail",
                    last_reason,
                    failure_ids,
                    comparable_failures=comparable_failures,
                )
                verify_feedback = self._build_verify_retry_feedback(verify_result)
                feedback = self._format_retry_feedback(
                    "local_verification",
                    reason=last_reason,
                    verification_summary=str(verify_feedback.get("verification_summary", "")),
                    proof_evidence_summary=self._format_proof_evidence_summary(
                        verify_result.get("proof_evidence")
                        if isinstance(verify_result.get("proof_evidence"), dict)
                        else None
                    ),
                    implicated_paths=list(verify_feedback.get("implicated_paths", [])),
                    raw_excerpts=list(verify_feedback.get("raw_excerpts", [])),
                )
                self._emit_task_verify_result(task, "fail", last_reason, stats=verify_stats)
                if bool(verify_result.get("contract_scope_issue")):
                    break
                if bool(verify_analysis["stop_retry"]):
                    if not comparable_failures:
                        last_reason = self._format_non_comparable_verify_failure_reason(last_reason)
                    else:
                        last_reason = self._format_repeated_verify_failure_reason(
                            last_reason,
                            first_attempt=verify_analysis["first_attempt"],
                            repeat=verify_analysis["repeat"],
                        )
                    break
                continue

            self._record_verify_result(task, attempt, "pass", str(verify_result["reason"]))
            self._emit_task_verify_result(task, "pass", str(verify_result["reason"]))

            review_fingerprint = worktree_fingerprint(self.project_root)
            gate_result = self._cached_review_result(state, task, review_fingerprint)
            if gate_result is None:
                self._emit_task_activity(task, "review", attempt)
                gate_result = self._run_task_review(
                    state.run_id,
                    task,
                    verify_reason=str(verify_result["reason"]),
                    proof_evidence=(
                        verify_result.get("proof_evidence")
                        if isinstance(verify_result.get("proof_evidence"), dict)
                        else None
                    ),
                    state=state,
                )
                if gate_result["ok"]:
                    self._store_task_review_cache(
                        state,
                        task,
                        review_fingerprint,
                        str(gate_result["review"]),
                    )
            if gate_result["ok"]:
                proof_findings = self._task_completion_proof_findings(task)
                if proof_findings:
                    last_reason = self._format_requirement_proof_findings(task, proof_findings)
                    feedback = self._format_retry_feedback(
                        "oracle_proof_gate",
                        reason=last_reason,
                        proof_evidence_summary=self._format_proof_evidence_summary(
                            verify_result.get("proof_evidence")
                            if isinstance(verify_result.get("proof_evidence"), dict)
                            else None
                        ),
                    )
                    continue
                gate_result["verify_current_failure_ids"] = list(
                    verify_result.get("current_failure_ids", [])
                )
                return gate_result

            last_reason = str(gate_result["reason"])
            last_review = str(gate_result["review"])
            
            task.review_summary = last_review
            task.review_history.append({
                "attempt": attempt,
                "summary": last_review,
            })

            rewind_stage = self._review_feedback_rewind_stage(last_review)
            if rewind_stage:
                return {
                    "ok": False,
                    "review": last_review,
                    "reason": last_reason,
                    "rewind_to_stage": rewind_stage,
                    "rewind_reason": (
                        f"review feedback points to {rewind_stage}-owned artifact; "
                        "rewinding to the owning stage"
                    ),
                }

            current_fp = self._review_fingerprint(last_review)
            if current_fp:
                if current_fp in review_fingerprints:
                    overflow_trigger = (
                        f"review blockers repeated (fingerprint {current_fp} seen on "
                        f"attempts {review_fingerprints.count(current_fp) + 1} of {attempt})"
                    )
                    overflow_fingerprint = current_fp
                    review_fingerprints.append(current_fp)
                    break
                review_fingerprints.append(current_fp)

            total_review_fails = prior_review_fails + attempt
            if total_review_fails >= self.ARBITER_MIN_REVIEW_FAILS:
                arbiter_result = self._run_scope_arbiter(
                    state.run_id, task, last_review,
                )
                task.arbitration_history.append({
                    "attempt": attempt,
                    "total_review_fails": total_review_fails,
                    "decision": arbiter_result.get("decision", ""),
                    "rationale": arbiter_result.get("rationale", ""),
                    "split_axis": list(arbiter_result.get("split_axis", []) or []),
                })
                self._persist_tasks(state.tasks if state.tasks else [task])
                if arbiter_result.get("decision") == "SPLIT":
                    overflow_trigger = (
                        "scope arbiter SPLIT after "
                        f"{total_review_fails} review fail(s): "
                        + (str(arbiter_result.get("rationale", "")) or "no rationale")
                    )
                    overflow_fingerprint = current_fp
                    overflow_arbiter = arbiter_result
                    break

            feedback = self._format_task_review_retry_feedback(
                task,
                reason=last_reason,
                review_history=task.review_history,
                review_summary=last_review,
                proof_evidence=(
                    verify_result.get("proof_evidence")
                    if isinstance(verify_result.get("proof_evidence"), dict)
                    else None
                ),
            )

        if overflow_trigger:
            return {
                "ok": False,
                "review": last_review or feedback,
                "reason": f"scope_overflow: {overflow_trigger}",
                "rewind_to_plan": True,
                "split_task_id": task.task_id,
                "split_trigger": overflow_trigger,
                "split_fingerprint": overflow_fingerprint,
                "arbiter": overflow_arbiter or {},
            }

        return {
            "ok": False,
            "review": last_review or feedback,
            "reason": last_reason,
            "failure_ids": list(last_failure_ids),
            "comparable_failures": bool(last_comparable_failures),
            "proof_evidence": last_proof_evidence or {},
        }

    def _run_agent_with_retries(
        self,
        state: Optional[RunState],
        stage: str,
        stage_key: str,
        prompt: str,
        validation_feedback: Optional[Callable[[AgentResult], Optional[str]]] = None,
        run_id: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> AgentResult:
        attempts = self._max_attempts(stage)
        active_run_id = run_id or (state.run_id if state is not None else load_run_state(self.project_root).run_id)
        snapshot_before = self._worktree_change_snapshot()
        resolved_effort = effort or self.config.efforts.get(stage, "balanced")
        feedback = ""
        last_error = f"{stage_key} failed"
        cumulative_usage: Optional[AgentUsage] = None
        usage_available = False
        restore_workspace = None
        restore_root: Optional[Path] = None
        if stage == "implement":
            restore_workspace = tempfile.TemporaryDirectory(prefix="auto-agents-restore-")
            restore_root = Path(restore_workspace.name)
            self._capture_auto_agents_restore_point(restore_root)

        try:
            for attempt in range(1, attempts + 1):
                attempt_prompt = prompt
                if feedback:
                    attempt_prompt = f"{prompt}\n\nPrevious attempt issues:\n{feedback}\n"

                artifact_stage = stage_key if attempt == 1 else f"{stage_key}-attempt-{attempt}"
                output_path = self._stage_output_path(active_run_id, artifact_stage)
                write_run_prompt(self.project_root, active_run_id, artifact_stage, attempt_prompt)
                request = AgentRequest(
                    stage=stage,
                    effort=resolved_effort,
                    prompt=attempt_prompt,
                    cwd=self.project_root,
                    output_path=output_path,
                    stream_output=self._stream_agent_output_callback(artifact_stage) if self._print_agent_output else None,
                )
                with log_timing(self.logger, f"agent:{artifact_stage} attempt={attempt}"):
                    result = self._call_with_failover(request)
                if result.usage is not None:
                    cumulative_usage = (cumulative_usage or AgentUsage()).plus(result.usage)
                    usage_available = True
                self._emit_agent_output(artifact_stage, result)
                self._cleanup_ephemeral_tooling_artifacts(include_untracked_build_lib=stage != "implement")
                if state is not None:
                    state.agent_attempts[stage_key] = attempt
                    save_run_state(self.project_root, state)
                violation = self._stage_mutation_scope_violation(
                    stage=stage,
                    stage_key=stage_key,
                    run_id=active_run_id,
                    before_snapshot=snapshot_before,
                )
                if violation is not None:
                    offending, allowed_scope = violation
                    if (
                        stage == "implement"
                        and restore_root is not None
                        and all(self._is_implement_restorable_scope_violation_path(path) for path in offending)
                    ):
                        self._restore_paths_from_restore_point(offending, restore_root)
                        last_error = (
                            f"stage {stage} modified files outside its ownership during {stage_key}. "
                            f"Changed paths: {self._changed_path_preview(offending)}. "
                            f"Allowed scope: {'; '.join(allowed_scope)}. "
                            "Do not edit orchestrator-owned .auto-agents state, docs, config, planning files, "
                            "or input specs during implementation; update repository code/tests instead."
                        )
                        feedback = last_error
                        continue
                    raise RuntimeError(
                        f"stage {stage} modified files outside its ownership during {stage_key}. "
                        f"Changed paths: {self._changed_path_preview(offending)}. "
                        f"Allowed scope: {'; '.join(allowed_scope)}."
                    )

                if not result.ok:
                    last_error = result.stderr or result.summary or f"{stage_key} failed"
                    feedback = f"- The command failed.\n- Details: {last_error}"
                    continue

                if validation_feedback is not None:
                    issue = validation_feedback(result)
                    if issue:
                        last_error = issue
                        feedback = issue
                        continue

                self._emit_agent_metrics(
                    stage_key,
                    result,
                    attempts=attempt,
                    usage=(cumulative_usage if usage_available else None),
                    model=result.model or self._model_label_for_agent_stage(stage, resolved_effort),
                )
                return result
        finally:
            if restore_workspace is not None:
                restore_workspace.cleanup()

        self._emit_agent_metrics(
            stage_key,
            AgentResult(
                ok=False,
                command=[],
                output_path=self._stage_output_path(active_run_id, stage_key),
                summary="",
                model=self._model_label_for_agent_stage(stage, resolved_effort),
                usage=(cumulative_usage if usage_available else None),
                stderr=last_error,
                returncode=1,
            ),
            attempts=attempts,
            usage=(cumulative_usage if usage_available else None),
            model=self._model_label_for_agent_stage(stage, resolved_effort),
        )
        raise RuntimeError(f"{stage_key} exhausted retries: {last_error}")

    def _emit_agent_output(self, stage_key: str, result: AgentResult) -> None:
        if not self._print_agent_output:
            return

        sections = [f"[agent:{stage_key}] returncode={result.returncode} ok={str(result.ok).lower()}"]
        summary_was_streamed = False
        if result.summary and result.streamed_stdout:
            summary_was_streamed = result.stdout.strip() == result.summary.strip()
        if result.summary and not summary_was_streamed:
            sections.append(result.summary.strip())
        if result.stderr and not result.streamed_stderr:
            sections.append(f"[stderr]\n{result.stderr.strip()}")
        self.logger.info("\n\n".join(sections))

    def _emit_stage_start(self, stage: str) -> None:
        model = self._model_label_for_top_level_stage(stage)
        self.logger.info(f"[stage:{stage}] start provider={self._current_provider} model={model}")

    def _emit_stage_verify_result(self, decision: str, summary: str, route: str = "") -> None:
        header = f"[stage:verify] decision={decision}"
        if route.strip():
            header = f"{header} route={route.strip()}"
        sections = [header]
        if summary.strip():
            sections.append(summary.strip())
        self.logger.info("\n".join(sections))

    def _emit_plan_task_count(self, tasks: Iterable[TaskSpec]) -> None:
        task_list = list(tasks)
        self.logger.info(f"[stage:plan] tasks={len(task_list)}")

    def _emit_agent_metrics(
        self,
        stage_key: str,
        result: AgentResult,
        attempts: int,
        usage: Optional[AgentUsage],
        model: str,
    ) -> None:
        usage_text = "unknown"
        if usage is not None:
            usage_text = (
                f"input={usage.input_tokens} cached_input={usage.cached_input_tokens} "
                f"output={usage.output_tokens} total={usage.total_tokens}"
            )
        repo_map_text = ""
        rm = self._last_repo_map_result
        if rm is not None:
            repo_map_text = (
                f" repo_map_enabled={str(rm.enabled).lower()}"
                f" repo_map_skipped={rm.skipped_reason or 'none'}"
                f" repo_map_files={rm.files_included}"
                f" repo_map_tokens={rm.tokens_actual}/{rm.tokens_budget}"
                f" repo_map_cache_hit={str(rm.cache_hit).lower()}"
                f" repo_map_cache_hits={rm.cache_hits}"
                f" repo_map_cache_misses={rm.cache_misses}"
            )
        self.logger.info(
            (
                f"[agent:{stage_key}] completed ok={str(result.ok).lower()} "
                f"returncode={result.returncode} attempts={attempts} "
                f"provider={self._current_provider} model={model or 'unknown'} "
                f"tokens={usage_text}"
                f"{repo_map_text}"
            )
        )

    def _emit_task_activity(self, task: TaskSpec, action: str, attempt: int) -> None:
        self.logger.info(f"[task:{task.task_id}] {action} attempt={attempt} title={task.title}")

    def _emit_task_blocked(self, task: TaskSpec, reason: str) -> None:
        self.logger.info(f"[task:{task.task_id}] blocked reason={reason}")

    def _emit_task_review_result(self, task: TaskSpec, decision: str, summary: str) -> None:
        sections = [f"[task:{task.task_id}] review decision={decision}"]
        if summary.strip():
            sections.append(summary.strip())
        self.logger.info("\n".join(sections))

    @staticmethod
    def _normalize_verify_failure_ids(failure_ids: Iterable[str], reason: str) -> List[str]:
        normalized = sorted({str(item).strip() for item in failure_ids if str(item).strip()})
        if normalized:
            return normalized
        collapsed = " ".join(reason.split()).strip()
        return [f"reason:{collapsed}"] if collapsed else ["reason:unknown"]

    def _verify_failure_signature_from_entry(self, entry: Dict[str, object]) -> List[str]:
        raw_ids = entry.get("failure_ids", [])
        if isinstance(raw_ids, list):
            return self._normalize_verify_failure_ids(raw_ids, str(entry.get("summary", "")))
        return self._normalize_verify_failure_ids([], str(entry.get("summary", "")))

    def _analyze_verify_failure(
        self,
        task: TaskSpec,
        failure_ids: List[str],
        *,
        comparable: bool = True,
    ) -> Dict[str, object]:
        prior_failures = [
            entry for entry in task.verify_history
            if (
                isinstance(entry, dict)
                and str(entry.get("decision", "")) == "fail"
                and bool(entry.get("comparable_failures", True))
            )
        ]
        failure_count = len(failure_ids)
        if not comparable:
            prior_non_comparable = [
                entry for entry in task.verify_history
                if (
                    isinstance(entry, dict)
                    and str(entry.get("decision", "")) == "fail"
                    and not bool(entry.get("comparable_failures", True))
                )
            ]
            current_signature = tuple(failure_ids)
            matching_entries = [
                entry
                for entry in prior_non_comparable
                if tuple(self._verify_failure_signature_from_entry(entry)) == current_signature
            ]
            if matching_entries:
                first_attempt = matching_entries[0].get("attempt", "?")
                repeat = len(matching_entries) + 1
                return {
                    "stats": (
                        f"compare=non-comparable-failure repeat={repeat} "
                        f"failure_ids={failure_count} action=stop-unresolved-identity"
                    ),
                    "stop_retry": True,
                    "non_comparable": True,
                    "first_attempt": first_attempt,
                    "repeat": repeat,
                }
            return {
                "stats": f"compare=non-comparable-failure failure_ids={failure_count} action=continue",
                "stop_retry": False,
                "non_comparable": True,
                "first_attempt": None,
                "repeat": 1,
            }
        if not prior_failures:
            return {
                "stats": f"compare=first-failure-set failure_ids={failure_count}",
                "stop_retry": False,
                "first_attempt": None,
                "repeat": 1,
            }

        current_signature = tuple(failure_ids)
        latest_entry = prior_failures[-1]
        latest_attempt = latest_entry.get("attempt", "?")
        latest_signature = tuple(self._verify_failure_signature_from_entry(latest_entry))
        matching_entries = [
            entry for entry in prior_failures
            if tuple(self._verify_failure_signature_from_entry(entry)) == current_signature
        ]
        if matching_entries:
            first_attempt = matching_entries[0].get("attempt", "?")
            repeat = len(matching_entries) + 1
            if latest_signature == current_signature:
                stats = (
                    f"compare=same-failure-set-as-attempt-{first_attempt} "
                    f"repeat={repeat} failure_ids={failure_count}"
                )
                if repeat >= 2:
                    stats = f"{stats} action=stop-unchanged-set"
                return {
                    "stats": stats,
                    "stop_retry": repeat >= 2,
                    "first_attempt": first_attempt,
                    "repeat": repeat,
                }
            return {
                "stats": (
                    f"compare=regression failure-set-from-attempt-{first_attempt} "
                    f"previous=attempt-{latest_attempt} repeat={repeat} failure_ids={failure_count}"
                ),
                "stop_retry": False,
                "first_attempt": first_attempt,
                "repeat": repeat,
            }

        latest_set = set(latest_signature)
        current_set = set(current_signature)
        new_count = len(current_set - latest_set)
        resolved_count = len(latest_set - current_set)
        return {
            "stats": (
                f"compare=changed-failure-set-vs-attempt-{latest_attempt} failure_ids={failure_count} "
                f"new={new_count} resolved={resolved_count}"
            ),
            "stop_retry": False,
            "first_attempt": None,
            "repeat": 1,
        }

    @staticmethod
    def _format_repeated_verify_failure_reason(
        reason: str,
        *,
        first_attempt: object,
        repeat: object,
    ) -> str:
        return (
            f"unchanged verify failure set repeated from attempt-{first_attempt} "
            f"(repeat={repeat}); stopping retries early\n{reason.strip()}"
        )

    @staticmethod
    def _format_non_comparable_verify_failure_reason(reason: str) -> str:
        return (
            "verification failed without stable test-case failure ids; "
            "stopping automatic retries until the failure identity can be resolved\n"
            f"{reason.strip()}"
        )

    def _task_verify_contract_scope_reason(
        self,
        task: Optional[TaskSpec],
        failure_ids: Iterable[str],
        *,
        task_scope_label: str,
    ) -> str:
        if task is None:
            return ""
        task_commands = self._build_task_verify_commands(task)
        if task_commands:
            return ""
        evidence_refs = self._task_planned_evidence_refs(task)
        if not evidence_refs:
            return ""
        owned_paths = {
            self._split_evidence_ref(ref)[0].replace("\\", "/").strip()
            for ref in evidence_refs
            if self._split_evidence_ref(ref)[0].replace("\\", "/").strip()
        }
        if not owned_paths:
            return ""
        failure_paths = {
            self._split_evidence_ref(failure_id)[0].replace("\\", "/").strip()
            for failure_id in failure_ids
            if str(failure_id).strip()
            and not str(failure_id).strip().startswith("reason:")
            and not str(failure_id).strip().startswith("cmd:")
        }
        if not failure_paths:
            return ""
        if failure_paths & owned_paths:
            return ""
        scope = task_scope_label or ", ".join(sorted(owned_paths)[:4])
        if len(owned_paths) > 4:
            scope = f"{scope}, ..."
        failure_preview = ", ".join(sorted(failure_paths)[:4])
        if len(failure_paths) > 4:
            failure_preview = f"{failure_preview}, ..."
        return (
            "verification scope mismatch: new failures are outside this task's owned test/proof "
            f"surface. Owned scope: {scope}. New failure paths: {failure_preview}. "
            "Treat this as a product-contract or gate-scope issue instead of retrying implementation."
        )

    @staticmethod
    def _record_verify_result(
        task: TaskSpec,
        attempt: int,
        decision: str,
        summary: str,
        failure_ids: Optional[Iterable[str]] = None,
        comparable_failures: bool = True,
    ) -> None:
        entry: Dict[str, object] = {
            "attempt": attempt,
            "decision": decision,
            "summary": summary.strip(),
        }
        normalized_failure_ids = [str(item).strip() for item in (failure_ids or []) if str(item).strip()]
        if normalized_failure_ids:
            entry["failure_ids"] = normalized_failure_ids
        entry["comparable_failures"] = bool(comparable_failures)
        task.verify_history.append(entry)

    def _emit_task_verify_result(self, task: TaskSpec, decision: str, summary: str, stats: str = "") -> None:
        header = f"[task:{task.task_id}] verify decision={decision}"
        if stats.strip():
            header = f"{header} {stats.strip()}"
        sections = [header]
        if summary.strip():
            sections.append(summary.strip())
        self.logger.info("\n".join(sections))

    def _format_task_failure_error(self, task: TaskSpec, reason: str, review_summary: str) -> str:
        message = f"Task {task.task_id} failed gates: {reason}"
        review_excerpt = self._review_failure_excerpt(reason, review_summary)
        if review_excerpt:
            return f"{message}. Review: {review_excerpt}"
        return message

    @staticmethod
    def _review_failure_excerpt(reason: str, review_summary: str, max_chars: int = 200) -> str:
        if reason != "review rejected the task":
            return ""
        for raw_line in review_summary.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > max_chars:
                return line[: max_chars - 3].rstrip() + "..."
            return line
        return ""

    def _stream_agent_output_callback(self, stage_key: str) -> Callable[[str, str], None]:
        line_starts = {"stdout": True, "stderr": True}

        def stream_output(stream_name: str, chunk: str) -> None:
            if not chunk:
                return
            prefix = f"[agent:{stage_key}:{stream_name}] "
            parts = chunk.splitlines(keepends=True)
            for part in parts:
                if line_starts.get(stream_name, True):
                    self.agent_output_stream.write(prefix)
                self.agent_output_stream.write(part)
                line_starts[stream_name] = part.endswith("\n")
            self.agent_output_stream.flush()

        return stream_output

    def _model_label_for_top_level_stage(self, stage: str) -> str:
        if stage == "verify":
            return "n/a"
        if stage == "implement":
            implement_model = self._model_label_for_agent_stage("implement", self.config.efforts.get("implement", "balanced"))
            review_effort = self.config.efforts.get("review", "balanced")
            review_model = "task-dependent" if review_effort == "balanced" else self._model_label_for_agent_stage("review", review_effort)
            return f"implement={implement_model} review={review_model}"
        return self._model_label_for_agent_stage(stage, self.config.efforts.get(stage, "balanced"))

    def _model_label_for_agent_stage(self, stage: str, effort: str) -> str:
        provider_kind = self.config.provider.kind
        if provider_kind == "mock":
            return "mock"
        if provider_kind not in ("codex", "copilot-cli"):
            return self.config.provider.binary

        explicit_model = self._configured_explicit_model()
        if explicit_model:
            return explicit_model

        profile = self.config.provider.profile_map.get(effort)
        if profile:
            return f"profile:{profile}"
        return "default"

    def _configured_explicit_model(self) -> str:
        extra_args = list(self.config.provider.extra_args)
        for index, value in enumerate(extra_args):
            if value in {"--model", "-m"} and index + 1 < len(extra_args):
                return extra_args[index + 1]
        return ""

    # -- provider failover ------------------------------------------------

    @staticmethod
    def _is_failover_error(result: AgentResult) -> bool:
        if result.ok:
            return False
        text = result.stderr or ""
        return _FAILOVER_PATTERN.search(text) is not None

    @staticmethod
    def _failover_error_label(result: AgentResult) -> str:
        text = result.stderr or ""
        if _FAILOVER_TIMEOUT_PATTERN.search(text):
            return "timeout/stall"
        if _FAILOVER_QUOTA_PATTERN.search(text):
            return "quota/rate error"
        return "provider availability error"

    def _failover_provider_order(self) -> List[str]:
        active = self.config.active_provider
        return [active] + [k for k in self.config.providers if k != active]

    def _build_adapter_for_provider(self, provider_kind: str):
        prov = self.config.providers[provider_kind]
        if prov.kind == "codex":
            return CodexAdapter(prov)
        if prov.kind == "copilot-cli":
            return CopilotCliAdapter(prov)
        if prov.kind == "antigravity":
            return AntigravityAdapter(prov)
        if prov.kind == "mock":
            return MockAdapter()
        return ShellAdapter(prov)

    def _call_with_failover(self, request: AgentRequest) -> AgentResult:
        # Build provider order: [last_successful or active] + untried + previously_failed
        base_order = self._failover_provider_order()
        first = self._last_successful_provider if self._last_successful_provider else self.config.active_provider
        rest = [k for k in base_order if k != first]
        untried = [k for k in rest if k not in self._failed_providers]
        retryable = [k for k in rest if k in self._failed_providers]
        order = [first] + untried + retryable

        tried: List[str] = []
        last_error = ""
        for kind in order:
            adapter = self.adapter if kind == self.config.active_provider else self._build_adapter_for_provider(kind)
            available_fn = getattr(adapter, "available", None)
            if available_fn is not None and not available_fn():
                self._failed_providers.add(kind)
                self.logger.info(f"[failover] provider={kind} binary not found, skipping")
                tried.append(kind)
                continue

            self._current_provider = kind
            result = adapter.run(request)
            tried.append(kind)

            if result.ok:
                self._last_successful_provider = kind
                self._failed_providers.discard(kind)
                if kind != self.config.active_provider:
                    self.logger.info(f"[failover] using provider={kind}")
                return result

            if not self._is_failover_error(result):
                return result

            self._failed_providers.add(kind)
            snippet = (result.stderr or "")[:120]
            label = self._failover_error_label(result)
            self.logger.info(f"[failover] provider={kind} {label} ({snippet}), trying next...")
            last_error = result.stderr or result.summary or "unknown error"

        raise RuntimeError(
            f"All providers exhausted. Tried: {tried}. Last error: {last_error}"
        )

    def _set_document_language(self, language: str) -> None:
        if language not in DOCUMENT_LANGUAGE_OPTIONS:
            raise ValueError(f"Unsupported document language: {language}")
        if self.config.docs.language == language:
            return
        self.config.docs.language = language
        save_project_config(self.project_root, self.config)

    def _set_active_provider(self, provider_kind: str) -> None:
        self._current_provider = provider_kind
        if self.config.active_provider == provider_kind:
            return
        self.config.set_active_provider(provider_kind)
        save_project_config(self.project_root, self.config)
        self.adapter = self._build_adapter(self.config)

    def _document_language_instruction(self) -> str:
        if self.config.docs.language == "zh":
            return "Write the document content and final bullets in Simplified Chinese."
        return "Write the document content and final bullets in English."

    def _plan_language_instruction(self) -> str:
        if self.config.docs.language == "zh":
            return (
                "Write all human-readable JSON fields and final bullets in Simplified Chinese. "
                "Keep shell commands and machine-readable keys in English."
            )
        return (
            "Write all human-readable JSON fields and final bullets in English. "
            "Keep shell commands and machine-readable keys in English."
        )

    def _review_language_instruction(self) -> str:
        if self.config.docs.language == "zh":
            return "After the first line, write the review summary in Simplified Chinese."
        return "After the first line, write the review summary in English."

    def _readme_language_instruction(self) -> str:
        if self.config.docs.language == "zh":
            return "Write the README content and final bullets in Simplified Chinese."
        return "Write the README content and final bullets in English."

    def _max_attempts(self, stage: str) -> int:
        return max(1, self.config.retries.per_stage.get(stage, self.config.retries.default_max_attempts))

    @staticmethod
    def _plan_summary_justifies_no_new_tasks(summary: str) -> bool:
        normalized = str(summary or "").strip().lower()
        if "coverage analysis" not in normalized and "覆盖分析" not in normalized:
            return False
        no_uncovered_markers = (
            "uncovered: none",
            "uncovered：none",
            "uncovered: no",
            "uncovered：no",
            "uncovered: 无",
            "uncovered：无",
            "未发现 uncovered",
            "无 uncovered",
            "无缺失",
            "无未覆盖",
        )
        return any(marker in normalized for marker in no_uncovered_markers)

    def _plan_validation_feedback(self, result: AgentResult) -> Optional[str]:
        payload = load_task_plan(self.project_root)
        trace = load_requirements_trace(self.project_root)
        payload, status_normalization_updates = normalize_generated_task_plan_statuses(payload)
        payload, oracle_preservation_updates = preserve_task_plan_negative_oracle_clauses(
            payload,
            trace,
        )
        plan_normalization_updates = status_normalization_updates + oracle_preservation_updates
        if plan_normalization_updates and isinstance(payload, dict):
            save_task_plan(self.project_root, payload)
            for update in plan_normalization_updates:
                self.logger.info(f"[plan] {update}")
        prior_done_tasks = [
            item
            for item in getattr(self, "_plan_prior_done_task_payloads", [])
            if isinstance(item, dict)
        ]
        errors = validate_task_plan_with_requirements(
            payload,
            trace,
            enforce_active_task_granularity=True,
            historical_tasks=load_archived_done_task_payloads(self.project_root) + prior_done_tasks,
        )
        raw_steps = payload.get("verification_steps", [])
        if isinstance(raw_steps, list) and raw_steps:
            steps = [
                VerificationStep.from_dict(dict(item))
                for item in raw_steps
                if isinstance(item, dict)
            ]
            try:
                step_commands = commands_from_verification_steps(steps, self.project_root)
            except ValueError as error:
                errors.append(str(error))
            else:
                errors.extend(
                    validate_verification_command_paths(
                        step_commands,
                        self.project_root,
                        "task plan verification_steps",
                    )
                )
        else:
            errors.extend(
                validate_verification_command_paths(
                    payload.get("verification_commands", []),
                    self.project_root,
                    "task plan verification_commands",
                )
            )
        if not errors:
            # Soft warning: if this is an iteration with no new pending tasks, nudge the agent.
            is_iteration = any(
                isinstance(t, dict) and t.get("status") == "done"
                for t in payload.get("tasks", [])
            )
            has_new = any(
                isinstance(t, dict) and t.get("status") != "done"
                for t in payload.get("tasks", [])
            )
            if is_iteration and not has_new:
                if self._plan_summary_justifies_no_new_tasks(result.summary):
                    return None
                return (
                    "WARNING: This is an iteration run but the task plan contains NO new pending tasks. "
                    "All tasks are marked 'done'. Re-examine whether the done tasks' ACCEPTANCE CRITERIA "
                    "truly cover every requirement in the brief's current iteration scope. "
                    "If they do, add a brief justification to your summary. "
                    "If not, append new tasks for the uncovered scope."
                )
            return None
        bullets = "\n".join(f"- {item}" for item in errors)
        return (
            "The task plan JSON is invalid. Rewrite the file and fix all issues exactly.\n"
            f"{bullets}"
        )

    def _provider_research_validation_feedback(self, _: AgentResult) -> Optional[str]:
        trace = load_requirements_trace(self.project_root)
        lock = load_provider_references_lock(self.project_root)
        missing = []
        refs = lock.get("references", {}) if isinstance(lock, dict) else {}
        if not isinstance(refs, dict):
            return "provider_references.lock.json must contain a 'references' object"
        for requirement in external_doc_requirements(trace):
            reference = str(requirement.get("provider_reference", "")).strip()
            if not reference:
                missing.append(f"{requirement.get('id')}: missing provider_reference")
                continue
            status = provider_reference_status(lock, reference)
            if status == "missing":
                missing.append(f"{requirement.get('id')}: no lock entry for {reference}")
            ref_path = self.project_root / reference
            if not ref_path.exists():
                missing.append(f"{requirement.get('id')}: missing provider reference file {reference}")
        if missing:
            bullets = "\n".join(f"- {item}" for item in missing)
            return (
                "Provider research output is incomplete. Update the local provider references and lock file.\n"
                f"{bullets}"
            )
        return None

    def _review_validation_feedback(self, result: AgentResult) -> Optional[str]:
        if not self._has_explicit_review_decision(result.summary):
            return (
                "The review response is invalid. It must include a line exactly equal to "
                "'DECISION: pass' or 'DECISION: fail'. Rewrite the review output."
            )
        decision, summary = self._parse_review_decision(result.summary)
        if decision == "fail" and self._review_blocks_on_orchestrator_state_snapshot(summary):
            return (
                "The review response is invalid. Do not fail solely because orchestrator-owned "
                ".auto-agents state snapshots still show an in-flight task status. Review product "
                "behavior and repository tests instead, and rewrite the review output."
            )
        return None

    @staticmethod
    def _review_blocks_on_orchestrator_state_snapshot(summary: str) -> bool:
        text = (summary or "").lower()
        if not text:
            return False
        if (
            ".auto-agents/state/task_plan.json" not in text
            and ".auto-agents/state/run_state.json" not in text
        ):
            return False
        return any(token in text for token in ("in_progress", "`in_progress`", "status", "`done`", "done"))

    def _clarify_validation_feedback(self, _: AgentResult) -> Optional[str]:
        path = docs_dir(self.project_root) / "project_brief.md"
        errors = validate_required_document(path, "project_brief.md")
        trace = load_requirements_trace(self.project_root, normalize=False)
        errors.extend(validate_requirements_trace_payload(trace))

        # Iteration safety: detect silent deletion of pre-existing REQ IDs.
        # The pre-snapshot is captured in _run_interactive_clarify before
        # generation; on first run it is empty so this check is a no-op.
        pre_ids = getattr(self, "_clarify_pre_trace_ids", set()) or set()
        if pre_ids:
            current_ids = {
                str(item.get("id", "")).strip()
                for item in (trace.get("requirements") or [])
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            }
            missing = sorted(pre_ids - current_ids)
            if missing:
                preview = ", ".join(missing[:10])
                more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
                errors.append(
                    "iteration trace deleted existing REQ IDs without marking them superseded: "
                    f"{preview}{more}. Restore these entries; if they are no longer in scope, "
                    "keep id/text/source/acceptance_oracles and set status='superseded' instead of removing them."
                )

        if not errors:
            return None
        bullets = "\n".join(f"- {item}" for item in errors)
        return (
            "The clarify output is incomplete. Rewrite the project brief and requirements trace in place, "
            "preserving required brief headings and valid requirements_trace.json shape.\n"
            f"{bullets}"
        )

    @staticmethod
    def _active_or_deferred_req_ids(trace: dict) -> Set[str]:
        ids: Set[str] = set()
        for item in (trace.get("requirements") or []):
            if not isinstance(item, dict):
                continue
            req_id = str(item.get("id", "")).strip()
            if not req_id:
                continue
            status = str(item.get("status", "")).strip().lower()
            # Superseded entries are already obsolete; we only require that
            # active/deferred entries are not silently dropped. (We still
            # forbid deleting superseded entries via the prompt, but we do
            # not hard-fail on those to avoid blocking long-tail cleanup.)
            if status in ("", "active", "deferred"):
                ids.add(req_id)
        return ids

    def _design_validation_feedback(self, _: AgentResult) -> Optional[str]:
        path = docs_dir(self.project_root) / "architecture.md"
        errors = validate_required_document(path, "architecture.md")
        try:
            trace = load_requirements_trace(self.project_root)
        except Exception:
            trace = {}
        architecture_rel = self._relative_repo_path(path)
        if isinstance(trace, dict):
            for requirement in trace.get("requirements", []) or []:
                if not isinstance(requirement, dict):
                    continue
                findings = forbidden_pattern_findings(
                    self.project_root,
                    requirement,
                    include_paths=[architecture_rel],
                )
                for finding in findings:
                    req_id = str(requirement.get("id", "")).strip() or "(unknown requirement)"
                    pattern = str(finding.get("pattern", "")).strip()
                    errors.append(
                        f"{architecture_rel} violates {req_id} forbidden_patterns: {pattern}"
                    )
        if not errors:
            return None
        bullets = "\n".join(f"- {item}" for item in errors)
        return (
            "The architecture document failed validation. Rewrite the file in place, preserve the exact "
            "required headings, and align it with active requirements_trace.json constraints.\n"
            f"{bullets}"
        )

    def _readme_validation_feedback(self, _: AgentResult) -> Optional[str]:
        path = self.project_root / "README.md"
        content = read_text(path).strip()
        if not content or content == f"# {self.config.project_name}":
            return "The README was not updated. Rewrite README.md in place with real project documentation."

        headings = [line.strip() for line in content.splitlines() if line.strip().startswith("#")]
        if len(headings) < 4:
            return (
                "The README is too thin. Add distinct markdown sections for overview, architecture, and usage, "
                "plus at least one more practical section."
            )
        if "```" not in content:
            return "The README must include at least one fenced code block with practical commands."
        return None

    def _apply_generated_verification_config(self) -> None:
        payload = load_task_plan(self.project_root)
        raw_steps = payload.get("verification_steps", [])
        steps: List[VerificationStep] = []
        if isinstance(raw_steps, list):
            steps = [
                VerificationStep.from_dict(dict(item))
                for item in raw_steps
                if isinstance(item, dict)
            ]
        if steps:
            steps = expand_pytest_directory_steps(steps, self.project_root)
            if not self.config.gates.allow_agent_updates:
                return
            try:
                commands = commands_from_verification_steps(steps, self.project_root)
            except ValueError as error:
                raise RuntimeError(f"generated verification steps are invalid:\n- {error}") from error
            errors = validate_verification_command_paths(
                commands,
                self.project_root,
                "task plan verification_steps",
            )
            if errors:
                bullets = "\n".join(f"- {item}" for item in errors)
                raise RuntimeError(f"generated verification steps are invalid:\n{bullets}")
            if self.config.gates.steps == steps and self.config.gates.commands == commands:
                return
            self.config.gates.steps = steps
            self.config.gates.commands = commands
            save_project_config(self.project_root, self.config)
            return
        commands = payload.get("verification_commands", [])
        if not isinstance(commands, list) or not commands:
            return
        if not self.config.gates.allow_agent_updates:
            return
        normalized = [str(item).strip() for item in commands if str(item).strip()]
        if not normalized:
            return
        errors = validate_verification_command_paths(
            normalized,
            self.project_root,
            "task plan verification_commands",
        )
        if errors:
            bullets = "\n".join(f"- {item}" for item in errors)
            raise RuntimeError(f"generated verification commands are invalid:\n{bullets}")
        if self.config.gates.commands == normalized:
            return
        self.config.gates.commands = normalized
        save_project_config(self.project_root, self.config)

    @staticmethod
    def _parse_review_decision(response: str) -> Tuple[str, str]:
        lines = [line.strip() for line in response.splitlines() if line.strip()]
        if not lines:
            return "fail", "Empty review response"
        for index, line in enumerate(lines):
            normalized = line.lower()
            if normalized == "decision: pass":
                summary = "\n".join(lines[index + 1 :]).strip()
                if summary:
                    return "pass", summary
                fallback = "\n".join(lines[:index]).strip()
                return "pass", fallback or "Review passed."
            if normalized == "decision: fail":
                summary = "\n".join(lines[index + 1 :]).strip()
                if summary:
                    return "fail", summary
                fallback = "\n".join(lines[:index]).strip()
                return "fail", fallback or "Review failed."
        return "fail", response.strip()

    @staticmethod
    def _has_explicit_review_decision(response: str) -> bool:
        lines = [line.strip().lower() for line in response.splitlines() if line.strip()]
        return any(line in {"decision: pass", "decision: fail"} for line in lines)

    def status(self) -> Dict[str, object]:
        state = load_run_state(self.project_root)
        return {
            "run_id": state.run_id,
            "status": state.status,
            "current_stage": state.current_stage,
            "pending_approval": state.pending_approval,
            "approved_gates": state.approved_gates,
            "agent_attempts": state.agent_attempts,
            "last_error": state.last_error,
            "tasks": [task.to_dict() for task in state.tasks],
            "changed_files": changed_files(self.project_root) if is_repo(self.project_root) else "",
        }

    def validate(self) -> Dict[str, object]:
        return validation_report(self.project_root)

    def run_provider_research(self, spec_file: Path) -> RunState:
        state = load_run_state(self.project_root)
        state = self._run_provider_research(state, spec_file)
        save_run_state(self.project_root, state)
        return state

    def audit_requirements(self) -> Dict[str, object]:
        state = load_run_state(self.project_root)
        tasks = state.tasks or self._load_tasks_from_plan()
        result = run_requirements_audit(self.project_root, tasks)
        return {
            "ok": bool(result["ok"]),
            "path": str(result["path"]),
            "summary": str(result["report"]),
        }

    def _pending_stages(self, state: RunState) -> List[str]:
        pending: List[str] = []
        completed = set(state.stage_summaries.keys())
        for stage in STAGE_ORDER:
            if stage == "implement":
                if state.rejected_stage == "implement" and state.rejection_reason:
                    pending.append(stage)
                    continue
                if not state.tasks:
                    pending.append(stage)
                    continue
                if any(task.status != "done" for task in state.tasks):
                    pending.append(stage)
                    continue
            elif stage == "verify":
                if stage not in completed:
                    pending.append(stage)
                    continue
                if self._verify_stage_failed(state):
                    pending.append(stage)
                    continue
            elif stage not in completed:
                pending.append(stage)
        return pending

    @staticmethod
    def _stage_summary_result(summary: str) -> str:
        match = re.search(r"^Result:\s*(pass|fail)\s*$", str(summary or ""), re.IGNORECASE | re.MULTILINE)
        if not match:
            return ""
        return match.group(1).lower()

    def _verify_stage_failed(self, state: RunState) -> bool:
        return self._stage_summary_result(state.stage_summaries.get("verify", "")) == "fail"

    def _commit_if_dirty(self, message: str) -> None:
        if not is_repo(self.project_root):
            return
        if not changed_files(self.project_root):
            return
        commit_all(self.project_root, message)

    def _commit_planning_baseline_if_needed(self, tasks: Iterable[TaskSpec]) -> None:
        changes = changed_files(self.project_root)
        if not changes:
            return
        # Skip if any task is already in progress (mid-execution resume).
        # Done tasks from previous iterations are fine — we still want to
        # commit the planning baseline for the new pending tasks.
        task_list = list(tasks)
        if any(task.status not in ("pending", "done") for task in task_list):
            return

        is_iteration = any(task.status == "done" for task in task_list)

        allowed = {".gitignore", "README.md", "spec.md"}
        if self._active_spec_file is not None:
            try:
                allowed.add(str(self._active_spec_file.relative_to(self.project_root)))
            except ValueError:
                pass

        only_known = True
        for line in changes.splitlines():
            path = line[3:].strip()
            if not path:
                continue
            if path.startswith(".auto-agents/"):
                continue
            if path in allowed:
                continue
            only_known = False
            break

        if only_known:
            # All changes are planning artifacts — commit everything.
            commit_all(self.project_root, "docs(project): capture planning baseline")
        elif is_iteration:
            # Iteration: repo has non-planning changes (e.g. from agents
            # touching project files).  Stage and commit only .auto-agents/
            # so that implement's clean-tree check can pass.
            from .git_ops import _git
            _git(self.project_root, "add", ".auto-agents/")
            for extra in allowed:
                extra_path = self.project_root / extra
                if extra_path.exists():
                    _git(self.project_root, "add", extra)
            _git(self.project_root, "commit", "-m", "docs(project): capture iteration planning baseline")

    def _should_resume_task(self, state: RunState, task: TaskSpec) -> bool:
        if task.status != "pending":
            return False
        if not changed_files(self.project_root):
            return False
        attempt_key = f"implement-{task.task_id}"
        return state.agent_attempts.get(attempt_key, 0) > 0
