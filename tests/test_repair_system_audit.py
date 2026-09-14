"""Cross-stage regressions: retries must preserve ordering, evidence and custody."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.models import CommandResult, GateResult
from auto_agents.repair_memory import read_record, save_record
from auto_agents.repair_planning import prepare_component
from auto_agents.repair_schedule import canonical_commands, quick_verification_plan, verification_plan
from auto_agents.self_repair import _VerificationResult, _FullSuiteShard
from test_repair_planning import setup
from test_repair_completion import completed
from test_self_repair_performance import _runner


def capabilities(pid=100, owner='first-owner'):
    return {'version': 1, 'production_namespace': {'supported': False,
        'nested_gate_metadata': {'supported': True, 'status': 'observed',
            'checks': {'input_trace_owner': owner, 'input_trace_supported': True,
                'standalone_supervisor_checks': {'version': 1, 'sources': {'metadata.py': 'hash'},
                    'launcher_pid': pid, 'checks': {'owner_death': {'supervisor_pid': pid + 1,
                        'tracer_pid': pid + 1, 'pid': pid + 2, 'tracee_pipes_closed': True}}}},
            'input_trace_activation': {'owner': owner, 'supported': True, 'protocol': 1}}}}


def test_restarted_capability_probes_do_not_repeat_an_approved_plan(setup):
    runner, state, _, calls = setup
    with patch('auto_agents.planning_capabilities.planning_capabilities', return_value=capabilities()), \
         patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        first = prepare_component(runner, runner.repo_root)
    runner._experiment = runner._experiment_store.load()
    runner._candidate_group = dict(state.finding_groups[0])
    fresh = json.loads(json.dumps(capabilities(900), sort_keys=True))
    with patch('auto_agents.planning_capabilities.planning_capabilities', return_value=fresh), \
         patch('auto_agents.repair_planning._probe', side_effect=AssertionError('repeated planning probe')):
        second = prepare_component(runner, runner.repo_root)
    assert second['request_id'] == first['request_id']
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['source', 'owner_source', 'owner_relationship', 'pid_relationship', 'exitkill', 'protocol'])
def test_capability_binding_retains_behavior_and_identity_relationships(change):
    from auto_agents.repair_capability_checks import capability_fingerprint
    old, new = capabilities(), capabilities(900)
    metadata = new['production_namespace']['nested_gate_metadata']
    report = metadata['checks']['standalone_supervisor_checks']
    if change == 'source':
        report['sources']['metadata.py'] = 'changed'
    elif change == 'owner_source':
        metadata['checks']['input_trace_owner'] = metadata['input_trace_activation']['owner'] = 'changed-source'
    elif change == 'owner_relationship':
        metadata['input_trace_activation']['owner'] = 'unrelated-owner'
    elif change == 'pid_relationship':
        report['checks']['owner_death']['tracer_pid'] = 905
    elif change == 'exitkill':
        report['checks']['owner_death']['tracee_pipes_closed'] = False
    else:
        metadata['input_trace_activation']['protocol'] = 2
    assert capability_fingerprint(old) != capability_fingerprint(new)


def test_shell_preparation_and_repeated_checks_keep_their_execution_positions(setup):
    runner, state, _, _ = setup
    command = 'python -m pytest -q tests/test_contract.py::test_contract'
    sequence = ['python -m unittest prepare', command, 'python -m unittest prepare', command]
    selected, requests = canonical_commands(sequence)
    assert selected == sequence
    assert [row['execution_index'] for row in requests] == [0, 1, 2, 3]
    group = {**state.finding_groups[0], 'focused_tests': sequence}
    expanded = verification_plan(state, group)
    quick = quick_verification_plan(state, group)
    assert expanded['commands'] == quick['commands'] == sequence
    assert [row['execution_index'] for row in expanded['requests']] == [0, 1, 2, 3]
    # Run the actual sequence: skipping the second invocation would leave one.
    (runner.repo_root / 'prepare.py').write_text(
        'from pathlib import Path\np=Path("phase"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n')
    (runner.repo_root / 'tests/test_contract.py').write_text(
        'from pathlib import Path\ndef test_contract():\n'
        '    phase=Path("phase").read_text()\n    Path("observations").open("a").write(phase)\n')
    runner._candidate_group = group
    result = runner._run_active_group_verification(runner.repo_root)
    assert result.ok, result.summary
    assert (runner.repo_root / 'observations').read_text() == '12'


@pytest.mark.parametrize('failure', ['ok_flag', 'termination', 'infrastructure', 'cleanup'])
def test_failed_process_with_zero_exit_cannot_pass_or_dispatch_another_check(setup, failure):
    runner, _, _, _ = setup
    process = CommandResult('diagnostic', failure != 'ok_flag', 0)
    if failure == 'termination':
        process.termination_reason = 'cancelled'
    elif failure == 'infrastructure':
        process.infrastructure_error = True
    elif failure == 'cleanup':
        process.cleanup_incomplete = True
    with patch('auto_agents.self_repair.run_commands', return_value=GateResult(False, [process])) as execute:
        result = runner._run_verification_commands(['true', 'echo must-not-run'], runner.repo_root)
    assert not result.ok and execute.call_count == 1
    assert result.payload['failure_evidence']
    if failure == 'cleanup':
        assert result.payload['cleanup_incomplete'] is True


@pytest.mark.parametrize('supplemental', [False, True])
def test_integration_retains_command_evidence_and_cleanup_state(tmp_path, supplemental):
    runner = _runner(tmp_path)
    runner.diagnosis = SimpleNamespace(final=SimpleNamespace(
        verification_commands=['python -m pytest -q tests/test_extra.py'] if supplemental else []))
    required = _VerificationResult(True, 'required', commands=('required',), returncodes=(0,), duration_seconds=3,
        payload={'source_commands': ['required'], 'executed_tests': ['test_required'], 'proof_refs': ['proof'],
                 'command_timings': [{'seconds': 3}], 'certificate_hits': 1})
    extra = _VerificationResult(False, 'extra', commands=('extra',), returncodes=(125,), duration_seconds=2,
        payload={'source_commands': ['extra'], 'cleanup_incomplete': True, 'failure_evidence': [{'command': 'extra'}]})
    with patch.object(runner, '_run_verification_commands', side_effect=[required, extra]):
        result = runner._run_verification(tmp_path)
    assert result.payload['executed_tests'] == ['test_required']
    assert result.payload['proof_refs'] == ['proof']
    assert result.duration_seconds == (5 if supplemental else 3)
    if supplemental:
        assert not result.ok and result.payload['cleanup_incomplete']
        assert {'command': 'extra'} in result.payload['failure_evidence']


def test_corrupt_memory_record_is_an_invalid_receipt_not_an_engine_exception(setup):
    runner, _, _, _ = setup
    reference = save_record(runner, 'test', {'value': 'retained'})
    path = runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json'
    path.write_text('[]')
    assert read_record(runner, reference) is None


def test_completion_cannot_seal_a_dropped_repeated_acceptance_command(completed):
    from auto_agents.repair_completion import seal
    from auto_agents.repair_memory import remember_check_timings
    runner, _, _, commands, _ = completed
    planned = [*commands, commands[0]]
    review = _VerificationResult(True, 'approved', payload={'decision': 'APPROVE', 'findings': [], 'resolved_finding_ids': []})
    partial = _VerificationResult(True, 'only first occurrences ran', returncodes=(0, 0),
        payload={'source_commands': commands})
    remember_check_timings(runner, runner.repo_root, runner._candidate_group, {'commands': planned}, partial, phase='expanded')
    assert seal(runner, runner.repo_root, review, partial) is None


def test_full_suite_cannot_pass_an_incomplete_shard_inventory(tmp_path):
    runner = _runner(tmp_path)
    assert not runner._aggregate_full_suite_shards([], recoverable=False, total_shards=2).ok


def test_full_suite_preserves_fatal_cleanup_evidence(tmp_path):
    runner = _runner(tmp_path)
    result = _VerificationResult(False, 'cleanup failed', commands=('test',), returncodes=(125,), duration_seconds=4,
        payload={'source_commands': ['test'], 'cleanup_incomplete': True, 'failure_evidence': [{'command': 'test'}]})
    combined = runner._aggregate_full_suite_shards([('shard', result, False)], recoverable=False, total_shards=1)
    assert not combined.ok and combined.payload['cleanup_incomplete']
    assert combined.payload['failure_evidence'] == result.payload['failure_evidence']
    assert combined.duration_seconds == 4


def test_shard_checkout_survives_incomplete_process_cleanup(setup, monkeypatch):
    import shutil
    from auto_agents.git_ops import remove_worktree
    runner, _, _, _ = setup
    roots = []
    def execute(root, shard):
        roots.append(root)
        return _VerificationResult(False, 'cleanup failed', payload={'cleanup_incomplete': True})
    monkeypatch.setattr(runner, '_execute_full_suite_shard_command', execute)
    shard = _FullSuiteShard('one', 'tests/test_contract.py', ('tests/test_contract.py',), isolated=True)
    try:
        result = runner._execute_full_suite_shard(runner.repo_root, shard)
        assert not result.ok and roots[0].exists()
    finally:
        if roots and roots[0].exists():
            remove_worktree(runner.repo_root, roots[0], force=True)
            shutil.rmtree(roots[0].parent)


@pytest.mark.parametrize('cached_state', ['fresh', 'cleanup', 'malformed', 'invalid_ok'])
def test_full_suite_reexecutes_fresh_or_invalid_checkpoint(setup, monkeypatch, cached_state):
    runner, _, _, _ = setup
    command = 'python -m pytest -q tests/test_contract.py'
    shard = _FullSuiteShard('one', 'tests/test_contract.py', ('tests/test_contract.py',), command=command)
    cached = _VerificationResult(True, 'previous', commands=(command,), returncodes=(0,)).to_dict()
    if cached_state == 'fresh':
        runner._verification_fresh = True
    elif cached_state == 'cleanup':
        cached['payload']['cleanup_incomplete'] = True
    elif cached_state == 'malformed':
        cached['returncodes'] = ['invalid']
    else:
        cached['ok'] = 'false'
    checkpoint = runner.repo_root / 'checkpoint.json'
    checkpoint.write_text(json.dumps({'schema_version': 1, 'suite_key': 'same', 'completed': {'one': cached}}))
    monkeypatch.setattr(runner, '_collect_full_suite_shards', lambda root: [shard])
    monkeypatch.setattr(runner, '_full_suite_checkpoint_key', lambda *args: 'same')
    monkeypatch.setattr(runner, '_full_suite_checkpoint_path', lambda *args: checkpoint)
    monkeypatch.setattr(runner, '_full_suite_proof_cache_lookup', lambda *args: None)
    monkeypatch.setattr(runner, '_full_suite_proof_cache_store', lambda *args: None)
    monkeypatch.setattr(runner, '_record_full_suite_timing', lambda *args: None)
    monkeypatch.setattr(runner, '_report_full_suite_progress', lambda *args, **kwargs: None)
    with patch.object(runner, '_execute_full_suite_shard', return_value=_VerificationResult(True, 'fresh pass')) as execute:
        result = runner._run_full_suite_shards(runner.repo_root)
    assert result.ok and execute.call_count == 1


def test_full_suite_threads_inherit_cancellation_and_stop_dispatch(setup, monkeypatch):
    import threading
    from auto_agents.repair_concurrent_validation import verification_cancel, cancellation_result
    from auto_agents.self_repair import _FullSuiteSlots
    runner, _, _, _ = setup
    runner._full_suite_slots = _FullSuiteSlots(1)
    shards = [_FullSuiteShard(str(i), 'tests/test_contract.py', ('tests/test_contract.py',)) for i in range(2)]
    monkeypatch.setattr(runner, '_collect_full_suite_shards', lambda root: shards)
    monkeypatch.setattr(runner, '_full_suite_checkpoint_key', lambda *args: 'same')
    monkeypatch.setattr(runner, '_full_suite_checkpoint_path', lambda *args: None)
    monkeypatch.setattr(runner, '_full_suite_proof_cache_lookup', lambda *args: None)
    monkeypatch.setattr(runner, '_report_full_suite_progress', lambda *args, **kwargs: None)
    event = threading.Event()
    dispatched = []
    def execute(root, shard):
        assert verification_cancel.get() is event
        dispatched.append(shard.shard_id)
        event.set()
        return cancellation_result('interrupted')
    monkeypatch.setattr(runner, '_execute_full_suite_shard', execute)
    token = verification_cancel.set(event)
    try:
        result = runner._run_full_suite_shards(runner.repo_root)
    finally:
        verification_cancel.reset(token)
    assert not result.ok and dispatched == ['0']
    assert result.payload['cancelled']


@pytest.mark.parametrize('always_changes', [False, True])
def test_full_suite_environment_changes_revalidate_without_mixing_proofs(setup, monkeypatch, always_changes):
    from auto_agents.self_repair import _FullSuiteSlots
    runner, _, _, _ = setup
    runner._full_suite_slots = _FullSuiteSlots(1)
    shards = [_FullSuiteShard(str(i), 'tests/test_contract.py', ('tests/test_contract.py',)) for i in range(2)]
    version, executions, proofs = [0], [], []
    monkeypatch.setattr(runner, '_full_suite_environment_fingerprint', lambda: tuple(version))
    monkeypatch.setattr(runner, '_collect_full_suite_shards', lambda root: shards)
    monkeypatch.setattr(runner, '_full_suite_checkpoint_key', lambda *args: str(version[0]))
    monkeypatch.setattr(runner, '_full_suite_checkpoint_path', lambda *args: None)
    monkeypatch.setattr(runner, '_full_suite_proof_cache_lookup', lambda *args: None)
    monkeypatch.setattr(runner, '_report_full_suite_progress', lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, '_record_full_suite_timing', lambda *args: None)
    monkeypatch.setattr(runner, '_full_suite_proof_cache_store', lambda root, shard, result: proofs.append(result.summary))
    def execute(root, shard):
        executions.append((version[0], shard.shard_id))
        if always_changes or len(executions) == 1:
            version[0] += 1
        return _VerificationResult(True, str(executions[-1]))
    monkeypatch.setattr(runner, '_execute_full_suite_shard', execute)
    result = runner._run_full_suite_shards(runner.repo_root)
    assert len(executions) == 4  # One bounded retry of both shards, with no model calls.
    assert result.ok is (not always_changes)
    if always_changes:
        assert result.recoverable and result.payload['outcome'] == 'invalid' and not proofs
    else:
        assert proofs == [str(item) for item in executions[2:]]
