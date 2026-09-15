import fcntl
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_v2.providers import AgentSandbox
from auto_agents.repair_v2.storage import (
    RESERVE_BYTES, disposable_source, execution_lease, recover_executions, require_space,
)
from auto_agents.repair_v2.types import RepairBlocked, ValidationUnit
from auto_agents.repair_v2.docker import DockerVerifier
from auto_agents.repair_v2.workspace import git, source_identity


@pytest.mark.parametrize('failure', [False, True])
def test_disposable_copy_releases_files_without_mutating_source(tmp_path, failure):
    source = tmp_path / 'snapshot'; source.mkdir()
    (source / 'test.py').write_text('original')
    target = tmp_path / 'execution/source'
    try:
        with disposable_source(source, target):
            (target / 'test.py').write_text('changed')
            if failure: raise RuntimeError('test infrastructure failed')
    except RuntimeError:
        pass
    assert not target.exists()
    assert (source / 'test.py').read_text() == 'original'


def test_no_copy_is_started_when_recovery_reserve_is_needed(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    with patch('auto_agents.repair_v2.storage.shutil.disk_usage', return_value=SimpleNamespace(free=RESERVE_BYTES - 1)):
        with pytest.raises(RepairBlocked, match='recovery reserve'):
            with disposable_source(source, tmp_path / 'target'):
                pytest.fail('copy was admitted')
    assert not (tmp_path / 'target').exists()


def test_recovery_reclaims_only_unleased_unmounted_disposable_sources(tmp_path):
    for name in ('dead', 'live', 'mounted', 'unknown'):
        base = tmp_path / 'executions' / name
        (base / 'source').mkdir(parents=True)
        (base / 'output.log').write_text('keep diagnostic evidence')
        if name != 'unknown': (base / 'lease').touch()
    with execution_lease(tmp_path / 'executions/live'):
        result = recover_executions(tmp_path, [tmp_path / 'executions/mounted/source'])
    assert result == [str(tmp_path / 'executions/dead/source')]
    assert (tmp_path / 'executions/dead/output.log').exists()
    for name in ('live', 'mounted', 'unknown'):
        assert (tmp_path / 'executions' / name / 'source').exists()


def test_provider_home_copies_only_selected_configuration_and_preserves_refresh(tmp_path):
    original = tmp_path / 'user'; (original / '.codex').mkdir(parents=True)
    (original / '.codex/auth.json').write_text('{"token":"initial"}')
    (original / '.codex/config.toml').write_text('model = "chosen"\n')
    history = original / '.copilot/profiles/deep/session-state'
    history.mkdir(parents=True); (history / 'unrelated.txt').write_text('never copy')
    sandbox = AgentSandbox(tmp_path / 'private', 'image', kind='codex')
    with patch.object(Path, 'home', return_value=original):
        home = sandbox.home('plan')
        (home / '.codex/auth.json').write_text('{"token":"refreshed"}')
        assert sandbox.home('implement') == home
    assert not (home / '.copilot').exists()
    assert json.loads((home / '.codex/auth.json').read_text())['token'] == 'refreshed'
    assert (home.stat().st_mode & 0o777) == 0o700


def test_antigravity_native_token_without_extension_is_copied(tmp_path):
    original = tmp_path / 'user'
    native = original / '.gemini/antigravity-cli'; native.mkdir(parents=True)
    (native / 'antigravity-oauth-token').write_text('private-auth')
    with patch.object(Path, 'home', return_value=original):
        home = AgentSandbox(tmp_path / 'private', 'image', kind='antigravity').home('plan')
    token = home / '.gemini/antigravity-cli/antigravity-oauth-token'
    assert token.read_text() == 'private-auth' and token.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('status', ['success', 'test_failure', 'docker_exception'])
def test_verifier_cleans_working_copy_and_preserves_result_log(tmp_path, status):
    source = tmp_path / 'snapshot'; source.mkdir(); git(source, 'init', '-q')
    (source / 'source.py').write_text('value = 1')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'input')
    verifier = DockerVerifier(tmp_path / 'verify', image='pinned')
    def fake_run(args, **kwargs):
        if args[:2] == ['docker', 'run']:
            kwargs['output'].write_text('captured test output')
            if status == 'docker_exception': raise OSError('transport lost')
            return (0 if status == 'success' else 1), 'captured test output'
        if args[:2] == ['docker', 'inspect']: return 0, '{"OOMKilled":false}'
        return 0, ''
    import threading
    with patch('auto_agents.repair_v2.docker.run', side_effect=fake_run):
        if status == 'docker_exception':
            with pytest.raises(OSError):
                verifier.execute(source_identity(source), source, ValidationUnit('test', 'true'), threading.Event())
        else:
            result = verifier.execute(source_identity(source), source, ValidationUnit('test', 'true'), threading.Event())
            assert result['ok'] == (status == 'success')
    runs = list((verifier.root / 'executions').iterdir())
    assert len(runs) == 1 and not (runs[0] / 'source').exists()
    assert (runs[0] / 'output.log').read_text() == 'captured test output'


def test_command_output_and_saved_log_are_bounded(tmp_path):
    import sys
    from auto_agents.repair_v2.docker import run, MAX_OUTPUT_BYTES
    logfile = tmp_path / 'output.log'
    code, output = run([sys.executable, '-c', 'import sys;sys.stdout.write("x" * 5000000 + "END")'], output=logfile)
    assert code == 0 and output.endswith('END')
    assert 'truncated' in output and logfile.stat().st_size < MAX_OUTPUT_BYTES + 100


def test_candidate_import_preserves_dirty_untracked_and_deleted_work(tmp_path):
    from auto_agents.repair_v2.workspace import Workspace
    source = tmp_path / 'repo'; source.mkdir(); git(source, 'init', '-q')
    (source / 'change.py').write_text('original')
    (source / 'remove.py').write_text('remove')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'baseline')
    base = git(source, 'rev-parse', 'HEAD')
    retained = tmp_path / 'retained'
    import subprocess
    subprocess.run(['git', 'clone', '-q', str(source), str(retained)], check=True)
    (retained / 'change.py').write_text('interrupted change')
    (retained / 'remove.py').unlink()
    (retained / 'new.py').write_text('untracked work')
    before = source_identity(retained)
    work = Workspace(tmp_path / 'work', source, base, retained=retained)
    candidate = work.prepare()
    assert source_identity(candidate) == before == source_identity(retained)
    assert not (candidate / 'remove.py').exists()
    assert git(candidate, 'status', '--porcelain') == ''


def test_failed_validation_does_not_dispatch_remaining_copies(tmp_path):
    import threading
    verifier = DockerVerifier(tmp_path / 'verify', image='pinned', workers=1)
    seen = []
    def execute(identity, snapshot, unit, cancel):
        seen.append(unit.identity)
        return {'unit': unit.identity, 'command': unit.command, 'ok': False,
                'excerpt': 'concrete failure', 'failed': ['test_x'], 'missing': [], 'infrastructure': False}
    with patch.object(verifier, 'execute', side_effect=execute):
        result = verifier.validate('source', tmp_path,
            [ValidationUnit(str(i), 'command ' + str(i)) for i in range(20)], threading.Event())
    assert not result.ok and seen == ['0']


def test_snapshot_retention_preserves_reproducible_commits(tmp_path):
    from auto_agents.repair_v2.workspace import Workspace
    source = tmp_path / 'repo'; source.mkdir(); git(source, 'init', '-q')
    (source / 'source.py').write_text('value = 0\n'); git(source, 'add', '.'); git(source, 'commit', '-qm', 'base')
    workspace = Workspace(tmp_path / 'work', source, git(source, 'rev-parse', 'HEAD'))
    candidate = workspace.prepare(); snapshots = []
    for value in range(5):
        (candidate / 'source.py').write_text(f'value = {value}\n')
        workspace.checkpoint(); identity, path = workspace.freeze(); snapshots.append(identity)
        workspace.collect_snapshots(identity)
    assert len([p for p in (workspace.root / 'snapshots').iterdir() if p.is_dir()]) == 2
    recovered = workspace.materialize(snapshots[0])
    assert source_identity(recovered) == snapshots[0]
    assert (recovered / 'source.py').read_text() == 'value = 0\n'


def test_initial_merge_conflict_remains_in_one_candidate_for_implementation(tmp_path):
    import subprocess
    from auto_agents.repair_v2.workspace import Workspace
    source = tmp_path / 'repo'; source.mkdir(); git(source, 'init', '-q')
    (source / 'source.py').write_text('value = 0\n'); git(source, 'add', '.'); git(source, 'commit', '-qm', 'base')
    retained = tmp_path / 'retained'; subprocess.run(['git', 'clone', '-q', str(source), str(retained)], check=True)
    (retained / 'source.py').write_text('value = 1\n'); git(retained, 'add', '.'); git(retained, 'commit', '-qm', 'partial repair')
    (source / 'source.py').write_text('value = 2\n'); git(source, 'add', '.'); git(source, 'commit', '-qm', 'upstream')
    workspace = Workspace(tmp_path / 'work', source, git(source, 'rev-parse', 'HEAD'), retained=retained)
    candidate = workspace.prepare()
    assert '<<<<<<<' in (candidate / 'source.py').read_text()
    (candidate / 'source.py').write_text('value = 3\n')
    workspace.checkpoint()
    assert not git(candidate, 'diff', '--name-only', '--diff-filter=U')
    for parent in (git(retained, 'rev-parse', 'HEAD'), git(source, 'rev-parse', 'HEAD')):
        git(candidate, 'merge-base', '--is-ancestor', parent, 'HEAD')
