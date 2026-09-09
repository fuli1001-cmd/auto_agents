"""Private candidate custody: writer receipts never authorize shared copy-back."""
import base64
import errno
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import os
import shutil
import stat
import subprocess
from uuid import uuid4

from .gate_execution import GateSnapshotManager, discover_dependency_links, install_dependency_links
from .session_verification import fingerprint, ownership_error, product_path
from .execution_binding import anchored_parent, restore_private_modes


def _git(root, *args):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.rstrip('\n')


def _image(root, relative):
    try:
        with anchored_parent(root, relative) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                return {'kind': 'symlink', 'mode': mode,
                        'target': os.readlink(name, dir_fd=parent)}
            if stat.S_ISREG(info.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=parent)
                with os.fdopen(descriptor, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise RuntimeError('candidate entry changed during capture')
                    return {'kind': 'file', 'mode': stat.S_IMODE(opened.st_mode),
                            'bytes': base64.b64encode(stream.read()).decode('ascii')}
            return {'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'special', 'mode': mode}
    except OSError as error:
        if error.errno not in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            raise
        # Indexed descendants displaced by a link are deletions, not images
        # of entries in the link's (possibly shared) target directory.
        return {'kind': 'absent'}


def _inventory(root):
    index = {}
    for row in _git(root, 'ls-files', '--stage', '-z').split('\0'):
        if '\t' in row:
            identity, path = row.split('\t', 1)
            index.setdefault(path, []).append(identity)
    paths = set(index) | set(_git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0'))
    # Git indexes leaves, but a replacement also owns the directory preimage.
    # Retain ancestor kinds and modes so file/directory transitions are exact.
    paths.update(parent.as_posix() for path in list(paths) if path
                 for parent in Path(path).parents if parent != Path('.'))
    dependencies = discover_dependency_links(root)
    return {path: {'worktree': _image(root, path), 'index': index.get(path, [])}
            for path in sorted(paths) if path and product_path(path)
            and path not in dependencies}


def _clone(source, revision, destination):
    # A clone owns its object database, refs, config and index. Git worktrees
    # would still mutate the shared repository's metadata during verification.
    initial_paths = []
    if not revision:
        from .git_ops import head_ref
        if head_ref(source):
            raise RuntimeError('initial candidate source requires an unborn repository')
        # Enumerate before creating storage inside the source tree. An unborn
        # standalone session has live initial inputs but no commit to fetch.
        initial_paths = [path for path in _git(source, 'ls-files', '--cached', '--others',
                                              '--exclude-standard', '-z').split('\0') if path]
    _git(source, 'clone', '--no-local', '--no-checkout', str(source), str(destination))
    _git(destination, 'config', 'user.name', 'auto_agents')
    _git(destination, 'config', 'user.email', 'auto-agents@localhost')
    if revision:
        _git(destination, 'fetch', '--no-tags', str(source), revision)
        _git(destination, 'checkout', '--detach', 'FETCH_HEAD')
    else:
        for relative in initial_paths:
            path = source / relative
            if path.is_file() or path.is_symlink():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target, follow_symlinks=False)
        # Only private metadata is written. Initial inputs become the retained
        # baseline, never part of the subsequent writer's candidate delta.
        snapshot = GateSnapshotManager(destination, 'initial-' + uuid4().hex).create()
        _git(destination, 'read-tree', snapshot.commit_sha)
        _git(destination, 'checkout', '--detach', snapshot.commit_sha)
        revision = snapshot.commit_sha
    install_dependency_links(destination, discover_dependency_links(source))
    return revision


@contextmanager
def execution_checkout(session, state):
    """Keep session control records durable while all product work is private."""
    if getattr(session, '_custody_control_root', None) is not None:
        yield
        return
    from .execution_binding import SessionExecutionBinding
    from .orchestrator import Orchestrator
    from .session_verification import validate_binding

    root = session.project_root
    if state.verification_binding:
        validate_binding(session, state)
    if not state.candidate_custody:
        if state.candidate_paths:
            raise ownership_error(state, 'candidate ownership is unavailable without a frozen receipt')
        revision = state.verification_binding['contract_revision']
        destination = root / '.auto-agents' / 'candidate-custody' / uuid4().hex / 'project'
        destination.parent.mkdir(parents=True)
        from .session_source import resolve_source
        source = resolve_source(root, state)
        code_revision = state.source_descriptor.get('revision', revision)
        revision = _clone(source, code_revision, destination)
        state.candidate_custody = {'schema_version': 1, 'checkout': str(destination),
            'repository': state.verification_binding['repository'], 'session_id': state.session_id,
            'binding_fingerprint': state.verification_binding['binding_fingerprint'],
            'source_id': state.source_descriptor.get('source_id', ''),
            'contract_revision': state.verification_binding['contract_revision'],
            'base_revision': revision, 'initial_source': not state.verification_binding['contract_revision'],
            'preimages': _inventory(destination)}
        session._save(state)
    custody = state.candidate_custody
    destination = Path(custody['checkout'])
    if (custody['session_id'] != state.session_id
            or custody['repository'] != (state.verification_binding.get('repository') or str(root.resolve()))
            or (state.verification_binding and custody['binding_fingerprint'] != state.verification_binding['binding_fingerprint'])):
        raise ownership_error(state, 'candidate custody conflicts with session authority')
    validate_receipt(state)
    context = (SessionExecutionBinding.for_checkout(session, state, destination)
               if state.verification_binding else None)
    previous = session.orch
    execution = Orchestrator(destination, agent_output_stream=previous.agent_output_stream,
                             user_input_fn=previous._user_input_fn)
    execution.adapter = previous.adapter
    # Instance-installed provider transports are also used by embedders.
    if '_call_with_failover' in previous.__dict__:
        execution._call_with_failover = previous.__dict__['_call_with_failover']
    execution.config = session.config
    execution._force_full_verify = previous._force_full_verify
    previous_context = getattr(session, '_execution_binding', None)
    session._custody_control_root = root
    session.project_root = destination
    session.orch = execution
    session._execution_binding = context
    try:
        yield
    finally:
        session._save(state)
        session.project_root = root
        session.orch = previous
        session._execution_binding = previous_context
        del session._custody_control_root


@contextmanager
def candidate_request(session, state, request):
    if request.purpose != 'fix' or not state.verification_binding:
        yield request
        return
    if not state.candidate_custody or Path(state.candidate_custody['checkout']) != session.project_root:
        raise ownership_error(state, 'bound writer requires its private execution checkout')
    source_head = _git(session.project_root, 'rev-parse', 'HEAD')
    yield replace(request, resume_session_id='', resume_provider='',
                  prompt_is_continuation=False, prompt_continuation='')
    if _git(session.project_root, 'rev-parse', 'HEAD') != source_head:
        raise ownership_error(state, 'isolated candidate changed its Git checkpoint')
    # Freeze before returning to ownership recording. Nothing is copied into
    # the shared worktree, including at the former publication boundary.
    after = _inventory(session.project_root)
    before = state.candidate_custody['preimages']
    absent = {'worktree': {'kind': 'absent'}, 'index': []}
    manifest = {path: {'preimage': before.get(path, absent), 'postimage': after.get(path, absent)}
                for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)}
    # Stage each replaced subtree once. Naming an indexed descendant below
    # its new regular-file ancestor is not a valid Git pathspec.
    snapshot_paths = [path for path in manifest
                      if not any(parent.as_posix() in manifest for parent in Path(path).parents)]
    snapshot = GateSnapshotManager(session.project_root, 'candidate-' + uuid4().hex).create(paths=snapshot_paths)
    receipt = {'attempt_id': uuid4().hex, 'attempt': state.current_attempt,
        'session_id': state.session_id, 'binding_fingerprint': state.verification_binding['binding_fingerprint'],
        'base_revision': state.candidate_custody['base_revision'],
        'source_revision': snapshot.commit_sha, 'manifest': manifest}
    receipt['fingerprint'] = fingerprint(receipt)
    session._candidate_receipt = receipt


def validate_receipt(state):
    custody = state.candidate_custody
    receipt = custody.get('receipt')
    if not receipt:
        if state.candidate_paths:
            raise ownership_error(state, 'candidate receipt is unavailable')
        return
    if (receipt.get('fingerprint') != fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'})
            or receipt['session_id'] != custody['session_id']
            or receipt['binding_fingerprint'] != custody['binding_fingerprint']):
        raise ownership_error(state, 'candidate receipt identity changed')
    expected_paths = {path: fingerprint(entry['postimage']) for path, entry in receipt['manifest'].items()}
    if state.candidate_paths != expected_paths:
        raise ownership_error(state, 'candidate paths differ from the writer receipt')
    root = Path(custody['checkout'])
    actual = _inventory(root)
    for path, entry in receipt['manifest'].items():
        postimage = entry['postimage']
        # Delivery intentionally commits the worktree, so its index then has
        # the delivered tree semantics. Before delivery the writer index is exact.
        if _image(root, path) != postimage['worktree'] or (
                not custody.get('delivered_revision') and actual.get(path, {}).get('index', []) != postimage['index']):
            raise ownership_error(state, 'private candidate changed after receipt', conflicting_paths=[path])
    _git(root, 'cat-file', '-e', receipt['source_revision'] + '^{commit}')


def record_receipt(session, state):
    receipt = getattr(session, '_candidate_receipt', None)
    if receipt is None:
        raise ownership_error(state, 'candidate writer did not return a frozen receipt')
    state.candidate_custody['receipt'] = receipt
    state.candidate_paths = {path: fingerprint(entry['postimage']) for path, entry in receipt['manifest'].items()}
    validate_receipt(state)
    session._candidate_receipt = None
    session._save(state)


def deliver_candidate(session, state, message):
    validate_receipt(state)
    custody = state.candidate_custody
    receipt = custody.get('receipt')
    if not receipt:
        raise ownership_error(state, 'candidate delivery requires a writer receipt')
    # The verified snapshot is a real commit in the private object database.
    # Give delivery its normal user-facing subject without changing its tree.
    tree = _git(session.project_root, 'rev-parse', receipt['source_revision'] + '^{tree}')
    revision = _git(session.project_root, 'commit-tree', tree, '-p', custody['base_revision'], '-m', message)
    custody['delivered_revision'] = revision
    _git(session.project_root, 'update-ref', 'refs/auto-agents/delivered/' + state.session_id, revision)
    session._save(state)
    return True


def completed_delivery(state):
    """A fix completion is durable in its verified private Git revision."""
    custody = state.candidate_custody
    receipt = custody.get('receipt')
    revision = custody.get('delivered_revision')
    if not receipt or not revision:
        return False
    validate_receipt(state)
    root = Path(custody['checkout'])
    if _git(root, 'rev-parse', revision + '^{tree}') != _git(root, 'rev-parse', receipt['source_revision'] + '^{tree}'):
        raise ownership_error(state, 'completed delivery differs from verified candidate')
    return True


def consume_delivery(root, state, delivery, *, child_id):
    """Materialize the child's revision for the parent's next public execution."""
    source = Path(delivery['checkout'])
    receipt = delivery['receipt']
    if (delivery['session_id'] != child_id or receipt['session_id'] != child_id
            or delivery['repository'] != str(root.resolve())
            or delivery['binding_fingerprint'] != receipt['binding_fingerprint']):
        raise ownership_error(state, 'delivered candidate belongs to another child or repository')
    if receipt['fingerprint'] != fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'}):
        raise ownership_error(state, 'delivered candidate receipt changed')
    revision = delivery['delivered_revision']
    if _git(source, 'rev-parse', revision + '^{tree}') != _git(source, 'rev-parse', receipt['source_revision'] + '^{tree}'):
        raise ownership_error(state, 'delivered revision differs from verified candidate')
    destination = root / '.auto-agents' / 'candidate-custody' / uuid4().hex / 'project'
    destination.parent.mkdir(parents=True)
    _clone(source, revision, destination)
    restore_private_modes(destination, {
        path: entry['postimage']['worktree']['mode']
        for path, entry in receipt['manifest'].items()
        if entry['postimage']['worktree']['kind'] in {'file', 'directory'}})
    state.lineage_changed_paths = sorted(set(state.lineage_changed_paths) | set(state.candidate_paths))
    state.candidate_paths = {}
    state.candidate_custody = {'schema_version': 1, 'checkout': str(destination),
        'repository': str(root.resolve()), 'session_id': state.session_id,
        'binding_fingerprint': state.verification_binding.get('binding_fingerprint', ''),
        'contract_revision': delivery.get('contract_revision', delivery['base_revision']),
        'base_revision': revision, 'preimages': _inventory(destination),
        'consumed_delivery': {'revision': revision, 'receipt_fingerprint': receipt['fingerprint']}}
