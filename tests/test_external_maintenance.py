"""Behavioral tests for optional maintenance and project-owned state."""
from pathlib import Path
from types import SimpleNamespace
import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import pytest
from auto_agents.business_state import BusinessStore, BusinessStateError, bind_model, read_projection, save_model, migrate
from auto_agents.business_calls import provider
from auto_agents.models import AgentRequest, AgentResult, ExecutionConfig
from auto_agents.supervision_api import Observer, milestone, status, resume_check
WATCH = Path(__file__).resolve().parents[1] / 'supervisor/src'
sys.path.insert(0, str(WATCH))
from auto_agents_watch.store import Store
from auto_agents_watch.providers import selection, arguments
from auto_agents_watch.process import cycle, run
from auto_agents_watch.git_delivery import git, commit, publish

def test_completed_supervisor_requests_core_cleanup_without_inherited_lock(tmp_path, monkeypatch):
    import fcntl
    from auto_agents_watch.runner import Runner
    from auto_agents_watch import runner as module
    watch = Store(tmp_path / 'watch')
    project = tmp_path / 'project'
    engine = tmp_path / 'engine'
    engine.mkdir()
    job = watch.create([sys.executable, '-m', 'auto_agents', 'collab', '--project', str(project)], project, engine)
    runner = Runner(watch)
    runner.directory = watch.root / 'jobs' / job['id']
    with runner.locked(job):
        pass
    monkeypatch.delenv('AUTO_AGENTS_STORAGE_MAINTENANCE', raising=False)

    def execute(argv, **kwargs):
        assert argv[-4:] == ['storage', 'maintain', '--project', str(project)]
        assert 'AUTO_AGENTS_RUN_LOCK_FD' not in kwargs['env']
        with (project / '.auto-agents/state/run.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return SimpleNamespace(stdout='{"ok":true}', returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', execute)
    runner.clean_business(job)
    assert watch.get(job['id'])['business_cleanup']['ok']

@pytest.mark.parametrize('pending,dirty', [(False, False), (True, False), (False, True)])
def test_completed_supervisor_reclaims_inputs_only_after_publication_and_durable_code(tmp_path, pending, dirty):
    from auto_agents_watch.cleanup import completed_job
    watch = Store(tmp_path / 'watch')
    job = watch.create([], tmp_path / 'project', tmp_path / 'engine')
    directory = watch.root / 'jobs' / job['id']
    candidate = directory / 'candidate'
    candidate.mkdir()
    git(candidate, 'init', '-q')
    (candidate / 'code.py').write_text('original')
    revision = commit(candidate, 'accepted')
    (directory / 'project').mkdir()
    (directory / 'project/input').write_text('checkpoint')
    (directory / 'verification').mkdir()
    (directory / 'verification/report.json').write_text('{"ok":true}')
    watch.save(job, 'DONE', candidate=str(candidate), candidate_revision=revision, publication={'state': 'pending' if pending else 'published'})
    if dirty:
        (candidate / 'code.py').write_text('uncommitted user change')
    result = completed_job(watch, job)
    if pending or dirty:
        assert result['state'] == 'retained' and candidate.exists() and (directory / 'project').exists()
    else:
        assert result['state'] == 'released' and (not candidate.exists()) and (not (directory / 'project').exists())
        restored = tmp_path / 'restored'
        git(tmp_path, 'clone', str(directory / 'candidate.bundle'), str(restored))
        assert git(restored, 'rev-parse', 'HEAD') == revision
        assert (restored / 'code.py').read_text() == 'original'
    assert (directory / 'verification/report.json').exists()

@pytest.mark.parametrize('crashed', [False, True])
def test_offline_check_reclaims_its_copy_and_preserves_input_and_report(tmp_path, crashed):
    from auto_agents_watch.verification import Verifier
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    (snapshot / 'input').write_text('original')
    root = tmp_path / 'verification'
    root.mkdir()
    copies = []

    class Sandbox:

        def verify(self, argv, candidate, evidence, result, log, *, project):
            copies.append(project)
            assert (project / 'input').read_text() == 'original'
            (project / 'input').write_text('private verification change')
            log.write_text(json.dumps({'ok': False, 'blocked_step_cleared': False, 'external_calls': 0, 'category': 'engine', 'type': 'KeyError'}) + '\n')
            if crashed:
                raise RuntimeError('container failure')
            return {'ok': False, 'returncode': 3, 'reason': ''}
    verifier = object.__new__(Verifier)
    verifier.root = root
    verifier.sandbox = Sandbox()
    for attempt in range(3):
        if crashed:
            with pytest.raises(RuntimeError, match='container failure'):
                verifier.recover(tmp_path, snapshot, {'project': '/original'})
        else:
            assert verifier.recover(tmp_path, snapshot, {'project': '/original'})['status'] == 'failed'
        assert not list(root.glob('recovery-*'))
        assert (snapshot / 'input').read_text() == 'original'
        assert json.loads((root / 'recovery.log').read_text())['type'] == 'KeyError'
    assert len(set(copies)) == 3

def test_business_state_is_local_and_rejects_stale_writes(tmp_path):
    store = BusinessStore(tmp_path)
    value = {'session_id': 's', 'mode': 'collab', 'status': 'paused', 'goal': 'original'}
    reference = store.save('sessions/s/session_state.json', value)
    first = store.get('sessions/s/session_state.json')
    second = store.get('sessions/s/session_state.json')
    store.save('sessions/s/session_state.json', {**first, 'status': 'executing'}, first.reference)
    with pytest.raises(BusinessStateError, match='changed'):
        store.save('sessions/s/session_state.json', {**second, 'goal': 'replacement'}, second.reference)
    assert store.path == tmp_path / '.auto-agents/state/business.sqlite3'
    assert store.get('sessions/s/session_state.json')['goal'] == 'original'

def test_provider_receipt_prevents_duplicate_dispatch(tmp_path):
    owner = SimpleNamespace(project_root=tmp_path)
    request = AgentRequest('implement', 'deep', 'original task', tmp_path, tmp_path / 'output', purpose='fix', attempt_id='attempt-1', usage_context={'workflow_kind': 'fix', 'subject_id': 's'})
    calls = []

    def execute(request):
        calls.append(request)
        return AgentResult(True, ['fake'], request.output_path, summary='done')
    assert provider(owner, request, execute).ok
    assert provider(owner, request, execute).ok
    assert len(calls) == 1

def test_unconfirmed_external_call_blocks_other_calls(tmp_path):
    store = BusinessStore(tmp_path)
    store.reserve('original', 's', 'implement', model=True)
    with pytest.raises(BusinessStateError, match='Reconcile'):
        store.reserve('replacement', 's', 'implement', model=True)
    assert len(store.pending()) == 1

def test_supervision_config_preserves_off_and_effort_defaults():
    config = ExecutionConfig.from_dict({'self_repair_diagnosis': {'mode': 'off'}})
    assert config.supervision.mode == 'off'
    assert 'health_watch' not in config.to_dict()
    assert 'self_repair_diagnosis' not in config.to_dict()

def test_repair_image_uses_configured_local_base_and_invalidates_on_identity_change(tmp_path, monkeypatch):
    from auto_agents_watch import sandbox as module
    binary = tmp_path / 'native-cli'
    binary.write_text('local executable')
    monkeypatch.setattr(module.shutil, 'which', lambda *_args, **_kwargs: str(binary))
    monkeypatch.delenv('AUTO_AGENTS_WATCH_BASE_IMAGE', raising=False)
    identity = ['sha256:first']
    builds = []

    def run(argv, **kwargs):
        if argv[:3] == ['docker', 'image', 'inspect']:
            return SimpleNamespace(returncode=0 if '--format' in argv else 1, stdout=identity[0])
        assert argv[:3] == ['docker', 'build', '--pull=false']
        recipe = (Path(argv[-1]) / 'Dockerfile').read_text()
        assert recipe.startswith('FROM mirror.example/node:22-bookworm\n')
        builds.append(argv[argv.index('-t') + 1])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    docker = module.Docker(tmp_path / 'sandbox', provider={'kind': 'codex', 'binary': 'native-cli'}, base_image='mirror.example/node:22-bookworm')
    first = docker.build()
    identity[0] = 'sha256:second'
    second = docker.build()
    assert first != second and builds == [first, second]
    configured = ExecutionConfig.from_dict({'supervision': {'base_image': 'mirror.example/node:22-bookworm'}})
    assert configured.to_dict()['supervision']['base_image'] == 'mirror.example/node:22-bookworm'

def test_repair_image_build_failure_reports_local_log_and_registry_error(tmp_path, monkeypatch):
    from auto_agents_watch import sandbox as module
    binary = tmp_path / 'native-cli'
    binary.write_text('local executable')
    monkeypatch.setattr(module.shutil, 'which', lambda *_args, **_kwargs: str(binary))

    def run(argv, **kwargs):
        if argv[:3] == ['docker', 'image', 'inspect']:
            return SimpleNamespace(returncode=1, stdout='')
        kwargs['stdout'].write('registry manifest request failed: 503 Service Unavailable\n')
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(module.subprocess, 'run', run)
    root = tmp_path / 'sandbox'
    with pytest.raises(RuntimeError) as failure:
        module.Docker(root, provider={'kind': 'codex', 'binary': 'native-cli'}).build()
    assert str(root / 'image-build.log') in str(failure.value)
    assert '503 Service Unavailable' in str(failure.value)
    assert not [path for path in root.glob('image-*') if path.is_dir()]

@pytest.mark.parametrize('kind', ['codex', 'claude-code'])
def test_provider_uses_existing_alias_profile_and_effort(kind):
    config = {'active_provider': 'fallback', 'providers': {'selected': {'kind': kind, 'binary': 'custom-cli', 'profile_map': {'deep': 'writer-profile', 'max': 'review-profile'}, 'extra_args': ['--custom']}, 'fallback': {'kind': 'codex'}}}
    name, selected = selection(config, ['collab', '--provider', 'selected'])
    assert name == 'selected'
    assert arguments(selected, 'implement', 'deep')[0] == 'custom-cli'
    assert 'writer-profile' in arguments(selected, 'implement', 'deep')
    assert 'review-profile' in arguments(selected, 'review', 'max')

def test_copilot_effort_selects_native_profile_instead_of_model_name(tmp_path, monkeypatch):
    monkeypatch.setenv('COPILOT_HOME', str(tmp_path))
    profile = tmp_path / 'profiles/writer'
    profile.mkdir(parents=True)
    (profile / 'config.json').write_text('{"model":"base"}')
    (profile / 'settings.json').write_text('{"model":"selected"}')
    config = {'kind': 'copilot-cli', 'profile_map': {'deep': 'writer'}}
    command = arguments(config, 'implement', 'deep')
    assert command[command.index('--model') + 1] == 'selected'
    assert command[command.index('--config-dir') + 1] == '/home/agent/.copilot/selected-profile'
    assert 'writer' not in command
    with pytest.raises(ValueError, match='unavailable'):
        arguments({**config, 'profile_map': {'deep': 'missing'}}, 'implement', 'deep')

def test_cancel_between_read_and_dispatch_refuses_reservation(tmp_path):
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    store.save(store.get(job['id']), cancel_requested=True)
    with pytest.raises(RuntimeError, match='cancelled'):
        store.reserve(job, 'implement')
    assert store.get(job['id'])['model_calls'] == 0

def test_duplicate_settlement_cannot_overwrite_another_call(tmp_path):
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    store.reserve(job, 'implement')
    stale = dict(job)
    store.settle(job, {'ok': True})
    store.reserve(job, 'review')
    with pytest.raises(RuntimeError, match='changed'):
        store.settle(stale, {'ok': False})
    assert store.get(job['id'])['active_call']['role'] == 'review'

def test_publication_refuses_unconfirmed_call_before_preparing_tools(tmp_path):
    from auto_agents_watch.runner import retry_publication
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    store.reserve(job, 'review')
    with pytest.raises(RuntimeError, match='quiescent'):
        retry_publication(store, job['id'])

def test_expired_maintenance_does_not_start_docker(tmp_path, monkeypatch):
    from auto_agents_watch import sandbox
    docker = sandbox.Docker(tmp_path)
    docker.deadline = 1
    monkeypatch.setattr(sandbox.shutil, 'which', lambda command: '/usr/bin/docker')
    monkeypatch.setattr(sandbox.subprocess, 'run', lambda *a, **kw: pytest.fail('Expired job dispatched Docker'))
    with pytest.raises(RuntimeError, match='duration'):
        docker.prepare()

def test_progress_uses_best_result_without_flip_credit(tmp_path):
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    check = lambda passed: [{'id': i, 'status': 'passed' if i in passed else 'failed'} for i in ['a', 'b', 'c']]
    assert store.progress(job, check({'a'})) == (True, True)
    assert store.progress(job, check({'b'})) == (False, True)
    assert store.progress(job, check({'a'})) == (False, False)
    assert store.get(job['id'])['best_passed'] == ['a']

def test_unknown_checks_do_not_spend_no_progress(tmp_path):
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    with pytest.raises(RuntimeError, match='unresolved'):
        store.progress(job, [{'id': 'a', 'status': 'not_run'}])
    assert store.get(job['id'])['no_progress'] == 0

def test_model_reservation_survives_process_restart(tmp_path):
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    store.reserve(job, 'implement')
    restarted = Store(tmp_path / 'watch')
    with pytest.raises(RuntimeError, match='Reconcile'):
        restarted.reserve(restarted.get(job['id']), 'implement')
    assert restarted.get(job['id'])['model_calls'] == 1

def test_cycle_requires_repetition_without_milestones():
    steps = [{'step_id': s, 'progress_seq': 0} for s in ['A', 'B', 'A', 'B', 'A', 'B']]
    assert cycle({'steps': steps, 'progress_seq': 0})
    assert not cycle({'steps': steps, 'progress_seq': 1})
    assert not cycle({'steps': steps, 'progress_seq': 0, 'waiting_for': 'user'})

def test_actual_child_timeout_preserves_log_and_stops_process(tmp_path):
    result = run([sys.executable, '-c', 'import time; print("started",flush=True); time.sleep(30)'], cwd=tmp_path, env=os.environ, log=tmp_path / 'process.log', timeout=0.3)
    assert result['reason'] == 'operation_timeout'
    assert result['returncode'] != 0
    assert 'started' in (tmp_path / 'process.log').read_text()

def test_supervisor_never_imports_business_modules():
    for path in WATCH.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith('auto_agents.')
            elif isinstance(node, ast.Import):
                assert all((not alias.name.startswith('auto_agents.') for alias in node.names))

def fixture_repository(tmp_path):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    first = tmp_path / 'a'
    subprocess.run(['git', 'clone', str(remote), str(first)], check=True, capture_output=True)
    git(first, 'config', 'user.name', 'test')
    git(first, 'config', 'user.email', 'test@localhost')
    (first / 'one').write_text('base\n')
    (first / 'two').write_text('base\n')
    commit(first, 'base')
    branch = git(first, 'branch', '--show-current')
    git(first, 'push', 'origin', branch)
    second = tmp_path / 'b'
    subprocess.run(['git', 'clone', str(remote), str(second)], check=True, capture_output=True)
    git(second, 'config', 'user.name', 'test')
    git(second, 'config', 'user.email', 'test@localhost')
    return (remote, first, second, 'refs/heads/' + branch)

def test_two_computers_merge_then_revalidate(tmp_path):
    remote, a, b, ref = fixture_repository(tmp_path)
    (a / 'one').write_text('computer A\n')
    commit(a, 'A')
    git(a, 'push', 'origin', ref)
    (b / 'two').write_text('computer B\n')
    commit(b, 'B')
    seen = []

    def verify():
        seen.append(git(b, 'rev-parse', 'HEAD'))
        assert (b / 'one').read_text() == 'computer A\n'
        assert (b / 'two').read_text() == 'computer B\n'
        return {'ok': True}
    result = publish(b, str(remote), ref, verify)
    assert result['state'] == 'published'
    assert len(seen) == 1

def test_content_conflict_retains_both_revisions(tmp_path):
    remote, a, b, ref = fixture_repository(tmp_path)
    (a / 'one').write_text('A\n')
    ra = commit(a, 'A')
    git(a, 'push', 'origin', ref)
    (b / 'one').write_text('B\n')
    rb = commit(b, 'B')
    result = publish(b, str(remote), ref, lambda: pytest.fail('Conflicted candidate must not be verified'))
    assert result['state'] == 'conflict' and result['paths'] == ['one']
    assert git(b, 'rev-parse', 'HEAD') == rb
    assert git(remote, 'rev-parse', ref) == ra

def test_clean_merge_with_regression_is_not_published(tmp_path):
    remote, a, b, ref = fixture_repository(tmp_path)
    (a / 'one').write_text('A\n')
    ra = commit(a, 'A')
    git(a, 'push', 'origin', ref)
    (b / 'two').write_text('B\n')
    commit(b, 'B')
    result = publish(b, str(remote), ref, lambda: {'ok': False})
    assert result['state'] == 'pending'
    assert git(remote, 'rev-parse', ref) == ra

def test_settlement_and_cancel_are_durable(tmp_path):
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    store.reserve(job, 'implement')
    cancellation = store.get(job['id'])
    store.save(cancellation, cancel_requested=True)
    store.settle(job, {'ok': True, 'text': 'completed'})
    saved = store.get(job['id'])
    assert saved['active_call'] is None and saved['cancel_requested']
    assert saved['model_calls'] == 1

def test_local_migration_preserves_cancelled_and_unknown_calls(tmp_path):
    project = tmp_path / 'project'
    state = project / '.auto-agents/state'
    state.mkdir(parents=True)
    control = tmp_path / 'legacy'
    control.mkdir()
    value = {'session_id': 'original', 'mode': 'collab', 'status': 'blocked', 'goal': 'approved goal', 'current_attempt': 28, 'authorization_policy': {'scope': 'original'}}
    raw = json.dumps(value, sort_keys=True).encode()
    identity = hashlib.sha256(raw).hexdigest()
    folder = control / 'kernel-objects' / identity[:2]
    folder.mkdir(parents=True)
    (folder / identity).write_bytes(raw)
    snapshot = {'project': str(project), 'projections': {'session:original': {'blob': identity}}, 'tasks': {'business': {'contract': {'kind': 'collab', 'parent_task': 'original'}}}, 'commands': {'cancelled': {'operation_key': 'cancelled', 'task_id': 'business', 'model_call': True, 'phase': 'route', 'status': 'finished', 'outcome': {'kind': 'cancelled', 'reason': 'User cancelled', 'details': {}}}, 'unknown': {'operation_key': 'unknown', 'task_id': 'business', 'model_call': True, 'phase': 'route', 'status': 'unknown'}}}
    with sqlite3.connect(control / 'control.sqlite3') as db:
        db.executescript('CREATE TABLE kernel_streams(id TEXT,snapshot TEXT);CREATE TABLE kernel_bindings(project TEXT,stream TEXT);CREATE TABLE kernel_project_heads(project TEXT,kind TEXT,name TEXT);')
        db.execute('INSERT INTO kernel_streams VALUES(?,?)', ('stream', json.dumps(snapshot)))
        db.execute('INSERT INTO kernel_bindings VALUES(?,?)', (str(project), 'stream'))
    marker = state / 'recovery-kernel.json'
    marker.write_text(json.dumps({'project': str(project), 'control_root': str(control)}))
    before = (control / 'control.sqlite3').read_bytes()
    report = migrate(project, check=True)
    assert report['pending_calls'] == 1 and marker.exists()
    assert (control / 'control.sqlite3').read_bytes() == before
    report = migrate(project)
    assert report['status'] == 'migrated' and (not marker.exists())
    assert BusinessStore(project).get('sessions/original/session_state.json') == value
    pending = BusinessStore(project).pending()
    assert len(pending) == 1 and pending[0]['id'] == 'unknown'
    assert (Path(report['backup']) / 'control.sqlite3').exists()

def test_business_record_corruption_is_rejected(tmp_path):
    store = BusinessStore(tmp_path)
    store.save('sessions/s/session_state.json', {'goal': 'original'})
    with store.connect() as db:
        db.execute("UPDATE records SET payload='{}'")
    with pytest.raises(BusinessStateError, match='checksum'):
        store.get('sessions/s/session_state.json')

def test_supervisor_runs_complete_business_without_model(tmp_path):
    from auto_agents_watch.runner import Runner
    project = tmp_path / 'project'
    project.mkdir()
    script = tmp_path / 'business.py'
    script.write_text('import os,json\nfrom pathlib import Path\nPath(os.environ["AUTO_AGENTS_OBSERVATION_FILE"]).write_text(json.dumps({"status":"completed"}))\n')
    runner = Runner(Store(tmp_path / 'watch'))
    result = runner.start([sys.executable, str(script), '--project', str(project)], tmp_path)
    assert result['state'] == 'DONE' and result['model_calls'] == 0
    assert runner.resume(result['id'])['state'] == 'DONE'

def test_unconfirmed_model_never_restarts_on_resume(tmp_path):
    from auto_agents_watch.runner import Runner
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path, tmp_path)
    store.reserve(job, 'implement')
    with pytest.raises(RuntimeError, match='Unconfirmed'):
        Runner(store).resume(job['id'])
    assert store.get(job['id'])['model_calls'] == 1

@pytest.mark.parametrize('category', ['verification', 'state', 'reconciliation'])
def test_resume_does_not_reclassify_non_engine_fault_as_maintenance(tmp_path, monkeypatch, category):
    from auto_agents_watch.runner import Runner
    watch = Store(tmp_path / 'watch')
    job = watch.create([], tmp_path / 'project', tmp_path / 'engine')
    watch.save(job, 'STOPPED', fault={'category': category, 'message': 'requires operator recovery'})
    runner = Runner(watch)
    monkeypatch.setattr(runner, 'maintain', lambda *_: pytest.fail('Non-engine fault triggered model repair'))
    monkeypatch.setattr(runner, 'business', lambda *_: pytest.fail('Implicit resume repeated business execution'))
    assert runner.resume(job['id'])['state'] == 'STOPPED'
    assert watch.get(job['id'])['model_calls'] == 0

@pytest.mark.parametrize('changed_checkpoint', [False, True])
def test_business_recheck_pairs_new_checkpoint_with_fresh_inputs_without_refilling_budget(tmp_path, monkeypatch, changed_checkpoint):
    from auto_agents_watch.runner import Runner
    from auto_agents_watch import runner as module
    watch = Store(tmp_path / 'watch')
    project = tmp_path / 'project'
    project.mkdir()
    job = watch.create(['business', '--project', str(project)], project, tmp_path / 'engine')
    runner = Runner(watch)
    runner.directory = watch.root / 'jobs' / job['id']
    runner.fd = 0
    runner.token = 'test-token'
    old = runner.directory / 'candidate'
    old.mkdir()
    (old / 'preserved-edit').write_text('old evidence')
    token = {'schema': 1, 'state_identity': 'old', 'step_id': 'parent:resume'}
    current = {**token, 'state_identity': 'new'} if changed_checkpoint else token
    checkpoint = runner.directory / 'new-token.json'
    checkpoint.write_text(json.dumps(current))
    watch.save(job, 'STOPPED', fault=None, candidate=str(old), snapshot=str(runner.directory / 'project'), resume_token=token, base='original-base', model_calls=7, attempts=3, no_progress=1, best_passed=['original-check'], original_reproduction_confirmed=True, original_reproduction={'status': 'passed'}, needs_verification=True, existing_source_clears_fault=True)

    def execute(*args, **kwargs):
        kwargs['observation'].write_text(json.dumps({'fault': {'category': 'engine', 'type': 'EngineFault', 'message': 'current defect', 'resume_token': str(checkpoint)}}))
        return {'reason': '', 'returncode': 3}
    monkeypatch.setattr(module, 'run', execute)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert runner.business(job)
    result = watch.get(job['id'])
    assert (result['model_calls'], result['attempts'], result['no_progress']) == (7, 3, 1)
    assert (old / 'preserved-edit').read_text() == 'old evidence'
    assert result['resume_token'] == current and result['best_passed'] == ['original-check']
    if changed_checkpoint:
        assert result['episode'] == 1 and result['candidate'] is None and (result['snapshot'] is None)
        assert result['original_reproduction_confirmed'] is False and result['original_reproduction'] is None
        assert result['needs_verification'] is False and result['existing_source_clears_fault'] is False
        assert result['history'][0]['candidate'] == str(old) and result['history'][0]['resume_token'] == token
    else:
        assert result['candidate'] == str(old) and 'episode' not in result
        assert result['original_reproduction_confirmed'] is True

def test_explicit_business_recheck_keeps_fault_history_and_spent_budget(tmp_path, monkeypatch):
    from auto_agents_watch.runner import Runner
    watch = Store(tmp_path / 'watch')
    job = watch.create([], tmp_path / 'project', tmp_path / 'engine')
    original = {'category': 'verification', 'message': 'missing prerequisite'}
    watch.save(job, 'STOPPED', fault=original, model_calls=2, attempts=1, no_progress=1, needs_maintenance=True)
    runner = Runner(watch)
    monkeypatch.setattr(runner, 'maintain', lambda *_: pytest.fail('Explicit business recheck ran maintenance'))

    def business(retained):
        assert runner.env(retained)['AUTO_AGENTS_MAINTENANCE_RESUME'] == '1'
        watch.save(retained, 'STOPPED', reason='business rechecked')
        return False
    monkeypatch.setattr(runner, 'business', business)
    result = runner.resume(job['id'], retry_business=True)
    assert result['retained_faults'][-1]['fault'] == original
    assert (result['model_calls'], result['attempts'], result['no_progress']) == (2, 1, 1)
    assert result['needs_maintenance'] is False

def test_business_recheck_without_original_selector_resumes_checkpoint_root(tmp_path, monkeypatch):
    from auto_agents_watch.runner import Runner
    from auto_agents_watch import runner as module
    watch=Store(tmp_path/'watch')
    argv=[sys.executable,'-m','auto_agents','collab','--project',str(tmp_path),'--provider','replacement']
    job=watch.create(argv,tmp_path,tmp_path)
    token={'schema':2,'project':str(tmp_path),'root_id':'original-root',
        'argv':['collab','--project',str(tmp_path),'--provider','original','--session','original-root']}
    watch.save(job,'STOPPED',fault={'category':'engine','message':'original defect'},resume_token=token,
        needs_maintenance=True,model_calls=3)
    observed=[]
    def execute(command,**kwargs):
        observed.append(command)
        assert command[:3]==[sys.executable,'-m','auto_agents']
        assert command[command.index('--session')+1]=='original-root'
        assert command[-2:]==['--provider','replacement']
        kwargs['observation'].write_text(json.dumps({'status':'stopped',
            'fault':{'category':'state','message':'current operator decision'}}))
        return {'reason':'','returncode':3}
    monkeypatch.setattr(module,'run',execute)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    runner=Runner(watch)
    monkeypatch.setattr(runner,'maintain',lambda *_:pytest.fail('Recheck dispatched old repair'))
    result=runner.resume(job['id'],retry_business=True)
    assert len(observed)==1 and result['model_calls']==3
    assert result['argv']==argv and result['fault']['message']=='current operator decision'

def test_resume_cli_forwards_explicit_business_recheck(tmp_path, monkeypatch, capsys):
    from auto_agents_watch import cli
    calls = []

    class Stub:

        def __init__(self, store):
            pass

        def resume(self, job, **options):
            calls.append((job, options))
            return {'state': 'STOPPED', 'reason': 'retained'}
    monkeypatch.setattr(cli, 'Runner', Stub)
    assert cli.main(['resume', '--root', str(tmp_path), '--job', 'original', '--retry-business', '--json']) == 3
    capsys.readouterr()
    assert calls == [('original', {'explicit': True, 'retry_business': True})]

@pytest.mark.parametrize('physical_failure', [False, True])
@pytest.mark.parametrize('repair_scope', ['', 'target_project', 'execution_environment'])
def test_browser_content_prerequisite_stays_failed_without_hiding_real_infrastructure(physical_failure, repair_scope):
    from auto_agents.gates import classify_reported_infrastructure_failure, extract_failure_info
    from auto_agents.models import CommandResult, GateResult
    marker = 'AUTO_AGENTS_INFRA_FAILURE id=browser_verification_infrastructure_failed capability=chrome contract=cdp-v1' + (f' repair_scope={repair_scope}' if repair_scope else '') + ': '
    output = 'FAIL src/e2e/page.test.ts > approved prototype\n' + marker + 'Timed out waiting for expression: document.querySelectorAll(".card").length === 3\n'
    if physical_failure:
        output += marker + 'Browser launch attempt 3/3 failed\n'
    result = CommandResult(command='vitest run page.test.ts', ok=False, returncode=1, stderr=output)
    classify_reported_infrastructure_failure(result)
    info = extract_failure_info(GateResult(ok=False, commands=[result]))
    assert result.ok is False and result.returncode == 1
    infrastructure = physical_failure or repair_scope == 'execution_environment'
    assert result.infrastructure_error is infrastructure
    assert info.comparable is (not infrastructure)
    if not infrastructure:
        assert info.failure_ids == ['src/e2e/page.test.ts > approved prototype']

@pytest.mark.parametrize('structured', [True, False])
def test_failed_supervisor_snapshot_reports_exit_and_reclaims_partial_copy(tmp_path, monkeypatch, structured):
    from auto_agents_watch.runner import Runner
    from auto_agents_watch import runner as module
    watch = Store(tmp_path / 'watch')
    job = watch.create([], tmp_path / 'project', tmp_path / 'engine')
    runner = Runner(watch)
    runner.directory = watch.root / 'jobs' / job['id']
    snapshot = runner.directory / 'project'
    snapshot.mkdir()
    (snapshot / 'old-partial').write_text('previous failed copy')
    calls = []

    def execute(argv, **kwargs):
        output = Path(argv[argv.index('--output') + 1])
        output.mkdir()
        (output / 'partial').write_text('interrupted export')
        calls.append(output)
        return SimpleNamespace(returncode=3 if structured else -9, stdout=json.dumps({'ok': False, 'error': 'registered source unavailable'}) if structured else '', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', execute)
    with runner.locked(job):
        for _ in range(2):
            with pytest.raises(RuntimeError, match='exit 3.*registered source' if structured else 'exit -9.*no diagnostic'):
                runner.export_snapshot(job, snapshot)
            assert not snapshot.exists() and (not list(runner.directory.glob('.snapshot-*')))
    assert len(calls) == 2 and calls[0] != calls[1]

def test_supervisor_publishes_only_complete_snapshot(tmp_path, monkeypatch):
    from auto_agents_watch.runner import Runner
    from auto_agents_watch import runner as module
    watch = Store(tmp_path / 'watch')
    job = watch.create([], tmp_path / 'project', tmp_path / 'engine')
    runner = Runner(watch)
    runner.directory = watch.root / 'jobs' / job['id']
    snapshot = runner.directory / 'project'

    def execute(argv, **kwargs):
        output = Path(argv[argv.index('--output') + 1])
        output.mkdir()
        (output / 'complete').write_text('receipt')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'state': {'pending_external': []}}), stderr='')
    monkeypatch.setattr(module.subprocess, 'run', execute)
    with runner.locked(job):
        result = runner.export_snapshot(job, snapshot)
    assert result['snapshot'] == str(snapshot) and (snapshot / 'complete').read_text() == 'receipt'
    assert not list(runner.directory.glob('.snapshot-*'))

def test_supervisor_snapshot_cli_inherits_ownership_without_overwriting_fault(tmp_path):
    from auto_agents_watch.runner import Runner
    from auto_agents.control import Store as BusinessStore
    from auto_agents.control.engine import Engine
    from auto_agents.control.api import status
    from auto_agents.models import ProjectConfig
    from auto_agents.config import save_project_config
    from test_control_engine import project, Worker

    root=project(tmp_path)
    config=ProjectConfig('fixture')
    save_project_config(root,config)
    business=BusinessStore(root)
    work=Engine(root,business,config,transport=Worker(),print_fn=lambda *a:None).start('run','Set value')
    before=status(root)
    engine=Path(__file__).resolve().parents[1]
    watch=Store(tmp_path/'watch')
    job=watch.create([sys.executable,'-m','auto_agents','run','--project',str(root)],root,engine)
    runner=Runner(watch)
    runner.directory=watch.root/'jobs'/job['id']
    observation=runner.directory/'observation.json'
    observation.write_text('{"fault":{"message":"original fault"}}')
    with runner.locked(job):
        # An unrelated process still cannot acquire the same project lock.
        refused=subprocess.run([sys.executable,'-m','auto_agents','snapshot','--project',str(root),
            '--output',str(tmp_path/'unauthorized-copy')],cwd=engine,
            env={**runner.env(job),'PYTHONPATH':str(engine/'src')},capture_output=True,text=True)
        assert refused.returncode==3 and 'already active' in refused.stdout
        result=runner.export_snapshot(job,runner.directory/'project')
    assert result['state']['identity']==before['identity']
    assert BusinessStore(runner.directory/'project').work(work['id'])['contract']==work['contract']
    assert status(root)['identity']==before['identity']
    assert json.loads(observation.read_text())=={'fault':{'message':'original fault'}}

def test_offline_recovery_does_not_send_notifications(monkeypatch):
    from auto_agents.notifications import send_wechat_markdown
    monkeypatch.setenv('AUTO_AGENTS_OFFLINE_RESUME', '1')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: pytest.fail('Offline validation sent a message'))
    assert send_wechat_markdown('check', 'https://example.invalid/webhook') is False

@pytest.mark.parametrize('kind,terminal', [('codex', {'type': 'turn.completed'}), ('claude-code', {'type': 'result', 'subtype': 'success', 'result': 'done'}), ('copilot-cli', {'type': 'session.end'})])
def test_native_partial_output_is_never_a_confirmed_result(tmp_path, kind, terminal):
    from auto_agents_watch.providers import final_message
    message = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}} if kind == 'codex' else {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'done'}]}} if kind == 'claude-code' else {'type': 'assistant.message', 'data': {'content': 'done'}}
    log = tmp_path / 'cli.log'
    log.write_text(json.dumps(message) + '\n')
    assert not final_message(log, kind)['ok']
    with log.open('a') as stream:
        stream.write(json.dumps(terminal) + '\n')
    assert final_message(log, kind)['ok']

def test_cancelled_task_does_not_dispatch_another_model(tmp_path):
    from auto_agents_watch.runner import Runner
    store = Store(tmp_path / 'watch')
    job = store.create(['auto-agents', 'collab', '--project', str(tmp_path / 'project')], tmp_path / 'project', tmp_path / 'engine')
    store.save(job, 'STOPPED', cancel_requested=True)
    assert Runner(store).start(job['argv'], job['engine'])['id'] == job['id']
    assert len(store.list()) == 1 and store.get(job['id'])['model_calls'] == 0

def test_user_rerun_rechecks_business_after_dirty_engine_admission_refusal(tmp_path):
    from auto_agents_watch.runner import Runner
    store = Store(tmp_path / 'watch')
    argv = ['auto-agents', 'collab', '--project', str(tmp_path / 'project'), '--session', 'parent']
    job = store.create(argv, tmp_path / 'project', tmp_path / 'engine')
    original = {'category': 'engine', 'message': 'old selector failure'}
    store.save(job, 'STOPPED', needs_maintenance=True, fault=original, no_progress=2, reason='Engine source has uncommitted changes; candidate admission preserves user work')
    visits = []

    class RepairedRunner(Runner):

        def business(self, job):
            visits.append('business')
            assert self.env(job)['AUTO_AGENTS_MAINTENANCE_RESUME'] == '1'
            self.store.save(job, 'STOPPED', reason='Business task stopped without an engine defect')
            return False

        def maintain(self, job):
            raise AssertionError('Old admission refusal must not preempt business recovery')
    result = RepairedRunner(store).start(argv, tmp_path / 'engine')
    assert visits == ['business']
    assert result['id'] == job['id'] and len(store.list()) == 1
    assert result['model_calls'] == 0 and result['attempts'] == 0 and (result['no_progress'] == 2)
    assert result['retained_faults'][0]['fault'] == original

def test_container_client_failure_still_removes_owned_container(tmp_path, monkeypatch):
    from auto_agents_watch import sandbox
    removed = []
    monkeypatch.setattr(sandbox, 'run', lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(sandbox.subprocess, 'run', lambda argv, **kw: removed.append(argv))
    docker = sandbox.Docker(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        docker.execute(['docker', 'run', '--name', 'owned-container', 'image'], cwd=tmp_path, env={}, log=tmp_path / 'log')
    assert removed == [['docker', 'rm', '-f', 'owned-container']]

def test_stale_cancel_cannot_erase_dispatch_reservation(tmp_path):
    store = Store(tmp_path / 'watch')
    job = store.create(['business'], tmp_path, tmp_path)
    stale = store.get(job['id'])
    store.reserve(job, 'implement')
    store.save(stale, cancel_requested=True)
    retained = store.get(job['id'])
    assert retained['model_calls'] == 1 and retained['active_call']
    assert retained['cancel_requested']

def test_provider_change_does_not_create_a_new_maintenance_budget(tmp_path):
    from auto_agents_watch.runner import Runner
    store = Store(tmp_path / 'watch')
    argv = ['auto-agents', 'collab', '--project', str(tmp_path), '--session', 's', '--provider', 'first']
    job = store.create(argv, tmp_path, tmp_path)
    store.reserve(job, 'implement')
    store.settle(job, {'ok': True})
    store.save(job, 'STOPPED', cancel_requested=True, no_progress=2)
    changed = [*argv[:-1], 'second']
    result = Runner(store).start(changed, tmp_path)
    assert result['id'] == job['id'] and result['model_calls'] == 1 and (result['no_progress'] == 2)
    assert len(store.list()) == 1

def test_maintenance_reconciliation_preserves_count_and_requires_explicit_resume(tmp_path, capsys):
    from auto_agents_watch.cli import main
    store = Store(tmp_path / 'watch')
    job = store.create(['business'], tmp_path / 'project', tmp_path / 'engine')
    store.reserve(job, 'implement')
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'job_id': job['id'], 'call': 1, 'result': {'confirmed': True, 'ok': False, 'text': '', 'reason': 'Operator confirmed cancellation'}}))
    assert main(['reconcile', '--root', str(store.root), '--job', job['id'], '--result', str(receipt)]) == 0
    saved = store.get(job['id'])
    assert saved['model_calls'] == 1 and saved['active_call'] is None
    assert saved['state'] == 'STOPPED' and saved['needs_verification']

def test_public_quiesce_stops_registered_owned_child_and_preserves_other_process(tmp_path):
    import threading
    from auto_agents_watch.runner import Runner
    from auto_agents.process_supervision import process_start_ticks
    store = Store(tmp_path / 'watch')
    job = store.create([], tmp_path / 'project', tmp_path / 'engine')
    runner = Runner(store)
    runner.directory = tmp_path / 'watch/jobs' / job['id']
    owned = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    other = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    reaper = threading.Thread(target=owned.wait, daemon=True)
    reaper.start()
    try:
        with runner.locked(job):
            control = Path(job['project']) / '.auto-agents/state/run.processes.json'
            control.write_text(json.dumps({'project': job['project'], 'run_token': runner.token, 'processes': [{'pid': owned.pid, 'pgid': owned.pid, 'start_ticks': process_start_ticks(owned.pid)}, {'pid': other.pid, 'pgid': other.pid, 'start_ticks': 0}]}))
            result = subprocess.run([sys.executable, '-m', 'auto_agents', 'quiesce', '--project', job['project']], env=runner.env(job), pass_fds=(runner.fd,), capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(result.stdout) == {'ok': True, 'groups': 1}
            assert other.poll() is None
        reaper.join(timeout=2)
        assert owned.returncode is not None
    finally:
        for child in (owned, other):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

def test_large_prompt_does_not_block_timeout_or_cancellation(tmp_path):
    result = run([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=tmp_path, env=os.environ, log=tmp_path / 'blocked.log', stdin='x' * 1000000, timeout=0.2)
    assert result['reason'] == 'operation_timeout'

def test_same_exception_type_with_different_reason_is_not_same_fault():
    from auto_agents_watch.runner import normalized_error
    assert normalized_error('missing file /tmp/a/file.py') == normalized_error('missing file /tmp/b/file.py')
    assert normalized_error('credentials unavailable') != normalized_error('original engine state corrupt')


def test_retired_job_starts_business_in_successor_without_old_docker_repair(tmp_path):
    from auto_agents_watch.runner import Runner
    project=tmp_path/'project';project.mkdir()
    store=Store(tmp_path/'watch')
    argv=['auto-agents','collab','--project',str(project),'--session','original','--provider','first']
    old=store.create(argv,project,tmp_path/'engine')
    store.save(old,'STOPPED',fault={'category':'engine','message':'obsolete classification conflict'},
        needs_maintenance=True,resume_token={'schema':1},candidate=str(tmp_path/'old-candidate'),snapshot=None,
        cleanup={'old_snapshot_available':False},model_calls=7,attempts=3,no_progress=1,best_passed=['retained'])
    old_created=old['created'];visits=[]
    class Recheck(Runner):
        def business(self,job):
            visits.append(job['id'])
            assert not job.get('fault') and not job.get('needs_maintenance')
            assert not job.get('candidate') and not job.get('resume_token')
            assert job['argv']==argv
            self.store.save(job,'STOPPED',reason='current business requires operator decision')
            return False
        def maintain(self,job):
            pytest.fail('Retired fault entered Docker before business ran')
    result=Recheck(store,sandbox_factory=lambda *a,**k:pytest.fail('Docker invoked')).start(argv,tmp_path/'engine')
    assert visits==[result['id']] and result['id']!=old['id']
    assert result['predecessor']==old['id']
    assert (result['model_calls'],result['attempts'],result['no_progress'])==(7,3,1)
    assert result['maintenance_started']==old_created and result['best_passed']==['retained']
    assert store.get(old['id'])['fault']['message']=='obsolete classification conflict'
    assert store.get(old['id'])['superseded_by']==result['id']
    with pytest.raises(RuntimeError,match='model-call limit'):
        store.reserve(result,'implement',max_calls=7)
    # Repeating the command reuses the successor, not another budget/fault.
    assert Recheck(store).start(argv,tmp_path/'engine')['id']==result['id']
    assert len(store.list())==2


def test_retired_job_unknown_call_cannot_be_bypassed_by_successor(tmp_path):
    from auto_agents_watch.runner import Runner
    store=Store(tmp_path/'watch')
    argv=['auto-agents','collab','--project',str(tmp_path),'--session','original']
    old=store.create(argv,tmp_path,tmp_path)
    store.reserve(old,'implement')
    store.save(old,'STOPPED',cleanup={'old_snapshot_available':False})
    with pytest.raises(RuntimeError,match='Unconfirmed model operation'):
        Runner(store).start(argv,tmp_path)
    assert len(store.list())==1 and store.get(old['id'])['model_calls']==1


def test_explicit_retry_of_retired_job_observes_current_business(tmp_path):
    from auto_agents_watch.runner import Runner
    store=Store(tmp_path/'watch')
    old=store.create(['auto-agents','collab','--project',str(tmp_path),'--session','original'],tmp_path,tmp_path)
    store.save(old,'STOPPED',cleanup={'original_in_place_recovery':False},fault={'category':'engine','message':'obsolete'},model_calls=4)
    class Recheck(Runner):
        def business(self,job):
            assert not job.get('fault')
            self.store.save(job,'STOPPED',reason='rechecked')
            return False
        def maintain(self,job):pytest.fail('Old fault dispatched')
    with pytest.raises(RuntimeError,match='inputs were retired'):
        Recheck(store).resume(old['id'],explicit=True)
    result=Recheck(store).resume(old['id'],explicit=True,retry_business=True)
    assert result['id']!=old['id'] and result['model_calls']==4
    assert Recheck(store).resume(old['id'],retry_business=True)['id']==result['id']


def test_successor_uses_durable_counters_and_preserves_cancellation(tmp_path):
    store=Store(tmp_path/'watch')
    old=store.create(['auto-agents'],tmp_path,tmp_path)
    stale=dict(old)
    store.save(old,'STOPPED',model_calls=5,attempts=2,cancel_requested=True,
        maintenance_started=None,cleanup={'old_snapshot_available':False})
    successor=store.successor(stale,old['argv'])
    assert successor['model_calls']==5 and successor['attempts']==2
    assert successor['cancel_requested'] is True
    assert successor['maintenance_started']==old['created']
    assert (store.root/'jobs'/successor['id']).is_dir()
    assert store.successor(stale,old['argv'])==successor
    assert len(store.list())==2
    from auto_agents_watch.runner import Runner
    assert Runner(store).resume(successor['id'])==successor
    with pytest.raises(RuntimeError,match='Only a stopped'):
        store.successor(successor,old['argv'])


def test_docker_service_failure_reports_prerequisite_without_building(tmp_path,monkeypatch):
    from auto_agents_watch import sandbox as module
    monkeypatch.setattr(module.shutil,'which',lambda name:'/usr/bin/docker')
    calls=[]
    def execute(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1,stderr='Cannot connect to the Docker daemon',stdout='')
    monkeypatch.setattr(module.subprocess,'run',execute)
    with pytest.raises(RuntimeError,match='start Docker.*Cannot connect'):
        module.Docker(tmp_path).prepare()
    assert calls==[['docker','info']]
