"""Small process observations; no decisions, model calls or hidden state writes."""

from pathlib import Path
import json
import os
import threading
import time
from .store import Store
from .types import canonical


def milestones(store, workflow):
    items = []
    for work in store.works():
        if work["workflow"] != workflow:
            continue
        if work["status"] == "COMPLETED":
            items.append(work["id"] + ":" + work["contract"])
        for gate, receipt in work["context"].get("approvals", {}).items():
            from .types import digest

            items.append(work["id"] + ":" + gate + ":" + digest(receipt["hashes"]))
    with store.connect() as db:
        items.extend(
            "verified:" + str(row["id"])
            for row in db.execute(
                "SELECT id FROM events WHERE event='verified_progress' AND work IN (SELECT id FROM work_items WHERE workflow=?)",
                (workflow,),
            )
        )
    return items


def step_history(store, workflow, limit=32):
    """Count ordinary reentries while preserving finite correction allowances."""
    with store.connect() as db:
        # A confirmed cancellation is an operator decision too. Read its
        # consumed receipt so existing reconciliations need no state rewrite.
        # Older human decisions have only their WAITING -> READY transition;
        # completed children in those histories already supply milestones.
        boundary = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM ("
            "SELECT id FROM events WHERE event='transition' "
            "AND work IN (SELECT id FROM work_items WHERE workflow=?) AND ("
            "json_extract(data,'$.operator_decision')=1 OR ("
            "json_type(data,'$.operator_decision') IS NULL "
            "AND json_extract(data,'$.from')='WAITING' "
            "AND json_extract(data,'$.to')='READY')) UNION ALL "
            "SELECT e.id FROM events e JOIN operations o "
            "ON o.id=json_extract(e.data,'$.operation') "
            "WHERE e.event='operation_settled' AND o.state='FAILED' "
            "AND o.consumed=1 AND json_extract(o.result,'$.code')='provider_cancelled' "
            "AND o.work IN (SELECT id FROM work_items WHERE workflow=?))",
            (workflow, workflow),
        ).fetchone()[0]
        # Include the transitions before the oldest retained step, where its
        # retry may have been admitted. The preceding step bounds that scan.
        preceding = db.execute(
            "SELECT id FROM events WHERE event='step_entered' AND id>? "
            "AND work IN (SELECT id FROM work_items WHERE workflow=?) "
            "ORDER BY id DESC LIMIT 1 OFFSET ?",
            (boundary, workflow, limit),
        ).fetchone()
        events = db.execute(
            "SELECT work,event,data FROM events WHERE id>? "
            "AND event IN ('transition','step_entered') "
            "AND work IN (SELECT id FROM work_items WHERE workflow=?) ORDER BY id",
            (preceding[0] if preceding else boundary, workflow),
        ).fetchall()
    steps, retries = [], {}
    for row in events:
        data = json.loads(row["data"])
        if row["event"] == "transition":
            if data.get("bounded_retry"):
                retries[row["work"]] = data["phase"]
        else:
            if retries.pop(row["work"], None) == data["phase"]:
                data["counted"] = False
            steps.append(data)
    return steps[-limit:]


class Observer:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.output = os.environ.get("AUTO_AGENTS_OBSERVATION_FILE")
        self.stopped = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        self.current_fault = None
        self.waiting_for_goal = False

    def __enter__(self):
        if self.output:
            self.thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.thread.start()
        return self

    def _heartbeat(self):
        while not self.stopped.is_set():
            try:
                self.publish()
            except (OSError, ValueError, RuntimeError):
                pass
            self.stopped.wait(15)

    def publish(self):
        if not self.output:
            return
        with self.lock:
            self._publish()

    def _publish(self):
        store = Store(self.project, readonly=True)
        if not store.path.exists():
            return
        works = store.works()
        root_id = store.meta("active_root")
        root = next((w for w in works if w["id"] == root_id), None)
        if not root:
            self._write({
                "schema": 2, "project": str(self.project),
                "status": "waiting" if self.waiting_for_goal else "running",
                "subject": "", "root_subject": "",
                "phase": "goal" if self.waiting_for_goal else "startup",
                "waiting_for": "user" if self.waiting_for_goal else "",
                "progress_seq": 0, "milestones": [], "steps": [],
                "heartbeat_at": time.time(),
            })
            return
        active = next(
            (
                w
                for w in reversed(works)
                if w["workflow"] == root["workflow"]
                and (
                    w["status"] == "RUNNING"
                    or w["context"].get("waiting_for") in {"user", "approval"}
                )
            ),
            root,
        )
        progress = milestones(store, root["workflow"])
        steps = step_history(store, root["workflow"])
        from ..engine_fault import engine_root
        import subprocess

        revision = subprocess.run(
            ["git", "-C", str(engine_root()), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        value = {
            "schema": 2,
            "project": str(self.project),
            "status": (
                "completed"
                if root["status"] == "COMPLETED"
                else (
                    "stopped"
                    if root["status"] == "CANCELLED"
                    else (
                        "failed"
                        if root["status"] == "BLOCKED"
                        else "waiting" if active["status"] == "WAITING" else "running"
                    )
                )
            ),
            "subject": active["id"],
            "root_subject": root["id"],
            "phase": active["phase"],
            "step_id": active["id"] + ":" + active["phase"],
            "progress_seq": len(progress),
            "milestones": progress,
            "steps": steps,
            "waiting_for": active["context"].get("waiting_for", ""),
            "heartbeat_at": time.time(),
            "runtime_revision": revision,
        }
        from .interaction import pending_decisions

        value["pending_decisions"] = pending_decisions(store, root)
        path = Path(self.output)
        if self.current_fault:
            value["fault"] = self.current_fault
            value["status"] = "failed"
        elif root["status"] == "BLOCKED" and path.exists():
            old = json.loads(path.read_text())
            if old.get("fault") and old.get("root_subject", root["id"]) == root["id"]:
                value["fault"] = old["fault"]
        self._write(value)

    def _write(self, value):
        path = Path(self.output)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(canonical(value))
        os.replace(temporary, path)

    def fault(self, error, argv):
        from . import api
        from .types import ControlError
        from ..diagnostic_redaction import sanitize
        from .projection import current

        if error.category != "engine" or not current(self.project):
            return

        store = Store(self.project, readonly=True)
        if not store.path.exists():
            return
        try:
            root = store.meta("active_root")
        except (RuntimeError, ValueError):
            return
        if not root:
            return
        explicit = next(
            (
                argv[i + 1]
                for i, arg in enumerate(argv[:-1])
                if arg in {"--session", "--workflow"}
            ),
            "",
        )
        if explicit:
            try:
                selected = store.work(explicit)
            except ControlError:
                return
            if store.workflow(selected["workflow"])["root"] != root:
                return
        try:
            node = store.work(root)
            if node["status"] not in {"BLOCKED", "COMPLETED", "CANCELLED"}:
                Store(self.project).transition(node, "BLOCKED", failure=error.to_dict())
            if node["status"] in {"COMPLETED", "CANCELLED"}:
                return
            fault = api.checkpoint(self.project, argv, root, error)
            with self.lock:
                self.current_fault = fault
            self.publish()
            return fault
        except (ControlError, OSError):
            return

    def __exit__(self, *args):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.publish()
