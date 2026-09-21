from copy import deepcopy
import json

import pytest

from auto_agents.repair_v2.incidents import latest_failure
from auto_agents.repair_v2.scope import context, ScopeGuard, witnesses
from auto_agents.repair_v2.store import atomic_json, Store
from auto_agents.repair_v2.transaction import transaction_root, frozen_request
from auto_agents.repair_v2.types import Acceptance, RepairRequest, RepairBlocked


@pytest.fixture
def scene(tmp_path):
    project, engine = tmp_path / 'project', tmp_path / 'engine'
    engine.mkdir(); (engine / 'current.py').write_text('version = 2\n')
    base = project / '.auto-agents/state'
    parent = {'session_id': 'parent', 'mode': 'collab', 'workflow_id': 'wf', 'goal': 'Make the original video'}
    child = {'session_id': 'child', 'mode': 'fix', 'workflow_id': 'wf', 'parent_handoff_id': 'original',
             'execution_log': [{'action': 'execution_preflight_blocked', 'result': 'old failure'}]}
    atomic_json(base / 'sessions/parent/session_state.json', parent)
    atomic_json(base / 'sessions/child/session_state.json', child)
    atomic_json(base / 'workflows/wf/workflow.json', {'root': {'kind': 'collab', 'native_id': 'parent'}})
    atomic_json(base / 'handoffs/original.json', {'workflow_id': 'wf', 'child': {'kind': 'fix', 'native_id': 'child'}})
    atomic_json(base / 'handoffs/returned.json', {'workflow_id': 'wf', 'child': None,
        'result': {'status': 'failed', 'diagnostic': {'session_id': 'child'}}})
    payload = {'project': str(project), 'base': 'base', 'autonomy': 'max', 'invocation': {
        'session_id': 'parent', 'workflow_id': 'wf', 'engine_route': {'issue_seed': {
            'failed_handoff_id': 'original', 'original_handoff_id': 'original', 'child_session_id': 'child'}}}}
    return project, engine, child, payload


def test_successful_preflight_is_not_a_failure_and_later_verify_is_current(scene):
    project, _, child, payload = scene
    child['execution_log'].append({'action': 'engine_preflight_recheck'})
    assert latest_failure(child) is None
    failure = {'action': 'verify', 'result': 'changed proof', 'failure_kind': 'verification_ownership',
               'diagnostic': {'verification_ref': 'tests/test_value.py'}}
    child['execution_log'].append(failure)
    atomic_json(project / '.auto-agents/state/sessions/child/session_state.json', child)
    found = context(project, payload)
    assert found['observation']['failure'] == failure
    assert found['incident']['phase'] == 'verification'
    assert found['incident']['event']['pointer'] == '/execution_log/2'
    wrapped = deepcopy(payload)
    wrapped['invocation']['engine_route']['issue_seed']['failed_handoff_id'] = 'returned'
    assert context(project, wrapped) == found


def test_new_failure_does_not_reuse_old_transaction_but_restart_does(scene, tmp_path):
    project, engine, child, payload = scene
    proposal = {'decision': 'required', 'blocked_step': 'video', 'consequence': 'blocked',
                'recovery_check': 'resume', 'evidence_refs': ['current.py']}
    config = {'root': str(tmp_path / 'control')}
    def admit(payload, name):
        guard = ScopeGuard(tmp_path / name, payload, project, engine)
        payload = {**payload, 'scope_receipt': guard.store.read(guard.admit(proposal))}
        root = transaction_root(config, payload)
        request = frozen_request(root, payload, lambda: RepairRequest(root.name, 'base', name,
            (Acceptance('video', name),), 'fake'))
        Store(root).save({'status': 'blocked', 'calls': 9, 'attempts': 3})
        return payload, root, request
    old, old_root, old_request = admit(payload, 'preflight')
    child['execution_log'] += [{'action': 'engine_preflight_recheck'},
                              {'action': 'verify', 'result': 'proof changed', 'failure_kind': 'verification_ownership'}]
    atomic_json(project / '.auto-agents/state/sessions/child/session_state.json', child)
    current, root, request = admit(payload, 'verification')
    assert root != old_root and request.incident_id != old_request.incident_id
    assert transaction_root(config, {**current, 'provider': 'another', 'base': 'upgrade'}) == root
    assert json.loads((old_root / 'request.json').read_text()) == json.loads(json.dumps(old_request.to_dict()))
    assert Store(old_root).load()['calls'] == 9


def test_explicit_event_evidence_reads_large_log_without_relaxing_file_limit(scene):
    project, engine, child, _ = scene
    path = project / '.auto-agents/state/sessions/child/session_state.json'
    child['conversation'] = [{'content': 'x' * (5 * 1024 * 1024)}]
    atomic_json(path, child)
    relative = str(path.relative_to(project))
    with pytest.raises(RepairBlocked): witnesses([relative], project, engine)
    row = witnesses([{'origin': 'target', 'path': relative, 'pointer': '/execution_log/0'}], project, engine)[0]
    assert row['pointer'] == '/execution_log/0'
    assert witnesses([row], project, engine) == [row]
    child['execution_log'][0]['result'] = 'different'
    atomic_json(path, child)
    with pytest.raises(RepairBlocked): witnesses([row], project, engine)


def test_evidence_never_silently_selects_a_different_repository(scene):
    project, engine, _, _ = scene
    (project / 'current.py').write_text('version = 1\n')
    with pytest.raises(RepairBlocked): witnesses(['current.py'], project, engine)
    ref = witnesses([{'origin': 'source', 'path': 'current.py'}], project, engine)[0]
    assert ref['origin'] == 'source'
    assert witnesses([ref], project, engine) == [ref]


def test_same_unresolved_failure_reuses_necessity_but_new_occurrence_does_not(scene, tmp_path):
    project, engine, child, payload = scene
    proposal = {'decision': 'required', 'blocked_step': 'video', 'consequence': 'blocked',
                'recovery_check': 'resume', 'evidence_refs': ['current.py']}
    guard = ScopeGuard(tmp_path / 'guard', payload, project, engine)
    first = guard.store.read(guard.admit(proposal))
    child['execution_log'].append({**child['execution_log'][0], 'timestamp': 'later', 'attempt': 7})
    path = project / '.auto-agents/state/sessions/child/session_state.json'
    atomic_json(path, child)
    repeated = ScopeGuard(tmp_path / 'guard', payload, project, engine)
    assert repeated.context['incident']['identity'] == guard.context['incident']['identity']
    assert repeated.context['incident']['revision'] == 2
    assert repeated.current() and repeated.import_receipt(first)
    child['execution_log'] += [{'action': 'engine_preflight_recheck'}, deepcopy(child['execution_log'][0])]
    atomic_json(path, child)
    later = ScopeGuard(tmp_path / 'guard', payload, project, engine)
    assert later.context['incident']['identity'] != guard.context['incident']['identity']
    assert later.current() is None


def test_migration_only_supersedes_the_exact_proved_resolved_failure(scene, tmp_path):
    import shutil
    from auto_agents.repair_v2.incidents import migrate
    project, _, child, payload = scene
    control = tmp_path / 'control'
    roots = [control / 'v2-transactions' / name for name in ('old', 'resolved', 'different')]
    for path in roots[:2]:
        atomic_json(path / 'original-payload.json', payload)
        shutil.copytree(project, path / 'target-evidence')
        Store(path).save({'status': 'blocked', 'calls': 4, 'attempts': 2})
    proof = {'job': 'recovery', 'generation': 1, 'artifact_id': 'artifact', 'operation': 'operation',
             'boundary': {'session_id': 'child', 'original_handoff_id': 'original'}}
    Store(roots[1]).save({'status': 'complete', 'phase': 'recovered', 'calls': 8, 'attempts': 3,
        'live_recovery': proof, 'recovery_owner': {k: proof[k] for k in ('job', 'generation', 'artifact_id')},
        'recovery_operation': 'operation'})
    child['execution_log'] += [{'action': 'engine_preflight_recheck'},
                              {'action': 'verify', 'failure_kind': 'verification_ownership', 'result': 'new proof failure'}]
    atomic_json(project / '.auto-agents/state/sessions/child/session_state.json', child)
    atomic_json(roots[2] / 'original-payload.json', payload)
    shutil.copytree(project, roots[2] / 'target-evidence')
    Store(roots[2]).save({'status': 'blocked', 'calls': 2, 'attempts': 0})
    protected = {p: p.read_bytes() for root in roots for p in root.glob('*.json')}
    result = migrate({'root': str(control)}, payload)
    assert result['transactions']['old']['status'] == 'superseded'
    assert result['transactions']['resolved']['status'] == 'resolved'
    assert result['transactions']['different']['status'] == 'open'
    assert migrate({'root': str(control)}, payload) == result
    assert {p: p.read_bytes() for p in protected} == protected


def test_proof_review_consumption_is_idempotent_and_not_an_implementation(tmp_path):
    from auto_agents.repair_v2.chain import RepairChain
    config = {'root': str(tmp_path / 'control')}
    payload = {'project': str(tmp_path / 'project'), 'invocation': {'session_id': 'root'}}
    chain = RepairChain(config, payload, tmp_path / 'transaction')
    chain.admit()
    chain.reserve_review('review', 1)
    chain.reserve_review('review', 1)
    assert chain.context()['used'] == {'model_calls': 1, 'implementations': 0, 'transactions': 1}


def test_product_test_failure_is_not_permission_to_change_engine(scene, tmp_path):
    project, engine, child, payload = scene
    child['execution_log'].append({'action': 'verify', 'result': 'assert 1 == 2', 'failure_kind': 'owned_verification_failed'})
    atomic_json(project / '.auto-agents/state/sessions/child/session_state.json', child)
    guard = ScopeGuard(tmp_path / 'guard', payload, project, engine)
    with pytest.raises(RepairBlocked) as denied:
        guard.admit({'decision': 'required', 'blocked_step': 'video', 'consequence': 'blocked',
                     'recovery_check': 'resume', 'evidence_refs': ['current.py']})
    assert denied.value.code == 'scope_failure_owner'


@pytest.mark.parametrize('damage', ['foreign', 'conflict', 'cycle'])
def test_conflicting_or_cyclic_failure_ownership_stops(scene, damage):
    project, _, child, payload = scene
    if damage == 'foreign':
        child['workflow_id'] = 'foreign'
        atomic_json(project / '.auto-agents/state/sessions/child/session_state.json', child)
    elif damage == 'conflict':
        payload['invocation']['engine_route']['issue_seed']['child_session_id'] = 'other'
    else:
        atomic_json(project / '.auto-agents/state/handoffs/original.json', {'workflow_id': 'wf',
                    'payload': {'resume_handoff_id': 'original'}})
    with pytest.raises(RepairBlocked): context(project, payload)
