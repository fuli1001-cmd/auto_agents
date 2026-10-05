"""Implementation requirements reuse visual approvals without changing their bytes."""
import copy
import json
from pathlib import Path

import pytest

from auto_agents.config import (frontend_design_lock_path, load_run_state,
    requirements_trace_path, save_run_state)
from auto_agents.frontend_contract_reuse import BoundDesignLock, recover
from auto_agents.frontend_design import (load_frontend_design_lock,
    missing_frontend_design_contract_requirement_ids, approved_frontend_design)
from auto_agents.git_ops import head_ref
from auto_agents.io_utils import write_json, write_text
from auto_agents.orchestrator import Orchestrator
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore, sha256_text
from test_frontend_design import PrototypeAdapter, write_frontend_fidelity_trace, frontend_task, HTML
from test_workflow_chain import _commit_baseline


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', '1')
    monkeypatch.setenv('WECHAT_WEBHOOK_URL', '')
    root = tmp_path / 'project'
    Orchestrator.init_project(root, 'project', 'mock')
    write_text(root / 'spec.md', '# Implement the already approved browser recovery')
    write_text(root / 'DESIGN.md', '# User design')
    write_text(root / 'specs/prototype/home.html', HTML)
    write_frontend_fidelity_trace(root)
    orch = Orchestrator(root)
    orch.adapter = PrototypeAdapter(root)
    state = load_run_state(root)
    orch._run_prototype_stage(state, root / 'spec.md')
    state = orch.approve('prototype')
    # Retain the real historical approval in a commit, just as in the incident.
    import subprocess
    subprocess.run(['git', 'add', '-f', '.auto-agents/state/frontend_design.lock.json'], cwd=root, check=True)
    _commit_baseline(root)
    original_bytes = frontend_design_lock_path(root).read_bytes()
    original = json.loads(original_bytes)
    trace = json.loads(requirements_trace_path(root).read_text())
    trace['frontend_scope']['design_action'] = 'reuse_approved'
    trace['frontend_scope']['surfaces'][0]['requirement_ids'] += ['REQ-002', 'REQ-003']
    trace['requirements'] += [
        {'id': 'REQ-002', 'status': 'active', 'text': 'Resume the same project through its browser card',
         'acceptance_oracles': ['Concurrent requests use one recovery attempt'], 'priority': 'mandatory'},
        {'id': 'REQ-003', 'status': 'active', 'text': 'Implement the already approved recovery card',
         'acceptance_oracles': ['Check its four states against the approved page and capture screenshots'],
         'priority': 'mandatory'}]
    write_json(requirements_trace_path(root), trace)
    write_text(root / 'src/pages/Home.tsx', 'export const Home = () => <main />')
    store = WorkflowStore(root)
    snapshot = store.create_root(WorkflowRef('collab', 'parent'))
    handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='run', goal='Inspect the real video',
        reason='Implement the approved recovery entry', payload={'spec_seed': {'existing_project_id': 'existing'}})
    store.bind_child(snapshot, handoff, WorkflowRef('run', state.run_id))
    handoff.result = {'status': 'failed', 'run_id': state.run_id,
                     'summary': 'preflight validation failed:\n- missing task plan file: old-plan'}
    store.save_handoff(handoff)
    state.status, state.current_stage, state.pending_approval = 'failed', 'prototype', ''
    state.stage_summaries = {'clarify': 'Existing project and approved prototype retained'}
    state.approved_gates = ['requirements']
    state.last_error = ''
    state.resume_context.update(parent_handoff_id=handoff.handoff_id, workflow_id=snapshot.workflow_id,
        iteration_spec_commit=head_ref(root), spec_file=str(root / 'spec.md'),
        iteration_spec_sha256=sha256_text((root / 'spec.md').read_text()))
    save_run_state(root, state)
    return root, orch, state, original, original_bytes, trace


def test_existing_page_reuses_visual_approval_without_hiding_implementation(scene, monkeypatch):
    root, orch, state, original, before, trace = scene
    monkeypatch.setattr(orch, '_create_prototype_variant', lambda **kw: pytest.fail('Prototype generation'))
    assert approved_frontend_design(root)
    bound = load_frontend_design_lock(root)
    assert isinstance(bound, BoundDesignLock)
    assert missing_frontend_design_contract_requirement_ids(bound, ['REQ-002', 'REQ-003']) == []
    assert missing_frontend_design_contract_requirement_ids(original, ['REQ-002']) == ['REQ-002']
    assert json.loads(json.dumps(bound)) == original
    orch._run_prototype_stage(state, root / 'spec.md')
    assert not state.pending_approval
    assert frontend_design_lock_path(root).read_bytes() == before
    after = json.loads(requirements_trace_path(root).read_text())
    assert after == trace and after['frontend_scope']['requested']
    task = frontend_task(status='pending'); task.requirement_ids = ['REQ-002', 'REQ-003']
    assert orch._route_frontend_design_contract_prerequisite(state, [task], task) is None
    assert 'APPROVED FRONTEND DESIGN CONTRACT' in orch._build_task_prompt(task, 'implement')


@pytest.mark.parametrize('change', ['redesign', 'route', 'surface', 'prototype', 'viewport', 'drift', 'inactive', 'missing_ref', 'new_interaction'])
def test_new_or_changed_visual_scope_cannot_borrow_an_existing_approval(scene, change):
    root, orch, state, original, before, trace = scene
    surface = trace['frontend_scope']['surfaces'][0]
    if change == 'redesign':
        trace['frontend_scope']['design_action'] = 'redesign'
    elif change == 'route':
        surface['route'] = '/new'
    elif change == 'surface':
        surface['id'] = 'new-surface'
    elif change == 'prototype':
        trace['frontend_surfaces'][0]['prototype_refs'].append('specs/new-design.html')
    elif change == 'viewport':
        trace['frontend_surfaces'][0]['viewports'].append('800x600')
    elif change == 'drift':
        write_text(root / 'DESIGN.md', '# Changed appearance')
    elif change == 'inactive':
        trace['requirements'][-1]['status'] = 'superseded'
    elif change == 'new_interaction':
        trace['requirements'][-1]['text'] = 'Add a new interaction requiring a newly approved prototype'
    else:
        trace['frontend_surfaces'][0]['prototype_refs'] = ['DESIGN.md']
    write_json(requirements_trace_path(root), trace)
    assert missing_frontend_design_contract_requirement_ids(load_frontend_design_lock(root), ['REQ-003']) == ['REQ-003']
    assert frontend_design_lock_path(root).read_bytes() == before


def spurious_lock(scene):
    root, orch, state, original, before, trace = scene
    current = copy.deepcopy(original)
    current.update(status='pending_approval', redesign_requested_at='2026-10-02T09:46:03+00:00',
                   redesign_requirement_ids=['REQ-002', 'REQ-003'])
    for key in ['approval', 'approved_at', 'contract_sha256']:
        current.pop(key, None)
    write_json(frontend_design_lock_path(root), current)
    state.resume_context[orch.FRONTEND_CONTRACT_RECOVERY_CONTEXT] = True
    save_run_state(root, state)
    return current


def test_interrupted_spurious_reapproval_restores_actual_historical_approval(scene, monkeypatch):
    root, orch, state, original, before, trace = scene
    spurious_lock(scene)
    write_text(root / '.auto-agents/docs/frontend_prototype_variants/interrupted-draft/home.html', 'Keep this draft')
    state = load_run_state(root)
    assert recover(root, state)
    assert frontend_design_lock_path(root).read_bytes() == before
    assert state.status == 'failed'  # Status changes require successful preflight.
    assert 'frontend_contract_reuse_recovery' in state.resume_context
    assert not state.resume_context.get(orch.FRONTEND_CONTRACT_RECOVERY_CONTEXT)
    assert (root / '.auto-agents/docs/frontend_prototype_variants/interrupted-draft/home.html').read_text() == 'Keep this draft'
    assert json.loads(requirements_trace_path(root).read_text()) == trace
    assert not recover(root, state)


@pytest.mark.parametrize('change', ['different_lock', 'redesign', 'artifact', 'spec', 'rejection', 'unbound', 'wrong_ids'])
def test_reapproval_recovery_refuses_operator_changes_and_new_decisions(scene, change):
    root, orch, state, original, before, trace = scene
    current = spurious_lock(scene)
    if change == 'different_lock':
        current['source']['kind'] = 'operator'
    elif change == 'redesign':
        trace['frontend_scope']['design_action'] = 'redesign'
        write_json(requirements_trace_path(root), trace)
    elif change == 'artifact':
        write_text(root / 'DESIGN.md', '# Different design')
    elif change == 'spec':
        write_text(root / 'spec.md', '# New user request')
    elif change == 'rejection':
        state.rejected_stage = 'prototype'
    elif change == 'unbound':
        state.resume_context.pop('parent_handoff_id')
    else:
        current['redesign_requirement_ids'] = ['REQ-999']
    write_json(frontend_design_lock_path(root), current)
    bytes_before = frontend_design_lock_path(root).read_bytes()
    assert not recover(root, state)
    assert frontend_design_lock_path(root).read_bytes() == bytes_before


@pytest.mark.parametrize('kind', ['preflight', 'cleared_preflight', 'implementation', 'active_blocker', 'skip'])
def test_successful_preflight_clears_only_proven_stale_failure_status(scene, monkeypatch, kind):
    root, orch, state, original, before, trace = scene
    monkeypatch.setattr('auto_agents.orchestrator.validation_report', lambda *a, **kw: {'ok': True})
    if kind == 'preflight':
        state.last_error = 'preflight validation failed:\n- missing old plan'
    elif kind == 'implementation':
        state.last_error = 'Product implementation failed'
    elif kind == 'active_blocker':
        state.active_blocker = {'owner': 'user', 'reason': 'An unresolved decision'}
    orch._ensure_preconditions(state, root / 'spec.md', skip_validate=kind == 'skip')
    assert state.status == ('pending' if kind in {'preflight', 'cleared_preflight'} else 'failed')
