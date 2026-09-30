from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from auto_agents.models import AgentResult
from auto_agents.recovery.native import review_candidate
from test_recovery_convergence_policy import scene, verify
from test_recovery_kernel import emit


@pytest.mark.parametrize('long_reason', [False, True])
def test_large_verification_matrix_is_available_without_embedding_in_review(scene, tmp_path, monkeypatch, long_reason):
    from auto_agents.recovery import native
    from auto_agents.repair_v2 import scope as repair_scope
    store, original = scene
    contract = replace(original, task_id='fix:child')
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    checks = {'tests/test_value.py::test_' + str(i): 'passed' for i in range(3000)}
    reason = ('pre-existing baseline trace\n' * 360_000) if long_reason else 'real verification'
    verify(store, contract, 'verified', checks, ok=True, reason=reason)
    state = SimpleNamespace(candidate_custody={'receipt': {'fingerprint': 'receipt', 'base_revision': 'base'}})
    seen = []
    def provider(request):
        seen.append(request)
        return AgentResult(True, [], request.output_path, summary='{}')
    owner = SimpleNamespace(project_root=tmp_path, orch=SimpleNamespace(_call_with_failover_owned=provider))
    monkeypatch.setattr(native, 'context', lambda *args: (store, 'workflow', tmp_path, 'fix', 'child', state))
    monkeypatch.setattr(native, '_source', lambda *args: 'a'*64)
    monkeypatch.setattr(repair_scope, 'changes', lambda *args, **kwargs: {})
    monkeypatch.setattr(native, 'perform', lambda owner, phase, key, execute, classify, **kwargs: execute())
    verification = {'ok': True, 'attestation_level': 'release',
                    'reason': reason,
                    'verification_checks': [{'id': key, 'status': value, 'command': 'long selected command ' * 80}
                                            for key, value in checks.items()],
                    'progress_checks': list(checks), 'baseline_failures': []}
    assert len(json.dumps(verification)) > 1_048_576
    review_candidate(owner, state, verification)
    assert len(str(seen[0].prompt)) < 30_000
    assert 'full_evidence' in str(seen[0].prompt) and '"check_count": 3000' in str(seen[0].prompt)
    if long_reason: assert len(reason) > 8_000_000 and reason not in str(seen[0].prompt)
    artifacts = list((tmp_path/'.auto-agents/recovery-evidence').glob('*.json'))
    observed = next(json.loads(path.read_text()) for path in artifacts if not path.name.endswith('-contract.json'))
    assert set(observed['checks']) == set(checks)
    assert observed['reason'] == reason


def test_provider_failure_keeps_bounded_diagnostic_receipt(scene, tmp_path, monkeypatch):
    from auto_agents.recovery import native
    from auto_agents.repair_v2 import scope as repair_scope
    store, original = scene
    contract = replace(original, task_id='fix:child')
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    verify(store, contract, 'verified', {'tests/test_value.py::test_owned': 'passed'}, ok=True)
    state = SimpleNamespace(candidate_custody={'receipt': {'fingerprint': 'receipt', 'base_revision': 'base'}})
    reply = AgentResult(False, [], tmp_path/'result', returncode=1,
                        stderr='provider error; Authorization: Bearer SECRET-VALUE')
    owner = SimpleNamespace(project_root=tmp_path,
        orch=SimpleNamespace(_call_with_failover_owned=lambda request: reply,
                             _failover_error_category=lambda result: 'provider_error'))
    monkeypatch.setattr(native, 'context', lambda *args: (store, 'workflow', tmp_path, 'fix', 'child', state))
    monkeypatch.setattr(native, '_source', lambda *args: 'a'*64)
    monkeypatch.setattr(repair_scope, 'changes', lambda *args, **kwargs: {})
    monkeypatch.setattr(native, 'perform', lambda owner, phase, key, execute, classify, **kwargs: execute())
    result = review_candidate(owner, state, {'ok': True, 'reason': 'real verification'})
    assert result['kind'] == 'environment_blocked'
    assert result['provider_receipt']['returncode'] == 1
    assert result['provider_receipt']['stdout_observed'] is False
    assert 'SECRET-VALUE' not in result['provider_receipt']['stderr_excerpt']
