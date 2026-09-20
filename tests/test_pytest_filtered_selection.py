"""Real collection preserves safety filters and proves required node selection."""
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from auto_agents.models import SessionState
from auto_agents.repair_v2.workspace import git
from auto_agents.session_verification import _validate_required_node_selection, SessionOwnershipError
from auto_agents.verification_context import VerificationExecutionContext


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir(); (root / 'tests').mkdir()
    (root / 'pytest.ini').write_text('[pytest]\naddopts = -m "not real_service"\nmarkers = real_service\n')
    (root / 'tests/test_owned.py').write_text(
        'import pytest\nfrom pathlib import Path\n'
        'def test_owned(): Path("executed").write_text("owned")\n'
        '@pytest.mark.real_service\ndef test_real(): raise AssertionError("must remain excluded")\n')
    git(root, 'init', '-q'); git(root, 'add', '.'); git(root, 'commit', '-qm', 'retained')
    ref = 'tests/test_owned.py::test_owned'
    state = SessionState(session_id='child', verification_binding={
        'contract_revision': git(root, 'rev-parse', 'HEAD'),
        'task_scope': {'task_ids': ['owned'], 'requirement_ids': []},
        'tasks': [{'task_id': 'owned', 'verification_refs': [ref]}],
        'proof_config_paths': ['pytest.ini'], 'proof_sources': {'pytest.ini': (root / 'pytest.ini').read_text()},
    })
    environment = {k: v for k, v in os.environ.items() if k not in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS')}
    session = SimpleNamespace(project_root=root, _proof_execution_context=VerificationExecutionContext(environment, {}))
    @contextmanager
    def fixture_only(argv, *args, **kwargs):
        yield argv
    # Synthetic sources only; the incident replay separately uses real Docker
    # and metadata confinement. No test body runs during this collection.
    monkeypatch.setattr('auto_agents.verification_sandbox.verification_argv', fixture_only)
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', 'tests/test_owned.py'])
    return root, session, state, command


def retain(root, state):
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'updated retained proof')
    state.verification_binding['contract_revision'] = git(root, 'rev-parse', 'HEAD')
    state.verification_binding['proof_sources']['pytest.ini'] = (root / 'pytest.ini').read_text()


def test_default_negative_filter_selects_owned_test_without_running_or_enabling_real_service(scene):
    root, session, state, command = scene
    _validate_required_node_selection(session, state, [command])
    assert not (root / 'executed').exists()
    completed = subprocess.run(shlex.split(command), cwd=root, env=dict(session._proof_execution_context.environment),
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '1 passed, 1 deselected' in completed.stdout
    assert (root / 'executed').read_text() == 'owned'


@pytest.mark.parametrize('source', ['configuration', 'environment', 'command', 'hook', 'parameters'])
def test_excluded_owned_node_is_still_rejected(scene, source):
    root, session, state, command = scene
    if source == 'configuration':
        (root / 'pytest.ini').write_text('[pytest]\naddopts = -k test_real\n')
        retain(root, state)
    elif source == 'environment':
        session._proof_execution_context.environment['PYTEST_ADDOPTS'] = '-k test_real'
    elif source == 'command':
        command += ' -k test_real'
    elif source == 'hook':
        (root / 'tests/conftest.py').write_text(
            'import pytest\n@pytest.hookimpl(tryfirst=True)\n'
            'def pytest_collection_modifyitems(items):\n'
            '    for item in items: item.add_marker(pytest.mark.real_service)\n')
        retain(root, state)
    else:
        (root / 'tests/test_owned.py').write_text(
            'import pytest\n@pytest.mark.parametrize("value", [1, 2])\n'
            'def test_owned(value): raise AssertionError("collection must not execute")\n')
        retain(root, state)
        command += ' -k "not [2]"'
    with pytest.raises(SessionOwnershipError, match='lacks unfiltered executable evidence'):
        _validate_required_node_selection(session, state, [command])
    assert not (root / 'executed').exists()


def test_observation_reused_only_for_same_retained_source_and_environment(scene, monkeypatch):
    root, session, state, command = scene
    from auto_agents import pytest_selection
    run = pytest_selection.subprocess.run
    calls = []
    def observe(argv, **kwargs):
        if argv[:2] == ['sh', '-c']: calls.append(argv)
        return run(argv, **kwargs)
    monkeypatch.setattr(pytest_selection.subprocess, 'run', observe)
    _validate_required_node_selection(session, state, [command, command])
    _validate_required_node_selection(session, state, [command])
    assert len(calls) == 1
    session._proof_execution_context.environment['PYTEST_ADDOPTS'] = '-k test_real'
    with pytest.raises(SessionOwnershipError): _validate_required_node_selection(session, state, [command])
    assert len(calls) == 2
    session._proof_execution_context.environment.pop('PYTEST_ADDOPTS')
    # Today's changed config must not replace the retained source.
    (root / 'pytest.ini').write_text('[pytest]\naddopts = -k test_real\n')
    _validate_required_node_selection(session, state, [command])
    assert len(calls) == 2
    retain(root, state)
    with pytest.raises(SessionOwnershipError): _validate_required_node_selection(session, state, [command])
    assert len(calls) == 3
