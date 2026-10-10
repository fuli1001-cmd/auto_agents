"""Controller-owned policies and the closed catalog of business stages."""

from dataclasses import dataclass, asdict
import re

from .types import ControlError

FEATURES = (
    "contract_policy_v1",
    "validated_outcome_v1",
    "explicit_retry_v1",
    "prototype_scope_v1",
)

# These corrections have their own finite allowance. Their admitted retry must
# not also spend a watchdog repetition before the corrected result is known.
BOUNDED_RETRIES = {"prototype_repairs": 2, "protocol_retries": 2}


@dataclass(frozen=True)
class WorkflowPolicy:
    kind: str = "standard"
    allow_source_edits: bool = True
    version: int = 1

    def __post_init__(self):
        if (
            self.kind
            not in {"standard", "existing_acceptance", "acceptance_blocker_fix"}
            or type(self.allow_source_edits) is not bool
            or self.version != 1
        ):
            raise ControlError("integrity", "Invalid workflow policy")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def derive(cls, contract, parent=None):
        from ..collab_goal_scope import acceptance_only

        forbidden = re.search(
            r"(?:不(?:得|允许)?(?:修改|改动)|禁止(?:修改|改动)).{0,12}(?:源码|源代码|产品代码)|"
            r"(?:do not|must not|no) (?:modify|change|edit|changes? to) (?:the )?(?:product |source )?code",
            contract.goal,
            re.I,
        )
        allowed = not forbidden and (parent is None or parent.allow_source_edits)
        if parent and parent.kind != "standard":
            kind = "acceptance_blocker_fix" if contract.mode == "fix" else parent.kind
        else:
            kind = (
                "existing_acceptance"
                if contract.mode == "collab" and acceptance_only(contract.goal)
                else "standard"
            )
        return cls(kind, bool(allowed))

    def route(self, action, *, child_mode=None):
        if not self.allow_source_edits and (action in {"fix", "run", "run_iteration"}):
            raise ControlError(
                "scope_policy",
                "The original goal forbids source changes",
                category="model",
            )
        if self.kind != "standard" and (
            action in {"run", "run_iteration"}
            or action == "resume"
            and child_mode == "run"
        ):
            raise ControlError(
                "scope_policy",
                "Existing-behavior acceptance cannot adopt development work",
                category="model",
            )


@dataclass(frozen=True)
class StageSpec:
    modes: frozenset
    successors: frozenset
    writes: tuple = ()
    verification_workspace: str = "candidate"


ALL = frozenset({"run", "fix", "collab", "provider_resolve", "upgrade"})
PRODUCT = frozenset({"run", "fix"})
STAGES = {
    "clarify": StageSpec(
        frozenset({"run"}),
        frozenset({"prototype", "prototype_prepare"}),
        (
            ".auto-agents/docs/project_brief.md",
            ".auto-agents/docs/requirements.md",
            ".auto-agents/docs/requirements_trace.json",
            ".auto-agents/project-rules.md",
        ),
    ),
    "prototype": StageSpec(
        frozenset({"run"}),
        frozenset({"design", "prototype_prepare", "prototype_verify"}),
        (
            ".auto-agents/docs/frontend_prototype/",
            ".auto-agents/docs/frontend_design/",
            "DESIGN.md",
        ),
    ),
    "prototype_prepare": StageSpec(
        frozenset({"run"}),
        frozenset({"prototype_scope_review", "design"}),
        (".auto-agents/docs/prototype_preparation/",),
    ),
    "prototype_scope_review": StageSpec(
        frozenset({"run"}), frozenset({"prototype", "prototype_prepare"})
    ),
    "prototype_verify": StageSpec(
        frozenset({"run"}),
        frozenset({"prototype_review", "prototype", "prototype_prepare"}),
    ),
    "prototype_review": StageSpec(
        frozenset({"run"}), frozenset({"design", "prototype", "prototype_prepare"})
    ),
    "design": StageSpec(
        frozenset({"run"}),
        frozenset({"plan"}),
        (".auto-agents/docs/architecture.md", "DESIGN.md"),
    ),
    "plan": StageSpec(
        frozenset({"run"}),
        frozenset({"provider_research"}),
        (".auto-agents/docs/task_plan.json",),
    ),
    "provider_research": StageSpec(
        frozenset({"run"}),
        frozenset({"implement"}),
        (
            ".auto-agents/docs/provider_references.md",
            ".auto-agents/docs/provider_references/",
            ".auto-agents/docs/provider_references.lock.json",
        ),
    ),
    "research": StageSpec(
        frozenset({"provider_resolve"}),
        frozenset({"verify", "review"}),
        (
            ".auto-agents/docs/provider_references.md",
            ".auto-agents/docs/provider_references/",
            ".auto-agents/docs/provider_references.lock.json",
        ),
    ),
    "classify": StageSpec(
        frozenset({"fix"}), frozenset({"implement", "verify", "review", "acceptance"})
    ),
    "implement": StageSpec(PRODUCT, frozenset({"persistence", "verify", "readme"})),
    "persistence": StageSpec(PRODUCT, frozenset({"verify"})),
    "readme": StageSpec(frozenset({"run"}), frozenset({"verify"}), ("README.md",)),
    "verify": StageSpec(
        ALL, frozenset({"review", "visual_judge", "artifact_verify", "implement"})
    ),
    "review": StageSpec(
        ALL, frozenset({"implement", "research", "finalize", "artifact_verify"})
    ),
    "visual_judge": StageSpec(
        frozenset({"run"}), frozenset({"implement", "finalize", "artifact_verify"})
    ),
    "finalize": StageSpec(ALL, frozenset({"review", "artifact_verify"})),
    "artifact_verify": StageSpec(ALL, frozenset({"finalize", "deliver"})),
    "diagnose": StageSpec(frozenset({"collab"}), frozenset({"acceptance"})),
    "acceptance": StageSpec(
        frozenset({"collab", "fix"}),
        frozenset({"diagnose", "acceptance_review"}),
        (".auto-agents/docs/acceptance/",),
        "owned_runtime",
    ),
    "acceptance_review": StageSpec(
        frozenset({"collab", "fix"}), frozenset({"acceptance", "deliver"})
    ),
    "deliver": StageSpec(ALL, frozenset()),
}


def stage(phase):
    try:
        return STAGES[phase]
    except KeyError:
        raise ControlError(
            "stage_policy",
            "Unsupported business phase: " + str(phase),
            category="migration",
        ) from None


def validate_work(mode, phase, status, context, *, previous=None):
    # Unsupported imported records remain inspectable and cancellable.
    if status == "CANCELLED" or status == "BLOCKED" and phase not in STAGES:
        return
    spec = stage(phase)
    if mode not in spec.modes:
        raise ControlError(
            "stage_policy",
            "Phase does not belong to business mode",
            details={"mode": mode, "phase": phase},
        )
    if (
        status != "BLOCKED"
        and previous
        and previous != phase
        and phase not in stage(previous).successors
    ):
        raise ControlError(
            "stage_policy",
            "Illegal phase transition",
            details={"from": previous, "to": phase},
        )
    if status == "WAITING":
        kind = context.get("waiting_for")
        required = {
            "user": "input_requests",
            "approval": "approval",
            "child": "child",
            "children": "children",
        }
        if (
            kind not in {*required, "task_limit"}
            or kind in required
            and not context.get(required[kind])
        ):
            raise ControlError(
                "stage_policy", "Waiting work needs its owned reason and binding"
            )


def next_action(work, pending=False):
    if (
        pending
        or work.get("failure", {}).get("category") == "reconciliation"
        or work.get("failure", {}).get("details", {}).get("recovery")
        == "legacy_unavailable"
    ):
        return "reconcile"
    if work["status"] in {"COMPLETED", "CANCELLED"}:
        return work["status"].lower()
    if work["context"].get("waiting_for") in {"user", "approval"}:
        return "answer" if work["context"]["waiting_for"] == "user" else "approve"
    return "diagnose" if work["context"].get("acceptance_blocker") else "continue"


def validate_dispatch(work, contract, policy):
    validate_work(work["mode"], work["phase"], work["status"], work["context"])
    if (
        policy.kind != "standard"
        and work["mode"] == "run"
        and not contract.inputs.get("variant_only")
    ):
        raise ControlError(
            "scope_policy",
            "The original acceptance goal cannot execute a retained development run",
            category="model",
        )
    if work["mode"] == "fix" and work["phase"] == "implement":
        if not policy.allow_source_edits:
            raise ControlError(
                "scope_policy",
                "The original goal forbids source changes",
                category="model",
            )
        if not contract.scope or not any(
            c.purpose == "behavior" and c.required for c in contract.checks
        ):
            raise ControlError(
                "stage_policy",
                "Implementation needs bound source scope and behavioral checks",
            )
    if (
        work["mode"] == "run"
        and work["phase"] == "implement"
        and not (work["context"].get("tasks") and contract.checks)
    ):
        raise ControlError(
            "stage_policy", "Run implementation needs its bound plan and release checks"
        )
    if work["phase"] == "acceptance_review" and not (
        work["context"].get("acceptance") and work["context"].get("verification")
    ):
        raise ControlError(
            "stage_policy",
            "Acceptance review needs the observed result and verification",
        )
