"""Read-only DTOs for existing domain tools. No legacy execution authority."""

from pathlib import Path
import sqlite3
from .store import Store


def current(project):
    path = Path(project) / ".auto-agents/state/business.sqlite3"
    if not path.exists():
        return False
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as db:
        return bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='control_meta'"
            ).fetchone()
        )


def project_work(store, work):
    contract = store.contract(work["contract"])
    status = {
        "READY": "pending",
        "RUNNING": "executing",
        "WAITING": (
            "awaiting_approval"
            if work["context"].get("waiting_for") == "approval"
            else "waiting_user"
        ),
        "BLOCKED": "blocked",
        "COMPLETED": "completed",
        "CANCELLED": "cancelled",
    }[work["status"]]
    tasks = [
        {
            **t,
            "title": t.get("goal", ""),
            "status": (
                "done"
                if t["task_id"] in work["context"].get("done_tasks", [])
                else "pending"
            ),
            "verification_refs": [
                ref for spec in t.get("checks", []) for ref in spec.get("targets", [])
            ],
        }
        for t in work["context"].get("tasks", [])
    ]
    return {
        "schema": 2,
        "session_id": work["id"],
        "run_id": work["id"],
        "workflow_id": work["workflow"],
        "mode": work["mode"],
        "goal": contract.goal,
        "status": status,
        "current_stage": work["phase"],
        "tasks": tasks,
        "pending_approval": work["context"].get("approval", ""),
        "approved_gates": list(work["context"].get("approvals", {})),
        "current_attempt": work["calls"],
        "hard_ceiling": work["max_calls"],
        "fix_verify_command": contract.checks[0].command if contract.checks else "",
        "authorization_policy": dict(contract.authorization),
        "verification_diagnostics": work["failure"],
        "resume_context": {
            "workflow_id": work["workflow"],
            "auto_approve": contract.authorization.get("auto_approve", False),
        },
    }


def read(project, relative):
    store = Store(project, readonly=True)
    if relative == "run_state.json":
        works = [
            w
            for w in store.works()
            if w["mode"] == "run"
            and not store.contract(w["contract"]).inputs.get("variant_only")
        ]
        return project_work(store, works[-1]) if works else None
    if relative.startswith("sessions/") and relative.endswith("/session_state.json"):
        from .types import ControlError

        try:
            return project_work(store, store.work(relative.split("/")[1]))
        except ControlError:
            return None
    return None
