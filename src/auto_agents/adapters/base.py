from __future__ import annotations

import os
import subprocess
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Thread
from typing import Dict, Iterator, List, Optional, TextIO

from ..models import AgentRequest, AgentResult, AgentTermination, SmartTimeoutConfig
from ..process_supervision import (
    ACTIVE_PROCESSES,
    ProcessTerminationResult,
    terminate_process_group,
)
from ..supervision import ProgressDecoder, ProgressSupervisor
from ..reporting import find_reporter
from ..artifact_runtime import capture_output


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


def _termination_diagnostic(stage: str, termination: AgentTermination) -> str:
    label = termination.reason.replace("_", " ")
    detail = (f"provider supervision: {label}; stage={stage}; "
              f"elapsed={termination.elapsed_seconds:.1f}s; "
              f"last_progress={termination.last_semantic_progress_seconds:.1f}s ago")
    if termination.report_path:
        detail += f"; report={termination.report_path}"
    return detail


@capture_output
def run_subprocess_with_optional_streaming(
    command: List[str],
    request: AgentRequest,
    env: Dict[str, str],
    stdin_input: Optional[str] = None,
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
                          capture_mode="live")
        except Exception:
            pass

    if request.termination_probe is not None:
        reason = request.termination_probe()
        if reason:
            supervisor = ProgressSupervisor(
                config=smart_timeout or SmartTimeoutConfig(), request=request,
                provider=provider, process_pid=0, decoder=progress_decoder,
            )
            termination = supervisor.termination(reason)
            supervisor.finalize("terminated", reason=reason)
            finish_capture(returncode=-1, termination_reason=reason)
            finish_visible()
            return SubprocessRunResult("", _termination_diagnostic(request.stage, termination),
                                       -1, False, False, termination=termination)

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

    # Streaming path: forward output in real-time via threads.
    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    streamed = {"stdout": False, "stderr": False}
    supervisor = ProgressSupervisor(
        config=smart_timeout or SmartTimeoutConfig(), request=request,
        provider=provider, process_pid=process.pid, decoder=progress_decoder,
    )

    def forward_output(stream_name: str, sink: List[str], pipe: TextIO) -> None:
        while True:
            chunk = pipe.readline()
            if not chunk:
                break
            sink.append(chunk)
            supervisor.observe_io(stream_name, chunk)
            observe(stream_name, chunk)
            if request.stream_output is not None:
                streamed[stream_name] = True
                request.stream_output(stream_name, chunk)
        pipe.close()

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

    termination_reason = ""
    cleanup_incomplete = False
    try:
        # Pipe writes can block when a provider stops reading. Keep prompt
        # delivery off the thread that enforces cancellation and progress supervision.
        stdin_thread.start()
        while process.poll() is None:
            if stdin_errors:
                raise stdin_errors[0]
            if request.termination_probe is not None:
                termination_reason = request.termination_probe() or ""
            if not termination_reason:
                termination_reason = supervisor.poll() or ""
            if termination_reason:
                cleanup_incomplete = _kill_process_group(process).cleanup_incomplete
                break
            time.sleep(1)
        if stdin_errors:
            raise stdin_errors[0]
    except BaseException as error:
        termination_reason = "external_interrupt"
        termination_result = _kill_process_group(process)
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
        if stdin_thread.ident is not None:
            stdin_thread.join(timeout=1)


    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
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

    termination: Optional[AgentTermination] = None
    if termination_reason:
        termination = supervisor.termination(termination_reason)
        supervisor.finalize("terminated", reason=termination_reason)
        tail = _tail_lines(stdout_chunks + stderr_chunks, 30)
        diagnostic = _termination_diagnostic(request.stage, termination)
        if tail:
            diagnostic += f"\n--- last output ---\n{tail}"
        stderr_chunks.insert(0, diagnostic + "\n")
        returncode = -1
    else:
        supervisor.finalize("completed")

    if cleanup_incomplete:
        # Keep the PID/start identity discoverable on a later workflow resume.
        supervisor.finalize("running", reason="cleanup_incomplete")

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
        provider_session_id=supervisor.session_id,
        termination=termination,
        cleanup_incomplete=cleanup_incomplete,
    )
