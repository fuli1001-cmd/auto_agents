"""Ephemeral execution observations for the console, never health evidence."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from uuid import uuid4

from .diagnostic_output import plain_text, redact


def text(value: object) -> str:
    return " ".join(plain_text(redact(value)).split())


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def check_name(command: str, index: int, zh: bool) -> str:
    # Only expose a recognizable relative test path, never arbitrary shell args.
    path = re.search(r"(?:^|\s)((?:tests?|src)/[\w./-]+(?:\.(?:py|tsx?|jsx?)))(?=\s|::|$)", command)
    prefix = f"检查 {index}" if zh else f"Check {index}"
    return prefix + (f" ({path.group(1)})" if path else "")


@dataclass
class Activity:
    title: str
    action: str
    attempt: int
    token: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class CheckSet:
    task_id: str = ""
    counts: dict = field(default_factory=dict)
    active: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    finished: bool = False
    interrupted: bool = False
    baseline: bool = False


@dataclass
class ExecutionDisplay:
    epoch: str = field(default_factory=lambda: uuid4().hex)
    stage_active: bool = False
    activities: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    calls: dict = field(default_factory=dict)
    buffered: dict = field(default_factory=dict)
    last_output: float | None = None

    def clear_task(self, task_id: str) -> None:
        self.activities.pop(task_id, None)
        self.checks = {k: v for k, v in self.checks.items() if v.task_id != task_id}
        self.calls = {k: v for k, v in self.calls.items() if v[0] != task_id}
        self.buffered = {k: v for k, v in self.buffered.items() if v != task_id}

    def lines(self, snapshot, active_tasks: dict, language: str, label) -> list[str]:
        zh = language == "zh"
        stage = label(snapshot.stage or snapshot.status, language)
        terminal = (snapshot.status in {"completed", "failed", "blocked", "waiting_user", "paused"}
                    and not (snapshot.repair and snapshot.status in {"failed", "blocked"}))
        activities = [(key, value) for key, value in self.activities.items() if key in active_tasks]
        sets = list(self.checks.values())
        running_checks = [item for item in sets if not item.finished]

        def action_name(action: str) -> str:
            if action == "implement":
                return "编写代码" if zh else "Implementation"
            if action == "review":
                return "代码审查" if zh else "Code review"
            if action == "verify":
                return "任务验证" if zh else "Task verification"
            return label(action, language)

        if terminal:
            action = label(snapshot.status, language)
        elif snapshot.repair:
            action = snapshot.repair
        elif running_checks:
            action = ("基线验证" if zh else "Baseline verification") if all(c.baseline for c in running_checks) else (
                ("任务验证" if zh else "Task verification") if any(c.task_id for c in running_checks)
                else ("验证检查" if zh else "Checks"))
        elif len(activities) == 1:
            action = action_name(active_tasks[activities[0][0]])
        elif activities:
            action = "并行执行" if zh else "Parallel execution"
        elif sets:
            action = "验证已结束" if zh else "Checks finished"
        elif self.calls:
            action = "调用服务" if zh else "Provider call"
        elif self.stage_active:
            action = "执行中" if zh else "Running"
        else:
            action = "当前动作未知" if zh else "Current action unknown"
        header = f"{stage} → {action}"
        if snapshot.tasks:
            header += (f" | 已完成 {snapshot.done}/{len(snapshot.tasks)} 项任务" if zh else
                       f" | Tasks completed {snapshot.done}/{len(snapshot.tasks)}")
        lines = [header]
        if not terminal:
            for task_id, activity in activities[:2]:
                attempt = f"第 {activity.attempt} 次尝试" if zh else f"attempt {activity.attempt}"
                lines.append(f"{task_id}「{text(activity.title)[:70]}」 · {action_name(active_tasks[task_id])} · {attempt}")
            if len(activities) > 2:
                lines.append(f"另有 {len(activities) - 2} 项执行中" if zh else f"{len(activities) - 2} more active tasks")
            # Each task retains its own counters; never combine unrelated suites.
            for check_set in sets[:2]:
                c = check_set.counts
                prefix = f"{check_set.task_id} " if check_set.task_id else ""
                state = ("验证中断" if zh else "Checks interrupted") if check_set.interrupted else (
                    ("验证结束" if zh else "Checks finished") if check_set.finished else
                    ("验证进度" if zh else "Checks"))
                lines.append(prefix + state + f" {c.get('completed', 0)}/{c.get('total', 0)}"
                             + (f"，通过 {c.get('passed', 0)}，失败 {c.get('failed', 0)}，取消 {c.get('cancelled', 0)}" if zh else
                                f", passed {c.get('passed', 0)}, failed {c.get('failed', 0)}, cancelled {c.get('cancelled', 0)}"))
                for name, started, state in list(check_set.active.values())[:1]:
                    elapsed = duration(time.monotonic() - started)
                    lines.append(("当前：" if zh else "Current: ") + name + (
                        (" · 已提交，等待执行结果" if zh else " · Submitted, awaiting result") if state == "submitted" else
                        (f" · 已执行 {elapsed}" if zh else f" · Running {elapsed}")))
                if len(check_set.active) > 1:
                    lines[-1] += f" (+{len(check_set.active) - 1})"
            if len(sets) > 2:
                lines.append(f"另有 {len(sets) - 2} 组验证" if zh else f"{len(sets) - 2} more check sets")
            if self.calls and not running_checks:
                providers = sorted({text(call[1]) for call in self.calls.values() if call[1]})
                lines.append(("服务调用：" if zh else "Provider: ") + (", ".join(providers) or "Agent"))
            if self.buffered:
                lines.append("当前调用的输出在结束后收集" if zh else "Current call output is collected at completion")
            if self.last_output is not None:
                seconds = max(0, int(time.monotonic() - self.last_output))
                lines.append(f"最近输出 {seconds} 秒前" if zh else f"Last output {seconds}s ago")
            elif not self.buffered and (self.stage_active or activities or running_checks or self.calls):
                lines.append("尚未收到执行输出" if zh else "No execution output received yet")
        return [text(line) for line in lines]
