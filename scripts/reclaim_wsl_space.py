#!/usr/bin/env python3
"""Owned-resource cleanup before Windows performs offline WSL compaction."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time


def busy_agents(proc_root=Path('/proc')):
    busy = []
    names = {'codex', 'claude', 'claude-code', 'copilot', 'copilot-cli', 'hermes', 'dots'}
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            argv = [os.fsdecode(v) for v in (proc/'cmdline').read_bytes().split(b'\0') if v]
            if not argv:
                continue
            executable = Path(argv[0]).name
            running_agent = executable in names or any(
                Path(v).name in {'auto-agents', 'auto_agents.py', 'auto-agents-watch'}
                or v in {'auto_agents', 'auto_agents_watch'} for v in argv[1:4])
            # Node CLI wrappers carry their actual application in argv[1].
            if executable in {'node', 'node.exe'} and len(argv) > 1:
                running_agent |= bool(re.search(r'(?:codex|claude-code|copilot)/(?:bin|dist)/', argv[1]))
            if running_agent:
                busy.append({'pid': int(proc.name), 'executable': executable})
        except (OSError, ValueError):
            continue
    return busy


def open_paths(proc_root=Path('/proc')):
    result = set()
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            try: result.add(Path(os.readlink(proc/'cwd')))
            except OSError: pass
            for fd in (proc/'fd').iterdir():
                try: result.add(Path(os.readlink(fd)))
                except OSError: pass
        except OSError:
            continue
    return result


def clean_test_temporaries(base, *, older_than, referenced, now=None):
    """Only owned pytest fixtures, with no open files; never arbitrary /tmp."""
    now = time.time() if now is None else now
    removed = []
    if not base.is_dir() or base.is_symlink() or base.stat().st_uid != os.getuid():
        return removed
    for path in base.iterdir():
        if not re.fullmatch(r'pytest-\d+', path.name) or path.is_symlink() or not path.is_dir():
            continue
        info = path.stat()
        if info.st_uid != os.getuid() or now-info.st_mtime < older_than:
            continue
        if any(ref == path or path in ref.parents for ref in referenced):
            continue
        shutil.rmtree(path)
        removed.append(str(path))
    link = base/'pytest-current'
    if link.is_symlink() and not link.exists():
        target = link.readlink()
        if (not target.is_absolute() and re.fullmatch(r'pytest-\d+', str(target))) or target.parent == base:
            link.unlink()
    return removed


def reclaim(projects, *, engine, preflight=False):
    busy = busy_agents()
    if busy:
        raise RuntimeError('Close running agents and WSL terminals before reclaiming: '+json.dumps(busy))
    sys.path.insert(0, str(engine/'src'))
    sys.path.insert(0, str(engine/'supervisor/src'))
    from auto_agents.run_lock import ProjectRunLock
    from contextlib import ExitStack
    with ExitStack() as locks:
        for project in projects:
            if project.is_dir() and (project/'.auto-agents').is_dir():
                locks.enter_context(ProjectRunLock(project))
        if preflight:
            return {'ok': True, 'phase': 'preflight', 'projects': [str(p) for p in projects]}
        from auto_agents.artifact_cleanup import clean
        result = {'registered': clean(seconds=300)}
        if not result['registered']['ok']:
            raise RuntimeError('Registered cleanup failed; see '+str(result['registered'].get('report')))
        from auto_agents.control.projection import current
        from auto_agents.control.store import Store
        from auto_agents.control.cleanup import collect
        result['projects'] = []
        for project in projects:
            if current(project):
                receipt = collect(Store(project), seconds=60, git_gc=True)
                if not receipt['ok']:
                    raise RuntimeError('Project cleanup failed: '+json.dumps(receipt['errors']))
                result['projects'].append({'project': str(project), **receipt})
        from auto_agents_watch.store import Store as WatchStore
        from auto_agents_watch.cleanup import completed_job, tool_images
        root = Path(os.environ.get('AUTO_AGENTS_WATCH_ROOT', str(Path.home()/'.local/state/auto-agents-watch')))
        result['watcher'] = []
        if (root/'maintenance.sqlite3').is_file():
            store = WatchStore(root)
            for job in store.list():
                if job['state'] != 'DONE':
                    continue
                # Maintenance may belong to a project outside the configured list.
                if Path(job['project']) not in projects:
                    with ProjectRunLock(Path(job['project'])):
                        receipt = completed_job(store, job)
                else:
                    receipt = completed_job(store, job)
                store.save(job, cleanup=receipt)
                result['watcher'].append({'id': job['id'], **receipt})
            result['tool_images'] = tool_images(store)
        result['pytest'] = clean_test_temporaries(
            Path('/tmp')/('pytest-of-'+__import__('getpass').getuser()),
            older_than=86400, referenced=open_paths())
        result.update(ok=True, linux_available_bytes=shutil.disk_usage('/').free)
        return result


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--request', required=True)
    p.add_argument('--preflight', action='store_true')
    args = p.parse_args(argv)
    try:
        request = json.loads(Path(args.request).read_text(encoding='utf-8-sig'))
        result = reclaim([Path(v).expanduser().resolve() for v in request['projects']],
                         engine=Path(request['engine']).resolve(), preflight=args.preflight)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
