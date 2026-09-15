import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from auto_agents.artifact_cleanup import clean, _clean_docker
from auto_agents.artifact_store import ArtifactStore, DAY
from auto_agents.repair_v2.workspace import git
from test_artifact_storage import store, artifact, age


def test_one_command_cleans_all_scopes_without_quarantining_cache(store, tmp_path, capsys):
    from auto_agents.cli import main
    resources = [artifact(store, tmp_path, kind, scope=scope) for kind, scope in
                 [('scratch', 'project:/one'), ('cache', 'repair:/two'), ('scratch', 'worker:/three')]]
    _, protected = artifact(store, tmp_path, 'recovery')
    unknown = tmp_path / 'user.tmp'; unknown.write_text('not an artifact')
    assert main(['storage', 'clean']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and result['complete']
    assert result['counts']['deleted'] == 3 and result['freed_bytes'] > 0
    assert all(not path.exists() for _, path in resources)
    assert not list(tmp_path.glob('.auto-agents-trash-*'))
    assert protected.exists() and unknown.read_text() == 'not an artifact'
    assert Path(result['report']).is_file()


def test_clean_has_no_scope_project_legacy_or_compaction_flags(store, tmp_path):
    from auto_agents.cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['storage', 'clean']).storage_action == 'clean'
    for flags in (['--scope', 'repair'], ['--project', str(tmp_path)], ['--include-legacy'], ['--compact']):
        with pytest.raises(SystemExit): parser.parse_args(['storage', 'clean', *flags])


def test_pins_references_active_users_and_retention_survive_clean(store, tmp_path):
    pinned, pinned_path = artifact(store, tmp_path, 'cache'); store.pin(pinned, 'keep')
    shared, shared_path = artifact(store, tmp_path, 'cache', reference='other-job')
    fresh, fresh_path = artifact(store, tmp_path, 'cache'); age(store, fresh, days=0)
    active = tmp_path / 'active'; active.mkdir()
    active_id = store.register(active); age(store, active_id)
    result = clean(store=store)
    assert result['ok'] and result['counts']['retained'] == 4
    assert all(p.exists() for p in [pinned_path, shared_path, fresh_path, active])
    assert 'active_process' in result['retained_reasons']


def test_clean_does_not_follow_replaced_parent_or_internal_symlink(store, tmp_path):
    root = tmp_path / 'parent'; root.mkdir()
    _, protected = artifact(store, root)
    root.rename(tmp_path / 'original')
    root.symlink_to(tmp_path / 'original', target_is_directory=True)
    _, scratch = artifact(store, tmp_path)
    user = tmp_path / 'source.py'; user.write_text('keep')
    (scratch / 'link').symlink_to(user)
    result = clean(store=store)
    assert result['ok'] and protected.exists() and not scratch.exists()
    assert user.read_text() == 'keep'


def test_clean_does_not_stop_at_legacy_thousand_item_limit(store, tmp_path, monkeypatch):
    # A cheap synthetic registry checks traversal separately from the filesystem
    # deletion tests above. Every initial ID must be considered exactly once.
    rows = [{'id': str(i), 'path': str(tmp_path / str(i)), 'scope': 'user',
             'metadata': {}, 'state': 'released'} for i in range(1103)]
    seen = []
    monkeypatch.setattr(store, 'rows', lambda *a, **k: rows)
    def delete(identity, **kwargs):
        seen.append(identity)
        return {'result': 'deleted', 'freed_bytes': 1}
    monkeypatch.setattr(store, 'clean_artifact', delete)
    monkeypatch.setattr('auto_agents.artifact_cleanup._clean_docker', lambda *a: None)
    monkeypatch.setattr('auto_agents.artifact_legacy.repair_roots', lambda *a: [])
    monkeypatch.setattr('auto_agents.artifact_cache.maintain_caches', lambda *a: [])
    monkeypatch.setattr(store, 'register', lambda *a, **k: 'report')
    monkeypatch.setattr(store, 'release', lambda *a: None)
    result = clean(store=store)
    assert result['complete'] and len(seen) == len(set(seen)) == 1103


def test_deletion_error_does_not_skip_other_eligible_files(store, tmp_path, monkeypatch):
    broken, broken_path = artifact(store, tmp_path)
    _, good = artifact(store, tmp_path)
    original = store._delete
    def fail(row, *args, **kwargs):
        if row['id'] == broken: raise PermissionError('cannot unlink this resource')
        return original(row, *args, **kwargs)
    monkeypatch.setattr(store, '_delete', fail)
    result = clean(store=store)
    assert not result['ok'] and result['counts']['error'] == 1
    assert broken_path.exists() and not good.exists()


def test_concurrent_clean_is_reported_without_deleting(store, tmp_path):
    _, path = artifact(store, tmp_path)
    with (store.root / 'cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = clean(store=store)
    assert not result['ok'] and result['reason'] == 'cleanup_already_running'
    assert path.exists()


def test_automatic_maintenance_uses_identical_eligibility_and_immediate_deletion(store, tmp_path):
    from auto_agents.artifact_runtime import maintain
    _, cache = artifact(store, tmp_path, 'cache')
    result = maintain()
    assert result['ok'] and not cache.exists()
    assert not list(tmp_path.glob('.auto-agents-trash-*'))


def test_background_explicit_default_storage_root_still_discovers_legacy_controls(tmp_path, monkeypatch):
    from auto_agents.artifact_legacy import repair_roots
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setenv('AUTO_AGENTS_STORAGE_ROOT', str(tmp_path / 'auto-agents/storage'))
    monkeypatch.delenv('AUTO_AGENTS_REPAIR_CONTROL_ROOT', raising=False)
    root = tmp_path / 'auto-agents/repair-control' / ('f' * 24); root.mkdir(parents=True)
    (root / 'operator.json').write_text(json.dumps({'root': str(root)}))
    (root / 'control.sqlite3').touch()
    assert repair_roots(ArtifactStore()) == [root]


def test_clean_reports_are_registered_for_later_cleanup(store, tmp_path):
    result = clean(store=store)
    row = next(r for r in store.rows() if r['path'] == result['report'])
    assert row['kind'] == 'log' and row['leases'] == []
    age(store, row['id'])
    following = clean(store=store)
    assert not Path(result['report']).exists()
    assert Path(following['report']).exists()


def test_interruption_keeps_the_partial_report_registered(store, tmp_path, monkeypatch):
    artifact(store, tmp_path)
    def interrupt(*args, **kwargs): raise KeyboardInterrupt()
    monkeypatch.setattr(store, 'clean_artifact', interrupt)
    with pytest.raises(KeyboardInterrupt): clean(store=store)
    reports = [r for r in store.rows() if Path(r['path']).name.startswith('cleanup-')]
    assert len(reports) == 1 and reports[0]['leases'] == []


@pytest.fixture
def legacy(store, tmp_path, monkeypatch):
    root = tmp_path / 'controls' / ('a' * 24); root.mkdir(parents=True)
    (root / 'operator.json').write_text(json.dumps({'root': str(root)}))
    with sqlite3.connect(root / 'control.sqlite3') as db:
        db.executescript('CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT,updated REAL,payload TEXT);'
                         'CREATE TABLE subscribers(job TEXT,state TEXT);CREATE TABLE outbox(job TEXT,state TEXT);')
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_ROOT', str(root.parent))
    monkeypatch.setattr('auto_agents.artifact_cleanup._clean_docker', lambda *a: None)
    def job(identity='b' * 24, state='cancelled'):
        with sqlite3.connect(root / 'control.sqlite3') as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?)', (identity, state, time.time(), '{}'))
        directory = root / 'jobs' / identity
        project = directory / 'working-evidence'; project.mkdir(parents=True)
        git(project, 'init', '-q')
        (project / '.gitignore').write_text('.next/\n')
        (project / 'source.py').write_text('SOURCE = 1\n')
        git(project, 'add', '.'); git(project, 'commit', '-qm', 'original source')
        cache = project / '.next/cache'; cache.mkdir(parents=True)
        (cache / 'bundle').write_bytes(b'generated cache' * 400)
        candidate = directory / 'continuous/repair'; candidate.mkdir(parents=True)
        (candidate / 'uncommitted.py').write_text('valuable repair')
        return directory, project, cache.parent
    return root, job


def test_legacy_build_cache_is_removed_without_candidate_or_evidence_loss(store, legacy):
    root, make_job = legacy
    directory, project, cache = make_job()
    proof = project / '.auto-agents/state'; proof.mkdir(parents=True)
    (proof / 'session.json').write_text('{"pending":true}')
    result = clean(store=store)
    assert result['ok'] and not cache.exists()
    assert (project / 'source.py').read_text() == 'SOURCE = 1\n'
    assert (directory / 'continuous/repair/uncommitted.py').read_text() == 'valuable repair'
    assert (proof / 'session.json').read_text() == '{"pending":true}'
    assert 'legacy_candidate_and_recovery_evidence' in result['retained_reasons']


def test_interrupted_legacy_cleanup_resumes_after_its_build_marker_was_deleted(store, legacy, monkeypatch):
    import shutil
    root, make_job = legacy
    _, _, cache = make_job()
    (cache / 'static').mkdir(); (cache / 'static/chunk.js').write_text('generated')
    def interrupt(*args):
        shutil.rmtree(cache / 'cache')
        raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr('auto_agents.artifact_legacy._remove_at', interrupt)
        with pytest.raises(KeyboardInterrupt): clean(store=store)
    assert cache.exists() and not (cache / 'cache').exists()
    assert clean(store=store)['ok'] and not cache.exists()
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM maintenance WHERE key LIKE 'legacy-delete:%'").fetchone()


def test_one_bad_legacy_cache_does_not_skip_other_jobs(store, legacy, monkeypatch):
    root, make_job = legacy
    _, _, bad = make_job()
    _, _, good = make_job('d' * 24)
    import auto_agents.artifact_legacy as module
    original = module.clean_build
    def fail(store, db, directory, project, path, *args):
        if path == bad: raise PermissionError('preserve inaccessible cache')
        return original(store, db, directory, project, path, *args)
    monkeypatch.setattr(module, 'clean_build', fail)
    result = clean(store=store)
    assert not result['ok'] and not result['complete']
    assert bad.exists() and not good.exists()


@pytest.mark.parametrize('protection', ['tracked', 'unignored', 'active', 'blocked', 'pin', 'symlink', 'unknown_contents'])
def test_legacy_discovery_never_grants_deletion_by_name_alone(store, legacy, protection):
    root, make_job = legacy
    directory, project, cache = make_job(state='blocked' if protection == 'blocked' else 'cancelled')
    if protection == 'tracked': git(project, 'add', '-f', '.next/cache/bundle')
    if protection == 'unignored': (project / '.gitignore').write_text('')
    if protection == 'active':
        from auto_agents.artifact_store import process_identity
        (directory / 'repair-g1-lease.json').write_text(json.dumps(process_identity()))
    if protection == 'pin':
        identity = store.register(project, kind='evidence'); store.release(identity); store.pin(identity, 'preserve exact bytes')
    if protection == 'symlink':
        saved = cache.with_name('saved-build'); cache.rename(saved); cache.symlink_to(saved, target_is_directory=True)
    if protection == 'unknown_contents': (cache / 'cache').rename(cache / 'user-files')
    clean(store=store)
    assert cache.exists()
    assert (directory / 'continuous/repair/uncommitted.py').exists()


def test_completed_job_log_is_not_pinned_by_an_unrelated_blocked_job(store, legacy):
    root, make_job = legacy
    done, _, _ = make_job(state='completed')
    active, _, _ = make_job('c' * 24, 'blocked')
    paths = []
    for directory in (done, active):
        path = directory / 'repair.log'; path.write_text('log')
        identity = store.register(path, kind='log', scope='repair:' + str(root), metadata={'repair_root': str(root)})
        store.release(identity); age(store, identity)
        paths.append(path)
    clean(store=store)
    assert not paths[0].exists() and paths[1].exists()


def test_new_subscriber_still_protects_completed_job_log(store, legacy):
    root, make_job = legacy
    directory, _, _ = make_job(state='completed')
    path = directory / 'repair.log'; path.write_text('needed log')
    identity = store.register(path, kind='log', metadata={'repair_root': str(root)})
    store.release(identity); age(store, identity)
    with sqlite3.connect(root / 'control.sqlite3') as db:
        db.execute('INSERT INTO subscribers VALUES(?,?)', (directory.name, 'validating'))
    clean(store=store)
    assert path.read_text() == 'needed log'


def test_original_engine_source_is_never_deleted_as_a_cached_runtime(store, legacy, tmp_path):
    root, _ = legacy
    source = tmp_path / 'auto-agents-source'; source.mkdir()
    path = source / 'repair_control.py'; path.write_text('actual source')
    (root / 'operator.json').write_text(json.dumps({'root': str(root), 'source_root': str(source)}))
    identity = store.register(path, kind='cache', metadata={'repair_root': str(root)})
    store.release(identity); age(store, identity)
    result = clean(store=store)
    assert path.read_text() == 'actual source'
    assert 'source_repository' in result['retained_reasons']


def test_orphan_target_is_recovered_even_after_source_was_already_deleted(tmp_path):
    from auto_agents.repair_v2.storage import recover_executions, execution_lease
    base = tmp_path / 'executions' / ('a' * 32)
    target = base / 'target'; target.mkdir(parents=True)
    (target / 'data').write_text('temporary target copy')
    (base / 'lease').touch()
    (base / 'output.log').write_text('keep the diagnosis')
    with execution_lease(base): assert recover_executions(tmp_path, []) == []
    records = []
    assert recover_executions(tmp_path, [], record=records.append) == [str(target)]
    assert records[0]['freed_bytes'] > 0 and not target.exists()
    assert (base / 'output.log').read_text() == 'keep the diagnosis'


def test_remaining_process_group_or_unreadable_birth_identity_protects_legacy_job(tmp_path, monkeypatch):
    from auto_agents.artifact_legacy import quiescent
    import subprocess
    import sys
    directory = tmp_path / 'job'; directory.mkdir()
    record = directory / 'processes.json'
    # A test runner can inherit a process group outside its PID namespace.
    # Create a visible group instead of relying on os.getpgrp() being nonzero.
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    try:
        record.write_text(json.dumps({'processes': [{'pgid': child.pid}]}))
        assert not quiescent(directory)
    finally:
        child.terminate(); child.wait(timeout=5)
    record.write_text(json.dumps({'processes': [{'pid': os.getpid(), 'ticks': 'unreadable'}]}))
    assert not quiescent(directory)


def test_reaped_mounts_are_refreshed_before_removing_orphan_copy(store, tmp_path, monkeypatch):
    import auto_agents.repair_v2.docker as docker
    root = tmp_path / 'control'; root.mkdir()
    source = root / 'v2-verification/executions' / ('e' * 32) / 'source'
    source.mkdir(parents=True); (source / 'data').write_text('orphan')
    (source.parent / 'lease').touch()
    monkeypatch.setenv('DOCKER_HOST', 'unix:///var/run/docker.sock')
    monkeypatch.setattr('auto_agents.artifact_cleanup.shutil.which', lambda _: '/usr/bin/docker')
    calls = 0
    def run(command, **kwargs):
        nonlocal calls
        if command[1] == 'ps':
            calls += 1
            return 0, 'orphan' if calls == 1 else ''
        if command[1] == 'inspect': return 0, json.dumps([{'Mounts': [{'Source': str(source)}]}])
        pytest.fail('unexpected Docker command: ' + str(command))
    monkeypatch.setattr(docker, 'run', run)
    monkeypatch.setattr('auto_agents.repair_v2.cleanup.reap_containers', lambda *a, **k: ['orphan'])
    monkeypatch.setattr('auto_agents.repair_v2.images.maintain', lambda **k: [])
    records = []
    with store.connect(True): pass
    _clean_docker(store, [root], None, float('inf'), records.append)
    assert calls == 2 and not source.exists()
    assert any(r['kind'] == 'orphan_verification_copy' and r['freed_bytes'] for r in records)


def test_no_wsl_or_provider_call_is_needed_for_clean(store, tmp_path, monkeypatch):
    import subprocess
    _, path = artifact(store, tmp_path)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('cleanup tried to start an external process'))
    assert clean(store=store)['ok'] and not path.exists()


def test_docker_cleanup_does_not_contact_remote_daemon_or_prune_volumes(store, tmp_path, monkeypatch):
    import auto_agents.repair_v2.docker as docker
    (store.root / 'v2-images').mkdir(parents=True)
    monkeypatch.setenv('DOCKER_HOST', 'ssh://some-other-machine')
    monkeypatch.setattr('auto_agents.artifact_cleanup.shutil.which', lambda _: '/usr/bin/docker')
    monkeypatch.setattr(docker, 'run', lambda *a, **k: pytest.fail('remote Docker was contacted'))
    records = []
    _clean_docker(store, [], None, float('inf'), records.append)
    assert records[0]['reason'] == 'nonlocal_or_unknown_docker_endpoint'


def test_explicit_remote_context_overrides_a_local_host_environment(store, monkeypatch):
    (store.root / 'v2-images').mkdir(parents=True)
    monkeypatch.setenv('DOCKER_HOST', 'unix:///var/run/docker.sock')
    monkeypatch.setenv('DOCKER_CONTEXT', 'remote')
    monkeypatch.setattr('auto_agents.artifact_cleanup.shutil.which', lambda _: '/usr/bin/docker')
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert command[1:3] == ['context', 'inspect']
        return 0, json.dumps('ssh://remote')
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    records = []
    _clean_docker(store, [], None, float('inf'), records.append)
    assert len(commands) == 1 and records[0]['result'] == 'retained'


def test_image_removal_failure_is_reported_and_never_prunes_global_cache(store, monkeypatch):
    from auto_agents.repair_v2 import images
    for n in range(3): images.record('sha256:' + str(n), 'owned:' + str(n))
    for path in (images.registry() / 'images').glob('*.json'):
        value = json.loads(path.read_text()); value['used'] = time.time() - (100 - int(value['image'][-1])) * DAY
        path.write_text(json.dumps(value))
    def run(command, **kwargs):
        assert not any(word in command for word in ('volume', 'prune', 'build'))
        if command[1:3] == ['image', 'inspect']:
            return 0, json.dumps([{'Config': {'Labels': {'org.auto-agents.registry': images.owner()}}}])
        if command[1:3] == ['image', 'rm']: return 1, 'removal failed'
        return 0, ''
    monkeypatch.setattr('auto_agents.repair_v2.docker.run', run)
    monkeypatch.setattr(images.shutil, 'which', lambda _: '/usr/bin/docker')
    records = []
    assert images.maintain(record=records.append) == []
    assert any(r['result'] == 'error' and r['reason'] == 'image_removal_failed' for r in records)
