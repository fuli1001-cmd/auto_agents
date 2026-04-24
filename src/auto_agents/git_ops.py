from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def ensure_repo(project_root: Path, auto_init: bool = True) -> None:
    if is_repo(project_root):
        return
    if not auto_init:
        raise RuntimeError(f"{project_root} is not a git repository")
    process = _git(project_root, "init")
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "git init failed")


def is_repo(project_root: Path) -> bool:
    process = _git(project_root, "rev-parse", "--is-inside-work-tree")
    return process.returncode == 0 and process.stdout.strip() == "true"


def working_tree_clean(project_root: Path) -> bool:
    process = _git(project_root, "status", "--porcelain")
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "git status failed")
    return process.stdout.strip() == ""


def require_clean_tree(project_root: Path) -> None:
    if not working_tree_clean(project_root):
        raise RuntimeError("working tree is not clean")


def commit_all(project_root: Path, message: str) -> str:
    add_process = _git(project_root, "add", "-A")
    if add_process.returncode != 0:
        raise RuntimeError(add_process.stderr.strip() or "git add failed")

    commit_process = _git(project_root, "commit", "-m", message)
    if commit_process.returncode != 0:
        raise RuntimeError(commit_process.stderr.strip() or "git commit failed")

    rev_process = _git(project_root, "rev-parse", "HEAD")
    if rev_process.returncode != 0:
        raise RuntimeError(rev_process.stderr.strip() or "git rev-parse failed")
    return rev_process.stdout.strip()


def changed_files(project_root: Path) -> str:
    process = _git(project_root, "status", "--short")
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "git status failed")
    return process.stdout.strip()


def changed_entries(
    project_root: Path,
    ignored_prefixes: tuple[str, ...] = (".auto-agents/",),
) -> list[tuple[str, str]]:
    process = _git(project_root, "status", "--porcelain=v1", "-uall")
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "git status failed")

    entries: list[tuple[str, str]] = []
    for raw_line in process.stdout.splitlines():
        status = raw_line[:2]
        path = raw_line[3:].strip()
        if " -> " in path:
            _, path = path.split(" -> ", 1)
        if any(path.startswith(prefix) for prefix in ignored_prefixes):
            continue
        entries.append((status, path))
    return entries


def changed_paths(project_root: Path, ignored_prefixes: tuple[str, ...] = (".auto-agents/",)) -> list[str]:
    return [path for _, path in changed_entries(project_root, ignored_prefixes=ignored_prefixes)]


def worktree_fingerprint(project_root: Path, ignored_prefixes: tuple[str, ...] = (".auto-agents/",)) -> str:
    hasher = hashlib.sha256()
    for path in changed_paths(project_root, ignored_prefixes=ignored_prefixes):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        file_path = project_root / path
        if file_path.is_file():
            hasher.update(file_path.read_bytes())
        else:
            hasher.update(b"[missing]")
        hasher.update(b"\0")

    return hasher.hexdigest()


def head_ref(project_root: Path) -> str:
    """Return the current HEAD commit hash, or empty string if unavailable."""
    result = _git(project_root, "rev-parse", "HEAD")
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def hard_reset_clean(
    project_root: Path,
    ref: str = "HEAD",
    preserve_prefixes: tuple[str, ...] = (".auto-agents/",),
) -> bool:
    """Hard-reset tracked files to *ref* and remove untracked files.

    ``preserve_prefixes`` keeps orchestrator state (default ``.auto-agents/``)
    intact so the run log/plan/task state survive the rollback.

    Returns True when the reset succeeded, False when the repo is missing or
    the ref cannot be resolved.
    """
    if not is_repo(project_root):
        return False
    target = ref or "HEAD"
    rev = _git(project_root, "rev-parse", "--verify", target)
    if rev.returncode != 0:
        return False
    resolved = rev.stdout.strip() or target

    reset = _git(project_root, "reset", "--hard", resolved)
    if reset.returncode != 0:
        return False

    clean_args = ["clean", "-fd"]
    for prefix in preserve_prefixes:
        clean_args.extend(["-e", prefix.rstrip("/") + "/" if not prefix.endswith("/") else prefix])
    cleaned = _git(project_root, *clean_args)
    return cleaned.returncode == 0
