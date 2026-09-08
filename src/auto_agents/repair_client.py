"""Foreground registration and the thin waiting relay for managed repair."""
from __future__ import annotations

from dataclasses import asdict
import functools
import json
import os
from pathlib import Path
import sys
import time
import signal

from .repair_control import configure, digest, ensure_supervisor, git, rpc, start_ticks


class EngineRepairRequired(RuntimeError):
    def __init__(self, payload):
        self.route_payload = payload
        super().__init__("auto_agents engine self-repair required for explicitly bound engine route: " + json.dumps(payload, ensure_ascii=False))


def engine_route(orchestrator, payload):
    """Return True only for a verified receipt; otherwise request engine repair."""
    from .execution_binding import repository_binding_error, route_sources
    registration = getattr(orchestrator, "_repair_registration", None)
    if registration and (not any(source.get("target_repository") for source in route_sources(payload))
                         or repository_binding_error(Path(registration["config"]["source_root"]), payload)):
        return False
    probe = os.environ.get("AUTO_AGENTS_REPAIR_ROUTE_PROBE")
    if probe:
        approved = json.loads(Path(probe).read_text())
        if digest(payload) == approved.get("route_digest"):
            orchestrator._repair_route_probe_consumed = approved["route_digest"]
            return True
    if not registration or not enabled():
        return False
    route_key = digest(payload)
    if route_key in getattr(orchestrator, "_unavailable_engine_routes", set()):
        return False
    subscriber = os.environ.get("AUTO_AGENTS_REPAIR_SUBSCRIBER")
    if subscriber:
        try:
            response = rpc(registration["config"], {"op": "consume-route", "subscriber": subscriber,
                            "route_digest": route_key, "pid": os.getpid()})
        except (OSError, RuntimeError, ValueError) as error:
            # This coordinator cannot recover a lost execution channel by
            # repeating the same product-bound child or provider request.
            unavailable = set(getattr(orchestrator, "_unavailable_engine_routes", set()))
            unavailable.add(route_key)
            orchestrator._unavailable_engine_routes = unavailable
            orchestrator._repair_control_error = str(error)
            return False
        if response.get("accepted"):
            return True
    raise EngineRepairRequired(payload)


def enabled():
    return os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_DISABLED", "").lower() not in {"1", "true"} and not os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_WORKER")


def triage_engine_request(orchestrator, project, error):
    """Admission is not a root-cause verdict or permission to accept a patch."""
    if not isinstance(error, EngineRepairRequired):
        return None
    from .authorization import authorization_policy_for_state
    from .config import load_session_state
    from .execution_binding import repository_binding_error, route_sources
    from .self_repair import SelfRepairDecision, SelfRepairTriageResult
    registration = getattr(orchestrator, "_repair_registration", None)
    invocation = getattr(orchestrator, "_invocation_context", {}) or {}
    reason = ""
    has_target = any(source.get("target_repository") for source in route_sources(error.route_payload))
    if not enabled() or not registration:
        reason = "independent repair control is unavailable"
    elif (not has_target
          or repository_binding_error(Path(registration["config"]["source_root"]), error.route_payload)):
        reason = "engine request does not target the registered engine repository"
    # Authorization comes from the invocation/saved session, never the model's
    # route payload. Receiving a control signal does not grant new authority.
    policy_payload = {}
    if invocation.get("session_id"):
        state = load_session_state(project, invocation["session_id"])
        policy_payload = state.authorization_policy
    policy = authorization_policy_for_state(
        auto_approve=bool(invocation.get("auto_approve")), payload=policy_payload)
    if policy.decide("engine_self_repair") != "AUTO_EXECUTE":
        reason = reason or "engine self-repair requires workflow authorization"
    return SelfRepairTriageResult(
        decision=SelfRepairDecision(not reason, category="explicit_engine_request",
            reason=reason or "authorized engine work request; upstream behavior and recovery still require proof",
            fingerprint=digest(error.route_payload)),
        source="engine_request", reason=reason or "submit to supervisor without terminal-error adjudication")


def symptom_key(error, project):
    from .execution_recovery import redact_incident_text
    return digest(redact_incident_text(str(error)).replace(str(Path(project).resolve()), "<project>"))


def cached_contract(orchestrator, project, error):
    registration = getattr(orchestrator, "_repair_registration", None)
    if not registration or not enabled() or os.environ.get("AUTO_AGENTS_REPAIR_SUBSCRIBER"):
        return None
    from .self_repair import auto_agents_repo_root
    try:
        response = rpc(registration["config"], {"op": "lookup-contract",
            "symptom_key": symptom_key(error, project), "base": git(auto_agents_repo_root(), "rev-parse", "HEAD")})
        cached = response.get("contract")
        if cached:
            # A cached generic contract is a hypothesis, not a new diagnosis of
            # this project. Both behavior and boundary are re-proved by worker.
            return json.loads(json.dumps(cached).replace(cached["project"], str(Path(project).resolve())))
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def register(lock, args, orchestrator):
    config = getattr(orchestrator, "config", None)
    execution = getattr(config, "execution", None)
    autonomy = getattr(execution, "autonomy", None)
    if (not enabled() or autonomy is None or getattr(args, "autonomy", None) == "off"
            or autonomy.mode == "off"):
        return None
    from .self_repair import auto_agents_repo_root
    try:
        configured = os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_CONFIG")
        config = json.loads(Path(configured).read_text()) if configured else configure(auto_agents_repo_root())
        ensure_supervisor(config)
        payload = {"project": str(lock.project_root), "token": lock.run_token,
                   "pid": os.getpid(), "ticks": start_ticks(os.getpid()), "command": args.command,
                   "cwd": str(Path.cwd().resolve())}
        response = rpc(config, {"op": "register", "payload": payload, "environment": dict(os.environ)}, [lock.fileno])
        registration = {"config": config, "subscriber": response["subscriber"]}
        lock.repair_registration = registration
        orchestrator._repair_registration = registration
        return registration
    except (OSError, RuntimeError, ValueError) as error:
        # Normal project execution is still possible; an actual repair request
        # will fail closed rather than falling into recursive local repair.
        orchestrator._repair_control_error = str(error)
        return None


def release(lock):
    registration = getattr(lock, "repair_registration", None)
    if registration:
        try:
            rpc(registration["config"], {"op": "finish", "subscriber": registration["subscriber"]})
        except (OSError, RuntimeError):
            pass


def cancel(lock):
    registration = getattr(lock, "repair_registration", None)
    if registration:
        rpc(registration["config"], {"op": "cancel", "project": str(lock.project_root)})


def submit_and_wait(project, orchestrator, error, decision, args, lock, diagnosis=None, repair_case=None):
    from .cli import _run_command_for_self_repair_resume
    from .self_repair import auto_agents_repo_root
    from .process_supervision import ACTIVE_PROCESSES, process_group_exists
    from .execution_recovery import redact_incident_text
    registration = getattr(orchestrator, "_repair_registration", None) or register(lock, args, orchestrator)
    if not registration:
        raise RuntimeError("repair control unavailable: " + getattr(orchestrator, "_repair_control_error", "registration failed"))
    ACTIVE_PROCESSES.terminate_all()
    if any(process_group_exists(record.pgid) for record in ACTIVE_PROCESSES.snapshot()):
        raise RuntimeError("managed process cleanup incomplete; repair cannot take ownership")
    health = getattr(orchestrator, "_workflow_health_runtime", None)
    if health is not None:
        health.set_phase("self_repair")
        health.set_active_operation("self_repair", "independent repair supervisor owns recovery")
    invocation = dict(getattr(orchestrator, "_invocation_context", {}) or {})
    argv = _run_command_for_self_repair_resume(args)
    if "--session" in argv:
        invocation["session_id"] = argv[argv.index("--session") + 1]
    if args.command == "resume":
        from .workflow_chain import WorkflowStore
        workflows = WorkflowStore(project)
        workflow = workflows.load(args.workflow) if args.workflow else workflows.active()
        if workflow and workflow.root.kind in {"fix", "collab"}:
            invocation.update(session_id=workflow.root.native_id, workflow_id=workflow.workflow_id,
                              command=workflow.root.kind)
    if isinstance(error, EngineRepairRequired):
        invocation["engine_route"] = error.route_payload
    from .config import load_session_state, load_run_state
    boundary = {"kind": "completion"}
    if invocation.get("session_id"):
        root_state = load_session_state(project, invocation["session_id"])
        invocation.setdefault("workflow_id", root_state.workflow_id)
        session = root_state
        if root_state.active_handoff_id:
            from .workflow_chain import WorkflowStore
            handoff = WorkflowStore(project).load_handoff(root_state.active_handoff_id)
            if handoff.child and handoff.child.kind == "fix":
                session = load_session_state(project, handoff.child.native_id)
        if session.fix_verify_command:
            boundary = {"kind": "gate", "command": session.fix_verify_command}
    else:
        state = load_run_state(project)
        invocation["run_id"] = state.run_id
        boundary = {"kind": "run_stage", "stage": state.current_stage, "fingerprint": decision.fingerprint}
        orchestrator.record_run_blocker(owner="auto_agents", category=decision.category,
            reason=decision.reason, fingerprint=decision.fingerprint)
    if invocation.get("engine_route"):
        boundary = {"kind": "engine_route", "route_digest": digest(invocation["engine_route"])}
    source = auto_agents_repo_root()
    payload = {"project": str(Path(project).resolve()), "base": git(source, "rev-parse", "HEAD"),
               "symptom_key": symptom_key(error, project),
               "fingerprint": decision.fingerprint, "error": redact_incident_text(str(error)),
               "contract": (diagnosis.final.to_dict() if diagnosis else
                            {"engine_request": invocation["engine_route"]} if invocation.get("engine_route") else {}),
               "decision": asdict(decision), "diagnosis": (diagnosis.to_dict() if diagnosis else
                   {"kind": "engine_request_contract_required", "repair_approved": False}
                   if invocation.get("engine_route") else None),
               "repair_case": repair_case.to_dict() if repair_case else None,
               "invocation": invocation, "boundary": boundary,
               "environment": digest([sys.version, sys.executable, orchestrator.config.execution.autonomy.to_dict()]),
               "provider": getattr(args, "provider", None),
               "autonomy": getattr(args, "autonomy", None) or orchestrator.config.execution.autonomy.mode,
               "resume_argv": argv}
    # The request marker deliberately is not a valid RootCauseDiagnosis. An
    # old immutable worker that cannot fetch a newer runtime must fail closed,
    # rather than treating diagnosis=None as permission for legacy repair.
    response = rpc(registration["config"], {"op": "submit", "subscriber": registration["subscriber"], "payload": payload})
    job = response["job"]
    last = ""
    previous_term = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        from .process_supervision import RunInterruptedError
        raise RunInterruptedError(signum)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        while True:
            try:
                response = rpc(registration["config"], {"op": "status", "subscriber": registration["subscriber"]})
            except (OSError, RuntimeError):
                ensure_supervisor(registration["config"])
                registration = register(lock, args, orchestrator)
                if not registration:
                    raise RuntimeError("cannot reattach to repair control")
                continue
            if registration["subscriber"] not in response.get("registered", []):
                registration = register(lock, args, orchestrator)
                if not registration:
                    raise RuntimeError("cannot restore repair ownership")
                continue
            status = response["job"]["state"]
            job = response["job"]["id"]
            subscriber = next(item for item in response["subscribers"] if item["id"] == registration["subscriber"])
            message = status + "/" + subscriber["state"]
            if message != last:
                print(f"Self-repair {job}: {message}; logs: {registration['config']['root']}/jobs/{job}", file=sys.stderr)
                last = message
            if subscriber["state"] == "finished":
                return 0
            if status in {"blocked", "cancelled"} or subscriber["state"] in {"blocked", "cancelled"}:
                detail = response["job"].get("result", {}).get("error", "")
                if detail:
                    print(f"Self-repair stopped: {detail}", file=sys.stderr)
                return 3
            time.sleep(1)
    except BaseException:
        cancel(lock)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)


def boundary_event(kind, **details):
    configured = os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_CONFIG")
    subscriber = os.environ.get("AUTO_AGENTS_REPAIR_SUBSCRIBER")
    if not configured or not subscriber or not enabled():
        return
    from .self_repair import auto_agents_repo_root
    try:
        rpc(json.loads(Path(configured).read_text()), {"op": "boundary", "subscriber": subscriber,
            "kind": kind, "details": details, "pid": os.getpid(), "runtime": str(auto_agents_repo_root())})
    except (OSError, RuntimeError, ValueError):
        pass


def gate_boundary(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        result = function(*args, **kwargs)
        if os.environ.get("AUTO_AGENTS_REPAIR_SUBSCRIBER") and enabled() and result.ok:
            for command in result.commands:
                if command.ok and not command.cached:
                    boundary_event("gate", command=command.command)
        return result
    return wrapped
