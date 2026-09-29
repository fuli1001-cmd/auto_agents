from dataclasses import replace
from types import SimpleNamespace
import json

from auto_agents.models import AgentResult
from auto_agents.recovery.native import review_candidate
from test_recovery_convergence_policy import scene, verify
from test_recovery_kernel import emit


def test_large_verification_matrix_is_available_without_embedding_in_review(scene, tmp_path, monkeypatch):
    from auto_agents.recovery import native
    from auto_agents.repair_v2 import scope as repair_scope
    store, original = scene
    contract = replace(original, task_id='fix:child')
    emit(store, 'task_bound', {'contract': contract.to_dict()})
    checks = {'tests/test_value.py::test_' + str(i): 'passed' for i in range(3000)}
    verify(store, contract, 'verified', checks, ok=True)
    state = SimpleNamespace(candidate_custody={'receipt': {'fingerprint': 'receipt', 'base_revision': 'base'}})
    seen = []
    def provider(request):
        seen.append(request)
        return AgentResult(True, [], request.output_path, summary='{}')
    owner = SimpleNamespace(project_root=tmp_path, orch=SimpleNamespace(_call_with_failover_owned=provider))
    monkeypatch.setattr(native, 'context', lambda *args: (store, 'workflow', tmp_path, 'fix', 'child', state))
    monkeypatch.setattr(native, '_source', lambda *args: 'a'*64)
    monkeypatch.setattr(repair_scope, 'changes', lambda *args: {})
    monkeypatch.setattr(native, 'perform', lambda owner, phase, key, execute, classify, **kwargs: execute())
    verification = {'ok': True, 'attestation_level': 'release',
                    'verification_checks': [{'id': key, 'status': value, 'command': 'long selected command ' * 80}
                                            for key, value in checks.items()],
                    'progress_checks': list(checks), 'baseline_failures': []}
    assert len(json.dumps(verification)) > 1_048_576
    review_candidate(owner, state, verification)
    assert len(str(seen[0].prompt)) < 30_000
    assert 'full_evidence' in str(seen[0].prompt) and '"check_count": 3000' in str(seen[0].prompt)
    artifacts = list((tmp_path/'.auto-agents/recovery-evidence').glob('*.json'))
    observed = next(json.loads(path.read_text()) for path in artifacts if not path.name.endswith('-contract.json'))
    assert set(observed['checks']) == set(checks)
