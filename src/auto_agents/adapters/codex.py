from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import List, Optional

from ..io_utils import read_text, write_text
from ..models import AgentRequest, AgentResult, AgentUsage, ProviderConfig
from .base import AgentAdapter


class CodexAdapter(AgentAdapter):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, request: AgentRequest) -> AgentResult:
        command: List[str] = [
            self.config.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--full-auto",
            self.config.cwd_flag,
            str(request.cwd),
            self.config.output_flag,
            str(request.output_path),
        ]

        profile = self.config.profile_map.get(request.effort)
        if profile:
            command.extend(["--profile", profile])

        command.extend(self.config.extra_args)

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort
        timeout = self.config.timeout_seconds or None
        try:
            process = subprocess.run(
                command,
                input=request.prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=str(request.cwd),
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                ok=False,
                command=command,
                output_path=request.output_path,
                summary="",
                model=self._model_label(request),
                stdout="",
                stderr=f"timed out after {timeout}s",
                returncode=-1,
            )
        visible_stdout, usage, error_messages = self._parse_json_stdout(process.stdout)
        stderr = process.stderr.strip()
        if error_messages:
            err_text = "\n".join(error_messages)
            stderr = f"{stderr}\n{err_text}".strip() if stderr else err_text

        streamed_stdout = False
        streamed_stderr = False
        if request.stream_output is not None and visible_stdout:
            request.stream_output("stdout", visible_stdout)
            streamed_stdout = True
        if request.stream_output is not None and stderr:
            request.stream_output("stderr", stderr + "\n")
            streamed_stderr = True

        summary = read_text(request.output_path).strip()
        if not summary and visible_stdout:
            summary = visible_stdout.strip()
            write_text(request.output_path, summary + "\n")

        return AgentResult(
            ok=process.returncode == 0,
            command=command,
            output_path=request.output_path,
            summary=summary,
            model=self._model_label(request),
            usage=usage,
            stdout=visible_stdout,
            stderr=stderr,
            returncode=process.returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
        )

    def _model_label(self, request: AgentRequest) -> str:
        explicit_model = self._explicit_model_arg()
        if explicit_model:
            return explicit_model

        profile = self.config.profile_map.get(request.effort)
        if profile:
            return f"profile:{profile}"
        return "default"

    def _explicit_model_arg(self) -> str:
        extra_args = list(self.config.extra_args)
        for index, value in enumerate(extra_args):
            if value in {"--model", "-m"} and index + 1 < len(extra_args):
                return extra_args[index + 1]
        return ""

    def _parse_json_stdout(self, stdout: str) -> tuple[str, Optional[AgentUsage], List[str]]:
        """Parse codex JSON-line stdout.

        Returns (visible_text, usage, error_messages).
        """
        visible_chunks: List[str] = []
        error_messages: List[str] = []
        usage: Optional[AgentUsage] = None

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                visible_chunks.append(raw_line + "\n")
                continue

            event_type = str(event.get("type", ""))
            if event_type == "item.completed":
                item = event.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        visible_chunks.append(text if text.endswith("\n") else text + "\n")
            elif event_type == "turn.completed":
                usage_payload = event.get("usage", {})
                if isinstance(usage_payload, dict):
                    usage = AgentUsage(
                        input_tokens=int(usage_payload.get("input_tokens", 0) or 0),
                        cached_input_tokens=int(usage_payload.get("cached_input_tokens", 0) or 0),
                        output_tokens=int(usage_payload.get("output_tokens", 0) or 0),
                    )
            elif event_type in ("error", "turn.failed"):
                msg = event.get("message") or ""
                if not msg and isinstance(event.get("error"), dict):
                    msg = event["error"].get("message", "")
                if msg:
                    error_messages.append(msg)

        return "".join(visible_chunks), usage, error_messages
