from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Optional

from ..io_utils import read_text, write_text
from ..models import (
    AgentProgressEvent,
    AgentRequest,
    AgentResult,
    ProviderConfig,
    SmartTimeoutConfig,
)
from .base import AgentAdapter, run_subprocess_with_optional_streaming
from ..supervision import ProgressDecoder


# Default directory for Copilot CLI profile config dirs.
# Each profile is a subdirectory containing settings that
# ``copilot --config-dir`` understands.
DEFAULT_PROFILES_ROOT = Path.home() / ".copilot" / "profiles"


class CopilotProgressDecoder(ProgressDecoder):
    def feed(self, stream_name: str, chunk: str):
        if stream_name != "stdout":
            return ()
        try:
            event = json.loads(chunk.strip())
        except (json.JSONDecodeError, AttributeError):
            if chunk.strip():
                raise ValueError("Copilot JSONL stream emitted invalid JSON")
            return ()
        if not isinstance(event, dict):
            raise ValueError("Copilot JSONL event must be an object")
        event_type = str(event.get("type", ""))
        if not event_type:
            raise ValueError("Copilot JSONL event is missing type")
        data = event.get("data", {})
        if not isinstance(data, dict):
            data = {}
        if event_type == "session.start":
            return (
                AgentProgressEvent(
                    kind="activity",
                    session_id=str(data.get("sessionId", "")),
                    detail=event_type,
                ),
            )
        if event_type == "tool.execution_start":
            detail = str(data.get("toolName") or "tool")
            arguments = data.get("arguments", {})
            fingerprint = hashlib.sha256(
                json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            return (
                AgentProgressEvent(
                    kind="tool_started",
                    tool_id=str(data.get("toolCallId", "")),
                    fingerprint=fingerprint,
                    detail=detail,
                ),
            )
        if event_type == "tool.execution_complete":
            fingerprint = hashlib.sha256(
                json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            return (
                AgentProgressEvent(
                    kind="tool_completed",
                    tool_id=str(data.get("toolCallId", "")),
                    fingerprint=fingerprint,
                    detail=str(data.get("toolName") or "tool"),
                    semantic=True,
                ),
            )
        if event_type in {"session.error", "assistant.error", "error"}:
            return (AgentProgressEvent(kind="error", detail=event_type),)
        if event_type in {"session.end", "assistant.turn_end"}:
            return (AgentProgressEvent(kind="completed", detail=event_type),)
        return (AgentProgressEvent(kind="activity", detail=event_type),)


class CopilotCliAdapter(AgentAdapter):
    """Programmatic CLI adapter for GitHub Copilot CLI.

    Mirrors the Codex adapter's minimal-config pattern:
    * project config only carries ``profile_map`` (effort → profile name)
    * all provider details live in native config dirs under
      ``~/.copilot/profiles/<profile-name>/``
    * ``--config-dir`` points Copilot CLI at the resolved directory
    * ``--allow-all-tools`` is the default for headless automation
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
        command = self._build_command(request)

        # Clear stale output so a reused output_path doesn't mask fresh results.
        write_text(request.output_path, "")

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort

        # When prompt_via_stdin is True, pipe the prompt through stdin.
        # Otherwise, append it to the command via -p (non-interactive mode).
        if self.config.prompt_via_stdin:
            stdin_input = request.prompt
        else:
            command.extend(["-p", request.prompt])
            stdin_input = ""

        timeout = self.config.timeout_seconds or None

        filtered_request = request
        if request.stream_output is not None:
            filtered_request = replace(
                request,
                stream_output=self._make_json_stream_filter(request.stream_output),
            )

        process_result = run_subprocess_with_optional_streaming(
            command,
            filtered_request,
            env,
            timeout=timeout,
            stdin_input=stdin_input,
            idle_timeout=self.config.idle_timeout_seconds or None,
            smart_timeout=self.smart_timeout,
            progress_decoder=CopilotProgressDecoder(),
            provider="copilot-cli",
        )
        stdout_raw, stderr, returncode, streamed_stdout, streamed_stderr = process_result

        stderr = stderr.strip()
        stdout = self._parse_json_stdout(stdout_raw)

        summary = read_text(request.output_path).strip()
        if not summary and stdout:
            summary = stdout.strip()
            write_text(request.output_path, summary + "\n")

        return AgentResult(
            ok=returncode == 0,
            command=command,
            output_path=request.output_path,
            summary=summary,
            model=self._model_label(request),
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
            provider_session_id=getattr(process_result, "provider_session_id", ""),
            termination=getattr(process_result, "termination", None),
            supervision_report_path=(
                str(request.progress_report_path) if request.progress_report_path else ""
            ),
        )

    # -- internal helpers --------------------------------------------------

    def _build_command(self, request: AgentRequest) -> List[str]:
        command: List[str] = [self.config.binary]

        # Non-interactive flags for clean scripting output.
        command.extend(["--no-color", "-s"])
        command.extend(["--output-format", "json", "--stream", "on"])

        # NOTE: copilot CLI does not support -C or -o flags.
        # Working directory is passed via subprocess.run(cwd=...).
        # Output is captured from stdout and written to output_path by run().

        # Resolve profile → config-dir
        config_dir = self._resolve_config_dir(request.effort)
        if config_dir:
            command.extend(["--config-dir", str(config_dir)])
            # Copilot currently ignores model in profile config for --config-dir.
            # Inject the profile model explicitly unless caller already provided one.
            if not self._has_model_flag():
                profile_model = self._load_model_from_config_dir(config_dir)
                if profile_model:
                    command.extend(["--model", profile_model])

        # Default to allow-all for headless automation unless the
        # caller explicitly passed tool-permission flags in extra_args.
        if not self._has_tool_permission_flag():
            command.append("--allow-all")

        # Ensure the agent works fully autonomously.
        command.append("--no-ask-user")

        if request.resume_session_id:
            command.append(f"--resume={request.resume_session_id}")

        # Passthrough extra arguments
        command.extend(self.config.extra_args)

        return command

    @staticmethod
    def _make_json_stream_filter(
        callback: Callable[[str, str], None],
    ) -> Callable[[str, str], None]:
        def filtered(stream_name: str, chunk: str) -> None:
            if stream_name != "stdout":
                callback(stream_name, chunk)
                return
            try:
                event = json.loads(chunk.strip())
            except json.JSONDecodeError:
                callback(stream_name, chunk)
                return
            if not isinstance(event, dict) or event.get("type") != "assistant.message":
                return
            data = event.get("data", {})
            content = data.get("content", "") if isinstance(data, dict) else ""
            if isinstance(content, str) and content:
                callback("stdout", content if content.endswith("\n") else content + "\n")
        return filtered

    @staticmethod
    def _parse_json_stdout(stdout: str) -> str:
        messages: List[str] = []
        for raw_line in stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                messages.append(raw_line + "\n")
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant.message":
                continue
            data = event.get("data", {})
            content = data.get("content", "") if isinstance(data, dict) else ""
            if isinstance(content, str) and content:
                messages.append(content if content.endswith("\n") else content + "\n")
        return "".join(messages)

    def _resolve_config_dir(self, effort: str) -> Optional[Path]:
        """Map effort → profile name → config directory path.

        Resolution order:
        1. profile_map entry for the effort level
        2. If the value is an absolute path that exists, use it directly
        3. Otherwise treat it as a profile name under ``~/.copilot/profiles/``
        """
        profile = self.config.profile_map.get(effort)
        if not profile:
            return None

        # Allow absolute path override
        candidate = Path(profile)
        if candidate.is_absolute() and candidate.is_dir():
            return candidate

        return DEFAULT_PROFILES_ROOT / profile

    def _has_tool_permission_flag(self) -> bool:
        permission_flags = {
            "--allow-all",
            "--allow-all-tools",
            "--allow-tool",
            "--deny-tool",
            "--yolo",
        }
        return any(arg in permission_flags for arg in self.config.extra_args)

    def _has_model_flag(self) -> bool:
        for arg in self.config.extra_args:
            if arg in {"--model", "-m"}:
                return True
        return False

    def _load_model_from_config_dir(self, config_dir: Path) -> Optional[str]:
        config_file = config_dir / "config.json"
        if not config_file.is_file():
            return None
        try:
            payload = json.loads(read_text(config_file))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        return None

    def _model_label(self, request: AgentRequest) -> str:
        # Check for explicit --model in extra_args
        extra = list(self.config.extra_args)
        for i, val in enumerate(extra):
            if val in {"--model", "-m"} and i + 1 < len(extra):
                return extra[i + 1]

        config_dir = self._resolve_config_dir(request.effort)
        if config_dir is not None:
            profile_model = self._load_model_from_config_dir(config_dir)
            if profile_model:
                return profile_model

        profile = self.config.profile_map.get(request.effort)
        if profile:
            return f"profile:{profile}"
        return "default"
