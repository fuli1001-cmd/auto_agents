from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..io_utils import read_text, write_text
from ..models import AgentRequest, AgentResult, ProviderConfig
from .base import AgentAdapter


class CopilotCliAdapter(AgentAdapter):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, request: AgentRequest) -> AgentResult:
        _, settings, legacy_value = self.config.profile_settings_for(request.effort, "copilot-cli")
        command: List[str] = [
            self.config.binary,
            "-p",
            request.prompt,
            "-s",
        ]

        model = self._string_setting(settings.get("model")) or legacy_value
        if model:
            command.extend(["--model", model])

        self._append_permission_flags(command, settings)

        profile_args = settings.get("args")
        if isinstance(profile_args, list):
            command.extend(str(item) for item in profile_args)

        command.extend(self.config.extra_args)

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort
        env["AUTO_AGENTS_OUTPUT_PATH"] = str(request.output_path)

        temp_config_dir: Optional[Path] = None
        command, env, temp_config_dir = self._apply_effort_level_config(command, env, settings)

        try:
            process = subprocess.run(
                command,
                text=True,
                capture_output=True,
                cwd=str(request.cwd),
                env=env,
            )
        finally:
            if temp_config_dir is not None:
                shutil.rmtree(temp_config_dir, ignore_errors=True)

        stdout = process.stdout
        stderr = process.stderr.strip()

        streamed_stdout = False
        streamed_stderr = False
        if request.stream_output is not None and stdout:
            request.stream_output("stdout", stdout if stdout.endswith("\n") else stdout + "\n")
            streamed_stdout = True
        if request.stream_output is not None and stderr:
            request.stream_output("stderr", stderr + "\n")
            streamed_stderr = True

        summary = read_text(request.output_path).strip()
        if not summary and stdout:
            summary = stdout.strip()
            write_text(request.output_path, summary + "\n")

        return AgentResult(
            ok=process.returncode == 0,
            command=command,
            output_path=request.output_path,
            summary=summary,
            model=self.config.model_label_for_effort(request.effort),
            stdout=stdout,
            stderr=stderr,
            returncode=process.returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
        )

    def _append_permission_flags(self, command: List[str], settings: Dict[str, object]) -> None:
        allow_all_tools = settings.get("allow_all_tools")
        if isinstance(allow_all_tools, bool) and allow_all_tools:
            command.append("--allow-all-tools")

        if isinstance(settings.get("autopilot"), bool) and settings.get("autopilot"):
            command.append("--autopilot")

        if isinstance(settings.get("no_ask_user"), bool) and settings.get("no_ask_user"):
            command.append("--no-ask-user")

        allow_tools = settings.get("allow_tools")
        if isinstance(allow_tools, list):
            for item in allow_tools:
                command.append(f"--allow-tool={str(item)}")

        deny_tools = settings.get("deny_tools")
        if isinstance(deny_tools, list):
            for item in deny_tools:
                command.append(f"--deny-tool={str(item)}")

        available_tools = settings.get("available_tools")
        if isinstance(available_tools, list) and available_tools:
            command.append("--available-tools=" + ",".join(str(item) for item in available_tools))

        excluded_tools = settings.get("excluded_tools")
        if isinstance(excluded_tools, list) and excluded_tools:
            command.append("--excluded-tools=" + ",".join(str(item) for item in excluded_tools))

    def _apply_effort_level_config(
        self,
        command: List[str],
        env: Dict[str, str],
        settings: Dict[str, object],
    ) -> Tuple[List[str], Dict[str, str], Optional[Path]]:
        effort_level = self._string_setting(settings.get("effort_level")) or self._string_setting(
            settings.get("copilot_effort_level")
        )
        if not effort_level:
            return command, env, None

        if self._has_config_dir_flag(command):
            return command, env, None

        configured_dir = self._string_setting(settings.get("config_dir")) or self._string_setting(
            settings.get("copilot_config_dir")
        )
        if configured_dir:
            command.append(f"--config-dir={configured_dir}")
            return command, env, None

        temp_dir = Path(tempfile.mkdtemp(prefix="auto-agents-copilot-config-"))
        merged = self._load_base_copilot_config(env)
        merged["effortLevel"] = effort_level
        (temp_dir / "config.json").write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        command.append(f"--config-dir={str(temp_dir)}")
        return command, env, temp_dir

    def _load_base_copilot_config(self, env: Dict[str, str]) -> Dict[str, object]:
        copilot_home = env.get("COPILOT_HOME", "")
        if copilot_home.strip():
            config_path = Path(copilot_home) / "config.json"
        else:
            config_path = Path.home() / ".copilot" / "config.json"

        if not config_path.exists():
            return {}
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(payload, dict):
            return {str(key): value for key, value in payload.items()}
        return {}

    def _has_config_dir_flag(self, command: List[str]) -> bool:
        for item in command:
            if item == "--config-dir" or item.startswith("--config-dir="):
                return True
        return False

    def _string_setting(self, value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""
