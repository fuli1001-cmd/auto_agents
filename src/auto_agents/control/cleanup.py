"""Lifecycle collection of owned resources; receipts and live references survive."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import time

from .types import ControlError, canonical


def register(store, path, kind, owner, *, references=(), metadata=None):
    path = Path(path).absolute()
    info = path.stat()
    identity = canonical([info.st_dev, info.st_ino])
    with store.connect(True) as db:
        db.execute(
            "INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                str(path),
                str(path),
                kind,
                owner,
                identity,
                canonical(list(references)),
                canonical(metadata or {}),
                "LIVE",
                time.time(),
            ),
        )


def release(store, owner):
    with store.connect(True) as db:
        db.execute(
            "UPDATE artifacts SET refs='[]',state='RELEASED' WHERE owner=?", (owner,)
        )


def collect(store, *, seconds=10, git_gc=False):
    deadline = time.monotonic() + seconds
    removed = []
    errors = []
    with store.connect() as db:
        rows = [
            dict(x)
            for x in db.execute(
                "SELECT * FROM artifacts WHERE state IN ('RELEASED','DELETING') OR (kind='scratch' AND owner IN (SELECT work FROM operations WHERE state IN ('CONFIRMED','FAILED')))"
            )
        ]
    for row in rows:
        if time.monotonic() >= deadline:
            break
        pending = [
            o
            for o in store.operations(row["owner"])
            if o["state"] in {"DISPATCHED", "UNKNOWN"}
        ]
        if row["kind"] != "scratch" and pending:
            continue
        if row["kind"] == "scratch":
            if any(str(row["path"]).endswith(o["id"]) for o in pending):
                continue
        elif json.loads(row["refs"]):
            continue
        path = Path(row["path"])
        metadata = json.loads(row["metadata"])
        managed = store.project / ".auto-agents/state/owned"
        try:
            if row["kind"].startswith("docker_"):
                remove_docker(row, metadata)
            else:
                if (
                    managed.resolve() != managed
                    or path.is_symlink()
                    or path.resolve() != path
                    or not path.resolve().is_relative_to(managed.resolve())
                ):
                    raise ControlError(
                        "cleanup_scope", "Resource leaves its managed directory"
                    )
                if path.exists():
                    info = path.stat()
                    if canonical([info.st_dev, info.st_ino]) != row["identity"]:
                        raise ControlError(
                            "cleanup_identity", "Cleanup resource was replaced"
                        )
                    with store.connect(True) as db:
                        db.execute(
                            "UPDATE artifacts SET state='DELETING' WHERE id=?",
                            (row["id"],),
                        )
                    if row["kind"] == "workspace":
                        result = subprocess.run(
                            [
                                "git",
                                "--git-dir",
                                str(managed / "objects.git"),
                                "worktree",
                                "remove",
                                "--force",
                                str(path),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        if result.returncode:
                            raise ControlError("cleanup_workspace", result.stderr)
                    elif path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            with store.connect(True) as db:
                db.execute(
                    "UPDATE artifacts SET state='DELETED',metadata='{}' WHERE id=?",
                    (row["id"],),
                )
            removed.append(row["path"])
        except (OSError, ControlError, subprocess.SubprocessError) as error:
            errors.append({"path": str(path), "reason": str(error)})
    with store.connect(True) as db:
        # A consumed terminal operation needs identity and accounting, not the
        # model's duplicate prose. Unknown results are never compacted.
        db.execute(
            "UPDATE operations SET result=? WHERE consumed=1 AND state IN ('CONFIRMED','FAILED') AND work IN (SELECT id FROM work_items WHERE status IN ('COMPLETED','CANCELLED')) AND length(result)>65536",
            (canonical({"compacted": True}),),
        )
        db.execute(
            "UPDATE operations SET inputs=NULL WHERE consumed=1 AND state IN ('CONFIRMED','FAILED') AND work IN (SELECT id FROM work_items WHERE status IN ('COMPLETED','CANCELLED')) AND length(inputs)>65536"
        )
        # Audit receipts and call accounting stay. Repeated heartbeat/step
        # diagnostics need only a bounded recent tail after completion.
        db.execute(
            "DELETE FROM events WHERE work IN (SELECT id FROM work_items WHERE status IN ('COMPLETED','CANCELLED')) AND id NOT IN (SELECT id FROM events e WHERE e.work=events.work ORDER BY id DESC LIMIT 64)"
        )
        db.execute(
            "DELETE FROM artifacts WHERE state='DELETED' AND created<?",
            (time.time() - 7 * 86400,),
        )
    with store.connect() as db:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        db.execute("PRAGMA incremental_vacuum(128)")
    if git_gc:
        try:
            collect_git(store)
        except (OSError, ControlError, subprocess.SubprocessError) as error:
            errors.append({"path": "objects.git", "reason": str(error)})
    return {"ok": not errors, "removed": removed, "errors": errors}


def collect_git(store):
    """Called only after the project execution lock has quiesced all workers."""
    archive = store.project / ".auto-agents/state/owned/objects.git"
    if not archive.exists():
        return
    if archive.is_symlink():
        raise ControlError("cleanup_identity", "Git archive is a symlink")
    works = store.works()
    pending = {
        o["work"] for o in store.operations() if o["state"] in {"UNKNOWN", "DISPATCHED"}
    }
    live_workflows = {
        w["workflow"]
        for w in works
        if w["status"] not in {"COMPLETED", "CANCELLED"} or w["id"] in pending
    }
    live = [w for w in works if w["workflow"] in live_workflows]
    keep = {store.contract(w["contract"]).source for w in live}
    keep.update(w["context"].get("candidate", {}).get("revision") for w in live)
    keep.update(w["result"].get("candidate", {}).get("revision") for w in live)

    def bare(*args):
        result = subprocess.run(
            ["git", "--git-dir", str(archive), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise ControlError("cleanup_git", result.stderr)
        return result.stdout

    for line in bare("for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        ref, revision = line.split()
        if (
            ref.startswith(
                (
                    "refs/candidates/",
                    "refs/snapshots/",
                    "refs/imports/",
                    "refs/offline/",
                )
            )
            and revision not in keep
        ):
            bare("update-ref", "-d", ref, revision)
    bare("worktree", "prune")
    # Reflogs of owned, completed candidates are disposable, not user history.
    bare("reflog", "expire", "--expire=now", "--all")
    bare("gc", "--prune=now", "--quiet")


def remove_docker(row, metadata):
    identity = metadata.get("docker_id")
    owner = metadata.get("owner_label")
    if not identity or not owner:
        raise ControlError("cleanup_owner", "Docker ownership is unavailable")
    kind = {
        "docker_container": "container",
        "docker_image": "image",
        "docker_volume": "volume",
    }[row["kind"]]
    result = subprocess.run(
        ["docker", kind, "inspect", identity],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        return
    item = json.loads(result.stdout)[0]
    labels = item.get("Config", {}).get("Labels") or item.get("Labels") or {}
    if labels.get("auto-agents.owner") != owner:
        raise ControlError("cleanup_owner", "Docker ownership differs")
    if kind == "container" and item.get("State", {}).get("Running"):
        return
    if metadata.get("current_recipe"):
        return
    subprocess.run(
        ["docker", kind, "rm", identity], check=True, capture_output=True, timeout=30
    )
