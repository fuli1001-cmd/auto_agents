from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional
from uuid import uuid4

from .config import state_dir
from .git_ops import commit_only_paths, head_ref
from .io_utils import read_json, read_text, write_json, write_text


WORKFLOW_SCHEMA_VERSION = 1
HANDOFF_SCHEMA_VERSION = 1
WORKFLOW_KINDS = {"collab", "fix", "run", "provider_resolve"}
HANDOFF_TARGETS = {"fix", "run", "resume"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_workflow_id() -> str:
    return f"wf-{uuid4().hex[:12]}"


def new_handoff_id() -> str:
    return f"hf-{uuid4().hex[:12]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkflowRef:
    kind: str
    native_id: str

    def __post_init__(self) -> None:
        if self.kind not in WORKFLOW_KINDS:
            raise ValueError(f"unsupported workflow kind: {self.kind}")
        if not str(self.native_id).strip():
            raise ValueError("workflow native_id must not be empty")

    @classmethod
    def from_dict(cls, payload: object) -> "WorkflowRef":
        data = dict(payload) if isinstance(payload, dict) else {}
        return cls(kind=str(data.get("kind", "")), native_id=str(data.get("native_id", "")))

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "native_id": self.native_id}


@dataclass
class WorkflowHandoff:
    handoff_id: str
    workflow_id: str
    parent: WorkflowRef
    target: str
    goal: str
    reason: str
    status: str = "prepared"
    child: Optional[WorkflowRef] = None
    input_ref: str = ""
    input_sha256: str = ""
    payload: Dict[str, object] = field(default_factory=dict)
    result: Dict[str, object] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    returned_at: str = ""
    consumed_operation_id: str = ""

    @classmethod
    def from_dict(cls, payload: object) -> "WorkflowHandoff":
        data = dict(payload) if isinstance(payload, dict) else {}
        child_payload = data.get("child")
        return cls(
            handoff_id=str(data.get("handoff_id", "")),
            workflow_id=str(data.get("workflow_id", "")),
            parent=WorkflowRef.from_dict(data.get("parent")),
            target=str(data.get("target", "")),
            goal=str(data.get("goal", "")),
            reason=str(data.get("reason", "")),
            status=str(data.get("status", "prepared")),
            child=(WorkflowRef.from_dict(child_payload) if isinstance(child_payload, dict) else None),
            input_ref=str(data.get("input_ref", "")),
            input_sha256=str(data.get("input_sha256", "")),
            payload=dict(data.get("payload", {})) if isinstance(data.get("payload"), dict) else {},
            result=dict(data.get("result", {})) if isinstance(data.get("result"), dict) else {},
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            returned_at=str(data.get("returned_at", "")),
            consumed_operation_id=str(data.get("consumed_operation_id", "")),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "handoff_id": self.handoff_id,
            "workflow_id": self.workflow_id,
            "parent": self.parent.to_dict(),
            "target": self.target,
            "goal": self.goal,
            "reason": self.reason,
            "status": self.status,
            "child": self.child.to_dict() if self.child is not None else None,
            "input_ref": self.input_ref,
            "input_sha256": self.input_sha256,
            "payload": dict(self.payload),
            "result": dict(self.result),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "returned_at": self.returned_at,
            "consumed_operation_id": self.consumed_operation_id,
        }


@dataclass
class WorkflowSnapshot:
    workflow_id: str
    root: WorkflowRef
    status: str = "active"
    active_frame: Optional[WorkflowRef] = None
    active_handoff_id: str = ""
    event_sequence: int = 0
    last_event_sha256: str = ""
    resume_epoch: int = 0
    recovery_required: bool = False
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, payload: object) -> "WorkflowSnapshot":
        data = dict(payload) if isinstance(payload, dict) else {}
        active = data.get("active_frame")
        return cls(
            workflow_id=str(data.get("workflow_id", "")),
            root=WorkflowRef.from_dict(data.get("root")),
            status=str(data.get("status", "active")),
            active_frame=(WorkflowRef.from_dict(active) if isinstance(active, dict) else None),
            active_handoff_id=str(data.get("active_handoff_id", "")),
            event_sequence=int(data.get("event_sequence", 0) or 0),
            last_event_sha256=str(data.get("last_event_sha256", "")),
            resume_epoch=int(data.get("resume_epoch", 0) or 0),
            recovery_required=bool(data.get("recovery_required", False)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "root": self.root.to_dict(),
            "status": self.status,
            "active_frame": self.active_frame.to_dict() if self.active_frame else None,
            "active_handoff_id": self.active_handoff_id,
            "event_sequence": self.event_sequence,
            "last_event_sha256": self.last_event_sha256,
            "resume_epoch": self.resume_epoch,
            "recovery_required": self.recovery_required,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkflowStore:
    """Durable workflow-chain registry and append-only transition journal."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.root = state_dir(self.project_root) / "workflows"
        self.handoffs_root = state_dir(self.project_root) / "handoffs"
        self.active_path = self.root / "active.json"

    def workflow_root(self, workflow_id: str) -> Path:
        return self.root / _safe_component(workflow_id)

    def snapshot_path(self, workflow_id: str) -> Path:
        return self.workflow_root(workflow_id) / "workflow.json"

    def handoff_path(self, handoff_id: str) -> Path:
        return self.handoffs_root / f"{_safe_component(handoff_id)}.json"

    def event_index_path(self, workflow_id: str) -> Path:
        return self.workflow_root(workflow_id) / "event_index.sqlite3"

    def create_root(
        self,
        root: WorkflowRef,
        *,
        workflow_id: str = "",
        activate: bool = True,
    ) -> WorkflowSnapshot:
        identifier = workflow_id or new_workflow_id()
        path = self.snapshot_path(identifier)
        if path.exists():
            snapshot = self.load(identifier)
        else:
            now = utc_now()
            snapshot = WorkflowSnapshot(
                workflow_id=identifier,
                root=root,
                active_frame=root,
                created_at=now,
                updated_at=now,
            )
            self.save(snapshot)
            self.append_event(snapshot, "workflow_started", details={"root": root.to_dict()})
        if activate:
            self.activate(snapshot.workflow_id)
        return snapshot

    def load(self, workflow_id: str) -> WorkflowSnapshot:
        try:
            payload = read_json(self.snapshot_path(workflow_id), default=None)
        except (OSError, ValueError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            rebuilt = self.rebuild(workflow_id)
            if rebuilt is None:
                raise FileNotFoundError(f"workflow not found: {workflow_id}")
            return rebuilt
        return WorkflowSnapshot.from_dict(payload)

    def rebuild(self, workflow_id: str) -> Optional[WorkflowSnapshot]:
        event_root = self.workflow_root(workflow_id) / "events"
        if not event_root.is_dir():
            return None
        root_ref: Optional[WorkflowRef] = None
        snapshot: Optional[WorkflowSnapshot] = None
        previous_sha = ""
        for path in sorted(event_root.glob("*.json")):
            try:
                payload = read_json(path, default=None)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            claimed = str(payload.get("event_sha256", ""))
            material = dict(payload)
            material.pop("event_sha256", None)
            actual = sha256_text(
                json.dumps(material, ensure_ascii=False, sort_keys=True)
            )
            if claimed != actual or str(payload.get("previous_event_sha256", "")) != previous_sha:
                raise RuntimeError(f"workflow journal hash chain is invalid at {path}")
            previous_sha = claimed
            details = payload.get("details", {})
            details = dict(details) if isinstance(details, dict) else {}
            if root_ref is None and str(payload.get("kind", "")) == "workflow_started":
                root_ref = WorkflowRef.from_dict(details.get("root"))
                snapshot = WorkflowSnapshot(
                    workflow_id=workflow_id,
                    root=root_ref,
                    active_frame=root_ref,
                    created_at=str(payload.get("created_at", "")),
                )
            if snapshot is None:
                continue
            active = payload.get("active_frame")
            if isinstance(active, dict):
                snapshot.active_frame = WorkflowRef.from_dict(active)
            snapshot.active_handoff_id = str(payload.get("active_handoff_id", ""))
            kind = str(payload.get("kind", ""))
            if kind == "workflow_terminal":
                snapshot.status = str(details.get("status", "completed"))
            elif kind == "recovery_required":
                snapshot.recovery_required = True
            elif kind == "workflow_resumed":
                snapshot.recovery_required = False
                snapshot.resume_epoch = int(details.get("resume_epoch", 0) or 0)
            snapshot.event_sequence = int(payload.get("sequence", 0) or 0)
            snapshot.last_event_sha256 = claimed
            snapshot.updated_at = str(payload.get("created_at", ""))
        if snapshot is None:
            return None
        self.save(snapshot)
        return snapshot

    def save(self, snapshot: WorkflowSnapshot) -> None:
        snapshot.updated_at = utc_now()
        write_json(self.snapshot_path(snapshot.workflow_id), snapshot.to_dict())

    def activate(self, workflow_id: str) -> None:
        snapshot = self.load(workflow_id)
        write_json(
            self.active_path,
            {
                "schema_version": WORKFLOW_SCHEMA_VERSION,
                "workflow_id": workflow_id,
                "root": snapshot.root.to_dict(),
                "updated_at": utc_now(),
            },
        )

    def active(self) -> Optional[WorkflowSnapshot]:
        try:
            payload = read_json(self.active_path, default={})
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
        workflow_id = str(payload.get("workflow_id", "")) if isinstance(payload, dict) else ""
        if not workflow_id:
            return None
        try:
            return self.load(workflow_id)
        except FileNotFoundError:
            return None

    def clear_active(self, workflow_id: str) -> None:
        payload = read_json(self.active_path, default={})
        if isinstance(payload, dict) and str(payload.get("workflow_id", "")) == workflow_id:
            write_json(self.active_path, {"schema_version": WORKFLOW_SCHEMA_VERSION})

    def append_event(
        self,
        snapshot: WorkflowSnapshot,
        kind: str,
        *,
        operation_id: str = "",
        details: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        try:
            durable = self.load(snapshot.workflow_id)
        except FileNotFoundError:
            durable = snapshot
        snapshot.event_sequence = durable.event_sequence
        snapshot.last_event_sha256 = durable.last_event_sha256
        snapshot.active_frame = durable.active_frame
        snapshot.active_handoff_id = durable.active_handoff_id
        snapshot.status = durable.status
        snapshot.resume_epoch = durable.resume_epoch
        snapshot.recovery_required = durable.recovery_required
        sequence = durable.event_sequence + 1
        event_id = uuid4().hex[:12]
        payload: Dict[str, object] = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": event_id,
            "operation_id": operation_id,
            "kind": str(kind),
            "workflow_id": snapshot.workflow_id,
            "active_frame": durable.active_frame.to_dict() if durable.active_frame else None,
            "active_handoff_id": durable.active_handoff_id,
            "previous_event_sha256": durable.last_event_sha256,
            "details": dict(details or {}),
            "created_at": utc_now(),
        }
        event_sha = sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        payload["event_sha256"] = event_sha
        event_path = (
            self.workflow_root(snapshot.workflow_id)
            / "events"
            / f"{sequence:08d}-{event_id}.json"
        )
        write_json(event_path, payload)
        self._index_event(snapshot.workflow_id, payload, event_path)
        snapshot.event_sequence = sequence
        snapshot.last_event_sha256 = event_sha
        self.save(snapshot)
        return payload

    def prepare_handoff(
        self,
        snapshot: WorkflowSnapshot,
        *,
        parent: WorkflowRef,
        target: str,
        goal: str,
        reason: str,
        input_ref: str = "",
        input_sha256: str = "",
        payload: Optional[Dict[str, object]] = None,
        handoff_id: str = "",
    ) -> WorkflowHandoff:
        if target not in HANDOFF_TARGETS:
            raise ValueError(f"unsupported handoff target: {target}")
        identifier = handoff_id or new_handoff_id()
        path = self.handoff_path(identifier)
        if path.exists():
            return self.load_handoff(identifier)
        now = utc_now()
        handoff = WorkflowHandoff(
            handoff_id=identifier,
            workflow_id=snapshot.workflow_id,
            parent=parent,
            target=target,
            goal=str(goal).strip(),
            reason=str(reason).strip(),
            input_ref=str(input_ref),
            input_sha256=str(input_sha256),
            payload=dict(payload or {}),
            created_at=now,
            updated_at=now,
        )
        write_json(path, handoff.to_dict())
        snapshot.active_handoff_id = identifier
        self.save(snapshot)
        self.append_event(
            snapshot,
            "handoff_prepared",
            operation_id=identifier,
            details={"target": target, "parent": parent.to_dict()},
        )
        return handoff

    def load_handoff(self, handoff_id: str) -> WorkflowHandoff:
        payload = read_json(self.handoff_path(handoff_id), default=None)
        if not isinstance(payload, dict):
            raise FileNotFoundError(f"handoff not found: {handoff_id}")
        return WorkflowHandoff.from_dict(payload)

    def save_handoff(self, handoff: WorkflowHandoff) -> None:
        handoff.updated_at = utc_now()
        write_json(self.handoff_path(handoff.handoff_id), handoff.to_dict())

    def bind_child(
        self,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
        child: WorkflowRef,
    ) -> None:
        if handoff.child is not None and handoff.child != child:
            raise RuntimeError(
                f"handoff {handoff.handoff_id} is already bound to "
                f"{handoff.child.kind}:{handoff.child.native_id}"
            )
        handoff.child = child
        handoff.status = "running"
        self.save_handoff(handoff)
        snapshot.active_frame = child
        snapshot.active_handoff_id = handoff.handoff_id
        self.save(snapshot)
        self.append_event(
            snapshot,
            "handoff_child_bound",
            operation_id=handoff.handoff_id,
            details={"child": child.to_dict()},
        )

    def record_result(
        self,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
        *,
        status: str,
        result: Dict[str, object],
    ) -> None:
        handoff.status = str(status)
        handoff.result = dict(result)
        self.save_handoff(handoff)
        self.append_event(
            snapshot,
            "handoff_result",
            operation_id=handoff.handoff_id,
            details={"status": status, "result_sha256": sha256_text(json.dumps(result, ensure_ascii=False, sort_keys=True))},
        )

    def consume_result(
        self,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
        *,
        operation_id: str,
    ) -> None:
        if handoff.returned_at:
            return
        handoff.returned_at = utc_now()
        handoff.consumed_operation_id = operation_id
        self.save_handoff(handoff)
        snapshot.active_frame = handoff.parent
        snapshot.active_handoff_id = ""
        self.save(snapshot)
        self.append_event(
            snapshot,
            "handoff_returned",
            operation_id=operation_id,
            details={"handoff_id": handoff.handoff_id, "parent": handoff.parent.to_dict()},
        )

    def mark_recovery_required(
        self,
        snapshot: WorkflowSnapshot,
        *,
        reason: str,
        details: Optional[Dict[str, object]] = None,
    ) -> None:
        snapshot.recovery_required = True
        self.save(snapshot)
        payload = {"reason": str(reason)}
        payload.update(dict(details or {}))
        self.append_event(snapshot, "recovery_required", details=payload)

    def begin_resume(self, snapshot: WorkflowSnapshot) -> None:
        snapshot.resume_epoch += 1
        snapshot.recovery_required = False
        snapshot.status = "active"
        self.save(snapshot)
        self.append_event(
            snapshot,
            "workflow_resumed",
            details={"resume_epoch": snapshot.resume_epoch},
        )

    def complete(self, snapshot: WorkflowSnapshot, *, status: str = "completed") -> None:
        snapshot.status = status
        snapshot.active_handoff_id = ""
        self.save(snapshot)
        self.append_event(snapshot, "workflow_terminal", details={"status": status})
        if status == "completed":
            self.clear_active(snapshot.workflow_id)

    def resumable(self) -> List[WorkflowSnapshot]:
        candidates: List[WorkflowSnapshot] = []
        if not self.root.is_dir():
            return candidates
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                snapshot = self.load(path.name)
            except (FileNotFoundError, RuntimeError, TypeError, ValueError):
                continue
            if snapshot.status != "completed":
                candidates.append(snapshot)
        return sorted(candidates, key=lambda item: item.updated_at, reverse=True)

    def events(self, workflow_id: str) -> List[Dict[str, object]]:
        indexed = self._indexed_events(workflow_id)
        if indexed is not None:
            return indexed
        event_root = self.workflow_root(workflow_id) / "events"
        if not event_root.is_dir():
            return []
        payloads: List[Dict[str, object]] = []
        records: List[tuple[Path, Dict[str, object]]] = []
        previous_sha = ""
        for path in sorted(event_root.glob("*.json")):
            payload = read_json(path, default=None)
            if not isinstance(payload, dict):
                continue
            claimed = str(payload.get("event_sha256", ""))
            material = dict(payload)
            material.pop("event_sha256", None)
            actual = sha256_text(
                json.dumps(material, ensure_ascii=False, sort_keys=True)
            )
            if claimed != actual or str(payload.get("previous_event_sha256", "")) != previous_sha:
                raise RuntimeError(f"workflow journal hash chain is invalid at {path}")
            previous_sha = claimed
            copied = dict(payload)
            payloads.append(copied)
            records.append((path, copied))
        self._rebuild_event_index(workflow_id, records)
        return payloads

    def _indexed_events(
        self,
        workflow_id: str,
    ) -> Optional[List[Dict[str, object]]]:
        path = self.event_index_path(workflow_id)
        if not path.is_file():
            return None
        try:
            with closing(sqlite3.connect(path)) as connection, connection:
                self._ensure_event_index(connection)
                rows = connection.execute(
                    "SELECT payload, source_name, source_size, source_mtime_ns "
                    "FROM workflow_events ORDER BY sequence"
                ).fetchall()
        except sqlite3.Error:
            return None
        snapshot = self.load(workflow_id)
        if len(rows) != snapshot.event_sequence:
            return None
        event_paths = sorted(
            (self.workflow_root(workflow_id) / "events").glob("*.json")
        )
        if len(event_paths) != len(rows):
            return None
        try:
            for row, source in zip(rows, event_paths):
                stat = source.stat()
                if (
                    str(row[1]) != source.name
                    or int(row[2] or -1) != stat.st_size
                    or int(row[3] or -1) != stat.st_mtime_ns
                ):
                    return None
        except OSError:
            return None
        payloads: List[Dict[str, object]] = []
        previous_sha = ""
        for row in rows:
            try:
                payload = json.loads(str(row[0]))
            except json.JSONDecodeError:
                return None
            if not isinstance(payload, dict):
                return None
            claimed = str(payload.get("event_sha256", ""))
            material = dict(payload)
            material.pop("event_sha256", None)
            actual = sha256_text(
                json.dumps(material, ensure_ascii=False, sort_keys=True)
            )
            if (
                claimed != actual
                or str(payload.get("previous_event_sha256", "")) != previous_sha
            ):
                return None
            previous_sha = claimed
            payloads.append(payload)
        if previous_sha != snapshot.last_event_sha256:
            return None
        return payloads

    def _index_event(
        self,
        workflow_id: str,
        payload: Mapping[str, object],
        source_path: Path,
    ) -> None:
        path = self.event_index_path(workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(path)) as connection, connection:
                self._ensure_event_index(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO workflow_events "
                    "(sequence, event_sha256, source_name, source_size, "
                    "source_mtime_ns, payload) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        int(payload.get("sequence", 0) or 0),
                        str(payload.get("event_sha256", "")),
                        source_path.name,
                        source_path.stat().st_size,
                        source_path.stat().st_mtime_ns,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return

    def _rebuild_event_index(
        self,
        workflow_id: str,
        records: List[tuple[Path, Dict[str, object]]],
    ) -> None:
        path = self.event_index_path(workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(path)) as connection, connection:
                self._ensure_event_index(connection)
                connection.execute("DELETE FROM workflow_events")
                connection.executemany(
                    "INSERT INTO workflow_events "
                    "(sequence, event_sha256, source_name, source_size, "
                    "source_mtime_ns, payload) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            int(payload.get("sequence", 0) or 0),
                            str(payload.get("event_sha256", "")),
                            source.name,
                            source.stat().st_size,
                            source.stat().st_mtime_ns,
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        )
                        for source, payload in records
                    ],
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return

    @staticmethod
    def _ensure_event_index(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_events ("
            "sequence INTEGER PRIMARY KEY, "
            "event_sha256 TEXT NOT NULL, "
            "source_name TEXT NOT NULL DEFAULT '', "
            "source_size INTEGER NOT NULL DEFAULT -1, "
            "source_mtime_ns INTEGER NOT NULL DEFAULT -1, "
            "payload TEXT NOT NULL)"
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(workflow_events)")
        }
        for name, definition in (
            ("source_name", "TEXT NOT NULL DEFAULT ''"),
            ("source_size", "INTEGER NOT NULL DEFAULT -1"),
            ("source_mtime_ns", "INTEGER NOT NULL DEFAULT -1"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE workflow_events ADD COLUMN {name} {definition}"
                )


class IterationSpecBuilder:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).expanduser().resolve()

    def materialize(
        self,
        handoff: WorkflowHandoff,
        seed: Dict[str, object],
    ) -> Dict[str, str]:
        title = str(seed.get("title") or seed.get("summary") or handoff.goal or "Iteration").strip()
        slug = _slug(title)
        timestamp = _compact_timestamp(handoff.created_at)
        relative = Path("specs") / "iterations" / f"{timestamp}-{handoff.handoff_id}-{slug}.md"
        path = self.project_root / relative
        content = self.render(handoff, seed, title=title)
        digest = sha256_text(content)
        if path.exists():
            existing = read_text(path)
            if sha256_text(existing) != digest:
                raise RuntimeError(f"iteration spec already exists with different content: {relative}")
        else:
            write_text(path, content)
        commit_sha = commit_only_paths(
            self.project_root,
            f"docs(spec): capture {handoff.handoff_id} iteration request",
            [relative.as_posix()],
            trailers=[
                f"Auto-Agents-Operation: spec-{handoff.handoff_id}",
                f"Auto-Agents-Workflow: {handoff.workflow_id}",
            ],
        )
        return {
            "path": relative.as_posix(),
            "sha256": digest,
            "commit_sha": commit_sha or head_ref(self.project_root),
        }

    @staticmethod
    def render(
        handoff: WorkflowHandoff,
        seed: Dict[str, object],
        *,
        title: str,
    ) -> str:
        goal = str(seed.get("goal") or handoff.goal).strip()
        gap = str(seed.get("gap") or seed.get("actual") or handoff.reason).strip()
        capability = str(seed.get("capability") or seed.get("requested_change") or goal).strip()
        acceptance = _string_list(seed.get("acceptance"))
        non_goals = _string_list(seed.get("non_goals"))
        evidence = _string_list(seed.get("evidence"))
        open_decisions = _string_list(seed.get("open_decisions"))
        raw_goal_environment = seed.get("goal_execution_environment", {})
        goal_environment = (
            dict(raw_goal_environment)
            if isinstance(raw_goal_environment, dict)
            else {}
        )
        lines = [
            f"# Iteration Request: {title}",
            "",
            f"- Handoff: `{handoff.handoff_id}`",
            f"- Source: `{handoff.parent.kind}:{handoff.parent.native_id}`",
            "",
            "## Goal",
            "",
            goal or "Complete the requested iteration.",
            "",
            "## Current Gap",
            "",
            gap or "The current project does not yet satisfy the requested goal.",
            "",
            "## Requested Capability",
            "",
            capability or goal,
            "",
            "## Goal Execution Environment",
            "",
            (
                json.dumps(goal_environment, ensure_ascii=False, sort_keys=True)
                if goal_environment
                else "Not recorded; implementation must not infer an environment."
            ),
            "",
            "## Acceptance Criteria",
            "",
            *(_markdown_items(acceptance) or ["- Clarify and derive executable acceptance criteria before implementation."]),
            "",
            "## Non-goals",
            "",
            *(_markdown_items(non_goals) or ["- Do not change unrelated behavior."]),
            "",
            "## Evidence",
            "",
            *(_markdown_items(evidence) or ["- See the originating workflow handoff and session evidence."]),
            "",
            "## Open Decisions",
            "",
            *(_markdown_items(open_decisions) or ["- None recorded; clarify any newly discovered product decisions before implementation."]),
            "",
        ]
        return "\n".join(lines)


class IssueBriefBuilder:
    def __init__(self, project_root: Path, session_id: str) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.session_id = str(session_id)
        self.root = state_dir(self.project_root) / "sessions" / self.session_id

    def materialize(self, payload: Dict[str, object]) -> Dict[str, str]:
        issue = {
            "schema_version": 1,
            "issue_id": f"issue-{self.session_id}",
            "summary": str(payload.get("summary", "")).strip(),
            "reported_goal": str(payload.get("reported_goal") or payload.get("goal") or "").strip(),
            "decision": str(payload.get("decision", "")).strip(),
            "reason": str(payload.get("reason", "")).strip(),
            "reproduction": _string_list(payload.get("reproduction")),
            "expected": str(payload.get("expected", "")).strip(),
            "actual": str(payload.get("actual", "")).strip(),
            "evidence_refs": _string_list(payload.get("evidence_refs")),
            "affected_contracts": _string_list(payload.get("affected_contracts")),
            "constraints": _string_list(payload.get("constraints")),
            "verification_command": str(payload.get("verification_command", "")).strip(),
            "source_handoff_id": str(payload.get("source_handoff_id", "")).strip(),
            "updated_at": utc_now(),
        }
        json_path = self.root / "issue.json"
        markdown_path = self.root / "issue.md"
        write_json(json_path, issue)
        write_text(markdown_path, self.render(issue))
        return {
            "json": str(json_path.relative_to(self.project_root)),
            "markdown": str(markdown_path.relative_to(self.project_root)),
            "sha256": sha256_text(json.dumps(issue, ensure_ascii=False, sort_keys=True)),
        }

    @staticmethod
    def render(issue: Dict[str, object]) -> str:
        reproduction = _string_list(issue.get("reproduction"))
        evidence = _string_list(issue.get("evidence_refs"))
        contracts = _string_list(issue.get("affected_contracts"))
        constraints = _string_list(issue.get("constraints"))
        return "\n".join(
            [
                f"# Issue: {issue.get('summary') or issue.get('reported_goal') or issue.get('issue_id')}",
                "",
                f"- Decision: `{issue.get('decision') or 'unclassified'}`",
                f"- Source handoff: `{issue.get('source_handoff_id') or 'standalone'}`",
                "",
                "## Reported Goal",
                "",
                str(issue.get("reported_goal", "")) or "Not recorded.",
                "",
                "## Reproduction",
                "",
                *(_markdown_items(reproduction) or ["- Not yet recorded."]),
                "",
                "## Expected / Actual",
                "",
                f"- Expected: {issue.get('expected') or 'Not yet recorded.'}",
                f"- Actual: {issue.get('actual') or 'Not yet recorded.'}",
                "",
                "## Evidence",
                "",
                *(_markdown_items(evidence) or ["- No evidence references recorded."]),
                "",
                "## Affected Contracts",
                "",
                *(_markdown_items(contracts) or ["- No affected contract recorded."]),
                "",
                "## Constraints",
                "",
                *(_markdown_items(constraints) or ["- No additional constraints recorded."]),
                "",
                "## Verification",
                "",
                str(issue.get("verification_command", "")) or "Not yet recorded.",
                "",
                "## Classification Rationale",
                "",
                str(issue.get("reason", "")) or "Not yet recorded.",
                "",
            ]
        )


def _safe_component(value: str) -> str:
    normalized = str(value).strip()
    if not normalized or not re.fullmatch(r"[A-Za-z0-9_.-]+", normalized):
        raise ValueError(f"unsafe workflow identifier: {value!r}")
    return normalized


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return (normalized[:48].rstrip("-") or "iteration")


def _compact_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _string_list(value: object) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _markdown_items(values: Iterable[str]) -> List[str]:
    return [f"- {value}" for value in values if str(value).strip()]
