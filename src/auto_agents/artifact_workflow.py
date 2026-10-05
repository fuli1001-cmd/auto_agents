"""Completed candidate delivery and reference retirement for ArtifactStore.

The project and artifact locks fence this transaction. A durable receipt precedes
every reference update, so an interrupted cleanup can finish without its checkout.
Unknown references and incomplete workflow graphs always retain the original.
"""
from copy import deepcopy
from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import time

from .artifact_store import _identity, _parents
from .local_io import atomic_json


def _persist(path, value):
    from .io_utils import write_json
    write_json(path, value)
    descriptor = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('unknown_reference: unsafe or oversized state: ' + str(path))
    from .io_utils import read_json
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError('unknown_reference: invalid state: ' + str(path))
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, timeout=20)
    if result.returncode:
        raise ValueError('candidate_delivery_unavailable: ' + result.stderr.strip())
    return result.stdout.strip()


def _contains(value, target):
    if isinstance(value, str):
        # Paths in structured records, including path:line evidence references.
        value = value.rsplit(':', 1)[0] if value.rsplit(':', 1)[-1].isdigit() else value
        return value == str(target) or value.startswith(str(target) + '/')
    if isinstance(value, dict):
        return any(_contains(v, target) for v in value.values())
    return isinstance(value, list) and any(_contains(v, target) for v in value)


def _component(value):
    if not isinstance(value, str) or not value or Path(value).name != value or value in {'.', '..'}:
        raise ValueError('unknown_reference: invalid workflow/session identity')
    return value


def _documents(project):
    state = project / '.auto-agents/state'
    yield from (state / 'sessions').glob('*/session_state.json')
    yield from (state / 'handoffs').glob('*.json')
    yield from (state / 'sources').glob('*.json')
    yield from (state / 'workflows').glob('*/workflow.json')
    if (state / 'run_state.json').exists():
        yield state / 'run_state.json'
    # Recovery records are consumers too. Event journals and transcripts are
    # immutable history, not authority to resume a finished handoff.
    yield from (project / '.auto-agents/runs').glob('*/repair-cases/*.json')
    yield from (project / '.auto-agents/runs').glob('*/attempt-checkpoints/**/*.json')


def _graph(row, *, locked=False):
    from .artifact_references import project_protection
    metadata = row['metadata']
    project = Path(metadata['project'])
    if not locked:
        reason = project_protection(project, recovery=False)
        if reason:
            raise ValueError(reason)
    session_id = _component(metadata['session_id'])
    state = project / '.auto-agents/state'
    session = _read(state / 'sessions' / session_id / 'session_state.json')
    workflow_id = session.get('workflow_id')
    if not workflow_id or workflow_id != metadata.get('workflow_id'):
        raise ValueError('unknown_reference: candidate workflow identity unavailable')
    workflow = _read(state / 'workflows' / _component(workflow_id) / 'workflow.json')
    if workflow.get('workflow_id') != workflow_id:
        raise ValueError('unknown_reference: candidate workflow identity changed')
    if workflow.get('status') != 'completed':
        raise ValueError('workflow_not_completed: ' + str(workflow.get('status', 'unknown')))
    if workflow.get('active_handoff_id') or workflow.get('recovery_required'):
        raise ValueError('workflow_child_pending')
    root = workflow.get('root', {})
    if root.get('kind') == 'run':
        root_state = _read(state / 'run_state.json')
        if root_state.get('run_id') != root.get('native_id'):
            raise ValueError('unknown_reference: workflow root run unavailable')
    else:
        root_state = _read(state / 'sessions' / _component(root.get('native_id')) / 'session_state.json')
    if root_state.get('workflow_id') != workflow_id or root_state.get('status') != 'completed':
        raise ValueError('workflow_root_not_completed')
    documents = {}
    deadline = time.monotonic() + 2
    for path in _documents(project):
        if time.monotonic() > deadline:
            raise ValueError('unknown_reference: candidate scan budget exhausted')
        value = _read(path)
        documents[path] = value
        if value.get('workflow_id') != workflow_id:
            continue
        if path.name in {'session_state.json', 'run_state.json'}:
            if value.get('status') != 'completed' or value.get('active_handoff_id'):
                raise ValueError('workflow_child_not_completed: ' + str(path))
        elif path.parent == state / 'handoffs':
            if value.get('status') != 'completed' or not value.get('returned_at'):
                raise ValueError('workflow_handoff_pending: ' + str(path))
            for ref in (value.get('parent'), value.get('child')):
                if not isinstance(ref, dict):
                    raise ValueError('unknown_reference: missing handoff participant')
                if ref.get('kind') != 'run':
                    participant = _read(state / 'sessions' / _component(ref.get('native_id')) / 'session_state.json')
                else:
                    participant = _read(state / 'run_state.json')
                    if participant.get('run_id') != ref.get('native_id'):
                        raise ValueError('unknown_reference: child run unavailable')
                if participant.get('workflow_id') != workflow_id or participant.get('status') != 'completed':
                    raise ValueError('workflow_child_not_completed')
    return project, workflow_id, session, documents


def _receipt_path(row):
    return Path(row['metadata']['project']) / '.auto-agents/state/candidate-deliveries' / (row['id'] + '.json')


def _verify_archive(row, receipt):
    project = Path(row['metadata']['project'])
    archive = project / '.auto-agents/state/candidate-deliveries/objects.git'
    if (receipt.get('schema_version') != 1 or receipt.get('artifact_id') != row['id']
            or receipt.get('inode') != row['inode'] or receipt.get('path') != row['path']
            or receipt.get('repository') != str(archive) or not receipt.get('commits')):
        raise ValueError('candidate_archive_invalid')
    if archive.is_symlink():
        raise ValueError('candidate_archive_replaced')
    digest = row['metadata'].get('delivery_sha256')
    if digest and digest != _hash(receipt):
        raise ValueError('candidate_archive_receipt_changed')
    session = _read(project / '.auto-agents/state/sessions' /
                    _component(row['metadata']['session_id']) / 'session_state.json')
    if _hash(session.get('execution_log', [])) != receipt['summary'].get('verification_sha256'):
        raise ValueError('candidate_verification_record_changed')
    for commit in receipt['commits']:
        ref = 'refs/auto-agents/candidates/' + row['id'] + '/' + commit
        if _git(archive, 'rev-parse', '--verify', ref + '^{commit}') != commit:
            raise ValueError('candidate_archive_commit_missing')
    for change in receipt['changes']:
        path = Path(change['path'])
        if not path.is_relative_to(project / '.auto-agents/state'):
            raise ValueError('candidate_archive_reference_escape')
        current = _read(path)
        keys = _changed_keys(change)
        if any(current.get(key) not in (change['before'].get(key), change['after'].get(key)) for key in keys):
            raise ValueError('candidate_reference_changed: ' + str(path))


def _changed_keys(change):
    return {key for key in set(change['before']) | set(change['after'])
            if change['before'].get(key) != change['after'].get(key)}


def _changes(row, workflow_id, documents, summary):
    target = Path(row['path'])
    state = Path(row['metadata']['project']) / '.auto-agents/state'
    changes = []
    for path, before in documents.items():
        if not _contains(before, target):
            continue
        if before.get('workflow_id') != workflow_id:
            raise ValueError('referenced_candidate: ' + str(path))
        after = deepcopy(before)
        if path.name == 'session_state.json':
            for key in ('candidate_custody', 'source_descriptor'):
                if _contains(after.get(key), target):
                    after[key] = {}
                    after['candidate_archive' if key == 'candidate_custody' else 'source_archive'] = summary
        elif path.parent == state / 'handoffs':
            for section, key in (('payload', 'source_descriptor'), ('result', 'candidate_delivery')):
                if _contains(after.get(section, {}).get(key), target):
                    after[section][key] = {}
                    after[section][key + '_archive'] = summary
        elif path.parent == state / 'sources':
            after = {'schema_version': 1, 'workflow_id': workflow_id,
                     'source_id': before.get('source_id'), 'archive': summary}
        if _contains(after, target):
            raise ValueError('referenced_candidate_evidence: ' + str(path))
        keys = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
        changes.append({'path': str(path),
                        'before': {key: before[key] for key in keys if key in before},
                        'after': {key: after[key] for key in keys if key in after}})
    return changes


def _delivery(row, session):
    from .session_source import validate_checkout
    from .models import SessionState
    from .session_candidate import completed_delivery, validate_materialized_source
    checkout = Path(row['metadata']['checkout'])
    if checkout.parent != Path(row['path']) or checkout.name != 'project':
        raise ValueError('candidate_checkout_identity_changed')
    native = SessionState.from_dict(session)
    if native.candidate_custody.get('checkout') != str(checkout):
        raise ValueError('candidate_delivery_unproven: session no longer owns checkout')
    validate_checkout(Path(row['metadata']['project']), native, checkout)
    if native.candidate_custody.get('receipt'):
        if not completed_delivery(native):
            raise ValueError('candidate_delivery_unproven')
        revision = native.candidate_custody['delivered_revision']
        validate_materialized_source(native, checkout, revision)
    else:
        # Parent collab commits carry an operation trailer. Merely inheriting a
        # child's base commit does not prove the parent finished delivery.
        from .workflow_runtime import _head_contains_completed_session
        if not _head_contains_completed_session(checkout, native.session_id):
            raise ValueError('candidate_delivery_unproven')
        revision = _git(checkout, 'rev-parse', 'HEAD')
        from .session_verification import product_path
        paths = _git(checkout, 'diff', '--name-only', 'HEAD').splitlines()
        paths += _git(checkout, 'ls-files', '--others', '--exclude-standard').splitlines()
        from .gate_execution import discover_dependency_links
        dependencies = discover_dependency_links(checkout)
        if any(product_path(path) and path not in dependencies for path in paths):
            raise ValueError('candidate_undelivered_changes')
    return checkout, revision


def candidate_protection(row):
    try:
        metadata = row['metadata']
        if metadata.get('candidate_lifecycle') != 1:
            return 'unknown_reference: unsupported candidate lifecycle'
        own = 'session:' + metadata['project'] + ':' + metadata['session_id']
        if any(ref != own for ref in row['references']):
            return 'referenced: ' + ', '.join(row['references'])
        _, workflow_id, session, documents = _graph(row)
        receipt_path = _receipt_path(row)
        if receipt_path.exists():
            receipt = _read(receipt_path)
            _verify_archive(row, receipt)
            _changes(row, workflow_id, documents, receipt['summary'])
        else:
            _changes(row, workflow_id, documents, {})
            _delivery(row, session)
        return ''
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return str(error) or 'unknown_reference: candidate unavailable'


def retire_candidate(row, deadline):
    """Called only under ArtifactStore's lifecycle and project deletion locks."""
    if time.monotonic() >= deadline:
        raise TimeoutError('candidate_retirement_budget_exhausted')
    project, workflow_id, session, documents = _graph(row, locked=True)
    receipt_path = _receipt_path(row)
    if receipt_path.exists():
        receipt = _read(receipt_path)
        _verify_archive(row, receipt)
        # Refuse new consumers even when recovering a partial retirement.
        expected = {change['path'] for change in receipt['changes']}
        changes = _changes(row, workflow_id, documents, receipt['summary'])
        if any(change['path'] not in expected for change in changes):
            raise ValueError('candidate_acquired_new_reference')
    else:
        if _identity(row['path']) != row['inode']:
            raise ValueError('candidate_directory_replaced')
        checkout, revision = _delivery(row, session)
        archive = receipt_path.parent / 'objects.git'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.is_symlink():
            raise ValueError('candidate_archive_replaced')
        if not archive.exists():
            _git(archive.parent, 'init', '--bare', str(archive))
        # A verified writer can leave an intentional staged/unstaged split.
        # Its index blobs need a reachable commit as well as the delivered tree.
        index_tree = _git(checkout, 'write-tree')
        index_revision = _git(checkout, 'commit-tree', index_tree, '-p', revision,
                              '-m', 'Retained candidate index ' + row['id'])
        commits = {revision, _git(checkout, 'rev-parse', 'HEAD')}
        commits.add(index_revision)
        commits.update(_git(checkout, 'for-each-ref', '--format=%(objectname)').splitlines())
        refs = []
        for commit in sorted(commits):
            if time.monotonic() >= deadline:
                raise TimeoutError('candidate_retirement_budget_exhausted')
            # Resolve tags to commits; retain all candidate histories and frozen
            # verification revisions in the independent object database.
            commit = _git(checkout, 'rev-parse', '--verify', commit + '^{commit}')
            ref = 'refs/auto-agents/candidates/' + row['id'] + '/' + commit
            _git(archive, '-c', 'core.fsync=all', 'fetch', '--no-tags', str(checkout), commit + ':' + ref)
            refs.append(commit)
        summary = {'receipt': str(receipt_path), 'repository': str(archive),
                   'revision': revision, 'workflow_id': workflow_id,
                   'session_id': row['metadata']['session_id'],
                   'index_revision': index_revision,
                   'verification_sha256': _hash(session.get('execution_log', []))}
        receipt = {'schema_version': 1, 'artifact_id': row['id'], 'inode': row['inode'],
                   'path': row['path'], 'repository': str(archive), 'commits': sorted(set(refs)),
                   'summary': summary, 'changes': _changes(row, workflow_id, documents, summary)}
        # Retain custody manifests and consumed handoff receipts. Verification
        # results stay in the durable session, with their hash in the summary.
        _persist(receipt_path, receipt)
        _verify_archive(row, receipt)
    for change in receipt['changes']:
        if time.monotonic() >= deadline:
            raise TimeoutError('candidate_retirement_budget_exhausted')
        current = _read(change['path'])
        for key in _changed_keys(change):
            if key in change['after']:
                current[key] = change['after'][key]
            else:
                current.pop(key, None)
        _persist(Path(change['path']), current)
    row['references'] = []
    row['metadata']['delivery_receipt'] = str(receipt_path)
    row['metadata']['delivery_sha256'] = _hash(receipt)


def archived_session(project, state):
    """Completed sessions return their retained result without reopening custody."""
    summary = state.candidate_archive
    if state.status != 'completed' or not summary:
        return False
    from .workflow_chain import WorkflowStore
    if WorkflowStore(project).load(state.workflow_id).status != 'completed':
        raise ValueError('archived candidate belongs to a resumed workflow')
    receipt = _read(summary['receipt'])
    archive = Path(project).resolve() / '.auto-agents/state/candidate-deliveries/objects.git'
    if receipt.get('summary') != summary or summary.get('repository') != str(archive):
        raise ValueError('candidate_archive_invalid')
    if _hash(state.execution_log) != summary.get('verification_sha256'):
        raise ValueError('candidate_verification_record_changed')
    ref = 'refs/auto-agents/candidates/' + receipt['artifact_id'] + '/' + summary['revision']
    if _git(archive, 'rev-parse', '--verify', ref + '^{commit}') != summary['revision']:
        raise ValueError('candidate_archive_commit_missing')
    return True


def owner_protection(metadata):
    """New workflow evidence cannot expire while its actual owner can resume."""
    try:
        state = Path(metadata['project']) / '.auto-agents/state'
        if metadata.get('session_id'):
            identity = _component(metadata['session_id'])
            owner = _read(state / 'sessions' / identity / 'session_state.json')
            key = 'session_id'
        elif metadata.get('run_id'):
            identity = _component(metadata['run_id'])
            owner = _read(state / 'run_state.json')
            key = 'run_id'
        else:
            return 'unknown_reference: workflow owner unavailable'
        if owner.get(key) != identity:
            return 'unknown_reference: workflow owner changed'
        if owner.get('status') != 'completed':
            return 'workflow_not_completed: ' + str(owner.get('status', 'unknown'))
        workflow_id = owner.get('workflow_id')
        if not workflow_id:
            return 'unknown_reference: workflow identity unavailable'
        workflow = _read(state / 'workflows' / _component(workflow_id) / 'workflow.json')
        if workflow.get('status') != 'completed':
            return 'workflow_not_completed: ' + str(workflow.get('status', 'unknown'))
        if workflow.get('active_handoff_id') or workflow.get('recovery_required'):
            return 'workflow_child_pending'
        deadline = time.monotonic() + 2
        for path in _documents(Path(metadata['project'])):
            if time.monotonic() > deadline:
                return 'unknown_reference: workflow scan budget exhausted'
            document = _read(path)
            if document.get('workflow_id') != workflow_id:
                continue
            if path.name in {'session_state.json', 'run_state.json'} and document.get('status') != 'completed':
                return 'workflow_child_not_completed'
            if path.parent == state / 'handoffs' and (
                    document.get('status') != 'completed' or not document.get('returned_at')):
                return 'workflow_handoff_pending'
        return ''
    except (OSError, ValueError, KeyError, TypeError) as error:
        return 'unknown_reference: ' + str(error)


def adopt_registered_candidate(row):
    """Upgrade only exact producer-owned legacy rows with verified custody.

    Never discover paths by scanning /tmp or interpreting directory prefixes.
    Missing/deleted sessions and unregistered historical directories stay unknown.
    """
    metadata = row['metadata']
    if row['kind'] != 'recovery' or metadata.get('candidate_lifecycle') or not metadata.get('project'):
        return False
    prefix = 'session:' + metadata['project'] + ':'
    owners = [ref[len(prefix):] for ref in row['references'] if ref.startswith(prefix)]
    if len(owners) != 1:
        return False
    try:
        from .models import SessionState
        from .session_source import validate_checkout
        project = Path(metadata['project'])
        state = SessionState.from_dict(_read(project / '.auto-agents/state/sessions' /
                                           _component(owners[0]) / 'session_state.json'))
        if state.status != 'completed':
            return False
        checkout = Path(state.candidate_custody.get('checkout', ''))
        if checkout != Path(row['path']) / 'project' or _identity(row['path']) != row['inode']:
            return False
        validate_checkout(project, state, checkout)
        candidate = deepcopy(row)
        candidate['metadata'].update(candidate_lifecycle=1, session_id=state.session_id,
                                     workflow_id=state.workflow_id, checkout=str(checkout))
        _graph(candidate)
        row['metadata'] = candidate['metadata']
        return True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


def _repair_databases(store):
    from .artifact_legacy import repair_roots
    roots = set(repair_roots(store))
    roots.update(Path(row['metadata']['repair_root']) for row in store.rows()
                 if row['metadata'].get('repair_root') and row['state'] != 'deleted')
    return [root / 'control.sqlite3' for root in sorted(roots)]


def repair_candidate_protection(store, row):
    try:
        deadline = time.monotonic() + 2
        for path in _repair_databases(store):
            _identity(path)
            _parents(path)
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
                for query in ('SELECT payload,result FROM jobs', 'SELECT project,payload FROM subscribers',
                              'SELECT payload FROM verification_contexts', 'SELECT payload FROM verifications'):
                    for values in db.execute(query):
                        if time.monotonic() > deadline:
                            return 'unknown_reference: repair reference scan budget exhausted'
                        for value in values:
                            decoded = json.loads(value) if value.startswith(('{', '[')) else value
                            if _contains(decoded, Path(row['path'])):
                                return 'referenced_repair_candidate: ' + str(path)
        return ''
    except (OSError, ValueError, sqlite3.Error) as error:
        return 'unknown_reference: repair candidate: ' + str(error)


@contextmanager
def candidate_repair_guard(store, row):
    with ExitStack() as stack:
        for path in _repair_databases(store):
            _identity(path)
            _parents(path)
            db = stack.enter_context(closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)))
            db.execute('BEGIN IMMEDIATE')
        reason = repair_candidate_protection(store, row)
        if reason:
            raise ValueError(reason)
        yield
