from __future__ import annotations

import subprocess
from threading import Thread
from abc import ABC, abstractmethod
from typing import Dict, List, TextIO, Tuple

from ..models import AgentRequest, AgentResult


class AgentAdapter(ABC):
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError


def run_subprocess_with_optional_streaming(
    command: List[str],
    request: AgentRequest,
    env: Dict[str, str],
    timeout: int | None = None,
) -> Tuple[str, str, int, bool, bool]:
    if request.stream_output is None:
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
            return "", f"timed out after {timeout}s", -1, False, False
        return process.stdout, process.stderr, process.returncode, False, False

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(request.cwd),
        env=env,
    )

    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    streamed = {"stdout": False, "stderr": False}

    def forward_output(stream_name: str, sink: List[str], pipe: TextIO) -> None:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            sink.append(chunk)
            streamed[stream_name] = True
            request.stream_output(stream_name, chunk)
        pipe.close()

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_thread = Thread(target=forward_output, args=("stdout", stdout_chunks, process.stdout))
    stderr_thread = Thread(target=forward_output, args=("stderr", stderr_chunks, process.stderr))
    stdout_thread.start()
    stderr_thread.start()

    process.stdin.write(request.prompt)
    process.stdin.close()

    stdout_thread.join(timeout=timeout)
    stderr_thread.join(timeout=timeout)
    if process.poll() is None:
        process.kill()
        process.wait()
        return (
            "".join(stdout_chunks),
            "".join(stderr_chunks) + f"\ntimed out after {timeout}s",
            -1,
            streamed["stdout"],
            streamed["stderr"],
        )
    returncode = process.wait()
    return (
        "".join(stdout_chunks),
        "".join(stderr_chunks),
        returncode,
        streamed["stdout"],
        streamed["stderr"],
    )
