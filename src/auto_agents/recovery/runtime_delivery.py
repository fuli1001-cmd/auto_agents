"""Journalled local delivery of accepted engine bytes, preserving the user's index."""
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
        require(previous.get('accepted', previous['after']) == wanted and previous['source'] == str(source),
                'source_delivery_pending', 'Another accepted source delivery is pending')
        return resume(store)
    if source_identity(source) == wanted: return
    before = inventory(base)
    after = inventory(candidate)
    current = inventory(source)
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    conflicts = sorted(k for k in changed if current.get(k) not in (before.get(k), after.get(k)))
    require(not conflicts, 'source_delivery_conflict',
            '引擎源码在自修复期间发生变化；已保留候选，需核对合并后继续',
            paths=conflicts, source=str(source), base=str(base), candidate=str(candidate))
    # Only the accepted patch is delivered. Unrelated working-tree changes are
    # retained and become part of a new runtime that needs its own verification.
    merged = dict(current)
    for name in changed:
        if name in after: merged[name] = after[name]
        else: merged.pop(name, None)
    conflicts = sorted(name for name in merged if any(str(p) in merged for p in Path(name).parents))
    require(not conflicts, 'source_delivery_conflict',
            '源码交付遇到文件与目录冲突，已保留候选', paths=conflicts,
            source=str(source), base=str(base), candidate=str(candidate))
    record = {'status': 'applying', 'source': str(source), 'base': str(base), 'candidate': str(candidate),
              'before': digest(current), 'after': digest(merged), 'accepted': wanted,
              'files_before': current, 'files_after': merged,
              'changed': sorted(changed,
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
        require(source_identity(candidate) == record.get('accepted', record['after']),
                'source_delivery_changed', 'Accepted source changed')
        # Check the entire transaction before writing anything, also on resume.
        # Changes arriving after journalling need reconciliation, not adoption
        # under the identity recorded for the earlier merged tree.
        current = inventory(source)
        conflicts = sorted(name for name in current.keys() | record['files_before'].keys() | record['files_after'].keys()
                           if current.get(name) not in (record['files_before'].get(name), record['files_after'].get(name)))
        require(not conflicts, 'source_delivery_conflict',
                '源码交付遇到并发修改，已保留候选和交付记录', paths=conflicts)
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
