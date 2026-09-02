from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from auto_agents.execution_recovery import (
    ExecutionIncident,
    IncidentDiagnosis,
    parse_incident_diagnosis,
)
from auto_agents.infrastructure_repair import (
    InfrastructureRepairResult,
    _chrome_cdp_probe,
    managed_diagnostic_refs,
    repair_workspace_local_conda,
    scoped_verification_repair_instructions,
    verification_repair_guard,
)
from auto_agents.models import DEFAULT_EFFORTS


def test_incident_judge_has_independent_max_effort() -> None:
    assert DEFAULT_EFFORTS["incident_judge"] == "max"
    assert DEFAULT_EFFORTS["incident_judge"] != DEFAULT_EFFORTS["arbiter"]


def test_self_repair_generation_and_review_have_distinct_defaults() -> None:
    assert DEFAULT_EFFORTS["self_repair"] == "deep"
    assert DEFAULT_EFFORTS["self_repair_review"] == "max"


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
    assert payload["schema_version"] == 5
    assert restored.repair_history == [{"action": "health_probe_failed"}]
    assert restored.recovery_policy_version == 5


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


def test_chrome_cdp_probe_avoids_desktop_keyring(tmp_path: Path) -> None:
    class FailedProcess:
        returncode = 1

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return "", "probe stopped"

    with patch(
        "auto_agents.infrastructure_repair.subprocess.Popen",
        return_value=FailedProcess(),
    ) as popen:
        result = _chrome_cdp_probe(tmp_path / "google-chrome")

    assert result["state"] == "unhealthy"
    assert "--password-store=basic" in popen.call_args.args[0]


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


def test_workspace_conda_is_recreated_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "name = \"demo\"\n"
        "version = \"0.1.0\"\n"
        "dependencies = []\n"
        "[project.optional-dependencies]\n"
        "dev = [\"pytest>=8\"]\n",
        encoding="utf-8",
    )
    workbench = tmp_path / "workbench"
    workbench.mkdir()
    (workbench / "package.json").write_text(
        '{"name":"demo","version":"1.0.0"}\n',
        encoding="utf-8",
    )
    (workbench / "package-lock.json").write_text(
        '{"name":"demo","version":"1.0.0","lockfileVersion":3,"packages":{}}\n',
        encoding="utf-8",
    )
    incident = ExecutionIncident(
        incident_id="conda-1",
        run_id="run-1",
        source="gate",
        kind="gate_reported_infrastructure_error",
        stage="implement",
        context="baseline",
        command="conda run -p ./.conda python -m pytest -q",
        stderr_tail=(
            "EnvironmentLocationNotFound: Not a conda environment: "
            f"{tmp_path / '.conda'}"
        ),
    )
    successes = [
        {"ok": True, "returncode": 0},
        {"ok": True, "returncode": 0},
        {"ok": True, "returncode": 0},
        {"ok": True, "returncode": 0, "stdout_tail": "workspace-conda-ready"},
    ]
    responses = iter(successes)

    def run_command(command, **_kwargs):
        if "create" in command:
            (tmp_path / ".conda").mkdir()
        return next(responses)

    with patch(
        "auto_agents.infrastructure_repair.shutil.which",
        return_value="/opt/conda/bin/conda",
    ), patch(
        "auto_agents.infrastructure_repair._run_repair_command",
        side_effect=run_command,
    ) as run:
        result = repair_workspace_local_conda(tmp_path, incident)

    assert result.repaired is True
    assert result.capability == "workspace_conda"
    assert result.action == "recreated_from_pyproject"
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[0][1:3] == ["create", "--yes"]
    assert "python=3.11" in commands[0]
    assert commands[1][-3:] == ["install", "-e", ".[dev]"]
    assert commands[2][-1] == "ci"
    assert "import pytest" in commands[3][-1]
