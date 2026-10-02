"""A routed implementation inherits its scope without reopening parent choices."""
import json
from pathlib import Path

import pytest

from auto_agents.config import (conversation_history_path, load_run_state, save_run_state)
from auto_agents.models import AgentResult, RunState
from auto_agents.orchestrator import Orchestrator
from auto_agents.run_handoff_context import RESTORE, restore
from auto_agents.workflow_chain import IterationSpecBuilder, WorkflowRef, WorkflowStore
from test_session import _make_project
from test_workflow_chain import _commit_baseline


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', '1')
    monkeypatch.setenv('WECHAT_WEBHOOK_URL', '')
    root = _make_project(str(tmp_path))
    _commit_baseline(root)
    store = WorkflowStore(root)
    snapshot = store.create_root(WorkflowRef('collab', 'parent'))
    seed = {'user_summary': 'Continue the browser test on the existing project',
        'existing_project_id': 'project-existing',
        'steps': ['Deliver the already specified browser recovery entry', 'Resume the existing project and inspect its video'],
        'approved_contract_refs': ['specs/approved-recovery.md REQ-270'],
        'constraints': ['Do not change DESIGN.md or the approved prototype', 'Retain media receipts and call budgets'],
        'observed_state': {'images': 6, 'video_submissions': 0},
        'extension': {'nested': ['Keep this future protocol field']}}
    environment = {'mode': 'real', 'confirmed': True}
    payload = {'spec_seed': seed, 'goal_execution_environment': environment,
               'authorization_policy': {'mode': 'auto', 'source': 'cli:auto-approve'}}
    handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='run',
        goal='Create a project in the browser and validate a real video',
        reason='The created project requires its approved recovery entry', payload=payload)
    seed = {**seed, 'goal_execution_environment': environment}
    render = IterationSpecBuilder.render
    with monkeypatch.context() as patch:
        patch.setattr(IterationSpecBuilder, 'render', staticmethod(
            lambda handoff, seed, *, title, legacy=False: render(handoff, seed, title=title, legacy=True)))
        old_spec = IterationSpecBuilder(root).materialize(handoff, seed)
    current = load_run_state(root)
    state = RunState(current.run_id, status='waiting_user')
    state.agent_attempts = {'clarify-conv-0': 1}
    state.resume_context = {'workflow_id': snapshot.workflow_id, 'parent_handoff_id': handoff.handoff_id,
        'spec_file': str(root / old_spec['path']), 'iteration_spec_sha256': old_spec['sha256'],
        'iteration_spec_commit': old_spec['commit_sha'], 'authorization_policy': payload['authorization_policy'],
        'goal_execution_environment': environment, 'auto_approve': True}
    store.bind_child(snapshot, handoff, WorkflowRef('run', state.run_id))
    save_run_state(root, state)
    history = [{'role': 'user', 'content': 'Use the existing project; retain the approved visual artifacts.'},
               {'role': 'agent', 'content': 'Choose between a new project and redesigning the recovery prototype.'}]
    history_path = conversation_history_path(root, state.run_id)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history))
    return root, state, handoff, seed, history


def test_spec_preserves_all_continuation_inputs_and_distinguishes_child_scope(scene):
    root, state, handoff, seed, _ = scene
    content = IterationSpecBuilder.render(handoff, seed, title='Browser recovery')
    retained = json.loads(content.split('## Retained Handoff Inputs\n\n```json\n')[1].split('\n```')[0])
    assert retained == seed
    capability = content.split('## Requested Capability\n\n')[1].split('\n\n')[0]
    assert capability == seed['user_summary']
    assert handoff.goal in content
    assert '## Constraints' in content and '## Approved Contract References' in content
    assert 'Clarify and derive executable acceptance criteria before implementation.' not in content
    explicit = {**seed, 'goal': 'Deliver only the existing browser recovery entry'}
    scoped = IterationSpecBuilder.render(handoff, explicit, title='Recovery')
    assert scoped.split('## Goal\n\n')[1].split('\n\n')[0] == handoff.goal
    assert scoped.split('## Requested Capability\n\n')[1].split('\n\n')[0] == explicit['goal']


@pytest.mark.parametrize('managed', [False, True])
def test_legacy_context_restoration_keeps_identity_history_and_counters(scene, monkeypatch, managed):
    root, state, handoff, seed, history = scene
    if managed:
        from test_recovery_native import activate
        activate(root, root.parent / 'control', monkeypatch)
        state = load_run_state(root)
    original_path = Path(state.resume_context['spec_file'])
    original = original_path.read_bytes()
    handoff_before = WorkflowStore(root).load_handoff(handoff.handoff_id).to_dict()
    updated = restore(root, state, original_path)
    assert updated != original_path and updated.is_file()
    assert original_path.read_bytes() == original
    assert state.status == 'waiting_user' and not state.stage_summaries and not state.approved_gates
    assert state.agent_attempts == {'clarify-conv-0': 1}
    assert WorkflowStore(root).load_handoff(handoff.handoff_id).to_dict() == handoff_before
    assert restore(root, load_run_state(root), updated) == updated
    assert len(list((root / 'specs/iterations').glob('*.md'))) == 2
    assert json.loads(conversation_history_path(root, state.run_id).read_text()) == history
    assert state.resume_context[RESTORE]['previous_spec_sha256']


@pytest.mark.parametrize('old_ready', [False, True])
def test_recovered_clarify_uses_new_context_without_reasking_stale_question(scene, monkeypatch, old_ready):
    root, state, handoff, seed, history = scene
    if old_ready:
        history[-1]['content'] = 'READY_TO_GENERATE'
        conversation_history_path(root, state.run_id).write_text(json.dumps(history))
    updated = restore(root, state, Path(state.resume_context['spec_file']))
    orch = Orchestrator(root, user_input_fn=lambda *a, **kw: pytest.fail('Repeated user question'))
    prompts = []
    def execute(**kwargs):
        prompts.append(kwargs)
        if kwargs['stage_key'].startswith('clarify-conv'):
            assert str(updated) in kwargs['prompt']
            assert 'restored continuation inputs' in kwargs['prompt']
            assert history[0]['content'] in kwargs['prompt']
            assert 'Do not reopen already resolved choices' in kwargs['prompt']
            summary = 'The retained scope and constraints suffice.\nREADY_TO_GENERATE'
        else:
            assert kwargs['stage_key'] == 'clarify-generate'
            assert str(updated) in kwargs['prompt']
            summary = 'Generated the execution contract'
        return AgentResult(True, [], Path('.'), summary=summary)
    monkeypatch.setattr(orch, '_run_agent_with_retries', execute)
    orch._run_interactive_clarify(state, updated, auto_approve=True)
    assert len(prompts) == 2 and prompts[0]['stage_key'].startswith('clarify-conv')
    saved = json.loads(conversation_history_path(root, state.run_id).read_text())
    assert saved[:2] == history
    assert saved[2]['role'] == 'orchestrator' and saved[2][RESTORE]
    assert 'clarify' in state.stage_summaries
    assert state.status == 'pending'


@pytest.mark.parametrize('conflict', ['spec_bytes', 'owner', 'workflow', 'planned', 'user_spec', 'approval'])
def test_context_restore_never_changes_foreign_or_delivered_work(scene, conflict):
    root, state, handoff, seed, history = scene
    spec = Path(state.resume_context['spec_file'])
    if conflict == 'spec_bytes':
        spec.write_text('An operator changed the request')
    elif conflict == 'owner':
        handoff.child = WorkflowRef('run', 'other')
        WorkflowStore(root).save_handoff(handoff)
    elif conflict == 'workflow':
        state.resume_context['workflow_id'] = 'other'
    elif conflict == 'planned':
        state.stage_summaries['plan'] = 'Delivered'
    elif conflict == 'user_spec':
        spec = root / 'user-request.md'
        spec.write_text('A new explicit user request')
    elif conflict == 'approval':
        state.pending_approval = 'clarify'
    before = spec.read_bytes()
    if conflict in {'spec_bytes', 'owner', 'workflow'}:
        with pytest.raises((RuntimeError, ValueError)):
            restore(root, state, spec)
    else:
        assert restore(root, state, spec) == spec
    assert spec.read_bytes() == before
    assert not state.resume_context.get(RESTORE)


def test_resume_capture_retains_context_receipt_and_original_authority(scene):
    root, state, handoff, seed, history = scene
    updated = restore(root, state, Path(state.resume_context['spec_file']))
    before = dict(state.resume_context)
    Orchestrator(root)._capture_resume_context(state, spec_file=updated, auto_approve=True,
        allow_dirty_tree=False, max_tasks=None, skip_validate=False, print_agent_output=False,
        provider_kind=None, doc_language=None)
    for key in [RESTORE, 'authorization_policy', 'goal_execution_environment', 'parent_handoff_id', 'workflow_id']:
        assert state.resume_context[key] == before[key]


def test_context_restores_authority_previously_dropped_from_resume_projection(scene):
    root, state, handoff, seed, history = scene
    for key in ['authorization_policy', 'goal_execution_environment']:
        state.resume_context.pop(key)
    restore(root, state, Path(state.resume_context['spec_file']))
    for key in ['authorization_policy', 'goal_execution_environment']:
        assert state.resume_context[key] == handoff.payload[key]


def test_interrupted_context_history_does_not_reuse_old_readiness(scene, monkeypatch):
    from auto_agents.run_handoff_context import reconcile_history
    root, state, handoff, seed, history = scene
    history[-1]['content'] = 'READY_TO_GENERATE'
    updated = restore(root, state, Path(state.resume_context['spec_file']))
    assert reconcile_history(state, updated, history)
    conversation_history_path(root, state.run_id).write_text(json.dumps(history))
    orch = Orchestrator(root, user_input_fn=lambda *a, **kw: pytest.fail('Stale readiness prompted'))
    keys = []
    def execute(**kwargs):
        keys.append(kwargs['stage_key'])
        return AgentResult(True, [], Path('.'), summary=('Reassessed.\nREADY_TO_GENERATE'
            if kwargs['stage_key'].startswith('clarify-conv') else 'Generated'))
    monkeypatch.setattr(orch, '_run_agent_with_retries', execute)
    orch._run_interactive_clarify(state, updated, auto_approve=True)
    assert len(keys) == 2 and keys[0].startswith('clarify-conv')


def test_real_unresolved_question_still_requires_user_answer(scene, monkeypatch):
    root, state, handoff, seed, history = scene
    updated = restore(root, state, Path(state.resume_context['spec_file']))
    answers = []
    def answer(prompt):
        answers.append(prompt)
        return 'The newly discovered external cost is not authorized'
    orch = Orchestrator(root, user_input_fn=answer)
    calls = []
    def execute(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            summary = 'A new external cost was discovered. What is the budget?'
        elif len(calls) == 2:
            assert 'The newly discovered external cost is not authorized' in kwargs['prompt']
            summary = 'Preserve that new restriction.\nREADY_TO_GENERATE'
        else:
            summary = 'Generated'
        return AgentResult(True, [], Path('.'), summary=summary)
    monkeypatch.setattr(orch, '_run_agent_with_retries', execute)
    orch._run_interactive_clarify(state, updated, auto_approve=True)
    assert len(answers) == 1 and len(calls) == 3


def test_run_entry_restores_old_spec_before_preconditions(scene, monkeypatch):
    root, state, handoff, seed, history = scene
    old_path = Path(state.resume_context['spec_file'])
    orch = Orchestrator(root)
    observed = []
    def stop_at_preconditions(current, spec_file, **kwargs):
        observed.append(spec_file)
        assert spec_file != old_path
        assert current.resume_context['spec_file'] == str(spec_file)
        assert seed['existing_project_id'] in spec_file.read_text()
        raise KeyboardInterrupt()
    monkeypatch.setattr(orch, '_ensure_preconditions', stop_at_preconditions)
    monkeypatch.setattr(orch, '_call_with_failover', lambda *a: pytest.fail('Provider before preconditions'))
    with pytest.raises(KeyboardInterrupt):
        orch.run(old_path, auto_approve=True)
    assert len(observed) == 1


def test_existing_contract_continuation_preserves_requirements_and_architecture(scene):
    root, state, handoff, seed, history = scene
    updated = restore(root, state, Path(state.resume_context['spec_file']))
    orch = Orchestrator(root)
    assert orch._is_iteration_run(state)
    for stage in ['clarify', 'design']:
        prompt = orch._build_prompt(stage, updated, is_iteration=orch._is_iteration_run(state))
        assert 'ITERATION' in prompt
    state.resume_context['parent_handoff_id'] = 'hf-other'
    assert not orch._is_iteration_run(state)


def test_restored_run_advances_past_clarify_instead_of_retaining_waiting_status(scene, monkeypatch):
    root, state, handoff, seed, history = scene
    old = Path(state.resume_context['spec_file'])
    orch = Orchestrator(root, user_input_fn=lambda *a, **kw: pytest.fail('Repeated path choice'))
    monkeypatch.setattr(orch, '_pending_stages', lambda current:
        ['clarify'] if 'clarify' not in current.stage_summaries else ['design'])
    stage = orch._run_agent_stage
    advanced = []
    def execute_stage(name, current, spec_file, **kwargs):
        if name == 'design':
            assert current.status != 'waiting_user'
            assert spec_file != old
            advanced.append(name)
            raise KeyboardInterrupt()
        return stage(name, current, spec_file, **kwargs)
    def execute(**kwargs):
        return AgentResult(True, [], Path('.'), summary=('Retained scope is sufficient.\nREADY_TO_GENERATE'
            if kwargs['stage_key'].startswith('clarify-conv') else 'Generated execution contract'))
    monkeypatch.setattr(orch, '_run_agent_stage', execute_stage)
    monkeypatch.setattr(orch, '_run_agent_with_retries', execute)
    with pytest.raises(KeyboardInterrupt):
        orch.run(old, auto_approve=True)
    assert advanced == ['design']
