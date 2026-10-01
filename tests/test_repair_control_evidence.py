"""Repair admission reads authoritative control records across private custody."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.config import load_run_state, load_session_state, save_run_state, save_session_state
from auto_agents.models import SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.recovery.authority import export_snapshot
from auto_agents.repair_client import EngineRepairRequired, triage_engine_request
from auto_agents.repair_v2.scope import ScopeGuard, witnesses
from auto_agents.repair_v2.store import digest
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.session import Session
from auto_agents.session_candidate import _runtime_checkout, execution_checkout
from auto_agents.workflow_chain import WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_recovery_native import activate
from test_session import _make_project, _confirm_collab_state
from test_workflow_chain import _commit_baseline


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', '1')
    monkeypatch.setenv('WECHAT_WEBHOOK_URL', '')
    root = _make_project(str(tmp_path))
    _commit_baseline(root)
    coordinator = WorkflowCoordinator(Orchestrator(root), auto_approve=True)
    workflow = coordinator.store.create_root(WorkflowRef('collab', 'parent'))
    refusal = 'run unused remains pending; no new run handoff was created'
    state = _confirm_collab_state(SessionState('parent', mode='collab', status='executing',
        goal='Resume the existing project', workflow_id=workflow.workflow_id, auto_approve=True,
        execution_log=[{'action': 'run_route_deferred', 'result': refusal}]), 'real')
    save_session_state(root, state)
    store = activate(root, tmp_path / 'control', monkeypatch)
    save_run_state(root, load_run_state(root))
    return root, store, coordinator, load_session_state(root, state.session_id)


@pytest.mark.parametrize('absent', [False, True])
@pytest.mark.parametrize('record,pointer', [
    ('run_state.json', '/current_stage'),
    ('sessions/parent/session_state.json', '/execution_log/0/result'),
])
def test_projection_witnesses_match_frozen_input_without_display_fields(scene, tmp_path, absent, record, pointer):
    root, store, _, state = scene
    relative = '.auto-agents/state/' + record
    path = root / relative
    # Display wrappers intentionally omit these values. Losing a wrapper also
    # does not lose the projection held by the controller journal.
    assert 'current_stage' not in json.loads(path.read_text())
    if absent:
        path.unlink()
    stream = store.binding(root, 'session:' + state.session_id)
    before = deepcopy(store.replay(stream))
    references = [{'origin': 'target', 'path': relative},
                  {'origin': 'target', 'path': relative, 'pointer': pointer}]
    evidence = witnesses(references, root, root)
    frozen = tmp_path / 'frozen'
    export_snapshot(root, frozen)
    assert witnesses(references, frozen, frozen) == evidence
    assert witnesses(evidence, root, root) == evidence
    assert witnesses(evidence, frozen, frozen) == evidence
    assert store.replay(stream) == before


def test_changed_authoritative_value_rejects_pinned_pointer_and_receipt(scene, tmp_path):
    root, _, _, state = scene
    ref = {'origin': 'target', 'path': '.auto-agents/state/sessions/parent/session_state.json',
           'pointer': '/execution_log/0/result'}
    value = {'decision': 'required', 'blocked_step': 'Resume project',
        'consequence': 'Cannot continue the original project', 'recovery_check': 'Routing enters',
        'evidence_refs': [ref]}
    payload = {'project': str(root), 'invocation': {'session_id': 'parent', 'workflow_id': state.workflow_id}}
    guard = ScopeGuard(tmp_path / 'scope', payload, root, root)
    receipt = guard.store.read(guard.admit(value))
    pinned = receipt['witnesses'][0]
    state.execution_log[0]['result'] = 'A different blocking condition'
    save_session_state(root, state)
    with pytest.raises(RepairBlocked, match='已变化'):
        witnesses([pinned], root, root)
    assert ScopeGuard(tmp_path / 'scope', payload, root, root).current() is None
    assert ScopeGuard(tmp_path / 'worker', payload, root, root).import_receipt(receipt) is None


@pytest.mark.parametrize('pointer', ['/execution_log/99', '/unknown', '/execution_log/00', '/bad~2escape'])
def test_managed_pointer_rejects_missing_or_invalid_values(scene, pointer):
    root, _, _, _ = scene
    with pytest.raises(RepairBlocked):
        witnesses([{'origin': 'target', 'path': '.auto-agents/state/sessions/parent/session_state.json',
                    'pointer': pointer}], root, root)


@pytest.mark.parametrize('damage', ['symlink', 'foreign', 'missing'])
def test_projection_lookup_does_not_authorize_foreign_or_missing_evidence(scene, tmp_path, damage):
    root, _, _, _ = scene
    relative = '.auto-agents/state/sessions/parent/session_state.json'
    if damage == 'symlink':
        path = root / relative
        path.unlink()
        path.symlink_to(tmp_path / 'outside.json')
        (tmp_path / 'outside.json').write_text('{"goal":"foreign"}')
    elif damage == 'foreign':
        root = tmp_path / 'foreign'
        root.mkdir()
    else:
        relative = '.auto-agents/state/sessions/unknown/session_state.json'
    with pytest.raises(RepairBlocked):
        witnesses([{'origin': 'target', 'path': relative}], root, root)


def prepare_engine_route(scene, tmp_path, monkeypatch, *, needs_user=False, engine_root=None):
    root, store, coordinator, state = scene
    # This uses the real independent Git checkout and custody registration.
    from auto_agents.git_ops import head_ref
    checkout, revision = _runtime_checkout(root, state, root, head_ref(root))
    state.candidate_custody = {'schema_version': 1, 'checkout': str(checkout),
        'repository': str(root), 'session_id': state.session_id, 'base_revision': revision}
    save_session_state(root, state)
    for name in ['run_state.json', 'sessions/parent/session_state.json']:
        (checkout / '.auto-agents/state' / name).unlink(missing_ok=True)
    from auto_agents.repair_control import git
    engine = engine_root or tmp_path / 'engine'
    if engine_root is None:
        engine.mkdir()
        (engine / 'engine.py').write_text('VALUE = 0\n')
        git(engine, 'init')
        git(engine, 'config', 'user.name', 'test')
        git(engine, 'config', 'user.email', 'test@example.com')
        git(engine, 'add', 'engine.py')
        git(engine, 'commit', '-m', 'engine baseline')
    # Native entrypoints validate their installed implementation path.
    # Keep the source path unspecified for this offline transport fixture.
    runtime = {'source': 'f' * 64, 'commit': git(engine, 'rev-parse', 'HEAD')}
    if engine_root is not None:
        runtime['path'] = str(engine_root)
    store.set_meta('active_runtime', runtime)
    (store.root / 'operator.json').write_text(json.dumps({'source_root': str(engine)}))
    monkeypatch.delenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', raising=False)
    for name in ['AUTO_AGENTS_REPAIR_SUBSCRIBER', 'AUTO_AGENTS_REPAIR_ROUTE_PROBE']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('auto_agents.self_repair.auto_agents_repo_root', lambda: engine)
    coordinator.orch._repair_registration = {'config': {'source_root': str(engine)}}
    coordinator.orch._kernel_subject = 'session:' + state.session_id
    coordinator.orch._invocation_context = {'command': 'collab', 'session_id': state.session_id,
        'workflow_id': state.workflow_id, 'auto_approve': True}
    session = Session(coordinator.orch, mode='collab', coordinator=coordinator, auto_approve=True)
    necessity = {'decision': 'required', 'blocked_step': 'Resume project',
        'consequence': 'Cannot continue the original project', 'recovery_check': 'Routing enters',
        'evidence_refs': [{'origin': 'target', 'path': '.auto-agents/state/run_state.json'},
            {'origin': 'target', 'path': '.auto-agents/state/sessions/parent/session_state.json',
             'pointer': '/execution_log/0'},
            {'origin': 'target', 'path': '.auto-agents/state/sessions/parent/session_state.json'}, {'origin': 'source',
             'path': 'engine.py' if engine_root is None else 'src/auto_agents/workflow_runtime.py'}]}
    if needs_user:
        necessity.update(decision='needs_user', question='Change the project goal?', suggestion='A new goal')
    route = {'target_repository': str(engine), 'issue_seed': {'summary': 'Repair the routing entry',
        'required_behavior': ['Resume the original project through its authorized route']}, 'necessity': necessity}
    return session, state, route, engine


def test_private_custody_route_reaches_engine_admission_and_frozen_revalidation(scene, tmp_path, monkeypatch):
    root, store, _, _ = scene
    session, state, route, engine = prepare_engine_route(scene, tmp_path, monkeypatch)
    run_before = load_run_state(root).to_dict()
    stream = store.binding(root, 'session:' + state.session_id)
    budget_before = deepcopy(store.load(stream)['budget'])
    with pytest.raises(EngineRepairRequired) as raised:
        with execution_checkout(session, state):
            assert not (session.project_root / '.auto-agents/state/run_state.json').exists()
            session._prepare_workflow_handoff(state, target='fix', reason='Restore original entry', payload=route)
    receipt = session.orch._repair_scope_receipt
    assert receipt['context']['owner']['project'] == str(root)
    assert receipt['context']['incident']['phase'] == 'routing'
    assert receipt['witnesses'][1]['sha256'] == digest(state.execution_log[0])
    assert triage_engine_request(session.orch, root, raised.value).decision.eligible
    assert session.orch._repair_scope_receipt == receipt
    assert load_run_state(root).to_dict() == run_before
    assert store.load(stream)['budget'] == budget_before
    payload = {'project': str(root), 'invocation': {'session_id': 'parent', 'workflow_id': state.workflow_id,
        'engine_route': route}}
    frozen = tmp_path / 'frozen'
    export_snapshot(root, frozen)
    guard = ScopeGuard(tmp_path / 'worker', payload, frozen, engine)
    assert guard.import_receipt(receipt) is not None
    assert guard.current() is not None


def test_goal_choice_uses_durable_owner_while_product_execution_is_private(scene, tmp_path, monkeypatch):
    root, _, _, _ = scene
    session, state, route, _ = prepare_engine_route(scene, tmp_path, monkeypatch, needs_user=True)
    observed = []
    def choice(session, state, context, proposal, continuation):
        observed.append(context)
        return 'keep'
    monkeypatch.setattr('auto_agents.scope_decisions.session_choice', choice)
    with execution_checkout(session, state):
        assert session._prepare_workflow_handoff(state, target='fix', reason='Goal choice', payload=route) is state
    assert observed[0]['owner']['project'] == str(root)
    assert observed[0]['original_goal'] == state.goal


@pytest.mark.parametrize('damage', ['missing_evidence', 'no_controller_failure'])
def test_private_custody_cannot_reuse_old_receipt_after_rejected_admission(scene, tmp_path, monkeypatch, damage):
    root, _, _, _ = scene
    session, state, route, _ = prepare_engine_route(scene, tmp_path, monkeypatch)
    session.orch._repair_scope_receipt = {'stale': 'earlier route'}
    if damage == 'missing_evidence':
        route['necessity']['evidence_refs'] = [{'origin': 'target', 'path': 'missing-proof.json'}]
    else:
        state.execution_log = []
        save_session_state(root, state)
    with execution_checkout(session, state):
        # A model-written copy in the candidate cannot substitute for a
        # missing failure in the real controller record.
        stale = session.project_root / '.auto-agents/state/sessions/parent/session_state.json'
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({'session_id': 'parent', 'goal': 'A substituted goal',
            'execution_log': [{'action': 'error', 'result': 'fabricated failure'}]}))
        result = session._prepare_workflow_handoff(state, target='fix', reason='Restore entry', payload=route)
        assert result is state and not state.active_handoff_id
    assert session.orch._repair_scope_receipt is None
    assert state.goal == 'Resume the existing project'
    assert any('依据' in row.get('content', '') or '失败记录' in row.get('content', '')
               for row in state.conversation)


def test_ordinary_execution_error_still_persists_failed_state(scene, tmp_path, monkeypatch):
    root, _, _, _ = scene
    session, state, _, _ = prepare_engine_route(scene, tmp_path, monkeypatch)
    def fail(state):
        raise RuntimeError('Actual execution failed')
    monkeypatch.setattr(session, '_phase_collab_loop', fail)
    with pytest.raises(RuntimeError, match='Actual execution failed'):
        with execution_checkout(session, state):
            session._drive_local_owned(state)
    saved = load_session_state(root, state.session_id)
    assert saved.status == 'failed' and saved.updated_at
    assert saved.execution_log[-1]['action'] == 'error'
    assert saved.execution_log[-1]['result'] == 'Actual execution failed'


def test_scope_receipt_survives_custody_and_actual_kernel_submission(scene, tmp_path, monkeypatch):
    from auto_agents.repair_client import submit_and_wait
    from auto_agents.recovery import engine as engine_runtime, OutcomeKind
    from auto_agents.recovery.native import perform
    from auto_agents.run_lock import ProjectRunLock

    root, store, _, _ = scene
    session, state, route, source = prepare_engine_route(scene, tmp_path, monkeypatch,
        engine_root=Path(__file__).resolve().parents[1])
    session._current_state = state
    perform(session, 'route', 'offline-owned-route', lambda: {'ok': True},
            lambda r: (OutcomeKind.SUCCESS, 'Observed retained route'))
    state.conversation.append({'role': 'agent', 'content': 'ROUTE_WORKFLOW v1: ' +
        json.dumps({'target': 'fix', **route})})
    save_session_state(root, state)
    control_before = load_session_state(root, state.session_id).to_dict()
    with pytest.raises(EngineRepairRequired) as raised:
        with execution_checkout(session, state):
            session._drive_local_owned(state)
    assert load_session_state(root, state.session_id).to_dict() == control_before
    original = deepcopy(session.orch._repair_scope_receipt)
    args = SimpleNamespace(command='collab', project=str(root), session=state.session_id,
        provider='mock', auto_approve=True, autonomy='max', full_verify=False)
    triage = triage_engine_request(session.orch, root, raised.value)
    assert triage.decision.eligible
    observed = []
    class SubmissionReached(BaseException):
        pass
    def effects(store, stream, contract, payload, working, base, progress):
        assert payload['project'] == str(root)
        assert payload['scope_receipt'] == original
        assert payload['invocation']['session_id'] == state.session_id
        assert payload['invocation']['workflow_id'] == state.workflow_id
        assert base == source
        guard = ScopeGuard(working, payload, working / 'target-evidence', base)
        assert guard.import_receipt(payload['scope_receipt']) is not None
        assert guard.current() is not None
        assert json.loads((working / 'target-evidence/.auto-agents/state/run_state.json').read_text())['current_stage']
        observed.append(payload)
        # Stop at the validated worker boundary; no Docker or model generation
        # is needed to prove that the actual entrypoint submitted this receipt.
        raise SubmissionReached()
    monkeypatch.setattr(engine_runtime, 'IsolatedEngineEffects', effects)
    stream = store.binding(root, 'session:' + state.session_id)
    before = deepcopy(store.load(stream)['budget'])
    with ProjectRunLock(root) as lock, pytest.raises(SubmissionReached):
        submit_and_wait(root, session.orch, raised.value, triage.decision, args, lock)
    assert len(observed) == 1
    assert store.load(stream)['budget']['model_calls'] == before['model_calls']
    assert store.load(stream)['budget']['implementations'] == before['implementations']
