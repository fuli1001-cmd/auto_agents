from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import (
    create_session,
    docs_dir,
    list_sessions,
    load_session_state,
    save_session_state,
    session_artifact_paths,
    sort_sessions,
)
from .gates import (
    commands_from_verification_steps,
    extract_failure_ids,
    extract_failure_info,
    run_gate_plan,
    run_commands,
    run_commands_collect_all,
)
from .git_ops import changed_paths, commit_all, head_ref, worktree_fingerprint
from .io_utils import read_text, write_text
from .models import (
    AgentRequest,
    AgentResult,
    SESSION_AGENT_ERROR_THRESHOLD,
    SESSION_STALL_THRESHOLD,
    SessionState,
)
from .validation import _looks_like_python_command, _uses_project_local_conda

_GOAL_CLEAR = re.compile(r"^GOAL_CLEAR\s*$", re.MULTILINE)
_NOT_A_BUG = re.compile(r"^NOT_A_BUG:\s*(.+)$", re.MULTILINE)
_NEED_USER_ASSIST = re.compile(r"^NEED_USER_ASSIST:\s*(.+)$", re.MULTILINE)
_BUG_FOUND = re.compile(r"^BUG_FOUND:\s*(.+)$", re.MULTILINE)
_GOAL_ACHIEVED = re.compile(r"^GOAL_ACHIEVED:\s*(.+)$", re.MULTILINE)
_FIX_VERIFY = re.compile(r"^FIX_VERIFY:\s*(.+)$", re.MULTILINE)
_COMMIT_MESSAGE = re.compile(r"^COMMIT_MESSAGE:\s*(.+)$", re.MULTILINE)
_SHELL_CONTROL_TOKENS = re.compile(r"[|;&<>`\n]")


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
    ) -> None:
        self.orch = orchestrator
        self.project_root = orchestrator.project_root
        self.config = orchestrator.config
        self.mode = mode
        self._print_agent_output = print_agent_output
        self._current_state: Optional[SessionState] = None
        # Expose the same user-input helper used by the orchestrator.
        self._prompt_user = orchestrator._prompt_user

    def _gate_commands(self) -> List[str]:
        if self.config.gates.steps:
            return commands_from_verification_steps(self.config.gates.steps, self.project_root)
        return list(self.config.gates.commands)

    # ── Public entry points ──────────────────────────────────────

    def start(self) -> SessionState:
        """Create a new session and drive it to completion (or interruption)."""
        state = create_session(self.project_root, self.mode)
        self._print(f"Session {state.session_id} started in {state.mode} mode.")
        return self._drive(state)

    def resume(self, session_id: str) -> SessionState:
        """Resume an existing session.

        Completed sessions are returned as-is.  Failed sessions are reset to
        ``executing`` with a fresh attempt counter so the user can continue
        where the previous run left off while preserving all prior context.
        """
        state = load_session_state(self.project_root, session_id)
        if state.status == "completed":
            self._print(f"Session {session_id} is already completed.")
            return state
        if state.status == "failed":
            self._print(
                f"Resuming failed session {session_id} — resetting attempt counter "
                f"and continuing from execution phase."
            )
            state.status = "executing"
            state.current_attempt = 0
            state.stall_count = 0
            state.consecutive_agent_errors = 0
            self._save(state)
            return self._drive(state)
        self._print(f"Resuming session {session_id} ({state.mode} mode, status={state.status}).")
        return self._drive(state)

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
        return self.start()

    # ── Main driver ──────────────────────────────────────────────

    def _drive(self, state: SessionState) -> SessionState:
        """Drive the session through its phases until completion or pause."""
        try:
            if state.status == "conversing":
                state = self._phase_converse(state)

            if state.status == "executing":
                if self.mode == "fix":
                    state = self._phase_fix_execute(state)
                elif self.mode == "provider_resolve":
                    state = self._phase_provider_resolve_execute(state)
                else:
                    state = self._phase_collab_loop(state)
        except KeyboardInterrupt:
            self._print("\nSession interrupted by user. Progress saved.")
            self._save(state)
        except RuntimeError as exc:
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

    # ── Phase 1: Conversational clarification ────────────────────

    def _phase_converse(self, state: SessionState) -> SessionState:
        if not state.goal:
            if self.mode == "provider_resolve":
                state.goal = self.orch.build_provider_resolve_goal()
                self._print("Loaded current provider_research blockers into a recovery session.")
            else:
                label = "bug" if self.mode == "fix" else "goal"
                self._print(f"Describe the {label} you want to address:")
                user_input = self._prompt_user("", multiline=True)
                if not user_input.strip():
                    self._print("No input provided. Exiting.")
                    return state
                state.goal = user_input.strip()
            state.conversation.append({"role": "user", "content": state.goal})
            self._save(state)
            self._print_agent_thinking()

        max_converse_rounds = 15
        rounds = 0

        while rounds < max_converse_rounds:
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
                # Extract optional FIX_VERIFY command from the reply
                fv_match = _FIX_VERIFY.search(reply)
                if fv_match and self.mode == "fix":
                    state.fix_verify_command = fv_match.group(1).strip()
                display = _GOAL_CLEAR.sub("", reply)
                display = _FIX_VERIFY.sub("", display).strip()
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

        # Max rounds reached – force proceed
        self._print("Max clarification rounds reached. Proceeding with current understanding.")
        state.status = "executing"
        self._save(state)
        return state

    # ── Phase 2a: Fix mode execution ─────────────────────────────

    def _phase_fix_execute(self, state: SessionState) -> SessionState:
        self._current_state = state
        self._ensure_baseline(state)
        feedback = ""
        while True:
            state.current_attempt += 1
            self._print(f"\n--- Fix attempt {state.current_attempt} ---")

            prompt = self._build_fix_prompt(state, feedback)
            try:
                reply = self._call_agent(state, f"fix-{state.current_attempt}", prompt)
            except RuntimeError as exc:
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
            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "verify",
                "result": "pass" if verify["ok"] else verify_reason,
                "timestamp": self._now(),
            })
            self._save(state)

            if verify["ok"]:
                self._print("Verification passed!")
                state.status = "completed"
                state.resolution = "fixed"
                self._git_commit(state, "fix", reply=reply)
                self._print(f"Bug fix completed in session {state.session_id}.")
                return state

            self._print(f"Verification failed: {verify_reason}")
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
        self._save(state)
        self._print("Fix session stopped (no further progress). Session marked as failed.")
        return state

    # ── Phase 2b: Collab mode loop ───────────────────────────────

    def _phase_collab_loop(self, state: SessionState) -> SessionState:
        self._current_state = state
        feedback = ""
        while True:
            state.current_attempt += 1
            self._print(f"\n--- Collab iteration {state.current_attempt} ---")

            prompt = self._build_collab_prompt(state, feedback)
            try:
                reply = self._call_agent(state, f"collab-{state.current_attempt}", prompt)
            except RuntimeError as exc:
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

            # Check for NEED_USER_ASSIST — counts as progress
            assist_match = _NEED_USER_ASSIST.search(reply)
            if assist_match:
                state.stall_count = 0
                display = reply.strip()
                self._print(f"\nAgent:\n{display}")
                self._print(f"\nAgent needs your assistance: {assist_match.group(1)}")
                state.status = "waiting_user"
                self._save(state)
                user_reply = self._prompt_user("\nYour response (or result): ", multiline=True)
                state.conversation.append({"role": "user", "content": user_reply.strip() or "Done."})
                state.status = "executing"
                self._save(state)
                self._print_agent_thinking()
                feedback = ""
                continue

            # Check for GOAL_ACHIEVED — counts as progress
            achieved_match = _GOAL_ACHIEVED.search(reply)
            if achieved_match:
                state.stall_count = 0
                display = _GOAL_ACHIEVED.sub("", reply).strip()
                if display:
                    self._print(f"\nAgent:\n{display}")
                self._print(f"\nAgent believes the goal is achieved: {achieved_match.group(1)}")

                # Verify
                verify = self._run_verify()
                verify_status = "pass" if verify["ok"] else "fail"
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "verify",
                    "result": verify_status,
                    "timestamp": self._now(),
                })
                self._save(state)

                if not verify["ok"]:
                    verify_reason = str(verify["reason"])
                    self._print(f"Verification failed: {verify_reason}")
                    self._print("Continuing the loop to fix verification issues.")
                    feedback = self.orch._format_retry_feedback("local_verification", reason=verify_reason)
                    continue

                self._print("Verification passed!")
                answer = self._prompt_user("Do you confirm the goal is achieved? (y/n) [y]: ", default="y")
                if answer.strip().lower() not in ("n", "no"):
                    state.status = "completed"
                    self._git_commit(state, "collab", reply=reply)
                    self._print(f"Collaborative session {state.session_id} completed successfully.")
                    return state

                committed = self._commit_verified_progress(state, "collab", reply=reply)
                if committed:
                    self._print("Verified progress committed before continuing.")
                user_feedback = self._prompt_user("What still needs to be done? ", multiline=True)
                state.conversation.append({"role": "user", "content": user_feedback.strip() or "Not yet done."})
                self._save(state)
                self._print_agent_thinking()
                feedback = ""
                continue

            # Check for BUG_FOUND – agent found & fixed a bug inline — counts as progress
            bug_match = _BUG_FOUND.search(reply)
            if bug_match:
                state.stall_count = 0
                self._print(f"\nAgent found a bug: {bug_match.group(1)}")
                self._print(f"\nAgent:\n{reply.strip()}")

                # Try to verify after the fix
                verify = self._run_verify()
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "bug_fix_verify",
                    "result": "pass" if verify["ok"] else str(verify["reason"]),
                    "timestamp": self._now(),
                })
                self._save(state)

                if verify["ok"]:
                    committed = self._commit_verified_progress(state, "collab", reply=reply)
                    if committed:
                        self._print("Bug fix verified and committed.")
                    else:
                        self._print("Bug fix verified.")
                else:
                    self._print(f"Bug fix verification failed: {verify['reason']}")
                    feedback = self.orch._format_retry_feedback("local_verification", reason=str(verify["reason"]))
                continue

            # General agent output (no special marker)
            self._print(f"\nAgent:\n{reply.strip()}")
            # Run verify to check progress
            verify = self._run_verify()
            if verify["ok"]:
                state.stall_count = 0
                self._print("Verification passed after agent's changes!")
                answer = self._prompt_user("Goal achieved? (y/n) [y]: ", default="y")
                if answer.strip().lower() not in ("n", "no"):
                    state.status = "completed"
                    self._git_commit(state, "collab", reply=reply)
                    self._print(f"Collaborative session {state.session_id} completed successfully.")
                    return state
                committed = self._commit_verified_progress(state, "collab", reply=reply)
                if committed:
                    self._print("Verified progress committed before continuing.")
                user_feedback = self._prompt_user("What still needs to be done? ", multiline=True)
                state.conversation.append({"role": "user", "content": user_feedback.strip() or "Not yet done."})
                self._save(state)
                self._print_agent_thinking()
                feedback = ""
            else:
                verify_reason = str(verify["reason"])
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
        self._save(state)
        self._print("Collab session stopped (no further progress). Session marked as failed.")
        return state

    def _phase_provider_resolve_execute(self, state: SessionState) -> SessionState:
        self._current_state = state
        feedback = ""
        while True:
            state.current_attempt += 1
            self._print(f"\n--- Provider recovery iteration {state.current_attempt} ---")

            prompt = self._build_provider_resolve_prompt(state, feedback)
            provider_artifacts_before = worktree_fingerprint(
                self.project_root, ignored_prefixes=(".antigravitycli/",)
            )
            try:
                reply = self._call_agent(state, f"provider-resolve-{state.current_attempt}", prompt)
            except RuntimeError as exc:
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

            assist_match = _NEED_USER_ASSIST.search(reply)
            if assist_match:
                state.stall_count = 0
                display = reply.strip()
                self._print(f"\nAgent:\n{display}")
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
            if worktree_fingerprint(
                self.project_root, ignored_prefixes=(".antigravitycli/",)
            ) != provider_artifacts_before:
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
                self._print("Provider references now pass local validation. Resuming run...")
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
                    state.status = "completed"
                    state.resolution = "provider_research_resolved"
                    self._save(state)
                    raise

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
            for msg in state.conversation:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                lines.append(f"\n[{role.upper()}]:\n{content}")
            lines.extend([
                "",
                "Analyze the unresolved provider references and the user's goal.",
                "- Ask targeted questions only when a decision is still needed.",
                "- If the unblock path is clear enough to begin editing provider-research artifacts, output 'GOAL_CLEAR' on a line by itself at the end.",
                "- Do not propose product-code changes in this mode.",
                "- Keep the scope limited to provider reference markdown, provider_references.lock.json, and tightly coupled requirement trace metadata only when the user chooses defer/assumption approval.",
                self.orch._document_language_instruction(),
            ])
            return "\n".join(lines)

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
            "",
            "--- Conversation History ---",
        ]
        for msg in state.conversation:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(f"\n[{role.upper()}]:\n{content}")

        lines.extend([
            "",
            f"Analyze the codebase and the {label} description.",
            "- If you need more information, ask specific questions.",
            "- If the problem is clear enough to proceed, output 'GOAL_CLEAR' on a line by itself at the end.",
        ])
        if self.mode == "fix":
            lines.extend([
                "- If after analyzing the codebase you determine this is NOT actually a bug "
                "(e.g., the behavior is by design, a configuration issue, or a user "
                "misunderstanding), explain your reasoning clearly and output "
                "'NOT_A_BUG: <one-line explanation>' on a line by itself.",
                "- When you output GOAL_CLEAR, also output on a separate line "
                "'FIX_VERIFY: <shell command>' — a single shell command that will "
                "return exit code 0 if the bug is fixed and non-zero if the bug still "
                "exists. This should target the specific bug described, not the whole test suite. "
                "Examples: a pytest invocation with -k filter, a curl command, a grep check, etc.",
                "- Match the repository's existing verification conventions when choosing FIX_VERIFY.",
                "- If the project uses a local conda env at ./.conda, every Python-oriented "
                "FIX_VERIFY command must run inside it via 'conda run -p ./.conda ...'.",
            ])
            gate_commands = self._gate_commands()
            if gate_commands:
                lines.append(
                    "- Current repository gate commands (reuse them as guidance for FIX_VERIFY when relevant):"
                )
                lines.extend(f"  - {command}" for command in gate_commands)
        lines.extend([
            "- Always explain your understanding before asking questions or declaring ready.",
            self.orch._document_language_instruction(),
        ])
        return "\n".join(lines)

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
            consolidated,
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
        for msg in state.conversation[-20:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(f"\n[{role.upper()}]:\n{content}")

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
                feedback,
                "",
            ])

        lines.extend([
            "",
            "Your task:",
            "1. Inspect the current provider reference markdown and lock files.",
            "2. Discuss the unblock path with the user when a decision is needed.",
            "3. Apply only the minimal edits needed to provider-research artifacts.",
            "4. If you need the user to choose between options, output 'NEED_USER_ASSIST: <question or decision needed>' on a line by itself.",
            "5. Do not modify product/runtime code, implementation tasks, or unrelated state files.",
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
            "- if the user defers a requirement, make only the tightly coupled trace/lock edits needed for that decision",
            "",
            "Final response: brief status update of what you changed and why.",
        ])
        return "\n".join(lines)

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
            consolidated,
            "",
        ]
        if feedback:
            lines.extend([
                "Previous attempt issues:",
                feedback,
                "",
                "CRITICAL: Before writing or modifying any code, output a step-by-step "
                "'Fix Plan' in Markdown detailing how you will address the issues above. "
                "Then proceed to implement the plan.",
                "",
            ])
        lines.extend([
            "Fix this bug:",
            "1. Identify the root cause",
            "2. Apply the minimal fix",
            "3. Update or add tests to cover the fix",
            "4. Do not modify .auto-agents state files",
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
        prompt = "\n".join(lines)
        repo_map = self._build_repo_map_section_for_session(consolidated, feedback)
        if repo_map:
            prompt = f"{prompt}\n\n{repo_map}"
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
            consolidated,
            "",
            "--- Conversation History ---",
        ]
        for msg in state.conversation[-20:]:  # keep context window manageable
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(f"\n[{role.upper()}]:\n{content}")

        if state.execution_log:
            lines.extend(["", "--- Execution Log (recent) ---"])
            for entry in state.execution_log[-10:]:
                lines.append(f"  Attempt {entry.get('attempt')}: {entry.get('action')} -> {str(entry.get('result', ''))[:200]}")

        if feedback:
            lines.extend([
                "",
                "Previous attempt issues:",
                feedback,
                "",
            ])

        lines.extend([
            "",
            "You are collaboratively debugging with the user. Your task:",
            "1. Analyze the current state of the code and any previous execution results",
            "2. Try to achieve the user's goal",
            "3. If you need the user to do something (e.g., test in browser, provide input),",
            "   output 'NEED_USER_ASSIST: <what you need>' on a line by itself",
            "4. If you discover a bug, output 'BUG_FOUND: <description>' and fix it",
            "5. If you believe the goal is achieved, output 'GOAL_ACHIEVED: <summary>' on a line by itself",
            "6. Provide a brief status update of what you did",
            "",
            "EXECUTION SAFETY RULES (critical — follow strictly):",
            "- Set a timeout for EVERY HTTP request or polling loop (max 60s per request, 5 min total for repeated polling).",
            "- If a subprocess or external command fails, report the failure immediately — do NOT retry indefinitely.",
            "- Use bounded retries: max 3 retries for any single operation, then stop and report the error.",
            "- Do NOT start infinite watch/poll/retry loops. Always use explicit exit conditions.",
            "- Prefer small incremental steps: start a service, verify it works, then proceed to the next step.",
            "- If you start background servers, verify they are healthy (e.g., curl health-check) before using them.",
            "- Report progress after each significant action so progress is visible.",
            "- If an operation is taking too long or keeps failing, stop and output BUG_FOUND with the error details.",
            "",
            "If this is a Python project, use the project-local conda env at ./.conda and install packages only inside it.",
            "Do not modify .auto-agents state files.",
            "",
            "IMPORTANT: If you made code changes in this turn, also output exactly one line in the form:",
            "  COMMIT_MESSAGE: <one-sentence description of what you changed and why>",
            "Rules for this line:",
            "- Imperative mood, plain prose, same language as the user's goal.",
            "- NO file paths, NO markdown links, NO code, NO shell commands, NO backticks, NO brackets.",
            "- Keep it under 72 characters; it is used verbatim as the git commit subject.",
        ])
        return "\n".join(lines)

    # ── Helpers ──────────────────────────────────────────────────

    def _call_agent(self, state: SessionState, label: str, prompt: str) -> str:
        """Call the AI agent and return its reply text."""
        prompt_path, output_path = session_artifact_paths(
            self.project_root, state.session_id, label,
        )
        write_text(prompt_path, prompt)
        effort_stage = "provider_research" if self.mode == "provider_resolve" else "implement"
        effort = self.config.efforts.get(effort_stage, "deep")
        # Always stream in collab/fix mode so the user sees real-time progress.
        # The --print-agent-output flag is not required.
        should_stream = self._print_agent_output or self.mode in ("collab", "fix", "provider_resolve")
        request = AgentRequest(
            stage=effort_stage,
            effort=effort,
            prompt=prompt,
            cwd=self.project_root,
            output_path=output_path,
            stream_output=(
                self.orch._stream_agent_output_callback(label)
                if should_stream
                else None
            ),
        )
        result: AgentResult = self.orch._call_with_failover(request)
        self.orch._emit_agent_output(label, result)
        if not result.ok:
            parts = []
            if result.stderr:
                parts.append(f"stderr={result.stderr}")
            if result.stdout:
                parts.append(f"stdout={result.stdout[:500]}")
            if result.summary and result.summary != result.stdout:
                parts.append(f"summary={result.summary[:500]}")
            detail = "; ".join(parts) if parts else "no output"
            raise RuntimeError(f"Agent call failed ({label}): {detail}")
        return (result.summary or result.stdout).strip()

    def _run_verify(self) -> Dict[str, object]:
        """Run verification appropriate for the session mode.

        For fix-mode sessions that have a baseline, this performs
        two-layer verification:
        1.  ``fix_verify_command`` — confirms the specific bug is fixed.
        2.  Baseline-diff gates — runs all gate commands and checks for
            *new* failures compared to the pre-fix baseline.

        For other sessions (collab, no baseline, no gates) falls back to
        the original short-circuit gate runner.
        """
        # Fix mode always goes through the baseline-diff path so that
        # fix_verify_command is checked even when no gate commands exist.
        if self.mode == "fix":
            return self._run_baseline_diff_verify()

        if not self._gate_commands():
            return {"ok": True, "reason": "no verification steps or commands configured"}
        return self.orch._run_task_verify()

    # ── Baseline-diff verification (fix mode) ────────────────────

    def _snapshot_baseline(self, state: SessionState) -> None:
        """Capture the current gate failures and git HEAD as the baseline.

        This is run once when the session transitions from *conversing* to
        *executing*, and again on resume if the git HEAD has moved.
        """
        self._print("Capturing baseline gate snapshot...")
        gate_commands = self._gate_commands()
        gate = run_gate_plan(
            gate_commands,
            self.config.gates.parallel_groups,
            self.project_root,
            collect_all=True,
        )
        state.baseline_failures = extract_failure_ids(gate)
        state.baseline_git_ref = head_ref(self.project_root)
        if state.baseline_failures:
            self._print(
                f"Baseline snapshot: {len(state.baseline_failures)} pre-existing failure(s) recorded."
            )
        else:
            self._print("Baseline snapshot: all gate commands pass.")
        self._save(state)

    def _ensure_baseline(self, state: SessionState) -> None:
        """Ensure a baseline snapshot exists, re-capturing if git HEAD moved."""
        current_head = head_ref(self.project_root)
        if state.baseline_git_ref and state.baseline_git_ref == current_head:
            return  # baseline is still valid
        if state.baseline_git_ref and state.baseline_git_ref != current_head:
            self._print(
                "Git HEAD changed since last baseline — re-capturing baseline snapshot."
            )
        self._snapshot_baseline(state)

    def _run_baseline_diff_verify(self) -> Dict[str, object]:
        """Two-layer verification for fix sessions.

        Returns ``{"ok": True, ...}`` only if:
        1.  ``fix_verify_command`` passes (if configured), AND
        2.  running the full gates produces no *new* failures relative to
            the stored baseline.
        """
        # Retrieve the current in-memory state via the save/load round-trip
        # is unnecessary — the caller already has it.  But _run_verify is
        # called from the loop which doesn't pass state, so we load it.
        # We'll refactor the signature after the feature is validated.
        state = self._current_state

        # Layer 1: targeted bug verification
        if state.fix_verify_command:
            import subprocess as _sp
            verify_command = self._fix_verify_command_for_execution(state.fix_verify_command)
            try:
                import os
                env = dict(os.environ)
                env["PYTEST_CURRENT_TEST"] = "auto_agents_session_verify"
                env["AUTO_AGENTS_TEST"] = "True"
                env["TESTING"] = "True"
                proc = _sp.run(
                    verify_command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    cwd=str(self.project_root),
                    timeout=120,
                    env=env,
                )
            except Exception as exc:
                return {"ok": False, "reason": f"fix_verify_command error: {exc}"}
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "non-zero exit").strip()
                return {
                    "ok": False,
                    "reason": f"fix_verify_command failed: {detail[:500]}",
                }

        # Layer 2: baseline-diff gate check
        gate_commands = self._gate_commands()
        if not gate_commands:
            return {"ok": True, "reason": "no verification steps or commands configured"}
        gate = run_gate_plan(
            gate_commands,
            self.config.gates.parallel_groups,
            self.project_root,
            collect_all=True,
        )
        extraction = extract_failure_info(gate)
        current_failures = extraction.failure_ids
        if not extraction.comparable and not gate.ok:
            return {
                "ok": False,
                "reason": "non-comparable verification failure: failed command did not yield stable test-case failure ids",
            }
        new_failures = sorted(set(current_failures) - set(state.baseline_failures))
        if new_failures:
            return {
                "ok": False,
                "reason": (
                    f"{len(new_failures)} new failure(s) introduced: "
                    + ", ".join(new_failures[:10])
                ),
            }
        return {"ok": True, "reason": gate.summary}

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
        if state.current_attempt >= state.hard_ceiling:
            return (
                f"Hard attempt ceiling ({state.hard_ceiling}) reached. Stopping."
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
            if not line or line == "GOAL_CLEAR" or line.startswith("FIX_VERIFY:") or line.startswith("COMMIT_MESSAGE:"):
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
        try:
            commit_all(self.project_root, message)
            return True
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

    def _consolidate_goal(self, state: SessionState) -> str:
        """Build a consolidated goal description from the conversation."""
        parts = []
        for msg in state.conversation:
            if msg.get("role") == "user":
                parts.append(msg.get("content", ""))
            elif msg.get("role") == "agent":
                content = msg.get("content", "")
                if _GOAL_CLEAR.search(content):
                    parts.append(f"Agent's understanding:\n{_GOAL_CLEAR.sub('', content).strip()}")
        return "\n\n".join(parts) if parts else state.goal

    def _save(self, state: SessionState) -> None:
        state.updated_at = self._now()
        save_session_state(self.project_root, state)

    def _print(self, msg: str, flush: bool = False) -> None:
        print(msg, file=sys.stderr, flush=flush)

    def _print_agent_thinking(self) -> None:
        self._print("\nAgent is thinking, please wait...", flush=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
