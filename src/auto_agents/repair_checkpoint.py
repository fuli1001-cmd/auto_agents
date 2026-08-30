from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List

from .config import (
    requirements_trace_path,
    run_path,
    run_state_path,
    task_plan_path,
)


REPAIR_CHECKPOINT_SCHEMA_VERSION = 1
DEFAULT_MAX_CHECKPOINT_BYTES = 512 * 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _git_output(project_root: Path, *args: str) -> bytes:
    process = subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            process.stderr.decode("utf-8", errors="replace").strip()
            or f"git {' '.join(args)} failed"
        )
    return process.stdout


def _changed_paths(project_root: Path) -> List[str]:
    head_probe = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(project_root),
        capture_output=True,
        check=False,
    )
    tracked = (
        _git_output(
            project_root,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            "HEAD",
            "--",
        )
        if head_probe.returncode == 0
        else _git_output(project_root, "ls-files", "-z")
    )
    untracked = _git_output(
        project_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    return sorted(
        {
            item.decode("utf-8", errors="surrogateescape")
            for item in (tracked + untracked).split(b"\0")
            if item
        }
    )


def _safe_path(project_root: Path, relative: str) -> Path:
    candidate = project_root / relative
    resolved_parent = candidate.parent.resolve()
    root = project_root.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise RuntimeError(f"repair checkpoint path escapes project root: {relative}")
    return candidate


def create_repair_checkpoint(
    project_root: Path,
    run_id: str,
    case_id: str,
    *,
    max_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES,
) -> Path:
    project = project_root.expanduser().resolve()
    root = run_path(project, run_id) / "repair-checkpoints" / case_id
    if root.exists():
        manifest = root / "manifest.json"
        if manifest.is_file():
            return manifest
        shutil.rmtree(root)
    blobs = root / "blobs"
    control = root / "control"
    blobs.mkdir(parents=True, exist_ok=False)
    control.mkdir(parents=True, exist_ok=False)
    total = 0
    entries: List[Dict[str, object]] = []
    for relative in _changed_paths(project):
        source = _safe_path(project, relative)
        if source.is_symlink():
            target = os.readlink(source)
            data = target.encode("utf-8", errors="surrogateescape")
            kind = "symlink"
            mode = stat.S_IMODE(source.lstat().st_mode)
        elif source.is_file():
            data = source.read_bytes()
            kind = "file"
            mode = stat.S_IMODE(source.stat().st_mode)
        elif source.exists():
            continue
        else:
            entries.append({"path": relative, "kind": "deleted"})
            continue
        total += len(data)
        if total > max_bytes:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError(
                "repair checkpoint exceeds the 512 MiB safety limit; "
                "live engine replacement is disabled for this repair case"
            )
        digest = _sha256(data)
        blob = blobs / digest
        if not blob.exists():
            blob.write_bytes(data)
        entries.append(
            {
                "path": relative,
                "kind": kind,
                "mode": mode,
                "sha256": digest,
                "size": len(data),
            }
        )

    control_entries = []
    for name, source in (
        ("run_state.json", run_state_path(project)),
        ("task_plan.json", task_plan_path(project)),
        ("requirements_trace.json", requirements_trace_path(project)),
    ):
        if not source.is_file():
            continue
        data = source.read_bytes()
        total += len(data)
        if total > max_bytes:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError(
                "repair checkpoint exceeds the 512 MiB safety limit; "
                "live engine replacement is disabled for this repair case"
            )
        destination = control / name
        destination.write_bytes(data)
        control_entries.append(
            {"name": name, "sha256": _sha256(data), "size": len(data)}
        )

    index_path_text = _git_output(project, "rev-parse", "--git-path", "index").decode(
        "utf-8", errors="replace"
    ).strip()
    index_path = Path(index_path_text)
    if not index_path.is_absolute():
        index_path = project / index_path
    index_entry: Dict[str, object] = {}
    if index_path.is_file():
        data = index_path.read_bytes()
        total += len(data)
        if total > max_bytes:
            shutil.rmtree(root, ignore_errors=True)
            raise RuntimeError(
                "repair checkpoint exceeds the 512 MiB safety limit; "
                "live engine replacement is disabled for this repair case"
            )
        (control / "git-index").write_bytes(data)
        index_entry = {
            "path": str(index_path),
            "sha256": _sha256(data),
            "size": len(data),
        }

    head_process = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(project),
        capture_output=True,
        check=False,
    )
    head = (
        head_process.stdout.decode("utf-8", errors="replace").strip()
        if head_process.returncode == 0
        else ""
    )
    manifest_payload = {
        "schema_version": REPAIR_CHECKPOINT_SCHEMA_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "project": str(project),
        "head": head,
        "total_bytes": total,
        "entries": entries,
        "control_entries": control_entries,
        "index": index_entry,
    }
    manifest = root / "manifest.json"
    _atomic_json(manifest, manifest_payload)
    return manifest


def restore_repair_control_checkpoint(
    project_root: Path,
    manifest_path: Path,
) -> None:
    """Restore only orchestrator-owned state after a boundary-only failure."""

    project = project_root.expanduser().resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or str(payload.get("project", "")) != str(project)
    ):
        raise RuntimeError("repair checkpoint project identity is invalid")
    destinations = {
        "run_state.json": run_state_path(project),
        "task_plan.json": task_plan_path(project),
        "requirements_trace.json": requirements_trace_path(project),
    }
    control_root = manifest_path.parent / "control"
    for entry in payload.get("control_entries", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        destination = destinations.get(name)
        source = control_root / name
        if destination is None or not source.is_file():
            raise RuntimeError(f"repair checkpoint control entry is invalid: {name}")
        data = source.read_bytes()
        if _sha256(data) != str(entry.get("sha256", "")):
            raise RuntimeError(f"repair checkpoint control hash mismatch: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(
            destination.suffix + f".{os.getpid()}.repair-rollback.tmp"
        )
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
