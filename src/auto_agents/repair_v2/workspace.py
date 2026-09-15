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
                raise RepairBlocked('incomplete_import',
                    'Interrupted candidate import is preserved at ' + str(preparing))
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
                    raise RepairBlocked('source_conflict', 'retained candidate requires merge resolution in ' + str(preparing)) from error
            else: git(preparing, 'checkout', '--quiet', '--detach', self.base)
            preparing.rename(self.candidate)
        return self.candidate

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
            atomic_json(destination.parent / (identity + '.json'), {'identity': identity, 'files': before})
        if inventory(destination) != before:
            raise RepairBlocked('snapshot_changed', 'saved verification snapshot was modified')
        return identity, destination
