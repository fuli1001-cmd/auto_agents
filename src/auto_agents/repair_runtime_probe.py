"""Offline behavioral probes, executed from the trusted controller checkout."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


def control_protocol(root):
    from auto_agents.repair_control import VERSION
    assert VERSION == 1, "unsupported repair control protocol"


def progress_supervision(root):
    from auto_agents.adapters import base
    from auto_agents.models import AgentRequest, SmartTimeoutConfig
    from auto_agents.supervision import ProgressSupervisor

    config = SmartTimeoutConfig.from_dict({"enabled": False, "safety_ceiling_seconds": 1})
    assert not {"enabled", "safety_ceiling_seconds"} & config.to_dict().keys(), "legacy elapsed-time policy retained"
    request = AgentRequest(stage="self_repair_contract", effort="deep", prompt="", cwd=root,
                           output_path=root / "output.json", progress_report_path=root / "progress.json")
    # Old request/config flags must not disable the transport's supervisor.
    request.smart_timeout_enabled = False
    request.timeout_seconds = 0.001
    config.enabled = False
    config.safety_ceiling_seconds = 1
    supervisors = []

    def create(**kwargs):
        supervisor = ProgressSupervisor(**kwargs)
        supervisors.append(supervisor)
        return supervisor

    with patch.object(base, "ProgressSupervisor", side_effect=create):
        result = base.run_subprocess_with_optional_streaming(
            [sys.executable, "-c", "pass"], request, dict(os.environ), smart_timeout=config)
    assert result.returncode == 0 and result.termination is None, "legacy provider deadline still applied"
    assert len(supervisors) == 1, "provider transport bypassed progress supervision"
    supervisor = supervisors[0]
    now = time.monotonic()
    supervisor.started_at = now - 86400
    supervisor.last_provider_activity = supervisor.last_semantic_progress = now
    supervisor.last_workspace_poll = supervisor.last_checkpoint = now
    with patch.object(supervisor, "_sample_process_group"), patch("auto_agents.supervision.time.monotonic", return_value=now):
        assert supervisor.poll() is None, "progressing work stopped at an elapsed-time ceiling"
        supervisor.last_semantic_progress = now - supervisor._effective_progress_lease_seconds() - 1
        assert supervisor.poll() == "semantic_stall", "semantic stalls are no longer detected"


def acceptance_planning(root):
    from auto_agents.repair_contract import prepare_contract

    route = {"issue_seed": {"required_behavior": ["preserve progress supervision"]}}
    calls = []

    def plan(request):
        calls.append(request)
        assert getattr(request, "smart_timeout_enabled", True) is not False, "planning disables supervision"
        assert getattr(request, "timeout_seconds", None) in (None, 0), "planning imposes a provider deadline"
        assert request.sandbox_mode == "read-only" and not request.record_execution_incidents
        return SimpleNamespace(ok=True, summary=json.dumps({"checks": [{
            "obligation": "preserve progress supervision", "nodeids": ["tests/test_runtime.py::test_progress"],
            "reason": "assert progress supervision remains active",
        }]}))

    orchestrator = SimpleNamespace(config=SimpleNamespace(efforts={}), _call_with_failover=plan)
    with patch("auto_agents.orchestrator.Orchestrator", return_value=orchestrator):
        payload = {"invocation": {"engine_route": route}}
        first = prepare_contract(payload, "probe", root, root, root)
        second = prepare_contract(payload, "probe", root, root, root)
    assert len(calls) == 1 and first.to_dict() == second.to_dict(), "planning cache is not preserved"


def terminal_repair_status(root):
    from auto_agents import repair_client
    from auto_agents.self_repair import SelfRepairDecision

    attached = {"subscriber": "probe", "config": {"root": str(root)}}
    autonomy = SimpleNamespace(mode="max", to_dict=lambda: {"mode": "max"})
    orchestrator = SimpleNamespace(_repair_registration=attached,
        config=SimpleNamespace(execution=SimpleNamespace(autonomy=autonomy)),
        record_run_blocker=lambda **kwargs: None)
    cases = [("blocked", "blocked", 3), ("blocked", "waiting", 3),
             ("ready", "blocked", 3), ("cancelled", "cancelled", 3), ("completed", "finished", 0)]
    for job_state, subscriber_state, expected in cases:
        calls = []

        def rpc(config, request):
            calls.append(request["op"])
            assert len(calls) <= 2, "terminal status did not stop the waiting relay"
            if request["op"] == "submit":
                return {"job": "job"}
            assert request["op"] == "status"
            return {"job": {"id": "job", "state": job_state, "result": {"error": "probe failure"}},
                    "subscribers": [{"id": "probe", "state": subscriber_state}], "registered": []}

        with contextlib.ExitStack() as patches:
            patches.enter_context(patch("auto_agents.cli._run_command_for_self_repair_resume", return_value=["run"]))
            patches.enter_context(patch("auto_agents.config.load_run_state", return_value=SimpleNamespace(run_id="run", current_stage="implement")))
            patches.enter_context(patch("auto_agents.process_supervision.ACTIVE_PROCESSES.terminate_all"))
            patches.enter_context(patch("auto_agents.process_supervision.ACTIVE_PROCESSES.snapshot", return_value=[]))
            patches.enter_context(patch.object(repair_client, "git", return_value="probe"))
            patches.enter_context(patch.object(repair_client, "rpc", side_effect=rpc))
            patches.enter_context(patch.object(repair_client, "register", side_effect=AssertionError("terminal status caused re-registration")))
            result = repair_client.submit_and_wait(root, orchestrator, RuntimeError("probe"),
                SelfRepairDecision(True, fingerprint="probe"), SimpleNamespace(command="run"), SimpleNamespace())
        assert result == expected and calls == ["submit", "status"], "incorrect terminal exit"


def main():
    runtime = Path(sys.argv[1]).resolve()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(runtime / "src"))
    checks = {}
    with tempfile.TemporaryDirectory(prefix="auto-agents-runtime-probe-") as temporary:
        root = Path(temporary)
        os.chdir(root)
        for name in ("AUTO_AGENTS_STORAGE_ROOT", "AUTO_AGENTS_VERIFICATION_ROOT",
                     "AUTO_AGENTS_WORKER_ROOT", "AUTO_AGENTS_CLUSTER_HOME"):
            os.environ[name] = str(root / name.lower())
        os.environ.update(AUTO_AGENTS_REPAIR_CONTROL_DISABLED="1", AUTO_AGENTS_STORAGE_MAINTENANCE="off")
        for check in (control_protocol, progress_supervision, acceptance_planning, terminal_repair_status):
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    check(root)
                checks[check.__name__] = True
            except Exception as error:
                checks[check.__name__] = type(error).__name__ + ": " + str(error)[:500]
    print(json.dumps({"checks": checks}))
    return 0 if all(value is True for value in checks.values()) else 3


if __name__ == "__main__":
    raise SystemExit(main())
