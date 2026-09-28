"""Freeze working-tree bytes without changing the user's index or branches."""
import json
import os
from pathlib import Path
import subprocess

from .model import digest, require


def inventory(source):
    from ..repair_v2.workspace import inventory as files
    result = files(source)
    for name in list(result):
        parts = Path(name).parts
        local = any(p in {'.auto-agents', '.venv', '.conda', '__pycache__', 'node_modules'} for p in parts)
        secret = Path(name).name in {'.env', '.env.local', 'credentials.json', 'auth.json'}
        secret |= Path(name).name.startswith('.env.') and not Path(name).name.endswith(('.example', '.sample', '.template'))
        if local or secret: del result[name]
        elif result[name][0] == 'link':
            require((Path(source) / name).resolve().is_relative_to(Path(source).resolve()),
                    'runtime_source', 'Runtime source symlink leaves the repository', path=name)
    return result


def source_identity(source):
    return digest(inventory(source))


def capture(store, source, *, expected=None):
    from ..repair_v2.workspace import git, overlay
    from ..repair_v2.runtime_artifact import build, verify
    from ..repair_v2.storage import require_space
    from .runtime_lifecycle import temporary, produced
    source = Path(source).resolve()
    before = inventory(source)
    identity = digest(before)
    require(expected is None or expected == identity, 'source_changed', 'Source changed before capture')
    # Reuse a compatible old-format snapshot as well as our deterministic ones.
    for manifest in (store.root / 'kernel-releases/runtime-artifacts').glob('*/manifest.json'):
        value = json.loads(manifest.read_text())
        if value.get('source') == identity and value.get('environments') == {'kernel_schema': 1, 'rpc': 2}:
            artifact = {**value, 'path': str(manifest.parent / 'source'), 'version': 1}
            verify(artifact)
            produced(store.root, artifact)
            return artifact
    require_space(store.root, sum((source / name).stat().st_size for name, row in before.items() if row[0] == 'file') * 3)
    with temporary(store, 'capture') as staging:
        checkout = staging / 'source'
        checkout.mkdir()
        git(checkout, 'init', '--quiet')
        overlay(source, checkout, before)
        # All selected paths, including ignored-but-tracked source, are explicit.
        command = ['git', '-c', 'core.hooksPath=/dev/null', '-C', str(checkout),
                   'add', '--force', '--pathspec-from-file=-', '--pathspec-file-nul']
        subprocess.run(command, input='\0'.join(before).encode() + b'\0' if before else b'', check=True,
                       capture_output=True, env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})
        require(inventory(source) == before and inventory(checkout) == before,
                'source_changed', 'Source changed while creating its runtime snapshot')
        env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+0000', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+0000'}
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=auto-agents runtime',
                        '-c', 'user.email=runtime@localhost', '-C', str(checkout), 'commit', '--quiet',
                        '--allow-empty', '-m', 'Runtime content ' + identity], env=env, check=True, capture_output=True)
        artifact = build(store.root / 'kernel-releases', checkout, identity, {'kernel_schema': 1, 'rpc': 2})
        origin = {'source_root': str(source), 'head': git(source, 'rev-parse', 'HEAD'),
                  'dirty': bool(git(source, 'status', '--porcelain')), 'source': identity}
        # Provenance is descriptive; it is never part of the verified artifact ID.
        store.set_meta('runtime_source:' + artifact['artifact_id'], origin)
        return artifact
