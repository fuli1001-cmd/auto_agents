"""Only this store writes business state. Events are audit, never replay authority."""

from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3
import time
import uuid

from .types import (
    SCHEMA,
    Contract,
    ControlError,
    Status,
    TRANSITIONS,
    canonical,
    digest,
)


class Store:
    def __init__(self, project, *, readonly=False, path=None):
        self.project = Path(project).resolve()
        self.path = Path(path or self.project / ".auto-agents/state/business.sqlite3")
        self.readonly = readonly
        if self.path.is_symlink() or any(
            parent.is_symlink()
            for parent in (
                self.project / ".auto-agents",
                self.project / ".auto-agents/state",
            )
        ):
            raise ControlError("state_identity", "Business state path became a symlink")
        if self.path.exists():
            db = sqlite3.connect("file:" + str(self.path) + "?mode=ro", uri=True)
            try:
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if tables and "control_meta" not in tables:
                    raise ControlError(
                        "migration_required",
                        "Run auto-agents migrate-state before execution",
                        category="migration",
                    )
                if "control_meta" in tables:
                    version = db.execute(
                        "SELECT value FROM control_meta WHERE key='schema'"
                    ).fetchone()
                    if not version or int(version[0]) != SCHEMA:
                        raise ControlError(
                            "schema", "Unsupported business database schema"
                        )
            finally:
                db.close()
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(
            "file:" + str(self.path) + ("?mode=ro" if self.readonly else "?mode=rwc"),
            uri=True,
            timeout=30,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                if self.readonly:
                    raise ControlError("readonly", "Read-only state cannot be changed")
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS control_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workflows(id TEXT PRIMARY KEY,root TEXT NOT NULL,intent TEXT NOT NULL,
                    max_calls INTEGER,calls INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS contracts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_items(id TEXT PRIMARY KEY,workflow TEXT NOT NULL REFERENCES workflows(id),
                    parent TEXT,mode TEXT NOT NULL,phase TEXT NOT NULL,status TEXT NOT NULL,
                    contract TEXT NOT NULL REFERENCES contracts(id),context TEXT NOT NULL,result TEXT NOT NULL,
                    failure TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,calls INTEGER NOT NULL DEFAULT 0,
                    max_calls INTEGER NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,work TEXT NOT NULL REFERENCES work_items(id),
                    kind TEXT NOT NULL,ordinal INTEGER NOT NULL,input_hash TEXT NOT NULL,state TEXT NOT NULL,
                    result TEXT,consumed INTEGER NOT NULL DEFAULT 0,model INTEGER NOT NULL,provider TEXT NOT NULL,
                    created REAL NOT NULL,UNIQUE(work,kind,ordinal));
                CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,path TEXT NOT NULL,kind TEXT NOT NULL,
                    owner TEXT NOT NULL,identity TEXT NOT NULL,refs TEXT NOT NULL,metadata TEXT NOT NULL,
                    state TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,work TEXT NOT NULL,event TEXT NOT NULL,
                    data TEXT NOT NULL,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS work_parent ON work_items(parent);
                CREATE INDEX IF NOT EXISTS operations_work ON operations(work,kind,ordinal);
            """)
            if "inputs" not in {
                row["name"] for row in db.execute("PRAGMA table_info(operations)")
            }:
                db.execute("ALTER TABLE operations ADD COLUMN inputs TEXT")
            db.execute(
                "INSERT OR IGNORE INTO control_meta VALUES('schema',?)", (str(SCHEMA),)
            )
            db.execute("INSERT OR IGNORE INTO control_meta VALUES('aliases','{}')")
            db.commit()
            row = db.execute(
                "SELECT value FROM control_meta WHERE key='schema'"
            ).fetchone()
            if int(row[0]) != SCHEMA:
                raise ControlError("schema", "Unsupported business database schema")

    def meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM control_meta WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connect(True) as db:
            db.execute(
                "INSERT OR REPLACE INTO control_meta VALUES(?,?)",
                (key, canonical(value)),
            )

    def create_workflow(
        self,
        mode,
        goal,
        source,
        *,
        authorization=None,
        inputs=None,
        scope=(),
        checks=(),
        max_calls=None,
        node_limit=15,
        identity=None,
    ):
        if (
            type(node_limit) is not int
            or node_limit <= 0
            or max_calls is not None
            and (type(max_calls) is not int or max_calls <= 0)
        ):
            raise ControlError("budget", "Call limits must be positive integers")
        work = identity or uuid.uuid4().hex[:12]
        workflow = "wf-" + uuid.uuid4().hex[:12]
        contract = Contract(
            workflow,
            work,
            mode,
            goal,
            source,
            authorization or {},
            tuple(scope),
            tuple(checks),
            inputs or {},
        )
        phase = (
            "clarify"
            if mode == "run"
            else (
                "classify"
                if mode == "fix"
                else "research" if mode == "provider_resolve" else "diagnose"
            )
        )
        with self.connect(True) as db:
            db.execute(
                "INSERT INTO workflows VALUES(?,?,?,?,?,?)",
                (
                    workflow,
                    work,
                    canonical({"goal": goal, "authorization": authorization or {}}),
                    max_calls,
                    0,
                    time.time(),
                ),
            )
            self._insert_work(db, contract, None, phase, node_limit)
        return self.work(work)

    def _insert_work(self, db, contract, parent, phase, limit):
        db.execute(
            "INSERT OR IGNORE INTO contracts VALUES(?,?)",
            (contract.identity, canonical(contract.to_dict())),
        )
        db.execute(
            "INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                contract.work_id,
                contract.workflow_id,
                parent,
                contract.mode,
                phase,
                "READY",
                contract.identity,
                "{}",
                "{}",
                "{}",
                0,
                0,
                limit,
                time.time(),
            ),
        )

    def child(
        self,
        parent,
        mode,
        goal,
        source,
        *,
        inputs=None,
        scope=(),
        checks=(),
        limit=15,
        operation=None,
    ):
        work = uuid.uuid4().hex[:12]
        pc = self.contract(parent["contract"])
        c = Contract(
            parent["workflow"],
            work,
            mode,
            goal,
            source,
            pc.authorization,
            tuple(scope),
            tuple(checks),
            inputs or {},
            pc.identity,
        )
        with self.connect(True) as db:
            self._insert_work(
                db,
                c,
                parent["id"],
                (
                    "clarify"
                    if mode == "run"
                    else "classify" if mode == "fix" else "research"
                ),
                limit,
            )
            self._transition(
                db,
                parent,
                "WAITING",
                context={**parent["context"], "child": work, "waiting_for": "child"},
            )
            if operation:
                self._consume(db, operation)
        return self.work(work)

    def batch(self, parent, tasks, source):
        pc = self.contract(parent["contract"])
        children = []
        with self.connect(True) as db:
            for task in tasks:
                work = uuid.uuid4().hex[:12]
                from .types import VerificationSpec

                c = Contract(
                    parent["workflow"],
                    work,
                    "fix",
                    task["goal"],
                    source,
                    pc.authorization,
                    tuple(task.get("paths", ())),
                    tuple(VerificationSpec.read(x) for x in task["checks"]),
                    {
                        "planned_task": task["task_id"],
                        "persistence_change": task.get("persistence_change"),
                    },
                    pc.identity,
                )
                self._insert_work(db, c, parent["id"], "implement", 15)
                db.execute(
                    "UPDATE work_items SET context=? WHERE id=?",
                    (canonical({"task_id": task["task_id"]}), work),
                )
                children.append(work)
            self._transition(
                db,
                parent,
                "WAITING",
                context={
                    **parent["context"],
                    "children": children,
                    "waiting_for": "children",
                },
            )
        return [self.work(x) for x in children]

    def addon(self, parent, goal, source, *, scope, inputs, phase):
        """An explicit operator artifact request shares the original call budget."""
        pc = self.contract(parent["contract"])
        identity = uuid.uuid4().hex[:12]
        contract = Contract(
            parent["workflow"],
            identity,
            "run",
            goal,
            source,
            pc.authorization,
            tuple(scope),
            (),
            inputs,
            pc.identity,
        )
        with self.connect(True) as db:
            self._insert_work(db, contract, parent["id"], phase, parent["max_calls"])
        return self.work(identity)

    @staticmethod
    def _decode(row):
        value = dict(row)
        for key in ("context", "result", "failure", "intent"):
            if key in value:
                value[key] = json.loads(value[key])
        return value

    def work(self, identity):
        aliases = self.meta("aliases", {})
        identity = aliases.get(identity, identity)
        if str(identity).startswith("wf-"):
            identity = self.workflow(identity)["root"]
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM work_items WHERE id=?", (identity,)
            ).fetchone()
        if not row:
            raise ControlError("unknown_work", "Unknown work item: " + str(identity))
        return self._decode(row)

    def works(self):
        with self.connect() as db:
            return [
                self._decode(r)
                for r in db.execute("SELECT * FROM work_items ORDER BY updated")
            ]

    def contract(self, identity):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM contracts WHERE id=?", (identity,)
            ).fetchone()
        if not row:
            raise ControlError("contract", "Missing contract")
        c = Contract.read(json.loads(row[0]))
        if c.identity != identity:
            raise ControlError("integrity", "Contract checksum changed")
        return c

    def workflow(self, identity):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM workflows WHERE id=?", (identity,)
            ).fetchone()
        if not row:
            raise ControlError("unknown_workflow", "Unknown workflow")
        return self._decode(row)

    def transition(self, work, status, *, operation=None, operations=(), **changes):
        status = status.value if isinstance(status, Status) else status
        with self.connect(True) as db:
            self._transition(db, work, status, **changes)
            if operation:
                self._consume(db, operation)
            for identity in operations:
                self._consume(db, identity)
        return self.work(work["id"])

    def _transition(self, db, work, status, **changes):
        current = db.execute(
            "SELECT * FROM work_items WHERE id=?", (work["id"],)
        ).fetchone()
        if not current or current["revision"] != work["revision"]:
            raise ControlError("stale_revision", "Work item changed after it was read")
        if status != current["status"] and status not in TRANSITIONS[current["status"]]:
            raise ControlError(
                "transition",
                "Invalid transition " + current["status"] + " -> " + status,
            )
        if set(changes) - {"phase", "context", "result", "failure"}:
            raise ControlError(
                "authority", "Transitions cannot change identity, contract or budget"
            )
        if (
            status == "COMPLETED"
            and db.execute(
                "SELECT id FROM operations WHERE state IN ('UNKNOWN','DISPATCHED') AND (work=? OR (? IS NULL AND work IN (SELECT id FROM work_items WHERE workflow=?)))",
                (work["id"], current["parent"], work["workflow"]),
            ).fetchone()
        ):
            raise ControlError(
                "outcome_unknown",
                "Completion requires reconciliation of external operations",
                category="reconciliation",
            )
        values = {
            k: changes.get(
                k,
                (
                    json.loads(current[k])
                    if k in {"context", "result", "failure"}
                    else current[k]
                ),
            )
            for k in ("phase", "context", "result", "failure")
        }
        db.execute(
            "UPDATE work_items SET status=?,phase=?,context=?,result=?,failure=?,revision=revision+1,updated=? WHERE id=?",
            (
                status,
                values["phase"],
                canonical(values["context"]),
                canonical(values["result"]),
                canonical(values["failure"]),
                time.time(),
                work["id"],
            ),
        )
        self._event(
            db,
            work["id"],
            "transition",
            {"from": current["status"], "to": status, "phase": values["phase"]},
        )

    def bind_contract(
        self, work, contract, *, next_phase=None, context=None, operation=None
    ):
        if (
            contract.work_id != work["id"]
            or contract.workflow_id != work["workflow"]
            or contract.mode != work["mode"]
        ):
            raise ControlError("authority", "Contract identity changed")
        previous = self.contract(work["contract"])
        if previous.checks and contract.checks != previous.checks:
            raise ControlError("authority", "Bound verification cannot change")
        if previous.scope and contract.scope != previous.scope:
            raise ControlError("authority", "Bound source scope cannot change")
        if (
            contract.goal != previous.goal
            or contract.source != previous.source
            or contract.authorization != previous.authorization
            or contract.inputs != previous.inputs
            or contract.parent_contract != previous.parent_contract
        ):
            raise ControlError(
                "authority", "Contract goal, source or authorization changed"
            )
        with self.connect(True) as db:
            row = db.execute(
                "SELECT revision,status FROM work_items WHERE id=?", (work["id"],)
            ).fetchone()
            if row["revision"] != work["revision"] or work["phase"] not in {
                "classify",
                "plan",
                "provider_research",
            }:
                raise ControlError(
                    "contract",
                    "Contract can be completed only by its classification/planning owner",
                )
            db.execute(
                "INSERT OR IGNORE INTO contracts VALUES(?,?)",
                (contract.identity, canonical(contract.to_dict())),
            )
            db.execute(
                "UPDATE work_items SET contract=?,revision=revision+1,updated=? WHERE id=?",
                (contract.identity, time.time(), work["id"]),
            )
            self._event(
                db, work["id"], "contract_bound", {"contract": contract.identity}
            )
            if next_phase is not None:
                db.execute(
                    "UPDATE work_items SET status=?,phase=?,context=? WHERE id=?",
                    (
                        "READY",
                        next_phase,
                        canonical(context or work["context"]),
                        work["id"],
                    ),
                )
                if operation:
                    self._consume(db, operation)
        return self.work(work["id"])

    def reserve(self, work, kind, ordinal, inputs, *, model=False, provider=""):
        identity = uuid.uuid5(
            uuid.NAMESPACE_URL, work["id"] + ":" + kind + ":" + str(ordinal)
        ).hex
        with self.connect(True) as db:
            old = db.execute(
                "SELECT * FROM operations WHERE id=?", (identity,)
            ).fetchone()
            if old:
                if old["input_hash"] != digest(inputs):
                    raise ControlError(
                        "operation_inputs", "Retained operation inputs changed"
                    )
                if old["state"] in {"DISPATCHED", "UNKNOWN"}:
                    raise ControlError(
                        "outcome_unknown",
                        "Reconcile operation " + identity,
                        category="reconciliation",
                    )
                return dict(old)
            node = db.execute(
                "SELECT * FROM work_items WHERE id=?", (work["id"],)
            ).fetchone()
            workflow = db.execute(
                "SELECT * FROM workflows WHERE id=?", (work["workflow"],)
            ).fetchone()
            if node["revision"] != work["revision"] or node["status"] != "RUNNING":
                raise ControlError(
                    "revision", "Execution authority changed before dispatch"
                )
            unassigned = db.execute(
                "SELECT key FROM control_meta WHERE key LIKE 'unassigned_call:%' AND json_extract(value,'$.state') IN ('dispatched','unknown')"
            ).fetchone()
            if unassigned:
                raise ControlError(
                    "outcome_unknown",
                    "Reconcile imported operation " + unassigned[0],
                    category="reconciliation",
                )
            unknown = db.execute(
                "SELECT id FROM operations WHERE work IN (SELECT id FROM work_items WHERE workflow=?) AND state='UNKNOWN'",
                (work["workflow"],),
            ).fetchone()
            if unknown:
                raise ControlError(
                    "outcome_unknown",
                    "Reconcile operation " + unknown[0],
                    category="reconciliation",
                )
            if model and (
                node["calls"] >= node["max_calls"]
                or workflow["max_calls"] is not None
                and workflow["calls"] >= workflow["max_calls"]
            ):
                raise ControlError(
                    "budget", "Provider call limit reached", category="budget"
                )
            db.execute(
                "INSERT INTO operations(id,work,kind,ordinal,input_hash,state,result,consumed,model,provider,created,inputs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    work["id"],
                    kind,
                    ordinal,
                    digest(inputs),
                    "DISPATCHED",
                    None,
                    0,
                    int(model),
                    provider,
                    time.time(),
                    canonical(inputs),
                ),
            )
            if model:
                db.execute(
                    "UPDATE work_items SET calls=calls+1 WHERE id=?", (work["id"],)
                )
                db.execute(
                    "UPDATE workflows SET calls=calls+1 WHERE id=?", (work["workflow"],)
                )
            self._event(
                db,
                work["id"],
                "operation_reserved",
                {"operation": identity, "kind": kind, "model": model},
            )
        return {"id": identity, "state": "DISPATCHED", "result": None, "new": True}

    def settle(self, identity, result, *, state="CONFIRMED"):
        if state not in {"CONFIRMED", "FAILED", "UNKNOWN"}:
            raise ControlError("operation_state", "Invalid operation outcome")
        with self.connect(True) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=?", (identity,)
            ).fetchone()
            if not row or row["state"] not in {"DISPATCHED", "UNKNOWN"}:
                raise ControlError(
                    "operation_state", "Operation already settled or missing"
                )
            db.execute(
                "UPDATE operations SET state=?,result=? WHERE id=?",
                (state, canonical(result), identity),
            )
            self._event(
                db,
                row["work"],
                "operation_settled",
                {"operation": identity, "state": state},
            )

    def derive_receipt(self, identity, result):
        """Attach controller-derived artifacts to an unconsumed confirmed reply."""
        with self.connect(True) as db:
            row = db.execute(
                "SELECT state,consumed,result FROM operations WHERE id=?", (identity,)
            ).fetchone()
            if not row or row["state"] != "CONFIRMED" or row["consumed"]:
                raise ControlError(
                    "receipt", "Receipt is no longer owned by this phase"
                )
            original = json.loads(row["result"])
            if any(
                result.get(k) != v
                for k, v in original.items()
                if k not in {"workspace_hash", "worker_workspace_hash"}
            ):
                raise ControlError(
                    "receipt", "Provider result fields cannot be changed"
                )
            db.execute(
                "UPDATE operations SET result=? WHERE id=?",
                (canonical(result), identity),
            )

    @staticmethod
    def _consume(db, identity):
        row = db.execute(
            "SELECT state FROM operations WHERE id=?", (identity,)
        ).fetchone()
        if not row or row[0] not in {"CONFIRMED", "FAILED"}:
            raise ControlError(
                "outcome_unknown", "Unconfirmed operation cannot be consumed"
            )
        db.execute("UPDATE operations SET consumed=1 WHERE id=?", (identity,))

    def consume(self, identity):
        with self.connect(True) as db:
            self._consume(db, identity)

    def operations(self, work=None):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM operations"
                + (" WHERE work=?" if work else "")
                + " ORDER BY created",
                (work,) if work else (),
            ).fetchall()
        return [
            {
                **dict(r),
                "result": json.loads(r["result"]) if r["result"] else None,
                "inputs": json.loads(r["inputs"]) if r["inputs"] else None,
            }
            for r in rows
        ]

    def event(self, work, event, data):
        with self.connect(True) as db:
            self._event(db, work, event, data)

    @staticmethod
    def _event(db, work, event, data):
        db.execute(
            "INSERT INTO events(work,event,data,created) VALUES(?,?,?,?)",
            (work, event, canonical(data), time.time()),
        )

    def orphan_calls(self):
        with self.connect(True) as db:
            db.execute(
                "UPDATE operations SET state='UNKNOWN' WHERE state='DISPATCHED' AND model=1"
            )
            # Local verification is repeatable only after the original process has quiesced.
            db.execute(
                "UPDATE operations SET state='UNKNOWN',result=? WHERE state='DISPATCHED' AND model=0 AND kind IN ('deliver','persistence')",
                (
                    canonical(
                        {
                            "code": "mutation_interrupted",
                            "message": "Reconcile a local mutation before continuing",
                            "category": "reconciliation",
                        }
                    ),
                ),
            )
            db.execute(
                "UPDATE operations SET state='FAILED',consumed=1,result=? WHERE state='DISPATCHED' AND model=0",
                (
                    canonical(
                        {
                            "code": "interrupted",
                            "message": "Local operation interrupted after process quiescence",
                            "category": "environment",
                        }
                    ),
                ),
            )
