from __future__ import annotations

import json
import sys
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
from .git_ops import changed_files, commit_all, ensure_repo, is_repo, require_clean_tree
from .io_utils import write_text
from .models import (
    APPROVAL_BY_STAGE,
    AgentResult,
    AgentRequest,
    DOCUMENT_LANGUAGE_OPTIONS,
    ProjectConfig,
    RunState,
    STAGE_ORDER,
    TaskSpec,
)
from .validation import validate_task_plan_payload, validation_report
from .validation import validate_required_document


class Orchestrator:
    def __init__(self, project_root: Path, agent_output_stream: Optional[TextIO] = None) -> None:
        self.project_root = project_root.resolve()
        self.config = load_project_config(self.project_root)
        self.adapter = self._build_adapter(self.config)
        self.agent_output_stream = agent_output_stream or sys.stderr
        self._print_agent_output = False

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

    def run(
        self,
        idea_file: Path,
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
            self._ensure_preconditions(state, idea_file=idea_file, skip_validate=skip_validate)

            if state.status == "completed":
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

            for stage in self._pending_stages(state):
                try:
                    if stage == "implement":
                        state = self._run_implementation_loop(state, max_tasks=max_tasks)
                    elif stage == "verify":
                        state = self._run_verify(state)
                    else:
                        state = self._run_agent_stage(stage, state, idea_file)
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

    def _ensure_preconditions(self, state: RunState, idea_file: Path, skip_validate: bool) -> None:
        if not idea_file.exists():
            state.status = "failed"
            state.last_error = f"idea file does not exist: {idea_file}"
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

    def _run_agent_stage(self, stage: str, state: RunState, idea_file: Path) -> RunState:
        prompt = self._build_prompt(stage=stage, idea_file=idea_file)
        validator_map = {
            "clarify": self._clarify_validation_feedback,
            "design": self._design_validation_feedback,
            "plan": self._plan_validation_feedback,
        }
        validator = validator_map.get(stage)
        result = self._run_agent_with_retries(
            state=state,
            stage=stage,
            stage_key=stage,
            prompt=prompt,
            validation_feedback=validator,
        )
        state.current_stage = stage
        state.stage_summaries[stage] = result.summary.strip()
        state.last_error = ""
        if stage == "plan":
            self._apply_generated_verification_config()
            state.tasks = self._load_tasks_from_plan()
        return state

    def _run_implementation_loop(self, state: RunState, max_tasks: Optional[int]) -> RunState:
        tasks = state.tasks or self._load_tasks_from_plan()
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

            gate_result = self._execute_task_with_retries(state, task, resume_existing=resume_existing)
            if not gate_result["ok"]:
                task.status = "blocked"
                task.review_summary = str(gate_result["review"])
                self._persist_tasks(tasks)
                raise RuntimeError(f"Task {task.task_id} failed gates: {gate_result['reason']}")

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

    def _run_task_review_and_verify(self, run_id: str, task: TaskSpec) -> Dict[str, object]:
        review_prompt = self._build_task_prompt(task, "review")
        review_result = self._run_agent_with_retries(
            state=None,
            stage="review",
            stage_key=f"review-{task.task_id}",
            prompt=review_prompt,
            validation_feedback=self._review_validation_feedback,
            run_id=run_id,
        )
        decision, summary = self._parse_review_decision(review_result.summary)
        write_text(review_path(self.project_root), summary + "\n")
        if decision != "pass":
            return {"ok": False, "review": summary, "reason": "review rejected the task"}

        verify_gate = run_commands(self.config.gates.commands, self.project_root)
        if not verify_gate.ok:
            return {
                "ok": False,
                "review": summary,
                "reason": verify_gate.summary,
            }
        return {"ok": True, "review": summary}

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

    def _build_prompt(self, stage: str, idea_file: Path) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        plan = task_plan_path(self.project_root)
        common = [
            f"Project root: {self.project_root}",
            "Work only inside this repository.",
            "Keep outputs concise and file-driven.",
            "Do not restate large documents in your final response.",
            "Do not modify the system-wide environment or install global packages.",
        ]

        if stage == "clarify":
            lines = common + [
                f"Read the idea from: {idea_file}",
                f"Update this file in place: {brief}",
                "Keep the brief compact and scoped to the MVP.",
                "Preserve the exact top-level and section headings already present in the file.",
                self._document_language_instruction(),
                "Final response: 3 short bullets summarizing the clarified scope.",
            ]
            return "\n".join(lines)

        if stage == "design":
            lines = common + [
                f"Read the current project brief: {brief}",
                f"Update this file in place: {architecture}",
                "Record only top-level architecture decisions and major risks.",
                "Preserve the exact top-level and section headings already present in the file.",
                self._document_language_instruction(),
                "Final response: 3 short bullets summarizing the design.",
            ]
            return "\n".join(lines)

        if stage == "plan":
            lines = common + [
                f"Read: {brief}",
                f"Read: {architecture}",
                f"Replace this JSON file with 3-10 minimal verifiable feature slices: {plan}",
                "At the root of the JSON, also define test_strategy and verification_commands.",
                "Choose the smallest practical automated verification strategy for this stack.",
                "If this is a Python project, require a project-local conda env at ./.conda.",
                "For Python verification, use 'conda run -p ./.conda python -m unittest discover -s tests' unless another command is clearly better.",
                "For non-Python projects, keep all dependency installation and tooling local to the repository and avoid global installs.",
                self._plan_language_instruction(),
                "Each task must contain task_id, title, description, acceptance, status, commit_message.",
                "Keep tasks small enough to implement and verify independently.",
                "Final response: 3 short bullets summarizing the plan.",
            ]
            return "\n".join(lines)

        raise RuntimeError(f"Unsupported stage: {stage}")

    def _build_task_prompt(self, task: TaskSpec, stage: str) -> str:
        brief = docs_dir(self.project_root) / "project_brief.md"
        architecture = docs_dir(self.project_root) / "architecture.md"
        task_json = json.dumps(task.to_dict(), indent=2, ensure_ascii=True)
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
                "For any other stack, keep dependencies and tool state local to the repository and never rely on global installs.",
                "Do not modify .auto-agents state files except when explicitly requested.",
                "Final response: 3 short bullets describing what changed.",
            ]
            return "\n".join(lines)

        if stage == "review":
            lines = common + [
                "Review the current uncommitted changes for correctness, regressions, and missing tests.",
                "Return only the review result. Do not include any preamble, file path note, or tool narration.",
                "The first non-empty line must be exactly 'DECISION: pass' or 'DECISION: fail'.",
                self._review_language_instruction(),
                "After the first line, provide a short review summary.",
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
        last_reason = "task failed without a recorded reason"
        last_review = ""

        for attempt in range(1, max_attempts + 1):
            state.current_stage = "implement"
            if resume_existing and attempt == 1:
                result = None
            else:
                implement_prompt = self._build_task_prompt(task, "implement")
                if feedback:
                    implement_prompt = f"{implement_prompt}\n\nPrevious attempt issues:\n{feedback}\n"
                result = self._run_agent_with_retries(
                    state=state,
                    stage="implement",
                    stage_key=f"implement-{task.task_id}",
                    prompt=implement_prompt,
                )
                if not result.ok:
                    last_reason = result.stderr or result.summary or "implementation failed"
                    feedback = f"- Implementation command failed.\n- Details: {last_reason}"
                    continue

            gate_result = self._run_task_review_and_verify(state.run_id, task)
            if gate_result["ok"]:
                return gate_result

            last_reason = str(gate_result["reason"])
            last_review = str(gate_result["review"])
            feedback = (
                f"- Review or verification rejected the task.\n"
                f"- Reason: {last_reason}\n"
                f"- Review summary:\n{last_review}"
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
    ) -> AgentResult:
        attempts = self._max_attempts(stage)
        active_run_id = run_id or (state.run_id if state is not None else load_run_state(self.project_root).run_id)
        feedback = ""
        last_error = f"{stage_key} failed"

        for attempt in range(1, attempts + 1):
            attempt_prompt = prompt
            if feedback:
                attempt_prompt = f"{prompt}\n\nPrevious attempt issues:\n{feedback}\n"

            artifact_stage = stage_key if attempt == 1 else f"{stage_key}-attempt-{attempt}"
            output_path = self._stage_output_path(active_run_id, artifact_stage)
            write_run_prompt(self.project_root, active_run_id, artifact_stage, attempt_prompt)
            request = AgentRequest(
                stage=stage,
                effort=self.config.efforts.get(stage, "balanced"),
                prompt=attempt_prompt,
                cwd=self.project_root,
                output_path=output_path,
            )
            result = self.adapter.run(request)
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

            return result

        raise RuntimeError(f"{stage_key} exhausted retries: {last_error}")

    def _emit_agent_output(self, stage_key: str, result: AgentResult) -> None:
        if not self._print_agent_output:
            return

        sections = [f"[agent:{stage_key}] returncode={result.returncode} ok={str(result.ok).lower()}"]
        if result.summary:
            sections.append(result.summary.strip())
        if result.stderr:
            sections.append(f"[stderr]\n{result.stderr.strip()}")
        print("\n\n".join(sections), file=self.agent_output_stream, flush=True)

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

        allowed = {".gitignore", "README.md", "idea.md"}
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
