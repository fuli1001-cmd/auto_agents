from __future__ import annotations

import re
import shlex
from json import JSONDecodeError
from pathlib import Path
from typing import Dict, Iterable, List

from .config import architecture_path, config_path, project_brief_path, requirements_trace_path, run_state_path, task_plan_path
from .frontend_fidelity import validate_frontend_fidelity_task_plan
from .frontend_design import (
    load_frontend_design_lock,
    validate_frontend_design_artifacts,
    validate_frontend_scope,
)
from .io_utils import read_json, read_text
from .models import (
    APPROVAL_ORDER,
    DEFAULT_EFFORTS,
    DOCUMENT_LANGUAGE_OPTIONS,
    SMART_TIMEOUT_PROGRESS_PROTOCOL,
    TASK_ORIGINS,
    VERIFICATION_CACHE_SCOPES,
    VERIFICATION_CADENCES,
    VERIFICATION_MEMORY_GUARDS,
    VERIFICATION_RESULT_CACHE_SCOPES,
    VERIFICATION_RESOURCE_CLASSES,
    VERIFICATION_SERIAL_REASONS,
)
from .requirements import (
    normalize_requirements_trace_payload,
    validate_requirements_trace_payload,
    validate_task_requirement_coverage,
)


TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ALLOWED_TASK_STATUS = {"pending", "in_progress", "blocked", "done"}
ALLOWED_EFFORTS = {"balanced", "deep", "max"}
REQUIRED_EFFORT_STAGES = tuple(DEFAULT_EFFORTS)
DEFAULTED_EFFORT_STAGES = {
    "prototype",
    "sync-agent-instructions",
    "provider_research",
    "arbiter",
    "incident_judge",
    "visual_judge",
    "self_repair",
    "evidence_preflight",
}
MAX_ACCEPTANCE_WITHOUT_SCOPE_RATIONALE = 5
MAX_ACCEPTANCE_HARD_LIMIT = 7
REQUIRED_DOC_HEADINGS = {
    "project_brief.md": ("# Project Brief", "## Problem", "## MVP Scope", "## Non-Goals", "## Constraints"),
    "architecture.md": ("# Architecture", "## System Boundary", "## Core Modules", "## Data Flow", "## Risks"),
}
PYTHON_STRATEGY_HINTS = ("python", "pytest", "unittest")
PYTHON_COMMAND_HINTS = ("python", "pytest", "unittest", "coverage", ".py")
SUPPORTED_VERIFICATION_TEST_RUNNERS = {"pytest", "vitest"}
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
CONDA_RUN_VALUE_OPTIONS = {"-n", "--name", "-p", "--prefix", "--cwd"}
PYTEST_VALUE_OPTIONS = {
    "-c",
    "--confcutdir",
    "--durations",
    "--ignore",
    "--ignore-glob",
    "--junitxml",
    "--log-file",
    "--maxfail",
    "--rootdir",
    "-k",
    "-m",
}


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
        if re.search(r"\bpython(?:3)?\s+-m\s+unittest\b|\bunittest\s+discover\b", lowered):
            errors.append(
                f"{field_name}[{index}] uses unittest; Python verification must use pytest"
            )

    return errors


def _validate_parallel_gate_groups(parallel_groups: object) -> List[str]:
    errors: List[str] = []
    if parallel_groups is None:
        return errors
    if not isinstance(parallel_groups, list):
        return ["gates.parallel_groups must be a list of objects"]
    for index, group in enumerate(parallel_groups, start=1):
        prefix = f"gates.parallel_groups[{index}]"
        if not isinstance(group, dict):
            errors.append(f"{prefix} must be an object")
            continue
        name = group.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}.name must be a non-empty string")
        commands = group.get("commands")
        if (
            not isinstance(commands, list)
            or not commands
            or any(not isinstance(item, str) or not item.strip() for item in commands)
        ):
            errors.append(f"{prefix}.commands must be a non-empty list of strings")
            continue
        python_required = _looks_like_python_workflow(None, commands)
        errors.extend(
            _validate_isolated_commands(
                commands,
                f"{prefix}.commands",
                python_required=python_required,
            )
        )
    return errors


def validate_verification_steps(
    steps: object,
    field_name: str = "verification_steps",
    *,
    policy_version: int = 1,
) -> List[str]:
    errors: List[str] = []
    if steps is None:
        return errors
    if not isinstance(steps, list):
        return [f"{field_name} must be a list of objects"]
    if not steps:
        return errors
    artifact_owners: Dict[str, str] = {}
    for index, raw_step in enumerate(steps, start=1):
        prefix = f"{field_name}[{index}]"
        if not isinstance(raw_step, dict):
            errors.append(f"{prefix} must be an object")
            continue
        kind = str(raw_step.get("kind", "test")).strip().lower()
        runner = str(raw_step.get("runner", "")).strip().lower()
        if kind != "test":
            errors.append(f"{prefix}.kind must be 'test'")
        if runner not in SUPPORTED_VERIFICATION_TEST_RUNNERS:
            allowed = ", ".join(sorted(SUPPORTED_VERIFICATION_TEST_RUNNERS))
            errors.append(f"{prefix}.runner must be one of: {allowed}")
        purpose = str(raw_step.get("purpose", "")).strip()
        if policy_version >= 2 and not purpose:
            errors.append(f"{prefix}.purpose is required under verification policy v2")
        targets = raw_step.get("targets", [])
        if targets is not None and (
            not isinstance(targets, list)
            or any(not isinstance(item, str) or not item.strip() for item in targets)
        ):
            errors.append(f"{prefix}.targets must be a list of non-empty strings when provided")
        args = raw_step.get("args", [])
        if args is not None and (
            not isinstance(args, list)
            or any(not isinstance(item, str) or not item.strip() for item in args)
        ):
            errors.append(f"{prefix}.args must be a list of non-empty strings when provided")
        command = str(raw_step.get("command", "")).strip()
        if command:
            errors.append(f"{prefix}.command is not allowed for structured test steps")
        if "parallel_safe" in raw_step and not isinstance(raw_step.get("parallel_safe"), bool):
            errors.append(f"{prefix}.parallel_safe must be a boolean when provided")
        max_batches = raw_step.get("max_batches", 0)
        if type(max_batches) is not int or max_batches < 0:
            errors.append(
                f"{prefix}.max_batches must be a non-negative integer when provided"
            )
        parallel_safe = bool(raw_step.get("parallel_safe", False))
        serial_reason = str(raw_step.get("serial_reason", "")).strip().lower()
        if serial_reason and serial_reason not in VERIFICATION_SERIAL_REASONS:
            allowed = ", ".join(VERIFICATION_SERIAL_REASONS)
            errors.append(f"{prefix}.serial_reason must be one of: {allowed}")
        if policy_version >= 2:
            if parallel_safe and serial_reason:
                errors.append(
                    f"{prefix}.serial_reason must be empty when parallel_safe is true"
                )
            if not parallel_safe and not serial_reason:
                errors.append(
                    f"{prefix}.serial_reason is required when parallel_safe is false"
                )
        cadence = str(raw_step.get("cadence", "implement_and_final")).strip().lower()
        if cadence not in VERIFICATION_CADENCES:
            allowed = ", ".join(VERIFICATION_CADENCES)
            errors.append(f"{prefix}.cadence must be one of: {allowed}")
        cache_scope = str(raw_step.get("cache_scope", "run_context")).strip().lower()
        if cache_scope not in VERIFICATION_CACHE_SCOPES:
            allowed = ", ".join(VERIFICATION_CACHE_SCOPES)
            errors.append(f"{prefix}.cache_scope must be one of: {allowed}")
        result_cache_scope = str(
            raw_step.get(
                "result_cache_scope",
                "candidate" if policy_version >= 2 else "off",
            )
        ).strip().lower()
        if result_cache_scope not in VERIFICATION_RESULT_CACHE_SCOPES:
            allowed = ", ".join(VERIFICATION_RESULT_CACHE_SCOPES)
            errors.append(f"{prefix}.result_cache_scope must be one of: {allowed}")
        resource_class = str(raw_step.get("resource_class", "normal")).strip().lower()
        if resource_class not in VERIFICATION_RESOURCE_CLASSES:
            allowed = ", ".join(VERIFICATION_RESOURCE_CLASSES)
            errors.append(f"{prefix}.resource_class must be one of: {allowed}")
        for field in ("cpu_slots", "memory_mb", "memory_reserve_mb"):
            value = raw_step.get(field, 0)
            if type(value) is not int or value < 0:
                errors.append(
                    f"{prefix}.{field} must be a non-negative integer when provided"
                )
        memory_guard = str(raw_step.get("memory_guard", "off")).strip().lower()
        if memory_guard not in VERIFICATION_MEMORY_GUARDS:
            allowed = ", ".join(VERIFICATION_MEMORY_GUARDS)
            errors.append(f"{prefix}.memory_guard must be one of: {allowed}")
        elif (
            memory_guard != "off"
            and (
                type(raw_step.get("memory_mb", 0)) is not int
                or raw_step.get("memory_mb", 0) <= 0
            )
        ):
            errors.append(
                f"{prefix}.memory_mb must be positive when memory_guard is "
                f"'{memory_guard}'"
            )
        for field in (
            "requires",
            "exclusive_resources",
            "dynamic_ports",
            "artifact_globs",
        ):
            value = raw_step.get(field, [])
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) or not item.strip() for item in value)
            ):
                errors.append(
                    f"{prefix}.{field} must be a list of non-empty strings when provided"
                )
        exclusive = raw_step.get("exclusive_resources", [])
        if isinstance(exclusive, list):
            for item in exclusive:
                if isinstance(item, str) and (
                    not item.startswith(("host:", "pool:"))
                    or not item.split(":", 1)[1].strip()
                ):
                    errors.append(
                        f"{prefix}.exclusive_resources entries must be host:<name> or pool:<name>"
                    )
        dynamic_ports = raw_step.get("dynamic_ports", [])
        if isinstance(dynamic_ports, list):
            seen_ports: set[str] = set()
            for item in dynamic_ports:
                if not isinstance(item, str):
                    continue
                name = item.strip()
                if (
                    not name
                    or not name[0].islower()
                    or any(
                        character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                        for character in name
                    )
                ):
                    errors.append(
                        f"{prefix}.dynamic_ports entries must use lowercase snake_case names"
                    )
                    continue
                if name in seen_ports:
                    errors.append(
                        f"{prefix}.dynamic_ports entries must be unique"
                    )
                seen_ports.add(name)
        artifacts = raw_step.get("artifact_globs", [])
        if isinstance(artifacts, list):
            for item in artifacts:
                if not isinstance(item, str):
                    continue
                normalized = item.replace("\\", "/")
                if normalized.startswith("/") or ".." in normalized.split("/"):
                    errors.append(
                        f"{prefix}.artifact_globs entries must be safe project-relative globs"
                    )
                if policy_version >= 2 and normalized:
                    owner = artifact_owners.get(normalized)
                    if owner is not None:
                        errors.append(
                            f"{prefix}.artifact_globs duplicates artifact ownership "
                            f"from {owner}: {normalized}"
                        )
                    else:
                        artifact_owners[normalized] = prefix
        if policy_version >= 2:
            targets_list = targets if isinstance(targets, list) else []
            if (
                cadence == "implement_and_final"
                and any(
                    "::" not in str(target)
                    and not re.search(
                        r"\.(?:py|[cm]?[jt]sx?)$",
                        str(target),
                        re.IGNORECASE,
                    )
                    for target in targets_list
                )
            ):
                errors.append(
                    f"{prefix} broad directory targets must use cadence='final_only'"
                )
            if result_cache_scope == "observed_inputs":
                if cache_scope != "source":
                    errors.append(
                        f"{prefix}.result_cache_scope observed_inputs requires cache_scope='source'"
                    )
                if not parallel_safe:
                    errors.append(
                        f"{prefix}.result_cache_scope observed_inputs requires parallel_safe=true"
                    )
                if any(
                    isinstance(raw_step.get(field, []), list)
                    and raw_step.get(field, [])
                    for field in (
                        "artifact_globs",
                        "exclusive_resources",
                        "dynamic_ports",
                    )
                ):
                    errors.append(
                        f"{prefix}.result_cache_scope observed_inputs cannot use artifacts, "
                        "exclusive resources, or dynamic ports"
                    )
    return errors


def _unwrap_conda_run(parts: List[str]) -> List[str]:
    if len(parts) < 2 or parts[0] != "conda" or parts[1] != "run":
        return parts

    index = 2
    while index < len(parts):
        token = parts[index]
        if token == "--":
            return parts[index + 1 :]
        if token in CONDA_RUN_VALUE_OPTIONS:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in CONDA_RUN_VALUE_OPTIONS):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return parts[index:]
    return []


def _pytest_target_candidates(command: str) -> List[str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return []

    parts = _unwrap_conda_run(parts)
    if not parts:
        return []

    executable = Path(parts[0]).name
    args: List[str]
    if executable in {"pytest", "py.test"}:
        args = parts[1:]
    elif len(parts) >= 3 and Path(parts[0]).name in {"python", "python3"} and parts[1] == "-m" and parts[2] == "pytest":
        args = parts[3:]
    else:
        return []

    targets: List[str] = []
    skip_next = False
    option_parsing_done = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            option_parsing_done = True
            continue
        if not option_parsing_done and arg.startswith("-"):
            if arg in PYTEST_VALUE_OPTIONS and "=" not in arg:
                skip_next = True
            continue
        targets.append(arg.split("::", 1)[0])
    return [target for target in targets if target and target not in {".", ".."}]


def validate_verification_command_paths(commands: object, project_root: Path, field_name: str) -> List[str]:
    errors: List[str] = []
    if not isinstance(commands, list):
        return errors

    for index, raw in enumerate(commands, start=1):
        if not isinstance(raw, str):
            continue
        command = raw.strip()
        if not command:
            continue
        for target in _pytest_target_candidates(command):
            if not target.endswith(".py") and ".py::" not in target:
                continue
            candidate = Path(target)
            resolved = candidate if candidate.is_absolute() else (project_root / candidate).resolve()
            if resolved.exists():
                continue
            errors.append(f"{field_name}[{index}] references missing pytest target: {target}")
    return errors


def schema_paths() -> Dict[str, str]:
    root = Path(__file__).resolve().parents[2] / "schemas"
    return {
        "project_config": str((root / "project_config.schema.json").resolve()),
        "task_plan": str((root / "task_plan.schema.json").resolve()),
        "requirements_trace": str((root / "requirements_trace.schema.json").resolve()),
    }


def validate_task_dependencies(
    tasks: object,
    *,
    require_depends_on_for_pending: bool = False,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(tasks, list):
        return errors

    known_ids = {
        str(task.get("task_id")).strip()
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("task_id"), str) and str(task.get("task_id")).strip()
    }
    dependency_map: Dict[str, List[str]] = {}

    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        prefix = f"task #{index}"
        task_id = str(task.get("task_id", "")).strip()
        status = str(task.get("status", "pending")).strip()
        depends_on = task.get("depends_on")
        if depends_on is None:
            if require_depends_on_for_pending and status != "done":
                errors.append(
                    f"{prefix} depends_on must be present for non-done tasks when execution.parallel_tasks.strict is enabled"
                )
            if task_id:
                dependency_map[task_id] = []
            continue
        if (
            not isinstance(depends_on, list)
            or any(not isinstance(item, str) or not item.strip() for item in depends_on)
        ):
            errors.append(f"{prefix} depends_on must be a list of non-empty strings")
            continue
        normalized = [item.strip() for item in depends_on]
        if len(set(normalized)) != len(normalized):
            errors.append(f"{prefix} depends_on must not contain duplicates")
        if task_id:
            dependency_map[task_id] = normalized

    for task_id, dependencies in dependency_map.items():
        for dependency in dependencies:
            if dependency == task_id:
                errors.append(f"task '{task_id}' cannot depend on itself")
                continue
            if dependency not in known_ids:
                errors.append(f"task '{task_id}' depends_on unknown task '{dependency}'")

    cycle_reported = False
    visiting: List[str] = []
    visited = set()

    def visit(node: str) -> None:
        nonlocal cycle_reported
        if node in visited or cycle_reported:
            return
        if node in visiting:
            cycle = visiting[visiting.index(node) :] + [node]
            errors.append("tasks contain cyclic depends_on relationship: " + " -> ".join(cycle))
            cycle_reported = True
            return
        visiting.append(node)
        for dependency in dependency_map.get(node, []):
            if dependency in known_ids and dependency != node:
                visit(dependency)
        visiting.pop()
        visited.add(node)

    for task_id in sorted(known_ids):
        visit(task_id)
        if cycle_reported:
            break

    return errors


def validate_task_plan_payload(
    payload: object,
    require_verification: bool = False,
    *,
    allow_empty_tasks: bool = False,
    require_depends_on_for_pending: bool = False,
    enforce_active_task_granularity: bool = False,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["task plan root must be a JSON object"]

    verification_policy_version = payload.get("verification_policy_version", 1)
    if (
        not isinstance(verification_policy_version, int)
        or isinstance(verification_policy_version, bool)
        or verification_policy_version not in {1, 2}
    ):
        errors.append("task plan verification_policy_version must be 1 or 2")
        verification_policy_version = 1

    test_strategy = payload.get("test_strategy")
    has_test_strategy = "test_strategy" in payload
    if has_test_strategy and test_strategy in ("", None):
        test_strategy = None
    if test_strategy is not None and (not isinstance(test_strategy, str) or not test_strategy.strip()):
        errors.append("task plan test_strategy must be a non-empty string when provided")

    verification_commands = payload.get("verification_commands")
    verification_steps = payload.get("verification_steps")
    has_verification_steps = "verification_steps" in payload
    if has_verification_steps and verification_steps == []:
        verification_steps = None
    if verification_steps is not None:
        errors.extend(
            validate_verification_steps(
                verification_steps,
                "task plan verification_steps",
                policy_version=verification_policy_version,
            )
        )
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
        if (
            not isinstance(verification_steps, list)
            or not verification_steps
        ) and (
            not isinstance(verification_commands, list)
            or not verification_commands
        ):
            errors.append("task plan must define at least one verification step")
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
        if allow_empty_tasks:
            return errors
        return ["task plan must contain at least one task"]

    seen_ids = set()
    required_fields = {"task_id", "title", "description", "acceptance", "status", "commit_message"}
    implement_targets = {
        str(target).strip()
        for step in (verification_steps or [])
        if isinstance(step, dict)
        and str(step.get("cadence", "implement_and_final")).strip().lower()
        == "implement_and_final"
        for target in (step.get("targets", []) or [])
        if isinstance(target, str) and target.strip()
    }

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
            if enforce_active_task_granularity and task.get("status") != "done":
                acceptance_count = len(acceptance)
                if acceptance_count > MAX_ACCEPTANCE_HARD_LIMIT:
                    errors.append(
                        f"{prefix} has {acceptance_count} acceptance criteria; active tasks with more than "
                        f"{MAX_ACCEPTANCE_HARD_LIMIT} criteria must be split"
                    )
                elif acceptance_count > MAX_ACCEPTANCE_WITHOUT_SCOPE_RATIONALE:
                    scope_boundaries = str(task.get("scope_boundaries", "")).strip()
                    if not scope_boundaries:
                        errors.append(
                            f"{prefix} has {acceptance_count} acceptance criteria; split the active task "
                            "or add scope_boundaries explaining why it remains one coherent slice"
                        )

        status = task.get("status")
        if not isinstance(status, str) or status not in ALLOWED_TASK_STATUS:
            errors.append(
                f"{prefix} status must be one of: {', '.join(sorted(ALLOWED_TASK_STATUS))}"
            )

        commit_message = task.get("commit_message")
        if not isinstance(commit_message, str):
            errors.append(f"{prefix} commit_message must be a string")

        requirement_ids = task.get("requirement_ids", [])
        if requirement_ids is not None and (
            not isinstance(requirement_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in requirement_ids)
        ):
            errors.append(f"{prefix} requirement_ids must be a list of non-empty strings")
        verification_refs = task.get("verification_refs", [])
        if verification_refs is not None and (
            not isinstance(verification_refs, list)
            or any(not isinstance(item, str) or not item.strip() for item in verification_refs)
        ):
            errors.append(f"{prefix} verification_refs must be a list of non-empty strings")
        if (
            verification_policy_version >= 2
            and status != "done"
            and (
                not isinstance(verification_refs, list)
                or not verification_refs
            )
        ):
            errors.append(
                f"{prefix} verification_refs must contain at least one executable ref "
                "under verification policy v2"
            )
        elif (
            verification_policy_version >= 2
            and isinstance(verification_refs, list)
        ):
            for ref in verification_refs:
                if not isinstance(ref, str):
                    continue
                normalized = ref.strip()
                if normalized.startswith("cmd:") and normalized[4:].strip():
                    continue
                path, separator, selector = normalized.partition("::")
                is_test_file = bool(
                    re.search(
                        r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.py)$",
                        path.replace("\\", "/"),
                        re.IGNORECASE,
                    )
                    or re.search(
                        r"\.(?:test|spec)\.[cm]?[jt]sx?$",
                        path,
                        re.IGNORECASE,
                    )
                )
                if not is_test_file:
                    errors.append(
                        f"{prefix} verification_refs entry is not executable: {normalized}"
                    )
                    continue
                if separator and selector.strip():
                    continue
                if path not in implement_targets:
                    errors.append(
                        f"{prefix} whole-file verification ref must map to an "
                        f"implement_and_final step or use an exact selector: {normalized}"
                    )
        recovery_history = task.get("recovery_history", [])
        if recovery_history is not None and (
            not isinstance(recovery_history, list)
            or any(not isinstance(item, dict) for item in recovery_history)
        ):
            errors.append(f"{prefix} recovery_history must be a list of objects")
        task_origin = task.get("task_origin", "planned")
        if not isinstance(task_origin, str) or task_origin not in TASK_ORIGINS:
            errors.append(
                f"{prefix} task_origin must be one of: {', '.join(TASK_ORIGINS)}"
            )
        for field_name in (
            "recovery_epoch",
            "recovery_round",
            "verify_retry_epoch",
        ):
            value = task.get(field_name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{prefix} {field_name} must be an integer >= 0")

    errors.extend(
        validate_task_dependencies(
            tasks,
            require_depends_on_for_pending=require_depends_on_for_pending,
        )
    )
    return errors


def validate_task_plan_with_requirements(
    plan_payload: object,
    trace_payload: object,
    *,
    enforce_active_task_granularity: bool = False,
    historical_tasks: Iterable[dict] = (),
) -> List[str]:
    errors = validate_task_plan_payload(
        plan_payload,
        require_verification=True,
        enforce_active_task_granularity=enforce_active_task_granularity,
    )
    if isinstance(trace_payload, dict):
        trace_payload = normalize_requirements_trace_payload(trace_payload)
        trace_errors = validate_requirements_trace_payload(trace_payload)
        errors.extend(trace_errors)
        if not trace_errors:
            errors.extend(
                validate_task_requirement_coverage(
                    plan_payload,
                    trace_payload,
                    historical_tasks=historical_tasks,
                )
            )
            errors.extend(
                validate_frontend_fidelity_task_plan(
                    plan_payload,
                    trace_payload,
                    historical_tasks=historical_tasks,
                )
            )
    return errors


def task_plan_warnings(payload: object) -> List[str]:
    warnings: List[str] = []
    if not isinstance(payload, dict):
        return warnings

    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return warnings

    active_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and str(task.get("status", "pending")) != "done"
    ]
    active_task_count = len(active_tasks)
    if active_task_count > 25:
        warnings.append(
            f"task plan contains {active_task_count} active tasks; review whether the work is oversliced into too many tiny tasks"
        )
    if active_task_count > 60:
        warnings.append(
            "task plan has an unusually large number of active tasks; confirm the project truly needs this many independently verified slices"
        )

    very_short_titles = 0
    very_short_descriptions = 0
    single_acceptance_tasks = 0
    for task in active_tasks:
        title = str(task.get("title", "")).strip()
        description = str(task.get("description", "")).strip()
        acceptance = task.get("acceptance")
        if title and len(title) <= 8:
            very_short_titles += 1
        if description and len(description) <= 30:
            very_short_descriptions += 1
        if isinstance(acceptance, list) and len([item for item in acceptance if isinstance(item, str) and item.strip()]) == 1:
            single_acceptance_tasks += 1

    if active_task_count >= 12 and very_short_titles >= max(4, active_task_count // 3):
        warnings.append(
            "many active task titles are extremely short; confirm tasks are not split into trivial bookkeeping steps"
        )
    if active_task_count >= 12 and very_short_descriptions >= max(4, active_task_count // 3):
        warnings.append(
            "many active task descriptions are very short; confirm each task is still a meaningful, independently verifiable slice"
        )
    if active_task_count >= 15 and single_acceptance_tasks >= max(6, active_task_count // 2):
        warnings.append(
            "many active tasks have only one acceptance criterion; confirm the plan is not over-fragmented"
        )

    oversized_tasks = []
    verbose_tasks = []
    duplicate_titles = []
    seen_titles = set()
    for task in active_tasks:
        tid = str(task.get("task_id", "?"))
        title = str(task.get("title", "")).strip()
        normalized_title = title.lower()
        if title and normalized_title in seen_titles:
            duplicate_titles.append(f"{tid} ('{title}')")
        elif title:
            seen_titles.add(normalized_title)
        acceptance = task.get("acceptance")
        description = str(task.get("description", ""))
        if isinstance(acceptance, list) and len(acceptance) > 5:
            oversized_tasks.append(f"{tid} ({len(acceptance)} criteria)")
        if len(description) > 500:
            verbose_tasks.append(f"{tid} ({len(description)} chars)")
    if duplicate_titles:
        warnings.append(
            f"active tasks with duplicate titles (consider making titles more specific): {', '.join(duplicate_titles)}"
        )
    if oversized_tasks:
        warnings.append(
            f"active tasks with >5 acceptance criteria (consider splitting): {', '.join(oversized_tasks)}"
        )
    if verbose_tasks:
        warnings.append(
            f"active tasks with very long descriptions >500 chars (may be too broad): {', '.join(verbose_tasks)}"
        )

    return warnings


def _allow_empty_task_plan_for_iteration(project_root: Path) -> bool:
    state_payload = read_json(run_state_path(project_root), default=None)
    if not isinstance(state_payload, dict):
        return False
    context = state_payload.get("resume_context", {})
    if not isinstance(context, dict) or not str(context.get("previous_run_id", "")).strip():
        return False
    summaries = state_payload.get("stage_summaries", {})
    if isinstance(summaries, dict) and "plan" in summaries:
        return False
    current_stage = str(state_payload.get("current_stage", "clarify")).strip()
    if current_stage not in {"clarify", "prototype", "design", "plan"}:
        return False
    return str(state_payload.get("status", "pending")).strip() != "completed"


def validate_project_config_payload(payload: object) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["project config root must be a JSON object"]

    required = {"project_name", "providers", "active_provider", "efforts", "gates", "git", "approvals", "retries"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        errors.append(f"project config missing required fields: {', '.join(missing)}")

    project_name = payload.get("project_name")
    if not isinstance(project_name, str) or not project_name.strip():
        errors.append("project_name must be a non-empty string")
    if "agent_instructions" in payload:
        errors.append(
            "agent_instructions is no longer supported; use efforts.sync-agent-instructions instead"
        )

    providers = payload.get("providers")
    if not isinstance(providers, dict) or not providers:
        errors.append("providers must be a non-empty object")
    else:
        for provider_name, provider in providers.items():
            if not isinstance(provider_name, str) or not provider_name.strip():
                errors.append("providers keys must be non-empty strings")
                continue
            if not isinstance(provider, dict):
                errors.append(f"providers.{provider_name} must be an object")
                continue

            for key in ("kind", "binary"):
                value = provider.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"providers.{provider_name}.{key} must be a non-empty string")

            for key in ("cwd_flag", "output_flag"):
                value = provider.get(key)
                if not isinstance(value, str):
                    errors.append(f"providers.{provider_name}.{key} must be a string")

            prompt_via_stdin = provider.get("prompt_via_stdin")
            if not isinstance(prompt_via_stdin, bool):
                errors.append(f"providers.{provider_name}.prompt_via_stdin must be a boolean")

            extra_args = provider.get("extra_args")
            if not isinstance(extra_args, list) or any(not isinstance(item, str) for item in extra_args):
                errors.append(f"providers.{provider_name}.extra_args must be a list of strings")

            profile_map = provider.get("profile_map")
            if not isinstance(profile_map, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in profile_map.items()
            ):
                errors.append(
                    f"providers.{provider_name}.profile_map must be an object of string keys and string values"
                )
            vision = str(provider.get("vision", "auto"))
            if vision not in {"auto", "enabled", "disabled"}:
                errors.append(f"providers.{provider_name}.vision must be one of: auto, enabled, disabled")
            if "progress_protocol" in provider and not isinstance(
                provider.get("progress_protocol"), str
            ):
                errors.append(
                    f"providers.{provider_name}.progress_protocol must be a string"
                )

    active_provider = payload.get("active_provider")
    if not isinstance(active_provider, str) or not active_provider.strip():
        errors.append("active_provider must be a non-empty string")
    elif isinstance(providers, dict) and active_provider not in providers:
        errors.append("active_provider must be one of providers keys")

    efforts = payload.get("efforts")
    if not isinstance(efforts, dict):
        errors.append("efforts must be an object")
    else:
        missing_stages = [
            stage
            for stage in REQUIRED_EFFORT_STAGES
            if stage not in efforts and stage not in DEFAULTED_EFFORT_STAGES
        ]
        if missing_stages:
            errors.append(f"efforts missing stages: {', '.join(missing_stages)}")
        for stage, value in {**DEFAULT_EFFORTS, **efforts}.items():
            if not isinstance(value, str) or value not in ALLOWED_EFFORTS:
                errors.append(
                    f"efforts.{stage} must be one of: {', '.join(sorted(ALLOWED_EFFORTS))}"
                )

    docs = payload.get("docs")
    if docs is not None:
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
        verification_policy_version = gates.get("verification_policy_version", 1)
        if (
            not isinstance(verification_policy_version, int)
            or isinstance(verification_policy_version, bool)
            or verification_policy_version not in {1, 2}
        ):
            errors.append("gates.verification_policy_version must be 1 or 2")
            verification_policy_version = 1
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
        errors.extend(
            validate_verification_steps(
                gates.get("steps", []),
                "gates.steps",
                policy_version=verification_policy_version,
            )
        )
        errors.extend(_validate_parallel_gate_groups(gates.get("parallel_groups", [])))
        clean = gates.get("require_clean_git_before_task")
        if not isinstance(clean, bool):
            errors.append("gates.require_clean_git_before_task must be a boolean")
        allow_agent_updates = gates.get("allow_agent_updates")
        if not isinstance(allow_agent_updates, bool):
            errors.append("gates.allow_agent_updates must be a boolean")
        parallel_workers = gates.get("parallel_workers", "auto")
        if not (
            parallel_workers == "auto"
            or (isinstance(parallel_workers, int) and parallel_workers >= 1)
        ):
            errors.append("gates.parallel_workers must be 'auto' or an integer >= 1")
        max_auto_workers = gates.get("max_auto_workers", "auto")
        if not (
            max_auto_workers == "auto"
            or (
                isinstance(max_auto_workers, int)
                and not isinstance(max_auto_workers, bool)
                and max_auto_workers >= 1
            )
        ):
            errors.append(
                "gates.max_auto_workers must be 'auto' or an integer >= 1"
            )
        target_final_seconds = gates.get("target_final_seconds", 0)
        if (
            not isinstance(target_final_seconds, int)
            or isinstance(target_final_seconds, bool)
            or target_final_seconds < 0
        ):
            errors.append("gates.target_final_seconds must be an integer >= 0")
        command_timeout_seconds = gates.get("command_timeout_seconds", 7200)
        if (
            not isinstance(command_timeout_seconds, int)
            or isinstance(command_timeout_seconds, bool)
            or command_timeout_seconds < 1
        ):
            errors.append("gates.command_timeout_seconds must be an integer >= 1")
        adaptive_timeout = gates.get("adaptive_timeout_enabled", True)
        if not isinstance(adaptive_timeout, bool):
            errors.append("gates.adaptive_timeout_enabled must be a boolean")
        idle_timeout = gates.get(
            "command_idle_timeout_seconds",
            min(900, command_timeout_seconds)
            if isinstance(command_timeout_seconds, int) and command_timeout_seconds > 0
            else 900,
        )
        if (
            not isinstance(idle_timeout, int)
            or isinstance(idle_timeout, bool)
            or idle_timeout < 1
        ):
            errors.append("gates.command_idle_timeout_seconds must be an integer >= 1")
        elif (
            isinstance(command_timeout_seconds, int)
            and not isinstance(command_timeout_seconds, bool)
            and idle_timeout > command_timeout_seconds
        ):
            errors.append(
                "gates.command_idle_timeout_seconds must be <= gates.command_timeout_seconds"
            )
        infrastructure_markers = gates.get(
            "reported_infrastructure_markers", []
        )
        if not isinstance(infrastructure_markers, list):
            errors.append(
                "gates.reported_infrastructure_markers must be a list"
            )
        else:
            seen_marker_ids: set[str] = set()
            for index, marker in enumerate(infrastructure_markers):
                prefix = f"gates.reported_infrastructure_markers[{index}]"
                if not isinstance(marker, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                unknown = sorted(set(marker) - {"id", "contains"})
                if unknown:
                    errors.append(
                        f"{prefix} contains unknown fields: {', '.join(unknown)}"
                    )
                marker_id = marker.get("id")
                contains = marker.get("contains")
                if (
                    not isinstance(marker_id, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", marker_id)
                ):
                    errors.append(
                        f"{prefix}.id must match [a-z][a-z0-9_-]{{1,63}}"
                    )
                elif marker_id in seen_marker_ids:
                    errors.append(f"{prefix}.id must be unique")
                else:
                    seen_marker_ids.add(marker_id)
                if (
                    not isinstance(contains, str)
                    or not contains.strip()
                    or len(contains) > 512
                ):
                    errors.append(
                        f"{prefix}.contains must be a non-empty string of at most 512 characters"
                    )
        isolation = gates.get("isolation", {})
        if not isinstance(isolation, dict):
            errors.append("gates.isolation must be an object")
        else:
            if not isinstance(isolation.get("enabled", True), bool):
                errors.append("gates.isolation.enabled must be a boolean")
            if isolation.get("mode", "git_worktree") != "git_worktree":
                errors.append("gates.isolation.mode must be 'git_worktree'")
            if not isinstance(isolation.get("worktree_root", ""), str):
                errors.append("gates.isolation.worktree_root must be a string")
            for key, default in (
                ("artifact_max_bytes", 256 * 1024 * 1024),
                ("artifact_max_files", 2000),
            ):
                value = isolation.get(key, default)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    errors.append(f"gates.isolation.{key} must be an integer >= 1")
        distributed = gates.get("distributed", {})
        if not isinstance(distributed, dict):
            errors.append("gates.distributed must be an object")
        else:
            deprecated_distributed_keys = sorted(
                {
                    "worker_pool",
                    "environment_id",
                    "fallback",
                    "connect_timeout_seconds",
                    "heartbeat_timeout_seconds",
                }
                & set(distributed)
            )
            if deprecated_distributed_keys:
                errors.append(
                    "gates.distributed no longer supports manually configured "
                    "SSH pools: "
                    + ", ".join(deprecated_distributed_keys)
                )
            if "enabled" in distributed and not isinstance(
                distributed.get("enabled"), bool
            ):
                errors.append("gates.distributed.enabled must be a boolean")
            mode = distributed.get("mode")
            if mode is None and "enabled" in distributed:
                enabled = distributed.get("enabled")
                mode = "auto" if enabled else "off"
            mode = mode or "auto"
            if mode not in {"auto", "off", "required"}:
                errors.append(
                    "gates.distributed.mode must be auto, off, or required"
                )
            if (
                mode != "off"
                and isinstance(isolation, dict)
                and not isolation.get("enabled", True)
            ):
                errors.append(
                    "gates.isolation.enabled must be true when distributed gates are enabled"
                )
            if (
                distributed.get("forward_environment", "all_except_denylist")
                != "all_except_denylist"
            ):
                errors.append(
                    "gates.distributed.forward_environment must be 'all_except_denylist'"
                )
            for key, default in (
                ("request_timeout_seconds", 15),
            ):
                value = distributed.get(key, default)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    errors.append(f"gates.distributed.{key} must be an integer >= 1")
            discovery_timeout = distributed.get(
                "discovery_timeout_seconds", 1.5
            )
            if (
                not isinstance(discovery_timeout, (int, float))
                or isinstance(discovery_timeout, bool)
                or discovery_timeout <= 0
            ):
                errors.append(
                    "gates.distributed.discovery_timeout_seconds must be a number > 0"
                )
            retry_limit = distributed.get("infrastructure_retry_limit", 2)
            if (
                not isinstance(retry_limit, int)
                or isinstance(retry_limit, bool)
                or retry_limit < 0
            ):
                errors.append(
                    "gates.distributed.infrastructure_retry_limit must be an integer >= 0"
                )
            reported_max = distributed.get(
                "reported_infrastructure_max_workers", 8
            )
            if (
                not isinstance(reported_max, int)
                or isinstance(reported_max, bool)
                or reported_max < 1
            ):
                errors.append(
                    "gates.distributed.reported_infrastructure_max_workers "
                    "must be an integer >= 1"
                )
            denylist = distributed.get("extra_environment_denylist", [])
            if not isinstance(denylist, list) or any(
                not isinstance(item, str) or not item.strip() for item in denylist
            ):
                errors.append(
                    "gates.distributed.extra_environment_denylist must be a list of non-empty strings"
                )

    git = payload.get("git")
    if not isinstance(git, dict):
        errors.append("git must be an object")
    else:
        for key in ("auto_init_repo",):
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

    execution = payload.get("execution")
    if execution is not None:
        if not isinstance(execution, dict):
            errors.append("execution must be an object")
        else:
            parallel_tasks = execution.get("parallel_tasks", {})
            if not isinstance(parallel_tasks, dict):
                errors.append("execution.parallel_tasks must be an object")
            else:
                enabled = parallel_tasks.get("enabled")
                if not isinstance(enabled, bool):
                    errors.append("execution.parallel_tasks.enabled must be a boolean")
                workers = parallel_tasks.get("workers")
                if not (
                    workers == "auto"
                    or (isinstance(workers, int) and workers >= 1)
                ):
                    errors.append("execution.parallel_tasks.workers must be 'auto' or an integer >= 1")
                max_auto_workers = parallel_tasks.get("max_auto_workers")
                if not isinstance(max_auto_workers, int) or max_auto_workers < 1:
                    errors.append("execution.parallel_tasks.max_auto_workers must be an integer >= 1")
                adaptive = parallel_tasks.get("adaptive")
                if not isinstance(adaptive, bool):
                    errors.append("execution.parallel_tasks.adaptive must be a boolean")
                strict = parallel_tasks.get("strict")
                if not isinstance(strict, bool):
                    errors.append("execution.parallel_tasks.strict must be a boolean")
                worktree_root = parallel_tasks.get("worktree_root")
                if not isinstance(worktree_root, str):
                    errors.append("execution.parallel_tasks.worktree_root must be a string")
                pressure_cooldown = parallel_tasks.get("pressure_cooldown_seconds", 3600)
                if not isinstance(pressure_cooldown, int) or pressure_cooldown < 0:
                    errors.append(
                        "execution.parallel_tasks.pressure_cooldown_seconds must be an integer >= 0"
                    )
                soft_threshold = parallel_tasks.get("soft_pressure_threshold", 2)
                if not isinstance(soft_threshold, int) or soft_threshold < 1:
                    errors.append(
                        "execution.parallel_tasks.soft_pressure_threshold must be an integer >= 1"
                    )
            requirements_audit = execution.get("requirements_audit", {})
            if not isinstance(requirements_audit, dict):
                errors.append("execution.requirements_audit must be an object")
            else:
                for key in ("pattern_timeout_ms", "total_timeout_seconds"):
                    value = requirements_audit.get(key, 250 if key == "pattern_timeout_ms" else 300)
                    if not isinstance(value, int) or value < 1:
                        errors.append(f"execution.requirements_audit.{key} must be an integer >= 1")
                if not isinstance(requirements_audit.get("cache_enabled", True), bool):
                    errors.append("execution.requirements_audit.cache_enabled must be a boolean")
            evidence_preflight = execution.get("evidence_preflight", {})
            if not isinstance(evidence_preflight, dict):
                errors.append("execution.evidence_preflight must be an object")
            elif evidence_preflight.get("mode", "high_risk") not in {"off", "high_risk", "all"}:
                errors.append("execution.evidence_preflight.mode must be one of: off, high_risk, all")
            smart_timeout = execution.get("smart_timeout", {})
            if not isinstance(smart_timeout, dict):
                errors.append("execution.smart_timeout must be an object")
            else:
                if not isinstance(smart_timeout.get("enabled", True), bool):
                    errors.append("execution.smart_timeout.enabled must be a boolean")
                timeout_defaults = {
                    "provider_idle_seconds": 1800,
                    "tool_idle_seconds": 900,
                    "semantic_stall_seconds": 3600,
                    "safety_ceiling_seconds": 14400,
                }
                for key, default in timeout_defaults.items():
                    value = smart_timeout.get(key, default)
                    if not isinstance(value, int) or isinstance(value, bool) or value < 60:
                        errors.append(f"execution.smart_timeout.{key} must be an integer >= 60")
                repeat_limit = smart_timeout.get("loop_repeat_limit", 3)
                if not isinstance(repeat_limit, int) or isinstance(repeat_limit, bool) or repeat_limit < 2:
                    errors.append("execution.smart_timeout.loop_repeat_limit must be an integer >= 2")
                resume_limit = smart_timeout.get("same_provider_resume_limit", 1)
                if not isinstance(resume_limit, int) or isinstance(resume_limit, bool) or resume_limit < 0:
                    errors.append(
                        "execution.smart_timeout.same_provider_resume_limit must be an integer >= 0"
                    )
                if (
                    "stage_progress_lease_seconds" in smart_timeout
                    and "stage_checkpoint_seconds" in smart_timeout
                ):
                    errors.append(
                        "execution.smart_timeout cannot define both "
                        "stage_progress_lease_seconds and deprecated stage_checkpoint_seconds"
                    )
                stage_lease_key = (
                    "stage_progress_lease_seconds"
                    if "stage_progress_lease_seconds" in smart_timeout
                    else "stage_checkpoint_seconds"
                )
                stage_leases = smart_timeout.get(stage_lease_key, {})
                if not isinstance(stage_leases, dict):
                    errors.append(
                        f"execution.smart_timeout.{stage_lease_key} must be an object"
                    )
                else:
                    for stage, seconds in stage_leases.items():
                        if not isinstance(stage, str) or not stage.strip():
                            errors.append(
                                f"execution.smart_timeout.{stage_lease_key} keys "
                                "must be non-empty strings"
                            )
                        if (
                            not isinstance(seconds, int)
                            or isinstance(seconds, bool)
                            or seconds < 60
                        ):
                            errors.append(
                                f"execution.smart_timeout.{stage_lease_key} values "
                                "must be integers >= 60"
                            )
                if (
                    "post_ceiling_finalize_seconds" in smart_timeout
                    and "active_tool_grace_seconds" in smart_timeout
                ):
                    errors.append(
                        "execution.smart_timeout cannot define both "
                        "post_ceiling_finalize_seconds and deprecated active_tool_grace_seconds"
                    )
                finalize_key = (
                    "post_ceiling_finalize_seconds"
                    if "post_ceiling_finalize_seconds" in smart_timeout
                    else "active_tool_grace_seconds"
                )
                finalize_seconds = smart_timeout.get(finalize_key, 600)
                if (
                    not isinstance(finalize_seconds, int)
                    or isinstance(finalize_seconds, bool)
                    or finalize_seconds < 0
                ):
                    errors.append(
                        f"execution.smart_timeout.{finalize_key} "
                        "must be an integer >= 0"
                    )
                fresh_limit = smart_timeout.get("fresh_continuation_limit", 1)
                if (
                    not isinstance(fresh_limit, int)
                    or isinstance(fresh_limit, bool)
                    or fresh_limit < 0
                ):
                    errors.append(
                        "execution.smart_timeout.fresh_continuation_limit "
                        "must be an integer >= 0"
                    )
                safety = smart_timeout.get("safety_ceiling_seconds", 14400)
                leases = [
                    smart_timeout.get(key, default)
                    for key, default in timeout_defaults.items()
                    if key != "safety_ceiling_seconds"
                ]
                if isinstance(stage_leases, dict):
                    leases.extend(stage_leases.values())
                if (
                    isinstance(safety, int)
                    and not isinstance(safety, bool)
                    and all(
                        isinstance(value, int) and not isinstance(value, bool)
                        for value in leases
                    )
                    and leases
                    and safety < max(leases)
                ):
                    errors.append(
                        "execution.smart_timeout.safety_ceiling_seconds must be >= all lease timeouts"
                    )
            provider_failover = execution.get("provider_failover", {})
            if not isinstance(provider_failover, dict):
                errors.append("execution.provider_failover must be an object")
            else:
                if not isinstance(
                    provider_failover.get("probe_enabled", True), bool
                ):
                    errors.append(
                        "execution.provider_failover.probe_enabled must be a boolean"
                    )
                failover_defaults = {
                    "probe_timeout_seconds": 60,
                    "connection_cooldown_seconds": 60,
                    "pressure_cooldown_seconds": 300,
                    "timeout_cooldown_seconds": 1800,
                    "quota_cooldown_seconds": 3600,
                    "max_cooldown_seconds": 14400,
                }
                for key, default in failover_defaults.items():
                    value = provider_failover.get(key, default)
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < 1
                    ):
                        errors.append(
                            f"execution.provider_failover.{key} must be an integer >= 1"
                        )
                maximum = provider_failover.get(
                    "max_cooldown_seconds", 14400
                )
                cooldowns = [
                    provider_failover.get(key, default)
                    for key, default in failover_defaults.items()
                    if key.endswith("_cooldown_seconds")
                    and key != "max_cooldown_seconds"
                ]
                if (
                    isinstance(maximum, int)
                    and not isinstance(maximum, bool)
                    and all(
                        isinstance(value, int) and not isinstance(value, bool)
                        for value in cooldowns
                    )
                    and maximum < max(cooldowns)
                ):
                    errors.append(
                        "execution.provider_failover.max_cooldown_seconds must "
                        "be >= all provider cooldowns"
                    )
            recovery = execution.get("recovery", {})
            if not isinstance(recovery, dict):
                errors.append("execution.recovery must be an object")
            else:
                enabled = recovery.get("enabled", True)
                if not isinstance(enabled, bool):
                    errors.append("execution.recovery.enabled must be a boolean")
                for key in (
                    "max_rounds",
                    "max_repair_tasks_per_round",
                    "max_refs_per_repair_task",
                    "max_incidents_per_run",
                    "diagnostic_probe_timeout_seconds",
                ):
                    value = recovery.get(key, 2 if key == "max_rounds" else 1)
                    if not isinstance(value, int) or value < 1:
                        errors.append(f"execution.recovery.{key} must be an integer >= 1")

    smart_timeout_enabled = True
    if isinstance(execution, dict) and isinstance(execution.get("smart_timeout", {}), dict):
        smart_timeout_enabled = execution.get("smart_timeout", {}).get("enabled", True) is True
    if smart_timeout_enabled and isinstance(providers, dict):
        native_kinds = {"codex", "copilot-cli", "antigravity", "mock"}
        for provider_name, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            if str(provider.get("kind", "")) in native_kinds:
                continue
            if provider.get("progress_protocol") != SMART_TIMEOUT_PROGRESS_PROTOCOL:
                errors.append(
                    f"providers.{provider_name}.progress_protocol must be "
                    f"'{SMART_TIMEOUT_PROGRESS_PROTOCOL}' when smart timeout is enabled"
                )

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
                if key not in (
                    "clarify",
                    "prototype",
                    "design",
                    "plan",
                    "sync-agent-instructions",
                    "provider_research",
                    "implement",
                    "review",
                    "arbiter",
                    "visual_judge",
                ):
                    errors.append(f"retries.per_stage contains unknown stage '{key}'")
                if not isinstance(value, int) or value < 1:
                    errors.append(f"retries.per_stage.{key} must be an integer >= 1")

    visual_judge = payload.get("visual_judge")
    if visual_judge is not None:
        if not isinstance(visual_judge, dict):
            errors.append("visual_judge must be an object")
        else:
            mode = visual_judge.get("mode", "auto")
            if mode not in {"auto", "off", "required"}:
                errors.append("visual_judge.mode must be one of: auto, off, required")
            threshold = visual_judge.get("threshold", 85)
            if not isinstance(threshold, int) or threshold < 0 or threshold > 100:
                errors.append("visual_judge.threshold must be an integer from 0 to 100")
            provider = visual_judge.get("provider", "")
            if not isinstance(provider, str):
                errors.append("visual_judge.provider must be a string")
            elif provider and isinstance(payload.get("providers"), dict) and provider not in payload["providers"]:
                errors.append("visual_judge.provider must be one of providers keys when set")
            max_pairs = visual_judge.get("max_pairs_per_task", 6)
            if not isinstance(max_pairs, int) or max_pairs < 1:
                errors.append("visual_judge.max_pairs_per_task must be an integer >= 1")
            require_artifacts = visual_judge.get("require_screenshot_artifacts", True)
            if not isinstance(require_artifacts, bool):
                errors.append("visual_judge.require_screenshot_artifacts must be a boolean")

    frontend_design = payload.get("frontend_design")
    if frontend_design is not None:
        if not isinstance(frontend_design, dict):
            errors.append("frontend_design must be an object")
        else:
            if frontend_design.get("mode", "auto") != "auto":
                errors.append("frontend_design.mode must be auto")
            if frontend_design.get("catalog_repository") != "VoltAgent/awesome-design-md":
                errors.append("frontend_design.catalog_repository must be VoltAgent/awesome-design-md")
            if not isinstance(frontend_design.get("catalog_ref"), str) or not str(frontend_design.get("catalog_ref", "")).strip():
                errors.append("frontend_design.catalog_ref must be a non-empty string")
            max_pages = frontend_design.get("max_pages", 3)
            if not isinstance(max_pages, int) or max_pages < 1 or max_pages > 3:
                errors.append("frontend_design.max_pages must be an integer from 1 to 3")
            viewports = frontend_design.get("viewports")
            if not isinstance(viewports, list) or not viewports or any(
                not isinstance(item, str) or not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", item)
                for item in (viewports or [])
            ):
                errors.append("frontend_design.viewports must be a non-empty list of WIDTHxHEIGHT strings")
            timeout = frontend_design.get("network_timeout_seconds", 30)
            if not isinstance(timeout, int) or timeout < 1 or timeout > 300:
                errors.append("frontend_design.network_timeout_seconds must be an integer from 1 to 300")

    return errors


def project_config_warnings(payload: object) -> List[str]:
    if not isinstance(payload, dict):
        return []
    execution = payload.get("execution", {})
    if not isinstance(execution, dict):
        return []
    smart_timeout = execution.get("smart_timeout", {})
    if not isinstance(smart_timeout, dict):
        return []
    warnings: List[str] = []
    if "stage_checkpoint_seconds" in smart_timeout:
        warnings.append(
            "execution.smart_timeout.stage_checkpoint_seconds is deprecated; "
            "use stage_progress_lease_seconds"
        )
    if "active_tool_grace_seconds" in smart_timeout:
        warnings.append(
            "execution.smart_timeout.active_tool_grace_seconds is deprecated; "
            "use post_ceiling_finalize_seconds"
        )
    return warnings


def validate_project_root(
    project_root: Path,
    *,
    allow_unsafe_forbidden_pattern_definitions: bool = False,
) -> Dict[str, List[str]]:
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
        warnings.extend(project_config_warnings(config_payload))
        errors.extend(
            validate_verification_command_paths(
                config_payload.get("gates", {}).get("commands", []),
                root,
                "gates.commands",
            )
        )

    try:
        plan_payload = read_json(task_plan_path(root), default=None)
    except JSONDecodeError as error:
        plan_payload = None
        errors.append(f"task plan file is not valid JSON: {task_plan_path(root)} ({error.msg})")
    if plan_payload is None and not any("task plan file is not valid JSON" in item for item in errors):
        errors.append(f"missing task plan file: {task_plan_path(root)}")
    elif plan_payload is not None:
        errors.extend(
            validate_task_plan_payload(
                plan_payload,
                allow_empty_tasks=_allow_empty_task_plan_for_iteration(root),
                enforce_active_task_granularity=True,
            )
        )
        verification_steps = plan_payload.get("verification_steps", [])
        if isinstance(verification_steps, list) and verification_steps:
            for index, step in enumerate(verification_steps, start=1):
                if not isinstance(step, dict):
                    continue
                if str(step.get("runner", "")).strip().lower() != "pytest":
                    continue
                for target in step.get("targets", []) or []:
                    target_text = str(target)
                    target_path = target_text.split("::", 1)[0]
                    if not target_path.endswith(".py"):
                        continue
                    candidate = Path(target_path)
                    resolved = candidate if candidate.is_absolute() else (root / candidate).resolve()
                    if not resolved.exists():
                        errors.append(
                            f"task plan verification_steps[{index}] references missing pytest target: {target}"
                        )
        else:
            errors.extend(
                validate_verification_command_paths(
                    plan_payload.get("verification_commands", []),
                    root,
                    "task plan verification_commands",
                )
            )
        warnings.extend(task_plan_warnings(plan_payload))
        parallel_tasks = (
            config_payload.get("execution", {}).get("parallel_tasks", {})
            if isinstance(config_payload, dict)
            else {}
        )
        if (
            isinstance(parallel_tasks, dict)
            and bool(parallel_tasks.get("enabled"))
            and bool(parallel_tasks.get("strict", False))
        ):
            for index, task in enumerate(plan_payload.get("tasks", []), start=1):
                if not isinstance(task, dict):
                    continue
                if str(task.get("status", "pending")) == "done":
                    continue
                if "depends_on" not in task:
                    errors.append(
                        f"task #{index} depends_on must be present for non-done tasks when execution.parallel_tasks.strict is enabled"
                    )

    try:
        trace_payload = read_json(requirements_trace_path(root), default=None)
    except JSONDecodeError as error:
        trace_payload = None
        errors.append(f"requirements trace file is not valid JSON: {requirements_trace_path(root)} ({error.msg})")
    if trace_payload is not None:
        trace_payload = normalize_requirements_trace_payload(trace_payload)
        trace_errors = validate_requirements_trace_payload(
            trace_payload,
            validate_forbidden_pattern_definitions=(
                not allow_unsafe_forbidden_pattern_definitions
            ),
        )
        errors.extend(trace_errors)
        errors.extend(validate_frontend_scope(trace_payload))

    frontend_lock = load_frontend_design_lock(root)
    if frontend_lock.get("status") == "approved":
        errors.extend(
            validate_frontend_design_artifacts(
                root,
                frontend_lock,
                require_approved=True,
            )
        )

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
        gates = config_payload.get("gates", {})
        gate_commands = gates.get("commands", []) if isinstance(gates, dict) else []
        gate_steps = gates.get("steps", []) if isinstance(gates, dict) else []
        plan_commands = plan_payload.get("verification_commands", [])
        plan_steps = plan_payload.get("verification_steps", [])
        if not gate_commands and not gate_steps and not plan_commands and not plan_steps:
            warnings.append("no verification steps are configured yet; verify stage will be a no-op")
        if (
            not plan_steps
            and isinstance(gate_commands, list)
            and isinstance(plan_commands, list)
            and plan_commands
        ):
            normalized_gate_commands = [
                str(item).strip() for item in gate_commands if isinstance(item, str) and str(item).strip()
            ]
            normalized_plan_commands = [
                str(item).strip() for item in plan_commands if isinstance(item, str) and str(item).strip()
            ]
            if normalized_plan_commands and normalized_gate_commands != normalized_plan_commands:
                allow_updates = bool(config_payload.get("gates", {}).get("allow_agent_updates", False))
                if allow_updates:
                    warnings.append(
                        "gates.commands differ from task plan verification_commands; "
                        "auto_agents will sync gates.commands from the task plan before running gates"
                    )
                else:
                    warnings.append(
                        "gates.commands differ from task plan verification_commands and "
                        "gates.allow_agent_updates is false; verify stage will use gates.commands"
                    )

    return {"errors": errors, "warnings": warnings}


def validation_report(
    project_root: Path,
    *,
    allow_unsafe_forbidden_pattern_definitions: bool = False,
) -> Dict[str, object]:
    result = validate_project_root(
        project_root,
        allow_unsafe_forbidden_pattern_definitions=(
            allow_unsafe_forbidden_pattern_definitions
        ),
    )
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
