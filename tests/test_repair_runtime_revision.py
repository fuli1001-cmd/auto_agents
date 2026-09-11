"""The controller must not replace itself with an engine missing the requested fixes."""
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.repair_client import _repair_failure_detail
from auto_agents.repair_control import Repository, Store, git
from auto_agents.repair_worker import repair
from test_repair_control import configuration, failure, make_remote


@pytest.mark.parametrize('selection', ['behind', 'diverged', 'prepared_old'])
def test_worker_rejects_missing_requested_revision_before_expensive_work(tmp_path, selection):
    config = configuration(tmp_path)
    engine = make_remote(config)
    repository = Repository(config)
    original, _ = repository.fetch()
    (engine / 'controller-fix.py').write_text('FIX = True\n')
    git(engine, 'add', '.')
    git(engine, 'commit', '-m', 'local controller fix')
    requested = git(engine, 'rev-parse', 'HEAD')
    if selection == 'diverged':
        other = repository.worktree(original, 'upstream-change')
        (other / 'other.py').write_text('OTHER = True\n')
        git(other, 'add', '.')
        git(other, 'commit', '-m', 'independent upstream change')
        repository.push(git(other, 'rev-parse', 'HEAD'))
    selected, _ = repository.fetch()
    (engine / 'unfinished.txt').write_text('operator work')
    git(engine, 'add', 'unfinished.txt')
    before = git(engine, 'diff', '--cached')
    project = tmp_path / 'project'
    project.mkdir()
    retained = Path(config['root']) / 'jobs/guard/continuous/repair'
    retained.mkdir(parents=True)
    (retained / 'candidate.txt').write_text('retained candidate')
    payload = {**failure(project), 'base': requested}
    request = {'config': config, 'job': {'id': 'guard', 'payload': payload}}
    if selection == 'prepared_old':
        request['prepared_runtime'] = {'revision': selected, 'fresh': False}
    with (patch('auto_agents.repair_worker.engine_environment', side_effect=AssertionError('environment prepared')),
          patch('auto_agents.repair_worker.execute_selected_worker', side_effect=AssertionError('old worker executed')),
          patch('auto_agents.repair_worker.make_runner', side_effect=AssertionError('planning started')),
          patch('auto_agents.root_cause.RootCauseCoordinator._copy_diagnostic_tree',
                side_effect=AssertionError('project copied'))):
        result = repair(request)
    assert result['status'] == 'runtime_revision_mismatch' and not result['ok']
    assert result['runtime_selection']['requested_revision'] == requested
    assert result['runtime_selection']['selected_revision'] == selected
    assert result['next_action']['kind'] == 'synchronize_engine'
    detail = _repair_failure_detail({'id': 'guard', 'result': result}, {})
    assert requested[:8] in detail and selected[:8] in detail and '同步远端分支' in detail
    assert (retained / 'candidate.txt').read_text() == 'retained candidate'
    assert git(engine, 'rev-parse', 'HEAD') == requested
    assert git(engine, 'diff', '--cached') == before
    with Store(config['root']).connect() as db:
        kinds = [row['kind'] for row in db.execute('SELECT kind FROM events WHERE job=?', ('guard',))]
    assert kinds == ['remote_checked', 'runtime_revision_mismatch']


def test_published_installation_reaches_runtime_preparation(tmp_path):
    config = configuration(tmp_path)
    engine = make_remote(config)
    (engine / 'controller-fix.py').write_text('FIX = True\n')
    git(engine, 'add', '.')
    git(engine, 'commit', '-m', 'controller fix')
    requested = git(engine, 'rev-parse', 'HEAD')
    git(engine, 'push', config['remote'], 'HEAD:master')
    project = tmp_path / 'project'
    project.mkdir()
    request = {'config': config, 'job': {'id': 'published',
               'payload': {**failure(project), 'base': requested}}}
    class ReachedRuntime(Exception):
        pass
    def environment(config, checkout):
        assert git(checkout, 'rev-parse', 'HEAD') == requested
        raise ReachedRuntime
    with patch('auto_agents.repair_worker.engine_environment', side_effect=environment):
        with pytest.raises(ReachedRuntime):
            repair(request)
    with Store(config['root']).connect() as db:
        kinds = [row['kind'] for row in db.execute('SELECT kind FROM events WHERE job=?', ('published',))]
    assert kinds == ['remote_checked', 'runtime_selected']
