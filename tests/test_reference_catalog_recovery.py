"""Reference catalogs remain sealed inputs, never substitutes for test proofs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from auto_agents.config import load_project_config, load_task_plan, load_session_state, save_session_state
from auto_agents.models import GateConfig, VerificationStep
from auto_agents.session_candidate import execution_checkout
from auto_agents.session_verification import (
    SessionOwnershipError, _session_reference_kind, validate_binding,
)
from test_engine_child_recovery import parent_workflow
from test_multilayer_engine_recovery import assert_recovery_budget, replay
from test_reference_exit_regressions import REFERENCE, reference_project
from test_session_verification_ownership import _binding_fixture, _retain_contract


CATALOGS = ['provider_references.lock.json', 'requirements_trace.json']


def catalog_project(tmp_path, catalog, *, failure='valid'):
    proof = '' if failure == 'no_proof' else 'missing.report.json' if failure == 'missing_proof' else 'owned.contract'
    root, child = reference_project(tmp_path, proof=proof)
    ref = '.auto-agents/state/' + catalog
    path = root / ref
    if catalog == 'provider_references.lock.json':
        path.write_text(json.dumps({'version': 1, 'references': {}}))
    if failure == 'missing':
        path.unlink()
    elif failure == 'malformed':
        path.write_text('{broken')
    plan, config = load_task_plan(root), load_project_config(root)
    plan['tasks'][0]['requirement_proofs'] = [
        {'requirement_id': 'REQ-owned', 'evidence_refs': [ref]}]
    _retain_contract(root, child, config, plan)
    return root, child, ref


@pytest.mark.parametrize('catalog', CATALOGS)
def test_catalog_reference_roles_preserve_retained_bytes(tmp_path, catalog):
    root, child, ref = catalog_project(tmp_path, catalog)
    source = (root / ref).read_bytes()
    # An unrelated shared edit cannot redefine the retained verification input.
    (root / ref).write_bytes(b'foreign edit\n')
    session = _binding_fixture(root, child)
    record = child.verification_binding['required_references'][ref]
    assert record['kind'] == 'artifact' and record['role'] == 'reference'
    assert record['sha256'] == hashlib.sha256(source).hexdigest()
    assert child.verification_binding['required_proof_ids'] == ['owned.contract']
    assert set(child.verification_binding['required_references']) == {REFERENCE, ref, 'owned.contract'}
    assert (root / ref).read_bytes() == b'foreign edit\n'
    # Explicit executable proof identities keep precedence over catalog roles.
    gates = GateConfig(steps=[VerificationStep(proof_id=ref, runner='pytest', targets=['tests/test_owned.py'])])
    assert _session_reference_kind(session, child, gates, ref) == 'proof'
    assert _session_reference_kind(session, child, GateConfig(), '.auto-agents/state/unknown.report.json') == 'proof'


@pytest.mark.parametrize('catalog', CATALOGS)
@pytest.mark.parametrize('mutation', ['changed', 'missing'])
def test_catalog_reference_integrity_is_required_in_private_checkout(tmp_path, catalog, mutation):
    root, child, ref = catalog_project(tmp_path, catalog)
    source = (root / ref).read_bytes()
    session = _binding_fixture(root, child)
    with execution_checkout(session, child):
        validate_binding(session, child)
        path = session.project_root / ref
        if mutation == 'changed':
            path.write_bytes(b'{}')
        else:
            path.unlink()
        with pytest.raises(SessionOwnershipError) as rejected:
            validate_binding(session, child)
        assert rejected.value.diagnostic['verification_ref'] == ref
        assert rejected.value.diagnostic['retry_fix'] is False
    assert (root / ref).read_bytes() == source


@pytest.mark.parametrize('catalog', CATALOGS)
@pytest.mark.parametrize('failure', ['valid', 'missing', 'malformed', 'no_proof', 'missing_proof'])
def test_public_engine_recheck_keeps_catalog_and_executable_obligations(tmp_path, catalog, failure):
    root, child, ref = catalog_project(tmp_path, catalog, failure=failure)
    store, _, handoff = parent_workflow(root, child, engine=True)
    child.status, child.resolution = 'blocked', 'verification_ownership'
    old = {'action': 'execution_preflight_blocked', 'failure_kind': child.resolution,
           'result': 'required verification reference has no executable proof: ' + ref,
           'retry_fix': False, 'diagnostic': {'verification_ref': ref},
           'timestamp': '2026-09-16T12:31:42.785042+00:00'}
    child.execution_log.append(old)
    save_session_state(root, child)
    before = deepcopy(child.to_dict())
    report = replay(root, handoff.payload, tmp_path)
    assert report['ok'] is (failure == 'valid'), report
    assert report['route_consumed'] and report['engine_runtime']['ok']
    observed = report['recovery_observation']
    saved = load_session_state(root, child.session_id)
    reservations = 1 if failure == 'valid' else 0
    assert_recovery_budget(observed, reservations)
    assert observed['previous_failure'] == old
    assert saved.execution_log[:len(before['execution_log'])] == before['execution_log']
    for key in ('goal', 'goal_execution_environment', 'authorization_policy', 'parent_handoff_id',
                'attempt_epoch', 'max_attempts', 'hard_ceiling'):
        assert getattr(saved, key) == before[key]
    for key in ('current_attempt', 'attempts_since_progress'):
        assert getattr(saved, key) == before[key] + reservations
    if failure == 'valid':
        assert observed['preflight_rechecked'] and observed['boundary_kind'] == 'implementation'
        assert observed['boundary_session_id'] == child.session_id
        assert observed['current_failure'] == {}
        assert observed['reference_decisions'][ref]['role'] == 'reference'
        assert saved.verification_binding['required_proof_ids'] == ['owned.contract']
        assert saved.verification_binding['task_scope'] == {'task_ids': ['task-owned'], 'requirement_ids': []}
    else:
        assert saved.status == 'blocked' and saved.verification_binding == {}
        assert observed['current_failure'] and observed['current_failure'] != old
        result = store.load_handoff(handoff.handoff_id).result
        assert result['candidate_ownership'] == 'none'
        assert result['changed_paths'] == result['commit_shas'] == result['rolled_back_paths'] == []
        assert result['retry_fix'] is False
        if failure in {'missing', 'malformed'}:
            assert observed['current_failure']['diagnostic']['verification_ref'] == ref
        elif failure == 'missing_proof':
            assert observed['current_failure']['diagnostic']['verification_ref'] == 'missing.report.json'
        else:
            assert 'no executable verification evidence' in observed['current_failure']['result']
