"""Public session recovery regressions with real Git and pytest execution."""
import json
import shlex
import subprocess
import sys
from pathlib import Path
import pytest
from execution_marker import ExecutionMarker
from auto_agents.config import load_project_config, save_project_config, save_session_state, load_session_state, save_task_plan
from auto_agents.git_ops import head_ref
from auto_agents.models import AgentResult, SessionState, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from test_session import _make_project

def git(root, *args):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout

def project(tmp_path, *, missing=False):
    root = _make_project(str(tmp_path))
    (root / '.conda').symlink_to(sys.prefix, target_is_directory=True)
    (root / 'value.py').write_text('VALUE = 0\n')
    (root / 'foreign.py').write_text('VALUE = 7\n')
    (root / 'tests').mkdir(exist_ok=True)
    (root / 'tests/test_owned.py').write_text('from pathlib import Path\ndef test_owned():\n    assert "VALUE = 1" in Path("value.py").read_text()\n    assert "VALUE = 7" in Path("foreign.py").read_text()\n    assert not Path("foreign-note.txt").exists()\n')
    config = load_project_config(root)
    config.gates.steps = [VerificationStep(runner='pytest', targets=['tests/test_owned.py::test_missing' if missing else 'tests/test_owned.py::test_owned'], proof_id='owned.contract', levels=['affected', 'release'], impact_paths=['value.py'])]
    config.gates.verification_policy_version = 4
    config.gates.release_worker.enabled = False
    config.gates.release_worker.auto_start = False
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-owned', 'title': 'Owned contract', 'requirement_ids': ['REQ-owned'], 'verification_refs': config.gates.steps[0].targets}], 'verification_steps': [s.to_dict() for s in config.gates.steps], 'verification_policy_version': 4})
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'owned contract')
    state = SessionState(session_id='owned-child', status='failed', mode='fix', goal='Repair the existing value', auto_approve=True, baseline_head_ref=head_ref(root), baseline_git_ref=head_ref(root))
    save_session_state(root, state)
    return (root, state)

def run_session(root, monkeypatch, *, mutate=True):
    orch = Orchestrator(root, user_input_fn=lambda *_args, **_kw: 'y')
    calls = []

    def agent(request):
        calls.append(request.purpose)
        if mutate:
            (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Repaired value.\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path, summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', agent)
    result = Session(orch, mode='fix', auto_approve=True).resume('owned-child')
    return (result, calls, orch)

def _retain_contract(root, child, config, plan):
    child.hard_ceiling = 1
    save_project_config(root, config)
    save_task_plan(root, plan)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain session regression contract')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)

def _binding_fixture(root, child):
    from auto_agents.authorization import authorization_policy_for_state
    from auto_agents.session_verification import bind_session
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
    if not child.workflow_id:
        child.workflow_id = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id)).workflow_id
    child.authorization_policy = authorization_policy_for_state(auto_approve=True).to_dict()
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    bind_session(session, child)
    return session

def _switch_ambient_binding_plan(root):
    from auto_agents.config import load_run_state, save_run_state
    config = load_project_config(root)
    config.gates.steps = [VerificationStep(proof_id='foreign.future', runner='pytest', targets=['tests/test_future.py::test_future'], levels=['affected', 'release'], impact_paths=['**'])]
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-foreign', 'title': 'Foreign pending task', 'workflow_id': 'foreign-workflow', 'status': 'pending', 'verification_refs': ['foreign.future']}], 'verification_steps': [step.to_dict() for step in config.gates.steps]})
    run = load_run_state(root)
    run.resume_context['workflow_id'] = 'foreign-workflow'
    save_run_state(root, run)
    return {name: (root / name).read_bytes() for name in ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}

def _assert_binding_blocked_before_execution(root, monkeypatch, *, parent=False):
    before_child = load_session_state(root, 'owned-child').to_dict()

    def baseline(*args, **kwargs):
        pytest.fail('Unresolved session authority must block before baseline capture')
    monkeypatch.setattr(Session, '_ensure_baseline', baseline)
    if parent:
        from workflow_support import ObservationBoundary, resume_to_observation
        collab_loop = Session._phase_collab_loop

        def parent_boundary(self, state):
            if state.session_id == 'parent':
                raise ObservationBoundary()
            return collab_loop(self, state)
        monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)

        def writer(*args):
            pytest.fail('Unresolved session authority must block before the writer')
        monkeypatch.setattr(Orchestrator, '_call_with_failover', writer)
        try:
            stopped = Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
        except ObservationBoundary:
            pass
        else:
            assert stopped.status in {'failed', 'blocked'}
            persisted = load_session_state(root, 'parent')
            assert persisted.status == 'blocked' and persisted.resolution == 'verification_ownership'
            assert 'conflicting child identities' in persisted.execution_log[-1]['result']
        saved = load_session_state(root, 'owned-child')
        if saved.status == 'failed':
            assert saved.to_dict() == before_child
            rejected = load_session_state(root, 'parent')
            assert rejected.status == 'blocked' and rejected.resolution == 'verification_ownership'
            diagnostic = rejected.execution_log[-1]['diagnostic']
            assert diagnostic['session_id'] == saved.session_id and diagnostic['retry_fix'] is False
            assert len(diagnostic['child_session_ids']) > 1
            return saved
    else:
        saved, calls, _ = run_session(root, monkeypatch)
        assert calls == []
    assert saved.status == 'blocked', saved.to_dict()
    assert saved.resolution == 'verification_ownership'
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['session_id'] == saved.session_id
    assert diagnostic['workflow_id'] == saved.workflow_id
    assert diagnostic['retry_fix'] is False
    return saved

def _prepare_binding_child_resume(root, store, snapshot, handoff):
    store.record_result(snapshot, handoff, status='failed', result={'status': 'failed', 'resolution': 'verification_ownership'})
    store.consume_result(snapshot, handoff, operation_id='retained-binding-failure')
    resume = store.prepare_handoff(snapshot, parent=snapshot.root, target='resume', goal=handoff.goal, reason='Resume retained child authority', payload={'resume_handoff_id': handoff.handoff_id})
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = resume.handoff_id
    save_session_state(root, parent)

def _supervised_public_resume(tmp_path, monkeypatch, scope, *, recover):
    from copy import deepcopy
    import shutil
    from auto_agents.config import load_task_plan
    from auto_agents.gate_execution import LocalGatePlanExecutor
    from auto_agents.session_verification import fingerprint
    from auto_agents.verification_input_trace import owner_identity, resolved_trace
    from auto_agents.verification_supervisor_checks import observation, LEGACY_SHA256
    from workflow_support import configure_local_writer, parent_workflow, resume_to_observation, ObservationBoundary
    owner = owner_identity()
    if not owner['metadata']:
        import os
        import auto_agents.verification_sandbox as sandbox
        name = 'test_public_resume_authenticated_history_with_supervised_input_tracing' if recover else 'test_public_resume_global_plan_switch_with_supervised_input_tracing'
        node = str(Path(__file__).resolve()) + '::' + name + '[' + scope + ']'
        command = [sys.executable, str(Path(sandbox.__file__).resolve()), '--metadata', json.dumps({'roots': [str(tmp_path), '/tmp'], 'supervisor_checks': True}), sys.executable, '-m', 'pytest', '-q', '--basetemp', str(tmp_path / 'supervised'), node]
        result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=240, env={**os.environ, 'AUTO_AGENTS_VERIFICATION_SANDBOX': '1'})
        assert result.returncode == 0, result.stdout + result.stderr
        return
    assert owner['metadata'] == owner['trace'] == 1, owner
    assert shutil.which('strace'), 'the retained tracing acceptance requires strace'
    if recover:
        legacy = observation('legacy_owner')
        assert legacy['launcher_pid'] > 0 and legacy['source_root']
        assert legacy['returncode'] == 0 and legacy['count'] == 'executed\n'
        assert legacy['legacy_sha256'] == LEGACY_SHA256 and legacy['shared_unchanged']
        record = legacy['trace']
        assert record['owner']['metadata'] == 1 and record['owner']['trace'] == 0
        assert record['reason'] == 'live owner has no input tracing'
        assert record['complete'] is False
        assert resolved_trace(json.dumps(record)) is None
    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    shared = [root / 'foreign.py', root / '.git/index']
    controls = "import errno, os, tempfile\nfrom pathlib import Path\nwith tempfile.NamedTemporaryFile(dir=os.environ['TMPDIR']) as private:\n    path = Path(private.name)\n    path.chmod(0o750)\n    assert path.stat().st_mode & 0o777 == 0o750\n    os.fchmod(private.fileno(), 0o640)\n    os.utime(path, ns=(123, 456))\n    assert path.stat().st_mtime_ns == 456\n    os.utime(private.fileno(), ns=(123, 789))\n    assert path.stat().st_mtime_ns == 789\n    assert path.stat().st_mode & 0o777 == 0o640\nfor name in SHARED:\n    path = Path(name)\n    before = path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns\n    fd = os.open(path, os.O_RDONLY)\n    try:\n        for action in (lambda: path.write_bytes(b'forbidden'),\n                       lambda: path.chmod(0o777), lambda: os.fchmod(fd, 0o777),\n                       lambda: os.chmod('/proc/self/fd/' + str(fd), 0o777),\n                       lambda: Path('/proc/self/fd/' + str(fd)).write_bytes(b'forbidden'),\n                       lambda: os.utime(path, ns=(1, 1)), lambda: os.utime(fd, ns=(1, 1))):\n            try:\n                action()\n            except OSError as error:\n                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error\n            else:\n                raise AssertionError('shared input escaped confinement')\n    finally:\n        os.close(fd)\n    assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before\n"
    probe = 'SHARED = ' + repr(list(map(str, shared))) + '\n' + controls
    owned_test = root / 'tests/test_owned.py'
    owned_test.write_text(owned_test.read_text() + '\n' + 'def test_confinement():\n' + ''.join(('    ' + line + '\n' for line in probe.splitlines())))
    (root / 'tests/test_setup.py').write_text('from pathlib import Path\ndef test_setup():\n    assert Path("value.py").read_text() == "VALUE = 1\\n"\n')
    config, plan = (load_project_config(root), load_task_plan(root))
    step = config.gates.steps[0]
    step.targets.append('tests/test_owned.py::test_confinement')
    step.depends_on_proofs = ['shared.setup']
    step.cache_scope = 'source'
    step.result_cache_scope = 'observed_inputs' if scope == 'observed_inputs' else 'auto'
    assert not step.artifact_globs and (not step.exclusive_resources) and (not step.dynamic_ports)
    config.gates.steps.append(VerificationStep(proof_id='shared.setup', runner='pytest', targets=['tests/test_setup.py::test_setup'], levels=['affected', 'release']))
    plan['tasks'][0]['workflow_id'] = child.workflow_id
    plan['tasks'][0]['verification_refs'] = list(step.targets)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Retained prerequisite owner', 'workflow_id': 'foreign-workflow', 'status': 'pending', 'requirement_ids': ['REQ-foreign'], 'verification_refs': ['shared.setup']})
    plan['verification_steps'] = [item.to_dict() for item in config.gates.steps]
    _retain_contract(root, child, config, plan)
    configure_local_writer(root, child, probe + '\nPath("value.py").write_text("VALUE = 1\\n")')
    child.baseline_git_ref = 'refs/auto-agents/gate-snapshots/retained-supervised'
    git(root, 'update-ref', child.baseline_git_ref, child.baseline_head_ref)
    _binding_fixture(root, child)
    retained = deepcopy(child.verification_binding)
    if recover:
        child.verification_binding['schema_version'] = 11
        for key in ('execution_environment', 'source_provenance', 'session_mode'):
            child.verification_binding.pop(key)
        child.verification_binding['binding_fingerprint'] = fingerprint({key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
    save_session_state(root, child)
    _prepare_binding_child_resume(root, store, snapshot, handoff)
    ambient = _switch_ambient_binding_plan(root) if not recover else {name: (root / name).read_bytes() for name in ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    before = {str(path): (path.read_bytes(), path.stat().st_mode) for path in shared}
    calls, results, resumed = ([], [], [])
    run, retain_authority = (LocalGatePlanExecutor.run, Session._retain_resume_authority)

    def observe_run(executor, command, **kwargs):
        result = run(executor, command, **kwargs)
        results.append((command, executor.metadata.get(command), result, owner_identity()))
        return result

    def observe_authority(session, state):
        resumed.append(state.session_id)
        return retain_authority(session, state)
    monkeypatch.setattr(LocalGatePlanExecutor, 'run', observe_run)
    monkeypatch.setattr(Session, '_retain_resume_authority', observe_authority)
    collab_loop = Session._phase_collab_loop

    def parent_boundary(session, state):
        if state.session_id == 'parent':
            raise ObservationBoundary()
        return collab_loop(session, state)
    monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)

    def observe_writer(state, prompt, candidate_root):
        calls.append(state.session_id)
        assert state.goal_execution_environment == child.goal_execution_environment
        assert state.goal == child.goal and state.parent_handoff_id == handoff.handoff_id
        assert candidate_root != root
        assert (candidate_root / 'value.py').read_text() == 'VALUE = 0\n'
    resume_to_observation(root, monkeypatch, observe_writer, real_dispatch=True)
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == [child.session_id] and child.session_id in resumed
    binding = saved.verification_binding
    for key in ('repository', 'session_id', 'workflow_id', 'original_handoff_id', 'authorization', 'task_scope', 'task_ids', 'requirement_ids', 'tasks', 'contract_revision', 'contract_fingerprint', 'gates', 'plan', 'baseline_identity', 'required_proof_ids', 'proof_owners', 'regression_dependencies'):
        assert binding[key] == retained[key], key
    assert binding['schema_version'] == 13
    assert binding['execution_environment'] == child.goal_execution_environment
    assert binding['source_provenance']['revision'] == child.baseline_head_ref
    assert binding['required_proof_ids'] == ['owned.contract', 'shared.setup']
    assert {item['task_id'] for item in binding['proof_owners']['shared.setup']} == {'task-owned', 'task-foreign'}
    selected = [(command, result, negotiated) for command, metadata, result, negotiated in results if metadata and metadata.cache_scope == 'source' and (metadata.result_cache_scope == step.result_cache_scope) and result.ok and (not result.cached)]
    assert selected, [(command, result.to_dict()) for command, _, result, _ in results]
    assert any(('test_confinement' in command for command, _, _ in selected))
    assert any(('test_setup' in command and result.ok and (not result.cached) for command, _, result, _ in results))
    for command, result, negotiated in selected:
        assert negotiated == owner
        assert result.returncode == 0 and result.backend == 'local-isolated'
        assert result.input_trace_complete or result.input_trace_reason
        print(json.dumps({'command': command, 'owner': negotiated, 'returncode': result.returncode, 'input_trace_complete': result.input_trace_complete, 'input_trace_reason': result.input_trace_reason}))
    if recover:
        custody = deepcopy(saved.candidate_custody)
        assert saved.baseline_git_ref.startswith('refs/auto-agents/gate-snapshots/')
        for repository in (root, Path(custody['checkout'])):
            for ref in {child.baseline_git_ref, saved.baseline_git_ref}:
                git(repository, 'update-ref', '-d', ref)
            expired = subprocess.run(['git', 'rev-parse', '--verify', saved.baseline_git_ref], cwd=repository, capture_output=True, text=True)
            assert expired.returncode != 0, 'the disposable baseline must really be unavailable'
            assert git(repository, 'rev-parse', '--verify', child.baseline_head_ref).strip() == child.baseline_head_ref
        _prepare_binding_child_resume(root, store, store.load(snapshot.workflow_id), store.load_handoff(handoff.handoff_id))
        parent = load_session_state(root, 'parent')
        parent.status = 'waiting_child'
        save_session_state(root, parent)
        resumed.clear()
        resume_to_observation(root, monkeypatch, observe_writer, real_dispatch=True)
        repeated = load_session_state(root, child.session_id)
        assert repeated.status == 'completed' and child.session_id in resumed
        assert calls == [child.session_id]
        assert repeated.candidate_custody == custody
        assert repeated.verification_binding == binding
    else:
        assert not (root / 'tests/test_future.py').exists()
        assert all(('test_future' not in command for command, _, _, _ in results))
        assert load_project_config(root).gates.steps[0].proof_id == 'foreign.future'
        assert load_task_plan(root)['tasks'][0]['status'] == 'pending'
    assert {name: (root / name).read_bytes() for name in ambient} == ambient
    assert {str(path): (path.read_bytes(), path.stat().st_mode) for path in shared} == before

def _assert_reused_session_authority(tmp_path, monkeypatch):
    from copy import deepcopy
    from auto_agents.config import load_task_plan
    import auto_agents.session as module
    root, first = project(tmp_path)
    second = deepcopy(first)
    second.session_id = 'second-child'
    config = load_project_config(root)
    config.gates.steps[0].args = ['--strict-markers']
    plan = load_task_plan(root)
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, second, config, plan)
    save_session_state(root, second)
    assert first.baseline_head_ref != second.baseline_head_ref
    ambient = _switch_ambient_binding_plan(root)
    orch = Orchestrator(root)
    session = Session(orch, mode='fix', auto_approve=True)
    observed, captures, writers = ([], [], [])
    execute = module.run_gate_plan
    retain = session._retain_resume_authority

    def observe(commands, *args, **kwargs):
        observed.extend(((session._current_state.session_id, command) for command in commands))
        return execute(commands, *args, **kwargs)

    def capture(state):
        result = retain(state)
        original = session._resumed_verification_state
        captures.append((state.session_id, original.session_id, original.baseline_head_ref))
        assert original is not state
        return result

    def writer(request):
        writers.append(session._current_state.session_id)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path, summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(module, 'run_gate_plan', observe)
    monkeypatch.setattr(session, '_retain_resume_authority', capture)
    monkeypatch.setattr(orch, '_call_with_failover', writer)
    for child in (first, second, first, first, second, second):
        result = session.resume(child.session_id)
        assert result.status == 'completed', result.to_dict()
        assert result.verification_binding['contract_revision'] == child.baseline_head_ref
        args = result.verification_binding['gates']['steps'][0]['args']
        assert ('--strict-markers' in args) == (child is second)
    assert session._resumed_verification_state is None
    assert writers == [first.session_id, second.session_id]
    assert all((requested == captured for requested, captured, _ in captures))
    for child in (first, second):
        commands = [command for sid, command in observed if sid == child.session_id and '--collect-only' not in command]
        assert commands
        assert all((('--strict-markers' in command) == (child is second) for command in commands))
    assert {name: (root / name).read_bytes() for name in ambient} == ambient

def _assert_public_missing_scope_recovery(tmp_path, monkeypatch, shape):
    from copy import deepcopy
    from auto_agents.session_verification import fingerprint
    from workflow_support import parent_workflow, resume_to_observation, ObservationBoundary
    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    if shape == 'missing_scope_requirement':
        handoff.payload.pop('task_id')
        handoff.payload['requirement_ids'] = ['REQ-owned']
        store.save_handoff(handoff)
    _binding_fixture(root, child)
    expected_scope = deepcopy(child.verification_binding.pop('task_scope'))
    child.verification_binding['schema_version'] = 13 if shape == 'missing_scope_current' else 11
    child.verification_binding['binding_fingerprint'] = fingerprint({key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
    if shape == 'missing_scope_unresolved':
        handoff.payload.pop('task_id')
        store.save_handoff(handoff)
    elif shape == 'missing_scope_conflict':
        handoff.payload['child_session_id'] = 'another-child'
        store.save_handoff(handoff)
    elif shape == 'missing_scope_fingerprint':
        child.verification_binding['binding_fingerprint'] = 'invalid-fingerprint'
    retained = deepcopy(child.verification_binding)
    save_session_state(root, child)
    _prepare_binding_child_resume(root, store, snapshot, handoff)
    ambient = _switch_ambient_binding_plan(root)
    handoff_path = root / '.auto-agents/state/handoffs' / (handoff.handoff_id + '.json')
    handoff_bytes = handoff_path.read_bytes()
    if shape in {'missing_scope_unresolved', 'missing_scope_conflict', 'missing_scope_fingerprint'}:
        for _ in range(2):
            saved = _assert_binding_blocked_before_execution(root, monkeypatch, parent=True)
            assert saved.verification_binding == retained
            diagnostic = (load_session_state(root, 'parent') if shape == 'missing_scope_conflict' else saved).execution_log[-1]['diagnostic']
            assert diagnostic['handoff_id'] == handoff.handoff_id
            assert diagnostic['contract_fingerprint'] == retained['contract_fingerprint']
            assert diagnostic['retry_fix'] is False
        assert (root / 'value.py').read_text() == 'VALUE = 0\n'
        assert handoff_path.read_bytes() == handoff_bytes
    else:
        collab_loop = Session._phase_collab_loop

        def parent_boundary(self, state):
            if state.session_id == 'parent':
                raise ObservationBoundary()
            return collab_loop(self, state)
        monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)
        calls = []

        def writer(state, prompt, candidate_root):
            calls.append(state.session_id)
            (candidate_root / 'value.py').write_text('VALUE = 1\n')
            return 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        resume_to_observation(root, monkeypatch, writer)
        saved = load_session_state(root, child.session_id)
        assert saved.status == 'completed', saved.to_dict()
        assert calls == [child.session_id]
        binding = saved.verification_binding
        assert binding['task_scope'] == expected_scope
        assert binding['required_proof_ids'] == ['owned.contract']
        for key in ('repository', 'session_id', 'workflow_id', 'original_handoff_id', 'authorization', 'contract_revision', 'contract_fingerprint', 'tasks', 'plan'):
            assert binding[key] == retained[key]
        assert binding['binding_fingerprint'] == fingerprint({key: value for key, value in binding.items() if key != 'binding_fingerprint'})
        resume_to_observation(root, monkeypatch, writer)
        assert calls == [child.session_id]
        assert load_session_state(root, child.session_id).verification_binding == binding
    assert store.load_handoff(handoff.handoff_id).payload == handoff.payload
    assert {name: (root / name).read_bytes() for name in ambient} == ambient

def _retain_foreign_prerequisite(root, child):
    from auto_agents.config import load_task_plan
    marker = ExecutionMarker(root.parent / 'shared-prerequisite-executed')
    (root / 'tests/test_shared.py').write_text(f"""from pathlib import Path\ndef test_setup():\n    value = Path("value.py").read_text()\n    assert value == "VALUE = 1\\n"\n    {marker.source('value')}\n""")
    config, plan = (load_project_config(root), load_task_plan(root))
    config.gates.steps[0].depends_on_proofs = ['shared.setup']
    config.gates.steps.append(VerificationStep(proof_id='shared.setup', runner='pytest', targets=['tests/test_shared.py::test_setup'], levels=['affected', 'release']))
    foreign = next((task for task in plan['tasks'] if task['task_id'] == 'task-foreign'), None)
    if foreign is None:
        foreign = {'task_id': 'task-foreign', 'title': 'Existing shared prerequisite', 'workflow_id': 'foreign-workflow', 'status': 'pending', 'requirement_ids': ['REQ-foreign'], 'verification_refs': []}
        plan['tasks'].append(foreign)
    foreign['verification_refs'].append('shared.setup')
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text(json.dumps({'task_id': 'task-owned'}))
    return marker

def _retain_legacy_prerequisite_owners(state):
    """The original inventory required prerequisites without propagating owners."""
    from auto_agents.session_verification import fingerprint
    binding = state.verification_binding
    assert binding['required_proof_ids'] == ['owned.contract', 'shared.setup']
    for key in ('proof_graph', 'proof_inventory_version', 'required_references'):
        binding.pop(key, None)
    binding['proof_owners'] = {'owned.contract': binding['proof_owners']['owned.contract']}
    binding['task_ids'], binding['requirement_ids'] = (['task-owned'], ['REQ-owned'])
    binding['binding_fingerprint'] = fingerprint({k: v for k, v in binding.items() if k != 'binding_fingerprint'})
    _retain_candidate_binding_identity(state)

def _assert_prerequisite_owner_enrichment(saved, marker):
    binding = saved.verification_binding
    original = saved.candidate_custody['binding_migration']['original_binding']
    assert original['task_ids'] == ['task-owned']
    assert original['requirement_ids'] == ['REQ-owned']
    assert binding['task_ids'] == ['task-foreign', 'task-owned']
    assert binding['requirement_ids'] == ['REQ-foreign', 'REQ-owned']
    assert binding['required_proof_ids'] == original['required_proof_ids'] == ['owned.contract', 'shared.setup']
    assert binding['regression_dependencies']['owned.contract'] == ['shared.setup']
    assert {owner['task_id'] for owner in binding['proof_owners']['shared.setup']} == {'task-owned', 'task-foreign'}
    assert binding['task_scope'] == original['task_scope'] == {'task_ids': ['task-owned'], 'requirement_ids': []}
    for key in ('tasks', 'plan', 'gates', 'authorization', 'original_handoff_id', 'contract_revision'):
        assert binding[key] == original[key]
    assert marker.read_text() == 'VALUE = 1\n', 'recovered prerequisite must execute on the retained candidate'

def _assert_receipt_policy_mismatch(tmp_path, monkeypatch, policy, change, *, boundary, switch_before):
    from copy import deepcopy
    from auto_agents.config import load_task_plan, requirements_trace_path
    from auto_agents.requirements import requirement_contract_sha256
    import auto_agents.session_candidate as candidate
    root, child = project(tmp_path)
    child.goal_execution_environment = {'mode': 'real', 'confirmed': True, 'source': 'explicit_goal'}
    config, plan = (load_project_config(root), load_task_plan(root))
    marker = ExecutionMarker(tmp_path / 'release-runs')
    (root / 'tests/test_release.py').write_text('from pathlib import Path\ndef test_release():\n    ' + marker.source(repr('release\n'), append=True) + '\n    assert True\n')
    config.gates.release_verification_mode = policy
    config.gates.distributed.mode = 'off'
    config.gates.steps.append(VerificationStep(proof_id='foreign.release', runner='pytest', targets=['tests/test_release.py'], levels=['release'], impact_paths=['unrelated.py']))
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Existing release regression', 'workflow_id': 'foreign-workflow', 'status': 'pending', 'requirement_ids': ['REQ-foreign'], 'verification_refs': ['foreign.release']})
    rows = [{'id': 'REQ-owned', 'text': 'Repair owned value', 'source': 'spec'}, {'id': 'REQ-foreign', 'text': 'Existing release contract', 'source': 'spec'}]
    for task, row in zip(plan['tasks'], rows):
        task['requirement_proofs'] = [{'requirement_id': row['id'], 'requirement_contract_sha256': requirement_contract_sha256(row), 'evidence_refs': task['verification_refs']}]
    trace = requirements_trace_path(root)
    trace.write_text(json.dumps({'requirements': rows}))
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text(json.dumps({'task_id': 'task-owned'}))

    def switch():
        _switch_ambient_binding_plan(root)
        ambient = load_project_config(root)
        ambient.gates.release_verification_mode = 'deferred' if policy == 'blocking' else 'blocking'
        ambient.gates.distributed.mode = 'auto'
        ambient.gates.distributed.extra_environment_denylist = ['LANG']
        save_project_config(root, ambient)
    if switch_before:
        switch()
    identities = []
    original_identity = candidate.verification_identity

    def identity(session, state, **kwargs):
        ambient = session.config.gates
        before = deepcopy(ambient.to_dict())
        try:
            result = original_identity(session, state, **kwargs)
            identities.append(result)
            return result
        finally:
            assert session.config.gates is ambient
            assert ambient.to_dict() == before
    monkeypatch.setattr(candidate, 'verification_identity', identity)
    with monkeypatch.context() as interrupt:
        if boundary == 'delivery':
            deliver = candidate.deliver_candidate

            def stop(session, state, message):
                deliver(session, state, message)
                raise KeyboardInterrupt()
            interrupt.setattr(candidate, 'deliver_candidate', stop)
        elif boundary != 'completed':

            def stop(self, state):
                raise KeyboardInterrupt()
            interrupt.setattr(Session, '_run_session_persistence_action', stop)
        paused, calls, _ = run_session(root, interrupt)
    assert paused.status == ('completed' if boundary == 'completed' else 'paused'), paused.to_dict()
    assert calls == ['fix'] and paused.full_verify is False
    assert marker.exists() == (policy == 'blocking'), 'actual initial runner scope must use retained policy'
    assert paused.verification_binding['task_ids'] == ['task-owned']
    assert paused.verification_binding['required_proof_ids'] == ['owned.contract']
    evidence = [entry for entry in paused.execution_log if entry.get('action') == 'receipt_verification']
    assert len(evidence) == 1 and evidence[0]['verification']['ok']
    original_key = evidence[0]['identity']
    if boundary == 'legacy_pass':
        evidence[0]['identity'] = 'older-receipt-context'
        save_session_state(root, paused)
    if not switch_before:
        switch()
    if change == 'changed':
        rows[-1]['text'] = 'Changed release contract'
    elif change == 'missing':
        rows.pop()
    trace.write_text(json.dumps({'requirements': rows}))
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').chmod(457)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00work')
    protected = {path: (root / path).read_bytes() for path in ('value.py', 'foreign.py', 'foreign-note.txt', '.git/index', '.auto-agents/config.json', '.auto-agents/state/task_plan.json', trace.relative_to(root).as_posix())}
    refs = git(root, 'show-ref')
    custody = deepcopy(paused.candidate_custody)
    retained_fields = {key: deepcopy(getattr(paused, key)) for key in ('current_attempt', 'auto_approve', 'authorization_policy', 'goal', 'goal_execution_environment', 'full_verify', 'verification_binding')}
    if marker.exists():
        marker.unlink()
    verified, delivered = ([], [])
    verify, deliver = (Session._run_verify, candidate.deliver_candidate)
    blocked = policy == 'blocking' and change != 'unchanged'

    def forbidden(*args, **kwargs):
        pytest.fail('Required release authority must reject before execution or completion side effects')

    def observe_verify(self, *args, **kwargs):
        assert boundary == 'legacy_pass', 'matching evidence must be reused'
        result = verify(self, *args, **kwargs)
        verified.append(result)
        return result

    def observe_delivery(session, state, message):
        delivered.append(state.session_id)
        return deliver(session, state, message)
    monkeypatch.setattr(Session, '_run_verify', forbidden if blocked else observe_verify)
    monkeypatch.setattr(candidate, 'deliver_candidate', forbidden if blocked else observe_delivery)
    if blocked:
        monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
        monkeypatch.setattr(Session, '_run_session_persistence_action', forbidden)
    for _ in range(2):
        saved, calls, orch = run_session(root, monkeypatch)
        assert calls == []
        assert saved.status == ('blocked' if blocked else 'completed'), saved.to_dict()
        assert orch.config.gates.to_dict() == load_project_config(root).gates.to_dict()
        assert {key: getattr(saved, key) for key in retained_fields} == retained_fields
        assert saved.candidate_custody['receipt'] == custody['receipt']
        if blocked:
            assert saved.candidate_custody == custody
            diagnostic = saved.execution_log[-1]['diagnostic']
            assert diagnostic['task_id'] == 'task-foreign'
            assert diagnostic['requirement_id'] == 'REQ-foreign'
            assert diagnostic['session_id'] == child.session_id
            assert diagnostic['contract_fingerprint'] and diagnostic['retry_fix'] is False
        else:
            assert identities[-1] == original_key
    assert len(verified) == (1 if boundary == 'legacy_pass' else 0)
    assert len(delivered) == (0 if blocked or boundary == 'completed' else 1)
    if boundary == 'legacy_pass':
        assert verified[0]['ok']
        assert ('foreign.release' in verified[0]['proof_ids']) == (policy == 'blocking')
        records = [entry for entry in saved.execution_log if entry.get('action') == 'receipt_verification']
        assert len(records) == 2 and records[0]['identity'] == 'older-receipt-context'
        assert records[1]['identity'] == original_key and records[1]['verification'] == verified[0]
        if policy == 'blocking' and (not marker.exists()):
            assert verified[0]['certificate_hits'] > 0
    else:
        assert not marker.exists(), 'matching receipts must not replay test bodies'
    assert {path: (root / path).read_bytes() for path in protected} == protected
    assert (root / 'foreign.py').stat().st_mode & 511 == 457
    assert git(root, 'show-ref') == refs

def _assert_receipt_current_authority(tmp_path, monkeypatch, outcome):
    from copy import deepcopy
    from auto_agents.config import load_task_plan, requirements_trace_path
    from auto_agents.requirements import requirement_contract_sha256
    from auto_agents.workflow_runtime import WorkflowCoordinator
    import auto_agents.session_candidate as candidate
    root, child = project(tmp_path)
    config, plan = (load_project_config(root), load_task_plan(root))
    owned = {'id': 'REQ-owned', 'text': 'Repair the owned value', 'source': 'spec'}
    foreign = {'id': 'REQ-foreign', 'text': 'Separate work', 'source': 'spec'}
    trace = requirements_trace_path(root)
    trace.write_text(json.dumps({'requirements': [owned, foreign]}))
    plan['tasks'][0]['requirement_proofs'] = [{'requirement_id': owned['id'], 'requirement_contract_sha256': requirement_contract_sha256(owned), 'evidence_refs': plan['tasks'][0]['verification_refs']}]
    marker = ExecutionMarker(tmp_path / 'release-executions')
    policy_case = outcome in {'full_failure', 'full_pass', 'persisted_full', 'affected_policy'}
    if policy_case:
        (root / 'tests/test_release.py').write_text('from pathlib import Path\ndef test_release():\n    ' + marker.source("Path('value.py').read_text()", append=True) + '\n' + ('    assert "VALUE = 0" in Path("value.py").read_text()\n' if outcome == 'full_failure' else '    assert True\n'))
        config.gates.steps.append(VerificationStep(proof_id='release.regression', runner='pytest', targets=['tests/test_release.py'], levels=['release'], impact_paths=['unrelated.py']))
        config.gates.release_verification_mode = 'deferred'
    _retain_contract(root, child, config, plan)
    child.full_verify = outcome == 'persisted_full'
    save_session_state(root, child)
    with monkeypatch.context() as interrupt:
        if outcome == 'restored_contract':
            record_receipt = candidate.record_receipt

            def missing_contract(session, state):
                record_receipt(session, state)
                trace.write_text(json.dumps({'requirements': [foreign]}))
            interrupt.setattr(candidate, 'record_receipt', missing_contract)
            record = candidate.record_verification

            def stop(session, state, result, **kwargs):
                record(session, state, result, **kwargs)
                assert result['retry_fix'] is False and (not result['ok'])
                raise KeyboardInterrupt()
            interrupt.setattr(candidate, 'record_verification', stop)
        else:

            def stop(self, state):
                raise KeyboardInterrupt()
            interrupt.setattr(Session, '_run_session_persistence_action', stop)
        paused, calls, _ = run_session(root, interrupt)
    assert paused.status == 'paused' and calls == ['fix'], paused.to_dict()
    evidence = [entry for entry in paused.execution_log if entry.get('action') == 'receipt_verification']
    assert len(evidence) == 1
    assert evidence[0]['verification']['ok'] == (outcome != 'restored_contract')
    retained = deepcopy(paused.candidate_custody)
    attempts = paused.current_attempt
    if outcome == 'full_failure':
        paused.hard_ceiling = attempts
        save_session_state(root, paused)
    ambient = _switch_ambient_binding_plan(root)
    if outcome == 'foreign_contract':
        trace.write_text(json.dumps({'requirements': [dict(owned, status='done', notes='non-normative'), dict(foreign, text='Different foreign scope')]}))
    protected_trace = trace.read_bytes()
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').chmod(457)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00bytes')
    protected = {name: (root / name).read_bytes() for name in (*ambient, '.git/index', 'foreign.py', 'foreign-note.txt')}
    refs = git(root, 'show-ref')
    if marker.exists():
        marker.unlink()
    observed = []
    verify = Session._run_verify

    def observe(self, *args, **kwargs):
        observed.append(self._current_state.full_verify)
        return verify(self, *args, **kwargs)
    monkeypatch.setattr(Session, '_run_verify', observe)

    def resume():
        if not policy_case:
            saved, calls, _ = run_session(root, monkeypatch)
            assert calls == []
            return saved
        orch = Orchestrator(root)

        def forbidden(*args, **kwargs):
            pytest.fail('Receipt policy recovery must not replay the writer')
        monkeypatch.setattr(orch, '_call_with_failover', forbidden)
        coordinator = WorkflowCoordinator(orch, full_verify=outcome in {'full_failure', 'full_pass'})
        saved = coordinator.resume_workflow(paused.workflow_id)
        expected = outcome != 'affected_policy'
        assert saved.full_verify is expected
        assert load_session_state(root, saved.session_id).full_verify is expected
        assert saved.auto_approve == paused.auto_approve
        assert orch._force_full_verify is expected
        return saved
    saved = resume()
    if outcome == 'restored_contract':
        assert saved.status == 'blocked' and observed == []
        diagnostic = saved.execution_log[-1]['diagnostic']
        assert diagnostic['requirement_id'] == 'REQ-owned' and diagnostic['retry_fix'] is False
        assert resume().status == 'blocked' and observed == []
        trace.write_text(json.dumps({'requirements': [owned, foreign]}))
        protected_trace = trace.read_bytes()
        saved = resume()
        assert observed == [False], 'restoring owned authority must release the stale inconclusive result'
    elif outcome in {'full_failure', 'full_pass'}:
        assert observed == [True], 'a prior affected pass cannot attest requested release scope'
        expected_runs = ['VALUE = 1', 'VALUE = 0'] if outcome == 'full_failure' else ['VALUE = 1']
        assert marker.read_text().splitlines() == expected_runs
    else:
        assert observed == [], 'unchanged relevant authority must reuse its durable pass'
        assert not marker.exists()
    assert saved.status == ('failed' if outcome == 'full_failure' else 'completed'), saved.to_dict()
    assert saved.current_attempt == attempts
    assert saved.candidate_custody['receipt'] == retained['receipt']
    count = len(observed)
    delivered = saved.candidate_custody.get('delivered_revision')
    repeated = resume()
    assert len(observed) == count
    assert repeated.candidate_custody.get('delivered_revision') == delivered
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert (root / 'foreign.py').stat().st_mode & 511 == 457
    assert trace.read_bytes() == protected_trace
    assert git(root, 'show-ref') == refs

def _retain_candidate_binding_identity(state):
    """Represent a frozen writer receipt created with the older binding format."""
    from auto_agents.session_verification import fingerprint
    custody = state.candidate_custody
    custody['binding_fingerprint'] = state.verification_binding['binding_fingerprint']
    receipt = custody['receipt']
    previous_fingerprint = receipt['fingerprint']
    receipt['binding_fingerprint'] = custody['binding_fingerprint']
    receipt['fingerprint'] = fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'})
    for entry in state.execution_log:
        if entry.get('action') == 'receipt_writer_result' and entry.get('receipt_fingerprint') == previous_fingerprint:
            entry['receipt_fingerprint'] = receipt['fingerprint']

def _assert_migrated_candidate_reused(root, monkeypatch, saved, retained):
    from auto_agents.session_verification import fingerprint
    custody = saved.candidate_custody
    assert {k: v for k, v in custody.items() if k != 'binding_migration'} == retained
    bridge = custody['binding_migration']
    assert bridge['original_binding']['binding_fingerprint'] == retained['binding_fingerprint']
    assert bridge['inventory_fingerprint'] == saved.verification_binding['binding_fingerprint']
    assert bridge['inventory_fingerprint'] != retained['binding_fingerprint']
    assert bridge['receipt'] == retained['receipt']
    assert any((entry['action'] == 'inventory_migration_verify' and entry['result'] == 'pass' for entry in saved.execution_log))
    contexts = []
    executor = Orchestrator._gate_executor_context

    def observe(self, *args, **kwargs):
        contexts.append(kwargs.get('contract_fingerprint'))
        return executor(self, *args, **kwargs)
    monkeypatch.setattr(Orchestrator, '_gate_executor_context', observe)
    repeated, calls, _ = run_session(root, monkeypatch)
    assert repeated.status == 'completed' and calls == []
    assert repeated.candidate_custody == custody
    expected = fingerprint([bridge['inventory_fingerprint'], retained['receipt']['fingerprint']])
    assert repeated.verification_binding == saved.verification_binding
    assert repeated.baseline_git_ref == saved.baseline_git_ref
    assert repeated.baseline_head_ref == saved.baseline_head_ref
    assert not contexts, 'matching durable verification must not execute again'
    assert git(Path(custody['checkout']), 'show', custody['receipt']['source_revision'] + ':value.py') == 'VALUE = 1\n'
