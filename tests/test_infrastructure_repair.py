from __future__ import annotations

import json
from pathlib import Path

from auto_agents.execution_recovery import (
    ExecutionIncident,
    IncidentDiagnosis,
    parse_incident_diagnosis,
)
from auto_agents.infrastructure_repair import (
    InfrastructureRepairResult,
    managed_diagnostic_refs,
    scoped_verification_repair_instructions,
    verification_repair_guard,
)
from auto_agents.models import DEFAULT_EFFORTS


def test_incident_judge_has_independent_max_effort() -> None:
    assert DEFAULT_EFFORTS["incident_judge"] == "max"
    assert DEFAULT_EFFORTS["incident_judge"] != DEFAULT_EFFORTS["arbiter"]


def test_new_infrastructure_owner_and_action_are_valid() -> None:
    diagnosis = parse_incident_diagnosis(
        json.dumps(
            {
                "owner": "verification_infrastructure",
                "action": "REPAIR_INFRASTRUCTURE",
                "confidence": 0.94,
                "reason": "browser runtime failed before assertion entry",
                "evidence": ["signal=SIGTRAP"],
            }
        )
    )
    assert diagnosis.valid()
    assert diagnosis.action == "REPAIR_INFRASTRUCTURE"


def test_incident_schema_preserves_repair_history() -> None:
    incident = ExecutionIncident(
        incident_id="infra-1",
        run_id="run-1",
        source="gate",
        kind="gate_reported_infrastructure_error",
        stage="implement",
        context="baseline",
        repair_history=[{"action": "health_probe_failed"}],
    )
    payload = incident.to_dict()
    restored = ExecutionIncident.from_dict(payload)
    assert payload["schema_version"] == 3
    assert restored.repair_history == [{"action": "health_probe_failed"}]
    assert restored.recovery_policy_version == 3


def test_managed_repair_result_is_json_serializable(tmp_path: Path) -> None:
    result = InfrastructureRepairResult(
        repaired=True,
        capability="chrome",
        action="selected_healthy_managed_runtime",
        reason="healthy",
        environment={"AUTO_AGENTS_CRASH_DIR": str(tmp_path)},
        manifest_path=str(tmp_path / "manifest.json"),
        artifact_fingerprint="abc",
    )
    assert json.loads(json.dumps(result.to_dict()))["repaired"] is True


def test_verification_repair_guard_rejects_weakened_tests() -> None:
    diff = """
+test.skip("browser proof", async () => {})
-expect(result.gateStatus).toBe("passed")
"""
    findings = verification_repair_guard(diff)
    assert "new skipped or focused test" in findings
    assert "assertion removed" in findings


def test_verification_repair_guard_accepts_runtime_selection_change() -> None:
    diff = """
-const binary = projectCandidate;
+const binary = process.env.AUTO_AGENTS_CAPABILITY_CHROME_PATH || projectCandidate;
+expect(binary).toBeTruthy();
"""
    assert verification_repair_guard(diff) == []


def test_legacy_incident_defaults_to_old_recovery_policy() -> None:
    restored = ExecutionIncident.from_dict(
        {
            "incident_id": "legacy",
            "run_id": "run",
            "source": "gate",
            "kind": "gate_reported_infrastructure_error",
            "stage": "implement",
            "context": "baseline",
            "status": "needs_human",
        }
    )
    assert restored.recovery_policy_version == 1


def test_scoped_repair_instructions_preserve_gate_strength() -> None:
    instructions = scoped_verification_repair_instructions()
    assert "Do not skip" in instructions
    assert "exact original verification command" in instructions


def test_managed_diagnostic_refs_are_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTO_AGENTS_WORKER_MANAGED_ROOT", str(tmp_path))
    incident = ExecutionIncident(
        incident_id="incident",
        run_id="run",
        source="gate",
        kind="gate_reported_infrastructure_error",
        stage="implement",
        context="baseline",
    )
    crash = tmp_path / "crashes" / "run" / "incident"
    crash.mkdir(parents=True)
    (crash / "browser.dmp").write_bytes(b"dump")
    refs = managed_diagnostic_refs(incident)
    assert refs == [
        {
            "path": str(crash / "browser.dmp"),
            "size": 4,
            "kind": "minidump",
        }
    ]
