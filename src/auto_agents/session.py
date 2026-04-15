from __future__ import annotations

import json
import re
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
from .gates import run_commands
from .git_ops import commit_all
from .io_utils import read_text, write_text
from .models import (
    AgentRequest,
    AgentResult,
    SessionState,
)

_GOAL_CLEAR = re.compile(r"^GOAL_CLEAR\s*$", re.MULTILINE)
_NEED_USER_ASSIST = re.compile(r"^NEED_USER_ASSIST:\s*(.+)$", re.MULTILINE)
_BUG_FOUND = re.compile(r"^BUG_FOUND:\s*(.+)$", re.MULTILINE)
_GOAL_ACHIEVED = re.compile(r"^GOAL_ACHIEVED:\s*(.+)$", re.MULTILINE)


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
        # Expose the same user-input helper used by the orchestrator.
        self._prompt_user = orchestrator._prompt_user

    # ── Public entry points ──────────────────────────────────────

    def start(self) -> SessionState:
        """Create a new session and drive it to completion (or interruption)."""
        state = create_session(self.project_root, self.mode)
        self._print(f"Session {state.session_id} started in {state.mode} mode.")
        return self._drive(state)

    def resume(self, session_id: str) -> SessionState:
        """Resume an existing session."""
        state = load_session_state(self.project_root, session_id)
        if state.status in ("completed", "failed"):
            self._print(f"Session {session_id} is already {state.status}.")
            return state
        self._print(f"Resuming session {session_id} ({state.mode} mode, status={state.status}).")
        return self._drive(state)

    def offer_resume_or_new(self) -> SessionState:
        """If there are active sessions for this mode, offer to resume; else start new."""
        active = [
            s for s in list_sessions(self.project_root)
            if s.mode == self.mode and s.status not in ("completed", "failed")
        ]
        if active:
            latest = active[-1]
            self._print(f"Found active {self.mode} session: {latest.session_id} (status={latest.status})")
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

            if _GOAL_CLEAR.search(reply):
                display = _GOAL_CLEAR.sub("", reply).strip()
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
        feedback = ""
        while state.current_attempt < state.max_attempts:
            state.current_attempt += 1
            self._print(f"\n--- Fix attempt {state.current_attempt}/{state.max_attempts} ---")

            prompt = self._build_fix_prompt(state, feedback)
            try:
                reply = self._call_agent(state, f"fix-{state.current_attempt}", prompt)
            except RuntimeError as exc:
                err_msg = str(exc)
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "agent_error",
                    "result": err_msg[:500],
                    "timestamp": self._now(),
                })
                self._save(state)
                self._print(f"Agent call failed (transient): {err_msg[:200]}")
                self._print("Will retry on next attempt.")
                feedback = f"Previous attempt failed with a transient error: {err_msg[:300]}"
                continue

            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "fix",
                "result": reply[:500],
                "timestamp": self._now(),
            })
            self._save(state)

            # Quick verify
            quick_fail = self.orch._quick_verify_failure()
            if quick_fail:
                self._print(f"Quick verify failed: {quick_fail}")
                feedback = self.orch._format_retry_feedback("pre_verify_check", reason=quick_fail)
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "quick_verify_fail",
                    "result": quick_fail,
                    "timestamp": self._now(),
                })
                self._save(state)
                continue

            # Gate verify
            verify = self._run_verify()
            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "verify",
                "result": "pass" if verify["ok"] else str(verify["reason"]),
                "timestamp": self._now(),
            })
            self._save(state)

            if verify["ok"]:
                self._print("Verification passed!")
                state.status = "completed"
                self._git_commit(state, "fix")
                self._print(f"Bug fix completed in session {state.session_id}.")
                return state

            self._print(f"Verification failed: {verify['reason']}")
            feedback = self.orch._format_retry_feedback("local_verification", reason=str(verify["reason"]))

        state.status = "failed"
        self._save(state)
        self._print("Max fix attempts exhausted. Session marked as failed.")
        return state

    # ── Phase 2b: Collab mode loop ───────────────────────────────

    def _phase_collab_loop(self, state: SessionState) -> SessionState:
        feedback = ""
        while state.current_attempt < state.max_attempts:
            state.current_attempt += 1
            self._print(f"\n--- Collab iteration {state.current_attempt}/{state.max_attempts} ---")

            prompt = self._build_collab_prompt(state, feedback)
            try:
                reply = self._call_agent(state, f"collab-{state.current_attempt}", prompt)
            except RuntimeError as exc:
                err_msg = str(exc)
                state.execution_log.append({
                    "attempt": state.current_attempt,
                    "action": "agent_error",
                    "result": err_msg[:500],
                    "timestamp": self._now(),
                })
                self._save(state)
                self._print(f"Agent call failed (transient): {err_msg[:200]}")
                self._print("Will retry on next iteration.")
                feedback = f"Previous attempt failed with a transient error: {err_msg[:300]}"
                continue

            state.conversation.append({"role": "agent", "content": reply})
            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "collab",
                "result": reply[:500],
                "timestamp": self._now(),
            })
            self._save(state)

            # Check for NEED_USER_ASSIST
            assist_match = _NEED_USER_ASSIST.search(reply)
            if assist_match:
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

            # Check for GOAL_ACHIEVED
            achieved_match = _GOAL_ACHIEVED.search(reply)
            if achieved_match:
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
                    self._print(f"Verification failed: {verify['reason']}")
                    self._print("Continuing the loop to fix verification issues.")
                    feedback = self.orch._format_retry_feedback("local_verification", reason=str(verify["reason"]))
                    continue

                self._print("Verification passed!")
                answer = self._prompt_user("Do you confirm the goal is achieved? (y/n) [y]: ", default="y")
                if answer.strip().lower() not in ("n", "no"):
                    state.status = "completed"
                    self._git_commit(state, "collab")
                    self._print(f"Collaborative session {state.session_id} completed successfully.")
                    return state

                user_feedback = self._prompt_user("What still needs to be done? ", multiline=True)
                state.conversation.append({"role": "user", "content": user_feedback.strip() or "Not yet done."})
                self._save(state)
                self._print_agent_thinking()
                feedback = ""
                continue

            # Check for BUG_FOUND – agent found & fixed a bug inline
            bug_match = _BUG_FOUND.search(reply)
            if bug_match:
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
                    self._git_commit(state, "collab")
                    self._print("Bug fix verified and committed.")
                else:
                    self._print(f"Bug fix verification failed: {verify['reason']}")
                    feedback = self.orch._format_retry_feedback("local_verification", reason=str(verify["reason"]))
                continue

            # General agent output (no special marker)
            self._print(f"\nAgent:\n{reply.strip()}")
            # Run verify to check progress
            verify = self._run_verify()
            if verify["ok"]:
                self._print("Verification passed after agent's changes!")
                answer = self._prompt_user("Goal achieved? (y/n) [y]: ", default="y")
                if answer.strip().lower() not in ("n", "no"):
                    state.status = "completed"
                    self._git_commit(state, "collab")
                    self._print(f"Collaborative session {state.session_id} completed successfully.")
                    return state
                user_feedback = self._prompt_user("What still needs to be done? ", multiline=True)
                state.conversation.append({"role": "user", "content": user_feedback.strip() or "Not yet done."})
                self._save(state)
                self._print_agent_thinking()
                feedback = ""
            else:
                feedback = self.orch._format_retry_feedback("local_verification", reason=str(verify["reason"]))

        state.status = "failed"
        self._save(state)
        self._print("Max collab iterations exhausted. Session marked as failed.")
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
        request = AgentRequest(
            stage="implement",
            effort=effort,
            prompt=prompt,
            cwd=self.project_root,
            output_path=output_path,
            stream_output=(
                self.orch._stream_agent_output_callback(label)
                if self._print_agent_output
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
        """Run configured gate commands."""
        if not self.config.gates.commands:
            return {"ok": True, "reason": "no verification commands configured"}
        return self.orch._run_task_verify()

    def _git_commit(self, state: SessionState, prefix: str) -> None:
        """Persist current state, then commit current changes if auto-commit is enabled."""
        if not self.config.git.commit_each_task:
            self._save(state)
            return
        summary = state.goal[:60].replace("\n", " ")
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
        except RuntimeError as exc:
            state.execution_log.append({
                "attempt": state.current_attempt,
                "action": "commit_failed",
                "result": str(exc),
                "timestamp": self._now(),
            })
            self._save(state)

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
