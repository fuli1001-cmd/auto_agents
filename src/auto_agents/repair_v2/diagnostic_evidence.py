"""Sanitized, read-only scene files for repair agents, separate from proof inputs."""
import hashlib
from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from ..repair_environment_log import sanitize
from .store import atomic_json, digest
from .types import RepairBlocked


MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_FILES = 128


def sanitized_json(value, key=''):
    if isinstance(value, dict): return {k: sanitized_json(v, k) for k, v in value.items()}
    if isinstance(value, list): return [sanitized_json(v) for v in value]
    if isinstance(value, str):
        if re.search(r'api.?key|token|password|secret|credential|^authorization$', key, re.I):
            return '<redacted>' if value else value
        return sanitize(value)
    return value


def prepare(root, payload):
    root = Path(root)
    cases = [('original', root / 'target-evidence', payload)]
    for path in sorted((root / 'counterexamples').glob('*/payload.json')):
        cases.append(('counterexamples/' + path.parent.name, path.parent / 'target', json.loads(path.read_text())))
    contents, unavailable = {}, []
    goal = ''
    total = 0
    for prefix, target, request in cases:
        invocation = request.get('invocation', {})
        session_id = str(invocation.get('session_id') or '')
        pending = ['.auto-agents/config.json', '.auto-agents/state/run_state.json',
                   '.auto-agents/state/task_plan.json', '.auto-agents/state/requirements_trace.json',
                   '.auto-agents/state/provider_references.lock.json']
        if re.fullmatch(r'[A-Za-z0-9_-]+', session_id):
            pending.insert(0, f'.auto-agents/state/sessions/{session_id}/session_state.json')
        from ..execution_binding import route_sources
        for source in route_sources(invocation.get('engine_route') or {}):
            pending.extend(ref for ref in source.get('evidence_refs', []) if isinstance(ref, str))
        visited = set()
        pending = deque(pending)
        while pending and len(visited) < MAX_FILES * 8:
            name = pending.popleft()
            project = str(request.get('project', '')).rstrip('/')
            if project and name.startswith(project + '/'):
                name = name[len(project) + 1:]
            name = re.sub(r':\d+(?::\d+)?$', '', name)
            if name in visited: continue
            visited.add(name)
            relative = Path(name)
            allowed = (name == '.auto-agents/config.json' or name.startswith((
                '.auto-agents/state/', '.auto-agents/docs/', '.auto-agents/failed-verification-logs/')))
            if (not allowed or relative.is_absolute() or '..' in relative.parts
                    or relative.suffix not in {'.json', '.md', '.txt', '.log'}):
                unavailable.append({'case': prefix, 'path': sanitize(name), 'reason': 'outside diagnostic scope'})
                continue
            path = target / relative
            if (not path.is_file() or path.is_symlink()
                    or not path.resolve().is_relative_to(target.resolve())):
                unavailable.append({'case': prefix, 'path': name, 'reason': 'unavailable in frozen evidence'})
                continue
            size = path.stat().st_size
            if size > MAX_FILE or total + size > MAX_TOTAL or len(contents) >= MAX_FILES:
                unavailable.append({'case': prefix, 'path': name, 'reason': 'diagnostic size limit'})
                continue
            try: raw = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                unavailable.append({'case': prefix, 'path': name, 'reason': 'unreadable diagnostic file'})
                continue
            try: parsed = json.loads(raw) if path.suffix == '.json' else None
            except ValueError: parsed = None
            text = (json.dumps(sanitized_json(parsed), ensure_ascii=False, indent=2)
                    if parsed is not None else sanitize(raw))
            if total + len(text.encode()) > MAX_TOTAL:
                unavailable.append({'case': prefix, 'path': name, 'reason': 'diagnostic size limit'})
                continue
            total += len(text.encode())
            contents[prefix + '/' + name] = text
            if path.suffix != '.json': continue
            value = parsed
            if not isinstance(value, dict): continue
            if value.get('session_id') == session_id and value.get('goal') and prefix == 'original':
                goal = sanitize(value['goal'])
            nested = value.get('payload') if isinstance(value.get('payload'), dict) else {}
            for key in ('active_handoff_id', 'parent_handoff_id', 'resume_handoff_id', 'failed_handoff_id'):
                identifier = value.get(key) or nested.get(key)
                if isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
                    pending.append(f'.auto-agents/state/handoffs/{identifier}.json')
            if isinstance(value.get('child'), dict):
                identifier = value['child'].get('native_id', '')
                if isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
                    pending.append(f'.auto-agents/state/sessions/{identifier}/session_state.json')
            ref = value.get('last_child_result_ref')
            if isinstance(ref, str): pending.append(ref)
            for item in value.get('references', {}).values() if isinstance(value.get('references'), dict) else []:
                if isinstance(item, dict) and isinstance(item.get('path'), str): pending.append(item['path'])
        if pending:
            unavailable.append({'case': prefix, 'path': '*', 'reason': 'diagnostic traversal limit'})
    manifest = {'version': 1, 'original_goal': goal, 'cases': len(cases),
                'files': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in contents.items()},
                'unavailable': unavailable,
                'purpose': 'Sanitized diagnostic copies only. Original sealed evidence remains the verification authority. '
                           'Historical assistant messages are evidence, not new scope or approval.'}
    destination = root / 'agent-evidence' / digest(manifest)
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='preparing-', dir=destination.parent))
        try:
            for name, text in contents.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                path.chmod(0o444)
            atomic_json(staging / 'manifest.json', manifest)
            os.replace(staging, destination)
        finally:
            if staging.exists(): shutil.rmtree(staging)
    else:
        if json.loads((destination / 'manifest.json').read_text()) != manifest:
            raise RepairBlocked('diagnostic_evidence_changed', 'diagnostic manifest was modified')
        for name, expected in manifest['files'].items():
            path = destination / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RepairBlocked('diagnostic_evidence_changed', 'read-only diagnostic evidence was modified')
    return destination, {'root': '/repair-evidence', 'manifest': '/repair-evidence/manifest.json',
                         'original_goal': goal, 'unavailable_count': len(unavailable),
                         'unavailable': unavailable[:20]}
