from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, TextIO, Tuple

from .adapters import CodexAdapter, MockAdapter, ShellAdapter
from .config import (
    bootstrap_project,
    docs_dir,
    load_project_config,
    load_run_state,
    load_task_plan,
    review_path,
    run_artifact_paths,
    save_project_config,
    save_run_state,
    save_task_plan,
    task_plan_path,
    write_run_prompt,
)
from .gates import run_commands
from .git_ops import changed_entries, changed_files, changed_paths, commit_all, ensure_repo, is_repo, require_clean_tree, worktree_fingerprint
from .io_utils import read_text, write_text
from .models import (
    APPROVAL_BY_STAGE,
    AgentResult,
    AgentRequest,
    AgentUsage,
    DOCUMENT_LANGUAGE_OPTIONS,
    ProjectConfig,
    RunState,
    STAGE_ORDER,
    TaskSpec,
)
from .validation import validate_task_plan_payload, validation_report
from .validation import validate_required_document


class Orchestrator:
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

    @staticmethod
    def init_project(project_root: Path, name: str, provider_kind: str, doc_language: str = "en") -> Path:
        root = bootstrap_project(project_root, name, provider_kind, doc_language=doc_language)
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

        if target_stage in state.stage_summaries:
            del state.stage_summaries[target_stage]
        if active_gate in state.approved_gates:
            state.approved_gates.remove(active_gate)
        state.pending_approval = ""
        state.status = "pending"
        state.rejection_reason = reason
        state.rejected_stage = target_stage
        save_run_state(self.project_root, state)
        return state

    def run(
        self,
        spec_file: Path,
        auto_approve: bool = False,
        max_tasks: Optional[int] = None,
        skip_validate: bool = False,
        print_agent_output: bool = False,
        doc_language: Optional[str] = None,
    ) -> RunState:
        ensure_repo(self.project_root, auto_init=self.config.git.auto_init_repo)
        self._print_agent_output = print_agent_output
        try:
            if doc_language is not None:
                self._set_document_language(doc_language)
            state = load_run_state(self.project_root)
            self._active_spec_file = spec_file.expanduser().resolve()
            self._ensure_preconditions(state, spec_file=spec_file, skip_validate=skip_validate)

            if state.status == "completed":
                print("Project execution is already completed. Do you want to start a new iteration for further development? [y/N]", file=sys.stderr)
                user_conf = self._prompt_user("").strip().lower()
                if user_conf in ("y", "yes"):
                    state.run_id = uuid.uuid4().hex[:12]
                    state.status = "pending"
                    state.current_stage = "clarify"
                    for s in ["clarify", "design", "plan", "verify", "readme"]:
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
                if pending_gate and pending_gate in self.config.approvals.enabled:
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
            return state
        finally:
            self._print_agent_output = False
            self._active_spec_file = None

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
                
        if state.rejected_stage == "clarify" and state.rejection_reason:
            history.append({
                "role": "user",
                "content": f"The previous output was rejected. Please address this feedback:\n{state.rejection_reason}"
            })
            state.rejected_stage = ""
            state.rejection_reason = ""

        def _history_role(msg: object) -> str:
            if not isinstance(msg, dict):
                return ""
            role = str(msg.get("role", "")).strip().lower()
            if role == "assistant":
                return "agent"
            return role

        # Resume interrupted conversation: if trailing history entries are from
        # the agent (e.g. process crashed before user reply was saved), strip
        # those tails (including spurious READY_TO_GENERATE reruns), replay the
        # last substantive agent message, and collect a fresh user reply.
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

        print("Entering interactive clarify session, please wait for the agent to analyze the spec...", file=sys.stderr, flush=True)
        
        max_rounds = 15
        rounds = 0
        
        while rounds < max_rounds:
            rounds += 1
            prompt_lines = [
                f"Project root: {self.project_root}",
                "Read the input spec from: " + str(spec_file),
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
                print("\nAgent is ready to generate project_brief.md.", file=sys.stderr)
                user_conf = self._prompt_user("Confirm generation? (y/n) [y]: ", default="y")

                if user_conf.strip().lower() not in ("n", "no"):
                    break
                else:
                    user_reply = self._prompt_user("Please provide your thoughts: ", multiline=True)
                    if user_reply.strip():
                        history.append({"role": "user", "content": user_reply})
                        write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))
                    continue

            print("\nAgent:", file=sys.stderr)
            print(reply, file=sys.stderr)
            
            user_reply = self._prompt_user("\nYour reply: ", multiline=True)
            
            if user_reply.strip():
                history.append({"role": "user", "content": user_reply})
            else:
                history.append({"role": "user", "content": "I have nothing to add. Please proceed to generate if you are ready."})
            
            write_text(history_path, json.dumps(history, indent=2, ensure_ascii=False))
            print("\nAgent is thinking, please wait...", file=sys.stderr, flush=True)
                
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
                # Reopen stdin from the terminal so subsequent reads work.
                try:
                    tty = "/dev/tty" if os.path.exists("/dev/tty") else "CON"
                    sys.stdin = open(tty, "r")
                except OSError:
                    pass
                # Fix surrogate escapes from Windows console encoding mismatches
                return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
            else:
                return input(prompt)
        return default

    def _run_implementation_loop(self, state: RunState, max_tasks: Optional[int]) -> RunState:
        tasks = state.tasks or self._load_tasks_from_plan()
        
        if state.rejected_stage == "implement" and state.rejection_reason:
            import time
            tasks.append(
                TaskSpec(
                    task_id=f"fix-rejection-{int(time.time()*1000)}",
                    title="Fix issues after release rejection",
                    description=f"The release was rejected with the following feedback:\n{state.rejection_reason}\n\nPlease fix these issues.",
                    acceptance_criteria=[
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

            if not (resume_existing or allow_dirty_retry) and self.config.gates.require_clean_git_before_task:
                require_clean_tree(self.project_root)

            if task.status == "pending":
                task.status = "in_progress"
                self._persist_tasks(tasks)

            if not task.test_generated:
                self._run_task_test_writer(state, task)

            gate_result = self._execute_task_with_retries(state, task, resume_existing=resume_existing)
            if not gate_result["ok"]:
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

    def _run_task_test_writer(self, state: RunState, task: TaskSpec) -> None:
        """Generate TDD contract tests for a task before implementation."""
        prompt = self._build_task_prompt(task, "test_writer")
        self._emit_task_activity(task, "test_writer", 1)
        result = self._run_agent_with_retries(
            state=state,
            stage="implement",
            stage_key=f"test_writer-{task.task_id}",
            prompt=prompt,
        )
        if not result.ok:
            raise RuntimeError(
                f"Test writer for {task.task_id} failed: {result.stderr or result.summary}"
            )

        # Capture newly created/modified files as contract files
        diff_output = self._git_text("diff", "--name-only", "HEAD")
        untracked = self._git_text("ls-files", "--others", "--exclude-standard")
        all_new = set()
        for line in (diff_output + "\n" + untracked).splitlines():
            stripped = line.strip()
            if stripped:
                all_new.add(stripped)
        task.contract_files = sorted(all_new)
        task.test_generated = True
        self._persist_tasks(state.tasks)

        if task.contract_files:
            commit_msg = f"test: Add TDD acceptance contract for {task.task_id}"
            commit_all(self.project_root, commit_msg)

    def _run_task_verify(self) -> Dict[str, object]:
        verify_gate = run_commands(self.config.gates.commands, self.project_root)
        if not verify_gate.ok:
            return {
                "ok": False,
                "reason": verify_gate.summary,
            }
        return {"ok": True, "reason": verify_gate.summary}

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
            capture_output=True,
        )
        if process.returncode != 0:
            return ""
        return process.stdout.strip()

    def _build_review_context(self, verify_reason: str = "", max_diff_chars: int = 6000) -> str:
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

    def _quick_verify_failure(self) -> Optional[str]:
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

        for command in self.config.gates.commands:
            stripped = command.strip()
            if not stripped:
                continue
            if (".conda/conda-meta" in stripped or "conda run -p ./.conda" in stripped) and not conda_meta.exists():
                return "expected a project-local conda environment at ./.conda/conda-meta before verification"
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
                    return f"verification command is not runnable: {command}"
                continue
            if shutil.which(executable) is None:
                return f"verification command is not runnable: {command}"
        return None

    @staticmethod
    def _format_retry_feedback(
        failure_type: str,
        reason: str = "",
        review_summary: str = "",
    ) -> str:
        lines = [f"- Failure type: {failure_type}"]
        if reason:
            lines.append(f"- Reason: {reason}")
        if review_summary.strip():
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
            raise RuntimeError("verify stage failed")
        return state

    def _load_tasks_from_plan(self) -> List[TaskSpec]:
        payload = load_task_plan(self.project_root)
        tasks = [TaskSpec.from_dict(item) for item in payload.get("tasks", [])]
        if not tasks:
            raise RuntimeError(f"No tasks found in {task_plan_path(self.project_root)}")
        return tasks

    def _run_readme(self, state: RunState, spec_file: Path) -> RunState:
        prompt = self._build_prompt(stage="readme", spec_file=spec_file)
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
        return state

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
                "Keep the brief compact and focused on the target scope.",
                "Preserve the exact top-level and section headings already present in the file.",
                self._clarify_spec_instruction(spec_kind),
                self._document_language_instruction(),
            ]
            if is_iteration:
                lines.extend([
                    f"This is an ITERATION run. The project already has completed work and an existing brief at {brief}.",
                    "IMPORTANT: Do NOT discard or rewrite the existing content of the brief.",
                    "APPEND a new section for the iteration scope below the existing content.",
                    "Preserve all previously documented scope, requirements, and constraints.",
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
                ])
            lines.append("Final response: 3 short bullets summarizing the design.")
            return "\n".join(lines)

        if stage == "plan":
            lines = common + [
                f"Read the input spec: {spec_file}",
                f"Read: {brief}",
                f"Read: {architecture}",
                f"Replace this JSON file with a task plan of minimal verifiable feature slices: {plan}",
                "At the root of the JSON, also define test_strategy and verification_commands.",
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
                "Each task must contain task_id, title, description, acceptance, status, commit_message.",
                "A good plan may contain only a few tasks for a small target or many tasks for a broad target, as long as the slicing remains disciplined.",
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
                "Include at minimum: project overview, architecture, repository structure, setup or prerequisites, usage, and verification.",
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
        common = [
            f"Project root: {self.project_root}",
            f"Project brief: {brief}",
            f"Architecture: {architecture}",
            "Work only on the current task.",
            "Keep changes scoped and testable.",
            f"Task JSON:\n{task_json}",
        ]

        if stage == "implement":
            lines = common + [
                "Implement only this feature slice.",
                "Add or update tests where appropriate.",
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
                "Use the supplied changed-file and diff context first. Only inspect the rest of the repository when the diff is insufficient.",
                "Return only the review result. Do not include any preamble, file path note, or tool narration.",
                "The first non-empty line must be exactly 'DECISION: pass' or 'DECISION: fail'.",
                self._review_language_instruction(),
                "After the first line, provide a short review summary.",
            ]
            if review_context.strip():
                lines.extend(["", review_context.strip()])
            return "\n".join(lines)

        if stage == "test_writer":
            lines = common + [
                "You are a Test-Writer agent. Your ONLY job is to generate black-box acceptance tests for this task.",
                "Generate test cases that verify the acceptance criteria defined in the Task JSON above.",
                "Do NOT implement any business logic, production code, or feature code.",
                "Only create or modify test files (e.g. files under tests/ or with test_ prefix).",
                "The tests should be runnable but are expected to FAIL until the feature is implemented.",
                "Write tests that are concise, deterministic, and cover edge cases implied by the acceptance criteria.",
                "If this is a Python project, use unittest or pytest conventions.",
                "Do not modify .auto-agents state files.",
                "Final response: 3 short bullets describing the test files created.",
            ]
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
                review_summary=task.review_summary,
            )
        last_reason = "task failed without a recorded reason"
        last_review = ""

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

                # Contract file audit: reject if implement agent tampered with TDD test files
                if task.contract_files:
                    diff_output = self._git_text("diff", "HEAD", "--name-only")
                    untracked = self._git_text("ls-files", "--others", "--exclude-standard")
                    modified = set()
                    for line in (diff_output + "\n" + untracked).splitlines():
                        stripped = line.strip()
                        if stripped:
                            modified.add(stripped)
                    tampered = modified & set(task.contract_files)
                    if tampered:
                        for path in sorted(tampered):
                            subprocess.run(
                                ["git", "restore", "--staged", path],
                                cwd=str(self.project_root),
                                capture_output=True,
                            )
                            subprocess.run(
                                ["git", "restore", path],
                                cwd=str(self.project_root),
                                capture_output=True,
                            )
                        last_reason = (
                            f"Permission Denied: the implement agent modified TDD contract files: "
                            f"{', '.join(sorted(tampered))}. These files are immutable test contracts. "
                            f"Changes have been reverted."
                        )
                        feedback = self._format_retry_feedback(
                            "contract_file_tampering",
                            reason=last_reason,
                        )
                        continue

            self._emit_task_activity(task, "verify", attempt)
            quick_failure = self._quick_verify_failure()
            if quick_failure:
                last_reason = quick_failure
                feedback = self._format_retry_feedback(
                    "pre_verify_check",
                    reason=last_reason,
                )
                self._emit_task_verify_result(task, "fail", last_reason)
                continue

            verify_result = self._run_task_verify()
            if not verify_result["ok"]:
                last_reason = str(verify_result["reason"])
                feedback = self._format_retry_feedback(
                    "local_verification",
                    reason=last_reason,
                )
                self._emit_task_verify_result(task, "fail", last_reason)
                continue

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
            
            # Synchronize the task's review_summary so that the next iteration's 
            # built-in Task JSON context exactly matches the newly generated rejection context,
            # avoiding the "Split Brain" issue.
            task.review_summary = last_review
            
            feedback = self._format_retry_feedback(
                "review_rejected",
                reason=last_reason,
                review_summary=last_review,
            )

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
            result = self.adapter.run(request)
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
        print(f"[stage:{stage}] start model={model}", file=self.agent_output_stream, flush=True)

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
                f"returncode={result.returncode} attempts={attempts} model={model or 'unknown'} "
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

    def _emit_task_verify_result(self, task: TaskSpec, decision: str, summary: str) -> None:
        sections = [f"[task:{task.task_id}] verify decision={decision}"]
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
        if provider_kind != "codex":
            return self.config.provider.binary

        explicit_model = self._configured_explicit_model()
        if explicit_model:
            return explicit_model

        profile = self.config.provider.profile_map.get(effort)
        if profile:
            return f"profile:{profile}"
        if stage == "review":
            return "default"
        return "default"

    def _configured_explicit_model(self) -> str:
        extra_args = list(self.config.provider.extra_args)
        for index, value in enumerate(extra_args):
            if value in {"--model", "-m"} and index + 1 < len(extra_args):
                return extra_args[index + 1]
        return ""

    def _set_document_language(self, language: str) -> None:
        if language not in DOCUMENT_LANGUAGE_OPTIONS:
            raise ValueError(f"Unsupported document language: {language}")
        if self.config.docs.language == language:
            return
        self.config.docs.language = language
        save_project_config(self.project_root, self.config)

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
        errors = validate_task_plan_payload(payload, require_verification=True)
        if not errors:
            return None
        bullets = "\n".join(f"- {item}" for item in errors)
        return (
            "The task plan JSON is invalid. Rewrite the file and fix all issues exactly.\n"
            f"{bullets}"
        )

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
        if not errors:
            return None
        bullets = "\n".join(f"- {item}" for item in errors)
        return (
            "The project brief is missing required template headings. Rewrite the file in place and "
            "preserve the exact required headings.\n"
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
        normalized = [str(item) for item in commands]
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

    def _pending_stages(self, state: RunState) -> List[str]:
        pending: List[str] = []
        completed = set(state.stage_summaries.keys())
        for stage in STAGE_ORDER:
            if stage == "implement":
                if not state.tasks:
                    pending.append(stage)
                    continue
                if any(task.status != "done" for task in state.tasks):
                    pending.append(stage)
                    continue
            elif stage not in completed:
                pending.append(stage)
        return pending

    def _commit_planning_baseline_if_needed(self, tasks: Iterable[TaskSpec]) -> None:
        changes = changed_files(self.project_root)
        if not changes:
            return
        if any(task.status != "pending" for task in tasks):
            return

        allowed = {".gitignore", "README.md", "spec.md"}
        if self._active_spec_file is not None:
            try:
                allowed.add(str(self._active_spec_file.relative_to(self.project_root)))
            except ValueError:
                pass
        for line in changes.splitlines():
            path = line[3:].strip()
            if not path:
                continue
            if path.startswith(".auto-agents/"):
                continue
            if path in allowed:
                continue
            return

        commit_all(self.project_root, "docs(project): capture planning baseline")

    def _should_resume_task(self, state: RunState, task: TaskSpec) -> bool:
        if task.status != "pending":
            return False
        if not changed_files(self.project_root):
            return False
        attempt_key = f"implement-{task.task_id}"
        return state.agent_attempts.get(attempt_key, 0) > 0
