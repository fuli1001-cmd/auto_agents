"""Offline invariants for full task reconstruction and token optimizations."""
from dataclasses import replace
import pytest
from auto_agents.models import AgentRequest, AgentResult, ProviderConfig
from auto_agents.orchestrator import Orchestrator
from auto_agents.prompting import ContextBlock, PromptBlock, ProviderRuntime, compose_prompt, prepare_request
from auto_agents.prompting.core import fresh_request
from auto_agents.prompting.runtime import resolve_runtime

def request(root):
    return AgentRequest('implement', 'deep', compose_prompt(['Preserve every existing database row.', ContextBlock('User correction: preserve fractional totals.', 'user', 'message:0'), PromptBlock('Return IMPLEMENTED and proof references.', kind='output')], purpose='implement'), root, root / 'result.md')

def test_native_effort_edit_changes_runtime_identity(tmp_path):
    home = tmp_path / 'native'
    home.mkdir()
    (home / 'config.toml').write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
    config = ProviderConfig(kind='codex', profile_map={'deep': ''})
    first = resolve_runtime(config, request(tmp_path), env={'CODEX_HOME': str(home)}, probe=False)
    (home / 'config.toml').write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n')
    second = resolve_runtime(config, request(tmp_path), env={'CODEX_HOME': str(home)}, probe=False)
    assert first.resolved_model == second.resolved_model == 'gpt-6-astra'
    assert first.settings_fingerprint != second.settings_fingerprint

@pytest.fixture
def session_case(tmp_path):
    from auto_agents.session import Session
    from auto_agents.models import SessionState
    Orchestrator.init_project(tmp_path, 'token-test', 'mock')
    orch = Orchestrator(tmp_path)
    session = Session(orch, mode='collab')
    goal = 'Preserve all existing behavior. ' * 100
    state = SessionState(session_id='token-test', mode='collab', status='executing', goal=goal, conversation=[{'role': 'user', 'content': goal}])
    requests = []
    runtime = ProviderRuntime('mock', resolved_model='fixed-test-model')

    def call(req):
        prepared = prepare_request(req, runtime)
        requests.append(prepared)
        return AgentResult(True, [], req.output_path, summary='Recorded the requirement.', provider_session_id='native-session', prompt_metadata=prepared.prompt_metadata)
    orch._call_with_failover = call
    return (session, state, requests)

def session_turn(session, state, label):
    return session._call_agent(state, label, session._build_collab_prompt(state, ''))
