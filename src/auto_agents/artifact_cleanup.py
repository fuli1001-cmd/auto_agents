"""One local cleanup engine shared by the CLI and background maintenance.

No provider calls, project workflow startup, global Docker prune, or WSL disk
operations belong here. Unknown resources are not inferred from their names.
"""
from collections import Counter
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import uuid4

from .artifact_store import ArtifactStore


@contextmanager
def managed_report(store, report, scope):
    with report.open('x', encoding='utf-8') as journal:
        identity = store.register(report, kind='log', scope=scope or 'user')
        try: yield journal, identity
        finally:
            # A concurrent producer can briefly hold the registry lock. The
            # process lease is still reaped after exit if release must defer.
            for attempt in range(5):
                try:
                    store.release(identity)
                    break
                except (BlockingIOError, sqlite3.OperationalError):
                    if attempt < 4: time.sleep(.05)


def clean(*, store=None, scope=None, seconds=None, progress=None):
    store = store or ArtifactStore()
    started = time.monotonic()
    deadline = started + seconds if seconds is not None else float('inf')
    with store.connect(True): pass
    fd = os.open(store.root / 'cleanup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid(): raise ValueError('unowned cleanup lock')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ok': False, 'complete': False, 'reason': 'cleanup_already_running', 'freed_bytes': 0}
        return _clean(store, scope, started, deadline, progress)
    finally: os.close(fd)


def _clean(store, scope, started, deadline, progress):
    from .artifact_cache import maintain_caches
    from .artifact_legacy import repair_roots, clean_legacy
    from .proof_support.store import atomic_json
    results, reasons = Counter(), Counter()
    total, retained, measured = 0, 0, True
    name = 'cleanup-' + uuid4().hex + '.jsonl'
    report = store.root / name
    complete = True
    phase_seconds = (deadline - started) / 4 if math.isfinite(deadline) else float('inf')
    with managed_report(store, report, scope) as (journal, report_id):
        def record(item):
            nonlocal total, retained, measured
            journal.write(json.dumps(item, ensure_ascii=False) + '\n'); journal.flush()
            results[item['result']] += 1
            if item.get('reason') and item['result'] != 'deleted': reasons[item['reason']] += 1
            total += item.get('freed_bytes', 0)
            if item['result'] == 'retained': retained += item.get('bytes', 0)
            measured = measured and item.get('size_complete', True)
            if progress: progress(dict(results), total)

        maintain_caches(store, min(deadline, time.monotonic() + 5), scope)
        # Cleanup is local. WSL capacity probes belong to admission, not deletion.
        backing_pressure = False
        # Capture a finite inventory: concurrent producers need not finish for
        # this command to complete. Automatic rounds rotate by last scan time.
        rows = [r for r in store.rows(scope, oldest=True) if r['id'] != report_id]
        totals, policy = store.totals(), store.policy()
        roots = repair_roots(store, scope)
        registry_deadline = min(deadline, time.monotonic() + phase_seconds)
        for row in rows:
            if time.monotonic() >= registry_deadline:
                complete = False; break
            budget = policy['budgets'].get(row['scope'].split(':', 1)[0], policy['budgets']['user'])
            pressure = backing_pressure or totals.get(row['scope'], 0) > budget
            try:
                usage = shutil.disk_usage(Path(row.get('trash') or row['path']).parent)
                pressure = pressure or usage.free < usage.total * .1
            except OSError: pass
            try:
                item = store.clean_artifact(row['id'], deadline=registry_deadline, pressure=pressure)
                totals[row['scope']] = max(0, totals.get(row['scope'], 0) - item.get('freed_bytes', 0))
                record(item)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                record({'path': row['path'], 'result': 'error', 'reason': str(error), 'freed_bytes': 0})
        complete = clean_legacy(store, roots, min(deadline, time.monotonic() + phase_seconds), record, scope) and complete
        from .artifact_compaction import compact_repair_copies
        if roots and time.monotonic() < deadline:
            complete = compact_repair_copies(store, roots, min(deadline, time.monotonic() + phase_seconds), record) and complete
        if time.monotonic() < deadline:
            try: _clean_docker(store, roots, scope, deadline, record)
            except (OSError, ValueError, RuntimeError) as error:
                record({'result': 'error', 'reason': 'docker_cleanup: ' + str(error), 'freed_bytes': 0})
        else: complete = False
    result = {'ok': not (results['error'] or results['deferred']),
              'complete': complete and not (results['error'] or results['deferred']),
              'counts': dict(results), 'freed_bytes': total, 'freed_bytes_are_estimated': True,
              'retained_bytes': retained, 'sizes_complete': measured,
              'retained_reasons': dict(reasons), 'report': str(report),
              'inventory': 'registered resources and verified legacy copies; unknown paths retained',
              'seconds': time.monotonic() - started}
    atomic_json(store.root / 'cleanup-last.json', result)
    with store.connect(True) as db:
        db.execute("INSERT OR REPLACE INTO maintenance VALUES('last_result',?)", (json.dumps(result),))
    return result


def _clean_docker(store, roots, scope, deadline, record):
    for root in roots:
        record({"path":str(root),"result":"retained","reason":"legacy_archive","freed_bytes":0})
