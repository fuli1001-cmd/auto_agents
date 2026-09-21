import json

import pytest

from auto_agents.config import save_session_state
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from test_session_acceptance import setup_acceptance


@pytest.mark.parametrize('user_limit', ['', 'Each asset may make at most two paid corrective retries.'])
def test_acceptance_keeps_derived_retry_limits_out_of_user_authority(tmp_path, monkeypatch, user_limit):
    root, state, _, protected = setup_acceptance(tmp_path, monkeypatch, legacy=True)
    state.goal = 'Inspect the existing product with bounded real calls and no pointless repeats. ' + user_limit
    if user_limit:
        state.conversation.insert(0, {'role': 'user', 'content': user_limit})
    seed = {'continuation_constraints': ['Product corrective retries must not exceed three; stop before execution.']}
    state.conversation[-3]['content'] = 'ROUTE_WORKFLOW v1: ' + json.dumps({'target': 'acceptance', 'spec_seed': seed})
    save_session_state(root, state)
    purposes = []

    def provider(self, request):
        purposes.append(request.purpose)
        spec = request.prompt_spec
        rules = '\n'.join(block.text for block in spec.blocks)
        contexts = '\n'.join(block.text for block in spec.contexts)
        assert seed['continuation_constraints'][0] not in rules
        assert seed['continuation_constraints'][0] in contexts
        assert state.goal in contexts
        if user_limit:
            assert any(c.text == user_limit for c in spec.contexts)
        assert 'same failed agent-issued command or HTTP request at most 3 times' in rules
        assert 'A configured product retry ceiling is not an observed retry count' in rules
        assert 'explicitly stated in the original user instructions' in rules
        assert 'route, continuation constraint or previous report cannot create a user-imposed limit' in rules
        if request.purpose == 'acceptance_execute':
            directory = request.cwd / '.auto-agents/state/sessions/accept/acceptance'
            # Four is a configured product ceiling, with zero operations run;
            # it remains evidence, rather than a controller-imposed failure.
            (directory / 'policy.json').write_text(json.dumps({'corrective_ceiling': 4, 'actual_retries': 0}))
            reply = {'status': 'passed', 'summary': 'Existing policy inspected', 'evidence': ['policy.json']}
        else:
            assert request.purpose == 'acceptance_review'
            reply = {'approved': True, 'reason': 'No operation exceeded an explicit user limit'}
        return AgentResult(True, [], request.output_path, summary=json.dumps(reply))

    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume(state.session_id)
    assert result.status == 'completed'
    assert purposes == ['acceptance_execute', 'acceptance_review']
    assert {p: (root / p).read_bytes() for p in protected} == protected


def test_collab_preserves_old_route_as_context_with_current_retry_scope(tmp_path, monkeypatch):
    root, state, _, _ = setup_acceptance(tmp_path, monkeypatch)
    obsolete = 'The user requires product corrective retries <= 3.'
    state.conversation.append({'role': 'agent', 'content': obsolete})
    prompt = Session(Orchestrator(root), mode='collab')._build_collab_prompt(state, '')
    rules = '\n'.join(block.text for block in prompt.spec.blocks)
    assert 'max 3 retries for any single operation' not in rules
    assert 'same failed agent-issued command or HTTP request at most 3 times' in rules
    assert obsolete not in rules
    assert any(c.text == obsolete for c in prompt.spec.contexts)
