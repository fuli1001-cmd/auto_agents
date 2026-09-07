"""Host-local, stdlib-only ownership and retention for generated artifacts.

No directory-name discovery grants ownership. Producers register exact paths;
unknown legacy data remains outside the deletion set. State reads are read-only.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

DAY = 86400
POLICY = {"scratch": DAY, "incomplete": DAY, "cache": 14 * DAY,
          "environment": 30 * DAY, "log": 30 * DAY, "evidence": 7 * DAY,
          "recovery": 7 * DAY, "worktree": 7 * DAY, "permanent": None}
DEFAULT_BUDGETS = {"project": 10 << 30, "repair": 20 << 30, "worker": 30 << 30, "user": 5 << 30}


def storage_root():
    return Path(os.environ.get("AUTO_AGENTS_STORAGE_ROOT") or str(
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auto-agents/storage"))


def process_identity(pid=None):
    pid = pid or os.getpid()
    try:
        ticks = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"pid": pid, "ticks": ticks, "boot": boot}
    except (OSError, IndexError):
        return {"pid": pid, "ticks": "unknown", "boot": "unknown"}


def alive(owner):
    try:
        if Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    current = process_identity(owner["pid"])
    if current["ticks"] == "unknown":
        try:
            os.kill(owner["pid"], 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return all(current[key] == owner.get(key) for key in ("pid", "ticks", "boot"))


def _identity(path):
    info = Path(path).lstat()
    if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"unowned or symbolic artifact: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f"unsupported artifact type: {path}")
    return [info.st_dev, info.st_ino]


def _parents(path):
    result = []
    for parent in reversed(Path(path).parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError(f"unsafe artifact ancestor: {parent}")
        result.append([str(parent), info.st_dev, info.st_ino])
    return result


def _read(path):
    return json.loads(Path(path).read_text())


def _allocated(path, deadline):
    """Bounded metadata scan, never follow links or cross devices."""
    total, count, seen = 0, 0, set()
    device = Path(path).lstat().st_dev
    pending = [Path(path)]
    while pending:
        if time.monotonic() > deadline or count >= 10000:
            return total, False
        item = pending.pop()
        info = item.lstat()
        count += 1
        if info.st_dev != device:
            raise ValueError("artifact crosses a filesystem")
        if (info.st_dev, info.st_ino) not in seen:
            total += info.st_blocks * 512
            seen.add((info.st_dev, info.st_ino))
        if stat.S_ISDIR(info.st_mode):
            pending.extend(item.iterdir())
    return total, True


@contextlib.contextmanager
def _parent_fd(row, path):
    # Reopen every ancestor without following symlinks and compare its inode.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name, dev, ino in row["parents"]:
            if name != "/":
                child = os.open(Path(name).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if [info.st_dev, info.st_ino] != [dev, ino]:
                raise ValueError("artifact ancestor changed")
        info = os.stat(Path(path).name, dir_fd=fd, follow_symlinks=False)
        if [info.st_dev, info.st_ino] != row["inode"] or info.st_uid != os.getuid():
            raise ValueError("artifact identity changed")
        yield fd
    finally:
        os.close(fd)


def _remove_at(fd, name, device, deadline):
    if time.monotonic() > deadline:
        raise TimeoutError("deletion time budget exhausted")
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if info.st_dev != device:
        raise ValueError("refusing cross-filesystem deletion")
    if stat.S_ISDIR(info.st_mode):
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if os.fstat(child).st_ino != info.st_ino:
                raise ValueError("directory replaced during deletion")
            for entry in os.listdir(child):
                _remove_at(child, entry, device, deadline)
        finally:
            os.close(child)
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != [info.st_dev, info.st_ino]:
            raise ValueError("directory replaced before removal")
        os.rmdir(name, dir_fd=fd)
    else:
        # Unlink a contained symlink/hardlink; never modify its target contents.
        os.unlink(name, dir_fd=fd)


class ArtifactStore:
    def __init__(self, root=None):
        self.root = Path(root or storage_root()).absolute()
        self.path = self.root / "artifacts.sqlite3"

    @contextlib.contextmanager
    def connect(self, write=False):
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _parents(self.root)
            _identity(self.root)
            if self.root.stat().st_mode & 0o077:
                raise PermissionError("storage root must be private")
            db = sqlite3.connect(self.path, timeout=2)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, path TEXT UNIQUE, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS maintenance(key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, time REAL, artifact TEXT, data TEXT);
                CREATE INDEX IF NOT EXISTS artifact_measurements ON artifacts(json_extract(data,'$.measured'));
            """)
            os.chmod(self.path, 0o600)
        else:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextlib.contextmanager
    def locked(self, wait=2):
        with self.connect(True):
            pass
        with (self.root / "lifecycle.lock").open("a+") as handle:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            yield

    def rows(self, scope=None, limit=None, oldest=False):
        if not self.path.exists():
            return []
        with self.connect() as db:
            query, params, predicates = "SELECT data FROM artifacts", [], []
            if scope is not None:
                predicates.append("(json_extract(data,'$.scope')=? OR substr(json_extract(data,'$.scope'),1,?)=?)")
                params = [scope, len(scope) if scope.endswith(":") else 0, scope]
            if oldest:
                predicates.append("json_extract(data,'$.state')!='deleted'")
            if predicates:
                query += " WHERE " + " AND ".join(predicates)
            query += " ORDER BY " + ("coalesce(json_extract(data,'$.measured'),0),id" if oldest else "id")
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            return [json.loads(row[0]) for row in db.execute(query, params)]

    def totals(self):
        if not self.path.exists():
            return {}
        with self.connect() as db:
            return dict(db.execute("SELECT json_extract(data,'$.scope'),sum(json_extract(data,'$.bytes')) FROM artifacts WHERE json_extract(data,'$.state')!='deleted' GROUP BY json_extract(data,'$.scope')"))

    def policy(self):
        path = self.root / "policy.json"
        config = _read(path) if path.exists() else {}
        ttl = dict(POLICY)
        for kind, days in config.get("retention_days", {}).items():
            if kind not in ttl or kind == "permanent" or not isinstance(days, (int, float)) or days < 0:
                raise ValueError("invalid retention_days policy")
            ttl[kind] = days * DAY
        budgets = {**DEFAULT_BUDGETS, **config.get("budgets", {})}
        if any(not isinstance(value, int) or value <= 0 for value in budgets.values()):
            raise ValueError("budgets must be positive byte counts")
        return {"ttl": ttl, "budgets": budgets}

    def get(self, identity):
        with self.connect() as db:
            result = db.execute("SELECT data FROM artifacts WHERE id=?", (identity,)).fetchone()
        if not result:
            raise ValueError("unknown artifact")
        return json.loads(result[0])

    def _save(self, row, event=""):
        row["revision"] = row.get("revision", 0) + 1
        with self.connect(True) as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,data=excluded.data",
                       (row["id"], row["path"], json.dumps(row)))
            if event:
                db.execute("INSERT INTO events(time,artifact,data) VALUES(?,?,?)",
                           (time.time(), row["id"], json.dumps({"event": event, "state": row["state"], "error": row.get("error", "")})))

    def register(self, path, *, kind="scratch", scope="user", owner=None, metadata=None, reference=""):
        if kind not in POLICY:
            raise ValueError("unknown artifact kind")
        path = Path(path).absolute()
        if ".." in path.parts or path in {Path("/"), Path.home(), self.root} or self.root.is_relative_to(path):
            raise ValueError("unsafe artifact root")
        with self.locked(wait=30):
            rows = self.rows()
            row = next((r for r in rows if r["path"] == str(path)), None)
            if row and row["state"] not in {"deleted", "released", "live"}:
                raise RuntimeError("artifact is under maintenance; restore before use")
            inode = _identity(path)
            if row and row["state"] != "deleted" and inode != row["inode"]:
                raise ValueError("registered artifact was replaced")
            for other in rows:
                if other["path"] != str(path) and other["state"] != "deleted" and (
                    path.is_relative_to(other["path"]) or Path(other["path"]).is_relative_to(path)):
                    raise ValueError("overlapping artifact ownership")
            now = time.time()
            if row is None or row["state"] == "deleted":
                row = {"schema_version": 1, "id": uuid4().hex, "path": str(path), "inode": inode, "parents": _parents(path),
                       "kind": kind, "scope": scope, "created": now, "last_used": now, "released": None,
                       "state": "live", "leases": [], "references": [], "pin": "", "metadata": metadata or {},
                       "bytes": 0, "size_complete": False, "revision": 0}
                # Deleted paths may be reused, but old plans must not target the new object.
                with self.connect(True) as db:
                    db.execute("DELETE FROM artifacts WHERE path=? AND id!=?", (str(path), row["id"]))
            elif row["kind"] != kind or row["scope"] != scope:
                raise ValueError("artifact ownership contract changed")
            lease = owner or process_identity()
            row["leases"] = [x for x in row["leases"] if alive(x)]
            if lease not in row["leases"]:
                row["leases"].append(lease)
            if reference and reference not in row["references"]:
                row["references"].append(reference)
            row.update(state="live", last_used=now, released=None)
            self._save(row, "acquired")
            return row["id"]

    def release(self, identity, *, owner=None, failed=False, reference=None):
        with self.locked():
            row = self.get(identity)
            row["leases"] = [x for x in row["leases"] if x != (owner or process_identity()) and alive(x)]
            if reference is not None:
                row["references"] = [x for x in row["references"] if x != reference]
            row["released"] = time.time()
            row["last_used"] = time.time()
            row["failed"] = failed
            if not row["leases"]:
                row["state"] = "released"
                if not row.get("trash") and not Path(row["path"]).exists():
                    row["state"] = "deleted"
            self._save(row, "released")

    def promote(self, identity, kind):
        if kind not in POLICY:
            raise ValueError("unknown artifact kind")
        with self.locked():
            row = self.get(identity)
            if row["state"] not in {"live", "released"}:
                raise ValueError("artifact is under maintenance")
            row["kind"] = kind
            self._save(row, "promoted")

    def dispose_worktree(self, identity):
        """A plan-close receipt releases an exact, registered clean worktree."""
        with self.locked():
            row = self.get(identity)
            if row["kind"] != "worktree" or row["state"] not in {"live", "released"}:
                raise ValueError("resource is not an available worktree")
            reason = self.protection(row)
            if reason:
                raise ValueError(reason)
            from .artifact_references import deletion_guard
            with _parent_fd(row, row["path"]), deletion_guard(row):
                self._delete(row, time.monotonic() + 20)
        return {"ok": True, "id": identity}

    def pin(self, identity, reason):
        with self.locked():
            row = self.get(identity)
            if row["state"] in {"quarantining", "quarantined", "deleting", "deleted"}:
                raise ValueError("restore artifact before pinning")
            row["pin"] = reason
            self._save(row, "pin_changed")

    def protection(self, row):
        if row["kind"] == "permanent":
            return "permanent"
        if row["pin"]:
            return "pinned: " + row["pin"]
        if row["references"]:
            return "referenced: " + ", ".join(row["references"])
        if any(alive(x) for x in row["leases"]):
            return "active_process"
        from .artifact_references import protection
        return protection(row)

    def classify(self, row, now, pressure=False):
        if row.get("schema_version", 1) != 1:
            return "unknown: unsupported artifact schema"
        if row["state"] == "deleted":
            return "deleted"
        try:
            reason = self.protection(row)
            if reason:
                return reason
            target = row.get("trash") or row["path"]
            if not os.path.lexists(target):
                return "missing"
            with _parent_fd(row, target):
                pass
            if row["state"] in {"quarantined", "quarantining", "deleting"}:
                if row["state"] == "deleting":
                    return "eligible"
                return "eligible" if now >= row.get("purge_after", now + DAY) else "quarantine_grace"
            ttl = self.policy()["ttl"][row["kind"]]
            if row.get("failed") and row["kind"] in {"evidence", "log"}:
                ttl = 30 * DAY
            if pressure and row["kind"] in {"cache", "environment"}:
                ttl = 0
            since = row.get("released") or row["last_used"]
            return "eligible" if now - since >= ttl else "retention"
        except (OSError, ValueError, KeyError, sqlite3.Error) as error:
            return "unknown: " + str(error)

    def _reconcile(self, row):
        if row["state"] not in {"quarantining", "quarantined", "deleting"}:
            return
        original = Path(row["path"])
        trash = Path(row["trash"]) if row.get("trash") else None
        if trash and trash.exists():
            with _parent_fd(row, trash):
                pass
            if row["state"] == "quarantining":
                row["state"] = "quarantined"
                self._save(row, "reconciled_quarantine")
        elif original.exists() and trash and row["state"] != "deleting":
            # Crash before quarantine rename, or after restore rename.
            with _parent_fd(row, original):
                pass
            row.update(state="released", released=time.time())
            row.pop("trash", None)
            self._save(row, "reconciled_restore")
        elif row["state"] == "deleting" and not original.exists() and (trash is None or not trash.exists()):
            row["state"] = "deleted"
            self._save(row, "reconciled_delete")

    def status(self, scope=None):
        rows = self.rows(scope, limit=1001)
        items = [{"id": r["id"], "path": r["path"], "scope": r["scope"], "kind": r["kind"],
                  "state": r["state"], "bytes": r["bytes"], "size_complete": r["size_complete"],
                  "reason": r.get("last_reason", "not_scanned"), "checked_at": r.get("measured")}
                 for r in rows[:1000] if r["state"] != "deleted"]
        totals = self.totals()
        registered = sum(value for key, value in totals.items() if scope is None or key == scope or (scope.endswith(":") and key.startswith(scope)))
        last = None
        if self.path.exists():
            with self.connect() as db:
                result = db.execute("SELECT data FROM maintenance WHERE key='last_result'").fetchone()
                last = json.loads(result[0]) if result else None
        return {"ok": True, "items": items, "registered_bytes": registered, "scope_bytes": totals, "last_maintenance": last,
                "policy": self.policy(),
                "eligible_bytes": sum(r["bytes"] for r in items if r["reason"] == "eligible"),
                "quarantine_bytes": sum(r["bytes"] for r in items if r["state"] == "quarantined"),
                "inventory": "registered resources only; unregistered paths are never deleted",
                "size_complete": len(rows) <= 1000 and all(r["size_complete"] for r in items),
                "inventory_complete": len(rows) <= 1000, "reasons": "cached by the last plan; apply always revalidates"}

    def plan(self, scope=None, *, seconds=30, limit=1000, pressure=False, reclaim_bytes=None):
        now = time.time()
        deadline = time.monotonic() + min(30, max(0.01, seconds))
        items, scanned = [], 0
        rows = self.rows(scope, limit=limit + 1, oldest=True)
        if pressure:
            rows.sort(key=lambda row: row["last_used"])
        selected_bytes = 0
        for row in rows:
            if scanned >= limit or time.monotonic() >= deadline:
                break
            if row["state"] == "deleted":
                continue
            scanned += 1
            with self.locked():
                row = self.get(row["id"])
                self._reconcile(row)
            reason = self.classify(row, now, pressure)
            if pressure and reclaim_bytes is not None and selected_bytes >= reclaim_bytes:
                reason = self.classify(row, now, False)
            with self.locked():
                current = self.get(row["id"])
                if current["revision"] != row["revision"]:
                    continue
                if reason == "missing":
                    row["state"] = "deleted"
                elif not reason.startswith("unknown") and row["state"] != "deleted":
                    try:
                        measured, complete = _allocated(row.get("trash") or row["path"], min(deadline, time.monotonic() + 0.2))
                        if complete or measured > row["bytes"]:
                            row["bytes"] = measured
                        row["size_complete"] = complete
                    except OSError:
                        reason = "unknown: path changed during scan"
                row["measured"] = now
                row["last_reason"] = reason
                self._save(row)
            if reason == "eligible":
                selected_bytes += row["bytes"]
            items.append({"id": row["id"], "revision": row["revision"], "path": row["path"],
                          "reason": reason, "bytes": row["bytes"], "size_complete": row["size_complete"]})
        plan = {"id": uuid4().hex, "created": now, "expires": now + 600, "scope": scope,
                "pressure": pressure, "items": items, "scan_complete": scanned >= len([r for r in rows if r["state"] != "deleted"])}
        with self.connect(True) as db:
            db.execute("INSERT INTO plans VALUES(?,?)", (plan["id"], json.dumps(plan)))
            db.execute("DELETE FROM plans WHERE id!=? AND json_extract(data,'$.expires')<?", (plan["id"], now - DAY))
        return {"ok": True, **plan}

    def apply(self, identity, *, seconds=30):
        with self.connect() as db:
            found = db.execute("SELECT data FROM plans WHERE id=?", (identity,)).fetchone()
        if not found:
            raise ValueError("unknown cleanup plan")
        plan = json.loads(found[0])
        if time.time() > plan["expires"]:
            raise ValueError("cleanup plan expired; generate a new plan")
        deadline, results = time.monotonic() + min(30, seconds), []
        for item in plan["items"]:
            if item["reason"] != "eligible" or time.monotonic() >= deadline:
                continue
            try:
                with self.locked():
                    row = self.get(item["id"])
                    if row["revision"] != item["revision"]:
                        results.append({"id": row["id"], "result": "skipped_changed"})
                        continue
                    reason = self.classify(row, time.time(), plan["pressure"])
                    if reason != "eligible":
                        results.append({"id": row["id"], "result": "skipped", "reason": reason})
                        continue
                    from .artifact_references import deletion_guard
                    with deletion_guard(row):
                        self._delete(row, deadline)
                    results.append({"id": row["id"], "result": row["state"],
                                    "freed_bytes": row["bytes"] if row["state"] == "deleted" else 0})
            except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
                results.append({"id": item["id"], "result": "error", "reason": str(error)})
        return {"ok": all(r["result"] != "error" for r in results), "results": results,
                "freed_bytes": sum(r.get("freed_bytes", 0) for r in results),
                "budget_exhausted": time.monotonic() >= deadline}

    def _delete(self, row, deadline):
        if row["kind"] == "worktree":
            from .artifact_references import remove_worktree
            remove_worktree(row)
            row["state"] = "deleted"
            self._save(row, "deleted")
            return
        target = row.get("trash") or row["path"]
        with _parent_fd(row, target) as fd:
            if row["state"] not in {"quarantining", "quarantined", "deleting"} and row["kind"] not in {"scratch", "incomplete"}:
                row.update(state="quarantining", trash=str(Path(row["path"]).with_name(".auto-agents-trash-" + row["id"])), purge_after=time.time() + DAY)
                self._save(row, "quarantining")
                os.rename(Path(target).name, Path(row["trash"]).name, src_dir_fd=fd, dst_dir_fd=fd)
                row["state"] = "quarantined"
                self._save(row, "quarantined")
                return
            row["state"] = "deleting"
            self._save(row, "deleting")
            _remove_at(fd, Path(target).name, row["inode"][0], deadline)
            os.fsync(fd)
            row["state"] = "deleted"
            self._save(row, "deleted")

    def restore(self, identity):
        with self.locked():
            row = self.get(identity)
            if row["state"] not in {"quarantined", "quarantining"}:
                raise ValueError("artifact has no restorable quarantine copy")
            if os.path.lexists(row["path"]):
                raise ValueError("original path is occupied")
            with _parent_fd(row, row["trash"]) as fd:
                os.rename(Path(row["trash"]).name, Path(row["path"]).name, src_dir_fd=fd, dst_dir_fd=fd)
            row.update(state="released", released=time.time(), last_used=time.time())
            row.pop("trash", None)
            self._save(row, "restored")
        return {"ok": True, "id": identity, "path": row["path"]}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--maintain", action="store_true")
    parser.parse_args(argv)
    from auto_agents.artifact_runtime import maintain
    print(json.dumps(maintain()))


if __name__ == "__main__":
    main()
