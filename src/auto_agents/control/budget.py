"""A cross-project acceptance budget; native CLI turns are not counted as HTTP requests."""

from pathlib import Path
import fcntl
import json
import os

from .types import ControlError, canonical


def admit(alias, phase):
    filename = os.environ.get("AUTO_AGENTS_ACCEPTANCE_BUDGET")
    if not filename:
        return
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        file.seek(0)
        raw = file.read()
        value = json.loads(raw) if raw else {"limit": 30, "calls": []}
        if len(value["calls"]) >= value["limit"]:
            raise ControlError(
                "acceptance_budget",
                "Real acceptance provider invocation limit reached",
                category="budget",
            )
        value["calls"].append({"provider": alias, "phase": phase})
        file.seek(0)
        file.truncate()
        file.write(canonical(value))
        file.flush()
        os.fsync(file.fileno())
