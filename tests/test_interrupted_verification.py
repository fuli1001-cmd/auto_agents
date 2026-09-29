"""Recover an actually exited verifier, preserving custody and all consumption."""
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.recovery import KernelError, OutcomeKind
from auto_agents.recovery.interrupted_verification import prepare, reconcile_installation
from auto_agents.recovery.native import perform


def interrupted(tmp_path, monkeypatch, *, legacy=False, saved=False):
    from auto_agents.recovery.executor import Executor
    from auto_agents.recovery.policy import enable
    from auto_agents.session import Session
    from auto_agents.orchestrator import Orchestrator
    from test_retained_candidate_resume import stopped_candidate
    root, store, child, calls = stopped_candidate(tmp_path, monkeypatch)
    stream = store.binding(root, 'session:' + child.session_id)
    enable(store, stream)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    session._current_state = child
    session._custody_control_root = root
    session.project_root = Path(child.candidate_custody['checkout'])
    session.orch._kernel_project = root
    def die():
        if legacy:
            from auto_agents.artifact_store import process_identity
            identity = process_identity()
            (root/'.auto-agents/state/health-watch-control.json').write_text(json.dumps({
                'project': str(root), 'subject_id': child.session_id, 'owner_pid': identity['pid'],
                'owner_start_ticks': int(identity['ticks']), 'active_operation': {'kind': 'verification'}}))
        original = Executor._event
        def emit(self, target, kind, data, key):
            if legacy and kind == 'command_dispatched': data.pop('dispatch_identity', None)
            if saved and kind == 'command_finished': os._exit(23)
            return original(self, target, kind, data, key)
        Executor._event = emit
        def verify():
            if not saved: os._exit(23)
            return {'ok': False, 'reason': 'Actual failed test result'}
        perform(session, 'verify', 'crash-test', verify,
            lambda v: (OutcomeKind.CANDIDATE_REJECTED, 'Actual failed test result'))
    process = multiprocessing.get_context('fork').Process(target=die)
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate(); process.join(); pytest.fail('Verifier subprocess did not reach its crash boundary')
    assert process.exitcode == 23
    snapshot = store.load(stream)
    command = max(snapshot['commands'].values(), key=lambda v: v['sequence'])
    assert command['status'] == 'running'
    return root, store, stream, child, session, command


@pytest.mark.parametrize('legacy', [False, True])
def test_dead_verifier_is_inconclusive_without_progress_or_budget_reset(tmp_path, monkeypatch, legacy):
    root, store, stream, child, session, command = interrupted(tmp_path, monkeypatch, legacy=legacy)
    before = deepcopy(store.replay(stream))
    assert prepare(store, stream, command['command_id'])['candidate'] == child.candidate_custody['receipt']['fingerprint']
    assert reconcile_installation(store) == [command['command_id']]
    after = store.replay(stream)
    assert after['budget'] == before['budget']
    assert after['recovery'] == before['recovery']
    assert after['projections'] == before['projections']
    final = after['commands'][command['command_id']]['outcome']
    assert final['kind'] == 'environment_blocked' and not final['evidence']
    assert store.read(final['details']['interrupted_verification'])['progress_credit'] is False
    assert reconcile_installation(store) == []


def test_same_runtime_reverifies_interruption_once_with_a_new_durable_operation(tmp_path, monkeypatch):
    root, store, stream, child, session, command = interrupted(tmp_path, monkeypatch)
    reconcile_installation(store)
    before = deepcopy(store.load(stream)['budget'])
    calls = []
    def verify():
        calls.append(True)
        return {'ok': True, 'verification_checks': [{'id': 'tests/test_owned.py::test_owned', 'status': 'passed'}]}
    for _ in range(2):
        assert perform(session, 'verify', 'crash-test', verify,
            lambda v: (OutcomeKind.SUCCESS, 'Actual completed verification'))['ok']
    assert calls == [True]
    after = store.replay(stream)
    assert after['budget'] == before
    assert after['commands'][command['command_id']]['outcome']['kind'] == 'environment_blocked'
    assert len([v for v in after['commands'].values() if v['phase']=='verify' and v['status']=='finished'
                and (v.get('outcome') or {}).get('kind')=='success']) == 1


def test_existing_durable_test_result_takes_precedence_over_interruption(tmp_path, monkeypatch):
    root, store, stream, child, session, command = interrupted(tmp_path, monkeypatch, saved=True)
    assert prepare(store, stream, command['command_id']) is None
    assert reconcile_installation(store) == []
    final = store.replay(stream)['commands'][command['command_id']]
    assert final['status'] == 'finished' and final['outcome']['kind'] == 'candidate_rejected'
    assert 'interrupted_verification' not in final['outcome']['details']


def test_live_owner_is_never_reconciled_as_interrupted(tmp_path, monkeypatch):
    root, store, stream, child, session, command = interrupted(tmp_path, monkeypatch)
    monkeypatch.setattr('auto_agents.artifact_store.alive', lambda identity: True)
    before = deepcopy(store.replay(stream))
    assert reconcile_installation(store) == []
    assert store.replay(stream) == before


def test_changed_private_candidate_blocks_interruption_recovery(tmp_path, monkeypatch):
    root, store, stream, child, session, command = interrupted(tmp_path, monkeypatch)
    before = deepcopy(store.replay(stream))
    (session.project_root/'value.py').write_text('UNOWNED = 2\n')
    with pytest.raises(Exception, match='private candidate changed'): reconcile_installation(store)
    assert store.replay(stream) == before


def test_legacy_without_bound_process_identity_stays_unconfirmed(tmp_path, monkeypatch):
    root, store, stream, child, session, command = interrupted(tmp_path, monkeypatch, legacy=True)
    (root/'.auto-agents/state/health-watch-control.json').unlink()
    before = deepcopy(store.replay(stream))
    assert reconcile_installation(store) == []
    assert store.replay(stream) == before


def test_public_bootstrap_reconciles_even_without_a_runtime_change(tmp_path, monkeypatch):
    from auto_agents.recovery import runtime_manager
    root, store, stream, child, session, command = interrupted(tmp_path, monkeypatch)
    before = deepcopy(store.load(stream)['budget'])
    monkeypatch.setattr(runtime_manager, 'source_identity', lambda root: 'f'*64)
    monkeypatch.setattr('auto_agents.repair_v2.runtime_artifact.verify', lambda artifact: None)
    monkeypatch.setattr(runtime_manager.lifecycle, 'migrate', lambda store: None)
    monkeypatch.setattr(runtime_manager.lifecycle, 'register', lambda *a, **k: None)
    runtime_manager.ensure_current_runtime(store, source=root, automatic=False)
    after = store.replay(stream)
    assert after['commands'][command['command_id']]['outcome']['kind'] == 'environment_blocked'
    assert after['budget'] == before


@pytest.mark.parametrize('phase,model', [('implement', True), ('verify', True), ('deliver', False), ('review', True)])
def test_model_writes_delivery_and_reviews_do_not_use_verification_recovery(phase, model):
    command = {'phase': phase, 'model_call': model, 'status': 'unknown'}
    store = SimpleNamespace(load=lambda stream: {'commands': {'command': command}})
    assert prepare(store, 'stream', 'command') is None
