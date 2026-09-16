"""Reference roles and pre-implementation returns through retained handoffs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from auto_agents.config import (
    load_project_config, load_session_state, load_task_plan, requirements_trace_path,
    save_session_state,
)
from auto_agents.git_ops import head_ref
from auto_agents.models import GateConfig, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.session_verification import (
    SessionOwnershipError, _reference_kind, bind_session, selected_requirement_contracts,
)
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_engine_child_recovery import parent_workflow, resume_to_observation
from test_session_verification_ownership import project, git, run_session, _retain_contract, _binding_fixture


REFERENCE = '.auto-agents/docs/provider_references/aliyun_oss_object_storage.md'


def reference_project(tmp_path, *, location='task', reference=REFERENCE, proof='owned.contract', missing=False):
    root, child = project(tmp_path)
    if not missing:
        path = root / reference
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'# Retained official reference\n')
    plan, config = load_task_plan(root), load_project_config(root)
    refs = [reference, proof] if proof else [reference]
    task = plan['tasks'][0]
    task['verification_refs'] = refs if location == 'task' else []
    if location == 'requirement':
        task['requirement_proofs'] = [{'requirement_id': 'REQ-owned', 'evidence_refs': refs}]
    requirements_trace_path(root).write_text(json.dumps({'requirements': [{
        'id': 'REQ-owned', 'text': 'Retain source semantics', 'provider_reference': reference,
    }]}))
    _retain_contract(root, child, config, plan)
    return root, child


@pytest.mark.parametrize('location,reference', [
    ('task', REFERENCE), ('requirement', REFERENCE), ('requirement', 'docs/provider.reference'),
])
def test_public_reference_and_executable_evidence_remain_distinct(tmp_path, monkeypatch, location, reference):
    root, child = reference_project(tmp_path, location=location, reference=reference)
    original = (root / reference).read_bytes()
    # The unrelated shared copy is not the retained candidate's evidence.
    (root / reference).write_bytes(b'Another workflow is editing this reference\n')
    ambient = (root / reference).read_bytes()
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    binding = saved.verification_binding
    assert binding['required_proof_ids'] == ['owned.contract']
    assert set(binding['required_references']) == {reference, 'owned.contract'}
    record = binding['required_references'][reference]
    assert record['kind'] == 'artifact' and record['role'] == 'reference'
    assert record['sha256'] == hashlib.sha256(original).hexdigest()
    assert (root / reference).read_bytes() == ambient
    assert (Path(saved.candidate_custody['checkout']) / reference).read_bytes() == original


@pytest.mark.parametrize('reference', ['owned.report.json', 'owned.report.md'])
def test_extensions_never_exempt_an_unresolved_proof(reference):
    assert _reference_kind(reference, GateConfig(), source_exists=lambda _: True) == 'proof'
    gates = GateConfig(steps=[VerificationStep(proof_id=reference, targets=['tests/test_owned.py'])])
    assert _reference_kind(reference, gates, reference_paths=[reference]) == 'proof'


@pytest.mark.parametrize('failure', ['missing_reference', 'missing_proof', 'reference_only'])
def test_reference_roles_cannot_supply_missing_executable_proofs(tmp_path, monkeypatch, failure):
    root, _ = reference_project(tmp_path, missing=failure == 'missing_reference',
        proof='missing.report.json' if failure == 'missing_proof' else '' if failure == 'reference_only' else 'owned.contract')
    def forbidden(*args, **kwargs):
        pytest.fail('Admission must reject before baseline, cache lookup or writer execution')
    monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'blocked' and calls == []
    assert saved.verification_binding == {} and saved.candidate_custody == {}
    entry = saved.execution_log[-1]
    assert entry['retry_fix'] is False
    if failure == 'missing_reference':
        assert 'reference artifact is unavailable' in entry['result']
        assert entry['diagnostic']['verification_ref'] == REFERENCE
    elif failure == 'missing_proof':
        assert entry['diagnostic']['verification_ref'] == 'missing.report.json'
    else:
        assert 'no executable verification evidence' in entry['result']


def test_candidate_cannot_replace_reference_to_make_its_proof_pass(tmp_path, monkeypatch):
    root, child = reference_project(tmp_path)
    store, _, handoff = parent_workflow(root, child)
    original = (root / REFERENCE).read_bytes()
    def writer(state, prompt, checkout):
        (checkout / 'value.py').write_text('VALUE = 1\n')
        (checkout / REFERENCE).write_text('A forged official reference\n')
        return 'Candidate ready'
    resume_to_observation(root, monkeypatch, writer)
    saved = load_session_state(root, child.session_id)
    returned = store.load_handoff(handoff.handoff_id).result
    assert saved.status == returned['status'] == 'blocked'
    assert returned['diagnostic']['verification_ref'] == REFERENCE
    assert 'reference artifact changed' in returned['summary']
    assert returned['rolled_back_paths'] == []
    assert (root / REFERENCE).read_bytes() == original
    assert saved.candidate_custody['receipt']['manifest']


@pytest.mark.parametrize('route', ['fix', 'resume'])
@pytest.mark.parametrize('advance_head', [False, True])
def test_zero_candidate_preflight_preserves_dirty_workspace_and_first_diagnostic(tmp_path, monkeypatch, route, advance_head):
    root, child = reference_project(tmp_path, proof='missing.report.json')
    store, snapshot, original = parent_workflow(root, child)
    coordinator = WorkflowCoordinator(Orchestrator(root))
    coordinator._ensure_handoff_checkpoint(snapshot, original)
    if advance_head:
        (root / 'foreign-committed.txt').write_text('Another workflow delivered this\n')
        git(root, 'add', 'foreign-committed.txt')
        git(root, 'commit', '-m', 'unrelated delivery after child checkpoint')
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'foreign\0untracked')
    if route == 'resume':
        from test_session_verification_ownership import _prepare_binding_child_resume
        _prepare_binding_child_resume(root, store, snapshot, original)
    parent = load_session_state(root, 'parent')
    parent.lineage_head_ref = child.baseline_head_ref
    save_session_state(root, parent)
    active = parent.active_handoff_id
    before = {path: (root / path).read_bytes() for path in
              ('foreign.py', 'foreign-note.txt', '.git/index')}
    refs, head = git(root, 'show-ref'), head_ref(root)
    def forbidden(*args, **kwargs):
        pytest.fail('A proved pre-implementation failure must not invoke rollback or a child writer')
    from test_engine_child_recovery import ObservationBoundary
    def observe_parent(self, state):
        assert state.session_id == 'parent'
        raise ObservationBoundary()
    monkeypatch.setattr(Session, '_phase_collab_loop', observe_parent)
    monkeypatch.setattr(WorkflowCoordinator, '_rollback_handoff_uncommitted', forbidden)
    resume_to_observation(root, monkeypatch, forbidden)
    saved = load_session_state(root, child.session_id)
    result = store.load_handoff(active).result
    entry = saved.execution_log[-1]
    assert result['status'] == 'blocked'
    assert result['candidate_ownership'] == 'none'
    assert result['changed_paths'] == result['commit_shas'] == result['rolled_back_paths'] == []
    assert result['head_after'] == ''
    assert result['summary'] == entry['result']
    assert result['diagnostic'] == entry['diagnostic']
    assert result['diagnostic']['verification_ref'] == 'missing.report.json'
    assert result['retry_fix'] is False and 'rollback_diagnostic' not in result
    assert load_session_state(root, 'parent').lineage_changed_paths == parent.lineage_changed_paths
    assert load_session_state(root, 'parent').lineage_head_ref == parent.lineage_head_ref
    assert {path: (root / path).read_bytes() for path in before} == before
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert head_ref(root) == head and git(root, 'show-ref') == refs
    resume_to_observation(root, monkeypatch, forbidden)
    assert store.load_handoff(active).result == result


@pytest.mark.parametrize('ambiguity', ['no_preflight', 'candidate', 'prior_writer', 'wrong_child', 'custody'])
def test_empty_binding_never_authorizes_unknown_candidate_rollback(tmp_path, ambiguity):
    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    child.status, child.resolution = 'blocked', 'verification_ownership'
    child.execution_log = [{'action': 'execution_preflight_blocked', 'result': 'original failure',
                            'failure_kind': child.resolution, 'retry_fix': False,
                            'diagnostic': {'verification_ref': 'missing.proof'}}]
    if ambiguity == 'no_preflight':
        child.execution_log = []
    elif ambiguity == 'candidate':
        child.candidate_paths = {'value.py': 'unattributed'}
    elif ambiguity == 'prior_writer':
        child.execution_log.insert(0, {'action': 'fix', 'attempt': 1})
    elif ambiguity == 'wrong_child':
        handoff.payload['child_session_id'] = 'another-child'
        store.save_handoff(handoff)
    else:
        child.candidate_custody = {'checkout': str(root)}
    save_session_state(root, child)
    (root / 'value.py').write_bytes(b'foreign content\n')
    coordinator = WorkflowCoordinator(Orchestrator(root))
    before = (root / 'value.py').read_bytes()
    with pytest.raises(SessionOwnershipError):
        coordinator._rollback_handoff_uncommitted(snapshot, handoff)
    result = coordinator._session_result(child, handoff)
    coordinator._finish_failed_handoff(snapshot, handoff, result)
    assert result['status'] == 'blocked' and result['candidate_ownership'] == 'unknown'
    assert result['rolled_back_paths'] == [] and result['rollback_diagnostic']['retry_fix'] is False
    if ambiguity != 'no_preflight':
        assert result['summary'] == 'original failure'
        assert result['diagnostic'] == {'verification_ref': 'missing.proof'}
    assert (root / 'value.py').read_bytes() == before


def test_resume_retains_attempt_evidence_before_resetting_local_counter(tmp_path, monkeypatch):
    from test_engine_child_recovery import ObservationBoundary
    root, child = reference_project(tmp_path, proof='missing.report.json')
    child.current_attempt = 1
    assert child.execution_log == [] and child.candidate_custody == {}
    store, _, handoff = parent_workflow(root, child)
    def forbidden(*args, **kwargs):
        pytest.fail('Unknown historical writer ownership cannot authorize rollback or another writer')
    def observe_parent(self, state):
        assert state.session_id == 'parent'
        raise ObservationBoundary()
    monkeypatch.setattr(Session, '_phase_collab_loop', observe_parent)
    monkeypatch.setattr(WorkflowCoordinator, '_rollback_handoff_uncommitted', forbidden)
    resume_to_observation(root, monkeypatch, forbidden)
    saved = load_session_state(root, child.session_id)
    result = store.load_handoff(handoff.handoff_id).result
    assert saved.current_attempt == 0
    assert any(entry['action'] == 'implementation_attempts_retained' and entry['attempt'] == 1
               for entry in saved.execution_log)
    assert result['status'] == 'blocked' and result['candidate_ownership'] == 'unknown'
    assert result['diagnostic']['verification_ref'] == 'missing.report.json'
    assert result['rolled_back_paths'] == [] and result['rollback_diagnostic']['retry_fix'] is False


def test_dependency_owner_summary_is_not_task_authority(tmp_path):
    from test_session_verification_ownership import _retain_foreign_prerequisite
    root, child = project(tmp_path)
    _retain_foreign_prerequisite(root, child)
    session = _binding_fixture(root, child)
    binding = child.verification_binding
    assert binding['task_ids'] == ['task-foreign', 'task-owned']
    assert binding['task_scope'] == {'task_ids': ['task-owned'], 'requirement_ids': []}
    # Give both tasks contract records without altering their proof identities.
    for task in binding['tasks']:
        task['requirement_proofs'] = [{'requirement_id': task['requirement_ids'][0],
                                      'evidence_refs': task['verification_refs']}]
    selected = list(selected_requirement_contracts(session, child, []))
    assert [task['task_id'] for task, _, _ in selected] == ['task-owned']
    selected = list(selected_requirement_contracts(session, child, ['shared-command'],
                    metadata={'shared-command': {'proof_ids': ['shared.setup']}}))
    assert {task['task_id'] for task, _, _ in selected} == {'task-owned', 'task-foreign'}


@pytest.mark.parametrize('receipt_state', ['valid', 'missing', 'conflicting'])
def test_private_candidate_receipt_never_authorizes_shared_copyback(tmp_path, receipt_state):
    from auto_agents.gate_execution import GateSnapshotManager
    from auto_agents.session_candidate import execution_checkout, _inventory, record_receipt
    from auto_agents.session_verification import fingerprint

    root, child = project(tmp_path)
    _, snapshot, handoff = parent_workflow(root, child)
    session = _binding_fixture(root, child)
    # Construct a real, frozen private candidate as fixture input to rollback.
    # No writer execution or confinement policy is replaced by this fixture.
    with execution_checkout(session, child):
        child.current_attempt = 1
        (session.project_root / 'value.py').write_text('VALUE = 1\n')
        postimage = _inventory(session.project_root)['value.py']
        frozen = GateSnapshotManager(session.project_root, 'exit-receipt').create(paths=['value.py'])
        receipt = {'attempt_id': 'fixture-writer', 'attempt': 1, 'session_id': child.session_id,
                   'binding_fingerprint': child.verification_binding['binding_fingerprint'],
                   'base_revision': child.candidate_custody['base_revision'],
                   'source_revision': frozen.commit_sha,
                   'manifest': {'value.py': {'preimage': child.candidate_custody['preimages']['value.py'],
                                             'postimage': postimage}}}
        receipt['fingerprint'] = fingerprint(receipt)
        session._candidate_receipt = receipt
        record_receipt(session, child)
    child.status, child.resolution = 'failed', 'verification_inconclusive'
    if receipt_state == 'missing':
        child.candidate_custody.pop('receipt')
    elif receipt_state == 'conflicting':
        child.candidate_custody['receipt']['session_id'] = 'another-writer'
    save_session_state(root, child)
    (root / 'value.py').write_text('VALUE = 88\n')
    git(root, 'add', 'value.py')
    (root / 'value.py').write_text('VALUE = 99\n')
    (root / 'value.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'unrelated\0content')
    protected = {name: (root / name).read_bytes() for name in ('value.py', 'foreign-note.txt', '.git/index')}
    head, refs = head_ref(root), git(root, 'show-ref')
    retained = deepcopy(child.candidate_custody)
    coordinator = WorkflowCoordinator(Orchestrator(root))
    if receipt_state == 'valid':
        assert coordinator._rollback_handoff_uncommitted(snapshot, handoff) == []
        result = coordinator._session_result(child, handoff)
        assert result['candidate_ownership'] == 'private'
        assert result['changed_paths'] == ['value.py']
    else:
        with pytest.raises(SessionOwnershipError):
            coordinator._rollback_handoff_uncommitted(snapshot, handoff)
    assert child.candidate_custody == retained
    assert (Path(retained['checkout']) / 'value.py').read_text() == 'VALUE = 1\n'
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert (root / 'value.py').stat().st_mode & 0o777 == 0o711
    assert head_ref(root) == head and git(root, 'show-ref') == refs


@pytest.mark.parametrize('mutation', ['intact', 'changed', 'missing'])
def test_reference_integrity_is_bound_before_any_writer(tmp_path, mutation):
    from auto_agents.session_candidate import execution_checkout
    from auto_agents.session_verification import validate_binding
    root, child = reference_project(tmp_path)
    session = _binding_fixture(root, child)
    original = (root / REFERENCE).read_bytes()
    assert child.verification_binding['required_references'][REFERENCE]['sha256'] == hashlib.sha256(original).hexdigest()
    with execution_checkout(session, child):
        path = session.project_root / REFERENCE
        if mutation == 'changed':
            path.write_bytes(b'replacement provenance\n')
        elif mutation == 'missing':
            path.unlink()
        if mutation == 'intact':
            validate_binding(session, child)
        else:
            with pytest.raises(SessionOwnershipError, match='reference artifact changed'):
                validate_binding(session, child)
    assert (root / REFERENCE).read_bytes() == original


@pytest.mark.parametrize('conflict', [False, True])
def test_reference_role_migration_is_atomic_and_preserves_authority(tmp_path, conflict):
    from auto_agents.session_verification import fingerprint
    root, child = reference_project(tmp_path)
    session = _binding_fixture(root, child)
    binding = child.verification_binding
    binding.pop('reference_role_version')
    if conflict:
        binding['required_references'][REFERENCE]['sha256'] = '0' * 64
    else:
        binding['required_references'][REFERENCE] = {'kind': 'artifact', 'owners': []}
    binding['binding_fingerprint'] = fingerprint({key: value for key, value in binding.items()
                                                if key != 'binding_fingerprint'})
    retained = deepcopy(binding)
    if conflict:
        with pytest.raises(SessionOwnershipError, match='reference artifact conflicts'):
            bind_session(session, child)
        assert child.verification_binding == retained
    else:
        bind_session(session, child)
        assert child.verification_binding['reference_role_version'] == 1
        assert child.verification_binding['required_references'][REFERENCE]['role'] == 'reference'
        for key in ('task_scope', 'tasks', 'plan', 'authorization', 'contract_revision',
                    'schema_version', 'proof_inventory_version'):
            assert child.verification_binding[key] == retained[key]


@pytest.mark.parametrize('receipt', ['matching', 'mismatch', 'missing'])
@pytest.mark.parametrize('proof', ['owned.contract', 'missing.report.json'])
@pytest.mark.parametrize('route_reference', ['child', 'handoff'])
def test_engine_return_rechecks_blocked_child_without_resetting_budget(tmp_path, monkeypatch, receipt, proof, route_reference):
    from auto_agents.repair_control import digest
    root, child = reference_project(tmp_path, proof=proof)
    store, snapshot, handoff = parent_workflow(root, child, engine=True)
    child = load_session_state(root, child.session_id)
    if route_reference == 'handoff':
        handoff.payload.pop('child_session_id')
        handoff.payload['issue_seed'].update(failed_handoff_id=child.parent_handoff_id, evidence_base=str(root))
        store.save_handoff(handoff)
    child.status, child.resolution = 'blocked', 'verification_ownership'
    failure = {'action': 'execution_preflight_blocked', 'result': 'legacy reference classification failure',
               'failure_kind': child.resolution, 'retry_fix': False,
               'diagnostic': {'verification_ref': REFERENCE}}
    child.execution_log.append(failure)
    save_session_state(root, child)
    expected = {key: deepcopy(getattr(child, key)) for key in (
        'session_id', 'parent_handoff_id', 'workflow_id', 'goal', 'goal_execution_environment',
        'authorization_policy', 'current_attempt', 'attempt_epoch', 'attempts_since_progress',
        'hard_ceiling', 'max_attempts', 'conversation')}
    if receipt != 'missing':
        probe = tmp_path / 'verified-route.json'
        probe.write_text(json.dumps({'route_digest': digest(handoff.payload) if receipt == 'matching' else 'another-route'}))
        monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(probe))
    dispatched = []
    drive = WorkflowCoordinator._drive_session
    def observe(self, session, state, workflow, *, root):
        if state.session_id != child.session_id:
            return drive(self, session, state, workflow, root=root)
        dispatched.append(state.session_id)
        assert state.status == 'executing'
        assert {key: getattr(state, key) for key in expected} == expected
        assert failure in state.execution_log
        state.status = 'paused'
        save_session_state(self.project_root, state)
        return state
    def forbidden(*args, **kwargs):
        pytest.fail('This observation ends before the child writer or shared rollback')
    monkeypatch.setattr(WorkflowCoordinator, '_drive_session', observe)
    monkeypatch.setattr(WorkflowCoordinator, '_rollback_handoff_uncommitted', forbidden)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', forbidden)
    from test_engine_child_recovery import ObservationBoundary
    def parent_boundary(self, state):
        assert state.session_id == 'parent'
        raise ObservationBoundary()
    monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)
    try:
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    except ObservationBoundary:
        pass
    saved = load_session_state(root, child.session_id)
    reopened = receipt == 'matching' and proof == 'owned.contract'
    assert dispatched == ([child.session_id] if reopened else [])
    assert saved.status == ('paused' if reopened else 'blocked')
    assert {key: getattr(saved, key) for key in expected} == expected
    assert failure in saved.execution_log
    assert len([row for row in saved.execution_log if row['action'] == 'engine_preflight_recheck']) == int(reopened)
