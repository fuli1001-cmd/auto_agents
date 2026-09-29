"""Unexpected necessary product paths remain owned and require independent review."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json

import pytest

from auto_agents.recovery import KernelError, OutcomeKind
from auto_agents.recovery.scope_amendments import approval, pending, product_paths, recover_writer, _receipt_source
from test_recovery_convergence_policy import scene, operation, verify
from test_recovery_kernel import finish, success, emit


def test_amendment_is_atomic_with_writer_and_needs_complete_independent_approval(scene):
    store, contract = scene
    command = operation(store, contract, 'implement', 'writer', model=True)
    outcome = replace(success(store, command), details={'scope_amendment_required': ['boundary.py'],
        'native_result': store.put({'ok': True}), 'post_source': command.source})
    finish(store, command, outcome)
    assert pending(store.replay('workflow'), 'fix') == ['boundary.py']
    with pytest.raises(KernelError, match='independent approval'):
        operation(store, contract, 'deliver', 'premature-delivery')
    verify(store, contract, 'verified', {'test': 'passed'}, ok=True)
    reviewer = operation(store, contract, 'review', 'reviewer', model=True)
    emit(store, 'command_dispatched', {'command_id': reviewer.command_id, 'epoch': 1, 'owner': 'worker'})
    before = store.replay('workflow')
    result = success(store, reviewer)
    event = {'command_id': reviewer.command_id, 'epoch': 1, 'owner': 'worker', 'outcome': result.to_dict()}
    with pytest.raises(KernelError): emit(store, 'command_finished', event)
    assert store.replay('workflow') == before
    coverage = [{'path': 'boundary.py', 'reason': 'Preserves outbound field', 'evidence': 'test_boundary passed'}]
    grant = approval({'decision': 'APPROVE', 'scope_coverage': coverage}, ['boundary.py'], reviewer.source)
    event['outcome'] = replace(result, details={'scope_approval': grant}).to_dict()
    emit(store, 'command_finished', event)
    after = store.replay('workflow')
    assert pending(after, 'fix') == []
    assert after['budget'] == before['budget']
    from auto_agents.recovery.convergence import scope
    assert scope(after, 'fix')['scope_approvals'][0]['coverage'] == coverage


@pytest.mark.parametrize('path', ['../outside.py', '/tmp/outside', '.git/config', '.auto-agents/state/x',
    '.codex/config.toml', '.env', 'dir/AGENTS.md'])
def test_protected_paths_cannot_be_scope_amendments(path):
    assert not product_paths([path])


@pytest.mark.parametrize('coverage', [[], [{'path': 'a', 'reason': '', 'evidence': 'test'}],
    [{'path': 'wrong', 'reason': 'needed', 'evidence': 'test'}]])
def test_missing_or_unrelated_necessity_evidence_is_rejected(coverage):
    with pytest.raises(KernelError):
        approval({'decision': 'APPROVE', 'scope_coverage': coverage}, ['a'], 'a'*64)


def retained_writer(tmp_path, monkeypatch):
    from test_retained_candidate_resume import stopped_candidate
    from auto_agents.recovery import native, policy
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.config import save_session_state
    from auto_agents.models import AgentResult
    from auto_agents.recovery.convergence import scope, failures
    root, store, child, calls = stopped_candidate(tmp_path, monkeypatch, candidate_value=-1,
        stop_kind='kernel_ownership_conflict')
    stream = store.binding(root, 'session:' + child.session_id)
    policy.enable(store, stream)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    session._current_state = child
    session._custody_control_root = root
    session.project_root = Path(child.candidate_custody['checkout'])
    session.orch._kernel_project = root
    session.orch.project_root = session.project_root
    session.orch._current_state = child
    native.perform(session, 'verify', 'observed', lambda: {'ok': False,
        'verification_checks': [{'id': 'tests/test_owned.py::test_owned', 'status': 'failed'}]},
        lambda r: (OutcomeKind.CANDIDATE_REJECTED, 'Retained failure'))
    item = scope(store.load(stream), 'fix:' + child.session_id)
    text = json.dumps({'observation': item['latest'], 'hypothesis': 'Fix value and its output projection',
        'failure_ids': failures(item), 'paths': ['value.py'], 'expected_result': 'Owned value passes'})
    native.perform(session, 'diagnose', 'diagnose', lambda: {'summary': text},
        lambda r: (OutcomeKind.SUCCESS, 'Diagnosis'), model=True)
    assert _receipt_source(child) == native._source(session, child)
    def writer():
        (session.project_root/'value.py').write_text('VALUE = 1\n')
        (session.project_root/'boundary.py').write_text('from value import VALUE\n')
        return AgentResult(True, [], tmp_path/'reply', summary='Fixed\nCOMMIT_MESSAGE: Fix boundary')
    with monkeypatch.context() as patch:
        # Reproduce the old adapter's completed writer, without sealing a new receipt.
        patch.setattr(policy, 'correction_paths', lambda *args: [])
        with pytest.raises(KernelError):
            native.perform(session, 'implement', 'old-writer', writer,
                lambda r: (OutcomeKind.OWNERSHIP_CONFLICT, 'Correction exceeded approved paths: boundary.py'), model=True)
    save_session_state(root, child)
    return root, store, stream, child, session.project_root


def test_completed_legacy_writer_recovers_exact_bytes_without_spending_or_retrying(tmp_path, monkeypatch):
    root, store, stream, child, checkout = retained_writer(tmp_path, monkeypatch)
    before = deepcopy(store.load(stream)['budget'])
    original = deepcopy(child.candidate_custody['receipt'])
    attempt = child.current_attempt
    assert recover_writer(root, child)
    assert child.current_attempt == attempt
    assert store.replay(stream)['budget'] == before
    assert pending(store.load(stream), 'fix:' + child.session_id) == ['boundary.py']
    receipt = child.candidate_custody['receipt']
    assert receipt != original
    assert any(row.get('receipt') == original for row in child.execution_log if row['action'] == 'candidate_superseded')
    assert (checkout/'boundary.py').read_text() == 'from value import VALUE\n'
    assert not (root/'boundary.py').exists()
    assert recover_writer(root, child)
    assert child.candidate_custody['receipt'] == receipt
    assert store.replay(stream)['budget'] == before


def test_changed_retained_writer_bytes_cannot_be_adopted(tmp_path, monkeypatch):
    root, store, stream, child, checkout = retained_writer(tmp_path, monkeypatch)
    before = deepcopy(store.load(stream))
    (checkout/'boundary.py').write_text('unowned change\n')
    with pytest.raises(KernelError, match='Private bytes changed'): recover_writer(root, child)
    assert store.replay(stream) == before


def test_parent_resume_recovers_completed_writer_then_verifies_reviews_and_delivers(tmp_path, monkeypatch):
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.session import Session
    from auto_agents.config import load_session_state
    from test_engine_child_recovery import ObservationBoundary
    root, store, stream, child, checkout = retained_writer(tmp_path, monkeypatch)
    before = deepcopy(store.load(stream)['budget'])
    original = Orchestrator._call_with_failover_owned
    calls = []
    def transport(self, request):
        calls.append(request.purpose)
        result = original(self, request)
        if request.purpose == 'review':
            assert 'scope_coverage' in request.response_schema['required']
            value = json.loads(result.summary)
            value['scope_coverage'] = [{'path': 'boundary.py', 'reason': 'Publishes repaired value',
                                       'evidence': 'boundary.py imports VALUE'}]
            result.summary = result.stdout = json.dumps(value)
        return result
    monkeypatch.setattr(Orchestrator, '_call_with_failover_owned', transport)
    store.set_meta('active_runtime', {'source': 'a'*64})
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'completed', saved.resolution
    assert calls == ['review', 'collab']
    after = store.replay(stream)
    assert after['budget']['implementations'] == before['implementations']
    assert not pending(after, 'fix:' + child.session_id)
    assert saved.current_attempt == child.current_attempt


def test_diagnostic_copy_omits_atomic_control_temps_but_keeps_product_files(tmp_path, monkeypatch):
    from auto_agents.root_cause import RootCauseCoordinator
    for name in ('WSL_DISTRO_NAME', 'WSL_INTEROP'): monkeypatch.delenv(name, raising=False)
    root = tmp_path/'project'
    state = root/'.auto-agents/state'
    state.mkdir(parents=True)
    (state/'health.json.22426.827c672e.tmp').write_text('incomplete')
    (state/'health.json').write_text('{}')
    (root/'product.json.22426.827c672e.tmp').write_text('owned product file')
    destination = tmp_path/'snapshot'
    RootCauseCoordinator._copy_diagnostic_tree(root, destination)
    assert not (destination/'.auto-agents/state/health.json.22426.827c672e.tmp').exists()
    assert (destination/'.auto-agents/state/health.json').read_text() == '{}'
    assert (destination/'product.json.22426.827c672e.tmp').read_text() == 'owned product file'
