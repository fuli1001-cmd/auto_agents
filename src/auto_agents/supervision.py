from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from .git_ops import worktree_fingerprint
from .models import (
    AgentProgressEvent,
    AgentRequest,
    AgentTermination,
    SmartTimeoutConfig,
)


PROTOCOL_STARTUP_SECONDS = 120
WORKSPACE_POLL_SECONDS = 15
CHECKPOINT_SECONDS = 30


def process_start_identity(pid: int) -> str:
    """Return the Linux process start tick used to distinguish PID reuse."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError):
        return ""


class ProgressDecoder:
    """Translate provider-native output or sidecars into normalized events."""

    requires_protocol = True

    def feed(self, stream_name: str, chunk: str) -> Iterable[AgentProgressEvent]:
        return ()

    def poll(self) -> Iterable[AgentProgressEvent]:
        return ()


class ProgressSupervisor:
    def __init__(
        self,
        *,
        config: SmartTimeoutConfig,
        request: AgentRequest,
        provider: str,
        process_pid: int,
        decoder: Optional[ProgressDecoder],
    ) -> None:
        now = time.monotonic()
        self.config = config
        self.request = request
        self.provider = provider
        self.process_pid = process_pid
        self.decoder = decoder
        self.started_at = now
        self.last_provider_activity = now
        self.last_tool_activity = now
        self.last_semantic_progress = now
        self.last_workspace_poll = 0.0
        self.last_checkpoint = 0.0
        self.workspace_fingerprint = self._workspace_fingerprint()
        self.output_fingerprint = self._file_fingerprint(request.output_path)
        self.session_id = request.resume_session_id
        self.active_tool = ""
        self.active_tool_id = ""
        self.protocol_seen = decoder is None or not decoder.requires_protocol
        self.repeat_count = 0
        self.last_loop_fingerprint = ""
        self.seen_semantic_fingerprints: set[str] = set()
        self.forced_reason = ""
        self.events: Deque[Dict[str, object]] = deque(maxlen=50)
        self._events_lock = Lock()
        self._checkpoint_lock = Lock()
        self._process_snapshot: Dict[int, Tuple[int, int, str]] = {}
        self._record("supervision_started", detail=request.attempt_id)
        self.write_checkpoint("running", force=True)

    def observe_io(self, stream_name: str, chunk: str) -> List[AgentProgressEvent]:
        if chunk:
            self.last_provider_activity = time.monotonic()
        if self.decoder is None:
            return []
        try:
            events = list(self.decoder.feed(stream_name, chunk))
        except Exception as exc:
            self.forced_reason = "protocol_error"
            self._record("protocol_error", detail=str(exc)[:300])
            return []
        self.observe_events(events)
        return events

    def poll(self) -> Optional[str]:
        now = time.monotonic()
        if self.decoder is not None:
            try:
                self.observe_events(self.decoder.poll())
            except Exception as exc:
                self.forced_reason = "protocol_error"
                self._record("protocol_error", detail=str(exc)[:300])

        self._sample_process_group(now)
        if now - self.last_workspace_poll >= WORKSPACE_POLL_SECONDS:
            self.last_workspace_poll = now
            self._sample_workspace(now)
        if now - self.last_checkpoint >= CHECKPOINT_SECONDS:
            self.write_checkpoint("running")

        if self.forced_reason:
            return self.forced_reason
        elapsed = now - self.started_at
        if elapsed >= self.config.safety_ceiling_seconds:
            return "safety_ceiling"
        if (
            not self.protocol_seen
            and elapsed >= min(PROTOCOL_STARTUP_SECONDS, self.config.provider_idle_seconds)
        ):
            return "protocol_error"
        if now - self.last_provider_activity >= self.config.provider_idle_seconds:
            return "provider_idle"
        if self.active_tool and now - self.last_tool_activity >= self.config.tool_idle_seconds:
            return "tool_stalled"
        if not self.active_tool and now - self.last_semantic_progress >= self.config.semantic_stall_seconds:
            return "semantic_stall"
        return None

    def observe_events(self, events: Iterable[AgentProgressEvent]) -> None:
        for event in events:
            now = time.monotonic()
            self.protocol_seen = True
            self.last_provider_activity = now
            session_changed = False
            if event.session_id:
                session_changed = event.session_id != self.session_id
                self.session_id = event.session_id
            if event.kind == "tool_started":
                self.active_tool_id = event.tool_id
                self.active_tool = event.detail or event.fingerprint or event.tool_id
                self.last_tool_activity = now
            elif event.kind == "tool_progress":
                if not self.active_tool_id or event.tool_id == self.active_tool_id:
                    self.last_tool_activity = now
            elif event.kind == "tool_completed":
                self.last_tool_activity = now
                self._observe_loop(event)
                if not self.active_tool_id or event.tool_id == self.active_tool_id:
                    self.active_tool_id = ""
                    self.active_tool = ""
            elif event.kind == "error":
                self.forced_reason = "provider_error"
            if event.semantic:
                semantic_fingerprint = self._semantic_fingerprint(event)
                if semantic_fingerprint not in self.seen_semantic_fingerprints:
                    self.seen_semantic_fingerprints.add(semantic_fingerprint)
                    self.last_semantic_progress = now
            self._record(
                event.kind,
                detail=event.detail,
                fingerprint=event.fingerprint,
                semantic=event.semantic,
            )
            if session_changed:
                self.write_checkpoint("running", force=True)

    def termination(self, reason: str) -> AgentTermination:
        now = time.monotonic()
        report_path = str(self.request.progress_report_path or "")
        return AgentTermination(
            reason=reason,
            elapsed_seconds=round(now - self.started_at, 3),
            last_provider_activity_seconds=round(now - self.last_provider_activity, 3),
            last_semantic_progress_seconds=round(now - self.last_semantic_progress, 3),
            active_tool=self.active_tool[:300],
            repeat_count=self.repeat_count,
            report_path=report_path,
        )

    def finalize(self, status: str, reason: str = "") -> None:
        self._sample_workspace(time.monotonic())
        self._record("supervision_finished", detail=reason or status)
        self.write_checkpoint(status, reason=reason, force=True)

    def write_checkpoint(self, status: str, *, reason: str = "", force: bool = False) -> None:
        path = self.request.progress_report_path
        if path is None:
            return
        now = time.monotonic()
        if not force and now - self.last_checkpoint < CHECKPOINT_SECONDS:
            return
        self.last_checkpoint = now
        with self._events_lock:
            events = list(self.events)
        payload = {
            "version": 1,
            "status": status,
            "reason": reason,
            "provider": self.provider,
            "stage": self.request.stage,
            "attempt_id": self.request.attempt_id,
            "cwd": str(self.request.cwd),
            "session_id": self.session_id,
            "pid": self.process_pid,
            "process_start_identity": process_start_identity(self.process_pid),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(now - self.started_at, 3),
            "last_provider_activity_seconds": round(now - self.last_provider_activity, 3),
            "last_semantic_progress_seconds": round(now - self.last_semantic_progress, 3),
            "active_tool": self.active_tool[:300],
            "repeat_count": self.repeat_count,
            "workspace_fingerprint": self.workspace_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "events": events,
        }
        with self._checkpoint_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = path.with_suffix(path.suffix + ".tmp")
                temp_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temp_path, path)
            except OSError as exc:
                self._record("checkpoint_error", detail=str(exc)[:300])

    def _observe_loop(self, event: AgentProgressEvent) -> None:
        basis = "\0".join(
            (
                self.request.stage,
                self.workspace_fingerprint,
                event.fingerprint,
                self._normalize_detail(event.detail),
            )
        )
        fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()
        if fingerprint == self.last_loop_fingerprint:
            self.repeat_count += 1
        else:
            self.last_loop_fingerprint = fingerprint
            self.repeat_count = 1
        if self.repeat_count >= self.config.loop_repeat_limit:
            self.forced_reason = "loop_detected"

    def _sample_workspace(self, now: float) -> None:
        workspace = self._workspace_fingerprint()
        output = self._file_fingerprint(self.request.output_path)
        if workspace != self.workspace_fingerprint or output != self.output_fingerprint:
            self.workspace_fingerprint = workspace
            self.output_fingerprint = output
            self.last_semantic_progress = now
            self.repeat_count = 0
            self.last_loop_fingerprint = ""
            self._record("workspace_changed", fingerprint=workspace, semantic=True)

    def _sample_process_group(self, now: float) -> None:
        snapshot = self._read_process_group(self.process_pid)
        if not snapshot:
            return
        changed = snapshot != self._process_snapshot
        if changed:
            self.last_provider_activity = now
            if self.active_tool:
                self.last_tool_activity = now
        self._process_snapshot = snapshot

    def _workspace_fingerprint(self) -> str:
        try:
            return worktree_fingerprint(self.request.cwd)
        except Exception:
            return ""

    @staticmethod
    def _file_fingerprint(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        except OSError:
            return ""

    def _semantic_fingerprint(self, event: AgentProgressEvent) -> str:
        basis = "\0".join(
            (
                event.kind,
                event.fingerprint,
                self._normalize_detail(event.detail),
                self.workspace_fingerprint,
                self.output_fingerprint,
            )
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_detail(detail: str) -> str:
        text = " ".join(str(detail).strip().lower().split())
        return text[:1000]

    def _record(
        self,
        kind: str,
        *,
        detail: str = "",
        fingerprint: str = "",
        semantic: bool = False,
    ) -> None:
        with self._events_lock:
            self.events.append(
                {
                    "at_seconds": round(time.monotonic() - self.started_at, 3),
                    "kind": kind,
                    "detail": detail[:300],
                    "fingerprint": fingerprint[:128],
                    "semantic": semantic,
                }
            )

    @staticmethod
    def _read_process_group(root_pid: int) -> Dict[int, Tuple[int, int, str]]:
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return {}
        result: Dict[int, Tuple[int, int, str]] = {}
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "stat").read_text(encoding="utf-8")
                fields = raw[raw.rfind(")") + 2 :].split()
                process_group = int(fields[2])
                if process_group != root_pid:
                    continue
                cpu_ticks = int(fields[11]) + int(fields[12])
                io_total = 0
                io_path = entry / "io"
                if io_path.is_file():
                    for line in io_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith(("read_bytes:", "write_bytes:")):
                            io_total += int(line.split(":", 1)[1].strip())
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace"
                )
                result[int(entry.name)] = (cpu_ticks, io_total, command[:300])
            except (OSError, ValueError, IndexError):
                continue
        return result
