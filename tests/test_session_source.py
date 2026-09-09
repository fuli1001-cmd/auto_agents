"""Private commits survive a second real public child handoff and restart."""
import json
from pathlib import Path

import pytest

from auto_agents.config import load_session_state
from auto_agents.git_ops import head_ref
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from test_engine_child_recovery import ObservationBoundary, parent_workflow
from test_session_verification_ownership import project


def test_second_child_inherits_private_source_and_survives_parent_restart(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    store, _, _ = parent_workflow(root, child)
    shared_head, shared_index = head_ref(root), (root / '.git/index').read_bytes()
    calls, observed = [], []

    def agent(self, request):
        if request.purpose.startswith('collab'):
            parent = load_session_state(root, 'parent')
            if len(calls) == 1:
                reply = 'ROUTE_WORKFLOW v1: ' + json.dumps({
                    'target': 'fix', 'reason': 'Continue repairing the same owned value',
                    'issue_seed': {'summary': 'Retain the verified value and add the next implementation step'}})
            else:
                observed.append(request.cwd)
                assert (request.cwd / 'second.txt').read_text() == 'second private child'
                assert parent.candidate_custody['consumed_delivery']['revision'] == head_ref(request.cwd)
                raise ObservationBoundary()
        elif request.purpose.endswith("_converse"):
            assert request.cwd != root
            assert (request.cwd / "value.py").read_text() == "VALUE = 1\n"
            reply = 'FIX_DISPOSITION v1: {"decision":"fix","summary":"Continue the owned repair","reason":"An authorized second step remains"}'
        else:
            assert request.cwd != root
            if calls:
                assert (request.cwd / 'value.py').read_text() == 'VALUE = 1\n'
                (request.cwd / 'second.txt').write_text('second private child')
            else:
                (request.cwd / 'value.py').write_text('VALUE = 1\n')
            calls.append(request.cwd)
            reply = 'Fixed\nCOMMIT_MESSAGE: Preserve private child lineage'
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)

    monkeypatch.setattr(Orchestrator, '_call_with_failover', agent)
    for _ in range(2):
        with pytest.raises(ObservationBoundary):
            Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert observed[0] == observed[1]
    assert head_ref(root) == shared_head
    assert (root / '.git/index').read_bytes() == shared_index
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    assert not (root / 'second.txt').exists()


def test_registered_source_rejects_tampering_and_survives_source_retirement(tmp_path, monkeypatch):
    import copy
    from auto_agents.session_source import resolve_source
    from auto_agents.session_verification import SessionOwnershipError
    test_second_child_inherits_private_source_and_survives_parent_restart(tmp_path, monkeypatch)
    root = tmp_path / 'demo'
    receivers = [load_session_state(root, path.parent.name)
                 for path in (root / '.auto-agents/state/sessions').glob('*/session_state.json')]
    receiver = next(state for state in receivers if state.source_descriptor)
    original = copy.deepcopy(receiver.source_descriptor)
    receiver.source_descriptor['tree'] = '0' * 40
    with pytest.raises(SessionOwnershipError, match='descriptor changed'):
        resolve_source(root, receiver)
    receiver.source_descriptor = original
    source = Path(original['checkout'])
    source.rename(source.with_name('retired-source'))
    # Materialized recipients retain their own objects and authority.
    assert resolve_source(root, receiver) == Path(receiver.candidate_custody['checkout'])
    receiver.parent_handoff_id = 'missing-handoff'
    with pytest.raises(SessionOwnershipError, match='handoff is unavailable'):
        resolve_source(root, receiver)
