from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from .repomap.config import RepoMapConfig


STAGE_ORDER = ["clarify", "prototype", "design", "plan", "provider_research", "implement", "visual_judge", "verify", "readme"]
APPROVAL_ORDER = ["requirements", "prototype", "architecture", "release"]
SESSION_MODES = ("fix", "collab", "provider_resolve")
SESSION_STATUSES = ("conversing", "executing", "verifying", "waiting_user", "completed", "failed")
DEFAULT_SESSION_MAX_ATTEMPTS = {"fix": 4, "collab": 10, "provider_resolve": 8}
SESSION_STALL_THRESHOLD = 3
SESSION_AGENT_ERROR_THRESHOLD = 5
SESSION_HARD_CEILING = {"fix": 15, "collab": 25, "provider_resolve": 15}
DOCUMENT_LANGUAGE_OPTIONS = ("en", "zh")
TASK_ORIGINS = ("planned", "scope_split", "evidence_repair", "stage_recovery")
VERIFICATION_CADENCES = ("implement_and_final", "final_only")
VERIFICATION_CACHE_SCOPES = ("source", "run_context")
VERIFICATION_RESOURCE_CLASSES = ("normal", "heavy")
VERIFICATION_MEMORY_GUARDS = ("off", "advisory", "required")
SUPPORTED_PROVIDER_KINDS = ("codex", "copilot-cli", "antigravity-claude", "antigravity-gemini")
DEFAULT_EFFORTS = {
    "clarify": "deep",
    "prototype": "max",
    "design": "max",
    "plan": "max",
    "sync-agent-instructions": "deep",
    "provider_research": "deep",
    "implement": "deep",
    "review": "balanced",
    "visual_judge": "balanced",
    "self_repair": "max",
    "verify": "balanced",
    "readme": "balanced",
    "arbiter": "balanced",
    "incident_judge": "max",
    "evidence_preflight": "balanced",
}
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 1800
DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS = 3600
LEGACY_PROVIDER_IDLE_TIMEOUT_SECONDS = 300
DEFAULT_COPILOT_CLI_TIMEOUT_SECONDS = 3600
DEFAULT_COPILOT_CLI_IDLE_TIMEOUT_SECONDS = 3600
DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS = 7200
DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS = 900
DEFAULT_COPILOT_CLI_PROFILE_MAP = {"balanced": "balanced", "deep": "deep", "max": "max"}
SMART_TIMEOUT_PROGRESS_PROTOCOL = "auto-agents-jsonl-v1"
DEFAULT_RETRY_PER_STAGE = {
    "clarify": 2,
    "prototype": 2,
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
    "prototype": "prototype",
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
    task_origin: str = "planned"
    recovery_epoch: int = 0
    recovery_round: int = 0
    expected_test_migrations: List[str] = field(default_factory=list)
    requirement_proofs: List[Dict[str, object]] = field(default_factory=list)
    verification_refs: List[str] = field(default_factory=list)
    scratchpad: str = ""
    arbitration_history: List[Dict[str, object]] = field(default_factory=list)
    recovery_history: List[Dict[str, object]] = field(default_factory=list)
    evidence_preflight: Dict[str, object] = field(default_factory=dict)
    verify_retry_epoch: int = 0

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
            task_origin=str(data.get("task_origin", "planned") or "planned"),
            recovery_epoch=max(0, int(data.get("recovery_epoch", 0) or 0)),
            recovery_round=max(0, int(data.get("recovery_round", 0) or 0)),
            expected_test_migrations=[str(item) for item in data.get("expected_test_migrations", [])],
            requirement_proofs=requirement_proofs,
            verification_refs=[str(item) for item in data.get("verification_refs", [])],
            scratchpad=str(data.get("scratchpad", "")),
            arbitration_history=[
                entry for entry in (data.get("arbitration_history", []) or [])
                if isinstance(entry, dict)
            ],
            recovery_history=[
                entry for entry in (data.get("recovery_history", []) or [])
                if isinstance(entry, dict)
            ],
            evidence_preflight=(
                dict(data.get("evidence_preflight", {}))
                if isinstance(data.get("evidence_preflight", {}), dict)
                else {}
            ),
            verify_retry_epoch=max(0, int(data.get("verify_retry_epoch", 0) or 0)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ProviderConfig:
    kind: str = "codex"
    binary: str = "codex"
    profile_map: Dict[str, str] = field(
        default_factory=lambda: {
            "balanced": "balanced",
            "deep": "deep",
            "max": "max",
        }
    )
    extra_args: List[str] = field(default_factory=list)
    cwd_flag: str = "-C"
    prompt_via_stdin: bool = True
    output_flag: str = "-o"
    timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    idle_timeout_seconds: int = DEFAULT_PROVIDER_IDLE_TIMEOUT_SECONDS
    subscription_tier: str = "default"
    vision: str = "auto"
    progress_protocol: str = ""

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
            or {"balanced": "balanced", "deep": "deep", "max": "max"},
            extra_args=[str(item) for item in data.get("extra_args", [])],
            cwd_flag=str(data.get("cwd_flag", "-C")),
            prompt_via_stdin=(
                False
                if kind == "antigravity"
                else bool(data.get("prompt_via_stdin", True))
            ),
            output_flag=str(data.get("output_flag", "-o")),
            timeout_seconds=cls._timeout_seconds_from_dict(data, timeout_default),
            idle_timeout_seconds=int(data.get("idle_timeout_seconds", idle_timeout_default)),
            subscription_tier=str(data.get("subscription_tier", "default")),
            vision=str(data.get("vision", "auto")),
            progress_protocol=str(data.get("progress_protocol", "")),
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
    parallel_safe: bool = False
    cadence: str = "implement_and_final"
    cache_scope: str = "run_context"
    resource_class: str = "normal"
    cpu_slots: int = 0
    memory_mb: int = 0
    memory_reserve_mb: int = 0
    memory_guard: str = "off"
    requires: List[str] = field(default_factory=list)
    exclusive_resources: List[str] = field(default_factory=list)
    dynamic_ports: List[str] = field(default_factory=list)
    artifact_globs: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "VerificationStep":
        return cls(
            kind=str(data.get("kind", "test")),
            runner=str(data.get("runner", "")),
            targets=[str(item) for item in data.get("targets", [])],
            args=[str(item) for item in data.get("args", [])],
            command=str(data.get("command", "")),
            parallel_safe=bool(data.get("parallel_safe", False)),
            cadence=str(data.get("cadence", "implement_and_final")),
            cache_scope=str(data.get("cache_scope", "run_context")),
            resource_class=str(data.get("resource_class", "normal")),
            cpu_slots=int(data.get("cpu_slots", 0) or 0),
            memory_mb=int(data.get("memory_mb", 0) or 0),
            memory_reserve_mb=int(data.get("memory_reserve_mb", 0) or 0),
            memory_guard=str(data.get("memory_guard", "off")),
            requires=[str(item) for item in data.get("requires", [])],
            exclusive_resources=[
                str(item) for item in data.get("exclusive_resources", [])
            ],
            dynamic_ports=[
                str(item) for item in data.get("dynamic_ports", [])
            ],
            artifact_globs=[str(item) for item in data.get("artifact_globs", [])],
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "runner": self.runner,
            "targets": list(self.targets),
            "args": list(self.args),
            "parallel_safe": self.parallel_safe,
            "cadence": self.cadence,
            "cache_scope": self.cache_scope,
            "resource_class": self.resource_class,
            "cpu_slots": self.cpu_slots,
            "memory_mb": self.memory_mb,
            "memory_reserve_mb": self.memory_reserve_mb,
            "memory_guard": self.memory_guard,
            "requires": list(self.requires),
            "exclusive_resources": list(self.exclusive_resources),
            "dynamic_ports": list(self.dynamic_ports),
            "artifact_globs": list(self.artifact_globs),
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
class GateIsolationConfig:
    enabled: bool = False
    mode: str = "git_worktree"
    worktree_root: str = ""
    artifact_max_bytes: int = 256 * 1024 * 1024
    artifact_max_files: int = 2000

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GateIsolationConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            mode=str(data.get("mode", "git_worktree")),
            worktree_root=str(data.get("worktree_root", "")),
            artifact_max_bytes=int(data.get("artifact_max_bytes", 256 * 1024 * 1024)),
            artifact_max_files=int(data.get("artifact_max_files", 2000)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class DistributedGatesConfig:
    mode: str = "auto"
    discovery_timeout_seconds: float = 1.5
    request_timeout_seconds: int = 15
    infrastructure_retry_limit: int = 2
    reported_infrastructure_max_workers: int = 8
    forward_environment: str = "all_except_denylist"
    extra_environment_denylist: List[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "DistributedGatesConfig":
        raw_mode = data.get("mode")
        if raw_mode is None and "enabled" in data:
            raw_mode = "auto" if bool(data.get("enabled")) else "off"
        return cls(
            mode=str(raw_mode or "auto"),
            discovery_timeout_seconds=float(
                data.get("discovery_timeout_seconds", 1.5)
            ),
            request_timeout_seconds=int(data.get("request_timeout_seconds", 15)),
            infrastructure_retry_limit=int(data.get("infrastructure_retry_limit", 2)),
            reported_infrastructure_max_workers=int(
                data.get("reported_infrastructure_max_workers", 8)
            ),
            forward_environment=str(
                data.get("forward_environment", "all_except_denylist")
            ),
            extra_environment_denylist=[
                str(item) for item in data.get("extra_environment_denylist", [])
            ],
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class InfrastructureFailureMarker:
    marker_id: str
    contains: str

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "InfrastructureFailureMarker":
        return cls(
            marker_id=str(data.get("id", "")).strip(),
            contains=str(data.get("contains", "")).strip(),
        )

    def to_dict(self) -> Dict[str, str]:
        return {"id": self.marker_id, "contains": self.contains}


@dataclass
class GateConfig:
    commands: List[str] = field(default_factory=list)
    steps: List[VerificationStep] = field(default_factory=list)
    parallel_groups: List[GateParallelGroup] = field(default_factory=list)
    require_clean_git_before_task: bool = True
    allow_agent_updates: bool = True
    parallel_workers: Union[int, str] = "auto"
    max_auto_workers: Union[int, str] = "auto"
    command_timeout_seconds: int = DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS
    adaptive_timeout_enabled: bool = True
    command_idle_timeout_seconds: int = DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS
    reported_infrastructure_markers: List[InfrastructureFailureMarker] = field(
        default_factory=list
    )
    isolation: GateIsolationConfig = field(default_factory=GateIsolationConfig)
    distributed: DistributedGatesConfig = field(default_factory=DistributedGatesConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GateConfig":
        raw_groups = data.get("parallel_groups", [])
        raw_steps = data.get("steps", [])
        command_timeout_seconds = int(
            data.get("command_timeout_seconds", DEFAULT_GATE_COMMAND_TIMEOUT_SECONDS)
        )
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
            parallel_workers=(
                int(data.get("parallel_workers"))
                if isinstance(data.get("parallel_workers"), int)
                else str(data.get("parallel_workers", "auto"))
            ),
            max_auto_workers=(
                int(data.get("max_auto_workers"))
                if isinstance(data.get("max_auto_workers"), int)
                else str(data.get("max_auto_workers", "auto"))
            ),
            command_timeout_seconds=command_timeout_seconds,
            adaptive_timeout_enabled=bool(data.get("adaptive_timeout_enabled", True)),
            command_idle_timeout_seconds=int(
                data.get(
                    "command_idle_timeout_seconds",
                    min(DEFAULT_GATE_COMMAND_IDLE_TIMEOUT_SECONDS, command_timeout_seconds),
                )
            ),
            reported_infrastructure_markers=[
                InfrastructureFailureMarker.from_dict(dict(item))
                for item in data.get("reported_infrastructure_markers", [])
                if isinstance(item, dict)
            ],
            isolation=GateIsolationConfig.from_dict(
                dict(data.get("isolation", {}))
                if isinstance(data.get("isolation", {}), dict)
                else {}
            ),
            distributed=DistributedGatesConfig.from_dict(
                dict(data.get("distributed", {}))
                if isinstance(data.get("distributed", {}), dict)
                else {}
            ),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "commands": list(self.commands),
            "steps": [step.to_dict() for step in self.steps],
            "parallel_groups": [group.to_dict() for group in self.parallel_groups],
            "require_clean_git_before_task": self.require_clean_git_before_task,
            "allow_agent_updates": self.allow_agent_updates,
            "parallel_workers": self.parallel_workers,
            "max_auto_workers": self.max_auto_workers,
            "command_timeout_seconds": self.command_timeout_seconds,
            "adaptive_timeout_enabled": self.adaptive_timeout_enabled,
            "command_idle_timeout_seconds": self.command_idle_timeout_seconds,
            "reported_infrastructure_markers": [
                marker.to_dict() for marker in self.reported_infrastructure_markers
            ],
            "isolation": self.isolation.to_dict(),
            "distributed": self.distributed.to_dict(),
        }


@dataclass
class GitConfig:
    auto_init_repo: bool = True
    commit_message_template: str = "feat({task_id}): {title}"

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GitConfig":
        return cls(
            auto_init_repo=bool(data.get("auto_init_repo", True)),
            commit_message_template=str(
                data.get("commit_message_template", "feat({task_id}): {title}")
            ),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ApprovalConfig:
    enabled: List[str] = field(
        default_factory=lambda: ["requirements", "prototype", "architecture", "release"]
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
class VisualJudgeConfig:
    mode: str = "auto"
    threshold: int = 85
    provider: str = ""
    max_pairs_per_task: int = 6
    require_screenshot_artifacts: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "VisualJudgeConfig":
        return cls(
            mode=str(data.get("mode", "auto")),
            threshold=int(data.get("threshold", 85)),
            provider=str(data.get("provider", "")),
            max_pairs_per_task=int(data.get("max_pairs_per_task", 6)),
            require_screenshot_artifacts=bool(data.get("require_screenshot_artifacts", True)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class FrontendDesignConfig:
    mode: str = "auto"
    catalog_repository: str = "VoltAgent/awesome-design-md"
    catalog_ref: str = "main"
    max_pages: int = 3
    viewports: List[str] = field(default_factory=lambda: ["1440x900", "390x844"])
    network_timeout_seconds: int = 30

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "FrontendDesignConfig":
        return cls(
            mode=str(data.get("mode", "auto")),
            catalog_repository=str(
                data.get("catalog_repository", "VoltAgent/awesome-design-md")
            ),
            catalog_ref=str(data.get("catalog_ref", "main")),
            max_pages=int(data.get("max_pages", 3)),
            viewports=[str(item) for item in data.get("viewports", ["1440x900", "390x844"])],
            network_timeout_seconds=int(data.get("network_timeout_seconds", 30)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ParallelTasksConfig:
    enabled: bool = False
    workers: Union[int, str] = "auto"
    max_auto_workers: int = 4
    adaptive: bool = True
    strict: bool = False
    worktree_root: str = ""
    pressure_cooldown_seconds: int = 3600
    soft_pressure_threshold: int = 2

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ParallelTasksConfig":
        workers: Union[int, str]
        raw_workers = data.get("workers", "auto")
        if isinstance(raw_workers, int):
            workers = raw_workers
        else:
            workers = str(raw_workers)
        return cls(
            enabled=bool(data.get("enabled", False)),
            workers=workers,
            max_auto_workers=int(data.get("max_auto_workers", 4)),
            adaptive=bool(data.get("adaptive", True)),
            strict=bool(data.get("strict", False)),
            worktree_root=str(data.get("worktree_root", "")),
            pressure_cooldown_seconds=int(data.get("pressure_cooldown_seconds", 3600)),
            soft_pressure_threshold=int(data.get("soft_pressure_threshold", 2)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class RecoveryConfig:
    enabled: bool = True
    max_rounds: int = 2
    max_repair_tasks_per_round: int = 6
    max_refs_per_repair_task: int = 8
    max_incidents_per_run: int = 6
    diagnostic_probe_timeout_seconds: int = 300
    managed_runtime_downloads_enabled: bool = True
    max_managed_runtime_candidates: int = 3
    managed_runtime_layout_repairs_enabled: bool = True
    max_managed_repair_attempts_per_incident: int = 6

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RecoveryConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            max_rounds=int(data.get("max_rounds", 2)),
            max_repair_tasks_per_round=int(data.get("max_repair_tasks_per_round", 6)),
            max_refs_per_repair_task=int(data.get("max_refs_per_repair_task", 8)),
            max_incidents_per_run=int(data.get("max_incidents_per_run", 6)),
            diagnostic_probe_timeout_seconds=int(
                data.get("diagnostic_probe_timeout_seconds", 300)
            ),
            managed_runtime_downloads_enabled=bool(
                data.get("managed_runtime_downloads_enabled", True)
            ),
            max_managed_runtime_candidates=max(
                1, int(data.get("max_managed_runtime_candidates", 3))
            ),
            managed_runtime_layout_repairs_enabled=bool(
                data.get("managed_runtime_layout_repairs_enabled", True)
            ),
            max_managed_repair_attempts_per_incident=max(
                1, int(data.get("max_managed_repair_attempts_per_incident", 6))
            ),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class RequirementsAuditConfig:
    pattern_timeout_ms: int = 250
    total_timeout_seconds: int = 300
    cache_enabled: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RequirementsAuditConfig":
        return cls(
            pattern_timeout_ms=int(data.get("pattern_timeout_ms", 250)),
            total_timeout_seconds=int(data.get("total_timeout_seconds", 300)),
            cache_enabled=bool(data.get("cache_enabled", True)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class EvidencePreflightConfig:
    mode: str = "high_risk"

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "EvidencePreflightConfig":
        return cls(mode=str(data.get("mode", "high_risk")))

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class SmartTimeoutConfig:
    enabled: bool = True
    provider_idle_seconds: int = 1800
    tool_idle_seconds: int = 900
    semantic_stall_seconds: int = 3600
    safety_ceiling_seconds: int = 43200
    loop_repeat_limit: int = 3
    same_provider_resume_limit: int = 1

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "SmartTimeoutConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            provider_idle_seconds=int(data.get("provider_idle_seconds", 1800)),
            tool_idle_seconds=int(data.get("tool_idle_seconds", 900)),
            semantic_stall_seconds=int(data.get("semantic_stall_seconds", 3600)),
            safety_ceiling_seconds=int(data.get("safety_ceiling_seconds", 43200)),
            loop_repeat_limit=int(data.get("loop_repeat_limit", 3)),
            same_provider_resume_limit=int(data.get("same_provider_resume_limit", 1)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ProviderFailoverConfig:
    probe_enabled: bool = True
    probe_timeout_seconds: int = 60
    connection_cooldown_seconds: int = 60
    pressure_cooldown_seconds: int = 300
    timeout_cooldown_seconds: int = 1800
    quota_cooldown_seconds: int = 3600
    max_cooldown_seconds: int = 14400

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ProviderFailoverConfig":
        return cls(
            probe_enabled=bool(data.get("probe_enabled", True)),
            probe_timeout_seconds=int(data.get("probe_timeout_seconds", 60)),
            connection_cooldown_seconds=int(
                data.get("connection_cooldown_seconds", 60)
            ),
            pressure_cooldown_seconds=int(
                data.get("pressure_cooldown_seconds", 300)
            ),
            timeout_cooldown_seconds=int(
                data.get("timeout_cooldown_seconds", 1800)
            ),
            quota_cooldown_seconds=int(data.get("quota_cooldown_seconds", 3600)),
            max_cooldown_seconds=int(data.get("max_cooldown_seconds", 14400)),
        )

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ExecutionConfig:
    parallel_tasks: ParallelTasksConfig = field(default_factory=ParallelTasksConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    requirements_audit: RequirementsAuditConfig = field(default_factory=RequirementsAuditConfig)
    evidence_preflight: EvidencePreflightConfig = field(default_factory=EvidencePreflightConfig)
    smart_timeout: SmartTimeoutConfig = field(default_factory=SmartTimeoutConfig)
    provider_failover: ProviderFailoverConfig = field(
        default_factory=ProviderFailoverConfig
    )

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ExecutionConfig":
        return cls(
            parallel_tasks=ParallelTasksConfig.from_dict(dict(data.get("parallel_tasks", {}))),
            recovery=RecoveryConfig.from_dict(dict(data.get("recovery", {}))),
            requirements_audit=RequirementsAuditConfig.from_dict(
                dict(data.get("requirements_audit", {}))
            ),
            evidence_preflight=EvidencePreflightConfig.from_dict(
                dict(data.get("evidence_preflight", {}))
            ),
            smart_timeout=SmartTimeoutConfig.from_dict(
                dict(data.get("smart_timeout", {}))
            ),
            provider_failover=ProviderFailoverConfig.from_dict(
                dict(data.get("provider_failover", {}))
            ),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "parallel_tasks": self.parallel_tasks.to_dict(),
            "recovery": self.recovery.to_dict(),
            "requirements_audit": self.requirements_audit.to_dict(),
            "evidence_preflight": self.evidence_preflight.to_dict(),
            "smart_timeout": self.smart_timeout.to_dict(),
            "provider_failover": self.provider_failover.to_dict(),
        }


@dataclass
class ProjectConfig:
    project_name: str
    providers: Dict[str, ProviderConfig] = field(
        default_factory=lambda: {
            "codex": ProviderConfig(
                kind="codex",
                binary="codex",
                profile_map={"balanced": "balanced", "deep": "deep", "max": "max"},
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
                vision="auto",
            ),
            "antigravity-claude": ProviderConfig(
                kind="antigravity",
                binary="agy",
                profile_map={
                    "balanced": "Claude Sonnet 4.6 (Thinking)",
                    "deep": "Claude Opus 4.6 (Thinking)",
                    "max": "Claude Opus 4.6 (Thinking)",
                },
                extra_args=[],
                cwd_flag="",
                prompt_via_stdin=False,
                output_flag="",
                timeout_seconds=7200,
                idle_timeout_seconds=7200,
                vision="disabled",
            ),
            "antigravity-gemini": ProviderConfig(
                kind="antigravity",
                binary="agy",
                profile_map={
                    "balanced": "Gemini 3.5 Flash (Low)",
                    "deep": "Gemini 3.5 Flash (Medium)",
                    "max": "Gemini 3.5 Flash (High)",
                },
                extra_args=[],
                cwd_flag="",
                prompt_via_stdin=False,
                output_flag="",
                timeout_seconds=7200,
                idle_timeout_seconds=7200,
                vision="disabled",
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
    visual_judge: VisualJudgeConfig = field(default_factory=VisualJudgeConfig)
    frontend_design: FrontendDesignConfig = field(default_factory=FrontendDesignConfig)
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
            visual_judge=VisualJudgeConfig.from_dict(dict(data.get("visual_judge", {}))),
            frontend_design=FrontendDesignConfig.from_dict(
                dict(data.get("frontend_design", {}))
            ),
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
            "visual_judge": self.visual_judge.to_dict(),
            "frontend_design": self.frontend_design.to_dict(),
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
    workflow_version: int = 1
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
    recovery_loop_events: List[Dict[str, object]] = field(default_factory=list)
    last_recovery_route: Dict[str, object] = field(default_factory=dict)
    active_execution_incident_id: str = ""
    execution_incidents: List[Dict[str, object]] = field(default_factory=list)
    execution_incident_budget_epoch: int = 0
    execution_incident_budget_checkpoint: Dict[str, object] = field(
        default_factory=dict
    )
    active_blocker: Dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "RunState":
        return cls(
            run_id=str(data["run_id"]),
            workflow_version=int(data.get("workflow_version", 1)),
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
            recovery_loop_events=[
                entry for entry in (data.get("recovery_loop_events", []) or [])
                if isinstance(entry, dict)
            ],
            last_recovery_route=(
                dict(data.get("last_recovery_route", {}))
                if isinstance(data.get("last_recovery_route", {}), dict)
                else {}
            ),
            active_execution_incident_id=str(
                data.get("active_execution_incident_id", "")
            ),
            execution_incidents=[
                entry for entry in (data.get("execution_incidents", []) or [])
                if isinstance(entry, dict)
            ],
            execution_incident_budget_epoch=max(
                0, int(data.get("execution_incident_budget_epoch", 0) or 0)
            ),
            execution_incident_budget_checkpoint=(
                dict(data.get("execution_incident_budget_checkpoint", {}))
                if isinstance(
                    data.get("execution_incident_budget_checkpoint", {}), dict
                )
                else {}
            ),
            active_blocker=(
                dict(data.get("active_blocker", {}))
                if isinstance(data.get("active_blocker", {}), dict)
                else {}
            ),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "run_id": self.run_id,
            "workflow_version": self.workflow_version,
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
            "recovery_loop_events": list(self.recovery_loop_events),
            "last_recovery_route": dict(self.last_recovery_route),
            "active_execution_incident_id": self.active_execution_incident_id,
            "execution_incidents": list(self.execution_incidents),
            "execution_incident_budget_epoch": self.execution_incident_budget_epoch,
            "execution_incident_budget_checkpoint": dict(
                self.execution_incident_budget_checkpoint
            ),
            "active_blocker": dict(self.active_blocker),
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
    attachments: List[Path] = field(default_factory=list)
    attempt_id: str = ""
    progress_report_path: Optional[Path] = None
    resume_session_id: str = ""


@dataclass
class AgentProgressEvent:
    kind: str
    source: str = "provider"
    session_id: str = ""
    tool_id: str = ""
    fingerprint: str = ""
    detail: str = ""
    semantic: bool = False


@dataclass
class AgentTermination:
    reason: str
    elapsed_seconds: float = 0.0
    last_provider_activity_seconds: float = 0.0
    last_semantic_progress_seconds: float = 0.0
    active_tool: str = ""
    repeat_count: int = 0
    report_path: str = ""


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
    provider_session_id: str = ""
    termination: Optional[AgentTermination] = None
    supervision_report_path: str = ""


@dataclass
class CommandResult:
    command: str
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    comparable_failures: bool = False
    duration_seconds: float = 0.0
    termination_reason: str = ""
    timeout_seconds: float = 0.0
    cleanup_incomplete: bool = False
    last_activity_seconds: float = 0.0
    activity_kind: str = ""
    process_snapshot: Dict[str, object] = field(default_factory=dict)
    job_id: str = ""
    worker_id: str = ""
    backend: str = "local"
    infrastructure_error: bool = False
    infrastructure_failure_id: str = ""
    infrastructure_capability: str = ""
    infrastructure_contract: str = ""
    infrastructure_attempts: List[Dict[str, object]] = field(default_factory=list)
    mutation_paths: List[str] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)


@dataclass
class GateResult:
    ok: bool
    commands: List[CommandResult]
    summary: str = ""
    comparable_failures: bool = True
