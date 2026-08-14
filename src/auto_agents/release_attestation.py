from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .git_ops import head_ref, worktree_fingerprint
from .io_utils import read_json, write_json


def release_attestation_path(project_root: Path) -> Path:
    return Path(project_root) / ".auto-agents" / "state" / "release_attestation.json"


def candidate_id(project_root: Path) -> str:
    payload = {
        "head": head_ref(project_root),
        "worktree": worktree_fingerprint(project_root),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def enqueue_release_verification(
    project_root: Path,
    *,
    source: str,
    affected_proof_ids: list[str],
) -> dict[str, object]:
    current_candidate = candidate_id(project_root)
    existing = read_json(release_attestation_path(project_root), default={})
    if (
        isinstance(existing, dict)
        and existing.get("candidate_id") == current_candidate
        and existing.get("status") == "passed"
    ):
        return existing
    payload: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": current_candidate,
        "status": "pending",
        "source": source,
        "affected_proof_ids": list(dict.fromkeys(affected_proof_ids)),
        "release_proof_ids": [],
        "logical_commands": 0,
        "executed_commands": 0,
        "certificate_hits": 0,
        "queued_at": _now(),
        "started_at": "",
        "completed_at": "",
        "reason": "",
    }
    write_json(release_attestation_path(project_root), payload)
    return payload


def begin_release_verification(project_root: Path) -> dict[str, object]:
    payload = _current_payload(project_root)
    payload.update(
        {
            "candidate_id": candidate_id(project_root),
            "status": "running",
            "started_at": _now(),
            "completed_at": "",
            "reason": "",
        }
    )
    write_json(release_attestation_path(project_root), payload)
    return payload


def complete_release_verification(
    project_root: Path,
    result: Mapping[str, object],
) -> dict[str, object]:
    payload = _current_payload(project_root)
    payload.update(
        {
            "candidate_id": candidate_id(project_root),
            "status": "passed" if bool(result.get("ok")) else "failed",
            "release_proof_ids": list(result.get("proof_ids", [])),
            "logical_commands": int(result.get("logical_commands", 0)),
            "executed_commands": int(result.get("executed_commands", 0)),
            "certificate_hits": int(result.get("certificate_hits", 0)),
            "completed_at": _now(),
            "reason": str(result.get("reason", "")),
        }
    )
    write_json(release_attestation_path(project_root), payload)
    return payload


def _current_payload(project_root: Path) -> dict[str, object]:
    payload = read_json(release_attestation_path(project_root), default={})
    return dict(payload) if isinstance(payload, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
