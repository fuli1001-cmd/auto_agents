"""Partial native verification remains bound to its actual candidate and checks."""
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from auto_agents.recovery.engine import IsolatedEngineEffects
from auto_agents.recovery.model import Command
from auto_agents.repair_v2.types import Acceptance, RepairBlocked, ValidationResult, ValidationUnit
from auto_agents.repair_v2.workspace import git, source_identity
from test_recovery_kernel import scene


@pytest.fixture
def verification(scene, tmp_path, monkeypatch):
    store, contract = scene
    source = tmp_path / 'source'; source.mkdir()
    git(source, 'init', '-q')
    (source / 'value.py').write_text('VALUE = 1\n')
    (source / 'tests').mkdir()
    (source / 'tests/test_value.py').write_text('def test_value():\n    assert True\n')
    git(source, 'add', '-A'); git(source, 'commit', '-qm', 'Original tests')
    base = git(source, 'rev-parse', 'HEAD')
    contract = replace(contract, kind='engine_repair')
    effects = IsolatedEngineEffects.__new__(IsolatedEngineEffects)
    effects.store, effects.stream, effects.contract = store, 'workflow', contract
    effects.root, effects.payload = tmp_path / 'repair', {'base': base}
    effects.accepted = SimpleNamespace(acceptance=(Acceptance('original', 'Original checks', ('pytest tests',)),))
    effects.cancel, effects.progress = threading.Event(), None
    effects.source = lambda: source_identity(source)
    artifact = {'path': str(source), 'source': effects.source(), 'commit': base}
    effects._previous = lambda phase: {'artifact': artifact}
    checks = [{'unit': 'original', 'ok': True, 'command': 'pytest tests', 'passed': ['tests/test_value.py::test_value']}]
    class Verifier:
        runtime, suite_runtime, calls = 'boundary-driver-1', 'suite-runtime', 0
        def suite_units(self, *args):
            return [ValidationUnit('original', 'pytest tests')]
        def validate_suite(self, identity, *args):
            self.calls += 1
            return ValidationResult(True, identity, checks=checks)
    effects.verifier = Verifier()
    def failed_boundary(*args):
        error = RepairBlocked('recovery_unclassified', 'Retained recovery failed')
        error.failure = {'domain': 'controller', 'code': error.code}
        error.boundary_results = [{'ok': False, 'observed': {'error_code': 'kernel_inactive'}}]
        raise error
    monkeypatch.setattr('auto_agents.repair_v2.integration._boundaries', failed_boundary)
    command = Command('verify', 'workflow', contract.task_id, 'verify', effects.source(),
        contract.identity, 'b'*64, 'c'*64, 'verify', False)
    return effects, command, artifact


def test_boundary_failure_retains_suite_and_resumes_only_boundary(verification, monkeypatch):
    effects, command, _ = verification
    before = effects.store.load('workflow')['budget']
    result = effects._execute(command)
    assert result.kind.value == 'environment_blocked'
    observed = effects.store.read(result.details['result_ref'])
    assert observed['suite']['ok'] is True and observed['suite']['checks'][0]['ok'] is True
    assert observed['boundary']['cases'][0]['observed']['error_code'] == 'kernel_inactive'
    assert observed['recovery_failure']['code'] == 'recovery_unclassified'
    effects.verifier.runtime = 'boundary-driver-2'
    monkeypatch.setattr('auto_agents.repair_v2.integration._boundaries', lambda *a: {'ok': True})
    assert effects._execute(replace(command, command_id='retry')).kind.value == 'success'
    assert effects.verifier.calls == 1
    assert effects.store.load('workflow')['budget'] == before


@pytest.mark.parametrize('changed', ['source', 'contract', 'checks', 'runtime', 'corrupt'])
def test_suite_checkpoint_does_not_credit_changed_inputs(verification, monkeypatch, changed):
    effects, command, artifact = verification
    effects._execute(command)
    if changed == 'source':
        (Path(artifact['path']) / 'value.py').write_text('VALUE = 2\n')
        artifact['source'] = effects.source()
        command = replace(command, source=effects.source())
    elif changed == 'contract':
        effects.contract = replace(effects.contract, constraints=('Additional obligation',))
    elif changed == 'checks':
        effects.accepted.acceptance = (Acceptance('additional', 'Additional checks', ('pytest more',)),)
    elif changed == 'runtime':
        effects.verifier.suite_runtime = 'different-image'
    else:
        marker = next((effects.root / 'phase-proofs').glob('*.json'))
        marker.write_text('{broken')
    effects._execute(command)
    assert effects.verifier.calls == 2
