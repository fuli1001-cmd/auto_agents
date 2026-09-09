import json
import shlex
import sys
from pathlib import Path

from auto_agents.process_supervision import run_supervised_shell_command
from auto_agents.repair_feedback import command_evidence, failure_excerpt
from auto_agents.repair_schedule import verification_plan
from auto_agents.self_repair_search import SelfRepairCandidateRecord, SelfRepairExperiment


def test_middle_failure_survives_long_output_and_durable_capture(tmp_path):
    script = "print('passing context\\n' * 400); print('AssertionError: retained middle failure'); print('unrelated tail\\n' * 400); raise SystemExit(1)"
    result = run_supervised_shell_command(shlex.join([sys.executable, '-c', script]),
        cwd=tmp_path, timeout_seconds=10, evidence_dir=tmp_path / 'evidence')
    path = Path(result.process_snapshot['diagnostic_artifacts']['stdout'])
    assert 'AssertionError: retained middle failure' in path.read_text()
    assert 'AssertionError: retained middle failure' in failure_excerpt(path.read_text(), 800)


def test_progress_can_exceed_deadline_but_repeated_output_cannot(tmp_path):
    progress = tmp_path / 'progress.json'
    script = ("import json,time; from pathlib import Path; p=Path(" + repr(str(progress)) + "); "
              "\nfor n in range(6):\n p.write_text(json.dumps({'checkpoints':[str(n)]})); time.sleep(.08)\n")
    result = run_supervised_shell_command(shlex.join([sys.executable, '-c', script]),
        cwd=tmp_path, timeout_seconds=.1, idle_timeout_seconds=.3,
        timeout_policy='progress', progress_path=progress)
    assert result.returncode == 0 and not result.termination_reason
    assert result.duration_seconds > .1
    repeated = "import time\nwhile True:\n print('heartbeat',flush=True); time.sleep(.01)"
    result = run_supervised_shell_command(shlex.join([sys.executable, '-c', repeated]),
        cwd=tmp_path, timeout_seconds=10, idle_timeout_seconds=.1, timeout_policy='progress')
    assert result.termination_reason == 'stalled'
    assert 'stall_diagnostic' in result.process_snapshot


def test_regrouping_and_restart_do_not_credit_the_same_proof(tmp_path):
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='engine',
        base_commit='base', expected_postconditions=['owned behavior'])
    group = {'group_id': 'original', 'contract_obligation_ids': state.contract_obligation_ids,
             'focused_tests': ['python -m pytest -q tests/test_contract.py::test_behavior']}
    state.finding_groups = [group]
    first = SelfRepairCandidateRecord('first', status='candidate_group_completed',
        finding_group_id='original', candidate_commit='first')
    assert state.register_candidate(first) == 'net_progress'
    state.consecutive_non_improvements = 2
    restored = SelfRepairExperiment.from_dict(json.loads(json.dumps(state.to_dict())))
    restored.candidates['first'].fatal = True  # Old proof invalidated, history retained.
    restored.finding_groups = [{**group, 'group_id': 'renamed'}]
    repeated = SelfRepairCandidateRecord('second', status='candidate_group_completed',
        finding_group_id='renamed', candidate_commit='second')
    assert restored.register_candidate(repeated) == 'no_progress'
    assert restored.patience_exhausted


def test_schema_four_history_keeps_credit_before_restart_invalidates_proof():
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='engine', base_commit='base')
    state.finding_groups = [{'group_id': 'done', 'focused_tests': ['python -m pytest -q tests/test_a.py'],
                            'contract_obligation_ids': ['root:one']}]
    state.register_candidate(SelfRepairCandidateRecord('first', status='candidate_group_completed',
        finding_group_id='done', candidate_commit='first'))
    old = state.to_dict()
    old['schema_version'] = 4
    old.pop('progress_credits')
    recovered = SelfRepairExperiment.from_dict(old)
    assert recovered.progress_credits
    recovered.candidates['first'].fatal = True
    recovered.candidates['first'].component_receipts = {}
    assert recovered.register_candidate(SelfRepairCandidateRecord('again', status='candidate_group_completed',
        finding_group_id='done', candidate_commit='again')) == 'no_progress'


def test_active_failure_runs_before_regressions_and_future_checks_are_deferred():
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='engine', base_commit='base')
    active = {'group_id': 'active', 'focused_tests': ['python -m pytest -q tests/test_active.py::test_failure']}
    state.finding_groups = [active, {'group_id': 'future', 'focused_tests': ['python -m pytest -q tests/test_future.py::test_missing']}]
    state.sticky_verification_commands = ['python -m pytest -q tests/test_regression.py tests/test_future.py::test_missing']
    plan = verification_plan(state, active)
    assert plan['commands'] == [active['focused_tests'][0], 'python -m pytest -q tests/test_regression.py']
    assert plan['deferred'][0]['targets'] == ['tests/test_future.py::test_missing']
    assert 'test_future' in state.sticky_verification_commands[0]  # Never delete final obligations.


def test_supervisor_uses_registered_checks_not_changing_output_or_names(tmp_path):
    from auto_agents.models import AgentProgressEvent, AgentRequest, SmartTimeoutConfig
    from auto_agents.supervision import ProgressSupervisor
    evidence = tmp_path / 'context.json'
    request = AgentRequest(stage='self_repair', effort='deep', prompt='repair', cwd=tmp_path,
        output_path=tmp_path / 'output.md', progress_evidence_path=evidence,
        progress_expected_checks=['tests/test_a.py::test_contract'])
    supervisor = ProgressSupervisor(config=SmartTimeoutConfig(), request=request, provider='fixture', process_pid=0, decoder=None)
    initial = supervisor.last_semantic_progress
    request.output_path.write_text('rewritten output')
    supervisor._sample_workspace(initial + 1)
    supervisor.observe_events([AgentProgressEvent(kind='tool_completed', semantic=True, fingerprint='new text')])
    assert supervisor.last_semantic_progress == initial
    evidence.write_text(json.dumps({'context': 'context', 'checkpoints': [
        'start:tests/test_a.py::test_contract_fake', 'tests/test_a.py::test_contract:call:passed']}))
    assert supervisor._trusted_checkpoints() == {'tests/test_a.py::test_contract:call:passed'}


def test_declared_ini_overrides_preserve_environment_and_cli_precedence(tmp_path):
    from auto_agents.pytest_invocation import compile_ini_overrides
    (tmp_path / 'pytest.ini').write_text('[pytest]\naddopts = -o python_files=check_*.py\n')
    command = compile_ini_overrides('python -m pytest -q -o python_files=cli_*.py', tmp_path,
                                    {'PYTEST_ADDOPTS': '-o python_files=env_*.py'})
    parts = shlex.split(command)
    assert [part for part in parts if part.startswith('python_files=')] == [
        'python_files=check_*.py', 'python_files=env_*.py', 'python_files=cli_*.py']


def test_inline_environment_assignment_remains_shell_assignment(tmp_path):
    import subprocess
    from auto_agents.pytest_invocation import compile_ini_overrides
    (tmp_path / 'pytest.ini').write_text('[pytest]\naddopts = -o python_files=excluded_*.py\n')
    (tmp_path / 'check_a.py').write_text('def test_a(): assert True\n')
    original = "PYTEST_ADDOPTS='-o python_files=check_*.py' " + shlex.join([sys.executable, '-m', 'pytest', '-q'])
    command = compile_ini_overrides(original, tmp_path)
    assert command.startswith('PYTEST_ADDOPTS=')
    result = subprocess.run(command, cwd=tmp_path, shell=True, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_failure_from_broad_command_gets_a_minimal_reproducer_first():
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='engine', base_commit='base')
    active = {'group_id': 'active', 'focused_tests': ['python -m pytest -q tests/test_big.py']}
    state.finding_groups = [active]
    state.candidates['failed'] = SelfRepairCandidateRecord('failed', finding_group_id='active',
        failure_evidence=[{'phase': 'candidate', 'command': active['focused_tests'][0],
                           'failures': [{'nodeid': 'tests/test_big.py::test_regression'}]}])
    assert verification_plan(state, active)['commands'] == [
        'python -m pytest -q tests/test_big.py::test_regression', active['focused_tests'][0]]


def test_intermediate_verification_returns_complete_redacted_evidence(tmp_path):
    from types import SimpleNamespace
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision
    runner = AutoAgentsSelfRepairRunner(SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace())),
        target_project_root=tmp_path, error=RuntimeError('failure'), decision=SelfRepairDecision(True))
    runner.repo_root = tmp_path
    runner._verification_python_cache = sys.executable
    script = "print('padding\\n'*500); assert False, 'middle failure API_KEY=example-secret'"
    result = runner._run_verification_commands([shlex.join([sys.executable, '-c', script])], tmp_path)
    assert not result.ok
    evidence = result.to_dict()['payload']['failure_evidence']
    assert evidence and 'middle failure' in evidence[0]['excerpt']
    assert 'example-secret' not in json.dumps(evidence)
    assert Path(evidence[0]['artifacts']['stderr']).is_file()


def test_diagnosis_reads_retained_revision_and_reuses_only_unchanged_inputs(tmp_path):
    import subprocess
    from types import SimpleNamespace
    from auto_agents.repair_actions import prepare_action
    from auto_agents.self_repair_search import SelfRepairExperimentStore
    root = tmp_path / 'engine'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (root / 'value.txt').write_text('base')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (root / 'value.txt').write_text('retained')
    git('commit', '-qam', 'retained')
    retained = git('rev-parse', 'HEAD')
    git('checkout', '-q', '--detach', base)
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='root', category='engine', base_commit=base)
    state.candidates['failure'] = SelfRepairCandidateRecord('failure', candidate_commit=retained,
        finding_group_id='active', failure_evidence=[{'phase': 'candidate', 'evidence_id': 'failure',
            'next_action': 'diagnose_failure', 'command': 'retained check'}])
    store = SelfRepairExperimentStore(tmp_path, 'run', 'root')
    store.save(state)
    calls = []
    def diagnose(request):
        calls.append(request.cwd)
        assert request.cwd != root and request.sandbox_mode == 'read-only'
        assert (request.cwd / 'value.txt').read_text() == 'retained'
        return SimpleNamespace(ok=True, summary=json.dumps({'kind': 'repair_code', 'cause': 'observed',
            'evidence_ids': ['failure'], 'completion': 'retained check'}))
    runner = SimpleNamespace(repo_root=root, _candidate_group={'group_id': 'active'}, _experiment_store=store,
        _effort=lambda: 'deep', _autonomy_config=lambda: SimpleNamespace(),
        target_orchestrator=SimpleNamespace(_call_with_failover=diagnose))
    assert prepare_action(runner, state)['kind'] == 'repair_code'
    assert prepare_action(runner, state)['kind'] == 'repair_code'
    assert len(calls) == 1 and not calls[0].exists()
    assert (root / 'value.txt').read_text() == 'base'


def test_large_failure_packet_keeps_an_explicit_complete_index(tmp_path):
    from auto_agents.repair_feedback import prompt_evidence
    path = tmp_path / 'failure.json'
    failures = [{'nodeid': f'tests/test_a.py::test_{i}', 'phase': 'call',
                 'detail': 'AssertionError: exact cause\n' + 'context\n' * 1000} for i in range(20)]
    complete = {'failures': failures, 'artifacts': {'failure': str(path)}}
    path.write_text(json.dumps(complete))
    projected = prompt_evidence([complete])[0]
    assert projected['failure_count'] == 20 and projected['unread_failure_count'] == 12
    assert len(projected['failures']) == 8
    assert all('exact cause' in failure['detail_excerpt'] for failure in projected['failures'])
    assert len(json.dumps(projected)) < 10000
    assert json.loads(Path(projected['complete_evidence_ref']).read_text())['failures'] == failures
