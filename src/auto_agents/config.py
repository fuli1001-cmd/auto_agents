from __future__ import annotations

import copy
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple
from uuid import uuid4

from .io_utils import read_json, read_text, write_if_missing, write_json, write_text
from .models import (
    DEFAULT_COPILOT_CLI_IDLE_TIMEOUT_SECONDS,
    DEFAULT_EFFORTS,
    DEFAULT_RETRY_PER_STAGE,
    DEFAULT_SESSION_MAX_ATTEMPTS,
    DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
    ProjectConfig,
    RunState,
    SESSION_HARD_CEILING,
    SessionState,
    SUPPORTED_PROVIDER_KINDS,
)


AUTO_DIR = ".auto-agents"
CONFIG_FILE = "config.json"
PROJECT_GITIGNORE = """__pycache__/
*.pyc
.pytest_cache/
.conda/
.venv/
node_modules/
.data/
.tmp/
.tmp-tests/
.DS_Store
.antigravitycli/
"""
AUTO_GITIGNORE_ENTRIES = (
    "runs/",
    "state/gate_baseline_cache.json",
    "state/repomap_cache.json",
    "state/parallel_tuning.json",
)
LEGACY_AUTO_GITIGNORE_ENTRIES = {
    "runs/*",
    "!runs/*/",
    "runs/*/*",
    "!runs/*/task_plan.final.json",
    "state/run_state.json",
}


PROJECT_BRIEF_TEMPLATE = """# Project Brief

## Problem

- Fill in the user problem and target audience.

## MVP Scope

- List the core user-visible capabilities.

## Non-Goals

- State what V1 will not do.

## Constraints

- Budget, stack, deployment, compliance, or integration constraints.
- Do not modify the system-wide environment; Python projects must use a project-local conda env at `./.conda`.
- Mutable local test/runtime artifacts should live under ignored temp/data paths such as `./.tmp/`, `./.tmp-tests/`, or `./.data/`, not as tracked repo-root files.
"""


ARCHITECTURE_TEMPLATE = """# Architecture

## System Boundary

- Describe the runtime boundary and external interfaces.

## Core Modules

- List the main modules and responsibilities.

## Data Flow

- Describe how requests and data move through the system.

## Risks

- Record high-risk choices and tradeoffs.
"""


TASK_PLAN_TEMPLATE = {
    "tasks": [
        {
            "task_id": "task-001",
            "title": "replace-me",
            "description": "Describe one minimal verifiable feature slice.",
            "requirement_ids": [],
            "depends_on": [],
            "acceptance": ["State one concrete acceptance criterion."],
            "status": "pending",
            "commit_message": ""
        }
    ]
}

REQUIREMENTS_TRACE_TEMPLATE = {
    "version": 1,
    "requirements": [],
}

PROVIDER_REFERENCES_LOCK_TEMPLATE = {
    "version": 1,
    "references": {},
}


RUN_STATE_TEMPLATE = {
    "run_id": "",
    "status": "pending",
    "current_stage": "clarify",
    "pending_approval": "",
    "approved_gates": [],
    "tasks": [],
    "stage_summaries": {},
    "agent_attempts": {},
    "task_review_cache": {},
    "last_error": "",
    "rejection_reason": "",
    "rejected_stage": "",
}


DEFAULT_CONFIG = {
    "project_name": "unnamed-project",
    "providers": {
        "codex": {
            "kind": "codex",
            "binary": "codex",
            "profile_map": {
                "balanced": "balanced",
                "deep": "deep",
                "max": "max",
            },
            "extra_args": [],
            "cwd_flag": "-C",
            "prompt_via_stdin": True,
            "output_flag": "-o",
            "idle_timeout_seconds": DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
            "subscription_tier": "default",
        },
        "copilot-cli": {
            "kind": "copilot-cli",
            "binary": "copilot",
            "profile_map": {
                "balanced": "balanced",
                "deep": "deep",
                "max": "max",
            },
            "extra_args": [],
            "cwd_flag": "",
            "prompt_via_stdin": True,
            "output_flag": "",
            "timeout_seconds": 3600,
            "idle_timeout_seconds": DEFAULT_COPILOT_CLI_IDLE_TIMEOUT_SECONDS,
            "subscription_tier": "default",
        },
        "antigravity-claude": {
            "kind": "antigravity",
            "binary": "agy",
            "profile_map": {
                "balanced": "Claude Sonnet 4.6 (Thinking)",
                "deep": "Claude Opus 4.6 (Thinking)",
                "max": "Claude Opus 4.6 (Thinking)",
            },
            "extra_args": [],
            "cwd_flag": "",
            "prompt_via_stdin": True,
            "output_flag": "",
            "timeout_seconds": 7200,
            "idle_timeout_seconds": 7200,
            "subscription_tier": "default",
        },
        "antigravity-gemini": {
            "kind": "antigravity",
            "binary": "agy",
            "profile_map": {
                "balanced": "Gemini 3.5 Flash (Low)",
                "deep": "Gemini 3.5 Flash (Medium)",
                "max": "Gemini 3.5 Flash (High)",
            },
            "extra_args": [],
            "cwd_flag": "",
            "prompt_via_stdin": True,
            "output_flag": "",
            "timeout_seconds": 7200,
            "idle_timeout_seconds": 7200,
            "subscription_tier": "default",
        },
    },
    "active_provider": "codex",
    "docs": {
        "language": "en",
    },
    "efforts": dict(DEFAULT_EFFORTS),
    "gates": {
        "commands": [],
        "steps": [],
        "parallel_groups": [],
        "require_clean_git_before_task": True,
        "allow_agent_updates": True,
    },
    "git": {
        "auto_init_repo": True,
        "commit_message_template": "feat({task_id}): {title}",
    },
    "execution": {
        "parallel_tasks": {
            "enabled": False,
            "workers": "auto",
            "max_auto_workers": 4,
            "adaptive": True,
            "strict": False,
            "worktree_root": "",
        },
        "recovery": {
            "enabled": True,
            "max_rounds": 2,
            "max_repair_tasks_per_round": 6,
            "max_refs_per_repair_task": 8,
        },
    },
    "approvals": {
        "enabled": ["requirements", "architecture", "release"],
    },
    "retries": {
        "default_max_attempts": 2,
        "per_stage": dict(DEFAULT_RETRY_PER_STAGE),
    },
}


def auto_dir(project_root: Path) -> Path:
    return project_root / AUTO_DIR


def config_path(project_root: Path) -> Path:
    return auto_dir(project_root) / CONFIG_FILE


def docs_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "docs"


def project_brief_path(project_root: Path) -> Path:
    return docs_dir(project_root) / "project_brief.md"


def architecture_path(project_root: Path) -> Path:
    return docs_dir(project_root) / "architecture.md"


def provider_references_dir(project_root: Path) -> Path:
    return docs_dir(project_root) / "provider_references"


def requirements_audit_path(project_root: Path) -> Path:
    return docs_dir(project_root) / "requirements_audit.md"


def state_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "state"


def runs_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "runs"


def history_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "history"


def run_path(project_root: Path, run_id: str) -> Path:
    return runs_dir(project_root) / run_id


def archived_task_plans_dir(project_root: Path) -> Path:
    return history_dir(project_root) / "task_plans"


def archived_task_plan_path(project_root: Path, run_id: str) -> Path:
    return archived_task_plans_dir(project_root) / f"{run_id}.json"


def archived_run_state_path(project_root: Path, run_id: str) -> Path:
    return run_path(project_root, run_id) / "run_state.final.json"


def run_state_path(project_root: Path) -> Path:
    return state_dir(project_root) / "run_state.json"


def task_plan_path(project_root: Path) -> Path:
    return state_dir(project_root) / "task_plan.json"


def requirements_trace_path(project_root: Path) -> Path:
    return state_dir(project_root) / "requirements_trace.json"


def provider_references_lock_path(project_root: Path) -> Path:
    return state_dir(project_root) / "provider_references.lock.json"


def project_rules_path(project_root: Path) -> Path:
    return auto_dir(project_root) / "project-rules.md"


def normalized_project_rules_path(project_root: Path) -> Path:
    return auto_dir(project_root) / "project-rules.normalized.json"


def agent_instructions_lock_path(project_root: Path) -> Path:
    return state_dir(project_root) / "agent_instructions.lock.json"


def gate_baseline_cache_path(project_root: Path) -> Path:
    return state_dir(project_root) / "gate_baseline_cache.json"


def review_path(project_root: Path) -> Path:
    return docs_dir(project_root) / "review.md"


def auto_gitignore_path(project_root: Path) -> Path:
    return auto_dir(project_root) / ".gitignore"


def ensure_auto_gitignore(project_root: Path) -> None:
    path = auto_gitignore_path(project_root)
    current = read_text(path)
    entries = []
    seen = set()
    for raw_line in current.splitlines():
        line = raw_line.strip()
        if not line or line in LEGACY_AUTO_GITIGNORE_ENTRIES or line in seen:
            continue
        entries.append(line)
        seen.add(line)
    for entry in AUTO_GITIGNORE_ENTRIES:
        if entry not in seen:
            entries.append(entry)
    desired = "".join(f"{entry}\n" for entry in entries)
    if current == desired:
        return
    write_text(path, desired)


def supported_provider_kinds() -> Tuple[str, ...]:
    return SUPPORTED_PROVIDER_KINDS


def bootstrap_project(project_root: Path, name: str, doc_language: str = "en") -> Path:
    root = project_root.resolve()
    
    if auto_dir(root).is_dir():
        print(f"Project already initialized at {root}", file=import_sys().stderr)
        return root
        
    has_existing_content = False
    if root.exists():
        for child in root.iterdir():
            if child.name not in (".git", "spec.md", AUTO_DIR) and not child.name.startswith("."):
                has_existing_content = True
                break
                
    root.mkdir(parents=True, exist_ok=True)

    config = dict(DEFAULT_CONFIG)
    config["project_name"] = name
    config["providers"] = copy.deepcopy(DEFAULT_CONFIG["providers"])
    config["active_provider"] = "codex"
    config["docs"] = dict(DEFAULT_CONFIG["docs"])
    config["docs"]["language"] = doc_language

    write_json(config_path(root), config)
    ensure_auto_gitignore(root)
    write_if_missing(project_brief_path(root), PROJECT_BRIEF_TEMPLATE)
    write_if_missing(architecture_path(root), ARCHITECTURE_TEMPLATE)
    write_if_missing(review_path(root), "# Review\n\nNo review has been recorded yet.\n")
    provider_references_dir(root).mkdir(parents=True, exist_ok=True)
    write_json(requirements_trace_path(root), REQUIREMENTS_TRACE_TEMPLATE)
    write_json(provider_references_lock_path(root), PROVIDER_REFERENCES_LOCK_TEMPLATE)
    write_json(task_plan_path(root), TASK_PLAN_TEMPLATE)
    
    run_state = dict(RUN_STATE_TEMPLATE)
    if has_existing_content:
        run_state["status"] = "completed"
        run_state["current_stage"] = "readme"
    write_json(run_state_path(root), run_state)
    
    write_if_missing(root / ".gitignore", PROJECT_GITIGNORE)
    write_if_missing(root / "README.md", f"# {name}\n")
    return root


def import_sys():
    import sys
    return sys


def load_project_config(project_root: Path) -> ProjectConfig:
    data = read_json(config_path(project_root), default=None)
    if data is None:
        raise FileNotFoundError(f"Missing config: {config_path(project_root)}")
    return ProjectConfig.from_dict(data)


def save_project_config(project_root: Path, config: ProjectConfig) -> None:
    write_json(config_path(project_root), config.to_dict())


def create_run(project_root: Path) -> RunState:
    run_id = uuid4().hex[:12]
    data = dict(RUN_STATE_TEMPLATE)
    data["run_id"] = run_id
    state = RunState.from_dict(data)
    save_run_state(project_root, state)
    return state


def migrate_archived_task_plans(project_root: Path) -> Dict[str, str]:
    root = project_root.resolve()
    migrated: Dict[str, str] = {}
    for legacy_path in sorted(runs_dir(root).glob("*/task_plan.final.json")):
        run_id = legacy_path.parent.name
        target_path = archived_task_plan_path(root, run_id)
        legacy_payload = read_json(legacy_path, default=None)
        if target_path.exists():
            existing_payload = read_json(target_path, default=None)
            if existing_payload != legacy_payload:
                raise RuntimeError(
                    f"task plan archive migration conflict: {legacy_path} -> {target_path}"
                )
            legacy_path.unlink()
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_path), str(target_path))
        migrated[str(legacy_path.resolve())] = str(target_path.resolve())

    if not migrated:
        return migrated

    state_payload = read_json(run_state_path(root), default=None)
    if not isinstance(state_payload, dict):
        return migrated
    context = state_payload.get("resume_context", {})
    if not isinstance(context, dict):
        return migrated
    raw_archive = str(context.get("previous_task_plan_archive", "")).strip()
    if not raw_archive:
        return migrated
    archive_path = Path(raw_archive)
    if not archive_path.is_absolute():
        archive_path = root / archive_path
    replacement = migrated.get(str(archive_path.resolve()))
    if not replacement:
        return migrated
    updated_payload = dict(state_payload)
    updated_context = dict(context)
    updated_context["previous_task_plan_archive"] = replacement
    updated_payload["resume_context"] = updated_context
    write_json(run_state_path(root), updated_payload)
    return migrated


def load_run_state(project_root: Path) -> RunState:
    migrate_archived_task_plans(project_root)
    data = read_json(run_state_path(project_root), default=None)
    if data is None or not data.get("run_id"):
        return create_run(project_root)
    return RunState.from_dict(data)


def save_run_state(project_root: Path, state: RunState) -> None:
    ensure_auto_gitignore(project_root)
    write_json(run_state_path(project_root), state.to_dict())


def load_task_plan(project_root: Path) -> dict:
    return read_json(task_plan_path(project_root), default={"tasks": []})


def save_task_plan(project_root: Path, payload: dict) -> None:
    write_json(task_plan_path(project_root), payload)


def run_artifact_paths(project_root: Path, run_id: str, stage: str) -> Tuple[Path, Path]:
    run_root = run_path(project_root, run_id)
    prompt_path = run_root / "prompts" / f"{stage}.txt"
    output_path = run_root / "outputs" / f"{stage}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return prompt_path, output_path


def write_run_prompt(project_root: Path, run_id: str, stage: str, prompt: str) -> Path:
    prompt_path, _ = run_artifact_paths(project_root, run_id, stage)
    write_text(prompt_path, prompt)
    return prompt_path


def load_stage_output(project_root: Path, run_id: str, stage: str) -> str:
    _, output_path = run_artifact_paths(project_root, run_id, stage)
    return read_text(output_path)


def conversation_history_path(project_root: Path, run_id: str) -> Path:
    return run_path(project_root, run_id) / "clarify_conversation.json"


# ── Session helpers ──────────────────────────────────────────────


def sessions_dir(project_root: Path) -> Path:
    return state_dir(project_root) / "sessions"


def session_dir(project_root: Path, session_id: str) -> Path:
    return sessions_dir(project_root) / session_id


def session_state_path(project_root: Path, session_id: str) -> Path:
    return session_dir(project_root, session_id) / "session_state.json"


def session_artifact_paths(
    project_root: Path, session_id: str, label: str,
) -> Tuple[Path, Path]:
    root = session_dir(project_root, session_id)
    prompt_path = root / "prompts" / f"{label}.txt"
    output_path = root / "outputs" / f"{label}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return prompt_path, output_path


def create_session(project_root: Path, mode: str) -> SessionState:
    session_id = uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    state = SessionState(
        session_id=session_id,
        mode=mode,
        max_attempts=DEFAULT_SESSION_MAX_ATTEMPTS.get(mode, 4),
        hard_ceiling=SESSION_HARD_CEILING.get(mode, 15),
        created_at=now,
        updated_at=now,
    )
    save_session_state(project_root, state)
    return state


def load_session_state(project_root: Path, session_id: str) -> SessionState:
    data = read_json(session_state_path(project_root, session_id), default=None)
    if data is None:
        raise FileNotFoundError(
            f"Session not found: {session_state_path(project_root, session_id)}"
        )
    return SessionState.from_dict(data)


def save_session_state(project_root: Path, state: SessionState) -> None:
    path = session_state_path(project_root, state.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, state.to_dict())


def _session_timestamp(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def sort_sessions(sessions: list[SessionState]) -> list[SessionState]:
    return sorted(
        sessions,
        key=lambda s: (
            _session_timestamp(s.updated_at or s.created_at),
            _session_timestamp(s.created_at),
            s.session_id,
        ),
        reverse=True,
    )


def _validated_session_dir(project_root: Path, session_id: str) -> Path:
    root = sessions_dir(project_root).resolve()
    target = (root / session_id).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Invalid session id: {session_id}") from exc
    if len(relative.parts) != 1:
        raise ValueError(f"Invalid session id: {session_id}")
    return target


def delete_session(project_root: Path, session_id: str) -> None:
    target = _validated_session_dir(project_root, session_id)
    if not target.is_dir():
        raise FileNotFoundError(f"Session not found: {session_state_path(project_root, session_id)}")
    shutil.rmtree(target)


def clear_sessions(project_root: Path) -> int:
    root = sessions_dir(project_root)
    if not root.is_dir():
        return 0
    deleted = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "session_state.json").is_file():
            continue
        shutil.rmtree(child)
        deleted += 1
    return deleted


def list_sessions(project_root: Path) -> list:
    root = sessions_dir(project_root)
    if not root.is_dir():
        return []
    result = []
    for child in sorted(root.iterdir()):
        state_file = child / "session_state.json"
        if state_file.is_file():
            try:
                data = read_json(state_file, default=None)
                if data:
                    result.append(SessionState.from_dict(data))
            except Exception:
                pass
    return sort_sessions(result)
