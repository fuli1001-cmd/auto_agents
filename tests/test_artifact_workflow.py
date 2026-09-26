"""Candidate cleanup uses real Git delivery, workflow graphs and crash recovery."""
import json
from pathlib import Path
import subprocess
import time

import pytest

from auto_agents.artifact_cleanup import clean
from auto_agents.artifact_runtime import command_context
from auto_agents.config import create_session, save_session_state, load_session_state
from auto_agents.session_candidate import _runtime_checkout
from auto_agents.workflow_chain import WorkflowStore, WorkflowRef
from test_artifact_storage import store, age


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def candidate(store, tmp_path):
    from unittest.mock import patch
    import tempfile
    project = tmp_path / 'project'
    project.mkdir()
    git(project, 'init', '-q')
    git(project, 'config', 'user.name', 'Test')
    git(project, 'config', 'user.email', 'test@example.com')
    (project / 'product.txt').write_text('before')
    git(project, 'add', '.')
    git(project, 'commit', '-qm', 'base')
    session = create_session(project, 'collab')
    workflows = WorkflowStore(project)
    workflow = workflows.create_root(WorkflowRef('collab', session.session_id))
    session.workflow_id = workflow.workflow_id
    mkdtemp = tempfile.mkdtemp
    with command_context(project):
        with patch('auto_agents.session_candidate.tempfile.mkdtemp',
                   side_effect=lambda **kwargs: mkdtemp(prefix=kwargs['prefix'], dir=tmp_path)):
            checkout, revision = _runtime_checkout(project, session, project, git(project, 'rev-parse', 'HEAD'))
    session.candidate_custody = {'schema_version': 1, 'checkout': str(checkout),
        'repository': str(project), 'session_id': session.session_id, 'base_revision': revision}
    session.status = 'completed'
    (checkout / 'product.txt').write_text('delivered')
    save_session_state(checkout, session)
    save_session_state(project, session)
    git(checkout, 'add', '-f', 'product.txt', f'.auto-agents/state/sessions/{session.session_id}/session_state.json')
    git(checkout, 'commit', '-qm', 'collab delivery')
    workflows.complete(workflow)
    row = next(row for row in store.rows() if row['path'] == str(checkout.parent))
    return project, session, workflows, workflow, checkout, row['id']


def test_candidate_registration_and_immediate_completed_cleanup(store, tmp_path):
    project, session, _, _, checkout, identity = candidate(store, tmp_path)
    row = store.get(identity)
    assert row['kind'] == 'recovery'
    assert row['metadata']['session_id'] == session.session_id
    assert row['metadata']['workflow_id'] == session.workflow_id
    assert row['leases'] == []
    head = git(checkout, 'rev-parse', 'HEAD')
    result = clean(store=store)
    assert result['ok'], result
    assert not checkout.parent.exists(), result
    state = load_session_state(project, session.session_id)
    assert not state.candidate_custody
    assert state.candidate_archive['revision'] == head
    archive = Path(state.candidate_archive['repository'])
    assert git(archive, 'show', head + ':product.txt') == 'delivered'
    assert (project / 'product.txt').read_text() == 'before'
    receipt = json.loads(Path(state.candidate_archive['receipt']).read_text())
    assert receipt['changes'][0]['before']['candidate_custody'] == session.candidate_custody
    assert store.get(identity)['state'] == 'deleted'
    from auto_agents.artifact_workflow import archived_session
    assert archived_session(project, state)


@pytest.mark.parametrize('status', ['paused', 'blocked', 'failed', 'cancelled', 'active'])
def test_only_completed_workflows_qualify_even_under_pressure(store, tmp_path, status):
    _, _, workflows, workflow, checkout, identity = candidate(store, tmp_path)
    workflow.status = status
    workflows.save(workflow)
    age(store, identity, days=365)
    plan = store.plan(pressure=True)
    assert 'workflow_not_completed: ' + status == plan['items'][0]['reason']
    result = clean(store=store)
    assert checkout.exists()
    assert 'workflow_not_completed: ' + status in result['retained_reasons']


def test_pending_sibling_or_handoff_prevents_retirement(store, tmp_path):
    project, _, workflows, workflow, checkout, identity = candidate(store, tmp_path)
    sibling = create_session(project, 'fix')
    sibling.workflow_id = workflow.workflow_id
    sibling.status = 'blocked'
    save_session_state(project, sibling)
    assert 'workflow_child_not_completed' in store.classify(store.get(identity), time.time())
    clean(store=store)
    assert checkout.exists()
    sibling.status = 'completed'
    save_session_state(project, sibling)
    handoff = project / '.auto-agents/state/handoffs/pending.json'
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(json.dumps({'workflow_id': workflow.workflow_id, 'status': 'completed'}))
    assert 'workflow_handoff_pending' in store.classify(store.get(identity), time.time())


def test_external_reference_and_undelivered_changes_are_retained(store, tmp_path):
    project, session, _, _, checkout, identity = candidate(store, tmp_path)
    other = create_session(project, 'fix')
    other.source_descriptor = {'checkout': str(checkout)}
    save_session_state(project, other)
    assert 'referenced_candidate' in store.classify(store.get(identity), time.time())
    other.source_descriptor = {}
    save_session_state(project, other)
    (checkout / 'product.txt').write_text('unrecorded change')
    assert store.classify(store.get(identity), time.time()) == 'candidate_undelivered_changes'
    clean(store=store)
    assert (checkout / 'product.txt').read_text() == 'unrecorded change'


def test_plan_apply_rechecks_workflow_and_archives_only_on_apply(store, tmp_path):
    project, _, workflows, workflow, checkout, identity = candidate(store, tmp_path)
    plan = store.plan()
    assert plan['items'][0]['reason'] == 'eligible'
    assert not (project / '.auto-agents/state/candidate-deliveries').exists()
    workflow.status = 'paused'
    workflows.save(workflow)
    result = store.apply(plan['id'])
    assert result['results'][0]['result'] == 'skipped'
    assert checkout.exists()
    workflows.complete(workflow)
    assert store.apply(store.plan()['id'])['ok']
    assert not checkout.exists()


def test_interrupted_reference_retirement_and_delete_are_retryable(store, tmp_path, monkeypatch):
    project, session, _, _, checkout, identity = candidate(store, tmp_path)
    from auto_agents import artifact_workflow, artifact_store
    write = artifact_workflow.atomic_json
    with monkeypatch.context() as patch:
        def fail_reference(path, value):
            if Path(path).name == 'session_state.json':
                raise OSError('reference write interrupted')
            write(path, value)
        patch.setattr(artifact_workflow, 'atomic_json', fail_reference)
        assert not clean(store=store)['ok']
    assert checkout.exists()
    assert load_session_state(project, session.session_id).candidate_custody
    with monkeypatch.context() as patch:
        def fail_delete(*args, **kwargs):
            import shutil
            shutil.rmtree(checkout / '.git')
            raise TimeoutError('delete interrupted')
        patch.setattr(artifact_store, '_remove_at', fail_delete)
        assert not clean(store=store)['ok']
    assert store.get(identity)['state'] == 'deleting'
    assert not load_session_state(project, session.session_id).candidate_custody
    assert clean(store=store)['ok']
    assert not checkout.parent.exists()


def test_active_lease_pin_and_missing_durable_commit_protect_candidate(store, tmp_path):
    project, session, _, _, checkout, identity = candidate(store, tmp_path)
    store.pin(identity, 'review')
    assert 'pinned' in store.classify(store.get(identity), time.time())
    store.pin(identity, '')
    row = store.get(identity)
    store.register(row['path'], kind=row['kind'], scope=row['scope'])
    assert store.classify(store.get(identity), time.time()) == 'active_process'
    store.release(identity)
    git(checkout, 'reset', '--hard', session.candidate_custody['base_revision'])
    assert store.classify(store.get(identity), time.time()) == 'candidate_delivery_unproven'
    assert clean(store=store)['ok']
    assert checkout.exists()


def test_completed_command_requests_maintenance_after_releasing_leases(store, tmp_path, monkeypatch):
    from auto_agents import artifact_runtime
    calls = []
    monkeypatch.setattr(artifact_runtime, 'schedule', lambda **kwargs: calls.append(kwargs))
    with command_context(tmp_path):
        artifact_runtime.workflow_completed()
    assert calls == [{}, {'completed': True}]


def test_fix_delivery_preserves_dirty_receipt_and_can_be_resumed(store, tmp_path):
    from types import SimpleNamespace
    from auto_agents.session_candidate import _inventory, fingerprint, deliver_candidate
    from auto_agents.gate_execution import GateSnapshotManager
    project, state, _, _, checkout, identity = candidate(store, tmp_path)
    state.mode = 'fix'
    state.verification_binding = {'repository': str(project), 'binding_fingerprint': 'fixture-binding'}
    state.candidate_custody['binding_fingerprint'] = 'fixture-binding'
    state.candidate_custody['base_revision'] = git(checkout, 'rev-parse', 'HEAD')
    before = _inventory(checkout)
    (checkout / 'product.txt').write_text('staged alternative')
    git(checkout, 'add', 'product.txt')
    (checkout / 'product.txt').write_text('verified delivery')
    (checkout / 'product.txt').chmod(0o750)
    after = _inventory(checkout)
    manifest = {'product.txt': {'preimage': before['product.txt'], 'postimage': after['product.txt']}}
    source = GateSnapshotManager(checkout, 'storage-receipt').create(paths=['product.txt'])
    receipt = {'session_id': state.session_id, 'binding_fingerprint': 'fixture-binding',
               'base_revision': state.candidate_custody['base_revision'],
               'source_revision': source.commit_sha, 'manifest': manifest}
    receipt['fingerprint'] = fingerprint(receipt)
    state.candidate_custody['receipt'] = receipt
    state.candidate_paths = {'product.txt': fingerprint(manifest['product.txt']['postimage'])}
    state.execution_log.append({'action': 'receipt_verification', 'receipt_fingerprint': receipt['fingerprint'],
                               'binding_fingerprint': 'fixture-binding', 'verification': {'ok': True}})
    producer = SimpleNamespace(project_root=checkout, _save=lambda value: save_session_state(project, value))
    assert deliver_candidate(producer, state, 'verified fix')
    assert git(checkout, 'show', ':product.txt') == 'staged alternative'
    revision = state.candidate_custody['delivered_revision']
    result = store.clean_artifact(identity)
    assert result['result'] == 'deleted', result
    archived = load_session_state(project, state.session_id)
    assert archived.candidate_archive['revision'] == revision
    archive = Path(archived.candidate_archive['repository'])
    assert git(archive, 'show', revision + ':product.txt') == 'verified delivery'
    assert git(archive, 'show', archived.candidate_archive['index_revision'] + ':product.txt') == 'staged alternative'
    retained = json.loads(Path(archived.candidate_archive['receipt']).read_text())
    assert retained['changes'][0]['before']['candidate_custody']['receipt'] == receipt
    from auto_agents.session import Session
    from auto_agents.orchestrator import Orchestrator
    from auto_agents.config import save_project_config
    from auto_agents.models import ProjectConfig
    save_project_config(project, ProjectConfig(project_name='storage-fixture'))
    assert Session(Orchestrator(project), mode='fix').resume(state.session_id).status == 'completed'


def test_legacy_registered_candidate_requires_matching_completed_custody(store, tmp_path):
    _, _, workflows, workflow, checkout, identity = candidate(store, tmp_path)
    row = store.get(identity)
    row['metadata'] = {'project': row['metadata']['project']}
    store._save(row)
    workflow.status = 'paused'
    workflows.save(workflow)
    clean(store=store)
    assert not store.get(identity)['metadata'].get('candidate_lifecycle')
    assert checkout.exists()
    workflows.complete(workflow)
    assert clean(store=store)['ok']
    assert not checkout.exists()


def test_diagnostic_evidence_cannot_expire_for_failed_or_cancelled_workflow(store, tmp_path):
    project, session, workflows, workflow, _, _ = candidate(store, tmp_path)
    evidence = tmp_path / 'diagnosis'
    evidence.mkdir()
    identity = store.register(evidence, kind='evidence', metadata={'project': str(project),
        'workflow_artifact': 1, 'session_id': session.session_id, 'disposable': True})
    store.release(identity)
    age(store, identity)
    for status in ('failed', 'paused', 'blocked', 'cancelled'):
        workflow.status = status
        workflows.save(workflow)
        assert store.classify(store.get(identity), time.time(), pressure=True) == 'workflow_not_completed: ' + status
    workflows.complete(workflow)
    assert store.classify(store.get(identity), time.time()) == 'eligible'


def test_completed_handoff_and_source_references_are_archived_together(store, tmp_path):
    project, session, _, workflow, checkout, identity = candidate(store, tmp_path)
    child = create_session(project, 'fix')
    child.workflow_id = workflow.workflow_id
    child.status = 'completed'
    source = {'workflow_id': workflow.workflow_id, 'checkout': str(checkout),
              'session_id': session.session_id, 'source_id': 'source-one'}
    child.source_descriptor = source
    save_session_state(project, child)
    state = project / '.auto-agents/state'
    handoff = state / 'handoffs/finished.json'
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(json.dumps({'workflow_id': workflow.workflow_id, 'status': 'completed',
        'returned_at': '2026-09-26T00:00:00Z',
        'parent': {'kind': session.mode, 'native_id': session.session_id},
        'child': {'kind': child.mode, 'native_id': child.session_id},
        'payload': {'source_descriptor': source}}))
    source_path = state / 'sources/source-one.json'
    source_path.parent.mkdir(parents=True)
    source_path.write_text(json.dumps(source))
    assert store.clean_artifact(identity)['result'] == 'deleted'
    retained = load_session_state(project, child.session_id)
    assert not retained.source_descriptor and retained.source_archive
    assert not json.loads(handoff.read_text())['payload']['source_descriptor']
    assert json.loads(source_path.read_text())['archive'] == retained.source_archive


def test_lock_replacement_and_new_reference_after_plan_block_cleanup(store, tmp_path):
    import fcntl
    import hashlib
    import tempfile
    project, session, _, _, checkout, identity = candidate(store, tmp_path)
    lock = Path(tempfile.gettempdir()) / 'auto-agents-run-locks' / (hashlib.sha256(str(project).encode()).hexdigest() + '.lock')
    lock.parent.mkdir(exist_ok=True)
    with lock.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert store.clean_artifact(identity)['reason'] == 'active_project'
    plan = store.plan()
    other = create_session(project, 'fix')
    other.source_descriptor = {'checkout': str(checkout)}
    save_session_state(project, other)
    assert store.apply(plan['id'])['results'][0]['result'] == 'skipped'
    other.source_descriptor = {}
    save_session_state(project, other)
    moved = checkout.parent.with_name('moved')
    checkout.parent.rename(moved)
    checkout.parent.mkdir()
    assert store.clean_artifact(identity)['result'] == 'retained'
    assert (moved / 'project/product.txt').read_text() == 'delivered'


def test_repair_records_protect_candidate_after_workflow_completion(store, tmp_path):
    from auto_agents.repair_control import Store
    _, _, _, _, checkout, identity = candidate(store, tmp_path)
    control = tmp_path / 'repair'
    repair = Store(control)
    (control / 'operator.json').write_text(json.dumps({'root': str(control)}))
    managed = store.register(control / 'operator.json', kind='permanent', metadata={'repair_root': str(control)})
    store.release(managed)
    with repair.connect() as db:
        db.execute('INSERT INTO verification_contexts VALUES(?,?)',
                   ('frozen', json.dumps({'source_root': str(checkout)})))
    result = store.clean_artifact(identity)
    assert result['result'] == 'retained' and 'referenced_repair_candidate' in result['reason']
    assert checkout.exists()
    with repair.connect() as db:
        db.execute('DELETE FROM verification_contexts')
    assert store.clean_artifact(identity)['result'] == 'deleted'
