"""Explain retained node rejection without changing execution admission."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from auto_agents.models import SessionState
from auto_agents.session_verification import SessionOwnershipError, _validate_required_node_selection
from auto_agents.verification_context import VerificationExecutionContext


NODE = 'tests/test_project_api_moderation.py::test_req_275_body_semantics_precede_http_for_text_image_and_video_safety'
COMMAND = './.conda/bin/python -m pytest -q ' + NODE


def selection(tmp_path, config='[pytest]\n', environment=None):
    state = SessionState(session_id='retained-child', workflow_id='retained-workflow',
        parent_handoff_id='original', verification_binding={
            'contract_revision': 'retained', 'contract_fingerprint': 'unchanged',
            'task_scope': {'task_ids': [], 'requirement_ids': ['REQ-275']},
            'tasks': [{'task_id': 'task-460', 'requirement_ids': ['REQ-275'], 'verification_refs': [NODE]}],
            'proof_config_paths': ['pytest.ini'], 'proof_sources': {'pytest.ini': config}})
    session = SimpleNamespace(project_root=tmp_path,
        _proof_execution_context=VerificationExecutionContext(environment or {}, {}))
    return session, state


@pytest.mark.parametrize('source', ['configuration', 'environment', 'discovery'])
def test_explicit_node_rejection_reports_effective_retained_selection(tmp_path, source, monkeypatch):
    monkeypatch.setattr('auto_agents.pytest_selection.selected_nodes', lambda *a: (set(), set()))
    config, environment = '[pytest]\n', {}
    if source == 'configuration':
        config += 'addopts = -m control\n'
    elif source == 'environment':
        environment['PYTEST_ADDOPTS'] = '-m control'
    else:
        config += 'python_functions = test_control\n'
    session, state = selection(tmp_path, config, environment)
    before = deepcopy(state.to_dict())
    # Today's shared configuration cannot replace the frozen input.
    (tmp_path / 'pytest.ini').write_text('[pytest]\npython_functions = test_*\n')
    with pytest.raises(SessionOwnershipError) as rejected:
        _validate_required_node_selection(session, state, [COMMAND])
    diagnostic = rejected.value.diagnostic
    assert diagnostic['verification_ref'] == NODE and diagnostic['retry_fix'] is False
    assert diagnostic['commands'] == [COMMAND]
    assert diagnostic['task_scope'] == before['verification_binding']['task_scope']
    detail, = diagnostic['selection_rejections']
    assert detail['command'] == COMMAND and detail['target'] == NODE
    assert detail['configuration'] == ['pytest.ini'] and detail['cwd'] == '.'
    assert detail['selection_restricted'] is (source != 'discovery')
    assert detail['discovery_excluded'] is True
    assert ('-m control' if source != 'discovery' else 'python_functions=test_control') in detail['effective_pytest_args']
    assert diagnostic['unparsed_commands'] == []
    assert state.to_dict() == before


def test_unfiltered_explicit_node_keeps_its_existing_admission(tmp_path):
    session, state = selection(tmp_path)
    before = deepcopy(state.to_dict())
    _validate_required_node_selection(session, state, [COMMAND])
    assert state.to_dict() == before


def test_selection_diagnostics_are_bounded_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr('auto_agents.pytest_selection.selected_nodes', lambda *a: (set(), set()))
    session, state = selection(tmp_path, environment={'PYTEST_ADDOPTS': '-m token=private-value'})
    with pytest.raises(SessionOwnershipError) as rejected:
        _validate_required_node_selection(session, state, [COMMAND] * 20)
    entries = rejected.value.diagnostic['selection_rejections']
    assert len(entries) == 8
    assert all('private-value' not in entry['effective_pytest_args'] for entry in entries)
    assert all(len(entry['effective_pytest_args']) <= 2000 for entry in entries)
