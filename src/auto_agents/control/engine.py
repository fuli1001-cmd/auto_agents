"""One iterative business controller; handlers supply facts, never mutate state."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json
import os
import uuid

from .types import Contract, ControlError, ExecutionContext, Status, RUN_PHASES, digest
from .effects import Provider, Verifier, parse_reply, compile_checks
from .prompts import build
from .workspace import Workspaces, git, product_path
from . import cleanup


class Engine:
    def __init__(
        self,
        project,
        store,
        config,
        *,
        provider=None,
        transport=None,
        verifier=None,
        auto_approve=False,
        print_fn=print,
        offline=False,
        max_tasks=None,
        fresh=False,
    ):
        self.project = Path(project).resolve()
        self.store = store
        self.config = config
        self.provider = provider or config.active_provider
        self.explicit_provider = provider
        self.transport = transport or Provider(config, alias=self.provider)
        self.verifier = verifier or Verifier(config, fresh=fresh)
        self.auto_approve = auto_approve
        self.workspaces = Workspaces(self.project, store)
        self.print = print_fn
        self.offline = offline
        self.visited = []
        self.task_limit = max_tasks
        self.completed_tasks = 0
        parallel = config.execution.parallel_tasks
        self.parallel_workers = (
            parallel.max_auto_workers
            if parallel.workers == "auto"
            else max(1, int(parallel.workers))
        )

    def start(
        self,
        mode,
        goal,
        *,
        inputs=None,
        checks=(),
        scope=(),
        allow_dirty=False,
        max_calls=None,
    ):
        source = self.workspaces.snapshot(allow_dirty=allow_dirty)
        authorization = {
            "auto_approve": self.auto_approve,
            "real": bool((inputs or {}).get("real", True)),
        }
        work = self.store.create_workflow(
            mode,
            goal,
            source,
            inputs={"control_version": 2, **(inputs or {})},
            checks=checks,
            scope=scope,
            authorization=authorization,
            max_calls=max_calls,
            node_limit=self.config.execution.session_limits.for_mode(mode),
        )
        self.store.set_meta("active_root", work["id"])
        return self.resume(work["id"])

    def resume(self, identity):
        work = self.store.work(identity)
        if work["parent"]:
            work = self.store.work(self.store.workflow(work["workflow"])["root"])
        self.store.set_meta("active_root", work["id"])
        for operation in self.store.operations():
            if operation["kind"] == "deliver" and operation["state"] == "UNKNOWN":
                journal = self.store.meta("delivery:" + operation["id"])
                if journal:
                    receipt = self.workspaces.finish_delivery(operation["id"], journal)
                    self.store.settle(operation["id"], receipt)
                    owner = self.store.work(operation["work"])
                    if owner["failure"].get("category") == "reconciliation":
                        self.store.transition(owner, "READY", failure={})
        work = self.store.work(work["id"])
        if work["status"] in {"COMPLETED", "CANCELLED"}:
            return work
        if self.explicit_provider:
            for node in self.store.works():
                if node["workflow"] != work["workflow"] or node["status"] in {
                    "COMPLETED",
                    "CANCELLED",
                }:
                    continue
                if node["context"].get("provider") != self.explicit_provider:
                    self.store.transition(
                        node,
                        node["status"],
                        context={**node["context"], "provider": self.explicit_provider},
                    )
            work = self.store.work(work["id"])
        elif work["context"].get("provider") in self.config.providers:
            self.provider = work["context"]["provider"]
            if isinstance(self.transport, Provider):
                self.transport.alias = self.provider
        if work["context"].get("waiting_for") == "task_limit":
            state = {k: v for k, v in work["context"].items() if k != "waiting_for"}
            work = self.store.transition(work, "READY", context=state)
        selected = {work["id"]}
        queue = [work]
        while queue:
            parent = queue.pop()
            child_ids = parent["context"].get("children", []) or (
                [parent["context"]["child"]] if parent["context"].get("child") else []
            )
            for child_id in child_ids:
                if child_id in selected:
                    raise ControlError(
                        "work_cycle",
                        "Owned work graph contains a cycle",
                        category="engine",
                    )
                child = self.store.work(child_id)
                if child["parent"] != parent["id"]:
                    raise ControlError(
                        "child_authority",
                        "Child belongs to another parent",
                        category="state",
                    )
                selected.add(child_id)
                queue.append(child)
        for identity in selected - {work["id"]}:
            node = self.store.work(identity)
            if node["status"] in {"BLOCKED", "RUNNING"} and node["failure"].get(
                "category"
            ) not in {"reconciliation", "budget", "migration"}:
                retries = [
                    o["id"]
                    for o in self.store.operations(identity)
                    if not o["consumed"]
                    and o["state"] == "FAILED"
                    and node["failure"].get("category") in {"provider", "environment"}
                ]
                node = self.store.transition(
                    node, "READY", failure={}, operations=retries
                )
                if node["context"].get("waiting_for") in {"child", "children"}:
                    self.store.transition(node, "WAITING")
        if work["status"] == "BLOCKED":
            if work["failure"].get("category") in {
                "reconciliation",
                "budget",
                "migration",
            }:
                return work
            retries = [
                o["id"]
                for o in self.store.operations(work["id"])
                if not o["consumed"]
                and o["state"] == "FAILED"
                and work["failure"].get("category") in {"provider", "environment"}
            ]
            work = self.store.transition(work, "READY", failure={}, operations=retries)
        elif work["status"] == "RUNNING":
            work = self.store.transition(work, "READY")
        if work["status"] == "READY" and work["context"].get("waiting_for") in {
            "child",
            "children",
        }:
            work = self.store.transition(work, "WAITING")
        try:
            return self.drive(work["id"])
        finally:
            cleanup.collect(self.store)

    def context(self, work):
        contract = self.store.contract(work["contract"])
        path = self.workspaces.ensure(work, contract.source)
        environment = {
            k: v
            for k, v in os.environ.items()
            if k not in {"DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"}
        }
        from ..operator_inputs import OperatorInputStore

        bindings = []
        owner = work
        while owner:
            bindings.extend(owner["context"].get("input_bindings", []))
            owner = self.store.work(owner["parent"]) if owner["parent"] else None
        supplied, _ = OperatorInputStore(self.project).environment(bindings)
        environment.update(supplied)
        alias = (
            work["context"].get("provider")
            or self.store.work(self.store.workflow(work["workflow"])["root"])[
                "context"
            ].get("provider")
            or self.provider
        )
        return ExecutionContext(
            self.project, path, work["id"], contract, alias, environment
        )

    def wait_user(self, work, questions, operation):
        from .interaction import pending_questions

        requests, bindings = pending_questions(self.project, work, questions)
        state = {
            **work["context"],
            "input_requests": requests,
            "input_bindings": [*work["context"].get("input_bindings", []), *bindings],
        }
        return self.store.transition(
            work,
            "WAITING",
            context={**state, "waiting_for": "user", "question": requests},
            operation=operation,
        )

    def effect(self, work, kind, inputs, function, *, model=False):
        ordinal = work["context"].get("ordinals", {}).get(kind, 0)
        consumed = [
            x["ordinal"]
            for x in self.store.operations(work["id"])
            if x["kind"] == kind and x["consumed"]
        ]
        ordinal = max(ordinal, 1 + max(consumed, default=-1))
        if self.offline and model:
            retained = next(
                (
                    x
                    for x in self.store.operations(work["id"])
                    if x["kind"] == kind and x["ordinal"] == ordinal
                ),
                None,
            )
            if retained is None:
                raise ControlError(
                    "offline_boundary",
                    "Reached the next external operation",
                    category="offline",
                    details={"work_id": work["id"], "phase": kind},
                )
        operation = self.store.reserve(
            work,
            kind,
            ordinal,
            inputs,
            model=model,
            provider=inputs.get("provider", self.provider) if model else "",
        )
        if not operation.get("new"):
            if operation["state"] == "FAILED":
                failure = json.loads(operation["result"])
                raise ControlError(
                    failure["code"],
                    failure["message"],
                    category=failure["category"],
                    details=failure.get("details"),
                )
            return json.loads(operation["result"]), operation["id"]
        try:
            result = function(operation["id"])
        except ControlError as error:
            self.store.settle(
                operation["id"],
                error.to_dict(),
                state="UNKNOWN" if error.category == "reconciliation" else "FAILED",
            )
            raise
        except BaseException:
            self.store.settle(
                operation["id"],
                {
                    "code": "outcome_unknown",
                    "message": "Operation interrupted",
                    "category": "reconciliation",
                },
                state=(
                    "UNKNOWN"
                    if model or kind in {"deliver", "persistence"}
                    else "FAILED"
                ),
            )
            raise
        self.store.settle(operation["id"], result)
        return result, operation["id"]

    def verify(self, ctx, specs, identity):
        return self.verifier.run(replace(ctx, operation_id=identity), specs)

    def model(
        self, work, context, phase, *, inputs=None, readonly=True, attachments=()
    ):
        from .quality import source_seal
        from .protocol import validate

        retained = [
            op
            for op in self.store.operations(work["id"])
            if op["kind"] == "model:" + phase and not op["consumed"]
        ]
        if retained:
            operation = max(retained, key=lambda op: op["ordinal"])
            if operation["state"] in {"UNKNOWN", "DISPATCHED"}:
                raise ControlError(
                    "outcome_unknown",
                    "Reconcile operation " + operation["id"],
                    category="reconciliation",
                )
            if operation["state"] == "CONFIRMED" and operation.get("inputs"):
                if operation["inputs"].get("contract") != context.contract.identity:
                    raise ControlError(
                        "operation_inputs", "Retained model contract changed"
                    )
                result = operation["result"]
                if result.get("workspace_hash") and result[
                    "workspace_hash"
                ] != source_seal(context.workspace_root):
                    raise ControlError(
                        "source_changed",
                        "Retained response workspace changed; reconcile its original candidate",
                    )
                return validate(phase, parse_reply(result["text"])), operation["id"]
        if self.offline:
            raise ControlError(
                "offline_boundary",
                "Reached the next external operation",
                category="offline",
                details={"work_id": work["id"], "phase": phase},
            )
        if isinstance(self.transport, Provider):
            from .domain import context_inputs

            inputs = {
                **context_inputs(context, phase, self.config, self.store),
                **(inputs or {}),
            }
        if work["context"].get("operator_answers"):
            inputs = {
                **(inputs or {}),
                "operator_answers": work["context"]["operator_answers"],
            }
        prompt = build(
            context, phase, inputs=inputs, feedback=work["context"].get("feedback")
        )

        def invoke(identity):
            directory = self.workspaces.root / "operations" / identity
            directory.mkdir(parents=True, exist_ok=True)
            cleanup.register(
                self.store, directory, "scratch", work["id"], references=(work["id"],)
            )
            from .quality import verify_approved, stage_writes, workspace_guard

            approved = {}
            owner = work
            while owner:
                approved.update(owner["context"].get("approvals", {}))
                owner = self.store.work(owner["parent"]) if owner["parent"] else None
            verify_approved(context.workspace_root, approved)
            before = workspace_guard(context.workspace_root)
            trace = context.workspace_root / ".auto-agents/docs/requirements_trace.json"
            previous_trace = trace.read_text() if trace.exists() else ""
            result = self.transport.run(
                context,
                phase,
                prompt,
                directory / "response.txt",
                readonly=readonly,
                attachments=attachments,
            )
            after = workspace_guard(context.workspace_root)
            if readonly and after != before:
                raise ControlError(
                    "readonly",
                    "Read-only worker changed workspace files",
                    category="model",
                )
            if not readonly:
                scope = (
                    context.contract.scope
                    if phase == "implement"
                    or context.contract.inputs.get("variant_only")
                    else ()
                )
                if phase == "acceptance":
                    scope = (
                        ".auto-agents/docs/acceptance/",
                        *[
                            name
                            for spec in context.contract.checks
                            for name in spec.outputs
                        ],
                    )
                if phase == "finalize":
                    scope = tuple((inputs or {}).get("paths", ()))
                stage_writes(before, after, phase, scope)
            if trace.exists():
                from .domain import trace_transition

                trace_transition(context, previous_trace, trace.read_text())
            verify_approved(context.workspace_root, approved)
            return {**result, "workspace_hash": source_seal(context.workspace_root)}

        raw, operation = self.effect(
            work,
            "model:" + phase,
            {
                "prompt_hash": digest(prompt),
                "contract": context.contract.identity,
                "inputs": inputs or {},
                "provider": context.provider,
            },
            invoke,
            model=True,
        )
        return validate(phase, parse_reply(raw["text"])), operation

    def advance(self, work, phase, *, context=None, result=None, operation=None):
        updated = self.store.transition(
            work,
            "READY",
            phase=phase,
            context=context or work["context"],
            failure={},
            operation=operation,
        )
        return updated

    def complete(self, work, result, *, operation=None):
        result = {**result, "contract": work["contract"]}
        updated = self.store.transition(
            work, "COMPLETED", result=result, failure={}, operation=operation
        )
        if (
            not work["parent"]
            and result.get("delivery")
            and work["mode"] in {"run", "fix", "collab"}
        ):
            from .release import enqueue

            deferred = enqueue(self, updated)
            if deferred:
                updated = self.store.transition(
                    updated,
                    "COMPLETED",
                    result={**updated["result"], "release_work_id": deferred["id"]},
                )
        cleanup.release(self.store, work["id"])
        if not self.store.work(work["id"])["context"].get("keep_workspace"):
            try:
                self.workspaces.release(work)
            except (OSError, ControlError) as error:
                self.store.event(work["id"], "cleanup_deferred", {"reason": str(error)})
        return updated

    def drive(self, identity):
        while True:
            work = self.store.work(identity)
            if work["status"] in {"COMPLETED", "CANCELLED", "BLOCKED"}:
                return work
            if work["status"] == "WAITING":
                if work["context"].get("waiting_for") == "children":
                    ids = work["context"]["children"]
                    maximum = max(1, self.parallel_workers)
                    with ThreadPoolExecutor(max_workers=maximum) as pool:
                        children = list(pool.map(self.drive, ids))
                    incomplete = [x for x in children if x["status"] != "COMPLETED"]
                    if incomplete:
                        blocked = next(
                            (x for x in incomplete if x["status"] == "BLOCKED"), None
                        )
                        return (
                            self.store.transition(
                                work,
                                "BLOCKED",
                                failure={
                                    **blocked["failure"],
                                    "child_id": blocked["id"],
                                },
                            )
                            if blocked
                            else work
                        )
                    state = {**work["context"]}
                    for child in children:
                        if child["result"].get("candidate"):
                            self.workspaces.adopt(work, child["result"]["candidate"])
                        state["done_tasks"] = [
                            *state.get("done_tasks", []),
                            child["context"]["task_id"],
                        ]
                        self.completed_tasks += 1
                    parent_ctx = self.context(work)
                    state["candidate"] = self.workspaces.candidate(
                        work, parent_ctx.workspace_root, parent_ctx.contract.source
                    )
                    state.pop("children", None)
                    state.pop("waiting_for", None)
                    work = self.store.transition(work, "READY", context=state)
                    continue
                if work["context"].get("waiting_for") != "child":
                    return work
                child = self.drive(work["context"]["child"])
                if child["status"] != "COMPLETED":
                    if child["status"] == "BLOCKED":
                        return self.store.transition(
                            work,
                            "BLOCKED",
                            failure={**child["failure"], "child_id": child["id"]},
                        )
                    return work
                context = {**work["context"]}
                context.pop("child", None)
                context.pop("waiting_for", None)
                if child["result"].get("candidate"):
                    self.workspaces.adopt(work, child["result"]["candidate"])
                parent_ctx = self.context(work)
                context["candidate"] = self.workspaces.candidate(
                    work, parent_ctx.workspace_root, parent_ctx.contract.source
                )
                context["child_results"] = [
                    *context.get("child_results", []),
                    {"id": child["id"], "result": child["result"]},
                ]
                if work["mode"] == "run":
                    context["done_tasks"] = [
                        *context.get("done_tasks", []),
                        child["context"]["task_id"],
                    ]
                    self.completed_tasks += 1
                work = self.store.transition(
                    work,
                    "READY",
                    phase=(
                        "acceptance"
                        if work["mode"] == "collab"
                        or work["mode"] == "fix"
                        and work["phase"] == "classify"
                        else work["phase"]
                    ),
                    context=context,
                )
            if work["status"] == "READY":
                work = self.store.transition(work, "RUNNING")
            context = None
            try:
                context = self.context(work)
                from .observer import milestones

                self.visited.append(
                    {
                        "step_id": work["id"] + ":" + work["phase"],
                        "work_id": work["id"],
                        "phase": work["phase"],
                        "progress_seq": len(milestones(self.store, work["workflow"])),
                    }
                )
                self.store.event(work["id"], "step_entered", self.visited[-1])
                self.print(
                    "[" + work["mode"] + "] " + work["phase"] + " (" + work["id"] + ")"
                )
                self.step(work, context)
            except ControlError as error:
                latest = self.store.work(identity)
                if (
                    error.code in {"provider_quota", "provider_unavailable"}
                    and context is not None
                    and context.provider == "codex-fuli0110"
                    and "codex" in self.config.providers
                ):
                    state = {**latest["context"], "provider": "codex"}
                    key = "model:" + latest["phase"]
                    ordinal = state.get("ordinals", {}).get(key, 0)
                    operations = [
                        x
                        for x in self.store.operations(identity)
                        if x["kind"] == key and not x["consumed"]
                    ]
                    for operation in operations:
                        if operation["state"] == "FAILED":
                            self.store.consume(operation["id"])
                    self.store.transition(latest, "READY", context=state)
                    continue
                if (
                    error.category == "verification"
                    and latest["mode"] == "fix"
                    and latest["phase"] in {"verify", "review"}
                ):
                    from .progress import observe

                    progress, allowed = observe(
                        latest["context"].get("repair_progress", {}),
                        error,
                        limit=self.config.execution.supervision.no_progress_limit,
                    )
                    if progress["best"] != latest["context"].get(
                        "repair_progress", {}
                    ).get("best") and error.details.get("checks"):
                        self.store.event(
                            work["id"], "verified_progress", {"best": progress["best"]}
                        )
                    if allowed:
                        state = {
                            **latest["context"],
                            "feedback": error.to_dict(),
                            "repair_progress": progress,
                        }
                        state.pop("disposition", None)
                        # A verified improvement never refunds an implementation call.
                        for operation in self.store.operations(identity):
                            if not operation["consumed"] and operation["state"] in {
                                "CONFIRMED",
                                "FAILED",
                            }:
                                self.store.consume(operation["id"])
                        self.store.transition(
                            latest, "READY", phase="implement", context=state
                        )
                        continue
                if error.category == "model" and error.code not in {
                    "readonly",
                    "stage_scope",
                    "worker_git",
                    "approved_contract",
                    "proof_source_changed",
                    "source_changed",
                }:
                    retries = latest["context"].get("protocol_retries", 0)
                    if retries < 2:
                        state = {
                            **latest["context"],
                            "feedback": error.to_dict(),
                            "protocol_retries": retries + 1,
                        }
                        key = "model:" + latest["phase"]
                        settled = [
                            x
                            for x in self.store.operations(identity)
                            if x["kind"] == key
                            and not x["consumed"]
                            and x["state"] in {"CONFIRMED", "FAILED"}
                        ]
                        operation = (
                            max(settled, key=lambda x: x["ordinal"])
                            if settled
                            else None
                        )
                        self.store.transition(
                            latest,
                            "READY",
                            context=state,
                            operation=operation["id"] if operation else None,
                        )
                        continue
                return self.store.transition(latest, "BLOCKED", failure=error.to_dict())
            except Exception as error:
                import traceback

                fault = ControlError(
                    "engine_exception",
                    str(error),
                    category="engine",
                    details={
                        "exception_type": type(error).__name__,
                        "traceback": traceback.format_exc(),
                    },
                )
                return self.store.transition(
                    self.store.work(identity), "BLOCKED", failure=fault.to_dict()
                )
            finally:
                cleanup.collect(self.store, seconds=0.25)

    def step(self, work, ctx):
        phase = work["phase"]
        mode = work["mode"]
        state = dict(work["context"])
        contract = ctx.contract
        if phase in RUN_PHASES[:5] or phase in {"readme", "research"}:
            if (
                phase == "prototype"
                and not state.get("frontend")
                and not contract.inputs.get("variant_only")
            ):
                return self.advance(work, "design")
            if phase == "plan":
                from ..requirements import load_requirements_trace
                from ..validation import validate_active_persistence_target_readiness

                errors = validate_active_persistence_target_readiness(
                    load_requirements_trace(ctx.workspace_root),
                    configured_targets=[
                        target.to_dict() for target in self.config.persistence.targets
                    ],
                )
                if errors:
                    raise ControlError(
                        "persistence_configuration",
                        "Register ready persistence targets before planning",
                        category="operator",
                        details={"errors": errors},
                    )
            proposal, operation = self.model(work, ctx, phase, readonly=False)
            if proposal.get("questions"):
                return self.wait_user(work, proposal["questions"], operation)
            if phase == "plan":
                tasks = proposal.get("tasks")
                checks = compile_checks(proposal.get("checks", []), self.project)
                from .release import configured_checks

                deferred = (
                    self.config.gates.release_verification_mode == "deferred"
                    and not contract.inputs.get("force_release")
                )
                required = configured_checks(
                    self.config,
                    ctx.workspace_root,
                    level="affected" if deferred else "release",
                )
                checks = tuple(dict.fromkeys([*checks, *required]))
                if not isinstance(tasks, list) or not tasks or not checks:
                    raise ControlError(
                        "model_output",
                        "Plan needs tasks and release verification",
                        category="model",
                    )
                ids = {t.get("task_id") for t in tasks}
                if None in ids or len(ids) != len(tasks):
                    raise ControlError(
                        "model_output",
                        "Task identities must be unique",
                        category="model",
                    )
                for task in tasks:
                    if any(
                        d not in ids or d == task["task_id"]
                        for d in task.get("depends_on", [])
                    ):
                        raise ControlError(
                            "model_output", "Invalid task dependency", category="model"
                        )
                    task["checks"] = [
                        x.to_dict()
                        for x in compile_checks(task.get("checks", []), self.project)
                    ]
                    if not task["checks"]:
                        raise ControlError(
                            "model_output",
                            "Every task requires behavioral checks",
                            category="model",
                        )
                    if not any(
                        spec["purpose"] == "behavior" and spec["required"]
                        for spec in task["checks"]
                    ):
                        raise ControlError(
                            "model_output",
                            "Every task requires a behavior proof",
                            category="model",
                        )
                if not any(
                    spec.purpose == "behavior" and spec.required for spec in checks
                ):
                    raise ControlError(
                        "model_output",
                        "Release needs a required behavior proof",
                        category="model",
                    )
                from .quality import validate_plan, audit_requirements

                validate_plan(tasks)
                state["requirements_audit"] = audit_requirements(
                    ctx.workspace_root,
                    tasks,
                    required=contract.inputs.get("control_version") == 2,
                )
                (ctx.workspace_root / ".auto-agents/docs/task_plan.json").write_text(
                    json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2)
                )
                state.update(tasks=tasks, done_tasks=[])
                state["plan_source"] = git(ctx.workspace_root, "rev-parse", "HEAD")
                candidate = self.workspaces.candidate(
                    work, ctx.workspace_root, contract.source
                )
                state["candidate"] = candidate
                return self.store.bind_contract(
                    work,
                    replace(contract, checks=checks),
                    next_phase="provider_research",
                    context=state,
                    operation=operation,
                )
            else:
                if phase in {"provider_research", "research"}:
                    from .domain import validate_provider_documents

                    validate_provider_documents(ctx, proposal, self.store, operation)
                self.validate_artifacts(ctx, phase, proposal)
                if phase == "prototype":
                    from .domain import validate_prototype

                    proposal = validate_prototype(
                        ctx, proposal, self.config, self.store, operation
                    )
                if phase == "clarify":
                    state["frontend"] = bool(proposal.get("frontend"))
            candidate = self.workspaces.candidate(
                work, ctx.workspace_root, contract.source, contract.scope
            )
            state["candidate"] = candidate
            if contract.inputs.get("variant_only"):
                return self.complete(
                    work,
                    {"candidate": candidate, "artifacts": proposal["artifacts"]},
                    operation=operation,
                )
            if mode == "provider_resolve":
                return self.advance(
                    work,
                    "verify" if contract.checks else "review",
                    context={
                        **state,
                        "candidate": candidate,
                        "references": proposal.get("references", []),
                    },
                    operation=operation,
                )
            next_phase = (
                RUN_PHASES[RUN_PHASES.index(phase) + 1]
                if phase != "readme"
                else "verify"
            )
            gate = {
                "clarify": "requirements",
                "prototype": "prototype",
                "design": "architecture",
            }.get(phase)
            if gate and (
                gate == "prototype"
                or gate in self.config.approvals.enabled
                and not contract.authorization.get("auto_approve")
            ):
                return self.store.transition(
                    work,
                    "WAITING",
                    phase=next_phase,
                    context={
                        **state,
                        "waiting_for": "approval",
                        "approval": gate,
                        "approval_phase": phase,
                        "approval_artifacts": proposal.get("artifacts", []),
                    },
                    operation=operation,
                )
            if gate:
                from .quality import artifact_hashes

                state["approvals"] = {
                    **state.get("approvals", {}),
                    gate: {
                        "hashes": artifact_hashes(
                            ctx.workspace_root, proposal["artifacts"]
                        ),
                        "automatic": True,
                    },
                }
            return self.advance(work, next_phase, context=state, operation=operation)
        if phase == "classify":
            proposal, operation = self.model(work, ctx, "classify")
            decision = proposal.get("decision")
            if decision == "need_user":
                return self.wait_user(work, proposal.get("question"), operation)
            if decision == "not_bug":
                return self.advance(
                    work,
                    "verify" if contract.checks else "review",
                    context={
                        **state,
                        "disposition": "not_bug",
                        "classification": proposal,
                    },
                    operation=operation,
                )
            if decision == "run_iteration":
                return self.store.child(
                    work,
                    "run",
                    proposal.get("summary") or contract.goal,
                    git(ctx.workspace_root, "rev-parse", "HEAD"),
                    inputs={"parent_issue": contract.goal},
                    operation=operation,
                )
            if decision != "fix":
                raise ControlError(
                    "model_output", "Unknown classification decision", category="model"
                )
            if not contract.scope and not proposal.get("paths"):
                raise ControlError(
                    "model_output",
                    "Classification requires a bounded source scope",
                    category="model",
                )
            if contract.checks:
                commands = [x.command for x in contract.checks]
                proposed = proposal.get("verification_command")
                if "verification_command" in proposal and (
                    not isinstance(proposed, str)
                    or proposed
                    and proposed not in commands
                ):
                    raise ControlError(
                        "model_output",
                        "Classification cannot change a bound command",
                        category="model",
                    )
                if "checks" in proposal and proposal["checks"] != [
                    x.to_dict() for x in contract.checks
                ]:
                    raise ControlError(
                        "model_output",
                        "Classification cannot change bound verification",
                        category="model",
                    )
                if not contract.scope and proposal.get("paths"):
                    return self.store.bind_contract(
                        work,
                        replace(contract, scope=tuple(proposal["paths"])),
                        next_phase="implement",
                        context={
                            **state,
                            "persistence_change": proposal.get("persistence_change"),
                        },
                        operation=operation,
                    )
            else:
                checks = compile_checks(
                    proposal.get("checks")
                    or (
                        [proposal["verification_command"]]
                        if proposal.get("verification_command")
                        else []
                    ),
                    self.project,
                )
                if not checks:
                    raise ControlError(
                        "model_output",
                        "Classification needs a verification contract",
                        category="model",
                    )
                if not any(
                    spec.purpose == "behavior" and spec.required for spec in checks
                ):
                    raise ControlError(
                        "model_output",
                        "Fix needs a required behavior proof",
                        category="model",
                    )
                return self.store.bind_contract(
                    work,
                    replace(
                        contract, checks=checks, scope=tuple(proposal.get("paths", ()))
                    ),
                    next_phase="implement",
                    context={
                        **state,
                        "persistence_change": proposal.get("persistence_change"),
                    },
                    operation=operation,
                )
            state["persistence_change"] = proposal.get("persistence_change")
            return self.advance(work, "implement", context=state, operation=operation)
        if phase == "implement" and mode == "run":
            pending = [
                t
                for t in state["tasks"]
                if t["task_id"] not in state.get("done_tasks", [])
            ]
            if not pending:
                return self.advance(work, "readme")
            if self.task_limit is not None and self.completed_tasks >= self.task_limit:
                return self.store.transition(
                    work, "WAITING", context={**state, "waiting_for": "task_limit"}
                )
            ready = [
                t
                for t in pending
                if all(
                    d in state.get("done_tasks", []) for d in t.get("depends_on", [])
                )
            ]
            if not ready:
                raise ControlError("plan_cycle", "Task graph has no executable work")
            parallel = self.config.execution.parallel_tasks
            if parallel.enabled and self.parallel_workers > 1:
                batch = []
                paths = []
                for task in ready:
                    scopes = task.get("paths", [])
                    if not scopes or any(
                        a == b
                        or a.startswith(b.rstrip("/") + "/")
                        or b.startswith(a.rstrip("/") + "/")
                        for a in scopes
                        for b in paths
                    ):
                        continue
                    batch.append(task)
                    paths.extend(scopes)
                    available = (
                        self.parallel_workers
                        if self.task_limit is None
                        else min(
                            self.parallel_workers,
                            self.task_limit - self.completed_tasks,
                        )
                    )
                    if len(batch) >= available:
                        break
                if len(batch) > 1:
                    return self.store.batch(
                        work, batch, git(ctx.workspace_root, "rev-parse", "HEAD")
                    )
            task = ready[0]
            child = self.store.child(
                work,
                "fix",
                task["goal"],
                git(ctx.workspace_root, "rev-parse", "HEAD"),
                inputs={
                    "planned_task": task["task_id"],
                    "persistence_change": task.get("persistence_change"),
                },
                scope=task.get("paths", ()),
                checks=compile_checks(task["checks"], self.project),
            )
            child = self.store.transition(
                child,
                "RUNNING",
                phase=(
                    "classify" if task.get("classification_required") else "implement"
                ),
                context={"task_id": task["task_id"]},
            )
            self.store.transition(child, "READY")
            return child
        if phase == "implement":
            proposal, operation = self.model(work, ctx, "implement", readonly=False)
            candidate = self.workspaces.candidate(
                work, ctx.workspace_root, contract.source, contract.scope
            )
            if not candidate["paths"]:
                raise ControlError(
                    "no_progress",
                    "Implementation produced no change",
                    category="verification",
                )
            from .quality import validate_persistence

            change = (
                contract.inputs.get("persistence_change")
                or state.get("persistence_change")
                or proposal.get("persistence_change")
            )
            manifest = validate_persistence(ctx, candidate, change)
            return self.advance(
                work,
                "persistence" if manifest else "verify",
                context={
                    **state,
                    "candidate": candidate,
                    "implementation": proposal,
                    "persistence_manifest": manifest,
                },
                operation=operation,
            )
        if phase == "persistence":
            manifest = state["persistence_manifest"]
            change = manifest["change"]
            from ..persistence import (
                build_persistence_action_manifest,
                execute_persistence_action,
                persistence_storage_transition,
            )

            # Targets resolve inside the owned checkout. Production is always
            # generate-only; configured user databases are never mounted here.
            action = build_persistence_action_manifest(
                ctx.workspace_root,
                change,
                self.config.persistence,
                candidate_fingerprint=manifest["candidate"],
            )
            if (
                persistence_storage_transition(change) == "rebuild"
                and state.get("persistence_approval") != manifest["fingerprint"]
            ):
                return self.store.transition(
                    work,
                    "WAITING",
                    context={
                        **state,
                        "waiting_for": "approval",
                        "approval": "persistence-reset",
                        "approval_phase": "persistence",
                        "approval_fingerprint": manifest["fingerprint"],
                        "persistence_action": action,
                    },
                )
            outcome, operation = self.effect(
                work,
                "persistence",
                {"manifest": manifest},
                lambda _: execute_persistence_action(
                    ctx.workspace_root, change, self.config.persistence
                ),
            )
            return self.advance(
                work,
                "verify",
                context={**state, "persistence_outcome": outcome},
                operation=operation,
            )
        if phase == "verify":
            report, operation = self.effect(
                work,
                "verify",
                {
                    "contract": contract.identity,
                    "revision": git(ctx.workspace_root, "rev-parse", "HEAD"),
                },
                lambda identity: self.verify(
                    ctx,
                    tuple(
                        spec for spec in contract.checks if spec.purpose != "artifact"
                    ),
                    identity,
                ),
            )
            if contract.inputs.get("release_only"):
                if any(s.purpose == "artifact" for s in contract.checks):
                    return self.advance(
                        work,
                        "artifact_verify",
                        context={**state, "verification": report},
                        operation=operation,
                    )
                return self.complete(
                    work,
                    {
                        "verification": report,
                        "verified_revision": self.store.meta(
                            "source:" + contract.source, {}
                        ).get("head"),
                    },
                    operation=operation,
                )
            return self.advance(
                work,
                (
                    ("visual_judge" if state.get("frontend") else "artifact_verify")
                    if mode == "run"
                    else "review"
                ),
                context={**state, "verification": report},
                operation=operation,
            )
        if phase == "finalize":
            from .quality import source_seal, artifact_hashes

            outputs = tuple({name for spec in contract.checks for name in spec.outputs})
            if (
                source_seal(ctx.workspace_root, outputs)
                != state["verification"]["source_hash"]
            ):
                raise ControlError(
                    "proof_source_changed",
                    "Verified source changed before evidence finalization",
                    category="verification",
                )
            immutable = {
                name: hash_
                for name, hash_ in state["verification"]["outputs"].items()
                if name not in state["finalize_paths"]
            }
            proposal, operation = self.model(
                work,
                ctx,
                "finalize",
                readonly=False,
                inputs={
                    "paths": state["finalize_paths"],
                    "verification": state["verification"],
                    "review": state["finalize_review"],
                },
                attachments=[
                    ctx.workspace_root / name
                    for name in immutable
                    if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                ],
            )
            if (
                source_seal(ctx.workspace_root, outputs)
                != state["verification"]["source_hash"]
                or artifact_hashes(ctx.workspace_root, list(immutable)) != immutable
            ):
                raise ControlError(
                    "proof_source_changed",
                    "Evidence finalizer changed verified source or captured evidence",
                    category="model",
                )
            candidate = self.workspaces.candidate(
                work, ctx.workspace_root, contract.source, contract.scope
            )
            return self.advance(
                work,
                state.get("finalize_return", "review"),
                context={
                    **state,
                    "candidate": candidate,
                    "evidence_finalization": proposal,
                },
                operation=operation,
            )
        if phase in {"review", "visual_judge", "acceptance_review"}:
            proposal, operation = self.model(
                work,
                ctx,
                phase,
                inputs={
                    "verification": state.get("verification"),
                    "candidate": state.get("candidate"),
                    "acceptance": state.get("acceptance"),
                    "classification": state.get("classification"),
                },
                attachments=[
                    ctx.workspace_root / name
                    for name in (state.get("verification") or {}).get("outputs", {})
                    if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                ],
            )
            if phase == "review" and proposal.get("decision") == "finalize":
                outputs = {name for spec in contract.checks for name in spec.outputs}
                paths = proposal.get("evidence_paths", [])
                if not paths or any(
                    name not in outputs
                    or Path(name).suffix.lower() not in {".json", ".md"}
                    for name in paths
                ):
                    raise ControlError(
                        "evidence_contract",
                        "Finalization may modify only declared output reports",
                        category="model",
                    )
                return self.advance(
                    work,
                    "finalize",
                    context={
                        **state,
                        "finalize_paths": paths,
                        "finalize_review": proposal,
                    },
                    operation=operation,
                )
            from .quality import verify_review

            verify_review(
                proposal, ctx, state.get("candidate") if phase == "review" else None
            )
            if state.get("disposition") == "not_bug":
                return self.complete(
                    work,
                    {
                        "resolution": "not_bug",
                        "summary": state["classification"].get("summary", ""),
                        "review": proposal,
                        "verification": state.get("verification"),
                    },
                    operation=operation,
                )
            if phase == "visual_judge":
                if not proposal.get("evidence_refs"):
                    raise ControlError(
                        "visual_evidence",
                        "Visual review requires actual evidence",
                        category="verification",
                    )
                self.validate_evidence(ctx, proposal["evidence_refs"])
                return self.advance(
                    work,
                    "artifact_verify",
                    context={**state, "visual_review": proposal},
                    operation=operation,
                )
            if phase == "acceptance_review":
                return self.advance(
                    work,
                    "deliver",
                    context={**state, "acceptance_review": proposal},
                    operation=operation,
                )
            return self.advance(
                work,
                "artifact_verify",
                context={**state, "review": proposal},
                operation=operation,
            )
        if phase == "artifact_verify":
            specs = tuple(
                spec for spec in contract.checks if spec.purpose == "artifact"
            )
            if not specs:
                return self.advance(work, "deliver")
            from .quality import source_seal

            outputs = tuple({name for spec in contract.checks for name in spec.outputs})
            if (
                source_seal(ctx.workspace_root, outputs)
                != state["verification"]["source_hash"]
            ):
                raise ControlError(
                    "proof_source_changed",
                    "Source changed after verification",
                    category="verification",
                )
            try:
                report, operation = self.effect(
                    work,
                    "artifact_verify",
                    {
                        "contract": contract.identity,
                        "outputs": state["verification"]["outputs"],
                    },
                    lambda identity: self.verify(ctx, specs, identity),
                )
            except ControlError as error:
                paths = [
                    name
                    for name in outputs
                    if Path(name).suffix.lower() in {".json", ".md"}
                ]
                if (
                    error.category != "verification"
                    or not paths
                    or state.get("artifact_corrections", 0) >= 2
                ):
                    raise
                settled = [
                    o
                    for o in self.store.operations(work["id"])
                    if o["kind"] == "artifact_verify"
                    and not o["consumed"]
                    and o["state"] == "FAILED"
                ]
                return self.advance(
                    work,
                    "finalize",
                    context={
                        **state,
                        "finalize_paths": paths,
                        "finalize_review": state.get("visual_review")
                        or state.get("review"),
                        "finalize_return": "artifact_verify",
                        "artifact_corrections": state.get("artifact_corrections", 0)
                        + 1,
                        "feedback": error.to_dict(),
                    },
                    operation=settled[-1]["id"] if settled else None,
                )
            return self.advance(
                work,
                "deliver",
                context={**state, "artifact_verification": report},
                operation=operation,
            )
        if phase == "diagnose":
            proposal, operation = self.model(
                work,
                ctx,
                "diagnose",
                inputs={"child_results": state.get("child_results", [])},
            )
            action = proposal.get("action")
            if action == "need_user":
                return self.wait_user(work, proposal.get("question"), operation)
            if action == "resume":
                child = self.store.work(proposal.get("child_id", ""))
                if child["parent"] != work["id"] or child["status"] != "BLOCKED":
                    raise ControlError(
                        "model_output",
                        "Only this parent's blocked child can resume",
                        category="model",
                    )
                if child["failure"].get("category") in {
                    "budget",
                    "reconciliation",
                    "migration",
                }:
                    raise ControlError(
                        "child_blocked", "Child requires operator reconciliation"
                    )
                self.store.transition(child, "READY", failure={})
                return self.store.transition(
                    work,
                    "WAITING",
                    context={**state, "child": child["id"], "waiting_for": "child"},
                    operation=operation,
                )
            if action in {"fix", "run"}:
                if "target_repository" in proposal:
                    raise ControlError(
                        "model_output",
                        "Foreign repository route is not authorized",
                        category="model",
                    )
                issue = proposal.get("issue", {})
                goal = issue.get("summary") if action == "fix" else proposal.get("spec")
                if not isinstance(goal, str) or not goal:
                    raise ControlError(
                        "model_output",
                        "Delegated work needs a concrete goal",
                        category="model",
                    )
                child = self.store.child(
                    work,
                    action,
                    goal,
                    git(ctx.workspace_root, "rev-parse", "HEAD"),
                    inputs={"parent_goal": contract.goal, "issue": issue},
                    checks=contract.checks if action == "fix" else (),
                    scope=contract.scope if action == "fix" else (),
                    operation=operation,
                )
                if action == "fix" and contract.checks:
                    child = self.store.transition(child, "RUNNING", phase="implement")
                    self.store.transition(child, "READY")
                return child
            if action != "acceptance":
                raise ControlError(
                    "model_output", "Unknown diagnostic action", category="model"
                )
            return self.advance(work, "acceptance", operation=operation)
        if phase == "acceptance":
            proposal, operation = self.model(work, ctx, "acceptance", readonly=False)
            if not proposal.get("accepted") or not proposal.get("evidence_refs"):
                raise ControlError(
                    "acceptance",
                    "Original goal acceptance is incomplete",
                    category="verification",
                )
            self.validate_evidence(ctx, proposal["evidence_refs"])
            proposed = compile_checks(proposal.get("checks", []), self.project)
            if contract.checks and proposed and proposed != contract.checks:
                raise ControlError(
                    "model_output",
                    "Acceptance cannot replace the original bound checks",
                    category="model",
                )
            checks = contract.checks or proposed
            if not checks:
                raise ControlError(
                    "acceptance",
                    "Acceptance requires executable behavior proof",
                    category="verification",
                )
            report, verify_op = self.effect(
                work,
                "acceptance_verify",
                {"checks": [s.to_dict() for s in checks]},
                lambda identity: self.verify(ctx, checks, identity),
            )
            self.store.consume(verify_op)
            return self.advance(
                work,
                "acceptance_review",
                context={**state, "acceptance": proposal, "verification": report},
                operation=operation,
            )
        if phase == "deliver":
            if contract.inputs.get("release_only"):
                return self.complete(
                    work,
                    {
                        "verification": state["verification"],
                        "artifact_verification": state.get("artifact_verification"),
                        "verified_revision": self.store.meta(
                            "source:" + contract.source, {}
                        ).get("head"),
                    },
                )
            if mode == "run" and not state.get("verification"):
                raise ControlError("completion", "Release verification is missing")
            if mode == "collab" and not state.get("acceptance_review"):
                raise ControlError("completion", "Semantic acceptance is missing")
            if (
                contract.checks
                and any(
                    spec.purpose == "artifact" and spec.required
                    for spec in contract.checks
                )
                and not state.get("artifact_verification")
            ):
                raise ControlError(
                    "completion", "Required artifact verification is missing"
                )
            if (
                mode == "run"
                and not work["parent"]
                and "release" in self.config.approvals.enabled
                and not contract.authorization.get("auto_approve")
                and not state.get("approvals", {}).get("release")
            ):
                return self.store.transition(
                    work,
                    "WAITING",
                    context={
                        **state,
                        "waiting_for": "approval",
                        "approval": "release",
                        "approval_phase": "verify",
                        "approval_artifacts": list(
                            state.get("verification", {}).get("outputs", {})
                        ),
                    },
                )
            candidate = self.workspaces.candidate(
                work, ctx.workspace_root, contract.source, contract.scope
            )
            proof = {
                key: state.get(key)
                for key in (
                    "verification",
                    "review",
                    "visual_review",
                    "artifact_verification",
                    "acceptance",
                    "acceptance_review",
                )
                if state.get(key) is not None
            }
            if work["parent"]:
                return self.complete(work, {"candidate": candidate, **proof})
            result, operation = self.effect(
                work,
                "deliver",
                {"candidate": candidate},
                lambda identity: self.workspaces.deliver(
                    work, candidate, operation=identity
                ),
            )
            return self.complete(
                work, {"delivery": result, **proof}, operation=operation
            )
        raise ControlError("phase", "Unknown phase: " + phase, category="engine")

    def validate_evidence(self, ctx, paths):
        for name in paths:
            if not isinstance(name, str) or not product_path(name):
                raise ControlError(
                    "evidence",
                    "Evidence path leaves the workspace",
                    category="verification",
                )
            from .quality import safe_file

            path = safe_file(ctx.workspace_root, name)
            if not path.is_file() or not path.stat().st_size:
                raise ControlError(
                    "evidence",
                    "Missing or empty evidence: " + name,
                    category="verification",
                )

    def validate_artifacts(self, ctx, phase, proposal):
        if proposal.get("not_required"):
            if phase not in {"provider_research"} or not proposal.get("reason"):
                raise ControlError(
                    "model_output", "Required stage cannot be skipped", category="model"
                )
            return
        if phase in {"provider_research", "research"}:
            from .quality import validate_references

            validate_references(proposal)
        paths = proposal.get("artifacts", [])
        if not isinstance(paths, list) or not paths:
            raise ControlError(
                "model_output", "Stage needs actual artifacts", category="model"
            )
        self.validate_evidence(ctx, paths)
        required = {
            "clarify": [
                ".auto-agents/docs/project_brief.md",
                ".auto-agents/docs/requirements.md",
            ],
            "prototype": [".auto-agents/docs/frontend_prototype/home.html"],
            "design": [".auto-agents/docs/architecture.md"],
            "readme": ["README.md"],
        }.get(phase, [])
        if phase == "prototype" and ctx.contract.inputs.get("variant_only"):
            required = [
                ".auto-agents/docs/frontend_prototype_variants/"
                + ctx.contract.inputs["variant_only"]
                + "/home.html"
            ]
        if any(name not in paths for name in required):
            raise ControlError(
                "model_output",
                "Missing required stage artifact",
                category="model",
                details={"required": required},
            )
