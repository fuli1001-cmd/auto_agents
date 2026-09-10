"""Private candidate custody: writer receipts never authorize shared copy-back."""
import base64
import errno
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import os
import stat
import subprocess
import tempfile
from uuid import uuid4

from .gate_execution import GateSnapshotManager, discover_dependency_links, install_dependency_links
from .session_verification import fingerprint, ownership_error, product_path
from .execution_binding import anchored_parent, restore_private_modes, validate_custody_binding


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
    initial_images = {}
    if not revision:
        from .git_ops import head_ref
        if head_ref(source):
            raise RuntimeError('initial candidate source requires an unborn repository')
        # Enumerate before creating storage inside the source tree. An unborn
        # standalone session has live initial inputs but no commit to fetch.
        initial_paths = {path for path in _git(source, 'ls-files', '--cached', '--others',
                                              '--exclude-standard', '-z').split('\0') if path}
        initial_paths.update(parent.as_posix() for path in list(initial_paths)
                             for parent in Path(path).parents if parent != Path('.'))
        # Capture through directory descriptors before materializing anything.
        # The index may still name descendants of a replaced directory; those
        # names must never cause a read through its new symlink target.
        initial_images = {path: _image(source, path) for path in sorted(initial_paths)}
    _git(source, 'clone', '--no-local', '--no-checkout', str(source), str(destination))
    _git(destination, 'config', 'user.name', 'auto_agents')
    _git(destination, 'config', 'user.email', 'auto-agents@localhost')
    if revision:
        _git(destination, 'fetch', '--no-tags', str(source), revision)
        _git(destination, 'checkout', '--detach', 'FETCH_HEAD')
    else:
        for relative, entry in initial_images.items():
            if entry['kind'] == 'absent':
                continue
            with anchored_parent(destination, relative) as (parent, name):
                if entry['kind'] == 'directory':
                    os.mkdir(name, dir_fd=parent)
                elif entry['kind'] == 'symlink':
                    os.symlink(entry['target'], name, dir_fd=parent)
                elif entry['kind'] == 'file':
                    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=parent)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(base64.b64decode(entry['bytes']))
                        os.fchmod(stream.fileno(), entry['mode'])
                else:
                    raise RuntimeError('initial candidate source contains an unsupported entry kind')
        # Only private metadata is written. Initial inputs become the retained
        # baseline, never part of the subsequent writer's candidate delta.
        snapshot = GateSnapshotManager(destination, 'initial-' + uuid4().hex).create()
        _git(destination, 'read-tree', snapshot.commit_sha)
        _git(destination, 'checkout', '--detach', snapshot.commit_sha)
        restore_private_modes(destination, {
            path: entry['mode'] for path, entry in reversed(list(initial_images.items()))
            if entry['kind'] in {'file', 'directory'}})
        revision = snapshot.commit_sha
    install_dependency_links(destination, discover_dependency_links(source))
    return revision


def _runtime_checkout(root, state, source, revision):
    from .session_source import register_checkout
    runtime = Path(tempfile.gettempdir()).resolve()
    if runtime.is_relative_to(Path(root).resolve()):
        raise ownership_error(state, 'candidate runtime storage must be outside the shared repository')
    destination = Path(tempfile.mkdtemp(prefix='auto-agents-candidate-', dir=runtime)) / 'project'
    revision = _clone(source, revision, destination)
    register_checkout(root, state, destination)
    return destination, revision


@contextmanager
def execution_checkout(session, state):
    """Keep session control records durable while all product work is private."""
    if getattr(session, '_custody_control_root', None) is not None:
        yield
        return
    from .execution_binding import SessionExecutionBinding
    from .orchestrator import Orchestrator
    from .session_verification import bind_session

    root = session.project_root
    if state.verification_binding:
        bind_session(session, state)
    if not state.candidate_custody:
        if state.candidate_paths:
            raise ownership_error(state, 'candidate ownership is unavailable without a frozen receipt')
        revision = state.verification_binding['contract_revision']
        from .session_source import resolve_source
        source = resolve_source(root, state)
        code_revision = state.source_descriptor.get('revision', revision)
        destination, revision = _runtime_checkout(root, state, source, code_revision)
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
    from .session_source import validate_checkout
    validate_checkout(root, state, destination)
    if state.verification_binding:
        validate_custody_binding(state)
    elif custody['session_id'] != state.session_id or custody['repository'] != str(root.resolve()):
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
        if state.mode == 'fix' and custody.get('receipt'):
            previous_state = session._current_state
            session._current_state = state
            try:
                recover_receipt(session, state)
            finally:
                session._current_state = previous_state
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
    from .verification_sandbox import candidate_writer_boundary
    with candidate_writer_boundary(session.project_root, state) as boundary:
        yield replace(request, resume_session_id='', resume_provider='',
                      prompt_is_continuation=False, prompt_continuation='',
                      writer_boundary=boundary)
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
    if state.verification_binding:
        validate_custody_binding(state)
    receipt = custody.get('receipt')
    if not receipt:
        if state.candidate_paths:
            raise ownership_error(state, 'candidate receipt is unavailable')
        return
    accepted_bindings = {custody['binding_fingerprint'],
                         state.verification_binding.get('binding_fingerprint')}
    # validate_custody_binding above authenticates the exact retained receipt,
    # including a writer created between successive inventory upgrades.
    bridge = custody.get('binding_migration', {})
    if state.verification_binding and receipt == bridge.get('receipt'):
        accepted_bindings.add(receipt.get('binding_fingerprint'))
    if (receipt.get('fingerprint') != fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'})
            or receipt['session_id'] != custody['session_id']
            or receipt['binding_fingerprint'] not in accepted_bindings):
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


def verification_identity(session, state):
    """Relevant private evidence, independent of resume epochs and shared edits."""
    with session._session_verification_config():
        from .workers import gate_environment_fingerprint
        gates = session.config.gates
        environment = gate_environment_fingerprint(
            isolation_mode=gates.isolation.mode, environment_id=gates.distributed.mode,
            distributed=gates.distributed.enabled,
            extra_denylist=gates.distributed.extra_environment_denylist,
            project_root=session.project_root)
        from .session_verification import selected_requirement_contracts
        plan, commands = session._verification_plan_commands()
        contracts = {proof['requirement_id']: current for _, proof, current in
                     selected_requirement_contracts(session, state, commands, metadata=plan.metadata)}
        receipt = state.candidate_custody['receipt']
        return fingerprint(['retained-config-contracts-v2', contracts, commands, receipt['fingerprint'], receipt['source_revision'],
            state.verification_binding, state.fix_verify_command, state.full_verify,
            environment])


def record_verification(session, state, result, *, identity=None):
    if not state.candidate_custody.get('receipt'):
        return
    from copy import deepcopy
    state.execution_log.append({'action': 'receipt_verification',
        'identity': identity or verification_identity(session, state),
        'receipt_fingerprint': state.candidate_custody['receipt']['fingerprint'],
        'binding_fingerprint': state.verification_binding['binding_fingerprint'],
        'verification': deepcopy(result)})
    session._save(state)


def recover_receipt(session, state):
    """Verify an interrupted writer's exact candidate before retry or delivery."""
    from .session_verification import validate_selected_contracts
    receipt = state.candidate_custody['receipt']
    with session._session_verification_config():
        plan, commands = session._verification_plan_commands()
        validate_selected_contracts(session, state, commands, metadata=plan.metadata)
        identity = verification_identity(session, state)
    retained = next((entry for entry in reversed(state.execution_log)
        if entry.get('action') == 'receipt_verification' and entry.get('identity') == identity), None)
    if retained is None:
        # Legacy pass logs and delivered revisions do not attest this inventory.
        # A changed environment reopens diagnostics for the same candidate.
        state.verification_diagnostics = {}
        with session._session_verification_context():
            session._ensure_baseline(state)
        result = session._run_verify()
        session._append_verification_log(state, 'inventory_migration_verify', result)
        record_verification(session, state, result, identity=identity)
    else:
        result = retained['verification']
    if result['ok']:
        if completed_delivery(state) and any(
                entry.get('action') == 'receipt_completion' and entry.get('identity') == identity
                and entry.get('delivered_revision') == state.candidate_custody['delivered_revision']
                for entry in state.execution_log):
            state.status, state.resolution = 'completed', 'fixed'
            session._save(state)
            return
        writer = next((entry for entry in reversed(state.execution_log)
            if entry.get('action') == 'receipt_writer_result'
            and entry.get('receipt_fingerprint') == receipt['fingerprint']), None)
        if writer is not None:
            reply = writer['reply'] if writer['ok'] else ''
        else:
            # The pre-receipt protocol retained successful writer replies here.
            reply = next((entry.get('result', '') for entry in reversed(state.execution_log)
                if entry.get('action') == 'fix' and entry.get('attempt') == receipt['attempt']), '')
            if len(reply) >= 500:
                reply = ''  # Truncated legacy text cannot establish the full disposition.
        disposition, error = session._parse_fix_disposition(reply)
        if (not reply or error or (disposition and disposition.get('decision') == 'run_iteration')
                or session._apply_session_persistence_marker(state, reply)
                or session._session_persistence_issue(state)):
            raise ownership_error(state, 'retained writer disposition or persistence evidence is unavailable')
        session._complete_verified_fix(state, result, reply, identity=identity)
        return
    if result.get('retry_fix') is False:
        state.status = 'blocked'
        state.resolution = 'verification_inconclusive'
        session._save(state)
        return
    stop = session._should_stop(state, str(result.get('reason', 'verification failed')))
    if stop:
        state.status = 'failed'
        session._record_terminal_stop(state)
        session._save(state)
        return
    # Admission and the ordinary writer boundary still govern a retry. Keep
    # the receipt and any old delivery until a new writer receipt is recorded.
    session._receipt_retry_feedback = session.orch._format_retry_feedback(
        'local_verification', reason=str(result.get('reason', 'verification failed')))
    state.status, state.resolution = 'executing', ''
    session._save(state)


def record_receipt(session, state):
    receipt = getattr(session, '_candidate_receipt', None)
    if receipt is None:
        raise ownership_error(state, 'candidate writer did not return a frozen receipt')
    previous = state.candidate_custody.get('receipt')
    if previous and previous != receipt:
        from copy import deepcopy
        state.execution_log.append({'action': 'candidate_superseded',
            'receipt': deepcopy(previous),
            'delivered_revision': state.candidate_custody.get('delivered_revision', ''),
            'binding_fingerprint': state.verification_binding['binding_fingerprint']})
        state.candidate_custody.pop('delivered_revision', None)
    state.candidate_custody['receipt'] = receipt
    writer = getattr(session, '_candidate_writer_result', None)
    if writer is not None:
        state.execution_log.append({'action': 'receipt_writer_result',
            'receipt_fingerprint': receipt['fingerprint'], **writer})
        session._candidate_writer_result = None
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
    if completed_delivery(state):
        return True
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
    for entry in reversed(state.execution_log):
        if (entry.get('action') == 'receipt_verification'
                and entry.get('receipt_fingerprint') == receipt['fingerprint']
                and entry.get('binding_fingerprint') == state.verification_binding.get('binding_fingerprint')):
            if not entry['verification']['ok']:
                return False
            break
    validate_receipt(state)
    root = Path(custody['checkout'])
    if _git(root, 'rev-parse', revision + '^{tree}') != _git(root, 'rev-parse', receipt['source_revision'] + '^{tree}'):
        raise ownership_error(state, 'completed delivery differs from verified candidate')
    return True


def consume_delivery(root, state, delivery, *, child_id):
    """Materialize the child's revision for the parent's next public execution."""
    from .config import load_session_state
    child = load_session_state(root, child_id)
    if child.status != 'completed' or not completed_delivery(child) or child.candidate_custody != delivery:
        raise ownership_error(state, 'delivery is not eligible under the retained child verification')
    source = Path(delivery['checkout'])
    receipt = delivery['receipt']
    receipt_binding = delivery['binding_fingerprint']
    if delivery.get('binding_migration'):
        from .config import load_session_state
        child = load_session_state(root, child_id)
        if child.candidate_custody != delivery:
            raise ownership_error(state, 'delivered migration differs from retained child custody')
        validate_receipt(child)
        receipt_binding = receipt['binding_fingerprint']
    if (delivery['session_id'] != child_id or receipt['session_id'] != child_id
            or delivery['repository'] != str(root.resolve())
            or receipt_binding != receipt['binding_fingerprint']):
        raise ownership_error(state, 'delivered candidate belongs to another child or repository')
    if receipt['fingerprint'] != fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'}):
        raise ownership_error(state, 'delivered candidate receipt changed')
    revision = delivery['delivered_revision']
    if _git(source, 'rev-parse', revision + '^{tree}') != _git(source, 'rev-parse', receipt['source_revision'] + '^{tree}'):
        raise ownership_error(state, 'delivered revision differs from verified candidate')
    from .session_source import validate_checkout
    validate_checkout(root, state, source, owner_id=child_id)
    destination, _ = _runtime_checkout(root, state, source, revision)
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
