from __future__ import annotations

import shlex
import subprocess
import sys

import pytest

from auto_agents import process_supervision as supervision


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
