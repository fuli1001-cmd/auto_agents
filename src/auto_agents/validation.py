from __future__ import annotations

import re
from typing import Dict, List


TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ALLOWED_TASK_STATUS = {"pending", "blocked", "done"}


def validate_task_plan_payload(payload: object) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["task plan root must be a JSON object"]

    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return ["task plan must contain a 'tasks' list"]
    if not tasks:
        return ["task plan must contain at least one task"]
    if len(tasks) > 25:
        errors.append("task plan may contain at most 25 tasks")

    seen_ids = set()
    seen_titles = set()
    required_fields = {"task_id", "title", "description", "acceptance", "status", "commit_message"}

    for index, task in enumerate(tasks, start=1):
        prefix = f"task #{index}"
        if not isinstance(task, dict):
            errors.append(f"{prefix} must be an object")
            continue

        missing = sorted(required_fields - set(task.keys()))
        if missing:
            errors.append(f"{prefix} missing required fields: {', '.join(missing)}")

        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append(f"{prefix} has an invalid task_id")
        else:
            if not TASK_ID_PATTERN.match(task_id):
                errors.append(
                    f"{prefix} task_id '{task_id}' must match {TASK_ID_PATTERN.pattern}"
                )
            if task_id in seen_ids:
                errors.append(f"{prefix} duplicates task_id '{task_id}'")
            seen_ids.add(task_id)

        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{prefix} has an empty title")
        else:
            normalized_title = title.strip().lower()
            if normalized_title in seen_titles:
                errors.append(f"{prefix} duplicates title '{title.strip()}'")
            seen_titles.add(normalized_title)

        description = task.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{prefix} has an empty description")

        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            errors.append(f"{prefix} must have a non-empty acceptance list")
        else:
            bad_items = [
                item
                for item in acceptance
                if not isinstance(item, str) or not item.strip()
            ]
            if bad_items:
                errors.append(f"{prefix} acceptance items must be non-empty strings")

        status = task.get("status")
        if not isinstance(status, str) or status not in ALLOWED_TASK_STATUS:
            errors.append(
                f"{prefix} status must be one of: {', '.join(sorted(ALLOWED_TASK_STATUS))}"
            )

        commit_message = task.get("commit_message")
        if not isinstance(commit_message, str):
            errors.append(f"{prefix} commit_message must be a string")

    return errors

