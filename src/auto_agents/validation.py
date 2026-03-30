from __future__ import annotations

import re
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, List

from .config import architecture_path, config_path, project_brief_path, task_plan_path
from .io_utils import read_json, read_text
from .models import APPROVAL_ORDER, DOCUMENT_LANGUAGE_OPTIONS


TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ALLOWED_TASK_STATUS = {"pending", "in_progress", "blocked", "done"}
ALLOWED_EFFORTS = {"balanced", "deep"}
REQUIRED_EFFORT_STAGES = ("clarify", "design", "plan", "implement", "review", "verify")
REQUIRED_DOC_HEADINGS = {
    "project_brief.md": ("# Project Brief", "## Problem", "## MVP Scope", "## Non-Goals", "## Constraints"),
    "architecture.md": ("# Architecture", "## System Boundary", "## Core Modules", "## Data Flow", "## Risks"),
}
PYTHON_STRATEGY_HINTS = ("python", "pytest", "unittest")
PYTHON_COMMAND_HINTS = ("python", "pytest", "unittest", "coverage", ".py")
GLOBAL_INSTALL_PATTERNS = (
    (re.compile(r"(^|[\s;&|])pip(?:3)?\s+install\b"), "use the project-local conda env instead of global pip installs"),
    (
        re.compile(r"(^|[\s;&|])python(?:3)?\s+-m\s+pip\s+install\b"),
        "use 'conda run -p ./.conda python -m pip install ...' instead of system pip",
    ),
    (
        re.compile(r"(^|[\s;&|])conda\s+install\b"),
        "use a project-local conda prefix such as '.conda' instead of a shared conda environment",
    ),
    (re.compile(r"(^|[\s;&|])npm\s+install\s+-g\b"), "avoid global npm installs"),
    (re.compile(r"(^|[\s;&|])pnpm\s+add\s+-g\b"), "avoid global pnpm installs"),
    (re.compile(r"(^|[\s;&|])yarn\s+global\s+add\b"), "avoid global yarn installs"),
    (re.compile(r"(^|[\s;&|])cargo\s+install\b"), "avoid global cargo installs"),
    (re.compile(r"(^|[\s;&|])go\s+install\b"), "avoid global go installs"),
)


def _looks_like_python_workflow(test_strategy: object, commands: object) -> bool:
    strategy = str(test_strategy or "").strip().lower()
    if any(hint in strategy for hint in PYTHON_STRATEGY_HINTS):
        return True
    if not isinstance(commands, list):
        return False
    return any(_looks_like_python_command(str(item)) for item in commands)


def _looks_like_python_command(command: str) -> bool:
    lowered = command.lower()
    return any(hint in lowered for hint in PYTHON_COMMAND_HINTS)


def _uses_project_local_conda(command: str) -> bool:
    lowered = command.lower()
    if re.search(r"conda\s+run\s+(?:-p|--prefix)\s+[^\s]*\.conda(?:\s|$)", lowered):
        return True
    if re.search(r"(?:^|[\s'\"])(?:\./)?\.conda(?:/|\\\\).*(?:python|pytest|coverage)(?:\s|$)", lowered):
        return True
    return False


def _validate_isolated_commands(commands: object, field_name: str, python_required: bool) -> List[str]:
    errors: List[str] = []
    if not isinstance(commands, list):
        return errors

    for index, raw in enumerate(commands, start=1):
        if not isinstance(raw, str):
            continue
        command = raw.strip()
        lowered = command.lower()

        for pattern, message in GLOBAL_INSTALL_PATTERNS:
            if not pattern.search(lowered):
                continue
            if "conda install" in pattern.pattern and _uses_project_local_conda(command):
                continue
            if ("pip install" in pattern.pattern or "python(?:3)?\\s+-m\\s+pip\\s+install" in pattern.pattern) and _uses_project_local_conda(command):
                continue
            errors.append(f"{field_name}[{index}] must not modify shared system environments: {message}")
            break

        if python_required and _looks_like_python_command(command) and not _uses_project_local_conda(command):
            errors.append(
                f"{field_name}[{index}] must run Python verification inside a project-local conda env such as 'conda run -p ./.conda ...'"
            )

    return errors


def schema_paths() -> Dict[str, str]:
    root = Path(__file__).resolve().parents[2] / "schemas"
    return {
        "project_config": str((root / "project_config.schema.json").resolve()),
        "task_plan": str((root / "task_plan.schema.json").resolve()),
    }


def validate_task_plan_payload(payload: object, require_verification: bool = False) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["task plan root must be a JSON object"]

    test_strategy = payload.get("test_strategy")
    has_test_strategy = "test_strategy" in payload
    if has_test_strategy and test_strategy in ("", None):
        test_strategy = None
    if test_strategy is not None and (not isinstance(test_strategy, str) or not test_strategy.strip()):
        errors.append("task plan test_strategy must be a non-empty string when provided")

    verification_commands = payload.get("verification_commands")
    has_verification_commands = "verification_commands" in payload
    if has_verification_commands and verification_commands == []:
        verification_commands = None
    if verification_commands is not None:
        if not isinstance(verification_commands, list) or not verification_commands:
            errors.append("task plan verification_commands must be a non-empty list when provided")
        elif any(not isinstance(item, str) or not item.strip() for item in verification_commands):
            errors.append("task plan verification_commands items must be non-empty strings")

    if require_verification:
        if not isinstance(test_strategy, str) or not test_strategy.strip():
            errors.append("task plan must define a non-empty test_strategy")
        if not isinstance(verification_commands, list) or not verification_commands:
            errors.append("task plan must define at least one verification command")
    python_required = _looks_like_python_workflow(test_strategy, verification_commands)
    errors.extend(
        _validate_isolated_commands(
            verification_commands,
            "task plan verification_commands",
            python_required=python_required,
        )
    )

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


def validate_project_config_payload(payload: object) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["project config root must be a JSON object"]

    required = {"project_name", "provider", "docs", "efforts", "gates", "git", "approvals", "retries"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        errors.append(f"project config missing required fields: {', '.join(missing)}")

    project_name = payload.get("project_name")
    if not isinstance(project_name, str) or not project_name.strip():
        errors.append("project_name must be a non-empty string")

    provider = payload.get("provider")
    if not isinstance(provider, dict):
        errors.append("provider must be an object")
    else:
        for key in ("kind", "binary", "cwd_flag", "output_flag"):
            value = provider.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"provider.{key} must be a non-empty string")

        prompt_via_stdin = provider.get("prompt_via_stdin")
        if not isinstance(prompt_via_stdin, bool):
            errors.append("provider.prompt_via_stdin must be a boolean")

        extra_args = provider.get("extra_args")
        if not isinstance(extra_args, list) or any(not isinstance(item, str) for item in extra_args):
            errors.append("provider.extra_args must be a list of strings")

        profile_map = provider.get("profile_map")
        if not isinstance(profile_map, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in profile_map.items()
        ):
            errors.append("provider.profile_map must be an object of string keys and string values")

    efforts = payload.get("efforts")
    if not isinstance(efforts, dict):
        errors.append("efforts must be an object")
    else:
        missing_stages = [stage for stage in REQUIRED_EFFORT_STAGES if stage not in efforts]
        if missing_stages:
            errors.append(f"efforts missing stages: {', '.join(missing_stages)}")
        for stage in REQUIRED_EFFORT_STAGES:
            value = efforts.get(stage)
            if not isinstance(value, str) or value not in ALLOWED_EFFORTS:
                errors.append(
                    f"efforts.{stage} must be one of: {', '.join(sorted(ALLOWED_EFFORTS))}"
                )

    docs = payload.get("docs")
    if not isinstance(docs, dict):
        errors.append("docs must be an object")
    else:
        language = docs.get("language")
        if not isinstance(language, str) or language not in DOCUMENT_LANGUAGE_OPTIONS:
            errors.append(
                f"docs.language must be one of: {', '.join(sorted(DOCUMENT_LANGUAGE_OPTIONS))}"
            )

    gates = payload.get("gates")
    if not isinstance(gates, dict):
        errors.append("gates must be an object")
    else:
        commands = gates.get("commands")
        if not isinstance(commands, list) or any(not isinstance(item, str) for item in commands):
            errors.append("gates.commands must be a list of strings")
        else:
            python_required = _looks_like_python_workflow(None, commands)
            errors.extend(
                _validate_isolated_commands(
                    commands,
                    "gates.commands",
                    python_required=python_required,
                )
            )
        clean = gates.get("require_clean_git_before_task")
        if not isinstance(clean, bool):
            errors.append("gates.require_clean_git_before_task must be a boolean")
        allow_agent_updates = gates.get("allow_agent_updates")
        if not isinstance(allow_agent_updates, bool):
            errors.append("gates.allow_agent_updates must be a boolean")

    git = payload.get("git")
    if not isinstance(git, dict):
        errors.append("git must be an object")
    else:
        for key in ("auto_init_repo", "commit_each_task"):
            if not isinstance(git.get(key), bool):
                errors.append(f"git.{key} must be a boolean")
        template = git.get("commit_message_template")
        if not isinstance(template, str) or not template.strip():
            errors.append("git.commit_message_template must be a non-empty string")
        else:
            if "{task_id}" not in template:
                errors.append("git.commit_message_template must contain '{task_id}'")
            if "{title}" not in template:
                errors.append("git.commit_message_template must contain '{title}'")

    approvals = payload.get("approvals")
    if not isinstance(approvals, dict):
        errors.append("approvals must be an object")
    else:
        enabled = approvals.get("enabled")
        if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
            errors.append("approvals.enabled must be a list of strings")
        else:
            invalid = [item for item in enabled if item not in APPROVAL_ORDER]
            if invalid:
                errors.append(
                    f"approvals.enabled contains invalid values: {', '.join(sorted(invalid))}"
                )

    retries = payload.get("retries")
    if not isinstance(retries, dict):
        errors.append("retries must be an object")
    else:
        default_max_attempts = retries.get("default_max_attempts")
        if not isinstance(default_max_attempts, int) or default_max_attempts < 1:
            errors.append("retries.default_max_attempts must be an integer >= 1")

        per_stage = retries.get("per_stage")
        if not isinstance(per_stage, dict):
            errors.append("retries.per_stage must be an object")
        else:
            for key, value in per_stage.items():
                if key not in ("clarify", "design", "plan", "implement", "review"):
                    errors.append(f"retries.per_stage contains unknown stage '{key}'")
                if not isinstance(value, int) or value < 1:
                    errors.append(f"retries.per_stage.{key} must be an integer >= 1")

    return errors


def validate_project_root(project_root: Path) -> Dict[str, List[str]]:
    root = project_root.resolve()
    errors: List[str] = []
    warnings: List[str] = []

    try:
        config_payload = read_json(config_path(root), default=None)
    except JSONDecodeError as error:
        config_payload = None
        errors.append(f"config file is not valid JSON: {config_path(root)} ({error.msg})")
    if config_payload is None and not errors:
        errors.append(f"missing config file: {config_path(root)}")
    elif config_payload is not None:
        errors.extend(validate_project_config_payload(config_payload))

    try:
        plan_payload = read_json(task_plan_path(root), default=None)
    except JSONDecodeError as error:
        plan_payload = None
        errors.append(f"task plan file is not valid JSON: {task_plan_path(root)} ({error.msg})")
    if plan_payload is None and not any("task plan file is not valid JSON" in item for item in errors):
        errors.append(f"missing task plan file: {task_plan_path(root)}")
    elif plan_payload is not None:
        errors.extend(validate_task_plan_payload(plan_payload))

    docs = {
        "project_brief.md": project_brief_path(root),
        "architecture.md": architecture_path(root),
    }
    for name, path in docs.items():
        errors.extend(validate_required_document(path, name))

    review_file = root / ".auto-agents" / "docs" / "review.md"
    if not review_file.exists():
        warnings.append(f"review file not found yet: {review_file}")

    if config_payload is not None and plan_payload is not None:
        gate_commands = config_payload.get("gates", {}).get("commands", [])
        plan_commands = plan_payload.get("verification_commands", [])
        if not gate_commands and not plan_commands:
            warnings.append("no verification commands are configured yet; verify stage will be a no-op")

    return {"errors": errors, "warnings": warnings}


def validation_report(project_root: Path) -> Dict[str, object]:
    result = validate_project_root(project_root)
    return {
        "ok": not result["errors"],
        "errors": result["errors"],
        "warnings": result["warnings"],
        "schemas": schema_paths(),
    }


def validate_required_document(path: Path, name: str) -> List[str]:
    errors: List[str] = []
    if not path.exists():
        return [f"missing required document: {path}"]

    content = read_text(path).strip()
    if not content:
        return [f"document is empty: {path}"]

    for heading in REQUIRED_DOC_HEADINGS[name]:
        if heading not in content:
            errors.append(f"{path} is missing heading '{heading}'")
    return errors
