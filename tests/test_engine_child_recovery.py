"""Recovery through the public session entrypoint and real workflow handoffs."""
import json

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.git_ops import head_ref
from auto_agents.models import AgentResult, SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.repair_control import digest
from auto_agents.session import Session
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
from test_session import _confirm_collab_state
from test_session_verification_ownership import project, git


class ObservationBoundary(BaseException):
    pass


def parent_workflow(root, child, *, engine=False):
    store = WorkflowStore(root)
    parent = _confirm_collab_state(SessionState(
        session_id='parent', mode='collab', status='waiting_child', auto_approve=True,
        goal='Continue the already selected real project; request browser observation.',
    ), 'real')
    snapshot = store.create_root(WorkflowRef('collab', parent.session_id))
    parent.workflow_id = child.workflow_id = snapshot.workflow_id
    original = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix',
        goal=child.goal, reason='repair the saved defect', payload={'child_session_id': child.session_id,
            'head_before': head_ref(root), 'auto_approve': True})
    store.bind_child(snapshot, original, WorkflowRef('fix', child.session_id))
    child.parent_handoff_id = original.handoff_id
    child.goal_execution_environment = dict(parent.goal_execution_environment)
    child.conversation = [{'role': 'user', 'content': 'Reuse the existing project and verified provider semantics.'}]
    save_session_state(root, child)
    handoff = original
    if engine:
        store.record_result(snapshot, original, status='failed', result={'status': 'failed', 'resolution': 'verification_inconclusive'})
        store.consume_result(snapshot, original, operation_id='original-return')
        handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix',
            goal='Repair the engine then continue the saved child', reason='engine recovery',
            payload={'child_session_id': child.session_id,
                     'issue_seed': {'target_repository': str(root.parent / 'engine'), 'scope': 'session verification'}})
    parent.active_handoff_id = handoff.handoff_id
    save_session_state(root, parent)
    return store, snapshot, handoff


def resume_to_observation(root, monkeypatch, action, *, observe=None):
    def agent(self, request):
        if request.purpose.startswith('collab'):
            if observe is not None:
                observe(request)
            raise ObservationBoundary()
        state = load_session_state(root, 'owned-child')
        reply = action(state, request.prompt, request.cwd)
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', agent)
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')


def test_verified_engine_repair_resumes_bound_existing_child_once(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child, engine=True)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    calls = []
    def action(state, prompt, candidate_root):
        calls.append(state.session_id)
        assert state.goal == child.goal
        assert state.goal_execution_environment == child.goal_execution_environment
        assert state.conversation == child.conversation
        (candidate_root / 'value.py').write_text('VALUE = 1\n')
        return 'Fixed existing child\nCOMMIT_MESSAGE: Repair owned value'
    resume_to_observation(root, monkeypatch, action)
    assert load_session_state(root, child.session_id).status == 'completed'
    assert store.load_handoff(handoff.handoff_id).returned_at
    resume_to_observation(root, monkeypatch, action)
    assert calls == [child.session_id]
    assert len(list((root / '.auto-agents/state/sessions').iterdir())) == 2


def test_child_continuation_preserves_real_goal_project_and_observation_boundary(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    store, _, handoff = parent_workflow(root, child, engine=True)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    def action(state, prompt, candidate_root):
        assert state.goal_execution_environment['mode'] == 'real'
        assert state.goal_execution_environment['confirmed'] is True
        assert 'Reuse the existing project' in state.conversation[0]['content']
        (candidate_root / 'value.py').write_text('VALUE = 1\n')
        return 'Fixed\nCOMMIT_MESSAGE: Repair owned value'
    resume_to_observation(root, monkeypatch, action)
    parent = load_session_state(root, 'parent')
    assert parent.status != 'completed'
    assert parent.goal_execution_environment['mode'] == 'real'
    assert parent.goal.startswith('Continue the already selected real project')


@pytest.mark.parametrize('owner', ['engine', 'other'])
def test_engine_return_preserves_unresolved_mixed_verification_refs(tmp_path, monkeypatch, owner):
    from auto_agents.models import AgentResult

    root, child = project(tmp_path)
    # This command has both target and foreign test references. Only the
    # repository explicitly bound to the verified engine return may trigger
    # recovery classification; unrelated foreign references stay blocked.
    target_ref = 'tests/test_owned.py::test_owned'
    foreign_ref = str(tmp_path / owner / 'tests/test_contract.py::test_required')
    child.fix_verify_command = f'python -m pytest -q {target_ref} && python -m pytest -q {foreign_ref}'
    store, _, handoff = parent_workflow(root, child, engine=True)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    original_conversation = list(child.conversation)
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        assert request.purpose == 'fix_converse'
        assert child.fix_verify_command in request.prompt
        # Merely repeating the unresolved command must not attest success or
        # execute either repository's tests after classification returns.
        reply = 'FIX_DISPOSITION v1: ' + json.dumps({
            'decision': 'fix', 'summary': 'Keep the required checks',
            'verification_command': child.fix_verify_command,
        })
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    before = head_ref(root)
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    saved = load_session_state(root, child.session_id)
    assert result.status == saved.status == 'blocked'
    assert saved.resolution == 'verification_execution_binding'
    assert calls == (['fix_converse'] if owner == 'engine' else [])
    assert saved.fix_verify_command == child.fix_verify_command
    assert saved.conversation[:len(original_conversation)] == original_conversation
    assert saved.goal == child.goal
    assert saved.goal_execution_environment == child.goal_execution_environment
    entries = [row for row in saved.execution_log if row['action'] == 'engine_verification_reconciliation']
    if owner == 'engine':
        assert len(entries) == 1
        assert entries[0]['verification_command'] == child.fix_verify_command
        assert entries[0]['engine_verification_refs'] == [foreign_ref]
    else:
        assert entries == []
    assert saved.candidate_paths == {}
    assert store.load_handoff(handoff.handoff_id).result['status'] == 'blocked'
    assert head_ref(root) == before


@pytest.mark.parametrize('route_kind', ['fix', 'resume'])
@pytest.mark.parametrize('preflight', ['environment', 'contract'])
def test_engine_child_preflight_failure_preserves_unowned_work(tmp_path, monkeypatch, route_kind, preflight):
    from auto_agents.workflow_runtime import WorkflowCoordinator

    root, child = project(tmp_path)
    if preflight == 'environment':
        (root / '.conda').unlink()
        child.fix_verify_command = 'conda run -p ./.conda python -m pytest -q tests/test_owned.py::test_owned'
        expected_resolution = 'verification_execution_binding'
    else:
        child.baseline_git_ref = 'refs/auto-agents/gate-snapshots/expired'
        child.baseline_head_ref = child.lineage_head_ref = ''
        expected_resolution = 'verification_ownership'
    store, snapshot, original = parent_workflow(root, child)
    # Retain a genuine handoff checkpoint, then let a different workflow
    # advance HEAD and leave staged, unstaged, and untracked work behind.
    WorkflowCoordinator(Orchestrator(root))._ensure_handoff_checkpoint(snapshot, original)
    (root / 'foreign-committed.txt').write_text('Another workflow delivered this\n')
    git(root, 'add', 'foreign-committed.txt')
    git(root, 'commit', '-m', 'another workflow delivery')
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00bytes')
    instruction = root / '.agents/rules/auto-agents.md'
    instruction.write_text(instruction.read_text() + '\nUnrelated instruction edit.\n')
    before = {path: (root / path).read_bytes() for path in (
        'foreign.py', 'foreign-note.txt', '.agents/rules/auto-agents.md')}
    index_before = git(root, 'ls-files', '--stage', '-z')
    staged_before = git(root, 'show', ':foreign.py')
    instruction_mtime = instruction.stat().st_mtime_ns
    head_before = head_ref(root)

    payload = {'child_session_id': child.session_id,
               'issue_seed': {'target_repository': str(root.parent / 'engine'), 'scope': 'session recovery'}}
    store.record_result(snapshot, original, status='failed', result={'status': 'failed'})
    store.consume_result(snapshot, original, operation_id='original-return')
    if route_kind == 'resume':
        # The retained public resume route carries its engine binding on the
        # original child handoff, rather than on the resume wrapper.
        original.payload.update(payload)
        store.save_handoff(original)
        binding = original.payload
        payload = {'resume_handoff_id': original.handoff_id}
    else:
        binding = payload
    handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target=route_kind,
        goal='Continue the saved child after engine repair', reason='engine recovery', payload=payload)
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = handoff.handoff_id
    save_session_state(root, parent)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(binding)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))

    # Exercise Session.resume through the real provider dispatch boundary.
    # Failed preflight must return to the parent without executing a child
    # provider, claiming unrelated commits, or attempting any rollback.
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        assert request.purpose.startswith('collab')
        raise ObservationBoundary()
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    orch = Orchestrator(root)
    session = Session(orch, mode='collab', auto_approve=True)
    if preflight == 'environment':
        result = session.resume(parent.session_id)
        assert result.status == 'blocked'
        assert result.resolution == expected_resolution
        assert calls == []
    else:
        with pytest.raises(ObservationBoundary):
            session.resume(parent.session_id)
        assert calls == ['collab']

    saved = load_session_state(root, child.session_id)
    returned = store.load_handoff(handoff.handoff_id)
    assert saved.status == returned.result['status'] == 'blocked'
    assert saved.resolution == returned.result['resolution'] == expected_resolution
    assert saved.execution_log[-1]['action'] == 'execution_preflight_blocked'
    assert saved.verification_binding == {}
    assert returned.result['changed_paths'] == returned.result['commit_shas'] == []
    assert returned.result['rolled_back_paths'] == []
    assert orch._repair_route_probe_consumed == digest(binding)
    assert {path: (root / path).read_bytes() for path in before} == before
    assert git(root, 'ls-files', '--stage', '-z') == index_before
    assert git(root, 'show', ':foreign.py') == staged_before
    assert instruction.stat().st_mtime_ns == instruction_mtime
    assert head_ref(root) == head_before
    assert len(list((root / '.auto-agents/state/sessions').iterdir())) == 2


def snapshot_project(tmp_path):
    import hashlib
    import shlex
    import sys
    root, child = project(tmp_path)
    reference = root / 'provider.md'
    reference.write_text('# Provider reference\n\n**[Provider official]** Frame rate is output metadata, not a request parameter.\n')
    provenance = hashlib.sha256(reference.read_bytes()).hexdigest()
    (root / 'capabilities.json').write_text(json.dumps({'request_frame_rate': True, 'source_sha256': provenance}))
    (root / 'tests/test_owned.py').write_text(
        'import hashlib, json\nfrom pathlib import Path\n'
        'def test_owned():\n'
        '    source = Path("provider.md").read_bytes()\n'
        '    snapshot = json.loads(Path("capabilities.json").read_text())\n'
        f'    assert hashlib.sha256(source).hexdigest() == {provenance!r}\n'
        '    assert snapshot["source_sha256"] == hashlib.sha256(source).hexdigest()\n'
        '    assert snapshot["request_frame_rate"] is False\n')
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain provider source and consumer contract')
    child.baseline_head_ref = child.baseline_git_ref = head_ref(root)
    child.fix_verify_command = shlex.join([sys.executable, '-m', 'pytest', '-q', 'tests/test_owned.py::test_owned'])
    child.goal = 'Repair the provider capability snapshot against the retained official reference; preserve provenance.'
    save_session_state(root, child)
    return root, child, provenance


def test_resumed_child_delivers_snapshot_matching_retained_provider_semantics(tmp_path, monkeypatch):
    root, child, provenance = snapshot_project(tmp_path)
    store, _, handoff = parent_workflow(root, child, engine=True)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    reference = (root / 'provider.md').read_bytes()
    original = (root / 'capabilities.json').read_bytes()
    shared_head = head_ref(root)
    shared_index = git(root, 'ls-files', '--stage', '-z')
    observed = []
    def action(state, prompt, candidate_root):
        assert 'preserve provenance' in state.goal
        (candidate_root / 'capabilities.json').write_text(json.dumps({'request_frame_rate': False, 'source_sha256': provenance}))
        return 'Matched capability snapshot to retained official semantics.\nCOMMIT_MESSAGE: Align provider capability snapshot'
    def observe(request):
        observed.append(request.cwd)
        assert request.cwd != root
        assert json.loads((request.cwd / 'capabilities.json').read_text()) == {
            'request_frame_rate': False, 'source_sha256': provenance}
        assert (request.cwd / 'provider.md').read_bytes() == reference
        result = store.load_handoff(handoff.handoff_id).result
        assert head_ref(request.cwd) == result['candidate_delivery']['delivered_revision']
        assert list(result['candidate_delivery']['receipt']['manifest']) == ['capabilities.json']
    resume_to_observation(root, monkeypatch, action, observe=observe)
    assert load_session_state(root, child.session_id).status == 'completed'
    assert len(observed) == 1
    assert (root / 'capabilities.json').read_bytes() == original
    assert head_ref(root) == shared_head
    assert git(root, 'ls-files', '--stage', '-z') == shared_index
    assert (root / 'provider.md').read_bytes() == reference
    assert store.load_handoff(handoff.handoff_id).result['changed_paths'] == ['capabilities.json']


@pytest.mark.parametrize('mutation', ['stale', 'forged', 'unsupported'])
def test_snapshot_repair_rejects_stale_or_forged_provenance(tmp_path, monkeypatch, mutation):
    root, child, provenance = snapshot_project(tmp_path)
    store, _, handoff = parent_workflow(root, child, engine=True)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    before = (root / 'capabilities.json').read_bytes()
    def action(state, prompt, candidate_root):
        if mutation == 'forged':
            (candidate_root / 'provider.md').write_text('A replacement source grants request frame rate control.')
        (candidate_root / 'capabilities.json').write_text(json.dumps({
            'request_frame_rate': mutation == 'unsupported',
            'source_sha256': provenance if mutation == 'unsupported' else '0' * 64,
        }))
        return 'Candidate snapshot ready'
    resume_to_observation(root, monkeypatch, action)
    assert load_session_state(root, child.session_id).status != 'completed'
    assert store.load_handoff(handoff.handoff_id).result['status'] == 'failed'
    assert (root / 'capabilities.json').read_bytes() == before
