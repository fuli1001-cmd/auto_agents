from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from auto_agents.gate_result_cache import GateResultCache
from auto_agents.models import CommandResult
from auto_agents.verification_baseline import BaselineCertificates, failure_signatures, node_replay_command
from auto_agents.gates import GateCommandMetadata
from auto_agents.session_candidate import require_release_verification
from auto_agents.session_verification import SessionOwnershipError


@pytest.fixture
def baseline(tmp_path):
    source = tmp_path / 'service.py'; source.write_text('fixed baseline')
    cache = GateResultCache(tmp_path, cache_path=tmp_path / 'proofs.sqlite3', environment_fingerprint='env', context_fingerprint='contract')
    executor = SimpleNamespace(project_root=tmp_path, result_cache=cache, snapshot=SimpleNamespace(tree_sha='base-tree'))
    result = CommandResult('python -m pytest tests/test_service.py', False, 1,
        stdout='FAILED tests/test_service.py::test_used - AssertionError',
        input_trace_complete=True,
        observed_inputs={'service.py': 'file:' + hashlib.sha256(source.read_bytes()).hexdigest()},
        test_results={'tests/test_service.py::test_used': {'phases': {'setup':'passed','call':'failed','teardown':'passed'},
                                                        'detail':'E AssertionError: old failure'}})
    return executor, result, GateCommandMetadata(node_replay_safe=True)


def test_baseline_failure_reuse_never_becomes_candidate_success(baseline):
    executor, result, metadata = baseline
    cache = BaselineCertificates(executor)
    cache.put_result(result.command, metadata, result)
    reused = cache.get_result(result.command, metadata)
    assert reused is not None and reused.cached and not reused.ok
    assert reused.backend == 'baseline-certificate'
    assert failure_signatures(reused) == failure_signatures(result)
    assert executor.result_cache.lookup(result.command, source_fingerprint='base-tree', cache_scope='source',
        result_cache_scope='candidate', metadata_signature='contract') is None
    (executor.project_root / 'service.py').write_text('changed external observation')
    assert cache.get_result(result.command, metadata) is None


def test_baseline_identity_binds_environment_source_and_rules(baseline):
    executor, result, metadata = baseline
    cache = BaselineCertificates(executor); cache.put_result(result.command, metadata, result)
    executor.snapshot.tree_sha = 'another-baseline'
    assert BaselineCertificates(executor).get_result(result.command, metadata) is None
    executor.snapshot.tree_sha = 'base-tree'; executor.result_cache.environment_fingerprint = 'different-env'
    assert BaselineCertificates(executor).get_result(result.command, metadata) is None
    executor.result_cache.environment_fingerprint = 'env'
    assert cache.get_result(result.command, replace(metadata, node_replay_safe=False)) is None


@pytest.mark.parametrize('changes', [dict(input_trace_complete=False), dict(network_observed=True),
    dict(infrastructure_error=True), dict(termination_reason='timeout'), dict(mutation_paths=['service.py']),
    dict(cleanup_incomplete=True)])
def test_incomplete_or_unstable_baseline_cannot_be_reused(baseline, changes):
    executor, result, metadata = baseline; cache = BaselineCertificates(executor)
    cache.put_result(result.command, metadata, replace(result, **changes))
    assert cache.get_result(result.command, metadata) is None


def test_failed_node_replay_keeps_filters_and_requires_independence(baseline):
    _, result, _ = baseline
    command = "python -m pytest -q -m 'not live' tests/test_service.py tests/test_other.py"
    replay = node_replay_command(command, result, True)
    assert 'tests/test_service.py::test_used' in replay
    assert 'tests/test_other.py' not in replay
    assert "-m 'not live'" in replay
    assert node_replay_command(command, result, False) == command
    assert node_replay_command('source env && ' + command, result, True) == 'source env && ' + command
    result.test_results['tests/test_service.py::test_used']['phases']['setup'] = 'failed'
    assert node_replay_command(command, result, True) == command


def test_same_node_different_failure_is_not_existing_baseline(baseline):
    _, result, _ = baseline
    other = replace(result, test_results={'tests/test_service.py::test_used': {
        'phases':{'setup':'passed','call':'failed','teardown':'passed'}, 'detail':'E ValueError: new regression'}})
    assert failure_signatures(result) != failure_signatures(other)


def test_cache_explains_environment_and_metadata_changes(tmp_path):
    cache = GateResultCache(tmp_path, cache_path=tmp_path/'cache.sqlite3', environment_fingerprint='first', context_fingerprint='goal')
    arguments = dict(source_fingerprint='tree', cache_scope='run_context', result_cache_scope='candidate', metadata_signature='same')
    cache.record('check', CommandResult('check', True, 0), **arguments)
    cache.environment_fingerprint = 'second'
    assert cache.lookup('check', **arguments) is None
    assert cache.miss_components['check'] == ['environment']
    cache.environment_fingerprint = 'first'
    assert cache.lookup('check', **{**arguments, 'metadata_signature':'changed'}) is None
    assert cache.miss_components['check'] == ['metadata']
    cache.environment_fingerprint = 'unavailable:private'
    assert cache.lookup_with_reason('check', **arguments)[1] == 'environment_identity_unavailable'


def test_v5_delivery_requires_current_complete_release_receipt(monkeypatch):
    monkeypatch.setattr('auto_agents.session_candidate.verification_identity', lambda *a, **kw:'final-id')
    state = SimpleNamespace(session_id='s', workflow_id='w', parent_handoff_id='',
        verification_binding={'binding_fingerprint':'binding','gates':{'verification_policy_version':5,
            'steps':[{'proof_id':'affected.one','levels':['affected']},{'proof_id':'release.two','levels':['release']}]}},
        candidate_custody={'receipt':{'fingerprint':'candidate'}}, execution_log=[])
    row = {'action':'receipt_verification','identity':'final-id','receipt_fingerprint':'candidate',
           'binding_fingerprint':'binding','verification':{'ok':True,'execution_identity':'final-id',
           'scope':'progress','attestation_level':'affected','proof_ids':['affected.one']}}
    state.execution_log = [row]
    with pytest.raises(SessionOwnershipError): require_release_verification(None, state)
    row['verification'].update(scope='final', attestation_level='release')
    with pytest.raises(SessionOwnershipError): require_release_verification(None, state)
    row['verification']['proof_ids'].append('release.two')
    require_release_verification(None, state)
    row['receipt_fingerprint'] = 'old-candidate'
    with pytest.raises(SessionOwnershipError): require_release_verification(None, state)
