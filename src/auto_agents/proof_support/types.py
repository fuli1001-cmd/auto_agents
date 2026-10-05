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
    incident_id: str = ''
    incident_revision: int = 1
    contract_revision: str = ''

    def to_dict(self):
        value = asdict(self)
        if not self.incident_id:
            for key in ('incident_id', 'incident_revision', 'contract_revision'):
                value.pop(key)
        return value

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


@dataclass(frozen=True)
class RuntimeArtifact:
    artifact_id: str
    path: str
    commit: str
    source: str
    environments: Dict[str, Any]
    format: str
    version: int = 1


@dataclass(frozen=True)
class RecoveryContext:
    request: str
    invocation: Dict[str, Any]
    boundary: Dict[str, Any]
    scope: Dict[str, Any]
    ledger: str
    version: int = 2
    incident_id: str = ''
    incident_revision: int = 1
    contract_revision: str = ''


@dataclass(frozen=True)
class FailureIncident:
    identity: str
    owner: Dict[str, Any]
    phase: str
    code: str
    check: str
    event: Dict[str, Any]
    candidate: str = ''
    status: str = 'open'
    revision: int = 1
    version: int = 1
    domain: str = 'unknown'


@dataclass(frozen=True)
class EvidenceRef:
    origin: str
    snapshot: str
    path: str
    sha256: str
    pointer: str = ''
    version: int = 1


@dataclass(frozen=True)
class ProofAmendmentReceipt:
    policy: str
    inputs: str
    decision: str
    changes: Dict[str, Any]
    verdict: Dict[str, Any]
    version: int = 1


@dataclass(frozen=True)
class PhaseProof:
    stage: str
    inputs: Dict[str, Any]
    result: Dict[str, Any]
    policy: int = 1


@dataclass(frozen=True)
class RepairFailure:
    domain: str
    code: str
    owner: str
    recover_at: str
    message: str
    evidence: Dict[str, Any]
    version: int = 1


class Cancellation:
    def __init__(self, parent, local): self.parent, self.local = parent, local
    def is_set(self): return self.parent.is_set() or self.local.is_set()
