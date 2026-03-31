from __future__ import annotations

from pathlib import Path
from typing import Tuple
from uuid import uuid4

from .io_utils import read_json, read_text, write_if_missing, write_json, write_text
from .models import ProjectConfig, RunState


AUTO_DIR = ".auto-agents"
CONFIG_FILE = "config.json"
PROJECT_GITIGNORE = """__pycache__/
*.pyc
.pytest_cache/
.conda/
.venv/
node_modules/
.DS_Store
"""


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
            "acceptance": ["State one concrete acceptance criterion."],
            "status": "pending",
            "commit_message": ""
        }
    ]
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
}


DEFAULT_CONFIG = {
    "project_name": "unnamed-project",
    "provider": {
        "kind": "codex",
        "binary": "codex",
        "profile_map": {
            "balanced": "m",
            "deep": "h",
        },
        "extra_args": [],
        "cwd_flag": "-C",
        "prompt_via_stdin": True,
        "output_flag": "-o",
    },
    "docs": {
        "language": "en",
    },
    "efforts": {
        "clarify": "deep",
        "design": "deep",
        "plan": "balanced",
        "implement": "balanced",
        "review": "balanced",
        "verify": "balanced",
    },
    "gates": {
        "commands": [],
        "require_clean_git_before_task": True,
        "allow_agent_updates": True,
    },
    "git": {
        "auto_init_repo": True,
        "commit_each_task": True,
        "commit_message_template": "feat({task_id}): {title}",
    },
    "approvals": {
        "enabled": ["requirements", "architecture", "release"],
    },
    "retries": {
        "default_max_attempts": 2,
        "per_stage": {
            "clarify": 2,
            "design": 2,
            "plan": 3,
            "implement": 2,
            "review": 2
        }
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


def state_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "state"


def runs_dir(project_root: Path) -> Path:
    return auto_dir(project_root) / "runs"


def run_path(project_root: Path, run_id: str) -> Path:
    return runs_dir(project_root) / run_id


def run_state_path(project_root: Path) -> Path:
    return state_dir(project_root) / "run_state.json"


def task_plan_path(project_root: Path) -> Path:
    return state_dir(project_root) / "task_plan.json"


def review_path(project_root: Path) -> Path:
    return docs_dir(project_root) / "review.md"


def bootstrap_project(project_root: Path, name: str, provider_kind: str, doc_language: str = "en") -> Path:
    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    config = dict(DEFAULT_CONFIG)
    config["project_name"] = name
    config["provider"] = dict(DEFAULT_CONFIG["provider"])
    config["provider"]["kind"] = provider_kind
    config["provider"]["binary"] = "codex" if provider_kind == "codex" else provider_kind
    config["docs"] = dict(DEFAULT_CONFIG["docs"])
    config["docs"]["language"] = doc_language

    write_json(config_path(root), config)
    write_if_missing(
        auto_dir(root) / ".gitignore",
        "runs/\nstate/run_state.json\n",
    )
    write_if_missing(project_brief_path(root), PROJECT_BRIEF_TEMPLATE)
    write_if_missing(architecture_path(root), ARCHITECTURE_TEMPLATE)
    write_if_missing(review_path(root), "# Review\n\nNo review has been recorded yet.\n")
    write_json(task_plan_path(root), TASK_PLAN_TEMPLATE)
    write_json(run_state_path(root), RUN_STATE_TEMPLATE)
    write_if_missing(root / ".gitignore", PROJECT_GITIGNORE)
    write_if_missing(root / "README.md", f"# {name}\n")
    return root


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


def load_run_state(project_root: Path) -> RunState:
    data = read_json(run_state_path(project_root), default=None)
    if data is None or not data.get("run_id"):
        return create_run(project_root)
    return RunState.from_dict(data)


def save_run_state(project_root: Path, state: RunState) -> None:
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
