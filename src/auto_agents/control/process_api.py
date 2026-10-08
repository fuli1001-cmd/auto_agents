"""Only the lock owner may quiesce its recorded subprocess groups."""

from pathlib import Path
import os
import signal
import time
from .types import ControlError


def quiesce(project):
    from ..run_lock import (
        ProjectRunLock,
        _live_control_processes,
        _signal_groups,
        process_group_exists,
    )

    lock = ProjectRunLock(Path(project))
    if lock._inherited_fd() is None:
        raise ControlError(
            "ownership", "Quiesce requires the owning project lock descriptor"
        )
    processes = _live_control_processes(
        lock.control_path,
        expected_project=str(lock.project_root),
        expected_token=os.environ["AUTO_AGENTS_RUN_TOKEN"],
    )
    groups = _signal_groups(processes, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        process_group_exists(group) for group in groups
    ):
        time.sleep(0.1)
    for group in groups:
        if process_group_exists(group):
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        process_group_exists(group) for group in groups
    ):
        time.sleep(0.1)
    return {
        "ok": not any(process_group_exists(group) for group in groups),
        "groups": len(groups),
    }
