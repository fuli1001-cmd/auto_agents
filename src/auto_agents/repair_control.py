"""Versioned, stdlib-only repair control plane.

This module is also copied to an immutable bootstrap before starting the daemon.
It must not import the business CLI, providers, or the repair implementation.
"""
from __future__ import annotations

import argparse
import array
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
from uuid import uuid4

VERSION = 1
TERMINAL = {"completed", "blocked", "cancelled"}
PUBLISH_DELAYS = (60, 300, 900, 3600)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def contract_identity(payload):
    raw = payload["contract"]
    keys = ("owner", "category", "failure_scope", "expected_postconditions", "verification_commands", "proposed_fix_scope")
    semantic = {key: raw[key] for key in keys if key in raw} or raw
    encoded = json.dumps(semantic, sort_keys=True).replace(payload["project"], "<project>")
    return json.loads(encoded)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def start_ticks(pid):
    try:
        return int(Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return 0


def alive(pid, ticks):
    if not ticks or start_ticks(pid) != ticks:
        return False
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and result.returncode:
        # Do not copy a credential-bearing Git diagnostic into the task record.
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip() if check else result


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"repair state directory must be private and owned by this user: {path}")
    return path


def operator_root():
    return Path(os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_ROOT") or
                str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auto-agents/repair-control")).expanduser().resolve()


def publication_policy(config):
    path = Path(config["root"]) / "operator.json" if config.get("root") else None
    current = json.loads(path.read_text()) if path and path.exists() else config
    if any(current.get(key) != config.get(key) for key in ("remote", "ref", "identity")):
        raise PermissionError("operator repository identity changed; use a separate control namespace")
    if not current.get("publish", False):
        raise PermissionError("automatic publication is not authorized by the operator")
    return current


def configure(source_root):
    """Pin the installation's trusted upstream, never a model-supplied target."""
    source = Path(source_root).resolve()
    top = private_directory(operator_root())
    installation = top / (digest(str(source))[:24] + ".json")
    if installation.exists():
        recorded = json.loads(installation.read_text())
        return json.loads((Path(recorded["root"]) / "operator.json").read_text())
    branch = git(source, "branch", "--show-current")
    remote = git(source, "config", "--get", f"branch.{branch}.remote", check=False).stdout.strip()
    ref = git(source, "config", "--get", f"branch.{branch}.merge", check=False).stdout.strip()
    if not remote or not ref.startswith("refs/heads/"):
        raise RuntimeError("repair control requires an explicitly tracked engine upstream")
    url = git(source, "remote", "get-url", remote)
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.password or (parsed.scheme in {"http", "https"} and parsed.username):
        raise RuntimeError("use a Git credential helper, not credentials embedded in the remote URL")
    identity = digest([os.getuid(), url, ref])[:24]
    state = private_directory(top / identity)
    config_path = state / "operator.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
    else:
        config = {"version": VERSION, "identity": identity, "root": str(state),
                  "source_root": str(source), "remote": url, "ref": ref,
                  "python": str(Path(sys.executable).resolve()), "publish": True}
        atomic_json(config_path, config)
    atomic_json(installation, config)
    return config


class Store:
    def __init__(self, root):
        self.root = private_directory(root)
        self.path = self.root / "control.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, dedup TEXT NOT NULL, state TEXT NOT NULL,
                  payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                  generation INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS subscribers (
                  id TEXT PRIMARY KEY, job TEXT, project TEXT NOT NULL, token TEXT NOT NULL,
                  state TEXT NOT NULL, payload TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  sequence INTEGER PRIMARY KEY, job TEXT NOT NULL, kind TEXT NOT NULL,
                  payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                  job TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  due REAL NOT NULL, detail TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS verification_contexts (
                  id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS verifications (
                  id TEXT PRIMARY KEY, context TEXT NOT NULL, state TEXT NOT NULL,
                  payload TEXT NOT NULL, created REAL NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, job, kind, payload=None):
        with self.connect() as db:
            db.execute("INSERT INTO events(job,kind,payload,created) VALUES(?,?,?,?)",
                       (job, kind, json.dumps(payload or {}), time.time()))

    def register(self, payload):
        identity = digest([payload["project"], payload["token"]])[:24]
        with self.connect() as db:
            existing = db.execute("SELECT state,payload FROM subscribers WHERE id=?", (identity,)).fetchone()
            if existing and existing["state"] == "cancelled":
                raise RuntimeError("this workflow generation was cancelled")
            if existing:
                payload = {**json.loads(existing["payload"]), **payload}
            db.execute("INSERT INTO subscribers VALUES(?,NULL,?,?,'registered',?,?) "
                       "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
                       (identity, payload["project"], payload["token"], json.dumps(payload), time.time()))
        return identity

    def submit(self, subscriber, payload):
        key = digest([payload.get("symptom_key") or payload["fingerprint"], contract_identity(payload), payload["base"], payload["environment"]])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            subscription = db.execute("SELECT * FROM subscribers WHERE id=?", (subscriber,)).fetchone()
            if not subscription or subscription["state"] == "cancelled":
                raise RuntimeError("missing or cancelled workflow registration")
            existing = db.execute("SELECT id,state,result FROM jobs WHERE dedup=? AND state!='cancelled' ORDER BY updated DESC LIMIT 1", (key,)).fetchone()
            identity = existing["id"] if existing else uuid4().hex[:24]
            if (existing and existing["state"] == "completed" and subscription["job"] == identity
                    and subscription["state"] == "resuming"):
                receipt = {**json.loads(existing["result"]), "ok": False,
                           "error": "the same failure recurred in the verified runtime without a new contract", "recurrence": True}
                db.execute("UPDATE jobs SET state='blocked',result=?,generation=generation+1 WHERE id=?", (json.dumps(receipt), identity))
                db.execute("UPDATE outbox SET state='invalidated' WHERE job=? AND state!='published'", (identity,))
                db.execute("UPDATE subscribers SET state='blocked' WHERE id=?", (subscriber,))
                return identity
            if not existing:
                db.execute("INSERT INTO jobs(id,dedup,state,payload,updated) VALUES(?,?,'queued',?,?)",
                           (identity, key, json.dumps(payload), time.time()))
            db.execute("UPDATE subscribers SET job=?,state='waiting',updated=? WHERE id=?",
                       (identity, time.time(), subscriber))
            registered = json.loads(subscription["payload"])
            registered["repair"] = payload
            db.execute("UPDATE subscribers SET payload=? WHERE id=?", (json.dumps(registered), subscriber))
        self.event(identity, "subscribed", {"subscriber": subscriber})
        return identity

    def job(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("unknown repair job")
        result = dict(row)
        for key in ("payload", "result"):
            result[key] = json.loads(result[key])
        return result

    def transition(self, identity, state, result=None, *, generation=None):
        with self.connect() as db:
            row = db.execute("SELECT state,generation FROM jobs WHERE id=?", (identity,)).fetchone()
            if not row or row["state"] == "cancelled" or (generation is not None and row["generation"] != generation):
                return False
            db.execute("UPDATE jobs SET state=?,result=COALESCE(?,result),updated=? WHERE id=?",
                       (state, json.dumps(result) if result is not None else None, time.time(), identity))
        self.event(identity, state)
        return True

    def subscriptions(self, job=None):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM subscribers" + (" WHERE job=?" if job else ""), (job,) if job else ()).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def record_subscriber_failure(self, db, subscriber, job, phase, error):
        """Keep the stop reason with its workflow, in the state-change transaction."""
        from .repair_environment_log import sanitize
        row = db.execute("SELECT payload FROM subscribers WHERE id=? AND job=? AND state='blocked'",
                         (subscriber, job["id"])).fetchone()
        if row is None:
            return
        payload = json.loads(row["payload"])
        payload["repair_failure"] = {"job": job["id"], "generation": job["generation"],
                                     "phase": phase, "error": sanitize(error)}
        db.execute("UPDATE subscribers SET payload=? WHERE id=?", (json.dumps(payload), subscriber))

    def cancel(self, *, project=None, job=None):
        if not project and not job:
            raise ValueError("cancel needs a project or job")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT DISTINCT job FROM subscribers WHERE " + ("project=?" if project else "job=?"), (project or job,)).fetchall()
            db.execute("UPDATE subscribers SET state='cancelled',updated=? WHERE " + ("project=?" if project else "job=?"), (time.time(), project or job))
            for row in rows:
                remaining = db.execute("SELECT 1 FROM subscribers WHERE job=? AND state NOT IN ('cancelled','finished')", (row["job"],)).fetchone()
                if not remaining:
                    db.execute("UPDATE jobs SET state='cancelled',generation=generation+1,updated=? WHERE id=?", (time.time(), row["job"]))
                    db.execute("UPDATE outbox SET state='cancelled' WHERE job=? AND state!='published'", (row["job"],))
        return {"ok": True, "state": "cancelled"}

    def due_publish(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox WHERE state='pending' AND due<=?", (time.time(),))]

    def enqueue_publish(self, job):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO outbox(job,state,due) VALUES(?,'pending',?)", (job, time.time()))

    def publish_later(self, job, *, permission=False, detail=""):
        with self.connect() as db:
            current = db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()
            if not current or current["state"] == "cancelled":
                return
            previous = db.execute("SELECT attempts FROM outbox WHERE job=?", (job,)).fetchone()
            attempt = previous["attempts"] if previous else 0
            delay = PUBLISH_DELAYS[min(attempt, len(PUBLISH_DELAYS) - 1)]
            db.execute("INSERT OR REPLACE INTO outbox VALUES(?,?,?,?,?)",
                       (job, "authorization_required" if permission else "pending", attempt + 1, time.time() + delay, detail))


class Repository:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["root"])
        self.cache = self.root / "engine.git"

    @contextlib.contextmanager
    def locked(self):
        with (self.root / "repository.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def fetch(self):
        with self.locked():
            self.initialize()
            for attempt in range(3):
                try:
                    git(self.cache, "fetch", "--no-tags", self.config["remote"],
                        self.config["ref"] + ":refs/remotes/trusted")
                    return git(self.cache, "rev-parse", "refs/remotes/trusted"), True
                except (RuntimeError, subprocess.TimeoutExpired):
                    if attempt < 2:
                        time.sleep((1, 3)[attempt])
            cached = git(self.cache, "rev-parse", "--verify", "refs/remotes/trusted", check=False)
            if cached.returncode:
                raise RuntimeError("remote unavailable and no trusted engine revision cached")
            return cached.stdout.strip(), False

    def worktree(self, revision, name):
        if not name or Path(name).name != name:
            raise ValueError("invalid runtime name")
        path = self.root / "runtimes" / name
        with self.locked():
            if path.exists():
                if git(path, "rev-parse", "HEAD") != revision:
                    raise RuntimeError("runtime revision mismatch")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                git(self.cache, "worktree", "add", "--detach", str(path), revision)
        # Optional on old committed runtimes; this module remains stdlib-only.
        try:
            from auto_agents.artifact_runtime import track
        except ImportError:
            pass
        else:
            track(path, "worktree", scope="repair:" + str(self.root),
                  metadata={"repair_root": str(self.root), "repository": str(self.cache)})
        return path

    def import_commit(self, source, revision):
        with self.locked():
            self.initialize()
            git(self.cache, "fetch", str(source), revision)
        return revision

    def initialize(self):
        if not self.cache.exists():
            subprocess.run(["git", "init", "--bare", str(self.cache)], check=True, capture_output=True)
            git(self.cache, "config", "user.name", "auto-agents")
            git(self.cache, "config", "user.email", "self-repair@localhost")

    def push(self, revision):
        publication_policy(self.config)
        with self.locked():
            result = git(self.cache, "push", self.config["remote"], revision + ":" + self.config["ref"], check=False)
        if result.returncode:
            text = result.stderr.lower()
            permission = (any(word in text for word in ("permission denied", "authentication failed", "protected branch", "not allowed"))
                          or bool(re.search(r"\b(?:http(?:/[0-9.]+)?(?:\s+error)?|status(?:\s+code)?|returned\s+error)\s*[:=]?\s+403\b|\b403\s+forbidden\b", text)))
            raise PermissionError("remote publication not authorized") if permission else RuntimeError("remote publication failed; fetch and revalidate before retry")


def socket_path(config):
    directory = private_directory(config.get("socket_dir") or Path("/tmp") / f"auto-agents-control-{os.getuid()}")
    namespace = digest([config["identity"], str(Path(config["root"]).resolve())])[:24]
    return directory / (namespace + ".sock")


def rpc(config, request, fds=()):
    message = {"version": VERSION, **request}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(15)
        channel.connect(str(socket_path(config)))
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        encoded = json.dumps(message).encode() + b"\n"
        sent = channel.sendmsg([encoded], ancillary)
        if sent < len(encoded):
            channel.sendall(encoded[sent:])
        data = b""
        while b"\n" not in data:
            chunk = channel.recv(65536)
            if not chunk:
                raise RuntimeError("repair control disconnected")
            data += chunk
            if len(data) > 4 * 1024 * 1024:
                raise RuntimeError("repair control response too large")
    result = json.loads(data.split(b"\n", 1)[0])
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "repair control request failed"))
    return result


def ensure_supervisor(config):
    root = private_directory(config["root"])
    with (root / "supervisor-start.lock").open("a+") as ownership:
        fcntl.flock(ownership, fcntl.LOCK_EX)
        return _ensure_supervisor(config)


def retire_idle_legacy_supervisor(config, response):
    """Freeze submissions before replacing an idle older controller revision.

    A SQLite write transaction fences even old clients that do not know about
    the startup lock. Never stop a controller with live owners, workers or work.
    """
    store = Store(config["root"])
    pid, ticks = response.get("pid", 0), response.get("ticks", 0)
    if not alive(pid, ticks) or pid == os.getpid():
        return False
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed','blocked','cancelled') LIMIT 1").fetchone():
            return False
        if db.execute("SELECT 1 FROM outbox WHERE state IN ('pending','integration_pending','publishing') LIMIT 1").fetchone():
            return False
        if db.execute("SELECT 1 FROM verifications WHERE state IN ('queued','running') LIMIT 1").fetchone():
            return False
        for row in db.execute("SELECT payload FROM subscribers WHERE state NOT IN ('finished','blocked','cancelled')"):
            owner = json.loads(row[0])
            if alive(owner.get("pid", 0), owner.get("ticks", 0)):
                return False
        for path in store.root.glob("jobs/*/*-lease.json"):
            lease = json.loads(path.read_text())
            if alive(lease.get("pid", 0), lease.get("ticks", 0)):
                return False
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while alive(pid, ticks) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not alive(pid, ticks)


def _ensure_supervisor(config):
    revision = git(config["source_root"], "rev-parse", "HEAD")
    try:
        response = rpc(config, {"op": "ping"})
    except (OSError, RuntimeError):
        response = None
    if response is not None:
        supports = "managed-verification-v1" in response.get("capabilities", [])
        committed_support = git(config["source_root"], "cat-file", "-e", "HEAD:src/auto_agents/verification_worker.py", check=False).returncode == 0
        running_revision = response.get("implementation_revision", "")
        if not running_revision and config.get("implementation_root"):
            pinned_revision = git(config["implementation_root"], "rev-parse", "HEAD", check=False)
            if not pinned_revision.returncode:
                running_revision = pinned_revision.stdout.strip()
        if (supports and running_revision == revision) or not committed_support:
            return
        if not retire_idle_legacy_supervisor(config, response):
            raise RuntimeError("repair supervisor upgrade deferred: the older controller still owns active work; retry after it becomes idle")
    root = Path(config["root"])
    pinned = config.get("implementation_root")
    if pinned:
        old = git(pinned, "rev-parse", "HEAD", check=False)
        if old.returncode or old.stdout.strip() != revision:
            config.pop("implementation_root", None)
    if not config.get("implementation_root"):
        repository = Repository(config)
        repository.import_commit(config["source_root"], revision)
        implementation = repository.worktree(revision, "controller-" + revision[:20])
        if not (implementation / "src/auto_agents/repair_worker.py").is_file():
            raise RuntimeError("repair controller must be installed from a committed implementation")
        config["implementation_root"] = str(implementation)
    config["implementation_revision"] = revision
    atomic_json(root / "operator.json", config)
    source = (Path(config["implementation_root"]) / "src/auto_agents/repair_control.py").read_bytes()
    bootstrap = root / ("bootstrap-" + hashlib.sha256(source).hexdigest()[:20] + ".py")
    if not bootstrap.exists():
        bootstrap.write_bytes(source)
        bootstrap.chmod(0o500)
    # The daemon keeps only engine-launch/Git authentication plumbing. Per-job
    # provider credentials arrive in memory, never in the durable job payload.
    names = {"PATH", "HOME", "LANG", "LC_ALL", "SSH_AUTH_SOCK", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "AUTO_AGENTS_REPAIR_CONTROL_ROOT"}
    names.update({"AUTO_AGENTS_STORAGE_ROOT", "AUTO_AGENTS_STORAGE_DISABLED", "AUTO_AGENTS_STORAGE_MAINTENANCE"})
    environment = {key: value for key, value in os.environ.items() if key in names}
    with (root / "supervisor.log").open("ab") as output:
        subprocess.Popen([config["python"], str(bootstrap), "--serve", str(root / "operator.json")],
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                         close_fds=True, start_new_session=True, env=environment)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            rpc(config, {"op": "ping"})
            return
        except (OSError, RuntimeError):
            time.sleep(0.1)
    raise RuntimeError("repair supervisor did not become ready")


class RecoveredProcess:
    """Observe a pre-existing child without ever trusting a recycled PID."""
    def __init__(self, receipt):
        self.pid, self.ticks = receipt["pid"], receipt["ticks"]
        self.returncode = None

    def poll(self):
        if alive(self.pid, self.ticks):
            return None
        self.returncode = 3  # completion needs a durable receipt, not a guess
        return self.returncode


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.store = Store(config["root"])
        self.registrations = {}
        self.workers = {}
        self.resumes = {}
        self.relays = []
        self.halt = False
        self.stopping = {}
        self.publisher = None
        self.verification_processes = {}
        for lease_path in self.store.root.glob("verifications/*/lease.json"):
            lease = json.loads(lease_path.read_text())
            if alive(lease["pid"], lease["ticks"]):
                self.verification_processes[lease_path.parent.name] = RecoveredProcess(lease)
        for path in sorted(self.store.root.glob("jobs/*/*-lease.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            lease = json.loads(path.read_text())
            if lease.get("kind") == "resume":
                subscription = next((row for row in self.store.subscriptions(lease["job"]) if row["id"] == lease["subscriber"]), None)
                if subscription and subscription["state"] == "resuming":
                    self.resumes[lease["subscriber"]] = RecoveredProcess(lease)
            elif lease.get("kind") == "worker":
                job = self.store.job(lease["job"])
                recoverable = alive(lease["pid"], lease["ticks"]) or job["state"] == "repairing"
                if (job["generation"] == lease["generation"] and job["state"] != "cancelled"
                        and recoverable and lease["job"] not in self.workers):
                    self.workers[lease["job"]] = (RecoveredProcess(lease), lease["generation"], lease["operation"])

    def register(self, request, fds):
        payload = request["payload"]
        project = str(Path(payload["project"]).resolve())
        expected = Path(self.config.get("lock_dir", "/tmp/auto-agents-run-locks")) / (hashlib.sha256(project.encode()).hexdigest() + ".lock")
        if not fds:
            raise RuntimeError("registration requires the held project lock")
        left, right = os.fstat(fds[0]), expected.stat()
        if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
            raise RuntimeError("project lock identity mismatch")
        # A newly opened descriptor for the same inode is not a transferred
        # lock capability. Only the held open-file description can reacquire it.
        fcntl.flock(fds[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = json.loads(os.pread(fds[0], 8192, 0))
        if (owner.get("run_token") != payload["token"] or not alive(payload["pid"], payload["ticks"])
                or request.get("_peer_pid", payload["pid"]) != payload["pid"]):
            raise RuntimeError("stale project registration")
        identity = self.store.register(payload)
        previous = self.registrations.pop(identity, None)
        if previous:
            for fd in previous["fds"]:
                os.close(fd)
        self.registrations[identity] = {"fds": list(fds), "env": request.get("environment", {}), "payload": payload}
        resumed = self.resumes.get(identity)
        if resumed and resumed.poll() is None:
            self.registrations[identity]["payload"] = {**payload, "pid": resumed.pid, "ticks": start_ticks(resumed.pid)}
        fds.clear()
        return {"ok": True, "subscriber": identity}

    def dispatch(self, request, fds):
        if request.get("version") != VERSION:
            raise RuntimeError("incompatible repair control protocol")
        op = request["op"]
        if op == "ping":
            return {"ok": True, "version": VERSION, "pid": os.getpid(), "ticks": start_ticks(os.getpid()),
                    "implementation_revision": self.config.get("implementation_revision", ""),
                    "capabilities": ["managed-verification-v1"]}
        if op.startswith("verify-"):
            return self.verification_dispatch(request)
        if op == "register":
            return self.register(request, fds)
        if op == "submit":
            identity = request["subscriber"]
            if identity not in self.registrations:
                raise RuntimeError("workflow must register with the current supervisor")
            if request.get("_peer_pid") != self.registrations[identity]["payload"]["pid"]:
                raise RuntimeError("repair request must originate from the registered workflow")
            payload = request["payload"]
            # Code/entrypoint come from the trusted installation, never a route.
            payload["engine_root"] = self.config["source_root"]
            job = self.store.submit(identity, payload)
            prior = self.resumes.pop(identity, None)
            if prior is not None:
                self.relays.append((identity, prior))
            return {"ok": True, "job": job}
        if op == "status":
            if request.get("subscriber"):
                row = next((item for item in self.store.subscriptions() if item["id"] == request["subscriber"]), None)
                if not row or not row["job"]:
                    raise RuntimeError("subscriber has no repair job")
                return {"ok": True, "job": self.store.job(row["job"]), "subscribers": [row], "registered": list(self.registrations)}
            if request.get("job"):
                return {"ok": True, "job": self.store.job(request["job"]), "subscribers": self.store.subscriptions(request["job"]), "registered": list(self.registrations)}
            with self.store.connect() as db:
                return {"ok": True, "jobs": [dict(row) for row in db.execute("SELECT id,state,updated FROM jobs ORDER BY updated DESC")], "publications": [dict(row) for row in db.execute("SELECT * FROM outbox")]}
        if op == "lookup-contract":
            with self.store.connect() as db:
                rows = db.execute("SELECT payload FROM jobs WHERE state!='cancelled' ORDER BY updated DESC").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                diagnosis = payload.get("diagnosis") or {}
                final = diagnosis.get("final") or {}
                if (payload.get("symptom_key") == request.get("symptom_key")
                        and payload.get("base") == request.get("base")
                        and diagnosis.get("repair_approved") and final.get("generic")
                        and final.get("verification_commands")):
                    return {"ok": True, "contract": {"project": payload["project"],
                            "decision": payload["decision"], "diagnosis": diagnosis}}
            return {"ok": True, "contract": None}
        if op == "cancel":
            return self.store.cancel(project=request.get("project"), job=request.get("job"))
        if op == "boundary":
            row = next((item for item in self.store.subscriptions() if item["id"] == request["subscriber"]), None)
            if not row or row["state"] != "resuming" or not row["job"]:
                raise RuntimeError("boundary has no live recovery owner")
            job = self.store.job(row["job"])
            registration = self.registrations.get(row["id"], {})
            owner = registration.get("payload", {})
            if request.get("_peer_pid") != owner.get("pid") or not alive(owner.get("pid", 0), owner.get("ticks", 0)):
                raise RuntimeError("boundary sender is not the registered business process")
            if Path(request["runtime"]).resolve() != Path(job["result"]["runtime"]).resolve():
                raise RuntimeError("boundary loaded the wrong engine")
            expected = row["payload"]["repair"]["boundary"]
            details = request.get("details", {})
            passed = expected["kind"] == request["kind"] == "gate" and details.get("command") == expected.get("command")
            if expected["kind"] == request["kind"] == "run_stage":
                passed = (details.get("run_id") == row["payload"]["repair"]["invocation"].get("run_id")
                          and details.get("completed_stage") == expected.get("stage")
                          and details.get("fingerprint") != expected.get("fingerprint"))
            if passed and job["state"] != "completed":
                self.store.event(job["id"], "live_boundary_passed", {"subscriber": row["id"], "commit": job["result"]["commit"]})
                if job["result"].get("status") == "repaired":
                    self.store.enqueue_publish(job["id"])
                self.store.transition(job["id"], "completed")
            return {"ok": True, "accepted": passed}
        if op == "consume-route":
            row = next((item for item in self.store.subscriptions() if item["id"] == request["subscriber"]), None)
            if not row or row["state"] != "resuming":
                return {"ok": True, "accepted": False}
            owner = self.registrations.get(row["id"], {}).get("payload", {})
            if request.get("_peer_pid") != owner.get("pid"):
                raise RuntimeError("route receipt sender is not the registered business process")
            expected = row["payload"]["repair"]["boundary"]
            passed = expected.get("kind") == "engine_route" and expected.get("route_digest") == request.get("route_digest")
            if passed:
                job = self.store.job(row["job"])
                self.store.event(job["id"], "engine_route_consumed", {"subscriber": row["id"]})
                if job["state"] != "completed":
                    if job["result"].get("status") == "repaired":
                        self.store.enqueue_publish(job["id"])
                    self.store.transition(job["id"], "completed")
            return {"ok": True, "accepted": passed}
        if op == "finish":
            identity = request["subscriber"]
            with self.store.connect() as db:
                db.execute("UPDATE subscribers SET state='finished',updated=? WHERE id=? AND state='registered'", (time.time(), identity))
            return {"ok": True}
        if op in {"resume", "retry-publish"}:
            job = self.store.job(request["job"])
            if op == "retry-publish":
                with self.store.connect() as db:
                    db.execute("UPDATE outbox SET state='pending',due=? WHERE job=?", (time.time(), job["id"]))
            elif job["state"] == "blocked":
                for subscriber in self.store.subscriptions(job["id"]):
                    if subscriber["state"] == "cancelled" or subscriber["id"] in self.registrations:
                        continue
                    project = subscriber["project"]
                    path = Path(self.config.get("lock_dir", "/tmp/auto-agents-run-locks")) / (hashlib.sha256(project.encode()).hexdigest() + ".lock")
                    fd = os.open(path, os.O_RDWR)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        owner = json.loads(os.pread(fd, 8192, 0))
                        if owner.get("run_token") != subscriber["token"]:
                            raise RuntimeError("project entered a newer workflow generation; old repair cannot resume it")
                    except BaseException:
                        os.close(fd)
                        raise
                    self.registrations[subscriber["id"]] = {"fds": [fd], "payload": subscriber["payload"], "env": request.get("environment", {})}
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET state=?,generation=generation+1 WHERE id=?",
                               ("ready" if job["result"].get("ok") else "queued", job["id"]))
                    db.execute("UPDATE subscribers SET state='waiting' WHERE job=? AND state='blocked'", (job["id"],))
            else:
                raise RuntimeError("only blocked repair jobs can be resumed")
            return {"ok": True}
        raise RuntimeError("unsupported control operation")

    def stop_process(self, process):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                since = self.stopping.setdefault(process.pid, time.monotonic())
                os.killpg(process.pid, signal.SIGKILL if time.monotonic() - since > 10 else signal.SIGTERM)

    def drain_worker_children(self, identity):
        path = self.store.root / "jobs" / identity / "processes.json"
        if not path.exists():
            return
        receipt = json.loads(path.read_text())
        if receipt.get("run_token") != identity or Path(receipt.get("project", "")).resolve() != path.parent.resolve():
            raise RuntimeError("worker process registry belongs to a different job")
        for item in receipt.get("processes", []):
            pid, ticks = int(item["pid"]), int(item["start_ticks"])
            if alive(pid, ticks) and int(item["pgid"]) == pid:
                self.stop_process(RecoveredProcess({"pid": pid, "ticks": ticks}))

    def tick(self):
        self.tick_verifications()
        for identity, process in list(self.relays):
            row = next((item for item in self.store.subscriptions() if item["id"] == identity), None)
            if row and row["state"] == "cancelled":
                self.stop_process(process)
            if process.poll() is not None:
                self.relays.remove((identity, process))
        for identity, (process, generation, operation) in list(self.workers.items()):
            job = self.store.job(identity)
            if job["state"] == "cancelled":
                self.stop_process(process)
                self.drain_worker_children(identity)
            if process.poll() is None:
                continue
            self.drain_worker_children(identity)
            del self.workers[identity]
            result_path = self.store.root / "jobs" / identity / (operation + f"-g{generation}-result.json")
            result = json.loads(result_path.read_text()) if result_path.exists() else {"ok": False, "error": "repair worker exited without a receipt"}
            if result.get("generation", generation) != generation:
                result = {"ok": False, "error": "stale worker receipt"}
            if job["state"] == "cancelled" or job["generation"] != generation:
                continue
            if operation.startswith("validate-"):
                subscriber_id = operation[len("validate-"):]
                with self.store.connect() as db:
                    if result.get("ok") and result.get("engine_full_proof"):
                        updated = {**job["result"], "engine_full_proof": result["engine_full_proof"]}
                        db.execute("UPDATE jobs SET result=? WHERE id=? AND generation=?",
                                   (json.dumps(updated), identity, generation))
                    db.execute("UPDATE subscribers SET state=?,updated=? WHERE id=? AND state='validating'",
                               ("verified" if result.get("ok") else "blocked", time.time(), subscriber_id))
                    if not result.get("ok"):
                        self.store.record_subscriber_failure(db, subscriber_id, job, "validation",
                            result.get("error") or result.get("proof") or "未返回具体原因，请查看详细日志")
                self.store.event(identity, "subscriber_validated", {"subscriber": subscriber_id, "ok": bool(result.get("ok"))})
            elif operation == "publish":
                if result.get("ok"):
                    with self.store.connect() as db:
                        db.execute("UPDATE outbox SET state='published',detail=? WHERE job=?", (result.get("commit", ""), identity))
                else:
                    self.store.publish_later(identity, permission=result.get("permission", False), detail=result.get("error", "publication failed"))
            else:
                self.store.transition(identity, "ready" if result.get("ok") else "blocked", result, generation=generation)
                if not result.get("ok"):
                    with self.store.connect() as db:
                        db.execute("UPDATE subscribers SET state='blocked',updated=? WHERE job=? AND state IN ('waiting','validating','verified')",
                                   (time.time(), identity))
        for row in self.store.subscriptions():
            identity = row["id"]
            process = self.resumes.get(identity)
            if row["state"] == "registered" and not alive(row["payload"]["pid"], row["payload"]["ticks"]):
                # A crash is not proof of an engine bug or permission to repeat
                # external effects. Explicitly queued repairs remain resumable.
                with self.store.connect() as db:
                    db.execute("UPDATE subscribers SET state='finished' WHERE id=?", (identity,))
                self.store.event(row["job"] or "", "unexpected_owner_exit", {"subscriber": identity})
            if row["state"] == "cancelled" and process:
                self.stop_process(process)
            if process and process.poll() is not None:
                completion = self.store.root / "jobs" / row["job"] / ("resume-" + identity + "-result.json")
                receipt = json.loads(completion.read_text()) if completion.exists() else {}
                exit_code = receipt.get("exit_code", 3) if completion.exists() else process.returncode
                with self.store.connect() as db:
                    db.execute("UPDATE subscribers SET state=?,updated=? WHERE id=? AND state!='cancelled'",
                               ("finished" if exit_code == 0 else "blocked", time.time(), identity))
                    if exit_code != 0:
                        self.store.record_subscriber_failure(db, identity, self.store.job(row["job"]), "resume",
                            receipt.get("error") or f"原任务进程退出（退出码 {exit_code}），请查看详细日志")
                self.store.event(row["job"], "workflow_exit", {"subscriber": identity, "exit_code": exit_code})
                if exit_code == 0 and row["state"] != "cancelled":
                    job = self.store.job(row["job"])
                    if job["state"] != "completed":
                        self.store.transition(job["id"], "completed")
                        if job["result"].get("status") == "repaired":
                            self.store.enqueue_publish(job["id"])
                del self.resumes[identity]
            if row["state"] == "verified" and identity not in self.resumes:
                self.launch_resume(row)
            if row["state"] in {"finished", "cancelled", "blocked"} and identity not in self.resumes and row["job"] not in self.workers:
                registration = self.registrations.pop(identity, None)
                if registration:
                    for fd in registration["fds"]:
                        os.close(fd)
        with self.store.connect() as db:
            pending = db.execute("SELECT id FROM jobs WHERE state='ready'").fetchall()
            for item in pending:
                active = db.execute("SELECT 1 FROM subscribers WHERE job=? AND state IN ('waiting','validating','verified','resuming')", (item["id"],)).fetchone()
                if not active:
                    db.execute("UPDATE jobs SET state='blocked' WHERE id=?", (item["id"],))
        # Network-only publication must not wait behind another model repair.
        # Integration/review still shares the single code-worker slot.
        if self.publisher is None or not self.publisher.is_alive():
            due = [item for item in self.store.due_publish() if item["job"] not in self.workers]
            if due:
                self.publisher = threading.Thread(target=self.fast_publish, args=(due[0]["job"],), daemon=True)
                self.publisher.start()
        if not self.workers:
            for row in self.store.subscriptions():
                if row["state"] == "waiting" and row["job"] and self.store.job(row["job"])["state"] in {"ready", "completed"}:
                    with self.store.connect() as db:
                        db.execute("UPDATE subscribers SET state='validating' WHERE id=?", (row["id"],))
                    self.launch_worker(row["job"], "validate-" + row["id"])
                    return
            with self.store.connect() as db:
                integration = db.execute("SELECT job FROM outbox WHERE state='integration_pending' AND due<=? ORDER BY due LIMIT 1", (time.time(),)).fetchone()
            if integration:
                self.launch_worker(integration["job"], "publish")
                return
            with self.store.connect() as db:
                row = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY updated LIMIT 1").fetchone()
            if row:
                self.launch_worker(row["id"], "repair")

    def fast_publish(self, identity):
        job = self.store.job(identity)
        if job["state"] != "completed" or not job["result"].get("ok"):
            return
        try:
            config = publication_policy(self.config)
            repository = Repository(config)
            revision, fresh = repository.fetch()
            if not fresh:
                raise RuntimeError("publication requires a successful upstream refresh")
            current = self.store.job(identity)
            if current["state"] == "cancelled" or current["generation"] != job["generation"]:
                return
            candidate = job["result"]["commit"]
            contained = git(repository.cache, "merge-base", "--is-ancestor", candidate, revision, check=False).returncode == 0
            if not contained and revision != job["result"]["base"]:
                with self.store.connect() as db:
                    db.execute("UPDATE outbox SET state='integration_pending',due=? WHERE job=? AND state='pending'", (time.time(), identity))
                return
            if not contained:
                repository.push(candidate)
            with self.store.connect() as db:
                db.execute("UPDATE outbox SET state='published',detail=? WHERE job=? AND state!='cancelled'", (revision if contained else candidate, identity))
            self.store.event(identity, "published", {"commit": revision if contained else candidate})
        except PermissionError as error:
            if self.store.job(identity)["generation"] == job["generation"]:
                self.store.publish_later(identity, permission=True, detail=str(error))
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            if self.store.job(identity)["generation"] == job["generation"]:
                self.store.publish_later(identity, detail=str(error))

    def launch_worker(self, identity, operation):
        job = self.store.job(identity)
        subscribers = self.store.subscriptions(identity)
        registration = next((self.registrations[item["id"]] for item in subscribers if item["id"] in self.registrations and item["state"] != "cancelled"), None)
        if operation != "publish" and registration is None:
            if job["state"] != "completed":
                self.store.transition(identity, "blocked", {**job["result"], "ok": bool(job["result"].get("ok")),
                    "control_error": "workflow registration must be restored before repair"})
            with self.store.connect() as db:
                db.execute("UPDATE subscribers SET state='blocked' WHERE job=? AND state IN ('waiting','validating','verified')", (identity,))
            return
        root = self.store.root / "jobs" / identity
        root.mkdir(parents=True, exist_ok=True)
        request = {"job": job, "config": self.config, "operation": operation}
        if operation.startswith("validate-"):
            request["subscriber"] = next(item for item in subscribers if item["id"] == operation[len("validate-"):])
        request_path = root / (operation + f"-g{job['generation']}-request.json")
        result_path = root / (operation + f"-g{job['generation']}-result.json")
        if result_path.exists():
            result_path.unlink()
        atomic_json(request_path, request)
        environment = dict(os.environ)
        if registration:
            environment.update(registration["env"])
        environment["AUTO_AGENTS_REPAIR_CONTROL_WORKER"] = "1"
        environment["AUTO_AGENTS_REPAIR_CONTROL_CONFIG"] = str(self.store.root / "operator.json")
        environment["AUTO_AGENTS_REPAIR_JOB"] = identity
        lock_fds = tuple(registration["fds"]) if registration else ()
        if registration:
            environment["AUTO_AGENTS_REPAIR_LOCK_FD"] = str(lock_fds[0])
            request["registration"] = registration["payload"]
            atomic_json(request_path, request)
        with (root / (operation + ".log")).open("ab") as output:
            process = subprocess.Popen([self.config["python"], str(Path(self.config.get("implementation_root", self.config["source_root"])) / "src/auto_agents/repair_worker.py"), str(request_path)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                start_new_session=True, close_fds=True, pass_fds=lock_fds, env=environment)
        self.workers[identity] = (process, job["generation"], operation)
        if operation == "repair":
            self.store.transition(identity, "repairing")
        self.store.event(identity, operation + "_worker", {"pid": process.pid, "ticks": start_ticks(process.pid)})
        atomic_json(root / (operation + f"-g{job['generation']}-lease.json"), {"kind": "worker", "job": identity,
            "pid": process.pid, "ticks": start_ticks(process.pid), "generation": job["generation"], "operation": operation})

    def launch_resume(self, row):
        registration = self.registrations.get(row["id"])
        if registration is None:
            with self.store.connect() as db:
                db.execute("UPDATE subscribers SET state='blocked' WHERE id=?", (row["id"],))
                self.store.record_subscriber_failure(db, row["id"], self.store.job(row["job"]), "resume",
                    "任务登记已失效，需要恢复登记后继续")
            return
        job = self.store.job(row["job"])
        root = self.store.root / "jobs" / job["id"]
        request = {"subscriber": row, "result": job["result"], "config": self.config}
        path = root / ("resume-" + row["id"] + ".json")
        atomic_json(path, request)
        fd = registration["fds"][0]
        environment = {**os.environ, **registration["env"],
                       "AUTO_AGENTS_REPAIR_CONTROL_CONFIG": str(self.store.root / "operator.json"),
                       "AUTO_AGENTS_REPAIR_SUBSCRIBER": row["id"],
                       "AUTO_AGENTS_RUN_LOCK_FD": str(fd),
                       "AUTO_AGENTS_RUN_LOCK_KEY": hashlib.sha256(row["project"].encode()).hexdigest(),
                       "AUTO_AGENTS_RUN_TOKEN": row["token"],
                       "AUTO_AGENTS_SELF_REPAIR_HEALTH_REBASE": "1"}
        environment.pop("AUTO_AGENTS_REPAIR_CONTROL_WORKER", None)
        with (root / ("resume-" + row["id"] + ".log")).open("ab") as output:
            process = subprocess.Popen([job["result"]["python"], str(Path(self.config.get("implementation_root", self.config["source_root"])) / "src/auto_agents/repair_launch.py"), str(path)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                env=environment, pass_fds=(fd,), start_new_session=True,
                cwd=row["payload"].get("cwd", row["project"]))
        self.resumes[row["id"]] = process
        with self.store.connect() as db:
            db.execute("UPDATE subscribers SET state='resuming',updated=? WHERE id=?", (time.time(), row["id"]))
        self.store.event(job["id"], "workflow_started", {"subscriber": row["id"], "pid": process.pid, "ticks": start_ticks(process.pid)})
        atomic_json(root / ("resume-" + row["id"] + "-lease.json"), {"kind": "resume", "job": job["id"],
            "subscriber": row["id"], "pid": process.pid, "ticks": start_ticks(process.pid)})

    def verification_context(self, identity):
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM verification_contexts WHERE id=?", (identity,)).fetchone()
        if not row:
            raise RuntimeError("unknown verification context")
        context = json.loads(row[0])
        if context.get("job"):
            job = self.store.job(context["job"])
            if job["generation"] != context["generation"] or job["state"] not in {"repairing", "ready", "validating"}:
                raise RuntimeError("verification context belongs to an obsolete repair generation")
        else:
            registration = self.registrations.get(context["subscriber"])
            if not registration or registration["payload"]["token"] != context["run_token"]:
                raise RuntimeError("verification workflow owner is no longer registered")
            row = next((r for r in self.store.subscriptions() if r["id"] == context["subscriber"]), None)
            if not row or row["state"] in {"finished", "cancelled", "blocked"}:
                raise RuntimeError("verification workflow is no longer active")
        if not alive(context["pid"], context["ticks"]):
            raise RuntimeError("verification owner exited")
        workspace = Path(context["workspace"])
        info = workspace.stat()
        if workspace.resolve() != workspace or [info.st_dev, info.st_ino] != context["inode"]:
            raise RuntimeError("verification workspace identity changed")
        return context

    def verification_dispatch(self, request):
        op = request["op"]
        if op == "verify-context":
            workspace = Path(request["workspace"]).resolve()
            peer = request.get("_peer_pid")
            job_id = request.get("job", "")
            if job_id:
                worker = self.workers.get(job_id)
                job = self.store.job(job_id)
                if not worker or worker[0].pid != peer or job["state"] != "repairing":
                    raise RuntimeError("only the active repair worker can bind engine verification")
                if not workspace.is_relative_to(self.store.root / "jobs" / job_id):
                    common = git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
                    if Path(common).resolve() != (self.store.root / "engine.git").resolve():
                        raise RuntimeError("engine verification workspace is outside the repair repository")
                source = job["payload"]["project"]
                generation = job["generation"]
                run_token = ""
            else:
                registration = self.registrations.get(request.get("subscriber", ""))
                if not registration or registration["payload"]["pid"] != peer:
                    raise RuntimeError("only the registered workflow can bind project verification")
                if request.get("engine"):
                    raise RuntimeError("project verification cannot grant engine write scope")
                source = registration["payload"]["project"]
                common = lambda path: git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
                if common(workspace) != common(source):
                    raise RuntimeError("verification workspace belongs to a different repository")
                generation, run_token = 0, registration["payload"]["token"]
            runtime = Path(request["runtime"]).resolve()
            if not (runtime == Path(self.config["source_root"]).resolve()
                    or runtime.is_relative_to(self.store.root / "runtimes")):
                raise RuntimeError("verification executor is not a trusted engine runtime")
            revision = git(runtime, "rev-parse", "HEAD")
            if runtime == Path(self.config["source_root"]).resolve():
                repository = Repository(self.config)
                repository.import_commit(runtime, revision)
                runtime = repository.worktree(revision, "verification-controller-" + revision[:20])
            if not (runtime / "src/auto_agents/verification_worker.py").is_file():
                raise RuntimeError("runtime has no managed verification executor")
            if git(runtime, "diff", "--quiet", "HEAD", "--", "src/auto_agents", check=False).returncode:
                raise RuntimeError("trusted verification runtime was modified")
            identity = uuid4().hex
            info = workspace.stat()
            context = {"workspace": str(workspace), "inode": [info.st_dev, info.st_ino],
                       "source": source, "runtime": str(runtime), "engine": bool(job_id),
                       "job": job_id, "generation": generation, "run_token": run_token,
                       "subscriber": request.get("subscriber", ""), "pid": peer, "ticks": start_ticks(peer),
                       "repository": self.config["source_root"] if job_id else source,
                       "python": request.get("python") or self.config["python"], "runtime_commit": revision,
                       "resource_environment": {key: str(value) for key, value in request.get("resource_environment", {}).items()
                           if key in {"AUTO_AGENTS_CLUSTER_HOME", "AUTO_AGENTS_WORKER_ROOT", "AUTO_AGENTS_WORKER_SLOTS", "AUTO_AGENTS_WORKER_CONFIG", "AUTO_AGENTS_VERIFICATION_ROOT"}}}
            with self.store.connect() as db:
                db.execute("INSERT INTO verification_contexts VALUES(?,?)", (identity, json.dumps(context)))
            return {"ok": True, "context": identity}
        context = self.verification_context(request["context"])
        if op == "verify-submit":
            if request.get("level") != "focused" or not isinstance(request.get("tests"), list) or not request["tests"]:
                raise RuntimeError("model verification requires explicit focused test targets")
            identity = uuid4().hex
            payload = {**context, "tests": request["tests"], "level": "focused", "fresh": bool(request.get("fresh"))}
            with self.store.connect() as db:
                db.execute("INSERT INTO verifications VALUES(?,?,'queued',?,?)",
                           (identity, request["context"], json.dumps(payload), time.time()))
            return {"ok": True, "verification": identity}
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM verifications WHERE id=? AND context=?",
                             (request.get("verification"), request["context"])).fetchone()
        if not row:
            raise RuntimeError("verification does not belong to this context")
        if op == "verify-status":
            path = self.store.root / "verifications" / row["id"] / "result.json"
            result = json.loads(path.read_text()) if path.exists() else {}
            return {"ok": True, "state": row["state"], "result": result}
        raise RuntimeError("unknown managed verification operation")

    def tick_verifications(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM verifications WHERE state IN ('queued','running') ORDER BY created").fetchall()
        for row in rows:
            root = self.store.root / "verifications" / row["id"]
            process = self.verification_processes.get(row["id"])
            try:
                context = self.verification_context(row["context"])
            except (OSError, RuntimeError, ValueError):
                if process and process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    # Repeated ticks escalate only this tracked process group.
                    since = self.stopping.setdefault("verify:" + row["id"], time.monotonic())
                    if time.monotonic() - since > 10:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                    continue
                state = "cancelled"
            else:
                if process and process.poll() is None:
                    continue
                if row["state"] == "queued":
                    if len(self.verification_processes) >= 4:
                        continue
                    root.mkdir(parents=True, exist_ok=True)
                    payload = json.loads(row["payload"])
                    atomic_json(root / "request.json", payload)
                    environment = {key: value for key, value in os.environ.items()
                                   if key in {"PATH", "HOME", "LANG", "CODEX_HOME", "XDG_STATE_HOME", "AUTO_AGENTS_WORKER_ROOT", "AUTO_AGENTS_VERIFICATION_ROOT", "AUTO_AGENTS_VERIFICATION_SANDBOX", "AUTO_AGENTS_STORAGE_ROOT", "AUTO_AGENTS_STORAGE_DISABLED", "AUTO_AGENTS_STORAGE_MAINTENANCE"}}
                    environment["AUTO_AGENTS_REPAIR_CONTROL_CONFIG"] = str(self.store.root / "operator.json")
                    if context["job"]:
                        environment["AUTO_AGENTS_REPAIR_JOB"] = context["job"]
                    environment["PYTHONPATH"] = str(Path(context["runtime"]) / "src")
                    environment.update(context.get("resource_environment", {}))
                    with (root / "worker.log").open("ab") as output:
                        process = subprocess.Popen([context["python"], str(Path(context["runtime"]) / "src/auto_agents/verification_worker.py"), str(root / "request.json")],
                            stdout=output, stderr=output, stdin=subprocess.DEVNULL, env=environment,
                            start_new_session=True)
                    self.verification_processes[row["id"]] = process
                    atomic_json(root / "lease.json", {"pid": process.pid, "ticks": start_ticks(process.pid)})
                    state = "running"
                else:
                    path = root / "result.json"
                    result = json.loads(path.read_text()) if path.exists() else {}
                    state = "completed" if result.get("ok") else "failed"
            with self.store.connect() as db:
                db.execute("UPDATE verifications SET state=? WHERE id=?", (state, row["id"]))
            if state != "running":
                self.verification_processes.pop(row["id"], None)

    def serve(self):
        with (self.store.root / "supervisor.lock").open("a+") as ownership:
            try:
                fcntl.flock(ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            path = socket_path(self.config)
            if path.exists():
                path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(16)
                listener.settimeout(0.2)
                while not self.halt:
                    try:
                        channel, _ = listener.accept()
                    except socket.timeout:
                        self.tick()
                        continue
                    fds = []
                    with channel:
                        channel.settimeout(5)
                        try:
                            raw, ancillary, flags, _ = channel.recvmsg(65536, socket.CMSG_SPACE(4 * 16))
                            if flags & socket.MSG_CTRUNC:
                                raise ValueError("truncated lock transfer")
                            for level, kind, value in ancillary:
                                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                                    received = array.array("i")
                                    received.frombytes(value[:len(value) - len(value) % received.itemsize])
                                    fds.extend(received)
                            while b"\n" not in raw:
                                if len(raw) > 4 * 1024 * 1024:
                                    raise ValueError("request too large")
                                chunk = channel.recv(65536)
                                if not chunk:
                                    raise ValueError("incomplete control request")
                                raw += chunk
                            request = json.loads(raw.split(b"\n", 1)[0])
                            request["_peer_pid"] = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
                            reply = self.dispatch(request, fds)
                        except Exception as error:
                            reply = {"ok": False, "error": str(error)}
                        finally:
                            for fd in fds:
                                os.close(fd)
                        with contextlib.suppress(OSError):
                            channel.sendall(json.dumps(reply).encode() + b"\n")
                    self.tick()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", required=True)
    arguments = parser.parse_args()
    configured = json.loads(Path(arguments.serve).read_text())
    implementation = Path(configured.get("implementation_root", "/__missing_controller_runtime__"))
    if configured.get("implementation_root") and (implementation / "src/auto_agents/artifact_store.py").is_file():
        sys.path.insert(0, str(implementation / "src"))
        from auto_agents.artifact_runtime import activate, schedule, track
        activate(scope="repair:" + configured["root"])
        track(implementation, "worktree", scope="repair:" + configured["root"],
              metadata={"repair_root": configured["root"], "repository": str(Path(configured["root"]) / "engine.git")})
        track(Path(__file__), "cache", scope="repair:" + configured["root"], metadata={"repair_root": configured["root"]})
        schedule()
    Supervisor(configured).serve()
