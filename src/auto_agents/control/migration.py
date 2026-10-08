"""One-way import of proven legacy facts. The execution engine never imports this."""

from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid

from .store import Store
from .types import Contract, ControlError, VerificationSpec, canonical, digest
from .workspace import Workspaces, git, product_path


def legacy_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def inventory(project):
    project = Path(project).resolve()
    state = project / ".auto-agents/state"
    records = {}
    calls = []
    database = state / "business.sqlite3"
    if database.exists():
        db = sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            tables = {
                x[0]
                for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "control_meta" in tables:
                version = db.execute(
                    "SELECT value FROM control_meta WHERE key='schema'"
                ).fetchone()
                if not version or int(version[0]) != 2:
                    raise ControlError(
                        "schema",
                        "Unsupported business database schema",
                        category="migration",
                    )
                return {"current": True, "records": {}, "calls": []}
            if "records" not in tables:
                raise ControlError(
                    "migration", "Unknown business database", category="migration"
                )
            for row in db.execute("SELECT * FROM records"):
                if (
                    hashlib.sha256(row["payload"].encode()).hexdigest()
                    != row["reference"]
                ):
                    raise ControlError(
                        "migration",
                        "Legacy record checksum differs: " + row["path"],
                        category="migration",
                    )
                records[row["path"]] = json.loads(row["payload"])
            calls = [dict(row) for row in db.execute("SELECT * FROM calls")]
        finally:
            db.close()
    else:
        for pattern in (
            "sessions/*/session_state.json",
            "workflows/*/workflow.json",
            "handoffs/*.json",
            "run_state.json",
        ):
            for path in state.glob(pattern):
                records[path.relative_to(state).as_posix()] = json.loads(
                    path.read_text()
                )
    if (state / "recovery-kernel.json").exists():
        raise ControlError(
            "migration",
            "Export the retired recovery-kernel state before importing control v2",
            category="migration",
        )
    return {"current": False, "records": records, "calls": calls}


def resolve_handoff(records, identity):
    visited = set()
    chain = []
    while identity:
        if identity in visited or len(visited) >= 16:
            raise ControlError(
                "migration", "Invalid legacy resume chain", category="migration"
            )
        visited.add(identity)
        row = records.get("handoffs/" + identity + ".json")
        if not row:
            raise ControlError(
                "migration", "Missing handoff " + identity, category="migration"
            )
        chain.append(row)
        if row.get("target") != "resume":
            return row, chain
        identity = row.get("payload", {}).get("resume_handoff_id")
    raise ControlError(
        "migration", "Resume chain has no original task", category="migration"
    )


def retained_run(project, value, source_path, records=None):
    """Translate facts only. Unspecified source scope gets fresh classification."""
    from .effects import compile_checks
    from .quality import audit_requirements
    import shlex

    problems = []
    context = {}
    spec = Path(
        value.get("resume_context", {}).get("spec_file") or Path(project) / "spec.md"
    )
    goal = spec.read_text() if spec.is_file() else value.get("goal", "")
    if not goal:
        problems.append("goal_evidence_missing")
    stage = value.get("current_stage", "clarify")
    if stage not in {
        "clarify",
        "prototype",
        "design",
        "plan",
        "provider_research",
        "implement",
        "visual_judge",
        "verify",
        "readme",
    }:
        problems.append("stage_evidence_missing")
    artifacts = {
        "clarify": [
            ".auto-agents/docs/project_brief.md",
            ".auto-agents/docs/requirements.md",
        ],
        "prototype": [".auto-agents/docs/frontend_prototype/home.html"],
        "design": [".auto-agents/docs/architecture.md"],
        "plan": [".auto-agents/docs/task_plan.json"],
    }
    ordered = [
        "clarify",
        "prototype",
        "design",
        "plan",
        "provider_research",
        "implement",
        "visual_judge",
        "verify",
        "readme",
    ]
    from ..frontend_design import frontend_scope_requested

    trace = source_path / ".auto-agents/docs/requirements_trace.json"
    trace_payload = (
        json.loads(trace.read_text())
        if trace.is_file()
        else (records or {}).get("requirements_trace.json")
    )
    context["frontend"] = frontend_scope_requested(trace_payload)
    for prior in ordered[: ordered.index(stage) if stage in ordered else 0]:
        if prior == "prototype" and not context["frontend"]:
            continue
        for name in artifacts.get(prior, []):
            if not (source_path / name).is_file():
                problems.append("artifact_missing:" + name)
    normalized = []
    done = []
    for task in value.get("tasks", []):
        identity = task.get("task_id")
        refs = task.get("verification_refs", [])
        goal_text = "\n".join(
            str(task.get(k, ""))
            for k in ("title", "description", "acceptance")
            if task.get(k)
        )
        if not identity or not goal_text or not refs:
            problems.append("task_contract_missing:" + str(identity))
            continue
        commands = list(task.get("verification_commands", []))
        if not commands:
            python_refs = [r for r in refs if r.split("::")[0].endswith(".py")]
            js_refs = [
                r
                for r in refs
                if r.split("::")[0].endswith((".ts", ".tsx", ".js", ".jsx"))
            ]
            if len(python_refs) + len(js_refs) != len(refs):
                problems.append("test_target_missing:" + identity)
            if python_refs:
                commands.append("python -m pytest -q " + shlex.join(python_refs))
            if js_refs:
                commands.append("npm exec -- vitest run " + shlex.join(js_refs))
        try:
            checks = compile_checks(commands, source_path)
        except ControlError:
            problems.append("test_contract_invalid:" + identity)
            checks = ()
        paths = task.get("paths") or task.get("target_paths") or []
        normalized.append(
            {
                **task,
                "task_id": identity,
                "goal": goal_text,
                "paths": paths,
                "depends_on": task.get("depends_on", []),
                "checks": [x.to_dict() for x in checks],
                "classification_required": not paths,
            }
        )
        if task.get("status") == "done":
            history = task.get("verify_history", [])
            from .proof import executed_count

            verified = any(
                isinstance(x, dict)
                and x.get("ok") is True
                and x.get("commands")
                and any(
                    command.get("ok") is True
                    and executed_count(
                        str(command.get("stdout", ""))
                        + "\n"
                        + str(command.get("stderr", ""))
                    )
                    > 0
                    for command in x["commands"]
                    if isinstance(command, dict)
                )
                for x in history
            )
            reviewed = any(
                isinstance(x, dict) and x.get("decision") == "pass"
                for x in task.get("review_history", [])
            )
            commit = task.get("commit_sha")
            try:
                if not commit:
                    raise ControlError("missing", "missing")
                git(source_path, "cat-file", "-e", commit + "^{commit}")
            except ControlError:
                problems.append("task_commit_missing:" + identity)
            if not verified or not reviewed:
                problems.append("task_proof_missing:" + identity)
            else:
                done.append(identity)
    if normalized:
        try:
            audit_requirements(
                source_path, normalized, required=True, trace_payload=trace_payload
            )
        except (ControlError, ValueError) as error:
            problems.append("requirements_contract:" + str(error))
        context.update(tasks=normalized, done_tasks=done)
    checks = tuple(
        spec
        for task in normalized
        for spec in compile_checks(task["checks"], source_path)
    )
    context["legacy_approved_gates"] = value.get("approved_gates", [])
    # Original records remain in the verified migration archive. Do not keep
    # every old conversation/log twice in the active business database.
    context["legacy_progress"] = {
        "run_id": value.get("run_id"),
        "stage_statuses": value.get("stage_statuses", {}),
    }
    from .quality import artifact_hashes

    approval_paths = {
        "requirements": artifacts["clarify"],
        "architecture": artifacts["design"],
        "prototype": [
            ".auto-agents/docs/frontend_prototype/manifest.json",
            ".auto-agents/docs/frontend_prototype/home.html",
        ],
    }
    for gate in value.get("approved_gates", []):
        if gate not in approval_paths:
            continue
        paths = approval_paths[gate]
        try:
            hashes = artifact_hashes(source_path, paths)
        except ControlError:
            problems.append("approval_artifact_missing:" + gate)
            continue
        if gate == "prototype":
            from ..frontend_design import (
                load_frontend_design_lock,
                validate_frontend_design_artifacts,
            )

            errors = validate_frontend_design_artifacts(
                source_path,
                load_frontend_design_lock(source_path),
                require_approved=True,
            )
            if errors:
                problems.append("prototype_approval:" + str(errors))
                continue
        context.setdefault("approvals", {})[gate] = {
            "hashes": hashes,
            "legacy_record": value.get("run_id"),
            "automatic": False,
        }
    return goal, context, checks, problems


def plan(project):
    project = Path(project).resolve()
    old = inventory(project)
    if old["current"]:
        return {
            "ok": True,
            "status": "already_current",
            "schema": 2,
            "items": [],
            "issues": [],
        }
    items = []
    issues = []
    records = old["records"]
    for relative, value in records.items():
        if not (
            relative.endswith("/session_state.json") or relative == "run_state.json"
        ):
            continue
        identity = value.get("session_id") or value.get("run_id")
        mode = value.get("mode", "run")
        if not identity or mode not in {"run", "fix", "collab", "provider_resolve"}:
            issues.append({"record": relative, "code": "identity"})
            continue
        problem = []
        custody = value.get("candidate_custody", {})
        descriptor = value.get("source_descriptor", {})
        binding = value.get("verification_binding", {})
        source_path = Path(
            custody.get("checkout") or descriptor.get("checkout") or project
        )
        source = (
            descriptor.get("revision")
            or custody.get("base_revision")
            or value.get("baseline_git_ref")
        )
        if not source:
            try:
                source = git(source_path, "rev-parse", "HEAD")
            except ControlError:
                problem.append("source_unavailable")
        if source:
            try:
                source = git(source_path, "rev-parse", "--verify", source + "^{commit}")
            except ControlError:
                problem.append("source_unavailable")
        delivery_head = (
            custody.get("contract_revision")
            or descriptor.get("contract_revision")
            or source
        )
        if (
            not value.get("parent_handoff_id")
            and (custody.get("consumed_delivery") or {}).get("revision") == source
            and delivery_head
            and delivery_head != source
        ):
            # The parent received a verified private child commit. Its original
            # shared baseline remains the delivery base, and those received
            # bytes must stay a candidate delta rather than disappear into it.
            try:
                source = git(
                    source_path, "rev-parse", "--verify", delivery_head + "^{commit}"
                )
            except ControlError:
                problem.append("delivery_base_unavailable")
        if (
            source
            and "source_unavailable" not in problem
            and source_path == project
            and not custody.get("receipt")
        ):
            changed = [
                name
                for name in git(source_path, "diff", "--name-only", source).splitlines()
                if name and product_path(name)
            ]
            if changed:
                problem.append("unowned_source_changes")
        if custody and source_path.exists():
            registration = (
                project
                / ".auto-agents/state/custody"
                / (legacy_digest(str(source_path)) + ".json")
            )
            if registration.exists():
                reg = json.loads(registration.read_text())
                stat = source_path.stat()
                if (
                    reg.get("session_id") != identity
                    or reg.get("inode") != stat.st_ino
                    or reg.get("device") != stat.st_dev
                ):
                    problem.append("workspace_identity")
            elif not source_path.is_relative_to(
                project / ".auto-agents/candidate-custody"
            ):
                problem.append("workspace_registration_missing")
        if binding and binding.get("binding_fingerprint") != legacy_digest(
            {k: v for k, v in binding.items() if k != "binding_fingerprint"}
        ):
            problem.append("binding_integrity")
        command = value.get("fix_verify_command", "")
        issue = {}
        parent = ""
        handoff_id = value.get("parent_handoff_id") or value.get(
            "resume_context", {}
        ).get("parent_handoff_id")
        if handoff_id:
            try:
                original, chain = resolve_handoff(records, handoff_id)
                if original.get("child", {}).get("native_id") != identity:
                    problem.append("handoff_identity")
                parent = original.get("parent", {}).get("native_id", "")
                issue = original.get("payload", {}).get("issue_seed", {})
                if (
                    descriptor
                    and original.get("payload", {}).get("source_descriptor")
                    != descriptor
                ):
                    problem.append("source_authority")
                if command and issue.get("verification_command", command) != command:
                    problem.append("command_authority")
            except ControlError as error:
                problem.append(error.code + ":" + str(error))
        if binding and binding.get("fix_verify_command", command) != command:
            problem.append("command_binding")
        future_tests = []
        if command:
            from .proof import launches

            for invocation in launches(command):
                for target in invocation["targets"]:
                    path = source_path / target.split("::", 1)[0]
                    if not path.is_file() and not path.is_dir():
                        if value.get("status") in {
                            "verifying",
                            "executing",
                            "completed",
                        } or custody.get("receipt"):
                            problem.append("verification_target_missing:" + target)
                        else:
                            future_tests.append(target)
        status = value.get("status", "pending")
        if status == "completed":
            completed = any(
                x.get("action") == "receipt_completion"
                for x in value.get("execution_log", [])
            )
            accepted = value.get("acceptance_execution", {}).get("phase") == "completed"
            if mode != "collab" and not completed or mode == "collab" and not accepted:
                problem.append("completion_evidence_missing")
        phase = (
            "classify"
            if mode == "fix"
            else (
                "diagnose"
                if mode == "collab"
                else (
                    value.get("current_stage", "clarify")
                    if mode == "run"
                    else "research"
                )
            )
        )
        if mode == "fix" and status in {"executing", "verifying"}:
            phase = "verify" if custody.get("receipt") else "implement"
        progress = {}
        run_checks = ()
        if mode == "run":
            run_goal, progress, run_checks, run_problems = retained_run(
                project, value, source_path, records
            )
            problem.extend(run_problems)
        if custody.get("receipt"):
            try:
                from ..models import SessionState
                from ..session_candidate import validate_receipt

                validate_receipt(SessionState.from_dict(value))
            except (RuntimeError, ValueError, KeyError, OSError) as error:
                problem.append("candidate_receipt:" + str(error))
            else:
                if status != "completed":
                    phase = "verify"
        items.append(
            {
                "id": identity,
                "mode": mode,
                "parent": parent,
                "goal": (run_goal if mode == "run" else "")
                or issue.get("summary")
                or value.get("goal")
                or value.get("resume_context", {}).get("goal", ""),
                "root_goal": value.get("goal", ""),
                "source": source or "",
                "delivery_head": delivery_head or "",
                "source_path": str(source_path),
                "phase": phase,
                "command": command,
                "issue": issue,
                "status": status,
                "record": relative,
                "problems": problem,
                "context": {
                    **progress,
                    **({"planned_test_targets": future_tests} if future_tests else {}),
                },
                "checks": [x.to_dict() for x in run_checks],
            }
        )
        issues.extend({"work_id": identity, "code": code} for code in problem)
    pending = [x["id"] for x in old["calls"] if x["state"] in {"dispatched", "unknown"}]
    issues.extend({"operation_id": x, "code": "outcome_unknown"} for x in pending)
    return {
        "ok": not issues,
        "status": "blocked" if issues else "ready",
        "schema": 2,
        "items": items,
        "issues": issues,
        "record_count": len(records),
        "call_count": len(old["calls"]),
    }


def migrate(project, *, check=False):
    project = Path(project).resolve()
    report = plan(project)
    if check or report["status"] == "already_current":
        return report
    old = inventory(project)
    state = project / ".auto-agents/state"
    state.mkdir(parents=True, exist_ok=True)
    # A new schema can contain blocked nodes, but never silently discards their evidence.
    temporary = state / ("migration-" + uuid.uuid4().hex + ".sqlite3")
    target = Store(project, path=temporary)
    workspaces = Workspaces(project, target)
    aliases = {}
    by_id = {}
    records = old["records"]
    published = False
    try:
        for item in report["items"]:
            source = item["source"]
            if (
                source
                and "source_unavailable" not in item["problems"]
                and Path(item["source_path"]).exists()
            ):
                workspaces.bare(
                    "fetch", item["source_path"], source + ":refs/imports/" + item["id"]
                )
            else:
                source = "unavailable-" + item["id"]
            target.set_meta(
                "source:" + source,
                {
                    "head": source,
                    "project": str(project),
                    "dirty": [],
                    "tree": (
                        workspaces.bare("rev-parse", source + "^{tree}")
                        if not source.startswith("unavailable-")
                        else ""
                    ),
                },
            )
            value = records[item["record"]]
            checks = (
                tuple(VerificationSpec.read(x) for x in item["checks"])
                if item.get("checks")
                else (VerificationSpec(item["command"]),) if item["command"] else ()
            )
            work = target.create_workflow(
                item["mode"],
                item["goal"] or "Retained goal needs reconciliation",
                source,
                authorization={
                    **(
                        value.get("authorization_policy")
                        or value.get("resume_context", {}).get(
                            "authorization_policy", {}
                        )
                    ),
                    "auto_approve": bool(
                        value.get(
                            "auto_approve",
                            value.get("resume_context", {}).get("auto_approve", False),
                        )
                    ),
                },
                inputs={
                    "issue": item["issue"],
                    "migration_origin": item["record"],
                    "parent_goal": item.get("root_goal", ""),
                    "execution_environment": value.get("verification_binding", {}).get(
                        "execution_environment",
                        value.get("goal_execution_environment", {}),
                    ),
                    "legacy_binding_fingerprint": value.get(
                        "verification_binding", {}
                    ).get("binding_fingerprint", ""),
                    "delivery_head": item["delivery_head"],
                },
                checks=checks,
                node_limit=max(1, int(value.get("hard_ceiling", 15))),
                identity=item["id"],
            )
            by_id[item["id"]] = work
            aliases[item["id"]] = work["id"]
            status = (
                "COMPLETED"
                if item["status"] == "completed" and not item["problems"]
                else (
                    "BLOCKED"
                    if item["problems"] or item["status"] == "blocked"
                    else "READY"
                )
            )
            with target.connect(True) as db:
                db.execute(
                    "UPDATE work_items SET phase=?,status=?,failure=?,calls=?,context=? WHERE id=?",
                    (
                        item["phase"],
                        status,
                        (
                            canonical(
                                {
                                    "code": "migration_evidence",
                                    "category": "migration",
                                    "message": "Reconcile retained evidence",
                                    "details": {"issues": item["problems"]},
                                }
                            )
                            if status == "BLOCKED"
                            else "{}"
                        ),
                        max(
                            int(value.get("current_attempt", 0)),
                            sum(
                                1
                                for c in old["calls"]
                                if c["model"]
                                and c["subject"].split(":")[-1] == item["id"]
                            ),
                        ),
                        canonical(item.get("context", {})),
                        work["id"],
                    ),
                )
            custody = value.get("candidate_custody", {})
            if (
                Path(item["source_path"]).exists()
                and not source.startswith("unavailable-")
                and status != "COMPLETED"
                and "unowned_source_changes" not in item["problems"]
            ):
                destination = workspaces.ensure(work, source)
                path = Path(item["source_path"])
                names = set(git(path, "ls-files", "-z").split("\0")) | set(
                    git(path, "ls-files", "--others", "--exclude-standard", "-z").split(
                        "\0"
                    )
                )
                names |= set(git(destination, "ls-files", "-z").split("\0"))
                names |= set(custody.get("receipt", {}).get("manifest", {}))
                names |= {
                    p.relative_to(path).as_posix()
                    for p in (path / ".auto-agents/docs").rglob("*")
                    if p.is_file()
                }
                from .quality import safe_file

                for name in sorted(names):
                    if not name:
                        continue
                    from .workspace import product_path

                    if not product_path(name):
                        continue
                    src = safe_file(path, name)
                    dst = safe_file(destination, name)
                    if src.is_file():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                    elif dst.is_file():
                        dst.unlink()
                for name in (
                    "requirements_trace.json",
                    "task_plan.json",
                    "frontend_design.lock.json",
                ):
                    dst = destination / ".auto-agents/docs" / name
                    if dst.exists():
                        continue
                    bound = value.get("verification_binding", {})
                    retained = bound.get("proof_sources", {}).get(
                        ".auto-agents/state/" + name
                    )
                    payload = bound.get("plan") if name == "task_plan.json" else None
                    payload = payload or records.get(name)
                    if retained is not None or payload is not None:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_text(
                            retained
                            if isinstance(retained, str)
                            else canonical(payload)
                        )
                candidate = workspaces.candidate(
                    target.work(work["id"]), destination, source
                )
                node = target.work(work["id"])
                approvals = node["context"].get("approvals", {})
                lock_path = destination / ".auto-agents/docs/frontend_design.lock.json"
                if (
                    lock_path.exists()
                    and json.loads(lock_path.read_text()).get("status") == "approved"
                ):
                    from ..frontend_design import (
                        validate_frontend_design_artifacts,
                        frontend_design_artifact_hashes,
                    )

                    lock = json.loads(lock_path.read_text())
                    errors = validate_frontend_design_artifacts(
                        destination, lock, require_approved=True
                    )
                    if errors:
                        target.transition(
                            node,
                            "BLOCKED",
                            failure={
                                "code": "migration_evidence",
                                "category": "migration",
                                "message": "Approved prototype evidence is incomplete",
                                "details": {"issues": errors},
                            },
                        )
                        node = target.work(node["id"])
                    else:
                        approvals = {
                            **approvals,
                            "legacy_prototype": {
                                "hashes": frontend_design_artifact_hashes(destination),
                                "automatic": False,
                            },
                        }
                if item["mode"] == "fix" and checks:
                    from .proof import launches
                    from .quality import artifact_hashes

                    proofs = sorted(
                        {
                            ref.split("::", 1)[0]
                            for spec in checks
                            for invocation in launches(spec.command)
                            for ref in invocation["targets"]
                            if (destination / ref.split("::", 1)[0]).is_file()
                        }
                    )
                    if proofs:
                        approvals = {
                            **approvals,
                            "legacy_proofs": {
                                "hashes": artifact_hashes(destination, proofs),
                                "automatic": False,
                            },
                        }
                target.transition(
                    node,
                    node["status"],
                    context={
                        **node["context"],
                        "candidate": candidate,
                        "approvals": approvals,
                    },
                )
        for item in report["items"]:
            if item["parent"] in by_id:
                parent = target.work(item["parent"])
                child = target.work(item["id"])
                pc = target.contract(parent["contract"])
                cc = target.contract(child["contract"])
                from dataclasses import replace

                updated = replace(
                    cc, workflow_id=parent["workflow"], parent_contract=pc.identity
                )
                with target.connect(True) as db:
                    db.execute(
                        "INSERT OR IGNORE INTO contracts VALUES(?,?)",
                        (updated.identity, canonical(updated.to_dict())),
                    )
                    db.execute(
                        "UPDATE work_items SET workflow=?,parent=?,contract=? WHERE id=?",
                        (
                            parent["workflow"],
                            parent["id"],
                            updated.identity,
                            child["id"],
                        ),
                    )
                    db.execute(
                        "DELETE FROM workflows WHERE id=? AND id NOT IN (SELECT workflow FROM work_items)",
                        (child["workflow"],),
                    )
        for item in report["items"]:
            value = records[item["record"]]
            active = value.get("active_handoff_id")
            if active:
                try:
                    original, _ = resolve_handoff(records, active)
                    child = original["child"]["native_id"]
                    if child in by_id:
                        with target.connect(True) as db:
                            parent = target.work(item["id"])
                            db.execute(
                                "UPDATE work_items SET status=?,context=? WHERE id=?",
                                (
                                    "WAITING",
                                    canonical(
                                        {
                                            **parent["context"],
                                            "child": child,
                                            "waiting_for": "child",
                                        }
                                    ),
                                    item["id"],
                                ),
                            )
                except (ControlError, KeyError):
                    pass
        call_ordinals = {}
        for call in old["calls"]:
            subject = call["subject"].split(":")[-1]
            if subject not in by_id:
                target.set_meta("unassigned_call:" + call["id"], call)
                continue
            result = json.loads(call["result"]) if call.get("result") else None
            state_name = (
                "UNKNOWN"
                if call["state"] in {"dispatched", "unknown"}
                else "CONFIRMED" if call["state"] == "finished" else "FAILED"
            )
            key = (subject, call["phase"])
            ordinal = call_ordinals.get(key, 0)
            call_ordinals[key] = ordinal + 1
            with target.connect(True) as db:
                db.execute(
                    "INSERT INTO operations(id,work,kind,ordinal,input_hash,state,result,consumed,model,provider,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        call["id"],
                        subject,
                        "legacy:" + call["phase"],
                        ordinal,
                        digest({"legacy_id": call["id"]}),
                        state_name,
                        canonical(result) if result is not None else None,
                        1 if state_name != "UNKNOWN" else 0,
                        call["model"],
                        "",
                        call["created"],
                    ),
                )
        for relative, record in records.items():
            if relative.startswith("workflows/") and relative.endswith(
                "/workflow.json"
            ):
                original_root = record.get("root", {}).get("native_id")
                if original_root in by_id:
                    aliases[record.get("workflow_id") or relative.split("/")[1]] = (
                        original_root
                    )
        target.set_meta("aliases", aliases)
        target.set_meta("migration_report", report)
        for row in target.works():
            with target.connect(True) as db:
                total = db.execute(
                    "SELECT sum(calls) FROM work_items WHERE workflow=?",
                    (row["workflow"],),
                ).fetchone()[0]
                db.execute(
                    "UPDATE workflows SET calls=? WHERE id=?", (total, row["workflow"])
                )
        with target.connect() as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ControlError("migration", "Migration database integrity failed")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup = (
            state
            / "legacy-archive"
            / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        )
        backup.mkdir(parents=True, exist_ok=True)
        original = state / "business.sqlite3"
        if original.exists():
            with (
                sqlite3.connect(original) as source_db,
                sqlite3.connect(backup / "business.sqlite3") as backup_db,
            ):
                source_db.backup(backup_db)
            for suffix in ("-wal", "-shm"):
                Path(str(original) + suffix).unlink(missing_ok=True)
        (backup / "migration-report.json").write_text(canonical(report))
        if not original.exists():
            (backup / "records.json").write_text(canonical(old["records"]))
        os.replace(temporary, original)
        published = True
        from ..config import ensure_auto_gitignore

        ensure_auto_gitignore(project)
        return {
            **report,
            "ok": True,
            "status": "migrated_with_blocks" if report["issues"] else "migrated",
            "migrated": True,
            "backup": str(backup),
        }
    finally:
        if not published:
            # No model executes during import. Roll back only the private
            # workspaces registered in this failed staging database; original
            # custody and old business state remain untouched.
            for work in target.works():
                registration = target.meta("workspace:" + work["id"])
                if registration:
                    try:
                        workspaces.release(work)
                    except (OSError, ControlError):
                        pass
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
