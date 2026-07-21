from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Event, Thread
from typing import Dict, Iterator, List, Optional, TextIO

from ..models import AgentRequest, AgentResult, AgentTermination, SmartTimeoutConfig
from ..process_supervision import (
    ACTIVE_PROCESSES,
    ProcessTerminationResult,
    terminate_process_group,
)
from ..supervision import ProgressDecoder, ProgressSupervisor


class AgentAdapter(ABC):
    def supports_image_attachments(self) -> bool:
        """Return whether this adapter can attach image files to a request.

        Capability checks fail closed so providers cannot silently receive only
        attachment paths in the prompt when the request requires native image
        input.
        """
        return False

    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError


@dataclass
class SubprocessRunResult:
    stdout: str
    stderr: str
    returncode: int
    streamed_stdout: bool
    streamed_stderr: bool
    provider_session_id: str = ""
    termination: Optional[AgentTermination] = None

    def __iter__(self) -> Iterator[object]:
        # Preserve the historical five-value destructuring contract.
        yield self.stdout
        yield self.stderr
        yield self.returncode
        yield self.streamed_stdout
        yield self.streamed_stderr

    def __len__(self) -> int:
        return 5

    def __getitem__(self, index: int) -> object:
        return tuple(iter(self))[index]


def _kill_process_group(process: subprocess.Popen) -> ProcessTerminationResult:
    """Kill the entire process group so child processes (servers, scripts) are cleaned up."""
    return terminate_process_group(process)


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
    smart_timeout: Optional[SmartTimeoutConfig] = None,
    progress_decoder: Optional[ProgressDecoder] = None,
    provider: str = "",
) -> SubprocessRunResult:
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
    ACTIVE_PROCESSES.register(process, kind=f"provider:{provider or 'agent'}")

    smart_enabled = bool(smart_timeout and smart_timeout.enabled)
    if request.stream_output is None and not smart_enabled:
        # Non-streaming: collect all output at once.
        try:
            stdout, stderr = process.communicate(input=actual_stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            termination_result = _kill_process_group(process)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                stdout, stderr = "", ""
            ACTIVE_PROCESSES.unregister(
                process.pid,
                preserve_if_alive=termination_result.cleanup_incomplete,
            )
            return SubprocessRunResult(
                stdout or "",
                (stderr or "") + f"\ntimed out after {timeout}s",
                -1,
                False,
                False,
            )
        except BaseException:
            termination_result = _kill_process_group(process)
            ACTIVE_PROCESSES.unregister(
                process.pid,
                preserve_if_alive=termination_result.cleanup_incomplete,
            )
            raise
        ACTIVE_PROCESSES.unregister(process.pid)
        return SubprocessRunResult(
            stdout or "", stderr or "", process.returncode, False, False
        )

    # Streaming path: forward output in real-time via threads.
    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    streamed = {"stdout": False, "stderr": False}
    last_activity = [time.monotonic()]  # mutable container for thread-safe updates
    stalled = Event()
    watchdog_cleanup_incomplete = [False]
    supervisor = (
        ProgressSupervisor(
            config=smart_timeout,
            request=request,
            provider=provider,
            process_pid=process.pid,
            decoder=progress_decoder,
        )
        if smart_enabled and smart_timeout is not None
        else None
    )

    def forward_output(stream_name: str, sink: List[str], pipe: TextIO) -> None:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            sink.append(chunk)
            last_activity[0] = time.monotonic()
            if supervisor is not None:
                supervisor.observe_io(stream_name, chunk)
            if request.stream_output is not None:
                streamed[stream_name] = True
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
                watchdog_cleanup_incomplete[0] = _kill_process_group(
                    process
                ).cleanup_incomplete
                return

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_thread = Thread(
        target=forward_output,
        args=("stdout", stdout_chunks, process.stdout),
        daemon=True,
    )
    stderr_thread = Thread(
        target=forward_output,
        args=("stderr", stderr_chunks, process.stderr),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    done_event = Event()
    watchdog_thread: Optional[Thread] = None
    if not smart_enabled and idle_timeout and idle_timeout > 0:
        watchdog_thread = Thread(target=idle_watchdog, args=(idle_timeout, done_event), daemon=True)
        watchdog_thread.start()

    termination_reason = ""
    cleanup_incomplete = watchdog_cleanup_incomplete[0]
    try:
        if actual_stdin:
            process.stdin.write(actual_stdin)
        process.stdin.close()

        started_at = time.monotonic()
        while process.poll() is None:
            if supervisor is not None:
                termination_reason = supervisor.poll() or ""
            elif stalled.is_set():
                termination_reason = "provider_idle"
            elif timeout and time.monotonic() - started_at >= timeout:
                termination_reason = "timed_out"
            if termination_reason:
                cleanup_incomplete = _kill_process_group(process).cleanup_incomplete
                break
            time.sleep(1)
    except BaseException:
        termination_reason = "external_interrupt"
        termination_result = _kill_process_group(process)
        if supervisor is not None:
            supervisor.finalize("interrupted", reason=termination_reason)
        ACTIVE_PROCESSES.unregister(
            process.pid,
            preserve_if_alive=termination_result.cleanup_incomplete,
        )
        raise
    finally:
        done_event.set()

    cleanup_incomplete = cleanup_incomplete or watchdog_cleanup_incomplete[0]

    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
    if watchdog_thread is not None:
        watchdog_thread.join(timeout=11)
    cleanup_incomplete = cleanup_incomplete or watchdog_cleanup_incomplete[0]
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        cleanup_incomplete = (
            _kill_process_group(process).cleanup_incomplete or cleanup_incomplete
        )
    returncode = process.poll()
    if returncode is None:
        termination_result = _kill_process_group(process)
        cleanup_incomplete = termination_result.cleanup_incomplete or cleanup_incomplete
        returncode = process.poll()
    if returncode is None:
        returncode = -1

    # The legacy watchdog can terminate the process between polling iterations.
    if not smart_enabled and not termination_reason and stalled.is_set():
        termination_reason = "provider_idle"

    termination: Optional[AgentTermination] = None
    if termination_reason:
        if supervisor is not None:
            termination = supervisor.termination(termination_reason)
            supervisor.finalize("terminated", reason=termination_reason)
        elapsed = (
            termination.elapsed_seconds
            if termination is not None
            else time.monotonic() - started_at
        )
        tail = _tail_lines(stdout_chunks + stderr_chunks, 30)
        if supervisor is None:
            if termination_reason == "provider_idle":
                diagnostic = f"stalled (no output) after {idle_timeout}s"
            else:
                diagnostic = f"timed out after {timeout}s"
        else:
            label = termination_reason.replace("_", " ")
            report = termination.report_path if termination is not None else ""
            diagnostic = f"smart timeout: {label} after {elapsed:.1f}s"
            if report:
                diagnostic += f"; report={report}"
        if tail:
            diagnostic += f"\n--- last output ---\n{tail}"
        stderr_chunks.append("\n" + diagnostic)
        returncode = -1
    elif supervisor is not None:
        supervisor.finalize("completed")

    ACTIVE_PROCESSES.unregister(
        process.pid,
        preserve_if_alive=cleanup_incomplete,
    )

    return SubprocessRunResult(
        "".join(stdout_chunks),
        "".join(stderr_chunks),
        returncode,
        streamed["stdout"],
        streamed["stderr"],
        provider_session_id=supervisor.session_id if supervisor is not None else "",
        termination=termination,
    )
