from auto_agents.authorization import (
    WorkflowAuthorizationPolicy,
    authorization_policy_for_state,
    classify_assistance_request,
)


def test_auto_approve_authorizes_internal_work_but_not_human_decisions() -> None:
    policy = WorkflowAuthorizationPolicy.for_invocation(auto_approve=True)

    assert policy.decide("engine_self_repair") == "AUTO_EXECUTE"
    assert policy.decide("safe_state_upgrade") == "AUTO_EXECUTE"
    assert policy.decide("repository_selection") == "AUTO_EXECUTE"
    assert policy.decide("credential") == "WAIT_USER"
    assert policy.decide("unbudgeted_external_cost") == "WAIT_USER"
    assert policy.decide("destructive_change") == "WAIT_USER"


def test_auto_approve_upgrades_a_persisted_interactive_policy() -> None:
    interactive = WorkflowAuthorizationPolicy.for_invocation(
        auto_approve=False
    ).to_dict()

    upgraded = authorization_policy_for_state(
        auto_approve=True,
        payload=interactive,
    )

    assert upgraded.mode == "auto"
    assert upgraded.source == "cli:auto-approve"
    assert upgraded.decide("workflow_resume") == "AUTO_EXECUTE"


def test_legacy_assistance_is_classified_without_treating_cost_as_internal() -> None:
    assert (
        classify_assistance_request(
            "是否允许在 auto_agents 仓库中采用向后兼容迁移并恢复 workflow？"
        )
        == "implementation_scope"
    )
    assert (
        classify_assistance_request("需要 API 密钥后才能继续")
        == "credential"
    )
    assert (
        classify_assistance_request("这会产生未授权的付费调用，是否继续？")
        == "unbudgeted_external_cost"
    )
    assert (
        classify_assistance_request("Please open the browser and check the player")
        == "external_observation"
    )
