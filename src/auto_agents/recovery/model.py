"""Versioned wire contracts. Missing authority is an error, never a default."""
from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Tuple

SCHEMA = 1
PROTOCOL = 2
KINDS = frozenset({'collab', 'fix', 'run', 'provider_resolve', 'engine_repair', 'upgrade'})
PHASES = frozenset({'prepare', 'clarify', 'requirements', 'architecture', 'plan', 'research',
    'route', 'implement', 'verify', 'review', 'deliver', 'acceptance', 'environment',
    'protocol', 'adopt', 'publish', 'reconcile', 'diagnose'})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class KernelError(RuntimeError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def require(condition, code, message, **details):
    if not condition:
        raise KernelError(code, message, **details)


def identifier(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', value)),
            'identity', 'Invalid recovery identity')
    return value


def checksum(value):
    require(isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value)),
            'digest', 'A SHA-256 content identity is required')
    return value


@dataclass(frozen=True)
class Contract:
    goal_id: str
    task_id: str
    kind: str
    goal_ref: str
    issue_ref: str
    constraints: Tuple[str, ...]
    required_checks: Tuple[str, ...]
    source_scope: Tuple[str, ...]
    authorization_ref: str
    completion: str
    phases: Tuple[str, ...]
    plan_tasks: Tuple[str, ...] = ()
    parent_task: str = ''
    schema: int = SCHEMA

    def __post_init__(self):
        for value in (self.goal_id, self.task_id): identifier(value)
        for value in (self.goal_ref, self.issue_ref, self.authorization_ref): checksum(value)
        require(self.schema == SCHEMA and self.kind in KINDS, 'contract', 'Unsupported task contract')
        require(self.completion in {'phase_completed', 'preflight_recovered', 'candidate_delivered', 'goal_accepted', 'version_adopted'},
                'contract', 'Completion must identify the proof boundary')
        require(bool(self.phases) and all(p in PHASES for p in self.phases)
                and len(set(self.phases)) == len(self.phases), 'contract', 'Invalid stage sequence')
        require(bool(self.required_checks) and bool(self.source_scope), 'contract',
                'Task contract requires checks and source scope')
        for values in (self.constraints, self.required_checks, self.source_scope, self.plan_tasks):
            require(all(isinstance(v, str) and bool(v.strip()) for v in values), 'contract', 'Invalid contract item')

    @property
    def identity(self): return digest(asdict(self))

    def to_dict(self): return asdict(self)

    @classmethod
    def read(cls, value):
        required = set(cls.__dataclass_fields__) - {'schema', 'plan_tasks', 'parent_task'}
        require(isinstance(value, Mapping) and required <= value.keys()
                and set(value) <= set(cls.__dataclass_fields__), 'contract', 'Incomplete task contract')
        return cls(**{**value, **{key: tuple(value.get(key, ())) for key in (
            'constraints', 'required_checks', 'source_scope', 'phases', 'plan_tasks')}})


@dataclass(frozen=True)
class Evidence:
    blob: str
    task_id: str
    source: str
    contract: str
    environment: str
    verifier: str
    phase: str
    predicate: str
    schema: int = SCHEMA

    def __post_init__(self):
        for value in (self.blob, self.source, self.contract, self.environment, self.verifier): checksum(value)
        identifier(self.task_id)
        require(self.schema == SCHEMA and self.phase in PHASES and bool(self.predicate),
                'evidence', 'Incomplete phase proof')

    def to_dict(self): return asdict(self)


class OutcomeKind(str, Enum):
    SUCCESS = 'success'
    CANDIDATE_REJECTED = 'candidate_rejected'
    ENVIRONMENT_BLOCKED = 'environment_blocked'
    PROTOCOL_INVALID = 'protocol_invalid'
    ENGINE_DEFECT = 'engine_defect'
    OWNERSHIP_CONFLICT = 'ownership_conflict'
    EVIDENCE_INVALID = 'evidence_invalid'
    OUTCOME_UNKNOWN = 'outcome_unknown'
    CANCELLED = 'cancelled'
    NEED_INPUT = 'need_input'


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    reason: str
    evidence: Tuple[Evidence, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    schema: int = SCHEMA

    def __post_init__(self):
        require(isinstance(self.kind, OutcomeKind) and self.schema == SCHEMA, 'outcome', 'Unknown outcome type')
        require(self.kind != OutcomeKind.SUCCESS or bool(self.evidence), 'evidence', 'Success requires a phase proof')
        require(self.kind != OutcomeKind.ENGINE_DEFECT or bool(self.details.get('counterexample')),
                'engine_defect', 'An engine repair needs a concrete counterexample')

    def to_dict(self): return {**asdict(self), 'kind': self.kind.value}

    @classmethod
    def read(cls, value):
        require(isinstance(value, Mapping) and {'kind', 'reason'} <= value.keys()
                and set(value) <= set(cls.__dataclass_fields__), 'outcome', 'Invalid outcome envelope')
        try:
            return cls(**{**value, 'kind': OutcomeKind(value['kind']),
                          'evidence': tuple(Evidence(**row) for row in value.get('evidence', ()))})
        except (TypeError, ValueError) as error:
            raise KernelError('outcome', 'Invalid outcome fields') from error


@dataclass(frozen=True)
class Command:
    command_id: str
    workflow_id: str
    task_id: str
    phase: str
    source: str
    contract: str
    environment: str
    runtime: str
    operation_key: str
    model_call: bool = False
    schema: int = SCHEMA

    def __post_init__(self):
        for value in (self.command_id, self.workflow_id, self.task_id): identifier(value)
        for value in (self.source, self.contract, self.environment, self.runtime): checksum(value)
        require(self.schema == SCHEMA and self.phase in PHASES and bool(self.operation_key),
                'command', 'Incomplete execution command')

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    data: Mapping[str, Any]
    schema: int = SCHEMA

    def __post_init__(self):
        identifier(self.event_id)
        require(self.schema == SCHEMA and isinstance(self.data, Mapping), 'event', 'Unsupported event envelope')

    def to_dict(self): return asdict(self)
