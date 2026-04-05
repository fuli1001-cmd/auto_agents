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
        _, settings, legacy_profile = self.config.profile_settings_for(request.effort, "codex")
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

        profile = self._string_setting(settings.get("codex_profile")) or self._string_setting(settings.get("profile"))
        if not profile:
            profile = legacy_profile
        if profile:
            command.extend(["--profile", profile])

        model = self._string_setting(settings.get("model"))
        if model:
            command.extend(["--model", model])

        profile_args = settings.get("args")
        if isinstance(profile_args, list):
            command.extend(str(item) for item in profile_args)

        command.extend(self.config.extra_args)

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort
        process = subprocess.run(
            command,
            input=request.prompt,
            text=True,
            capture_output=True,
            cwd=str(request.cwd),
            env=env,
        )
        visible_stdout, usage = self._parse_json_stdout(process.stdout)
        stderr = process.stderr.strip()

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
            model=self.config.model_label_for_effort(request.effort),
            usage=usage,
            stdout=visible_stdout,
            stderr=stderr,
            returncode=process.returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
        )

    def _string_setting(self, value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    def _parse_json_stdout(self, stdout: str) -> tuple[str, Optional[AgentUsage]]:
        visible_chunks: List[str] = []
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

        return "".join(visible_chunks), usage
