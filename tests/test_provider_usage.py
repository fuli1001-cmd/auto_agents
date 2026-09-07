"""Physical call accounting, with no provider binaries or network calls."""
import json
from dataclasses import replace

import pytest

from auto_agents.models import AgentRequest, AgentUsage, AgentTermination
from auto_agents.performance_trace import PerformanceTrace
from auto_agents.provider_usage import invoke_provider
from test_failover import _stub_orchestrator, _SequenceAdapter, _make_result


def setup(tmp_path, first, second=None):
    adapters = {"codex": _SequenceAdapter(first)}
    if second is not None:
        adapters["other"] = _SequenceAdapter(second)
    orch = _stub_orchestrator({key: {} for key in adapters}, "codex", adapters)
    req = AgentRequest("implement", "deep", "Complete the owned task.", tmp_path, tmp_path / "result.md",
                       usage_context={"project_root": str(tmp_path), "workflow_kind": "run", "subject_id": "usage-test"})
    trace = PerformanceTrace(tmp_path, workflow_kind="run", subject_id="usage-test")
    return orch, req, trace, adapters


def measured(ok=True, input_tokens=100, output_tokens=10, **kwargs):
    return replace(_make_result(ok, **kwargs), usage=AgentUsage(input_tokens, 20, output_tokens))


def test_failover_counts_failed_provider_and_avoids_parent_double_count(tmp_path):
    orch, req, trace, _ = setup(tmp_path, [measured(False, stderr="rate limit")], [measured(input_tokens=200)])
    result = orch._call_with_failover(req)
    assert result.usage.input_tokens == 300
    assert len(result.usage_attempts) == 2
    trace.event("agent", "logical-result", metadata={
        "logical_call_id": result.prompt_metadata["logical_call_id"],
        "input_tokens": 300, "cached_input_tokens": 40, "output_tokens": 20,
    })
    # Replayed diagnostics with the same physical ID must also not double count.
    trace.event("provider_attempt", "implement", metadata=result.usage_attempts[0])
    summary = trace.summary()
    assert summary["metrics"]["input_tokens"] == 300
    assert summary["metrics"]["provider_calls"] == 2
    assert summary["metrics"]["agent_calls"] == 1
    assert summary["usage_accounting"] == "physical"
    assert sum(group["failed_calls"] for group in summary["provider_usage"]) == 1


def test_smart_recovery_counts_each_physical_call_once(tmp_path):
    termination = AgentTermination("semantic_stall", 1, 1, 1)
    orch, req, trace, _ = setup(tmp_path, [measured(False, termination=termination), measured()])
    result = orch._call_with_failover(req)
    assert result.ok and result.usage.input_tokens == 200
    assert trace.summary()["metrics"]["provider_calls"] == 2


def test_unknown_usage_is_not_reported_as_zero(tmp_path):
    orch, req, trace, _ = setup(tmp_path, [_make_result(False, stderr="rate limit")], [measured()])
    result = orch._call_with_failover(req)
    assert not result.prompt_metadata["usage_complete"]
    metrics = trace.summary()["metrics"]
    assert metrics["input_tokens"] is None
    assert metrics["known_input_tokens"] == 100
    assert metrics["unknown_usage_calls"] == 1


def test_all_providers_exhausted_still_records_usage(tmp_path):
    orch, req, trace, _ = setup(tmp_path, [measured(False, stderr="rate limit")], [measured(False, stderr="rate limit")])
    with pytest.raises(RuntimeError, match="All providers exhausted"):
        orch._call_with_failover(req)
    assert trace.summary()["metrics"]["input_tokens"] == 200


def test_health_probe_is_a_separate_physical_call(tmp_path):
    orch, req, trace, _ = setup(tmp_path, [measured()])
    health = orch._record_provider_failure("codex", category="connection", detail="offline")
    health.next_probe_at = 0
    orch._build_probe_adapter_for_provider = lambda kind: _SequenceAdapter([measured(summary="PROVIDER_READY")])
    assert orch._call_with_failover(req).ok
    summary = trace.summary()
    assert summary["metrics"]["provider_calls"] == 2
    assert summary["metrics"]["input_tokens"] == 200
    assert {group["stage"] for group in summary["provider_usage"]} == {"implement", "provider_probe"}


def test_exception_records_unknown_attempt_without_replacing_error(tmp_path):
    _, req, trace, _ = setup(tmp_path, [])
    class Broken:
        def run(self, request):
            raise RuntimeError("connection failed")
    with pytest.raises(RuntimeError, match="connection failed"):
        invoke_provider(Broken(), req, "codex")
    assert trace.summary()["metrics"]["unknown_usage_calls"] == 1


def test_diagnostics_failure_does_not_retry_completed_operation(tmp_path, monkeypatch):
    _, req, _, adapters = setup(tmp_path, [measured()])
    def unavailable(*args, **kwargs):
        raise OSError("trace disk unavailable")
    monkeypatch.setattr(PerformanceTrace, "event", unavailable)
    result = invoke_provider(adapters["codex"], req, "codex")
    assert result.ok
    assert result.usage_attempts[0]["recording_error"] == "OSError"
    assert adapters["codex"].calls == 1


def test_legacy_trace_remains_readable_and_identified(tmp_path):
    trace = PerformanceTrace(tmp_path, workflow_kind="run", subject_id="legacy")
    trace.event("agent", "old", metadata={"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10})
    summary = trace.summary()
    assert summary["metrics"]["input_tokens"] == 100
    assert summary["metrics"]["legacy_usage_calls"] == 1
    assert summary["usage_accounting"] == "legacy"


@pytest.mark.parametrize("payload", [None, {}, {"output_tokens": 10}])
def test_native_parsers_keep_missing_usage_unknown(payload):
    from auto_agents.adapters.codex import CodexAdapter
    from auto_agents.adapters.claude_code import ClaudeCodeAdapter
    from auto_agents.models import ProviderConfig
    _, usage, _ = CodexAdapter(ProviderConfig())._parse_json_stdout(json.dumps({"type": "turn.completed", "usage": payload}))
    assert usage is None
    _, usage, _, _, _ = ClaudeCodeAdapter(ProviderConfig(kind="claude-code"))._parse_json_stdout(json.dumps({"type": "result", "usage": payload}))
    assert usage is None
