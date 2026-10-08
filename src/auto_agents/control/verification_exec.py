"""Write confinement for checks even when a project disables gate worktrees."""

import sys

if __name__ == "__main__":
    sys.path.pop(0)
from pathlib import Path
import json
import os


def execute(payload):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from auto_agents.verification_metadata import metadata_exec

    workspace = Path(payload["workspace"]).resolve()
    scratch = Path(payload["scratch"]).resolve()
    for name in ("home", "tmp", "cache", "state", "config", "data"):
        (scratch / name).mkdir(exist_ok=True)
    if payload.get("environment") is not None:
        os.environ.clear()
        os.environ.update(payload["environment"])
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        os.environ.pop(key, None)
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "COPILOT_HOME"):
        os.environ.pop(key, None)
    os.environ.update(
        HOME=str(scratch / "home"),
        TMPDIR=str(scratch / "tmp"),
        XDG_CACHE_HOME=str(scratch / "cache"),
        XDG_STATE_HOME=str(scratch / "state"),
        XDG_CONFIG_HOME=str(scratch / "config"),
        XDG_DATA_HOME=str(scratch / "data"),
        PYTHONDONTWRITEBYTECODE="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    os.chdir(workspace)
    return metadata_exec(
        ["/bin/sh", "-c", payload["command"]], [str(workspace), str(scratch)]
    )


if __name__ == "__main__":
    raise SystemExit(execute(json.loads(Path(sys.argv[1]).read_text())))
