from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, TextIO, Tuple

from .adapters import CodexAdapter, CopilotCliAdapter, MockAdapter, ShellAdapter
from .config import (
    bootstrap_project,
    docs_dir,
    load_project_config,
    load_run_state,
    load_task_plan,
    provider_references_dir,
    provider_references_lock_path,
    requirements_audit_path,
    requirements_trace_path,
    review_path,
    run_artifact_paths,
    save_project_config,
    save_run_state,
    save_task_plan,
    task_plan_path,
    write_run_prompt,
)
from .gates import extract_failure_ids, run_commands, run_commands_collect_all
from .git_ops import changed_entries, changed_files, changed_paths, commit_all, ensure_repo, hard_reset_clean, head_ref, is_repo, require_clean_tree, worktree_fingerprint
from .io_utils import read_text, write_text
from .models import (
    APPROVAL_ORDER,
    APPROVAL_BY_STAGE,
    AgentResult,
    AgentRequest,
    AgentUsage,
    DOCUMENT_LANGUAGE_OPTIONS,
    ProviderConfig,
    ProjectConfig,
    RunState,
    STAGE_ORDER,
    TaskSpec,
)
from .requirements import (
    external_doc_requirements,
    format_requirement_context,
    load_provider_references_lock,
    load_requirements_trace,
    provider_reference_status,
    run_requirements_audit,
    requirements_for_task,
    validate_requirements_trace_payload,
)
from .validation import (
    validate_required_document,
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
        self._print_agent_output = False
        self._active_spec_file: Optional[Path] = None
        self._user_input_fn = user_input_fn
        self._allow_dirty_tree = False
        # Run-level failover memory (in-memory only, never persisted)
        self._last_successful_provider: Optional[str] = None
        self._failed_providers: Set[str] = set()
        self._current_provider: str = self.config.active_provider

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
                        profile_map={"balanced": "m", "deep": "h", "max": "xh"},
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
        state.pending_approval = ""
        state.status = "pending"
        state.current_stage = target_stage
        state.last_error = ""

    def _normalize_legacy_requirements_audit_resume(self, state: RunState) -> bool:
        last_error = state.last_error.strip()
        if not last_error.startswith("requirements audit failed:"):
            return False
        has_stale_verify = "verify" in state.stage_summaries
        has_stale_audit = "requirements_audit" in state.stage_summaries
        if not (has_stale_verify or has_stale_audit):
            return False
        self._rewind_state_from_stage(state, "verify")
        state.rejected_stage = ""
        state.rejection_reason = ""
        return True

    @staticmethod
    def _audit_issue_route(blocker: Dict[str, object]) -> Tuple[Optional[str], str]:
        kind = str(blocker.get("kind", "")).strip()
        message = str(blocker.get("message", "")).strip() or "requirements audit blocker"
        if kind == "forbidden_pattern":
            return "implement", ""
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
        return None, f"{message}; no automatic recovery route is defined for blocker kind '{kind or 'unknown'}'"

    @staticmethod
    def _audit_blocker_feedback(blocker: Dict[str, object]) -> str:
        kind = str(blocker.get("kind", "")).strip()
        if kind == "forbidden_pattern":
            path = str(blocker.get("path", "")).strip() or "unknown path"
            return f"forbidden pattern found in {path}"
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

    def _build_requirements_audit_feedback(self, audit_result: Dict[str, object], target_stage: str) -> str:
        report_path = str(audit_result.get("path", requirements_audit_path(self.project_root)))
        lines = [
            f"The requirements audit failed. Use {report_path} as the source of truth.",
            f"Recovery route: rerun from {target_stage}.",
            "Address every failing mandatory requirement before continuing.",
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
        self._print_agent_output = print_agent_output
        self._allow_dirty_tree = allow_dirty_tree
        try:
            if provider_kind is not None:
                self._set_active_provider(provider_kind)
            if doc_language is not None:
                self._set_document_language(doc_language)
            state = load_run_state(self.project_root)
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
            self._ensure_preconditions(state, spec_file=spec_file, skip_validate=skip_validate)

            if state.status == "completed":
                print("Project execution is already completed. Do you want to start a new iteration for further development? [y/N]", file=sys.stderr)
                user_conf = self._prompt_user("").strip().lower()
                if user_conf in ("y", "yes"):
                    state.run_id = uuid.uuid4().hex[:12]
                    state.status = "pending"
                    state.current_stage = "clarify"
                    for s in ["clarify", "design", "plan", "provider_research", "implement", "verify", "readme"]:
                        state.stage_summaries.pop(s, None)
                    state.approved_gates = []
                    state.agent_attempts = {}
                    state.task_review_cache = {}
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

            state.status = "completed"
            save_run_state(self.project_root, state)
            self._commit_if_dirty("chore: finalize run state")
            return state
        finally:
            self._print_agent_output = False
            self._active_spec_file = None
            self._allow_dirty_tree = False

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
        if config.provider.kind == "mock":
            return MockAdapter()
        return ShellAdapter(config.provider)

    def _run_agent_stage(self, stage: str, state: RunState, spec_file: Path, auto_approve: bool = False) -> RunState:
        if stage == "clarify":
            if auto_approve:
                # If auto_approve is on, skip conversation and just do a single-shot generation
                pass
            else:
                return self._run_interactive_clarify(state, spec_file)

        is_iteration = any(t.status == "done" for t in state.tasks)
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
        result = self._run_agent_with_retries(
            state=state,
            stage=stage,
            stage_key=stage,
            prompt=prompt,
            validation_feedback=validator,
            effort=effort,
        )
        state.current_stage = stage
        state.stage_summaries[stage] = result.summary.strip()
        state.last_error = ""
        if stage == "plan":
            self._apply_generated_verification_config()
            state.tasks = self._load_tasks_from_plan()
            self._emit_plan_task_count(state.tasks)
        return state

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
            print("\nAgent is thinking, please wait...", file=sys.stderr, flush=True)

        if resume_to_confirm:
            # Show the agent's last reply (minus the marker) and re-ask.
            last_agent_content = ""
            for msg in reversed(history):
                if isinstance(msg, dict) and str(msg.get("role", "")).lower() in ("agent", "assistant"):
                    last_agent_content = str(msg.get("content", ""))
                    break
            display = last_agent_content.replace("READY_TO_GENERATE", "").strip()
            if display:
                print("\n[Resuming previous conversation]", file=sys.stderr)
                print("\nAgent:", file=sys.stderr)
                print(display, file=sys.stderr)
            print("\nAgent is ready to generate project_brief.md.", file=sys.stderr)
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
                    print("\n[Resuming previous conversation]", file=sys.stderr)
                    print("\nAgent:", file=sys.stderr)
                    print(replay_msg["content"], file=sys.stderr)
                    user_reply = self._prompt_user("\nYour reply: ", multiline=True)
                    if user_reply.strip():
                        history.append({"role": "user", "content": user_reply})
                    else:
                        history.append({"role": "user", "content": "I have nothing to add. Please proceed to generate if you are ready."})
                write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))

        if not confirmed_generation:
            print("Entering interactive clarify session, please wait for the agent to analyze the spec...", file=sys.stderr, flush=True)

            max_rounds = 15
            rounds = 0

            while rounds < max_rounds:
                rounds += 1
                prompt_lines = [
                    f"Project root: {self.project_root}",
                    "Read the input spec from: " + str(spec_file),
                    "Clarify will later generate both project_brief.md and .auto-agents/state/requirements_trace.json.",
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

                if "READY_TO_GENERATE" in reply and not post_rejection:
                    display_reply = reply.replace("READY_TO_GENERATE", "").strip()
                    if display_reply:
                        print("\nAgent:", file=sys.stderr)
                        print(display_reply, file=sys.stderr)
                    print("\nAgent is ready to generate project_brief.md.", file=sys.stderr)
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

                print("\nAgent:", file=sys.stderr)
                print(display_reply, file=sys.stderr)

                user_reply = self._prompt_user("\nYour reply: ", multiline=True)

                if user_reply.strip():
                    history.append({"role": "user", "content": user_reply})
                else:
                    history.append({"role": "user", "content": "I have nothing to add. Please proceed to generate if you are ready."})

                write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))
                print("\nAgent is thinking, please wait...", file=sys.stderr, flush=True)

        if not confirmed_generation:
            raise RuntimeError(
                "Clarify ended without explicit confirmation to generate project_brief.md."
            )

        # Generate the actual project brief
        print("\nGenerating project_brief.md, please wait...", file=sys.stderr, flush=True)
        is_iteration = any(t.status == "done" for t in state.tasks)
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
                print(prompt + " (Press Ctrl+D or Ctrl+Z to submit):", file=sys.stderr)
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

    def _implement_touched_code(self) -> bool:
        """Return True if the last implement step touched any non-orchestrator file."""
        try:
            paths = changed_paths(
                self.project_root,
                ignored_prefixes=(".auto-agents/",),
            )
        except TypeError:
            paths = [p for p in changed_paths(self.project_root) if not p.startswith(".auto-agents/")]
        return bool(paths)

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
        tasks = state.tasks or self._load_tasks_from_plan()
        
        if state.rejected_stage == "implement" and state.rejection_reason:
            import time
            tasks.append(
                TaskSpec(
                    task_id=f"fix-rejection-{int(time.time()*1000)}",
                    title="Fix issues after release rejection",
                    description=f"The release was rejected with the following feedback:\n{state.rejection_reason}\n\nPlease fix these issues.",
                    acceptance=[
                        "Feedback is fully addressed",
                        "Tests pass"
                    ]
                )
            )
            state.rejected_stage = ""
            state.rejection_reason = ""
            
        state.tasks = tasks
        self._commit_planning_baseline_if_needed(tasks)

        processed = 0
        for task in tasks:
            if task.status == "done":
                continue
            if max_tasks is not None and processed >= max_tasks:
                break

            resume_existing = task.status == "in_progress" or self._should_resume_task(state, task)
            allow_dirty_retry = task.status == "blocked"
            if (resume_existing or allow_dirty_retry) and task.status != "in_progress":
                task.status = "in_progress"
                self._persist_tasks(tasks)

            if (
                not (resume_existing or allow_dirty_retry or self._allow_dirty_tree)
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
                if gate_result.get("rewind_to_plan"):
                    rewind_state = self._handle_scope_overflow_rewind(
                        state, task, tasks, gate_result
                    )
                    if rewind_state is not None:
                        return rewind_state
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
            if self.config.git.commit_each_task:
                task.commit_sha = commit_all(self.project_root, commit_message)
            processed += 1

        state.tasks = tasks
        state.current_stage = "implement"
        state.stage_summaries["implement"] = f"Completed {sum(task.status == 'done' for task in tasks)} tasks."
        state.last_error = ""
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

        baseline_ref = task.verify_baseline_ref or state.stage_summaries.get("implement_baseline_ref", "")
        if baseline_ref:
            hard_reset_clean(self.project_root, baseline_ref)

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
            require_clean_tree(self.project_root)
        except RuntimeError as error:
            if str(error) != "working tree is not clean":
                raise

            changed = changed_paths(self.project_root)
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

    def _run_task_verify(self, task: Optional[TaskSpec] = None) -> Dict[str, object]:
        quick_failure = self._quick_verify_failure()
        if quick_failure:
            return {
                "ok": False,
                "reason": quick_failure,
                "failure_ids": self._normalize_verify_failure_ids([], quick_failure),
                "current_failure_ids": self._normalize_verify_failure_ids([], quick_failure),
                "baseline_failure_ids": list(task.verify_baseline_failures) if task is not None else [],
                "new_failure_ids": self._normalize_verify_failure_ids([], quick_failure),
                "raw_output": quick_failure,
            }
        verify_gate = (
            run_commands_collect_all(self.config.gates.commands, self.project_root)
            if task is not None
            else run_commands(self.config.gates.commands, self.project_root)
        )
        current_failure_ids = self._normalize_verify_failure_ids(
            extract_failure_ids(verify_gate),
            verify_gate.summary,
        )
        baseline_failure_ids = (
            self._normalize_verify_failure_ids(task.verify_baseline_failures, verify_gate.summary)
            if task is not None and task.verify_baseline_failures
            else []
        )
        new_failure_ids = sorted(set(current_failure_ids) - set(baseline_failure_ids))
        if task is not None and task.expected_test_migrations:
            allowed_migrations = {str(item) for item in task.expected_test_migrations}
            new_failure_ids = [fid for fid in new_failure_ids if fid not in allowed_migrations]
        raw_output = self._gate_raw_output(verify_gate)
        if task is not None and current_failure_ids and not new_failure_ids:
            return {
                "ok": True,
                "reason": f"task baseline only: {len(current_failure_ids)} pre-existing failure(s) remain",
                "failure_ids": [],
                "current_failure_ids": current_failure_ids,
                "baseline_failure_ids": baseline_failure_ids,
                "new_failure_ids": [],
                "raw_output": raw_output,
            }
        if not verify_gate.ok:
            effective_failure_ids = new_failure_ids or current_failure_ids
            if task is not None and new_failure_ids:
                reason = (
                    f"{len(new_failure_ids)} new verification failure(s) vs task baseline: "
                    + ", ".join(new_failure_ids[:10])
                )
            else:
                reason = verify_gate.summary
            return {
                "ok": False,
                "reason": reason,
                "failure_ids": effective_failure_ids,
                "current_failure_ids": current_failure_ids,
                "baseline_failure_ids": baseline_failure_ids,
                "new_failure_ids": new_failure_ids or effective_failure_ids,
                "raw_output": raw_output,
            }
        return {
            "ok": True,
            "reason": verify_gate.summary,
            "failure_ids": [],
            "current_failure_ids": current_failure_ids,
            "baseline_failure_ids": baseline_failure_ids,
            "new_failure_ids": [],
            "raw_output": raw_output,
        }

    def _task_verify_baseline_ref(self) -> str:
        return f"{head_ref(self.project_root)}:{worktree_fingerprint(self.project_root)}"

    def _ensure_task_verify_baseline(self, task: TaskSpec) -> bool:
        baseline_ref = self._task_verify_baseline_ref()
        if task.verify_baseline_ref == baseline_ref:
            return False
        task.verify_baseline_ref = baseline_ref
        if not self.config.gates.commands:
            task.verify_baseline_failures = []
            return True
        gate = run_commands_collect_all(self.config.gates.commands, self.project_root)
        task.verify_baseline_failures = self._normalize_verify_failure_ids(
            extract_failure_ids(gate),
            gate.summary,
        )
        return True

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
        summary_lines: List[str] = []
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
        raw_excerpts = self._extract_verify_excerpts(raw_output)
        if not raw_excerpts and reason:
            raw_excerpts = [self._truncate_feedback_text(reason, limit=900)]
        return {
            "verification_summary": "\n".join(summary_lines).strip(),
            "implicated_paths": implicated_paths,
            "raw_excerpts": raw_excerpts,
        }

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

    def _run_task_review(self, run_id: str, task: TaskSpec, verify_reason: str = "") -> Dict[str, object]:
        review_effort = self._review_effort_for_task(task)
        review_prompt = self._build_task_prompt(
            task,
            "review",
            review_context=self._build_review_context(verify_reason=verify_reason),
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

        paths = changed_paths(self.project_root)
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

    def _build_review_context(self, verify_reason: str = "", max_diff_chars: int = 20000) -> str:
        entries = changed_entries(self.project_root)
        lines = [
            "Review the current task by prioritizing the diff context below before exploring unrelated files.",
        ]
        if verify_reason.strip():
            lines.extend(["Local verification summary:", verify_reason.strip()])
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

    def _quick_verify_failure_details(self) -> Optional[Tuple[str, bool]]:
        conda_meta = self.project_root / ".conda" / "conda-meta"
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
            self.config.gates.commands,
            self.project_root,
            "gates.commands",
        )
        if command_path_errors:
            return command_path_errors[0], False

        for command in self.config.gates.commands:
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

    def _quick_verify_failure(self) -> Optional[str]:
        failure = self._quick_verify_failure_details()
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
        verify_gate = run_commands(self.config.gates.commands, self.project_root)
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
            self._emit_stage_verify_result("fail", summary.strip())
            raise RuntimeError("verify stage failed")
        tasks = state.tasks or self._load_tasks_from_plan()
        state.tasks = tasks
        audit_result = run_requirements_audit(self.project_root, tasks)
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
        return state

    def _load_tasks_from_plan(self) -> List[TaskSpec]:
        payload = load_task_plan(self.project_root)
        tasks = [TaskSpec.from_dict(item) for item in payload.get("tasks", [])]
        if not tasks:
            raise RuntimeError(f"No tasks found in {task_plan_path(self.project_root)}")
        return tasks

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
            print("Entering README preparation, please wait for the agent to analyze the project...", file=sys.stderr, flush=True)

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

            print("\nAgent:", file=sys.stderr)
            print(last_agent_msg, file=sys.stderr)

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

            print("\nAgent is updating the plan, please wait...", file=sys.stderr, flush=True)
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
        print("\nGenerating README.md, please wait...", file=sys.stderr, flush=True)
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
        if isinstance(current_payload.get("test_strategy"), str) and current_payload["test_strategy"].strip():
            next_payload["test_strategy"] = current_payload["test_strategy"].strip()
        verification_commands = current_payload.get("verification_commands")
        if isinstance(verification_commands, list) and verification_commands:
            next_payload["verification_commands"] = [
                str(item).strip() for item in verification_commands if str(item).strip()
            ]
        save_task_plan(self.project_root, next_payload)

    def _stage_output_path(self, run_id: str, stage: str) -> Path:
        _, output_path = run_artifact_paths(self.project_root, run_id, stage)
        return output_path

    def _build_prompt(self, stage: str, spec_file: Path, is_iteration: bool = False) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        plan = task_plan_path(self.project_root)
        requirements_trace = requirements_trace_path(self.project_root)
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
                f"Generate or update the requirements trace in place: {requirements_trace}",
                "Keep the brief compact and focused on the target scope.",
                "Preserve the exact top-level and section headings already present in the file.",
                "The requirements trace is the downstream execution contract. It must be valid JSON with version=1 and a requirements list.",
                "Every active requirement must have id, text, source, status, priority, acceptance_oracles, forbidden_patterns, external_docs_required, provider_reference, and notes fields.",
                "Use stable IDs like REQ-001. Mark hard requirements as priority='mandatory'. Use status='active', 'deferred', or 'superseded'.",
                "If a requirement needs an external provider protocol or official API docs, set external_docs_required=true and provider_reference to a local path under .auto-agents/docs/provider_references/.",
                self._clarify_spec_instruction(spec_kind),
                self._document_language_instruction(),
            ]
            if is_iteration:
                lines.extend([
                    f"This is an ITERATION run. The project already has completed work and an existing brief at {brief}.",
                    "IMPORTANT: Do NOT discard or rewrite the existing content of the brief.",
                    "ADD or UPDATE sections relevant to the new iteration scope while preserving existing content.",
                    "Extend existing sections in place rather than appending a separate duplicate block at the end.",
                    f"Review the existing task plan at {task_plan_path(self.project_root)} to understand what has already been completed.",
                ])
            lines.append("Final response: 3 short bullets summarizing the clarified scope.")
            return "\n".join(lines)

        if stage == "design":
            lines = common + [
                f"Read the input spec: {spec_file}",
                f"Read the current project brief: {brief}",
                f"Update this file in place: {architecture}",
                "Record only top-level architecture decisions and major risks.",
                "Preserve the exact top-level and section headings already present in the file.",
                self._design_spec_instruction(spec_kind),
                self._document_language_instruction(),
            ]
            if is_iteration:
                lines.extend([
                    f"This is an ITERATION run. The project already has completed work and an existing architecture at {architecture}.",
                    "IMPORTANT: Do NOT discard or rewrite the existing architecture decisions.",
                    "ADD or UPDATE sections relevant to the new iteration scope while preserving existing content.",
                    f"Review the existing task plan at {task_plan_path(self.project_root)} to understand what has already been completed.",
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
                "At the root of the JSON, also define test_strategy and verification_commands.",
                "Every new non-done task must include requirement_ids listing the requirements it covers.",
                "All active mandatory requirements in requirements_trace.json must be covered by at least one task requirement_ids entry unless the requirement is explicitly deferred or superseded.",
                "Task acceptance criteria must preserve the bound requirement's concrete acceptance_oracles; do not weaken direct/API/protocol requirements into naming or configuration-only checks.",
                "If a requirement has external_docs_required=true, create at least one implementation task that consumes its provider_reference and tests against that protocol reference.",
                "Choose the smallest practical automated verification strategy for this stack.",
                "If this is a Python project, require a project-local conda env at ./.conda.",
                "Choose the number of tasks based on project complexity rather than an arbitrary cap.",
                "Keep each task small enough to implement, review, and verify independently, but do not split into trivial housekeeping-only tasks.",
                "Avoid oversized tasks that bundle multiple loosely related features together.",
                "Prefer tasks that each deliver one coherent, testable capability or technical slice.",
                "For Python verification, every Python-oriented entry in verification_commands must run inside that env via 'conda run -p ./.conda ...'.",
                "Do not include bare 'python', 'python3', 'pytest', 'coverage', or 'pip' commands in verification_commands for Python projects.",
                "For Python verification, prefer checking './.conda/conda-meta' and then running 'conda run -p ./.conda python -m unittest discover -s tests' unless another repository-local command is clearly better.",
                "For non-Python projects, keep all dependency installation and tooling local to the repository and avoid global installs.",
                self._plan_spec_instruction(spec_kind),
                self._plan_language_instruction(),
                "Review .auto-agents/state/task_plan.json if it exists. DO NOT overwrite or delete existing completed tasks. APPEND new tasks to the end of the JSON array for the new features.",
                "When existing completed tasks are present, cross-reference the brief and architecture against those done tasks to identify ONLY the scope not yet covered. Do NOT create tasks for capabilities already delivered by completed tasks.",
                "CRITICAL — COVERAGE VERIFICATION: when determining whether a done task covers a brief requirement, you MUST compare the requirement against the task's ACCEPTANCE CRITERIA and REVIEW SUMMARY, not its title or description alone. A task titled 'Real X Integration' does NOT cover a requirement for actual real-model output if its acceptance criteria only verify adapter switching, infrastructure patterns, or fixture/stub results rather than actual external API calls producing real output.",
                "If the brief explicitly states that a capability must be 'real' / 'production' / '真实' / '公网', verify that the done task's acceptance criteria confirm actual external API calls producing real output — not just adapter infrastructure or fixture-based testing.",
                "Before generating the task list, produce a COVERAGE ANALYSIS in your final summary response (NOT in the JSON file): for each key requirement in the brief's current iteration scope, state which done task covers it (citing the specific acceptance criterion that proves delivery) or mark it as UNCOVERED. Any UNCOVERED requirement MUST result in a new task.",
                "Each task must contain task_id, title, description, acceptance, status, commit_message.",
                "A good plan may contain only a few tasks for a small target or many tasks for a broad target, as long as the slicing remains disciplined.",
                "",
                "TASK SPLITTING — ANTI-PATTERNS (avoid these):",
                "1. God Task: a single task with >5 acceptance criteria or a description exceeding ~500 characters. Split by feature slice.",
                "2. Cross-cutting Bundle: acceptance criteria that span multiple unrelated subsystems (e.g. 'set up DB schema AND implement API AND write CLI'). Each subsystem should be its own task.",
                "3. Infra + Feature Combo: mixing infrastructure setup (dependencies, CI, env config) with business logic in one task. Split infra into a prerequisite task.",
                "4. Vague Acceptance: criteria like 'code is clean' or 'follows best practices'. Every criterion must be objectively verifiable by a test or a command.",
                "5. False Coverage: concluding a done task covers a new requirement based on its title, while its acceptance criteria only verify infrastructure, adapters, or fixture results — not the actual capability the brief demands. Always verify coverage by reading acceptance criteria, not titles. Especially dangerous when the brief uses terms like 'real' / 'production' / '真实' / '公网' — these signal that adapter-level or fixture-level delivery is insufficient.",
                "",
                "TASK SPLITTING — STRATEGIES:",
                "1. Vertical Slice: each task delivers one user-facing or API-facing capability end-to-end (route, logic, test).",
                "2. Dependency-first: extract shared setup (DB schema, env, config) into an early task, then layer feature tasks on top.",
                "3. Test-boundary: if a single task would require tests in 3+ unrelated test files, it likely needs splitting.",
                "4. Acceptance count rule of thumb: aim for 2-4 acceptance criteria per task. >5 is a strong signal to split.",
                "",
                "Final response: 3 short bullets summarizing the plan.",
            ]
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

    def _build_task_prompt(self, task: TaskSpec, stage: str, review_context: str = "") -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        task_json = json.dumps(task.to_dict(), indent=2, ensure_ascii=False)
        requirement_context = format_requirement_context(requirements_for_task(self.project_root, task))
        common = [
            f"Project root: {self.project_root}",
            f"Project brief: {brief}",
            f"Architecture: {architecture}",
            f"Requirements trace: {requirements_trace_path(self.project_root)}",
            "Work only on the current task.",
            "Keep changes scoped and testable.",
            f"Task JSON:\n{task_json}",
        ]
        if requirement_context:
            common.extend(["", requirement_context])

        if stage == "implement":
            lines = common + [
                "Implement only this feature slice.",
                "If local verification exposes a tightly coupled regression in files you touched or in paths explicitly implicated by retry feedback, fix it in the same attempt even if it sits slightly outside the nominal task slice.",
                "The bound requirements and acceptance oracles above are hard requirements, not optional background.",
                "If Task JSON and bound requirements conflict, preserve the bound requirements and mention the conflict in the final summary.",
                "You MUST also write or update tests that verify the acceptance criteria in the Task JSON.",
                "Tests should validate observable behavior (API contracts, input/output, side-effects), not internal implementation details.",
                "For external provider integrations, use the listed provider_reference files as the source of truth. Do not search for alternate docs or invent protocol details unless the reference is marked insufficient; stop and report missing documentation instead.",
                "For protocol/direct-integration tasks, add contract tests that verify outbound request shape, auth/header behavior, response normalization, and forbidden legacy payloads where applicable.",
                "If this is a Python project, create and use a project-local conda env at ./.conda and install packages only inside it.",
                "Do not use '.conda' as a generic directory, pip target, virtualenv, or venv path. It must remain a real conda prefix created with 'conda create -p ./.conda ...', including '.conda/conda-meta'.",
                "For any other stack, keep dependencies and tool state local to the repository and never rely on global installs.",
                "Do not modify .auto-agents state files except when explicitly requested.",
                "Final response: 3 short bullets describing what changed.",
            ]
            return "\n".join(lines)

        if stage == "review":
            lines = common + [
                "Review the current uncommitted changes for correctness, regressions, and missing tests.",
                "The bound requirements and acceptance oracles above are in scope. A task passes only if both Task JSON and the bound requirement oracles are satisfied.",
                "TEST AUDIT: Separately evaluate whether the tests truly cover the acceptance criteria "
                "from the Task JSON. Check that tests validate observable behavior (API contracts, "
                "input/output, side-effects) rather than internal implementation details. "
                "If the tests only pass by mocking/faking internal state instead of exercising real "
                "public interfaces, that is a 'DECISION: fail' issue.",
                "SCOPE RULE: Your review scope is bounded by the acceptance criteria in the Task JSON plus the bound requirements and acceptance oracles above. "
                "A 'DECISION: fail' is warranted ONLY when the implementation does not satisfy one or more "
                "task acceptance criteria, bound requirement oracles, introduces a regression in existing tests, or leaves the codebase in a "
                "non-buildable state. Architectural concerns, future robustness improvements, and suggestions "
                "beyond the stated acceptance criteria and bound requirements should be noted as '[NON-BLOCKING]' advisory notes, "
                "NOT as failure reasons.",
                "When issuing 'DECISION: fail', you MUST cite the specific acceptance criterion (by index or text) "
                "or requirement ID/oracle that is not satisfied. If no acceptance criterion or requirement oracle is violated but you have advisory concerns, "
                "issue 'DECISION: pass' with those concerns listed as '[NON-BLOCKING]' notes.",
                "For external provider integrations, verify the code and tests against the provider_reference file. Fail if the implementation invents protocol fields, reuses a legacy private gateway payload, or tests only mock an internal gateway contract.",
                "Use the supplied changed-file and diff context first. Only inspect the rest of the repository when the diff is insufficient.",
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
            return "\n".join(lines)

        raise RuntimeError(f"Unsupported task stage: {stage}")

    def _execute_task_with_retries(
        self,
        state: RunState,
        task: TaskSpec,
        resume_existing: bool = False,
    ) -> Dict[str, object]:
        max_attempts = self._max_attempts("implement")
        feedback = ""
        if task.review_summary.strip():
            feedback = self._format_retry_feedback(
                "review_rejected",
                reason="review rejected the task",
                review_history=task.review_history,
                review_summary=task.review_summary,
            )
        last_reason = "task failed without a recorded reason"
        last_review = ""

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
                implement_prompt = self._build_task_prompt(task, "implement")
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

                if not self._implement_touched_code():
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
            quick_failure = self._quick_verify_failure_details()
            if quick_failure:
                last_reason, retryable = quick_failure
                failure_ids = self._normalize_verify_failure_ids([], last_reason)
                verify_analysis = self._analyze_verify_failure(task, failure_ids)
                verify_stats = str(verify_analysis["stats"])
                self._record_verify_result(task, attempt, "fail", last_reason, failure_ids)
                feedback = self._format_retry_feedback(
                    "pre_verify_check",
                    reason=last_reason,
                )
                self._emit_task_verify_result(task, "fail", last_reason, stats=verify_stats)
                if not retryable:
                    break
                if bool(verify_analysis["stop_retry"]):
                    last_reason = self._format_repeated_verify_failure_reason(
                        last_reason,
                        first_attempt=verify_analysis["first_attempt"],
                        repeat=verify_analysis["repeat"],
                    )
                    break
                continue

            verify_result = self._run_task_verify(task)
            if not verify_result["ok"]:
                last_reason = str(verify_result["reason"])
                failure_ids = self._normalize_verify_failure_ids(
                    verify_result.get("failure_ids", []),
                    last_reason,
                )
                verify_analysis = self._analyze_verify_failure(task, failure_ids)
                verify_stats = str(verify_analysis["stats"])
                self._record_verify_result(task, attempt, "fail", last_reason, failure_ids)
                verify_feedback = self._build_verify_retry_feedback(verify_result)
                feedback = self._format_retry_feedback(
                    "local_verification",
                    reason=last_reason,
                    verification_summary=str(verify_feedback.get("verification_summary", "")),
                    implicated_paths=list(verify_feedback.get("implicated_paths", [])),
                    raw_excerpts=list(verify_feedback.get("raw_excerpts", [])),
                )
                self._emit_task_verify_result(task, "fail", last_reason, stats=verify_stats)
                if bool(verify_analysis["stop_retry"]):
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
                gate_result = self._run_task_review(state.run_id, task, verify_reason=str(verify_result["reason"]))
                if gate_result["ok"]:
                    self._store_task_review_cache(
                        state,
                        task,
                        review_fingerprint,
                        str(gate_result["review"]),
                    )
            if gate_result["ok"]:
                return gate_result

            last_reason = str(gate_result["reason"])
            last_review = str(gate_result["review"])
            
            task.review_summary = last_review
            task.review_history.append({
                "attempt": attempt,
                "summary": last_review,
            })

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

            feedback = self._format_retry_feedback(
                "review_rejected",
                reason=last_reason,
                review_history=task.review_history,
                review_summary=last_review,
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

        return {"ok": False, "review": last_review or feedback, "reason": last_reason}

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
        resolved_effort = effort or self.config.efforts.get(stage, "balanced")
        feedback = ""
        last_error = f"{stage_key} failed"
        cumulative_usage: Optional[AgentUsage] = None
        usage_available = False

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
            result = self._call_with_failover(request)
            if result.usage is not None:
                cumulative_usage = (cumulative_usage or AgentUsage()).plus(result.usage)
                usage_available = True
            self._emit_agent_output(artifact_stage, result)
            if state is not None:
                state.agent_attempts[stage_key] = attempt
                save_run_state(self.project_root, state)

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
        print("\n\n".join(sections), file=self.agent_output_stream, flush=True)

    def _emit_stage_start(self, stage: str) -> None:
        model = self._model_label_for_top_level_stage(stage)
        print(f"[stage:{stage}] start provider={self._current_provider} model={model}", file=self.agent_output_stream, flush=True)

    def _emit_stage_verify_result(self, decision: str, summary: str, route: str = "") -> None:
        header = f"[stage:verify] decision={decision}"
        if route.strip():
            header = f"{header} route={route.strip()}"
        sections = [header]
        if summary.strip():
            sections.append(summary.strip())
        print(
            "\n".join(sections),
            file=self.agent_output_stream,
            flush=True,
        )

    def _emit_plan_task_count(self, tasks: Iterable[TaskSpec]) -> None:
        task_list = list(tasks)
        print(f"[stage:plan] tasks={len(task_list)}", file=self.agent_output_stream, flush=True)

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
        print(
            (
                f"[agent:{stage_key}] completed ok={str(result.ok).lower()} "
                f"returncode={result.returncode} attempts={attempts} "
                f"provider={self._current_provider} model={model or 'unknown'} "
                f"tokens={usage_text}"
            ),
            file=self.agent_output_stream,
            flush=True,
        )

    def _emit_task_activity(self, task: TaskSpec, action: str, attempt: int) -> None:
        print(
            f"[task:{task.task_id}] {action} attempt={attempt} title={task.title}",
            file=self.agent_output_stream,
            flush=True,
        )

    def _emit_task_blocked(self, task: TaskSpec, reason: str) -> None:
        print(
            f"[task:{task.task_id}] blocked reason={reason}",
            file=self.agent_output_stream,
            flush=True,
        )

    def _emit_task_review_result(self, task: TaskSpec, decision: str, summary: str) -> None:
        sections = [f"[task:{task.task_id}] review decision={decision}"]
        if summary.strip():
            sections.append(summary.strip())
        print(
            "\n".join(sections),
            file=self.agent_output_stream,
            flush=True,
        )

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

    def _analyze_verify_failure(self, task: TaskSpec, failure_ids: List[str]) -> Dict[str, object]:
        prior_failures = [
            entry for entry in task.verify_history
            if isinstance(entry, dict) and str(entry.get("decision", "")) == "fail"
        ]
        failure_count = len(failure_ids)
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
    def _record_verify_result(
        task: TaskSpec,
        attempt: int,
        decision: str,
        summary: str,
        failure_ids: Optional[Iterable[str]] = None,
    ) -> None:
        entry: Dict[str, object] = {
            "attempt": attempt,
            "decision": decision,
            "summary": summary.strip(),
        }
        normalized_failure_ids = [str(item).strip() for item in (failure_ids or []) if str(item).strip()]
        if normalized_failure_ids:
            entry["failure_ids"] = normalized_failure_ids
        task.verify_history.append(entry)

    def _emit_task_verify_result(self, task: TaskSpec, decision: str, summary: str, stats: str = "") -> None:
        header = f"[task:{task.task_id}] verify decision={decision}"
        if stats.strip():
            header = f"{header} {stats.strip()}"
        sections = [header]
        if summary.strip():
            sections.append(summary.strip())
        print(
            "\n".join(sections),
            file=self.agent_output_stream,
            flush=True,
        )

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
                print(
                    f"[failover] provider={kind} binary not found, skipping",
                    file=self.agent_output_stream, flush=True,
                )
                tried.append(kind)
                continue

            self._current_provider = kind
            result = adapter.run(request)
            tried.append(kind)

            if result.ok:
                self._last_successful_provider = kind
                self._failed_providers.discard(kind)
                if kind != self.config.active_provider:
                    print(
                        f"[failover] using provider={kind}",
                        file=self.agent_output_stream, flush=True,
                    )
                return result

            if not self._is_failover_error(result):
                return result

            self._failed_providers.add(kind)
            snippet = (result.stderr or "")[:120]
            label = self._failover_error_label(result)
            print(
                f"[failover] provider={kind} {label} ({snippet}), trying next...",
                file=self.agent_output_stream, flush=True,
            )
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

    def _plan_validation_feedback(self, _: AgentResult) -> Optional[str]:
        payload = load_task_plan(self.project_root)
        trace = load_requirements_trace(self.project_root)
        errors = validate_task_plan_with_requirements(payload, trace)
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
        if self._has_explicit_review_decision(result.summary):
            return None
        return (
            "The review response is invalid. It must include a line exactly equal to "
            "'DECISION: pass' or 'DECISION: fail'. Rewrite the review output."
        )

    def _clarify_validation_feedback(self, _: AgentResult) -> Optional[str]:
        path = docs_dir(self.project_root) / "project_brief.md"
        errors = validate_required_document(path, "project_brief.md")
        trace = load_requirements_trace(self.project_root)
        errors.extend(validate_requirements_trace_payload(trace))
        if not errors:
            return None
        bullets = "\n".join(f"- {item}" for item in errors)
        return (
            "The clarify output is incomplete. Rewrite the project brief and requirements trace in place, "
            "preserving required brief headings and valid requirements_trace.json shape.\n"
            f"{bullets}"
        )

    def _design_validation_feedback(self, _: AgentResult) -> Optional[str]:
        path = docs_dir(self.project_root) / "architecture.md"
        errors = validate_required_document(path, "architecture.md")
        if not errors:
            return None
        bullets = "\n".join(f"- {item}" for item in errors)
        return (
            "The architecture document is missing required template headings. Rewrite the file in place "
            "and preserve the exact required headings.\n"
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
            elif stage not in completed:
                pending.append(stage)
        return pending

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
