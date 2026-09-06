from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from auto_agents import process_supervision as supervision


@pytest.mark.parametrize("startup_blocked", [False, True])
def test_timeout_preserves_only_output_emitted_before_deadline(tmp_path, monkeypatch, startup_blocked):
    ready = tmp_path / "ready"
    startup = tmp_path / "sitecustomize.py"
    # A startup hook reproduces the empty-output failure without relying on a
    # busy host: the interpreter is alive but has not reached the -c body.
    startup.write_text(
        "import time\nfrom pathlib import Path\n"
        f"Path({str(ready)!r}).touch()\ntime.sleep(30)\n"
        if startup_blocked else "",
        encoding="utf-8",
    )
    code = (
        "import sys, time; from pathlib import Path; "
        "print('started', flush=True); print('diagnostic', file=sys.stderr, flush=True); "
        f"Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    original_popen = subprocess.Popen
    clock_offset = 0.0
    raw_output = {}

    def synchronized_popen(*args, **kwargs):
        nonlocal clock_offset
        process = original_popen(*args, **kwargs)
        try:
            wait_deadline = time.monotonic() + 5
            while not ready.exists():
                assert process.poll() is None, "child exited before readiness"
                assert time.monotonic() < wait_deadline, "child never became ready"
                time.sleep(0.005)
            for stream in ("stdout", "stderr"):
                raw_output[stream] = os.pread(kwargs[stream].fileno(), 4096, 0).decode().strip()
            # Advance only the supervisor's clock, after establishing whether
            # bytes exist. No assumption about Python starting within 200ms.
            clock_offset = 0.2
            return process
        except BaseException:
            supervision.terminate_process_group(process)
            raise

    monkeypatch.setattr(supervision.subprocess, "Popen", synchronized_popen)
    monkeypatch.setattr(supervision, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() + clock_offset, sleep=time.sleep,
    ))
    result = supervision.run_supervised_shell_command(
        "exec " + shlex.join([sys.executable, "-c", code]),
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)},
        timeout_seconds=0.2,
    )
    assert result.termination_reason == "timeout"
    assert result.timeout_seconds == 0.2
    assert not result.cleanup_incomplete
    assert result.stdout == raw_output["stdout"] == ("" if startup_blocked else "started")
    assert raw_output["stderr"] == ("" if startup_blocked else "diagnostic")
    assert result.stderr == (
        "" if startup_blocked else "diagnostic\n"
    ) + "command timed out after 0.2s"


@pytest.mark.parametrize("failure_point", ["register", "on_start", "snapshot"])
def test_startup_failures_terminate_and_unregister_process(tmp_path, monkeypatch, failure_point):
    processes = []
    original_popen = subprocess.Popen
    registry = supervision.ActiveProcessRegistry()
    monkeypatch.setattr(supervision, "ACTIVE_PROCESSES", registry)

    def tracked_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail(*args, **kwargs):
        raise RuntimeError("startup hook failed")

    monkeypatch.setattr(supervision.subprocess, "Popen", tracked_popen)
    if failure_point == "register":
        monkeypatch.setattr(registry, "_write_locked", fail)
    elif failure_point == "snapshot":
        monkeypatch.setattr(supervision, "_process_group_snapshot", fail)
    command = "exec " + shlex.join([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(RuntimeError, match="startup hook failed"):
            supervision.run_supervised_shell_command(
                command, cwd=tmp_path, timeout_seconds=1,
                on_start=fail if failure_point == "on_start" else None,
            )

        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert not supervision.process_group_exists(processes[0].pid)
        assert registry.snapshot() == []
    finally:
        for process in processes:
            supervision.terminate_process_group(process)
