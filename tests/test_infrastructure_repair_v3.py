from __future__ import annotations

from auto_agents.execution_recovery import (
    ExecutionIncident,
    IncidentDiagnosis,
    command_incident,
)
from auto_agents.infrastructure_repair import InfrastructureRepairResult
from auto_agents.gates import classify_reported_infrastructure_failure
from auto_agents.models import CommandResult, RecoveryConfig
from auto_agents.cli import build_parser
from auto_agents.workers import WORKER_PROTOCOL_VERSION, enrich_worker_probe


def _reported_result(label: str = "") -> CommandResult:
    return CommandResult(
        command="npm exec -- vitest run browser-verification.test.ts",
        ok=False,
        returncode=1,
        stderr=(
            "AUTO_AGENTS_INFRA_FAILURE "
            "id=browser_verification_infrastructure_failed SIGTRAP "
            f"{label}"
        ),
        infrastructure_error=True,
        infrastructure_failure_id="browser_verification_infrastructure_failed",
    )


def test_reported_infrastructure_identity_ignores_recovery_context() -> None:
    first = command_incident(
        run_id="run-1",
        stage="implement",
        context="task verification commands (recover-execution-first-r1)",
        result=_reported_result("first"),
        head_ref="head-1",
        worktree_fingerprint="worktree-1",
    )
    second = command_incident(
        run_id="run-1",
        stage="implement",
        context="task verification commands (recover-execution-second-r1)",
        result=_reported_result("second"),
        head_ref="head-2",
        worktree_fingerprint="worktree-2",
    )
    assert first.incident_fingerprint == second.incident_fingerprint
    assert first.root_cause_fingerprint == second.root_cause_fingerprint
    assert first.evidence_fingerprint != second.evidence_fingerprint


def test_incident_v3_preserves_root_identity_and_cause_status() -> None:
    incident = ExecutionIncident(
        incident_id="incident-1",
        run_id="run-1",
        source="gate",
        kind="gate_reported_infrastructure_error",
        stage="implement",
        context="verification",
        root_incident_id="incident-1",
        root_cause_fingerprint="root-1",
        origin_command="npm test",
    )
    restored = ExecutionIncident.from_dict(incident.to_dict())
    assert incident.to_dict()["schema_version"] == 3
    assert restored.root_cause_fingerprint == "root-1"
    diagnosis = IncidentDiagnosis(
        owner="execution_environment",
        action="REPAIR_INFRASTRUCTURE",
        confidence=0.95,
        cause_status="suspected",
        reason="SIGTRAP is observed; exact browser cause is not proven",
    )
    assert diagnosis.valid()


def test_recovery_config_enables_bounded_managed_downloads() -> None:
    config = RecoveryConfig.from_dict({})
    assert config.managed_runtime_downloads_enabled is True
    assert config.max_managed_runtime_candidates == 3


def test_managed_repair_result_carries_probe_and_candidate_evidence() -> None:
    result = InfrastructureRepairResult(
        repaired=True,
        capability="chrome",
        action="selected_cdp_healthy_runtime",
        reason="healthy",
        environment={"AUTO_AGENTS_CAPABILITY_CHROME_PATH": "/managed/chrome"},
        probe={"state": "healthy", "probe_kind": "chrome_cdp_v1"},
        candidate_attempts=[{"path": "/system/chrome", "state": "unhealthy"}],
    )
    payload = result.to_dict()
    assert payload["probe"]["probe_kind"] == "chrome_cdp_v1"
    assert payload["candidate_attempts"]


def test_structured_infrastructure_marker_preserves_capability_contract() -> None:
    result = CommandResult(
        command="npm test",
        ok=False,
        returncode=1,
        stderr=(
            "AUTO_AGENTS_INFRA_FAILURE id=browser_launch_failed "
            "capability=chrome contract=cdp-v1"
        ),
    )
    classify_reported_infrastructure_failure(result)
    assert result.infrastructure_failure_id == "browser_launch_failed"
    assert result.infrastructure_capability == "chrome"
    assert result.infrastructure_contract == "cdp-v1"


def test_run_parser_accepts_explicit_blocked_restart() -> None:
    args = build_parser().parse_args(
        ["run", "--project", "/tmp/demo", "--restart-blocked"]
    )
    assert args.restart_blocked is True


def test_empty_blocked_restart_is_an_explicit_cli_only_transition() -> None:
    args = build_parser().parse_args(
        ["run", "--project", "/tmp/demo", "--restart-blocked"]
    )
    assert args.command == "run"
    assert args.restart_blocked is True


def test_worker_protocol_advertises_managed_capability_repair_v2() -> None:
    probe = enrich_worker_probe(
        {
            "platform": "linux-x86_64",
            "capabilities": ["python"],
            "features": [],
        }
    )
    assert WORKER_PROTOCOL_VERSION == 4
    assert "managed_capability_repair_v2" in probe["features"]
