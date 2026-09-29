"""Retain in-project plan amendments for independent review, not automatic delivery."""
import base64
from copy import deepcopy
import hashlib
import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from .model import Event, digest, require
from .convergence import scope, owner


def product_paths(paths):
    return bool(paths) and all(isinstance(path, str) and path and not PurePosixPath(path).is_absolute()
        and not any(part in {'..', '.', '.git', '.auto-agents', '.codex', '.agents'} for part in PurePosixPath(path).parts)
        and PurePosixPath(path).name not in {'AGENTS.md', 'CLAUDE.md', '.env', 'credentials.json', 'auth.json'}
        and not PurePosixPath(path).name.startswith('.env.') for path in paths)


def note(store, stream, command_id, paths):
    snapshot = store.load(stream)
    command = snapshot['commands'][command_id]
    data = {'command_id': command_id, 'paths': sorted(set(paths)),
            'source': command['outcome']['details']['post_source'],
            'result_ref': command['outcome']['details']['native_result']}
    store.apply(stream, snapshot['revision'], Event('scope-change:' + command_id, 'recovery_scope_change_requested', data))


def pending(snapshot, task_id):
    return sorted(scope(snapshot, task_id).get('scope_changes', {}))


def schema(base, paths):
    if not paths: return base
    value = deepcopy(base)
    value['properties']['scope_coverage'] = {'type': 'array', 'items': {'type': 'object',
        'properties': {'path': {'type': 'string', 'enum': paths}, 'reason': {'type': 'string'},
                       'evidence': {'type': 'string'}},
        'required': ['path', 'reason', 'evidence'], 'additionalProperties': False}}
    value['required'].append('scope_coverage')
    return value


def approval(value, paths, source):
    if not paths or value.get('decision') != 'APPROVE': return None
    rows = value.get('scope_coverage')
    require(isinstance(rows, list) and len(rows) == len(paths)
            and all(isinstance(row, dict) and row.get('path') in paths
                    and all(isinstance(row.get(key), str) and row[key].strip() for key in ('reason', 'evidence'))
                    for row in rows) and {row['path'] for row in rows} == set(paths),
            'protocol_invalid', 'Scope amendment needs independent necessity evidence for every added path')
    return {'source': source, 'paths': paths, 'coverage': rows}


def _receipt_source(state):
    """Reconstruct the pre-call fingerprint from immutable custody, including dependency links."""
    from ..session_candidate import _tree, _image, _blob_identity, _git
    from ..gate_execution import discover_dependency_links
    custody = state.candidate_custody
    root = Path(custody['checkout'])
    require(_git(root, 'rev-parse', 'HEAD') == custody['base_revision'],
            'ownership_conflict', 'Retained writer changed its Git checkpoint')
    tree = _tree(root, custody['base_revision'])
    expected = deepcopy(custody['preimages'])
    expected.update({path: entry['postimage'] for path, entry in custody['receipt']['manifest'].items()})
    for path in discover_dependency_links(root):
        index = [row.split('\t', 1)[0] for row in _git(root, 'ls-files', '--stage', '-z', '--', path).split('\0') if '\t' in row]
        expected[path] = {'worktree': _image(root, path), 'index': index}
    changes = []
    for path, entry in expected.items():
        image = entry['worktree']
        if path.startswith(('.auto-agents/', '.antigravitycli/')) or image['kind'] == 'directory': continue
        blob = None if image['kind'] == 'absent' else _blob_identity(image)
        base, index = tree.get(path), []
        if base and base[1] == 'blob':
            index = [base[0] + ' ' + hashlib.sha1(b'blob ' + str(len(base[2])).encode() + b'\0' + base[2]).hexdigest() + ' 0']
        if base != blob or entry['index'] != index:
            changes.append((not bool(entry['index'] or base), path, image))
    result = hashlib.sha256()
    for _, path, image in sorted(changes):
        result.update(os.fsencode(path) + b'\0')
        if image['kind'] == 'symlink': result.update(b'symlink\0' + os.fsencode(image['target']))
        elif image['kind'] == 'file':
            result.update(b'executable\0' if image['mode'] & 0o100 else b'file\0')
            result.update(base64.b64decode(image['bytes'], validate=True))
        else: result.update(b'[missing]')
        result.update(b'\0')
    return digest([custody['base_revision'], result.hexdigest()])


def recover_writer(project, state):
    """Recover a completed writer rejected only for exceeding its diagnostic plan.

Both old receipt bytes and current bytes must match the recorded call. All
ownership conflicts outside this exact case remain blocked.
"""
    if state.mode != 'fix' or state.resolution != 'kernel_ownership_conflict' or not state.candidate_custody.get('receipt'):
        return False
    from .authority import installed
    store = installed(project)
    stream = store.binding(project, 'session:' + state.session_id) if store else None
    if not stream: return False
    snapshot = store.load(stream)
    if 'recovery' not in snapshot: return False
    rows = [command for command in snapshot['commands'].values() if command['phase'] == 'implement'
            and command['status'] == 'finished' and owner(snapshot, command['task_id']) == 'fix:' + state.session_id]
    if not rows: return False
    command = max(rows, key=lambda row: row['sequence'])
    outcome = command.get('outcome') or {}
    if outcome.get('kind') != 'ownership_conflict' or not outcome.get('reason', '').startswith('Correction exceeded approved paths: '):
        return False
    require(not any(row['status'] in {'running', 'unknown', 'reserved'} for row in snapshot['commands'].values()),
            'outcome_unknown', 'Unsettled operations must finish before writer custody recovery')
    from ..session_source import validate_checkout
    validate_checkout(project, state, Path(state.candidate_custody['checkout']))
    from ..session_candidate import _inventory, record_receipt, freeze_writer_receipt, validate_source
    from ..session_verification import fingerprint
    from ..execution_binding import validate_custody_binding
    from ..config import save_session_state
    from .native import _source
    root = Path(state.candidate_custody['checkout'])
    require(_source(SimpleNamespace(project_root=root, _recovery_policy_active=True), state)
            == outcome['details']['post_source'], 'ownership_conflict', 'Private bytes changed after the retained writer')
    reply = store.read(outcome['details']['native_result'])
    require(reply.get('ok') is True and not reply.get('cleanup_incomplete') and (reply.get('summary') or reply.get('stdout')),
            'ownership_conflict', 'Retained writer has no complete result')
    previous = next((row for row in state.execution_log if row.get('action') == 'receipt_writer_result'
                     and row.get('recovered_command') == command['command_id']), None)
    if previous:
        note(store, stream, command['command_id'], previous['scope_changes'])
        return True
    validate_custody_binding(state)
    receipt = state.candidate_custody['receipt']
    require(receipt['fingerprint'] == fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'}),
            'ownership_conflict', 'Previous receipt identity changed')
    validate_source(state, receipt['source_revision'])
    require(_receipt_source(state) == command['source'], 'ownership_conflict', 'Writer input does not match previous custody')
    before = deepcopy(state.candidate_custody['preimages'])
    before.update({path: row['postimage'] for path, row in receipt['manifest'].items()})
    from .policy import correction_paths
    paths = correction_paths(snapshot, command['task_id'], before, _inventory(root))
    require(product_paths(paths), 'ownership_conflict', 'Scope change crosses a protected boundary')
    recorder = SimpleNamespace(_candidate_receipt=freeze_writer_receipt(root, state, state.candidate_custody['base_revision']),
        _candidate_writer_result={'ok': True, 'reply': reply.get('summary') or reply['stdout'],
                                  'recovered_command': command['command_id'], 'scope_changes': paths},
        _save=lambda value: save_session_state(project, value))
    record_receipt(recorder, state)
    note(store, stream, command['command_id'], paths)
    return True
