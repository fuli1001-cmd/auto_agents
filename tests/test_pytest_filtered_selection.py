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
from auto_agents.execution_binding import RunnerContextError, test_invocations as parse_test_invocations
from auto_agents.pytest_selection import selected_nodes
from auto_agents.repair_v2.workspace import git
from auto_agents.session_verification import (_validate_required_node_selection,
    planned_pytest_execution_nodes, SessionOwnershipError)
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


def test_focused_fix_admits_planned_node_then_requires_candidate_selection(scene, monkeypatch):
    root, session, state, _ = scene
    existing = 'tests/test_owned.py::test_owned'
    planned = 'tests/test_owned.py::TestFuture::test_new'
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', '-m', 'not real_service',
                          existing, planned])
    state.fix_verify_command = command
    state.verification_binding['task_scope'] = {
        'mode': 'focused_fix', 'task_ids': [], 'requirement_ids': [],
        'verification_refs': [existing, planned]}
    state.verification_binding['tasks'] = []
    retained_revision = state.verification_binding['contract_revision']

    _validate_required_node_selection(session, state, [command])
    assert not (root / 'executed').exists()

    monkeypatch.setattr('auto_agents.session_candidate.validate_receipt', lambda _state: None)
    monkeypatch.setattr('auto_agents.session_source.validate_checkout', lambda *_args: None)
    state.candidate_custody = {'checkout': str(root),
                               'receipt': {'source_revision': retained_revision}}
    with pytest.raises(RunnerContextError, match='retained pytest selection failed'):
        _validate_required_node_selection(session, state, [command])
    source = (root / 'tests/test_owned.py').read_text()
    (root / 'tests/test_owned.py').write_text(source + '\nclass TestFuture:\n'
        '    def test_new(self): pass\n')
    retain(root, state)
    state.candidate_custody['receipt']['source_revision'] = state.verification_binding['contract_revision']
    # The retained source remains the initial commit; the candidate is a new
    # committed snapshot whose exact node must collect under the same filter.
    state.verification_binding['contract_revision'] = retained_revision
    _validate_required_node_selection(session, state, [command])

    (root / 'tests/test_owned.py').write_text(source + '\nclass TestFuture:\n'
        '    @pytest.mark.real_service\n'
        '    def test_new(self): pass\n')
    retain(root, state)
    state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
    state.verification_binding['contract_revision'] = retained_revision
    with pytest.raises(SessionOwnershipError, match='lacks unfiltered executable evidence'):
        _validate_required_node_selection(session, state, [command])
    assert not (root / 'executed').exists()


def test_task_owned_fix_cannot_provisionally_admit_missing_node(scene):
    root, session, state, _ = scene
    existing = 'tests/test_owned.py::test_owned'
    missing = 'tests/test_owned.py::TestFuture::test_new'
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', '-m', 'not real_service',
                          existing, missing])
    state.verification_binding['tasks'][0]['verification_refs'] = [existing, missing]
    with pytest.raises(RunnerContextError, match='retained pytest selection failed'):
        _validate_required_node_selection(session, state, [command])
    assert not (root / 'executed').exists()


@pytest.mark.parametrize('filtered', [False, True])
def test_focused_fix_admits_planned_test_file_but_requires_candidate_collection(scene, monkeypatch, filtered):
    root, session, state, _ = scene
    if not filtered:
        (root / 'pytest.ini').write_text('[pytest]\nmarkers = real_service\n')
        retain(root, state)
    existing = 'tests/test_owned.py::test_owned'
    planned = 'tests/test_future.py::test_future'
    arguments = [sys.executable, '-m', 'pytest', '-q']
    if filtered:
        arguments += ['-m', 'not real_service']
    command = shlex.join([*arguments, existing, planned])
    state.fix_verify_command = command
    state.verification_binding['task_scope'] = {
        'mode': 'focused_fix', 'task_ids': [], 'requirement_ids': [],
        'verification_refs': [existing, planned]}
    state.verification_binding['tasks'] = []
    retained_revision = state.verification_binding['contract_revision']

    _validate_required_node_selection(session, state, [command], candidate=False)
    assert not (root / 'executed').exists()
    monkeypatch.setattr('auto_agents.session_candidate.validate_receipt', lambda _state: None)
    monkeypatch.setattr('auto_agents.session_source.validate_checkout', lambda *_args: None)
    state.candidate_custody = {'checkout': str(root), 'receipt': {'source_revision': retained_revision}}
    with pytest.raises(RunnerContextError) as failure:
        _validate_required_node_selection(session, state, [command], candidate=True)
    assert failure.value.diagnostic['missing_nodes'] == [planned]

    (root / 'tests/test_future.py').write_text('def test_future(): pass\n')
    retain(root, state)
    state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
    state.verification_binding['contract_revision'] = retained_revision
    _validate_required_node_selection(session, state, [command], candidate=True)

    if filtered:
        (root / 'tests/test_future.py').write_text(
            'import pytest\n@pytest.mark.real_service\ndef test_future(): pass\n')
        retain(root, state)
        state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
        state.verification_binding['contract_revision'] = retained_revision
        with pytest.raises(SessionOwnershipError, match='lacks unfiltered executable evidence'):
            _validate_required_node_selection(session, state, [command], candidate=True)
    (root / 'tests/conftest.py').write_text("raise RuntimeError('collection failed')\n")
    retain(root, state)
    state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
    state.verification_binding['contract_revision'] = retained_revision
    with pytest.raises(RunnerContextError) as failure:
        _validate_required_node_selection(session, state, [command], candidate=True)
    assert failure.value.kind == 'discovery'
    assert not (root / 'executed').exists()


def test_task_owned_fix_cannot_provisionally_admit_missing_test_file(scene):
    root, session, state, _ = scene
    existing = 'tests/test_owned.py::test_owned'
    missing = 'tests/test_future.py::test_future'
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', '-m', 'not real_service',
                          existing, missing])
    state.verification_binding['tasks'][0]['verification_refs'] = [existing, missing]
    with pytest.raises(RunnerContextError, match='retained pytest selection failed'):
        _validate_required_node_selection(session, state, [command])
    assert not (root / 'executed').exists()


@pytest.mark.parametrize('filtered', [False, True])
def test_focused_fix_admits_planned_file_target_only_until_candidate_collects_it(scene, monkeypatch, filtered):
    root, session, state, _ = scene
    if not filtered:
        (root / 'pytest.ini').write_text('[pytest]\nmarkers = real_service\n')
        retain(root, state)
    existing = 'tests/test_owned.py::test_owned'
    planned = 'tests/test_future.py'
    command = shlex.join([sys.executable, '-m', 'pytest', '-q',
                          *(['-m', 'not real_service'] if filtered else []), existing, planned])
    state.fix_verify_command = command
    state.verification_binding['task_scope'] = {
        'mode': 'focused_fix', 'task_ids': [], 'requirement_ids': [],
        'verification_refs': [existing, planned]}
    state.verification_binding['tasks'] = []
    retained_revision = state.verification_binding['contract_revision']

    _validate_required_node_selection(session, state, [command], candidate=False)
    assert not (root / 'executed').exists()
    monkeypatch.setattr('auto_agents.session_candidate.validate_receipt', lambda _state: None)
    monkeypatch.setattr('auto_agents.session_source.validate_checkout', lambda *_args: None)
    state.candidate_custody = {'checkout': str(root), 'receipt': {'source_revision': retained_revision}}
    with pytest.raises(RunnerContextError) as failure:
        _validate_required_node_selection(session, state, [command], candidate=True)
    assert failure.value.diagnostic['missing_nodes'] == [planned]

    (root / planned).write_text('def test_future(): pass\n')
    retain(root, state)
    state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
    state.verification_binding['contract_revision'] = retained_revision
    _validate_required_node_selection(session, state, [command], candidate=True)

    (root / planned).write_text('import pytest\n'
        '@pytest.mark.real_service\ndef test_future(): pass\n')
    retain(root, state)
    state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
    state.verification_binding['contract_revision'] = retained_revision
    if filtered:
        with pytest.raises(SessionOwnershipError, match='lacks unfiltered executable evidence'):
            _validate_required_node_selection(session, state, [command], candidate=True)
    else:
        _validate_required_node_selection(session, state, [command], candidate=True)

    if filtered:
        (root / planned).write_text('import pytest\n'
            'def test_future(): pass\n'
            '@pytest.mark.real_service\ndef test_other(): pass\n')
        retain(root, state)
        state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
        state.verification_binding['contract_revision'] = retained_revision
        with pytest.raises(SessionOwnershipError, match='lacks unfiltered executable evidence'):
            _validate_required_node_selection(session, state, [command], candidate=True)

    (root / planned).write_text('def test_future(): pass\n'
        'def test_other(): pass\n')
    (root / 'tests/conftest.py').write_text("raise RuntimeError('collection failed')\n")
    retain(root, state)
    state.candidate_custody['receipt']['source_revision'] = git(root, 'rev-parse', 'HEAD')
    state.verification_binding['contract_revision'] = retained_revision
    with pytest.raises(RunnerContextError, match='retained pytest selection failed'):
        _validate_required_node_selection(session, state, [command], candidate=True)
    assert not (root / 'executed').exists()


def test_task_owned_fix_cannot_provisionally_admit_missing_file_target(scene):
    root, session, state, _ = scene
    existing = 'tests/test_owned.py::test_owned'
    missing = 'tests/test_future.py'
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', existing, missing])
    state.verification_binding['tasks'][0]['verification_refs'] = [existing, missing]
    with pytest.raises(RunnerContextError, match='retained pytest selection failed'):
        _validate_required_node_selection(session, state, [command])
    assert not (root / 'executed').exists()


def test_planned_file_execution_requires_every_collected_candidate_node(scene, monkeypatch):
    root, session, state, _ = scene
    planned = 'tests/test_future.py'
    command = shlex.join([sys.executable, '-m', 'pytest', '-q',
        'tests/test_owned.py::test_owned', planned])
    state.fix_verify_command = command
    state.verification_binding['task_scope'] = {
        'mode': 'focused_fix', 'task_ids': [], 'requirement_ids': [],
        'verification_refs': ['tests/test_owned.py::test_owned', planned]}
    state.verification_binding['tasks'] = []
    (root / planned).write_text('import pytest\n'
        'def test_first(): pass\n'
        '@pytest.mark.skip(reason="pending")\ndef test_second(): pass\n')
    retain(root, state)
    candidate_revision = state.verification_binding['contract_revision']
    state.verification_binding['contract_revision'] = git(root, 'rev-parse', 'HEAD~1')
    state.candidate_custody = {'checkout': str(root), 'receipt': {'source_revision': candidate_revision}}
    monkeypatch.setattr('auto_agents.session_candidate.validate_receipt', lambda _state: None)
    monkeypatch.setattr('auto_agents.session_source.validate_checkout', lambda *_args: None)
    assert planned_pytest_execution_nodes(session, state, command) == {
        planned + '::test_first', planned + '::test_second'}


def test_only_planned_absent_files_are_omitted_and_existing_filters_survive(scene):
    root, session, state, _ = scene
    existing = 'tests/test_owned.py::test_owned'
    planned = ['tests/test_first.py::test_first', 'tests/test_second.py::test_second']
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', '-m', 'not real_service',
                          existing, *planned])
    invocation, = parse_test_invocations(command)
    selected, deselected, missing = selected_nodes(session, state, invocation, expected_missing=planned)
    assert selected == {existing}
    assert deselected == set()
    assert missing == set(planned)
    assert not (root / 'executed').exists()

    unplanned = 'tests/test_unplanned.py::test_unplanned'
    invocation, = parse_test_invocations(command + ' ' + unplanned)
    with pytest.raises(RunnerContextError, match='retained pytest selection failed'):
        selected_nodes(session, state, invocation, expected_missing=planned)


def test_only_planned_file_targets_are_omitted_and_existing_nodes_collect(scene):
    root, session, state, _ = scene
    existing = 'tests/test_owned.py::test_owned'
    planned = 'tests/test_future.py'
    command = shlex.join([sys.executable, '-m', 'pytest', '-q', '-m', 'not real_service',
                          existing, planned])
    invocation, = parse_test_invocations(command)
    selected, deselected, missing = selected_nodes(session, state, invocation, expected_missing=[planned])
    assert selected == {existing}
    assert deselected == set()
    assert missing == {planned}
    assert not (root / 'executed').exists()

    invocation, = parse_test_invocations(command + ' tests/test_unplanned.py')
    with pytest.raises(RunnerContextError, match='retained pytest selection failed'):
        selected_nodes(session, state, invocation, expected_missing=[planned])

    invocation, = parse_test_invocations(shlex.join([sys.executable, '-m', 'pytest', '-q', planned]))
    selected, deselected, missing = selected_nodes(session, state, invocation, expected_missing=[planned])
    assert selected == deselected == set()
    assert missing == {planned}


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
