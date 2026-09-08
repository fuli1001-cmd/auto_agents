"""Mandatory supervision, configuration compatibility, and caller cancellation."""
import copy
import json
import os
import sys
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from auto_agents.adapters.base import run_subprocess_with_optional_streaming
from auto_agents.config import DEFAULT_CONFIG, load_project_config, migrate_project_config
from auto_agents.models import AgentRequest, AgentResult, AgentTermination
from auto_agents.orchestrator import Orchestrator
from auto_agents.run_lock import ProjectRunLock
from auto_agents.supervision import execution_budget_probe


def test_old_config_cannot_disable_supervision_or_restore_deadlines(tmp_path):
    payload = copy.deepcopy(DEFAULT_CONFIG)
    payload["providers"]["codex"].update(timeout_seconds=1, idle_timeout_seconds=1)
    payload["execution"]["smart_timeout"].update(
        enabled=False, safety_ceiling_seconds=1, post_ceiling_finalize_seconds=1,
        active_tool_grace_seconds=1, fresh_continuation_limit=99)
    config_path = tmp_path / ".auto-agents/config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(payload))
    before = config_path.read_bytes()
    with pytest.warns(UserWarning, match="obsolete and ignored"):
        config = load_project_config(tmp_path)
    assert config_path.read_bytes() == before
    assert config.execution.smart_timeout.provider_idle_seconds == 1800
    assert "enabled" not in config.execution.smart_timeout.to_dict()
    assert "timeout_seconds" not in config.providers["codex"].to_dict()
    with ProjectRunLock(tmp_path):
        assert migrate_project_config(tmp_path)
        assert not migrate_project_config(tmp_path)
    saved = json.loads(config_path.read_text())
    assert saved["execution"]["smart_timeout"] == DEFAULT_CONFIG["execution"]["smart_timeout"]
    assert saved["providers"]["codex"] == DEFAULT_CONFIG["providers"]["codex"]


def test_expired_budget_does_not_launch_provider_and_preserves_reason(tmp_path):
    with patch("auto_agents.supervision.time.monotonic", return_value=100):
        probe = execution_budget_probe(10)
    request = AgentRequest("implement", "deep", "work", tmp_path, tmp_path / "out",
                           termination_probe=probe, progress_report_path=tmp_path / "attempt.json")
    with patch("auto_agents.supervision.time.monotonic", return_value=111), \
         patch("auto_agents.supervision.worktree_fingerprint", return_value=""), \
         patch("auto_agents.adapters.base.subprocess.Popen") as launch:
        result = run_subprocess_with_optional_streaming([sys.executable, "-c", "print('unexpected')"],
                                                       request, dict(os.environ))
    launch.assert_not_called()
    assert result.termination.reason == "execution_budget_exhausted"
    assert json.loads((tmp_path / "attempt.json").read_text())["reason"] == "execution_budget_exhausted"
    from auto_agents.self_repair import classify_auto_agents_error
    assert classify_auto_agents_error(result.stderr).category == "execution_time_budget"


def test_attempt_preparation_preserves_budget_and_health_across_continuation(tmp_path):
    orchestrator = Orchestrator.__new__(Orchestrator)
    health = Mock(return_value="")
    orchestrator._health_termination_probe = health
    with patch("auto_agents.supervision.time.monotonic", return_value=100):
        probe = execution_budget_probe(10)
    request = AgentRequest("implement", "deep", "work", tmp_path, tmp_path / "out",
                           termination_probe=probe)
    first = orchestrator._provider_request_for_attempt(request, provider="codex", resume_index=0,
                                                      allow_interrupted_resume=False)
    continuation = orchestrator._provider_request_for_attempt(
        replace(first, resume_session_id="session"), provider="codex", resume_index=1,
        allow_interrupted_resume=False)
    with patch("auto_agents.supervision.time.monotonic", return_value=105):
        assert first.termination_probe() == ""
        health.assert_called()
        health.return_value = "health_quiesce"
        assert continuation.termination_probe() == "health_quiesce"
    with patch("auto_agents.supervision.time.monotonic", return_value=111):
        assert first.termination_probe() == "execution_budget_exhausted"
        assert continuation.termination_probe() == "execution_budget_exhausted"


@pytest.mark.parametrize("reason", ["execution_budget_exhausted", "verification_environment_blocked"])
def test_budget_cancellation_does_not_retry_or_modify_run_incidents(tmp_path, reason):
    from test_failover import _SequenceAdapter, _stub_orchestrator
    result = AgentResult(False, [], tmp_path / "out",
                         termination=AgentTermination(reason))
    adapter = _SequenceAdapter([result])
    orchestrator = _stub_orchestrator({"codex": {}}, "codex", {"codex": adapter})
    orchestrator._record_provider_execution_incident = Mock(side_effect=AssertionError("unexpected incident"))
    request = AgentRequest("self_repair", "deep", "work", tmp_path, tmp_path / "out")
    assert not orchestrator._call_with_failover(request).ok
    assert adapter.calls == 1
    orchestrator._record_provider_execution_incident.assert_not_called()
    assert not orchestrator._provider_health_map()


@pytest.mark.parametrize("reason", ["execution_budget_exhausted", "verification_environment_blocked"])
def test_expired_budget_precedes_provider_health_canary(tmp_path, reason):
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._probe_active_provider = Mock(side_effect=AssertionError("unexpected model call"))
    request = AgentRequest("implement", "deep", "work", tmp_path, tmp_path / "out",
                           termination_probe=lambda: reason)
    assert orchestrator._call_with_failover(request).termination.reason == reason
    orchestrator._probe_active_provider.assert_not_called()


def test_health_canary_uses_short_inactivity_allowance_without_deadline(tmp_path):
    from auto_agents.models import AgentProgressEvent, ProviderConfig, SmartTimeoutConfig
    from auto_agents.supervision import ProgressDecoder, ProgressSupervisor
    from test_failover import _stub_orchestrator
    orchestrator = _stub_orchestrator({"codex": {}}, "codex", {})
    orchestrator.config.providers["codex"] = ProviderConfig()
    orchestrator.config.execution.smart_timeout = SmartTimeoutConfig()
    orchestrator._provider_failover_config = lambda: type("Config", (), {"probe_timeout_seconds": 5})()
    adapter = orchestrator._build_probe_adapter_for_provider("codex")
    request = AgentRequest("provider_probe", "deep", "ready?", tmp_path, tmp_path / "out")
    with patch("auto_agents.supervision.time.monotonic", return_value=100):
        supervisor = ProgressSupervisor(config=adapter.smart_timeout, request=request,
            provider="codex", process_pid=99999999, decoder=ProgressDecoder())
    with patch("auto_agents.supervision.time.monotonic", return_value=106):
        supervisor.observe_events([AgentProgressEvent(kind="activity", detail="heartbeat")])
        assert supervisor.poll() == "semantic_stall"


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_explicit_budget_is_rejected(seconds):
    with pytest.raises(ValueError, match="finite positive"):
        execution_budget_probe(seconds)


@pytest.mark.parametrize("option,expected", [([], None), (["--timeout", "15"], 15)])
def test_evaluation_budget_requires_explicit_cli_option(option, expected):
    from auto_agents.prompting import evaluate
    with patch.object(evaluate, "run") as run:
        evaluate.main(["run", "--project", "/tmp/project", "--baseline", "/tmp/corpus.json",
                       "--output", "/tmp/results", *option])
    assert run.call_args.args[0].timeout == expected


def test_cancelled_evaluation_retains_termination_and_cannot_accept_partial_answer(tmp_path):
    from auto_agents.prompting import ProviderRuntime, evaluate
    project = tmp_path / "project"
    Orchestrator.init_project(project, "test", "mock")
    corpus = tmp_path / "corpus.json"
    case = {**evaluate.CASES[2], "baseline_native": {}, "baseline_prompt": "Review"}
    corpus.write_text(json.dumps({"version": 1, "cases": [case]}))
    output = tmp_path / "evaluation"
    adapter = Mock()
    adapter.available.return_value = True
    adapter.describe_runtime.return_value = ProviderRuntime(provider="mock")
    def cancel(request):
        assert request.termination_probe is not None
        assert request.progress_report_path.parent == output
        return AgentResult(False, [], request.output_path, summary="DECISION: fail",
                           termination=AgentTermination("execution_budget_exhausted"))
    adapter.run.side_effect = cancel
    with patch.object(Orchestrator, "_build_adapter_for_provider", return_value=adapter):
        evaluate.main(["run", "--project", str(project), "--baseline", str(corpus),
                       "--output", str(output), "--providers", "mock", "--repetitions", "1", "--timeout", "1"])
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert all(row["protocol_ok"] and not row["accepted"] and not row["ok"] for row in rows)
    assert all(row["termination"]["reason"] == "execution_budget_exhausted" for row in rows)


@pytest.mark.parametrize("provider", ["codex", "copilot", "claude"])
@pytest.mark.parametrize("different_inputs", [False, True])
def test_native_tool_ids_do_not_create_progress_but_inputs_do(tmp_path, provider, different_inputs):
    from auto_agents.adapters.codex import CodexProgressDecoder
    from auto_agents.adapters.copilot_cli import CopilotProgressDecoder
    from auto_agents.adapters.claude_code import ClaudeProgressDecoder
    from auto_agents.models import SmartTimeoutConfig
    from auto_agents.supervision import ProgressSupervisor
    decoder = {"codex": CodexProgressDecoder, "copilot": CopilotProgressDecoder,
               "claude": ClaudeProgressDecoder}[provider]()
    request = AgentRequest("implement", "deep", "work", tmp_path, tmp_path / "out")
    clock = [0.0]
    with patch("auto_agents.supervision.time.monotonic", side_effect=lambda: clock[0]):
        supervisor = ProgressSupervisor(config=SmartTimeoutConfig(), request=request,
            provider=provider, process_pid=99999999, decoder=decoder)
        for index in range(3):
            clock[0] = float(index + 1)
            identifier = f"tool-{index}"
            command = f"read evidence-{index if different_inputs else 0}"
            if provider == "codex":
                item = {"id": identifier, "type": "command_execution", "command": command}
                events = [{"type": "item.started", "item": item},
                          {"type": "item.completed", "item": {**item, "aggregated_output": "ok", "exit_code": 0}}]
            elif provider == "copilot":
                events = [{"type": "tool.execution_start", "data": {
                    "toolCallId": identifier, "toolName": "shell", "arguments": {"command": command}}},
                    {"type": "tool.execution_complete", "data": {
                        "toolCallId": identifier, "toolName": "shell", "result": "ok"}}]
            else:
                events = [{"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": identifier, "name": "shell", "input": {"command": command}}]}},
                    {"type": "user", "message": {"content": [{
                        "type": "tool_result", "tool_use_id": identifier, "content": "ok"}]}}]
            for event in events:
                supervisor.observe_io("stdout", json.dumps(event))
        assert supervisor.poll() == (None if different_inputs else "loop_detected")
        assert supervisor.last_semantic_progress == (3.0 if different_inputs else 1.0)
        completed = [event for event in supervisor.events if event["kind"] == "tool_completed"]
        assert [event["semantic"] for event in completed] == (
            [True, True, True] if different_inputs else [True, False, False])


def test_loop_detection_checks_workspace_before_stopping_fast_edit_cycle(tmp_path):
    from auto_agents.models import AgentProgressEvent, SmartTimeoutConfig
    from auto_agents.supervision import ProgressSupervisor
    request = AgentRequest("implement", "deep", "work", tmp_path, tmp_path / "out")
    with patch("auto_agents.supervision.worktree_fingerprint", return_value="before") as fingerprint:
        supervisor = ProgressSupervisor(config=SmartTimeoutConfig(), request=request,
            provider="test", process_pid=99999999, decoder=None)
        result = AgentProgressEvent("tool_completed", fingerprint="same-output", semantic=True)
        supervisor.observe_events([result, result])
        fingerprint.return_value = "after"
        supervisor.observe_events([result])
        assert supervisor.poll() is None
        assert supervisor.workspace_fingerprint == "after"
