from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .adapters import CodexAdapter, MockAdapter, ShellAdapter
from .config import (
    bootstrap_project,
    docs_dir,
    load_project_config,
    load_run_state,
    load_stage_output,
    load_task_plan,
    review_path,
    run_artifact_paths,
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
    AgentRequest,
    ProjectConfig,
    RunState,
    STAGE_ORDER,
    TaskSpec,
)


class Orchestrator:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.config = load_project_config(self.project_root)
        self.adapter = self._build_adapter(self.config)

    @staticmethod
    def init_project(project_root: Path, name: str, provider_kind: str) -> Path:
        root = bootstrap_project(project_root, name, provider_kind)
        ensure_repo(root, auto_init=True)
        return root

    def approve(self, gate: str) -> RunState:
        state = load_run_state(self.project_root)
        if gate not in self.config.approvals.enabled:
            raise RuntimeError(f"Unknown approval gate: {gate}")
        if gate not in state.approved_gates:
            state.approved_gates.append(gate)
        if state.pending_approval == gate:
            state.pending_approval = ""
            state.status = "pending"
        save_run_state(self.project_root, state)
        return state

    def run(self, idea_file: Path, auto_approve: bool = False, max_tasks: Optional[int] = None) -> RunState:
        ensure_repo(self.project_root, auto_init=self.config.git.auto_init_repo)
        state = load_run_state(self.project_root)

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
            if stage == "implement":
                state = self._run_implementation_loop(state, max_tasks=max_tasks)
            elif stage == "verify":
                state = self._run_verify(state)
            else:
                state = self._run_agent_stage(stage, state, idea_file)

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

    def _build_adapter(self, config: ProjectConfig):
        if config.provider.kind == "codex":
            return CodexAdapter(config.provider)
        if config.provider.kind == "mock":
            return MockAdapter()
        return ShellAdapter(config.provider)

    def _run_agent_stage(self, stage: str, state: RunState, idea_file: Path) -> RunState:
        prompt = self._build_prompt(stage=stage, idea_file=idea_file)
        output_path = self._stage_output_path(state.run_id, stage)
        write_run_prompt(self.project_root, state.run_id, stage, prompt)
        request = AgentRequest(
            stage=stage,
            effort=self.config.efforts.get(stage, "balanced"),
            prompt=prompt,
            cwd=self.project_root,
            output_path=output_path,
        )
        result = self.adapter.run(request)
        if not result.ok:
            raise RuntimeError(
                f"Stage {stage} failed with code {result.returncode}: {result.stderr or result.summary}"
            )
        state.current_stage = stage
        state.stage_summaries[stage] = result.summary.strip()
        if stage == "plan":
            state.tasks = self._load_tasks_from_plan()
        return state

    def _run_implementation_loop(self, state: RunState, max_tasks: Optional[int]) -> RunState:
        tasks = state.tasks or self._load_tasks_from_plan()
        state.tasks = tasks
        self._commit_planning_baseline_if_needed()

        processed = 0
        for task in tasks:
            if task.status == "done":
                continue
            if max_tasks is not None and processed >= max_tasks:
                break

            if self.config.gates.require_clean_git_before_task:
                require_clean_tree(self.project_root)

            state.current_stage = "implement"
            implement_prompt = self._build_task_prompt(task, "implement")
            implement_output = self._stage_output_path(state.run_id, f"implement-{task.task_id}")
            request = AgentRequest(
                stage="implement",
                effort=self.config.efforts.get("implement", "balanced"),
                prompt=implement_prompt,
                cwd=self.project_root,
                output_path=implement_output,
            )
            write_run_prompt(self.project_root, state.run_id, f"implement-{task.task_id}", implement_prompt)
            implement_result = self.adapter.run(request)
            if not implement_result.ok:
                raise RuntimeError(
                    f"Task {task.task_id} implementation failed: {implement_result.stderr or implement_result.summary}"
                )

            gate_result = self._run_task_review_and_verify(state.run_id, task)
            if not gate_result["ok"]:
                task.status = "blocked"
                task.review_summary = gate_result["review"]
                self._persist_tasks(tasks)
                raise RuntimeError(f"Task {task.task_id} failed gates: {gate_result['reason']}")

            task.status = "done"
            task.review_summary = gate_result["review"]
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
        return state

    def _run_task_review_and_verify(self, run_id: str, task: TaskSpec) -> Dict[str, object]:
        review_prompt = self._build_task_prompt(task, "review")
        output_path = self._stage_output_path(run_id, f"review-{task.task_id}")
        request = AgentRequest(
            stage="review",
            effort=self.config.efforts.get("review", "deep"),
            prompt=review_prompt,
            cwd=self.project_root,
            output_path=output_path,
        )
        write_run_prompt(self.project_root, run_id, f"review-{task.task_id}", review_prompt)
        review_result = self.adapter.run(request)
        if not review_result.ok:
            return {"ok": False, "review": review_result.stderr or review_result.summary, "reason": "review failed"}

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
        payload = []
        for task in tasks:
            item = task.to_dict()
            item.pop("commit_sha", None)
            payload.append(item)
        save_task_plan(
            self.project_root,
            {"tasks": payload},
        )

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
        ]

        if stage == "clarify":
            lines = common + [
                f"Read the idea from: {idea_file}",
                f"Update this file in place: {brief}",
                "Keep the brief compact and scoped to the MVP.",
                "Final response: 3 short bullets summarizing the clarified scope.",
            ]
            return "\n".join(lines)

        if stage == "design":
            lines = common + [
                f"Read the current project brief: {brief}",
                f"Update this file in place: {architecture}",
                "Record only top-level architecture decisions and major risks.",
                "Final response: 3 short bullets summarizing the design.",
            ]
            return "\n".join(lines)

        if stage == "plan":
            lines = common + [
                f"Read: {brief}",
                f"Read: {architecture}",
                f"Replace this JSON file with 3-10 minimal verifiable feature slices: {plan}",
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
                "Do not modify .auto-agents state files except when explicitly requested.",
                "Final response: 3 short bullets describing what changed.",
            ]
            return "\n".join(lines)

        if stage == "review":
            lines = common + [
                "Review the current uncommitted changes for correctness, regressions, and missing tests.",
                f"Write the review summary to: {review_path(self.project_root)}",
                "Return the first line exactly as 'DECISION: pass' or 'DECISION: fail'.",
                "After the first line, provide a short review summary.",
            ]
            return "\n".join(lines)

        raise RuntimeError(f"Unsupported task stage: {stage}")

    @staticmethod
    def _parse_review_decision(response: str) -> Tuple[str, str]:
        lines = [line.strip() for line in response.splitlines() if line.strip()]
        if not lines:
            return "fail", "Empty review response"
        first = lines[0].lower()
        if first == "decision: pass":
            return "pass", "\n".join(lines[1:]) or "Review passed."
        if first == "decision: fail":
            return "fail", "\n".join(lines[1:]) or "Review failed."
        return "fail", response.strip()

    def status(self) -> Dict[str, object]:
        state = load_run_state(self.project_root)
        return {
            "run_id": state.run_id,
            "status": state.status,
            "current_stage": state.current_stage,
            "pending_approval": state.pending_approval,
            "approved_gates": state.approved_gates,
            "tasks": [task.to_dict() for task in state.tasks],
            "changed_files": changed_files(self.project_root) if is_repo(self.project_root) else "",
        }

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

    def _commit_planning_baseline_if_needed(self) -> None:
        if not changed_files(self.project_root):
            return
        commit_all(self.project_root, "docs(project): capture planning baseline")
