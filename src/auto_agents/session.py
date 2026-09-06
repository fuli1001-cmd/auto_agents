from __future__ import annotations

import hashlib
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from .authorization import (
    WorkflowAuthorizationPolicy,
    authorization_policy_for_state,
    classify_assistance_request,
)
from .config import (
    create_session,
    docs_dir,
    list_sessions,
    load_run_state,
    load_session_state,
    provider_references_dir,
    provider_references_lock_path,
    requirements_trace_path,
    save_session_state,
    session_artifact_paths,
    sort_sessions,
)
from .gates import (
    build_failure_identity_diagnostic_command,
    commands_from_verification_steps,
    extract_failure_ids,
    extract_failure_info,
    run_gate_plan,
)
from .gate_execution import (
    GATE_SNAPSHOT_RUNTIME_PATHS,
    GateSnapshotManager,
    discover_dependency_links,
    repository_exclusion_paths,
)
from .git_ops import (
    changed_paths,
    commit_only_paths,
    head_ref,
    worktree_fingerprint,
)
from .io_utils import read_json, read_text, write_json, write_text
from .logging_utils import attach_run_file_logger
from .reporting import reporting_scope
from .models import (
    AgentRequest,
    AgentResult,
    SESSION_AGENT_ERROR_THRESHOLD,
    SESSION_STALL_THRESHOLD,
    GateResult,
    SessionState,
)
from .persistence import (
    PersistenceContractError,
    build_persistence_action_manifest,
    detect_persistence_schema_changes,
    execute_persistence_action,
    persistence_candidate_fingerprint,
    persistence_change_strategy,
)
from .performance_trace import PerformanceTrace
from .provider_contract import provider_policy_prompt_lines
from .prompting import (ContextBlock, PromptBlock, append_context, compose_prompt,
                        instruction_fingerprint, policy_fingerprint)
from .requirements import (
    load_requirements_trace,
    validate_provider_resolve_trace_transition,
    validate_requirements_trace_payload,
)
from .release_attestation import (
    complete_release_verification,
    enqueue_release_verification,
)
from .validation import (
    _looks_like_python_command,
    _uses_project_local_conda,
    validate_persistence_change,
)

_GOAL_CLEAR = re.compile(r"^GOAL_CLEAR\s*$", re.MULTILINE)
_NOT_A_BUG = re.compile(r"^NOT_A_BUG:\s*(.+)$", re.MULTILINE)
_NEED_USER_ASSIST = re.compile(r"^NEED_USER_ASSIST:\s*(.+)$", re.MULTILINE)
_NEED_USER_ASSIST_V1 = re.compile(
    r"^NEED_USER_ASSIST\s+v1:\s*(\{.*\})\s*$",
    re.MULTILINE,
)
_NEED_USER_DEFER = re.compile(
    r"^NEED_USER_DEFER:\s*([^|\n]+)\|\s*(.+)$",
    re.MULTILINE,
)
_REQUIRES_CLARIFY = re.compile(r"^REQUIRES_CLARIFY:\s*(.+)$", re.MULTILINE)
_REQUIREMENT_ID = re.compile(r"\bREQ-[A-Za-z0-9_-]+\b")
_BUG_FOUND = re.compile(r"^BUG_FOUND:\s*(.+)$", re.MULTILINE)
_GOAL_ACHIEVED = re.compile(r"^GOAL_ACHIEVED:\s*(.+)$", re.MULTILINE)
_GOAL_ENVIRONMENT = re.compile(
    r"^GOAL_ENVIRONMENT\s+v1:\s*(\{.*\})\s*$",
    re.MULTILINE,
)
_ROUTE_WORKFLOW = re.compile(r"^ROUTE_WORKFLOW\s+v1:\s*(\{.*\})\s*$", re.MULTILINE)
_FIX_DISPOSITION = re.compile(r"^FIX_DISPOSITION\s+v1:\s*(\{.*\})\s*$", re.MULTILINE)
_FIX_VERIFY = re.compile(r"^FIX_VERIFY:\s*(.+)$", re.MULTILINE)
_COMMIT_MESSAGE = re.compile(r"^COMMIT_MESSAGE:\s*(.+)$", re.MULTILINE)
_PERSISTENCE_CHANGE = re.compile(r"^PERSISTENCE_CHANGE:\s*(\{.*\})\s*$", re.MULTILINE)
_SHELL_CONTROL_TOKENS = re.compile(r"[|;&<>`\n]")
_ORCHESTRATOR_CONTROL_ASSISTANCE = re.compile(
    r"(?:\bauto-agents\b|\bauto_agents(?:\.py)?\b)\s+"
    r"(?:resume|collab|fix|run|health-watch|stop)\b|--no-health-watch",
    re.IGNORECASE,
)
# Version 2 makes pre-fix records ineligible after rejected-output and process
# resume boundaries became explicit continuation invalidation points.
_PROVIDER_CONTINUATION_POLICY_VERSION = 4


class Session:
    """Lightweight conversational workflow for bug fixes and recovery sessions.

    Reuses the *Orchestrator* instance for adapter calls, verification and git
    operations but bypasses the seven-stage pipeline entirely.
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",  # noqa: F821 – forward ref
        mode: str = "fix",
        print_agent_output: bool = False,
        full_verify: bool = False,
        auto_approve: bool = False,
        health_runtime: object = None,
        coordinator: object = None,
    ) -> None:
        self.orch = orchestrator
        self.project_root = orchestrator.project_root
        self.mode = mode
        self._print_agent_output = print_agent_output
        self._full_verify = bool(full_verify)
        self._auto_approve = bool(auto_approve)
        self._health_runtime = health_runtime
        self._coordinator = coordinator
        self._coordinator_managed = coordinator is not None
        self._replace_active_workflow = False
        # ``fix --full-verify`` keeps its existing session-wide semantics.
        # Collab only bypasses certificates for the final attestation; progress
        # checks must remain incremental so the interactive loop stays fast.
        self.orch._force_full_verify = bool(full_verify and mode == "fix")
        self._current_state: Optional[SessionState] = None
        # Expose the same user-input helper used by the orchestrator.
        self._prompt_user = orchestrator._prompt_user

    @property
    def config(self):
        """Always use configuration from the current lifecycle generation."""

        return self.orch.config

    def _prepare_project_config_for_supervision(self) -> bool:
        return bool(self.orch._prepare_project_config_for_supervision())

    def _supervised_worktree_snapshot(self) -> Dict[str, str]:
        self._prepare_project_config_for_supervision()
        return self.orch._worktree_change_snapshot()

    def _gate_commands(self) -> List[str]:
        if self.config.gates.steps and not self.config.gates.parallel_groups:
            return commands_from_verification_steps(
                self.config.gates.steps, self.project_root
            )
        return list(self.config.gates.commands)

    def _session_gate_plan(self, scope: str):
        """Resolve the gate plan used by a session verification scope."""
        if scope == "release" or (
            scope == "final"
            and (
                self._full_verify
                or self.config.gates.release_verification_mode == "blocking"
            )
        ):
            return self.orch._resolved_gate_plan("final", level="release")
        changed_path_set = set(changed_paths(self.project_root))
        if self._current_state is not None:
            changed_path_set.update(self._current_state.lineage_changed_paths)
        return self.orch._resolved_gate_plan(
            "implement",
            level="affected",
            changed_path_set=sorted(changed_path_set),
        )

    def _release_gate_plan(self):
        return self._session_gate_plan("release")

    def _session_persistence_issue(self, state: SessionState) -> str:
        findings = detect_persistence_schema_changes(self.project_root)
        if not findings or persistence_change_strategy(state.persistence_change) != "none":
            return ""
        evidence = "; ".join(
            f"{finding.path}: {finding.evidence}" for finding in findings[:6]
        )
        return (
            "The session changed persistent schema without a user-approved strategy: "
            f"{evidence}. Choose a persistence strategy before more writes or verification."
        )

    def _apply_session_persistence_marker(
        self, state: SessionState, reply: str
    ) -> str:
        match = _PERSISTENCE_CHANGE.search(reply)
        if not match:
            return ""
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            return f"PERSISTENCE_CHANGE JSON is invalid: {error}"
        errors = validate_persistence_change(
            payload,
            "session.persistence_change",
            required=True,
        )
        if errors:
            return "Invalid persistence contract: " + "; ".join(errors)
        target_errors = self._session_persistence_target_errors(payload)
        if target_errors:
            return "Invalid persistence targets: " + "; ".join(target_errors)
        state.persistence_change = dict(payload)
        self._save(state)
        return ""

    def _session_persistence_target_errors(
        self, payload: Dict[str, object]
    ) -> List[str]:
        strategy = persistence_change_strategy(payload)
        if strategy == "none":
            return []
        errors: List[str] = []
        for raw_target_id in payload.get("target_ids", []):
            target_id = str(raw_target_id).strip()
            target = self.config.persistence.target(target_id)
            if target is None:
                errors.append(
                    f"target {target_id} is not configured; run persistence-configure"
                )
            elif strategy == "clean_break" and target.environment == "production":
                errors.append(f"clean_break cannot target production: {target_id}")
        return errors

    def _run_session_persistence_action(self, state: SessionState) -> Dict[str, object]:
        strategy = persistence_change_strategy(state.persistence_change)
        if strategy in {"none", "initial_schema"}:
            return {"ok": True, "strategy": strategy, "executed": False}
        candidate_fingerprint = persistence_candidate_fingerprint(self.project_root)
        manifest = build_persistence_action_manifest(
            self.project_root,
            state.persistence_change,
            self.config.persistence,
            candidate_fingerprint=candidate_fingerprint,
        )
        fingerprint = str(manifest["fingerprint"])
        prior = state.persistence_actions.get("session", {})
        if prior.get("fingerprint") == fingerprint and prior.get("status") == "verified":
            return dict(prior.get("result", {}))
        destructive_requires_user = bool(
            strategy == "clean_break"
            and self._authorization_policy(state).decide("destructive_change")
            == "WAIT_USER"
        )
        if destructive_requires_user:
            answer = self._prompt_user(
                "Clean break will permanently delete and rebuild these registered "
                "development/test targets:\n"
                + json.dumps(manifest, indent=2, ensure_ascii=False)
                + "\nApprove this complete reset? (y/n) [n]: ",
                default="n",
            )
            if answer.strip().lower() not in {"y", "yes"}:
                raise PersistenceContractError("clean break reset was not approved")
        state.persistence_actions["session"] = {
            "fingerprint": fingerprint,
            "status": "approved",
            "manifest": manifest,
            "approval": (
                "interactive" if destructive_requires_user else "auto"
            ),
            "approved_at": self._now(),
        }
        self._save(state)
        try:
            result = execute_persistence_action(
                self.project_root,
                state.persistence_change,
                self.config.persistence,
            )
        except PersistenceContractError as error:
            state.persistence_actions["session"].update(
                status="failed", error=str(error), failed_at=self._now()
            )
            self._save(state)
            raise
        state.persistence_actions["session"].update(
            status="verified", result=result, verified_at=self._now()
        )
        self._save(state)
        return result

    @staticmethod
    def _logical_gate_commands(plan) -> List[str]:
        """Return every unique command, including commands in parallel groups."""
        return list(
            dict.fromkeys(
                list(plan.commands)
                + [
                    command
                    for group in plan.parallel_groups
                    for command in group.commands
                ]
            )
        )

    def _append_verification_log(
        self,
        state: SessionState,
        action: str,
        verify: Dict[str, object],
    ) -> None:
        """Persist verification outcome and execution/certificate metrics."""
        entry = {
            "attempt": state.current_attempt,
            "action": action,
            "result": "pass" if verify["ok"] else str(verify["reason"]),
            "verification_scope": str(verify.get("scope", "final")),
            "logical_commands": int(verify.get("logical_commands", 0)),
            "executed_commands": int(verify.get("executed_commands", 0)),
            "certificate_hits": int(verify.get("certificate_hits", 0)),
            "duration_seconds": float(verify.get("duration_seconds", 0.0)),
            "timestamp": self._now(),
        }
        for key in ("failure_kind", "raw_log_path", "retry_fix"):
            if key in verify:
                entry[key] = verify[key]
        state.execution_log.append(entry)

    # ── Public entry points ──────────────────────────────────────

    def start(self) -> SessionState:
        """Create a new session and drive it to completion (or interruption)."""
        if self._coordinator is None:
            from .workflow_runtime import WorkflowCoordinator

            self._coordinator = WorkflowCoordinator(
                self.orch,
                print_agent_output=self._print_agent_output,
                full_verify=self._full_verify,
                auto_approve=self._auto_approve,
                health_runtime=self._health_runtime,
            )
        return self._coordinator.start_session(self)

    def resume(self, session_id: str) -> SessionState:
        """Resume an existing session.

        Completed sessions are returned as-is.  Failed sessions are reset to
        ``executing`` with a fresh attempt counter so the user can continue
        where the previous run left off while preserving all prior context.
        """
        existing = load_session_state(self.project_root, session_id)
        if existing.mode != self.mode:
            raise ValueError(
                f"session {session_id} is {existing.mode}, not {self.mode}"
            )
        if existing.status == "completed" and not existing.parent_handoff_id:
            if not existing.workflow_id:
                self._print(f"Session {session_id} is already completed.")
                return existing
            from .workflow_chain import WorkflowStore

            workflow_store = WorkflowStore(self.project_root)
            snapshot = workflow_store.load(existing.workflow_id)
            from .workflow_runtime import _head_contains_completed_session

            if snapshot.status == "completed" and _head_contains_completed_session(
                self.project_root,
                session_id,
            ):
                workflow_store.clear_active(snapshot.workflow_id)
                self._print(f"Session {session_id} is already completed.")
                return existing
        if self._coordinator is None:
            from .workflow_runtime import WorkflowCoordinator

            self._coordinator = WorkflowCoordinator(
                self.orch,
                print_agent_output=self._print_agent_output,
                full_verify=self._full_verify,
                auto_approve=self._auto_approve,
                health_runtime=self._health_runtime,
            )
        return self._coordinator.resume_session(self, session_id)

    def offer_resume_or_new(self) -> SessionState:
        """If there are active or failed sessions for this mode, offer to resume; else start new."""
        resumable = sort_sessions([
            s for s in list_sessions(self.project_root)
            if s.mode == self.mode and s.status != "completed"
        ])
        if resumable:
            selected = self._select_resumable_session(resumable)
            if selected is not None:
                return self.resume(selected.session_id)
            self._replace_active_workflow = True
        return self.start()

    # ── Main driver ──────────────────────────────────────────────

    @reporting_scope
    def _drive_local(self, state: SessionState) -> SessionState:
        """Drive the session through its phases until completion or pause."""
        reporter = getattr(self.orch, "reporter", None)
        if reporter is not None:
            reporter.bind(self.mode, state.session_id, goal=state.goal, workflow_id=state.workflow_id)
            self._print_agent_output = self._print_agent_output or reporter.presenter.raw_output
            self.orch._print_agent_output = self._print_agent_output
            reporter.observe_session(state)
            attach_run_file_logger(self.orch.logger, reporter.root / "run.log")
        active_phase = (
            state.status if state.status in {"conversing", "executing"} else ""
        )
        try:
            self._check_health_action()
            if self._reconcile_prepared_session_handoff(state):
                return state
            if self.mode == "collab" and state.status in {"conversing", "executing"}:
                # A provider can be interrupted after mutating the target but
                # before the read-only guard gets a chance to roll it back.
                # Restore that durable preimage before consuming a saved route
                # or capturing any baseline for a child workflow.
                self._reconcile_interrupted_collab_checkpoints(state)
                routed, normalization_error = (
                    self._resume_pending_collab_disposition(state)
                )
                if routed is not None:
                    return routed
                if normalization_error:
                    state.conversation.append(
                        {"role": "user", "content": normalization_error}
                    )
                    state.execution_log.append(
                        {
                            "attempt": state.current_attempt,
                            "action": "collab_protocol_normalization_retry",
                            "result": normalization_error[:500],
                            "timestamp": self._now(),
                        }
                    )
                    self._save(state)
                elif state.status == "conversing":
                    goal_clear_error = self._resume_pending_collab_goal_clear(
                        state
                    )
                    if goal_clear_error:
                        state.conversation.append(
                            {"role": "user", "content": goal_clear_error}
                        )
                        self._save(state)
                if state.status == "executing" and not state.active_handoff_id:
                    self._resume_pending_collab_assistance(state)
                    completed = self._resume_pending_collab_completion(state)
                    if completed is not None:
                        return completed
            if state.status == "conversing":
                active_phase = "conversing"
                state = self._phase_converse(state)

            if state.status == "executing":
                active_phase = "executing"
                if self.mode == "fix":
                    if state.return_phase == "after_child":
                        state = self._phase_fix_after_child(state)
                    else:
                        state = self._phase_fix_execute(state)
                elif self.mode == "provider_resolve":
                    state = self._phase_provider_resolve_execute(state)
                else:
                    state = self._phase_collab_loop(state)
            self._check_health_action()
        except KeyboardInterrupt:
            self._print("\nSession interrupted by user. Progress saved.")
            state.resume_phase = (
                state.status
                if state.status in {"conversing", "executing"}
                else active_phase
            )
            state.status = "paused"
            state.resolution = "interrupted_by_user"
            self._save(state)
        except RuntimeError as exc:
            if reporter is not None:
                reporter.exception(exc)
            state.resume_phase = (
                state.status
                if state.status in {"conversing", "executing"}
                else active_phase
            )
            state.status = "failed"
            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "error",
                "result": str(exc),
                "timestamp": self._now(),
            })
            self._save(state)
            raise
        return state

    def _reconcile_prepared_session_handoff(self, state: SessionState) -> bool:
        """Recover a handoff prepared just before its parent-state receipt."""

        if state.active_handoff_id or not state.workflow_id:
            return False
        from .workflow_chain import WorkflowRef, WorkflowStore

        store = (
            self._coordinator.store
            if self._coordinator is not None
            else WorkflowStore(self.project_root)
        )
        snapshot = store.load(state.workflow_id)
        handoff_id = str(snapshot.active_handoff_id).strip()
        if not handoff_id:
            return False
        try:
            handoff = store.load_handoff(handoff_id)
        except (FileNotFoundError, RuntimeError, ValueError):
            return False
        if (
            handoff.parent != WorkflowRef(state.mode, state.session_id)
            or handoff.returned_at
        ):
            return False
        state.active_handoff_id = handoff_id
        state.status = "waiting_child"
        state.return_phase = ""
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "action": "prepared_handoff_reconciled",
                "result": f"{handoff.target}: {handoff.reason}"[:500],
                "handoff_id": handoff_id,
                "timestamp": self._now(),
            }
        )
        self._save(state)
        return True

    def _drive(self, state: SessionState) -> SessionState:
        """Backward-compatible local driver used by older integrations."""
        return self._drive_local(state)

    # ── Phase 1: Conversational clarification ────────────────────

    def _phase_converse(self, state: SessionState) -> SessionState:
        if not state.goal:
            if self.mode == "provider_resolve":
                blocked_run = self.orch.restore_exhausted_provider_recovery()
                if blocked_run is not None:
                    state.status = "blocked"
                    state.resolution = "provider_recovery_contract_unsatisfied"
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "provider_contract_blocked",
                        "result": str(
                            blocked_run.active_blocker.get("reason", "")
                        )[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    self._print(
                        "Provider recovery remains blocked because its consumer "
                        "contract has not changed."
                    )
                    return state
                state.goal = self.orch.build_provider_resolve_goal()
                self._print("Loaded current provider_research blockers into a recovery session.")
            else:
                label = "bug" if self.mode == "fix" else "goal"
                self._print(f"Describe the {label} you want to address:")
                user_input = self._prompt_user("", multiline=True)
                if not user_input.strip():
                    self._print("No input provided. Exiting.")
                    state.status = "failed"
                    state.resolution = "no_input"
                    self._save(state)
                    return state
                state.goal = user_input.strip()
            state.conversation.append({"role": "user", "content": state.goal})
            self._save(state)
            self._print_agent_thinking()

        max_converse_rounds = 15
        rounds = 0

        while rounds < max_converse_rounds:
            self._check_health_action()
            rounds += 1
            prompt = self._build_converse_prompt(state)
            try:
                reply = self._call_agent(state, f"converse-{rounds}", prompt)
            except RuntimeError as exc:
                err_msg = str(exc)
                state.execution_log.append({
                    "attempt": rounds,
                    "action": "converse_error",
                    "result": err_msg[:500],
                    "timestamp": self._now(),
                })
                self._save(state)
                self._print(f"Agent call failed (transient): {err_msg[:200]}")
                self._print("Retrying clarification...")
                continue

            state.conversation.append({"role": "agent", "content": reply})
            self._save(state)

            if self.mode == "collab":
                if not self._goal_environment_confirmed(state):
                    handled, environment_error = (
                        self._consume_goal_environment_reply(state, reply)
                    )
                    if environment_error:
                        state.conversation.append(
                            {"role": "user", "content": environment_error}
                        )
                        state.execution_log.append(
                            {
                                "attempt": rounds,
                                "action": "goal_environment_protocol_retry",
                                "result": environment_error[:500],
                                "timestamp": self._now(),
                            }
                        )
                        self._save(state)
                        self._print_agent_thinking()
                        continue
                    if handled:
                        self._print_agent_thinking()
                        continue
                    missing_environment = (
                        "Before routing or implementing the goal, output exactly one "
                        "GOAL_ENVIRONMENT v1 marker. Infer a confirmed real or "
                        "simulated outcome only when the user's goal is explicit; "
                        "otherwise generate a project-specific, plain-language "
                        "question and choices with decision=ask_user."
                    )
                    state.conversation.append(
                        {"role": "user", "content": missing_environment}
                    )
                    state.execution_log.append(
                        {
                            "attempt": rounds,
                            "action": "goal_environment_required",
                            "result": missing_environment[:500],
                            "timestamp": self._now(),
                        }
                    )
                    self._save(state)
                    self._print_agent_thinking()
                    continue
                routed, normalization_error = (
                    self._route_collab_workflow_reply(state, reply)
                )
                if routed is not None:
                    return routed
                if normalization_error:
                    state.conversation.append(
                        {"role": "user", "content": normalization_error}
                    )
                    state.execution_log.append(
                        {
                            "attempt": rounds,
                            "action": "collab_protocol_normalization_retry",
                            "result": normalization_error[:500],
                            "timestamp": self._now(),
                        }
                    )
                    self._save(state)
                    self._print_agent_thinking()
                    continue

            if self.mode == "fix":
                disposition, disposition_error = self._parse_fix_disposition(
                    reply
                )
                if disposition_error:
                    state.conversation.append(
                        {"role": "user", "content": disposition_error}
                    )
                    self._save(state)
                    continue
                if disposition is not None:
                    decision = str(disposition.get("decision", "")).strip()
                    if decision not in {
                        "fix",
                        "run_iteration",
                        "not_bug",
                        "need_user",
                        "resume_child",
                    }:
                        state.conversation.append(
                            {
                                "role": "user",
                                "content": (
                                    "FIX_DISPOSITION v1 decision must be fix, "
                                    "run_iteration, not_bug, need_user, or resume_child."
                                ),
                            }
                        )
                        self._save(state)
                        continue
                    if decision == "run_iteration":
                        spec_seed = disposition.get("spec_seed")
                        if not isinstance(spec_seed, dict) or not spec_seed:
                            state.conversation.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "FIX_DISPOSITION v1 decision=run_iteration "
                                        "requires a non-empty spec_seed JSON object."
                                    ),
                                }
                            )
                            self._save(state)
                            continue
                    if decision == "resume_child" and not str(
                        disposition.get("resume_handoff_id", "")
                    ).strip():
                        state.conversation.append(
                            {
                                "role": "user",
                                "content": (
                                    "FIX_DISPOSITION v1 decision=resume_child "
                                    "requires resume_handoff_id."
                                ),
                            }
                        )
                        self._save(state)
                        continue
                    if decision == "resume_child":
                        resume_error = self._resume_handoff_error(
                            state,
                            str(disposition["resume_handoff_id"]).strip(),
                        )
                        if resume_error:
                            state.conversation.append(
                                {"role": "user", "content": resume_error}
                            )
                            self._save(state)
                            continue
                    issue_ref = self._materialize_fix_issue(state, disposition)
                    if decision == "fix":
                        verify_command = str(
                            disposition.get("verification_command", "")
                        ).strip()
                        if verify_command:
                            state.fix_verify_command = verify_command
                        persistence_payload = disposition.get("persistence_change")
                        if isinstance(persistence_payload, dict):
                            errors = validate_persistence_change(
                                persistence_payload,
                                "session.persistence_change",
                                required=True,
                            )
                            if errors:
                                state.conversation.append(
                                    {
                                        "role": "user",
                                        "content": "Invalid persistence contract: "
                                        + "; ".join(errors),
                                    }
                                )
                                self._save(state)
                                continue
                            target_errors = self._session_persistence_target_errors(
                                persistence_payload
                            )
                            if target_errors:
                                state.conversation.append(
                                    {
                                        "role": "user",
                                        "content": "Invalid persistence targets: "
                                        + "; ".join(target_errors),
                                    }
                                )
                                self._save(state)
                                continue
                            state.persistence_change = dict(persistence_payload)
                        state.status = "executing"
                        state.return_phase = ""
                        state.execution_log.append(
                            {
                                "attempt": 0,
                                "action": "fix_disposition",
                                "result": "fix",
                                "issue_ref": issue_ref,
                                "timestamp": self._now(),
                            }
                        )
                        self._save(state)
                        return state
                    if decision == "run_iteration":
                        payload = dict(disposition)
                        payload["issue_ref"] = issue_ref
                        return self._prepare_workflow_handoff(
                            state,
                            target="run",
                            reason=str(disposition.get("reason", "")),
                            payload={
                                "spec_seed": dict(disposition["spec_seed"]),
                                "fix_disposition": payload,
                            },
                        )
                    if decision == "resume_child":
                        return self._prepare_workflow_handoff(
                            state,
                            target="resume",
                            reason=str(disposition.get("reason", "resume child")),
                            payload={
                                "resume_handoff_id": str(
                                    disposition.get("resume_handoff_id", "")
                                )
                            },
                        )
                    if decision == "not_bug":
                        reason = str(disposition.get("reason", "")).strip()
                        self._print(f"\nAgent believes this is NOT a bug: {reason}")
                        answer = self._prompt_user(
                            "Do you agree this is not a bug? (y/n) [y]: ",
                            default="y",
                        )
                        if answer.strip().lower() not in ("n", "no"):
                            state.status = "completed"
                            state.resolution = "not_a_bug"
                            self._save(state)
                            return state
                        user_reply = self._prompt_user(
                            "\nPlease explain why you believe this is a bug: ",
                            multiline=True,
                        )
                        state.conversation.append(
                            {
                                "role": "user",
                                "content": user_reply.strip()
                                or "I still believe this is a bug. Please look again.",
                            }
                        )
                        self._save(state)
                        continue
                    question = str(
                        disposition.get("question")
                        or disposition.get("reason")
                        or "What additional information is needed?"
                    )
                    decision_class = classify_assistance_request(
                        question,
                        declared_class=str(
                            disposition.get("decision_class", "")
                        ),
                    )
                    if (
                        self._authorization_policy(state).decide(decision_class)
                        == "AUTO_EXECUTE"
                    ):
                        auto_resolution = (
                            "The requested decision concerns internal implementation "
                            f"scope ({decision_class}) and is already authorized. "
                            "Continue autonomously without asking the user."
                        )
                        state.conversation.append(
                            {"role": "orchestrator", "content": auto_resolution}
                        )
                        state.execution_log.append(
                            {
                                "attempt": rounds,
                                "action": "internal_action_auto_authorized",
                                "result": auto_resolution[:500],
                                "timestamp": self._now(),
                            }
                        )
                        self._save(state)
                        self._print_agent_thinking()
                        continue
                    self._print(f"\nAgent:\n{reply.strip()}")
                    user_reply = self._prompt_user(f"\n{question}\nYour reply: ", multiline=True)
                    state.conversation.append(
                        {"role": "user", "content": user_reply.strip() or "No additional information."}
                    )
                    self._save(state)
                    continue

            # Check for NOT_A_BUG (fix mode only) before GOAL_CLEAR
            if self.mode == "fix":
                not_bug_match = _NOT_A_BUG.search(reply)
                if not_bug_match:
                    reason = not_bug_match.group(1).strip()
                    display = _NOT_A_BUG.sub("", reply).strip()
                    if display:
                        self._print(f"\nAgent:\n{display}")
                    self._print(f"\nAgent believes this is NOT a bug: {reason}")
                    answer = self._prompt_user(
                        "Do you agree this is not a bug? (y/n) [y]: ", default="y",
                    )
                    if answer.strip().lower() not in ("n", "no"):
                        self._materialize_fix_issue(
                            state,
                            {
                                "decision": "not_bug",
                                "summary": state.goal,
                                "reason": reason,
                            },
                        )
                        state.status = "completed"
                        state.resolution = "not_a_bug"
                        state.execution_log.append({
                            "attempt": 0,
                            "action": "not_a_bug",
                            "result": reason,
                            "timestamp": self._now(),
                        })
                        self._save(state)
                        self._print(f"Session {state.session_id} closed as not-a-bug.")
                        return state
                    # User disagrees — continue conversation
                    user_reply = self._prompt_user(
                        "\nPlease explain why you believe this is a bug: ",
                        multiline=True,
                    )
                    if not user_reply.strip():
                        user_reply = "I still believe this is a bug. Please look again."
                    state.conversation.append({"role": "user", "content": user_reply})
                    self._save(state)
                    self._print_agent_thinking()
                    continue

            if _GOAL_CLEAR.search(reply):
                persistence_match = _PERSISTENCE_CHANGE.search(reply)
                if persistence_match:
                    try:
                        persistence_change = json.loads(persistence_match.group(1))
                    except json.JSONDecodeError as error:
                        state.conversation.append(
                            {
                                "role": "user",
                                "content": f"PERSISTENCE_CHANGE JSON is invalid: {error}",
                            }
                        )
                        self._save(state)
                        continue
                    persistence_errors = validate_persistence_change(
                        persistence_change,
                        "session.persistence_change",
                        required=True,
                    )
                    if persistence_errors:
                        state.conversation.append(
                            {
                                "role": "user",
                                "content": "Invalid persistence contract: "
                                + "; ".join(persistence_errors),
                            }
                        )
                        self._save(state)
                        continue
                    target_errors = self._session_persistence_target_errors(
                        persistence_change
                    )
                    if target_errors:
                        state.conversation.append(
                            {
                                "role": "user",
                                "content": "Invalid persistence targets: "
                                + "; ".join(target_errors),
                            }
                        )
                        self._save(state)
                        continue
                    state.persistence_change = dict(persistence_change)
                # Extract optional FIX_VERIFY command from the reply
                fv_match = _FIX_VERIFY.search(reply)
                if fv_match and self.mode == "fix":
                    state.fix_verify_command = fv_match.group(1).strip()
                if self.mode == "fix":
                    self._materialize_fix_issue(
                        state,
                        {
                            "decision": "fix",
                            "summary": state.goal,
                            "reason": "Legacy GOAL_CLEAR response accepted for compatibility.",
                            "verification_command": state.fix_verify_command,
                        },
                    )
                display = _GOAL_CLEAR.sub("", reply)
                display = _FIX_VERIFY.sub("", display)
                display = _PERSISTENCE_CHANGE.sub("", display).strip()
                if display:
                    self._print(f"\nAgent:\n{display}")
                self._print("\nAgent has understood the goal. Proceeding to execution.")
                state.status = "executing"
                self._save(state)
                return state

            self._print(f"\nAgent:\n{reply}")
            user_reply = self._prompt_user("\nYour reply: ", multiline=True)
            if not user_reply.strip():
                user_reply = "No additional information. Please proceed if you are ready."
            state.conversation.append({"role": "user", "content": user_reply})
            self._save(state)
            self._print_agent_thinking()

        if self.mode == "collab" and not self._goal_environment_confirmed(state):
            state.status = "failed"
            state.resolution = "goal_execution_environment_unresolved"
            self._save(state)
            self._print(
                "Could not establish the goal's execution environment; "
                "stopping before implementation."
            )
            return state

        # Max rounds reached – force proceed
        self._print("Max clarification rounds reached. Proceeding with current understanding.")
        state.status = "executing"
        self._save(state)
        return state

    @staticmethod
    def _parse_protocol_json(
        pattern: re.Pattern,
        reply: str,
        *,
        label: str,
    ) -> Tuple[Optional[Dict[str, object]], str]:
        matches = list(pattern.finditer(reply))
        if not matches:
            return None, ""
        if len(matches) != 1:
            return None, f"Output exactly one {label} marker."
        try:
            payload = json.loads(matches[0].group(1))
        except json.JSONDecodeError as error:
            return None, f"{label} JSON is invalid: {error}"
        if not isinstance(payload, dict):
            return None, f"{label} must contain one JSON object."
        unsafe_refs = Session._unsafe_protocol_refs(payload)
        if unsafe_refs:
            return None, (
                f"{label} contains unsafe evidence references: "
                + ", ".join(unsafe_refs[:5])
            )
        return dict(payload), ""

    @staticmethod
    def _parse_protocol_envelope(
        reply: str,
        *,
        marker: str,
        version: str,
        label: str,
    ) -> Tuple[Optional[Dict[str, object]], str]:
        try:
            payload = json.loads(reply.strip())
        except json.JSONDecodeError:
            return None, ""
        if not isinstance(payload, dict) or marker not in payload:
            return None, ""
        declared = str(payload.get(marker, "")).strip().lower()
        if declared != version.lower():
            return None, f"{label} envelope version must be {version}."
        normalized = dict(payload)
        normalized.pop(marker, None)
        unsafe_refs = Session._unsafe_protocol_refs(normalized)
        if unsafe_refs:
            return None, (
                f"{label} contains unsafe evidence references: "
                + ", ".join(unsafe_refs[:5])
            )
        return normalized, ""

    def _parse_fix_disposition(
        self,
        reply: str,
    ) -> Tuple[Optional[Dict[str, object]], str]:
        """Accept both documented and legacy structured fix dispositions."""

        disposition, error = self._parse_protocol_json(
            _FIX_DISPOSITION,
            reply,
            label="FIX_DISPOSITION v1",
        )
        if disposition is None and not error:
            disposition, error = self._parse_protocol_envelope(
                reply,
                marker="FIX_DISPOSITION",
                version="v1",
                label="FIX_DISPOSITION v1",
            )
        return disposition, error

    @staticmethod
    def _goal_environment_confirmed(state: SessionState) -> bool:
        contract = state.goal_execution_environment
        return bool(
            isinstance(contract, dict)
            and contract.get("confirmed") is True
            and str(contract.get("mode", "")).strip() in {"real", "simulated"}
        )

    def _authorization_policy(
        self,
        state: SessionState,
    ) -> WorkflowAuthorizationPolicy:
        policy = authorization_policy_for_state(
            auto_approve=bool(self._auto_approve or state.auto_approve),
            payload=state.authorization_policy,
        )
        if state.authorization_policy != policy.to_dict():
            state.authorization_policy = policy.to_dict()
            self._save(state)
        return policy

    def _parse_goal_environment(
        self,
        reply: str,
    ) -> Tuple[Optional[Dict[str, object]], str]:
        payload, error = self._parse_protocol_json(
            _GOAL_ENVIRONMENT,
            reply,
            label="GOAL_ENVIRONMENT v1",
        )
        if payload is None and not error:
            payload, error = self._parse_protocol_envelope(
                reply,
                marker="GOAL_ENVIRONMENT",
                version="v1",
                label="GOAL_ENVIRONMENT v1",
            )
        return payload, error

    @staticmethod
    def _goal_environment_choices(
        payload: Dict[str, object],
    ) -> Tuple[List[Dict[str, str]], str]:
        raw_choices = payload.get("choices")
        if not isinstance(raw_choices, list):
            return [], (
                "GOAL_ENVIRONMENT v1 decision=ask_user requires two "
                "project-specific choices."
            )
        choices: List[Dict[str, str]] = []
        for raw in raw_choices:
            if not isinstance(raw, dict):
                return [], "GOAL_ENVIRONMENT v1 choices must be JSON objects."
            value = str(raw.get("value", "")).strip()
            label = str(raw.get("label", "")).strip()
            description = str(raw.get("description", "")).strip()
            if value not in {"real", "simulated"} or not label or not description:
                return [], (
                    "Each GOAL_ENVIRONMENT v1 choice requires value=real or "
                    "simulated plus a non-empty project-specific label and "
                    "description."
                )
            choices.append(
                {
                    "value": value,
                    "label": label,
                    "description": description,
                }
            )
        if len(choices) != 2 or {item["value"] for item in choices} != {
            "real",
            "simulated",
        }:
            return [], (
                "GOAL_ENVIRONMENT v1 choices must contain exactly one real "
                "choice and one simulated choice."
            )
        return choices, ""

    def _record_goal_environment(
        self,
        state: SessionState,
        *,
        mode: str,
        source: str,
        summary: str,
        label: str = "",
        question: str = "",
        answer: str = "",
    ) -> None:
        state.goal_execution_environment = {
            "schema_version": 1,
            "mode": mode,
            "source": source,
            "summary": summary,
            "label": label,
            "question": question,
            "answer": answer,
            "confirmed": True,
            "confirmed_at": self._now(),
        }
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "action": "goal_execution_environment_confirmed",
                "result": f"{mode}: {summary}"[:500],
                "source": source,
                "timestamp": self._now(),
            }
        )
        self._save(state)

    def _consume_goal_environment_reply(
        self,
        state: SessionState,
        reply: str,
    ) -> Tuple[bool, str]:
        payload, error = self._parse_goal_environment(reply)
        if error or payload is None:
            return False, error
        decision = str(payload.get("decision", "")).strip()
        if decision in {"real", "simulated"}:
            summary = str(payload.get("summary", "")).strip()
            if not summary:
                return False, (
                    "GOAL_ENVIRONMENT v1 decision=real or simulated requires "
                    "a project-specific summary of the accepted outcome."
                )
            self._record_goal_environment(
                state,
                mode=decision,
                source="explicit_goal",
                summary=summary,
                label=str(payload.get("label", "")).strip(),
            )
            return True, ""
        if decision != "ask_user":
            return False, (
                "GOAL_ENVIRONMENT v1 decision must be real, simulated, or "
                "ask_user."
            )

        question = str(payload.get("question", "")).strip()
        if not question:
            return False, (
                "GOAL_ENVIRONMENT v1 decision=ask_user requires a "
                "project-specific plain-language question."
            )
        choices, choice_error = self._goal_environment_choices(payload)
        if choice_error:
            return False, choice_error
        display = [question]
        for index, choice in enumerate(choices, start=1):
            display.append(
                f"{index}. {choice['label']}: {choice['description']}"
            )
        self._print("\nAgent:\n" + "\n".join(display))
        user_reply = self._prompt_user("\nYour reply: ", multiline=True).strip()
        state.conversation.append(
            {
                "role": "user",
                "content": user_reply or "No selection provided.",
            }
        )
        selected = next(
            (
                choice
                for index, choice in enumerate(choices, start=1)
                if user_reply.casefold()
                in {
                    str(index),
                    choice["value"].casefold(),
                    choice["label"].casefold(),
                }
            ),
            None,
        )
        if selected is not None:
            self._record_goal_environment(
                state,
                mode=selected["value"],
                source="user_selection",
                summary=selected["description"],
                label=selected["label"],
                question=question,
                answer=user_reply,
            )
        else:
            self._save(state)
        return True, ""

    def _goal_environment_prompt_lines(
        self,
        state: SessionState,
    ) -> List[str]:
        if self._goal_environment_confirmed(state):
            contract = json.dumps(
                state.goal_execution_environment,
                ensure_ascii=False,
                sort_keys=True,
            )
            lines = [
                "Confirmed goal execution environment (binding):",
                contract,
                (
                    "Preserve this choice across diagnosis, routing, "
                    "implementation, and acceptance. Do not silently change or "
                    "downgrade it."
                ),
            ]
            if str(state.goal_execution_environment.get("mode")) == "real":
                lines.append(
                    "Mocks or fixtures may support internal tests, but they cannot "
                    "serve as the final completion evidence for this real outcome."
                )
            else:
                lines.append(
                    "Simulated artifacts are allowed for this goal, but disclose "
                    "their nature and do not represent them as real-world output."
                )
            return lines
        return [
            "Before routing or implementation, determine the outcome environment required by the user's goal.",
            "- If the goal explicitly requires an externally usable or real outcome, return one final marker: GOAL_ENVIRONMENT v1: {\"decision\":\"real\",\"summary\":\"<project-specific accepted outcome>\"}",
            "- If the goal explicitly requests an offline, simulated, mock, fixture, or rehearsal outcome, use decision=simulated with a project-specific summary.",
            "- Never infer simulated merely from words such as test, demo, verify, sample, or prototype when the expected deliverable is unclear.",
            "- If unclear, return decision=ask_user with question and exactly two choices (value real and simulated), each containing a project-specific label and description.",
            "- Generate the question and choice wording from this project, its artifacts, and the user's goal. Do not reuse a stock example from another project.",
            "- Use plain user-facing language in question, label, and description; avoid internal implementation terminology unless the user already used it.",
            "- The GOAL_ENVIRONMENT marker must be the entire final response for this turn. Do not route, edit, or implement in the same turn.",
        ]

    def _route_collab_foreign_fix_disposition(
        self,
        state: SessionState,
        reply: str,
    ) -> Tuple[Optional[SessionState], str]:
        disposition, error = self._parse_fix_disposition(reply)
        if error or disposition is None:
            return None, error

        decision = str(disposition.get("decision", "")).strip()
        if decision == "fix":
            issue_seed = {
                key: disposition[key]
                for key in (
                    "summary",
                    "reason",
                    "expected",
                    "actual",
                    "evidence_refs",
                    "affected_contracts",
                    "verification_command",
                )
                if key in disposition
            }
            reproduction = disposition.get("reproduction")
            if isinstance(reproduction, str) and reproduction.strip():
                issue_seed["reproduction"] = [reproduction.strip()]
            elif isinstance(reproduction, list):
                issue_seed["reproduction"] = list(reproduction)
            issue_seed["decision"] = "fix"
            issue_seed["reported_goal"] = state.goal
            discarded_persistence = isinstance(
                disposition.get("persistence_change"), dict
            )
            state.execution_log.append(
                {
                    "attempt": state.current_attempt,
                    "action": "collab_foreign_disposition_normalized",
                    "result": "fix",
                    "discarded_persistence_change": discarded_persistence,
                    "timestamp": self._now(),
                }
            )
            self._save(state)
            return (
                self._prepare_workflow_handoff(
                    state,
                    target="fix",
                    reason=str(
                        disposition.get("reason")
                        or disposition.get("summary")
                        or "bounded defect"
                    ),
                    payload={"issue_seed": issue_seed},
                ),
                "",
            )

        if decision == "run_iteration":
            spec_seed = disposition.get("spec_seed")
            if not isinstance(spec_seed, dict) or not spec_seed:
                return None, (
                    "Collab received FIX_DISPOSITION decision=run_iteration without "
                    "spec_seed. Emit ROUTE_WORKFLOW v1 target=run with a complete spec_seed."
                )
            state.execution_log.append(
                {
                    "attempt": state.current_attempt,
                    "action": "collab_foreign_disposition_normalized",
                    "result": "run",
                    "timestamp": self._now(),
                }
            )
            self._save(state)
            return (
                self._prepare_workflow_handoff(
                    state,
                    target="run",
                    reason=str(
                        disposition.get("reason")
                        or disposition.get("summary")
                        or "product iteration"
                    ),
                    payload={"spec_seed": dict(spec_seed)},
                ),
                "",
            )

        if decision == "resume_child":
            resume_id = str(disposition.get("resume_handoff_id", "")).strip()
            if not resume_id:
                return None, (
                    "Collab received FIX_DISPOSITION decision=resume_child without "
                    "resume_handoff_id."
                )
            resume_error = self._resume_handoff_error(state, resume_id)
            if resume_error:
                return None, resume_error
            return (
                self._prepare_workflow_handoff(
                    state,
                    target="resume",
                    reason=str(disposition.get("reason") or "resume child"),
                    payload={"resume_handoff_id": resume_id},
                ),
                "",
            )

        return None, (
            "Collab cannot consume FIX_DISPOSITION decision="
            f"{decision or '<missing>'}. Emit the matching ROUTE_WORKFLOW v1, "
            "NEED_USER_ASSIST, or GOAL_ACHIEVED marker instead."
        )

    def _route_collab_workflow_reply(
        self,
        state: SessionState,
        reply: str,
    ) -> Tuple[Optional[SessionState], str]:
        """Normalize and validate every workflow route accepted by collab."""

        route, error = self._parse_protocol_json(
            _ROUTE_WORKFLOW,
            reply,
            label="ROUTE_WORKFLOW v1",
        )
        if route is None and not error:
            route, error = self._parse_protocol_envelope(
                reply,
                marker="ROUTE_WORKFLOW",
                version="v1",
                label="ROUTE_WORKFLOW v1",
            )
        if error:
            return None, error
        if route is None:
            routed, foreign_error = self._route_collab_foreign_fix_disposition(
                state,
                reply,
            )
            if routed is not None or foreign_error:
                return routed, foreign_error
            legacy_bug = _BUG_FOUND.search(reply)
            if legacy_bug:
                reason = legacy_bug.group(1).strip()
                self._print(f"\nAgent found a bug: {reason}")
                return (
                    self._prepare_workflow_handoff(
                        state,
                        target="fix",
                        reason=reason,
                        payload={
                            "issue_seed": {
                                "summary": reason,
                                "reported_goal": state.goal,
                                "reason": (
                                    "Legacy BUG_FOUND marker routed through fix."
                                ),
                            }
                        },
                    ),
                    "",
                )
            return None, ""

        target = str(route.get("target", "")).strip()
        if target not in {"fix", "run", "resume"}:
            return None, "ROUTE_WORKFLOW v1 target must be fix, run, or resume."

        if target == "resume":
            resume_id = str(route.get("resume_handoff_id", "")).strip()
            if not resume_id:
                return None, (
                    "ROUTE_WORKFLOW v1 target=resume requires resume_handoff_id."
                )
            resume_error = self._resume_handoff_error(state, resume_id)
            if resume_error:
                return None, resume_error
            payload = {"resume_handoff_id": resume_id}
        elif target == "fix":
            raw_issue_seed = route.get("issue_seed", {})
            if raw_issue_seed is None:
                raw_issue_seed = {}
            if not isinstance(raw_issue_seed, dict):
                return None, "ROUTE_WORKFLOW v1 issue_seed must be a JSON object."
            issue_seed = dict(raw_issue_seed)
            issue_seed.setdefault("summary", str(route.get("summary", "")))
            issue_seed.setdefault("reason", str(route.get("reason", "")))
            payload = {"issue_seed": issue_seed}
        else:
            raw_spec_seed = route.get("spec_seed")
            if not isinstance(raw_spec_seed, dict) or not raw_spec_seed:
                return None, (
                    "ROUTE_WORKFLOW v1 target=run requires a non-empty "
                    "spec_seed JSON object."
                )
            payload = {"spec_seed": dict(raw_spec_seed)}

        return (
            self._prepare_workflow_handoff(
                state,
                target=target,
                reason=str(route.get("reason") or route.get("summary") or target),
                payload=payload,
            ),
            "",
        )

    def _resume_handoff_error(
        self,
        state: SessionState,
        resume_handoff_id: str,
    ) -> str:
        from .workflow_chain import WorkflowStore

        if not state.workflow_id:
            return "Cannot resume a child before the parent workflow is durable."
        store = (
            self._coordinator.store
            if self._coordinator is not None
            else WorkflowStore(self.project_root)
        )
        try:
            original = store.load_handoff(resume_handoff_id)
        except (FileNotFoundError, RuntimeError, ValueError):
            return f"Unknown resume_handoff_id: {resume_handoff_id}."
        if original.workflow_id != state.workflow_id:
            return (
                f"resume_handoff_id {resume_handoff_id} belongs to another "
                "workflow."
            )
        if (
            original.parent.kind != state.mode
            or original.parent.native_id != state.session_id
        ):
            return (
                f"Handoff {resume_handoff_id} was not routed by the current "
                f"{state.mode} session {state.session_id}; a session cannot "
                "resume its parent or a sibling handoff."
            )
        if original.child is None:
            return f"Handoff {resume_handoff_id} has no child to resume."
        return ""

    def _resume_pending_collab_disposition(
        self,
        state: SessionState,
    ) -> Tuple[Optional[SessionState], str]:
        if (
            state.active_handoff_id
            or not state.conversation
            or not self._goal_environment_confirmed(state)
        ):
            return None, ""
        latest = state.conversation[-1]
        if str(latest.get("role", "")).strip().lower() not in {
            "agent",
            "assistant",
        }:
            return None, ""
        return self._route_collab_workflow_reply(
            state,
            str(latest.get("content", "")),
        )

    def _resume_pending_collab_goal_clear(
        self,
        state: SessionState,
    ) -> str:
        if not state.conversation:
            return ""
        latest = state.conversation[-1]
        if str(latest.get("role", "")).strip().lower() not in {
            "agent",
            "assistant",
        }:
            return ""
        reply = str(latest.get("content", ""))
        if not _GOAL_CLEAR.search(reply):
            return ""
        if not self._goal_environment_confirmed(state):
            return (
                "Saved GOAL_CLEAR cannot be resumed until the goal execution "
                "environment is confirmed."
            )
        marker_error = self._apply_session_persistence_marker(state, reply)
        if marker_error:
            return marker_error
        state.status = "executing"
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "action": "collab_goal_clear_reconciled",
                "result": "resumed saved GOAL_CLEAR response",
                "timestamp": self._now(),
            }
        )
        self._save(state)
        self._print("Resuming execution from the saved goal clarification.")
        return ""

    def _resume_pending_collab_completion(
        self,
        state: SessionState,
    ) -> Optional[SessionState]:
        if not state.conversation:
            return None
        latest = state.conversation[-1]
        if str(latest.get("role", "")).strip().lower() not in {
            "agent",
            "assistant",
        }:
            return None
        reply = str(latest.get("content", ""))
        achieved_match = _GOAL_ACHIEVED.search(reply)
        if not achieved_match:
            return None
        if not self._goal_environment_confirmed(state):
            return None
        self._prepare_collab_execution(state)
        return self._handle_collab_goal_achieved(
            state,
            reply,
            achieved_match,
        )

    def _resume_pending_collab_assistance(self, state: SessionState) -> bool:
        if not state.conversation:
            return False
        latest = state.conversation[-1]
        if str(latest.get("role", "")).strip().lower() not in {
            "agent",
            "assistant",
        }:
            return False
        reply = str(latest.get("content", ""))
        assistance, decision_class, parse_error = self._assistance_request(reply)
        if parse_error:
            self._reject_collab_assistance(state, parse_error)
            return False
        if not assistance:
            return False
        assistance_error = self._collab_assistance_error(
            state,
            assistance,
            decision_class=decision_class,
        )
        if assistance_error:
            self._reject_collab_assistance(state, assistance_error)
            return False
        self._handle_collab_assistance(state, reply, assistance)
        return True

    def _assistance_request(self, reply: str) -> Tuple[str, str, str]:
        payload, error = self._parse_protocol_json(
            _NEED_USER_ASSIST_V1,
            reply,
            label="NEED_USER_ASSIST v1",
        )
        if error:
            return "", "", error
        if payload is not None:
            question = str(payload.get("question", "")).strip()
            decision_class = str(payload.get("decision_class", "")).strip()
            if not question or not decision_class:
                return "", "", (
                    "NEED_USER_ASSIST v1 requires question and decision_class."
                )
            return question, decision_class, ""
        legacy = _NEED_USER_ASSIST.search(reply)
        if legacy is None:
            return "", "", ""
        question = legacy.group(1).strip()
        return question, classify_assistance_request(question), ""

    def _collab_assistance_error(
        self,
        state: SessionState,
        assistance: str,
        *,
        decision_class: str = "",
    ) -> str:
        normalized = " ".join(str(assistance).split())
        attempts = self._attempts_in_current_epoch(state)
        classified = classify_assistance_request(
            normalized,
            declared_class=decision_class,
        )
        policy_decision = self._authorization_policy(state).decide(classified)
        if policy_decision == "AUTO_EXECUTE":
            return (
                "internal_action_auto_authorized: the request concerns "
                f"{classified}, which is an implementation decision already "
                "authorized by the workflow policy. Continue autonomously and "
                "do not ask the user."
            )
        if decision_class and classified == "unknown":
            return (
                "unsupported_user_decision_class: user assistance must be a "
                "goal choice, credential, rights attestation, unbudgeted external "
                "cost, destructive change, irreversible product decision, or "
                "external observation only the user can perform."
            )
        if _ORCHESTRATOR_CONTROL_ASSISTANCE.search(normalized):
            return (
                "orchestrator_control_request: NEED_USER_ASSIST may request "
                "external information or a user-only action, but it may not ask "
                "the user to run or reconfigure "
                "auto-agents. Continue in the current workflow and emit a valid "
                "ROUTE_WORKFLOW marker when another workflow is needed. "
                f"Current attempt epoch={state.attempt_epoch}, calls={attempts}, "
                f"hard_ceiling={state.hard_ceiling}."
            )
        lower = normalized.casefold()
        limit_claimed = any(
            marker in lower
            for marker in (
                "执行上限",
                "次数上限",
                "attempt limit",
                "attempt ceiling",
                "hard ceiling",
            )
        )
        if limit_claimed and attempts < state.hard_ceiling:
            return (
                "stale_attempt_ceiling_claim: NEED_USER_ASSIST claimed that the "
                "attempt ceiling was reached, "
                f"but the current epoch has {attempts}/{state.hard_ceiling} "
                "provider calls. Re-evaluate the durable state and continue."
            )
        return ""

    def _reject_collab_assistance(
        self,
        state: SessionState,
        reason: str,
    ) -> None:
        state.conversation.append(
            {
                "role": "orchestrator",
                "content": "Rejected stale or invalid assistance marker: " + reason,
            }
        )
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "attempt_epoch": state.attempt_epoch,
                "action": "collab_assistance_rejected",
                "result": reason[:500],
                "timestamp": self._now(),
            }
        )
        self._update_stall_state(
            state,
            self._compute_diff_hash(),
            self._compute_verify_sig(reason.partition(":")[0]),
        )
        self._save(state)

    @staticmethod
    def _unsafe_protocol_refs(payload: Dict[str, object]) -> List[str]:
        unsafe: List[str] = []

        def visit(value: object, key: str = "") -> None:
            if isinstance(value, dict):
                for inner_key, inner_value in value.items():
                    visit(inner_value, str(inner_key))
                return
            if isinstance(value, list):
                for item in value:
                    visit(item, key)
                return
            if key not in {"evidence_refs", "artifact_refs", "contract_refs"}:
                return
            raw = str(value).strip().replace("\\", "/")
            parts = raw.split("/")
            if (
                not raw
                or raw.startswith("/")
                or re.match(r"^[A-Za-z]:/", raw)
                or ".." in parts
                or raw == ".env"
                or raw.startswith(".auto-agents/operator/")
            ):
                unsafe.append(raw or "(empty)")

        visit(payload)
        return unsafe

    def _materialize_fix_issue(
        self,
        state: SessionState,
        disposition: Dict[str, object],
    ) -> str:
        from .workflow_chain import IssueBriefBuilder

        payload = dict(disposition)
        payload.setdefault("reported_goal", state.goal)
        payload.setdefault("source_handoff_id", state.parent_handoff_id)
        refs = IssueBriefBuilder(self.project_root, state.session_id).materialize(payload)
        return str(refs["json"])

    def _prepare_workflow_handoff(
        self,
        state: SessionState,
        *,
        target: str,
        reason: str,
        payload: Dict[str, object],
    ) -> SessionState:
        from .workflow_chain import WorkflowRef, WorkflowStore

        if not state.workflow_id:
            store = WorkflowStore(self.project_root)
            snapshot = store.create_root(WorkflowRef(state.mode, state.session_id))
            state.workflow_id = snapshot.workflow_id
        else:
            store = (
                self._coordinator.store
                if self._coordinator is not None
                else WorkflowStore(self.project_root)
            )
            snapshot = store.load(state.workflow_id)
        if target == "run" and self._coordinator is not None:
            route_ready, route_detail = self._coordinator.prepare_run_route()
            state.execution_log.append(
                {
                    "attempt": state.current_attempt,
                    "action": (
                        "run_route_preflight_passed"
                        if route_ready
                        else "run_route_deferred"
                    ),
                    "result": route_detail[:500],
                    "timestamp": self._now(),
                }
            )
            if not route_ready:
                state.conversation.append(
                    {
                        "role": "orchestrator",
                        "content": route_detail,
                    }
                )
                state.status = "executing"
                state.return_phase = ""
                self._save(state)
                return state
        if self.mode in {"fix", "collab"} and not state.baseline_git_ref:
            self.orch._apply_generated_verification_config()
            self._ensure_baseline(state)
        handoff_payload = dict(payload)
        handoff_payload.setdefault(
            "authorization_policy",
            self._authorization_policy(state).to_dict(),
        )
        if self._goal_environment_confirmed(state):
            handoff_payload.setdefault(
                "goal_execution_environment",
                dict(state.goal_execution_environment),
            )
        handoff_payload.setdefault("auto_approve", bool(state.auto_approve))
        handoff_payload.setdefault("head_before", head_ref(self.project_root))
        handoff_payload.setdefault(
            "protected_preexisting_paths", list(state.protected_preexisting_paths)
        )
        handoff = store.prepare_handoff(
            snapshot,
            parent=WorkflowRef(state.mode, state.session_id),
            target=target,
            goal=state.goal,
            reason=reason,
            payload=handoff_payload,
        )
        state.active_handoff_id = handoff.handoff_id
        state.status = "waiting_child"
        state.return_phase = ""
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "action": "workflow_routed",
                "result": f"{target}: {reason}"[:500],
                "handoff_id": handoff.handoff_id,
                "timestamp": self._now(),
            }
        )
        self._begin_attempt_epoch(
            state,
            reason=f"workflow routed to {target}",
            reset_stall=False,
        )
        self._save(state)
        return state

    # ── Phase 2a: Fix mode execution ─────────────────────────────

    def _phase_fix_execute(self, state: SessionState) -> SessionState:
        self._current_state = state
        self.orch._apply_generated_verification_config()
        self._ensure_baseline(state)
        feedback = ""
        while True:
            self._reconcile_interrupted_collab_checkpoints(state)
            self._check_health_action()
            stop = self._should_stop(state, "attempt limit reached")
            if stop:
                self._print(stop)
                break
            self._record_agent_attempt(state)
            self._save(state)
            self._print(f"\n--- Fix attempt {state.current_attempt} ---")

            prompt = self._build_fix_prompt(state, feedback)
            before_snapshot = self._supervised_worktree_snapshot()
            restore_guard = tempfile.TemporaryDirectory(
                prefix="auto-agents-fix-route-"
            )
            restore_root = Path(restore_guard.name)
            self._capture_collab_restore_point(restore_root, before_snapshot)
            try:
                reply = self._call_agent(state, f"fix-{state.current_attempt}", prompt)
            except RuntimeError as exc:
                restore_guard.cleanup()
                err_msg = str(exc)
                state.consecutive_agent_errors += 1
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "agent_error",
                    "result": err_msg[:500],
                    "timestamp": self._now(),
                })
                self._save(state)
                stop = self._should_stop(state, "agent_error")
                if stop:
                    self._print(stop)
                    break
                feedback = self._build_error_feedback(err_msg)
                self._print("Will retry on next attempt.")
                continue

            # Successful agent call resets transient error counter
            state.consecutive_agent_errors = 0

            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "fix",
                "result": reply[:500],
                "timestamp": self._now(),
            })
            self._save(state)

            disposition, disposition_error = self._parse_fix_disposition(reply)
            if disposition_error:
                restore_guard.cleanup()
                feedback = disposition_error
                continue
            if disposition is not None and str(
                disposition.get("decision", "")
            ).strip() == "run_iteration":
                restored = self._restore_collab_mutations(
                    state,
                    before_snapshot,
                    restore_root,
                )
                restore_guard.cleanup()
                raw_spec_seed = disposition.get("spec_seed")
                if not isinstance(raw_spec_seed, dict) or not raw_spec_seed:
                    feedback = (
                        "FIX_DISPOSITION v1 decision=run_iteration requires a "
                        "non-empty spec_seed JSON object."
                    )
                    state.execution_log.append(
                        {
                            "attempt": state.current_attempt,
                            "action": "fix_route_rejected",
                            "result": feedback,
                            "rolled_back_paths": restored,
                            "timestamp": self._now(),
                        }
                    )
                    self._save(state)
                    continue
                issue_ref = self._materialize_fix_issue(state, disposition)
                return self._prepare_workflow_handoff(
                    state,
                    target="run",
                    reason=str(disposition.get("reason", "fix scope expanded")),
                    payload={
                        "spec_seed": dict(raw_spec_seed),
                        "fix_disposition": dict(disposition),
                        "issue_ref": issue_ref,
                        "rolled_back_paths": restored,
                    },
                )
            restore_guard.cleanup()

            marker_error = self._apply_session_persistence_marker(state, reply)
            if marker_error:
                feedback = marker_error
                self._print(marker_error)
                continue
            persistence_issue = self._session_persistence_issue(state)
            if persistence_issue:
                self._print(persistence_issue)
                state.status = "waiting_user"
                self._save(state)
                user_reply = self._prompt_user(
                    "Choose startup_compatible, clean_break, or external_operator and identify the registered target(s): ",
                    multiline=True,
                )
                state.conversation.append(
                    {"role": "user", "content": user_reply.strip() or persistence_issue}
                )
                state.status = "executing"
                self._begin_attempt_epoch(
                    state,
                    reason="user supplied persistence guidance",
                )
                self._save(state)
                feedback = persistence_issue
                continue

            # Quick verify
            quick_fail = self.orch._quick_verify_failure_details()
            if quick_fail:
                quick_reason, retryable = quick_fail
                self._print(f"Quick verify failed: {quick_reason}")
                feedback = self.orch._format_retry_feedback("pre_verify_check", reason=quick_reason)
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "quick_verify_fail",
                    "result": quick_reason,
                    "timestamp": self._now(),
                })
                diff_hash = self._compute_diff_hash()
                verify_sig = self._compute_verify_sig(quick_reason)
                self._update_stall_state(state, diff_hash, verify_sig)
                self._save(state)
                if not retryable:
                    self._print("Quick verify failure is deterministic. Stopping without retry.")
                    break
                stop = self._should_stop(state, quick_reason)
                if stop:
                    self._print(stop)
                    break
                continue

            # Gate verify
            verify = self._run_verify()
            verify_reason = "" if verify["ok"] else str(verify["reason"])
            self._append_verification_log(state, "verify", verify)
            self._save(state)

            if verify["ok"]:
                self._print("Verification passed!")
                self._run_session_persistence_action(state)
                state.status = "completed"
                state.resolution = "fixed"
                if not self._git_commit(state, "fix", reply=reply):
                    state.status = "failed"
                    state.resolution = "commit_failed"
                    self._save(state)
                    self._print(
                        "Verification passed, but the fix commit did not complete."
                    )
                    return state
                self._record_release_attestation(state, verify)
                self._release_baseline(state)
                self._print(f"Bug fix completed in session {state.session_id}.")
                return state

            self._print(f"Verification failed: {verify_reason}")
            if verify.get("retry_fix") is False:
                state.resolution = "verification_inconclusive"
                self._print(
                    "Verification could not establish a comparable regression; "
                    "stopping without another fix-agent attempt."
                )
                break
            diff_hash = self._compute_diff_hash()
            verify_sig = self._compute_verify_sig(verify_reason)
            self._update_stall_state(state, diff_hash, verify_sig)
            self._save(state)
            stop = self._should_stop(state, verify_reason)
            if stop:
                self._print(stop)
                break
            feedback = self.orch._format_retry_feedback("local_verification", reason=verify_reason)

        state.status = "failed"
        self._record_terminal_stop(state)
        self._save(state)
        self._print("Fix session stopped (no further progress). Session marked as failed.")
        return state

    def _phase_fix_after_child(self, state: SessionState) -> SessionState:
        """Re-check the original defect after a routed child returns."""

        from .io_utils import read_json

        state.return_phase = ""
        handoff_payload = read_json(Path(state.last_child_result_ref), default={})
        result = (
            dict(handoff_payload.get("result", {}))
            if isinstance(handoff_payload, dict)
            and isinstance(handoff_payload.get("result"), dict)
            else {}
        )
        child_status = str(result.get("status", ""))
        if child_status != "completed":
            feedback = str(
                result.get("summary")
                or result.get("resolution")
                or f"child returned status={child_status or 'unknown'}"
            )
            state.conversation.append(
                {
                    "role": "user",
                    "content": (
                        "The routed child did not complete successfully. Reassess the "
                        f"original issue and decide whether to resume or choose another route.\n{feedback}"
                    ),
                }
            )
            self._update_stall_state(
                state,
                self._compute_diff_hash(),
                self._compute_verify_sig(feedback),
            )
            stop = self._should_stop(state, feedback)
            if stop:
                state.status = "failed"
                state.resolution = "child_route_stalled"
                self._save(state)
                self._print(stop)
                return state
            state.status = "conversing"
            self._save(state)
            return state

        self._current_state = state
        verify = self._run_verify(scope="final")
        self._append_verification_log(state, "post_child_fix_verify", verify)
        self._save(state)
        if verify["ok"]:
            state.status = "completed"
            state.resolution = "resolved_by_iteration"
            state.stall_count = 0
            self._run_session_persistence_action(state)
            committed = self._git_commit(
                state,
                "fix",
                reply=str(result.get("summary", "")),
            )
            if not committed:
                state.status = "failed"
                state.resolution = "commit_failed"
                self._save(state)
                self._print(
                    "Verification passed, but the parent fix receipt was not committed."
                )
                return state
            self._record_release_attestation(state, verify)
            self._release_baseline(state)
            self._print("The routed iteration resolved the original issue.")
            return state

        reason = str(verify.get("reason", "post-child verification failed"))
        state.conversation.append(
            {
                "role": "user",
                "content": (
                    "The routed child completed, but the original defect verification "
                    f"still fails. Reassess the disposition.\n{reason}"
                ),
            }
        )
        diff_hash = self._compute_diff_hash()
        verify_sig = self._compute_verify_sig(reason)
        self._update_stall_state(state, diff_hash, verify_sig)
        stop = self._should_stop(state, reason)
        if stop:
            state.status = "failed"
            state.resolution = "post_iteration_verification_failed"
            self._save(state)
            self._print(stop)
            return state
        state.status = "conversing"
        self._save(state)
        return state

    # ── Phase 2b: Collab mode loop ───────────────────────────────

    def _phase_collab_loop(self, state: SessionState) -> SessionState:
        if not self._goal_environment_confirmed(state):
            state.status = "conversing"
            state.execution_log.append(
                {
                    "attempt": state.current_attempt,
                    "action": "goal_environment_required_before_collab",
                    "result": (
                        "returned to clarification before implementation routing"
                    ),
                    "timestamp": self._now(),
                }
            )
            self._save(state)
            return state
        self._prepare_collab_execution(state)
        feedback = ""
        while True:
            self._check_health_action()
            stop = self._should_stop(state, "attempt limit reached")
            if stop:
                self._print(stop)
                break
            self._record_agent_attempt(state)
            self._save(state)
            self._print(f"\n--- Collab iteration {state.current_attempt} ---")

            prompt = self._build_collab_prompt(state, feedback)
            before_snapshot = self._supervised_worktree_snapshot()
            if state.workflow_id:
                durable_restore = (
                    self.project_root
                    / ".auto-agents"
                    / "state"
                    / "workflows"
                    / state.workflow_id
                    / "checkpoints"
                    / f"collab-{state.session_id}-{state.current_attempt}"
                )
                restore_context = contextlib.nullcontext(str(durable_restore))
            else:
                durable_restore = None
                restore_context = tempfile.TemporaryDirectory(
                    prefix="auto-agents-collab-readonly-"
                )
            with restore_context as restore_tmp:
                restore_root = Path(restore_tmp)
                self._capture_collab_restore_point(restore_root, before_snapshot)
                try:
                    reply = self._call_agent(
                        state, f"collab-{state.current_attempt}", prompt
                    )
                except KeyboardInterrupt:
                    offending = self._restore_collab_mutations(
                        state,
                        before_snapshot,
                        restore_root,
                    )
                    if offending:
                        self._invalidate_provider_continuations(
                            state,
                            keys=["collab"],
                            reason="interrupted collab response was rolled back",
                        )
                    if durable_restore is not None:
                        shutil.rmtree(durable_restore, ignore_errors=True)
                    raise
                except RuntimeError as exc:
                    offending = self._restore_collab_mutations(
                        state,
                        before_snapshot,
                        restore_root,
                    )
                    if offending:
                        self._invalidate_provider_continuations(
                            state,
                            keys=["collab"],
                            reason="failed collab response was rolled back",
                        )
                    if durable_restore is not None:
                        shutil.rmtree(durable_restore, ignore_errors=True)
                    err_msg = str(exc)
                    state.consecutive_agent_errors += 1
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "agent_error",
                        "result": err_msg[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    stop = self._should_stop(state, "agent_error")
                    if stop:
                        self._print(stop)
                        break
                    feedback = self._build_error_feedback(err_msg)
                    self._print("Will retry on next iteration.")
                    continue
                offending = self._restore_collab_mutations(
                    state,
                    before_snapshot,
                    restore_root,
                )
                if offending:
                    self._invalidate_provider_continuations(
                        state,
                        keys=["collab"],
                        reason="collab response rejected after read-only rollback",
                    )
                    reason = (
                        "Collab is diagnostic-only and restored product mutations from "
                        "the agent attempt. Changed paths: " + ", ".join(offending[:10])
                    )
                    state.execution_log.append(
                        {
                            "attempt": state.current_attempt,
                            "action": "collab_mutation_restored",
                            "result": reason[:500],
                            "timestamp": self._now(),
                        }
                    )
                    self._update_stall_state(
                        state,
                        self._compute_diff_hash(),
                        self._compute_verify_sig(reason),
                    )
                    self._save(state)
                    if durable_restore is not None:
                        shutil.rmtree(durable_restore, ignore_errors=True)
                    stop = self._should_stop(state, reason)
                    if stop:
                        self._print(stop)
                        break
                    feedback = reason + ". Diagnose and emit ROUTE_WORKFLOW v1 instead of editing."
                    continue
                if durable_restore is not None:
                    shutil.rmtree(durable_restore, ignore_errors=True)

            # Successful agent call resets transient error counter
            state.consecutive_agent_errors = 0

            state.conversation.append({"role": "agent", "content": reply})
            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "collab",
                "result": reply[:500],
                "timestamp": self._now(),
            })
            self._save(state)

            routed, normalization_error = self._route_collab_workflow_reply(
                state,
                reply,
            )
            if routed is not None:
                return routed
            if normalization_error:
                feedback = normalization_error
                state.execution_log.append(
                    {
                        "attempt": state.current_attempt,
                        "action": "collab_protocol_normalization_retry",
                        "result": normalization_error[:500],
                        "timestamp": self._now(),
                    }
                )
                self._update_stall_state(
                    state,
                    self._compute_diff_hash(),
                    self._compute_verify_sig(normalization_error),
                )
                self._save(state)
                stop = self._should_stop(state, normalization_error)
                if stop:
                    self._print(stop)
                    break
                continue

            # Check for NEED_USER_ASSIST — counts as progress
            assistance, decision_class, assistance_parse_error = (
                self._assistance_request(reply)
            )
            if assistance_parse_error:
                self._reject_collab_assistance(state, assistance_parse_error)
                feedback = assistance_parse_error
                continue
            if assistance:
                assistance_error = self._collab_assistance_error(
                    state,
                    assistance,
                    decision_class=decision_class,
                )
                if assistance_error:
                    self._reject_collab_assistance(state, assistance_error)
                    stop = self._should_stop(state, assistance_error)
                    if stop:
                        self._print(stop)
                        break
                    feedback = assistance_error
                    continue
                self._handle_collab_assistance(state, reply, assistance)
                feedback = ""
                continue

            # Check for GOAL_ACHIEVED — counts as progress
            achieved_match = _GOAL_ACHIEVED.search(reply)
            if achieved_match:
                terminal = self._handle_collab_goal_achieved(
                    state,
                    reply,
                    achieved_match,
                )
                if terminal is not None:
                    return terminal
                feedback = ""
                continue

            # General diagnostic output (no special marker). It is not treated
            # as implementation progress and never commits project files.
            self._print(f"\nAgent:\n{reply.strip()}")
            diff_hash = self._compute_diff_hash()
            verify_sig = self._compute_verify_sig(reply)
            self._update_stall_state(state, diff_hash, verify_sig)
            self._save(state)
            stop = self._should_stop(state, "collab produced no actionable marker")
            if stop:
                self._print(stop)
                break
            feedback = (
                "Continue diagnosis. When the next action is clear, emit exactly one "
                "ROUTE_WORKFLOW v1 marker; use GOAL_ACHIEVED only after the user's "
                "overall goal is actually satisfied."
            )

        state.status = "failed"
        self._record_terminal_stop(state)
        self._save(state)
        self._print("Collab session stopped (no further progress). Session marked as failed.")
        return state

    def _prepare_collab_execution(self, state: SessionState) -> None:
        self._current_state = state
        # Collab can be the first command after a v2 -> v3 policy migration.
        # Materialize the generated verification config before resolving the
        # plan or capturing the shared session baseline.
        self.orch._apply_generated_verification_config()
        final_plan = self._release_gate_plan()
        if final_plan.commands or final_plan.parallel_groups:
            self._ensure_baseline(state)

    def _handle_collab_goal_achieved(
        self,
        state: SessionState,
        reply: str,
        achieved_match: re.Match,
    ) -> Optional[SessionState]:
        display = _GOAL_ACHIEVED.sub("", reply).strip()
        if display:
            self._print(f"\nAgent:\n{display}")
        self._print(
            f"\nAgent believes the goal is achieved: {achieved_match.group(1)}"
        )

        verify = self._run_verify(scope="final")
        self._append_verification_log(state, "verify", verify)
        self._save(state)

        if not verify["ok"]:
            verify_reason = str(verify["reason"])
            self._print(f"Verification failed: {verify_reason}")
            self._print("Continuing the loop to fix verification issues.")
            retry_feedback, stop = self._collab_verify_failure(
                state,
                verify_reason,
            )
            if stop:
                self._print(stop)
                state.status = "failed"
                self._record_terminal_stop(state)
                self._save(state)
                self._print(
                    "Collab session stopped (no further progress). "
                    "Session marked as failed."
                )
                return state
            state.conversation.append(
                {"role": "user", "content": retry_feedback}
            )
            self._save(state)
            return None

        state.stall_count = 0
        self._print("Final verification passed!")
        if (
            self._authorization_policy(state).decide("completion_confirmation")
            == "AUTO_EXECUTE"
        ):
            answer = "y"
            state.execution_log.append(
                {
                    "attempt": state.current_attempt,
                    "action": "completion_auto_confirmed",
                    "result": "final verification passed under auto-approve",
                    "timestamp": self._now(),
                }
            )
            self._save(state)
        else:
            answer = self._prompt_user(
                "Do you confirm the goal is achieved? (y/n) [y]: ",
                default="y",
            )
        if answer.strip().lower() not in ("n", "no"):
            self._run_session_persistence_action(state)
            state.status = "completed"
            state.resolution = "goal_achieved"
            if not self._git_commit(state, "collab", reply=reply):
                state.status = "failed"
                state.resolution = "commit_failed"
                self._save(state)
                self._print(
                    "Final verification passed, but the collab receipt was not committed."
                )
                return state
            self._record_release_attestation(state, verify)
            self._release_baseline(state)
            self._print(
                f"Collaborative session {state.session_id} completed successfully."
            )
            return state

        committed = self._commit_verified_progress(state, "collab", reply=reply)
        if committed:
            self._print("Verified progress committed before continuing.")
        user_feedback = self._prompt_user(
            "What still needs to be done? ",
            multiline=True,
        )
        state.conversation.append(
            {
                "role": "user",
                "content": user_feedback.strip() or "Not yet done.",
            }
        )
        self._begin_attempt_epoch(
            state,
            reason="user rejected completion and supplied new guidance",
        )
        self._save(state)
        self._print_agent_thinking()
        return None

    def _handle_collab_assistance(
        self,
        state: SessionState,
        reply: str,
        assistance: str,
    ) -> None:
        state.stall_count = 0
        self._print(f"\nAgent:\n{reply.strip()}")
        self._print(f"\nAgent needs your assistance: {assistance}")
        state.status = "waiting_user"
        self._save(state)
        user_reply = self._prompt_user(
            "\nYour response (or result): ",
            multiline=True,
        )
        state.conversation.append(
            {"role": "user", "content": user_reply.strip() or "Done."}
        )
        state.status = "executing"
        self._begin_attempt_epoch(
            state,
            reason="user assistance supplied",
        )
        self._save(state)
        self._print_agent_thinking()

    def _phase_provider_resolve_execute(self, state: SessionState) -> SessionState:
        self._current_state = state
        feedback = ""
        while True:
            self._check_health_action()
            if state.current_attempt >= state.max_attempts:
                self._print(
                    f"Provider recovery attempt limit ({state.max_attempts}) "
                    "reached. Stopping."
                )
                break
            state.current_attempt += 1
            self._save(state)
            self._print(f"\n--- Provider recovery iteration {state.current_attempt} ---")

            try:
                trace_before = load_requirements_trace(self.project_root, normalize=False)
            except Exception as exc:
                trace_before = None
                baseline_errors = [f"requirements trace could not be loaded: {exc}"]
            else:
                baseline_errors = validate_requirements_trace_payload(trace_before)
            if baseline_errors:
                reason = self._format_provider_validation_errors(
                    "provider-resolve preflight rejected the existing requirements trace",
                    baseline_errors,
                )
                state.status = "failed"
                state.resolution = "preflight_blocked"
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "preflight_blocked",
                    "result": reason[:500],
                    "timestamp": self._now(),
                })
                self._save(state)
                raise RuntimeError(reason)

            prompt = self._build_provider_resolve_prompt(state, feedback)
            approved_defer_ids = self._provider_defer_approved_ids(state)
            with tempfile.TemporaryDirectory(prefix="auto-agents-provider-restore-") as restore_tmp:
                restore_root = Path(restore_tmp)
                self._capture_provider_artifact_restore_point(restore_root)
                worktree_before = self._supervised_worktree_snapshot()
                try:
                    reply = self._call_agent(
                        state,
                        f"provider-resolve-{state.current_attempt}",
                        prompt,
                    )
                except RuntimeError as exc:
                    self._restore_provider_artifacts(restore_root)
                    violation = self.orch._stage_mutation_scope_violation(
                        stage="provider_resolve",
                        stage_key=f"provider-resolve-{state.current_attempt}",
                        run_id=state.session_id,
                        before_snapshot=worktree_before,
                    )
                    if violation is not None:
                        offending, allowed_scope = violation
                        raise RuntimeError(
                            "provider-resolve modified files outside its ownership while the agent call failed. "
                            f"Changed paths: {self.orch._changed_path_preview(offending)}. "
                            f"Allowed scope: {'; '.join(allowed_scope)}."
                        ) from exc
                    err_msg = str(exc)
                    state.consecutive_agent_errors += 1
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "agent_error",
                        "result": err_msg[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    stop = self._should_stop(state, "agent_error")
                    if stop:
                        self._print(stop)
                        break
                    feedback = self._build_error_feedback(err_msg)
                    self._print("Will retry on next iteration.")
                    continue

                state.consecutive_agent_errors = 0
                state.conversation.append({"role": "agent", "content": reply})
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "provider_resolve",
                    "result": reply[:500],
                    "timestamp": self._now(),
                })
                self._save(state)

                violation = self.orch._stage_mutation_scope_violation(
                    stage="provider_resolve",
                    stage_key=f"provider-resolve-{state.current_attempt}",
                    run_id=state.session_id,
                    before_snapshot=worktree_before,
                )
                if violation is not None:
                    self._restore_provider_artifacts(restore_root)
                    offending, allowed_scope = violation
                    reason = (
                        "provider-resolve modified files outside its ownership. "
                        f"Changed paths: {self.orch._changed_path_preview(offending)}. "
                        f"Allowed scope: {'; '.join(allowed_scope)}."
                    )
                    state.status = "failed"
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "provider_scope_rejected",
                        "result": reason[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    raise RuntimeError(reason)

                try:
                    trace_after = load_requirements_trace(self.project_root, normalize=False)
                except Exception as exc:
                    trace_errors = [f"requirements trace could not be loaded after the attempt: {exc}"]
                else:
                    trace_errors = validate_provider_resolve_trace_transition(
                        trace_before,
                        trace_after,
                        deferred_requirement_ids=approved_defer_ids,
                    )
                    trace_errors.extend(validate_requirements_trace_payload(trace_after))
                trace_errors = list(dict.fromkeys(trace_errors))
                if trace_errors:
                    self._restore_provider_artifacts(restore_root)
                    reason = self._format_provider_validation_errors(
                        "provider-resolve rejected requirements trace changes and restored the attempt",
                        trace_errors,
                    )
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "provider_trace_rejected",
                        "result": reason[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    diff_hash = self._compute_diff_hash()
                    verify_sig = self._compute_verify_sig(reason)
                    self._update_stall_state(state, diff_hash, verify_sig)
                    self._save(state)
                    stop = self._should_stop(state, reason)
                    if stop:
                        self._print(stop)
                        break
                    feedback = reason
                    self._print(reason)
                    continue

                clarify_match = _REQUIRES_CLARIFY.search(reply)
                if clarify_match:
                    self._restore_provider_artifacts(restore_root)
                    reason = clarify_match.group(1).strip()
                    self.orch.route_provider_contract_change_to_clarify(reason)
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "routed_to_clarify",
                        "result": reason[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    try:
                        resumed = self.orch.resume_saved_run()
                    except RuntimeError:
                        state.status = "failed"
                        state.resolution = "clarify_handoff_failed"
                        self._save(state)
                        raise
                    if resumed.status == "failed":
                        state.status = "failed"
                        state.resolution = "clarify_handoff_failed"
                        self._save(state)
                        raise RuntimeError(resumed.last_error or "run failed after clarify handoff")
                    state.status = "completed"
                    state.resolution = "routed_to_clarify"
                    self._save(state)
                    return state

                defer_match = _NEED_USER_DEFER.search(reply)
                if defer_match:
                    requested_ids = sorted(set(_REQUIREMENT_ID.findall(defer_match.group(1))))
                    if not requested_ids:
                        feedback = (
                            "NEED_USER_DEFER must name at least one requirement ID before the '|' separator."
                        )
                        continue
                    state.stall_count = 0
                    self._print(f"\nAgent:\n{reply.strip()}")
                    state.status = "waiting_user"
                    self._save(state)
                    user_reply = self._prompt_user(
                        f"\nDefer {', '.join(requested_ids)}? {defer_match.group(2).strip()} (yes/no): ",
                        multiline=False,
                    )
                    approved = self._is_affirmative_provider_decision(user_reply)
                    state.conversation.append({
                        "role": "user",
                        "content": user_reply.strip() or "No.",
                    })
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "provider_defer_approved" if approved else "provider_defer_denied",
                        "result": ",".join(requested_ids),
                        "timestamp": self._now(),
                    })
                    state.status = "executing"
                    self._save(state)
                    self._print_agent_thinking()
                    feedback = "" if approved else "The user did not approve deferring those requirements."
                    continue

                assist_match = _NEED_USER_ASSIST.search(reply)
                if assist_match:
                    state.stall_count = 0
                    self._print(f"\nAgent:\n{reply.strip()}")
                    self._print(f"\nAgent needs your assistance: {assist_match.group(1)}")
                    state.status = "waiting_user"
                    self._save(state)
                    user_reply = self._prompt_user("\nYour response (or decision): ", multiline=True)
                    state.conversation.append({"role": "user", "content": user_reply.strip() or "Done."})
                    state.status = "executing"
                    self._save(state)
                    self._print_agent_thinking()
                    feedback = ""
                    continue

            self._print(f"\nAgent:\n{reply.strip()}")
            self.orch.bind_resolved_provider_reference_contracts()
            verify = self.orch.provider_research_resolution_report()
            verify_reason = "" if verify["eligible"] is False and not verify["blockers"] else str(
                verify.get("reason") or "\n".join(
                    f"{item.get('requirement_id')}: {item.get('reason')}"
                    for item in verify.get("blockers", [])
                    if isinstance(item, dict)
                )
            ).strip()

            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "provider_verify",
                "result": "pass" if not verify.get("blockers") else verify_reason,
                "timestamp": self._now(),
            })
            self._save(state)

            if not verify.get("blockers"):
                preflight = self.orch.validate()
                if not preflight.get("ok"):
                    reason = self._format_provider_validation_errors(
                        "provider references pass, but run preflight is still blocked",
                        [str(item) for item in preflight.get("errors", [])],
                    )
                    state.status = "failed"
                    state.resolution = "preflight_blocked"
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "preflight_blocked",
                        "result": reason[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    raise RuntimeError(reason)
                blocked_run_before_resume = load_run_state(self.project_root)
                contract_scope_message = blocked_run_before_resume.last_error
                consumer_contract_fingerprint = (
                    self.orch.provider_recovery_contract_fingerprint(
                        state=blocked_run_before_resume,
                        blocker_message=contract_scope_message,
                    )
                )
                self._print("Provider references and full preflight now pass. Resuming run...")
                try:
                    resumed = self.orch.resume_saved_run()
                except RuntimeError as exc:
                    err_msg = str(exc)
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "resume_run_failed",
                        "result": err_msg[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    if self.orch.is_provider_research_blocked_error(err_msg):
                        if self._handoff_unsatisfied_provider_contract(
                            state,
                            expected_contract_fingerprint=(
                                consumer_contract_fingerprint
                            ),
                            blocker_message=err_msg,
                            contract_scope_message=contract_scope_message,
                        ):
                            return state
                        diff_hash = self._compute_diff_hash()
                        verify_sig = self._compute_verify_sig(err_msg)
                        self._update_stall_state(state, diff_hash, verify_sig)
                        self._save(state)
                        stop = self._should_stop(state, err_msg)
                        if stop:
                            self._print(stop)
                            break
                        feedback = (
                            "The provider references were edited, but rerunning the pipeline still reported "
                            "a provider_research blocker.\n"
                            f"{err_msg}\n"
                            "Re-read the current provider reference files and lock entries, explain the remaining "
                            "gap, and apply the minimal additional edits needed."
                        )
                        continue
                    state.status = "failed"
                    state.resolution = "provider_research_resolved_run_failed"
                    self._save(state)
                    raise

                if resumed.status in {"blocked", "waiting_user"}:
                    state.status = resumed.status
                    state.resolution = (
                        "provider_recovery_contract_unsatisfied"
                        if resumed.status == "blocked"
                        else "provider_recovery_waiting_user"
                    )
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": f"resume_run_{resumed.status}",
                        "result": (
                            resumed.last_error
                            or f"run status={resumed.status}"
                        )[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    self._print(
                        "Provider recovery handed off to the persisted run "
                        f"state ({resumed.status})."
                    )
                    return state

                if resumed.status == "failed":
                    resumed_error = resumed.last_error or "resumed run returned failed"
                    if (
                        self.orch.is_provider_research_blocked_error(resumed_error)
                        and self._handoff_unsatisfied_provider_contract(
                            state,
                            expected_contract_fingerprint=(
                                consumer_contract_fingerprint
                            ),
                            blocker_message=resumed_error,
                            contract_scope_message=contract_scope_message,
                        )
                    ):
                        return state
                    state.status = "failed"
                    state.resolution = "provider_research_resolved_run_failed"
                    state.execution_log.append({
                        "attempt": state.current_attempt,
                        "action": "resume_run_failed",
                        "result": resumed_error[:500],
                        "timestamp": self._now(),
                    })
                    self._save(state)
                    raise RuntimeError(resumed_error)

                state.status = "completed"
                state.resolution = "provider_research_resolved"
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "resume_run",
                    "result": f"run status={resumed.status}",
                    "timestamp": self._now(),
                })
                self._save(state)
                self._print("Provider-research recovery completed and the run was resumed.")
                return state

            diff_hash = self._compute_diff_hash()
            verify_sig = self._compute_verify_sig(verify_reason)
            self._update_stall_state(state, diff_hash, verify_sig)
            self._save(state)
            stop = self._should_stop(state, verify_reason)
            if stop:
                self._print(stop)
                break
            feedback = (
                "Provider references are still unresolved.\n"
                f"{verify_reason}\n"
                "Update only provider-research artifacts to resolve these blockers."
            )

        state.status = "failed"
        self._save(state)
        self._print("Provider recovery session stopped (no further progress). Session marked as failed.")
        return state

    # ── Prompt builders ──────────────────────────────────────────

    def _build_converse_prompt(self, state: SessionState) -> str:
        if self.mode == "provider_resolve":
            report = self.orch.provider_research_resolution_report()
            lines = [
                f"Project root: {self.project_root}",
                "",
                "You are helping recover a blocked provider_research stage.",
                "The user wants to unblock provider references through conversation and then continue the run.",
                "",
                "Blocked-run summary:",
                state.goal,
                "",
                f"Last run error: {report.get('last_error', '')}",
                "",
                "--- Conversation History ---",
            ]
            for message_index, msg in enumerate(state.conversation):
                role = msg.get("role", "user")
                content = msg.get("content", "")
                lines.append(ContextBlock(content, f"Conversation: {role}", f"conversation:{message_index}"))
            lines.extend([
                "",
                "Analyze the unresolved provider references and the user's goal.",
                "- Ask targeted questions only when a decision is still needed.",
                "- If the unblock path is clear enough to begin editing provider-research artifacts, output 'GOAL_CLEAR' on a line by itself at the end.",
                "- Do not propose product-code changes in this mode.",
                "- Keep the scope limited to provider reference markdown, provider_references.lock.json, and tightly coupled requirement trace metadata only when the user chooses defer/assumption approval.",
                self.orch._document_language_instruction(),
            ])
            return compose_prompt(lines, purpose=self.mode + "_converse")

        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        label = "bug" if self.mode == "fix" else "goal"
        lines = [
            f"Project root: {self.project_root}",
            f"Project brief: {brief}",
            f"Architecture: {architecture}",
            "",
            f"You are working on a completed project. The user reports a {label}.",
            "",
            f"User's {label} description:",
            state.goal,
        ]
        if self.mode == "fix" and state.parent_handoff_id:
            issue_path = (
                self.project_root
                / ".auto-agents"
                / "state"
                / "sessions"
                / state.session_id
                / "issue.md"
            )
            routed_issue = read_text(issue_path).strip() if issue_path.is_file() else ""
            if routed_issue:
                lines.extend(
                    [
                        "",
                        "--- Authoritative Routed Issue Brief ---",
                        f"Source: {issue_path}",
                        routed_issue,
                        "",
                        (
                            "This routed issue brief defines the child fix scope. "
                            "Its expected/actual behavior and constraints take "
                            "precedence over the broader reported goal, which remains "
                            "the parent workflow's acceptance target."
                        ),
                    ]
                )
        lines.extend(["", "--- Conversation History ---"])
        for message_index, msg in enumerate(state.conversation):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(ContextBlock(content, f"Conversation: {role}", f"conversation:{message_index}"))

        if self.mode == "collab" or self._goal_environment_confirmed(state):
            lines.extend(["", *self._goal_environment_prompt_lines(state)])

        lines.extend([
            "",
            f"Analyze the codebase and the {label} description.",
            "- If you need more information, ask specific questions.",
        ])
        if self.mode == "fix":
            lines.extend([
                "- This clarification/classification phase is read-only. Do not modify files, create generated artifacts, or run mutating commands.",
                "- Classify the work and put the disposition in the final response before any implementation begins.",
                "- decision='fix' only for a bounded defect against existing behavior; include summary, reason, reproduction, expected, actual, evidence_refs, affected_contracts, verification_command, and persistence_change.",
                "- decision='run_iteration' when resolution needs new public capability, changed requirements, architecture expansion, or a persistence-model change; include reason and spec_seed with title, goal, gap, capability, acceptance, non_goals, evidence, and open_decisions.",
                "- decision='not_bug' for expected/configuration/user-misunderstanding cases, decision='need_user' with question when evidence is insufficient, or decision='resume_child' with resume_handoff_id for a prior routed child.",
                PromptBlock("- Preferred exact wire form: FIX_DISPOSITION v1: {\"decision\":\"...\",...}", kind="output"),
                PromptBlock("- Do not encode the marker only as a JSON field and do not place the disposition only in commentary or tool output.", kind="output"),
                PromptBlock("- The final response must contain the valid one-line disposition and no other disposition marker; put the explanation inside its JSON fields.", kind="output"),
                "- Match repository verification conventions when choosing verification_command.",
                "- If the project uses a local conda env at ./.conda, every Python-oriented "
                "verification_command must run inside it via 'conda run -p ./.conda ...'.",
            ])
            gate_commands = self._gate_commands()
            if gate_commands:
                lines.append(
                    "- Current repository gate commands (reuse them as guidance for FIX_VERIFY when relevant):"
                )
                lines.extend(f"  - {command}" for command in gate_commands)
        else:
            lines.extend(
                [
                    PromptBlock("- Never output FIX_DISPOSITION in collab mode; that protocol belongs to the child fix workflow.", kind="output"),
                    "- If an existing-behavior defect is already clear, output one single-line ROUTE_WORKFLOW v1 marker with target='fix', reason, summary, and issue_seed.",
                    "- If a missing capability or requirements, architecture, or persistence change is already clear, output one single-line ROUTE_WORKFLOW v1 marker with target='run', reason, summary, and spec_seed.",
                    "- Otherwise, if the goal is clear enough for more read-only diagnosis, output 'GOAL_CLEAR' on a line by itself at the end.",
                ]
            )
        if self.mode != "fix":
            lines.append(
                "- Always explain your understanding before asking questions or declaring ready."
            )
        lines.extend(
            [
                "- Never infer permission to discard or migrate persistent data. Ask the user when an explicit decision is missing.",
                (
                    "- A user-visible generated artifact is not successful when it "
                    "comes from a fake, mock, fixture, placeholder, or synthetic "
                    "adapter unless the user explicitly requested a stub. File "
                    "existence, status flags, and technical media probes alone do "
                    "not prove semantic or visual acceptance."
                ),
                (
                    "- For generated deliverables, verify that content matches the "
                    "user input and that provider provenance satisfies the requested "
                    "acceptance. If a real or paid provider, credential, or approval "
                    "is required, request that input instead of substituting a fake "
                    "artifact."
                ),
                self.orch._document_language_instruction(),
            ]
        )
        return compose_prompt(lines, purpose=self.mode + "_converse")

    def _build_provider_resolve_prompt(self, state: SessionState, feedback: str) -> str:
        report = self.orch.provider_research_resolution_report()
        trace_path = self.project_root / ".auto-agents" / "state" / "requirements_trace.json"
        lock_path = self.project_root / ".auto-agents" / "state" / "provider_references.lock.json"
        refs_dir = self.project_root / ".auto-agents" / "docs" / "provider_references"
        consolidated = self._consolidate_goal(state)
        lines = [
            f"Project root: {self.project_root}",
            f"Requirements trace: {trace_path}",
            f"Provider references lock: {lock_path}",
            f"Provider references directory: {refs_dir}",
            "",
            "Recovery goal:",
            *self._goal_contexts(state),
            "",
            f"Current run error: {report.get('last_error', '')}",
            "",
            "Current unresolved provider references:",
        ]
        blockers = report.get("blockers", [])
        if isinstance(blockers, list):
            for blocker in blockers:
                if not isinstance(blocker, dict):
                    continue
                lines.append(
                    f"- {blocker.get('requirement_id')}: {blocker.get('reference') or '(missing)'} "
                    f"is {blocker.get('status')} ({blocker.get('reason')})"
                )
        lines.extend([
            "",
            "--- Conversation History ---",
        ])
        for message_index, msg in [(i, m) for i, m in enumerate(state.conversation) if m.get("role") != "user"][-20:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(ContextBlock(content, f"Conversation: {role}", f"conversation:{message_index}"))

        if state.execution_log:
            lines.extend(["", "--- Execution Log (recent) ---"])
            for entry in state.execution_log[-10:]:
                lines.append(
                    f"  Attempt {entry.get('attempt')}: {entry.get('action')} -> "
                    f"{str(entry.get('result', ''))[:200]}"
                )

        if feedback:
            lines.extend([
                "",
                "Previous attempt issues:",
                ContextBlock(feedback, "Previous attempt issues"),
                "",
            ])

        lines.extend([
            "",
            "Your task:",
            "1. Inspect the current provider reference markdown and lock files.",
            "2. Discuss the unblock path with the user when a decision is needed.",
            "3. Apply only the minimal edits needed to provider-research artifacts.",
            "4. If you need the user to choose between options, output 'NEED_USER_ASSIST: <question or decision needed>' on a line by itself.",
            "5. To ask for an explicit defer decision, output 'NEED_USER_DEFER: REQ-001,REQ-002 | <question>' on a line by itself; do not change status before approval.",
            "6. If the provider finding requires any normative requirement change, do not edit the trace; output 'REQUIRES_CLARIFY: <reason>' on a line by itself.",
            "7. Do not modify product/runtime code, implementation tasks, or unrelated state files.",
            "",
            "Success criteria for this mode:",
            "- every required provider reference exists locally",
            "- required references now resolve to passing statuses (verified, assumption_approved, or deferred)",
            "- the pipeline can be resumed from the stored run context",
            "",
            "When editing:",
            "- keep markdown factual and aligned with the user's decision",
            "- update provider_references.lock.json consistently with the markdown file",
            "- if the user approves assumptions, record assumption_approved explicitly",
            "- requirements_trace.json is read-only except for notes and an explicitly approved active-to-deferred status change",
            "- record approval context in notes, never in source, text, or another proof-bearing contract field",
            "- contract_sha256 and provider lock consumer contract hashes are engine-owned; do not calculate or edit them",
            "- do not add, remove, reorder, reactivate, or supersede requirements in provider-resolve",
            *provider_policy_prompt_lines("provider_research"),
            "",
            PromptBlock("Final response: brief status update of what you changed and why.", kind="output"),
        ])
        return compose_prompt(lines, purpose="provider_resolve")

    def _build_fix_prompt(self, state: SessionState, feedback: str) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        consolidated = self._consolidate_goal(state)
        lines = [
            f"Project root: {self.project_root}",
            f"Project brief: {brief}",
            f"Architecture: {architecture}",
            "",
            "Bug description (from conversation):",
            *self._goal_contexts(state),
            "",
        ]
        if self._goal_environment_confirmed(state):
            lines.extend([*self._goal_environment_prompt_lines(state), ""])
        if feedback:
            lines.extend([
                "Previous attempt issues:",
                ContextBlock(feedback, "Previous attempt issues"),
                "",
                "Use the failure evidence to identify the root cause and apply a bounded fix. "
                "Explain the repair target briefly when it helps the user follow progress.",
                "",
            ])
        lines.extend([
            "Fix this bug:",
            "0. Re-check the scope before editing. If the correct resolution requires new public capability, changed requirements, architecture expansion, or a persistence-model change, do not edit files; output one single-line FIX_DISPOSITION v1 JSON object with decision='run_iteration', reason, and spec_seed.",
            "1. Identify the root cause",
            "2. Apply the minimal fix",
            "3. Ensure focused behavioral coverage; reuse sufficient tests or add missing coverage",
            "4. Do not modify .auto-agents state files",
            *provider_policy_prompt_lines("fix"),
            "If this is a Python project, use the project-local conda env at ./.conda and install packages only inside it.",
            "",
            "Final response: short summary of what you changed and why.",
            "",
            "IMPORTANT: At the very end of your reply, output exactly one line in the form:",
            "  COMMIT_MESSAGE: <one-sentence description of the bug fix>",
            "Rules for this line:",
            "- Describe what the bug was and/or how it was fixed (imperative mood, e.g. 'fix null pointer in public voice handler').",
            "- Plain prose only: NO file paths, NO markdown links, NO code, NO shell commands, NO backticks, NO brackets.",
            "- Keep it under 72 characters; write it in the same language as the bug description.",
            "- This line is used verbatim as the git commit subject, so it must stand alone and be human-readable.",
        ])
        output_start = next(i for i, line in enumerate(lines)
                            if isinstance(line, str) and line.startswith("Final response:"))
        prompt = compose_prompt([*lines[:output_start], *[
            PromptBlock(line, kind="output") for line in lines[output_start:] if line
        ]], purpose="fix")
        repo_map = self._build_repo_map_section_for_session(consolidated, feedback)
        if repo_map:
            prompt = append_context(prompt, repo_map, "Repo Map")
        return prompt

    def _build_repo_map_section_for_session(self, goal: str, feedback: str = "") -> str:
        """Inject a repo map for fix-mode prompts when enabled.

        Reuses the orchestrator's RepoMapBuilder so the cache is shared and
        all configuration goes through a single place.
        """
        builder = self.orch._get_repo_map_builder() if hasattr(self.orch, "_get_repo_map_builder") else None
        if builder is None:
            self.orch._last_repo_map_result = None
            return ""

        class _SessionTaskLike:
            title = ""
            description = ""
            acceptance: List[str] = []
            scope_boundaries = ""
            commit_message = ""

        task_like = _SessionTaskLike()
        task_like.description = goal or ""
        task_like.acceptance = [feedback] if feedback else []

        budget = self.orch.config.repo_map.review_budget_tokens
        result = builder.build(task_like, budget_tokens=budget)
        self.orch._last_repo_map_result = result
        return result.text or ""

    def _describe_session_for_resume(self, state: SessionState) -> str:
        goal = state.goal or "(no goal recorded)"
        lines = [
            f"session_id={state.session_id}",
            f"status={state.status}",
            f"updated={self._format_session_timestamp(state.updated_at or state.created_at)}",
        ]
        goal_lines = goal.splitlines() or [goal]
        lines.append(f"goal={goal_lines[0]}")
        lines.extend(goal_lines[1:])
        return "\n".join(lines)

    @staticmethod
    def _format_session_timestamp(value: str) -> str:
        if not value:
            return "unknown"
        normalized = value
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return value
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    def _select_resumable_session(self, resumable: List[SessionState]) -> Optional[SessionState]:
        self._print(
            f"Found {len(resumable)} unfinished {self.mode} session(s), newest first. "
            f"Default recommendation: {resumable[0].session_id}"
        )
        for index, item in enumerate(resumable, start=1):
            detail = self._describe_session_for_resume(item).replace("\n", "\n     ")
            self._print(f"  {index}.\n     {detail}")
        prompt = (
            "Choose a session number or ID to resume, or enter 'n' to start a new one "
            f"[1]: "
        )
        while True:
            answer = self._prompt_user(prompt, default="1").strip()
            if not answer:
                answer = "1"
            lowered = answer.lower()
            if lowered in ("n", "no", "new"):
                return None
            if answer.isdigit():
                index = int(answer)
                if 1 <= index <= len(resumable):
                    return resumable[index - 1]
            else:
                for item in resumable:
                    if item.session_id == answer:
                        return item
            self._print("Invalid selection. Enter a listed number, a session ID, or 'n' for a new session.")

    def _build_collab_prompt(self, state: SessionState, feedback: str) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        consolidated = self._consolidate_goal(state)
        lines = [
            f"Project root: {self.project_root}",
            f"Project brief: {brief}",
            f"Architecture: {architecture}",
            "",
            "User's goal:",
            *self._goal_contexts(state),
            "",
            *self._goal_environment_prompt_lines(state),
            "",
            "--- Conversation History ---",
        ]
        for message_index, msg in [(i, m) for i, m in enumerate(state.conversation) if m.get("role") != "user"][-20:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(ContextBlock(content, f"Conversation: {role}", f"conversation:{message_index}"))

        if state.execution_log:
            lines.extend(["", "--- Execution Log (recent) ---"])
            for entry_index, entry in list(enumerate(state.execution_log))[-10:]:
                lines.append(ContextBlock(f"Attempt {entry.get('attempt')}: {entry.get('action')} -> {str(entry.get('result', ''))[:200]}", "Recent execution log", f"execution:{entry_index}"))

        if feedback:
            lines.extend([
                "",
                "Previous attempt issues:",
                ContextBlock(feedback, "Previous attempt issues"),
                "",
            ])

        lines.extend([
            "",
            ContextBlock(
                "Current local attempt budget: "
                f"epoch={state.attempt_epoch}, "
                f"provider_calls={self._attempts_in_current_epoch(state)}, "
                f"hard_ceiling={state.hard_ceiling}."
            ),
            "",
            "You are the read-only diagnostic and routing frame for a collaborative workflow. Your task:",
            "1. Analyze the current state of the code and previous execution results without editing product files",
            "2. Determine the next workflow needed to achieve the user's overall goal",
            "3. Ask the user only for a goal choice, credential, rights attestation, unbudgeted external cost, destructive change, irreversible product decision, or an external observation only the user can perform.",
            "   Output NEED_USER_ASSIST v1: {\"decision_class\":\"<allowed class>\",\"question\":\"<plain project-specific question>\"} on one line.",
            "4. For an existing-behavior defect, output one single-line ROUTE_WORKFLOW v1 JSON marker with target='fix', reason, summary, and issue_seed",
            "5. For missing/new capability or a requirements, architecture, or persistence change, output one single-line ROUTE_WORKFLOW v1 JSON marker with target='run', reason, summary, and spec_seed",
            "6. To retry a previously returned child after its blocker changed, use target='resume' and resume_handoff_id",
            "7. If you believe the goal is achieved, output 'GOAL_ACHIEVED: <summary>' on a line by itself",
            "8. Provide a brief diagnostic status update",
            "9. Never implement, fix, commit, or edit target-project code in collab; route every write to fix or run",
            "10. Repository selection, implementation scope, test strategy, safe migration, engine self-repair, commits, and workflow recovery are internal decisions. Never ask the user to choose or authorize them.",
            "",
            "EXECUTION SAFETY RULES (critical — follow strictly):",
            "- Set a timeout for EVERY HTTP request or polling loop (max 60s per request, 5 min total for repeated polling).",
            "- If a subprocess or external command fails, report the failure immediately — do NOT retry indefinitely.",
            "- Use bounded retries: max 3 retries for any single operation, then stop and report the error.",
            "- Do NOT start infinite watch/poll/retry loops. Always use explicit exit conditions.",
            "- Prefer small incremental steps: start a service, verify it works, then proceed to the next step.",
            "- If you start background servers, verify they are healthy (e.g., curl health-check) before using them.",
            "- Report progress after each significant action so progress is visible.",
            "- If an operation is taking too long or keeps failing, stop and report bounded diagnostic evidence; do not edit code.",
            *provider_policy_prompt_lines("collab"),
            "",
            "If this is a Python project, inspect using its existing project-local conda env at ./.conda. Route missing runtime dependencies through the owning workflow.",
            "Do not modify .auto-agents state files.",
            "",
            PromptBlock("ROUTE_WORKFLOW v1 must be valid JSON on one line and must be the only route marker in the response.", kind="output"),
        ])
        return compose_prompt(lines, purpose="collab")

    # ── Helpers ──────────────────────────────────────────────────

    def _provider_artifact_paths(self) -> List[Path]:
        return [
            requirements_trace_path(self.project_root),
            provider_references_lock_path(self.project_root),
            provider_references_dir(self.project_root),
        ]

    def _capture_provider_artifact_restore_point(self, restore_root: Path) -> None:
        for source in self._provider_artifact_paths():
            relative = source.relative_to(self.project_root)
            target = restore_root / relative
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.exists() or source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)

    def _restore_provider_artifacts(self, restore_root: Path) -> None:
        for target in self._provider_artifact_paths():
            relative = target.relative_to(self.project_root)
            source = restore_root / relative
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.exists() or source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)

    @staticmethod
    def _format_provider_validation_errors(title: str, errors: List[str]) -> str:
        detail = "\n".join(f"- {item}" for item in errors if str(item).strip())
        return f"{title}:\n{detail}" if detail else title

    def _handoff_unsatisfied_provider_contract(
        self,
        state: SessionState,
        *,
        expected_contract_fingerprint: str,
        blocker_message: str,
        contract_scope_message: str,
    ) -> bool:
        blocked_run = self.orch.block_repeated_provider_recovery(
            expected_contract_fingerprint=expected_contract_fingerprint,
            blocker_message=blocker_message,
            contract_scope_message=contract_scope_message,
        )
        if blocked_run is None:
            return False
        state.status = "blocked"
        state.resolution = "provider_recovery_contract_unsatisfied"
        state.execution_log.append({
            "attempt": state.current_attempt,
            "action": "provider_contract_blocked",
            "result": str(blocked_run.active_blocker.get("reason", ""))[:500],
            "timestamp": self._now(),
        })
        self._save(state)
        self._print(
            "Provider recovery stopped because the downstream verification "
            "contract is unchanged."
        )
        return True

    @staticmethod
    def _provider_defer_approved_ids(state: SessionState) -> set[str]:
        approved: set[str] = set()
        for entry in state.execution_log:
            if str(entry.get("action", "")).strip() != "provider_defer_approved":
                continue
            approved.update(_REQUIREMENT_ID.findall(str(entry.get("result", ""))))
        return approved

    @staticmethod
    def _is_affirmative_provider_decision(value: str) -> bool:
        normalized = str(value or "").strip().casefold()
        if normalized in {"y", "yes", "ok", "approve", "approved", "同意", "是", "确认", "暂缓", "延期"}:
            return True
        return normalized.startswith(("yes ", "approve ", "同意", "确认"))

    def _call_agent(self, state: SessionState, label: str, prompt: str) -> str:
        """Call the AI agent and return its reply text."""
        self._prepare_project_config_for_supervision()
        prompt_path, output_path = session_artifact_paths(
            self.project_root, state.session_id, label,
        )
        write_text(prompt_path, prompt)
        effort_stage = "provider_research" if self.mode == "provider_resolve" else "implement"
        effort = self.config.efforts.get(effort_stage, "deep")
        # Transport and visible output are independent: hiding the stream must
        # not disable the legacy idle watchdog when smart supervision is off.
        should_stream = self._print_agent_output
        acceleration = self.config.execution.acceleration
        continuation_key = (
            "converse" if label.startswith("converse-") else self.mode
        )
        current_head = head_ref(self.project_root)
        current_workspace = worktree_fingerprint(self.project_root)
        continuation = state.provider_continuations.get(continuation_key, {})
        resume_session_id = ""
        resume_provider = ""
        compatible_checkpoint = (
            continuation.get("head") == current_head
            and continuation.get("workspace_fingerprint") == current_workspace
            and int(continuation.get("policy_version", 0) or 0)
            == _PROVIDER_CONTINUATION_POLICY_VERSION
        )
        if acceleration.enabled and acceleration.session_continuation_enabled and compatible_checkpoint:
            resume_session_id = str(
                continuation.get("provider_session_id", "")
            ).strip()
            resume_provider = str(continuation.get("provider", "")).strip()
            if not resume_provider:
                resume_session_id = ""
        request = AgentRequest(
            stage=effort_stage,
            purpose=(self.mode + "_converse" if label.startswith("converse-") else self.mode),
            effort=effort,
            prompt=prompt,
            cwd=self.project_root,
            output_path=output_path,
            resume_session_id=resume_session_id,
            resume_provider=resume_provider,
            resume_prompt_hash=str(continuation.get("prompt_compatibility_hash", "")),
            model_adaptation=self.config.prompting.model_adaptation,
            sandbox_mode=(
                "read-only"
                if label.startswith("converse-")
                or (
                    self.mode == "collab"
                    and acceleration.enabled
                    and acceleration.collab_read_only_enabled
                )
                else "workspace-write"
            ),
            stream_output=(
                self.orch._stream_agent_output_callback(label)
                if should_stream
                else None
            ),
            stream_transport=self.mode in ("collab", "fix", "provider_resolve"),
        )
        if request.prompt_spec is not None and state.authorization_policy:
            request = replace(request, prompt_spec=replace(request.prompt_spec, blocks=(
                *request.prompt_spec.blocks,
                PromptBlock("Workflow policy for new decisions (this invocation already authorizes its stage-permitted task actions): " + json.dumps(state.authorization_policy, sort_keys=True), "stage.authorization"),
            )))
        from .prompting.continuation import delta_context, input_checkpoint
        from .prompting.core import digest, fresh_request
        sent_input = None
        if request.prompt_spec is not None:
            sent_input = input_checkpoint(request.prompt_spec, state.conversation, state.execution_log)
            if (compatible_checkpoint and acceleration.delta_context_enabled
                    and acceleration.session_continuation_enabled
                    and (acceleration.enabled or acceleration.observing)):
                delta, reason = delta_context(request.prompt_spec, continuation.get("input_checkpoint"),
                                              state.conversation, state.execution_log)
                request = replace(request, prompt_metadata={
                    "delta_candidate_bytes": len(delta.encode("utf-8")) if not reason else None,
                    "delta_fallback_reason": reason,
                })
                if reason and continuation.get("input_checkpoint") is not None:
                    request = fresh_request(request, reason)
                elif not reason and resume_session_id:
                    request = replace(request, prompt_is_continuation=True, prompt_continuation=delta)
        started = time.monotonic()
        publish_operation = getattr(self._health_runtime, "set_active_operation", None)
        if callable(publish_operation):
            publish_operation("provider", label)
        try:
            result: AgentResult = self.orch._call_with_failover(request)
        except BaseException:
            state.provider_continuations.pop(continuation_key, None)
            self._save(state)
            raise
        finally:
            if callable(publish_operation):
                publish_operation()
        usage = result.usage
        PerformanceTrace(
            self.project_root,
            workflow_kind=self.mode,
            subject_id=state.session_id,
            workflow_id=state.workflow_id,
        ).event(
            "agent",
            label,
            duration_seconds=time.monotonic() - started,
            metadata={
                "prompt": dict(result.prompt_metadata),
                "provider_session_resumed": bool(resume_session_id) and bool(result.prompt_metadata.get("resumed", True)),
                "provider_session_id": result.provider_session_id,
                "provider": self.orch._current_provider,
                "head": head_ref(self.project_root),
                "ok": result.ok,
                "input_tokens": int(usage.input_tokens) if usage else 0,
                "cached_input_tokens": (
                    int(usage.cached_input_tokens) if usage else 0
                ),
                "output_tokens": int(usage.output_tokens) if usage else 0,
            },
        )
        self.orch._emit_agent_output(label, result)
        if not result.ok:
            state.provider_continuations.pop(continuation_key, None)
            self._save(state)
            parts = []
            if result.stderr:
                parts.append(f"stderr={result.stderr}")
            if result.stdout:
                parts.append(f"stdout={result.stdout[:500]}")
            if result.summary and result.summary != result.stdout:
                parts.append(f"summary={result.summary[:500]}")
            detail = "; ".join(parts) if parts else "no output"
            raise RuntimeError(f"Agent call failed ({label}): {detail}")
        if result.provider_session_id:
            if sent_input is not None:
                sent_input["response_hash"] = digest((result.summary or result.stdout).strip())
            state.provider_continuations[continuation_key] = {
                "provider_session_id": result.provider_session_id,
                "provider": self.orch._current_provider,
                "head": head_ref(self.project_root),
                "workspace_fingerprint": worktree_fingerprint(self.project_root),
                "policy_version": _PROVIDER_CONTINUATION_POLICY_VERSION,
                "prompt_compatibility_hash": result.prompt_metadata.get("compatibility_hash", ""),
                "input_checkpoint": sent_input,
                "updated_at": self._now(),
            }
            self._save(state)
        else:
            state.provider_continuations.pop(continuation_key, None)
            self._save(state)
        return (result.summary or result.stdout).strip()

    def _invalidate_provider_continuations(
        self,
        state: SessionState,
        *,
        reason: str,
        keys: Optional[List[str]] = None,
    ) -> bool:
        selected = (
            sorted(state.provider_continuations)
            if keys is None
            else sorted(
                {
                    str(key).strip()
                    for key in keys
                    if str(key).strip() in state.provider_continuations
                }
            )
        )
        if not selected:
            return False
        for key in selected:
            state.provider_continuations.pop(key, None)
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "action": "provider_continuation_invalidated",
                "result": f"{', '.join(selected)}: {reason}"[:500],
                "timestamp": self._now(),
            }
        )
        return True

    def _run_verify(self, scope: str = "final") -> Dict[str, object]:
        publish_operation = getattr(
            self._health_runtime, "set_active_operation", None
        )
        if callable(publish_operation):
            publish_operation("verification", scope)
        try:
            return self._run_verify_inner(scope)
        finally:
            if callable(publish_operation):
                publish_operation()

    def _run_verify_inner(self, scope: str = "final") -> Dict[str, object]:
        """Run verification appropriate for the session mode.

        Fix and collab both use affected-proof attestation. A release plan is
        synchronous only for critical impact, blocking policy, or an explicit
        ``--full-verify`` request.

        Other session modes fall back to the original short-circuit gate
        runner.
        """
        if scope not in {"progress", "final"}:
            raise ValueError(f"unsupported session verification scope: {scope}")
        # Fix and collab modes use the baseline-diff path so pre-existing,
        # unrelated project failures do not become work for the session.
        if self.mode in {"fix", "collab"}:
            return self._run_baseline_diff_verify(scope=scope)

        plan = self._session_gate_plan(scope)
        if not plan.commands and not plan.parallel_groups:
            return {
                "ok": True,
                "reason": "no verification steps or commands configured",
                "scope": scope,
                "logical_commands": 0,
                "executed_commands": 0,
                "certificate_hits": 0,
                "duration_seconds": 0.0,
            }
        return self.orch._run_task_verify()

    # ── Baseline-diff verification (fix / collab) ───────────────

    def _snapshot_baseline(self, state: SessionState) -> None:
        """Capture the current gate failures and git HEAD as the baseline.

        This is run once when the session transitions from *conversing* to
        *executing*, and again on resume if the git HEAD has moved.
        """
        self._print("Capturing baseline gate snapshot...")
        plan = self._release_gate_plan()
        baseline_commands = self._logical_gate_commands(plan)
        if (
            self.config.gates.verification_policy_version >= 3
            and self.config.gates.incremental_mode == "auto"
            and bool(self.config.gates.steps)
        ):
            dependency_links = discover_dependency_links(self.project_root)
            manager = GateSnapshotManager(
                self.project_root,
                f"session-{state.session_id}-baseline",
                excluded_paths=repository_exclusion_paths(
                    self.project_root,
                    dependency_links=dependency_links,
                    surface_paths=GATE_SNAPSHOT_RUNTIME_PATHS,
                ),
            )
            snapshot = manager.create()
            # Deliberately keep the ref until the session is complete. The
            # baseline commands are evaluated lazily, and only for shards
            # that fail on the candidate.
            state.baseline_failures = []
            state.baseline_git_ref = snapshot.ref_name
            state.baseline_head_ref = head_ref(self.project_root)
            state.baseline_commands = baseline_commands
            self._print(
                "Baseline source captured; gate execution is deferred until "
                "a candidate shard fails."
            )
            self._save(state)
            return
        with self.orch._gate_executor_context(plan.metadata) as gate_executor:
            gate = run_gate_plan(
                plan.commands,
                plan.parallel_groups,
                self.project_root,
                collect_all=True,
                parallel_workers=self.orch._gate_parallel_workers(),
                command_timeout_seconds=self.config.gates.command_timeout_seconds,
                adaptive_timeout_enabled=self.config.gates.adaptive_timeout_enabled,
                command_idle_timeout_seconds=self.config.gates.command_idle_timeout_seconds,
                progress=self.orch._gate_progress_callback("session baseline snapshot"),
                gate_executor=gate_executor,
            )
        self.orch._classify_reported_infrastructure_failures(gate)
        self.orch._raise_for_baseline_termination(
            gate,
            context="session baseline snapshot",
        )
        state.baseline_failures = extract_failure_ids(gate)
        state.baseline_git_ref = head_ref(self.project_root)
        state.baseline_head_ref = state.baseline_git_ref
        state.baseline_commands = baseline_commands
        if state.baseline_failures:
            self._print(
                f"Baseline snapshot: {len(state.baseline_failures)} pre-existing failure(s) recorded."
            )
        else:
            self._print("Baseline snapshot: all gate commands pass.")
        self._save(state)

    def _ensure_baseline(self, state: SessionState) -> None:
        """Ensure a baseline snapshot exists, re-capturing if git HEAD moved."""
        if (
            self.config.gates.verification_policy_version >= 3
            and self.config.gates.incremental_mode == "auto"
            and bool(self.config.gates.steps)
        ):
            if state.baseline_git_ref:
                probe = subprocess.run(
                    ["git", "rev-parse", "--verify", state.baseline_git_ref],
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                )
                if probe.returncode == 0:
                    return
            self._snapshot_baseline(state)
            return
        current_head = head_ref(self.project_root)
        if state.baseline_git_ref and state.baseline_git_ref == current_head:
            return  # baseline is still valid
        if (
            state.baseline_git_ref
            and state.lineage_changed_paths
            and state.lineage_head_ref == current_head
        ):
            # A routed child advanced HEAD. Keep the parent's original failure
            # baseline and attest the registered child path set against it.
            return
        if state.baseline_git_ref and state.baseline_git_ref != current_head:
            self._print(
                "Git HEAD changed since last baseline — re-capturing baseline snapshot."
            )
        self._snapshot_baseline(state)

    def _release_baseline(self, state: SessionState) -> None:
        ref_name = state.baseline_git_ref
        if not ref_name.startswith("refs/auto-agents/gate-snapshots/"):
            return
        subprocess.run(
            ["git", "update-ref", "-d", ref_name],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

    def _run_baseline_diff_verify(self, scope: str = "final") -> Dict[str, object]:
        """Baseline-diff verification for fix and collab sessions.

        Returns ``{"ok": True, ...}`` only if:
        1.  ``fix_verify_command`` passes for fix sessions (if configured), AND
        2.  the scope's gate plan produces no *new* failures relative to the
            session-start baseline.
        """
        state = self._current_state
        if state is None:
            raise RuntimeError("session verification requires an active session state")
        started = time.monotonic()
        logical_commands = 0
        executed_commands = 0
        certificate_hits = 0

        def record_gate(gate_result) -> None:
            nonlocal logical_commands, executed_commands, certificate_hits
            logical_commands += len(gate_result.commands)
            hits = sum(bool(result.cached) for result in gate_result.commands)
            certificate_hits += hits
            executed_commands += len(gate_result.commands) - hits

        def outcome(
            ok: bool,
            reason: str,
            **details: object,
        ) -> Dict[str, object]:
            result: Dict[str, object] = {
                "ok": ok,
                "reason": reason,
                "scope": scope,
                "logical_commands": logical_commands,
                "executed_commands": executed_commands,
                "certificate_hits": certificate_hits,
                "duration_seconds": round(time.monotonic() - started, 6),
                "attestation_level": getattr(plan, "verification_level", scope),
                "proof_ids": list(getattr(plan, "proof_ids", [])),
                "unmapped_paths": list(getattr(plan, "unmapped_paths", [])),
                "forced_release_reason": str(
                    getattr(plan, "forced_release_reason", "")
                ),
            }
            result.update(details)
            return result

        def run_identity_diagnostic(
            gate_result: GateResult,
            *,
            label: str,
            source_ref: str = "",
        ) -> Optional[GateResult]:
            commands = list(
                dict.fromkeys(
                    diagnostic
                    for command_result in gate_result.commands
                    if not command_result.ok
                    and not command_result.termination_reason
                    for diagnostic in [
                        build_failure_identity_diagnostic_command(
                            command_result.command
                        )
                    ]
                    if diagnostic
                )
            )
            if not commands:
                return None
            with self.orch._gate_executor_context(
                {command: {} for command in commands},
                source_ref=source_ref,
                use_result_cache=False,
            ) as diagnostic_executor:
                diagnostic_gate = run_gate_plan(
                    commands,
                    [],
                    self.project_root,
                    collect_all=True,
                    parallel_workers=self.orch._gate_parallel_workers(),
                    command_timeout_seconds=(
                        self.config.gates.command_timeout_seconds
                    ),
                    adaptive_timeout_enabled=(
                        self.config.gates.adaptive_timeout_enabled
                    ),
                    command_idle_timeout_seconds=(
                        self.config.gates.command_idle_timeout_seconds
                    ),
                    progress=self.orch._gate_progress_callback(label),
                    gate_executor=diagnostic_executor,
                )
            record_gate(diagnostic_gate)
            self.orch._classify_reported_infrastructure_failures(
                diagnostic_gate
            )
            return diagnostic_gate

        # Resolve once so targeted and affected layers share identical proof
        # metadata and therefore the same candidate certificate.
        plan = self._session_gate_plan(scope)

        # Layer 1: targeted bug verification
        if self.mode == "fix" and state.fix_verify_command:
            verify_command = self._fix_verify_command_for_execution(state.fix_verify_command)
            try:
                with self.orch._gate_executor_context(
                    {verify_command: plan.metadata.get(verify_command, {})}
                ) as gate_executor:
                    targeted_gate = run_gate_plan(
                        [verify_command],
                        [],
                        self.project_root,
                        collect_all=False,
                        command_timeout_seconds=self.config.gates.command_timeout_seconds,
                        adaptive_timeout_enabled=self.config.gates.adaptive_timeout_enabled,
                        command_idle_timeout_seconds=self.config.gates.command_idle_timeout_seconds,
                        progress=self.orch._gate_progress_callback("session fix verification"),
                        gate_executor=gate_executor,
                    )
            except Exception as exc:
                return outcome(False, f"fix_verify_command error: {exc}")
            record_gate(targeted_gate)
            self.orch._classify_reported_infrastructure_failures(targeted_gate)
            if not targeted_gate.ok:
                command_result = targeted_gate.commands[0]
                if command_result.infrastructure_error:
                    return outcome(
                        False,
                        (
                            "fix_verify_command reported infrastructure failure: "
                            f"{command_result.infrastructure_failure_id or 'unknown'}"
                        ),
                    )
                detail = (
                    command_result.stderr
                    or command_result.stdout
                    or targeted_gate.summary
                    or "non-zero exit"
                ).strip()
                return outcome(False, f"fix_verify_command failed: {detail[:500]}")

        # Layer 2: baseline-diff gate check
        if not plan.commands and not plan.parallel_groups:
            return outcome(True, "no verification steps or commands configured")
        metadata = plan.metadata
        force_current_candidate = bool(self._full_verify and scope == "final")
        with self.orch._gate_executor_context(
            metadata,
            use_result_cache=not force_current_candidate,
        ) as gate_executor:
            gate = run_gate_plan(
                plan.commands,
                plan.parallel_groups,
                self.project_root,
                collect_all=True,
                parallel_workers=self.orch._gate_parallel_workers(),
                command_timeout_seconds=self.config.gates.command_timeout_seconds,
                adaptive_timeout_enabled=self.config.gates.adaptive_timeout_enabled,
                command_idle_timeout_seconds=self.config.gates.command_idle_timeout_seconds,
                progress=self.orch._gate_progress_callback(
                    f"session {scope} verification"
                ),
                gate_executor=gate_executor,
            )
        record_gate(gate)
        self.orch._classify_reported_infrastructure_failures(gate)
        extraction = extract_failure_info(gate)
        raw_output = self.orch._gate_raw_output(gate)
        if not gate.ok and not extraction.comparable:
            diagnostic_gate = run_identity_diagnostic(
                gate,
                label="session failure identity diagnostic",
            )
            if diagnostic_gate is not None:
                diagnostic_output = self.orch._gate_raw_output(diagnostic_gate)
                if diagnostic_output:
                    raw_output = (
                        f"{raw_output.rstrip()}\n\n"
                        "=== Failure Identity Diagnostic ===\n"
                        f"{diagnostic_output}"
                    ).strip()
                if diagnostic_gate.ok:
                    return outcome(
                        True,
                        "transient gate failure cleared on one identity rerun",
                        failure_kind="transient_verification",
                    )
                diagnostic_extraction = extract_failure_info(diagnostic_gate)
                if diagnostic_extraction.comparable:
                    extraction = diagnostic_extraction
        if (
            not gate.ok
            and self.config.gates.verification_policy_version >= 3
            and self.config.gates.incremental_mode == "auto"
            and bool(self.config.gates.steps)
            and state.baseline_git_ref
        ):
            failed_commands = list(
                dict.fromkeys(
                    result.command for result in gate.commands if not result.ok
                )
            )
            baseline_metadata = {
                command: metadata.get(command, {}) for command in failed_commands
            }
            if failed_commands:
                with self.orch._gate_executor_context(
                    baseline_metadata,
                    source_ref=state.baseline_git_ref,
                ) as baseline_executor:
                    baseline_gate = run_gate_plan(
                        failed_commands,
                        [],
                        self.project_root,
                        collect_all=True,
                        parallel_workers=self.orch._gate_parallel_workers(),
                        command_timeout_seconds=self.config.gates.command_timeout_seconds,
                        adaptive_timeout_enabled=self.config.gates.adaptive_timeout_enabled,
                        command_idle_timeout_seconds=self.config.gates.command_idle_timeout_seconds,
                        progress=self.orch._gate_progress_callback(
                            "session lazy baseline verification"
                        ),
                        gate_executor=baseline_executor,
                    )
                self.orch._classify_reported_infrastructure_failures(baseline_gate)
                record_gate(baseline_gate)
                self.orch._raise_for_baseline_termination(
                    baseline_gate,
                    context="session lazy baseline verification",
                )
                baseline_extraction = extract_failure_info(baseline_gate)
                baseline_failures_to_add = baseline_extraction.failure_ids
                if not baseline_gate.ok and not baseline_extraction.comparable:
                    baseline_diagnostic = run_identity_diagnostic(
                        baseline_gate,
                        label="session lazy baseline identity diagnostic",
                        source_ref=state.baseline_git_ref,
                    )
                    if baseline_diagnostic is not None:
                        if baseline_diagnostic.ok:
                            baseline_failures_to_add = []
                        else:
                            diagnostic_extraction = extract_failure_info(
                                baseline_diagnostic
                            )
                            if diagnostic_extraction.comparable:
                                baseline_extraction = diagnostic_extraction
                                baseline_failures_to_add = (
                                    diagnostic_extraction.failure_ids
                                )
                state.baseline_failures = sorted(
                    set(state.baseline_failures)
                    | set(baseline_failures_to_add)
                )
                self._save(state)
        current_failures = extraction.failure_ids
        if not extraction.comparable and not gate.ok:
            failed_commands = [
                result.command for result in gate.commands if not result.ok
            ]
            raw_log_path = self.orch._persist_failed_verification_log(
                raw_output,
                label="session-verify",
            )
            reason = (
                "verification inconclusive after one identity rerun; failed "
                "command did not yield stable test-case failure ids"
                if diagnostic_gate is not None
                else (
                    "verification inconclusive; failed command has no supported "
                    "stable-identity diagnostic"
                )
            )
            if failed_commands:
                reason += ": " + ", ".join(failed_commands[:3])
            if raw_log_path:
                reason += f"; raw log: {raw_log_path}"
            return outcome(
                False,
                reason,
                retry_fix=False,
                failure_kind="verification_inconclusive",
                raw_log_path=raw_log_path,
            )
        failed_gate_commands = {
            result.command for result in gate.commands if not result.ok
        }
        command_level_prefixes = (
            "cmd:",
            "cmd-timeout:",
            "cmd-stalled:",
            "cmd-terminated:",
        )
        relevant_non_comparable_baseline = any(
            any(
                failure_id == f"{prefix}{command}"
                for prefix in command_level_prefixes
                for command in failed_gate_commands
            )
            or failure_id.startswith(("infra:", "reason:"))
            for failure_id in map(str, state.baseline_failures)
        )
        if (
            not gate.ok
            and extraction.comparable
            and relevant_non_comparable_baseline
        ):
            raw_log_path = self.orch._persist_failed_verification_log(
                raw_output,
                label="session-verify",
            )
            reason = (
                "verification failure identity changed from a command-level "
                "baseline to stable test-case ids; baseline comparison is "
                "non-comparable: "
                + ", ".join(current_failures[:10])
            )
            if raw_log_path:
                reason += f"; raw log: {raw_log_path}"
            return outcome(
                False,
                reason,
                retry_fix=False,
                failure_kind="verification_inconclusive",
                raw_log_path=raw_log_path,
            )
        new_failures = sorted(set(current_failures) - set(state.baseline_failures))
        if new_failures:
            return outcome(
                False,
                (
                    f"{len(new_failures)} new failure(s) introduced: "
                    + ", ".join(new_failures[:10])
                ),
            )
        return outcome(True, gate.summary)

    def _fix_verify_command_for_execution(self, command: str) -> str:
        stripped = command.strip()
        if not stripped:
            return stripped
        conda_meta = self.project_root / ".conda" / "conda-meta"
        if not conda_meta.exists():
            return stripped
        if _uses_project_local_conda(stripped):
            return stripped
        if not _looks_like_python_command(stripped):
            return stripped
        if _SHELL_CONTROL_TOKENS.search(stripped):
            return stripped
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
            return stripped
        return f"conda run -p ./.conda {stripped}"

    # ── Convergence detection ────────────────────────────────────

    def _compute_diff_hash(self) -> str:
        """Return a hash of the current ``git diff`` output."""
        try:
            result = subprocess.run(
                ["git", "diff"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return hashlib.sha256(result.stdout.encode()).hexdigest()[:16]
        except Exception:
            return ""

    @staticmethod
    def _normalize_verify_reason(reason: str) -> str:
        """Strip timestamps, PIDs, and similar noise from verification output."""
        cleaned = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*", "<TS>", reason)
        cleaned = re.sub(r"\bpid[= ]\d+\b", "pid=<PID>", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b0x[0-9a-fA-F]+\b", "<HEX>", cleaned)
        return cleaned.strip()

    def _compute_verify_sig(self, reason: str) -> str:
        """Return a hash of the normalized verification failure reason."""
        normalized = self._normalize_verify_reason(reason)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _update_stall_state(self, state: SessionState, diff_hash: str, verify_sig: str) -> None:
        """Update stall tracking.  If both diff and verify signature are
        unchanged from the previous attempt, increment the stall counter;
        otherwise reset it."""
        if (
            state.last_diff_hash
            and diff_hash == state.last_diff_hash
            and verify_sig == state.last_verify_sig
        ):
            state.stall_count += 1
        else:
            state.stall_count = 0
        state.last_diff_hash = diff_hash
        state.last_verify_sig = verify_sig

    def _collab_verify_failure(
        self,
        state: SessionState,
        verify_reason: str,
    ) -> Tuple[str, Optional[str]]:
        """Record a collab verification failure and enforce convergence limits."""
        diff_hash = self._compute_diff_hash()
        verify_sig = self._compute_verify_sig(verify_reason)
        self._update_stall_state(state, diff_hash, verify_sig)
        self._save(state)
        feedback = self.orch._format_retry_feedback(
            "local_verification",
            reason=verify_reason,
        )
        return feedback, self._should_stop(state, verify_reason)

    def _capture_collab_restore_point(
        self,
        restore_root: Path,
        before_snapshot: Dict[str, str],
    ) -> None:
        files_root = restore_root / "files"
        for relative in before_snapshot:
            source = self.project_root / relative
            target = files_root / relative
            if source.is_dir():
                for item in source.rglob("*"):
                    if item.is_file() and not item.is_symlink():
                        self._copy_checkpoint_file(
                            item,
                            target / item.relative_to(source),
                        )
            elif source.exists() or source.is_symlink():
                self._copy_checkpoint_file(source, target)
        index = subprocess.run(
            ["git", "rev-parse", "--git-path", "index"],
            cwd=self.project_root,
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        if index.returncode == 0:
            index_path = Path(index.stdout.strip())
            if not index_path.is_absolute():
                index_path = self.project_root / index_path
            if index_path.is_file():
                self._copy_checkpoint_file(
                    index_path,
                    restore_root / ".git-index.snapshot",
                )
        write_json(
            restore_root / "manifest.json",
            {
                "schema_version": 1,
                "before_snapshot": dict(before_snapshot),
                "head": head_ref(self.project_root),
                "created_at": self._now(),
            },
        )

    def _copy_checkpoint_file(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            shutil.copy2(source, target, follow_symlinks=False)
            return
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        blob = (
            self.project_root
            / ".auto-agents"
            / "state"
            / "checkpoint_blobs"
            / digest[:2]
            / digest
        )
        if not blob.is_file():
            blob.parent.mkdir(parents=True, exist_ok=True)
            temporary = blob.with_suffix(
                f".{os.getpid()}.{uuid4().hex[:8]}.tmp"
            )
            shutil.copy2(source, temporary)
            try:
                os.replace(temporary, blob)
            finally:
                if temporary.exists():
                    temporary.unlink()
        try:
            os.link(blob, target)
        except OSError:
            shutil.copy2(blob, target)

    def _reconcile_interrupted_collab_checkpoints(
        self,
        state: SessionState,
    ) -> None:
        if not state.workflow_id:
            return
        checkpoint_root = (
            self.project_root
            / ".auto-agents"
            / "state"
            / "workflows"
            / state.workflow_id
            / "checkpoints"
        )
        if not checkpoint_root.is_dir():
            return
        for path in sorted(checkpoint_root.glob(f"collab-{state.session_id}-*")):
            manifest_path = path / "manifest.json"
            manifest = read_json(manifest_path, default={})
            if (
                not manifest_path.is_file()
                or not isinstance(manifest, dict)
                or manifest.get("schema_version") != 1
                or not isinstance(manifest.get("before_snapshot"), dict)
            ):
                # The manifest is written only after the restore point is
                # complete and before the provider call starts. A partial
                # directory therefore cannot contain provider mutations and
                # must not be interpreted as an empty-worktree checkpoint.
                state.execution_log.append(
                    {
                        "attempt": state.current_attempt,
                        "action": "collab_incomplete_checkpoint_discarded",
                        "result": str(path),
                        "timestamp": self._now(),
                    }
                )
                self._save(state)
                shutil.rmtree(path, ignore_errors=True)
                continue
            before = (
                {
                    str(key): str(value)
                    for key, value in manifest.get("before_snapshot", {}).items()
                }
                if isinstance(manifest, dict)
                and isinstance(manifest.get("before_snapshot"), dict)
                else {}
            )
            if not (path / ".git-index.snapshot").is_file():
                raise RuntimeError(
                    f"collab checkpoint is incomplete and cannot be reconciled: {path}"
                )
            restored = self._restore_collab_mutations(state, before, path)
            self._invalidate_provider_continuations(
                state,
                keys=["collab"],
                reason="interrupted collab checkpoint was reconciled",
            )
            state.execution_log.append(
                {
                    "attempt": state.current_attempt,
                    "action": "collab_interruption_reconciled",
                    "result": ", ".join(restored)[:500],
                    "timestamp": self._now(),
                }
            )
            self._save(state)
            shutil.rmtree(path, ignore_errors=True)

    def _restore_collab_mutations(
        self,
        state: SessionState,
        before_snapshot: Dict[str, str],
        restore_root: Path,
    ) -> List[str]:
        after_snapshot = self.orch._worktree_change_snapshot()
        delta = self.orch._snapshot_delta_paths(before_snapshot, after_snapshot)
        session_prefix = f".auto-agents/state/sessions/{state.session_id}/"
        checkpoint_prefix = (
            f".auto-agents/state/workflows/{state.workflow_id}/checkpoints/"
            if state.workflow_id
            else ""
        )
        offending = [
            path
            for path in delta
            if not path.startswith(session_prefix)
            and not (checkpoint_prefix and path.startswith(checkpoint_prefix))
            and not path.startswith(".auto-agents/state/checkpoint_blobs/")
            and path
            not in {
                ".auto-agents/state/health-watch-control.json",
                ".auto-agents/state/health-watch-control.lock",
            }
            and path != ".auto-agents/.gitignore"
        ]
        if not offending:
            return []
        files_root = restore_root / "files"
        for relative in offending:
            target = self.project_root / relative
            source = files_root / relative
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            if relative in before_snapshot:
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                elif source.exists() or source.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target, follow_symlinks=False)
                continue
            tracked = subprocess.run(
                ["git", "cat-file", "-e", f"HEAD:{relative}"],
                cwd=self.project_root,
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            if tracked.returncode == 0:
                restore = subprocess.run(
                    [
                        "git",
                        "restore",
                        "--source=HEAD",
                        "--staged",
                        "--worktree",
                        "--",
                        relative,
                    ],
                    cwd=self.project_root,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                )
                if restore.returncode != 0:
                    raise RuntimeError(
                        restore.stderr.strip()
                        or f"could not restore collab mutation: {relative}"
                    )
        saved_index = restore_root / ".git-index.snapshot"
        if saved_index.is_file():
            self.orch._restore_index_paths_from_restore_point(
                offending,
                restore_root,
            )
        restored = self.orch._worktree_change_snapshot()
        unrestored = [
            path
            for path in offending
            if before_snapshot.get(path) != restored.get(path)
        ]
        if unrestored:
            raise RuntimeError(
                "collab read-only rollback could not restore: "
                + ", ".join(unrestored[:10])
            )
        return offending

    @staticmethod
    def _record_agent_attempt(state: SessionState) -> None:
        """Count one provider invocation in both the audit and local budgets."""
        state.current_attempt += 1
        state.attempts_since_progress += 1

    def _begin_attempt_epoch(
        self,
        state: SessionState,
        *,
        reason: str,
        reset_stall: bool = True,
    ) -> None:
        """Open a fresh local attempt budget after durable workflow progress."""
        state.attempt_epoch += 1
        state.attempts_since_progress = 0
        state.consecutive_agent_errors = 0
        if reset_stall:
            state.stall_count = 0
            state.last_diff_hash = ""
            state.last_verify_sig = ""
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "attempt_epoch": state.attempt_epoch,
                "action": "attempt_epoch_started",
                "result": reason[:500],
                "timestamp": self._now(),
            }
        )

    @staticmethod
    def _attempts_in_current_epoch(state: SessionState) -> int:
        if state.attempt_epoch or state.attempts_since_progress:
            return state.attempts_since_progress
        # Compatibility for in-memory callers and pre-epoch session records.
        return state.current_attempt

    def _record_terminal_stop(self, state: SessionState) -> None:
        resolution = ""
        if state.stall_count >= SESSION_STALL_THRESHOLD:
            resolution = "no_progress"
        elif state.consecutive_agent_errors >= SESSION_AGENT_ERROR_THRESHOLD:
            resolution = "agent_errors_exhausted"
        elif self._attempts_in_current_epoch(state) >= state.hard_ceiling:
            resolution = "hard_ceiling_reached"
        if not resolution:
            return
        if not state.resolution:
            state.resolution = resolution
        state.execution_log.append(
            {
                "attempt": state.current_attempt,
                "attempt_epoch": state.attempt_epoch,
                "action": "session_stopped",
                "result": state.resolution,
                "timestamp": self._now(),
            }
        )

    def _should_stop(self, state: SessionState, reason: str) -> Optional[str]:
        """Return a stop-reason string if the session should stop, else None."""
        if state.stall_count >= SESSION_STALL_THRESHOLD:
            return (
                f"No progress detected for {state.stall_count} consecutive attempts "
                f"(same diff and same verification error). Stopping."
            )
        if state.consecutive_agent_errors >= SESSION_AGENT_ERROR_THRESHOLD:
            return (
                f"Agent encountered {state.consecutive_agent_errors} consecutive "
                f"transient errors. Stopping."
            )
        if (
            state.mode == "provider_resolve"
            and state.current_attempt >= state.max_attempts
        ):
            return (
                f"Provider recovery attempt limit ({state.max_attempts}) reached. "
                "Stopping."
            )
        attempts_in_epoch = self._attempts_in_current_epoch(state)
        if attempts_in_epoch >= state.hard_ceiling:
            return (
                f"Hard attempt ceiling ({state.hard_ceiling}) reached after "
                f"{attempts_in_epoch} agent calls since the last durable progress "
                "boundary. Stopping."
            )
        return None

    def _build_error_feedback(self, err_msg: str) -> str:
        """Build feedback string from an agent error, with stall/timeout diagnostics."""
        tail_section = ""
        if "--- last output ---" in err_msg:
            tail_section = err_msg.split("--- last output ---", 1)[1].strip()

        if "stalled" in err_msg.lower():
            self._print("Agent stalled (no output for extended period).")
            if tail_section:
                self._print(f"Last output before stall:\n{tail_section[:300]}")
            feedback = (
                "CRITICAL: The previous attempt STALLED — the agent produced no output "
                "for an extended period and was terminated. This usually means a subprocess "
                "or polling loop ran indefinitely.\n"
                "You MUST use a different approach: use bounded retries, set explicit timeouts, "
                "and break the work into smaller verifiable steps.\n"
            )
            if tail_section:
                feedback += f"Last output before the stall:\n{tail_section[:500]}\n"
            return feedback

        if "timed out" in err_msg.lower():
            self._print("Agent timed out.")
            feedback = (
                "The previous attempt TIMED OUT. The agent ran for too long without completing.\n"
                "Try a more focused approach: break the task into smaller steps.\n"
            )
            if tail_section:
                feedback += f"Last output before timeout:\n{tail_section[:500]}\n"
            return feedback

        self._print(f"Agent call failed (transient): {err_msg[:200]}")
        return f"Previous attempt failed with a transient error: {err_msg[:300]}"

    def _commit_verified_progress(self, state: SessionState, prefix: str, reply: str = "") -> bool:
        """Commit verified project changes while the collab loop continues."""
        if not changed_paths(self.project_root):
            return False
        self._run_session_persistence_action(state)
        return self._git_commit(state, prefix, reply=reply)

    def _extract_commit_summary(self, text: str) -> str:
        """Extract a concise commit summary from an agent reply."""
        if not text.strip():
            return ""

        for match in _COMMIT_MESSAGE.finditer(text):
            candidate = self._finalize_commit_candidate(match.group(1))
            if candidate:
                return candidate

        for pattern in (_GOAL_ACHIEVED, _BUG_FOUND, _NOT_A_BUG):
            match = pattern.search(text)
            if match:
                candidate = self._finalize_commit_candidate(match.group(1))
                if candidate:
                    return candidate

        candidates: List[str] = []
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if (
                not line
                or line == "GOAL_CLEAR"
                or line.startswith("FIX_VERIFY:")
                or line.startswith("COMMIT_MESSAGE:")
                or line.startswith("PERSISTENCE_CHANGE:")
            ):
                continue
            if any(pattern.match(line) for pattern in (_GOAL_ACHIEVED, _BUG_FOUND, _NOT_A_BUG, _NEED_USER_ASSIST)):
                continue
            if line == "```":
                continue
            line = re.sub(r"^#{1,6}\s+", "", line).strip()
            if not line or line.lower() == "fix plan":
                continue
            if re.match(r"^(?:[-*+]\s+|\d+\.\s+)", line):
                continue
            candidate = self._finalize_commit_candidate(line)
            if candidate:
                candidates.append(candidate)

        if not candidates:
            return ""
        return candidates[-1]

    @staticmethod
    def _is_status_only_commit_subject(subject: str) -> bool:
        return bool(re.fullmatch(
            r"(?:"
            r"verification passed|verified|all checks passed|checks passed|tests passed|"
            r"validation passed|validated|"
            r"验证已通过|验证通过|已验证|校验已通过|检查已通过|测试已通过|测试通过"
            r")",
            subject,
            flags=re.IGNORECASE,
        ))

    @staticmethod
    def _is_command_like_commit_subject(subject: str) -> bool:
        stripped = subject.strip()
        if not stripped:
            return False
        if stripped.startswith(("$ ", "> ")):
            return True
        if re.match(r"^(?:\./|\.\./|/)", stripped):
            return True

        lowered = stripped.lower()
        command_prefixes = (
            "conda", "python", "python3", "pytest", "py.test", "pip", "pip3", "uv",
            "poetry", "tox", "nox", "npm", "pnpm", "yarn", "npx", "node", "go",
            "cargo", "rustc", "make", "cmake", "bash", "sh", "zsh", "git", "docker",
            "docker-compose", "kubectl", "java", "javac", "mvn", "gradle", "perl",
            "ruby",
        )
        first_token = lowered.split(" ", 1)[0]
        if first_token not in command_prefixes:
            return False

        command_markers = (
            " -", " --", " ./", " ../", " /", ".py", ".sh", ".js", ".ts", ".tsx",
            ".jsx", "::", " run ", " test", " unittest", " pytest", " discover",
        )
        return any(marker in lowered for marker in command_markers)

    def _finalize_commit_candidate(self, text: str) -> str:
        raw = " ".join(text.split()).strip()
        if not raw or raw.endswith((":", "：")):
            return ""
        # Strip inline markdown links `[text](url)` -> `text`.
        raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)
        # Drop any residual markdown-link fragments or raw filesystem paths —
        # these indicate a broken / mid-truncated line that is not human-readable.
        if "](" in raw or re.search(r"[\[\]]", raw):
            return ""
        if re.search(r"(?:^|\s)(?:/|\./|\.\./|[A-Za-z]:[\\/])\S+", raw):
            return ""
        # Reject candidates containing relative file paths (e.g. "app/foo.py"),
        # which indicate the line is quoting code locations rather than
        # describing the fix.
        if re.search(r"\b[\w.-]+/[\w./-]+\.[A-Za-z0-9]{1,6}\b", raw):
            return ""
        subject = self._normalize_commit_subject(raw)
        if (
            not subject
            or self._is_status_only_commit_subject(subject)
            or self._is_command_like_commit_subject(subject)
        ):
            return ""
        return subject

    def _normalize_commit_subject(self, text: str, max_length: int = 72) -> str:
        subject = " ".join(text.replace("`", "").replace("*", "").split())
        trim_chars = " \t\r\n.,;:!-?，。；：！？"
        subject = subject.strip(trim_chars)
        if not subject:
            return ""
        if len(subject) <= max_length:
            return subject
        truncated = subject[: max_length + 1].rsplit(" ", 1)[0].rstrip(trim_chars)
        if len(truncated) < max_length // 2:
            truncated = subject[:max_length].rstrip(trim_chars)
        # Refuse truncations that land inside a path/URL/identifier token —
        # this prevents commit subjects like "... in [foo](/long/path/to/fi".
        if re.search(r"[\[\]()/\\]", truncated) and not re.search(r"[\[\]()/\\]", subject[:max_length // 2]):
            return ""
        if truncated.count("(") != truncated.count(")") or truncated.count("[") != truncated.count("]"):
            return ""
        return truncated

    def _session_commit_summary(self, state: SessionState, reply: str) -> str:
        summary = self._extract_commit_summary(reply)
        if summary:
            return summary
        for entry in reversed(state.execution_log):
            if str(entry.get("action", "")) not in {"fix", "collab"}:
                continue
            summary = self._extract_commit_summary(str(entry.get("result", "")))
            if summary:
                return summary
        return self._normalize_commit_subject(state.goal.replace("\n", " ")) or "verified update"

    def _git_commit(self, state: SessionState, prefix: str, reply: str = "") -> bool:
        """Persist current state, then commit current changes."""
        summary = self._session_commit_summary(state, reply)
        message = f"{prefix}: {summary}"
        state.execution_log.append({
            "attempt": state.current_attempt,
            "action": "commit",
            "result": message,
            "timestamp": self._now(),
        })
        self._save(state)
        operation_id = f"session-{state.session_id}-{state.current_attempt}"
        workflow_snapshot = None
        if state.workflow_id and self._coordinator is not None:
            workflow_snapshot = self._coordinator.store.load(state.workflow_id)
            self._coordinator.store.append_event(
                workflow_snapshot,
                "operation_intent",
                operation_id=operation_id,
                details={"kind": "session_commit", "message": message},
            )
        try:
            protected = {
                str(path) for path in state.protected_preexisting_paths if str(path).strip()
            }
            owned_product = [
                path for path in changed_paths(self.project_root) if path not in protected
            ]
            owned_state = [
                f".auto-agents/state/sessions/{state.session_id}/session_state.json",
                f".auto-agents/state/sessions/{state.session_id}/issue.json",
                f".auto-agents/state/sessions/{state.session_id}/issue.md",
                ".auto-agents/state/handoffs",
                f".auto-agents/state/workflows/{state.workflow_id}",
                ".auto-agents/state/workflows/active.json",
                ".auto-agents/.gitignore",
            ]
            commit_sha = commit_only_paths(
                self.project_root,
                message,
                [*owned_product, *owned_state],
                trailers=(
                    [
                        f"Auto-Agents-Operation: session-{state.session_id}-{state.current_attempt}",
                        f"Auto-Agents-Workflow: {state.workflow_id}",
                    ]
                    if state.workflow_id
                    else []
                ),
            )
            if workflow_snapshot is not None:
                self._coordinator.store.append_event(
                    workflow_snapshot,
                    "operation_completed",
                    operation_id=operation_id,
                    details={"kind": "session_commit", "commit_sha": commit_sha},
                )
            return bool(commit_sha)
        except RuntimeError as exc:
            self._print(f"Git commit failed: {exc}")
            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "commit_failed",
                "result": str(exc),
                "timestamp": self._now(),
            })
            self._save(state)
            return False

    def _record_release_attestation(
        self,
        state: SessionState,
        verify: Dict[str, object],
    ) -> None:
        if str(verify.get("attestation_level", "")) == "release":
            payload = complete_release_verification(self.project_root, verify)
        elif self.config.gates.release_verification_mode == "deferred":
            payload = enqueue_release_verification(
                self.project_root,
                source=f"{self.mode}:{state.session_id}",
                affected_proof_ids=[str(item) for item in verify.get("proof_ids", [])],
            )
        else:
            return
        # The session commit already captured its terminal state. The release
        # queue is ignored runtime state, so do not dirty the tracked session
        # record after committing the candidate.

    def _consolidate_goal(self, state: SessionState) -> str:
        """Preserve every user correction in order without duplicating agent summaries."""
        messages = [str(msg.get("content", "")) for msg in state.conversation
                    if msg.get("role") == "user"]
        if state.goal and state.goal not in messages:
            messages.insert(0, state.goal)
        return "\n\n".join(f"User input {i}:\n{text}" for i, text in enumerate(messages, 1)) or state.goal

    @staticmethod
    def _goal_contexts(state: SessionState):
        users = [(i, msg) for i, msg in enumerate(state.conversation) if msg.get("role") == "user"]
        contexts = []
        if state.goal and all(msg.get("content") != state.goal for _, msg in users):
            contexts.append(ContextBlock(state.goal, "Initial user goal", "goal"))
        contexts.extend(ContextBlock(str(msg.get("content", "")), "Conversation: user", f"conversation:{i}")
                        for i, msg in users)
        return contexts

    def _save(self, state: SessionState) -> None:
        state.updated_at = self._now()
        save_session_state(self.project_root, state)
        publish = getattr(self._health_runtime, "publish_session", None)
        if callable(publish):
            publish(state)
        reporter = getattr(self.orch, "reporter", None)
        if reporter is not None:
            reporter.observe_session(state)

    def _check_health_action(self) -> None:
        pending = getattr(self._health_runtime, "pending_session_action", None)
        if not callable(pending):
            return
        request = pending()
        if not isinstance(request, dict):
            return
        request_id = str(request.get("request_id", ""))
        complete = getattr(self._health_runtime, "complete_session_action", None)
        if callable(complete) and request_id:
            complete(request_id, detail="routed to foreground terminal triage")
        raise RuntimeError(
            "health sidecar requested foreground diagnosis: "
            + str(request.get("reason", "unknown health anomaly"))
        )

    def _print(self, msg: str, flush: bool = False) -> None:
        reporter = getattr(self.orch, "reporter", None)
        if reporter is not None:
            reporter.text(msg)
        else:
            print(msg, file=sys.stderr, flush=flush)

    def _print_agent_thinking(self) -> None:
        self._print("\nAgent is thinking, please wait...", flush=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
