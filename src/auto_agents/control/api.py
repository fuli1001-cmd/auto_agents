"""Version 2 process protocol, independent of the optional watcher store."""

from pathlib import Path
import json
import os
import shutil
import sqlite3
import uuid

from .store import Store
from .types import canonical, digest, ControlError
from .migration import migrate


def status(project):
    store = Store(project, readonly=True)
    if not store.path.exists():
        return {
            "ok": True,
            "schema": 2,
            "project": str(store.project),
            "identity": digest({}),
            "records": {},
            "pending_external": [],
        }
    with store.connect() as db:
        if "control_meta" not in {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }:
            return {
                "ok": False,
                "schema": 1,
                "project": str(store.project),
                "status": "migration_required",
                "pending_external": [],
            }
    works = store.works()
    records = {}
    for w in works:
        contract = store.contract(w["contract"])
        from .quality import source_seal

        live = store.project / ".auto-agents/state/owned/workspaces" / w["id"]
        retained = store.meta("workspace_snapshot:" + w["id"], {})
        input_hash = source_seal(live) if live.is_dir() else retained.get("input_hash")
        records["work_items/" + w["id"]] = {
            **w,
            "goal": contract.goal,
            "contract_id": contract.identity,
            "input_hash": input_hash,
            "session_id": w["id"] if w["mode"] != "run" else "",
            "run_id": w["id"] if w["mode"] == "run" else "",
        }
    pending = [
        {"id": x["id"], "subject": x["work"], "phase": x["kind"], "state": x["state"]}
        for x in store.operations()
        if x["state"] in {"UNKNOWN", "DISPATCHED"}
    ]
    return {
        "schema": 2,
        "ok": True,
        "project": str(store.project),
        "identity": digest(records),
        "records": records,
        "pending_external": pending,
    }


def snapshot(project, destination):
    project = Path(project).resolve()
    destination = Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(project):
        raise ControlError("snapshot", "Snapshot must be a new external directory")
    source = Store(project, readonly=True)
    current = status(project)
    if current["pending_external"]:
        raise ControlError(
            "outcome_unknown",
            "Unconfirmed external operations require reconciliation",
            category="reconciliation",
        )
    destination.mkdir(parents=True)
    snapshot_source(project, destination)
    state = destination / ".auto-agents/state"
    state.mkdir(parents=True)
    # Git objects are shared by all active workspaces. Copy one object repository,
    # not every historical checkout and every ignored product directory.
    owned = source.project / ".auto-agents/state/owned"
    if owned.exists():
        shutil.copytree(
            owned,
            state / "owned",
            symlinks=True,
            ignore=shutil.ignore_patterns("operations", "workspaces", "__pycache__"),
        )
    if source.path.exists():
        with (
            source.connect() as db,
            sqlite3.connect(state / "business.sqlite3") as copy,
        ):
            db.backup(copy)
    else:
        Store(destination)
    config = project / ".auto-agents/config.json"
    if config.exists():
        data = json.loads(config.read_text())
        for provider in data.get("providers", {}).values():
            provider["environment"] = {}
        (destination / ".auto-agents/config.json").write_text(canonical(data))
    copied = Store(destination)
    from .workspace import Workspaces, git, product_path
    import tempfile

    archive = Workspaces(destination, copied)
    # Preserve live candidate bytes and the original HEAD/index separately.
    # Committing a snapshot must not manufacture a different execution HEAD.
    for work in source.works() if source.path.exists() else []:
        live = owned / "workspaces" / work["id"]
        if not live.exists():
            continue
        registration = source.meta("workspace:" + work["id"])
        if (
            live.is_symlink()
            or not registration
            or list((live.stat().st_dev, live.stat().st_ino))
            != registration.get("identity")
        ):
            raise ControlError("snapshot", "Active workspace ownership changed")
        head = git(live, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory(
            prefix="export-", dir=archive.root
        ) as directory:
            private = Path(directory) / "checkout"
            private.mkdir()
            git(private, "init")
            git(private, "fetch", str(archive.archive), head)
            git(private, "checkout", "--detach", head)
            names = set(git(live, "ls-files", "-z").split("\0")) | set(
                git(live, "ls-files", "--others", "--exclude-standard", "-z").split(
                    "\0"
                )
            )
            names |= set(git(private, "ls-files", "-z").split("\0"))
            declared = {
                name
                for spec in source.contract(work["contract"]).checks
                for name in spec.outputs
            }
            names |= declared
            for name in sorted(names):
                if not product_path(name):
                    continue
                src = live / name
                dst = private / name
                if src.is_symlink():
                    raise ControlError(
                        "snapshot", "Undeclared product symlink: " + name
                    )
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                elif dst.is_file():
                    dst.unlink()
            git(private, "add", "-A")
            if declared:
                git(
                    private,
                    "add",
                    "-f",
                    "--",
                    *[name for name in declared if (private / name).is_file()],
                )
            git(
                private,
                "-c",
                "user.name=auto-agents",
                "-c",
                "user.email=local@auto-agents",
                "commit",
                "--allow-empty",
                "-m",
                "snapshot: retained active bytes",
            )
            revision = git(private, "rev-parse", "HEAD")
            archive.bare(
                "fetch", str(private), revision + ":refs/offline/" + work["id"]
            )
        index = Path(git(live, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = live / index
        saved = archive.root / "snapshot-inputs" / (work["id"] + ".index")
        saved.parent.mkdir(exist_ok=True)
        shutil.copy2(index, saved)
        from .quality import source_seal

        copied.set_meta(
            "workspace_snapshot:" + work["id"],
            {
                "head": head,
                "revision": revision,
                "index": str(saved.relative_to(destination)),
                "input_hash": source_seal(live),
            },
        )
    # Workspaces are rematerialized from their immutable revisions by the
    # isolated verifier; relocation never changes the original source contract.
    for work in copied.works():
        copied.set_meta("workspace:" + work["id"], {"relocated": True})
    return {
        "ok": True,
        "schema": 2,
        "project": str(project),
        "snapshot": str(destination),
        "record_count": len(current["records"]),
        "state": {k: v for k, v in current.items() if k != "records"},
    }


def snapshot_source(project, destination):
    """Keep exact HEAD/tree IDs without copying protected blobs or user history."""
    import subprocess
    from .workspace import git, product_path
    from .quality import safe_file

    git(destination, "init")
    try:
        head = git(project, "rev-parse", "HEAD")
    except ControlError:
        return

    def copy_object(identity, kind):
        raw = subprocess.run(
            ["git", "-C", str(project), "cat-file", kind, identity],
            capture_output=True,
            check=True,
        ).stdout
        created = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(destination),
                    "hash-object",
                    "-w",
                    "-t",
                    kind,
                    "--stdin",
                ],
                input=raw,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        if created != identity:
            raise ControlError("snapshot", "Git object identity changed")

    copy_object(head, "commit")
    trees = {git(project, "rev-parse", head + "^{tree}")}
    entries = git(project, "ls-tree", "-r", "-t", head).splitlines()
    for line in entries:
        header, name = line.split("\t", 1)
        mode, kind, identity = header.split()
        if kind == "tree":
            trees.add(identity)
        elif kind == "blob" and product_path(name):
            copy_object(identity, "blob")
    for identity in trees:
        copy_object(identity, "tree")
    ref = subprocess.run(
        ["git", "-C", str(project), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
    )
    if ref.returncode == 0:
        git(destination, "symbolic-ref", "HEAD", ref.stdout.strip())
    else:
        git(destination, "update-ref", "--no-deref", "HEAD", head)
    git(destination, "update-ref", "HEAD", head)
    (destination / ".git/shallow").write_text(head + "\n")
    git(destination, "read-tree", head)
    index = Path(git(project, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = project / index
    if index.is_file():
        shutil.copy2(index, destination / ".git/index")
    protected = [
        name
        for name in git(project, "ls-files", "-z").split("\0")
        if name and not product_path(name)
    ]
    if protected:
        git(destination, "update-index", "--skip-worktree", "--", *protected)
    names = set(git(project, "ls-files", "-z").split("\0")) | set(
        git(project, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    )
    names |= {
        p.relative_to(project).as_posix()
        for p in (project / ".auto-agents/docs").rglob("*")
        if p.is_file()
    }
    for name in names:
        if not product_path(name):
            continue
        source = safe_file(project, name)
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def checkpoint(project, argv, work_id, error):
    store = Store(project)
    work = store.work(work_id)
    state = status(project)
    while work["failure"].get("child_id"):
        child = store.work(work["failure"]["child_id"])
        if child["parent"] != work["id"]:
            raise ControlError(
                "checkpoint", "Failure attribution belongs to another parent"
            )
        work = child
    work_id = work["id"]
    root = store.workflow(work["workflow"])["root"]
    selected = store.work(root)
    invocation = [selected["mode"].replace("_", "-")]
    index = 1
    discard = {
        "--session",
        "--workflow",
        "--run",
        "--goal",
        "--spec-file",
        "--verify-command",
        "--max-provider-calls",
    }
    while index < len(argv):
        arg = argv[index]
        if arg.split("=", 1)[0] in discard:
            index += 1 if "=" in arg else 2
            continue
        invocation.append(arg)
        index += 1
    invocation += ["--session", root]
    token = {
        "schema": 2,
        "project": str(store.project),
        "argv": invocation,
        "root_id": root,
        "work_id": work_id,
        "step_id": work_id + ":" + work["phase"],
        "contract_id": work["contract"],
        "state_identity": state["identity"],
    }
    directory = store.project / ".auto-agents/state/resume-checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (uuid.uuid4().hex + ".json")
    path.write_text(canonical(token))
    from ..diagnostic_redaction import sanitize

    def redacted(value):
        if isinstance(value, dict):
            return {k: redacted(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [redacted(v) for v in value]
        return sanitize(value) if isinstance(value, str) else value

    fault = {
        "category": error.category if isinstance(error, ControlError) else "engine",
        "type": (
            error.details.get("exception_type", type(error).__name__)
            if isinstance(error, ControlError)
            else type(error).__name__
        ),
        "message": str(error),
        "step_id": token["step_id"],
        "resume_token": str(path),
        "evidence": error.details if isinstance(error, ControlError) else {},
    }
    fault = redacted(fault)
    diagnostic = path.with_suffix(".diagnostics.json")
    diagnostic.write_text(canonical(fault))
    fault["diagnostics_path"] = str(diagnostic)
    return fault


def resume_check(project, token_path):
    if (
        os.environ.get("AUTO_AGENTS_OFFLINE_RESUME") != "1"
        or os.environ.get("AUTO_AGENTS_WATCH_ISOLATED") != "1"
        or not Path("/.dockerenv").exists()
    ):
        raise ControlError(
            "isolation", "resume-check requires the credential-free offline container"
        )
    token = json.loads(Path(token_path).read_text())
    project = Path(project).resolve()
    if token.get("schema") != 2 or token.get("project") != str(project):
        raise ControlError("checkpoint", "Checkpoint project or schema differs")
    current = status(project)
    if current["identity"] != token["state_identity"]:
        return {
            "ok": False,
            "category": "state",
            "reason": "Resume checkpoint changed",
            "blocked_step_cleared": False,
            "external_calls": 0,
        }
    from ..config import load_project_config
    from .engine import Engine

    store = Store(project)
    work = store.work(token["work_id"])
    if work["contract"] != token["contract_id"]:
        raise ControlError("checkpoint", "Retained contract changed")
    engine = Engine(
        project,
        store,
        load_project_config(project),
        offline=True,
        auto_approve=True,
        print_fn=lambda *a: None,
    )
    before_contracts = {w["id"]: store.contract(w["contract"]) for w in store.works()}
    before_budgets = {
        w["id"]: (w["max_calls"], store.workflow(w["workflow"])["max_calls"])
        for w in store.works()
    }
    result = engine.resume(token["root_id"])
    failure = result["failure"]

    def constraints(identity, previous):
        current = store.contract(store.work(identity)["contract"])
        fields = (
            "workflow_id",
            "work_id",
            "mode",
            "goal",
            "source",
            "authorization",
            "inputs",
            "parent_contract",
        )
        return (
            all(getattr(previous, k) == getattr(current, k) for k in fields)
            and (not previous.checks or previous.checks == current.checks)
            and (not previous.scope or previous.scope == current.scope)
        )

    preserved = all(constraints(k, v) for k, v in before_contracts.items())
    preserved = preserved and all(
        (
            store.work(k)["max_calls"],
            store.workflow(store.work(k)["workflow"])["max_calls"],
        )
        == v
        for k, v in before_budgets.items()
    )
    visited = {x["step_id"] for x in engine.visited}
    progressed = (
        store.work(token["work_id"])["phase"] != work["phase"]
        or store.work(token["work_id"])["status"] == "COMPLETED"
    )
    cleared = (
        failure.get("category") == "offline"
        and preserved
        and token["step_id"] in visited
        and progressed
    )
    return {
        "ok": cleared,
        "blocked_step_cleared": cleared,
        "external_calls": 0,
        "retained_constraints": preserved,
        "category": failure.get("category", "state"),
        "type": failure.get("details", {}).get("exception_type", "ControlError"),
        "reason": failure.get("message", ""),
        "steps": engine.visited,
        "next_operation": failure.get("details"),
    }
