from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Mapping


AUTHORIZATION_POLICY_SCHEMA_VERSION = 1

AUTO_APPROVE_ACTIONS = (
    "target_code_change",
    "engine_self_repair",
    "isolated_checkout",
    "safe_state_upgrade",
    "test_execution",
    "local_git_commit",
    "workflow_resume",
    "completion_confirmation",
)

HUMAN_ONLY_ACTIONS = (
    "goal_choice",
    "credential",
    "rights_attestation",
    "unbudgeted_external_cost",
    "destructive_change",
    "irreversible_product_decision",
    "external_observation",
)

INTERNAL_ROUTING_ACTIONS = (
    "repository_selection",
    "implementation_scope",
    "test_strategy",
    "safe_migration_strategy",
    "workflow_routing",
)


@dataclass(frozen=True)
class WorkflowAuthorizationPolicy:
    schema_version: int = AUTHORIZATION_POLICY_SCHEMA_VERSION
    mode: str = "interactive"
    auto_actions: tuple[str, ...] = field(default_factory=tuple)
    human_only_actions: tuple[str, ...] = HUMAN_ONLY_ACTIONS
    source: str = "default"

    @classmethod
    def for_invocation(
        cls,
        *,
        auto_approve: bool,
        existing: Mapping[str, object] | None = None,
    ) -> "WorkflowAuthorizationPolicy":
        if existing:
            loaded = cls.from_dict(existing)
            if not auto_approve or loaded.mode == "auto":
                return loaded
        return cls(
            mode="auto" if auto_approve else "interactive",
            auto_actions=AUTO_APPROVE_ACTIONS if auto_approve else (),
            human_only_actions=HUMAN_ONLY_ACTIONS,
            source="cli:auto-approve" if auto_approve else "default",
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "WorkflowAuthorizationPolicy":
        mode = str(payload.get("mode", "interactive")).strip()
        if mode not in {"interactive", "auto"}:
            mode = "interactive"
        raw_auto = payload.get("auto_actions", ())
        raw_human = payload.get("human_only_actions", HUMAN_ONLY_ACTIONS)
        auto_actions = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (raw_auto if isinstance(raw_auto, (list, tuple)) else ())
                if str(item).strip()
            )
        )
        human_only = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (
                    raw_human
                    if isinstance(raw_human, (list, tuple))
                    else HUMAN_ONLY_ACTIONS
                )
                if str(item).strip()
            )
        )
        return cls(
            schema_version=AUTHORIZATION_POLICY_SCHEMA_VERSION,
            mode=mode,
            auto_actions=auto_actions,
            human_only_actions=human_only or HUMAN_ONLY_ACTIONS,
            source=str(payload.get("source", "persisted")).strip() or "persisted",
        )

    def decide(self, action: str) -> str:
        normalized = str(action).strip()
        if normalized in self.human_only_actions:
            return "WAIT_USER"
        if normalized in INTERNAL_ROUTING_ACTIONS:
            return "AUTO_EXECUTE"
        if normalized in self.auto_actions:
            return "AUTO_EXECUTE"
        if normalized in AUTO_APPROVE_ACTIONS:
            return "WAIT_USER"
        return "DENY"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["auto_actions"] = list(self.auto_actions)
        payload["human_only_actions"] = list(self.human_only_actions)
        return payload


def authorization_policy_for_state(
    *,
    auto_approve: bool,
    payload: Mapping[str, object] | None = None,
) -> WorkflowAuthorizationPolicy:
    return WorkflowAuthorizationPolicy.for_invocation(
        auto_approve=auto_approve,
        existing=payload,
    )


_HUMAN_DECISION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("credential", r"credential|secret|api[-_ ]?key|token|password|凭据|密钥|令牌|密码"),
    ("unbudgeted_external_cost", r"cost|budget|payment|charge|paid|费用|预算|付费|收费"),
    ("rights_attestation", r"copyright|ownership|rights? to use|授权素材|版权|所有权"),
    ("destructive_change", r"delete|drop|erase|reset|destructive|删除|清空|重置|破坏性"),
    ("goal_choice", r"real or simulated|真实.*模拟|模拟.*真实|最终希望|目标是"),
    (
        "external_observation",
        r"open .*browser|run .*browser|inspect .*browser|browser .*check|"
        r"check .*player|manual check|"
        r"打开.*浏览器|浏览器.*检查|人工.*检查|请.*查看",
    ),
)

_INTERNAL_ACTION_PATTERN = re.compile(
    r"auto[-_ ]?agents|repository|repo\b|module|backward.compat|migration|"
    r"lifecycle|handoff|blocked[-_ ]?run|workflow|test strategy|commit|"
    r"仓库|模块|向后兼容|迁移方案|生命周期|工作流|测试策略|提交|恢复.*run",
    re.IGNORECASE,
)


def classify_assistance_request(
    text: str,
    *,
    declared_class: str = "",
) -> str:
    normalized_class = str(declared_class).strip()
    if normalized_class in HUMAN_ONLY_ACTIONS or normalized_class in (
        *AUTO_APPROVE_ACTIONS,
        *INTERNAL_ROUTING_ACTIONS,
    ):
        return normalized_class
    normalized = " ".join(str(text).split())
    for decision_class, pattern in _HUMAN_DECISION_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return decision_class
    if _INTERNAL_ACTION_PATTERN.search(normalized):
        return "implementation_scope"
    return "unknown"
