"""Ephemeral execution observations for the console, never health evidence."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from uuid import uuid4

from .diagnostic_output import plain_text, redact


def text(value: object) -> str:
    return " ".join(plain_text(redact(value)).split())


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
    output_times: dict = field(default_factory=dict)

    def clear_task(self, task_id: str) -> None:
        self.activities.pop(task_id, None)
        self.output_times.pop(task_id, None)
        self.checks = {k: v for k, v in self.checks.items() if v.task_id != task_id}
        self.calls = {k: v for k, v in self.calls.items() if v[0] != task_id}
        self.buffered = {k: v for k, v in self.buffered.items() if v != task_id}

    def suffix(self, task_id: str, language: str) -> str:
        zh = language == "zh"
        parts = []
        for checks in self.checks.values():
            if checks.task_id != task_id or checks.finished:
                continue
            counts = checks.counts
            total = counts.get("total", 0)
            settled = counts.get("completed", 0) + counts.get("cancelled", 0)
            if total:
                filled = min(12, 12 * settled // total)
                parts.append(f"[{'█' * filled}{'░' * (12 - filled)}] {settled}/{total}")
            for key, cn, en in (("failed", "失败", "failed"), ("cancelled", "取消", "cancelled")):
                if counts.get(key):
                    parts.append(f"{cn if zh else en} {counts[key]}")
        last_output = self.output_times.get(task_id)
        if last_output is not None:
            seconds = max(0, int(time.monotonic() - last_output))
            parts.append(f"最近输出 {seconds} 秒前" if zh else f"Last output {seconds}s ago")
        return " | ".join(parts)


def action_name(action: str, language: str) -> str:
    names = {
        "implement": ("编码", "Coding"), "retry": ("编码", "Coding"),
        "verify": ("验证", "Verification"), "review": ("审查", "Review"),
        "commit": ("集成", "Integration"), "waiting_integration": ("等待集成", "Waiting for integration"),
    }
    pair = names.get(action)
    return pair[0 if language == "zh" else 1] if pair else text(action)


@dataclass
class ActionLine:
    name: str
    timestamp: str
    task_id: str = ""
    live: bool = False
    contextual: bool = False
    repair: bool = False

    @property
    def message(self) -> str:
        if self.repair:
            return text(self.name)
        context = f"{text(self.task_id)} · " if self.contextual and self.task_id else ""
        return "    " + context + text(self.name)
