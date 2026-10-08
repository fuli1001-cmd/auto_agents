"""Reference roles and pre-implementation returns through retained handoffs."""
import hashlib
import json
import pytest
from auto_agents.models import GateConfig, VerificationStep
from auto_agents.session_verification import SessionOwnershipError, _reference_kind
REFERENCE = '.auto-agents/docs/provider_references/aliyun_oss_object_storage.md'

@pytest.mark.parametrize('reference', ['owned.report.json', 'owned.report.md'])
def test_extensions_never_exempt_an_unresolved_proof(reference):
    assert _reference_kind(reference, GateConfig(), source_exists=lambda _: True) == 'proof'
    gates = GateConfig(steps=[VerificationStep(proof_id=reference, targets=['tests/test_owned.py'])])
    assert _reference_kind(reference, gates, reference_paths=[reference]) == 'proof'

@pytest.mark.parametrize('reference,pattern', [('generated/schema.py', 'generated/*.py'), ('generated/client.test.ts', 'generated/*.ts'), ('generated/report.json', 'generated/*.json')])
def test_explicit_output_artifact_role_precedes_filename_inference(reference, pattern):
    from auto_agents.models import SessionState
    from auto_agents.session_verification import _owned_inventory
    step = VerificationStep(proof_id='owned.contract', runner='pytest', targets=['tests/test_owned.py::test_owned'], artifact_globs=[pattern])
    gates = GateConfig(steps=[step])
    task = {'task_id': 'task-owned', 'requirement_ids': ['REQ-owned'], 'verification_refs': ['owned.contract', reference]}
    state = SessionState(session_id='output-role', workflow_id='owned-workflow', verification_binding={'tasks': [task], 'task_scope': {'task_ids': ['task-owned'], 'requirement_ids': []}})
    assert _reference_kind(reference, gates) == 'artifact'
    required, _ = _owned_inventory(state, gates)
    assert required == ['owned.contract']
    task['verification_refs'].append('tests/test_missing.py::test_missing')
    with pytest.raises(SessionOwnershipError, match='has no executable proof') as error:
        _owned_inventory(state, gates)
    assert error.value.diagnostic['verification_ref'] == 'tests/test_missing.py::test_missing'
    task['verification_refs'] = [reference]
    with pytest.raises(SessionOwnershipError, match='no executable verification evidence'):
        _owned_inventory(state, gates)
    step.proof_id = reference
    assert _reference_kind(reference, gates) == 'proof'
