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
        with store.connect() as db:
            events = [
                dict(x)
                for x in db.execute(
                    "SELECT work,data,id FROM events WHERE event='step_entered' AND work IN (SELECT id FROM work_items WHERE workflow=?) ORDER BY id DESC LIMIT 32",
                    (root["workflow"],),
                )
            ]
        steps = [json.loads(x["data"]) for x in reversed(events)]
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
