"""Stdlib-only handoff from an editable launcher to the installed manager.

No candidate config/model/CLI modules are imported before this handoff.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4


def installation_root():
    explicit = os.environ.get('AUTO_AGENTS_RECOVERY_CONTROL')
    if explicit: return Path(explicit).resolve()
    if os.environ.get('AUTO_AGENTS_REPAIR_CONTROL_DISABLED') == '1': return None
    configured = os.environ.get('AUTO_AGENTS_REPAIR_CONTROL_CONFIG')
    if configured and Path(configured).is_file(): return Path(json.loads(Path(configured).read_text())['root']).resolve()
    source = str(Path(__file__).resolve().parents[2])
    home = Path(os.environ.get('AUTO_AGENTS_REPAIR_CONTROL_ROOT') or
                str(Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'auto-agents/repair-control'))
    name = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    binding = home / (name + '.json')
    return Path(json.loads(binding.read_text())['root']).resolve() if binding.is_file() else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify(artifact):
    root = Path(artifact['path'])
    expected = {key: artifact[key] for key in ('format', 'commit', 'source', 'environments')}
    if (artifact.get('version') != 1 or artifact.get('format') != 'standalone-git-v1'
            or _digest(expected) != artifact['artifact_id']
            or json.loads((root.parent / 'manifest.json').read_text()) != {**expected, 'artifact_id': artifact['artifact_id']}):
        raise RuntimeError('Installed runtime manifest is invalid')
    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                        '-C', str(root), *args], text=True,
                                       env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}).strip()
    if (not (root / '.git').is_dir() or (root / '.git').is_symlink()
            or (root / '.git/commondir').exists() or (root / '.git/objects/info/alternates').exists()
            or git('rev-parse', 'HEAD') != artifact['commit']):
        raise RuntimeError('Installed runtime Git identity is invalid')
    files = {}
    for name in sorted(set(filter(None, git('ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')))):
        relative = Path(name); path = root / relative
        if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
            raise RuntimeError('Invalid installed runtime path')
        if any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            raise RuntimeError('Installed runtime traverses a symlink')
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()): raise RuntimeError('Installed runtime link escaped')
            files[name] = ['link', os.readlink(path)]
        elif path.is_file(): files[name] = ['file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777]
        elif path.is_dir(): raise RuntimeError('Installed runtime contains an unmaterialized gitlink')
    if _digest(files) != artifact['source']: raise RuntimeError('Installed runtime source was modified')


def handoff(arguments):
    if os.environ.get('AUTO_AGENTS_RUNTIME_USE'): return False
    root = installation_root()
    if root is None or not (root / 'control.sqlite3').is_file(): return False
    with sqlite3.connect('file:' + str(root / 'control.sqlite3') + '?mode=rw', uri=True) as db:
        try:
            row = db.execute("SELECT value FROM kernel_meta WHERE key='runtime_manager_runtime'").fetchone()
            if not row: row = db.execute("SELECT value FROM kernel_meta WHERE key='active_runtime'").fetchone()
        except sqlite3.OperationalError: return False
        artifact = json.loads(row[0]) if row else None
        if not artifact or not (Path(artifact['path']) / 'src/auto_agents/recovery/runtime_manager.py').is_file(): return False
        # Protect the manager before importing it or allowing another adoption.
        db.execute('BEGIN IMMEDIATE')
        current_row = db.execute("SELECT value FROM kernel_meta WHERE key='runtime_manager_runtime'").fetchone()
        if not current_row: current_row = db.execute("SELECT value FROM kernel_meta WHERE key='active_runtime'").fetchone()
        current = json.loads(current_row[0])
        artifact = current
        if not (Path(artifact['path']) / 'src/auto_agents/recovery/runtime_manager.py').is_file(): return False
        verify(artifact)
        db.execute('CREATE TABLE IF NOT EXISTS kernel_runtime_uses('
                   'token TEXT PRIMARY KEY,runtime TEXT NOT NULL,owner TEXT NOT NULL,purpose TEXT NOT NULL)')
        token = uuid4().hex
        ticks = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
        owner = {'pid': os.getpid(), 'ticks': ticks, 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        instance = artifact['artifact_id']
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='kernel_runtimes'").fetchone():
            registered = db.execute('SELECT id FROM kernel_runtimes WHERE path=?', (str(Path(artifact['path']).parent),)).fetchone()
            if registered: instance = registered[0]
            elif db.execute('SELECT 1 FROM kernel_runtimes WHERE id=?', (instance,)).fetchone():
                instance = _digest([instance, str(Path(artifact['path']).parent)])
        db.execute('INSERT INTO kernel_runtime_uses VALUES(?,?,?,?)',
                   (token, instance, json.dumps(owner), 'manager'))
    script = ('import sys,runpy; sys.path.insert(0,' + repr(str(Path(artifact['path']) / 'src'))
              + "); runpy.run_module('auto_agents.recovery.runtime_manager',run_name='__main__')")
    env = {**os.environ, 'PYTHONPATH': str(Path(artifact['path']) / 'src'),
           'AUTO_AGENTS_RECOVERY_CONTROL': str(root), 'AUTO_AGENTS_MANAGER_USE': token}
    os.execve(sys.executable, [sys.executable, '-c', script, str(root), *arguments], env)
    return True
