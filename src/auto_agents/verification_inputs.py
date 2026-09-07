"""Conservative runtime input observer for Python-only managed tests.

Unresolved native inputs decline cross-snapshot reuse, rather than pretending
an import graph or a keyword search describes all runtime dependencies.
"""
import hashlib
import json
import os
from pathlib import Path
import sys


class InputObserver:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.inputs, self.reasons = {}, set()
        self.busy, self.active = False, True
        self.runtime = [Path(os.environ[key]).resolve() for key in ("TMPDIR", "HOME", "CODEX_HOME") if os.environ.get(key)]
        self.runtime = [path for path in self.runtime if path != self.root and path not in self.root.parents]
        self.helpers = {str(Path(__file__).resolve()), str(Path(__file__).with_name("verification_pytest.py").resolve())}

    def source_frame(self, frame):
        for _ in range(3):
            if frame is None:
                return False
            if frame.f_code.co_filename in self.helpers:
                return False
            if frame.f_code.co_filename.startswith(str(self.root) + os.sep):
                return True
            frame = frame.f_back
        return False

    def profile(self, frame, event, arg):
        if not self.active or self.busy or not self.source_frame(frame):
            return
        module = str(getattr(arg, "__module__", "")) if event == "c_call" else str(frame.f_globals.get("__name__", ""))
        name = str(getattr(arg, "__name__", "")) if event == "c_call" else frame.f_code.co_name
        if event == "call":
            names = set(frame.f_code.co_names)
            if ("sys" in names and names.intersection({"argv", "modules"})) or ("os" in names and "environ" in names):
                self.reasons.add("process_context_input")
        if module in {"time", "random", "uuid"} or "random" in module or (name in {"now", "today", "utcnow"} and event == "c_call"):
            self.reasons.add("uncontrolled_time_or_randomness")
        controlled_patch = (frame.f_back is not None and frame.f_back.f_code.co_filename.replace("\\", "/").endswith("/_pytest/monkeypatch.py"))
        if module == "os" and name in {"getenv", "__getitem__"} and not controlled_patch:
            self.reasons.add("environment_input")
        if event == "c_call" and module in {"posix", "nt"} and name in {"stat", "lstat", "access", "readlink", "getcwd"}:
            self.reasons.add("untraced_filesystem_probe")
        if event == "c_call" and module == "_thread" and name == "start_new_thread":
            self.reasons.add("native_threads")
        if event == "c_call" and name in {"getpid", "getppid", "urandom", "id", "hash"}:
            self.reasons.add("process_identity_or_randomness")
        if self.reasons:
            self.active = False
            sys.setprofile(None)

    def audit(self, event, args):
        if not self.active or self.busy:
            return
        self.busy = True
        try:
            if event in {"subprocess.Popen", "os.system", "os.fork", "os.posix_spawn", "socket.connect", "socket.sendto"}:
                self.reasons.add("external_or_child_process_input")
                self.active = False
                sys.setprofile(None)
                return
            if event in {"os.chdir", "os.fchdir"}:
                if event == "os.chdir" and args and isinstance(args[0], (str, bytes)) and Path(os.fsdecode(args[0])).resolve() == Path.cwd():
                    return
                self.reasons.add("working_directory_changed")
                return
            if event not in {"open", "os.listdir", "os.scandir"} or not args:
                return
            if not isinstance(args[0], (str, bytes, os.PathLike)):
                self.reasons.add("unresolved_descriptor")
                return
            path = Path(os.fsdecode(args[0])).absolute()
            if any(path == base or base in path.parents for base in self.runtime):
                return
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                self.reasons.add("symlink_input")
                return
            if str(path).startswith(("/proc/", "/sys/", "/dev/")):
                if str(path) != "/dev/null":
                    self.reasons.add("volatile_system_input")
                return
            if event == "open" and len(args) > 2 and isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_TRUNC):
                return
            relative = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else "@" + str(path)
            if relative.startswith((".git/", ".auto-agents-gate-runtime/")):
                if relative.startswith(".git/"):
                    self.reasons.add("git_metadata_input")
                return
            if relative in self.inputs:
                return
            if not path.exists():
                self.inputs["!" + relative] = "missing"
            elif path.is_dir():
                names = sorted(item.name for item in path.iterdir())
                self.inputs[relative] = "dir:" + hashlib.sha256(json.dumps(names, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            elif path.stat().st_size <= 16 * 1024 * 1024:
                self.inputs[relative] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                self.reasons.add("oversized_input")
        except (OSError, ValueError):
            self.reasons.add("unresolved_input")
        finally:
            self.busy = False

    def start(self):
        if sys.getprofile() is not None:
            self.reasons.add("another_profiler")
            self.active = False
            return
        sys.addaudithook(self.audit)
        sys.setprofile(self.profile)

    def finish(self):
        if self.active and sys.getprofile() != self.profile:
            self.reasons.add("observer_replaced")
        self.active = False
        if sys.getprofile() == self.profile:
            sys.setprofile(None)
        return {"manifest": self.inputs, "complete": bool(self.inputs) and not self.reasons,
                "reasons": sorted(self.reasons)}
