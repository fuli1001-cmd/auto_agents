from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

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

# Old reports remain readable, but these reasons describe local execution
# limits, never provider availability or a reason to obtain a fresh budget.
LOCAL_EXECUTION_LIMITS = frozenset({"execution_budget_exhausted", "timed_out", "safety_ceiling"})


def execution_budget_probe(seconds: float) -> Callable[[], str]:
    """Create one explicit caller budget, shared across copies and continuations."""
    import math
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("execution budget must be a finite positive duration")
    deadline = time.monotonic() + seconds
    return lambda: "execution_budget_exhausted" if time.monotonic() >= deadline else ""


def combine_termination_probes(*probes: Optional[Callable[[], str]]) -> Callable[[], str]:
    """Preserve caller cancellation while adding workflow health supervision."""
    def poll() -> str:
        for probe in probes:
            if probe is not None:
                reason = probe()
                if reason:
                    return reason
        return ""
    return poll


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
        self.active_tools: Dict[str, str] = {}
        self._tool_context: Dict[str, Tuple[str, str]] = {}
        self.protocol_seen = decoder is None or not decoder.requires_protocol
        self.repeat_count = 0
        self.last_loop_fingerprint = ""
        self.seen_semantic_fingerprints: set[str] = set()
        self.trusted_progress = (request.purpose == "self_repair"
                                 or bool(request.progress_evidence_path and request.progress_expected_checks))
        self.seen_verified_checkpoints = self._trusted_checkpoints() if self.trusted_progress else set()
        self.forced_reason = ""
        self.events: Deque[Dict[str, object]] = deque(maxlen=50)
        self._events_lock = Lock()
        self._checkpoint_lock = Lock()
        self._process_snapshot: Dict[int, Tuple[int, int, str]] = {}
        self._record("supervision_started", detail=request.attempt_id)
        self.write_checkpoint("running", force=True)

    def _trusted_checkpoints(self):
        try:
            payload = json.loads(self.request.progress_evidence_path.read_text())
            if payload.get("context") != self.request.progress_evidence_path.stem:
                return set()
            allowed = self.request.progress_expected_checks
            observed = set()
            for checkpoint in payload.get("checkpoints", []):
                if checkpoint.startswith("start:"):
                    identity, phase = checkpoint[6:], "start"
                else:
                    parts = checkpoint.rsplit(":", 2)
                    if len(parts) != 3:
                        continue
                    identity, phase = parts[0], ":".join(parts[1:])
                for target in allowed:
                    if identity == target or identity.startswith((target + "::", target + "[")):
                        # A broad file check cannot renew a lease forever by
                        # adding unrelated test names inside that same file.
                        observed.add(target + ":" + phase)
            return observed
        except (OSError, ValueError, TypeError, AttributeError):
            return set()

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
        if self.trusted_progress:
            checkpoints = self._trusted_checkpoints()
            if checkpoints - self.seen_verified_checkpoints:
                self.seen_verified_checkpoints.update(checkpoints)
                self.last_semantic_progress = now
                self._record("verified_stage_progress", semantic=True)
        if now - self.last_workspace_poll >= WORKSPACE_POLL_SECONDS:
            self.last_workspace_poll = now
            self._sample_workspace(now)
        if now - self.last_checkpoint >= CHECKPOINT_SECONDS:
            self.write_checkpoint("running")

        if self.forced_reason:
            return self.forced_reason
        elapsed = now - self.started_at
        if (
            not self.protocol_seen
            and elapsed >= min(PROTOCOL_STARTUP_SECONDS, self.config.provider_idle_seconds)
        ):
            return "protocol_error"
        if now - self.last_provider_activity >= self.config.provider_idle_seconds:
            return "provider_idle"
        if self.active_tools and now - self.last_tool_activity >= self.config.tool_idle_seconds:
            return "tool_stalled"
        if (
            (not self.active_tools or self.trusted_progress)
            and now - self.last_semantic_progress
            >= self._effective_progress_lease_seconds()
        ):
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
                tool_id = self._tool_key(event)
                self.active_tools[tool_id] = (
                    event.detail or event.fingerprint or event.tool_id
                )
                self._tool_context[tool_id] = (event.fingerprint, event.detail)
                self.last_tool_activity = now
            elif event.kind == "tool_progress":
                if self._tool_key(event) in self.active_tools:
                    self.last_tool_activity = now
            elif event.kind == "tool_completed":
                self.last_tool_activity = now
                context = self._tool_context.pop(self._tool_key(event), None)
                if context is not None:
                    # Equal results from different inputs are distinct evidence.
                    fingerprint = hashlib.sha256(json.dumps(
                        [context, event.fingerprint], ensure_ascii=False,
                    ).encode("utf-8")).hexdigest()
                    event = replace(event, fingerprint=fingerprint)
                self._observe_loop(event)
                self.active_tools.pop(self._tool_key(event), None)
            elif event.kind == "error":
                self.forced_reason = "provider_error"
            new_progress = False
            if event.semantic and not self.trusted_progress:
                semantic_fingerprint = self._semantic_fingerprint(event)
                if semantic_fingerprint not in self.seen_semantic_fingerprints:
                    self.seen_semantic_fingerprints.add(semantic_fingerprint)
                    self.last_semantic_progress = now
                    new_progress = True
            self._record(
                event.kind,
                detail=event.detail,
                fingerprint=event.fingerprint,
                semantic=new_progress,
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
            active_tool=self._active_tool_summary(),
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
            "prompt_metadata": dict(self.request.prompt_metadata),
            "attempt_id": self.request.attempt_id,
            "cwd": str(self.request.cwd),
            "session_id": self.session_id,
            "pid": self.process_pid,
            "process_start_identity": process_start_identity(self.process_pid),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(now - self.started_at, 3),
            "last_provider_activity_seconds": round(now - self.last_provider_activity, 3),
            "last_semantic_progress_seconds": round(now - self.last_semantic_progress, 3),
            "active_tool": self._active_tool_summary(),
            "active_tool_count": len(self.active_tools),
            "active_tools": [
                {"tool_id": tool_id, "detail": detail[:300]}
                for tool_id, detail in sorted(self.active_tools.items())
            ],
            "effective_progress_lease_seconds": self._effective_progress_lease_seconds(),
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
                "" if self.trusted_progress else self.workspace_fingerprint,
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
            # A fast edit/tool cycle may precede the periodic workspace probe.
            # Confirm there was no workspace progress before stopping it.
            self._sample_workspace(time.monotonic())
            if self.repeat_count >= self.config.loop_repeat_limit:
                self.forced_reason = "loop_detected"

    def _sample_workspace(self, now: float) -> None:
        workspace = self._workspace_fingerprint()
        output = self._file_fingerprint(self.request.output_path)
        if workspace != self.workspace_fingerprint or output != self.output_fingerprint:
            self.workspace_fingerprint = workspace
            self.output_fingerprint = output
            if not self.trusted_progress:
                self.last_semantic_progress = now
                self.repeat_count = 0
                self.last_loop_fingerprint = ""
            self._record("workspace_changed", fingerprint=workspace, semantic=not self.trusted_progress)

    def _sample_process_group(self, now: float) -> None:
        snapshot = self._read_process_group(self.process_pid)
        if not snapshot:
            return
        changed = snapshot != self._process_snapshot
        if changed:
            self.last_provider_activity = now
            if self.active_tools:
                self.last_tool_activity = now
        self._process_snapshot = snapshot

    def _effective_progress_lease_seconds(self) -> int:
        # Canary configuration permits short waits; it is not a task-analysis
        # lease and must not silently inherit the ordinary 60-second minimum.
        minimum = 1 if self.request.stage == "provider_probe" else 60
        request_lease = int(self.request.progress_lease_seconds or 0)
        if request_lease > 0:
            return max(minimum, request_lease)
        return max(
            minimum,
            int(
                self.config.stage_progress_lease_seconds.get(
                    self.request.stage,
                    self.config.semantic_stall_seconds,
                )
            ),
        )

    def _active_tool_summary(self) -> str:
        return "; ".join(self.active_tools.values())[:300]

    def _tool_key(self, event: AgentProgressEvent) -> str:
        return str(
            event.tool_id
            or event.fingerprint
            or self._normalize_detail(event.detail)
            or "unknown-tool"
        )

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
