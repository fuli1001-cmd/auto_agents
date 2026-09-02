from __future__ import annotations

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
