from dataclasses import replace
from types import SimpleNamespace
import pytest
from auto_agents.gate_execution import _metadata_signature
from auto_agents.gate_result_cache import GateResultCache
from auto_agents.gates import resolve_gate_plan_from_verification_steps
from auto_agents.models import CommandResult, VerificationStep
from auto_agents.verification_batch_cache import constituents, run_remaining
from auto_agents.verification_migration import migrate_config
from auto_agents.verification_pytest import Recorder

@pytest.mark.parametrize('body_receipt', [True, False])
def test_partial_batch_executes_only_uncertified_nodes(tmp_path, body_receipt):
    steps = [VerificationStep(proof_id='proof.' + name, runner='pytest', coalesce_safe=True, levels=['affected'], targets=['tests/test_a.py::test_' + name], result_cache_scope='candidate') for name in ('a', 'b')]
    original = resolve_gate_plan_from_verification_steps(steps, tmp_path)
    plan = resolve_gate_plan_from_verification_steps(steps, tmp_path, coalesce=True)
    merged = plan.commands[0]
    cache = GateResultCache(tmp_path, cache_path=tmp_path / 'cache.sqlite3', environment_fingerprint='env', context_fingerprint='goal')
    first = original.commands[0]
    cache.record(first, CommandResult(first, True, 0, executed_tests=['tests/test_a.py::test_a'] if body_receipt else []), source_fingerprint='tree', cache_scope='run_context', result_cache_scope='candidate', metadata_signature=_metadata_signature(original.metadata[first], {}))
    executor = SimpleNamespace(metadata=plan.metadata, result_cache=cache, snapshot=SimpleNamespace(tree_sha='tree'), use_result_cache=True, proof_audit_sample_rate=0, input_reuse_mode='off', dependency_links={}, _cache_miss_reasons={})
    cached, missing = constituents(executor, merged)
    if not body_receipt:
        assert not cached and set(missing) == set(original.commands)
        return
    assert len(cached) == len(missing) == 1
    executions = []

    def execute(actual):
        executions.append(actual)
        assert '::test_a' not in actual and '::test_b' in actual
        assert executor.metadata[actual].proof_ids == ['proof.b']
        return CommandResult(actual, True, 0, executed_tests=['tests/test_a.py::test_b'])
    result = run_remaining(executor, merged, cached, missing, execute)
    assert len(executions) == 1
    assert result.command == merged and result.ok and (not result.cached)
    assert set(result.executed_tests) == {'tests/test_a.py::test_a', 'tests/test_a.py::test_b'}
    assert result.process_snapshot['reused_tests'] == ['tests/test_a.py::test_a']
    assert set(plan.metadata[merged].proof_ids) == {'proof.a', 'proof.b'}

def test_subtest_failure_cannot_be_overwritten_by_passed_parent():
    recorder = Recorder()
    for phase, outcome in [('setup', 'passed'), ('call', 'failed'), ('call', 'passed'), ('teardown', 'passed')]:
        recorder.pytest_runtest_logreport(SimpleNamespace(nodeid='test_matrix', when=phase, outcome=outcome, failed=outcome == 'failed', duration=0, longreprtext='E AssertionError: failed subcase'))
    recorder.collected = ['test_matrix']
    assert recorder.result()['passed'] == []
    assert recorder.result()['nodes']['test_matrix']['phases']['call'] == 'failed'

def test_migration_preserves_exact_checks_and_final_proof_union():
    config = {'gates': {'verification_policy_version': 4, 'steps': [{'proof_id': 'owned.check', 'kind': 'test', 'runner': 'pytest', 'targets': ['tests/test_a.py::test_a'], 'levels': ['affected'], 'args': ['-m', 'not live'], 'depends_on_proofs': []}, {'proof_id': 'release.check', 'kind': 'test', 'runner': 'pytest', 'targets': ['tests/test_b.py'], 'levels': ['release'], 'depends_on_proofs': ['owned.check']}]}}
    migrated = migrate_config(config, {'release.check': {'depends_on_proofs': []}})
    assert config['gates']['verification_policy_version'] == 4
    assert migrated['gates']['verification_policy_version'] == 5
    for before, after in zip(config['gates']['steps'], migrated['gates']['steps']):
        assert before['targets'] == after['targets'] and before.get('args') == after.get('args')
    assert migrated['gates']['parallel_workers'] == 2
