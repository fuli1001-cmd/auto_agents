"""Retained reference recovery, including the resume wrapper in the incident.

These are synthetic regression contracts. Only the controller's frozen target
replay can attest recovery of the original product session.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from auto_agents.config import (
    load_project_config, load_session_state, load_task_plan, requirements_trace_path,
    save_session_state,
)
from auto_agents.orchestrator import Orchestrator
from auto_agents.repair_control import digest
from auto_agents.repair_runtime_identity import RuntimeIdentityError, observe_engine
from auto_agents.session_verification import SessionOwnershipError
from auto_agents.workflow_runtime import WorkflowCoordinator
from test_engine_child_recovery import parent_workflow
from test_reference_exit_regressions import REFERENCE, reference_project
from test_session_verification_ownership import _retain_contract


ENGINE = Path(__file__).resolve().parents[1]


def retained_recovery(tmp_path, *, wrapped=True, failure='valid'):
    proof = '' if failure == 'reference_only' else 'missing.report.json' if failure == 'missing_proof' else 'owned.contract'
    root, child = reference_project(tmp_path, proof=proof, missing=failure == 'missing_reference')
    if failure in {'bad_lock', 'stale_lock', 'missing_lock', 'invalid_v2'}:
        trace = json.loads(requirements_trace_path(root).read_text())
        trace['requirements'][0]['external_docs_required'] = True
        requirements_trace_path(root).write_text(json.dumps(trace))
        lock = root / '.auto-agents/state/provider_references.lock.json'
        if failure == 'bad_lock':
            lock.write_text('{broken')
        elif failure != 'missing_lock':
            lock.write_text(json.dumps({'references': {'oss': {
                'path': REFERENCE, 'status': 'needs_refresh' if failure == 'stale_lock' else 'verified',
                'contract_version': 2 if failure == 'invalid_v2' else 1,
            }}}))
        _retain_contract(root, child, load_project_config(root), load_task_plan(root))
    store, snapshot, original = parent_workflow(root, child)
    child.status, child.resolution = 'blocked', 'verification_ownership'
    child.attempt_epoch, child.attempts_since_progress, child.hard_ceiling = 7, 1, 25
    child.goal = 'APIMart: complete specified story, smart recommendation, anime, 9:16; real playback or frame extraction.'
    old_failure = {'action': 'execution_preflight_blocked', 'failure_kind': child.resolution,
                   'retry_fix': False, 'result': 'required verification reference has no executable proof: ' + REFERENCE,
                   'diagnostic': {'verification_ref': REFERENCE, 'original_marker': 'retained'}}
    child.execution_log.append(old_failure)
    save_session_state(root, child)
    store.record_result(snapshot, original, status='blocked', result={'status': 'blocked'})
    store.consume_result(snapshot, original, operation_id='original-return')
    failed = original
    if wrapped:
        failed = store.prepare_handoff(snapshot, parent=snapshot.root, target='resume',
            goal=child.goal, reason='original blocked recovery',
            payload={'resume_handoff_id': original.handoff_id})
        failed.child = original.child
        store.save_handoff(failed)
        store.record_result(snapshot, failed, status='blocked', result={'status': 'blocked'})
        store.consume_result(snapshot, failed, operation_id='resume-return')
    payload = {'issue_seed': {
        'target_repository': str(ENGINE), 'failed_handoff_id': failed.handoff_id,
        'original_handoff_id': original.handoff_id, 'evidence_base': str(root),
        'requirement_ids': ['REQ-275'],
        'scope_relation': 'Association only; do not adopt task-459 or task-461.',
    }}
    repair = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix', goal='Repair engine recovery',
                                   reason='retained reference failure', payload=payload)
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = repair.handoff_id
    save_session_state(root, parent)
    return root, child, store, snapshot, original, failed, repair, old_failure


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('failure', ['valid', 'missing_reference', 'missing_proof', 'reference_only',
                                     'bad_lock', 'missing_lock', 'stale_lock', 'invalid_v2'])
def test_retained_reference_replay_requires_the_bound_child(tmp_path, wrapped, failure):
    root, child, store, _, original, _, repair, old_failure = retained_recovery(
        tmp_path, wrapped=wrapped, failure=failure)
    marker = tmp_path / 'probe.json'
    marker.write_text(json.dumps({'route_digest': digest(repair.payload), 'engine_route': repair.payload}))
    before = {key: deepcopy(getattr(child, key)) for key in (
        'goal', 'goal_execution_environment', 'authorization_policy', 'attempt_epoch',
        'attempts_since_progress', 'hard_ceiling', 'current_attempt', 'parent_handoff_id')}
    product = (root / 'foreign.py').read_bytes()
    completed = subprocess.run([sys.executable, '-B', str(ENGINE / 'src/auto_agents/session_replay.py'),
        str(ENGINE), str(root), 'parent', 'collab'], capture_output=True, text=True, timeout=90,
        env={**os.environ, 'AUTO_AGENTS_REPAIR_ROUTE_PROBE': str(marker),
             'AUTO_AGENTS_REPAIR_CONTROL_DISABLED': '1', 'AUTO_AGENTS_STORAGE_MAINTENANCE': 'off'})
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.splitlines()[-1])
    saved = load_session_state(root, child.session_id)
    assert report['route_consumed'] is True, report
    assert report['engine_runtime']['ok'] is True, report
    module = report['engine_runtime']['modules']['auto_agents.session_verification']
    assert Path(module['path']).resolve() == ENGINE / 'src/auto_agents/session_verification.py'
    assert module['functions']['_reference_kind']['matches_source'] is True
    observation = report['recovery_observation']
    assert observation['child_session_id'] == child.session_id
    assert observation['original_handoff_id'] == original.handoff_id
    assert observation['previous_failure'] == old_failure
    assert observation['diagnostic_provider_calls'] == 0
    assert {key: getattr(saved, key) for key in before} == before
    assert old_failure in saved.execution_log
    assert (root / 'foreign.py').read_bytes() == product
    assert report['ok'] is (failure == 'valid'), report
    if failure == 'valid':
        assert observation['boundary_session_id'] == child.session_id
        assert observation['preflight_rechecked'] is True
        assert observation['retained_constraints'] is True
        assert observation['task_scope'] == {'task_ids': ['task-owned'], 'requirement_ids': []}
        reference = observation['reference_decisions'][REFERENCE]
        assert reference['kind'] == 'artifact' and reference['role'] == 'reference'
        assert reference['sha256'] == hashlib.sha256((root / REFERENCE).read_bytes()).hexdigest()
        assert saved.verification_binding['required_proof_ids'] == ['owned.contract']
        assert saved.verification_binding['requirement_ids'] == ['REQ-owned']
        assert observation['current_failure'] == {}  # history is not a new failure
        assert observation['preflight_outcome'] == 'passed'
        assert any(item['reference'] == REFERENCE and item['kind'] == 'artifact'
                   for item in observation['reference_classifications'])
    else:
        assert saved.status == 'blocked'
        returned = store.load_handoff(repair.handoff_id).result
        assert returned['diagnostic'] == saved.execution_log[-1]['diagnostic']
        assert returned['rolled_back_paths'] == []
        assert returned['changed_paths'] == []
        assert returned['retry_fix'] is False
        if failure != 'invalid_v2':
            assert saved.verification_binding == {}
            assert returned['candidate_ownership'] == 'none'


@pytest.mark.parametrize('conflict', ['cycle', 'missing', 'workflow', 'child', 'original', 'repository'])
def test_engine_resume_chain_conflicts_do_not_change_retained_authority(tmp_path, conflict):
    root, child, store, snapshot, original, failed, repair, _ = retained_recovery(tmp_path)
    payload = deepcopy(repair.payload)
    if conflict == 'cycle':
        failed.payload['resume_handoff_id'] = failed.handoff_id
    elif conflict == 'missing':
        failed.payload['resume_handoff_id'] = 'unavailable-handoff'
    elif conflict == 'workflow':
        failed.workflow_id = 'another-workflow'
    elif conflict == 'child':
        from auto_agents.workflow_chain import WorkflowRef
        failed.child = WorkflowRef('fix', 'another-child')
    elif conflict == 'original':
        payload['issue_seed']['original_handoff_id'] = failed.handoff_id
    else:
        payload['issue_seed']['evidence_base'] = str(tmp_path / 'another-project')
    store.save_handoff(failed)
    before = load_session_state(root, child.session_id).to_dict()
    with pytest.raises(SessionOwnershipError):
        WorkflowCoordinator(Orchestrator(root))._engine_child_id(payload, snapshot)
    assert load_session_state(root, child.session_id).to_dict() == before
    assert store.load_handoff(original.handoff_id).child.native_id == child.session_id


def test_runtime_report_identifies_loaded_code_without_installation_evidence():
    report = observe_engine(ENGINE)
    assert report['python'] == sys.executable
    assert report['ok'] is True and report['commit']
    assert report['source_version'] == '0.1.0'
    for module in report['modules'].values():
        assert module['path'] == module['origin']
        assert len(module['source_sha256']) == 64
        assert all(function.get('matches_source') is True for function in module['functions'].values())


@pytest.mark.parametrize('mismatch', ['origin', 'loaded_code'])
def test_same_distribution_version_cannot_certify_another_loaded_engine(monkeypatch, mismatch):
    from auto_agents import session_verification
    monkeypatch.setattr('importlib.metadata.version', lambda _: '0.1.0')
    if mismatch == 'origin':
        monkeypatch.setattr(session_verification, '__file__', '/another/auto_agents/session_verification.py')
    else:
        monkeypatch.setattr(session_verification, '_reference_kind', lambda *args, **kwargs: 'proof')
    with pytest.raises(RuntimeIdentityError) as failure:
        observe_engine(ENGINE)
    assert failure.value.report['distribution_version'] == '0.1.0'
    assert failure.value.report['ok'] is False
    assert any('session_verification' in item for item in failure.value.report['mismatches'])


@pytest.mark.parametrize('stale_scope', ['module', 'catalog_only'])
def test_baseline_catalog_under_candidate_filename_cannot_attest_loaded_runtime(stale_scope):
    # Use the actual incident baseline in a separate interpreter: loading it
    # in this pytest process would replace exception classes and other tests'
    # imported globals. The source on disk and Git metadata remain untouched.
    program = r'''
import hashlib, json, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'src'))
from auto_agents import session_verification as verification
from auto_agents.models import SessionState
from auto_agents.repair_runtime_identity import observe_engine, RuntimeIdentityError
head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                      check=True, capture_output=True, text=True).stdout.strip()
path = Path(verification.__file__)
disk_source = path.read_bytes()
fresh = observe_engine(root, expected_commit=head)
name = 'auto_agents.session_verification'
assert fresh['modules'][name]['functions']['_retained_reference_catalog']['matches_source'] is True
state = SessionState(session_id='catalog-probe', workflow_id='workflow-probe',
                     verification_binding={'contract_revision': 'retained'})
session = SimpleNamespace(project_root=root)
read_bytes = verification._retained_reference_bytes
verification._retained_reference_bytes = lambda *args: b'{broken'
try:
    verification._retained_reference_catalog(session, state)
except verification.SessionOwnershipError as error:
    assert error.diagnostic['verification_ref'] == '.auto-agents/state/requirements_trace.json'
else:
    raise AssertionError('current catalog did not retain the structured failure')
finally:
    verification._retained_reference_bytes = read_bytes
baseline = subprocess.run(['git', '-C', str(root), 'show',
    'c4906fee7becfbda6c06fb96be6df6843d36754a:src/auto_agents/session_verification.py'],
    check=True, capture_output=True).stdout
namespace = verification.__dict__ if sys.argv[2] == 'module' else dict(verification.__dict__)
exec(compile(baseline, str(path), 'exec', dont_inherit=True), namespace)
if sys.argv[2] == 'catalog_only':
    verification._retained_reference_catalog = namespace['_retained_reference_catalog']
try:
    observe_engine(root, expected_commit=head)
except RuntimeIdentityError as error:
    report = error.report
    assert name + ':_retained_reference_catalog' in report['mismatches'], report
    module = report['modules'][name]
    assert module['path'] == module['origin'] == str(path)
    assert module['source_sha256'] == hashlib.sha256(disk_source).hexdigest()
    assert module['functions']['_retained_reference_catalog']['matches_source'] is False
    assert all(module['functions'][function]['matches_source'] is True for function in
               ('_reference_kind', '_session_reference_kind', '_mandatory_refs', '_owned_inventory'))
else:
    raise AssertionError('baseline catalog was accepted under the candidate filename')
namespace['_retained_reference_bytes'] = lambda *args: b'{broken'
try:
    verification._retained_reference_catalog(session, state)
except json.JSONDecodeError:
    pass
else:
    raise AssertionError('counterexample did not load the baseline catalog behavior')
assert path.read_bytes() == disk_source
print(json.dumps({'stale_catalog_rejected': True, 'scope': sys.argv[2]}))
'''
    result = subprocess.run([sys.executable, '-B', '-c', program, str(ENGINE), stale_scope],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {'stale_catalog_rejected': True, 'scope': stale_scope}


@pytest.mark.parametrize('path', ['/', '/work', '/work/child', '/result', '../project', '/tmp/home'])
def test_replay_cannot_mount_evidence_over_its_trusted_runtime(path):
    from auto_agents.repair_v2.docker import replay_project_path
    from auto_agents.repair_v2.types import RepairBlocked
    with pytest.raises(RepairBlocked):
        replay_project_path({'project': path})


def test_replay_preserves_original_project_identity_instead_of_rewriting_contracts():
    from auto_agents.repair_v2.docker import replay_project_path
    payload = {'project': '/home/fuli/projects/sdgp', 'invocation': {'engine_route': {
        'issue_seed': {'evidence_base': '/home/fuli/projects/sdgp', 'requirement_ids': ['REQ-275']}}}}
    before = deepcopy(payload)
    assert str(replay_project_path(payload)) == '/home/fuli/projects/sdgp'
    assert payload == before


@pytest.mark.parametrize('alternative', ['engine_only', 'run', 'health'])
def test_bound_recovery_cannot_fall_back_to_an_unrelated_boundary(tmp_path, monkeypatch, alternative):
    from auto_agents.repair_v2 import boundary_driver
    result = tmp_path / 'result'
    result.mkdir()
    request = {'invocation': {'engine_route': {'issue_seed': {'failed_handoff_id': 'retained-resume'}}}}
    if alternative == 'run':
        request['invocation']['run_id'] = 'another-run'
    elif alternative == 'health':
        request['repair_case'] = {'progress_history': [{'event': 'unrelated'}]}
    (result / 'request.json').write_text(json.dumps(request))
    def private_path(value):
        path = Path(value)
        return result / path.relative_to('/result') if path.is_relative_to('/result') else path
    monkeypatch.setattr(boundary_driver, 'Path', private_path)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    with pytest.raises(RuntimeError, match='retained session entrypoint'):
        boundary_driver.main()
    assert not (result / 'boundary.json').exists()
