"""One read-only rule for selecting existing work or starting a new root."""

from .types import ControlError

NEW_SESSION_COMMANDS = {"fix", "collab", "provider-resolve", "provider-research"}


def selected_work(store, args):
    selectors = [
        getattr(args, name, None) for name in ("session", "workflow", "run")
    ]
    selectors = [value for value in selectors if value]
    if selectors:
        if not store.path.exists():
            raise ControlError("unknown_work", "Unknown work item: " + selectors[0])
        selected = [store.work(value) for value in selectors]
        if len({work["id"] for work in selected}) != 1:
            raise ControlError("selection", "Selectors identify different work items")
        return selected[0]
    if args.command in NEW_SESSION_COMMANDS or not store.path.exists():
        return None
    roots = [
        work for work in store.works()
        if not work["parent"] and work["status"] not in {"COMPLETED", "CANCELLED"}
    ]
    if args.command == "run":
        roots = [
            work for work in roots
            if work["mode"] == "run"
            and not store.contract(work["contract"]).inputs.get("release_only")
            and not store.contract(work["contract"]).inputs.get("variant_only")
        ]
    if len(roots) == 1:
        return roots[0]
    if len(roots) > 1 and args.command in {
        "run", "resume", "approve", "reject", "answer", "cancel"
    }:
        raise ControlError("selection", "Choose --session or --workflow explicitly")
    return None


def request(project, argv):
    from .cli import parser, EXECUTION
    from .store import Store

    args = parser().parse_args(argv)
    if args.command not in EXECUTION:
        raise ControlError("selection", "An execution command is required")
    from pathlib import Path

    if Path(args.project).expanduser().resolve() != Path(project).resolve():
        raise ControlError("selection", "Invocation belongs to another project")
    store = Store(project, readonly=True)
    selected = selected_work(store, args)
    if args.command == "resume" and not selected:
        raise ControlError("selection", "No unique resumable workflow")
    if selected and args.command != "resume":
        mode = args.command.replace("-", "_")
        if mode == "provider_research":
            mode = "provider_resolve"
        if selected["mode"] != mode:
            raise ControlError("selection", "Requested mode differs from retained work item")
    restart = getattr(args, "restart_blocked", False)
    if restart and selected and selected["status"] != "BLOCKED":
        raise ControlError("restart", "Only a blocked run can be restarted")
    return {
        "ok": True, "schema": 2, "project": str(store.project),
        "intent": "resume" if selected and not restart else "start",
        "root_id": store.workflow(selected["workflow"])["root"] if selected and not restart else None,
        "work_id": selected["id"] if selected and not restart else None,
    }
