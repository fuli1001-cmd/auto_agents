from __future__ import annotations

import os
import subprocess
import time
import traceback
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
from ..reporting import find_reporter


class AgentAdapter(ABC):
    def describe_runtime(self, request: AgentRequest):
        from ..prompting import ProviderRuntime
        from ..prompting.runtime import resolve_runtime
        config = getattr(self, "config", None)
        return resolve_runtime(config, request) if config is not None else ProviderRuntime()

    def prepare_request(self, request: AgentRequest) -> AgentRequest:
        from ..prompting import prepare_request
        if request.prompt_spec is None:
            return request
        prepared = prepare_request(request, self.describe_runtime(request))
        if prepared.progress_report_path is not None:
            from ..io_utils import write_json, write_text
            write_text(prepared.progress_report_path.with_suffix(".prompt.txt"), str(prepared.prompt))
            write_json(prepared.progress_report_path.with_suffix(".prompt.json"), dict(prepared.prompt_metadata))
        return prepared

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
    cleanup_incomplete: bool = False

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
    observer = request.diagnostic_output
    if observer is None:
        reporter = find_reporter(request.cwd)
        if reporter is not None:
            observer = reporter.capture(stage=request.stage, attempt_id=request.attempt_id,
                                        kind="provider", provider=provider)
    def observe(stream_name: str, chunk: str) -> None:
        if observer is not None:
            try:
                observer(stream_name, chunk)
            except Exception:
                pass

    def finish_capture(**metadata: object) -> None:
        finish = getattr(observer, "finish", None)
        if callable(finish):
            try:
                finish(**metadata)
            except Exception:
                pass

    def finish_visible() -> None:
        finish = getattr(request.stream_output, "finish", None)
        if callable(finish):
            try:
                finish()
            except Exception:
                pass

    start_capture = getattr(observer, "start", None)
    if callable(start_capture):
        try:
            start_capture(command, env, cwd=str(request.cwd), provider=provider,
                          capture_mode="live" if request.stream_transport or request.stream_output is not None
                          or (smart_timeout and smart_timeout.enabled) else "completion")
        except Exception:
            pass

    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=str(request.cwd), env=env,
            start_new_session=True,
        )
    except BaseException as error:
        finish_capture(status="launch_error", error=str(error), traceback=traceback.format_exc())
        raise
    ACTIVE_PROCESSES.register(process, kind=f"provider:{provider or 'agent'}")

    smart_enabled = bool(smart_timeout and smart_timeout.enabled)
    if smart_enabled and request.progress_managed_timeout:
        # Progress-managed requests are bounded by provider/tool/semantic leases,
        # loop detection, and the smart-timeout safety ceiling. Keep the supplied
        # timeout as the legacy fallback when smart supervision is disabled.
        timeout = None
    if request.stream_output is None and not request.stream_transport and not smart_enabled:
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
            observe("stdout", stdout or "")
            observe("stderr", stderr or "")
            finish_capture(status="terminated", termination_reason="timed_out", returncode=-1)
            return SubprocessRunResult(
                stdout or "",
                (stderr or "") + f"\ntimed out after {timeout}s",
                -1,
                False,
                False,
                cleanup_incomplete=termination_result.cleanup_incomplete,
            )
        except BaseException as error:
            termination_result = _kill_process_group(process)
            ACTIVE_PROCESSES.unregister(
                process.pid,
                preserve_if_alive=termination_result.cleanup_incomplete,
            )
            finish_capture(status="interrupted", error=str(error), traceback=traceback.format_exc(),
                           output_complete=False)
            raise
        ACTIVE_PROCESSES.unregister(process.pid)
        observe("stdout", stdout or "")
        observe("stderr", stderr or "")
        finish_capture(returncode=process.returncode)
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
            observe(stream_name, chunk)
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

    stdin_errors: List[Exception] = []

    def feed_input() -> None:
        try:
            with process.stdin:
                if actual_stdin:
                    process.stdin.write(actual_stdin)
        except BrokenPipeError:
            # Providers may exit or close stdin without consuming the prompt.
            pass
        except Exception as error:
            stdin_errors.append(error)

    stdin_thread = Thread(target=feed_input, daemon=True)
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
    started_at = time.monotonic()
    try:
        # Pipe writes can block when a provider stops reading. Keep prompt
        # delivery off the thread that enforces deadlines and health probes.
        stdin_thread.start()
        while process.poll() is None:
            if stdin_errors:
                raise stdin_errors[0]
            if request.termination_probe is not None:
                termination_reason = request.termination_probe() or ""
            if not termination_reason and supervisor is not None:
                termination_reason = supervisor.poll() or ""
            if not termination_reason and stalled.is_set():
                termination_reason = "provider_idle"
            if (
                not termination_reason
                and timeout
                and time.monotonic() - started_at >= timeout
            ):
                termination_reason = "timed_out"
            if termination_reason:
                cleanup_incomplete = _kill_process_group(process).cleanup_incomplete
                break
            time.sleep(1)
        if stdin_errors:
            raise stdin_errors[0]
    except BaseException as error:
        termination_reason = "external_interrupt"
        termination_result = _kill_process_group(process)
        if supervisor is not None:
            supervisor.finalize("interrupted", reason=termination_reason)
        ACTIVE_PROCESSES.unregister(
            process.pid,
            preserve_if_alive=termination_result.cleanup_incomplete,
        )
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        finish_capture(status="interrupted", error=str(error), traceback=traceback.format_exc(),
                       output_complete=False)
        finish_visible()
        raise
    finally:
        done_event.set()
        if stdin_thread.ident is not None:
            stdin_thread.join(timeout=1)

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
    finish_capture(returncode=returncode, termination_reason=termination_reason,
                   cleanup_incomplete=cleanup_incomplete)
    finish_visible()

    return SubprocessRunResult(
        "".join(stdout_chunks),
        "".join(stderr_chunks),
        returncode,
        streamed["stdout"],
        streamed["stderr"],
        provider_session_id=supervisor.session_id if supervisor is not None else "",
        termination=termination,
        cleanup_incomplete=cleanup_incomplete,
    )
