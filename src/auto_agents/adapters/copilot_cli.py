from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import List, Optional

from ..io_utils import read_text, write_text
from ..models import AgentRequest, AgentResult, AgentUsage, ProviderConfig
from .base import AgentAdapter


# Default directory for Copilot CLI profile config dirs.
# Each profile is a subdirectory containing settings that
# ``copilot --config-dir`` understands.
DEFAULT_PROFILES_ROOT = Path.home() / ".copilot" / "profiles"


class CopilotCliAdapter(AgentAdapter):
    """Programmatic CLI adapter for GitHub Copilot CLI.

    Mirrors the Codex adapter's minimal-config pattern:
    * project config only carries ``profile_map`` (effort → profile name)
    * all provider details live in native config dirs under
      ``~/.copilot/profiles/<profile-name>/``
    * ``--config-dir`` points Copilot CLI at the resolved directory
    * ``--allow-all-tools`` is the default for headless automation
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, request: AgentRequest) -> AgentResult:
        command = self._build_command(request)

        # Clear stale output so a reused output_path doesn't mask fresh results.
        write_text(request.output_path, "")

        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort

        import subprocess

        # When prompt_via_stdin is True, pipe the prompt through stdin.
        # Otherwise, append it to the command via -p (non-interactive mode).
        stdin_input: Optional[str] = None
        if self.config.prompt_via_stdin:
            stdin_input = request.prompt
        else:
            command.extend(["-p", request.prompt])

        process = subprocess.run(
            command,
            input=stdin_input,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=str(request.cwd),
            env=env,
        )

        stdout = process.stdout or ""
        stderr = (process.stderr or "").strip()

        streamed_stdout = False
        streamed_stderr = False
        if request.stream_output is not None and stdout.strip():
            request.stream_output("stdout", stdout)
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
            model=self._model_label(request),
            stdout=stdout,
            stderr=stderr,
            returncode=process.returncode,
            streamed_stdout=streamed_stdout,
            streamed_stderr=streamed_stderr,
        )

    # -- internal helpers --------------------------------------------------

    def _build_command(self, request: AgentRequest) -> List[str]:
        command: List[str] = [self.config.binary]

        # Non-interactive flags for clean scripting output.
        command.extend(["--no-color", "-s"])

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

        # Passthrough extra arguments
        command.extend(self.config.extra_args)

        return command

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
