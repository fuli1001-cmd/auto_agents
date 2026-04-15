from __future__ import annotations

import os
import shutil

from ..io_utils import read_text, write_text
from ..models import AgentRequest, AgentResult, ProviderConfig
from .base import AgentAdapter, run_subprocess_with_optional_streaming


class ShellAdapter(AgentAdapter):
    """Generic adapter for wrapper scripts around non-Codex CLIs.

    The command receives the prompt on stdin and the following environment variables:

    - AUTO_AGENTS_STAGE
    - AUTO_AGENTS_EFFORT
    - AUTO_AGENTS_OUTPUT_PATH

    If the command does not write the output file itself, stdout is persisted as the stage output.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return shutil.which(self.config.binary) is not None

    def run(self, request: AgentRequest) -> AgentResult:
        command = [self.config.binary] + self.config.extra_args
        env = dict(os.environ)
        env["AUTO_AGENTS_STAGE"] = request.stage
        env["AUTO_AGENTS_EFFORT"] = request.effort
        env["AUTO_AGENTS_OUTPUT_PATH"] = str(request.output_path)

        stdout, stderr, returncode, streamed_stdout, streamed_stderr = run_subprocess_with_optional_streaming(
            command,
            request,
            env,
            timeout=self.config.timeout_seconds or None,
            idle_timeout=self.config.idle_timeout_seconds or None,
        )

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
        )
