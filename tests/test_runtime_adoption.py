"""Working-tree adoption and lifetime contracts, without paid provider calls."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from auto_agents.recovery.store import KernelStore
from auto_agents.recovery.model import KernelError
from auto_agents.recovery import runtime_lifecycle as lifecycle, runtime_manager as manager
from auto_agents.recovery.runtime_source import capture, source_identity


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                                    '-C', str(root), *args], text=True).strip()


@pytest.fixture
def installation(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.mkdir()
    git(source, 'init', '--quiet')
    (source / 'value.py').write_text('VALUE = 1\n')
    (source / 'removed.py').write_text('OLD = True\n')
    (source / '.gitignore').write_text('__pycache__/\n.env\n')
    git(source, 'add', '.'); git(source, 'commit', '--quiet', '-m', 'base')
    store = KernelStore(tmp_path / 'control')
    store.set_meta('runtime_source_root', str(source))
    monkeypatch.setattr(lifecycle, 'external_users', lambda store: [])
    yield store, source
    for (root, identity), token in list(lifecycle._produced.items()):
        if root == str(store.root):
            lifecycle.release(store, token, cleanup=False)
            lifecycle._produced.pop((root, identity))


def test_dirty_capture_matches_added_deleted_modes_without_changing_git(installation):
    store, source = installation
    (source / 'value.py').write_text('VALUE = 2\n')
    (source / 'removed.py').unlink()
    (source / 'new.py').write_text('NEW = True\n')
    (source / 'new.py').chmod(0o755)
    (source / '.env').write_text('SECRET=private\n')
    before = (git(source, 'status', '--porcelain'), (source / '.git/index').read_bytes(), git(source, 'rev-parse', 'HEAD'))
    artifact = capture(store, source)
    runtime = Path(artifact['path'])
    assert (runtime / 'value.py').read_text() == 'VALUE = 2\n'
    assert not (runtime / 'removed.py').exists()
    assert (runtime / 'new.py').stat().st_mode & 0o111
    assert not (runtime / '.env').exists()
    assert source_identity(source) == artifact['source']
    assert before == (git(source, 'status', '--porcelain'), (source / '.git/index').read_bytes(), git(source, 'rev-parse', 'HEAD'))
    git(source, 'add', '-A'); git(source, 'commit', '--quiet', '-m', 'now committed')
    assert capture(store, source)['artifact_id'] == artifact['artifact_id']
    assert len(list((store.root / 'kernel-releases/runtime-artifacts').iterdir())) == 1


def test_capture_refuses_mixed_source_and_removes_temporary_copy(installation, monkeypatch):
    store, source = installation
    from auto_agents.repair_v2 import workspace
    original = workspace.overlay
    def changing(*args):
        original(*args)
        (source / 'value.py').write_text('VALUE = 99\n')
    monkeypatch.setattr(workspace, 'overlay', changing)
    with pytest.raises(KernelError, match='changed'): capture(store, source)
    assert not list((store.root / 'runtime-staging').iterdir())


def adopt_fixture(store, source):
    artifact = capture(store, source)
    store.set_meta('active_runtime', artifact)
    store.set_meta('mode', 'active')
    lifecycle.release_produced(store, artifact)
    return artifact


def test_last_use_release_immediately_removes_superseded_runtime(installation):
    store, source = installation
    first = adopt_fixture(store, source)
    token = lifecycle.acquire(store, first, 'business')
    (source / 'value.py').write_text('VALUE = 2\n')
    second = adopt_fixture(store, source)
    assert Path(first['path']).exists()
    lifecycle.release(store, token)
    assert not Path(first['path']).exists()
    assert Path(second['path']).exists()
    with store.connect() as db:
        row = db.execute('SELECT state,freed FROM kernel_runtimes WHERE id=?', (first['artifact_id'],)).fetchone()
    assert row['state'] == 'deleted' and row['freed'] > 0


def test_current_rollback_verifier_survive_many_upgrades_but_history_does_not_pin(installation):
    store, source = installation
    first = adopt_fixture(store, source)
    store.set_meta('trusted_verifier_runtime', first)
    previous = first
    for number in range(2, 7):
        store.set_meta('previous_runtime', previous)
        (source / 'value.py').write_text(f'VALUE = {number}\n')
        current = adopt_fixture(store, source)
        store.set_meta('historical_receipt', {'old': previous})
        previous = current
    paths = list((store.root / 'kernel-releases/runtime-artifacts').iterdir())
    assert len(paths) == 3
    assert Path(first['path']).exists()
    assert Path(store.meta('previous_runtime')['path']).exists()
    assert Path(current['path']).exists()


def test_external_container_or_child_keeps_runtime_after_parent_lease_ends(installation):
    store, source = installation
    first = adopt_fixture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    second = capture(store, source)
    store.set_meta('active_runtime', second)
    result = lifecycle.maintain(store, users=[first['path'] + '/src'])
    assert Path(first['path']).exists()
    assert any('live_process' in r.get('reason', '') for r in result)
    lifecycle.maintain(store, users=[])
    assert not Path(first['path']).exists()


def test_live_kernel_task_source_scope_protects_its_original_runtime(installation):
    from auto_agents.recovery.model import Contract, Event
    store, source = installation
    first = adopt_fixture(store, source)
    store.apply('wf', 0, Event('register', 'workflow_registered', {'workflow_id': 'wf', 'goal_id': 'goal', 'project': str(source)}))
    ref = store.put({'goal': 'recover'})
    contract = Contract('goal', 'repair', 'engine_repair', ref, ref, (), ('test',),
                        (first['path'],), ref, 'preflight_recovered', ('implement', 'verify', 'review'))
    store.apply('wf', 1, Event('bind', 'task_bound', {'contract': contract.to_dict()}))
    (source / 'value.py').write_text('VALUE = 2\n')
    adopt_fixture(store, source)
    assert Path(first['path']).exists()
    report = lifecycle.maintain(store)
    assert any('task:repair' in r.get('reason', '') for r in report)


def test_deletion_failure_is_durable_and_retried_without_grace_period(installation, monkeypatch):
    store, source = installation
    first = adopt_fixture(store, source)
    from auto_agents import artifact_store
    remove = artifact_store._remove_at
    monkeypatch.setattr(artifact_store, '_remove_at', Mock(side_effect=OSError('busy')))
    store.set_meta('active_runtime', None)
    report = lifecycle.maintain(store)
    assert any(r['result'] == 'deferred' for r in report)
    with store.connect() as db:
        assert db.execute('SELECT state FROM kernel_runtimes WHERE id=?', (first['artifact_id'],)).fetchone()[0] == 'deleting'
    with pytest.raises(KernelError, match='available'): lifecycle.acquire(store, first)
    monkeypatch.setattr(artifact_store, '_remove_at', remove)
    report = lifecycle.maintain(store)
    assert any(r['result'] == 'deleted' for r in report)
    assert not list((store.root / 'runtime-trash').iterdir())


def test_new_command_adopts_dirty_source_once_and_reuses_current_content(installation, monkeypatch):
    store, source = installation
    first = adopt_fixture(store, source)
    store.set_meta('trusted_verifier_runtime', first)
    checks = Mock(return_value={'verified': True})
    monkeypatch.setattr(manager, 'oracle', checks)
    def activate(store, runtime, receipt):
        store.set_meta('previous_runtime', store.meta('active_runtime'))
        store.set_meta('active_runtime', runtime)
        return {'ok': True}
    monkeypatch.setattr(manager, 'cutover', activate)
    (source / 'value.py').write_text('VALUE = 2\n')
    with manager.adoption_lock(store): result = manager.ensure_current_runtime(store)
    assert Path(result['path']).joinpath('value.py').read_text() == 'VALUE = 2\n'
    with manager.adoption_lock(store): assert manager.ensure_current_runtime(store) == result
    assert checks.call_count == 1


def test_failed_new_source_never_silently_runs_old_version(installation, monkeypatch):
    store, source = installation
    first = adopt_fixture(store, source)
    store.set_meta('trusted_verifier_runtime', first)
    monkeypatch.setattr(manager, 'oracle', Mock(side_effect=KernelError('runtime_verification', 'bad candidate')))
    (source / 'value.py').write_text('VALUE = invalid\n')
    with manager.adoption_lock(store), pytest.raises(KernelError, match='bad candidate'):
        manager.ensure_current_runtime(store)
    assert store.meta('active_runtime') == first
    assert len(list((store.root / 'kernel-releases/runtime-artifacts').iterdir())) == 1


def test_legacy_cli_import_is_lazy_in_a_fresh_interpreter():
    script = ('import sys; from auto_agents.cli import main; '
              "assert 'auto_agents.cli_impl' not in sys.modules; "
              "assert 'auto_agents.orchestrator' not in sys.modules")
    subprocess.run([sys.executable, '-c', script], cwd='/tmp', check=True,
                   env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})


def test_real_child_executes_selected_bytes_and_preserves_arguments(installation, capfd):
    store, source = installation
    package = source / 'src/auto_agents'; package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'cli.py').write_text(
        'import json\nfrom pathlib import Path\n'
        'def main(argv):\n'
        ' print(json.dumps({"argv":argv,"file":str(Path(__file__).resolve())}))\n'
        ' return 17\n')
    active = adopt_fixture(store, source)
    arguments = ['collab', '--project', '/project with spaces', '--session', 'retained']
    assert manager.launch(store, arguments) == 17
    result = json.loads(capfd.readouterr().out)
    assert result['argv'] == arguments
    assert result['file'] == active['path'] + '/src/auto_agents/cli.py'
    with store.connect() as db: assert db.execute('SELECT count(*) FROM kernel_runtime_uses').fetchone()[0] == 0


def test_changed_source_during_verification_is_superseded_before_execution(installation, monkeypatch):
    store, source = installation
    first = adopt_fixture(store, source)
    store.set_meta('trusted_verifier_runtime', first)
    (source / 'value.py').write_text('VALUE = 2\n')
    calls, adopted = [], []
    def verify(store, runtime, action):
        calls.append(runtime)
        if len(calls) == 1: (source / 'value.py').write_text('VALUE = 3\n')
        return {'verified': True}
    def activate(store, runtime, receipt):
        adopted.append(runtime)
        store.set_meta('active_runtime', runtime)
        return {'ok': True}
    monkeypatch.setattr(manager, 'oracle', verify)
    monkeypatch.setattr(manager, 'cutover', activate)
    result = manager.ensure_current_runtime(store)
    assert len(calls) == 2 and len(adopted) == 1
    assert Path(result['path']).joinpath('value.py').read_text() == 'VALUE = 3\n'
    assert not Path(calls[0]['path']).exists()


def test_deterministic_failure_is_cached_but_explicit_retry_rechecks(installation, monkeypatch):
    store, source = installation
    first = adopt_fixture(store, source)
    store.set_meta('trusted_verifier_runtime', first)
    (source / 'value.py').write_text('VALUE = 2\n')
    check = Mock(side_effect=KernelError('runtime_verification', 'test failed', deterministic=True))
    monkeypatch.setattr(manager, 'oracle', check)
    for _ in range(2):
        with pytest.raises(KernelError): manager.ensure_current_runtime(store)
    assert check.call_count == 1
    with pytest.raises(KernelError): manager.ensure_current_runtime(store, retry=True)
    assert check.call_count == 2


def test_interrupted_cutover_restores_mode_without_replaying_business(installation):
    store, source = installation
    first = adopt_fixture(store, source)
    store.set_meta('runtime_cutover', {'owner': {'pid': 999999999, 'ticks': '0', 'boot': 'gone'},
                                     'prior': 'active', 'before': first, 'after': {'source': 'next'}})
    store.set_meta('mode', 'draining')
    manager.recover_cutover(store)
    assert store.meta('mode') == 'active'
    assert store.meta('active_runtime') == first
    assert store.meta('runtime_cutover') is None


def test_dead_process_lease_is_reaped_without_age_threshold(installation):
    store, source = installation
    first = adopt_fixture(store, source)
    token = lifecycle.acquire(store, first)
    with store.connect() as db:
        db.execute('UPDATE kernel_runtime_uses SET owner=? WHERE token=?',
                   (json.dumps({'pid': 999999999, 'ticks': '0', 'boot': 'gone'}), token))
    store.set_meta('active_runtime', None)
    lifecycle.maintain(store)
    assert not Path(first['path']).exists()


def test_inherited_process_claims_own_lease_and_handoff_releases_waiting_family(installation, monkeypatch):
    store, source = installation
    active = adopt_fixture(store, source)
    token = lifecycle.acquire(store, active, 'business')
    monkeypatch.setenv('AUTO_AGENTS_RUNTIME_USE', token)
    monkeypatch.setenv('AUTO_AGENTS_RUNTIME_ID', active['artifact_id'])
    monkeypatch.setenv('AUTO_AGENTS_RECOVERY_CONTROL', str(store.root))
    # Simulate a child process while keeping real parent identity observation.
    from auto_agents import artifact_store
    monkeypatch.setattr(artifact_store, 'alive', lambda owner: True)
    with store.connect() as db:
        owner = json.loads(db.execute('SELECT owner FROM kernel_runtime_uses WHERE token=?', (token,)).fetchone()[0])
        owner['pid'] += 1
        db.execute('UPDATE kernel_runtime_uses SET owner=? WHERE token=?', (json.dumps(owner), token))
    assert manager.inherited(store) == active
    child = os.environ['AUTO_AGENTS_RUNTIME_USE']
    assert child != token
    manager.suspend_business(store)
    with store.connect() as db:
        assert {r[0] for r in db.execute('SELECT purpose FROM kernel_runtime_uses')} == {'continuation'}
    manager.wait_business(store)


def test_accepted_self_repair_updates_dirty_source_without_touching_index(installation):
    from auto_agents.recovery.runtime_delivery import deliver
    store, source = installation
    (source / 'value.py').write_text('VALUE = 2\n')
    before = adopt_fixture(store, source)
    index = (source / '.git/index').read_bytes()
    (source / 'value.py').write_text('VALUE = 3\n')
    accepted = capture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    deliver(store, source, before['path'], accepted['path'])
    assert (source / 'value.py').read_text() == 'VALUE = 3\n'
    assert (source / '.git/index').read_bytes() == index
    assert store.meta('runtime_delivery')['status'] == 'complete'
    deliver(store, source, before['path'], accepted['path'])


def test_source_delivery_refuses_concurrent_changes(installation):
    from auto_agents.recovery.runtime_delivery import deliver
    store, source = installation
    before = adopt_fixture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    accepted = capture(store, source)
    (source / 'value.py').write_text('VALUE = 99\n')
    with pytest.raises(KernelError, match='变化') as failure:
        deliver(store, source, before['path'], accepted['path'])
    assert failure.value.details['paths'] == ['value.py']
    assert (source / 'value.py').read_text() == 'VALUE = 99\n'
    assert store.meta('runtime_delivery') is None


@pytest.mark.parametrize('already_applied', [False, True])
def test_source_delivery_preserves_unrelated_edits_and_partial_manual_merge(installation, already_applied):
    from auto_agents.recovery.runtime_delivery import deliver
    store, source = installation
    before = adopt_fixture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    (source / 'added.py').write_text('ADDED = True\n')
    accepted = capture(store, source)
    if not already_applied: (source / 'value.py').write_text('VALUE = 1\n')
    (source / 'added.py').unlink()
    (source / 'removed.py').unlink()
    (source / 'user.py').write_text('USER = True\n')
    (source / 'user.py').chmod(0o755)
    index = (source / '.git/index').read_bytes()
    delivery = deliver(store, source, before['path'], accepted['path'])
    assert delivery['status'] == 'complete'
    assert delivery['after'] == source_identity(source) != accepted['source']
    assert delivery['accepted'] == accepted['source']
    assert (source / 'value.py').read_text() == 'VALUE = 2\n'
    assert (source / 'added.py').read_text() == 'ADDED = True\n'
    assert (source / 'user.py').read_text() == 'USER = True\n'
    assert (source / 'user.py').stat().st_mode & 0o111
    assert not (source / 'removed.py').exists()
    assert (source / '.git/index').read_bytes() == index
    deliver(store, source, before['path'], accepted['path'])
    assert source_identity(source) == delivery['after']


def test_source_delivery_preflights_all_paths_before_resuming(installation, monkeypatch):
    from auto_agents.recovery import runtime_delivery
    store, source = installation
    before = adopt_fixture(store, source)
    (source / 'removed.py').write_text('OLD = False\n')
    (source / 'value.py').write_text('VALUE = 2\n')
    accepted = capture(store, source)
    (source / 'removed.py').write_text('OLD = True\n')
    (source / 'value.py').write_text('VALUE = 1\n')
    with monkeypatch.context() as patch:
        patch.setattr(runtime_delivery, 'resume', lambda store: None)
        runtime_delivery.deliver(store, source, before['path'], accepted['path'])
    (source / 'value.py').write_text('VALUE = 99\n')
    with pytest.raises(KernelError) as failure: runtime_delivery.resume(store)
    assert failure.value.details['paths'] == ['value.py']
    assert (source / 'removed.py').read_text() == 'OLD = True\n'


def test_source_delivery_refuses_unrelated_file_blocking_candidate_directory(installation):
    from auto_agents.recovery.runtime_delivery import deliver
    store, source = installation
    before = adopt_fixture(store, source)
    (source / 'new').mkdir()
    (source / 'new/value.py').write_text('VALUE = 2\n')
    accepted = capture(store, source)
    (source / 'new/value.py').unlink()
    (source / 'new').rmdir()
    (source / 'new').write_text('user file\n')
    with pytest.raises(KernelError) as failure:
        deliver(store, source, before['path'], accepted['path'])
    assert failure.value.code == 'source_delivery_conflict'
    assert failure.value.details['paths'] == ['new/value.py']
    assert (source / 'new').read_text() == 'user file\n'
    assert store.meta('runtime_delivery') is None


@pytest.mark.parametrize('fail_verification', [False, True])
def test_engine_delivery_adopts_verified_merged_snapshot(installation, monkeypatch, fail_verification):
    from auto_agents.recovery.engine import deliver_runtime
    from auto_agents.repair_v2.runtime_artifact import verify
    store, source = installation
    before = adopt_fixture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    accepted = capture(store, source)
    (source / 'value.py').write_text('VALUE = 1\n')
    (source / 'removed.py').write_text('USER = True\n')
    calls = []
    def adopt(control, path):
        runtime = capture(store, path)
        verify(runtime)
        assert control == store.root
        assert Path(path) != source
        assert runtime['source'] == source_identity(source) != accepted['source']
        assert (Path(path) / 'value.py').read_text() == 'VALUE = 2\n'
        assert (Path(path) / 'removed.py').read_text() == 'USER = True\n'
        calls.append(runtime)
        if fail_verification: raise KernelError('runtime_verification', 'verification failed')
        store.set_meta('active_runtime', runtime)
    monkeypatch.setattr(manager, 'adopt_source', adopt)
    progress = Mock()
    if fail_verification:
        with pytest.raises(KernelError, match='verification failed'):
            deliver_runtime(store, before['path'], accepted, progress)
        assert store.meta('active_runtime') == before
    else:
        assert deliver_runtime(store, before['path'], accepted, progress) == calls[0]
    assert len(calls) == 1
    assert [call.args[0] for call in progress.phase.call_args_list] == ['deliver', 'activation']


@pytest.mark.parametrize('concurrent_edit', [False, True])
def test_source_delivery_resumes_after_file_write_before_checkpoint(installation, monkeypatch, concurrent_edit):
    from auto_agents.recovery import runtime_delivery
    store, source = installation
    before = adopt_fixture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    accepted = capture(store, source)
    (source / 'value.py').write_text('VALUE = 1\n')
    if concurrent_edit: (source / 'removed.py').write_text('USER = True\n')
    original = store.set_meta
    def killed(key, value):
        if key == 'runtime_delivery' and value.get('done'): raise SystemExit('killed')
        return original(key, value)
    monkeypatch.setattr(store, 'set_meta', killed)
    with pytest.raises(SystemExit): runtime_delivery.deliver(store, source, before['path'], accepted['path'])
    monkeypatch.setattr(store, 'set_meta', original)
    runtime_delivery.deliver(store, source, before['path'], accepted['path'])
    assert (source / 'value.py').read_text() == 'VALUE = 2\n'
    if concurrent_edit: assert (source / 'removed.py').read_text() == 'USER = True\n'
    assert source_identity(source) == store.meta('runtime_delivery')['after']
    assert store.meta('runtime_delivery')['status'] == 'complete'


def test_concurrent_startups_share_one_adoption(installation, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    store, source = installation
    first = adopt_fixture(store, source)
    store.set_meta('trusted_verifier_runtime', first)
    (source / 'value.py').write_text('VALUE = 2\n')
    entered, proceed = threading.Event(), threading.Event()
    calls = []
    def verify(*args):
        calls.append(1); entered.set()
        assert proceed.wait(5)
        return {'verified': True}
    def activate(store, runtime, receipt):
        store.set_meta('active_runtime', runtime)
        return {'ok': True}
    monkeypatch.setattr(manager, 'oracle', verify)
    monkeypatch.setattr(manager, 'cutover', activate)
    def start():
        with manager.adoption_lock(store): return manager.ensure_current_runtime(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_call = pool.submit(start)
        assert entered.wait(5)
        second_call = pool.submit(start)
        proceed.set()
        assert first_call.result(timeout=10) == second_call.result(timeout=10)
    assert len(calls) == 1


def test_killed_consumer_is_reaped_by_supervisor_observer(installation):
    import time
    store, source = installation
    first = adopt_fixture(store, source)
    script = '''import json,sys,time
sys.path.insert(0,sys.argv[1])
from auto_agents.recovery.store import KernelStore
from auto_agents.recovery.runtime_lifecycle import acquire
s=KernelStore(sys.argv[2]); a=json.loads(sys.argv[3])
acquire(s,a,'business'); print('ready',flush=True)
time.sleep(60)
'''
    child = subprocess.Popen([sys.executable, '-c', script,
                              str(Path(__file__).resolve().parents[1] / 'src'), str(store.root), json.dumps(first)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        store.set_meta('active_runtime', None)
        with lifecycle.watch(store):
            lifecycle.maintain(store)
            assert Path(first['path']).exists()
            child.kill(); child.wait(timeout=5)
            until = time.monotonic() + 5
            while Path(first['path']).exists() and time.monotonic() < until: time.sleep(.05)
            assert not Path(first['path']).exists()
    finally:
        if child.poll() is None: child.kill(); child.wait(timeout=5)


def test_editable_bootstrap_hands_off_before_importing_broken_source(installation):
    store, source = installation
    package = source / 'src/auto_agents'
    recovery = package / 'recovery'; recovery.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (recovery / '__init__.py').write_text('')
    from auto_agents import bootstrap, runtime_entry
    (package / 'bootstrap.py').write_bytes(Path(bootstrap.__file__).read_bytes())
    (package / 'runtime_entry.py').write_bytes(Path(runtime_entry.__file__).read_bytes())
    (recovery / 'runtime_manager.py').write_text('import json,sys; print(json.dumps(sys.argv[2:]))\n')
    active = adopt_fixture(store, source)
    (recovery / 'authority.py').write_text('raise AssertionError("unverified source imported")\n')
    (recovery / 'model.py').write_text('invalid python !\n')
    script = ('import sys; sys.path.insert(0,' + repr(str(package.parent)) + '); '
              'from auto_agents.bootstrap import main; main(sys.argv[1:])')
    arguments = ['collab', '--session', 'retained']
    result = subprocess.run([sys.executable, '-c', script, *arguments], check=True, capture_output=True, text=True,
                            env={**os.environ, 'AUTO_AGENTS_RECOVERY_CONTROL': str(store.root)})
    assert json.loads(result.stdout) == arguments
    assert store.meta('active_runtime') == active


def test_ignored_but_tracked_source_is_preserved(installation):
    store, source = installation
    (source / '.gitignore').write_text('ignored.py\n')
    (source / 'ignored.py').write_text('TRACKED = True\n')
    git(source, 'add', '-f', 'ignored.py')
    artifact = capture(store, source)
    assert Path(artifact['path']).joinpath('ignored.py').read_text() == 'TRACKED = True\n'


def test_container_mount_of_parent_protects_nested_runtime(installation):
    store, source = installation
    active = adopt_fixture(store, source)
    store.set_meta('active_runtime', None)
    lifecycle.maintain(store, users=[{'mount': str(store.root)}])
    assert Path(active['path']).exists()
    lifecycle.maintain(store, users=[])
    assert not Path(active['path']).exists()


def test_raw_legacy_provider_output_is_not_a_runtime_reference(installation):
    store, source = installation
    active = adopt_fixture(store, source)
    store.set_meta('active_runtime', None)
    with store.connect() as db:
        db.execute('CREATE TABLE jobs(id TEXT,state TEXT,payload TEXT,result TEXT)')
        db.execute("INSERT INTO jobs VALUES('cancelled','cancelled','{}','{}')")
    root = store.root / 'jobs/cancelled'; root.mkdir(parents=True)
    (root / 'request-contract-old.output.json').write_text('Provider did not return JSON')
    lifecycle.maintain(store)
    assert not Path(active['path']).exists()


def test_explicit_storage_pin_is_honored_by_runtime_cleanup(installation):
    from auto_agents.artifact_store import ArtifactStore
    store, source = installation
    active = adopt_fixture(store, source)
    directory = Path(active['path']).parent
    registry = ArtifactStore()
    resource = registry.register(directory, kind='cache')
    registry.release(resource)
    registry.pin(resource, 'retain for reproduction')
    store.set_meta('active_runtime', None)
    result = lifecycle.maintain(store, users=lifecycle.storage_pins(store))
    assert directory.exists()
    assert any('storage_pin' in r.get('reason', '') for r in result)
    registry.pin(resource, '')
    lifecycle.maintain(store, users=lifecycle.storage_pins(store))
    assert not directory.exists()


def test_pending_source_merge_keeps_its_clean_worktree(installation):
    store, source = installation
    repository = store.root / 'engine.git'
    subprocess.run(['git', 'clone', '--quiet', '--bare', str(source), str(repository)], check=True)
    runtime = store.root / 'runtimes/source-merge-retained'
    runtime.parent.mkdir()
    git(repository, 'worktree', 'add', '--detach', str(runtime), 'HEAD')
    receipt = runtime.with_name(runtime.name + '.source.json')
    receipt.write_text(json.dumps({'pending': True}))
    lifecycle.maintain(store)
    assert runtime.exists()
    receipt.write_text(json.dumps({'pending': False}))
    lifecycle.maintain(store)
    assert not runtime.exists()


def test_new_consumer_after_scan_still_prevents_deletion(installation, monkeypatch):
    from auto_agents import artifact_store
    store, source = installation
    first = adopt_fixture(store, source)
    (source / 'value.py').write_text('VALUE = 2\n')
    second = capture(store, source)
    token = lifecycle._produced.pop((str(store.root), second['path']))
    lifecycle.release(store, token, cleanup=False)
    store.set_meta('active_runtime', None)
    original = artifact_store._remove_at
    acquired = []
    def acquire_between_deletions(fd, name, *args):
        if not acquired:
            other = second if name == first['artifact_id'] else first
            acquired.append(other)
            lifecycle.acquire(store, other, 'business')
        return original(fd, name, *args)
    monkeypatch.setattr(artifact_store, '_remove_at', acquire_between_deletions)
    lifecycle.maintain(store)
    assert Path(acquired[0]['path']).exists()
    assert sum(Path(a['path']).exists() for a in (first, second)) == 1
