"""Private candidate clones and immutable, byte-bound verification snapshots."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

from .store import atomic_json, digest
from .types import RepairBlocked
from .storage import require_space, tree_bytes


def git(root, *args):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0'}
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
        '-c', 'protocol.file.allow=always', '-c', 'user.name=auto-agents repair',
        '-c', 'user.email=repair@localhost', '-C', str(root), *args], env=env, text=True).strip()


def inventory(root):
    """Ignore execution artefacts through Git, but bind every delivered file byte."""
    root = Path(root)
    names = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')
    result = {}
    for name in sorted(set(filter(None, names))):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
            raise RepairBlocked('invalid_source', 'candidate contains an unsafe path')
        path = root / relative
        # An ancestor link must not cause host paths to be read/copied as source.
        if any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            raise RepairBlocked('invalid_source', 'candidate source traverses an ancestor link: ' + name)
        if path.is_symlink(): result[name] = ['link', os.readlink(path)]
        elif path.is_file():
            result[name] = ['file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777]
        elif path.is_dir():
            raise RepairBlocked('gitlink_source', 'gitlink materialization requires an explicit immutable source: ' + name)
    return result


def source_identity(root):
    return digest(inventory(root))


def overlay(source, target, files):
    """Materialize tracked deletions, dirty files and untracked candidate work."""
    for name in filter(None, git(target, 'ls-files', '-z').split('\0')):
        path = target / name
        if name not in files and (path.is_file() or path.is_symlink()): path.unlink()
    for name, item in files.items():
        original, destination = source / name, target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink(): destination.unlink()
        if item[0] == 'link':
            destination.unlink(missing_ok=True); destination.symlink_to(item[1])
        else: shutil.copy2(original, destination)


class Workspace:
    def __init__(self, root, source, base, retained=None):
        self.root, self.source, self.base = Path(root), Path(source), base
        self.candidate = self.root / 'candidate'
        self.retained = Path(retained) if retained else None

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.candidate.exists():
            origin = self.retained or self.source
            preparing = self.root / 'candidate.preparing'
            if preparing.exists():
                # No provider sees this directory before the atomic rename.
                shutil.rmtree(preparing)
            require_space(self.root, tree_bytes(origin) * 2)
            subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks',
                            str(origin), str(preparing)], check=True)
            if self.retained:
                retained = inventory(origin)
                overlay(origin, preparing, retained)
                if inventory(origin) != retained or inventory(preparing) != retained:
                    raise RepairBlocked('source_changed', 'retained work changed during import')
                git(preparing, 'add', '-A')
                if git(preparing, 'status', '--porcelain'):
                    git(preparing, 'commit', '-qm', 'Preserve interrupted candidate changes')
                # Carry the complete retained lineage onto the chosen controller.
                git(preparing, 'fetch', '--quiet', str(self.source), self.base)
                try: git(preparing, 'merge', '--no-edit', self.base)
                except subprocess.CalledProcessError as error:
                    conflicts = git(preparing, 'diff', '--name-only', '--diff-filter=U').splitlines()
                    if not conflicts: raise
                    atomic_json(self.root / 'import-conflicts.json', {'paths': conflicts, 'base': self.base})
            else: git(preparing, 'checkout', '--quiet', '--detach', self.base)
            preparing.rename(self.candidate)
        return self.candidate

    def checkpoint(self):
        # Only the controller writes Git metadata; native implementations see
        # a read-only .git mount, including during conflict resolution.
        git(self.candidate, 'add', '-A')
        if git(self.candidate, 'status', '--porcelain') or (self.candidate / '.git/MERGE_HEAD').exists():
            git(self.candidate, 'commit', '-qm', 'Checkpoint unified repair candidate')

    def freeze(self):
        before = inventory(self.candidate)
        identity = digest(before)
        destination = self.root / 'snapshots' / identity
        if not destination.exists():
            staging = destination.with_name(identity + '.building')
            if staging.exists(): shutil.rmtree(staging)
            require_space(self.root, tree_bytes(self.candidate) * 2)
            subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks',
                            str(self.candidate), str(staging)], check=True)
            overlay(self.candidate, staging, before)
            if inventory(self.candidate) != before or inventory(staging) != before:
                raise RepairBlocked('source_changed', 'candidate changed while freezing verification inputs')
            # Metadata comes from our clone; never execute hooks/filters supplied by the candidate.
            (staging / '.git/config').write_text('[core]\n repositoryformatversion = 0\n bare = false\n')
            git(staging, 'add', '-A')
            if git(staging, 'status', '--porcelain'):
                git(staging, 'commit', '-qm', 'Freeze repair candidate')
            staging.rename(destination)
            commit = git(destination, 'rev-parse', 'HEAD')
            git(self.candidate, 'fetch', '--quiet', str(destination),
                commit + ':refs/auto-agents/v2-snapshots/' + identity)
            atomic_json(destination.parent / (identity + '.json'),
                        {'identity': identity, 'commit': commit, 'files': before})
        if inventory(destination) != before:
            raise RepairBlocked('snapshot_changed', 'saved verification snapshot was modified')
        return identity, destination

    def collect_snapshots(self, current, *, keep=2):
        """Retain history in Git; bound redundant checked-out source directories."""
        import json
        root = self.root / 'snapshots'
        saved = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                        and len(p.name) == 64 and all(c in '0123456789abcdef' for c in p.name)),
                       key=lambda p: p.stat().st_mtime_ns, reverse=True) if root.exists() else []
        retained = {current}
        for path in saved:
            if len(retained) >= max(1, keep): break
            retained.add(path.name)
        removed = []
        for path in saved:
            if path.name in retained: continue
            manifest = root / (path.name + '.json')
            if not manifest.is_file(): continue
            record = json.loads(manifest.read_text())
            reference = 'refs/auto-agents/v2-snapshots/' + path.name
            try: commit = git(self.candidate, 'rev-parse', '--verify', reference)
            except subprocess.CalledProcessError: continue
            if record.get('commit') != commit or inventory(path) != record.get('files'): continue
            # The manifest and reachable commit remain; no acceptance evidence
            # is discarded and no dirty/unrecognized checkout is removed.
            shutil.rmtree(path)
            removed.append(path.name)
        return removed

    def materialize(self, identity):
        import json
        root = self.root / 'snapshots'
        destination = root / identity
        record = json.loads((root / (identity + '.json')).read_text())
        if record.get('identity') != identity:
            raise RepairBlocked('snapshot_changed', 'snapshot manifest identity changed')
        if not destination.exists():
            require_space(root, tree_bytes(self.candidate) * 2)
            subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks',
                            str(self.candidate), str(destination)], check=True)
            git(destination, 'fetch', '--quiet', str(self.candidate), record['commit'])
            git(destination, 'checkout', '--quiet', '--detach', record['commit'])
            for name, entry in record['files'].items():
                if entry[0] == 'file': (destination / name).chmod(entry[2])
        if inventory(destination) != record['files']:
            raise RepairBlocked('snapshot_changed', 'materialized snapshot differs from its manifest')
        return destination
