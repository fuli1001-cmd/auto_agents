import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.config import load_session_state
from auto_agents.models import SessionState
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session


@pytest.mark.parametrize('resumed', [False, True])
def test_provider_recovery_question_is_visible_before_input(tmp_path, resumed):
    project = tmp_path / 'project'
    Orchestrator.init_project(project, 'dialogue', 'mock')
    stream = io.StringIO()
    orch = Orchestrator(project, agent_output_stream=stream)
    session = Session(orch, mode='provider_resolve')
    state = SessionState(session_id='dialogue', mode='provider_resolve', status='conversing', goal='Assess existing references')
    session._save(state)
    if resumed:
        state = load_session_state(project, state.session_id)
        session = Session(orch, mode='provider_resolve')
    question = '请确认新增的模型端点；已有批准保持有效。'
    def answer(*args, **kwargs):
        assert question in stream.getvalue()
        user_log = orch.reporter.root / 'user.log'
        assert question in user_log.read_text()
        return '保留原端点'
    with patch.object(session, '_call_agent', side_effect=[question, 'GOAL_CLEAR']), \
         patch.object(session, '_prompt_user', side_effect=answer):
        result = session._phase_converse(state)
    assert result.status == 'executing'
    events = [json.loads(line) for line in (orch.reporter.root / 'events.jsonl').read_text().splitlines()]
    assert any(e['type']=='user.message' and question in e.get('message','') for e in events)
    assert not any(e['type']=='diagnostic.message' and question in e.get('message','') for e in events)
    orch.reporter.close()


def test_execution_assistance_is_visible_without_exposing_execution_prose(tmp_path):
    project = tmp_path / 'project'
    Orchestrator.init_project(project, 'dialogue', 'mock')
    stream = io.StringIO()
    orch = Orchestrator(project, agent_output_stream=stream)
    session = Session(orch, mode='provider_resolve')
    session._save(SessionState(session_id='dialogue', mode='provider_resolve', status='executing', goal='Recover'))
    session._print('internal implementation details')
    session._print('需要确认具体新增能力', user_facing=True)
    assert '需要确认具体新增能力' in stream.getvalue()
    assert 'internal implementation details' not in stream.getvalue()
    orch.reporter.close()
