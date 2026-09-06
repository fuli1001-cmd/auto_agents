"""Presentation and diagnostic observation, deliberately outside health control.

The execution engine owns state. Reporters only observe successful persistence
and explicit operations. Neither the renderer nor its heartbeat writes run.log.
"""
from __future__ import annotations

import builtins
import contextvars
import hashlib
import functools
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Dict, Mapping, Optional, TextIO
from uuid import uuid4

from .diagnostic_output import OutputCapture, atomic_json, clean_payload, now, redact, plain_text
from .reporting_messages import user_text, REPAIR_PHASES


_CURRENT = contextvars.ContextVar("auto_agents_reporting", default=None)
_REPORTERS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_REGISTRY_LOCK = threading.RLock()
_TERMINAL = {"completed", "failed", "blocked", "waiting_user", "paused"}
_LABELS = {
    "clarify": ("需求", "Requirements"), "prototype": ("原型", "Prototype"),
    "design": ("设计", "Design"), "plan": ("计划", "Plan"),
    "provider_research": ("服务准备", "Provider preparation"),
    "implement": ("实现", "Implementation"), "visual_judge": ("视觉检查", "Visual checks"),
    "verify": ("验收", "Verification"), "readme": ("文档", "Documentation"),
    "conversing": ("讨论目标", "Discussing the goal"), "executing": ("执行", "Executing"),
    "verifying": ("验证", "Verifying"), "pending": ("等待执行", "Pending"),
    "in_progress": ("执行中", "In progress"), "done": ("已完成", "Done"),
    "completed": ("已完成", "Completed"), "failed": ("执行失败", "Failed"),
    "blocked": ("受阻", "Blocked"), "waiting_user": ("等待用户", "Waiting for input"),
    "paused": ("已暂停", "Paused"), "review": ("审查", "Review"),
    "resume": ("恢复", "Resuming"), "start": ("开始", "Starting"),
    "retry": ("修正", "Reworking"), "commit": ("集成", "Integrating"),
    "waiting_integration": ("等待集成", "Waiting for integration"),
    "current": ("当前阶段", "the current stage"),
    "run": ("准备任务", "Preparing the run"),
    "sync_agent_instructions": ("同步项目指令", "Synchronizing project instructions"),
    "audit_requirements": ("需求审计", "Requirements audit"),
    "provider_resolve": ("恢复服务准备", "Recovering provider preparation"),
    "answer": ("处理用户输入", "Processing user input"),
    "approve": ("确认方案", "Approving the proposal"),
    "reject": ("记录修改意见", "Recording requested changes"),
}
_MESSAGES = {
    "stage.started": ("进入{stage}阶段", "Starting {stage}"),
    "stage.completed": ("{stage}阶段已完成", "{stage} completed"),
    "stage.skipped": ("已跳过{stage}阶段", "{stage} skipped"),
    "stage.rewind": ("从{source}返回{stage}，受影响阶段需要重新验证", "Returning from {source} to {stage}; affected stages need revalidation"),
    "plan.ready": ("当前计划共 {total} 项任务，已完成 {done} 项", "Current plan: {total} tasks, {done} completed"),
    "plan.changed": ("计划调整：{before} → {total} 项；已完成 {done} 项", "Plan adjusted: {before} → {total} tasks; {done} completed"),
    "task.started": ("「{title}」：{action}，第 {attempt} 次尝试", "{title}: {action}, attempt {attempt}"),
    "task.completed": ("已完成「{title}」", "Completed: {title}"),
    "task.blocked": ("「{title}」暂时受阻，正在评估恢复方式", "{title} is blocked; evaluating recovery"),
    "task.result.pass": ("「{title}」{action}通过", "{title}: {action} passed"),
    "task.result.fail": ("「{title}」{action}未通过，正在评估下一步", "{title}: {action} did not pass; evaluating next steps"),
    "run.resumed": ("已恢复执行，当前已完成 {done}/{total} 项任务", "Resuming with {done}/{total} tasks completed"),
    "status": ("当前状态：{status}", "Status: {status}"),
    "heartbeat": ("仍在{stage}，已用时 {elapsed}", "Still in {stage}; elapsed {elapsed}"),
    "diagnostics": ("诊断记录：{path}", "Diagnostics: {path}"),
    "capture.failed": ("诊断记录不完整：{error}", "Diagnostic recording is incomplete: {error}"),
    "repair.phase": ("正在修复 auto_agents：{phase}", "Repairing auto_agents: {phase}"),
    "repair.checks": ("修复验证：{suite}，已执行 {completed}/{total} 组检查", "Repair validation: {suite}, {completed}/{total} check groups executed"),
    "verification.started": ("正在验证：{context}", "Verifying: {context}"),
    "verification.finished": ("验证检查已执行 {completed}/{total} 项，通过 {passed}，失败 {failed}，取消 {cancelled}", "Checks executed: {completed}/{total}; passed {passed}, failed {failed}, cancelled {cancelled}"),
    "invocation.stopped": ("本次执行已结束，仍有待办任务", "This invocation has ended; work remains"),
    "release.pending": ("前台流程已完成；后台完整验证尚未完成", "Foreground workflow completed; background release verification is still pending"),
    "provider.recovering": ("服务调用暂未成功，正在切换恢复方式", "The provider call did not succeed; trying another recovery route"),
    "stage.retry": ("正在重试{stage}，第 {attempt} 次尝试", "Retrying {stage}, attempt {attempt}"),
    "clock": ("本次执行时间：{date}", "Invocation time: {date}"),
    "verify.passed": ("最终验收通过", "Final verification passed"),
    "verify.failed": ("最终验收未通过；下一步：{route}", "Final verification did not pass; next: {route}"),
    "repair.eligible": ("诊断已完成，将启动 auto_agents 自动修复", "Diagnosis completed; automatic auto_agents repair will start"),
    "repair.not_eligible": ("诊断已完成，本次不会启动自动修复；详情已保存", "Diagnosis completed; automatic repair will not start for this issue; details are saved"),
    "repair.resume_pending": ("修复已通过验证，正在准备恢复原任务", "The repair passed validation; preparing to resume the original task"),
    "diagnosis.unavailable": ("诊断服务暂不可用，详情已保存", "The diagnosis provider is unavailable; details are saved"),
    "diagnosis.review_incomplete": ("根因调查已完成，复核未完成；调查报告已保留，本次不启动自动修复", "Investigation completed; independent review is incomplete. Evidence is preserved; automatic repair will not start"),
    "command.completed": ("本次操作已完成", "This operation is complete"),
    "command.failed": ("本次操作已停止，诊断记录已保存", "This operation stopped; diagnostics are saved"),
}


def _label(key: str, language: str) -> str:
    pair = _LABELS.get(key)
    return pair[0 if language == "zh" else 1] if pair else key


def _elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass
class ProgressSnapshot:
    subject: str = ""
    kind: str = "run"
    goal: str = ""
    stage: str = ""
    status: str = "pending"
    plan_id: str = ""
    tasks: Dict[str, dict] = field(default_factory=dict)
    stages: Dict[str, str] = field(default_factory=dict)
    repair: str = ""
    checks: Dict[str, int] = field(default_factory=dict)

    @property
    def done(self) -> int:
        return sum(task.get("status") == "done" for task in self.tasks.values())

    @classmethod
    def from_run(cls, state: object) -> "ProgressSnapshot":
        from .models import STAGE_ORDER
        tasks = {}
        definitions = []
        for task in state.tasks:
            tasks[task.task_id] = {"title": task.title, "status": task.status}
            definitions.append({
                key: getattr(task, key, None) for key in (
                    "task_id", "title", "description", "acceptance", "requirement_ids",
                    "depends_on", "parent_task_id", "task_origin", "scope_boundaries",
                )
            })
        plan_id = hashlib.sha256(json.dumps(sorted(definitions, key=lambda x: x["task_id"]),
                                            sort_keys=True).encode()).hexdigest() if tasks else ""
        stages = {}
        for stage in STAGE_ORDER:
            summary = str(state.stage_summaries.get(stage, ""))
            if not summary:
                stages[stage] = "pending"
            elif summary.lower().startswith(("skipped:", "skip:")):
                stages[stage] = "skipped"
            elif re.search(r"(?mi)^Result:\s*fail\s*$", summary):
                stages[stage] = "pending"
            else:
                stages[stage] = "completed"
        # Task completion is authoritative even when an old implement summary remains.
        if tasks and any(task["status"] != "done" for task in tasks.values()):
            stages["implement"] = "pending"
        # Match the engine's explicit compatibility skip for older/downstream runs.
        prototype_index = STAGE_ORDER.index("prototype")
        current_index = STAGE_ORDER.index(state.current_stage) if state.current_stage in STAGE_ORDER else -1
        if not state.stage_summaries.get("prototype") and (
            state.workflow_version < 2
            or current_index > prototype_index
            or any(stage in state.stage_summaries for stage in STAGE_ORDER[prototype_index + 1:])
        ):
            stages["prototype"] = "skipped"
        context = getattr(state, "resume_context", {})
        return cls(subject=str(state.run_id), kind="run",
                   goal=str(context.get("goal", "")), stage=str(state.current_stage),
                   status=str(state.status), tasks=tasks, stages=stages, plan_id=plan_id)


class ConsolePresenter:
    def __init__(self, stream: TextIO, mode: str = "auto") -> None:
        self.stream = stream
        self.mode = mode
        self.raw_output = False
        self.focus: Optional[Reporter] = None
        self._live = None
        self._console = None
        self._live_unavailable = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._suspended = 0
        self._last_event = time.monotonic()
        self._last_heartbeat = self._last_event
        self._started = self._last_event
        self._closed = False
        self.heartbeat_enabled = False
        self.clock_announced = False
        self.external_owner = False

    def _ensure_started(self) -> None:
        if self._thread is not None or self._closed:
            return
        try:
            interactive = bool(self.stream.isatty()) and os.environ.get("TERM", "") != "dumb"
            if self.mode == "auto" and interactive and not self.raw_output and not self._live_unavailable:
                from rich.console import Console
                from rich.live import Live
                self._console = Console(file=self.stream, markup=False, highlight=False)
                self._live = Live("", console=self._console, auto_refresh=False,
                                  redirect_stdout=False, redirect_stderr=False,
                                  screen=False, transient=True)
                self._live.start(refresh=False)
        except Exception:
            self._stop_live()
            self._live_unavailable = True
            self._console = None
        if self._live is None and not self.heartbeat_enabled:
            return
        self._thread = threading.Thread(target=self._refresh_loop, name="auto-agents-display", daemon=True)
        self._thread.start()

    def configure(self, mode: str, raw_output: bool) -> None:
        with self._lock:
            self.mode, self.raw_output = mode, raw_output
            if self._live is not None and (mode != "auto" or raw_output):
                self._stop_live()

    def _stop_live(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass

    def show(self, reporter: "Reporter", message: str, *, timestamp: str = "", debug: bool = False) -> None:
        if debug and self.mode != "debug":
            return
        with self._lock:
            if self._closed:
                return
            self._ensure_started()
            self._last_event = time.monotonic()
            try:
                when = datetime.fromisoformat(timestamp) if timestamp else datetime.now().astimezone()
                prefix = when.astimezone().strftime("[%H:%M:%S] ")
                text = "\n".join(prefix + line for line in redact(message).splitlines())
                if not text:
                    return
                if self._live is not None and not self._suspended:
                    from rich.text import Text
                    self._console.print(Text(text))
                else:
                    self.stream.write(text + "\n")
                    self.stream.flush()
            except Exception:
                self._stop_live()

    def _frame(self, reporter: "Reporter") -> str:
        snapshot = reporter.snapshot
        zh = reporter.language == "zh"
        stamp = datetime.now().astimezone().strftime("[%H:%M:%S] ")
        duration = _elapsed(time.monotonic() - self._started)
        goal = snapshot.goal or reporter.project_name
        label = ("目标" if zh else "Goal") if snapshot.goal else ("项目" if zh else "Project")
        lines = [f"{label}: {goal[:100]}  {duration}"]
        if snapshot.stages:
            symbols = {"completed": "✓", "skipped": "—", "pending": "○", "invalidated": "↺"}
            labels = [
                f"{_label(stage, reporter.language)} "
                + ("●" if stage == snapshot.stage and snapshot.status not in _TERMINAL else symbols.get(status, "○"))
                for stage, status in snapshot.stages.items()
            ]
            lines.append("  ".join(labels))
        else:
            lines.append(_label(snapshot.stage or snapshot.status, reporter.language))
        if snapshot.tasks:
            done, total = snapshot.done, len(snapshot.tasks)
            bars = 20 * done // total
            lines.append(f"{'当前计划' if zh else 'Current plan'} [{'█' * bars}{'░' * (20 - bars)}] {done}/{total}")
            blocked = sum(task["status"] == "blocked" for task in snapshot.tasks.values())
            active_count = sum(task_id in snapshot.tasks and snapshot.tasks[task_id]["status"] not in {"done", "blocked"}
                               for task_id in reporter.active_tasks)
            pending = max(0, total - done - blocked - active_count)
            lines.append(
                f"{'处理中' if zh else 'Active'} {active_count}  "
                f"{'待开始' if zh else 'Pending'} {pending}  {'受阻' if zh else 'Blocked'} {blocked}"
            )
        if snapshot.repair:
            lines.append(snapshot.repair)
        if snapshot.checks:
            c = snapshot.checks
            lines.append(f"{'检查' if zh else 'Checks'} {c.get('completed', 0)}/{c.get('total', 0)}"
                         f"  {'失败' if zh else 'failed'} {c.get('failed', 0)}")
        active = list(reporter.active_tasks.items())
        for task_id, operation in active[:3]:
            title = snapshot.tasks.get(task_id, {}).get("title", task_id)
            lines.append(f"  {title[:70]}: {_label(operation, reporter.language)}")
        if len(active) > 3:
            lines.append(f"  +{len(active) - 3} {'项执行中' if zh else 'active tasks'}")
        lines.append(_label(snapshot.status, reporter.language))
        return "\n".join(stamp + line for line in lines)

    def _refresh_loop(self) -> None:
        while not self._stop.wait(0.5):
            reporter = self.focus
            if reporter is None or self._suspended or self.external_owner:
                continue
            heartbeat = False
            with self._lock:
                if self._live is not None:
                    try:
                        from rich.text import Text
                        self._live.update(Text(self._frame(reporter)), refresh=True)
                    except Exception:
                        self._stop_live()
                elif (
                    (reporter.snapshot.status not in _TERMINAL or reporter.snapshot.repair)
                    and time.monotonic() - max(self._last_event, self._last_heartbeat) >= 60
                ):
                    self._last_heartbeat = time.monotonic()
                    heartbeat = True
            if heartbeat:
                # Never acquire a reporter lock while holding the presentation lock.
                reporter.emit("heartbeat", stage=_label(reporter.snapshot.stage, reporter.language),
                              elapsed=_elapsed(self._last_heartbeat - self._started))

    @contextmanager
    def input(self):
        with self._lock:
            self._suspended += 1
            if self._live is not None:
                try:
                    self._live.update("", refresh=True)
                    self._console.show_cursor(True)
                except Exception:
                    self._stop_live()
        try:
            yield
        finally:
            with self._lock:
                self._suspended -= 1
                if self._suspended == 0 and self._live is not None:
                    try:
                        self._console.show_cursor(False)
                    except Exception:
                        self._stop_live()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        with self._lock:
            self._stop_live()
            self._closed = True

    def handoff(self) -> None:
        with self._lock:
            self.external_owner = True
            self._stop_live()


class Reporter:
    def __init__(self, project_root: Path, stream: TextIO, *, language: str = "en",
                 presenter: Optional[ConsolePresenter] = None, runtime=None,
                 lane_task: str = "", parent: Optional["Reporter"] = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.project_name = self.project_root.name
        self.language = language if language in {"en", "zh"} else "en"
        self.presenter = presenter or ConsolePresenter(stream)
        self.runtime = runtime
        self.lane_task = lane_task
        self.parent = parent
        self.root: Optional[Path] = None
        self.snapshot = ProgressSnapshot()
        self.active_tasks: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._sequence = 0
        self._invocation = uuid4().hex[:12]
        self._last_snapshot = ""
        self._persisted_stage = ""
        self._announced_plan = ""
        self._invalidated_stages: set[str] = set()
        self._lane_tokens: Dict[str, str] = {}
        self._lane_token = uuid4().hex
        self._artifacts: Dict[str, dict] = {}
        self._capture_error = False
        self._closed = False
        self._handed_off = False
        self._captures: list[OutputCapture] = []
        self._checks: Dict[str, Dict[str, str]] = {}
        self._loggers: list[logging.Logger] = []
        with _REGISTRY_LOCK:
            _REPORTERS[str(self.project_root)] = self
        if runtime is not None:
            runtime.reporters.append(self)
        if parent is None:
            self.presenter.focus = self

    def child(self, project_root: Path, task_id: str) -> "Reporter":
        self.ensure_bound()
        child = Reporter(project_root, self.presenter.stream, language=self.language,
                         presenter=self.presenter, runtime=self.runtime, lane_task=task_id, parent=self)
        child.root = self.root
        child.snapshot = replace(self.snapshot, checks={}, stages=dict(self.snapshot.stages))
        self._lane_tokens[task_id] = child._lane_token
        return child

    def _current_lane(self) -> bool:
        return self.parent is None or (
            self.root == self.parent.root
            and self.parent._lane_tokens.get(self.lane_task) == self._lane_token
        )

    @property
    def options(self) -> dict:
        return {"log_mode": self.presenter.mode, "print_agent_output": self.presenter.raw_output}

    def bind(self, kind: str, subject: str, *, goal: str = "", workflow_id: str = "") -> None:
        if self.lane_task:
            return
        if not subject:
            return
        if self.snapshot.subject == subject and self.snapshot.kind == kind and self.root is not None:
            if goal:
                self.snapshot.goal = goal
            self.presenter.focus = self
            return
        with self._lock:
            root = (self.project_root / ".auto-agents" / "runs" / subject
                    if kind == "run" else
                    self.project_root / ".auto-agents" / "state" / "sessions" / subject / "logs")
            previous_root = self.root
            previous_snapshot = self.snapshot
            self.root = root
            self.snapshot = ProgressSnapshot(subject=subject, kind=kind, goal=goal or previous_snapshot.goal)
            if subject.startswith("_commands/") and self.runtime is not None:
                self.snapshot.stage = str(getattr(self.runtime.args, "command", "")).replace("-", "_")
                self.snapshot.status = "running"
            self._last_snapshot = ""
            self._persisted_stage = ""
            self._announced_plan = ""
            self.active_tasks.clear()
            previous = _read_json(root / "diagnostics.json")
            artifacts = previous.get("artifacts", {})
            self._artifacts = {
                str(key): dict(value) for key, value in artifacts.items() if isinstance(value, dict)
            } if isinstance(artifacts, dict) else {}
            progress = previous.get("progress", {})
            invalidated = progress.get("invalidated_stages", []) if isinstance(progress, dict) else []
            self._invalidated_stages = {
                value for value in invalidated if isinstance(value, str)
            } if isinstance(invalidated, list) else set()
            if previous_root and previous_root != root:
                self._artifacts["previous_subject"] = {"path": str(previous_root / "diagnostics.json"), "kind": "index"}
            if self.runtime is not None and not subject.startswith("_commands/"):
                self.runtime.resolve_options(previous.get("presentation", {}))
            self.presenter.focus = self
            self._index()
            from .logging_utils import attach_run_file_logger
            for logger in self._loggers:
                attach_run_file_logger(logger, root / "run.log")
            self.event("subject.bound", {"workflow_id": workflow_id, "previous_subject": previous_snapshot.subject})
            if not self.presenter.clock_announced:
                self.presenter.clock_announced = True
                self.emit("clock", date=datetime.now().astimezone().isoformat(timespec="seconds"))
            self.emit("diagnostics", path=str(root / "diagnostics.json"))

    @contextmanager
    def preserve_subject(self):
        previous = (self.root, self.snapshot, self._artifacts, self.active_tasks, self._last_snapshot,
                    self._announced_plan, set(self._invalidated_stages), self._persisted_stage)
        try:
            yield
        finally:
            root, snapshot, artifacts, tasks, last, announced, invalidated, persisted_stage = previous
            if root is not None and not snapshot.subject.startswith("_commands/") and root != self.root:
                child_root = self.root
                self._index()
                (self.root, self.snapshot, self._artifacts, self.active_tasks, self._last_snapshot,
                 self._announced_plan, self._invalidated_stages, self._persisted_stage) = previous
                if child_root:
                    self._artifacts[str(child_root)] = {"path": str(child_root / "diagnostics.json"), "kind": "index"}
                self._index()
                from .logging_utils import attach_run_file_logger
                for logger in self._loggers:
                    attach_run_file_logger(logger, root / "run.log")
                self.presenter.focus = self
                self.event("subject.returned", {"child": str(child_root)})

    def ensure_bound(self) -> None:
        if self.root is None:
            self.bind("run", "_commands/" + self._invocation)

    def _index(self, *, force: bool = False) -> None:
        if self.parent is not None:
            self.parent._index()
            return
        if self.root is None or self._handed_off or (self._closed and not force):
            return
        try:
            atomic_json(self.root / "diagnostics.json", {
                "schema_version": 1, "subject_id": self.snapshot.subject,
                "kind": self.snapshot.kind, "presentation": self.options,
                "updated_at": now(), "incomplete": self._capture_error,
                "progress": {"invalidated_stages": sorted(self._invalidated_stages)},
                "logs": {"user": "user.log", "detail": "run.log", "events": "events.jsonl"},
                "state_path": str(
                    self.project_root / ".auto-agents/state/run_state.json"
                    if self.snapshot.kind == "run" else
                    self.project_root / ".auto-agents/state/sessions" / self.snapshot.subject / "session_state.json"
                ),
                "artifacts": self._artifacts,
            })
        except Exception as error:
            self.capture_failed(error)

    def capture_failed(self, error: Exception) -> None:
        if self._capture_error:
            return
        self._capture_error = True
        self.presenter.show(self, self.message("capture.failed", error=redact(str(error))))

    def register(self, path: Path, metadata: Mapping[str, object]) -> None:
        with self._lock:
            if self._closed:
                return
            key = str(path)
            if key in self._artifacts and path.name != "attempt.json":
                return
            self._artifacts[key] = {"path": str(path), **{
                key: metadata[key] for key in (
                    "attempt_id", "task_id", "stage", "kind", "status", "returncode",
                    "incident_id", "experiment_id", "candidate_id",
                ) if key in metadata
            }}
            self._index()

    def capture(self, *, stage: str = "", attempt_id: str = "", task_id: str = "",
                **metadata: object) -> OutputCapture:
        owner = self.parent or self
        with owner._lock:
            if owner._closed or self._closed:
                return OutputCapture(owner.project_root, {}, register=owner.register,
                                     failed=owner.capture_failed, enabled=False)
            owner.ensure_bound()
            identifier = uuid4().hex[:16]
            capture = OutputCapture(
                owner.root / "diagnostic-output" / identifier,
                {"stage": stage or self.snapshot.stage, "attempt_id": attempt_id or identifier,
                 "task_id": task_id or self.lane_task or (
                     next(iter(self.active_tasks)) if len(self.active_tasks) == 1 else ""
                 ), "subject_id": self.snapshot.subject, **metadata},
                register=owner.register, failed=owner.capture_failed,
            )
            owner._captures.append(capture)
            return capture

    def message(self, kind: str, **values: object) -> str:
        pair = _MESSAGES.get(kind)
        if not pair:
            return str(values.get("message", kind))
        return pair[0 if self.language == "zh" else 1].format(**values)

    def event(self, kind: str, data: Mapping[str, object], *, audience: str = "debug",
              message: str = "", level: str = "INFO") -> None:
        owner = self.parent or self
        if owner._closed or self._closed:
            return
        owner.ensure_bound()
        current_lane = self._current_lane()
        output_root = owner.root if current_lane or self.root is None else self.root
        timestamp = now()
        message = plain_text(message)
        with owner._lock:
            if owner._closed or self._closed:
                return
            owner._sequence += 1
            record = clean_payload({
                "schema_version": 1, "timestamp": timestamp, "type": kind,
                "event_id": f"{owner._invocation}:{owner._sequence}",
                "subject_id": owner.snapshot.subject if current_lane else self.snapshot.subject,
                "subject_kind": owner.snapshot.kind if current_lane else self.snapshot.kind,
                "lane_id": self._lane_token if self.lane_task else "",
                "task_id": str(data.get("task_id") or self.lane_task),
                "stage_id": str(data.get("stage_id") or self.snapshot.stage),
                "audience": audience, "level": level,
                "message": message, "data": dict(data),
            })
            try:
                with (output_root / "events.jsonl").open("a", encoding="utf-8") as target:
                    target.write(json.dumps(record, ensure_ascii=False) + "\n")
                if audience == "user" and message and current_lane:
                    with (owner.root / "user.log").open("a", encoding="utf-8") as target:
                        for line in redact(message).splitlines():
                            target.write(f"{timestamp} {line}\n")
            except Exception as error:
                owner.capture_failed(error)
        if message and current_lane:
            owner.presenter.show(owner, message, timestamp=timestamp, debug=audience != "user")

    def emit(self, kind: str, **data: object) -> None:
        self.event(kind, data, audience="user", message=self.message(kind, **data))

    def text(self, message: str, *, diagnostic: bool = False) -> None:
        self.event("diagnostic.message" if diagnostic else "user.message", {},
                   audience="debug" if diagnostic else "user",
                   message=message if diagnostic else user_text(message, self.language))

    def diagnostic(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        data = {"logger": record.name, "file": record.pathname, "line": record.lineno}
        if record.exc_info:
            data["traceback"] = "".join(traceback.format_exception(*record.exc_info))
        self.event("diagnostic.message", data, message=message, level=record.levelname)
        if getattr(record, "audience", "") == "user":
            self.text(message)

    def exception(self, error: BaseException) -> None:
        if getattr(self, "_last_exception", None) is error:
            return
        self._last_exception = error
        self.event("diagnostic.exception", {
            "error_type": type(error).__name__, "error": str(error),
            "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        }, message=str(error), level="ERROR")
        self.emit("diagnostics", path=str((self.parent or self).root / "diagnostics.json"))

    def stage(self, stage: str) -> None:
        if self.lane_task:
            self.task(self.lane_task, self.lane_task, stage, 1)
            return
        self.snapshot.stage = stage
        self.snapshot.status = "running"
        self.snapshot.repair = ""
        self.emit("stage.started", stage=_label(stage, self.language))

    def rewind(self, stage: str, *, source: str = "", reason: str = "") -> None:
        source = source or self.snapshot.stage or "current"
        found = False
        for key in self.snapshot.stages:
            found = found or key == stage
            if found:
                self._invalidated_stages.add(key)
                self.snapshot.stages[key] = "invalidated"
        self.active_tasks.clear()
        self.snapshot.stage = stage
        message = self.message("stage.rewind", source=_label(source, self.language), stage=_label(stage, self.language))
        if reason:
            message += ": " + " ".join(reason.split())[:160]
        self.event("stage.rewind", {"source": source, "target": stage, "reason": reason},
                   audience="user", message=message)

    def task(self, task_id: str, title: str, action: str, attempt: int) -> None:
        if not self._current_lane():
            return
        owner = self.parent or self
        owner.active_tasks[task_id] = action
        if action in {"implement", "verify"}:
            self.snapshot.checks = {}
        self.event("task.started", {"task_id": task_id, "action": action, "attempt": attempt},
                   audience="user", message=self.message("task.started", title=title,
                       action=_label(action, self.language), attempt=attempt))

    def task_result(self, task_id: str, title: str, action: str, decision: str) -> None:
        passed = decision.lower() in {"pass", "passed", "approve", "approved", "accept", "accepted"}
        self.event("task.result", {"task_id": task_id, "action": action, "decision": decision},
                   audience="user", message=self.message("task.result.pass" if passed else "task.result.fail",
                                                        title=title, action=_label(action, self.language)))

    def observe_run(self, state: object) -> None:
        if self._handed_off:
            return
        if self.lane_task:
            if not self._current_lane():
                return
            for task in state.tasks:
                if task.task_id == self.lane_task and task.status == "done":
                    self.parent.active_tasks[self.lane_task] = "waiting_integration"
            return
        if self.snapshot.kind != "run" or self.snapshot.subject != str(state.run_id):
            return
        current = ProgressSnapshot.from_run(state)
        if state.current_stage == self._persisted_stage and self.snapshot.stage:
            current.stage = self.snapshot.stage
        self._persisted_stage = state.current_stage
        current.goal = self.snapshot.goal or current.goal
        current.repair = self.snapshot.repair
        current.checks = self.snapshot.checks
        encoded = json.dumps(current.__dict__, sort_keys=True)
        if encoded == self._last_snapshot:
            return
        previous = self.snapshot
        initialized = bool(self._last_snapshot)
        for stage, status in list(current.stages.items()):
            if status == "pending" and (
                stage in self._invalidated_stages
                or initialized and previous.stages.get(stage) == "completed"
            ):
                self._invalidated_stages.add(stage)
                current.stages[stage] = "invalidated"
            elif status in {"completed", "skipped"}:
                self._invalidated_stages.discard(stage)
        self.snapshot = current
        self._last_snapshot = encoded
        self.event("state.snapshot", current.__dict__)
        if previous.plan_id != current.plan_id and current.plan_id and self._announced_plan != current.plan_id:
            self.emit("plan.changed" if previous.plan_id else "plan.ready",
                      before=len(previous.tasks), total=len(current.tasks), done=current.done)
        self._announced_plan = current.plan_id
        if initialized:
            for task_id, task in current.tasks.items():
                if task["status"] == "done" and previous.tasks.get(task_id, {}).get("status") != "done":
                    self.active_tasks.pop(task_id, None)
                    self.emit("task.completed", title=task["title"], task_id=task_id)
            for stage, status in current.stages.items():
                if status in {"completed", "skipped"} and previous.stages.get(stage) != status:
                    self.emit("stage.skipped" if status == "skipped" else "stage.completed",
                              stage=_label(stage, self.language), stage_id=stage)
            if current.status in _TERMINAL and previous.status != current.status:
                self.emit("status", status=_label(current.status, self.language))
        self.active_tasks = {
            task_id: action for task_id, action in self.active_tasks.items()
            if task_id in current.tasks and current.tasks[task_id]["status"] != "done"
        }
        self._index()

    def observe_session(self, state: object) -> None:
        if self.snapshot.subject != str(state.session_id):
            return
        previous_status = self.snapshot.status
        self.snapshot.goal = str(state.goal)
        self.snapshot.kind = str(state.mode)
        self.snapshot.stage = str(state.status)
        self.snapshot.status = str(state.status)
        if previous_status != self.snapshot.status:
            self.emit("status", status=_label(self.snapshot.status, self.language))
        self.event("session.snapshot", {
            "status": state.status, "attempt": state.current_attempt, "goal": state.goal,
        })

    def repair(self, phase: str, **data: object) -> None:
        pair = REPAIR_PHASES.get(phase, ("处理当前修复步骤", "working on the current repair step"))
        rendered = pair[0 if self.language == "zh" else 1]
        self.snapshot.repair = self.message("repair.phase", phase=rendered)
        self.emit("repair.phase", phase=rendered, phase_id=phase, **data)

    def plan(self, tasks: list) -> None:
        from .models import RunState
        accepted = ProgressSnapshot.from_run(RunState(self.snapshot.subject, tasks=tasks))
        if accepted.plan_id and accepted.plan_id != self._announced_plan:
            self.emit("plan.changed" if self._announced_plan else "plan.ready",
                      before=len(self.snapshot.tasks), total=len(tasks), done=accepted.done)
            self._announced_plan = accepted.plan_id

    def handoff(self) -> None:
        self._index()
        self._handed_off = True
        self.presenter.handoff()

    def cancel_handoff(self) -> None:
        self._handed_off = False
        self.presenter.external_owner = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for capture in self._captures:
            capture.finish(status="interrupted" if not capture._finished else "finished")
        self._index(force=True)
        for logger in self._loggers:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
        if self.runtime is None and self.parent is None:
            self.presenter.close()

class GateObservation:
    """One selected logical check set; transport retries are not extra checks."""

    def __init__(self, progress, reporter: Reporter, total: int) -> None:
        self.progress = progress
        self.reporter = reporter
        self.context = str(getattr(progress, "context", "") or _label("verify", reporter.language))
        self.counts = {"total": total, "completed": 0, "passed": 0, "failed": 0, "cancelled": 0}
        self.reporter.snapshot.checks = dict(self.counts)
        if "baseline" in self.context:
            label = "验证基线" if reporter.language == "zh" else "baseline"
        elif reporter.lane_task:
            label = reporter.snapshot.tasks.get(reporter.lane_task, {}).get("title", reporter.lane_task)
        else:
            label = "当前任务" if reporter.language == "zh" else "current task"
        self.reporter.emit("verification.started", context=label)
        self.reporter.event("verification.selected", {"context": self.context, "total": total})

    def __call__(self, event: str, command: str, elapsed: float) -> None:
        if self.progress is not None:
            self.progress(event, command, elapsed)

    def result(self, result: object) -> None:
        if getattr(result, "termination_reason", "") == "cancelled":
            self.counts["cancelled"] += 1
        else:
            self.counts["completed"] += 1
            self.counts["passed" if result.ok else "failed"] += 1
        self.reporter.snapshot.checks = dict(self.counts)
        self.reporter.event("verification.result", {
            "context": self.context, "command": result.command, "ok": result.ok,
            "returncode": result.returncode, "termination_reason": result.termination_reason,
            "cached": getattr(result, "cached", False),
            "worker_id": getattr(result, "worker_id", ""), "job_id": getattr(result, "job_id", ""),
            **self.counts,
        })
        if getattr(result, "backend", "") == "lan-worker":
            capture = self.reporter.capture(kind="gate", backend="lan-worker",
                                           job_id=result.job_id, worker_id=result.worker_id)
            capture.start(result.command, {}, capture_mode="remote_result",
                          worker_full_output=f"diagnostic-output/{result.job_id}")
            capture("stdout", result.stdout)
            capture("stderr", result.stderr)
            capture.finish(returncode=result.returncode, termination_reason=result.termination_reason)

    def finish(self, result: object) -> None:
        # Reconcile all returned results, including one-command and cached paths.
        commands = result.commands
        self.counts.update(
            completed=sum(item.termination_reason != "cancelled" for item in commands),
            passed=sum(item.ok for item in commands),
            failed=sum(not item.ok and item.termination_reason != "cancelled" for item in commands),
            cancelled=sum(item.termination_reason == "cancelled" for item in commands),
        )
        self.reporter.snapshot.checks = dict(self.counts)
        self.reporter.emit("verification.finished", **self.counts)


def observe_gate_result(progress, result: object) -> None:
    if isinstance(progress, GateObservation):
        progress.result(result)


class ReportingRuntime:
    def __init__(self, args: object) -> None:
        self.args = args
        self.explicit_mode = getattr(args, "log_mode", None)
        self.explicit_raw = bool(getattr(args, "print_agent_output", False))
        self.presenter = ConsolePresenter(sys.stderr, self.explicit_mode or "auto")
        self.presenter.raw_output = self.explicit_raw
        self.presenter.heartbeat_enabled = True
        self.reporters: list[Reporter] = []
        self._resolved = False
        project = getattr(args, "project", None)
        if project:
            root = Path(project).expanduser().resolve() / ".auto-agents"
            session_id = str(getattr(args, "session", "") or "")
            workflow_id = str(getattr(args, "workflow", "") or "")
            if not workflow_id and getattr(args, "command", "") == "resume":
                workflow_id = str(_read_json(root / "state/workflows/active.json").get("workflow_id", ""))
            frame = _read_json(root / "state/workflows" / workflow_id / "workflow.json").get("root", {}) if workflow_id else {}
            if isinstance(frame, dict) and frame.get("kind") != "run":
                session_id = session_id or str(frame.get("native_id", ""))
            if session_id:
                index = root / "state/sessions" / session_id / "logs/diagnostics.json"
            elif getattr(args, "command", "") in {"fix", "collab", "provider-resolve"}:
                index = root / "no-selected-session"
            else:
                run_id = str(_read_json(root / "state/run_state.json").get("run_id", ""))
                index = root / "runs" / run_id / "diagnostics.json"
            saved = _read_json(index).get("presentation", {})
            self.resolve_options(saved, final=bool(saved))

    def resolve_options(self, saved: object, *, final: bool = True) -> None:
        if self._resolved:
            return
        saved = saved if isinstance(saved, dict) else {}
        mode = self.explicit_mode or str(saved.get("log_mode", "auto"))
        if mode not in {"auto", "plain", "debug"}:
            mode = "auto"
        raw = self.explicit_raw or saved.get("print_agent_output") is True
        self.presenter.configure(mode, raw)
        self.args.print_agent_output = raw
        self.args.log_mode = mode
        self._resolved = final

    def close(self) -> None:
        for reporter in self.reporters:
            reporter.close()
        self.presenter.close()

    def finish(self, code: int) -> None:
        reporter = self.presenter.focus
        if reporter is None or reporter._handed_off or not reporter.snapshot.subject.startswith("_commands/"):
            return
        reporter.snapshot.status = "completed" if code == 0 else "failed"
        reporter.emit("command.completed" if code == 0 else "command.failed")
        if code != 0:
            reporter.emit("diagnostics", path=str(reporter.root / "diagnostics.json"))


def get_reporter(project_root: Path, stream: TextIO, *, language: str = "en") -> Reporter:
    runtime = _CURRENT.get()
    return Reporter(project_root, stream, language=language, runtime=runtime,
                    presenter=runtime.presenter if runtime is not None else None)


def find_reporter(project_root: Optional[Path] = None) -> Optional[Reporter]:
    if project_root is not None:
        with _REGISTRY_LOCK:
            reporter = _REPORTERS.get(str(Path(project_root).resolve()))
            if reporter is not None and not reporter._closed:
                return reporter
    runtime = _CURRENT.get()
    return runtime.presenter.focus if runtime is not None else None


def observe_saved_run(project_root: Path, state: object) -> None:
    # Never bind or mutate a workflow in response to a persistence observation.
    try:
        with _REGISTRY_LOCK:
            reporter = _REPORTERS.get(str(Path(project_root).resolve()))
        if reporter is not None and not reporter._closed:
            reporter.observe_run(state)
    except Exception as error:
        if "reporter" in locals() and reporter is not None:
            reporter.capture_failed(error)


def reporting_scope(method):
    """Restore a parent's view when an in-process child workflow returns."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        owner = getattr(self, "orch", self)
        reporter = getattr(owner, "reporter", None)
        if not isinstance(reporter, Reporter):
            return method(self, *args, **kwargs)
        with reporter.preserve_subject():
            try:
                return method(self, *args, **kwargs)
            except BaseException as error:
                reporter.exception(error)
                raise
    return wrapped


@contextmanager
def reporting_command(args: object):
    runtime = ReportingRuntime(args)
    token = _CURRENT.set(runtime)
    try:
        yield runtime
    except BaseException as error:
        reporter = runtime.presenter.focus
        if reporter is None and getattr(args, "project", None):
            reporter = get_reporter(Path(args.project), sys.stderr)
        if reporter is not None:
            reporter.exception(error)
        raise
    finally:
        runtime.close()
        _CURRENT.reset(token)


def print_message(*values: object, **kwargs: object) -> None:
    """Module-local replacement for direct stderr prints; stdout stays untouched."""
    runtime = _CURRENT.get()
    error = sys.exc_info()[1]
    if error is not None and runtime is not None:
        reporter = find_reporter()
        if reporter is None and getattr(runtime.args, "project", None):
            reporter = get_reporter(Path(runtime.args.project), sys.stderr)
        if reporter is not None:
            reporter.exception(error)
    if kwargs.get("file") is sys.stderr:
        reporter = find_reporter()
        if reporter is not None:
            sep = str(kwargs.get("sep", " "))
            reporter.text(sep.join(str(value) for value in values))
            return
    builtins.print(*values, **kwargs)


def notice(kind: str, legacy: str, **data: object) -> None:
    reporter = find_reporter()
    if reporter is None:
        builtins.print(legacy, file=sys.stderr)
        return
    reporter.event("diagnostic.notice", data, message=legacy)
    reporter.emit(kind, **data)
