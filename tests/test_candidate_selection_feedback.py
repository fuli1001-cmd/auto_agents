"""Missing candidate proofs return to the same writer without losing its issue."""
import shlex
import sys
from pathlib import Path

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_chain import IssueBriefBuilder
from test_session_verification_ownership import project


@pytest.mark.parametrize('mode', ['fresh', 'resume', 'collection_fault', 'resume_collection_fault'])
def test_candidate_selection_failure_has_durable_feedback_and_preserves_budget(tmp_path, monkeypatch, mode):
    root, state = project(tmp_path)
    required = 'tests/test_owned.py::test_planned_regression'
    state.fix_verify_command = shlex.join([sys.executable, '-m', 'pytest', '-q', '-m',
        'not real_service', 'tests/test_owned.py::test_owned', required])
    state.hard_ceiling = 3
    state.goal = 'Complete the original end-to-end outcome'
    save_session_state(root, state)
    IssueBriefBuilder(root, state.session_id).materialize({
        'summary': 'Repair the classified value defect', 'expected': 'The value becomes one',
        'reproduction': ['Read the existing value'], 'decision': 'fix',
        'verification_scope': {'mode': 'focused_fix'},
        'verification_command': state.fix_verify_command})
    calls = []
    collection_fault = mode.endswith('collection_fault')
    def writer(request):
        assert 'Repair the classified value defect' in request.prompt
        assert required in request.prompt
        calls.append(request.prompt)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        if collection_fault:
            (request.cwd / 'tests/conftest.py').write_text("raise RuntimeError('candidate collection unavailable')\n")
        elif len(calls) == 2:
            assert 'candidate is missing required tests' in request.prompt
            with (request.cwd / 'tests/test_owned.py').open('a') as stream:
                # The retained test checks actual file behavior. This separate
                # fixture node exercises the promised collection obligation.
                stream.write('\ndef test_planned_regression():\n'
                             '    value = 1\n    assert value == 1\n')
        reply = 'Repaired value\nCOMMIT_MESSAGE: Repair the classified value defect'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    def resume():
        orch = Orchestrator(root, user_input_fn=lambda *_args, **_kwargs: 'y')
        monkeypatch.setattr(orch, '_call_with_failover', writer)
        return Session(orch, mode='fix', auto_approve=True).resume(state.session_id)
    if mode.startswith('resume'):
        from auto_agents import session_candidate
        record = session_candidate.record_receipt
        def interrupt(session, child):
            record(session, child)
            raise KeyboardInterrupt()
        with monkeypatch.context() as context:
            context.setattr(session_candidate, 'record_receipt', interrupt)
            assert resume().status == 'paused'
        assert load_session_state(root, state.session_id).current_attempt == 1
    result = resume()
    failures = [entry['verification'] for entry in result.execution_log
                if entry.get('action') == 'receipt_verification' and not entry['verification']['ok']]
    assert failures, [(row.get('action'), row.get('result')) for row in result.execution_log]
    if collection_fault:
        assert result.status == 'blocked' or result.resolution == 'verification_inconclusive'
        assert len(calls) == result.current_attempt == 1
        assert failures[0]['retry_fix'] is False
        assert 'candidate collection unavailable' in failures[0]['diagnostic']['stderr_tail']
        assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    else:
        assert result.status == 'completed', result.resolution
        assert len(calls) == result.current_attempt == 2
        assert failures[0]['retry_fix'] is True
        assert failures[0]['diagnostic']['missing_nodes'] == [required]
        assert failures[0]['executed_commands'] == 0
        candidate = Path(result.candidate_custody['checkout'])
        assert (candidate / 'value.py').read_text() == 'VALUE = 1\n'
        assert result.candidate_custody['delivered_revision']
        assert any(row.get('action') == 'receipt_verification' and row['verification']['ok']
                   for row in result.execution_log)
        assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    assert result.hard_ceiling == 3


def test_private_prompt_uses_control_issue_and_rejects_foreign_handoff(tmp_path):
    root, state = project(tmp_path)
    private = tmp_path / 'private'; private.mkdir()
    IssueBriefBuilder(root, state.session_id).materialize({'summary': 'Original classified issue'})
    IssueBriefBuilder(private, state.session_id).materialize({'summary': 'Unrelated private issue'})
    session = Session(Orchestrator(root), mode='fix')
    session._custody_control_root = root
    session.project_root = private
    session._build_repo_map_section_for_session = lambda *_args: ''
    prompt = session._build_fix_prompt(state, '')
    assert 'Original classified issue' in prompt and 'Unrelated private issue' not in prompt
    IssueBriefBuilder(root, state.session_id).materialize({'summary': 'Foreign', 'source_handoff_id': 'other'})
    from auto_agents.session_verification import SessionOwnershipError
    with pytest.raises(SessionOwnershipError, match='another session or handoff'):
        session._build_fix_prompt(state, '')
