from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


STAGE_ORDER = ["clarify", "design", "plan", "implement", "verify", "readme"]
APPROVAL_ORDER = ["requirements", "architecture", "release"]
DOCUMENT_LANGUAGE_OPTIONS = ("en", "zh")
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
    status: str = "pending"
    commit_message: str = ""
    commit_sha: str = ""
    review_summary: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TaskSpec":
        return cls(
            task_id=str(data["task_id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            acceptance=[str(item) for item in data.get("acceptance", [])],
            status=str(data.get("status", "pending")),
            commit_message=str(data.get("commit_message", "")),
            commit_sha=str(data.get("commit_sha", "")),
            review_summary=str(data.get("review_summary", "")),
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

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ProviderConfig":
        return cls(
            kind=str(data.get("kind", "codex")),
            binary=str(data.get("binary", "codex")),
            profile_map={str(k): str(v) for k, v in dict(data.get("profile_map", {})).items()}
            or {"balanced": "m", "deep": "h"},
            extra_args=[str(item) for item in data.get("extra_args", [])],
            cwd_flag=str(data.get("cwd_flag", "-C")),
            prompt_via_stdin=bool(data.get("prompt_via_stdin", True)),
            output_flag=str(data.get("output_flag", "-o")),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class GateConfig:
    commands: List[str] = field(default_factory=list)
    require_clean_git_before_task: bool = True
    allow_agent_updates: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GateConfig":
        return cls(
            commands=[str(item) for item in data.get("commands", [])],
            require_clean_git_before_task=bool(data.get("require_clean_git_before_task", True)),
            allow_agent_updates=bool(data.get("allow_agent_updates", True)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


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
    per_stage: Dict[str, int] = field(
        default_factory=lambda: {
            "clarify": 2,
            "design": 2,
            "plan": 3,
            "implement": 4,
            "review": 2,
        }
    )

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RetryConfig":
        return cls(
            default_max_attempts=int(data.get("default_max_attempts", 2)),
            per_stage={str(k): int(v) for k, v in dict(data.get("per_stage", {})).items()}
            or {
                "clarify": 2,
                "design": 2,
                "plan": 3,
                "implement": 4,
                "review": 2,
            },
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ProjectConfig:
    project_name: str
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    docs: DocsConfig = field(default_factory=DocsConfig)
    efforts: Dict[str, str] = field(
        default_factory=lambda: {
            "clarify": "deep",
            "design": "deep",
            "plan": "deep",
            "implement": "deep",
            "review": "balanced",
            "verify": "balanced",
        }
    )
    gates: GateConfig = field(default_factory=GateConfig)
    git: GitConfig = field(default_factory=GitConfig)
    approvals: ApprovalConfig = field(default_factory=ApprovalConfig)
    retries: RetryConfig = field(default_factory=RetryConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ProjectConfig":
        return cls(
            project_name=str(data.get("project_name", "unnamed-project")),
            provider=ProviderConfig.from_dict(dict(data.get("provider", {}))),
            docs=DocsConfig.from_dict(dict(data.get("docs", {}))),
            efforts={str(k): str(v) for k, v in dict(data.get("efforts", {})).items()}
            or {
                "clarify": "deep",
                "design": "deep",
                "plan": "balanced",
                "implement": "balanced",
                "review": "balanced",
                "verify": "balanced",
            },
            gates=GateConfig.from_dict(dict(data.get("gates", {}))),
            git=GitConfig.from_dict(dict(data.get("git", {}))),
            approvals=ApprovalConfig.from_dict(
                dict(
                    data.get(
                        "approvals",
                        {"enabled": ["requirements", "architecture", "release"]},
                    )
                )
            ),
            retries=RetryConfig.from_dict(dict(data.get("retries", {}))),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "project_name": self.project_name,
            "provider": self.provider.to_dict(),
            "docs": self.docs.to_dict(),
            "efforts": dict(self.efforts),
            "gates": self.gates.to_dict(),
            "git": self.git.to_dict(),
            "approvals": self.approvals.to_dict(),
            "retries": self.retries.to_dict(),
        }


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
    last_error: str = ""
    rejection_reason: str = ""
    rejected_stage: str = ""

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
            last_error=str(data.get("last_error", "")),
            rejection_reason=str(data.get("rejection_reason", "")),
            rejected_stage=str(data.get("rejected_stage", "")),
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
            "last_error": self.last_error,
            "rejection_reason": self.rejection_reason,
            "rejected_stage": self.rejected_stage,
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


@dataclass
class GateResult:
    ok: bool
    commands: List[CommandResult]
    summary: str = ""
