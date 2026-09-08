from __future__ import annotations

import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_runtime import (
    INCOMPATIBLE_RUNTIME, RUNTIME_CAPABILITIES, RuntimeCompatibilityError,
    require_runtime, verify_runtime,
)


REPOSITORY = Path(__file__).resolve().parents[1]


def declare(runtime, capabilities=None):
    package = runtime / "src/auto_agents"
    package.mkdir(parents=True, exist_ok=True)
    (package / "repair_runtime.py").write_text("RUNTIME_CAPABILITIES = " + repr(
        RUNTIME_CAPABILITIES if capabilities is None else capabilities))
    return package


@pytest.mark.parametrize("manifest", [None, "invalid syntax !", "RUNTIME_CAPABILITIES = {}",
                                       "RUNTIME_CAPABILITIES = {'progress_supervision': True}",
                                       "RUNTIME_CAPABILITIES = {'progress_supervision': {1}}"])
def test_incompatible_declaration_stops_before_any_process(tmp_path, manifest):
    if manifest is not None:
        package = declare(tmp_path)
        (package / "repair_runtime.py").write_text(manifest)
    with patch("auto_agents.repair_runtime.subprocess.run", side_effect=AssertionError("unexpected process")):
        with pytest.raises(RuntimeCompatibilityError) as stopped:
            verify_runtime(tmp_path, sys.executable)
    result = stopped.value.to_result()
    json.dumps(result)  # Invalid declarations must still produce durable JSON.
    assert result["category"] == "runtime_incompatible"
    assert result["runtime_compatibility"]["runtime"] == str(tmp_path)
    assert result["runtime_compatibility"]["required_capabilities"] == RUNTIME_CAPABILITIES


def test_runtime_preflight_happens_before_environment_installation(tmp_path):
    from auto_agents.repair_worker import engine_environment
    package = tmp_path / "src/auto_agents"
    package.mkdir(parents=True)
    (package / "repair_control.py").write_text("VERSION = 1")
    (package / "repair_client.py").touch()
    with patch("auto_agents.repair_worker.subprocess.run", side_effect=AssertionError("unexpected installation")):
        with pytest.raises(RuntimeCompatibilityError):
            engine_environment({"root": str(tmp_path / "control")}, tmp_path)
    assert not (tmp_path / "control").exists()


def test_current_runtime_passes_controller_owned_behavior_checks():
    receipt = verify_runtime(REPOSITORY, sys.executable)
    assert receipt["checks"] == dict.fromkeys(RUNTIME_CAPABILITIES, True)
    assert len(receipt["probe_sha256"]) == 64


@pytest.mark.parametrize("regression", ["progress", "planning", "terminal", "candidate_probe"])
def test_candidate_declarations_cannot_replace_trusted_behavior_checks(tmp_path, regression):
    shutil.copytree(REPOSITORY / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    package = tmp_path / "src/auto_agents"
    if regression == "progress":
        with (package / "supervision.py").open("a") as output:
            output.write("\nProgressSupervisor.poll = lambda self: 'timed_out'\n")
    elif regression == "planning":
        path = package / "repair_contract.py"
        path.write_text(path.read_text().replace("AgentRequest(", "_legacy_request(") + """
def _legacy_request(**kwargs):
    from .models import AgentRequest
    request = AgentRequest(**kwargs)
    request.smart_timeout_enabled = False
    request.timeout_seconds = 180
    return request
""")
    elif regression == "terminal":
        path = package / "repair_client.py"
        source = path.read_text()
        start = source.index("            # Terminal subscribers")
        end = source.index("            time.sleep(1)", start)
        old_order = source[start:end]
        source = source[:start] + source[end:]
        source = source.replace('            status = response["job"]["state"]',
                                old_order + '            status = response["job"]["state"]', 1)
        path.write_text(source)
    else:
        (package / "repair_runtime_probe.py").write_text("raise AssertionError('candidate controls the probe')")
    assert require_runtime(tmp_path) == RUNTIME_CAPABILITIES
    if regression == "candidate_probe":
        assert all(verify_runtime(tmp_path, sys.executable)["checks"].values())
    else:
        with pytest.raises(RuntimeCompatibilityError) as stopped:
            verify_runtime(tmp_path, sys.executable)
        check = {"progress": "progress_supervision", "planning": "acceptance_planning",
                 "terminal": "terminal_repair_status"}[regression]
        assert stopped.value.details["checks"][check] is not True


@pytest.mark.parametrize("result", [subprocess.TimeoutExpired("probe", 30),
    SimpleNamespace(returncode=0, stdout='{"checks":{}}'),
    SimpleNamespace(returncode=0, stdout="not json")])
def test_incomplete_probe_is_not_compatibility_proof(tmp_path, result):
    declare(tmp_path)
    options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
    with patch("auto_agents.repair_runtime.subprocess.run", **options):
        with pytest.raises(RuntimeCompatibilityError):
            verify_runtime(tmp_path, sys.executable)


@pytest.mark.parametrize("operation", ["repair", "validate-subscriber", "publish"])
def test_selected_worker_checks_even_cached_or_previously_verified_runtime(tmp_path, operation):
    from auto_agents.repair_worker import execute_selected_worker
    package = tmp_path / "selected/src/auto_agents"
    package.mkdir(parents=True)
    (package / "repair_worker.py").touch()
    request_path = tmp_path / "request.json"
    request = {"operation": operation, "_request_path": str(request_path),
               "prepared_runtime": {"fresh": False, "revision": "old"},
               "job": {"result": {"engine_full_proof": {"ok": True}}},
               "runtime_compatibility": {"checks": dict.fromkeys(RUNTIME_CAPABILITIES, True)}}
    with patch("auto_agents.repair_worker.os.execve", side_effect=AssertionError("old runtime executed")):
        with pytest.raises(RuntimeCompatibilityError):
            execute_selected_worker(request, tmp_path / "selected", sys.executable)
    assert not request_path.exists()


def test_resume_checks_compatibility_before_live_workflow_access(tmp_path, monkeypatch):
    from auto_agents import repair_launch
    request = {"subscriber": {}, "result": {"runtime": str(tmp_path / "old"), "commit": "approved"}}
    path = tmp_path / "resume.json"
    path.write_text(json.dumps(request))
    monkeypatch.setattr(sys, "argv", ["repair_launch.py", str(path)])
    with patch.object(repair_launch.subprocess, "run", return_value=SimpleNamespace(stdout="approved", returncode=0)), \
         patch("auto_agents.orchestrator.Orchestrator", side_effect=AssertionError("live workflow accessed")):
        with pytest.raises(RuntimeCompatibilityError):
            repair_launch.main()


def test_worker_persists_structured_incompatibility(tmp_path, monkeypatch):
    from auto_agents import repair_worker
    path = tmp_path / "repair-g1-request.json"
    request = {"config": {"root": str(tmp_path), "implementation_revision": "controller"},
               "job": {"id": "job", "generation": 1}, "operation": "repair"}
    path.write_text(json.dumps(request))
    monkeypatch.setattr(sys, "argv", ["repair_worker.py", str(path)])
    failure = RuntimeCompatibilityError(tmp_path / "old", "missing capability declaration", phase="environment")
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with patch.object(repair_worker, "repair", side_effect=failure):
            repair_worker.main()
    finally:
        signal.signal(signal.SIGTERM, previous)
        from auto_agents.process_supervision import ACTIVE_PROCESSES
        ACTIVE_PROCESSES.clear_configuration(remove_if_empty=True)
    result = json.loads((tmp_path / "repair-g1-result.json").read_text())
    assert result["category"] == "runtime_incompatible" and result["generation"] == 1
    assert result["runtime_compatibility"]["controller_revision"] == "controller"
    from auto_agents.repair_client import _repair_failure_detail
    assert "同步引擎版本" in _repair_failure_detail({"id": "job", "result": result}, {})
    assert result["error"] == INCOMPATIBLE_RUNTIME
