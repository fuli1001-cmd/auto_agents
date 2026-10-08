"""Physical call accounting, with no provider binaries or network calls."""
import json
from dataclasses import replace
import pytest
from auto_agents.models import AgentRequest, AgentUsage, AgentTermination
from auto_agents.performance_trace import PerformanceTrace
from auto_agents.provider_usage import invoke_provider

def test_legacy_trace_remains_readable_and_identified(tmp_path):
    trace = PerformanceTrace(tmp_path, workflow_kind='run', subject_id='legacy')
    trace.event('agent', 'old', metadata={'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 10})
    summary = trace.summary()
    assert summary['metrics']['input_tokens'] == 100
    assert summary['metrics']['legacy_usage_calls'] == 1
    assert summary['usage_accounting'] == 'legacy'

@pytest.mark.parametrize('payload', [None, {}, {'output_tokens': 10}])
def test_native_parsers_keep_missing_usage_unknown(payload):
    from auto_agents.adapters.codex import CodexAdapter
    from auto_agents.adapters.claude_code import ClaudeCodeAdapter
    from auto_agents.models import ProviderConfig
    _, usage, _ = CodexAdapter(ProviderConfig())._parse_json_stdout(json.dumps({'type': 'turn.completed', 'usage': payload}))
    assert usage is None
    _, usage, _, _, _ = ClaudeCodeAdapter(ProviderConfig(kind='claude-code'))._parse_json_stdout(json.dumps({'type': 'result', 'usage': payload}))
    assert usage is None
