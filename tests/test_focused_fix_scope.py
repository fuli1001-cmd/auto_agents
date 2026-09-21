"""A repair's requirement association must not adopt unfinished product work."""
from copy import deepcopy
import json

import pytest

from auto_agents.config import load_project_config, load_task_plan, save_session_state, load_session_state
from auto_agents.models import VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.session_verification import _task_scope, bind_session, session_gates, SessionOwnershipError
from auto_agents.workflow_chain import IssueBriefBuilder
from test_engine_child_recovery import parent_workflow
from test_multilayer_engine_recovery import replay
from test_session_verification_ownership import project, _retain_contract


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


@pytest.mark.parametrize('legacy', [False, True])
def test_focused_engine_resume_keeps_original_task_and_skips_unadopted_future_work(tmp_path, legacy):
    root, child, store, original, engine = focused_scene(tmp_path, legacy)
    before = {p: (root / p).read_bytes() for p in ('.auto-agents/state/task_plan.json',
                                                '.auto-agents/config.json', 'foreign.py')}
    report = replay(root, engine.payload, tmp_path)
    assert report['ok'], report
    saved = load_session_state(root, child.session_id)
    scope = saved.verification_binding['task_scope']
    assert scope == {'mode': 'focused_fix', 'task_ids': [], 'requirement_ids': [],
                     'associated_requirement_ids': ['REQ-related'],
                     'verification_refs': ['tests/test_owned.py::test_owned']}
    assert saved.verification_binding['required_proof_ids'] == ['owned.contract']
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    assert 'future.feature' not in {s.proof_id for s in session_gates(session, saved).steps}
    assert report['recovery_observation']['boundary_kind'] == 'implementation'
    assert report['recovery_observation']['retained_constraints'] is True
    assert saved.execution_log[:len(child.execution_log)] == child.execution_log
    assert {p: (root / p).read_bytes() for p in before} == before
    assert not (root / 'tests/test_future.py').exists()


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
