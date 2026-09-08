"""Conservative command rewriting and repository-bound workflow preflight."""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Callable, Mapping


class ExecutionBindingError(ValueError):
    """A command cannot be executed by the currently bound repository."""


def command_spans(command: str) -> list[tuple[int, int]]:
    """Locate simple commands without changing quoting or shell expansion.

    Nested shell programs are deliberately unsupported; never rewrite them as
    though their strings or substitutions were top-level executables.
    """
    spans = []
    start = index = 0
    quote = ""
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and (char == "`" or command.startswith("$(", index)):
                raise ExecutionBindingError("nested shell expansion requires an explicit verification script")
        elif char in "\"'":
            quote = char
        elif char in "()`":
            raise ExecutionBindingError("nested shell syntax requires an explicit verification script")
        elif char == "#" and (index == 0 or command[index - 1].isspace()):
            spans.append((start, index))
            end = command.find("\n", index)
            if end < 0:
                return spans
            start = index = end + 1
            continue
        elif char in ";|&\n" and not (char == "&" and index and command[index - 1] in "<>"):
            spans.append((start, index))
            while index + 1 < len(command) and command[index + 1] == char:
                index += 1
            start = index + 1
        index += 1
    if quote:
        raise ExecutionBindingError("unclosed shell quote")
    spans.append((start, len(command)))
    return spans


def rewrite_simple_commands(command: str, transform: Callable[[str], str]) -> str:
    spans = command_spans(command)
    for start, end in reversed(spans):
        raw = command[start:end]
        stripped = raw.rstrip()
        command = command[:start] + transform(stripped) + raw[len(stripped):] + command[end:]
    return command


def executable_tokens(command: str) -> list[str]:
    """Peel only known launch wrappers, never arbitrary argument substrings."""
    tokens = shlex.split(command)
    while tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        elif tokens[0] in {"exec", "env"}:
            tokens.pop(0)
        elif Path(tokens[0]).name == "conda" and tokens[1:2] == ["run"]:
            tokens = tokens[2:]
            while tokens and tokens[0].startswith("-"):
                option = tokens.pop(0)
                if option in {"-p", "--prefix", "-n", "--name", "--cwd"}:
                    tokens = tokens[1:]
        else:
            break
    return tokens


def disable_vitest_cache(command: str) -> str:
    def transform(raw: str) -> str:
        args = executable_tokens(raw)
        if not args:
            return raw
        runner = Path(args[0]).name
        if runner in {"npm", "pnpm", "yarn"} and args[1:2] in (["exec"], ["dlx"]):
            args = args[2:]
            if args[:1] == ["--"]:
                args = args[1:]
        elif runner == "npx":
            args = args[1:]
            while args[:1] in (["--yes"], ["-y"], ["--no-install"], ["--"]):
                args = args[1:]
        elif runner in {"pnpm", "yarn"}:
            args = args[1:]
        if not args or Path(args[0]).name != "vitest":
            return raw
        if any(arg in {"--cache", "--no-cache"} or arg.startswith("--cache=") for arg in args[1:]):
            return raw
        return raw + " --no-cache"

    try:
        return rewrite_simple_commands(command, transform)
    except (ExecutionBindingError, ValueError):
        # Do not corrupt shell syntax that this small transformer cannot parse.
        return command


def route_sources(payload: Mapping[str, object]):
    """Walk the same routing envelopes for admission and execution."""
    sources = [payload]
    while sources:
        source = sources.pop()
        yield source
        for key in reversed(("issue_seed", "spec_seed", "fix_disposition")):
            value = source.get(key)
            if isinstance(value, dict):
                sources.append(value)


def repository_binding_error(project_root: Path, payload: Mapping[str, object]) -> str:
    root = project_root.resolve()
    for source in route_sources(payload):
        target = str(source.get("target_repository", "")).strip()
        if target and (root / Path(target).expanduser()).resolve() != root:
            return (
                f"execution_binding_mismatch: requested repository {target}, "
                f"but workflow, write scope, verification and rollback are bound to {root}. "
                "No cross-repository execution channel is bound; use an engine-owned "
                "self-repair workflow or an explicitly authorized target workflow."
            )
    return ""


def validate_verification_binding(command: str, project_root: Path) -> None:
    """Reject known deterministic environment/cwd errors before model work."""
    root = project_root.resolve()
    cwd = root
    try:
        spans = command_spans(command)
    except ExecutionBindingError:
        # General shell programs retain their existing execution semantics.
        return
    for start, end in spans:
        raw = command[start:end]
        args = executable_tokens(raw)
        tokens = shlex.split(raw)
        if args[:1] == ["cd"]:
            destination = (cwd / Path(args[-1]).expanduser()).resolve()
            if len(args) != 2 or "$" in args[1] or (destination != root and root not in destination.parents):
                raise ExecutionBindingError(f"verification cwd must stay bound to {root}; route a different repository explicitly")
            cwd = destination
        # Inspect only a real conda launcher, not a path/string mentioning it.
        launch = list(tokens)
        while launch and (launch[0] in {"exec", "env"} or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", launch[0])):
            launch.pop(0)
        if launch and Path(launch[0]).name == "conda" and launch[1:2] == ["run"]:
            index = 2
            while index < len(launch) and launch[index].startswith("-"):
                option = launch[index]
                value = ""
                if option in {"-p", "--prefix", "--cwd", "-n", "--name"}:
                    index += 1
                    value = launch[index] if index < len(launch) else ""
                elif option.startswith(("--prefix=", "--cwd=")):
                    option, value = option.split("=", 1)
                if option in {"-p", "--prefix"}:
                    prefix = (cwd / Path(value).expanduser()).resolve()
                    if not value or "$" in value or not (prefix / "conda-meta").is_dir():
                        raise ExecutionBindingError(f"verification conda environment does not exist: {prefix}")
                elif option == "--cwd":
                    destination = (cwd / Path(value).expanduser()).resolve()
                    if not value or "$" in value or (destination != root and root not in destination.parents):
                        raise ExecutionBindingError(f"verification conda cwd must stay bound to {root}")
                index += 1
        if args and args[0].startswith(("./.conda/", ".conda/")) and not (cwd / args[0]).is_file():
            raise ExecutionBindingError(f"verification interpreter does not exist: {cwd / args[0]}")
        if args and (Path(args[0]).name.startswith("python") or Path(args[0]).name in {"pytest", "vitest"}):
            for arg in args[1:]:
                path = Path(arg.split("::", 1)[0])
                if path.is_absolute() and path.suffix in {".py", ".js", ".ts", ".tsx"} and root not in path.resolve().parents:
                    raise ExecutionBindingError(f"verification source belongs to a different repository: {path}")


def engine_verification_command(command: str, root: Path, python: str, source_root: Path) -> str:
    """Bind copied engine checks to the candidate and its explicit interpreter."""
    from .self_repair import self_repair_verification_command
    root, source_root = root.resolve(), source_root.resolve()
    def compile_branch(raw):
        original = shlex.split(raw)
        if not original:
            return raw
        if original[0] == "cd":
            if len(original) != 2:
                raise ExecutionBindingError("engine verification needs an explicit cwd")
            destination = Path(original[1])
            if destination == source_root or original[1] == source_root.name and not (root / destination).is_dir():
                return "cd " + shlex.quote(str(root))
            resolved = (root / destination).resolve()
            if resolved != root and root not in resolved.parents:
                raise ExecutionBindingError("engine verification cannot leave its candidate workspace")
            return raw
        args = executable_tokens(raw)
        if not args:
            return raw
        executable = Path(args[0]).name
        if executable != "pytest" and not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
            return raw
        if any("$" in value or "`" in value for value in original):
            raise ExecutionBindingError("dynamic engine verification must use an explicit verification script")
        index = next((i for i in range(len(original)) if original[i:] == args), 0)
        assignments = [value for value in original[:index] if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value)]
        mapped = []
        for arg in args[1:]:
            if arg.startswith(str(source_root) + "/"):
                arg = str(root) + arg[len(str(source_root)):]
            mapped.append(arg)
        if executable == "pytest" or mapped[:2] == ["-m", "pytest"]:
            tests = mapped if executable == "pytest" else mapped[2:]
            for arg in tests:
                path = Path(arg.split("::", 1)[0])
                if path.is_absolute() and root not in path.resolve().parents:
                    raise ExecutionBindingError("engine pytest selector belongs to another repository")
            result = self_repair_verification_command(shlex.join(["python", "-m", "pytest", *tests]), root, python_executable=python)
        else:
            result = shlex.join([python, *mapped])
        if assignments:
            result = "env " + shlex.join([value.replace(str(source_root), str(root)) for value in assignments]) + " " + result
        return result
    return rewrite_simple_commands(command, compile_branch)
