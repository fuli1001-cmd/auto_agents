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
    NativeOutcome,
    ValidationResult,
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

    def has_features(self):
        with self.connect() as db:
            tables = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE name IN ('operation_outcomes','contract_policies')"
                )
            }
            marker = db.execute(
                "SELECT value FROM control_meta WHERE key='control_features'"
            ).fetchone()
        if not tables and marker is None:
            return False
        if (
            tables != {"operation_outcomes", "contract_policies"}
            or not marker
            or marker[0] != "1"
        ):
            raise ControlError(
                "feature_schema",
                "Incomplete or unsupported control features",
                category="migration",
            )
        return True

    def ensure_features(self):
        """Add recovery metadata without rewriting schema-2 contracts or receipts."""
        if self.has_features():
            return
        if self.readonly:
            raise ControlError("readonly", "Feature installation requires a writer")
        from ..run_lock import ProjectRunLock, require_project_run_lock
        from contextlib import nullcontext

        try:
            require_project_run_lock(self.project)
            lock = nullcontext()
        except RuntimeError:
            lock = ProjectRunLock(self.project)
        with lock:
            if self.has_features():
                return
            with self.connect() as db:
                populated = db.execute("SELECT 1 FROM work_items LIMIT 1").fetchone()
                if populated:
                    directory = self.project / ".auto-agents/state/schema-upgrades"
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    backup = directory / (
                        "control-features-v1-" + uuid.uuid4().hex + ".sqlite3"
                    )
                    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                    with sqlite3.connect(backup) as copy:
                        db.backup(copy)
            with self.connect(True) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS contract_policies(contract TEXT PRIMARY KEY REFERENCES contracts(id),payload TEXT NOT NULL,identity TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS operation_outcomes(operation TEXT PRIMARY KEY REFERENCES operations(id),native_state TEXT NOT NULL,payload TEXT NOT NULL,binding TEXT NOT NULL,identity TEXT NOT NULL,validation TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT OR REPLACE INTO control_meta VALUES('control_features','1')"
                )

    def _policy(self, contract, db):
        from .policy import WorkflowPolicy

        available = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='contract_policies'"
        ).fetchone()
        row = (
            db.execute(
                "SELECT payload,identity FROM contract_policies WHERE contract=?",
                (contract.identity,),
            ).fetchone()
            if available
            else None
        )
        if row:
            payload = json.loads(row["payload"])
            if digest(payload) != row["identity"]:
                raise ControlError("integrity", "Workflow policy checksum changed")
            return WorkflowPolicy(**payload)
        parent = None
        if contract.parent_contract:
            row = db.execute(
                "SELECT payload FROM contracts WHERE id=?", (contract.parent_contract,)
            ).fetchone()
            if not row:
                raise ControlError("contract", "Missing parent contract")
            parent = self._policy(Contract.read(json.loads(row[0])), db)
        return WorkflowPolicy.derive(contract, parent)

    def policy(self, contract, *, persist=False):
        if persist:
            self.ensure_features()
        with self.connect(persist) as db:
            value = self._policy(contract, db)
            if persist:
                db.execute(
                    "INSERT OR IGNORE INTO contract_policies VALUES(?,?,?)",
                    (
                        contract.identity,
                        canonical(value.to_dict()),
                        digest(value.to_dict()),
                    ),
                )
            return value

    def outcome(self, identity):
        if not self.has_features():
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operation_outcomes WHERE operation=?", (identity,)
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        for name in ("payload", "binding", "validation"):
            value[name] = json.loads(value[name])
        if (
            digest(
                {
                    "state": value["native_state"],
                    "payload": value["payload"],
                    "binding": value["binding"],
                }
            )
            != value["identity"]
        ):
            raise ControlError(
                "integrity",
                "Native outcome checksum changed",
                category="reconciliation",
            )
        return value

    def record_native(self, identity, payload, binding, *, state="confirmed"):
        if state not in {"confirmed", "failed", "unknown"}:
            raise ControlError("receipt", "Invalid native outcome state")
        self.ensure_features()
        seal = digest(NativeOutcome(state, payload, binding).to_dict())
        with self.connect(True) as db:
            operation = db.execute(
                "SELECT * FROM operations WHERE id=?", (identity,)
            ).fetchone()
            if not operation or operation["consumed"]:
                raise ControlError("receipt", "Native outcome has no active operation")
            owner = db.execute(
                "SELECT contract FROM work_items WHERE id=?", (operation["work"],)
            ).fetchone()
            if (
                binding.get("contract") != owner[0]
                or binding.get("input_hash") != operation["input_hash"]
            ):
                raise ControlError(
                    "receipt", "Native outcome belongs to different inputs"
                )
            old = db.execute(
                "SELECT identity FROM operation_outcomes WHERE operation=?", (identity,)
            ).fetchone()
            if old and old[0] != seal:
                raise ControlError("receipt", "Native outcomes are immutable")
            db.execute(
                "INSERT OR IGNORE INTO operation_outcomes VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    state,
                    canonical(payload),
                    canonical(binding),
                    seal,
                    canonical({"state": "pending"}),
                ),
            )
            self._event(
                db,
                operation["work"],
                "native_outcome_recorded",
                {"operation": identity, "native_state": state},
            )

    def validate_outcome(self, identity, *, error=None, result=None, observation=None):
        """Publish validation of known inputs without dispatch or budget changes."""
        from .access import POLICY_VERSION

        with self.connect(True) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=?", (identity,)
            ).fetchone()
            native = db.execute(
                "SELECT * FROM operation_outcomes WHERE operation=?", (identity,)
            ).fetchone()
            if (
                not row
                or not native
                or row["consumed"]
                or (
                    native["native_state"] != "confirmed"
                    and not (native["native_state"] == "failed" and error is not None)
                )
            ):
                raise ControlError(
                    "receipt", "Validation needs an owned confirmed native outcome"
                )
            binding, payload = (
                json.loads(native["binding"]),
                json.loads(native["payload"]),
            )
            owner = db.execute(
                "SELECT contract FROM work_items WHERE id=?", (row["work"],)
            ).fetchone()
            if (
                binding["contract"] != owner[0]
                or binding["input_hash"] != row["input_hash"]
            ):
                raise ControlError("receipt", "Validation changed its original inputs")
            if (
                result is not None
                and binding.get("kind") != "verification"
                and any(result.get(k) != v for k, v in payload.items())
            ):
                raise ControlError(
                    "receipt", "Validation cannot replace the native response"
                )
            decision = ValidationResult(
                "rejected" if error else "accepted", POLICY_VERSION, error
            ).to_dict()
            previous = json.loads(native["validation"])
            if observation is None:
                observation = previous.get("observation")
            if observation is not None:
                decision["observation"] = observation
            db.execute(
                "UPDATE operation_outcomes SET validation=? WHERE operation=?",
                (canonical(decision), identity),
            )
            self._event(
                db,
                row["work"],
                "outcome_validation",
                {
                    "operation": identity,
                    "previous_state": row["state"],
                    "previous_failure": (
                        json.loads(row["result"])
                        if row["state"] in {"FAILED", "UNKNOWN"} and row["result"]
                        else None
                    ),
                    **decision,
                },
            )
            if result is not None and not error:
                db.execute(
                    "UPDATE operations SET state='CONFIRMED',result=? WHERE id=?",
                    (canonical(result), identity),
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
        self.ensure_features()
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
        from .policy import validate_work

        validate_work(contract.mode, phase, "READY", {})
        db.execute(
            "INSERT OR IGNORE INTO contracts VALUES(?,?)",
            (contract.identity, canonical(contract.to_dict())),
        )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='contract_policies'"
        ).fetchone():
            policy = self._policy(contract, db).to_dict()
            db.execute(
                "INSERT OR IGNORE INTO contract_policies VALUES(?,?,?)",
                (contract.identity, canonical(policy), digest(policy)),
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
        self.policy(self.contract(parent["contract"])).route(mode)
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

    def upgrade_prototype_gate(self, work):
        """Adopt unapproved schema-2 candidates without changing their receipts."""
        state = dict(work["context"])
        if (
            work["status"] != "WAITING"
            or state.get("approval") != "prototype"
            or state.get("prototype_certificate")
        ):
            raise ControlError(
                "prototype_migration",
                "Only an unvalidated pending prototype can be adopted",
            )
        if any(
            o["state"] in {"UNKNOWN", "DISPATCHED"} for o in self.operations(work["id"])
        ):
            raise ControlError(
                "outcome_unknown",
                "Reconcile prototype operations before adopting scope checks",
                category="reconciliation",
            )
        for key in ("waiting_for", "approval", "approval_phase"):
            state.pop(key, None)
        state["prototype_legacy_candidate"] = True
        state["frontend"] = True
        return self.transition(work, "READY", phase="prototype_prepare", context=state)

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

    def redirect_acceptance(self, parent, child, retained):
        """Retire a mistaken development route without discarding its evidence."""
        if child["parent"] != parent["id"] or child["workflow"] != parent["workflow"]:
            raise ControlError("child_authority", "Child belongs to another parent")
        state = dict(parent["context"])
        state.pop("child", None)
        state.pop("waiting_for", None)
        state["retained_acceptance_work"] = [
            *state.get("retained_acceptance_work", []),
            retained,
        ]
        with self.connect(True) as db:
            if db.execute(
                "SELECT o.id FROM operations o JOIN work_items w ON w.id=o.work "
                "WHERE w.workflow=? AND o.state IN ('UNKNOWN','DISPATCHED')",
                (parent["workflow"],),
            ).fetchone():
                raise ControlError(
                    "outcome_unknown",
                    "Reconcile external operations before rerouting",
                    category="reconciliation",
                )
            self._transition(
                db,
                child,
                "CANCELLED",
                context={**child["context"], "superseded_by_acceptance": parent["id"]},
            )
            self._transition(
                db, parent, "READY", phase="acceptance", context=state, failure={}
            )
            self._event(db, parent["id"], "acceptance_route_recovered", retained)
        return self.work(parent["id"])

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
        from .policy import validate_work

        admission = (
            current["status"] == "READY"
            and status == "RUNNING"
            and current["calls"] == 0
        )
        previous_context = json.loads(current["context"])
        # A recorded human gate owns its return phase. Rejecting it may return
        # to that phase, without allowing arbitrary backward stage transitions.
        rejection = (
            current["status"] in {"WAITING", "BLOCKED"}
            and status == "READY"
            and previous_context.get("waiting_for") == "approval"
            and previous_context.get("approval_phase") == values["phase"]
            and bool(previous_context.get("approval"))
            and not any(
                key in values["context"]
                for key in ("waiting_for", "approval", "approval_phase")
            )
            and previous_context["approval"]
            not in values["context"].get("approvals", {})
            and isinstance(values["context"].get("feedback"), str)
            and bool(values["context"]["feedback"].strip())
        )
        prototype_migration = (
            current["status"] == "WAITING"
            and status == "READY"
            and previous_context.get("approval") == "prototype"
            and not previous_context.get("prototype_certificate")
            and values["phase"] == "prototype_prepare"
            and values["context"].get("prototype_legacy_candidate") is True
            and not any(
                k in values["context"]
                for k in ("waiting_for", "approval", "approval_phase")
            )
        )
        validate_work(
            current["mode"],
            values["phase"],
            status,
            values["context"],
            previous=(
                None
                if admission or rejection or prototype_migration
                else current["phase"]
            ),
        )
        if status == "WAITING" and values["context"].get("waiting_for") in {
            "child",
            "children",
        }:
            children = (
                values["context"].get("children", [])
                if values["context"]["waiting_for"] == "children"
                else [values["context"]["child"]]
            )
            for child in children:
                owned = db.execute(
                    "SELECT parent,workflow FROM work_items WHERE id=?", (child,)
                ).fetchone()
                if (
                    not owned
                    or owned["parent"] != work["id"]
                    or owned["workflow"] != current["workflow"]
                ):
                    raise ControlError(
                        "child_authority", "Waiting child belongs to another parent"
                    )
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
            {
                "from": current["status"],
                "to": status,
                "phase": values["phase"],
                "operator_decision": (
                    (
                        current["status"] == "WAITING"
                        and previous_context.get("waiting_for") in {"user", "approval"}
                        and status == "READY"
                    )
                    or previous_context.get("operator_answers", [])
                    != values["context"].get("operator_answers", [])
                ),
            },
        )

    def bind_contract(
        self, work, contract, *, next_phase=None, context=None, operation=None
    ):
        if next_phase:
            from .policy import validate_work

            validate_work(
                work["mode"],
                next_phase,
                "READY",
                context or work["context"],
                previous=work["phase"],
            )
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
