from __future__ import annotations

import hashlib
import json
from typing import Dict, Mapping


SESSION_PROGRESS_SCHEMA_VERSION = 1


def build_session_progress(payload: Mapping[str, object]) -> Dict[str, object]:
    """Return the canonical durable-progress projection for a session state."""
    return {
        "goal_set": bool(str(payload.get("goal", "")).strip()),
        "conversation_entries": len(payload.get("conversation", []) or []),
        "execution_entries": len(payload.get("execution_log", []) or []),
        "attempt": int(payload.get("current_attempt", 0) or 0),
        "resolution_set": bool(str(payload.get("resolution", "")).strip()),
        "status": str(payload.get("status", "")),
        "diff": str(payload.get("last_diff_hash", "")),
        "verify": str(payload.get("last_verify_sig", "")),
        "workflow_id": str(payload.get("workflow_id", "")),
        "active_handoff_id": str(payload.get("active_handoff_id", "")),
        "return_phase": str(payload.get("return_phase", "")),
    }


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def build_session_progress_identity(
    payload: Mapping[str, object],
    *,
    run_token: str,
) -> Dict[str, object]:
    progress = build_session_progress(payload)
    return {
        "run_token": str(run_token),
        "progress_schema_version": SESSION_PROGRESS_SCHEMA_VERSION,
        "state_digest": _digest(payload),
        "progress_digest": _digest(progress),
        "progress": progress,
    }


def session_progress_disagrees(
    main: Mapping[str, object],
    independent: Mapping[str, object],
) -> bool:
    """Return true only for a genuine same-boundary projection mismatch."""

    return bool(
        str(main.get("run_token", ""))
        and str(main.get("run_token", ""))
        == str(independent.get("run_token", ""))
        and str(main.get("progress_schema_version", ""))
        == str(independent.get("progress_schema_version", ""))
        and str(main.get("state_digest", ""))
        and str(main.get("state_digest", ""))
        == str(independent.get("state_digest", ""))
        and str(main.get("progress_digest", ""))
        and str(independent.get("progress_digest", ""))
        and str(main.get("progress_digest", ""))
        != str(independent.get("progress_digest", ""))
    )
