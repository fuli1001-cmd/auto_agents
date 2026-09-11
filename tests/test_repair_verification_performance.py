"""Scheduling regressions from the retained c74/c75 repair workload."""
import json
import os
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest

from auto_agents.models import AgentRequest
from auto_agents.prompting import ProviderRuntime, prepare_request
from auto_agents.prompting.core import fresh_request
from auto_agents.repair_memory import component_key
from auto_agents.repair_schedule import order_verification_commands, verification_plan
from auto_agents.repair_verification import run_component_checks
from auto_agents.self_repair import _VerificationResult
from auto_agents.self_repair_search import SelfRepairExperiment
from test_repair_routing import git
from test_self_repair_performance import _runner
from test_self_repair_feedback import repair_feedback


def test_short_regression_runs_before_matrices_without_losing_request_owners():
    matrix = 'python -m pytest -q tests/test_matrix.py::test_all'
    small = 'python -m pytest -q tests/test_receipts.py::test_materialized'
    first = 'python -m pytest -x tests/test_contract.py::test_failed'
    state = SelfRepairExperiment.create(run_id='run', root_fingerprint='r', category='engine', base_commit='base')
    group = {'group_id': 'component', 'focused_tests': [matrix, small, first], 'quick_checks': [small]}
    state.component_memory[component_key(group)] = {'check_timings': {
        matrix: {'seconds': 562}, small: {'seconds': 15}, first: {'seconds': 40}}}
    plan = verification_plan(state, group)
    assert plan['commands'] == [small, first, matrix]
    assert len(plan['requests']) == 4 and plan['deduplicated_commands'] == 1
    assert all(plan['commands'][r['execution_index']] == r['command'] for r in plan['requests'])
    assert order_verification_commands([matrix, small], state.component_memory[component_key(group)]['check_timings'], first=[matrix]) == [matrix, small]
    assert order_verification_commands([matrix, 'python scripts/prepare.py', small], {}) == [matrix, 'python scripts/prepare.py', small]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_checks.py').write_text('def test_one(): pass\ndef test_two(): pass\ndef test_three(): pass\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'initial')
    obj = _runner(tmp_path)
    obj._candidate_group = {'group_id': 'component'}
    obj._experiment = SimpleNamespace(component_memory={})
    monkeypatch.setattr('auto_agents.repair_verification.os.cpu_count', lambda: 4)
    monkeypatch.setattr(obj, '_full_suite_environment_fingerprint', lambda: ('stable',))
    monkeypatch.setattr(obj, '_full_suite_shard_resources', lambda *a: ((), True))
    return obj


def commands():
    return [f'python -m pytest -q -x --tb=short tests/test_checks.py::test_{name}' for name in ['one', 'two', 'three']]


def passed(command):
    return _VerificationResult(True, command, commands=(command,), returncodes=(0,),
        payload={'command_timings': [{'command': command, 'seconds': 1, 'cache_hit': False}],
                 'executed_tests': [command.split()[-1]]})


def test_parallel_checks_keep_commands_and_private_worktrees(runner, monkeypatch):
    rendezvous = threading.Barrier(2)
    observed = []
    def execute(selected, root):
        assert len(selected) == 1
        observed.append((selected[0], root))
        runtime = root / '.pytest_cache'
        runtime.mkdir()
        assert not (runtime / 'private').exists()
        (runtime / 'private').write_text(selected[0])
        rendezvous.wait(timeout=5)
        return passed(selected[0])
    monkeypatch.setattr(runner, '_run_verification_commands', execute)
    result = run_component_checks(runner, commands()[:2], runner.repo_root)
    assert result.ok and result.payload['parallel_workers'] == 2
    assert result.payload['source_commands'] == commands()[:2]
    assert {c for c, _ in observed} == set(commands()[:2])
    assert len({root for _, root in observed}) == 2
    assert all(root != runner.repo_root and not root.exists() for _, root in observed)
    assert not (runner.repo_root / '.pytest_cache/private').exists()


def test_short_failure_stops_expensive_wave_and_preserves_failed_command(runner, monkeypatch):
    selected = commands()
    runner._experiment.component_memory[component_key(runner._candidate_group)] = {
        'check_timings': {selected[1]: {'seconds': 500}, selected[2]: {'seconds': 400}}}
    calls = []
    def execute(items, root):
        calls.extend(items)
        return _VerificationResult(False, 'receipt fixture failed', commands=tuple(items), returncodes=(1,),
                                   payload={'failure_evidence': [{'command': items[0], 'outcome': 'behavior_failure'}]})
    monkeypatch.setattr(runner, '_run_verification_commands', execute)
    result = run_component_checks(runner, selected, runner.repo_root)
    assert not result.ok and calls == selected[:1]
    assert result.payload['source_commands'] == selected[:1]
    assert result.payload['failure_evidence'][0]['command'] == selected[0]
    assert result.payload['planned_commands'] == 3 and result.payload['completed_commands'] == 1


def test_shared_resource_waiters_do_not_block_independent_commands(runner, monkeypatch):
    independent = threading.Event()
    active, maximum = 0, 0
    lock = threading.Lock()
    def resources(root, name, targets):
        return (('service:shared',), True) if 'three' not in targets[0] else ((), True)
    def execute(items, root):
        nonlocal active, maximum
        if 'three' in items[0]:
            independent.set()
        else:
            with lock:
                active += 1
                maximum = max(maximum, active)
            assert independent.wait(5)
            with lock:
                active -= 1
        return passed(items[0])
    monkeypatch.setattr(runner, '_full_suite_shard_resources', resources)
    monkeypatch.setattr(runner, '_run_verification_commands', execute)
    result = run_component_checks(runner, commands(), runner.repo_root)
    assert result.ok and maximum == 1


@pytest.mark.parametrize('case', ['dirty', 'shell', 'single_cpu'])
def test_uncommitted_or_ordered_work_retains_serial_execution(runner, monkeypatch, case):
    selected = commands()
    if case == 'dirty':
        (runner.repo_root / 'new.py').write_text('uncommitted = True\n')
    elif case == 'shell':
        selected.insert(1, 'python scripts/prepare.py')
    else:
        monkeypatch.setattr('auto_agents.repair_verification.os.cpu_count', lambda: 1)
    calls = []
    monkeypatch.setattr(runner, '_run_verification_commands', lambda items, root: calls.append((items, root)) or passed(items[0]))
    assert run_component_checks(runner, selected, runner.repo_root).ok
    assert calls == [(selected, runner.repo_root)]


def test_changed_dependency_environment_revalidates_all_checks(runner, monkeypatch):
    env = iter([('old',), ('new',)])
    monkeypatch.setattr(runner, '_full_suite_environment_fingerprint', lambda: next(env))
    calls = []
    monkeypatch.setattr(runner, '_run_verification_commands', lambda items, root: calls.append((items, root)) or passed(items[0]))
    assert run_component_checks(runner, commands()[:2], runner.repo_root).ok
    assert calls[-1] == (commands()[:2], runner.repo_root)


def test_command_events_preserve_failed_environment_attempt_before_retry(runner, monkeypatch):
    from auto_agents.models import CommandResult, GateResult
    events = []
    runner._candidate_id = 'current'
    runner._control_phase_callback = lambda kind, payload: events.append((kind, payload))
    monkeypatch.setattr(runner, '_verification_python', lambda: sys.executable)
    results = iter([CommandResult('check', False, 1, stdout='missing prerequisite', duration_seconds=3),
                    CommandResult('check', True, 0, stdout='passed', duration_seconds=1)])
    def execute(*args, **kwargs):
        result = next(results)
        return GateResult(result.ok, [result], result.stdout)
    monkeypatch.setattr('auto_agents.self_repair.run_commands', execute)
    monkeypatch.setattr(runner, '_handle_verification_dependencies', lambda *a, **kw: True)
    with runner._phase_timer('focused_verification'):
        result = runner._run_verification_commands(commands()[:1], runner.repo_root)
    records = [payload for kind, payload in events if kind == 'verification_command_finished']
    assert result.ok and [row['returncode'] for row in records] == [1, 0]
    assert [row['seconds'] for row in records] == [3, 1]
    assert all(row['candidate_id'] == 'current' and row['phase'] == 'focused_verification' for row in records)
    assert records[0]['span_id'] == records[1]['span_id'] == events[-1][1]['span_id']


def test_full_repair_fallback_keeps_complete_feedback_once(repair_feedback):
    runner, artifact = repair_feedback
    prompt = runner._build_prompt()
    initial = prepare_request(AgentRequest('self_repair', 'deep', prompt, runner.repo_root, runner.repo_root / 'answer'),
                              ProviderRuntime('codex', settings_fingerprint='old'))
    from dataclasses import replace
    from auto_agents.prompting.core import digest
    request = replace(initial, resume_session_id='native', resume_prompt_hash=initial.prompt_metadata['compatibility_hash'],
        prompt_is_continuation=True, prompt_continuation=runner._candidate_continuation_prompt(),
        prompt_fallback_continuation='Complete current feedback is in Repair iteration evidence.',
        prompt_fallback_continuation_hash=digest(runner._candidate_continuation_prompt()),
        prompt_metadata={'resume_compatibility_components': initial.prompt_metadata['compatibility_components'],
                         'resume_settings_components': {'args': 'old'}})
    result = prepare_request(request, ProviderRuntime('codex', settings_fingerprint='new', settings_components={'args': 'new'}))
    assert result.prompt_metadata['compatibility_changes'] == ['settings_fingerprint']
    assert result.prompt_metadata['settings_changes'] == ['args']
    assert not result.resume_session_id
    for finding in artifact['review_findings'][:2]:
        assert finding['reason'] in result.prompt
        assert finding['required_test'] in result.prompt
    assert not any(c.source == 'previous progress' and len(c.text) > 100 for c in result.prompt_spec.contexts)
    handed = prepare_request(fresh_request(result, 'provider-switch', 'New tool failure after the canonical prompt'), ProviderRuntime('other'))
    assert 'New tool failure after the canonical prompt' in handed.prompt
    from auto_agents.orchestrator import Orchestrator
    retry = Orchestrator._prompt_handoff(result, 'A new assertion failed during this provider call', 'next-session')
    handed = prepare_request(retry, ProviderRuntime('other'))
    assert 'A new assertion failed during this provider call' in handed.prompt


def test_settings_diagnostics_identify_changes_without_persisting_values(tmp_path):
    from auto_agents.models import ProviderConfig
    from auto_agents.prompting.runtime import resolve_runtime
    native = tmp_path / 'native'
    native.mkdir()
    settings = native / 'config.toml'
    settings.write_text('model = "gpt-6-astra"\n')
    request = AgentRequest('self_repair', 'deep', 'repair', tmp_path, tmp_path / 'answer')
    config = ProviderConfig(kind='codex', profile_map={'deep': ''})
    env = {'CODEX_HOME': str(native), 'OPENAI_API_KEY': 'private-test-value'}
    before = resolve_runtime(config, request, env=env, probe=False)
    settings.write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
    after = resolve_runtime(config, request, env=env, probe=False)
    assert before.settings_fingerprint != after.settings_fingerprint
    assert [key for key in before.settings_components if before.settings_components[key] != after.settings_components[key]] == ['file:' + str(settings)]
    assert 'private-test-value' not in json.dumps(after.settings_components)


def test_report_includes_internal_commands_and_excludes_imported_schedules(tmp_path):
    from scripts.report_repair_performance import report
    root = tmp_path / 'control'
    root.mkdir()
    db = sqlite3.connect(root / 'control.sqlite3')
    db.executescript('CREATE TABLE jobs(id,state,updated,result); CREATE TABLE events(sequence,job,kind,payload,created); '
                     'CREATE TABLE verifications(id,payload);')
    db.execute('INSERT INTO jobs VALUES(?,?,?,?)', ('job', 'blocked', 200, '{}'))
    events = [('subscribed', {}, 100), ('candidate_result', {'candidate_id': 'current', 'status': 'failed'}, 190)]
    for i, (kind, value, created) in enumerate(events):
        db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (i, 'job', kind, json.dumps(value), created))
    db.commit()
    evidence = root / 'jobs/job/working-evidence/.auto-agents/runs/run/self-repair/root'
    evidence.mkdir(parents=True)
    histories = []
    for identity, stamp, seconds in [('a' * 32, 180, 15), ('b' * 32, 90, 500)]:
        path = evidence / 'planning' / identity / 'memory.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'source_commit': 'same', 'phase': 'expanded',
            'timings': [{'command': 'pytest', 'seconds': seconds, 'cache_hit': True}]}))
        os.utime(path, (stamp, stamp))
        histories.append({'id': identity})
    (evidence / 'experiment.json').write_text(json.dumps({'candidates': {'current': {'candidate_commit': 'same'}},
        'component_memory': {'component': {'verification_history': histories}}}))
    legacy = report(root, 'job')
    assert len(legacy['verification_commands']) == 1 and legacy['verification_commands'][0]['seconds'] == 15
    assert legacy['wall_seconds'] == 100 and legacy['cache_hit_commands'] == 1
    db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (2, 'job', 'verification_command_finished', json.dumps({
        'candidate_id': 'current', 'phase': 'focused_verification', 'command': 'pytest', 'seconds': 14,
        'cache_hit': False, 'input_trace_reason': 'external_input'}), 185))
    db.commit()
    result = report(root, 'job')
    assert len(result['verification_commands']) == 1
    assert result['command_origins'] == {'runner_event': 1}
    assert result['input_trace_reasons'] == {'external_input': 1}
    db.close()
