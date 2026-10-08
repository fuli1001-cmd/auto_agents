"""Check transport shapes before domain code reads model-provided fields."""

from .types import ControlError


def validate(phase, value):
    def invalid(field):
        raise ControlError(
            "model_output",
            "Invalid " + field + " in " + phase + " result",
            category="model",
        )

    lists = {
        "artifacts": str,
        "paths": str,
        "evidence_refs": str,
        "evidence_paths": str,
        "references": dict,
        "tasks": dict,
    }
    for key, kind in lists.items():
        if key in value and (
            not isinstance(value[key], list)
            or any(not isinstance(item, kind) for item in value[key])
        ):
            invalid(key)
    if "checks" in value and (
        not isinstance(value["checks"], list)
        or any(not isinstance(x, (str, dict)) for x in value["checks"])
    ):
        invalid("checks")
    for key in ("accepted", "frontend", "not_required", "test_changes_valid"):
        if key in value and type(value[key]) is not bool:
            invalid(key)
    for key in ("reason", "summary", "decision", "action", "verification_command"):
        if key in value and not isinstance(value[key], str):
            invalid(key)
    for key in ("issue", "selection"):
        if key in value and not isinstance(value[key], dict):
            invalid(key)
    if value.get("persistence_change") is not None and not isinstance(
        value["persistence_change"], dict
    ):
        invalid("persistence_change")
    for task in value.get("tasks", []):
        if not isinstance(task.get("task_id"), str) or not isinstance(
            task.get("goal"), str
        ):
            invalid("task identity or goal")
        for key in ("paths", "depends_on", "requirement_ids"):
            if key in task and (
                not isinstance(task[key], list)
                or any(not isinstance(x, str) for x in task[key])
            ):
                invalid("task " + key)
        if not isinstance(task.get("checks"), list) or any(
            not isinstance(x, (str, dict)) for x in task["checks"]
        ):
            invalid("task checks")
    return value
