"""Small, versioned business contracts; models never own execution authority."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
import hashlib
import json

SCHEMA = 2
MODES = frozenset({"run", "fix", "collab", "provider_resolve", "upgrade"})
RUN_PHASES = (
    "clarify",
    "prototype",
    "design",
    "plan",
    "provider_research",
    "implement",
    "visual_judge",
    "verify",
    "readme",
)


class FrozenDict(dict):
    """JSON-compatible recursive value with no writable contract references."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("Business contracts are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = (
        __ior__
    ) = _immutable


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    if isinstance(value, dict):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Status(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ControlError(RuntimeError):
    def __init__(self, code, message, *, category="state", details=None):
        super().__init__(message)
        self.code, self.category, self.details = code, category, dict(details or {})

    def to_dict(self):
        return {
            "code": self.code,
            "category": self.category,
            "message": str(self),
            "details": self.details,
        }


@dataclass(frozen=True)
class VerificationSpec:
    command: str
    targets: tuple = ()
    purpose: str = "behavior"
    required: bool = True
    timeout: int = 600
    outputs: tuple = ()
    cache_scope: str = "candidate"
    result_cache_scope: str = "candidate"

    def __post_init__(self):
        if not isinstance(self.command, str) or not self.command.strip():
            raise ControlError(
                "verification_spec", "A verification command is required"
            )
        if self.purpose not in {"behavior", "environment", "artifact"}:
            raise ControlError("verification_spec", "Unknown verification purpose")
        if type(self.timeout) is not int or self.timeout <= 0:
            raise ControlError("verification_spec", "Invalid verification timeout")
        if type(self.required) is not bool or any(
            not isinstance(x, str) for x in self.targets
        ):
            raise ControlError(
                "verification_spec", "Invalid verification flags or targets"
            )
        if self.cache_scope not in {
            "candidate",
            "run_context",
            "source",
            "off",
        } or self.result_cache_scope not in {
            "candidate",
            "observed_inputs",
            "auto",
            "off",
        }:
            raise ControlError("verification_spec", "Invalid verification cache policy")

    def to_dict(self):
        value = {
            "command": self.command,
            "targets": list(self.targets),
            "purpose": self.purpose,
            "required": self.required,
            "timeout": self.timeout,
        }
        if self.outputs:
            value["outputs"] = list(self.outputs)
        if self.cache_scope != "candidate":
            value["cache_scope"] = self.cache_scope
        if self.result_cache_scope != "candidate":
            value["result_cache_scope"] = self.result_cache_scope
        return value

    @classmethod
    def read(cls, value):
        return cls(
            value["command"],
            tuple(value.get("targets", ())),
            value.get("purpose", "behavior"),
            value.get("required", True),
            value.get("timeout", 600),
            tuple(value.get("outputs", ())),
            value.get("cache_scope", "candidate"),
            value.get("result_cache_scope", "candidate"),
        )


@dataclass(frozen=True)
class Contract:
    workflow_id: str
    work_id: str
    mode: str
    goal: str
    source: str
    authorization: dict = field(default_factory=dict)
    scope: tuple = ()
    checks: tuple = ()
    inputs: dict = field(default_factory=dict)
    parent_contract: str = ""
    schema: int = SCHEMA

    def __post_init__(self):
        if self.schema != SCHEMA or self.mode not in MODES or not self.goal.strip():
            raise ControlError("contract", "Invalid business contract")
        object.__setattr__(self, "authorization", freeze(self.authorization))
        object.__setattr__(self, "inputs", freeze(self.inputs))
        object.__setattr__(self, "scope", tuple(self.scope))
        object.__setattr__(self, "checks", tuple(self.checks))
        canonical(self.to_dict())
        if any(
            not isinstance(x, str)
            or not x
            or x.startswith("/")
            or ".." in Path(x).parts
            for x in self.scope
        ):
            raise ControlError(
                "scope", "Contract paths must remain relative to their workspace"
            )

    def to_dict(self):
        return {
            "schema": self.schema,
            "workflow_id": self.workflow_id,
            "work_id": self.work_id,
            "mode": self.mode,
            "goal": self.goal,
            "source": self.source,
            "authorization": thaw(self.authorization),
            "scope": list(self.scope),
            "checks": [x.to_dict() for x in self.checks],
            "inputs": thaw(self.inputs),
            "parent_contract": self.parent_contract,
        }

    @property
    def identity(self):
        return digest(self.to_dict())

    @classmethod
    def read(cls, value):
        return cls(
            value["workflow_id"],
            value["work_id"],
            value["mode"],
            value["goal"],
            value["source"],
            dict(value.get("authorization", {})),
            tuple(value.get("scope", ())),
            tuple(VerificationSpec.read(x) for x in value.get("checks", ())),
            dict(value.get("inputs", {})),
            value.get("parent_contract", ""),
            value.get("schema", 0),
        )


@dataclass(frozen=True)
class ExecutionContext:
    control_root: Path
    workspace_root: Path
    work_id: str
    contract: Contract
    provider: str
    environment: dict = field(default_factory=dict)
    operation_id: str = ""


@dataclass(frozen=True)
class StageResult:
    action: str
    data: dict = field(default_factory=dict)


TERMINAL = {Status.COMPLETED.value, Status.CANCELLED.value}
TRANSITIONS = {
    "READY": {"RUNNING", "WAITING", "BLOCKED", "CANCELLED"},
    "RUNNING": {"READY", "WAITING", "BLOCKED", "COMPLETED", "CANCELLED"},
    "WAITING": {"READY", "BLOCKED", "CANCELLED"},
    "BLOCKED": {"READY", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}
