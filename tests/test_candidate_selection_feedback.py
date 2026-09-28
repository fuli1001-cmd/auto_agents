"""Missing candidate proofs return to the same writer without losing its issue."""
import shlex
import json
import sys
from pathlib import Path

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.workflow_chain import IssueBriefBuilder
from test_session_verification_ownership import project


@pytest.mark.parametrize('mode', ['fresh', 'fresh_new_file', 'fresh_new_file_target', 'resume',
                                  'collection_fault', 'resume_collection_fault'])
@pytest.mark.parametrize('managed', [False, True])
def test_candidate_selection_failure_has_durable_feedback_and_preserves_budget(tmp_path, monkeypatch, mode, managed):
    root, state = project(tmp_path)
    new_file = mode in {'fresh_new_file', 'fresh_new_file_target'}
    required = ('tests/test_planned.py' if mode == 'fresh_new_file_target' else
                'tests/test_planned.py::test_planned_regression' if new_file else
                'tests/test_owned.py::test_planned_regression')
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
    if managed:
        from test_recovery_native import activate
        activate(root,tmp_path/'kernel-control',monkeypatch)
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
            target = request.cwd / ('tests/test_planned.py' if new_file else 'tests/test_owned.py')
            with target.open('a') as stream:
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
        if managed:
            def local(self,request):
                if request.purpose != 'review': return writer(request)
                props = request.response_schema['properties']['change_coverage']['items']['properties']
                checks = props['requirement']['enum'][:-1]
                text = json.dumps({'decision':'APPROVE','findings':[],
                    'coverage':[{'requirement':check,'nodes':[required]} for check in checks],
                    'change_coverage':[{'change':key,'requirement':checks[0],'reason':'Required regression',
                                        'evidence':required} for key in props['change'].get('enum',[])]})
                return AgentResult(True,['independent-fixture-reviewer'],request.output_path,summary=text)
            monkeypatch.setattr(Orchestrator,'_call_with_failover_owned',local)
        else: monkeypatch.setattr(orch, '_call_with_failover', writer)
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


def test_skipped_planned_file_cannot_complete_candidate_receipt(tmp_path, monkeypatch):
    root, state = project(tmp_path)
    planned = 'tests/test_planned.py'
    state.fix_verify_command = shlex.join([sys.executable, '-m', 'pytest', '-q',
        'tests/test_owned.py::test_owned', planned])
    state.hard_ceiling = 1
    save_session_state(root, state)
    IssueBriefBuilder(root, state.session_id).materialize({
        'summary': 'Repair the classified value defect', 'expected': 'The value becomes one',
        'reproduction': ['Read the existing value'], 'decision': 'fix',
        'verification_scope': {'mode': 'focused_fix'},
        'verification_command': state.fix_verify_command})

    def writer(request):
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        (request.cwd / planned).write_text('import pytest\n'
            '@pytest.mark.skip(reason="not implemented")\n'
            'def test_planned(): raise AssertionError("test body did not run")\n')
        reply = 'Repaired value\nCOMMIT_MESSAGE: Repair the classified value defect'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)

    orch = Orchestrator(root, user_input_fn=lambda *_args, **_kwargs: 'y')
    monkeypatch.setattr(orch, '_call_with_failover', writer)
    result = Session(orch, mode='fix', auto_approve=True).resume(state.session_id)
    failures = [entry['verification'] for entry in result.execution_log
                if entry.get('action') == 'receipt_verification' and not entry['verification']['ok']]
    assert failures
    assert failures[0]['failure_kind'] == 'candidate_execution'
    assert failures[0]['diagnostic']['unexecuted_nodes'] == [planned + '::test_planned']
    assert not result.candidate_custody.get('delivered_revision')
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
