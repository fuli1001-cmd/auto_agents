"""One classification of owned files, separate from stage write authorization."""

from enum import Enum
from pathlib import Path
import json
import os
import re
import subprocess

POLICY_VERSION = 1

EXCLUDED = (
    ".auto-agents/state/",
    ".auto-agents/runs/",
    ".auto-agents/operator/",
    ".auto-agents/config.json",
    ".auto-agents/.gitignore",
    ".conda/",
    ".venv/",
    "node_modules/",
    "workbench/node_modules/",
    ".data/",
    ".next/",
)


PRIVATE_RUNTIME_ROOTS = {".data", ".tmp", ".tmp-tests"}


def safe_relative_path(name):
    path = Path(name)
    return bool(
        name
        and not path.is_absolute()
        and ".." not in path.parts
        and not any(
            part
            in {
                ".git",
                ".aws",
                ".codex",
                ".claude",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".vite",
            }
            or part == ".env"
            or part.startswith(".env.")
            for part in path.parts
        )
    )


def legacy_product_path(name):
    return safe_relative_path(name) and not any(
        name == p.rstrip("/") or name.startswith(p) for p in EXCLUDED
    )


def product_path(name):
    return legacy_product_path(name) and not (
        {".conda", ".venv", "node_modules"} & set(Path(name).parts)
    )


def private_runtime_path(name):
    parts = Path(name).parts
    return bool(
        safe_relative_path(name)
        and len(parts) > 1
        and parts[0] in PRIVATE_RUNTIME_ROOTS
        and ".auto-agents" not in parts
    )


def private_runtime_link(root, path, tracked):
    """Temp aliases, including dangling browser locks, must stay in runtime."""
    try:
        resolved = path.resolve(strict=False)
        relative = resolved.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        private_runtime_path(relative)
        and relative not in tracked
        and not any(name.startswith(relative + "/") for name in tracked)
    )


def next_runtime_declarations(root, tracked):
    """Recognize ignored, generated Next.js types without allowing source edits."""
    manifests = {
        (root / name).parent for name in tracked if Path(name).name == "package.json"
    }
    result = set()
    for directory in manifests:
        path = directory / "next-env.d.ts"
        relative = path.relative_to(root).as_posix()
        if (
            not product_path(relative)
            or relative in tracked
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        if any(
            parent.is_symlink()
            for parent in (path, *path.parents)
            if parent != root.parent
        ):
            continue
        try:
            manifest = json.loads((directory / "package.json").read_text())
            if not isinstance(manifest, dict):
                continue
            dependencies = {
                **manifest.get("dependencies", {}),
                **manifest.get("devDependencies", {}),
            }
            lines = path.read_text().splitlines()
        except (OSError, ValueError, TypeError):
            continue
        directives = {
            '/// <reference types="next" />',
            '/// <reference types="next/image-types/global" />',
            '/// <reference types="next/navigation-types/compat/navigation" />',
        }
        if "next" not in dependencies or not lines or lines[0] not in directives:
            continue
        if any(
            line not in directives
            and line not in {"", "// NOTE: This file should not be edited"}
            and not re.fullmatch(
                r"// see https://nextjs\.org/docs/(?:app|pages)/api-reference/config/typescript for more information\.",
                line,
            )
            for line in lines
        ):
            continue
        ignored = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--", relative],
            capture_output=True,
        )
        if ignored.returncode == 0:
            result.add(relative)
    return result


def private_runtime_files(root):
    """Untracked local runtime entries, excluding source and escaping links."""
    from .workspace import git

    root = Path(root)
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    files = next_runtime_declarations(root, tracked)
    for name in PRIVATE_RUNTIME_ROOTS:
        directory = root / name
        if directory.is_symlink():
            continue
        for current, dirs, names in os.walk(directory, followlinks=False):
            names = [*names, *(d for d in dirs if (Path(current) / d).is_symlink())]
            dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
            for filename in names:
                path = Path(current) / filename
                relative = path.relative_to(root).as_posix()
                if (
                    relative not in tracked
                    and private_runtime_path(relative)
                    and (
                        private_runtime_link(root, path, tracked)
                        if path.is_symlink()
                        else path.is_file()
                    )
                ):
                    files.add(relative)
    return files


class FileRole(str, Enum):
    PROTECTED = "protected"
    SOURCE = "source"
    RUNTIME = "runtime"
    EVIDENCE = "evidence"


def classify(name, *, runtime=(), evidence=(), linked=False):
    if name in evidence and product_path(name) and not linked:
        return FileRole.EVIDENCE
    if name in runtime:
        return FileRole.RUNTIME
    if not product_path(name) or linked:
        return FileRole.PROTECTED
    if name.startswith(".auto-agents/docs/acceptance/"):
        return FileRole.EVIDENCE
    return FileRole.SOURCE


def file_roles(root, names, *, evidence=()):
    root = Path(root)
    runtime = private_runtime_files(root)
    return {
        name: classify(
            name,
            runtime=runtime,
            evidence=evidence,
            linked=linked_ancestor(root, root / name),
        )
        for name in names
    }


def linked_ancestor(root, path):
    root, path = Path(root), Path(path)
    if not path.is_relative_to(root):
        return True
    while path != root:
        if path.is_symlink():
            return True
        path = path.parent
    return root.is_symlink()
