"""Operator document migrations use the same work and operation ledger."""

from .store import Store
from .engine import Engine
from .types import ControlError, digest
from .projection import project_work
from ..config import load_project_config
from ..persistence_rebind import rebind_legacy_persistence_decision, _replace_json_batch
from ..persistence_upgrade import upgrade_persistence_contract, parse_decision_policies


def execute(project, args):
    store = Store(project)
    roots = [
        w
        for w in store.works()
        if w["mode"] == "run"
        and not w["parent"]
        and w["status"] not in {"COMPLETED", "CANCELLED"}
        and not store.contract(w["contract"]).inputs.get("release_only")
    ]
    if len(roots) != 1:
        raise ControlError("selection", "Persistence migration requires one active run")
    work = roots[0]
    if (
        work["context"].get("children")
        or work["context"].get("child")
        or any(
            o["state"] in {"UNKNOWN", "DISPATCHED"}
            and store.work(o["work"])["workflow"] == work["workflow"]
            for o in store.operations()
        )
    ):
        raise ControlError(
            "persistence",
            "Settle active child work and unknown operations before changing persistence documents",
        )
    config = load_project_config(project)
    engine = Engine(project, store, config)
    context = engine.context(work)

    def publish(documents, state):
        nonlocal work
        if work["context"].get("done_tasks") and state.get("tasks") != project_work(
            store, work
        ).get("tasks"):
            raise ControlError(
                "persistence", "Completed task contracts cannot be rewritten"
            )
        previous_status = work["status"]
        if previous_status != "RUNNING":
            if previous_status != "READY":
                work = store.transition(work, "READY")
            work = store.transition(work, "RUNNING")
        # Explicit operator migrations are local nonrepeatable effects. A
        # crash retains UNKNOWN rather than inventing another recovery path.
        try:
            result, operation = engine.effect(
                work,
                "persistence",
                {
                    "command": args.command,
                    "documents": {
                        str(p.relative_to(context.workspace_root)): value
                        for p, value in documents.items()
                    },
                },
                lambda operation_id: write(documents, state),
            )
        except BaseException:
            latest = store.work(work["id"])
            store.transition(latest, previous_status)
            raise
        latest = store.work(work["id"])
        updated = dict(latest["context"])
        if updated.get("done_tasks") and state.get("tasks") != project_work(
            store, latest
        ).get("tasks"):
            raise ControlError(
                "persistence", "Completed task contracts cannot be rewritten"
            )
        if "tasks" in updated:
            updated["tasks"] = state.get("tasks", updated["tasks"])
        updated["persistence_migration"] = result
        # A document edit requires fresh review; never retain approval of
        # bytes that no longer exist. It does not replenish calls or limits.
        updated.pop("approvals", None)
        store.transition(latest, previous_status, context=updated, operation=operation)

    def write(documents, state):
        if work["context"].get("done_tasks") and state.get("tasks") != project_work(
            store, work
        ).get("tasks"):
            raise ControlError(
                "persistence", "Completed task contracts cannot be rewritten"
            )
        _replace_json_batch(documents)
        return {
            "documents_hash": digest(
                {
                    str(p.relative_to(context.workspace_root)): v
                    for p, v in documents.items()
                }
            ),
            "command": args.command,
        }

    common = dict(
        state_payload=project_work(store, work), publish=publish, project_config=config
    )
    if args.command == "persistence-rebind":
        result = rebind_legacy_persistence_decision(
            context.workspace_root,
            decision_id=args.decision,
            target_ids=args.target,
            **common,
        )
    else:
        result = upgrade_persistence_contract(
            context.workspace_root,
            decision_policies=parse_decision_policies(args.decision_policy),
            resume_interrupted=bool(args.resume_interrupted),
            **common,
        )
    return result
