from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from ..io_utils import read_text, write_text
from ..models import (
    AgentProgressEvent,
    AgentRequest,
    AgentResult,
    ProviderConfig,
    SMART_TIMEOUT_PROGRESS_PROTOCOL,
    SmartTimeoutConfig,
)
from .base import AgentAdapter, run_subprocess_with_optional_streaming
from ..supervision import ProgressDecoder


class ShellProgressDecoder(ProgressDecoder):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def feed(self, stream_name: str, chunk: str):
        return ()

    def poll(self):
        if not self.path.is_file():
            return ()
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self.offset)
            lines = handle.readlines()
            self.offset = handle.tell()
        events = []
        for line in lines:
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("shell progress event must be an object")
            if payload.get("protocol") != SMART_TIMEOUT_PROGRESS_PROTOCOL:
                raise ValueError("shell progress protocol mismatch")
            event_type = str(payload.get("type", ""))
            kind_map = {
                "session.started": "activity",
                "activity": "activity",
                "tool.started": "tool_started",
                "tool.progress": "tool_progress",
                "tool.completed": "tool_completed",
                "milestone": "milestone",
                "session.completed": "completed",
                "error": "error",
            }
            if event_type not in kind_map:
                raise ValueError(f"unsupported shell progress event type: {event_type}")
            kind = kind_map[event_type]
            fingerprint = str(payload.get("fingerprint", ""))
            tool_id = str(payload.get("tool_id", ""))
            if kind in {"tool_completed", "milestone"} and not fingerprint:
                raise ValueError(f"shell progress event {event_type} requires fingerprint")
            if kind in {"tool_started", "tool_progress", "tool_completed"} and not tool_id:
                raise ValueError(f"shell progress event {event_type} requires tool_id")
            events.append(
                AgentProgressEvent(
                    kind=kind,
                    session_id=str(payload.get("session_id", "")),
                    tool_id=tool_id,
                    fingerprint=fingerprint,
                    detail=str(payload.get("detail", "")),
                    semantic=kind in {"tool_completed", "milestone"},
                )
            )
        return events


class ShellAdapter(AgentAdapter):
    """Generic adapter for wrapper scripts around non-Codex CLIs.

    The command receives the prompt on stdin and the following environment variables:

    - AUTO_AGENTS_STAGE
    - AUTO_AGENTS_EFFORT
    - AUTO_AGENTS_OUTPUT_PATH

    If the command does not write the output file itself, stdout is persisted as the stage output.
    """

    def __init__(
        self,
        config: ProviderConfig,
        smart_timeout: Optional[SmartTimeoutConfig] = None,
    ) -> None:
        self.config = config
        self.smart_timeout = smart_timeout or SmartTimeoutConfig(enabled=False)

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, request: AgentRequest) -> AgentResult:
        if (
            self.smart_timeout.enabled
            and self.config.progress_protocol != SMART_TIMEOUT_PROGRESS_PROTOCOL
        ):
            return AgentResult(
                ok=False,
                command=[self.config.binary] + self.config.extra_args,
                output_path=request.output_path,
                stderr=(
                    "provider protocol error: smart timeout requires shell progress_protocol="
                    f"{SMART_TIMEOUT_PROGRESS_PROTOCOL}"
                ),
                returncode=2,
            )
        command = [self.config.binary] + self.config.extra_args
        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort
        env["AUTO_AGENTS_OUTPUT_PATH"] = str(request.output_path)
        progress_path = (
            request.progress_report_path.with_suffix(".progress.jsonl")
            if request.progress_report_path is not None
            else request.output_path.with_suffix(".progress.jsonl")
        )
        env["AUTO_AGENTS_PROGRESS_PATH"] = str(progress_path)
        env["AUTO_AGENTS_ATTEMPT_ID"] = request.attempt_id
        env["AUTO_AGENTS_RESUME_SESSION_ID"] = request.resume_session_id
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        if not request.resume_session_id:
            progress_path.write_text("", encoding="utf-8")

        process_result = run_subprocess_with_optional_streaming(
            command,
            request,
            env,
            timeout=request.timeout_seconds or self.config.timeout_seconds or None,
            idle_timeout=self.config.idle_timeout_seconds or None,
            smart_timeout=self.smart_timeout,
            progress_decoder=ShellProgressDecoder(progress_path),
            provider=self.config.kind,
        )
        stdout, stderr, returncode, streamed_stdout, streamed_stderr = process_result

        summary = read_text(request.output_path).strip()
        if not summary and stdout:
            summary = stdout.strip()
            write_text(request.output_path, summary + "\n")

        return AgentResult(
            ok=returncode == 0,
            command=command,
            output_path=request.output_path,
            summary=summary,
            stdout=stdout,
            stderr=stderr.strip(),
            returncode=returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
            provider_session_id=getattr(process_result, "provider_session_id", ""),
            termination=getattr(process_result, "termination", None),
            supervision_report_path=(
                str(request.progress_report_path) if request.progress_report_path else ""
            ),
        )
