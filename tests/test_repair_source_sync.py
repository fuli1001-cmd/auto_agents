"""Local/remote selection and safe delivery use real Git histories."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.repair_control import Repository, Store, atomic_json, git
from auto_agents.repair_worker import repair, publish
from test_repair_control import configuration, failure, make_remote


def commit(root, name, text):
    (root / name).write_text(text)
    git(root, 'add', name)
    git(root, 'commit', '-m', name)
    return git(root, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('relation', ['equal', 'local_ahead', 'remote_ahead', 'merged'])
def test_worker_selects_latest_histories_and_updates_installation_before_execution(tmp_path, relation):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    remote = repository.worktree(base, 'remote-change')
    if relation in {'local_ahead', 'merged'}:
        commit(engine, 'local.py', 'LOCAL = True\n')
    if relation in {'remote_ahead', 'merged'}:
        repository.push(commit(remote, 'remote.py', 'REMOTE = True\n'))
    requested = git(engine, 'rev-parse', 'HEAD')
    project = tmp_path / 'project'
    project.mkdir()
    request = {'config': config, 'job': {'id': 'selection', 'payload': {**failure(project), 'base': requested}}}
    if relation == 'local_ahead':
        request['prepared_runtime'] = {'revision': base, 'fresh': False, 'python': 'obsolete', 'environment': 'old'}
    class ReachedWorker(Exception):
        pass
    def execute(incoming, root, python):
        selected = incoming['prepared_runtime']['source_selection']
        assert selected['relation'] == relation
        assert git(engine, 'rev-parse', 'HEAD') == git(root, 'rev-parse', 'HEAD') == selected['revision']
        assert git(root, 'merge-base', '--is-ancestor', requested, 'HEAD', check=False).returncode == 0
        if relation in {'local_ahead', 'merged'}:
            assert (root / 'local.py').is_file()
        if relation in {'remote_ahead', 'merged'}:
            assert (root / 'remote.py').is_file()
        raise ReachedWorker
    with (patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'env')),
          patch('auto_agents.repair_worker.execute_selected_worker', side_effect=execute)):
        with pytest.raises(ReachedWorker):
            repair(request)
    assert not git(engine, 'status', '--porcelain')
    if relation == 'local_ahead':
        assert git(Path(config['remote']), 'rev-parse', 'master') == base  # No push prerequisite.


@pytest.mark.parametrize('change', ['staged', 'unstaged', 'untracked'])
def test_dirty_installation_blocks_before_fetch_environment_or_models(tmp_path, change):
    config = configuration(tmp_path)
    engine = make_remote(config)
    requested = git(engine, 'rev-parse', 'HEAD')
    path = engine / ('new.py' if change == 'untracked' else 'bug.py')
    path.write_text('operator work\n')
    if change == 'staged':
        git(engine, 'add', 'bug.py')
    before = git(engine, 'status', '--porcelain')
    with patch.object(Repository, 'fetch', side_effect=AssertionError('remote fetched')):
        with pytest.raises(RuntimeError, match='uncommitted changes'):
            repair({'config': config, 'job': {'id': 'dirty', 'payload': {**failure(tmp_path), 'base': requested}}})
    assert path.read_text() == 'operator work\n'
    assert git(engine, 'status', '--porcelain') == before


def test_source_conflict_is_retained_without_changing_installation_and_can_resume(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    remote = repository.worktree(base, 'remote-change')
    local = commit(engine, 'bug.py', 'local fix\n')
    upstream = commit(remote, 'bug.py', 'remote fix\n')
    repository.push(upstream)
    with pytest.raises(RuntimeError, match='resolution and a commit'):
        repository.select_source(local)
    assert git(engine, 'rev-parse', 'HEAD') == local
    assert not git(engine, 'status', '--porcelain')
    merge = next(path for path in (repository.root / 'runtimes').glob('source-merge-*') if path.is_dir())
    assert git(merge, 'diff', '--name-only', '--diff-filter=U') == 'bug.py'
    from auto_agents.artifact_references import protection
    assert protection({'path': str(merge), 'kind': 'worktree',
                       'metadata': {'repair_root': config['root']}}) == 'source_merge_pending'
    resolved = commit(merge, 'bug.py', 'local and remote fixes\n')
    assert protection({'path': str(merge), 'kind': 'worktree',
                       'metadata': {'repair_root': config['root']}}) == 'source_merge_pending'
    selection = repository.select_source(local)
    assert selection['revision'] == resolved
    repository.update_source(selection['snapshot'], resolved)
    assert (engine / 'bug.py').read_text() == 'local and remote fixes\n'
    assert not git(engine, 'merge-base', '--is-ancestor', upstream, 'HEAD', check=False).returncode


@pytest.mark.parametrize('race', ['edit', 'commit', 'branch'])
def test_source_update_never_overwrites_intervening_work(tmp_path, race):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    remote = repository.worktree(base, 'remote-change')
    repository.push(commit(remote, 'remote.py', 'remote\n'))
    selection = repository.select_source(base)
    if race == 'edit':
        (engine / 'bug.py').write_text('new edit\n')
    elif race == 'commit':
        commit(engine, 'local.py', 'new local commit\n')
    else:
        git(engine, 'switch', '-c', 'other')
    before = (git(engine, 'rev-parse', 'HEAD'), git(engine, 'status', '--porcelain'), (engine / 'bug.py').read_text())
    with pytest.raises(RuntimeError, match='engine workspace'):
        repository.update_source(selection['snapshot'], selection['revision'])
    assert (git(engine, 'rev-parse', 'HEAD'), git(engine, 'status', '--porcelain'), (engine / 'bug.py').read_text()) == before


def delivery_fixture(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    root = repository.worktree(base, 'candidate')
    fixed = commit(root, 'bug.py', "value = 'fixed'\n")
    request = {'config': config, 'job': {'id': 'delivery', 'payload': {'base': base},
        'result': {'commit': fixed, 'base': base, 'python': 'python'}}}
    return config, engine, repository, fixed, request


def test_delivery_fast_forwards_local_then_pushes_and_pins_commit(tmp_path):
    config, engine, repository, fixed, request = delivery_fixture(tmp_path)
    result = publish(request)
    assert result['ok'] and git(engine, 'rev-parse', 'HEAD') == fixed
    assert git(Path(config['remote']), 'rev-parse', 'master') == fixed
    receipt = json.loads((repository.root / 'jobs/delivery/source-delivery.json').read_text())
    assert git(repository.cache, 'rev-parse', receipt['retained_ref']) == fixed


def test_dirty_delivery_preserves_candidate_and_does_not_push(tmp_path):
    config, engine, repository, fixed, request = delivery_fixture(tmp_path)
    (engine / 'bug.py').write_text('new user edit\n')
    with patch.object(Repository, 'push', side_effect=AssertionError('published before local integration')):
        with pytest.raises(RuntimeError, match='uncommitted changes'):
            publish(request)
    assert (engine / 'bug.py').read_text() == 'new user edit\n'
    assert git(repository.cache, 'cat-file', '-t', fixed) == 'commit'


def test_local_delivery_survives_temporary_remote_unavailability(tmp_path):
    config, engine, repository, fixed, request = delivery_fixture(tmp_path)
    base = request['job']['result']['base']
    with (patch.object(Repository, 'fetch', return_value=(base, False)),
          patch.object(Repository, 'push', side_effect=AssertionError('pushed without refresh'))):
        with pytest.raises(RuntimeError, match='successful upstream refresh'):
            publish(request)
    assert git(engine, 'rev-parse', 'HEAD') == fixed
    assert (repository.root / 'jobs/delivery/source-delivery.json').is_file()


@pytest.mark.parametrize('ok', [True, False])
def test_local_commits_require_verified_merge_before_delivery(tmp_path, ok):
    config, engine, repository, fixed, request = delivery_fixture(tmp_path)
    local = commit(engine, 'local.py', 'new local work\n')
    calls = []
    def suite(root):
        assert (root / 'local.py').read_text() == 'new local work\n'
        assert 'fixed' in (root / 'bug.py').read_text()
        calls.append(root)
        return SimpleNamespace(ok=ok, summary='suite')
    oracle = SimpleNamespace(_load_or_create_experiment=lambda: (None, None),
        _diagnosis_differential=lambda *args: SimpleNamespace(ok=True, summary='behavior'),
        _replay_candidate=lambda *args: SimpleNamespace(ok=True, summary='boundary'),
        _candidate_test_weakening_reason=lambda *args: '', _run_full_suite_shards=suite)
    with patch('auto_agents.repair_worker.make_runner', return_value=oracle):
        if ok:
            result = publish(request)
            assert git(engine, 'rev-parse', 'HEAD') == result['commit']
            assert git(engine, 'merge-base', '--is-ancestor', fixed, 'HEAD', check=False).returncode == 0
            assert git(engine, 'merge-base', '--is-ancestor', local, 'HEAD', check=False).returncode == 0
        else:
            with pytest.raises(RuntimeError, match='failed verification'):
                publish(request)
            assert git(engine, 'rev-parse', 'HEAD') == local
            assert git(Path(config['remote']), 'rev-parse', 'master') == request['job']['result']['base']
    assert len(calls) == 1


def test_remote_refresh_failure_does_not_select_stale_cache(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    base, _ = repository.fetch()
    with patch.object(repository, 'fetch', return_value=(base, False)):
        with pytest.raises(RuntimeError, match='successful remote refresh'):
            repository.select_source(base)
    assert git(engine, 'rev-parse', 'HEAD') == base


def test_changed_upstream_cannot_publish_into_the_old_operator_destination(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    git(engine, 'remote', 'add', 'origin', config['remote'])
    git(engine, 'config', 'branch.master.remote', 'origin')
    git(engine, 'config', 'branch.master.merge', 'refs/heads/other')
    with pytest.raises(RuntimeError, match='upstream differs'):
        Repository(config).source_snapshot()


def test_prepared_worker_requires_the_persisted_source_selection(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    base = git(engine, 'rev-parse', 'HEAD')
    request = {'config': config, 'job': {'id': 'prepared', 'payload': {**failure(tmp_path), 'base': base}},
               'prepared_runtime': {'revision': base, 'source_selection': {'revision': base, 'requested_revision': base}}}
    with patch.object(Repository, 'select_source', side_effect=AssertionError('silently selected another version')):
        with pytest.raises(RuntimeError, match='durable receipt'):
            repair(request)


def test_delivery_rechecks_local_head_after_expensive_verification(tmp_path):
    config, engine, repository, fixed, request = delivery_fixture(tmp_path)
    commit(engine, 'local.py', 'first local change\n')
    late = []
    def suite(root):
        late.append(commit(engine, 'later.py', 'new work during verification\n'))
        return SimpleNamespace(ok=True, summary='passed before source moved')
    oracle = SimpleNamespace(_load_or_create_experiment=lambda: (None, None),
        _diagnosis_differential=lambda *args: SimpleNamespace(ok=True, summary='behavior'),
        _replay_candidate=lambda *args: SimpleNamespace(ok=True, summary='boundary'),
        _candidate_test_weakening_reason=lambda *args: '', _run_full_suite_shards=suite)
    with (patch('auto_agents.repair_worker.make_runner', return_value=oracle),
          patch.object(Repository, 'push', side_effect=AssertionError('published stale integration'))):
        with pytest.raises(RuntimeError, match='advanced during verification'):
            publish(request)
    assert git(engine, 'rev-parse', 'HEAD') == late[0]
    assert (engine / 'later.py').read_text() == 'new work during verification\n'


def test_cancelled_candidate_retained_until_successor_delivery_is_durable(tmp_path):
    import sqlite3
    from auto_agents.artifact_references import retained_repair_candidates
    config, engine, repository, fixed, request = delivery_fixture(tmp_path)
    root = repository.root
    (root / 'jobs/old/continuous/repair').mkdir(parents=True)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE jobs(id TEXT, state TEXT)')
        db.executemany('INSERT INTO jobs VALUES(?,?)', [('old', 'cancelled'), ('delivery', 'completed')])
        assert retained_repair_candidates(root, db)
        atomic_json(root / 'jobs/delivery/prior-repair-import.json',
                    {'source_job': 'old', 'source_commit': request['job']['result']['base']})
        repository.record_delivery('delivery', repository.source_snapshot(), fixed)
        assert not retained_repair_candidates(root, db)
        receipt = json.loads((root / 'jobs/delivery/source-delivery.json').read_text())
        git(repository.cache, 'update-ref', '-d', receipt['retained_ref'])
        assert retained_repair_candidates(root, db)  # A JSON flag cannot replace retained Git objects.


def test_real_runtime_handoff_uses_unpublished_local_implementation(tmp_path):
    import shutil
    import sys
    config = configuration(tmp_path)
    engine = make_remote(config)
    repo = Path(__file__).resolve().parents[1]
    shutil.copytree(repo / 'src', engine / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(repo / 'pyproject.toml', engine / 'pyproject.toml')
    git(engine, 'add', '.')
    git(engine, 'commit', '-m', 'install current local repair implementation')
    requested = git(engine, 'rev-parse', 'HEAD')
    request_path = tmp_path / 'request.json'
    project = tmp_path / 'project'
    project.mkdir()
    request = {'_request_path': str(request_path), 'config': config,
               'job': {'id': 'handoff', 'payload': {**failure(project), 'base': requested}}}
    request_path.write_text(json.dumps(request))
    class Handoff(Exception):
        pass
    def execute(python, argv, environment):
        root = Path(argv[1]).parents[2]
        assert git(root, 'rev-parse', 'HEAD') == requested
        assert environment['PYTHONPATH'] == str(root / 'src')
        saved = json.loads(request_path.read_text())
        assert saved['prepared_runtime']['source_selection']['relation'] == 'local_ahead'
        assert all(saved['runtime_compatibility']['checks'].values())
        assert (root / 'src/auto_agents/repair_planning_input.py').is_file()
        raise Handoff
    with (patch('auto_agents.repair_worker.engine_environment', return_value=(sys.executable, 'test-env')),
          patch('auto_agents.repair_worker.os.execve', side_effect=execute),
          patch('auto_agents.repair_worker.make_runner', side_effect=AssertionError('model work began'))):
        with pytest.raises(Handoff):
            repair(request)
    assert git(engine, 'rev-parse', 'HEAD') == requested
    assert git(Path(config['remote']), 'rev-parse', 'master') != requested
