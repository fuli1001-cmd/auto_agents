"""Read-only queries for ownership checks of legacy artifacts."""
from pathlib import Path
import json
import subprocess

def _head_contains_completed_session(project_root: Path, session_id: str) -> bool:
    relative = f".auto-agents/state/sessions/{session_id}/session_state.json"
    process = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        return False
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        return False
    return bool(
        isinstance(payload, dict)
        and str(payload.get("session_id", "")) == session_id
        and str(payload.get("status", "")) == "completed"
    )
