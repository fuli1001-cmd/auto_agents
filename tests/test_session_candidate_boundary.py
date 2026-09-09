"""Public fix dispatch with real provider subprocesses and filesystem denial."""
import json
import os
import shutil
from pathlib import Path
import sys

import pytest

from auto_agents.adapters.claude_code import ClaudeCodeAdapter
from auto_agents.adapters.codex import CodexAdapter
from auto_agents.config import load_project_config, save_project_config, save_session_state
from auto_agents.git_ops import head_ref
from auto_agents.models import ProviderConfig
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from test_session_verification_ownership import project, git


def _writer_project(tmp_path, monkeypatch, *, fallback=False):
    root, child = project(tmp_path)
    dependency = root / '.provider-dependency'
    dependency.mkdir()
    (dependency / 'library').write_text('dependency retained')
    (root / 'node_modules').symlink_to(dependency, target_is_directory=True)
    (root / 'checkout-link').symlink_to(root / 'foreign.py')
    with (root / '.gitignore').open('a') as stream:
        stream.write('\n.provider-dependency/\nnode_modules\n')
    # The grandchild attempts real opens/chmods. A missing path is not evidence
    # of denial: each protected input must first be readable with retained bytes.
    attacks = [str(root / 'foreign.py'), 'checkout-link',
               str(dependency / 'library'), 'node_modules/library', str(root / '.git/index')]
    attack_code = f'''from pathlib import Path
import errno, os, json
paths = {attacks!r}
results = []
for name in paths:
    p = Path(name)
    before = p.read_bytes()
    mode = p.stat().st_mode
    for action in ('write', 'chmod'):
        try:
            if action == 'write': p.write_bytes(b'foreign overwrite')
            else: p.chmod(0o777)
        except OSError as error:
            assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), (name, error)
            results.append([name, action, os.getcwd()])
        else:
            raise AssertionError('unconfined writer: ' + name + ':' + action)
    assert p.read_bytes() == before and p.stat().st_mode == mode
Path(os.environ['BOUNDARY_REPORT']).write_text(json.dumps(results))
'''
    for provider in ('codex', 'claude'):
        stub = root / (provider + '-provider')
        script = f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
sys.stdin.read()
assert os.environ['BOUND_EXECUTION_ENVIRONMENT'] == 'retained-runtime'
env = dict(os.environ, BOUNDARY_REPORT={provider + '-boundary.json'!r})
subprocess.run([sys.executable, '-c', {attack_code!r}], env=env, check=True)
for key in ('HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'TMPDIR'):
    location = Path(os.environ[key])
    assert location.is_dir()
    (location / 'provider-state').write_text('private state')
'''
        if provider == 'codex':
            script += "sys.stderr.write('rate limit exceeded; try again later\\n')\nsys.exit(1)\n"
        else:
            script += "Path('value.py').write_text('VALUE = 1\\n')\nprint(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'Repaired value.\\nCOMMIT_MESSAGE: Repair owned value'}))\n"
        stub.write_text(script)
        stub.chmod(0o755)
    config = load_project_config(root)
    config.active_provider = 'codex' if fallback else 'claude-code'
    config.providers = {
        'claude-code': ProviderConfig(kind='claude-code', binary=str(root / 'claude-provider'), profile_map={}),
    }
    if fallback:
        config.providers['codex'] = ProviderConfig(kind='codex', binary=str(root / 'codex-provider'), profile_map={})
    save_project_config(root, config)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain provider transport and linked inputs')
    child.baseline_head_ref = child.baseline_git_ref = head_ref(root)
    save_session_state(root, child)
    # Foreign staged, unstaged and untracked content arrives after the binding.
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00untracked')
    monkeypatch.setenv('BOUND_EXECUTION_ENVIRONMENT', 'retained-runtime')
    # Select the configured first provider on each resume. Provider execution
    # and filesystem denial remain real, including the subsequent fallback.
    monkeypatch.setattr(Orchestrator, '_provider_health_map', lambda self: {})
    before = _shared_image(root, dependency)
    return root, dependency, before


def _shared_image(root, dependency):
    return {str(p): (p.read_bytes(), p.stat().st_mode) for p in (
        root / 'foreign.py', root / '.git/index', root / 'foreign-note.txt', dependency / 'library')}


def _resume(root):
    orch = Orchestrator(root, user_input_fn=lambda *_a, **_k: 'y')
    return Session(orch, mode='fix', auto_approve=True).resume('owned-child')


def _resume_retained_location(root, location):
    if location == 'runtime':
        return _resume(root)
    saved = _resume(root)
    assert saved.status == 'completed', json.dumps(saved.execution_log[-1], indent=2)
    custody = saved.candidate_custody
    previous_receipt = custody['receipt']['attempt_id']
    original_checkout = Path(custody['checkout'])
    legacy = root / '.auto-agents/candidate-custody/retained/project'
    legacy.parent.mkdir(parents=True)
    shutil.move(original_checkout, legacy)
    custody['checkout'] = str(legacy)
    # Legacy custody predates external-runtime registration. Its existing
    # source and receipt admission, not a new registration, authorize resume.
    for record in (root / '.auto-agents/state/custody').glob('*.json'):
        if json.loads(record.read_text())['checkout'] == str(original_checkout):
            record.unlink()
    saved.status = 'failed'
    save_session_state(root, saved)
    identity = (legacy.stat().st_dev, legacy.stat().st_ino)
    result = _resume(root)
    assert result.status == 'completed', json.dumps(result.execution_log[-1], indent=2)
    assert result.candidate_custody['checkout'] == str(legacy)
    assert (legacy.stat().st_dev, legacy.stat().st_ino) == identity
    assert result.candidate_custody['receipt']['attempt_id'] != previous_receipt
    return result


@pytest.mark.parametrize('location', ['runtime', 'legacy'])
def test_claude_writer_cannot_write_shared_or_dependency_targets(tmp_path, monkeypatch, location):
    root, dependency, before = _writer_project(tmp_path, monkeypatch)
    result = _resume_retained_location(root, location)
    assert result.status == 'completed', json.dumps(result.execution_log[-1], indent=2)
    candidate = Path(result.candidate_custody['checkout'])
    evidence = json.loads((candidate / 'claude-boundary.json').read_text())
    assert len(evidence) == 10
    assert all(row[2] == str(candidate) for row in evidence)
    assert (candidate / 'value.py').read_text() == 'VALUE = 1\n'
    assert _shared_image(root, dependency) == before
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'


# Retained repair commands from before prose-punctuation parsing was fixed use
# this exact selector. Keep both parametrized boundary checks executable under
# that spelling without changing the canonical test or orchestrator-owned state.
globals()['test_claude_writer_cannot_write_shared_or_dependency_targets.'] = (
    test_claude_writer_cannot_write_shared_or_dependency_targets
)


@pytest.mark.parametrize('location', ['runtime', 'legacy'])
def test_claude_fallback_keeps_candidate_write_boundary(tmp_path, monkeypatch, location):
    root, dependency, before = _writer_project(tmp_path, monkeypatch, fallback=True)
    result = _resume_retained_location(root, location)
    assert result.status == 'completed', json.dumps(result.execution_log[-1], indent=2)
    candidate = Path(result.candidate_custody['checkout'])
    for provider in ('codex', 'claude'):
        evidence = json.loads((candidate / (provider + '-boundary.json')).read_text())
        assert len(evidence) == 10
        assert all(row[2] == str(candidate) for row in evidence)
    assert _shared_image(root, dependency) == before


def test_unavailable_writer_confinement_blocks_before_dispatch(tmp_path, monkeypatch):
    import auto_agents.verification_sandbox as sandbox
    root, dependency, before = _writer_project(tmp_path, monkeypatch, fallback=True)
    monkeypatch.setattr(sandbox, 'landlock_abi', lambda: -1)
    which = sandbox.shutil.which
    monkeypatch.setattr(sandbox.shutil, 'which', lambda name, *a, **k: None if name == 'unshare' else which(name, *a, **k))
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('provider dispatched without confinement')
    monkeypatch.setattr(ClaudeCodeAdapter, 'run', forbidden)
    monkeypatch.setattr(CodexAdapter, 'run', forbidden)
    result = _resume(root)
    assert result.status != 'completed'
    diagnostic = result.execution_log[-1]['diagnostic']
    assert diagnostic['retry_fix'] is False and diagnostic['session_id'] == 'owned-child'
    assert diagnostic['contract_fingerprint']
    assert 'writer confinement is unavailable' in result.execution_log[-1]['result']
    assert not calls
    assert _shared_image(root, dependency) == before
