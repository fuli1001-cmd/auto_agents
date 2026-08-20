from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Dict, Iterable, List, Mapping
from uuid import uuid4

from .config import (
    load_project_config,
    requirements_trace_path,
    run_state_path,
    task_plan_path,
)
from .io_utils import read_json
from .requirements import validate_requirements_trace_payload
from .validation import (
    validate_persistence_plan_contract,
    validate_task_plan_payload,
)


_REQUIREMENT_ID_PATTERN = re.compile(r"^REQ-[0-9]+$", re.IGNORECASE)
_LEGACY_BLOCKER_MARKERS = (
    "target_ids must reference persistence targets, not requirement IDs",
    "target_ids reference unconfigured targets: REQ-",
)


class PersistenceRebindError(RuntimeError):
    pass


def _normalized_ids(values: object) -> List[str]:
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for item in values
            if isinstance(item, str) and str(item).strip()
        )
    )


def _legacy_requirement_targets(values: Iterable[str]) -> bool:
    targets = list(values)
    return bool(targets) and all(
        _REQUIREMENT_ID_PATTERN.fullmatch(item) for item in targets
    )


def _matching_decision(
    trace: Mapping[str, object],
    decision_id: str,
) -> Dict[str, object]:
    decisions = trace.get("persistence_decisions", [])
    if not isinstance(decisions, list):
        raise PersistenceRebindError(
            "requirements trace persistence_decisions must be a list"
        )
    matches = [
        item
        for item in decisions
        if isinstance(item, dict)
        and str(item.get("id", "")).strip() == decision_id
    ]
    if len(matches) != 1:
        raise PersistenceRebindError(
            f"persistence decision {decision_id} must exist exactly once"
        )
    decision = matches[0]
    if str(decision.get("status", "")).strip() != "active":
        raise PersistenceRebindError(
            f"persistence decision {decision_id} must be active"
        )
    return decision


def _rebind_task_changes(
    tasks: object,
    *,
    decision_id: str,
    previous_target_ids: List[str],
    target_ids: List[str],
    source_label: str,
) -> List[str]:
    if not isinstance(tasks, list):
        raise PersistenceRebindError(f"{source_label} tasks must be a list")
    updated: List[str] = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        change = task.get("persistence_change")
        if (
            not isinstance(change, dict)
            or str(change.get("decision_id", "")).strip() != decision_id
        ):
            continue
        existing = _normalized_ids(change.get("target_ids", []))
        if existing == target_ids:
            continue
        if existing != previous_target_ids:
            raise PersistenceRebindError(
                f"{source_label} task #{index}.persistence_change.target_ids "
                f"do not match legacy decision {decision_id}"
            )
        change["target_ids"] = list(target_ids)
        updated.append(str(task.get("task_id", f"#{index}")))
    return updated


def _clear_legacy_blocker(
    state: Dict[str, object],
) -> bool:
    blocker = state.get("active_blocker", {})
    blocker_payload = blocker if isinstance(blocker, dict) else {}
    category = str(blocker_payload.get("category", "")).strip()
    reason = "\n".join(
        (
            str(blocker_payload.get("reason", "")),
            str(state.get("last_error", "")),
        )
    )
    legacy_blocker = category == "invalid_target_persistence_metadata" or any(
        marker in reason for marker in _LEGACY_BLOCKER_MARKERS
    )
    if not legacy_blocker:
        return False
    state["active_blocker"] = {}
    if str(state.get("status", "")).strip() in {"blocked", "failed"}:
        state["status"] = "pending"
    state["last_error"] = ""
    return True


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _replace_json_batch(payloads: Mapping[Path, object]) -> None:
    token = uuid4().hex
    staged: Dict[Path, Path] = {}
    backups: Dict[Path, Path] = {}
    preserved_backups: set[Path] = set()
    originally_present = {path: path.exists() for path in payloads}
    try:
        for path, payload in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            staged_path = path.with_name(f".{path.name}.{token}.staged")
            with staged_path.open("wb") as handle:
                handle.write(_json_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            staged[path] = staged_path

        for path in payloads:
            if not path.exists():
                continue
            backup_path = path.with_name(f".{path.name}.{token}.backup")
            os.replace(path, backup_path)
            backups[path] = backup_path

        for path, staged_path in staged.items():
            os.replace(staged_path, path)
    except Exception as error:
        recovery_errors: List[str] = []
        for path in reversed(list(payloads)):
            backup_path = backups.get(path)
            try:
                if backup_path is not None and backup_path.exists():
                    os.replace(backup_path, path)
                elif not originally_present[path] and path.exists():
                    path.unlink()
            except OSError as recovery_error:
                if backup_path is not None and backup_path.exists():
                    preserved_backups.add(backup_path)
                recovery_errors.append(f"{path}: {recovery_error}")
        recovery_detail = (
            "; rollback incomplete; preserved backups: "
            + ", ".join(str(path) for path in sorted(preserved_backups))
            + "; errors: "
            + "; ".join(recovery_errors)
            if recovery_errors
            else ""
        )
        raise PersistenceRebindError(
            f"failed to commit persistence rebind transaction: {error}"
            f"{recovery_detail}"
        ) from error
    finally:
        for temporary_path in [*staged.values(), *backups.values()]:
            if temporary_path in preserved_backups:
                continue
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def rebind_legacy_persistence_decision(
    project_root: Path,
    *,
    decision_id: str,
    target_ids: Iterable[str],
) -> Dict[str, object]:
    root = project_root.expanduser().resolve()
    normalized_decision_id = str(decision_id).strip()
    selected_target_ids = list(
        dict.fromkeys(str(item).strip() for item in target_ids if str(item).strip())
    )
    if not normalized_decision_id:
        raise PersistenceRebindError("decision_id must be non-empty")
    if not selected_target_ids:
        raise PersistenceRebindError("at least one target_id is required")
    requirement_targets = [
        item
        for item in selected_target_ids
        if _REQUIREMENT_ID_PATTERN.fullmatch(item)
    ]
    if requirement_targets:
        raise PersistenceRebindError(
            "selected target_ids must identify persistence targets, not "
            "requirement IDs: "
            + ", ".join(requirement_targets)
        )

    config = load_project_config(root)
    configured_targets = {
        target.target_id: target
        for target in config.persistence.targets
    }
    missing_targets = [
        target_id
        for target_id in selected_target_ids
        if target_id not in configured_targets
    ]
    if missing_targets:
        raise PersistenceRebindError(
            "persistence target is not configured: "
            + ", ".join(missing_targets)
            + "; run persistence-configure first"
        )

    trace_path = requirements_trace_path(root)
    plan_path = task_plan_path(root)
    state_path = run_state_path(root)
    trace = read_json(trace_path, default=None)
    plan = read_json(plan_path, default=None)
    state = read_json(state_path, default=None)
    if not isinstance(trace, dict):
        raise PersistenceRebindError("requirements trace is missing or invalid")
    if not isinstance(plan, dict):
        raise PersistenceRebindError("task plan is missing or invalid")
    if not isinstance(state, dict):
        raise PersistenceRebindError("run state is missing or invalid")

    decision = _matching_decision(trace, normalized_decision_id)
    previous_target_ids = _normalized_ids(decision.get("target_ids", []))
    already_bound = previous_target_ids == selected_target_ids
    if not already_bound and not _legacy_requirement_targets(
        previous_target_ids
    ):
        raise PersistenceRebindError(
            f"persistence decision {normalized_decision_id} does not contain a "
            "legacy REQ-* target set; refusing to change an established target binding"
        )

    decision["target_ids"] = list(selected_target_ids)
    plan_tasks = _rebind_task_changes(
        plan.get("tasks", []),
        decision_id=normalized_decision_id,
        previous_target_ids=previous_target_ids,
        target_ids=selected_target_ids,
        source_label="task plan",
    )
    state_tasks = _rebind_task_changes(
        state.get("tasks", []),
        decision_id=normalized_decision_id,
        previous_target_ids=previous_target_ids,
        target_ids=selected_target_ids,
        source_label="run state",
    )

    blocker_cleared = _clear_legacy_blocker(state)
    resume_context = state.get("resume_context", {})
    if not isinstance(resume_context, dict):
        resume_context = {}
        state["resume_context"] = resume_context
    receipts = resume_context.get("persistence_rebinds", {})
    if not isinstance(receipts, dict):
        receipts = {}
    resume_context["persistence_rebinds"] = receipts
    existing_receipt = receipts.get(normalized_decision_id)
    if not already_bound or not isinstance(existing_receipt, dict):
        receipts[normalized_decision_id] = {
            "rebound_at": datetime.now(timezone.utc).isoformat(),
            "from_target_ids": previous_target_ids,
            "to_target_ids": selected_target_ids,
        }

    errors = []
    errors.extend(validate_requirements_trace_payload(trace))
    errors.extend(validate_task_plan_payload(plan))
    errors.extend(
        validate_persistence_plan_contract(
            plan,
            trace,
            configured_targets=[
                target.to_dict()
                for target in config.persistence.targets
            ],
        )
    )
    if errors:
        raise PersistenceRebindError(
            "persistence rebind validation failed:\n- "
            + "\n- ".join(dict.fromkeys(errors))
        )

    _replace_json_batch(
        {
            trace_path: trace,
            plan_path: plan,
            state_path: state,
        }
    )
    return {
        "ok": True,
        "decision_id": normalized_decision_id,
        "from_target_ids": previous_target_ids,
        "to_target_ids": selected_target_ids,
        "updated_plan_tasks": plan_tasks,
        "updated_run_state_tasks": state_tasks,
        "blocker_cleared": blocker_cleared,
        "no_op": (
            already_bound
            and not plan_tasks
            and not state_tasks
            and not blocker_cleared
        ),
    }
