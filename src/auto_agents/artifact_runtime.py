"""Producer integration and bounded out-of-process storage maintenance."""
from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import time
import threading
import functools
import shutil

from .artifact_store import ArtifactStore, process_identity, storage_root

_context = contextvars.ContextVar("artifact_context", default=None)
_owned = set()
_acquired = {}
_warned = False
_timer = None
_timer_lock = threading.Lock()


def _lease():
    return {**process_identity(), "token": str(threading.get_ident())}


def enabled():
    if os.environ.get("AUTO_AGENTS_STORAGE_DISABLED") == "1":
        return False
    return bool(_context.get() or os.environ.get("AUTO_AGENTS_STORAGE_ENABLED") == "1")


def activate(project=None, *, scope=None, process_control=None):
    project = str(Path(project).expanduser().resolve()) if project else ""
    value = {"scope": scope or ("project:" + project if project else "user"), "project": project,
             "process_control": str(process_control) if process_control else ""}
    return _context.set(value)


def deactivate(token):
    _context.reset(token)


def track(path, kind="scratch", *, project=None, scope=None, metadata=None, reference=""):
    global _warned
    if not enabled() or not Path(path).exists():
        return None
    context = _context.get() or {"scope": "user", "project": ""}
    details = dict(metadata or {})
    if context.get("process_control"):
        details.setdefault("process_control", context["process_control"])
    project = project or (context.get("project") if scope is None else "")
    if project:
        details.setdefault("project", str(Path(project).expanduser().resolve()))
    try:
        store = ArtifactStore()
        owner = _lease()
        key = (str(store.root), str(Path(path).absolute()), kind, json.dumps(owner, sort_keys=True))
        if key in _acquired:
            return _acquired[key]
        parents = {str(parent) for parent in Path(path).absolute().parents}
        if any(old[0] == key[0] and old[1] in parents and old[3] == key[3] for old in _acquired):
            return None  # The enclosing owned resource already protects this child.
        identity = store.register(path, kind=kind, scope=scope or context["scope"], metadata=details, reference=reference, owner=owner)
        _owned.add((str(store.root), identity, json.dumps(owner, sort_keys=True)))
        _acquired[key] = identity
        return identity
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        # Sandboxed producers may not own the host registry. Their enclosing
        # registered sandbox retains ownership; never broaden write permission.
        if not _warned:
            print(f"Storage tracking unavailable; unregistered artifacts will be retained: {error}", file=sys.stderr)
            _warned = True
        return None


def release(identity, *, failed=False):
    if identity:
        with contextlib.suppress(OSError, ValueError, RuntimeError, sqlite3.Error):
            ArtifactStore().release(identity, failed=failed, owner=_lease())
        _owned.discard((str(storage_root().absolute()), identity, json.dumps(_lease(), sort_keys=True)))
        for key, value in list(_acquired.items()):
            if value == identity and key[3] == json.dumps(_lease(), sort_keys=True):
                _acquired.pop(key, None)


def release_owned(all_owners=False):
    for root, identity, encoded in list(_owned):
        owner = json.loads(encoded)
        if not all_owners and owner != _lease():
            continue
        with contextlib.suppress(OSError, ValueError, RuntimeError, sqlite3.Error):
            ArtifactStore(root).release(identity, owner=owner)
        _owned.discard((root, identity, encoded))
        for key, value in list(_acquired.items()):
            if value == identity and key[3] == encoded:
                _acquired.pop(key, None)


def capture_output(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        request = kwargs.get("request")
        if request is None:
            request = next((x for x in args if hasattr(x, "output_path")), None)
        try:
            return function(*args, **kwargs)
        finally:
            if request is not None and request.output_path:
                track(request.output_path, "log")
    return call


def worker_scope(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        from .workers import load_local_worker_config
        config = load_local_worker_config()
        token = activate(scope="worker:" + str(config.managed_root))
        try:
            schedule()
            return function(*args, **kwargs)
        finally:
            release_owned()
            deactivate(token)
    return call


atexit.register(release_owned, True)


def schedule():
    """Only enqueue/spawn here; no directory scan in the caller's event loop."""
    if not enabled() or os.environ.get("AUTO_AGENTS_STORAGE_MAINTENANCE") == "off":
        return
    global _timer
    with _timer_lock:
        if _timer is None:
            captured = _context.get()
            def periodic():
                _context.set(captured)
                while True:
                    time.sleep(60)
                    schedule()
            _timer = threading.Thread(target=periodic, name="artifact-maintenance-clock", daemon=True)
            _timer.start()
    store = ArtifactStore()
    try:
        with store.locked(), store.connect(True) as db:
            previous = db.execute("SELECT data FROM maintenance WHERE key='scheduled'").fetchone()
            if previous and time.time() - json.loads(previous[0])["time"] < 3600:
                return
            db.execute("INSERT OR REPLACE INTO maintenance VALUES('scheduled',?)", (json.dumps({"time": time.time()}),))
        environment = dict(os.environ)
        environment["AUTO_AGENTS_STORAGE_ROOT"] = str(store.root)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        subprocess.Popen([sys.executable, "-m", "auto_agents.artifact_store", "--maintain"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=environment, cwd=store.root)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        pass  # Optional maintenance must not turn a business invocation into repair.


def maintain(scope_filter=None):
    store = ArtifactStore()
    started = time.monotonic()
    with store.locked(), store.connect(True) as db:
        previous = db.execute("SELECT data FROM maintenance WHERE key='running'").fetchone()
        if previous:
            from .artifact_store import alive
            if alive(json.loads(previous[0])):
                return {"ok": True, "skipped": "maintenance_already_running"}
        db.execute("INSERT OR REPLACE INTO maintenance VALUES('running',?)", (json.dumps(process_identity()),))
    try:
        totals = store.totals()
        if scope_filter is not None:
            totals = {key: value for key, value in totals.items()
                      if key == scope_filter or (scope_filter.endswith(":") and key.startswith(scope_filter))}
        limits = store.policy()["budgets"]
        scopes = sorted(totals)
        results = []
        from .artifact_cache import maintain_caches
        cache_results = maintain_caches(store, started + 5, scope_filter)
        for scope in scopes:
            if time.monotonic() - started >= 25:
                break
            budget = int(limits.get(scope.split(":", 1)[0], limits["user"]))
            scopes_rows = store.rows(scope, limit=1)
            disk_pressure = False
            disk_needed = 0
            if scopes_rows:
                try:
                    usage = shutil.disk_usage(Path(scopes_rows[0]["path"]).parent)
                    disk_pressure = usage.free < usage.total * 0.1
                    disk_needed = max(0, int(usage.total * 0.1) - usage.free)
                except OSError:
                    pass
            plan = store.plan(scope, seconds=min(5, 25 - (time.monotonic() - started)), pressure=totals[scope] > budget or disk_pressure,
                              reclaim_bytes=max(disk_needed, int(totals[scope] - budget * 0.8)))
            results.append(store.apply(plan["id"], seconds=max(0.01, 30 - (time.monotonic() - started))))
        result = {"ok": all(x["ok"] for x in results), "results": results, "cache_results": cache_results, "finished": time.time()}
        with store.connect(True) as db:
            db.execute("INSERT OR REPLACE INTO maintenance VALUES('last_result',?)", (json.dumps(result),))
        return result
    finally:
        with store.connect(True) as db:
            db.execute("DELETE FROM maintenance WHERE key='running'")


@contextlib.contextmanager
def command_context(project=None):
    token = _context.set({"scope": "project:" + str(Path(project).expanduser().resolve()) if project else "user",
                          "project": str(Path(project).expanduser().resolve()) if project else ""})
    try:
        schedule()
        yield
    finally:
        release_owned()
        schedule()
        _context.reset(token)
