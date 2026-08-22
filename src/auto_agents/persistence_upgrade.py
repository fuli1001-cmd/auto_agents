from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Mapping

from .config import (
    config_path,
    load_project_config,
    requirements_trace_path,
    run_state_path,
    task_plan_path,
)
from .io_utils import read_json
from .persistence_rebind import _replace_json_batch


class PersistenceContractUpgradeError(RuntimeError):
    pass


def parse_decision_policies(values: Iterable[str]) -> Dict[str, tuple[str, str]]:
    policies: Dict[str, tuple[str, str]] = {}
    for raw in values:
        parts = str(raw).split(":", 2)
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise PersistenceContractUpgradeError(
                "decision policies must use PERSIST-NNN:storage_transition:compatibility_policy"
            )
        decision_id, transition, policy = (part.strip() for part in parts)
        policies[decision_id] = (transition, policy)
    return policies


def _artifact_id(path: str) -> str:
    stem = Path(path).stem
    return stem.split("_", 1)[0] if stem else "migration"


def _upgrade_change(
    change: object,
    policies: Mapping[str, tuple[str, str]],
) -> dict:
    if not isinstance(change, dict):
        return {"storage_transition": "none", "compatibility_policy": "not_applicable"}
    if "storage_transition" in change:
        upgraded = dict(change)
        if (
            str(upgraded.get("storage_transition", "")) == "none"
            and str(upgraded.get("compatibility_policy", "")) == "not_applicable"
        ):
            return {
                "storage_transition": "none",
                "compatibility_policy": "not_applicable",
            }
        migrations = upgraded.get("migration_artifacts", [])
        contracts = [str(item) for item in upgraded.get("contract_artifacts", [])]
        normalized_migrations: list[object] = []
        if isinstance(migrations, list):
            for artifact in migrations:
                if not isinstance(artifact, dict):
                    normalized_migrations.append(artifact)
                    continue
                path = str(artifact.get("path", "")).replace("\\", "/")
                if "/versions/" in path or path.startswith("db/migrate/"):
                    normalized_migrations.append(artifact)
                elif path:
                    contracts.append(path)
        upgraded["migration_artifacts"] = normalized_migrations
        upgraded["contract_artifacts"] = list(dict.fromkeys(contracts))
        return upgraded
    strategy = str(change.get("strategy", "none") or "none")
    if strategy == "none":
        return {"storage_transition": "none", "compatibility_policy": "not_applicable"}
    decision_id = str(change.get("decision_id", ""))
    if decision_id not in policies:
        raise PersistenceContractUpgradeError(
            f"missing explicit v2 policy for {decision_id or '<unknown>'}"
        )
    transition, policy = policies[decision_id]
    upgraded = {
        "storage_transition": transition,
        "compatibility_policy": policy,
        "decision_id": decision_id,
        "target_ids": list(change.get("target_ids", [])),
        "to_version": str(change.get("to_version", "")),
        "migration_artifacts": [],
        "contract_artifacts": [],
        "legacy_fixture_refs": list(change.get("legacy_fixture_refs", [])),
    }
    for raw_path in change.get("migration_artifacts", []):
        path = str(raw_path)
        normalized = path.replace("\\", "/")
        if "/versions/" in normalized or normalized.startswith("db/migrate/"):
            upgraded["migration_artifacts"].append(
                {
                    "id": _artifact_id(path),
                    "path": path,
                    "kind": "baseline" if transition == "initialize" else "schema",
                }
            )
        else:
            upgraded["contract_artifacts"].append(path)
    return upgraded


def _upgrade_tasks(payload: dict, policies: Mapping[str, tuple[str, str]]) -> None:
    tasks = payload.get("tasks", [])
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                task["persistence_change"] = _upgrade_change(
                    task.get("persistence_change"), policies
                )


def upgrade_persistence_contract(
    project_root: Path,
    *,
    decision_policies: Mapping[str, tuple[str, str]],
    resume_interrupted: bool = False,
) -> dict:
    root = project_root.expanduser().resolve()
    trace = read_json(requirements_trace_path(root), default=None)
    plan = read_json(task_plan_path(root), default=None)
    state = read_json(run_state_path(root), default=None)
    if not all(isinstance(item, dict) for item in (trace, plan, state)):
        raise PersistenceContractUpgradeError(
            "requirements trace, task plan, and run state must all be valid objects"
        )
    assert isinstance(trace, dict) and isinstance(plan, dict) and isinstance(state, dict)
    decisions = trace.get("persistence_decisions", [])
    if not isinstance(decisions, list):
        raise PersistenceContractUpgradeError("persistence_decisions must be a list")
    upgraded_ids: list[str] = []
    for decision in decisions:
        if not isinstance(decision, dict) or "storage_transition" in decision:
            continue
        decision_id = str(decision.get("id", ""))
        if decision_id not in decision_policies:
            raise PersistenceContractUpgradeError(
                f"missing explicit v2 policy for {decision_id or '<unknown>'}"
            )
        transition, policy = decision_policies[decision_id]
        decision.pop("strategy", None)
        decision["storage_transition"] = transition
        decision["compatibility_policy"] = policy
        upgraded_ids.append(decision_id)

    trace["persistence_contract_version"] = 2
    plan["persistence_contract_version"] = 2
    _upgrade_tasks(plan, decision_policies)
    _upgrade_tasks(state, decision_policies)
    blocker = state.get("active_blocker")
    if isinstance(blocker, dict) and str(blocker.get("category", "")) in {
        "persistence_configuration_required",
        "persistence_target_configuration_required",
    }:
        state["active_blocker"] = {}
        state["last_error"] = ""
        if str(state.get("status", "")) in {"blocked", "failed"}:
            state["status"] = "pending"
    elif (
        resume_interrupted
        and isinstance(blocker, dict)
        and str(blocker.get("category", "")) == "run_interrupted"
    ):
        state["active_blocker"] = {}
        state["last_error"] = ""
        state["status"] = "pending"

    config = load_project_config(root).to_dict()
    _replace_json_batch(
        {
            requirements_trace_path(root): trace,
            task_plan_path(root): plan,
            run_state_path(root): state,
            config_path(root): config,
        }
    )
    return {
        "ok": True,
        "persistence_contract_version": 2,
        "upgraded_decisions": upgraded_ids,
        "resumed_interrupted_run": bool(resume_interrupted),
    }
