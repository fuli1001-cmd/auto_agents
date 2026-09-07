"""Maintenance of registered cache rows and their content-addressed objects."""
from __future__ import annotations

import contextlib
from pathlib import Path
import sqlite3
import time

from .artifact_store import DAY, alive, _parent_fd

TABLES = {"command_entries": (30 * DAY, 5000), "command_timings": (30 * DAY, 5000),
          "gate_proof_certificates": (14 * DAY, 20000), "gate_result_successes": (14 * DAY, 20000),
          "file_matches": (30 * DAY, 20000)}


def register_database(path):
    from .artifact_runtime import track
    track(path, "permanent", metadata={"cache_database": True})


def maintain_caches(store, deadline, scope=None):
    results = []
    for row in store.rows(scope, limit=1000):
        if time.monotonic() >= deadline:
            break
        if not row["metadata"].get("cache_database") or row["state"] == "deleted":
            continue
        if any(alive(x) for x in row["leases"]):
            continue
        try:
            from .artifact_references import protection, deletion_guard
            if protection(row):
                continue
            with store.locked(), deletion_guard(row), _parent_fd(row, row["path"]):
                with contextlib.closing(sqlite3.connect(Path(row["path"]).as_uri() + "?mode=rw", uri=True, timeout=0.1)) as db:
                    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        for name in tables & TABLES.keys():
                            ttl, count = TABLES[name]
                            db.execute(f"DELETE FROM {name} WHERE updated_at<?", (time.time() - ttl,))
                            db.execute(f"DELETE FROM {name} WHERE rowid IN (SELECT rowid FROM {name} ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (count,))
                    # SQLite alone owns journal/WAL lifecycle. Busy checkpoint or
                    # legacy non-incremental DB simply defers physical shrinkage.
                    db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    db.execute("PRAGMA incremental_vacuum(128)")
            results.append({"id": row["id"], "result": "cache_rows_pruned"})
        except (OSError, ValueError, sqlite3.Error) as error:
            results.append({"id": row["id"], "result": "deferred", "reason": str(error)})
    return results
