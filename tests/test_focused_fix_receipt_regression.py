"""Focused receipt recovery must not adopt unimplemented historical tasks."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents.config import (load_project_config, save_project_config, save_task_plan,
                                save_session_state, load_session_state)
from auto_agents.authorization import authorization_policy_for_state
from auto_agents.models import VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from auto_agents.session_verification import _future_foreign_step, session_gates
from auto_agents.workflow_chain import IssueBriefBuilder
from test_session_verification_ownership import project, git
from workflow_support import configure_local_writer, parent_workflow, REAL_PROVIDER_CALL, ObservationBoundary


@pytest.mark.parametrize('source,selector,projected', [
    ('import unittest\nclass ProtocolTests(unittest.TestCase):\n    def test_existing(self): pass\n',
     'ProtocolTests::test_future', True),
    ('from unittest import TestCase as Base\nclass ProtocolTests(Base):\n    def test_existing(self): pass\n',
     'ProtocolTests::test_future', True),
    ('class ProtocolTests:\n    def test_existing(self): pass\n', 'ProtocolTests::test_future', True),
    ('import unittest\nclass ProtocolTests(unittest.TestCase):\n    def test_future(self): pass\n',
     'ProtocolTests::test_future', False),
    ('from helpers import ProtocolTests\n', 'ProtocolTests::test_future', False),
    ('class Base:\n    def test_future(self): pass\nclass ProtocolTests(Base): pass\n',
     'ProtocolTests::test_future', False),
    ('from helpers import Base\nclass ProtocolTests(Base): pass\n', 'ProtocolTests::test_future', False),
    ('class ProtocolTests:\n    test_future = lambda self: None\n', 'ProtocolTests::test_future', False),
    ('class ProtocolTests: pass\nsetattr(ProtocolTests, "test_future", lambda self: None)\n',
     'ProtocolTests::test_future', False),
    ('class ProtocolTests: pass\nregister_tests(ProtocolTests)\n', 'ProtocolTests::test_future', False),
    ('def pytest_pycollect_makeitem(*args): pass\nclass ProtocolTests: pass\n',
     'ProtocolTests::test_future', False),
    ('class ProtocolTests: pass\n', 'ProtocolTests::test_future[case]', False),
    ('from helpers import test_future\n', 'test_future', False),
    ('def test_existing(): pass\n', 'test_future', True),
])
def test_absent_foreign_nodes_require_conservative_retained_evidence(tmp_path, source, selector, projected):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_protocol.py').write_text(source)
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'retained proof')
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={
        'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(),
        'task_scope': {'mode': 'focused_fix'},
    })
    target = 'test_protocol.py::' + selector
    step = {'runner': 'pytest', 'targets': [target], 'args': ['-q', '--basetemp=.tmp-tests/proof']}
    assert _future_foreign_step(SimpleNamespace(project_root=tmp_path), state, step, {target}) is projected


@pytest.mark.parametrize('args,hook', [
    (['-k', 'future'], ''), (['-p', 'custom_plugin'], ''), (['-o', 'python_functions=case_*'], ''),
    (['-q'], 'def pytest_pycollect_makeitem(*args): pass\n'),
])
def test_unresolved_selection_options_and_collectors_keep_foreign_checks(tmp_path, args, hook):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_protocol.py').write_text('def test_existing(): pass\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'retained proof')
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={
        'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(),
        'task_scope': {'mode': 'focused_fix'}, 'proof_sources': {'conftest.py': hook},
    })
    target = 'test_protocol.py::test_future'
    assert not _future_foreign_step(SimpleNamespace(project_root=tmp_path), state,
                                   {'targets': [target], 'args': args}, {target})


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
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={
        'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(),
        'task_scope': {'mode': 'focused_fix'}, 'proof_sources': sources,
    })
    target = 'test_protocol.py::ProtocolTests::test_future'
    assert not _future_foreign_step(SimpleNamespace(project_root=tmp_path), state, {'targets': [target]}, {target})


def test_retained_smoke_exclusion_defaults_do_not_synthesize_absent_nodes(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'test_protocol.py').write_text('import unittest\nclass ProtocolTests(unittest.TestCase): pass\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'retained proof')
    config = "[tool.pytest.ini_options]\naddopts = \"-m 'not storage_real_smoke and not real_provider_smoke'\"\n"
    state = SimpleNamespace(candidate_paths={}, lineage_changed_paths=[], verification_binding={
        'contract_revision': git(tmp_path, 'rev-parse', 'HEAD').strip(),
        'task_scope': {'mode': 'focused_fix'}, 'proof_sources': {'pyproject.toml': config},
    })
    target = 'test_protocol.py::ProtocolTests::test_future'
    assert _future_foreign_step(SimpleNamespace(project_root=tmp_path), state,
                               {'targets': [target], 'args': ['-q', '--basetemp=.tmp-tests/proof']}, {target})
    assert state.verification_binding['proof_sources']['pyproject.toml'] == config


def receipt_scene(tmp_path):
    root, child = project(tmp_path)
    (root / 'settings.json').write_text('{"value":0}\n')
    (root / 'tests/test_protocol.py').write_text(
        'import unittest\nclass ProtocolTests(unittest.TestCase):\n'
        '    def test_existing(self): self.assertTrue(True)\n')
    config = load_project_config(root)
    config.gates.verification_policy_version = 5
    config.gates.steps.extend([
        VerificationStep(proof_id='future.protocol', runner='pytest',
            targets=['tests/test_protocol.py::ProtocolTests::test_future'],
            args=['-q', '--basetemp=.tmp-tests/protocol'], levels=['affected', 'release']),
        VerificationStep(proof_id='future.browser', runner='vitest',
            targets=['workbench/live-acceptance.test.ts'], levels=['affected', 'release'],
            depends_on_proofs=['future.protocol']),
        VerificationStep(proof_id='future.media', runner='pytest',
            targets=['tests/test_media.py::test_future'], args=['-q'], levels=['affected', 'release'],
            depends_on_proofs=['future.browser']),
    ])
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{
        'task_id': 'historical-development', 'status': 'pending',
        'verification_refs': [target for step in config.gates.steps[1:] for target in step.targets],
    }], 'verification_steps': [s.to_dict() for s in config.gates.steps], 'verification_policy_version': 5})
    new_test = ('import json\nfrom pathlib import Path\ndef test_new():\n'
                '    assert json.loads(Path("settings.json").read_text())["value"] == 1\n')
    configure_local_writer(root, child,
        "Path('value.py').write_text('VALUE = 1\\n')\n"
        "Path('settings.json').write_text('{\"value\":1}\\n')\n"
        f"Path('tests/test_new.py').write_text({new_test!r})\n")
    store, snapshot, handoff = parent_workflow(root, child)
    child.authorization_policy = authorization_policy_for_state(auto_approve=True).to_dict()
    handoff.payload['authorization_policy'] = deepcopy(child.authorization_policy)
    handoff.payload.pop('task_id')
    child.fix_verify_command = './.conda/bin/python -m pytest -q tests/test_owned.py::test_owned tests/test_new.py'
    handoff.payload['issue_seed'] = {'verification_scope': {'mode': 'focused_fix'},
                                   'verification_command': child.fix_verify_command}
    store.save_handoff(handoff)
    IssueBriefBuilder(root, child.session_id).materialize({
        **handoff.payload['issue_seed'], 'source_handoff_id': handoff.handoff_id})
    save_session_state(root, child)
    parent = load_session_state(root, 'parent')
    parent.authorization_policy = deepcopy(child.authorization_policy)
    parent.baseline_commands = ['./.conda/bin/python -m pytest -q tests/test_owned.py::test_owned']
    parent.baseline_git_ref = parent.baseline_head_ref = child.baseline_head_ref
    save_session_state(root, parent)
    return root, child, store, snapshot, handoff


@pytest.mark.parametrize('interrupted', [False, True])
def test_focused_receipt_and_process_resume_preserve_scope_and_candidate(tmp_path, monkeypatch, interrupted):
    root, child, _, _, _ = receipt_scene(tmp_path)
    calls = []
    def agent(self, request):
        if request.purpose.startswith('collab'):
            raise ObservationBoundary()
        calls.append(request.purpose)
        assert request.purpose == 'fix'
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', agent)
    monkeypatch.setattr(Session, '_phase_collab_loop',
                        lambda *args: (_ for _ in ()).throw(ObservationBoundary()))
    original_plan = (root / '.auto-agents/state/task_plan.json').read_bytes()
    original_config = (root / '.auto-agents/config.json').read_bytes()
    def resume():
        try:
            Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
        except ObservationBoundary:
            pass
        return load_session_state(root, child.session_id)
    if interrupted:
        with monkeypatch.context() as paused:
            paused.setattr(Session, '_run_session_persistence_action',
                           lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
            result = resume()
        assert result.status == 'paused'
        receipt = deepcopy(result.candidate_custody['receipt'])
        attempts = result.current_attempt
        result = resume()
        assert result.candidate_custody['receipt'] == receipt
        assert result.current_attempt == attempts
    else:
        result = resume()
    assert result.status == 'completed', result.resolution
    assert calls == ['fix']
    records = [e['verification'] for e in result.execution_log if e.get('action') == 'receipt_verification']
    assert records and all(r['ok'] for r in records)
    assert any(r.get('forced_release_reason') for r in records)
    assert all('future.' not in str(r.get('proof_ids', [])) for r in records)
    assert result.verification_binding['task_scope']['task_ids'] == []
    assert result.verification_binding['task_scope']['requirement_ids'] == []
    assert (root / '.auto-agents/state/task_plan.json').read_bytes() == original_plan
    assert (root / '.auto-agents/config.json').read_bytes() == original_config
    assert (root / 'settings.json').read_text() == '{"value":0}\n'
    assert (Path(result.candidate_custody['checkout']) / 'settings.json').read_text() == '{"value":1}\n'


def test_saved_engine_route_resumes_failed_receipt_after_inventory_upgrade(tmp_path, monkeypatch):
    import auto_agents.session_verification as verification
    from auto_agents.engine_fault import engine_root
    root, child, store, _, original_handoff = receipt_scene(tmp_path)
    calls = []
    def agent(self, request):
        calls.append(request.purpose)
        assert request.purpose == 'fix'
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', agent)
    monkeypatch.setattr(Session, '_phase_collab_loop',
                        lambda *args: (_ for _ in ()).throw(ObservationBoundary()))
    projection = verification._future_foreign_step
    with monkeypatch.context() as old:
        old.setattr(verification, '_PROOF_INVENTORY_VERSION', 7)
        old.setattr(verification, '_future_foreign_step', lambda session, state, step, excluded:
                    False if step.get('args') or step.get('runner') == 'vitest'
                    else projection(session, state, step, excluded))
        with pytest.raises(ObservationBoundary):
            Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
    failed = load_session_state(root, child.session_id)
    assert failed.status == 'failed'
    failure = next(e['verification'] for e in reversed(failed.execution_log)
                   if e.get('action') == 'receipt_verification')
    assert failure['failure_kind'] == 'verification_entry_unavailable'
    receipt = deepcopy(failed.candidate_custody['receipt'])
    authority = deepcopy(failed.verification_binding['task_scope'])
    parent = load_session_state(root, 'parent')
    assert parent.last_child_result_ref.endswith(original_handoff.handoff_id + '.json')
    route = {'target': 'fix', 'reason': 'Historical selector blocks delivery', 'issue_seed': {
        'target_repository': str(engine_root()), 'summary': 'Repair verification selection',
    }}
    parent.conversation.append({'role': 'agent', 'content': 'ROUTE_WORKFLOW v1: ' + json.dumps(route)})
    save_session_state(root, parent)
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    completed = load_session_state(root, child.session_id)
    assert completed.status == 'completed', completed.resolution
    assert calls == ['fix']  # The retained writer is never repeated.
    assert completed.current_attempt == failed.current_attempt
    assert completed.candidate_custody['receipt'] == receipt
    assert completed.verification_binding['task_scope'] == authority
    assert completed.verification_binding['proof_inventory_version'] == 8
    assert completed.candidate_custody['binding_migration']['receipt'] == receipt
    assert store.load_handoff(original_handoff.handoff_id).result['status'] == 'failed'
