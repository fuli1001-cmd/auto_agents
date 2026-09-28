"""Journalled local delivery of accepted engine bytes, preserving the user's index."""
import json
import os
from pathlib import Path
import tempfile

from .model import require, digest
from .runtime_source import inventory, source_identity


def deliver(store, source, base, candidate):
    source, base, candidate = Path(source), Path(base), Path(candidate)
    wanted = source_identity(candidate)
    previous = store.meta('runtime_delivery')
    if previous and previous.get('status') != 'complete':
        require(previous['after'] == wanted and previous['source'] == str(source),
                'source_delivery_pending', 'Another accepted source delivery is pending')
        return resume(store)
    if source_identity(source) == wanted: return
    before = inventory(base)
    require(inventory(source) == before, 'source_delivery_conflict',
            '引擎源码在自修复期间发生变化；已保留候选，需核对合并后继续')
    after = inventory(candidate)
    record = {'status': 'applying', 'source': str(source), 'base': str(base), 'candidate': str(candidate),
              'before': digest(before), 'after': wanted, 'files_before': before, 'files_after': after,
              'changed': sorted((k for k in before.keys() | after.keys() if before.get(k) != after.get(k)),
                                key=lambda k: (k in after, -len(Path(k).parts) if k not in after else len(Path(k).parts), k)), 'done': []}
    store.set_meta('runtime_delivery', record)
    return resume(store)


def resume(store):
    record = store.meta('runtime_delivery')
    if not record or record['status'] == 'complete': return
    import fcntl
    from ..repair_v2.workspace import git
    source, candidate = Path(record['source']), Path(record['candidate'])
    common = Path(git(source, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    with (common / 'auto-agents-source.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        require(source_identity(candidate) == record['after'], 'source_delivery_changed', 'Accepted source changed')
        for name in record['changed']:
            current = inventory(source).get(name)
            before, after = record['files_before'].get(name), record['files_after'].get(name)
            require(current in (before, after), 'source_delivery_conflict',
                    '源码交付遇到并发修改，已保留候选和交付记录', path=name)
            if current != after:
                destination = source / name
                require(not any(p.is_symlink() for p in destination.parents if p != source and source in p.parents),
                        'source_delivery_path', 'Source delivery traverses a directory link')
                if after is None:
                    destination.unlink()
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.is_dir() and not destination.is_symlink(): destination.rmdir()
                    fd, temporary = tempfile.mkstemp(prefix='.auto-agents-delivery-', dir=destination.parent)
                    try:
                        if after[0] == 'link':
                            os.close(fd); Path(temporary).unlink(); os.symlink(after[1], temporary)
                        else:
                            with os.fdopen(fd, 'wb') as output:
                                output.write((candidate / name).read_bytes()); output.flush(); os.fsync(output.fileno())
                            os.chmod(temporary, after[2])
                        require(inventory(source).get(name) == before, 'source_delivery_conflict', 'Source changed during delivery', path=name)
                        os.replace(temporary, destination)
                    finally: Path(temporary).unlink(missing_ok=True)
            if name not in record['done']: record['done'].append(name)
            store.set_meta('runtime_delivery', record)
        require(source_identity(source) == record['after'], 'source_delivery_conflict',
                'Source contains additional concurrent changes; delivery is retained for reconciliation')
        # Keep concise evidence of the delivery; source directories cease to be
        # recovery pins only after the entire accepted content is present.
        record['status'] = 'complete'
        store.set_meta('runtime_delivery', record)
        return record
