from __future__ import annotations

import subprocess
from pathlib import Path


def _git(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        text=True,
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

