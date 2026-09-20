"""Small controller-owned contracts; plans are prose, not authority tokens."""
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class Acceptance:
    identity: str
    description: str
    commands: tuple = ()


@dataclass(frozen=True)
class RepairRequest:
    identity: str
    engine_base: str
    goal: str
    acceptance: tuple
    provider: str
    invocation: Dict[str, Any] = field(default_factory=dict)
    evidence: tuple = ()

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, 'acceptance': tuple(Acceptance(**{
            **row, 'commands': tuple(row.get('commands', ()))}) for row in value['acceptance']),
            'evidence': tuple(value.get('evidence', ()))})


@dataclass
class AgentReply:
    ok: bool
    text: str = ''
    session: str = ''
    error: str = ''
    usage: Dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    missing_session: bool = False
    timed_out: bool = False


@dataclass(frozen=True)
class ValidationUnit:
    identity: str
    command: str
    expected_nodes: tuple = ()
    fresh: bool = False
    profile: str = 'standard'


@dataclass
class ValidationResult:
    ok: bool
    snapshot: str
    checks: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    infrastructure: bool = False


@dataclass
class ReviewResult:
    ok: bool
    snapshot: str
    findings: List[Dict[str, Any]] = field(default_factory=list)
    text: str = ''
    coverage: List[Dict[str, Any]] = field(default_factory=list)
    change_coverage: List[Dict[str, Any]] = field(default_factory=list)


class RepairBlocked(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Cancellation:
    def __init__(self, parent, local): self.parent, self.local = parent, local
    def is_set(self): return self.parent.is_set() or self.local.is_set()
