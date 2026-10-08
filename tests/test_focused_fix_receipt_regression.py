"""Focused receipt recovery must not adopt unimplemented historical tasks."""
import json
from types import SimpleNamespace
import pytest
from auto_agents.session_verification import _future_foreign_step
from test_session_verification_ownership import git

@pytest.mark.parametrize('source,selector,projected', [('import unittest\nclass ProtocolTests(unittest.TestCase):\n    def test_existing(self): pass\n', 'ProtocolTests::test_future', True), ('from unittest import TestCase as Base\nclass ProtocolTests(Base):\n    def test_existing(self): pass\n', 'ProtocolTests::test_future', True), ('class ProtocolTests:\n    def test_existing(self): pass\n', 'ProtocolTests::test_future', True), ('import unittest\nclass ProtocolTests(unittest.TestCase):\n    def test_future(self): pass\n', 'ProtocolTests::test_future', False), ('from helpers import ProtocolTests\n', 'ProtocolTests::test_future', False), ('class Base:\n    def test_future(self): pass\nclass ProtocolTests(Base): pass\n', 'ProtocolTests::test_future', False), ('from helpers import Base\nclass ProtocolTests(Base): pass\n', 'ProtocolTests::test_future', False), ('class ProtocolTests:\n    test_future = lambda self: None\n', 'ProtocolTests::test_future', False), ('class ProtocolTests: pass\nsetattr(ProtocolTests, "test_future", lambda self: None)\n', 'ProtocolTests::test_future', False), ('class ProtocolTests: pass\nregister_tests(ProtocolTests)\n', 'ProtocolTests::test_future', False), ('def pytest_pycollect_makeitem(*args): pass\nclass ProtocolTests: pass\n', 'ProtocolTests::test_future', False), ('class ProtocolTests: pass\n', 'ProtocolTests::test_future[case]', False), ('from helpers import test_future\n', 'test_future', False), ('def test_existing(): pass\n', 'test_future', True)])
def test_absent_foreign_nodes_require_conservative_retained_evidence(tmp_path, source, selector, projected):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_protocol.py').write_text(source)
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'retained proof')
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(), 'task_scope': {'mode': 'focused_fix'}})
    target = 'test_protocol.py::' + selector
    step = {'runner': 'pytest', 'targets': [target], 'args': ['-q', '--basetemp=.tmp-tests/proof']}
    assert _future_foreign_step(SimpleNamespace(project_root=tmp_path), state, step, {target}) is projected

@pytest.mark.parametrize('args,hook', [(['-k', 'future'], ''), (['-p', 'custom_plugin'], ''), (['-o', 'python_functions=case_*'], ''), (['-q'], 'def pytest_pycollect_makeitem(*args): pass\n')])
def test_unresolved_selection_options_and_collectors_keep_foreign_checks(tmp_path, args, hook):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_protocol.py').write_text('def test_existing(): pass\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'retained proof')
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(), 'task_scope': {'mode': 'focused_fix'}, 'proof_sources': {'conftest.py': hook}})
    target = 'test_protocol.py::test_future'
    assert not _future_foreign_step(SimpleNamespace(project_root=tmp_path), state, {'targets': [target], 'args': args}, {target})

@pytest.mark.parametrize('control', ['environment', 'config'])
def test_collection_plugins_in_effective_options_keep_missing_nodes(tmp_path, monkeypatch, control):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_protocol.py').write_text('class ProtocolTests: pass\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'retained proof')
    sources = {}
    if control == 'environment':
        monkeypatch.setenv('PYTEST_ADDOPTS', '-p dynamic_collection')
    else:
        sources['pytest.ini'] = '[pytest]\naddopts = -p dynamic_collection\n'
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(), 'task_scope': {'mode': 'focused_fix'}, 'proof_sources': sources})
    target = 'test_protocol.py::ProtocolTests::test_future'
    assert not _future_foreign_step(SimpleNamespace(project_root=tmp_path), state, {'targets': [target]}, {target})

def test_retained_smoke_exclusion_defaults_do_not_synthesize_absent_nodes(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_protocol.py').write_text('import unittest\nclass ProtocolTests(unittest.TestCase): pass\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'retained proof')
    config = '[tool.pytest.ini_options]\naddopts = "-m \'not storage_real_smoke and not real_provider_smoke\'"\n'
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(), 'task_scope': {'mode': 'focused_fix'}, 'proof_sources': {'pyproject.toml': config}})
    target = 'test_protocol.py::ProtocolTests::test_future'
    assert _future_foreign_step(SimpleNamespace(project_root=tmp_path), state, {'targets': [target], 'args': ['-q', '--basetemp=.tmp-tests/proof']}, {target})
    assert state.verification_binding['proof_sources']['pyproject.toml'] == config
