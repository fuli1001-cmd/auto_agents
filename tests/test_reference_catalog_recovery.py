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
from workflow_support import parent_workflow
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
