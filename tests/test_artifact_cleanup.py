import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from auto_agents.artifact_cleanup import clean, _clean_docker
from auto_agents.artifact_store import ArtifactStore, DAY
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




def test_no_wsl_or_provider_call_is_needed_for_clean(store, tmp_path, monkeypatch):
    import subprocess
    _, path = artifact(store, tmp_path)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('cleanup tried to start an external process'))
    assert clean(store=store)['ok'] and not path.exists()
