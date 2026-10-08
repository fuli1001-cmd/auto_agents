"""Private candidate custody: writer receipts never authorize shared copy-back."""
import base64
import errno
import io
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










def validate_receipt(state):
    custody = state.candidate_custody
    if state.verification_binding:
        validate_custody_binding(state)
    receipt = custody.get('receipt')
    if not receipt:
        if state.candidate_paths:
            raise ownership_error(state, 'candidate receipt is unavailable')
        if state.mode == 'fix':
            actual = _inventory(Path(custody['checkout']))
            before = custody['preimages']
            conflicts = sorted(path for path in actual.keys() | before.keys()
                               if actual.get(path) != before.get(path))
            if conflicts:
                raise ownership_error(state, 'private changes have no recorded writer receipt',
                                      conflicting_paths=conflicts)
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
    validate_source(state, receipt['source_revision'])


def _raw_git(root, *args, data=None):
    """Read immutable objects, never filtered worktree content or replace refs."""
    result = subprocess.run(['git', '--no-replace-objects', *args], cwd=root,
                            capture_output=True, input=data)
    if result.returncode:
        raise RuntimeError(os.fsdecode(result.stderr).strip())
    return result.stdout


def _tree(root, revision):
    # NUL framing preserves tabs, newlines and non-UTF8 path bytes.
    commit = _raw_git(root, 'rev-parse', '--verify', revision + '^{commit}').strip().decode('ascii')
    rows = _raw_git(root, 'ls-tree', '-rz', '--full-tree', commit)
    result = {}
    for row in rows.split(b'\0'):
        if not row:
            continue
        identity, path = row.split(b'\t', 1)
        mode, kind, oid = identity.decode('ascii').split()
        result[os.fsdecode(path)] = (mode, kind, oid)
    blobs = sorted({oid for _, kind, oid in result.values() if kind == 'blob'})
    contents = {}
    if blobs:
        stream = io.BytesIO(_raw_git(root, 'cat-file', '--batch',
                                    data=('\n'.join(blobs) + '\n').encode('ascii')))
        for oid in blobs:
            header = stream.readline().split()
            if len(header) != 3 or header[:2] != [oid.encode('ascii'), b'blob']:
                raise RuntimeError('candidate tree blob is unavailable')
            size = int(header[2])
            content = stream.read(size)
            if len(content) != size or stream.read(1) != b'\n':
                raise RuntimeError('candidate tree blob is incomplete')
            contents[oid] = content
    return {path: (mode, kind, contents[oid] if kind == 'blob' else oid)
            for path, (mode, kind, oid) in result.items()}


def _blob_identity(image):
    if image['kind'] == 'symlink':
        data, mode = os.fsencode(image['target']), '120000'
    elif image['kind'] == 'file':
        data = base64.b64decode(image['bytes'], validate=True)
        mode = '100755' if image['mode'] & 0o100 else '100644'
    else:
        raise ValueError('candidate source contains an unsupported file kind')
    return mode, 'blob', data


def _expected_tree(state):
    custody = state.candidate_custody
    receipt = custody['receipt']
    if receipt.get('base_revision') != custody['base_revision']:
        raise ownership_error(state, 'candidate receipt base differs from authenticated custody',
                              conflicting_paths=sorted(receipt['manifest']))
    root = Path(custody['checkout'])
    expected = _tree(root, custody['base_revision'])
    for path, entry in receipt['manifest'].items():
        # Validate paths even for deletions, without resolving symlink parents.
        if any(part in {'', '.', '..'} for part in path.split('/')):
            raise ownership_error(state, 'invalid candidate receipt path', conflicting_paths=[path])
        image = entry['postimage']['worktree']
        expected.pop(path, None)
        if image['kind'] not in {'absent', 'directory'}:
            expected[path] = _blob_identity(image)
    return expected


def validate_source(state, revision):
    """The complete source is the retained base overlaid with frozen postimages."""
    try:
        expected = _expected_tree(state)
        actual = _tree(Path(state.candidate_custody['checkout']), revision)
        conflicts = sorted(path for path in expected.keys() | actual.keys()
                           if expected.get(path) != actual.get(path))
        if conflicts:
            raise ownership_error(state, 'candidate source tree differs from frozen receipt',
                                  conflicting_paths=conflicts)
        return expected
    except (OSError, RuntimeError, ValueError) as error:
        from .session_verification import SessionOwnershipError
        if isinstance(error, SessionOwnershipError):
            raise
        raise ownership_error(state, 'candidate source tree is unavailable',
                              conflicting_paths=sorted(state.candidate_paths), detail=str(error)) from error


def _retained_gitlink_placeholder(state, root, path, identity):
    """Admit only an untouched, uninitialized gitlink, without traversing it."""
    manifest = state.candidate_custody['receipt']['manifest']
    if any(name == path or name.startswith(path + '/') for name in manifest):
        return False
    try:
        index = _raw_git(root, 'ls-files', '--stage', '-z', '--', ':(literal)' + path)
        if index != b'160000 ' + identity[2].encode('ascii') + b' 0\t' + os.fsencode(path) + b'\0':
            return False
        with anchored_parent(root, path) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=parent)
            try:
                return not os.listdir(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, RuntimeError, ValueError):
        return False


def validate_materialized_source(state, root, revision):
    """Check raw source correspondence and anchored, fully restored postimages."""
    expected = validate_source(state, revision)
    conflicts = set()
    try:
        for path, identity in expected.items():
            if identity[:2] == ('160000', 'commit'):
                if not _retained_gitlink_placeholder(state, root, path, identity):
                    # Stop before general inventory can inspect an invalid
                    # submodule representation or anything below it.
                    conflicts.add(path)
                    raise ownership_error(state, 'retained gitlink materialization changed',
                                          conflicting_paths=[path])
                continue
            image = _image(root, path)
            if image['kind'] not in {'file', 'symlink'} or _blob_identity(image) != identity:
                conflicts.add(path)
        for path, entry in state.candidate_custody['receipt']['manifest'].items():
            if _image(root, path) != entry['postimage']['worktree']:
                conflicts.add(path)
        # Preserve runtime/dependency exclusions for generated files only.
        # Every Git entry above is checked, even inside an excluded directory.
        from .gate_execution import repository_exclusion_paths, GATE_SNAPSHOT_RUNTIME_PATHS
        excluded = repository_exclusion_paths(root, surface_paths=GATE_SNAPSHOT_RUNTIME_PATHS)
        for path in _inventory(root):
            if any(path == item or path.startswith(item + '/') for item in excluded):
                continue
            if _image(root, path)['kind'] not in {'absent', 'directory'} and path not in expected:
                conflicts.add(path)
    except (OSError, RuntimeError, ValueError) as error:
        from .session_verification import SessionOwnershipError
        if isinstance(error, SessionOwnershipError):
            raise
        raise ownership_error(state, 'candidate materialization could not be checked',
                              conflicting_paths=sorted(conflicts), detail=str(error)) from error
    if conflicts:
        raise ownership_error(state, 'candidate materialization differs from frozen receipt',
                              conflicting_paths=sorted(conflicts))


def admit_fresh_materialization(state):
    """Saved success still requires a fresh checkout, including smudge effects."""
    validate_receipt(state)
    receipt = state.candidate_custody['receipt']
    with tempfile.TemporaryDirectory(prefix='auto-agents-receipt-') as runtime:
        destination = Path(runtime) / 'project'
        _clone(Path(state.candidate_custody['checkout']), receipt['source_revision'], destination)
        restore_receipt_modes(state, destination)
        validate_materialized_source(state, destination, receipt['source_revision'])


def restore_receipt_modes(state, root):
    try:
        restore_private_modes(root, {path: entry['postimage']['worktree']['mode']
            for path, entry in state.candidate_custody['receipt']['manifest'].items()
            if entry['postimage']['worktree']['kind'] in {'file', 'directory'}})
    except (OSError, RuntimeError, ValueError) as error:
        raise ownership_error(state, 'candidate permissions could not be restored',
                              conflicting_paths=sorted(state.candidate_paths), detail=str(error)) from error














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
    validate_source(state, revision)
    if _git(root, 'rev-parse', revision + '^{tree}') != _git(root, 'rev-parse', receipt['source_revision'] + '^{tree}'):
        raise ownership_error(state, 'completed delivery differs from verified candidate')
    return True
