from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List

from ..io_utils import read_text, write_text
from ..models import AgentRequest, AgentResult, ProviderConfig
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
        process = subprocess.run(
            command,
            input=request.prompt,
            text=True,
            capture_output=True,
            cwd=str(request.cwd),
            env=env,
        )

        summary = read_text(request.output_path).strip()
        if not summary and process.stdout:
            summary = process.stdout.strip()
            write_text(request.output_path, summary + "\n")

        return AgentResult(
            ok=process.returncode == 0,
            command=command,
            output_path=request.output_path,
            summary=summary,
            stderr=process.stderr.strip(),
            returncode=process.returncode,
        )

