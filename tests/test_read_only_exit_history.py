"""Conversation rounds must not impersonate private-writer attempts."""
from copy import deepcopy
import json

import pytest

from auto_agents.config import load_project_config, load_session_state, load_task_plan, save_session_state
from auto_agents.authorization import authorization_policy_for_state
from auto_agents.git_ops import head_ref
from auto_agents.models import AgentResult, SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.session_verification import preimplementation_exit, preimplementation_failure
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_engine_child_recovery import ObservationBoundary, parent_workflow
from test_session_verification_ownership import project, git, _retain_contract, _prepare_binding_child_resume


@pytest.mark.parametrize('route', ['fix', 'resume'])
@pytest.mark.parametrize('outcome', ['legacy', 'structured', 'preflight'])
@pytest.mark.parametrize('first_event', ['converse_error', 'internal_action_auto_authorized'])
def test_public_read_only_round_then_zero_candidate_exit(tmp_path, monkeypatch, route, outcome, first_event):
    # Do not let read-only Git queries refresh the index stat cache. Staged
    # changes would still violate the exact index assertion below.
    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    root, child = project(tmp_path)
    if outcome == 'preflight':
        plan = load_task_plan(root)
        plan['tasks'][0]['verification_refs'].append('missing.proof')
        _retain_contract(root, child, load_project_config(root), plan)
    child.status = 'conversing'
    # Freeze the fixture's already selected policy before measuring that the
    # conversation leaves authority unchanged; exclude lazy legacy migration.
    child.authorization_policy = authorization_policy_for_state(auto_approve=child.auto_approve).to_dict()
    store, snapshot, original = parent_workflow(root, child)
    if route == 'resume':
        _prepare_binding_child_resume(root, store, snapshot, original)
    parent = load_session_state(root, 'parent')
    parent.lineage_head_ref = child.baseline_head_ref
    save_session_state(root, parent)
    active = parent.active_handoff_id
    preserved_state = {key: deepcopy(getattr(child, key)) for key in (
        'current_attempt', 'attempt_epoch', 'attempts_since_progress', 'max_attempts', 'hard_ceiling',
        'goal', 'goal_execution_environment', 'authorization_policy', 'parent_handoff_id', 'workflow_id')}
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'unrelated\0work')
    protected = {name: (root / name).read_bytes() for name in (
        'foreign.py', 'foreign-note.txt', '.git/index', '.auto-agents/state/task_plan.json')}
    head, refs = head_ref(root), git(root, 'show-ref')
    calls, confirmations, retained_history = [], [], []
    reason = 'The retained behavior is intentional.'
    replies = {
        'legacy': 'NOT_A_BUG: ' + reason,
        'structured': 'FIX_DISPOSITION v1: ' + json.dumps({
            'decision': 'not_bug', 'summary': child.goal, 'reason': reason}),
        'preflight': 'GOAL_CLEAR',
    }

    def provider(self, request):
        calls.append((request.purpose, request.sandbox_mode))
        assert request.purpose == 'fix_converse' and request.sandbox_mode == 'read-only'
        assert len(calls) <= 2
        if len(calls) == 1:
            if first_event == 'converse_error':
                raise RuntimeError('Transient read-only transport error')
            reply = 'FIX_DISPOSITION v1: ' + json.dumps({
                'decision': 'need_user', 'decision_class': 'implementation_scope',
                'question': 'May I inspect the existing behavior to classify this report?'})
        else:
            # Observe the real persisted retry/authorization event, not a
            # synthetic writer counter or a monkeypatched exit classifier.
            reloaded = load_session_state(root, child.session_id)
            assert reloaded.current_attempt == 0 and not reloaded.candidate_custody
            entry = next(row for row in reloaded.execution_log if row['action'] == first_event)
            assert entry['attempt'] == 1
            retained_history.append(deepcopy(entry))
            reply = replies[outcome]
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)

    def confirm(prompt):
        confirmations.append(prompt)
        assert outcome != 'preflight' and 'not a bug' in prompt
        return 'y'

    def forbidden(*args, **kwargs):
        pytest.fail('Read-only history must not trigger baseline execution, a writer, or shared rollback')

    def observe_parent(self, state):
        assert state.session_id == 'parent'
        raise ObservationBoundary()

    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
    if outcome != 'preflight':
        monkeypatch.setattr(Session, '_phase_fix_execute', forbidden)
    monkeypatch.setattr(Session, '_phase_collab_loop', observe_parent)
    monkeypatch.setattr(WorkflowCoordinator, '_rollback_handoff_uncommitted', forbidden)
    session = Session(Orchestrator(root, user_input_fn=confirm), mode='collab', auto_approve=True)
    with pytest.raises(ObservationBoundary):
        session.resume('parent')
    saved = load_session_state(root, child.session_id)
    result = store.load_handoff(active).result
    assert calls == [('fix_converse', 'read-only'), ('fix_converse', 'read-only')]
    assert len(retained_history) == 1 and retained_history[0] in saved.execution_log
    assert {key: getattr(saved, key) for key in preserved_state} == preserved_state
    assert saved.current_attempt == 0 and saved.candidate_paths == saved.candidate_custody == {}
    assert result['candidate_ownership'] == 'none'
    assert result['changed_paths'] == result['commit_shas'] == result['rolled_back_paths'] == []
    assert result['candidate_delivery'] == {} and result['head_after'] == ''
    assert 'ownership_diagnostic' not in result and 'rollback_diagnostic' not in result
    if outcome == 'preflight':
        assert saved.status == result['status'] == 'blocked'
        assert saved.resolution == result['resolution'] == 'verification_ownership'
        failure = saved.execution_log[-1]
        assert result['summary'] == failure['result']
        assert result['diagnostic'] == failure['diagnostic']
        assert result['diagnostic']['verification_ref'] == 'missing.proof'
        assert result['retry_fix'] is False and confirmations == []
        assert preimplementation_failure(saved) == failure
    else:
        assert saved.status == result['status'] == 'completed'
        assert saved.resolution == result['resolution'] == 'not_a_bug'
        assert len(confirmations) == 1
        assert saved.execution_log[-1]['action'] == 'not_a_bug'
        assert saved.execution_log[-1]['user_confirmed'] is True
        assert preimplementation_failure(saved) is None
    assert load_session_state(root, 'parent').lineage_head_ref == parent.lineage_head_ref
    assert load_session_state(root, 'parent').lineage_changed_paths == parent.lineage_changed_paths
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert head_ref(root) == head and git(root, 'show-ref') == refs
    with pytest.raises(ObservationBoundary):
        session.resume('parent')
    assert len(calls) == 2 and retained_history[0] in load_session_state(root, child.session_id).execution_log
    assert store.load_handoff(active).result == result


@pytest.mark.parametrize('evidence', [
    'confirmed', 'missing_confirmation', 'current_attempt', 'retained_attempt', 'unknown_attempt',
    'fix', 'candidate_superseded', 'receipt_writer_result', 'receipt_verification', 'receipt_completion',
    'child_returned', 'candidate_paths', 'candidate_custody',
])
def test_read_only_history_cannot_erase_implementation_or_create_confirmation(evidence):
    state = SessionState(session_id='retained-exit', mode='fix', status='completed', resolution='not_a_bug')
    state.verification_binding = {'task_scope': {'task_ids': ['task-owned'], 'requirement_ids': []}}
    state.execution_log = [
        {'action': 'converse_error', 'attempt': 1, 'result': 'Transient error'},
        {'action': 'internal_action_auto_authorized', 'attempt': 2, 'result': 'Continue inspecting'},
        {'action': 'not_a_bug', 'attempt': 0, 'user_confirmed': True, 'result': 'Expected behavior'},
    ]
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
        # A real effect record denies an empty exit even when its attempt is
        # missing or zero; subsequent read-only rounds cannot override it.
        state.execution_log.insert(0, {'action': evidence, 'attempt': 0})
    original = state.to_dict()
    loaded = SessionState.from_dict(json.loads(json.dumps(original)))
    result = preimplementation_exit(loaded)
    assert result == (loaded.execution_log[-1] if evidence == 'confirmed' else None)
    assert preimplementation_failure(loaded) is None
    assert loaded.to_dict() == original
