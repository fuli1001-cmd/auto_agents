from __future__ import annotations

import os
import signal
import subprocess
import time
from threading import Event, Thread
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, TextIO, Tuple

from ..models import AgentRequest, AgentResult


class AgentAdapter(ABC):
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill the entire process group so child processes (servers, scripts) are cleaned up."""
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _tail_lines(chunks: List[str], n: int) -> str:
    """Return the last *n* lines from a list of output chunks."""
    all_lines = "".join(chunks).splitlines()
    return "\n".join(all_lines[-n:])


def run_subprocess_with_optional_streaming(
    command: List[str],
    request: AgentRequest,
    env: Dict[str, str],
    timeout: int | None = None,
    stdin_input: Optional[str] = None,
    idle_timeout: int | None = None,
) -> Tuple[str, str, int, bool, bool]:
    """Run a subprocess, optionally streaming output in real-time.

    Uses ``start_new_session=True`` so child processes spawned by the
    agent (servers, scripts) are placed in a new process group and can
    be cleaned up on timeout via ``os.killpg``.

    *stdin_input* overrides what is written to the child's stdin.  When
    ``None`` (the default) ``request.prompt`` is used.  Pass ``""`` to
    send nothing (e.g. when the prompt is passed via command-line args).
    """
    actual_stdin = stdin_input if stdin_input is not None else request.prompt

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
        start_new_session=True,
    )

    if request.stream_output is None:
        # Non-streaming: collect all output at once.
        try:
            stdout, stderr = process.communicate(input=actual_stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                stdout, stderr = "", ""
            return stdout or "", (stderr or "") + f"\ntimed out after {timeout}s", -1, False, False
        return stdout or "", stderr or "", process.returncode, False, False

    # Streaming path: forward output in real-time via threads.
    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    streamed = {"stdout": False, "stderr": False}
    last_activity = [time.monotonic()]  # mutable container for thread-safe updates
    stalled = Event()

    def forward_output(stream_name: str, sink: List[str], pipe: TextIO) -> None:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            sink.append(chunk)
            streamed[stream_name] = True
            last_activity[0] = time.monotonic()
            request.stream_output(stream_name, chunk)
        pipe.close()

    def idle_watchdog(idle_limit: int, done: Event) -> None:
        """Kill the process group if no output is received for *idle_limit* seconds."""
        while not done.is_set():
            done.wait(timeout=10)
            if done.is_set():
                return
            elapsed = time.monotonic() - last_activity[0]
            if elapsed >= idle_limit:
                stalled.set()
                _kill_process_group(process)
                return

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_thread = Thread(target=forward_output, args=("stdout", stdout_chunks, process.stdout))
    stderr_thread = Thread(target=forward_output, args=("stderr", stderr_chunks, process.stderr))
    stdout_thread.start()
    stderr_thread.start()

    done_event = Event()
    watchdog_thread: Optional[Thread] = None
    if idle_timeout and idle_timeout > 0:
        watchdog_thread = Thread(target=idle_watchdog, args=(idle_timeout, done_event), daemon=True)
        watchdog_thread.start()

    if actual_stdin:
        process.stdin.write(actual_stdin)
    process.stdin.close()

    stdout_thread.join(timeout=timeout)
    stderr_thread.join(timeout=timeout)
    done_event.set()

    if process.poll() is None:
        reason = "stalled (no output)" if stalled.is_set() else "timed out"
        _kill_process_group(process)
        tail = _tail_lines(stdout_chunks + stderr_chunks, 30)
        return (
            "".join(stdout_chunks),
            "".join(stderr_chunks) + f"\n{reason} after {idle_timeout if stalled.is_set() else timeout}s"
            + (f"\n--- last output ---\n{tail}" if tail else ""),
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
