from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Iterable, Mapping


CHECKPOINT_APPLICATION_TRANSACTION_VERSION = 2
_CHECKPOINT_ENGINE_PATHS = (".auto-agents", ".antigravitycli")


def _git(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def _git_bytes(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Read-only observations must not rewrite the index stat cache.  Otherwise
    # merely checking a transaction could invalidate its exact index image.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        capture_output=True,
        env=env,
    )


def _immutable_git_bytes(
    project_root: Path,
    *args: str,
) -> subprocess.CompletedProcess:
    """Read physical Git objects without honoring replacement refs."""

    return _git_bytes(project_root, "--no-replace-objects", *args)


def normalize_repository_exclusions(paths: Iterable[str]) -> tuple[str, ...]:
    """Return unique, safe repository-relative exclusion prefixes."""

    normalized: list[str] = []
    for raw_path in paths:
        raw = str(raw_path).replace("\\", "/").strip()
        if not raw:
            continue
        path = PurePosixPath(raw)
        value = path.as_posix().rstrip("/")
        if (
            not value
            or value == "."
            or path.is_absolute()
            or bool(PureWindowsPath(raw).drive)
            or ".." in path.parts
        ):
            raise ValueError(
                "repository exclusions must be safe repository-relative paths"
            )
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def repository_path_is_excluded(
    path: str,
    exclude_prefixes: Iterable[str],
) -> bool:
    """Return whether *path* is equal to or below an exclusion prefix."""

    normalized_path = normalize_repository_exclusions((path,))
    if not normalized_path:
        return False
    candidate = normalized_path[0]
    return any(
        candidate == prefix or candidate.startswith(prefix + "/")
        for prefix in normalize_repository_exclusions(exclude_prefixes)
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
    normalized_excludes = normalize_repository_exclusions(exclude_prefixes)
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
    *,
    trailers: Iterable[str] = (),
) -> str:
    requested_paths = tuple(
        dict.fromkeys(
            path.replace("\\", "/").strip().rstrip("/")
            for path in paths
            if path.replace("\\", "/").strip().rstrip("/")
        )
    )
    normalized_paths = tuple(
        path
        for path in requested_paths
        if (project_root / path).exists()
        or bool(_git(project_root, "ls-files", "--", path).stdout.strip())
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

    commit_args = ["commit", "--only", "-m", message]
    trailer_lines = [str(item).strip() for item in trailers if str(item).strip()]
    if trailer_lines:
        commit_args.extend(["-m", "\n".join(trailer_lines)])
    commit_args.extend(["--", *pathspecs])
    commit_process = _git(project_root, *commit_args)
    if commit_process.returncode != 0:
        raise RuntimeError(
            commit_process.stderr.strip() or "git commit paths failed"
        )

    rev_process = _git(project_root, "rev-parse", "HEAD")
    if rev_process.returncode != 0:
        raise RuntimeError(rev_process.stderr.strip() or "git rev-parse failed")
    return rev_process.stdout.strip()


def amend_only_paths(project_root: Path, paths: Iterable[str]) -> str:
    """Amend HEAD with exact paths while preserving unrelated staged changes."""

    requested = tuple(
        dict.fromkeys(
            path.replace("\\", "/").strip().rstrip("/")
            for path in paths
            if path.replace("\\", "/").strip().rstrip("/")
        )
    )
    normalized = tuple(
        path
        for path in requested
        if (project_root / path).exists()
        or bool(_git(project_root, "ls-files", "--", path).stdout.strip())
    )
    if not normalized:
        return head_ref(project_root)
    pathspecs = tuple(f":(top,literal){path}" for path in normalized)
    add = _git(project_root, "add", "-A", "--", *pathspecs)
    if add.returncode != 0:
        raise RuntimeError(add.stderr.strip() or "git add amend paths failed")
    diff = _git(project_root, "diff", "--cached", "--quiet", "--", *pathspecs)
    if diff.returncode == 0:
        return head_ref(project_root)
    if diff.returncode != 1:
        raise RuntimeError(diff.stderr.strip() or "git diff amend paths failed")
    amend = _git(
        project_root,
        "commit",
        "--amend",
        "--no-edit",
        "--only",
        "--",
        *pathspecs,
    )
    if amend.returncode != 0:
        raise RuntimeError(amend.stderr.strip() or "git amend paths failed")
    return head_ref(project_root)


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
    process = _git(project_root, "status", "--porcelain=v1", "-z", "-uall")
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "git status failed")

    entries: list[tuple[str, str]] = []
    records = iter(process.stdout.split("\0"))
    for record in records:
        if not record:
            continue
        status = record[:2]
        path = record[3:]
        if "R" in status or "C" in status:
            # Porcelain -z emits the destination first, then the original path.
            next(records, None)
        if any(path.startswith(prefix) for prefix in ignored_prefixes):
            continue
        entries.append((status, path))
    return entries


def is_untracked_vim_swap(status: str, path: str) -> bool:
    """Return true only for an untracked Vim swap/recovery artifact."""

    if str(status).strip() != "??":
        return False
    normalized = str(path).replace("\\", "/").strip().lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        name.startswith(".")
        and len(name) > 4
        and name[-4:-1] == ".sw"
        and name[-1] in "abcdefghijklmnop"
    )


def changed_paths(project_root: Path, ignored_prefixes: tuple[str, ...] = (".auto-agents/", ".antigravitycli/")) -> list[str]:
    return [path for _, path in changed_entries(project_root, ignored_prefixes=ignored_prefixes)]


def changed_line_count(project_root: Path, paths: Iterable[str]) -> int | None:
    """Count final additions and deletions; None means binary or unreadable changes.

    Compare HEAD directly with the worktree to avoid counting staged changes
    twice. Parse the whole diff so rename sources remain visible to Git, then
    filter by destination. Untracked files (and unborn repositories) are additions.
    """
    selected = set(paths)
    if not selected:
        return 0
    total = 0
    try:
        if head_ref(project_root):
            diff = _git_bytes(
                project_root, "diff", "--numstat", "-z", "--no-ext-diff",
                "--no-textconv", "--find-renames", "HEAD", "--",
            )
            if diff.returncode:
                return None
            records = iter(diff.stdout.split(b"\0"))
            for record in records:
                if not record:
                    continue
                added, deleted, path = record.split(b"\t", 2)
                if not path:  # -z rename records have separate source/destination fields.
                    next(records)
                    path = next(records)
                if os.fsdecode(path) not in selected:
                    continue
                if added == b"-" or deleted == b"-":
                    return None
                total += int(added) + int(deleted)
            untracked = _git_bytes(project_root, "ls-files", "--others", "--exclude-standard", "-z")
            if untracked.returncode:
                return None
            additions = selected.intersection(os.fsdecode(p) for p in untracked.stdout.split(b"\0") if p)
        else:
            additions = selected
        for path in additions:
            file_path = project_root / path
            content = (
                os.fsencode(os.readlink(file_path))
                if file_path.is_symlink() else file_path.read_bytes()
            )
            if b"\0" in content:
                return None
            total += content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))
    except (OSError, ValueError, StopIteration):
        return None
    return total


def worktree_fingerprint(project_root: Path, ignored_prefixes: tuple[str, ...] = (".auto-agents/", ".antigravitycli/")) -> str:
    hasher = hashlib.sha256()
    for path in changed_paths(project_root, ignored_prefixes=ignored_prefixes):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        file_path = project_root / path
        if file_path.is_symlink():
            hasher.update(b"symlink\0")
            hasher.update(os.fsencode(os.readlink(file_path)))
        elif file_path.is_file():
            hasher.update(
                b"executable\0" if file_path.stat().st_mode & stat.S_IXUSR else b"file\0"
            )
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
    *,
    include_paths: Iterable[str] = (),
) -> None:
    """Apply a commit delta while protecting workspace-local dependency roots."""

    normalized_excludes = normalize_repository_exclusions(exclude_prefixes)
    normalized_includes = normalize_repository_exclusions(include_paths)
    if not normalized_excludes and not normalized_includes:
        cherry_pick_no_commit(project_root, commit_sha)
        return

    show_args = ["show", "--format=", "--binary", commit_sha, "--"]
    show_args.extend(
        f":(top,literal){path}" for path in normalized_includes
    )
    if not normalized_includes:
        show_args.append(".")
    show_args.extend(
        f":(top,exclude,literal){prefix}" for prefix in normalized_excludes
    )
    patch = _git_bytes(project_root, *show_args)
    if patch.returncode != 0:
        raise RuntimeError(
            patch.stderr.decode("utf-8", errors="replace").strip()
            or patch.stdout.decode("utf-8", errors="replace").strip()
            or "git show for filtered commit failed"
        )
    if not patch.stdout:
        return

    applied = subprocess.run(
        ["git", "apply", "--3way", "--index", "-"],
        cwd=str(project_root),
        input=patch.stdout,
        capture_output=True,
    )
    if applied.returncode == 0:
        return
    raise RuntimeError(
        applied.stderr.decode("utf-8", errors="replace").strip()
        or applied.stdout.decode("utf-8", errors="replace").strip()
        or "filtered commit apply failed"
    )


def _checkpoint_payload_fingerprint(payload: object) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _resolve_commit(project_root: Path, ref_name: str) -> str:
    result = _git_bytes(
        project_root,
        "rev-parse",
        "--verify",
        f"{ref_name}^{{commit}}",
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or f"checkpoint ref is not a commit: {ref_name}"
        )
    return result.stdout.decode("ascii").strip()


def _single_commit_parent(project_root: Path, commit_sha: str) -> str:
    result = _git_bytes(
        project_root,
        "rev-list",
        "--parents",
        "-n",
        "1",
        commit_sha,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or "checkpoint parent lookup failed"
        )
    revisions = result.stdout.decode("ascii").strip().split()
    if len(revisions) != 2:
        raise RuntimeError(
            "checkpoint application requires a single-parent retained commit"
        )
    return revisions[1]


def _decode_git_path(raw_path: bytes) -> str:
    try:
        decoded = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(
            "checkpoint paths must be valid UTF-8 repository paths"
        ) from error
    normalized = normalize_repository_exclusions((decoded,))
    if len(normalized) != 1:
        raise RuntimeError("checkpoint contains an empty repository path")
    return normalized[0]


def _commit_delta_paths(
    project_root: Path,
    parent_sha: str,
    commit_sha: str,
) -> tuple[str, ...]:
    result = _git_bytes(
        project_root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        parent_sha,
        commit_sha,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or "checkpoint path manifest lookup failed"
        )
    return tuple(
        dict.fromkeys(
            _decode_git_path(raw_path)
            for raw_path in result.stdout.split(b"\0")
            if raw_path
        )
    )


def _legacy_proof_path(raw_path: bytes) -> str:
    """Decode one canonical repository path for legacy ownership proof."""

    try:
        decoded = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(
            "legacy checkpoint paths must be valid UTF-8 repository paths"
        ) from error
    normalized = normalize_repository_exclusions((decoded,))
    if (
        len(normalized) != 1
        or normalized[0] != decoded
        or any(
            part.casefold() == ".git"
            for part in PurePosixPath(decoded).parts
        )
    ):
        raise RuntimeError(
            "legacy checkpoint contains a non-canonical or unsafe repository path"
        )
    return normalized[0]


def _legacy_commit_parents(
    project_root: Path,
    commit_sha: str,
) -> tuple[str, ...]:
    """Return parent IDs recorded in the physical retained commit object."""

    result = _immutable_git_bytes(
        project_root,
        "cat-file",
        "commit",
        commit_sha,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or "legacy checkpoint parent lookup failed"
        )
    raw_headers, separator, _ = result.stdout.partition(b"\n\n")
    header_lines = raw_headers.split(b"\n")
    object_id_length = len(commit_sha)
    hexadecimal = frozenset(b"0123456789abcdef")
    if not separator or not header_lines:
        raise RuntimeError("legacy checkpoint commit headers are invalid")
    tree_prefix = b"tree "
    tree_id = header_lines[0][len(tree_prefix) :]
    if (
        not header_lines[0].startswith(tree_prefix)
        or len(tree_id) != object_id_length
        or any(byte not in hexadecimal for byte in tree_id)
    ):
        raise RuntimeError("legacy checkpoint commit tree header is invalid")

    parents: list[str] = []
    parent_headers_closed = False
    parent_prefix = b"parent "
    for header_line in header_lines[1:]:
        if not header_line.startswith(parent_prefix):
            parent_headers_closed = True
            continue
        if parent_headers_closed:
            raise RuntimeError(
                "legacy checkpoint commit parent headers are ambiguous"
            )
        parent_id = header_line[len(parent_prefix) :]
        if (
            len(parent_id) != object_id_length
            or any(byte not in hexadecimal for byte in parent_id)
        ):
            raise RuntimeError(
                "legacy checkpoint commit parent header is invalid"
            )
        parents.append(parent_id.decode("ascii"))
    return tuple(parents)


def _legacy_commit_delta_paths(
    project_root: Path,
    parent_sha: str,
    commit_sha: str,
) -> tuple[str, ...]:
    args = [
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "--no-renames",
        "-r",
        "-z",
    ]
    if parent_sha:
        args.extend((parent_sha, commit_sha))
    else:
        args.extend(("--root", commit_sha))
    result = _immutable_git_bytes(project_root, *args)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or "legacy checkpoint path manifest lookup failed"
        )
    paths = tuple(
        _legacy_proof_path(raw_path)
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    )
    if len(paths) != len(set(paths)):
        raise RuntimeError("legacy checkpoint commit path manifest is ambiguous")
    return paths


def _commit_path_entry(
    project_root: Path,
    ref_name: str,
    path: str,
    *,
    include_content_bytes: bool = False,
    immutable_object_read: bool = False,
) -> Dict[str, object]:
    git_read = _immutable_git_bytes if immutable_object_read else _git_bytes
    result = git_read(
        project_root,
        "ls-tree",
        "--full-tree",
        "-z",
        ref_name,
        "--",
        f":(top,literal){path}",
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or f"checkpoint tree lookup failed for {path}"
        )
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    if not entries:
        return {
            "kind": "missing",
            "mode": "000000",
            "object_id": "",
            "size": 0,
            "content_sha256": "",
        }
    if len(entries) != 1:
        raise RuntimeError(f"checkpoint tree lookup was ambiguous for {path}")
    metadata, separator, raw_path = entries[0].partition(b"\t")
    if not separator or _decode_git_path(raw_path) != path:
        raise RuntimeError(f"checkpoint tree lookup returned the wrong path for {path}")
    parts = metadata.decode("ascii").split()
    if len(parts) != 3:
        raise RuntimeError(f"checkpoint tree metadata is invalid for {path}")
    mode, object_type, object_id = parts
    kind = {
        "120000": "symlink",
        "160000": "gitlink",
    }.get(mode, "directory" if object_type == "tree" else "file")
    content = b""
    if object_type == "blob":
        blob = git_read(project_root, "cat-file", "blob", object_id)
        if blob.returncode != 0:
            raise RuntimeError(
                blob.stderr.decode("utf-8", errors="replace").strip()
                or f"checkpoint blob lookup failed for {path}"
            )
        content = blob.stdout
    entry: Dict[str, object] = {
        "kind": kind,
        "mode": mode,
        "object_type": object_type,
        "object_id": object_id,
        "size": len(content),
        "content_sha256": (
            hashlib.sha256(content).hexdigest() if object_type == "blob" else ""
        ),
    }
    if include_content_bytes and object_type == "blob":
        entry["_content_bytes"] = content
    return entry


def _retained_path_manifest(
    project_root: Path,
    parent_sha: str,
    commit_sha: str,
    paths: Iterable[str],
) -> list[Dict[str, object]]:
    manifest: list[Dict[str, object]] = []
    for path in paths:
        before = _commit_path_entry(project_root, parent_sha, path)
        after = _commit_path_entry(project_root, commit_sha, path)
        if before["kind"] == "missing":
            change = "addition"
        elif after["kind"] == "missing":
            change = "deletion"
        elif (
            before["kind"] != after["kind"]
            or before["mode"] != after["mode"]
        ):
            change = "type_change"
        else:
            change = "modification"
        manifest.append(
            {
                "path": path,
                "change": change,
                "before": before,
                "after": after,
            }
        )
    return manifest


def _safe_worktree_path(project_root: Path, path: str) -> Path:
    normalized = normalize_repository_exclusions((path,))
    if len(normalized) != 1:
        raise RuntimeError("checkpoint path is empty")
    root = Path(project_root).resolve()
    candidate = root / normalized[0]
    current = root
    for component in PurePosixPath(normalized[0]).parts[:-1]:
        current = current / component
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(current_stat.st_mode):
            raise RuntimeError(
                f"checkpoint path traverses a symlinked parent: {path}"
            )
        if not stat.S_ISDIR(current_stat.st_mode):
            raise RuntimeError(
                f"checkpoint path traverses a non-directory parent: {path}"
            )
    return candidate


def _coalesced_worktree_roots(paths: Iterable[str]) -> tuple[str, ...]:
    """Return the shallowest paths whose snapshots cover every input path."""

    normalized = normalize_repository_exclusions(paths)
    roots: list[str] = []
    for path in sorted(
        normalized,
        key=lambda item: (len(PurePosixPath(item).parts), item),
    ):
        if any(path == root or path.startswith(root + "/") for root in roots):
            continue
        roots.append(path)
    return tuple(roots)


def _worktree_snapshot_roots(
    project_root: Path,
    paths: Iterable[str],
) -> tuple[str, ...]:
    """Include the first absent ancestor that checkpoint application may create."""

    project = Path(project_root).resolve()
    roots: list[str] = []
    for path in _coalesced_worktree_roots(paths):
        _safe_worktree_path(project_root, path)
        selected = path
        current = project
        components: list[str] = []
        for component in PurePosixPath(path).parts:
            components.append(component)
            current = current / component
            try:
                current.lstat()
            except FileNotFoundError:
                selected = PurePosixPath(*components).as_posix()
                break
        roots.append(selected)
    return _coalesced_worktree_roots(roots)


def _snapshot_node(path: Path, relative: str) -> list[Dict[str, object]]:
    details = path.lstat()
    mode = stat.S_IMODE(details.st_mode)
    if stat.S_ISREG(details.st_mode):
        content = path.read_bytes()
        return [
            {
                "path": relative,
                "kind": "file",
                "mode": mode,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
    if stat.S_ISLNK(details.st_mode):
        target = os.fsencode(os.readlink(path))
        return [
            {
                "path": relative,
                "kind": "symlink",
                "mode": mode,
                "content_base64": base64.b64encode(target).decode("ascii"),
                "content_sha256": hashlib.sha256(target).hexdigest(),
            }
        ]
    if stat.S_ISDIR(details.st_mode):
        entries: list[Dict[str, object]] = [
            {"path": relative, "kind": "directory", "mode": mode}
        ]
        children = sorted(
            path.iterdir(),
            key=lambda child: os.fsencode(child.name),
        )
        for child in children:
            child_relative = (
                f"{relative}/{child.name}" if relative else child.name
            )
            entries.extend(_snapshot_node(child, child_relative))
        return entries
    raise RuntimeError(f"checkpoint path has an unsupported file kind: {path}")


def _capture_worktree_path(
    project_root: Path,
    path: str,
) -> Dict[str, object]:
    candidate = _safe_worktree_path(project_root, path)
    try:
        entries = _snapshot_node(candidate, "")
    except FileNotFoundError:
        payload: Dict[str, object] = {"kind": "missing", "entries": []}
    else:
        payload = {
            "kind": str(entries[0]["kind"]),
            "entries": entries,
        }
    payload["fingerprint"] = _checkpoint_payload_fingerprint(payload)
    return payload


def _validate_worktree_snapshot(snapshot: Mapping[str, object]) -> None:
    payload = dict(snapshot)
    expected_fingerprint = str(payload.pop("fingerprint", ""))
    if not expected_fingerprint or (
        _checkpoint_payload_fingerprint(payload) != expected_fingerprint
    ):
        raise RuntimeError("checkpoint worktree snapshot fingerprint is invalid")
    raw_entries = payload.get("entries", [])
    if not isinstance(raw_entries, list):
        raise RuntimeError("checkpoint worktree snapshot entries are invalid")
    root_kind = str(payload.get("kind", ""))
    if root_kind == "missing":
        if raw_entries:
            raise RuntimeError("checkpoint missing path snapshot has entries")
        return
    if not raw_entries or not isinstance(raw_entries[0], dict):
        raise RuntimeError("checkpoint worktree snapshot root is missing")
    if (
        str(raw_entries[0].get("path", ""))
        or str(raw_entries[0].get("kind", "")) != root_kind
    ):
        raise RuntimeError("checkpoint worktree snapshot root is inconsistent")
    seen_paths: set[str] = set()
    entry_kinds: Dict[str, str] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise RuntimeError("checkpoint worktree snapshot entry is invalid")
        relative = str(raw_entry.get("path", ""))
        if relative in seen_paths:
            raise RuntimeError("checkpoint worktree snapshot path is duplicated")
        seen_paths.add(relative)
        if relative:
            normalized = normalize_repository_exclusions((relative,))
            if len(normalized) != 1 or normalized[0] != relative:
                raise RuntimeError(
                    "checkpoint worktree snapshot contains an unsafe path"
                )
        kind = str(raw_entry.get("kind", ""))
        if kind not in {"file", "symlink", "directory"}:
            raise RuntimeError("checkpoint worktree snapshot kind is invalid")
        entry_kinds[relative] = kind
        try:
            mode = int(raw_entry.get("mode", -1))
        except (TypeError, ValueError) as error:
            raise RuntimeError("checkpoint worktree snapshot mode is invalid") from error
        if mode < 0 or mode > 0o7777:
            raise RuntimeError("checkpoint worktree snapshot mode is invalid")
        if kind in {"file", "symlink"}:
            try:
                content = base64.b64decode(
                    str(raw_entry.get("content_base64", "")),
                    validate=True,
                )
            except (ValueError, TypeError) as error:
                raise RuntimeError(
                    "checkpoint worktree snapshot content is invalid"
                ) from error
            if hashlib.sha256(content).hexdigest() != str(
                raw_entry.get("content_sha256", "")
            ):
                raise RuntimeError(
                    "checkpoint worktree snapshot content fingerprint is invalid"
                )
    for relative in seen_paths:
        if relative and entry_kinds.get("") != "directory":
            raise RuntimeError(
                "checkpoint worktree snapshot hierarchy is invalid"
            )
        parent = PurePosixPath(relative).parent
        while parent.as_posix() not in {"", "."}:
            if entry_kinds.get(parent.as_posix()) != "directory":
                raise RuntimeError(
                    "checkpoint worktree snapshot hierarchy is invalid"
                )
            parent = parent.parent


def _remove_worktree_path(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _restore_one_worktree_snapshot(
    project_root: Path,
    path: str,
    snapshot: Mapping[str, object],
) -> None:
    if str(snapshot.get("kind", "")) == "missing":
        return
    root = _safe_worktree_path(project_root, path)
    raw_entries = snapshot.get("entries", [])
    entries = [dict(item) for item in raw_entries if isinstance(item, dict)]
    for entry in entries:
        if str(entry.get("kind", "")) != "directory":
            continue
        relative = str(entry.get("path", ""))
        target = root if not relative else root / PurePosixPath(relative)
        target.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        kind = str(entry.get("kind", ""))
        if kind == "directory":
            continue
        relative = str(entry.get("path", ""))
        target = root if not relative else root / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = base64.b64decode(str(entry.get("content_base64", "")))
        if kind == "file":
            target.write_bytes(content)
            os.chmod(target, int(entry.get("mode", 0o644)))
        elif kind == "symlink":
            os.symlink(os.fsdecode(content), target)
    directories = [
        entry for entry in entries if str(entry.get("kind", "")) == "directory"
    ]
    for entry in reversed(directories):
        relative = str(entry.get("path", ""))
        target = root if not relative else root / PurePosixPath(relative)
        os.chmod(target, int(entry.get("mode", 0o755)))


def _restore_worktree_snapshots(
    project_root: Path,
    snapshots: Mapping[str, object],
) -> None:
    roots = _coalesced_worktree_roots(str(path) for path in snapshots)
    for path in roots:
        raw_snapshot = snapshots.get(path)
        if not isinstance(raw_snapshot, Mapping):
            raise RuntimeError(f"checkpoint prestate is missing path {path}")
        _validate_worktree_snapshot(raw_snapshot)
        _safe_worktree_path(project_root, path)
    for path in sorted(roots, key=lambda item: item.count("/"), reverse=True):
        _remove_worktree_path(_safe_worktree_path(project_root, path))
    for path in roots:
        raw_snapshot = snapshots[path]
        assert isinstance(raw_snapshot, Mapping)
        _restore_one_worktree_snapshot(project_root, path, raw_snapshot)


def _worktree_snapshot_fingerprints(
    project_root: Path,
    snapshots: Mapping[str, object],
) -> Dict[str, str]:
    return {
        path: str(_capture_worktree_path(project_root, path)["fingerprint"])
        for path in sorted(str(item) for item in snapshots)
    }


def _index_path(project_root: Path) -> Path:
    result = _git_bytes(project_root, "rev-parse", "--git-path", "index")
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or "git index path lookup failed"
        )
    raw_path = os.fsdecode(result.stdout.rstrip(b"\r\n"))
    index_path = Path(raw_path)
    if not index_path.is_absolute():
        index_path = Path(project_root) / index_path
    return index_path


def _capture_index_image(project_root: Path) -> Dict[str, object]:
    path = _index_path(project_root)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.exists():
        raise RuntimeError("checkpoint transaction refused a locked git index")
    if not path.exists():
        return {
            "present": False,
            "mode": 0,
            "content_base64": "",
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    content = path.read_bytes()
    return {
        "present": True,
        "mode": stat.S_IMODE(path.stat().st_mode),
        "content_base64": base64.b64encode(content).decode("ascii"),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _validated_index_content(snapshot: Mapping[str, object]) -> bytes:
    if not isinstance(snapshot.get("present"), bool):
        raise RuntimeError("checkpoint index image presence marker is invalid")
    try:
        mode = int(snapshot.get("mode", -1))
    except (TypeError, ValueError) as error:
        raise RuntimeError("checkpoint index image mode is invalid") from error
    if mode < 0 or mode > 0o7777:
        raise RuntimeError("checkpoint index image mode is invalid")
    try:
        content = base64.b64decode(
            str(snapshot.get("content_base64", "")),
            validate=True,
        )
    except (ValueError, TypeError) as error:
        raise RuntimeError("checkpoint index image is invalid") from error
    if hashlib.sha256(content).hexdigest() != str(snapshot.get("sha256", "")):
        raise RuntimeError("checkpoint index image fingerprint is invalid")
    if not bool(snapshot.get("present")) and content:
        raise RuntimeError("checkpoint absent index image contains data")
    return content


def _restore_index_image(
    project_root: Path,
    snapshot: Mapping[str, object],
) -> None:
    content = _validated_index_content(snapshot)
    path = _index_path(project_root)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.exists():
        raise RuntimeError("checkpoint transaction refused a locked git index")
    if not bool(snapshot.get("present")):
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.checkpoint-",
        dir=str(path.parent),
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), int(snapshot.get("mode", 0o644)))
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _index_entries_for_path(
    project_root: Path,
    path: str,
    *,
    immutable_git_read: bool = False,
) -> list[Dict[str, str]]:
    git_read = _immutable_git_bytes if immutable_git_read else _git_bytes
    result = git_read(
        project_root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        f":(top,literal){path}",
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or f"git index lookup failed for {path}"
        )
    entries: list[Dict[str, str]] = []
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        metadata, separator, raw_path = raw_entry.partition(b"\t")
        observed_path = _decode_git_path(raw_path) if separator else ""
        if observed_path.startswith(path + "/"):
            # A literal pathspec names the directory recursively.  The index
            # itself has no directory entries, so descendants are not an
            # observation of the requested path.
            continue
        if not separator or observed_path != path:
            raise RuntimeError(f"git index lookup returned the wrong path for {path}")
        parts = metadata.decode("ascii").split()
        if len(parts) != 3:
            raise RuntimeError(f"git index metadata is invalid for {path}")
        entries.append(
            {"mode": parts[0], "object_id": parts[1], "stage": parts[2]}
        )
    return entries


def _path_is_filtered(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _semantic_index_fingerprint(
    project_root: Path,
    excluded_prefixes: tuple[str, ...],
) -> str:
    staged = _git_bytes(project_root, "ls-files", "--stage", "-z")
    flags = _git_bytes(project_root, "ls-files", "-v", "-z")
    if staged.returncode != 0 or flags.returncode != 0:
        message = staged.stderr or flags.stderr
        raise RuntimeError(
            message.decode("utf-8", errors="replace").strip()
            or "git index fingerprint lookup failed"
        )
    retained: list[bytes] = []
    for raw_entry in staged.stdout.split(b"\0"):
        if not raw_entry:
            continue
        _metadata, separator, raw_path = raw_entry.partition(b"\t")
        path = _decode_git_path(raw_path) if separator else ""
        if path and not _path_is_filtered(path, excluded_prefixes):
            retained.append(b"stage\0" + raw_entry)
    for raw_entry in flags.stdout.split(b"\0"):
        if not raw_entry:
            continue
        _flag, separator, raw_path = raw_entry.partition(b" ")
        path = _decode_git_path(raw_path) if separator else ""
        if path and not _path_is_filtered(path, excluded_prefixes):
            retained.append(b"flag\0" + raw_entry)
    hasher = hashlib.sha256()
    for entry in sorted(retained):
        hasher.update(entry)
        hasher.update(b"\0")
    return hasher.hexdigest()


def _changed_repository_paths(
    project_root: Path,
    excluded_prefixes: tuple[str, ...],
) -> tuple[str, ...]:
    tracked = _git_bytes(project_root, "diff", "--name-only", "-z", "HEAD", "--")
    untracked = _git_bytes(
        project_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if tracked.returncode != 0 or untracked.returncode != 0:
        message = tracked.stderr or untracked.stderr
        raise RuntimeError(
            message.decode("utf-8", errors="replace").strip()
            or "git worktree fingerprint lookup failed"
        )
    paths = {
        _decode_git_path(raw_path)
        for output in (tracked.stdout, untracked.stdout)
        for raw_path in output.split(b"\0")
        if raw_path
    }
    return tuple(
        path
        for path in sorted(paths)
        if not _path_is_filtered(path, excluded_prefixes)
    )


def checkpoint_repository_fingerprints(
    project_root: Path,
    *,
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
    excluded_paths: Iterable[str] = (),
) -> Dict[str, str]:
    """Fingerprint the exact non-engine worktree and semantic index state."""

    prefixes = normalize_repository_exclusions(
        (*tuple(ignored_prefixes), *tuple(excluded_paths))
    )
    path_snapshots = [
        {
            "path": path,
            "fingerprint": str(
                _capture_worktree_path(project_root, path)["fingerprint"]
            ),
        }
        for path in _coalesced_worktree_roots(
            _changed_repository_paths(project_root, prefixes)
        )
    ]
    index_image = _capture_index_image(project_root)
    return {
        "head": head_ref(project_root),
        "worktree": _checkpoint_payload_fingerprint(path_snapshots),
        "index": _semantic_index_fingerprint(project_root, prefixes),
        "index_image": str(index_image["sha256"]),
    }


def _worktree_entry_observation(
    project_root: Path,
    path: str,
    expected: Mapping[str, object],
    *,
    allow_directory_container: bool,
    immutable_git_read: bool = False,
) -> tuple[Dict[str, object], bool]:
    candidate = _safe_worktree_path(project_root, path)
    expected_kind = str(expected.get("kind", ""))
    try:
        details = candidate.lstat()
    except FileNotFoundError:
        observed: Dict[str, object] = {"kind": "missing", "mode": "000000"}
        return observed, expected_kind == "missing"
    observed_content: object = None
    if stat.S_ISREG(details.st_mode):
        content = candidate.read_bytes()
        observed_content = content
        mode = "100755" if details.st_mode & 0o111 else "100644"
        observed = {
            "kind": "file",
            "mode": mode,
            "size": len(content),
            "content_sha256": hashlib.sha256(content).hexdigest(),
        }
    elif stat.S_ISLNK(details.st_mode):
        content = os.fsencode(os.readlink(candidate))
        observed_content = content
        observed = {
            "kind": "symlink",
            "mode": "120000",
            "size": len(content),
            "content_sha256": hashlib.sha256(content).hexdigest(),
        }
    elif stat.S_ISDIR(details.st_mode):
        observed = {"kind": "directory", "mode": "040000"}
        if expected_kind == "gitlink":
            git_read = (
                _immutable_git_bytes if immutable_git_read else _git_bytes
            )
            submodule_root = git_read(
                candidate,
                "rev-parse",
                "--show-toplevel",
            )
            submodule = git_read(
                candidate,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            )
            try:
                observed_root = Path(
                    os.fsdecode(submodule_root.stdout).strip()
                ).resolve()
            except (OSError, RuntimeError, ValueError):
                observed_root = Path()
            if (
                submodule_root.returncode == 0
                and submodule.returncode == 0
                and observed_root == candidate.resolve()
            ):
                observed = {
                    "kind": "gitlink",
                    "mode": "160000",
                    "object_id": submodule.stdout.decode("ascii").strip(),
                }
    else:
        observed = {"kind": "unsupported", "mode": oct(details.st_mode)}
    if expected_kind == "missing":
        return observed, bool(
            allow_directory_container and observed.get("kind") == "directory"
        )
    matches = bool(
        observed.get("kind") == expected_kind
        and observed.get("mode") == expected.get("mode")
    )
    if expected_kind in {"file", "symlink"}:
        matches = bool(
            matches
            and observed.get("size") == expected.get("size")
            and observed.get("content_sha256")
            == expected.get("content_sha256")
        )
        expected_content = expected.get("_content_bytes")
        if isinstance(expected_content, bytes):
            matches = bool(matches and observed_content == expected_content)
    elif expected_kind == "gitlink":
        matches = bool(
            matches
            and observed.get("object_id") == expected.get("object_id")
        )
    return observed, matches


def _owned_path_observation(
    project_root: Path,
    manifest: Iterable[Mapping[str, object]],
    *,
    immutable_git_reads: bool = False,
) -> Dict[str, object]:
    entries = sorted(
        (dict(item) for item in manifest),
        key=lambda item: (
            len(PurePosixPath(str(item.get("path", ""))).parts),
            str(item.get("path", "")),
        ),
    )
    observations: Dict[str, object] = {}
    mismatches: list[Dict[str, object]] = []
    for entry in entries:
        path = str(entry.get("path", ""))
        raw_expected = entry.get("after", {})
        expected = dict(raw_expected) if isinstance(raw_expected, Mapping) else {}
        expected_kind = str(expected.get("kind", ""))
        expected_index = (
            []
            if expected_kind in {"missing", "directory"}
            else [
                {
                    "mode": str(expected.get("mode", "")),
                    "object_id": str(expected.get("object_id", "")),
                    "stage": "0",
                }
            ]
        )
        actual_index = _index_entries_for_path(
            project_root,
            path,
            immutable_git_read=immutable_git_reads,
        )
        allow_container = any(
            str(other.get("path", "")).startswith(path + "/")
            and isinstance(other.get("after"), Mapping)
            and str(dict(other["after"]).get("kind", "")) != "missing"
            for other in entries
        )
        blocked_by = ""
        parent = PurePosixPath(path).parent
        while parent.as_posix() not in {"", "."}:
            parent_observation = observations.get(parent.as_posix())
            if isinstance(parent_observation, Mapping):
                parent_worktree = parent_observation.get("worktree", {})
                if (
                    isinstance(parent_worktree, Mapping)
                    and str(parent_worktree.get("kind", "")) != "directory"
                ):
                    blocked_by = parent.as_posix()
                    break
            parent = parent.parent
        if blocked_by:
            worktree = {
                "kind": "missing",
                "mode": "000000",
                "blocked_by": blocked_by,
            }
            worktree_matches = expected_kind == "missing"
        else:
            worktree, worktree_matches = _worktree_entry_observation(
                project_root,
                path,
                expected,
                allow_directory_container=allow_container,
                immutable_git_read=immutable_git_reads,
            )
        index_matches = actual_index == expected_index
        observations[path] = {
            "index": actual_index,
            "worktree": worktree,
        }
        if not index_matches or not worktree_matches:
            mismatches.append(
                {
                    "path": path,
                    "index_matches": index_matches,
                    "worktree_matches": worktree_matches,
                }
            )
    return {
        "fingerprint": _checkpoint_payload_fingerprint(observations),
        "paths": observations,
        "mismatches": mismatches,
        "matches_retained": not mismatches,
    }


def _legacy_effective_changed_paths(
    project_root: Path,
    ignored_prefixes: tuple[str, ...],
) -> tuple[str, ...]:
    """Read the union of staged, unstaged, and untracked non-engine paths."""

    head = _immutable_git_bytes(
        project_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    )
    staged_args = [
        "diff",
        "--cached",
        "--name-only",
        "--no-renames",
        "--no-ext-diff",
        "--ignore-submodules=none",
        "-z",
    ]
    if head.returncode == 0:
        staged_args.append(head.stdout.decode("ascii").strip())
    staged_args.append("--")
    staged = _immutable_git_bytes(project_root, *staged_args)
    unstaged = _immutable_git_bytes(
        project_root,
        "diff",
        "--name-only",
        "--no-renames",
        "--no-ext-diff",
        "--ignore-submodules=none",
        "-z",
        "--",
    )
    untracked = _immutable_git_bytes(
        project_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if staged.returncode != 0 or unstaged.returncode != 0 or untracked.returncode != 0:
        message = staged.stderr or unstaged.stderr or untracked.stderr
        raise RuntimeError(
            message.decode("utf-8", errors="replace").strip()
            or "legacy checkpoint current path lookup failed"
        )
    paths = {
        _legacy_proof_path(raw_path)
        for output in (staged.stdout, unstaged.stdout, untracked.stdout)
        for raw_path in output.split(b"\0")
        if raw_path
    }
    return tuple(
        path
        for path in sorted(paths)
        if not _path_is_filtered(path, ignored_prefixes)
    )


def _legacy_recorded_changed_paths(
    raw_paths: object,
) -> tuple[str, ...]:
    if not isinstance(raw_paths, list) or not raw_paths:
        raise RuntimeError(
            "legacy checkpoint changed_paths must be a non-empty list"
        )
    if not all(isinstance(path, str) for path in raw_paths):
        raise RuntimeError("legacy checkpoint changed_paths contains a non-string path")
    paths = tuple(_legacy_proof_path(path.encode("utf-8")) for path in raw_paths)
    if len(paths) != len(set(paths)):
        raise RuntimeError("legacy checkpoint changed_paths contains duplicates")
    return paths


def _legacy_proof_result() -> Dict[str, object]:
    return {
        "proof_schema_version": 1,
        "ok": False,
        "proof": "legacy_applied_checkpoint_unproven",
        "reason": "",
        "classification": {
            "name": "legacy_applied_without_transaction",
            "matches": False,
            "checkpoint_schema_version": None,
            "checkpoint_status": "",
            "application_transaction": "absent",
        },
        "owner": {
            "matches": False,
            "state_map_owner_task_id": "",
            "checkpoint_task_id": "",
            "intended_task_id": "",
        },
        "retained_identity": {
            "matches": False,
            "recorded_ref": "",
            "ref_state": "unobserved",
            "ref_object": "",
            "ref_commit": "",
            "recorded_commit": "",
            "resolved_commit": "",
            "resolved_object_type": "",
            "commit_parent": "",
            "commit_parents": [],
            "commit_parent_count": None,
            "missing_ref_fallback": False,
        },
        "changed_paths": {
            "matches": False,
            "recorded": [],
            "normalized_recorded": [],
            "retained_commit": [],
            "current": [],
        },
        "expected_entries": {},
        "observed_entries": {},
        "entry_mismatches": [],
        "mismatch_codes": [],
    }


def _reject_legacy_proof(
    evidence: Dict[str, object],
    code: str,
    reason: str,
) -> Dict[str, object]:
    raw_codes = evidence.get("mismatch_codes", [])
    codes = raw_codes if isinstance(raw_codes, list) else []
    if code not in codes:
        codes.append(code)
    evidence["mismatch_codes"] = codes
    if not str(evidence.get("reason", "")).strip():
        evidence["reason"] = reason
    return evidence


def _canonical_commit_identity(project_root: Path, recorded: str) -> str:
    if (
        len(recorded) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in recorded)
    ):
        raise ValueError("legacy checkpoint commit_sha is not a canonical full object ID")
    result = _immutable_git_bytes(
        project_root,
        "rev-parse",
        "--verify",
        f"{recorded}^{{commit}}",
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or f"legacy checkpoint object is not a commit: {recorded}"
        )
    return result.stdout.decode("ascii").strip()


def prove_legacy_applied_checkpoint(
    project_root: Path,
    checkpoint: Mapping[str, object],
    *,
    state_map_owner_task_id: str,
    intended_task_id: str,
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
) -> Dict[str, object]:
    """Prove an already-applied schema-v1 candidate without repository mutation.

    This compatibility proof deliberately captures no pre-application state.  It
    only compares immutable retained-commit facts with the current index and
    worktree so a caller can decide whether the exact recorded owner may resume.
    """

    evidence = _legacy_proof_result()
    raw_transaction_state = (
        "absent"
        if "application_transaction" not in checkpoint
        else "null"
        if checkpoint.get("application_transaction") is None
        else "present"
    )
    classification = {
        "name": "legacy_applied_without_transaction",
        "matches": bool(
            type(checkpoint.get("schema_version")) is int
            and checkpoint.get("schema_version") == 1
            and checkpoint.get("status") == "applied"
            and raw_transaction_state in {"absent", "null"}
        ),
        "checkpoint_schema_version": checkpoint.get("schema_version"),
        "checkpoint_status": (
            checkpoint.get("status")
            if isinstance(checkpoint.get("status"), str)
            else ""
        ),
        "application_transaction": raw_transaction_state,
    }
    evidence["classification"] = classification

    state_owner = (
        state_map_owner_task_id.strip()
        if isinstance(state_map_owner_task_id, str)
        else ""
    )
    checkpoint_owner_raw = checkpoint.get("task_id")
    checkpoint_owner = (
        checkpoint_owner_raw.strip()
        if isinstance(checkpoint_owner_raw, str)
        else ""
    )
    intended_owner = (
        intended_task_id.strip() if isinstance(intended_task_id, str) else ""
    )
    owner_is_canonical = bool(
        state_owner == state_map_owner_task_id
        and checkpoint_owner == checkpoint_owner_raw
        and intended_owner == intended_task_id
    )
    owner_matches = bool(
        owner_is_canonical
        and state_owner
        and state_owner == checkpoint_owner == intended_owner
    )
    evidence["owner"] = {
        "matches": owner_matches,
        "state_map_owner_task_id": state_owner,
        "checkpoint_task_id": checkpoint_owner,
        "intended_task_id": intended_owner,
    }
    if not bool(classification["matches"]):
        _reject_legacy_proof(
            evidence,
            "legacy_classification_mismatch",
            "checkpoint is not schema-version-1 applied state without a transaction",
        )
    if not state_owner or not checkpoint_owner or not intended_owner:
        _reject_legacy_proof(
            evidence,
            "owner_identity_missing",
            "legacy checkpoint owner identities must all be non-empty",
        )
    elif not owner_matches:
        _reject_legacy_proof(
            evidence,
            "owner_identity_mismatch",
            "state-map, checkpoint, and intended task owners do not match exactly",
        )
    if evidence["mismatch_codes"]:
        return evidence

    try:
        normalized_ignored = normalize_repository_exclusions(ignored_prefixes)
    except ValueError as error:
        return _reject_legacy_proof(
            evidence,
            "proof_configuration_invalid",
            str(error),
        )

    retained_ref_raw = checkpoint.get("ref")
    retained_ref = (
        retained_ref_raw.strip() if isinstance(retained_ref_raw, str) else ""
    )
    recorded_commit_raw = checkpoint.get("commit_sha")
    recorded_commit = (
        recorded_commit_raw.strip()
        if isinstance(recorded_commit_raw, str)
        else ""
    )
    retained_identity = dict(evidence["retained_identity"])
    retained_identity["recorded_ref"] = retained_ref
    retained_identity["recorded_commit"] = recorded_commit
    evidence["retained_identity"] = retained_identity
    if retained_ref != retained_ref_raw or not retained_ref:
        return _reject_legacy_proof(
            evidence,
            "retained_ref_invalid",
            "legacy checkpoint retained ref is missing or non-canonical",
        )
    if recorded_commit != recorded_commit_raw or not recorded_commit:
        return _reject_legacy_proof(
            evidence,
            "retained_commit_not_canonical",
            "legacy checkpoint commit_sha is missing or non-canonical",
        )

    try:
        resolved_commit = _canonical_commit_identity(
            project_root,
            recorded_commit,
        )
    except ValueError as error:
        return _reject_legacy_proof(
            evidence,
            "retained_commit_not_canonical",
            str(error),
        )
    except (OSError, RuntimeError) as error:
        return _reject_legacy_proof(
            evidence,
            "retained_commit_unresolvable",
            str(error),
        )
    retained_identity["resolved_commit"] = resolved_commit
    if resolved_commit != recorded_commit:
        return _reject_legacy_proof(
            evidence,
            "retained_commit_not_canonical",
            "legacy checkpoint commit_sha does not equal its resolved commit",
        )
    retained_identity["resolved_object_type"] = "commit"

    ref_format = _immutable_git_bytes(
        project_root,
        "check-ref-format",
        retained_ref,
    )
    if ref_format.returncode != 0:
        return _reject_legacy_proof(
            evidence,
            "retained_ref_invalid",
            "legacy checkpoint retained ref is not a valid full ref name",
        )
    ref_presence = _immutable_git_bytes(
        project_root,
        "show-ref",
        "--verify",
        "--quiet",
        retained_ref,
    )
    if ref_presence.returncode == 0:
        ref_lookup = _immutable_git_bytes(
            project_root,
            "show-ref",
            "--verify",
            "--hash",
            retained_ref,
        )
        if ref_lookup.returncode != 0:
            return _reject_legacy_proof(
                evidence,
                "retained_ref_lookup_failed",
                ref_lookup.stderr.decode("utf-8", errors="replace").strip()
                or "legacy checkpoint retained ref changed during lookup",
            )
        ref_object = ref_lookup.stdout.decode("ascii").strip()
        retained_identity["ref_state"] = "resolved"
        retained_identity["ref_object"] = ref_object
        try:
            ref_commit = _canonical_commit_identity(
                project_root,
                ref_object,
            )
        except (OSError, RuntimeError, ValueError) as error:
            return _reject_legacy_proof(
                evidence,
                "retained_ref_unresolvable",
                str(error),
            )
        retained_identity["ref_commit"] = ref_commit
        if ref_commit != recorded_commit:
            retained_identity["ref_state"] = "mismatched"
            return _reject_legacy_proof(
                evidence,
                "retained_ref_commit_mismatch",
                "legacy checkpoint retained ref resolves to a different commit",
            )
        retained_identity["ref_state"] = "matched"
    elif ref_presence.returncode == 1:
        retained_identity["ref_state"] = "missing"
        retained_identity["missing_ref_fallback"] = True
    else:
        return _reject_legacy_proof(
            evidence,
            "retained_ref_lookup_failed",
            ref_presence.stderr.decode("utf-8", errors="replace").strip()
            or "legacy checkpoint retained ref lookup failed",
        )

    try:
        parent_commits = _legacy_commit_parents(project_root, recorded_commit)
    except (OSError, RuntimeError, UnicodeError) as error:
        return _reject_legacy_proof(
            evidence,
            "retained_commit_parent_ambiguous",
            str(error),
        )
    retained_identity["commit_parents"] = list(parent_commits)
    retained_identity["commit_parent_count"] = len(parent_commits)
    if len(parent_commits) > 1:
        return _reject_legacy_proof(
            evidence,
            "retained_commit_parent_ambiguous",
            "legacy checkpoint proof requires a root or single-parent "
            "physical retained commit",
        )
    parent_commit = parent_commits[0] if parent_commits else ""
    retained_identity["commit_parent"] = parent_commit
    retained_identity["matches"] = True

    try:
        retained_paths = _legacy_commit_delta_paths(
            project_root,
            parent_commit,
            recorded_commit,
        )
    except (OSError, RuntimeError, ValueError, UnicodeError) as error:
        return _reject_legacy_proof(
            evidence,
            "retained_changed_paths_unresolvable",
            str(error),
        )
    raw_recorded_paths = checkpoint.get("changed_paths")
    recorded_path_evidence = (
        list(raw_recorded_paths)
        if isinstance(raw_recorded_paths, list)
        and all(isinstance(path, str) for path in raw_recorded_paths)
        else []
    )
    try:
        recorded_paths = _legacy_recorded_changed_paths(
            raw_recorded_paths
        )
    except (OSError, RuntimeError, ValueError, UnicodeError) as error:
        recorded_paths = ()
        _reject_legacy_proof(
            evidence,
            "recorded_changed_paths_invalid",
            str(error),
        )
    try:
        current_paths = _legacy_effective_changed_paths(
            project_root,
            normalized_ignored,
        )
    except (OSError, RuntimeError, ValueError, UnicodeError) as error:
        current_paths = ()
        _reject_legacy_proof(
            evidence,
            "current_changed_paths_unresolvable",
            str(error),
        )

    ignored_retained_paths = tuple(
        path
        for path in retained_paths
        if _path_is_filtered(path, normalized_ignored)
    )
    effective_retained_paths = tuple(
        path
        for path in retained_paths
        if not _path_is_filtered(path, normalized_ignored)
    )
    path_evidence = {
        "matches": False,
        "recorded": recorded_path_evidence,
        "normalized_recorded": list(recorded_paths),
        "retained_commit": list(retained_paths),
        "current": list(current_paths),
    }
    evidence["changed_paths"] = path_evidence
    if not retained_paths:
        _reject_legacy_proof(
            evidence,
            "retained_changed_paths_empty",
            "legacy checkpoint retained commit has no changed paths",
        )
    if ignored_retained_paths:
        _reject_legacy_proof(
            evidence,
            "retained_commit_contains_ignored_paths",
            "legacy checkpoint retained commit changes engine-owned paths",
        )
    if set(recorded_paths) != set(retained_paths):
        _reject_legacy_proof(
            evidence,
            "recorded_changed_paths_mismatch",
            "recorded changed_paths do not equal the retained commit delta",
        )
    if set(current_paths) != set(effective_retained_paths):
        _reject_legacy_proof(
            evidence,
            "current_changed_paths_mismatch",
            "current non-engine changed paths do not equal the retained commit delta",
        )
    path_evidence["matches"] = bool(
        retained_paths
        and not ignored_retained_paths
        and set(recorded_paths) == set(retained_paths)
        and set(current_paths) == set(effective_retained_paths)
    )

    try:
        # Only the retained side is relevant here.  In particular, a root
        # commit must not tempt this compatibility path to invent a before
        # tree and call it the unavailable application prestate.
        manifest = [
            {
                "path": path,
                "after": _commit_path_entry(
                    project_root,
                    recorded_commit,
                    path,
                    include_content_bytes=True,
                    immutable_object_read=True,
                ),
            }
            for path in effective_retained_paths
        ]
    except (OSError, RuntimeError, ValueError, UnicodeError) as error:
        return _reject_legacy_proof(
            evidence,
            "retained_entries_unresolvable",
            str(error),
        )
    expected_entries = {
        str(item["path"]): {
            key: value
            for key, value in dict(item["after"]).items()
            if key != "_content_bytes"
        }
        for item in manifest
        if isinstance(item.get("after"), Mapping)
    }
    evidence["expected_entries"] = expected_entries
    try:
        owned = _owned_path_observation(
            project_root,
            manifest,
            immutable_git_reads=True,
        )
    except (OSError, RuntimeError, ValueError, UnicodeError) as error:
        return _reject_legacy_proof(
            evidence,
            "owned_entries_unresolvable",
            str(error),
        )
    evidence["observed_entries"] = dict(owned["paths"])
    evidence["entry_mismatches"] = list(owned["mismatches"])
    if not bool(owned["matches_retained"]):
        mismatches = list(owned["mismatches"])
        if any(not bool(item.get("index_matches")) for item in mismatches):
            _reject_legacy_proof(
                evidence,
                "owned_index_entry_mismatch",
                "owned index entries do not match the retained commit",
            )
        if any(not bool(item.get("worktree_matches")) for item in mismatches):
            _reject_legacy_proof(
                evidence,
                "owned_worktree_entry_mismatch",
                "owned worktree entries do not match the retained commit",
            )

    if evidence["mismatch_codes"]:
        return evidence
    evidence["ok"] = True
    evidence["proof"] = "legacy_applied_checkpoint_exact_owner"
    evidence["reason"] = ""
    return evidence


def _validated_checkpoint_prestate(
    transaction: Mapping[str, object],
) -> tuple[
    tuple[str, ...],
    Dict[str, object],
    Dict[str, object],
    Mapping[str, object],
]:
    try:
        version = int(transaction.get("schema_version", 0) or 0)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "checkpoint application transaction version is invalid"
        ) from error
    if version != CHECKPOINT_APPLICATION_TRANSACTION_VERSION:
        raise RuntimeError("checkpoint application transaction version is invalid")
    raw_paths = transaction.get("effective_paths", [])
    if not isinstance(raw_paths, list):
        raise RuntimeError("checkpoint effective path list is invalid")
    try:
        paths = normalize_repository_exclusions(str(path) for path in raw_paths)
    except ValueError as error:
        raise RuntimeError("checkpoint effective path binding is unsafe") from error
    if list(paths) != raw_paths or not paths:
        raise RuntimeError("checkpoint effective path binding is invalid")
    raw_prestate = transaction.get("pre_application", {})
    prestate = dict(raw_prestate) if isinstance(raw_prestate, Mapping) else {}
    raw_snapshots = prestate.get("worktree_paths", {})
    snapshots = dict(raw_snapshots) if isinstance(raw_snapshots, Mapping) else {}
    raw_snapshot_paths = list(snapshots)
    if not all(isinstance(path, str) for path in raw_snapshot_paths):
        raise RuntimeError("checkpoint worktree prestate path set is invalid")
    try:
        snapshot_paths = normalize_repository_exclusions(raw_snapshot_paths)
    except ValueError as error:
        raise RuntimeError(
            "checkpoint worktree prestate path set is unsafe"
        ) from error
    if set(snapshot_paths) != set(raw_snapshot_paths) or set(
        _coalesced_worktree_roots(snapshot_paths)
    ) != set(snapshot_paths):
        raise RuntimeError("checkpoint worktree prestate path set is invalid")
    if any(
        not any(path == root or path.startswith(root + "/") for root in snapshot_paths)
        for path in paths
    ) or any(
        not any(path == root or path.startswith(root + "/") for path in paths)
        for root in snapshot_paths
    ):
        raise RuntimeError("checkpoint worktree prestate path set is incomplete")
    for root, raw_snapshot in snapshots.items():
        if not isinstance(raw_snapshot, Mapping):
            raise RuntimeError("checkpoint worktree prestate is invalid")
        try:
            _validate_worktree_snapshot(raw_snapshot)
        except ValueError as error:
            raise RuntimeError("checkpoint worktree prestate is unsafe") from error
        if root not in paths and str(raw_snapshot.get("kind", "")) != "missing":
            raise RuntimeError(
                "checkpoint worktree ancestor prestate must be missing"
            )
    raw_index = prestate.get("index_image", {})
    if not isinstance(raw_index, Mapping):
        raise RuntimeError("checkpoint index prestate is missing")
    _validated_index_content(raw_index)
    raw_fingerprints = prestate.get("fingerprints", {})
    fingerprints = (
        dict(raw_fingerprints)
        if isinstance(raw_fingerprints, Mapping)
        else {}
    )
    required_fingerprints = {"head", "worktree", "index", "index_image"}
    if not required_fingerprints.issubset(fingerprints):
        raise RuntimeError("checkpoint prestate fingerprints are incomplete")
    if str(fingerprints.get("index_image", "")) != str(
        raw_index.get("sha256", "")
    ) or str(prestate.get("index_semantic_identity", "")) != str(
        fingerprints.get("index", "")
    ):
        raise RuntimeError("checkpoint index prestate identity is inconsistent")
    return paths, prestate, snapshots, raw_index


def _validate_checkpoint_transaction(
    project_root: Path,
    transaction: Mapping[str, object],
) -> tuple[
    tuple[str, ...],
    list[Dict[str, object]],
    Dict[str, object],
]:
    paths, _prestate, snapshots, _index = _validated_checkpoint_prestate(
        transaction
    )
    owner = str(transaction.get("owner_task_id", "")).strip()
    retained_ref = str(transaction.get("retained_ref", "")).strip()
    retained_commit = str(transaction.get("retained_commit", "")).strip()
    parent_commit = str(transaction.get("retained_parent", "")).strip()
    if not owner or not retained_ref or not retained_commit or not parent_commit:
        raise RuntimeError("checkpoint application transaction binding is incomplete")
    if _resolve_commit(project_root, retained_ref) != retained_commit:
        raise RuntimeError("checkpoint retained ref no longer matches its transaction")
    if _single_commit_parent(project_root, retained_commit) != parent_commit:
        raise RuntimeError("checkpoint retained parent no longer matches its transaction")
    raw_manifest = transaction.get("retained_manifest", [])
    if not isinstance(raw_manifest, list) or not all(
        isinstance(item, dict) for item in raw_manifest
    ):
        raise RuntimeError("checkpoint retained manifest is invalid")
    manifest = [dict(item) for item in raw_manifest]
    expected_manifest = _retained_path_manifest(
        project_root,
        parent_commit,
        retained_commit,
        paths,
    )
    if manifest != expected_manifest:
        raise RuntimeError("checkpoint retained path manifest no longer matches its ref")
    return paths, manifest, snapshots


def begin_checkpoint_application(
    project_root: Path,
    *,
    owner_task_id: str,
    retained_ref: str,
    retained_commit: str = "",
    changed_paths: Iterable[str],
    exclude_prefixes: Iterable[str] = (),
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
) -> Dict[str, object]:
    """Capture a durable, non-mutating prestate for a retained checkpoint."""

    owner = str(owner_task_id).strip()
    ref_name = str(retained_ref).strip()
    if not owner or not ref_name:
        raise RuntimeError("checkpoint owner and retained ref are required")
    resolved_commit = _resolve_commit(project_root, ref_name)
    expected_commit = str(retained_commit).strip()
    if expected_commit and _resolve_commit(project_root, expected_commit) != resolved_commit:
        raise RuntimeError("checkpoint commit identity does not match its retained ref")
    parent_commit = _single_commit_parent(project_root, resolved_commit)
    exclusions = normalize_repository_exclusions(exclude_prefixes)
    requested_paths = normalize_repository_exclusions(changed_paths)
    actual_paths = _commit_delta_paths(
        project_root,
        parent_commit,
        resolved_commit,
    )
    effective_requested = tuple(
        path for path in requested_paths if not _path_is_filtered(path, exclusions)
    )
    effective_actual = tuple(
        path for path in actual_paths if not _path_is_filtered(path, exclusions)
    )
    if set(effective_requested) != set(effective_actual) or not effective_actual:
        raise RuntimeError(
            "checkpoint changed paths do not exactly match the retained commit"
        )
    effective_paths = tuple(sorted(effective_actual))
    manifest = _retained_path_manifest(
        project_root,
        parent_commit,
        resolved_commit,
        effective_paths,
    )
    snapshot_roots = _worktree_snapshot_roots(project_root, effective_paths)
    worktree_paths = {
        path: _capture_worktree_path(project_root, path)
        for path in snapshot_roots
    }
    fingerprints = checkpoint_repository_fingerprints(
        project_root,
        ignored_prefixes=ignored_prefixes,
    )
    unowned_fingerprints = checkpoint_repository_fingerprints(
        project_root,
        ignored_prefixes=ignored_prefixes,
        excluded_paths=effective_paths,
    )
    # The exact image is captured last, after every read-only semantic query.
    index_image = _capture_index_image(project_root)
    fingerprints["index_image"] = str(index_image["sha256"])
    pre_application = {
        "head": fingerprints["head"],
        "worktree_paths": worktree_paths,
        "index_image": index_image,
        "index_semantic_identity": fingerprints["index"],
        "fingerprints": fingerprints,
        "unowned_fingerprints": {
            key: value
            for key, value in unowned_fingerprints.items()
            if key != "index_image"
        },
    }
    identity = {
        "version": CHECKPOINT_APPLICATION_TRANSACTION_VERSION,
        "owner_task_id": owner,
        "retained_ref": ref_name,
        "retained_commit": resolved_commit,
        "retained_parent": parent_commit,
        "effective_paths": list(effective_paths),
        "prestate_fingerprints": fingerprints,
    }
    return {
        "schema_version": CHECKPOINT_APPLICATION_TRANSACTION_VERSION,
        "transaction_id": _checkpoint_payload_fingerprint(identity)[:24],
        "owner_task_id": owner,
        "retained_ref": ref_name,
        "retained_commit": resolved_commit,
        "retained_parent": parent_commit,
        "excluded_prefixes": list(exclusions),
        "effective_paths": list(effective_paths),
        "retained_manifest": manifest,
        "pre_application": pre_application,
        "status": "applying",
    }


def checkpoint_application_state(
    project_root: Path,
    transaction: Mapping[str, object],
    *,
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
) -> Dict[str, object]:
    """Observe a transaction without changing its worktree or index."""

    paths, manifest, snapshots = _validate_checkpoint_transaction(
        project_root,
        transaction,
    )
    current_fingerprints = checkpoint_repository_fingerprints(
        project_root,
        ignored_prefixes=ignored_prefixes,
    )
    current_unowned = checkpoint_repository_fingerprints(
        project_root,
        ignored_prefixes=ignored_prefixes,
        excluded_paths=paths,
    )
    owned = _owned_path_observation(project_root, manifest)
    raw_prestate = transaction.get("pre_application", {})
    prestate = dict(raw_prestate) if isinstance(raw_prestate, Mapping) else {}
    expected_pre = prestate.get("fingerprints", {})
    expected_unowned = prestate.get("unowned_fingerprints", {})
    raw_applied = transaction.get("applied_state", {})
    applied = dict(raw_applied) if isinstance(raw_applied, Mapping) else {}
    expected_applied = applied.get("fingerprints", {})
    expected_owned_fingerprint = str(applied.get("owned_fingerprint", ""))
    raw_expected_applied_roots = applied.get("worktree_root_fingerprints", {})
    expected_applied_roots = (
        dict(raw_expected_applied_roots)
        if isinstance(raw_expected_applied_roots, Mapping)
        else {}
    )
    current_worktree_roots = _worktree_snapshot_fingerprints(
        project_root,
        snapshots,
    )
    expected_pre_worktree_roots = {
        path: str(dict(snapshot)["fingerprint"])
        for path, snapshot in snapshots.items()
        if isinstance(snapshot, Mapping)
    }
    current_unowned_comparable = {
        key: value
        for key, value in current_unowned.items()
        if key != "index_image"
    }
    prestate_matches = bool(
        isinstance(expected_pre, Mapping)
        and dict(expected_pre) == current_fingerprints
        and expected_pre_worktree_roots == current_worktree_roots
    )
    retained_matches = bool(
        owned["matches_retained"]
        and current_fingerprints["head"] == str(prestate.get("head", ""))
        and isinstance(expected_unowned, Mapping)
        and dict(expected_unowned) == current_unowned_comparable
    )
    applied_matches = bool(
        expected_applied
        and isinstance(expected_applied, Mapping)
        and dict(expected_applied) == current_fingerprints
        and expected_owned_fingerprint == str(owned["fingerprint"])
        and owned["matches_retained"]
        and expected_applied_roots == current_worktree_roots
    )
    return {
        "prestate_matches": prestate_matches,
        "retained_matches": retained_matches,
        "applied_matches": applied_matches,
        "fingerprints": current_fingerprints,
        "unowned_fingerprints": current_unowned_comparable,
        "owned_fingerprint": owned["fingerprint"],
        "owned_mismatches": list(owned["mismatches"]),
        "worktree_root_fingerprints": current_worktree_roots,
        "prestate_worktree_roots_match": (
            expected_pre_worktree_roots == current_worktree_roots
        ),
    }


def complete_checkpoint_application(
    project_root: Path,
    transaction: Dict[str, object],
    *,
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
) -> Dict[str, object]:
    observation = checkpoint_application_state(
        project_root,
        transaction,
        ignored_prefixes=ignored_prefixes,
    )
    if not bool(observation["retained_matches"]):
        raise RuntimeError(
            "checkpoint application is not byte-and-mode equivalent to its retained ref"
        )
    transaction["applied_state"] = {
        "head": str(dict(observation["fingerprints"])["head"]),
        "fingerprints": dict(observation["fingerprints"]),
        "unowned_fingerprints": dict(observation["unowned_fingerprints"]),
        "owned_fingerprint": str(observation["owned_fingerprint"]),
        "worktree_root_fingerprints": dict(
            observation["worktree_root_fingerprints"]
        ),
    }
    transaction["status"] = "applied"
    return observation


def apply_checkpoint_application(
    project_root: Path,
    transaction: Dict[str, object],
    *,
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
) -> Dict[str, object]:
    """Apply and verify a previously persisted checkpoint transaction."""

    paths, _manifest, _snapshots = _validate_checkpoint_transaction(
        project_root,
        transaction,
    )
    if str(transaction.get("status", "")) != "applying":
        raise RuntimeError("checkpoint transaction is not ready to apply")
    before = checkpoint_application_state(
        project_root,
        transaction,
        ignored_prefixes=ignored_prefixes,
    )
    if not bool(before["prestate_matches"]):
        raise RuntimeError("checkpoint pre-application state changed before apply")
    apply_commit_no_commit_excluding(
        project_root,
        str(transaction["retained_ref"]),
        tuple(str(item) for item in transaction.get("excluded_prefixes", [])),
        include_paths=paths,
    )
    return complete_checkpoint_application(
        project_root,
        transaction,
        ignored_prefixes=ignored_prefixes,
    )


def rollback_checkpoint_application(
    project_root: Path,
    transaction: Dict[str, object],
    *,
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
) -> Dict[str, object]:
    """Restore a failed application to its exact captured prestate."""

    _paths, prestate, snapshots, raw_index = _validated_checkpoint_prestate(
        transaction
    )
    if head_ref(project_root) != str(prestate.get("head", "")):
        raise RuntimeError("checkpoint rollback refused a changed HEAD")
    index_path = _index_path(project_root)
    if index_path.with_name(index_path.name + ".lock").exists():
        raise RuntimeError("checkpoint rollback refused a locked git index")
    _restore_worktree_snapshots(project_root, snapshots)
    _restore_index_image(project_root, raw_index)
    observed = checkpoint_repository_fingerprints(
        project_root,
        ignored_prefixes=ignored_prefixes,
    )
    raw_expected = prestate.get("fingerprints", {})
    expected = dict(raw_expected) if isinstance(raw_expected, Mapping) else {}
    semantic_observed = {
        key: value for key, value in observed.items() if key != "index_image"
    }
    semantic_expected = {
        key: value for key, value in expected.items() if key != "index_image"
    }
    # Worktree verification may refresh index stat data even with optional
    # locks disabled.  Put the saved bytes back once more after all Git reads,
    # then verify the image directly without another Git command.
    _restore_index_image(project_root, raw_index)
    restored_index = _capture_index_image(project_root)
    observed["index_image"] = str(restored_index["sha256"])
    restored_worktree_roots = _worktree_snapshot_fingerprints(
        project_root,
        snapshots,
    )
    expected_worktree_roots = {
        path: str(dict(snapshot)["fingerprint"])
        for path, snapshot in snapshots.items()
        if isinstance(snapshot, Mapping)
    }
    if semantic_observed != semantic_expected or observed.get(
        "index_image"
    ) != expected.get("index_image") or (
        restored_worktree_roots != expected_worktree_roots
    ):
        raise RuntimeError(
            "checkpoint rollback could not verify the exact prestate: "
            f"expected={expected} observed={observed} "
            "worktree_roots_match="
            f"{restored_worktree_roots == expected_worktree_roots}"
        )
    transaction["status"] = "rolled_back"
    return {"ok": True, "fingerprints": observed}


def detach_checkpoint_application(
    project_root: Path,
    transaction: Dict[str, object],
    *,
    ignored_prefixes: Iterable[str] = _CHECKPOINT_ENGINE_PATHS,
) -> Dict[str, object]:
    """Detach an applied checkpoint only after exact ownership proof."""

    try:
        observation = checkpoint_application_state(
            project_root,
            transaction,
            ignored_prefixes=ignored_prefixes,
        )
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "ok": False,
            "reason": str(error),
            "proof": "transaction_invalid",
        }
    if str(transaction.get("status", "")) == "applied" and bool(
        observation["prestate_matches"]
    ):
        transaction["status"] = "detached"
        return {
            "ok": True,
            "proof": "exact_prestate_already_restored",
            "fingerprints": dict(observation["fingerprints"]),
        }
    if str(transaction.get("status", "")) != "applied" or not bool(
        observation["applied_matches"]
    ):
        return {
            "ok": False,
            "reason": "applied checkpoint ownership could not be proven",
            "proof": "applied_state_mismatch",
            "expected_fingerprints": dict(
                dict(transaction.get("applied_state", {})).get(
                    "fingerprints",
                    {},
                )
                if isinstance(transaction.get("applied_state", {}), Mapping)
                else {}
            ),
            "observed_fingerprints": dict(observation["fingerprints"]),
            "owned_mismatches": list(observation["owned_mismatches"]),
        }
    try:
        restored = rollback_checkpoint_application(
            project_root,
            transaction,
            ignored_prefixes=ignored_prefixes,
        )
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "ok": False,
            "reason": str(error),
            "proof": "prestate_restore_unproven",
            "observed_fingerprints": dict(observation["fingerprints"]),
        }
    transaction["status"] = "detached"
    return {
        "ok": True,
        "proof": "exact_prestate_restored",
        "fingerprints": dict(restored["fingerprints"]),
    }


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
