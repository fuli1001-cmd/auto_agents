"""Conversation rounds must not impersonate private-writer attempts."""
import json
import pytest
from auto_agents.models import SessionState
from auto_agents.session_verification import preimplementation_exit, preimplementation_failure

@pytest.mark.parametrize('evidence', ['confirmed', 'missing_confirmation', 'current_attempt', 'retained_attempt', 'unknown_attempt', 'fix', 'candidate_superseded', 'receipt_writer_result', 'receipt_verification', 'receipt_completion', 'child_returned', 'candidate_paths', 'candidate_custody'])
def test_read_only_history_cannot_erase_implementation_or_create_confirmation(evidence):
    state = SessionState(session_id='retained-exit', mode='fix', status='completed', resolution='not_a_bug')
    state.verification_binding = {'task_scope': {'task_ids': ['task-owned'], 'requirement_ids': []}}
    state.execution_log = [{'action': 'converse_error', 'attempt': 1, 'result': 'Transient error'}, {'action': 'internal_action_auto_authorized', 'attempt': 2, 'result': 'Continue inspecting'}, {'action': 'not_a_bug', 'attempt': 0, 'user_confirmed': True, 'result': 'Expected behavior'}]
    if evidence == 'missing_confirmation':
        state.execution_log.pop()
    elif evidence == 'current_attempt':
        state.current_attempt = 1
    elif evidence == 'retained_attempt':
        state.execution_log.insert(0, {'action': 'implementation_attempts_retained', 'attempt': 1})
    elif evidence == 'unknown_attempt':
        state.execution_log.insert(0, {'action': 'unrecognized_event', 'attempt': 1})
    elif evidence == 'candidate_paths':
        state.candidate_paths = {'value.py': 'unattributed'}
    elif evidence == 'candidate_custody':
        state.candidate_custody = {'receipt': {'session_id': 'another-child'}}
    elif evidence != 'confirmed':
        state.execution_log.insert(0, {'action': evidence, 'attempt': 0})
    original = state.to_dict()
    loaded = SessionState.from_dict(json.loads(json.dumps(original)))
    result = preimplementation_exit(loaded)
    assert result == (loaded.execution_log[-1] if evidence == 'confirmed' else None)
    assert preimplementation_failure(loaded) is None
    assert loaded.to_dict() == original
