"""A repair's requirement association must not adopt unfinished product work."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.config import load_project_config, load_task_plan, save_session_state, load_session_state
from auto_agents.models import VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.local_io import digest
from auto_agents.session import Session
from auto_agents.session_verification import _task_scope, bind_session, session_gates, SessionOwnershipError
from auto_agents.workflow_chain import IssueBriefBuilder, WorkflowRef
from auto_agents.workflow_runtime import WorkflowCoordinator
from workflow_support import parent_workflow
from test_session_verification_ownership import project, _retain_contract


def seeded_focused_child(tmp_path, command='python -m pytest -q tests/test_owned.py::test_owned'):
    root, _ = project(tmp_path)
    coordinator = WorkflowCoordinator(Orchestrator(root), auto_approve=True)
    snapshot = coordinator.store.create_root(WorkflowRef('collab', 'parent'))
    handoff = coordinator.store.prepare_handoff(
        snapshot, parent=snapshot.root, target='fix', goal='Repair owned issue',
        reason='focused repair', payload={'issue_seed': {
            'summary': 'repair owned issue', 'verification_scope': {'mode': 'focused_fix'},
            'verification_command': command}})
    session = Session(coordinator.orch, mode='fix', auto_approve=True, coordinator=coordinator)
    with patch.object(coordinator, '_drive_session', side_effect=lambda _session, state, *_args, **_kw: state):
        state = coordinator.start_seeded_session(session, snapshot=snapshot, handoff=handoff)
    return root, coordinator, snapshot, handoff, state


def test_seeded_focused_command_is_bound_before_first_preflight(tmp_path):
    root, _, _, handoff, state = seeded_focused_child(tmp_path)
    command = handoff.payload['issue_seed']['verification_command']
    assert state.fix_verify_command == command
    assert load_session_state(root, state.session_id).fix_verify_command == command
    assert _task_scope(Session(Orchestrator(root), mode='fix'), state)['mode'] == 'focused_fix'
    assert state.current_attempt == 0 and not state.candidate_custody


@pytest.mark.parametrize('change', ['valid', 'missing', 'conflicting', 'candidate'])
def test_seeded_focused_retained_command_recovery_requires_original_authority(tmp_path, change):
    root, coordinator, snapshot, handoff, state = seeded_focused_child(tmp_path)
    command = state.fix_verify_command
    state.fix_verify_command = ''
    state.status, state.resolution = 'blocked', 'verification_ownership'
    state.execution_log.append({'action': 'execution_preflight_blocked',
        'failure_kind': 'verification_ownership', 'result': 'missing command', 'retry_fix': False})
    if change == 'missing':
        handoff.payload['issue_seed']['verification_command'] = ''
        coordinator.store.save_handoff(handoff)
        issue = root / '.auto-agents/state/sessions' / state.session_id / 'issue.json'
        data = json.loads(issue.read_text())
        data['verification_command'] = ''
        issue.write_text(json.dumps(data))
    elif change == 'conflicting':
        handoff.payload['issue_seed']['verification_command'] = 'python -m pytest -q tests/test_other.py'
        coordinator.store.save_handoff(handoff)
    elif change == 'candidate':
        state.candidate_paths = {'app.py': 'modified'}
    save_session_state(root, state)
    session = Session(coordinator.orch, mode='fix', auto_approve=True, coordinator=coordinator)
    with patch.object(coordinator, '_drive_session', side_effect=lambda _session, saved, *_args, **_kw: saved):
        resumed = coordinator.start_seeded_session(session, snapshot=snapshot, handoff=handoff)
    if change == 'valid':
        assert resumed.fix_verify_command == command
        assert _task_scope(session, resumed)['mode'] == 'focused_fix'
    else:
        assert not resumed.fix_verify_command
        assert resumed.current_attempt == 0
        if change != 'missing':
            assert resumed.resolution == 'verification_ownership'


@pytest.mark.parametrize('change', ['same', 'conflicting', 'task'])
def test_seeded_focused_never_replaces_existing_command_or_adopts_task(tmp_path, change):
    root, coordinator, snapshot, handoff, state = seeded_focused_child(tmp_path)
    original = state.fix_verify_command
    if change == 'conflicting':
        handoff.payload['issue_seed']['verification_command'] = 'python -m pytest -q tests/test_other.py'
    elif change == 'task':
        handoff.payload['task_id'] = 'unrelated'
    coordinator.store.save_handoff(handoff)
    session = Session(coordinator.orch, mode='fix', auto_approve=True, coordinator=coordinator)
    with patch.object(coordinator, '_drive_session', side_effect=lambda _session, saved, *_args, **_kw: saved):
        resumed = coordinator.start_seeded_session(session, snapshot=snapshot, handoff=handoff)
    assert resumed.fix_verify_command == original
    assert load_session_state(root, state.session_id).fix_verify_command == original
    if change == 'same':
        assert resumed.resolution != 'verification_ownership'
    else:
        assert resumed.resolution == 'verification_ownership'




def focused_scene(tmp_path, legacy=False):
    root, child = project(tmp_path)
    config, plan = load_project_config(root), load_task_plan(root)
    future = VerificationStep(proof_id='future.feature', runner='pytest',
        targets=['tests/test_future.py::test_future'], levels=['affected', 'release'])
    config.gates.steps.append(future)
    plan['verification_steps'].append(future.to_dict())
    plan['tasks'].append({'task_id': 'future', 'requirement_ids': ['REQ-related'], 'status': 'pending',
                         'workflow_id': 'stopped-workflow', 'verification_refs': future.targets})
    _retain_contract(root, child, config, plan)
    store, _, engine = parent_workflow(root, child, engine=True)
    original = store.load_handoff(child.parent_handoff_id)
    original.payload.pop('task_id')
    seed = {'requirement_ids': ['REQ-related']}
    if legacy:
        seed['retained_task_relation'] = '此关联仅标识依赖，不接管其实现、任务状态或完整验收责任。'
    else:
        seed['verification_scope'] = {'mode': 'focused_fix'}
    original.payload['issue_seed'] = seed
    store.save_handoff(original)
    child.fix_verify_command = 'python -m pytest -q tests/test_owned.py::test_owned'
    child.status, child.resolution = 'blocked', 'verification_ownership'
    child.execution_log.append({'action': 'execution_preflight_blocked', 'failure_kind': child.resolution,
        'result': 'incorrectly adopted an unrelated task', 'retry_fix': False})
    save_session_state(root, child)
    return root, child, store, original, engine




@pytest.mark.parametrize('conflict', ['task', 'requirements', 'missing_command'])
def test_focused_scope_cannot_override_explicit_task_authority_or_omit_verification(tmp_path, conflict):
    root, child, store, original, _ = focused_scene(tmp_path)
    if conflict == 'task': original.payload['task_id'] = 'future'
    elif conflict == 'requirements': original.payload['requirement_ids'] = ['REQ-related']
    else: child.fix_verify_command = ''
    store.save_handoff(original)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    with pytest.raises(SessionOwnershipError): _task_scope(session, child)


def test_focused_scope_preserves_existing_regressions_and_explicit_prerequisites(tmp_path):
    root, child, _, _, _ = focused_scene(tmp_path)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    bind_session(session, child)
    selected = session_gates(session, child)
    assert any(s.proof_id == 'owned.contract' for s in selected.steps)
    # Required ordering evidence cannot be removed by the association rule.
    graph = child.verification_binding['proof_graph']['gates']
    next(s for s in graph['steps'] if s['proof_id'] == 'owned.contract')['depends_on_proofs'] = ['future.feature']
    selected = session_gates(session, child)
    assert 'future.feature' in {s.proof_id for s in selected.steps}


def test_broad_coverage_is_not_authority_to_adopt_future_dependencies(tmp_path):
    from auto_agents.models import GateConfig
    from auto_agents.session_verification import _owned_inventory
    root, child, _, _, _ = focused_scene(tmp_path)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    bind_session(session, child)
    graph = child.verification_binding['proof_graph']['gates']
    graph['steps'].append(VerificationStep(proof_id='release.all', runner='pytest', targets=['tests'],
        levels=['release'], depends_on_proofs=['future.feature']).to_dict())
    required, _ = _owned_inventory(child, GateConfig.from_dict(graph), session)
    assert 'owned.contract' in required
    assert 'release.all' not in required and 'future.feature' not in required
    selected = session_gates(session, child)
    assert 'future.feature' not in {s.proof_id for s in selected.steps}
    broad = next(s for s in selected.steps if s.proof_id == 'release.all')
    assert broad.targets == ['tests'] and broad.depends_on_proofs == []


def test_issue_materialization_preserves_association_mode(tmp_path):
    root, child, _, original, _ = focused_scene(tmp_path)
    IssueBriefBuilder(root, child.session_id).materialize(original.payload['issue_seed'])
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    assert _task_scope(session, child)['mode'] == 'focused_fix'


def test_focused_fix_cannot_treat_unreadable_retained_plan_as_absent(tmp_path, monkeypatch):
    import subprocess
    import auto_agents.session_verification as verification

    root, child, _, _, _ = focused_scene(tmp_path)
    run = verification.subprocess.run
    def unreadable(command, *args, **kwargs):
        if command[:2] == ['git', 'show'] and command[-1].endswith(':.auto-agents/state/task_plan.json'):
            return subprocess.CompletedProcess(command, 128, '', 'unable to read retained blob')
        return run(command, *args, **kwargs)
    monkeypatch.setattr(verification.subprocess, 'run', unreadable)
    with pytest.raises(SessionOwnershipError, match='retained task plan ownership is unavailable'):
        bind_session(Session(Orchestrator(root), mode='fix', auto_approve=True), child)
    assert not child.verification_binding and child.current_attempt == 0
