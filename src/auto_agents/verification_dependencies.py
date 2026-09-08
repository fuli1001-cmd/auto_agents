"""Software-prerequisite evidence shared by trusted verification executors.

This module is stdlib-only so the pytest launcher can load its own copy before
adding the writable candidate to sys.path. Detection never chooses an installer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class MissingDependency:
    kind: str
    name: str
    evidence: str = ""

    @property
    def key(self):
        return f"{self.kind}:{self.name}"

    def to_dict(self):
        return asdict(self)


class VerificationDependencyError(RuntimeError):
    """An unsatisfied proof prerequisite, never evidence against a candidate."""

    def __init__(self, dependency, detail, cause=None):
        if isinstance(dependency, str):
            dependency = MissingDependency("executable", dependency)
        self.requirement = dependency
        self.dependency = dependency.name
        prefix = f"verification environment prerequisite {dependency.key}: "
        super().__init__(detail if detail.startswith(prefix) else prefix + detail)
        for name in ("environment_diagnostics", "environment_diagnostics_error"):
            if cause is not None and hasattr(cause, name):
                setattr(self, name, getattr(cause, name))

    def to_result(self):
        from .repair_environment_log import sanitize
        requirement = self.requirement.to_dict()
        requirement["evidence"] = sanitize(requirement["evidence"])
        return {"ok": False, "status": "verification_environment_blocked",
                "failure_domain": "execution_environment", "error": sanitize(str(self)),
                "missing_dependencies": [requirement],
                **{name: getattr(self, name) for name in ("environment_diagnostics", "environment_diagnostics_error")
                   if hasattr(self, name)}}


def _owned_python_module(name, workspace):
    top = name.split(".", 1)[0]
    if top in {"auto_agents", "tests", "conftest"}:
        return True
    if workspace is None:
        return False
    root = Path(workspace)
    return any((base / top).exists() or (base / (top + ".py")).exists()
               for base in (root, root / "src", root / "tests"))


def _requirement(kind, name, evidence, workspace=None):
    name = str(name).strip().strip("'\"`‘’“”")
    if not name or len(name) > 500 or any(c in name for c in "\n\r\x00"):
        return None
    if kind == "python":
        if not re.fullmatch(r"[A-Za-z_][\w.]*", name) or _owned_python_module(name, workspace):
            return None
    elif kind == "node":
        # Relative/absolute JS imports are source files, unless the error names
        # an installed package's node_modules path explicitly.
        if "/node_modules/" in name.replace("\\", "/"):
            name = name.replace("\\", "/").rsplit("/node_modules/", 1)[1]
        elif name.startswith((".", "/")) or re.match(r"^[A-Za-z]:", name):
            return None
        parts = name.split("/")
        name = "/".join(parts[:2]) if name.startswith("@") else parts[0]
        if not re.fullmatch(r"(?:@[\w.-]+/)?[\w.-]+", name):
            return None
    elif kind == "executable":
        normalized = name.replace("\\", "/")
        if "/" in normalized and workspace is not None:
            path = Path(normalized)
            local = not path.is_absolute() or path.is_relative_to(Path(workspace))
            # A missing repository script is an implementation/contract defect.
            # Virtualenv/node_modules executables belong to their environment.
            environment_path = "/node_modules/" in "/" + normalized or bool(
                re.search(r"(?:^|/)(?:\.?venv|\.conda)/", normalized))
            if local and not environment_path:
                return None
        if not re.fullmatch(r"[^\s;&|`$<>]+(?: [^;&|`$<>]+)*", name):
            return None
    elif kind != "shared_library":
        return None
    return MissingDependency(kind, name, evidence.strip()[-1000:])


def exception_dependencies(error, workspace=None):
    """Use actual exception type/traceback to distinguish a program from a file."""
    result, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        dependency = None
        if isinstance(error, ModuleNotFoundError) and error.name:
            dependency = _requirement("python", error.name, f"ModuleNotFoundError: {error}", workspace)
        elif isinstance(error, OSError) and error.filename and error.errno in {errno.ENOENT, errno.EACCES, errno.ENOEXEC}:
            trace = error.__traceback__
            executable_failure = False
            while trace:
                code = trace.tb_frame.f_code
                if Path(code.co_filename).name in {"subprocess.py", "os.py"} and code.co_name in {
                        "_execute_child", "_posix_spawn", "_execvpe", "_spawnvef"}:
                    cwd = trace.tb_frame.f_locals.get("cwd")
                    # Popen reports a failed chdir through this same frame.
                    # A missing working directory is not missing software.
                    executable_failure = cwd is None or str(error.filename) != str(cwd)
                    break
                trace = trace.tb_next
            if executable_failure:
                dependency = _requirement("executable", error.filename, f"{type(error).__name__}: {error}", workspace)
        if dependency:
            result.append(dependency)
        # A caught/expected error in __context__ does not prove that the final
        # assertion failed because of that dependency. Follow explicit causes.
        error = error.__cause__
    return result


def detect_verification_dependencies(output, *, workspace=None, structured=None):
    found = {}

    def add(kind, name, evidence):
        dependency = _requirement(kind, name, evidence, workspace)
        if dependency:
            found.setdefault(dependency.key, dependency)

    for record in structured or ():
        if isinstance(record, dict):
            add(record.get("kind", ""), record.get("name", ""), record.get("evidence", ""))
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(output))
    lines = text.splitlines()
    subprocess_trace = False
    for line in lines:
        if re.match(r"^(?:_{3,} .* _{3,}|={3,}.*={3,})\s*$", line):
            subprocess_trace = False
        if re.search(r"subprocess\.py(?::\d+|[\"'], line)|in _execute_child\b", line):
            subprocess_trace = True
        error = re.sub(r"^\s*E\s+", "", line).strip()
        match = re.match(r"(?:[\w.]*VerificationDependencyError:\s*)?verification environment prerequisite (executable|python|node|shared_library):(.+?): ", error)
        if match:
            add(match[1], match[2], error)
        match = re.match(r"(?:ModuleNotFoundError|ImportError): No module named ['\"]([^'\"]+)", error)
        if match:
            add("python", match[1], error)
        match = re.match(r"(?:importlib\.metadata\.)?PackageNotFoundError: No package metadata was found for ([\w.-]+)", error)
        if match:
            add("python", match[1].replace("-", "_"), error)
        match = re.match(r"(?:AssertionError: )?Error(?: \[\w+\])?: Cannot find (?:package|module) ['\"]([^'\"]+)", error)
        if match:
            add("node", match[1], error)
        match = re.match(r"(?:RuntimeError: )?([\w.+-]+) integration dependency is missing:", error)
        if match:
            add("executable", match[1].lower(), error)
        # Libraries often check PATH themselves, then raise without a chained
        # OS exception. Recognize their executable-oriented wording, not names
        # of individual products (and not a generic "file not found" message).
        message = re.sub(r"^(?:[\w.]*(?:Error|NotFound|NotInstalled)):\s*", "", error)
        for pattern in (
            r"^No ['\"]?([\w.+-]+)['\"]? (?:exe|executable|binary|program) (?:could be|was|is)?\s*(?:found|available)\b",
            r"^(?:Could not|Cannot|Unable to) find (?:executable|binary|program|command) ['\"]([^'\"]+)['\"]",
            r"^(?:Executable|Binary|Program|Command) ['\"]([^'\"]+)['\"] (?:was |is )?(?:not found|missing|not installed|unavailable)\b",
            r"^['\"]?([\w.+-]+)['\"]? (?:executable|binary|program|command) (?:was |is )?(?:not found|missing|not installed|unavailable)\b",
        ):
            match = re.match(pattern, message, re.I)
            if match:
                add("executable", match[1], error)
        if re.match(r"(?:[\w.]*)(?:Executable|Binary|Program|Command)NotFound(?:Error)?:", error):
            match = re.search(r"failed to execute (?:PosixPath\(|WindowsPath\()?['\"]([^'\"]+)['\"]", error)
            if match:
                add("executable", match[1], error)
        match = re.match(r"(?:.*(?:ba|da|z|k)?sh: (?:(?:line )?\d+: )?)?([^:]+): (?:command )?not found$", error)
        if match:
            add("executable", match[1], error)
        match = re.match(r".*(?:ba|da|z|k)?sh: (?:(?:line )?\d+: )?([^:]+): (?:Permission denied|cannot execute.*)$", error)
        if match:
            add("executable", match[1], error)
        match = re.match(r"(?:/[^:]+/)?env: ['‘\"]([^'’\"]+)['’\"]: No such file or directory", error)
        if match:
            add("executable", match[1], error)
        match = re.match(r"['\"]([^'\"]+)['\"] is not recognized as an internal or external command", error, re.I)
        if match:
            add("executable", match[1], error)
        match = re.search(r"Executable doesn't exist at\s+(.+)$", error)
        if match:
            add("executable", match[1], error)
        match = re.search(r"(?:error while loading shared libraries: |(?:OSError|ImportError): )([^\s:]+): cannot open shared object file", error)
        if match:
            add("shared_library", match[1], error)
        # A bare FileNotFoundError may concern an input fixture. Only a visible
        # subprocess traceback, or structured exception evidence, identifies it
        # as an executable failure.
        match = re.match(r"(?:FileNotFoundError|PermissionError|OSError): \[Errno (?:2|8|13)\] [^:\n]+: ['\"]([^'\"]+)['\"]", error)
        if match:
            if structured is None and subprocess_trace:
                add("executable", match[1], error)
            subprocess_trace = False
    if re.search(r"npm (?:ERR!|error) code ENOTCACHED", text, re.I):
        for match in re.finditer(r"request to (https?://\S+) failed: [^\n]*only-if-cached", text, re.I):
            name = unquote(urlsplit(match[1]).path).strip("/").split("/-/", 1)[0]
            add("node", name, match[0])
    return list(found.values())
