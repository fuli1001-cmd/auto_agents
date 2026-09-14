import json
from unittest.mock import patch

import pytest

from auto_agents.adapters.codex import CodexProgressDecoder
from auto_agents.models import AgentRequest, AgentTermination, ProvidersExhaustedError, SmartTimeoutConfig
from test_failover import _SequenceAdapter, _make_result, _stub_orchestrator
from test_smart_timeout import _supervisor


def test_waiting_for_network_is_activity_but_never_semantic_progress(tmp_path):
    clock = [0.0]
    decoder = CodexProgressDecoder()
    config = SmartTimeoutConfig()
    supervisor = _supervisor(tmp_path, clock, config, decoder)
    message = 'Reconnecting... waiting for network (Connection failed: error sending request)'
    with patch('auto_agents.supervision.time.monotonic', side_effect=lambda: clock[0]):
        for second in (10, 25, 55):
            clock[0] = second
            supervisor.observe_io('stdout', json.dumps({'type': 'error', 'message': message}))
            assert supervisor.poll() is None
            assert supervisor.last_semantic_progress == 0
        # Endless reconnect notifications still cannot renew the progress lease.
        clock[0] = config.semantic_stall_seconds + 1
        supervisor.observe_io('stdout', json.dumps({'type': 'error', 'message': message}))
        assert supervisor.poll() == 'semantic_stall'
    terminal = list(decoder.feed('stdout', json.dumps({'type': 'turn.failed', 'error': {'message': message}})))
    assert terminal[0].kind == 'error'


@pytest.mark.parametrize('outcome', ['recovered', 'disconnected', 'quota'])
def test_read_only_review_connection_recovery_is_bounded_and_preserves_session(tmp_path, outcome):
    failure = _make_result(ok=False, returncode=1, provider_session_id='review-session',
        termination=AgentTermination(reason='provider_error'),
        stderr='tls handshake eof' if outcome != 'quota' else 'quota exhausted')
    adapter = _SequenceAdapter([failure, _make_result(ok=True)] if outcome == 'recovered' else [failure])
    orchestrator = _stub_orchestrator({'codex': {}}, 'codex', {'codex': adapter})
    request = AgentRequest(stage='self_repair_plan_review', effort='max', prompt='independently audit',
        cwd=tmp_path, output_path=tmp_path / 'output.json', sandbox_mode='read-only', record_execution_incidents=False)
    if outcome == 'recovered':
        assert orchestrator._call_with_failover(request).ok
    else:
        with pytest.raises(ProvidersExhaustedError) as failure_info:
            orchestrator._call_with_failover(request)
        assert failure_info.value.category == ('connection' if outcome == 'disconnected' else 'provider_error')
    assert adapter.calls == (1 if outcome == 'quota' else 2)
    if outcome != 'quota':
        assert adapter.requests[1].resume_session_id == 'review-session'
        assert all(r.sandbox_mode == 'read-only' for r in adapter.requests)
