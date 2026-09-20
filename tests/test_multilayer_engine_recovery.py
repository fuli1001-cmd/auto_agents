"""Public recovery of a retained preflight through multiple resume envelopes."""
from copy import deepcopy
import json
import os
import subprocess
import sys

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.orchestrator import Orchestrator
from auto_agents.repair_control import digest
from auto_agents.session_verification import SessionOwnershipError
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_engine_reference_recovery import ENGINE, retained_recovery
from test_session_verification_ownership import git


def wrappers(store, snapshot, original, depth):
    current = original
    for _ in range(depth):
        current = store.prepare_handoff(snapshot, parent=original.parent, target='resume',
            goal=original.goal, reason='retained nested resume',
            payload={'resume_handoff_id': current.handoff_id})
        current.child = original.child
        store.save_handoff(current)
    return current


def incident(tmp_path, *, entry='engine', failure='valid'):
    root, child, store, snapshot, original, _, repair, old = retained_recovery(
        tmp_path, wrapped=False, failure=failure)
    child.attempt_epoch, child.attempts_since_progress, child.hard_ceiling = 10, 1, 25
    old['timestamp'] = '2026-09-16T12:31:42.785042+00:00'
    save_session_state(root, child)
    failed = wrappers(store, snapshot, original, 2)
    repair.payload['issue_seed']['failed_handoff_id'] = failed.handoff_id
    store.save_handoff(repair)
    active, route = repair, repair.payload
    if entry != 'engine':
        # This is the existing product-bound resume protocol. The engine
        # association does not import the repair request's requirement IDs.
        original.payload['issue_seed'] = {'target_repository': str(ENGINE)}
        store.save_handoff(original)
        active, route = failed, original.payload
        if entry == 'returned_resume':
            store.record_result(snapshot, active, status='blocked', result={
                'status': 'blocked', 'resolution': child.resolution,
                'diagnostic': old['diagnostic'], 'summary': old['result']})
            store.consume_result(snapshot, active, operation_id='retained-return')
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = active.handoff_id
    parent.attempt_epoch, parent.attempts_since_progress, parent.hard_ceiling = 10, 1, 25
    save_session_state(root, parent)
    snapshot.active_handoff_id = active.handoff_id
    store.save(snapshot)
    return root, child, store, snapshot, original, active, route, old


def replay(root, route, tmp_path, *, receipt='matching', session='parent', mode='collab'):
    marker = tmp_path / 'route.json'
    approved = {'route_digest': digest(route), 'engine_route': route}
    if receipt == 'mismatch':
        approved['route_digest'] = 'another-route'
    elif receipt == 'wrong_revision':
        approved['engine_commit'] = '0' * 40
    marker.write_text(json.dumps(approved))
    result = subprocess.run([sys.executable, '-B', str(ENGINE / 'src/auto_agents/session_replay.py'),
        str(ENGINE), str(root), session, mode], capture_output=True, text=True, timeout=90,
        env={**os.environ, 'AUTO_AGENTS_REPAIR_ROUTE_PROBE': str(marker),
             'AUTO_AGENTS_REPAIR_CONTROL_DISABLED': '1', 'AUTO_AGENTS_STORAGE_MAINTENANCE': 'off'})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize('entry', ['engine', 'resume', 'returned_resume', 'child'])
def test_public_multilayer_recovery_rechecks_retained_child(tmp_path, entry):
    root, child, store, _, original, active, route, old = incident(
        tmp_path, entry='engine' if entry == 'child' else entry)
    before = child.to_dict()
    returned_bytes = store.handoff_path(active.handoff_id).read_bytes()
    report = replay(root, route, tmp_path, session=child.session_id if entry == 'child' else 'parent',
                    mode='fix' if entry == 'child' else 'collab')
    assert report['ok'], report
    saved = load_session_state(root, child.session_id)
    observation = report['recovery_observation']
    assert observation['preflight_started'] and observation['preflight_rechecked']
    assert observation['diagnostic_origin'] == 'rechecked'
    assert observation['current_failure'] == {} and observation['previous_failure'] == old
    assert observation['boundary_session_id'] == child.session_id
    assert observation['original_handoff_id'] == original.handoff_id
    assert observation['diagnostic_provider_calls'] == 0
    assert len(observation['new_preflight_events']) == 2
    assert all(row['timestamp'] > old['timestamp'] for row in observation['new_preflight_events'])
    assert report['engine_runtime']['modules']['auto_agents.workflow_chain']['functions'][
        'WorkflowStore.resolve_handoff_chain']['matches_source'] is True
    for key in ('goal', 'goal_execution_environment', 'authorization_policy', 'parent_handoff_id',
                'attempt_epoch', 'attempts_since_progress', 'hard_ceiling', 'current_attempt'):
        assert getattr(saved, key) == before[key]
    parent = load_session_state(root, 'parent')
    assert (parent.attempt_epoch, parent.attempts_since_progress, parent.hard_ceiling) == (10, 1, 25)
    assert saved.execution_log[:len(before['execution_log'])] == before['execution_log']
    assert saved.verification_binding['task_scope'] == {'task_ids': ['task-owned'], 'requirement_ids': []}
    assert saved.verification_binding['requirement_ids'] == ['REQ-owned']
    assert len(list((root / '.auto-agents/state/sessions').iterdir())) == 2
    if entry == 'returned_resume':
        assert store.handoff_path(active.handoff_id).read_bytes() == returned_bytes


@pytest.mark.parametrize('failure', ['missing_reference', 'missing_proof', 'reference_only',
                                     'bad_lock', 'missing_lock', 'stale_lock', 'invalid_v2'])
def test_multilayer_new_preflight_failure_preserves_shared_work(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    root, child, store, _, _, active, route, old = incident(tmp_path, entry='resume', failure=failure)
    (root / 'foreign.py').write_bytes(b'foreign staged\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_bytes(b'foreign unstaged\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'unrelated.bin').write_bytes(b'foreign\0untracked')
    before = {path: (root / path).read_bytes() for path in ('foreign.py', 'unrelated.bin', '.git/index')}
    head, refs = git(root, 'rev-parse', 'HEAD'), git(root, 'show-ref')
    report = replay(root, route, tmp_path)
    assert not report['ok'], report
    observation = report['recovery_observation']
    assert observation['diagnostic_origin'] == 'fresh'
    assert observation['preflight_started']
    assert observation['current_failure'] and observation['previous_failure'] == old
    assert observation['diagnostic_provider_calls'] == 0
    saved = load_session_state(root, child.session_id)
    result = store.load_handoff(active.handoff_id).result
    assert saved.status == result['status'] == 'blocked'
    assert result['diagnostic'] == observation['current_failure']['diagnostic']
    assert result['changed_paths'] == result['commit_shas'] == result['rolled_back_paths'] == []
    assert result['head_after'] == '' and result['retry_fix'] is False
    if failure != 'invalid_v2':
        assert result['candidate_ownership'] == 'none'
        assert saved.verification_binding == saved.candidate_custody == {}
    assert old in saved.execution_log
    assert (saved.attempt_epoch, saved.attempts_since_progress, saved.hard_ceiling) == (10, 1, 25)
    assert {path: (root / path).read_bytes() for path in before} == before
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert git(root, 'rev-parse', 'HEAD') == head and git(root, 'show-ref') == refs


@pytest.mark.parametrize('depth', [0, 1, 2, 3, 64, 65])
def test_resume_resolution_is_bounded_and_uses_original_identity(tmp_path, depth):
    root, child, store, snapshot, original, _, _, _ = retained_recovery(tmp_path, wrapped=False)
    active = wrappers(store, snapshot, original, depth)
    coordinator = WorkflowCoordinator(Orchestrator(root))
    if depth == 65:
        with pytest.raises(SessionOwnershipError, match='maximum depth'):
            coordinator._validated_child_handoff(child, active)
    else:
        assert coordinator._validated_child_handoff(child, active).handoff_id == original.handoff_id


@pytest.mark.parametrize('conflict', ['cycle', 'missing', 'id', 'workflow', 'parent', 'child',
                                     'authorization', 'environment', 'scope', 'repository'])
def test_multilayer_conflicts_fail_before_mutating_authority(tmp_path, conflict):
    root, child, store, snapshot, original, _, _, _ = retained_recovery(tmp_path, wrapped=False)
    active = wrappers(store, snapshot, original, 2)
    middle = store.load_handoff(active.payload['resume_handoff_id'])
    if conflict == 'cycle':
        middle.payload['resume_handoff_id'] = active.handoff_id
    elif conflict == 'missing':
        middle.payload['resume_handoff_id'] = 'missing'
    elif conflict == 'id':
        payload = middle.to_dict()
        payload['handoff_id'] = 'wrong-id'
        store.handoff_path(middle.handoff_id).write_text(json.dumps(payload))
    elif conflict == 'workflow':
        middle.workflow_id = 'foreign-workflow'
    elif conflict == 'parent':
        middle.parent = WorkflowRef('collab', 'foreign-parent')
    elif conflict == 'child':
        middle.child = WorkflowRef('fix', 'foreign-child')
    else:
        key, value = {'authorization': ('authorization_policy', {'mode': 'manual'}),
                      'environment': ('goal_execution_environment', {'mode': 'mock'}),
                      'scope': ('task_ids', ['task-459', 'task-460', 'task-461']),
                      'repository': ('target_repository', str(tmp_path / 'foreign'))}[conflict]
        middle.payload[key] = value
    if conflict != 'id':
        store.save_handoff(middle)
    before = load_session_state(root, child.session_id).to_dict()
    coordinator = WorkflowCoordinator(Orchestrator(root))
    with pytest.raises(SessionOwnershipError):
        coordinator._validated_child_handoff(child, active)
    assert load_session_state(root, child.session_id).to_dict() == before
    assert store.load_handoff(original.handoff_id).child.native_id == child.session_id


@pytest.mark.parametrize('receipt', ['mismatch', 'wrong_revision'])
def test_old_preflight_marker_cannot_replace_verified_receipt(tmp_path, receipt):
    root, child, _, _, _, _, route, _ = incident(tmp_path)
    child.execution_log.append({'action': 'engine_preflight_recheck', 'route_digest': digest(route),
                                'timestamp': '2026-09-16T12:31:43+00:00'})
    save_session_state(root, child)
    before = deepcopy(child.to_dict())
    report = replay(root, route, tmp_path, receipt=receipt)
    assert not report['ok'] and not report['route_consumed']
    assert load_session_state(root, child.session_id).to_dict() == before
