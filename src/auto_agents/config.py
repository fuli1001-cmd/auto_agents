from __future__ import annotations

import copy
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple
from uuid import uuid4

from .io_utils import read_json, read_text, write_if_missing, write_json, write_text
from .models import (
    DEFAULT_CLAUDE_CODE_PROFILE_MAP,
    DEFAULT_CLAUDE_CODE_TIMEOUT_SECONDS,
    DEFAULT_COPILOT_CLI_IDLE_TIMEOUT_SECONDS,
    DEFAULT_EFFORTS,
    DEFAULT_RETRY_PER_STAGE,
    DEFAULT_SESSION_MAX_ATTEMPTS,
    DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
    ProviderConfig,
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
.env
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
    "operator/",
    "runtime/",
    "failed-verification-logs/",
    "runs/",
    "state/gate_baseline_cache.json",
    "state/gate_baseline_cache.sqlite3",
    "state/gate_baseline_cache.sqlite3-*",
    "state/requirements_audit_cache.sqlite3",
    "state/requirements_audit_cache.sqlite3-*",
    "state/repomap_cache.json",
    "state/parallel_tuning.json",
    "state/release_jobs.sqlite3",
    "state/release_jobs.sqlite3-shm",
    "state/release_jobs.sqlite3-wal",
    "state/release-worker.log",
    "state/release-worker.lock",
)
LEGACY_AUTO_GITIGNORE_ENTRIES = {"state/run_state.json"}


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
    "persistence_contract_version": 2,
    "tasks": [
        {
            "task_id": "task-001",
            "title": "replace-me",
            "description": "Describe one minimal verifiable feature slice.",
            "requirement_ids": [],
            "depends_on": [],
            "acceptance": ["State one concrete acceptance criterion."],
            "status": "pending",
            "commit_message": "",
            "persistence_change": {
                "storage_transition": "none",
                "compatibility_policy": "not_applicable",
            },
        }
    ]
}

REQUIREMENTS_TRACE_TEMPLATE = {
    "version": 1,
    "persistence_contract_version": 2,
    "persistence_decisions": [],
    "requirements": [],
}

PROVIDER_REFERENCES_LOCK_TEMPLATE = {
    "version": 1,
    "references": {},
}


RUN_STATE_TEMPLATE = {
    "run_id": "",
    "workflow_version": 2,
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
            "vision": "auto",
        },
        "claude-code": {
            "kind": "claude-code",
            "binary": "claude",
            "profile_map": dict(DEFAULT_CLAUDE_CODE_PROFILE_MAP),
            "extra_args": [],
            "cwd_flag": "",
            "prompt_via_stdin": True,
            "output_flag": "",
            "timeout_seconds": DEFAULT_CLAUDE_CODE_TIMEOUT_SECONDS,
            "idle_timeout_seconds": DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
            "subscription_tier": "default",
            "vision": "auto",
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
            "vision": "auto",
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
            "prompt_via_stdin": False,
            "output_flag": "",
            "timeout_seconds": 7200,
            "idle_timeout_seconds": 7200,
            "subscription_tier": "default",
            "vision": "disabled",
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
            "prompt_via_stdin": False,
            "output_flag": "",
            "timeout_seconds": 7200,
            "idle_timeout_seconds": 7200,
            "subscription_tier": "default",
            "vision": "disabled",
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
        "verification_policy_version": 4,
        "interactive_level": "affected",
        "release_verification_mode": "deferred",
        "unmapped_change_policy": "fallback",
        "fallback_proof_ids": [],
        "release_blocking_paths": [],
        "release_worker": {
            "enabled": False,
            "auto_start": False,
            "idle_delay_seconds": 60,
            "max_recovery_attempts": 2,
            "max_infrastructure_retries": 2,
            "background_parallel_workers": 1,
        },
        "incremental": {
            "mode": "auto",
            "warm_target_seconds": 900,
            "shard_target_seconds": 300,
            "cache_max_age_seconds": 1209600,
        },
        "require_clean_git_before_task": True,
        "allow_agent_updates": True,
        "parallel_workers": "auto",
        "max_auto_workers": "auto",
        "target_final_seconds": 0,
        "command_timeout_seconds": 7200,
        "worker_slot_wait_timeout_seconds": 7200,
        "adaptive_timeout_enabled": True,
        "command_idle_timeout_seconds": 900,
        "reported_infrastructure_markers": [],
        "isolation": {
            "enabled": True,
            "mode": "git_worktree",
            "worktree_root": "",
            "artifact_max_bytes": 268435456,
            "artifact_max_files": 2000,
        },
        "distributed": {
            "mode": "auto",
            "discovery_timeout_seconds": 1.5,
            "request_timeout_seconds": 15,
            "infrastructure_retry_limit": 2,
            "reported_infrastructure_max_workers": 8,
            "forward_environment": "all_except_denylist",
            "extra_environment_denylist": [],
        },
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
            "pressure_cooldown_seconds": 3600,
            "soft_pressure_threshold": 2,
        },
        "requirements_audit": {
            "pattern_timeout_ms": 250,
            "total_timeout_seconds": 300,
            "cache_enabled": True,
        },
        "evidence_preflight": {
            "mode": "high_risk",
        },
        "user_input": {
            "enabled": True,
            "mode": "auto",
            "secret_echo": "auto",
            "continue_independent_tasks": True,
            "auto_resume_on_answer": True,
            "operator_dir": ".auto-agents/operator",
        },
        "project_runtime": {
            "enabled": True,
            "root": ".auto-agents/runtime",
            "require_first_approval": True,
            "allow_downloads": True,
        },
        "smart_timeout": {
            "enabled": True,
            "provider_idle_seconds": 1800,
            "tool_idle_seconds": 900,
            "semantic_stall_seconds": 3600,
            "safety_ceiling_seconds": 14400,
            "loop_repeat_limit": 3,
            "same_provider_resume_limit": 1,
            "stage_progress_lease_seconds": {
                "plan": 1200,
                "implement": 3600,
                "review": 900,
                "clarify": 1200,
                "design": 1200,
                "readme": 900,
            },
            "post_ceiling_finalize_seconds": 600,
            "fresh_continuation_limit": 1,
        },
        "health_watch": {
            "enabled": True,
            "sidecar_enabled": True,
            "agent_triage_enabled": True,
            "poll_seconds": 30,
            "heartbeat_timeout_seconds": 120,
            "sidecar_grace_seconds": 60,
            "goal_stall_lease_multiplier": 2.0,
            "oscillation_repeat_limit": 3,
            "recovery_churn_limit": 3,
            "max_interventions_per_root": 3,
            "max_sidecar_restarts_per_run": 2,
            "quiesce_timeout_seconds": 600,
            "boundary_replay_timeout_seconds": 1200,
        },
        "provider_failover": {
            "probe_enabled": True,
            "probe_timeout_seconds": 60,
            "connection_cooldown_seconds": 60,
            "pressure_cooldown_seconds": 300,
            "timeout_cooldown_seconds": 1800,
            "quota_cooldown_seconds": 3600,
            "max_cooldown_seconds": 14400,
        },
        "self_repair_diagnosis": {
            "mode": "all_terminal",
            "investigator_timeout_seconds": 900,
            "reviewer_timeout_seconds": 600,
            "arbiter_timeout_seconds": 600,
            "command_timeout_seconds": 300,
            "max_dynamic_commands": 12,
            "confidence_threshold": 0.85,
            "arbiter_confidence_threshold": 0.90,
            "max_repair_cycles": 2,
            "network_enabled": False,
        },
        "autonomy": {
            "mode": "max",
            "max_consecutive_non_improving_candidates": 3,
            "max_frontier_candidates": 8,
            "candidate_timeout_seconds": 3600,
            "candidate_review_timeout_seconds": 600,
            "replay_timeout_seconds": 1200,
            "continue_independent_tasks": True,
            "allow_isolated_dirty_checkout": True,
            "require_remote_publish": False,
        },
        "recovery": {
            "enabled": True,
            "max_rounds": 2,
            "max_repair_tasks_per_round": 6,
            "max_refs_per_repair_task": 8,
            "max_incidents_per_run": 6,
            "max_occurrences_per_root_cause": 3,
            "diagnostic_probe_timeout_seconds": 300,
            "managed_runtime_downloads_enabled": True,
            "max_managed_runtime_candidates": 3,
            "managed_runtime_layout_repairs_enabled": True,
            "max_managed_repair_attempts_per_incident": 6,
        },
    },
    "approvals": {
        "enabled": ["requirements", "prototype", "architecture", "persistence-reset", "release"],
    },
    "retries": {
        "default_max_attempts": 2,
        "per_stage": dict(DEFAULT_RETRY_PER_STAGE),
    },
    "visual_judge": {
        "mode": "auto",
        "threshold": 85,
        "provider": "",
        "max_pairs_per_task": 6,
        "require_screenshot_artifacts": True,
    },
    "frontend_design": {
        "mode": "auto",
        "catalog_repository": "VoltAgent/awesome-design-md",
        "catalog_ref": "main",
        "max_pages": 3,
        "viewports": ["1440x900", "390x844"],
        "network_timeout_seconds": 30,
    },
    "persistence": {
        "targets": [],
    },
}


def default_provider_config(provider_name: str) -> ProviderConfig:
    """Return an independent typed copy of a built-in provider config."""
    raw = DEFAULT_CONFIG["providers"].get(provider_name)
    if not isinstance(raw, dict):
        supported = ", ".join(sorted(DEFAULT_CONFIG["providers"]))
        raise ValueError(
            f"Unsupported provider '{provider_name}'. Supported providers: {supported}"
        )
    return ProviderConfig.from_dict(copy.deepcopy(raw))


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


def operator_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "operator"


def operator_inputs_path(project_root: Path) -> Path:
    return operator_dir(project_root) / "inputs.json"


def operator_secrets_path(project_root: Path) -> Path:
    return operator_dir(project_root) / "secrets.env"


def project_runtime_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "runtime"


def runtime_requirements_lock_path(project_root: Path) -> Path:
    return state_dir(project_root) / "runtime_requirements.lock.json"


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


def frontend_design_lock_path(project_root: Path) -> Path:
    return state_dir(project_root) / "frontend_design.lock.json"


def frontend_design_docs_dir(project_root: Path) -> Path:
    return docs_dir(project_root) / "frontend_design"


def frontend_prototype_dir(project_root: Path) -> Path:
    return docs_dir(project_root) / "frontend_prototype"


def frontend_prototype_variants_dir(project_root: Path) -> Path:
    return docs_dir(project_root) / "frontend_prototype_variants"


def frontend_prototype_variants_registry_path(project_root: Path) -> Path:
    return state_dir(project_root) / "frontend_prototype_variants.json"


def frontend_design_cache_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "cache" / "awesome-design-md"


def design_md_path(project_root: Path) -> Path:
    return project_root / "DESIGN.md"


def provider_references_lock_path(project_root: Path) -> Path:
    return state_dir(project_root) / "provider_references.lock.json"


def project_rules_path(project_root: Path) -> Path:
    return auto_dir(project_root) / "project-rules.md"


def normalized_project_rules_path(project_root: Path) -> Path:
    return auto_dir(project_root) / "project-rules.normalized.json"


def agent_instructions_lock_path(project_root: Path) -> Path:
    return state_dir(project_root) / "agent_instructions.lock.json"


def gate_baseline_cache_path(project_root: Path) -> Path:
    return state_dir(project_root) / "gate_baseline_cache.sqlite3"


def requirements_audit_cache_path(project_root: Path) -> Path:
    return state_dir(project_root) / "requirements_audit_cache.sqlite3"


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
    gates = data.get("gates") if isinstance(data, dict) else None
    migrated = False
    if isinstance(gates, dict):
        version = int(gates.get("verification_policy_version", 1) or 1)
        if version == 2 and "incremental" not in gates:
            gates["verification_policy_version"] = 3
            gates["incremental"] = copy.deepcopy(
                DEFAULT_CONFIG["gates"]["incremental"]
            )
            for step in gates.get("steps", []):
                if (
                    isinstance(step, dict)
                    and step.get("result_cache_scope", "candidate") == "candidate"
                ):
                    step["result_cache_scope"] = "auto"
            migrated = True
    if migrated:
        write_json(config_path(project_root), data)
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


def load_run_state(project_root: Path) -> RunState:
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
