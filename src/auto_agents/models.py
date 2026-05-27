from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .repomap.config import RepoMapConfig


STAGE_ORDER = ["clarify", "design", "plan", "provider_research", "implement", "verify", "readme"]
APPROVAL_ORDER = ["requirements", "architecture", "release"]
SESSION_MODES = ("fix", "collab", "provider_resolve")
SESSION_STATUSES = ("conversing", "executing", "verifying", "waiting_user", "completed", "failed")
DEFAULT_SESSION_MAX_ATTEMPTS = {"fix": 4, "collab": 10, "provider_resolve": 8}
SESSION_STALL_THRESHOLD = 3
SESSION_AGENT_ERROR_THRESHOLD = 5
SESSION_HARD_CEILING = {"fix": 15, "collab": 25, "provider_resolve": 15}
DOCUMENT_LANGUAGE_OPTIONS = ("en", "zh")
SUPPORTED_PROVIDER_KINDS = ("codex", "copilot-cli", "antigravity")
DEFAULT_EFFORTS = {
    "clarify": "deep",
    "design": "deep",
    "plan": "deep",
    "sync-agent-instructions": "deep",
    "provider_research": "deep",
    "implement": "deep",
    "review": "balanced",
    "verify": "balanced",
    "readme": "balanced",
    "arbiter": "balanced",
}
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 1800
DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS = 3600
LEGACY_PROVIDER_IDLE_TIMEOUT_SECONDS = 300
DEFAULT_COPILOT_CLI_TIMEOUT_SECONDS = 3600
DEFAULT_COPILOT_CLI_IDLE_TIMEOUT_SECONDS = 3600
DEFAULT_COPILOT_CLI_PROFILE_MAP = {"balanced": "balanced", "deep": "deep", "max": "max"}
DEFAULT_RETRY_PER_STAGE = {
    "clarify": 2,
    "design": 2,
    "plan": 3,
    "sync-agent-instructions": 2,
    "provider_research": 2,
    "implement": 4,
    "review": 2,
    "arbiter": 2,
}
APPROVAL_BY_STAGE = {
    "clarify": "requirements",
    "design": "architecture",
    "verify": "release",
}


@dataclass
class TaskSpec:
    task_id: str
    title: str
    description: str
    acceptance: List[str]
    requirement_ids: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    status: str = "pending"
    commit_message: str = ""
    commit_sha: str = ""
    review_summary: str = ""
    scope_boundaries: str = ""
    review_history: List[Dict[str, object]] = field(default_factory=list)
    verify_history: List[Dict[str, object]] = field(default_factory=list)
    verify_baseline_failures: List[str] = field(default_factory=list)
    verify_baseline_ref: str = ""
    parent_task_id: str = ""
    split_depth: int = 0
    expected_test_migrations: List[str] = field(default_factory=list)
    requirement_proofs: List[Dict[str, object]] = field(default_factory=list)
    scratchpad: str = ""
    arbitration_history: List[Dict[str, object]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TaskSpec":
        raw_history = data.get("review_history", [])
        history = []
        if isinstance(raw_history, list):
            for entry in raw_history:
                if isinstance(entry, dict):
                    history.append(entry)
        raw_verify_history = data.get("verify_history", [])
        verify_history = []
        if isinstance(raw_verify_history, list):
            for entry in raw_verify_history:
                if isinstance(entry, dict):
                    verify_history.append(entry)
        raw_requirement_proofs = data.get("requirement_proofs", [])
        requirement_proofs = []
        if isinstance(raw_requirement_proofs, list):
            for entry in raw_requirement_proofs:
                if isinstance(entry, dict):
                    requirement_proofs.append(entry)
        return cls(
            task_id=str(data["task_id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            acceptance=[str(item) for item in data.get("acceptance", [])],
            requirement_ids=[str(item) for item in data.get("requirement_ids", [])],
            depends_on=[str(item) for item in data.get("depends_on", [])],
            status=str(data.get("status", "pending")),
            commit_message=str(data.get("commit_message", "")),
            commit_sha=str(data.get("commit_sha", "")),
            review_summary=str(data.get("review_summary", "")),
            scope_boundaries=str(data.get("scope_boundaries", "")),
            review_history=history,
            verify_history=verify_history,
            verify_baseline_failures=[str(item) for item in data.get("verify_baseline_failures", [])],
            verify_baseline_ref=str(data.get("verify_baseline_ref", "")),
            parent_task_id=str(data.get("parent_task_id", "")),
            split_depth=int(data.get("split_depth", 0) or 0),
            expected_test_migrations=[str(item) for item in data.get("expected_test_migrations", [])],
            requirement_proofs=requirement_proofs,
            scratchpad=str(data.get("scratchpad", "")),
            arbitration_history=[
                entry for entry in (data.get("arbitration_history", []) or [])
                if isinstance(entry, dict)
            ],
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ProviderConfig:
    kind: str = "codex"
    binary: str = "codex"
    profile_map: Dict[str, str] = field(
        default_factory=lambda: {
            "balanced": "m",
            "deep": "h",
            "max": "xh",
        }
    )
    extra_args: List[str] = field(default_factory=list)
    cwd_flag: str = "-C"
    prompt_via_stdin: bool = True
    output_flag: str = "-o"
    timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    idle_timeout_seconds: int = DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ProviderConfig":
        kind = str(data.get("kind", "codex"))
        timeout_default = (
            DEFAULT_COPILOT_CLI_TIMEOUT_SECONDS
            if kind == "copilot-cli"
            else DEFAULT_PROVIDER_TIMEOUT_SECONDS
        )
        idle_timeout_default = (
            DEFAULT_COPILOT_CLI_IDLE_TIMEOUT_SECONDS
            if kind == "copilot-cli"
            else DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS
        )
        return cls(
            kind=kind,
            binary=str(data.get("binary", "codex")),
            profile_map={str(k): str(v) for k, v in dict(data.get("profile_map", {})).items()}
            or {"balanced": "m", "deep": "h", "max": "xh"},
            extra_args=[str(item) for item in data.get("extra_args", [])],
            cwd_flag=str(data.get("cwd_flag", "-C")),
            prompt_via_stdin=bool(data.get("prompt_via_stdin", True)),
            output_flag=str(data.get("output_flag", "-o")),
            timeout_seconds=cls._timeout_seconds_from_dict(data, timeout_default),
            idle_timeout_seconds=int(data.get("idle_timeout_seconds", idle_timeout_default)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @staticmethod
    def _timeout_seconds_from_dict(data: Dict[str, object], timeout_default: int) -> int:
        raw_timeout = data.get("timeout_seconds")
        if raw_timeout is None:
            return timeout_default

        timeout_seconds = int(raw_timeout)
        if ProviderConfig._is_legacy_default_copilot_timeout(data, timeout_seconds):
            return DEFAULT_COPILOT_CLI_TIMEOUT_SECONDS
        return timeout_seconds

    @staticmethod
    def _is_legacy_default_copilot_timeout(data: Dict[str, object], timeout_seconds: int) -> bool:
        if str(data.get("kind", "")) != "copilot-cli":
            return False
        if timeout_seconds != DEFAULT_PROVIDER_TIMEOUT_SECONDS:
            return False
        profile_map = {str(k): str(v) for k, v in dict(data.get("profile_map", {})).items()}
        idle_timeout = int(data.get("idle_timeout_seconds", LEGACY_PROVIDER_IDLE_TIMEOUT_SECONDS))
        return (
            str(data.get("binary", "copilot")) == "copilot"
            and profile_map == DEFAULT_COPILOT_CLI_PROFILE_MAP
            and [str(item) for item in data.get("extra_args", [])] == []
            and str(data.get("cwd_flag", "")) == ""
            and bool(data.get("prompt_via_stdin", True))
            and str(data.get("output_flag", "")) == ""
            and idle_timeout in {
                LEGACY_PROVIDER_IDLE_TIMEOUT_SECONDS,
                DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
            }
        )


@dataclass
class VerificationStep:
    kind: str = "test"
    runner: str = ""
    targets: List[str] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    command: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "VerificationStep":
        return cls(
            kind=str(data.get("kind", "test")),
            runner=str(data.get("runner", "")),
            targets=[str(item) for item in data.get("targets", [])],
            args=[str(item) for item in data.get("args", [])],
            command=str(data.get("command", "")),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "runner": self.runner,
            "targets": list(self.targets),
            "args": list(self.args),
        }


@dataclass
class GateParallelGroup:
    name: str = ""
    commands: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GateParallelGroup":
        return cls(
            name=str(data.get("name", "")),
            commands=[str(item) for item in data.get("commands", [])],
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class GateConfig:
    commands: List[str] = field(default_factory=list)
    steps: List[VerificationStep] = field(default_factory=list)
    parallel_groups: List[GateParallelGroup] = field(default_factory=list)
    require_clean_git_before_task: bool = True
    allow_agent_updates: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GateConfig":
        raw_groups = data.get("parallel_groups", [])
        raw_steps = data.get("steps", [])
        return cls(
            commands=[str(item) for item in data.get("commands", [])],
            steps=[
                VerificationStep.from_dict(dict(item))
                for item in raw_steps
                if isinstance(item, dict)
            ],
            parallel_groups=[
                GateParallelGroup.from_dict(dict(item))
                for item in raw_groups
                if isinstance(item, dict)
            ],
            require_clean_git_before_task=bool(data.get("require_clean_git_before_task", True)),
            allow_agent_updates=bool(data.get("allow_agent_updates", True)),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "commands": list(self.commands),
            "steps": [step.to_dict() for step in self.steps],
            "parallel_groups": [group.to_dict() for group in self.parallel_groups],
            "require_clean_git_before_task": self.require_clean_git_before_task,
            "allow_agent_updates": self.allow_agent_updates,
        }


@dataclass
class GitConfig:
    auto_init_repo: bool = True
    commit_each_task: bool = True
    commit_message_template: str = "feat({task_id}): {title}"

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GitConfig":
        return cls(
            auto_init_repo=bool(data.get("auto_init_repo", True)),
            commit_each_task=bool(data.get("commit_each_task", True)),
            commit_message_template=str(
                data.get("commit_message_template", "feat({task_id}): {title}")
            ),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ApprovalConfig:
    enabled: List[str] = field(
        default_factory=lambda: ["requirements", "architecture", "release"]
    )

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ApprovalConfig":
        return cls(enabled=[str(item) for item in data.get("enabled", [])])

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class DocsConfig:
    language: str = "en"

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "DocsConfig":
        return cls(language=str(data.get("language", "en")))

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class RetryConfig:
    default_max_attempts: int = 2
    per_stage: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RETRY_PER_STAGE))

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RetryConfig":
        configured = {str(k): int(v) for k, v in dict(data.get("per_stage", {})).items()}
        return cls(
            default_max_attempts=int(data.get("default_max_attempts", 2)),
            per_stage={**DEFAULT_RETRY_PER_STAGE, **configured},
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ParallelTasksConfig:
    enabled: bool = False
    max_workers: int = 2
    strict: bool = False
    worktree_root: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ParallelTasksConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            max_workers=int(data.get("max_workers", 2)),
            strict=bool(data.get("strict", False)),
            worktree_root=str(data.get("worktree_root", "")),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ExecutionConfig:
    parallel_tasks: ParallelTasksConfig = field(default_factory=ParallelTasksConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ExecutionConfig":
        return cls(
            parallel_tasks=ParallelTasksConfig.from_dict(dict(data.get("parallel_tasks", {}))),
        )

    def to_dict(self) -> Dict[str, object]:
        return {"parallel_tasks": self.parallel_tasks.to_dict()}


@dataclass
class ProjectConfig:
    project_name: str
    providers: Dict[str, ProviderConfig] = field(
        default_factory=lambda: {
            "codex": ProviderConfig(
                kind="codex",
                binary="codex",
                profile_map={"balanced": "m", "deep": "h", "max": "xh"},
                extra_args=[],
                cwd_flag="-C",
                prompt_via_stdin=True,
                output_flag="-o",
                timeout_seconds=DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                idle_timeout_seconds=DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS,
            ),
            "copilot-cli": ProviderConfig(
                kind="copilot-cli",
                binary="copilot",
                profile_map=dict(DEFAULT_COPILOT_CLI_PROFILE_MAP),
                extra_args=[],
                cwd_flag="",
                prompt_via_stdin=True,
                output_flag="",
                timeout_seconds=DEFAULT_COPILOT_CLI_TIMEOUT_SECONDS,
                idle_timeout_seconds=DEFAULT_COPILOT_CLI_IDLE_TIMEOUT_SECONDS,
            ),
            "antigravity-claude": ProviderConfig(
                kind="antigravity",
                binary="agy-proxy",
                profile_map={
                    "balanced": "Claude Sonnet 4.6 (Thinking)",
                    "deep": "Claude Opus 4.6 (Thinking)",
                    "max": "Claude Opus 4.6 (Thinking)",
                },
                extra_args=[],
                cwd_flag="",
                prompt_via_stdin=True,
                output_flag="",
                timeout_seconds=7200,
                idle_timeout_seconds=7200,
            ),
            "antigravity-gemini": ProviderConfig(
                kind="antigravity",
                binary="agy-proxy",
                profile_map={
                    "balanced": "Gemini 3.5 Flash (Low)",
                    "deep": "Gemini 3.5 Flash (Medium)",
                    "max": "Gemini 3.5 Flash (High)",
                },
                extra_args=[],
                cwd_flag="",
                prompt_via_stdin=True,
                output_flag="",
                timeout_seconds=7200,
                idle_timeout_seconds=7200,
            ),
        }
    )
    active_provider: str = "codex"
    docs: DocsConfig = field(default_factory=DocsConfig)
    efforts: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EFFORTS))
    gates: GateConfig = field(default_factory=GateConfig)
    git: GitConfig = field(default_factory=GitConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    approvals: ApprovalConfig = field(default_factory=ApprovalConfig)
    retries: RetryConfig = field(default_factory=RetryConfig)
    repo_map: RepoMapConfig = field(default_factory=RepoMapConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ProjectConfig":
        providers_payload = data.get("providers")
        active_provider = str(data.get("active_provider", "")).strip()
        if not isinstance(providers_payload, dict) or not active_provider:
            raise ValueError(
                "Invalid config format: expected 'providers' and 'active_provider'. "
                "Re-run 'auto_agents init' to regenerate the config."
            )
        if "agent_instructions" in data:
            raise ValueError(
                "Invalid config format: 'agent_instructions' is no longer supported. "
                "Move sync-agent-instructions effort into 'efforts.sync-agent-instructions' "
                "and re-run 'auto_agents init' or update the project config."
            )

        providers = {
            str(kind): ProviderConfig.from_dict(dict(raw))
            for kind, raw in providers_payload.items()
            if isinstance(raw, dict)
        }
        if active_provider not in providers:
            raise ValueError(
                f"Invalid config format: active_provider '{active_provider}' is not defined in providers. "
                "Re-run 'auto_agents init' to regenerate the config."
            )

        return cls(
            project_name=str(data.get("project_name", "unnamed-project")),
            providers=providers,
            active_provider=active_provider,
            docs=DocsConfig.from_dict(dict(data.get("docs", {}))),
            efforts={
                **DEFAULT_EFFORTS,
                **{str(k): str(v) for k, v in dict(data.get("efforts", {})).items()},
            },
            gates=GateConfig.from_dict(dict(data.get("gates", {}))),
            git=GitConfig.from_dict(dict(data.get("git", {}))),
            execution=ExecutionConfig.from_dict(dict(data.get("execution", {}))),
            approvals=ApprovalConfig.from_dict(
                dict(
                    data.get(
                        "approvals",
                        {"enabled": ["requirements", "architecture", "release"]},
                    )
                )
            ),
            retries=RetryConfig.from_dict(dict(data.get("retries", {}))),
            repo_map=RepoMapConfig.from_dict(dict(data.get("repo_map", {}))),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "project_name": self.project_name,
            "providers": {kind: provider.to_dict() for kind, provider in self.providers.items()},
            "active_provider": self.active_provider,
            "docs": self.docs.to_dict(),
            "efforts": dict(self.efforts),
            "gates": self.gates.to_dict(),
            "git": self.git.to_dict(),
            "execution": self.execution.to_dict(),
            "approvals": self.approvals.to_dict(),
            "retries": self.retries.to_dict(),
            "repo_map": self.repo_map.to_dict(),
        }

    @property
    def provider(self) -> ProviderConfig:
        provider = self.providers.get(self.active_provider)
        if provider is None:
            raise ValueError(
                f"Configured active_provider '{self.active_provider}' is missing from providers."
            )
        return provider

    def set_active_provider(self, provider_kind: str) -> None:
        if provider_kind not in self.providers:
            supported = ", ".join(sorted(self.providers.keys()))
            raise ValueError(f"Unsupported provider '{provider_kind}'. Supported providers: {supported}")
        self.active_provider = provider_kind


@dataclass
class RunState:
    run_id: str
    status: str = "pending"
    current_stage: str = "clarify"
    pending_approval: str = ""
    approved_gates: List[str] = field(default_factory=list)
    tasks: List[TaskSpec] = field(default_factory=list)
    stage_summaries: Dict[str, str] = field(default_factory=dict)
    agent_attempts: Dict[str, int] = field(default_factory=dict)
    task_review_cache: Dict[str, Dict[str, str]] = field(default_factory=dict)
    implement_verify_baseline_failures: List[str] = field(default_factory=list)
    implement_verify_baseline_ref: str = ""
    plan_task_replacements: Dict[str, List[str]] = field(default_factory=dict)
    last_error: str = ""
    rejection_reason: str = ""
    rejected_stage: str = ""
    resume_context: Dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RunState":
        return cls(
            run_id=str(data["run_id"]),
            status=str(data.get("status", "pending")),
            current_stage=str(data.get("current_stage", "clarify")),
            pending_approval=str(data.get("pending_approval", "")),
            approved_gates=[str(item) for item in data.get("approved_gates", [])],
            tasks=[TaskSpec.from_dict(item) for item in data.get("tasks", [])],
            stage_summaries={
                str(key): str(value) for key, value in dict(data.get("stage_summaries", {})).items()
            },
            agent_attempts={
                str(key): int(value) for key, value in dict(data.get("agent_attempts", {})).items()
            },
            task_review_cache={
                str(key): {str(inner_key): str(inner_value) for inner_key, inner_value in dict(value).items()}
                for key, value in dict(data.get("task_review_cache", {})).items()
            },
            implement_verify_baseline_failures=[
                str(item) for item in data.get("implement_verify_baseline_failures", [])
            ],
            implement_verify_baseline_ref=str(data.get("implement_verify_baseline_ref", "")),
            plan_task_replacements={
                str(key): [str(item) for item in value]
                for key, value in dict(data.get("plan_task_replacements", {})).items()
                if isinstance(value, list)
            },
            last_error=str(data.get("last_error", "")),
            rejection_reason=str(data.get("rejection_reason", "")),
            rejected_stage=str(data.get("rejected_stage", "")),
            resume_context=dict(data.get("resume_context", {})),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "current_stage": self.current_stage,
            "pending_approval": self.pending_approval,
            "approved_gates": list(self.approved_gates),
            "tasks": [task.to_dict() for task in self.tasks],
            "stage_summaries": dict(self.stage_summaries),
            "agent_attempts": dict(self.agent_attempts),
            "task_review_cache": {
                key: {inner_key: inner_value for inner_key, inner_value in value.items()}
                for key, value in self.task_review_cache.items()
            },
            "implement_verify_baseline_failures": list(self.implement_verify_baseline_failures),
            "implement_verify_baseline_ref": self.implement_verify_baseline_ref,
            "plan_task_replacements": {
                key: list(value) for key, value in self.plan_task_replacements.items()
            },
            "last_error": self.last_error,
            "rejection_reason": self.rejection_reason,
            "rejected_stage": self.rejected_stage,
            "resume_context": dict(self.resume_context),
        }


@dataclass
class SessionState:
    session_id: str
    mode: str = "fix"
    status: str = "conversing"
    goal: str = ""
    conversation: List[Dict[str, str]] = field(default_factory=list)
    execution_log: List[Dict[str, object]] = field(default_factory=list)
    current_attempt: int = 0
    max_attempts: int = 4
    resolution: str = ""
    created_at: str = ""
    updated_at: str = ""
    stall_count: int = 0
    last_diff_hash: str = ""
    last_verify_sig: str = ""
    consecutive_agent_errors: int = 0
    hard_ceiling: int = 15
    fix_verify_command: str = ""
    baseline_failures: List[str] = field(default_factory=list)
    baseline_git_ref: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "SessionState":
        return cls(
            session_id=str(data["session_id"]),
            mode=str(data.get("mode", "fix")),
            status=str(data.get("status", "conversing")),
            goal=str(data.get("goal", "")),
            conversation=[
                {str(k): str(v) for k, v in dict(item).items()}
                for item in data.get("conversation", [])
                if isinstance(item, dict)
            ],
            execution_log=list(data.get("execution_log", [])),
            current_attempt=int(data.get("current_attempt", 0)),
            max_attempts=int(data.get("max_attempts", 4)),
            resolution=str(data.get("resolution", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            stall_count=int(data.get("stall_count", 0)),
            last_diff_hash=str(data.get("last_diff_hash", "")),
            last_verify_sig=str(data.get("last_verify_sig", "")),
            consecutive_agent_errors=int(data.get("consecutive_agent_errors", 0)),
            hard_ceiling=int(data.get("hard_ceiling", SESSION_HARD_CEILING.get(
                str(data.get("mode", "fix")), 15,
            ))),
            fix_verify_command=str(data.get("fix_verify_command", "")),
            baseline_failures=[str(f) for f in data.get("baseline_failures", [])],
            baseline_git_ref=str(data.get("baseline_git_ref", "")),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "status": self.status,
            "goal": self.goal,
            "conversation": list(self.conversation),
            "execution_log": list(self.execution_log),
            "current_attempt": self.current_attempt,
            "max_attempts": self.max_attempts,
            "resolution": self.resolution,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stall_count": self.stall_count,
            "last_diff_hash": self.last_diff_hash,
            "last_verify_sig": self.last_verify_sig,
            "consecutive_agent_errors": self.consecutive_agent_errors,
            "hard_ceiling": self.hard_ceiling,
            "fix_verify_command": self.fix_verify_command,
            "baseline_failures": list(self.baseline_failures),
            "baseline_git_ref": self.baseline_git_ref,
        }


@dataclass
class AgentRequest:
    stage: str
    effort: str
    prompt: str
    cwd: Path
    output_path: Path
    stream_output: Optional[Callable[[str, str], None]] = None


@dataclass
class AgentUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, other: Optional["AgentUsage"]) -> "AgentUsage":
        if other is None:
            return AgentUsage(
                input_tokens=self.input_tokens,
                cached_input_tokens=self.cached_input_tokens,
                output_tokens=self.output_tokens,
            )
        return AgentUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass
class AgentResult:
    ok: bool
    command: List[str]
    output_path: Path
    summary: str = ""
    model: str = ""
    usage: Optional[AgentUsage] = None
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    streamed_stdout: bool = False
    streamed_stderr: bool = False


@dataclass
class CommandResult:
    command: str
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    comparable_failures: bool = False


@dataclass
class GateResult:
    ok: bool
    commands: List[CommandResult]
    summary: str = ""
    comparable_failures: bool = True
