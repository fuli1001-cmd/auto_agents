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
        finally: store.release(identity)


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
    from .repair_v2.store import atomic_json
    results, reasons = Counter(), Counter()
    total, retained, measured = 0, 0, True
    name = 'cleanup-' + uuid4().hex + '.jsonl'
    report = store.root / name
    complete = True
    phase_seconds = (deadline - started) / 3 if math.isfinite(deadline) else float('inf')
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
            pressure = totals.get(row['scope'], 0) > budget
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
    # Only an existing, local Docker connection is eligible. Never contact a
    # remote worker/daemon or create an image just to perform cleanup.
    if scope is not None or not (roots or (store.root / 'v2-images').is_dir()) or not shutil.which('docker'): return
    from .repair_v2.docker import run
    host = os.environ.get('DOCKER_HOST', '') if not os.environ.get('DOCKER_CONTEXT') else ''
    if not host:
        code, output = run(['docker', 'context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'], timeout=5)
        if code:
            record({'result': 'deferred', 'reason': 'docker_context_unavailable', 'freed_bytes': 0}); return
        try: host = json.loads(output)
        except ValueError: host = ''
    if not isinstance(host, str) or not host.startswith(('unix:///', 'npipe:////./pipe/')):
        record({'result': 'retained', 'reason': 'nonlocal_or_unknown_docker_endpoint', 'freed_bytes': 0}); return
    code, ids = run(['docker', 'ps', '-aq'], timeout=5)
    if code:
        record({'result': 'deferred', 'reason': 'docker_unavailable', 'freed_bytes': 0}); return
    def active_mounts(ids):
        if not ids.strip(): return []
        code, text = run(['docker', 'inspect', *ids.split()], timeout=10)
        if code:
            raise RuntimeError('docker_mounts_unavailable')
        return [Path(m['Source']).resolve() for c in json.loads(text) for m in c.get('Mounts', []) if m.get('Source')]
    mounts = active_mounts(ids)
    from .repair_v2.cleanup import reap_containers
    from .repair_v2.storage import recover_executions
    from .artifact_legacy import enclosing_protection
    for root in roots:
        if time.monotonic() >= deadline: break
        verification = root / 'v2-verification'
        if verification.is_dir() and not verification.is_symlink():
            protection = enclosing_protection(store, verification)
            if protection:
                record({'result': 'retained', 'path': str(verification), 'reason': protection, 'freed_bytes': 0})
            else:
                removed = reap_containers(verification, kind='verification')
                for container in removed:
                    record({'result': 'deleted', 'kind': 'container', 'id': container, 'freed_bytes': 0})
                if removed:
                    code, current = run(['docker', 'ps', '-aq'], timeout=5)
                    if code: raise RuntimeError('docker_mounts_unavailable_after_reaping')
                    mounts = active_mounts(current)
                recover_executions(verification, mounts, deadline=deadline, record=record)
        for transaction in (root / 'v2-transactions').glob('*'):
            if time.monotonic() >= deadline: break
            if transaction.is_symlink() or not transaction.is_dir(): continue
            provider = transaction / 'provider-state'
            if provider.is_dir() and not provider.is_symlink() and not enclosing_protection(store, provider):
                for container in reap_containers(provider, kind='provider'):
                    record({'result': 'deleted', 'kind': 'container', 'id': container, 'freed_bytes': 0})
    if scope is None:
        from .repair_v2 import images
        images.maintain(deadline=deadline, registry_root=store.root / 'v2-images', record=record)
