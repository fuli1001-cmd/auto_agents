"""Record physical model calls once, including failed and unknown-usage calls."""
from dataclasses import asdict, replace
from pathlib import Path
import time
import uuid

from .models import AgentUsage
from .performance_trace import PerformanceTrace


def invoke_provider(adapter, request, provider):
    started = time.monotonic()
    result = None
    error_type = ""
    identifier = uuid.uuid4().hex
    try:
        result = adapter.run(request)
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        prompt = (result.prompt_metadata if result is not None else {}) or request.prompt_metadata
        record = {
            "call_id": identifier,
            "logical_call_id": request.logical_call_id,
            "attempt_id": request.attempt_id,
            "stage_attempt": prompt.get("stage_attempt", 1),
            "provider": provider, "stage": request.stage, "purpose": request.purpose,
            "model": prompt.get("resolved_model") or (result.model if result is not None else ""),
            "effort": request.effort,
            "ok": bool(result is not None and result.ok and not result.cleanup_incomplete),
            "error_type": error_type,
            "duration_seconds": time.monotonic() - started,
            "usage": asdict(result.usage) if result is not None and result.usage is not None else None,
            "prompt_bytes": prompt.get("prompt_bytes", len(str(request.prompt).encode("utf-8"))),
            "full_prompt_bytes": prompt.get("full_prompt_bytes", len(str(request.prompt).encode("utf-8"))),
            "prompt_mode": prompt.get("prompt_mode", "full"),
            "resumed": bool(prompt.get("resumed", request.resume_session_id)),
            "fallback_reason": prompt.get("fallback_reason", ""),
            "delta_candidate_bytes": prompt.get("delta_candidate_bytes"),
            "delta_fallback_reason": prompt.get("delta_fallback_reason", ""),
        }
        context = request.usage_context
        if context:
            try:
                trace = PerformanceTrace(Path(context["project_root"]), workflow_kind=context["workflow_kind"],
                                         subject_id=context["subject_id"], workflow_id=context.get("workflow_id", ""))
                trace.event("provider_attempt", request.stage, duration_seconds=record["duration_seconds"],
                            metadata=record, span_id=identifier)
            except (OSError, ValueError, KeyError) as error:
                # A diagnostics failure must not turn a completed operation into
                # an execution retry. The in-memory ledger remains available.
                record["recording_error"] = type(error).__name__
    return replace(result, usage_attempts=[record], prompt_metadata={
        **request.prompt_metadata, **result.prompt_metadata, "logical_call_id": request.logical_call_id,
    })


def with_attempt_usage(result, attempts):
    if not attempts:
        return result
    unique = {item["call_id"]: item for item in attempts}
    records = list(unique.values())
    usage = None
    for item in records:
        values = item.get("usage")
        if values is not None:
            usage = (usage or AgentUsage()).plus(AgentUsage(**values))
    return replace(result, usage=usage, usage_attempts=records, prompt_metadata={
        **result.prompt_metadata,
        "usage_complete": all(item.get("usage") is not None for item in records),
        "usage_attempt_count": len(records),
        "logical_call_id": records[-1]["logical_call_id"],
    })
