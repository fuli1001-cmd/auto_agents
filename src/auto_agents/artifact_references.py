"""Conservative adapters for durable workflow/repair/worker references."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from .artifact_store import process_identity


def _json(path):
    return json.loads(Path(path).read_text())


def project_protection(project, *, recovery=True):
    deadline = time.monotonic() + 0.5
    root = Path(project)
    key = hashlib.sha256(str(root).encode()).hexdigest()
    lock = Path(tempfile.gettempdir()) / "auto-agents-run-locks" / (key + ".lock")
    if lock.exists():
        with lock.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "active_project"
        control = lock.with_suffix(".processes.json")
        if control.exists():
            for process in _json(control).get("processes", []):
                pid = int(process.get("pid", 0))
                if pid > 0 and process_identity(pid)["ticks"] == str(process.get("start_ticks", "")):
                    return "active_child_process"
    if not recovery:
        return ""
    state = root / ".auto-agents/state"
    paths = [state / "run_state.json"]
    paths.extend((state / "sessions").glob("*/session_state.json"))
    paths.extend((state / "workflows").glob("*/workflow.json"))
    for path in paths:
        if time.monotonic() >= deadline:
            return "unknown_reference: project scan budget exhausted"
        if path.exists():
            if path.stat().st_size > 4 * 1024 * 1024:
                return "unknown_reference: large state requires indexed migration"
            value = _json(path)
            if value.get("status") not in {"completed", "cancelled", "superseded", "closed", "done"}:
                return "recoverable_workflow: " + str(path)
    return ""


def referenced_file(project, artifact):
    """Read typed evidence/path fields; opaque transcript text is not authority."""
    root = Path(project)
    target = Path(artifact)
    state = root / ".auto-agents/state"
    deadline = time.monotonic() + 0.3
    fields = {"path", "source_path", "log_path", "evidence_refs", "verification_refs", "artifacts",
              "diagnostic_attachments", "proof_ref", "result_ref", "last_child_result_ref", "baseline_git_ref"}

    def matches(value):
        if isinstance(value, str):
            candidate = value.rsplit(":", 1)[0] if value.rsplit(":", 1)[-1].isdigit() else value
            candidate = Path(candidate)
            if not candidate.is_absolute():
                candidate = root / candidate
            return candidate == target or candidate.is_relative_to(target)
        if isinstance(value, dict):
            return any(matches(v) for v in value.values())
        if isinstance(value, list):
            return any(matches(v) for v in value)
        return False

    def walk(value):
        if isinstance(value, dict):
            return any(matches(v) if k in fields else walk(v) for k, v in value.items())
        return isinstance(value, list) and any(walk(v) for v in value)

    paths = [state / "run_state.json"]
    paths.extend((state / "sessions").glob("*/session_state.json"))
    paths.extend((state / "handoffs").glob("*.json"))
    for path in paths:
        if time.monotonic() >= deadline:
            return "unknown_reference: evidence scan budget exhausted"
        if path.exists():
            if path.stat().st_size > 4 * 1024 * 1024:
                return "unknown_reference: large evidence index"
            if walk(_json(path)):
                return "referenced_project_evidence"
    return ""


def protection(row):
    metadata = row["metadata"]
    try:
        if metadata.get("process_control"):
            for process in _json(metadata["process_control"]).get("processes", []):
                pgid = int(process.get("pgid", 0) or 0)
                if pgid > 0:
                    try:
                        os.killpg(pgid, 0)
                        return "active_child_process"
                    except ProcessLookupError:
                        pass
        if metadata.get("proof_database"):
            with contextlib.closing(sqlite3.connect(Path(metadata["proof_database"]).as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
                if db.execute("SELECT 1 FROM gate_proof_certificates,json_each(result_payload,'$.artifacts') WHERE json_each.value=? LIMIT 1", (metadata["blob"],)).fetchone():
                    return "referenced_proof_blob"
        project = metadata.get("project")
        if project:
            reason = project_protection(project, recovery=row["kind"] in {"recovery", "evidence", "log", "worktree", "environment"})
            if reason:
                return reason
            if row["kind"] in {"log", "evidence", "recovery"}:
                reason = referenced_file(project, row["path"])
                if reason:
                    return reason
            # Lock files pin exact versions even after their producing run exits.
            if metadata.get("tool_id"):
                lock = Path(project) / ".auto-agents/state/runtime_requirements.lock.json"
                if lock.exists() and row["path"] in json.dumps(_json(lock), ensure_ascii=False):
                    return "locked_tool_version"
            if row["kind"] == "cache" and "awesome-design-md" in Path(row["path"]).parts:
                lock = Path(project) / ".auto-agents/state/frontend_design.lock.json"
                if lock.exists() and Path(row["path"]).name in json.dumps(_json(lock)):
                    return "locked_design_version"
        # Evidence is only explicitly unreferenced after its producer exports the
        # referenced proof. Unknown evidence contracts never become disposable.
        if row["kind"] in {"recovery", "evidence"} and not metadata.get("disposable"):
            return "evidence_contract_requires_retention"
        if metadata.get("repair_root"):
            root = Path(metadata["repair_root"])
            config = _json(root / "operator.json")
            if config.get("implementation_root") == row["path"]:
                return "installed_controller_runtime"
            if row["kind"] not in {"incomplete", "cache"}:
                with contextlib.closing(sqlite3.connect((root / "control.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
                    if db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed','cancelled') LIMIT 1").fetchone():
                        return "repair_or_recovery_pending"
                    if db.execute("SELECT 1 FROM subscribers WHERE state NOT IN ('finished','cancelled') LIMIT 1").fetchone():
                        return "subscriber_pending"
                    if db.execute("SELECT 1 FROM outbox WHERE state NOT IN ('published','invalidated') LIMIT 1").fetchone():
                        return "publication_pending"
        if metadata.get("worker_root"):
            root = Path(metadata["worker_root"])
            for path in (root / "jobs").glob("*.json"):
                job = _json(path)
                if job.get("result", {}).get("cleanup_incomplete"):
                    return "worker_cleanup_incomplete"
                pgid = int(job.get("pgid", 0) or 0)
                if pgid:
                    try:
                        os.killpg(pgid, 0)
                        return "active_worker_process"
                    except ProcessLookupError:
                        pass
                if job.get("state") not in {"terminal", "cancelled"} and row["kind"] not in {"scratch", "incomplete", "cache"}:
                    return "worker_job_pending"
                if metadata.get("job") == job.get("job_id") and job.get("artifact_archive") and not job.get("artifact_ack"):
                    return "artifact_not_acknowledged"
        if row["kind"] == "worktree":
            repo = metadata["repository"]
            status = subprocess.run(["git", "-C", row["path"], "status", "--porcelain", "--untracked-files=all"],
                                    capture_output=True, text=True, timeout=5)
            if status.returncode or status.stdout:
                return "worktree_dirty_or_unreadable"
            common = subprocess.run(["git", "-C", row["path"], "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    capture_output=True, text=True, timeout=5)
            expected = subprocess.run(["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                      capture_output=True, text=True, timeout=5)
            if common.returncode or expected.returncode or common.stdout != expected.stdout:
                return "worktree_repository_changed"
        return ""
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        return "unknown_reference: " + str(error)


@contextlib.contextmanager
def deletion_guard(row):
    with contextlib.ExitStack() as stack:
        metadata = row["metadata"]
        locks = []
        if metadata.get("project"):
            key = hashlib.sha256(metadata["project"].encode()).hexdigest()
            locks.append(Path(tempfile.gettempdir()) / "auto-agents-run-locks" / (key + ".lock"))
        if metadata.get("repair_root"):
            locks.append(Path(metadata["repair_root"]) / "repository.lock")
        if metadata.get("lock_path"):
            locks.append(Path(metadata["lock_path"]))
        for path in locks:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(path.open("a+"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if metadata.get("proof_database"):
            db = stack.enter_context(contextlib.closing(sqlite3.connect(Path(metadata["proof_database"]).as_uri() + "?mode=rw", uri=True, timeout=0.1)))
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM gate_proof_certificates,json_each(result_payload,'$.artifacts') WHERE json_each.value=? LIMIT 1", (metadata["blob"],)).fetchone():
                raise ValueError("proof blob acquired a new reference")
        # The project lock is now held by us. Avoid probing that same lock here;
        # the global artifact lifecycle lock fences new participating consumers.
        yield


def remove_worktree(row):
    repo, path = row["metadata"]["repository"], row["path"]
    listing = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True, timeout=10, check=True)
    if "worktree " + path not in listing.stdout.splitlines():
        raise ValueError("worktree is no longer registered in its owning repository")
    # No --force: never discard dirty files introduced since plan generation.
    subprocess.run(["git", "-C", repo, "worktree", "remove", path], capture_output=True, text=True, timeout=20, check=True)
    if Path(path).exists():
        raise RuntimeError("Git did not remove worktree")


def assert_session_unreferenced(project, identities):
    state = Path(project) / ".auto-agents/state"
    for path in (state / "workflows").glob("*/workflow.json"):
        workflow = _json(path)
        if workflow.get("status") not in {"completed", "cancelled", "closed", "superseded"}:
            raise ValueError("session history is referenced by a recoverable workflow")
    for path in (state / "sessions").glob("*/session_state.json"):
        if path.parent.name in identities:
            session = _json(path)
            draft = not any(session.get(key) for key in ("workflow_id", "active_handoff_id", "current_attempt", "conversation", "execution_log"))
            if not draft and session.get("status") not in {"completed", "cancelled", "closed"}:
                raise ValueError("cannot delete a recoverable session; close it first")
