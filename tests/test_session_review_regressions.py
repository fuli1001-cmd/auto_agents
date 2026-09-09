"""Public-session counterexamples from the ownership integration review."""
import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from auto_agents.config import load_project_config, save_project_config, save_session_state, save_task_plan, load_session_state, load_run_state, save_run_state
from auto_agents.git_ops import head_ref
from auto_agents.models import AgentResult, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from test_engine_child_recovery import ObservationBoundary, parent_workflow, resume_to_observation
from test_session_verification_ownership import git, project, run_session


def result(request, reply='Fixed\nCOMMIT_MESSAGE: Repair owned value'):
    request.output_path.write_text(reply)
    return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                       summary=reply, stdout=reply, returncode=0)


@pytest.mark.parametrize('owned_status', ['done', 'pending'])
def test_resumed_snapshot_repair_excludes_foreign_pending_gates_already_in_baseline(tmp_path, monkeypatch, owned_status):
    root, child = project(tmp_path)
    snapshot = 'config/provider_capabilities.json'
    (root / 'config').mkdir()
    (root / snapshot).write_text('{"request_frame_rate": true}')
    (root / 'tests/test_owned.py').write_text(
        'import json\nfrom pathlib import Path\n'
        'def test_owned():\n'
        f'    assert json.loads(Path({snapshot!r}).read_text())["request_frame_rate"] is False\n')
    marker = tmp_path / 'regression-ran'
    (root / 'tests/test_concurrency.py').write_text(
        'from pathlib import Path\ndef test_existing():\n'
        f'    Path({str(marker)!r}).write_text("ran")\n')
    config = load_project_config(root)
    config.gates.steps[0].impact_paths = ['app/provider.py']
    config.gates.steps[0].levels = ['affected']
    config.gates.steps[0].risk = 'critical'
    future = [f'tests/test_concurrency.py::test_future_{index}' for index in range(4)]
    config.gates.steps.extend(VerificationStep(
        runner='pytest', targets=[ref], proof_id=f'future.{index}', levels=['affected'],
        impact_paths=['app/resume.py'], risk='critical', depends_on_proofs=['owned.contract'],
    ) for index, ref in enumerate(future))
    config.gates.steps.append(VerificationStep(
        runner='pytest', targets=['tests/test_concurrency.py::test_existing'], proof_id='existing.regression',
        levels=['release'], depends_on_proofs=['owned.contract', *[f'future.{i}' for i in range(4)]],
    ))
    config.gates.release_blocking_paths = []
    config.gates.unmapped_change_policy = 'fallback'
    config.gates.fallback_proof_ids = [f'future.{i}' for i in range(4)]
    save_project_config(root, config)
    tasks = [
        {'task_id': 'task-owned', 'title': 'Snapshot repair', 'status': owned_status,
         'requirement_ids': ['REQ-snapshot'], 'verification_refs': ['tests/test_owned.py::test_owned']},
        {'task_id': 'task-foreign', 'title': 'Pending resume work', 'status': 'pending',
         'requirement_ids': ['REQ-concurrency'], 'verification_refs': future,
         'requirement_proofs': [{'requirement_id': 'REQ-concurrency', 'status': 'planned', 'evidence_refs': [ref]} for ref in future]},
    ]
    save_task_plan(root, {'tasks': tasks, 'verification_steps': [step.to_dict() for step in config.gates.steps],
                          'verification_policy_version': 4})
    run = load_run_state(root)
    run.status = 'pending'
    run.current_stage = 'implement'
    run.resume_context['workflow_id'] = 'foreign-workflow'
    save_run_state(root, run)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'baseline already contains another pending workflow')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    child.fix_verify_command = shlex.join([sys.executable, '-m', 'pytest', '-q', 'tests/test_owned.py::test_owned'])
    save_session_state(root, child)
    (root / '.auto-agents/state/sessions' / child.session_id / 'issue.json').write_text(
        json.dumps({'task_id': 'task-owned', 'requirement_ids': ['REQ-snapshot']}))
    before = {path: (root / path).read_bytes() for path in ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    plans = []
    resolve = Session._session_gate_plan
    def observe(self, scope):
        plan = resolve(self, scope)
        plans.extend(plan.commands)
        return plan
    monkeypatch.setattr(Session, '_session_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        (request.cwd / snapshot).write_text('{"request_frame_rate": false}')
        return result(request)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    assert saved.status == 'completed', saved.to_dict()
    assert set(saved.candidate_paths) == {snapshot}
    assert marker.read_text() == 'ran'
    assert 'owned.contract' in {step['proof_id'] for step in saved.verification_binding['gates']['steps']}
    assert not any(ref in command for ref in future for command in plans)
    assert any('test_existing' in command for command in plans)
    assert {path: (root / path).read_bytes() for path in before} == before


@pytest.mark.parametrize('owner', ['same', 'unknown', 'explicit'])
def test_pending_requirements_are_retained_without_proven_foreign_ownership(tmp_path, monkeypatch, owner):
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore

    root, child = project(tmp_path, missing=True)
    workflow = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id))
    child.workflow_id = workflow.workflow_id
    run = load_run_state(root)
    if owner != 'unknown':
        run.resume_context['workflow_id'] = child.workflow_id if owner == 'same' else 'foreign-workflow'
    save_run_state(root, run)
    if owner == 'explicit':
        (root / '.auto-agents/state/sessions' / child.session_id / 'issue.json').write_text(json.dumps({'task_id': 'task-owned'}))
    ref = 'tests/test_owned.py::test_missing'
    save_task_plan(root, {'tasks': [{'task_id': 'task-owned', 'title': 'Still required', 'status': 'pending',
                                   'requirement_ids': ['REQ-owned'], 'verification_refs': [ref]}]})
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'pending requirement still belongs to this session')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    saved, _, _ = run_session(root, monkeypatch)
    assert saved.status != 'completed'
    failure = next(iter(saved.verification_diagnostics.values()))
    assert failure['failure_kind'] == 'verification_entry_unavailable'
    assert ref in failure['diagnostic']['command']
    assert failure['diagnostic']['owners'][0]['task_id'] == 'task-owned'


@pytest.mark.parametrize('failure', [False, True])
def test_concurrent_provider_window_changes_are_never_claimed_or_rolled_back(tmp_path, monkeypatch, failure):
    root, child = project(tmp_path, missing=failure)
    store, _, handoff = parent_workflow(root, child)
    (root / 'foreign-unstaged.txt').write_text('original\n')
    owned_test = root / 'tests/test_owned.py'
    owned_test.write_text(owned_test.read_text()
        + '    assert Path("foreign-unstaged.txt").read_text() == "original\\n"\n'
        + '    assert not Path("late-foreign.txt").exists()\n')
    git(root, 'add', 'foreign-unstaged.txt', 'tests/test_owned.py')
    git(root, 'commit', '-m', 'existing foreign file')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    # This actor runs while the provider's private workspace is writable.
    # All three edits are made in the live workspace, outside that writer.
    def provider(state, prompt, candidate):
        (candidate / 'value.py').write_text('VALUE = 1\n')
        (root / 'foreign.py').write_text('VALUE = 8\n')
        git(root, 'add', 'foreign.py')
        (root / 'foreign.py').write_text('VALUE = 9\n')
        (root / 'foreign-unstaged.txt').write_text('concurrent unstaged\n')
        (root / 'late-foreign.txt').write_bytes(b'concurrent\x00untracked')
        return 'Owned candidate ready\nCOMMIT_MESSAGE: Repair owned value'
    resume_to_observation(root, monkeypatch, provider)
    saved = load_session_state(root, child.session_id)
    returned = store.load_handoff(handoff.handoff_id)
    assert saved.status == ('failed' if failure else 'completed'), saved.to_dict()
    assert returned.result.get('rolled_back_paths', []) == []
    assert not {'foreign.py', 'foreign-unstaged.txt', 'late-foreign.txt'} & set(saved.candidate_paths)
    assert git(root, 'show', ':foreign.py') == 'VALUE = 8\n'
    assert (root / 'foreign.py').read_text() == 'VALUE = 9\n'
    assert (root / 'foreign-unstaged.txt').read_text() == 'concurrent unstaged\n'
    assert (root / 'late-foreign.txt').read_bytes() == b'concurrent\x00untracked'
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    if not failure:
        assert git(Path(saved.candidate_custody['checkout']), 'show',
                   saved.candidate_custody['delivered_revision'] + ':value.py') == 'VALUE = 1\n'


@pytest.mark.parametrize('launcher', ['python', 'conda'])
@pytest.mark.parametrize('separator', [' && ', '; ', ' || '], ids=['and', 'sequence', 'or'])
@pytest.mark.parametrize('intervening', [False, True], ids=['pytest-only', 'mixed'])
def test_compound_pytest_preflight_checks_later_nodes_before_any_body(tmp_path, monkeypatch, launcher, separator, intervening):
    root, child = project(tmp_path)
    marker = tmp_path / 'test-body-ran'
    (root / 'tests/test_present.py').write_text(
        'from pathlib import Path\ndef test_present():\n'
        f'    Path({str(marker)!r}).write_text("executed")\n')
    (root / 'tests/test_missing.py').write_text('def test_different():\n    pass\n')
    prefix = [sys.executable, '-m', 'pytest', '-q']
    if launcher == 'conda':
        conda = shutil.which('conda')
        assert conda, 'the trusted test environment must provide Conda'
        (root / '.conda').unlink()
        (root / '.conda/conda-meta').mkdir(parents=True)
        (root / '.conda/bin').mkdir()
        (root / '.conda/bin/python').symlink_to(sys.executable)
        prefix = [conda, 'run', '-p', './.conda', 'python', '-m', 'pytest', '-q']
    present, missing = 'tests/test_present.py::test_present', 'tests/test_missing.py::test_missing'
    commands = [shlex.join([*prefix, present]), shlex.join([*prefix, missing])]
    setup_marker = tmp_path / 'intervening-command-ran'
    if intervening:
        commands.insert(1, shlex.join([sys.executable, '-c',
            f'from pathlib import Path; Path({str(setup_marker)!r}).write_text("executed")']))
    child.fix_verify_command = separator.join(commands)
    save_task_plan(root, {'tasks': [{'task_id': 'task-owned', 'title': 'Required compound check',
        'requirement_ids': ['REQ-compound'], 'verification_refs': [present, missing]}]})
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'compound required verification')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    saved, _, _ = run_session(root, monkeypatch)
    assert saved.status != 'completed'
    assert not marker.exists(), 'a test body ran before entry validation completed'
    assert not setup_marker.exists(), 'a non-collection command ran during entry validation'
    diagnostic = next(iter(saved.verification_diagnostics.values()))['diagnostic']
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['owners'][0]['task_id'] == 'task-owned'
    assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-compound']
    assert diagnostic['contract_fingerprint'] == saved.verification_binding['contract_fingerprint']
    assert missing in diagnostic['command'] and 'test_missing' in diagnostic['output']


@pytest.mark.parametrize('separator, executes_action', [(' && ', True), ('; ', True), (' || ', False)],
                         ids=['and', 'sequence', 'or'])
def test_mixed_command_execution_preserves_conditional_actions(tmp_path, monkeypatch, separator, executes_action):
    root, child = project(tmp_path)
    marker = tmp_path / 'execution-action'
    pytest_command = shlex.join([sys.executable, '-m', 'pytest', '-q', 'tests/test_owned.py::test_owned'])
    action = shlex.join([sys.executable, '-c',
                        f'from pathlib import Path; Path({str(marker)!r}).write_text("executed")'])
    child.fix_verify_command = separator.join([pytest_command, action, pytest_command])
    save_session_state(root, child)
    saved, _, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert saved.fix_verify_command == child.fix_verify_command
    assert marker.exists() is executes_action


@pytest.mark.parametrize('status', ['paused', 'waiting_user', 'waiting_child'])
def test_engine_child_nonterminal_receipt_stays_active_until_same_child_finishes(tmp_path, monkeypatch, status):
    from auto_agents.repair_control import digest

    root, child = project(tmp_path)
    store, _, handoff = parent_workflow(root, child, engine=True)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    ready = False
    visits = []
    drive = Session._drive_local
    def child_boundary(self, state):
        if state.session_id == child.session_id:
            visits.append(state.session_id)
            if not ready:
                # Model the native session boundary emitted by an asynchronous
                # prerequisite. Public resume must keep its durable route.
                state.status = status
                state.resolution = 'prerequisite_pending'
                save_session_state(root, state)
                return state
            # The prerequisite signals readiness; execution and verification
            # below use the real session driver and isolated provider writer.
            state.status, state.resolution = 'executing', ''
        return drive(self, state)
    monkeypatch.setattr(Session, '_drive_local', child_boundary)
    calls = []
    def provider(self, request):
        if request.purpose.startswith('collab'):
            raise ObservationBoundary()
        calls.append(request.purpose)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        return result(request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    parent = Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    pending = store.load_handoff(handoff.handoff_id)
    assert parent.status == 'waiting_child'
    assert parent.active_handoff_id == handoff.handoff_id
    assert pending.status == status and not pending.returned_at
    assert pending.result['session_id'] == child.session_id
    assert calls == []
    ready = True
    monkeypatch.delenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE')
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    assert visits == [child.session_id, child.session_id]
    assert calls == ['fix']
    assert load_session_state(root, child.session_id).status == 'completed'
    assert store.load_handoff(handoff.handoff_id).returned_at
    assert len(list((root / '.auto-agents/state/sessions').iterdir())) == 2
