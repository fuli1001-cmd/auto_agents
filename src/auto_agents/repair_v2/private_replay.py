"""Freeze registered private Git inputs without mounting the live checkout."""
import json
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

from .evidence import identity
from .store import atomic_json, digest
from .workspace import git
from .types import RepairBlocked


def prepare(root, target, payload):
    from ..execution_binding import route_sources
    from ..session_verification import fingerprint
    root, target = Path(root), Path(target)
    project = str(Path(payload['project']).resolve())
    pending = [payload.get('invocation', {}).get('engine_route') or {}]
    ids, seen = set(), set()
    while pending:
        for source in route_sources(pending.pop()):
            if source.get('child_session_id'): ids.add(source['child_session_id'])
            for key in ('failed_handoff_id', 'original_handoff_id', 'resume_handoff_id'):
                item = source.get(key)
                if not item or item in seen: continue
                seen.add(item)
                if not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', item):
                    raise RepairBlocked('replay_private_source', 'Invalid retained handoff')
                handoff = json.loads((target / '.auto-agents/state/handoffs' / (item + '.json')).read_text())
                child = handoff.get('child') or {}
                if child.get('kind') == 'fix': ids.add(child['native_id'])
                pending.append(handoff.get('payload') or {})
    result = []
    for child_id in sorted(ids):
        if not isinstance(child_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', child_id):
            raise RepairBlocked('replay_private_source', 'Invalid retained child')
        child = json.loads((target / '.auto-agents/state/sessions' / child_id / 'session_state.json').read_text())
        source = child.get('source_descriptor') or {}
        if not source: continue
        source_id = source.get('source_id')
        if source_id != fingerprint({k:v for k,v in source.items() if k != 'source_id'}):
            raise RepairBlocked('replay_private_source', 'Private source descriptor changed')
        stored = json.loads((target / '.auto-agents/state/sources' / (source_id + '.json')).read_text())
        handoff_id = child.get('parent_handoff_id')
        if not isinstance(handoff_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', handoff_id):
            raise RepairBlocked('replay_private_source', 'Invalid private source handoff')
        handoff = json.loads((target / '.auto-agents/state/handoffs' / (handoff_id + '.json')).read_text())
        if (handoff.get('payload', {}).get('source_descriptor') != source
                or handoff.get('child', {}).get('native_id') != child_id
                or source.get('handoff_id') != handoff_id
                or child.get('workflow_id') != source.get('workflow_id')
                or fingerprint(child.get('authorization_policy')) != source.get('authorization_fingerprint')):
            raise RepairBlocked('replay_private_source', 'Private source is not authorized by the retained child')
        checkout = Path(source['checkout'])
        registration = json.loads((target / '.auto-agents/state/custody' / (fingerprint(str(checkout)) + '.json')).read_text())
        if checkout.resolve() != checkout or checkout.is_relative_to(Path(project)) or not (checkout / '.git').is_dir():
            raise RepairBlocked('replay_private_source', 'Registered private source is unavailable')
        info = checkout.stat()
        if (stored != source or source.get('repository') != project or registration != {
                'repository': project, 'session_id': source['session_id'], 'checkout': str(checkout),
                'device': info.st_dev, 'inode': info.st_ino}):
            raise RepairBlocked('replay_private_source', 'Registered private source identity changed')
        if git(checkout, 'rev-parse', source['revision'] + '^{tree}') != source['tree']:
            raise RepairBlocked('replay_private_source', 'Private source tree changed')
        git(checkout, 'cat-file', '-e', source['contract_revision'] + '^{commit}')
        key = digest(source); cache = root / key; marker = cache / 'inputs.json'
        root.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            from .storage import require_space, tree_bytes
            require_space(root, tree_bytes(checkout / '.git') * 2)
            with tempfile.TemporaryDirectory(prefix='private-input-', dir=root) as temporary:
                staging = Path(temporary)
                repo = staging / 'source.git'
                subprocess.run(['git', 'clone', '-q', '--bare', '--no-local', '--no-hardlinks', str(checkout), str(repo)],
                               check=True, capture_output=True)
                git(repo, 'fetch', '-q', '--no-tags', str(checkout), source['revision'], source['contract_revision'])
                (repo / 'config').write_text('[core]\n repositoryformatversion = 0\n bare = true\n')
                git(repo, 'fsck', '--full', '--no-reflogs')
                atomic_json(staging / 'inputs.json', {'source': source, 'digest': identity(repo)})
                staging.rename(cache)
        record = json.loads(marker.read_text()); repo = cache / 'source.git'
        if record['source'] != source or record['digest'] != identity(repo) or checkout.stat().st_ino != info.st_ino:
            raise RepairBlocked('replay_private_source', 'Private Git snapshot changed')
        result.append({'kind': 'private-source-git', 'source_id': source_id, 'checkout': str(checkout),
            'revision': source['revision'], 'contract_revision': source['contract_revision'], 'tree': source['tree'],
            'root': str(repo), 'prefix': '/opt/repair/private-sources/' + source_id, 'digest': record['digest']})
    return result
