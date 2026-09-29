from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from auto_agents.models import AgentRequest, AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.recovery import KernelError
from auto_agents.recovery.native import provider
from auto_agents.recovery.policy import enable
from test_recovery_native import activate
from test_session_verification_ownership import project


def setup(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    store = activate(root, tmp_path/'control', monkeypatch)
    stream = store.binding(root, 'session:' + child.session_id)
    enable(store, stream)
    orch = Orchestrator(root)
    orch._invocation_context = {'command': 'fix', 'session_id': child.session_id}
    return root, store, stream, orch


def request(root, tmp_path, role='investigator'):
    return AgentRequest('self_repair_' + role, 'medium', 'Inspect the original failure', root,
                        tmp_path/role, purpose='diagnosis', sandbox_mode='read-only',
                        record_execution_incidents=False, attempt_id=role)


def test_parallel_roles_share_usage_but_never_publish_business_proofs(tmp_path, monkeypatch):
    root, store, stream, orch = setup(tmp_path, monkeypatch)
    before = store.load(stream)
    barrier = Barrier(2)
    calls = []
    def execute(req):
        calls.append(req.stage)
        barrier.wait(timeout=10)
        return AgentResult(True, ['read-only'], req.output_path, summary='Inspected evidence')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda role: provider(orch, request(root, tmp_path, role), execute),
                                ['investigator', 'reviewer']))
    assert all(result.ok for result in results)
    after = store.replay(stream)
    assert after['budget']['model_calls'] == before['budget']['model_calls'] + 2
    assert after['budget']['repair_model_calls'] == before['budget']['repair_model_calls'] + 2
    assert after['tasks'] == before['tasks'] and after['commands'] == before['commands']
    assert provider(orch, request(root, tmp_path), lambda req: pytest.fail('must reuse receipt')).ok
    assert len(calls) == 2


def test_crashed_role_cannot_be_redispatched_under_a_new_request(tmp_path, monkeypatch):
    from dataclasses import replace
    root, store, stream, orch = setup(tmp_path, monkeypatch)
    req = request(root, tmp_path)
    def crash(req): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt): provider(orch, req, crash)
    before = store.replay(stream)
    for retry in [req, replace(req, attempt_id='different', prompt='Rephrased inspection')]:
        with pytest.raises(KernelError):
            provider(orch, retry, lambda req: pytest.fail('unknown effects cannot be repeated'))
    assert store.replay(stream) == before


def test_duplicate_concurrent_role_dispatches_at_most_once(tmp_path, monkeypatch):
    root, store, stream, orch = setup(tmp_path, monkeypatch)
    req = request(root, tmp_path)
    calls = []
    def execute(req):
        calls.append(req.stage)
        return AgentResult(True, ['local'], req.output_path, summary='done')
    def call(_):
        try: return provider(orch, req, execute)
        except KernelError: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(call, range(2)))
    assert len(calls) == 1
    assert store.replay(stream)['budget']['model_calls'] == 1


def test_durable_auxiliary_result_settles_after_acknowledgement_crash(tmp_path, monkeypatch):
    from auto_agents.recovery import policy
    root, store, stream, orch = setup(tmp_path, monkeypatch)
    req = request(root, tmp_path)
    apply = policy.apply
    def interrupted(store, stream, kind, data, identity):
        if kind == 'recovery_auxiliary_finished': raise KeyboardInterrupt()
        return apply(store, stream, kind, data, identity)
    with monkeypatch.context() as patch:
        patch.setattr(policy, 'apply', interrupted)
        with pytest.raises(KeyboardInterrupt):
            provider(orch, req, lambda r: AgentResult(True, ['local'], r.output_path, summary='Retained response'))
    enable(store, stream)
    assert provider(orch, req, lambda r: pytest.fail('receipt must settle without redispatch')).ok
    assert store.replay(stream)['budget']['model_calls'] == 1
