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
)
from .gates import run_commands, run_commands_collect_all, extract_failure_ids
from .git_ops import changed_paths, commit_all, head_ref
from .io_utils import read_text, write_text
from .models import (
    AgentRequest,
    AgentResult,
    SESSION_AGENT_ERROR_THRESHOLD,
    SESSION_STALL_THRESHOLD,
    SessionState,
)

_GOAL_CLEAR = re.compile(r"^GOAL_CLEAR\s*$", re.MULTILINE)
_NOT_A_BUG = re.compile(r"^NOT_A_BUG:\s*(.+)$", re.MULTILINE)
_NEED_USER_ASSIST = re.compile(r"^NEED_USER_ASSIST:\s*(.+)$", re.MULTILINE)
_BUG_FOUND = re.compile(r"^BUG_FOUND:\s*(.+)$", re.MULTILINE)
_GOAL_ACHIEVED = re.compile(r"^GOAL_ACHIEVED:\s*(.+)$", re.MULTILINE)
_FIX_VERIFY = re.compile(r"^FIX_VERIFY:\s*(.+)$", re.MULTILINE)


class Session:
    """Lightweight conversational workflow for bug fixes and collaborative debugging.

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
        resumable = [
            s for s in list_sessions(self.project_root)
            if s.mode == self.mode and s.status != "completed"
        ]
        if resumable:
            latest = resumable[-1]
            self._print(f"Found resumable {self.mode} session: {latest.session_id} (status={latest.status})")
            answer = self._prompt_user("Resume this session? (y/n) [y]: ", default="y")
            if answer.strip().lower() not in ("n", "no"):
                return self.resume(latest.session_id)
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

    # ── Prompt builders ──────────────────────────────────────────

    def _build_converse_prompt(self, state: SessionState) -> str:
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
            ])
        lines.extend([
            "- Always explain your understanding before asking questions or declaring ready.",
            self.orch._document_language_instruction(),
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
        ])
        return "\n".join(lines)

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
        ])
        return "\n".join(lines)

    # ── Helpers ──────────────────────────────────────────────────

    def _call_agent(self, state: SessionState, label: str, prompt: str) -> str:
        """Call the AI agent and return its reply text."""
        prompt_path, output_path = session_artifact_paths(
            self.project_root, state.session_id, label,
        )
        write_text(prompt_path, prompt)
        effort = self.config.efforts.get("implement", "deep")
        # Always stream in collab/fix mode so the user sees real-time progress.
        # The --print-agent-output flag is not required.
        should_stream = self._print_agent_output or self.mode in ("collab", "fix")
        request = AgentRequest(
            stage="implement",
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

        if not self.config.gates.commands:
            return {"ok": True, "reason": "no verification commands configured"}
        return self.orch._run_task_verify()

    # ── Baseline-diff verification (fix mode) ────────────────────

    def _snapshot_baseline(self, state: SessionState) -> None:
        """Capture the current gate failures and git HEAD as the baseline.

        This is run once when the session transitions from *conversing* to
        *executing*, and again on resume if the git HEAD has moved.
        """
        self._print("Capturing baseline gate snapshot...")
        gate = run_commands_collect_all(self.config.gates.commands, self.project_root)
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
            try:
                proc = _sp.run(
                    state.fix_verify_command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    cwd=str(self.project_root),
                    timeout=120,
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
        if not self.config.gates.commands:
            return {"ok": True, "reason": "no verification commands configured"}
        gate = run_commands_collect_all(self.config.gates.commands, self.project_root)
        current_failures = extract_failure_ids(gate)
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

        for pattern in (_GOAL_ACHIEVED, _BUG_FOUND, _NOT_A_BUG):
            match = pattern.search(text)
            if match:
                return self._normalize_commit_subject(match.group(1))

        candidates: List[str] = []
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line or line == "GOAL_CLEAR" or line.startswith("FIX_VERIFY:"):
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
            candidates.append(line)

        if not candidates:
            return ""
        return self._normalize_commit_subject(candidates[-1])

    def _normalize_commit_subject(self, text: str, max_length: int = 72) -> str:
        subject = " ".join(text.replace("`", "").replace("*", "").split())
        subject = subject.strip(" \t\r\n.,;:-")
        if not subject:
            return ""
        if len(subject) <= max_length:
            return subject
        truncated = subject[: max_length + 1].rsplit(" ", 1)[0].rstrip(" \t\r\n.,;:-")
        if len(truncated) < max_length // 2:
            truncated = subject[:max_length].rstrip(" \t\r\n.,;:-")
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
        """Persist current state, then commit current changes if auto-commit is enabled."""
        if not self.config.git.commit_each_task:
            self._save(state)
            return False
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
