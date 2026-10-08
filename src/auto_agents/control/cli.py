"""Public entrypoints delegate to a single business controller."""

from pathlib import Path
import argparse
import json
import os
import sys

from .types import ControlError, VerificationSpec, canonical
from .store import Store
from .engine import Engine
from .migration import migrate
from . import api, cleanup

EXECUTION = {"run", "fix", "collab", "resume", "provider-resolve", "provider-research"}
PUBLIC = {
    "business-status",
    "snapshot",
    "resume-check",
    "checkpoint",
    "migrate-state",
    "reconcile-call",
    "quiesce",
    "capabilities",
}


def parser():
    from .arguments import build_parser

    p = build_parser()
    p.set_defaults(
        json=False,
        session=None,
        workflow=None,
        run=None,
        provider=None,
        goal=None,
        spec_file=None,
        auto_approve=False,
        allow_dirty_tree=False,
        max_provider_calls=None,
        verify_command=None,
        reason="",
        text="",
        gate=None,
        scope="final",
    )
    return p


def emit(value):
    print(canonical(value))


def ready(project):
    database = Path(project) / ".auto-agents/state/business.sqlite3"
    state = database.parent
    if (
        database.exists()
        or any(state.glob("sessions/*/session_state.json"))
        or (state / "run_state.json").exists()
    ):
        report = migrate(project, check=True)
        if report["status"] != "already_current":
            if not report["ok"]:
                raise ControlError(
                    "migration_required",
                    "Legacy state requires reconciliation",
                    category="migration",
                    details=report,
                )
            migrate(project)
    return Store(project)


def main(argv=None):
    from ..env import load_dotenv

    load_dotenv([Path.cwd() / ".env"])
    try:
        return invoke(argv)
    except ControlError as error:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if "--json" in arguments or arguments and arguments[0] in PUBLIC:
            emit({"ok": False, "error": error.to_dict()})
        else:
            from ..bootstrap import print_failure

            print_failure(str(error), diagnostics=error.details.get("diagnostics_path"))
        return 3
    except (OSError, ValueError, RuntimeError) as error:
        emit({"ok": False, "error": {"category": "state", "message": str(error)}})
        return 3


def invoke(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    tools = {
        "prompt-eval",
        "persistence-configure",
        "persistence-rebind",
        "persistence-upgrade-contract",
        "cluster",
        "workers",
        "worker",
    }
    if arguments and arguments[0] in tools:
        from .project_tools import build_parser, dispatch as dispatch_tool

        return dispatch_tool(build_parser().parse_args(arguments))
    args = parser().parse_args(arguments)
    if args.command == "capabilities":
        emit(
            {
                "ok": True,
                "protocol": 2,
                "business_schema": 2,
                "modes": ["run", "fix", "collab", "provider_resolve"],
            }
        )
        return 0
    if args.command == "storage" and not getattr(args, "project", None):
        from .project_tools import storage

        return storage(args)
    project = Path(args.project).expanduser().resolve()
    if args.command == "release-worker" and args.wait:
        import time
        from ..run_lock import ProjectRunLock, RunAlreadyActiveError

        delay = 1.0
        while True:
            try:
                with ProjectRunLock(project):
                    from .release import worker

                    return worker(project, once=False)
            except RunAlreadyActiveError:
                time.sleep(delay)
                delay = min(delay * 2, 30)
    if args.command == "verify" and (args.explain or args.engine):
        from ..managed_verification import explain, execute_engine

        result = (
            explain(
                project,
                engine=args.engine,
                level=args.level,
                tests=args.test,
                changed_from=args.changed_from,
            )
            if args.explain
            else execute_engine(
                project, tests=args.test, level=args.level, fresh=args.fresh
            )
        )
        emit(result)
        return 0 if result["ok"] else 3
    if args.command == "business-status":
        emit(api.status(project))
        return 0
    if args.command == "resume-check":
        result = api.resume_check(project, args.resume_token)
        emit(result)
        return 0 if result["ok"] else 3
    if args.command == "stop":
        from ..run_lock import stop_project_run

        result, code = stop_project_run(project)
        emit(result)
        return code
    if args.command == "quiesce":
        from .process_api import quiesce

        result = quiesce(project)
        emit(result)
        return 0 if result["ok"] else 3
    from ..run_lock import ProjectRunLock

    try:
        with ProjectRunLock(project):
            from .observer import Observer

            with Observer(project) as observer:
                try:
                    return dispatch(project, args, arguments)
                except Exception as error:
                    if not isinstance(error, ControlError):
                        import traceback

                        error = ControlError(
                            "engine_exception",
                            str(error),
                            category="engine",
                            details={
                                "exception_type": type(error).__name__,
                                "traceback": traceback.format_exc(),
                                "cause": getattr(error, "evidence", {}),
                            },
                        )
                    fault = observer.fault(error, arguments)
                    if fault:
                        error.details["diagnostics_path"] = fault["diagnostics_path"]
                    raise error
                finally:
                    if (
                        args.command in EXECUTION
                        and (project / ".auto-agents/state/business.sqlite3").exists()
                    ):
                        from .projection import current

                        if current(project):
                            cleanup.collect(Store(project), git_gc=True)
    except ControlError as error:
        raise


def dispatch(project, args, argv):
    from ..config import load_project_config, save_project_config
    from ..models import ProjectConfig

    if args.command == "init":
        project.mkdir(parents=True, exist_ok=True)
        if (project / ".auto-agents/config.json").exists():
            raise ControlError("config", "Project configuration already exists")
        (project / ".auto-agents").mkdir(exist_ok=True)
        config = ProjectConfig(args.name or project.name)
        config.docs.language = args.doc_language
        save_project_config(project, config)
        Store(project)
        emit({"ok": True, "project": str(project), "schema": 2})
        return 0
    if args.command == "migrate-state":
        result = migrate(project, check=args.check)
        emit(result)
        return 0 if result["ok"] else 3
    if args.command == "snapshot":
        emit(api.snapshot(project, args.output))
        return 0
    store = ready(project)
    if args.command in EXECUTION:
        store.orphan_calls()
    if args.command in {"status", "sessions"}:
        result = api.status(project)
        if args.command == "sessions":
            records = [
                record
                for record in result["records"].values()
                if record["mode"] != "run" and not record["context"].get("archived")
            ]
            if args.mode:
                records = [
                    record
                    for record in records
                    if record["mode"] == args.mode.replace("-", "_")
                ]
            if not args.all:
                records = [
                    record
                    for record in records
                    if record["status"] not in {"COMPLETED", "CANCELLED"}
                ]
            result = {
                "ok": True,
                "schema": 2,
                "project": str(project),
                "sessions": [
                    {
                        "session_id": r["id"],
                        "mode": r["mode"],
                        "status": r["status"],
                        "phase": r["phase"],
                        "goal": r["goal"],
                        "calls": r["calls"],
                        "max_calls": r["max_calls"],
                        "updated": r["updated"],
                    }
                    for r in sorted(records, key=lambda r: r["updated"], reverse=True)
                ],
            }
        emit(result)
        return 0
    if args.command == "checkpoint":
        observation = json.loads(Path(args.observation).read_text())
        if observation.get("project") != str(project):
            raise ControlError("checkpoint", "Observation belongs to another project")
        fault = api.checkpoint(
            project,
            json.loads(Path(args.invocation).read_text()),
            observation["subject"],
            ControlError(
                "control_cycle",
                "Repeated business step without verified progress",
                category="engine",
                details={"monitor": "control_cycle"},
            ),
        )
        emit({"ok": True, "fault": fault})
        return 0
    if args.command == "storage":
        action = args.storage_action
        if action in {"clean", "maintain"}:
            emit(cleanup.collect(store, seconds=30))
        else:
            with store.connect() as db:
                emit(
                    {
                        "ok": True,
                        "resources": [
                            dict(x)
                            for x in db.execute(
                                "SELECT id,kind,owner,state FROM artifacts"
                            )
                        ],
                    }
                )
        return 0
    if args.command == "reconcile-call":
        operations = [o for o in store.operations() if o["id"] == args.call]
        unassigned = store.meta("unassigned_call:" + args.call)
        if not operations and not unassigned:
            raise ControlError("reconcile", "Unknown operation")
        unresolved = (
            operations[0]["state"] in {"UNKNOWN", "DISPATCHED"}
            if operations
            else unassigned.get("state") in {"unknown", "dispatched"}
        )
        if not unresolved:
            raise ControlError(
                "reconcile", "Only unresolved operations can be reconciled"
            )
        if bool(args.result) == bool(args.confirm_cancelled):
            raise ControlError("reconcile", "Supply exactly one confirmed outcome")
        if args.result:
            result = json.loads(Path(args.result).read_text())
            if result.get("operation_id") != args.call:
                raise ControlError("reconcile", "Receipt belongs to another operation")
            if operations:
                store.settle(args.call, result["result"])
            else:
                store.set_meta(
                    "unassigned_call:" + args.call,
                    {**unassigned, "state": "finished", "result": result["result"]},
                )
        elif args.confirm_cancelled:
            if operations:
                store.settle(
                    args.call,
                    {
                        "code": "provider_cancelled",
                        "message": "Operator confirmed cancellation",
                        "category": "provider",
                    },
                    state="FAILED",
                )
                store.consume(args.call)
            else:
                store.set_meta(
                    "unassigned_call:" + args.call, {**unassigned, "state": "cancelled"}
                )
        else:
            raise ControlError(
                "reconcile", "Supply a confirmed result or explicit cancellation"
            )
        if operations:
            owner = store.work(operations[0]["work"])
            unresolved = [
                o
                for o in store.operations()
                if o["state"] in {"UNKNOWN", "DISPATCHED"}
                and store.work(o["work"])["workflow"] == owner["workflow"]
            ]
            if not unresolved:
                for node in store.works():
                    if (
                        node["workflow"] == owner["workflow"]
                        and node["status"] == "BLOCKED"
                        and node["failure"].get("category") == "reconciliation"
                    ):
                        store.transition(node, "READY", failure={})
        emit({"ok": True, "operation": args.call})
        return 0
    config = load_project_config(project)
    alias = getattr(args, "provider", None)
    if alias and alias not in config.providers:
        raise ControlError("provider", "Unknown configured provider: " + alias)
    if alias and args.command == "run":
        config.active_provider = alias
        save_project_config(project, config)
    if getattr(args, "doc_language", None):
        config.docs.language = args.doc_language
        save_project_config(project, config)
    if getattr(args, "no_repo_map", False):
        config.repo_map.enabled = False
    if getattr(args, "max_tasks", None) is not None and args.max_tasks <= 0:
        raise ControlError("task_limit", "--max-tasks must be positive")
    engine = Engine(
        project,
        store,
        config,
        provider=alias,
        auto_approve=getattr(args, "auto_approve", False),
        print_fn=(lambda *x: None) if args.json else print,
        max_tasks=getattr(args, "max_tasks", None),
        fresh=bool(
            getattr(args, "fresh", False) or getattr(args, "full_verify", False)
        ),
    )
    identity = (
        getattr(args, "session", None)
        or getattr(args, "workflow", None)
        or getattr(args, "run", None)
    )
    if not identity and args.command not in {
        "fix",
        "collab",
        "provider-resolve",
        "provider-research",
    }:
        roots = [
            x
            for x in store.works()
            if not x["parent"] and x["status"] not in {"COMPLETED", "CANCELLED"}
        ]
        if args.command == "run":
            roots = [
                w
                for w in roots
                if w["mode"] == "run"
                and not store.contract(w["contract"]).inputs.get("release_only")
            ]
        if len(roots) == 1:
            identity = roots[0]["id"]
        elif len(roots) > 1 and args.command in {
            "run",
            "resume",
            "approve",
            "reject",
            "answer",
            "cancel",
        }:
            raise ControlError("selection", "Choose --session or --workflow explicitly")
    if args.command == "prototype" or args.command == "prototype-preview":
        from . import prototypes

        work = prototypes.owner(store)
        action = getattr(args, "prototype_command", "preview")
        if action == "list":
            emit({"ok": True, "variants": prototypes.candidates(engine, work)})
            return 0
        if action == "generate":
            emit(
                {
                    "ok": True,
                    "work": prototypes.generate(
                        engine,
                        work,
                        args.prompt,
                        name=args.name,
                        base=args.base_variant,
                    ),
                }
            )
            return 0
        return prototypes.preview(
            engine.context(work).workspace_root
            / ".auto-agents/docs/frontend_prototype",
            host=args.host,
            port=args.port,
        )
    if args.command == "inputs":
        from ..operator_inputs import OperatorInputStore, UserInputRequest

        inputs = OperatorInputStore(project)
        if args.inputs_command == "list":
            emit({"ok": True, "records": inputs.records()})
        elif args.inputs_command == "remove":
            emit({"ok": True, "removed": inputs.remove(args.key)})
        else:
            value = args.value
            if value is None:
                if not sys.stdin.isatty():
                    raise ControlError("input", "Supply --value")
                import getpass

                value = getpass.getpass("Value: ") if args.secret else input("Value: ")
            request = UserInputRequest.from_dict(
                {
                    "key": args.key,
                    "kind": "secret" if args.secret else "text",
                    "question": "Operator supplied value",
                    "purpose": "Project execution",
                    "why_required": "Explicit operator configuration",
                    "sensitivity": "secret" if args.secret else "private",
                }
            )
            record = inputs.save_answer(request, value, source="cli")
            emit({"ok": True, "record": record})
        return 0
    if args.command in {"sessions-delete", "sessions-clear"}:
        targets = (
            [store.work(args.session)]
            if args.command == "sessions-delete"
            else store.works()
        )
        if any(w["status"] not in {"COMPLETED", "CANCELLED"} for w in targets):
            raise ControlError(
                "retention", "Active or blocked progress cannot be discarded"
            )
        for work in targets:
            store.transition(work, work["status"], context={"archived": True})
            cleanup.release(store, work["id"])
        emit({"ok": True, "archived": [w["id"] for w in targets]})
        return 0
    if args.command == "sync-agent-instructions":
        from ..agent_instructions import sync_agent_instructions

        emit({"ok": True, "result": sync_agent_instructions(project).to_dict()})
        return 0
    if args.command == "audit-requirements":
        from ..requirements import run_requirements_audit
        from ..models import TaskSpec

        if not identity:
            raise ControlError("selection", "Choose an active run")
        work = store.work(identity)
        report = run_requirements_audit(
            engine.context(work).workspace_root,
            [TaskSpec.from_dict(t) for t in work["context"].get("tasks", [])],
        )
        emit(report)
        return 0 if report.get("ok") else 3
    if args.command == "performance":
        with store.connect() as db:
            emit(
                {
                    "ok": True,
                    "operations": dict(
                        db.execute(
                            "SELECT state,count(*) FROM operations GROUP BY state"
                        ).fetchall()
                    ),
                    "provider_calls": sum(w["calls"] for w in store.works()),
                }
            )
        return 0
    if args.command in {"approve", "reject", "answer", "cancel"}:
        if not identity:
            raise ControlError("selection", "Choose a retained work item")
        work = store.work(identity)
        if args.command != "cancel":
            work = decision_owner(store, work, args.command)
        if args.command == "reject" and (args.variant or args.all_except):
            from .prototypes import reject

            work = reject(
                engine,
                work,
                args.variant,
                all_except=args.all_except,
                reason=args.reason,
            )
            emit({"ok": True, "work": work})
            return 0
        if args.command == "cancel":
            for node in reversed(store.works()):
                if node["workflow"] == work["workflow"] and node["status"] not in {
                    "COMPLETED",
                    "CANCELLED",
                }:
                    store.transition(node, "CANCELLED")
                    cleanup.release(store, node["id"])
            work = store.work(work["id"])
            cleanup.collect(store)
        elif args.command == "approve":
            if (
                work["status"] != "WAITING"
                or work["context"].get("waiting_for") != "approval"
            ):
                raise ControlError("approval", "No pending approval")
            context = {
                k: v
                for k, v in work["context"].items()
                if k not in {"waiting_for", "approval"}
            }
            if work["context"]["approval"] == "prototype":
                from .prototypes import approve

                context = approve(engine, work, getattr(args, "variant", ""))
                context.pop("waiting_for", None)
                context.pop("approval", None)
            from .quality import artifact_hashes

            receipt = {
                "hashes": artifact_hashes(
                    engine.context(work).workspace_root,
                    context.get("approval_artifacts", []),
                ),
                "automatic": False,
            }
            if args.gate and args.gate != work["context"]["approval"]:
                raise ControlError("approval", "Gate differs from the pending decision")
            context["approvals"] = {
                **context.get("approvals", {}),
                work["context"]["approval"]: receipt,
            }
            if work["context"]["approval"] == "persistence-reset":
                context["persistence_approval"] = work["context"][
                    "approval_fingerprint"
                ]
            work = store.transition(work, "READY", context=context)
        else:
            if work["status"] not in {"WAITING", "BLOCKED"}:
                raise ControlError("input", "Work item is not waiting")
            text = (
                getattr(args, "text", "")
                or getattr(args, "reason", "")
                or getattr(args, "value", "")
            )
            if args.command == "answer":
                if getattr(args, "yes", False):
                    text = "yes"
                elif getattr(args, "no", False):
                    text = "no"
                elif getattr(args, "from_env", None):
                    text = os.environ.get(args.from_env, "")
                elif getattr(args, "from_file", None):
                    text = Path(args.from_file).read_text()
            if not text:
                raise ControlError("input", "Supply an answer or rejection reason")
            context = {
                **work["context"],
                "feedback": text,
                "operator_answers": [
                    *work["context"].get("operator_answers", []),
                    text,
                ],
            }
            requests = work["context"].get("input_requests", [])
            if args.command == "answer" and requests:
                from ..operator_inputs import OperatorInputStore, UserInputRequest

                selected = [
                    r
                    for r in requests
                    if not args.request_id or r.get("request_id") == args.request_id
                ]
                if len(selected) != 1:
                    raise ControlError(
                        "input", "Choose --request-id for one pending input"
                    )
                request = UserInputRequest.from_dict(selected[0])
                record = OperatorInputStore(project).save_answer(
                    request, text, source="cli"
                )
                context = {
                    **work["context"],
                    "feedback": "Operator input "
                    + request.key
                    + " is available through its declared binding.",
                    "operator_answers": [
                        *work["context"].get("operator_answers", []),
                        {
                            "key": request.key,
                            "request_id": request.request_id,
                            "answer_ref": record.get("answer_ref", ""),
                        },
                    ],
                    "input_requests": [
                        r for r in requests if r.get("request_id") != request.request_id
                    ],
                }
            context.pop("waiting_for", None)
            if args.command == "reject":
                context["approvals"] = {
                    k: v
                    for k, v in context.get("approvals", {}).items()
                    if k != work["context"].get("approval")
                }
            phase = (
                context.pop("approval_phase", work["phase"])
                if args.command == "reject"
                else work["phase"]
            )
            waiting = bool(context.get("input_requests"))
            if waiting:
                context["waiting_for"] = "user"
            work = store.transition(
                work, "WAITING" if waiting else "READY", phase=phase, context=context
            )
        if (
            args.command == "answer"
            and not args.no_resume
            and work["status"] == "READY"
        ):
            work = engine.resume(work["id"])
        emit({"ok": work["status"] != "BLOCKED", "work": work})
        if work["status"] == "BLOCKED":
            failure = work["failure"]
            raise ControlError(
                failure["code"],
                failure["message"],
                category=failure["category"],
                details=failure.get("details"),
            )
        return 0
    if args.command == "validate":
        from ..validation import validate_project_config_payload

        errors = validate_project_config_payload(config.to_dict())
        emit({"ok": not errors, "errors": errors})
        return 0 if not errors else 3
    if args.command == "upgrade":
        from ..config import migrate_project_config

        changed = migrate_project_config(project)
        emit({"ok": True, "configuration_changed": changed, "schema": 2})
        return 0
    if args.command == "verify":
        from .release import verify

        result = verify(
            engine,
            work_id=identity,
            level=args.level,
            tests=args.test,
            changed_from=args.changed_from,
        )
        emit({"ok": result["status"] == "COMPLETED", "work": result})
        return 0 if result["status"] == "COMPLETED" else 3
    if args.command == "release-worker":
        from .release import worker

        return worker(project, once=args.once)
    if args.command == "attest":
        from .release import attest

        result = attest(store, args.require_release)
        emit(result)
        return 0 if result["ok"] else 3
    if args.command in EXECUTION:
        mode = {
            "provider-resolve": "provider_resolve",
            "provider-research": "provider_resolve",
        }.get(args.command, args.command)
        if getattr(args, "restart_blocked", False) and identity:
            previous = store.work(identity)
            if previous["status"] != "BLOCKED":
                raise ControlError("restart", "Only a blocked run can be restarted")
            if any(
                o["state"] in {"UNKNOWN", "DISPATCHED"}
                for o in store.operations()
                if store.work(o["work"])["workflow"] == previous["workflow"]
            ):
                raise ControlError(
                    "outcome_unknown",
                    "Reconcile old operations before starting another workflow",
                    category="reconciliation",
                )
            engine.workspaces.snapshot(allow_dirty=False)
            store.transition(
                previous, "CANCELLED", context={**previous["context"], "archived": True}
            )
            identity = None
        if identity:
            work = store.work(identity)
            if args.command != "resume" and work["mode"] != mode:
                raise ControlError(
                    "selection", "Requested mode differs from retained work item"
                )
            store.set_meta("active_root", store.workflow(work["workflow"])["root"])
            from ..notifications import notify_flow_started

            notify_flow_started(
                project,
                workflow=work["mode"],
                identifier=work["id"],
                stage=work["phase"],
            )
            result = engine.resume(identity)
        else:
            if args.command == "resume":
                raise ControlError("selection", "No unique resumable workflow")
            goal = args.goal
            spec = Path(args.spec_file or project / "spec.md")
            if not goal and spec.is_file():
                goal = spec.read_text()
            if not goal:
                if not sys.stdin.isatty():
                    raise ControlError("goal", "Supply --goal or --spec-file")
                goal = input("请输入目标或问题：\n").strip()
            checks = (
                (VerificationSpec(args.verify_command),) if args.verify_command else ()
            )
            from ..notifications import notify_flow_started

            notify_flow_started(project, workflow=mode)
            result = engine.start(
                mode,
                goal,
                checks=checks,
                inputs={"force_release": bool(getattr(args, "full_verify", False))},
                allow_dirty=args.allow_dirty_tree,
                max_calls=args.max_provider_calls,
            )
        if result["status"] in {"COMPLETED", "BLOCKED"}:
            from ..notifications import notify_flow_finished
            from ..diagnostic_redaction import sanitize

            notify_flow_finished(
                project,
                workflow=result["mode"],
                status=result["status"].lower(),
                identifier=result["id"],
                stage=result["phase"],
                detail=sanitize(result["failure"].get("message", "")),
                paths=[store.path],
            )
        if args.json:
            emit({"ok": result["status"] == "COMPLETED", "work": result})
        else:
            print("任务状态：" + result["status"] + "；ID：" + result["id"])
        if result["status"] == "COMPLETED" and result["result"].get("release_work_id"):
            from .release import ensure_worker

            ensure_worker(project)
        if result["status"] == "BLOCKED":
            error = ControlError(
                result["failure"]["code"],
                result["failure"]["message"],
                category=result["failure"]["category"],
                details=result["failure"].get("details"),
            )
            fault = api.checkpoint(project, argv, result["id"], error)
            observation = os.environ.get("AUTO_AGENTS_OBSERVATION_FILE")
            if observation:
                Path(observation).write_text(
                    canonical(
                        {
                            "schema": 2,
                            "project": str(project),
                            "status": "failed",
                            "fault": fault,
                            "subject": result["id"],
                            "phase": result["phase"],
                            "step_id": fault["step_id"],
                            "progress_seq": 0,
                        }
                    )
                )
            print("执行已停止：" + str(error), file=sys.stderr)
            print("详细诊断：" + fault["diagnostics_path"], file=sys.stderr)
        return 0 if result["status"] in {"COMPLETED", "WAITING", "READY"} else 3
    raise ControlError("command", "Unsupported command")


def decision_owner(store, root, command):
    """Locate a pending decision through owned child links, without a repair path."""
    pending = []
    queue = [root]
    seen = set()
    while queue:
        work = queue.pop()
        if work["id"] in seen:
            raise ControlError("work_cycle", "Owned work graph contains a cycle")
        seen.add(work["id"])
        state = work["context"]
        waiting = state.get("waiting_for")
        if work["status"] == "WAITING" and waiting == (
            "user" if command == "answer" else "approval"
        ):
            pending.append(work)
        for identity in state.get("children", []) or (
            [state["child"]] if state.get("child") else []
        ):
            child = store.work(identity)
            if child["parent"] != work["id"]:
                raise ControlError("child_authority", "Child belongs to another parent")
            queue.append(child)
    if len(pending) != 1:
        raise ControlError(
            "selection", "Choose the work item with one pending decision"
        )
    return pending[0]


if __name__ == "__main__":
    raise SystemExit(main())
