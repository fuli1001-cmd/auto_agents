"""One inherited filesystem boundary for every configured CLI provider."""

import sys

# This directory has types.py; never let a file launcher shadow stdlib types.
if __name__ == "__main__":
    sys.path.pop(0)
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from auto_agents.control.types import ControlError
else:
    from .types import ControlError


class Boundary:
    def __init__(self, workspace, scratch, *, readonly=False):
        self.root = Path(workspace).resolve()
        self.scratch = Path(scratch).resolve()
        self.readonly = readonly
        self.prepared = False
        self.roots = (
            [str(self.scratch)] if readonly else [str(self.root), str(self.scratch)]
        )
        self.scratch.mkdir(parents=True, exist_ok=True)

    def command(self, argv):
        launcher = Path(__file__).resolve()
        return [
            sys.executable,
            str(launcher),
            "--execute",
            json.dumps(self.roots),
            "env",
            "-C",
            str(self.root),
            *argv,
        ]

    def check(self):
        from ..verification_sandbox import landlock_abi

        if landlock_abi() < 3:
            raise ControlError(
                "confinement",
                "Provider writes require Landlock ABI 3",
                category="environment",
            )
        probe = subprocess.run(
            self.command([sys.executable, "-B", "-c", 'print("boundary-ready")']),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if probe.returncode or probe.stdout.strip() != "boundary-ready":
            raise ControlError(
                "confinement",
                "Provider filesystem boundary is unavailable: " + probe.stderr[-1200:],
                category="environment",
            )

    def dispatch(self, argv, env, cwd):
        if Path(cwd).resolve() != self.root:
            raise ControlError(
                "confinement", "Provider cwd differs from its owned workspace"
            )
        env = dict(os.environ if env is None else env)
        original = Path(env.get("HOME", str(Path.home())))
        sources = {
            "home": (original, (".claude.json", ".gitconfig")),
            "codex": (
                Path(env.get("CODEX_HOME", str(original / ".codex"))),
                ("config.toml", "auth.json"),
            ),
            "claude": (
                Path(env.get("CLAUDE_CONFIG_DIR", str(original / ".claude"))),
                ("settings.json", ".credentials.json"),
            ),
        }
        if not self.prepared:
            for name, (source, files) in sources.items():
                target = self.scratch / name
                target.mkdir(exist_ok=True)
                for filename in files:
                    if (source / filename).is_file():
                        shutil.copyfile(source / filename, target / filename)
                if name == "codex":
                    for file in source.glob("*.config.toml"):
                        if file.is_file():
                            shutil.copyfile(file, target / file.name)
            self.prepared = True
        aliases = {
            "HOME": "home",
            "CODEX_HOME": "codex",
            "CLAUDE_CONFIG_DIR": "claude",
            "TMPDIR": "tmp",
            "XDG_CACHE_HOME": "cache",
            "XDG_CONFIG_HOME": "config",
            "XDG_DATA_HOME": "data",
            "XDG_STATE_HOME": "state",
        }
        for key, name in aliases.items():
            directory = self.scratch / name
            directory.mkdir(exist_ok=True)
            env[key] = str(directory)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Read-only providers still need a private output and state directory.
        return self.command(argv), env


if __name__ == "__main__":
    # Executed as a file so the root launcher cannot shadow the package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from auto_agents.verification_metadata import metadata_exec

    if len(sys.argv) < 5 or sys.argv[1] != "--execute":
        raise SystemExit("Internal provider boundary")
    roots = json.loads(sys.argv[2])
    raise SystemExit(metadata_exec(sys.argv[3:], roots))
