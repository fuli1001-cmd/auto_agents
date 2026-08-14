from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


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


def commit_all_except(project_root: Path, message: str, exclude_prefixes: tuple[str, ...]) -> str:
    normalized_excludes = tuple(
        dict.fromkeys(
            prefix.replace("\\", "/").strip().rstrip("/")
            for prefix in exclude_prefixes
            if prefix.replace("\\", "/").strip().rstrip("/")
        )
    )
    add_args = ["add", "-A", "--", "."]
    add_args.extend(
        f":(top,exclude,literal){prefix}" for prefix in normalized_excludes
    )
    add_process = _git(project_root, *add_args)
    if add_process.returncode != 0:
        raise RuntimeError(add_process.stderr.strip() or "git add failed")

    if normalized_excludes:
        head_process = _git(project_root, "rev-parse", "--verify", "HEAD")
        if head_process.returncode == 0:
            reset_process = _git(
                project_root,
                "reset",
                "-q",
                "HEAD",
                "--",
                *(
                    f":(top,literal){prefix}"
                    for prefix in normalized_excludes
                ),
            )
        else:
            reset_process = _git(
                project_root,
                "rm",
                "-r",
                "--cached",
                "--ignore-unmatch",
                "--",
                *(
                    f":(top,literal){prefix}"
                    for prefix in normalized_excludes
                ),
            )
        if reset_process.returncode != 0:
            raise RuntimeError(
                reset_process.stderr.strip()
                or "git reset excluded paths failed"
            )

    commit_process = _git(project_root, "commit", "-m", message)
    if commit_process.returncode != 0:
        raise RuntimeError(commit_process.stderr.strip() or "git commit failed")

    rev_process = _git(project_root, "rev-parse", "HEAD")
    if rev_process.returncode != 0:
        raise RuntimeError(rev_process.stderr.strip() or "git rev-parse failed")
    return rev_process.stdout.strip()


def commit_only_paths(
    project_root: Path,
    message: str,
    paths: Iterable[str],
) -> str:
    normalized_paths = tuple(
        dict.fromkeys(
            path.replace("\\", "/").strip().rstrip("/")
            for path in paths
            if path.replace("\\", "/").strip().rstrip("/")
        )
    )
    if not normalized_paths:
        return ""
    pathspecs = tuple(
        f":(top,literal){path}" for path in normalized_paths
    )
    add_process = _git(project_root, "add", "-A", "--", *pathspecs)
    if add_process.returncode != 0:
        raise RuntimeError(add_process.stderr.strip() or "git add paths failed")

    diff_process = _git(
        project_root,
        "diff",
        "--cached",
        "--quiet",
        "--",
        *pathspecs,
    )
    if diff_process.returncode == 0:
        return ""
    if diff_process.returncode != 1:
        raise RuntimeError(
            diff_process.stderr.strip() or "git diff paths failed"
        )

    commit_process = _git(
        project_root,
        "commit",
        "--only",
        "-m",
        message,
        "--",
        *pathspecs,
    )
    if commit_process.returncode != 0:
        raise RuntimeError(
            commit_process.stderr.strip() or "git commit paths failed"
        )

    rev_process = _git(project_root, "rev-parse", "HEAD")
    if rev_process.returncode != 0:
        raise RuntimeError(rev_process.stderr.strip() or "git rev-parse failed")
    return rev_process.stdout.strip()


def changed_files(project_root: Path) -> str:
    process = _git(project_root, "status", "--short")
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "git status failed")
    return process.stdout.strip()


def tracked_files(project_root: Path) -> list[str]:
    process = _git(project_root, "ls-files", "-z")
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "git ls-files failed")
    return [item for item in process.stdout.split("\0") if item]


def changed_entries(
    project_root: Path,
    ignored_prefixes: tuple[str, ...] = (".auto-agents/", ".antigravitycli/"),
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


def changed_paths(project_root: Path, ignored_prefixes: tuple[str, ...] = (".auto-agents/", ".antigravitycli/")) -> list[str]:
    return [path for _, path in changed_entries(project_root, ignored_prefixes=ignored_prefixes)]


def worktree_fingerprint(project_root: Path, ignored_prefixes: tuple[str, ...] = (".auto-agents/", ".antigravitycli/")) -> str:
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


def add_worktree(project_root: Path, worktree_path: Path, ref: str = "HEAD") -> None:
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = _git(project_root, "worktree", "add", "--detach", str(worktree_path), ref)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree add failed")


def remove_worktree(project_root: Path, worktree_path: Path, force: bool = True) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    result = _git(project_root, *args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree remove failed")


def list_worktrees(project_root: Path) -> list[str]:
    result = _git(project_root, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git worktree list failed")
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree ") :].strip())
    return paths


def reconcile_managed_worktree(
    project_root: Path,
    worktree_path: Path,
    *,
    managed_root: Path,
    remove_existing: bool = False,
) -> bool:
    """Remove an abandoned worktree only inside an explicit managed root.

    Missing directories may still be registered in Git after a reboot. Existing
    worktrees are preserved by default; a caller holding the corresponding
    worker lease may opt in to removing its exact abandoned path.
    """
    project = Path(project_root).resolve()
    root = Path(managed_root).resolve()
    path = Path(worktree_path).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(
            f"refusing to reconcile worktree outside managed root: {path}"
        ) from error
    if not relative.parts or path in {root, project}:
        raise RuntimeError(f"refusing to reconcile unsafe managed worktree path: {path}")

    registered = {Path(item).resolve() for item in list_worktrees(project)}
    if path in registered:
        if path.exists() and not remove_existing:
            return False
        remove_worktree(project, path, force=True)
    elif path.exists():
        if not remove_existing:
            return False
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    else:
        return False

    remaining = {Path(item).resolve() for item in list_worktrees(project)}
    if path in remaining or path.exists():
        raise RuntimeError(f"managed worktree cleanup did not remove {path}")
    return True


def commit_changed_paths(project_root: Path, commit_sha: str) -> list[str]:
    result = _git(project_root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff-tree failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def update_ref(project_root: Path, ref_name: str, commit_sha: str) -> None:
    result = _git(project_root, "update-ref", ref_name, commit_sha)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git update-ref failed")


def delete_ref(project_root: Path, ref_name: str) -> None:
    result = _git(project_root, "update-ref", "-d", ref_name)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git update-ref -d failed")


def ref_exists(project_root: Path, ref_name: str) -> bool:
    result = _git(project_root, "rev-parse", "--verify", "--quiet", ref_name)
    return result.returncode == 0


def cherry_pick_no_commit(project_root: Path, commit_sha: str) -> None:
    result = _git(project_root, "cherry-pick", "--no-commit", commit_sha)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git cherry-pick failed")


def apply_commit_no_commit_excluding(
    project_root: Path,
    commit_sha: str,
    exclude_prefixes: tuple[str, ...],
) -> None:
    """Apply a commit delta while protecting workspace-local dependency roots."""

    normalized_excludes = tuple(
        dict.fromkeys(
            prefix.replace("\\", "/").strip().rstrip("/")
            for prefix in exclude_prefixes
            if prefix.replace("\\", "/").strip().rstrip("/")
        )
    )
    if not normalized_excludes:
        cherry_pick_no_commit(project_root, commit_sha)
        return

    show_args = ["show", "--format=", "--binary", commit_sha, "--", "."]
    show_args.extend(
        f":(top,exclude,literal){prefix}" for prefix in normalized_excludes
    )
    patch = _git(project_root, *show_args)
    if patch.returncode != 0:
        raise RuntimeError(
            patch.stderr.strip()
            or patch.stdout.strip()
            or "git show for filtered commit failed"
        )
    if not patch.stdout:
        return

    applied = subprocess.run(
        ["git", "apply", "--3way", "--index", "-"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        input=patch.stdout,
        capture_output=True,
    )
    if applied.returncode == 0:
        return
    _git(project_root, "reset", "--merge", "HEAD")
    raise RuntimeError(
        applied.stderr.strip()
        or applied.stdout.strip()
        or "filtered commit apply failed"
    )


def abort_cherry_pick(project_root: Path) -> str:
    result = _git(project_root, "cherry-pick", "--abort")
    if result.returncode != 0:
        return result.stderr.strip() or result.stdout.strip() or "git cherry-pick --abort failed"
    return ""


def hard_reset_clean(
    project_root: Path,
    ref: str = "HEAD",
    preserve_prefixes: tuple[str, ...] = (".auto-agents/", ".antigravitycli/"),
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
