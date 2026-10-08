"""Business-owned entrypoint with optional external supervision."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys


def print_failure(message, *, diagnostics=None):
    from .diagnostic_redaction import sanitize

    summary = " ".join(sanitize(str(message)).split())
    if len(summary) > 400:
        summary = summary[:397] + "…"
    print("执行已停止：" + summary, file=sys.stderr)
    if diagnostics:
        print("详细诊断：" + str(diagnostics), file=sys.stderr)


def select_runtime(argv=None):
    """A normal installation pointer, independent of the maintenance database."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    from .engine_fault import engine_root, digest

    origin = Path(os.environ.get("AUTO_AGENTS_ENGINE_SOURCE_ROOT") or engine_root())
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    pointer = (
        state
        / "auto-agents/installations"
        / digest(str(origin.resolve()))[:24]
        / "current.json"
    )
    if not pointer.is_file() or os.environ.get("AUTO_AGENTS_PINNED_RUNTIME"):
        return
    value = json.loads(pointer.read_text())
    python = Path(value["python"])
    source = Path(value["source"])
    if not python.is_file() or not (source / "src/auto_agents").is_dir():
        raise RuntimeError(
            "Selected engine version is unavailable; installation retained"
        )
    if source.resolve() == Path(__file__).resolve().parents[2]:
        return
    env = {
        **os.environ,
        "AUTO_AGENTS_PINNED_RUNTIME": value["revision"],
        "AUTO_AGENTS_ENGINE_SOURCE_ROOT": str(origin),
        "PYTHONPATH": str(source / "src"),
    }
    os.execve(str(python), [str(python), "-m", "auto_agents", *arguments], env)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    standalone = (
        "--no-supervisor" in arguments
        or os.environ.get("AUTO_AGENTS_NO_SUPERVISOR") == "1"
    )
    arguments = [arg for arg in arguments if arg != "--no-supervisor"]
    public = {
        "business-status",
        "snapshot",
        "resume-check",
        "migrate-state",
        "checkpoint",
        "reconcile-call",
        "quiesce",
    }
    if arguments and arguments[0] in public:
        from .control.cli import main as command

        try:
            return command(arguments)
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 3
    select_runtime(arguments)
    project = None
    for i, arg in enumerate(arguments):
        if arg == "--project" and i + 1 < len(arguments):
            project = Path(arguments[i + 1]).resolve()
        elif arg.startswith("--project="):
            project = Path(arg.split("=", 1)[1]).resolve()
    managed = bool(
        arguments
        and arguments[0] in {"run", "fix", "collab", "resume", "provider-resolve"}
    )
    if managed and project and not standalone:
        config_path = project / ".auto-agents/config.json"
        try:
            config = json.loads(config_path.read_text()) if config_path.exists() else {}
        except (OSError, ValueError) as error:
            print_failure(error)
            return 3
        execution = config.get("execution", {})
        fallback = (
            "off"
            if execution.get("self_repair_diagnosis", {}).get("mode") == "off"
            else "auto"
        )
        mode = execution.get("supervision", {}).get("mode", fallback)
        watcher = shutil.which("auto-agents-watch")
        if watcher and mode != "off":
            from .engine_fault import engine_root

            env = {**os.environ, "AUTO_AGENTS_NO_SUPERVISOR": "1"}
            return subprocess.call(
                [
                    watcher,
                    "run",
                    "--engine",
                    str(engine_root()),
                    "--",
                    sys.executable,
                    "-m",
                    "auto_agents",
                    *arguments,
                ],
                env=env,
            )
        if mode != "off":
            print("监督程序未安装，本次独立运行。", file=sys.stderr)
    from .control.cli import main as dispatch

    try:
        return dispatch(arguments)
    except KeyboardInterrupt:
        print_failure(
            "Interrupted; retained operations must be reconciled before retry"
        )
        return 130
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print_failure(error)
        return 3
