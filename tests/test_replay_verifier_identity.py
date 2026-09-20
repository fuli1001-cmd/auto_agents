"""A candidate cannot supply its outer verifier or attest unavailable isolation."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from auto_agents.repair_v2 import boundary_driver, docker, integration, replay_confinement
from auto_agents.repair_v2.store import Store
from auto_agents.repair_v2.transaction import bind_controller
from auto_agents.repair_v2.workspace import git
from auto_agents.verification_sandbox import ConfinementPreflightError


def test_confinement_denial_never_becomes_a_successful_probe(monkeypatch):
    diagnostic = {'failure_kind': 'verification_confinement', 'phase': 'verification_preflight',
                  'detail': 'unshare: unshare failed: Operation not permitted'}
    @contextmanager
    def unavailable(*args, **kwargs):
        raise ConfinementPreflightError(diagnostic)
        yield
    monkeypatch.setattr('auto_agents.verification_sandbox.verification_argv', unavailable)
    observed = replay_confinement.probe()
    assert observed['ok'] is False and observed['provider_calls'] == 0
    assert observed['phase'] == 'replay_confinement' and observed['diagnostic'] == diagnostic


@pytest.mark.parametrize('failure', ['', 'denied', 'bad_json', 'no_metadata', 'wrong_phase', 'provider_call'])
def test_driver_requires_executed_confinement_evidence(monkeypatch, failure):
    observed = {'ok': True, 'phase': 'replay_confinement', 'provider_calls': 0,
                'metadata': {'ok': True}}
    code = 0
    if failure == 'denied':
        code, observed['ok'] = 1, False
        observed['diagnostic'] = {'detail': 'unshare: Operation not permitted'}
    elif failure == 'no_metadata':
        observed.pop('metadata')
    elif failure == 'wrong_phase':
        observed['phase'] = 'installation'
    elif failure == 'provider_call':
        observed['provider_calls'] = 1
    def run(argv, **kwargs):
        assert argv[-2:] == ['-I', '/opt/repair/replay_confinement.py']
        assert kwargs['timeout'] == 60
        return subprocess.CompletedProcess(argv, code, '{broken' if failure == 'bad_json' else json.dumps(observed), '')
    monkeypatch.setattr(boundary_driver.subprocess, 'run', run)
    if failure:
        with pytest.raises(boundary_driver.ReplayEnvironmentUnavailable):
            boundary_driver.check_confinement()
    else:
        assert boundary_driver.check_confinement() == observed


@pytest.mark.parametrize('mismatch', ['', 'image', 'cap_add', 'network', 'readonly_root', 'privileged', 'user', 'security_options'])
def test_actual_container_configuration_must_match_trusted_profile(mismatch):
    observed = {'image': 'sha256:fixed', 'user': f'{os.getuid()}:{os.getgid()}', 'network': 'none',
                'readonly_root': True, 'privileged': False, 'cap_add': ['SYS_ADMIN', 'SYS_PTRACE'],
                'cap_drop': [], 'security_options': ['seccomp=unconfined']}
    if mismatch:
        observed[mismatch] = {'image': 'changed', 'cap_add': [], 'network': 'host',
                              'readonly_root': False, 'privileged': True, 'user': 'another-user',
                              'security_options': []}[mismatch]
    assert docker.matches_boundary_security(observed, 'sha256:fixed') is (not mismatch)


def test_next_controller_generation_preserves_transaction_and_previous_binding(tmp_path):
    roots = [tmp_path / 'controller-a', tmp_path / 'controller-b']
    for index, root in enumerate(roots):
        root.mkdir()
        git(root, 'init', '-q')
        (root / 'verifier.py').write_text('version = ' + str(index))
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'trusted controller')
    transaction = tmp_path / 'transaction'
    store = Store(transaction)
    retained = {'attempts': 12, 'calls': 28, 'plan': 'retained', 'status': 'blocked'}
    store.save(retained)
    first = bind_controller(transaction, {'source_root': str(roots[0])}, 'job:1')
    config = {'source_root': str(roots[1])}
    assert bind_controller(transaction, config, 'job:1') == first
    second = bind_controller(transaction, config, 'job:2')
    assert second['commit'] != first['commit'] and second['root'] == str(roots[1])
    assert bind_controller(transaction, config, 'job:1') == first
    assert store.load() == retained


def test_boundary_feedback_keeps_controller_identity_separate_from_candidate(tmp_path):
    from types import SimpleNamespace
    identity = {'root': '/trusted/controller', 'commit': 'controller-commit'}
    observation = {'ok': False, 'engine_runtime': {'commit': 'candidate-commit'}}
    before = deepcopy(observation)
    verifier = SimpleNamespace(runtime='runtime', boundary=lambda *args: {
        'ok': False, 'observed': observation, 'verifier_runtime': identity, 'infrastructure': True})
    import threading
    result = integration._boundaries(verifier, tmp_path, 'snapshot', tmp_path, {}, threading.Event())
    assert result['ok'] is False and result['infrastructure'] is True
    assert result['observed'][0]['engine_runtime']['commit'] == 'candidate-commit'
    assert result['observed'][0]['verifier_runtime'] == identity
    assert observation == before
