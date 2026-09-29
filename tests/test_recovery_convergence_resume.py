"""Full stopped-child recovery: actual Git custody, pytest, diagnosis and delivery."""
import json

import pytest

from auto_agents.config import load_session_state
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.recovery.policy import enable
from auto_agents.session import Session
from test_engine_child_recovery import ObservationBoundary
from test_retained_candidate_resume import stopped_candidate


def test_stopped_child_automatically_corrects_candidate_and_returns_to_parent(tmp_path, monkeypatch):
    root, store, child, calls = stopped_candidate(tmp_path, monkeypatch, candidate_value=-1,
        verify_command='python -m pytest -q tests/test_owned.py::test_owned')
    stream = store.binding(root, 'session:' + child.session_id)
    before = store.load(stream)['budget'].copy()
    original_receipt = child.candidate_custody['receipt'].copy()
    original = Orchestrator._call_with_failover_owned
    def transport(self, request):
        if request.attempt_id.startswith('recovery-diagnose:'):
            calls.append('diagnose')
            properties = request.response_schema['properties']
            text = json.dumps({'observation': properties['observation']['enum'][0],
                'hypothesis': 'The retained value is negative instead of the required one',
                'failure_ids': properties['failure_ids']['items']['enum'],
                'paths': ['value.py'], 'expected_result': 'The owned value test executes and passes'})
            return AgentResult(True, ['local-diagnosis'], request.output_path, summary=text, stdout=text)
        result = original(self, request)
        if request.purpose == 'fix':
            (request.cwd/'value.py').write_text('VALUE = 1\n')
        return result
    monkeypatch.setattr(Orchestrator, '_call_with_failover_owned', transport)
    # The new candidate must be checked against the actual bug selector, not
    # silently accepted as a pre-existing failure of the broader baseline.
    enable(store, stream)
    store.set_meta('active_runtime', {'source': 'a'*64})
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'completed', saved.to_dict()
    assert saved.candidate_custody['receipt']['fingerprint'] != original_receipt['fingerprint']
    assert calls == ['fix', 'diagnose', 'fix', 'review', 'collab']
    assert store.replay(stream)['budget']['implementations'] == before['implementations'] + 1
    assert store.load(stream)['recovery']['legacy_budget'] == before
    assert saved.current_attempt == child.current_attempt + 1


@pytest.mark.parametrize('stop_kind', ['kernel_environment_blocked', 'kernel_protocol_invalid', 'verification_inconclusive'])
def test_new_runtime_recovers_retained_verification_or_review_block_without_new_writer(tmp_path, monkeypatch, stop_kind):
    root, store, child, calls = stopped_candidate(tmp_path, monkeypatch, stop_kind=stop_kind)
    stream = store.binding(root, 'session:' + child.session_id)
    before = store.load(stream)['budget'].copy()
    enable(store, stream)
    store.set_meta('active_runtime', {'source': 'a'*64})
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    assert load_session_state(root, child.session_id).status == 'completed'
    assert calls == ['fix', 'review', 'collab']
    assert store.replay(stream)['budget']['implementations'] == before['implementations']
