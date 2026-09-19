"""Legacy committed results carry no authority over shared uncommitted work."""
from copy import deepcopy

import pytest

from auto_agents.config import save_session_state
from auto_agents.git_ops import commit_only_paths, head_ref
from auto_agents.models import SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session_verification import SessionOwnershipError
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_engine_child_recovery import parent_workflow
from test_session_verification_ownership import project, git


def legacy_completion(tmp_path):
    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    child.status, child.resolution = 'completed', 'fixed'
    save_session_state(root, child)
    (root / 'value.py').write_text('VALUE = 1\n')
    delivered = commit_only_paths(root, 'fix: legacy owned value', ['value.py'])
    assert delivered
    return root, child, store, snapshot, handoff, delivered


def dirty_work(root):
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'unrelated\0work')
    return {name: (root / name).read_bytes() for name in (
        'value.py', 'foreign.py', 'foreign-note.txt', '.git/index')}


@pytest.mark.parametrize('route', ['fix', 'resume'])
def test_legacy_completed_range_excludes_dirty_paths_and_cannot_authorize_rollback(tmp_path, monkeypatch, route):
    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    root, child, store, snapshot, original, delivered = legacy_completion(tmp_path)
    handoff = original
    if route == 'resume':
        handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='resume',
            goal=child.goal, reason='consume retained completion',
            payload={'resume_handoff_id': original.handoff_id})
        store.bind_child(snapshot, handoff, WorkflowRef('fix', child.session_id))
    protected = dirty_work(root)
    refs = git(root, 'show-ref')
    coordinator = WorkflowCoordinator(Orchestrator(root))
    retained = deepcopy(child.to_dict())
    result = coordinator._session_result(child, handoff)
    assert result['status'] == 'completed' and result['resolution'] == 'fixed'
    assert result['candidate_ownership'] == 'legacy_committed'
    assert result['head_before'] == original.payload['head_before']
    assert result['head_after'] == delivered
    assert result['commit_shas'] == [delivered]
    assert result['changed_paths'] == ['value.py']
    assert result['candidate_delivery'] == {} and result['rolled_back_paths'] == []
    assert 'ownership_diagnostic' not in result
    # A committed range is a compatibility report, not a private writer receipt.
    with pytest.raises(SessionOwnershipError):
        coordinator._rollback_handoff_uncommitted(snapshot, handoff)
    assert coordinator._session_result(child, handoff) == result
    assert child.to_dict() == retained
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert head_ref(root) == delivered and git(root, 'show-ref') == refs


@pytest.mark.parametrize('missing', [
    'baseline', 'expired_baseline', 'unchanged_head', 'divergent_baseline', 'child_identity',
    'saved_completion', 'matching_completion', 'candidate_receipt', 'receipt_history', 'binding',
    'preflight_failure',
])
def test_legacy_fallback_cannot_recover_missing_or_conflicting_candidate_ownership(tmp_path, monkeypatch, missing):
    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    root, child, store, snapshot, handoff, delivered = legacy_completion(tmp_path)
    if missing == 'baseline':
        handoff.payload.pop('head_before')
    elif missing == 'expired_baseline':
        handoff.payload['head_before'] = 'refs/auto-agents/gate-snapshots/expired'
    elif missing == 'unchanged_head':
        handoff.payload['head_before'] = delivered
    elif missing == 'divergent_baseline':
        tree = git(root, 'rev-parse', 'HEAD^{tree}').strip()
        handoff.payload['head_before'] = git(root, 'commit-tree', tree, '-m', 'unrelated root').strip()
    elif missing == 'child_identity':
        handoff.payload['child_session_id'] = 'another-child'
    elif missing == 'saved_completion':
        (root / '.auto-agents/state/sessions' / child.session_id / 'session_state.json').unlink()
    elif missing == 'matching_completion':
        pending = deepcopy(child)
        pending.status = 'failed'
        save_session_state(root, pending)
    else:
        if missing == 'candidate_receipt':
            child.candidate_paths = {'value.py': 'unattributed'}
        elif missing == 'receipt_history':
            child.execution_log.append({'action': 'receipt_writer_result', 'receipt_fingerprint': 'lost'})
        elif missing == 'binding':
            child.verification_binding = {'session_id': 'another-child'}
        else:
            child.status, child.resolution = 'blocked', 'verification_ownership'
            child.execution_log = [{'action': 'execution_preflight_blocked', 'result': 'missing executable proof',
                                    'failure_kind': child.resolution, 'retry_fix': False,
                                    'diagnostic': {'verification_ref': 'missing.proof'}}]
        save_session_state(root, child)
    store.save_handoff(handoff)
    protected = dirty_work(root)
    refs = git(root, 'show-ref')
    coordinator = WorkflowCoordinator(Orchestrator(root))
    result = coordinator._session_result(child, handoff)
    assert result['status'] == 'blocked' and result['resolution'] == 'verification_ownership'
    assert result['changed_paths'] == result['commit_shas'] == []
    assert result['head_after'] == '' and result['candidate_delivery'] == {}
    if missing == 'preflight_failure':
        assert result['candidate_ownership'] == 'none'
        assert result['summary'] == 'missing executable proof'
        assert result['diagnostic'] == {'verification_ref': 'missing.proof'}
    else:
        assert result['candidate_ownership'] == 'unknown'
        with pytest.raises(SessionOwnershipError):
            coordinator._rollback_handoff_uncommitted(snapshot, handoff)
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert head_ref(root) == delivered and git(root, 'show-ref') == refs


@pytest.mark.parametrize('claim', ['none', 'candidate', 'writer_history', 'attempt_without_receipt'])
def test_unbound_native_completion_carries_no_owned_work(tmp_path, monkeypatch, claim):
    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    root, _, store, snapshot, original, delivered = legacy_completion(tmp_path)
    handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix',
        goal='inspect native completion', reason='legacy delegate', payload={})
    state = SessionState(session_id='native-child', mode='fix', status='completed', resolution='fixed')
    if claim == 'candidate':
        state.candidate_paths = {'value.py': 'unattributed'}
    elif claim == 'writer_history':
        state.execution_log = [{'action': 'fix', 'attempt': 1}]
    elif claim == 'attempt_without_receipt':
        state.current_attempt = 1
    protected = dirty_work(root)
    result = WorkflowCoordinator(Orchestrator(root))._session_result(state, handoff)
    assert result['status'] == ('completed' if claim == 'none' else 'blocked')
    assert result['candidate_ownership'] == 'unknown'
    assert result['changed_paths'] == result['commit_shas'] == []
    assert result['candidate_delivery'] == {} and result['head_after'] == ''
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert head_ref(root) == delivered


def test_unbound_native_status_cannot_complete_a_workflow_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    root, _, store, snapshot, original, delivered = legacy_completion(tmp_path)
    handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix',
        goal='inspect native completion', reason='legacy delegate', payload={})
    parent = SessionState(session_id=snapshot.root.native_id, mode='collab', status='waiting_child',
                          workflow_id=snapshot.workflow_id, active_handoff_id=handoff.handoff_id)
    state = SessionState(session_id='native-child', mode='fix', status='completed', resolution='fixed')
    coordinator = WorkflowCoordinator(Orchestrator(root))
    monkeypatch.setattr(coordinator, 'start_seeded_session', lambda *args, **kwargs: state)
    protected = dirty_work(root)
    coordinator._drive_handoff(None, parent, snapshot)
    recorded = store.load_handoff(handoff.handoff_id)
    assert recorded.status == recorded.result['status'] == 'blocked'
    assert recorded.result['resolution'] == 'verification_ownership'
    assert recorded.result['candidate_ownership'] == 'unknown'
    assert recorded.result['changed_paths'] == recorded.result['commit_shas'] == recorded.result['rolled_back_paths'] == []
    assert recorded.result['candidate_delivery'] == {} and parent.lineage_changed_paths == []
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert head_ref(root) == delivered
