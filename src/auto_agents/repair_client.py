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


def _report_repair_progress(project, message):
    from .reporting import find_reporter
    from .repair_environment_log import sanitize
    message = sanitize(message)
    reporter = find_reporter(project)
    if reporter is not None:
        reporter.event("repair.control", {}, audience="user", message=message)
    else:
        print(message, file=sys.stderr)


def _repair_text(value, limit=80):
    from .repair_environment_log import sanitize
    # Redact before flattening/truncating, including multiline authorization headers.
    text = " ".join(sanitize(value).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _repair_problem(payload):
    route = payload.get("invocation", {}).get("engine_route") or {}
    if route:
        for seed in (route.get("issue_seed"), route.get("spec_seed"), route):
            if not isinstance(seed, dict):
                continue
            for key in ("summary", "title", "scope", "required_behavior", "requirements"):
                value = seed.get(key)
                if isinstance(value, list):
                    value = "；".join(item for item in value if isinstance(item, str))
                if isinstance(value, str) and value.strip():
                    return "正在修复", _repair_text(value)
        return "正在修复", "处理引擎修复请求（请求未提供问题说明）"
    diagnosis = payload.get("diagnosis") or {}
    if diagnosis.get("repair_approved"):
        chain = (diagnosis.get("final") or {}).get("causal_chain", [])
        causes = [item for item in chain if isinstance(item, str) and item.strip()]
        if causes:
            return "正在修复", _repair_text("；".join(causes[:2]))
    symptom = (payload.get("repair_case") or {}).get("symptom") or payload.get("error")
    if isinstance(symptom, str) and symptom.lstrip().startswith("Traceback (most recent call last):"):
        symptom = symptom.rstrip().splitlines()[-1]
    return "正在排查", _repair_text(symptom) or "任务执行异常，尚无具体问题说明"


def _repair_failure_detail(job, subscriber):
    failure = subscriber.get("payload", {}).get("repair_failure") or {}
    if failure.get("job") == job["id"] and failure.get("generation") == job.get("generation"):
        detail = _repair_text(failure.get("error"))
        if detail:
            phase = {"validation": "验证未通过", "resume": "原任务恢复失败"}.get(failure.get("phase"), "")
            return f"{phase}：{detail}" if phase else detail
    result = job.get("result") or {}
    detail = result.get("control_error") or result.get("error")
    known = {
        "workflow registration must be restored before repair": "任务登记已失效，需要恢复登记后继续",
        "repair worker exited without a receipt": "修复进程退出，未返回结果",
        "stale worker receipt": "修复进程返回了过期结果",
        "the same failure recurred in the verified runtime without a new contract": "恢复原任务后再次出现相同问题",
        "upstream behavior passed but full engine proof is incomplete or failed": "修复的完整验证未通过",
        "latest revision did not prove recovery; guarded mode will not generate code": "现有版本未通过恢复验证，当前模式不允许生成修复代码",
        "approved repair has no immutable candidate revision": "修复结果缺少可供验证的代码版本",
        "repair runtime is incompatible; synchronize the engine versions before retrying": "修复运行时版本不兼容，需要同步引擎版本后重试",
    }
    if result.get("environment_diagnostics"):
        return "修复环境准备失败，请查看详细日志中的环境安装记录"
    return known.get(detail) or _repair_text(detail) or "未返回具体原因，请查看详细日志"


def _repair_progress_message(job, subscriber):
    state, workflow = job["state"], subscriber["state"]
    if workflow == "finished":
        return "原任务已完成"
    if "cancelled" in (state, workflow):
        return "修复已取消"
    if "blocked" in (state, workflow):
        return "修复受阻：" + _repair_failure_detail(job, subscriber)
    if workflow == "resuming":
        return "已通过恢复检查，原任务继续运行" if state == "completed" else "正在恢复原任务"
    if workflow == "validating":
        return "修复方案已就绪，正在验证"
    if workflow == "verified":
        return "验证已通过，等待恢复原任务"
    if state in {"ready", "completed"}:
        return "修复方案已就绪，等待验证"
    if state == "queued":
        return "等待开始修复"
    if state == "repairing":
        progress = job.get("progress") or {}
        phase = progress.get("phase") or progress.get("kind", "")
        labels = {
            "request_contract_planning": "正在规划验收检查", "request_contract_ready": "验收检查已就绪",
            "candidate_generation": "正在生成修复代码", "candidate_correction": "正在修正候选",
            "repair_design": "正在设计修复方案", "contract_reanalysis": "正在重新分析验收要求",
            "component_plan": "正在细化当前组件方案", "scope_review": "正在独立判断问题是否必须修复",
            "plan_review": "正在独立审查组件方案", "planning_probe": "正在运行实施前诊断探针",
            "quick_verification": "正在检查本轮反例", "candidate_revalidation": "正在复核保留候选（不生成代码）",
            "environment_preparation": "正在准备验证依赖", "preparing_verification_environment": "正在准备验证依赖",
            "focused_verification": "正在执行针对性验证", "validating_focused_tests": "正在执行针对性验证",
            "boundary_replay": "正在验证原任务恢复边界", "validating_boundary_replay": "正在验证原任务恢复边界",
            "diagnosis_differential": "正在对比修复前后的行为", "full_suite": "正在执行完整回归",
            "integration_verification": "正在验证集成结果", "proof_seal": "正在确认验证证据",
            "candidate_result": "本轮候选已完成",
        }
        label = labels.get(phase, "正在审查候选" if phase.startswith("review_") else "正在修复")
        if progress.get("candidate"):
            label = f"第 {progress['candidate']} 轮：" + label
        previous = progress.get("last_result") or {}
        if previous and previous.get("status") not in {"approved", "candidate_group_completed"}:
            reason = _repair_text(previous.get("reason", ""))
            label += f"；第 {previous.get('candidate', '?')} 轮未通过：{reason}"
        imported = job.get("prior_repair_input") or {}
        if imported.get("source_job"):
            label = f"已接续上次候选（{_repair_text(imported['source_job'])[:8]}）；" + label
        return label
    return "等待修复进展"


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
    last = None
    announced = set()
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
            status = response["job"]["state"]
            job = response["job"]["id"]
            subscriber = next(item for item in response["subscribers"] if item["id"] == registration["subscriber"])
            prefix = f"Self-repair {job[:8]}："
            log_path = f"详细日志：{registration['config']['root']}/jobs/{job}"
            message = _repair_progress_message(response["job"], subscriber)
            first = job not in announced
            if first:
                # A resumed workflow can submit another repair while this relay waits.
                current_payload = subscriber.get("payload", {}).get("repair") or response["job"].get("payload") or payload
                action, problem = _repair_problem(current_payload)
                if status != "repairing" or subscriber["state"] != "waiting":
                    action = "修复问题" if action == "正在修复" else "待排查问题"
                _report_repair_progress(project, prefix + action + "：" + problem)
                _report_repair_progress(project, log_path)
                announced.add(job)
            if (job, message) != last:
                if not (first and status == "repairing" and subscriber["state"] == "waiting"
                        and not response["job"].get("progress")):
                    _report_repair_progress(project, prefix + message)
                if not first and (status in {"blocked", "cancelled"} or subscriber["state"] in {"blocked", "cancelled"}):
                    _report_repair_progress(project, log_path)
                last = (job, message)
            if subscriber["state"] == "finished":
                return 0
            if status in {"blocked", "cancelled"} or subscriber["state"] in {"blocked", "cancelled"}:
                return 3
            # Terminal subscribers have already released their registration.
            # Observe their durable result before trying to restore ownership,
            # otherwise each supervisor tick removes the new registration again.
            if registration["subscriber"] not in response.get("registered", []):
                registration = register(lock, args, orchestrator)
                if not registration:
                    raise RuntimeError("cannot restore repair ownership")
                continue
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
