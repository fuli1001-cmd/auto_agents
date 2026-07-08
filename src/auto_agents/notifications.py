from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional


WECHAT_WEBHOOK_ENV = "WECHAT_WEBHOOK_URL"
WECHAT_MARKDOWN_LIMIT_BYTES = 4096
logger = logging.getLogger(__name__)


def send_wechat_markdown(content: str, webhook_url: Optional[str] = None) -> bool:
    """Send a markdown notification to an Enterprise WeChat group robot."""
    url = (webhook_url or os.environ.get(WECHAT_WEBHOOK_ENV, "")).strip()
    if not url:
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": _truncate_utf8(content, WECHAT_MARKDOWN_LIMIT_BYTES),
        },
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "auto-agents/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
    except (OSError, ValueError, urllib.error.URLError) as exc:
        logger.warning("Failed to send Enterprise WeChat notification: %r", exc)
        return False

    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Enterprise WeChat notification returned invalid JSON: %r", exc)
        return False
    if isinstance(result, dict) and result.get("errcode") == 0:
        return True
    logger.warning("Enterprise WeChat notification API error: %r", result)
    return False


def notify_run_finished(
    project_root: Path,
    state_payload: Mapping[str, object],
    *,
    status: Optional[str] = None,
    error: str = "",
) -> bool:
    final_status = (status or str(state_payload.get("status", ""))).strip()
    if final_status not in {"completed", "failed"}:
        return False
    run_id = str(state_payload.get("run_id", "")).strip()
    stage = str(state_payload.get("current_stage", "")).strip()
    last_error = error or str(state_payload.get("last_error", "")).strip()
    paths = [
        project_root / ".auto-agents" / "state" / "run_state.json",
    ]
    if run_id:
        paths.append(project_root / ".auto-agents" / "runs" / run_id / "run.log")
    return notify_flow_finished(
        project_root,
        workflow="run",
        status=final_status,
        identifier=run_id,
        stage=stage,
        detail=last_error,
        paths=paths,
    )


def notify_run_started(project_root: Path) -> bool:
    return notify_flow_started(project_root, workflow="run")


def notify_session_finished(
    project_root: Path,
    state_payload: Mapping[str, object],
    *,
    command: str,
    status: Optional[str] = None,
    error: str = "",
) -> bool:
    final_status = (status or str(state_payload.get("status", ""))).strip()
    if final_status not in {"completed", "failed"}:
        return False
    session_id = str(state_payload.get("session_id", "")).strip()
    mode = str(state_payload.get("mode", command)).strip()
    resolution = str(state_payload.get("resolution", "")).strip()
    detail = error or resolution
    paths = []
    if session_id:
        paths.append(project_root / ".auto-agents" / "state" / "sessions" / session_id / "session_state.json")
    return notify_flow_finished(
        project_root,
        workflow=command,
        status=final_status,
        identifier=session_id,
        mode=mode,
        detail=detail,
        paths=paths,
    )


def notify_session_started(
    project_root: Path,
    *,
    command: str,
    session_id: str = "",
    mode: str = "",
) -> bool:
    return notify_flow_started(
        project_root,
        workflow=command,
        identifier=session_id,
        mode=mode or ("provider_resolve" if command == "provider-resolve" else command),
    )


def notify_self_repair_finished(
    target_project_root: Path,
    *,
    auto_agents_root: Path,
    status: str,
    reason: str,
    commit_sha: str = "",
    summary: str = "",
    verification: str = "",
) -> bool:
    if status not in {"completed", "failed"}:
        return False
    detail_lines = []
    if reason.strip():
        detail_lines.extend(["**Trigger**", _truncate_text(reason.strip(), 700)])
    if summary.strip():
        detail_lines.extend(["", "**Repair Summary**", _truncate_text(summary.strip(), 700)])
    if commit_sha.strip():
        detail_lines.extend(["", f"> Commit: {commit_sha.strip()}"])
    if verification.strip():
        detail_lines.extend(["", "**Verification**", _truncate_text(verification.strip(), 700)])
    detail_lines.extend(["", f"> auto_agents: {auto_agents_root.expanduser()}"])
    return notify_flow_finished(
        target_project_root,
        workflow="self-repair",
        status=status,
        identifier=commit_sha[:12] if commit_sha else "",
        detail="\n".join(detail_lines).strip(),
        paths=(auto_agents_root,),
    )


def notify_flow_started(
    project_root: Path,
    *,
    workflow: str,
    identifier: str = "",
    stage: str = "",
    mode: str = "",
    detail: str = "",
) -> bool:
    content = _format_flow_message(
        project_root,
        workflow=workflow,
        status="started",
        identifier=identifier,
        stage=stage,
        mode=mode,
        detail=detail,
    )
    return send_wechat_markdown(content)


def notify_flow_finished(
    project_root: Path,
    *,
    workflow: str,
    status: str,
    identifier: str = "",
    stage: str = "",
    mode: str = "",
    detail: str = "",
    paths: Iterable[Path] = (),
) -> bool:
    if status not in {"completed", "failed"}:
        return False
    content = _format_flow_message(
        project_root,
        workflow=workflow,
        status=status,
        identifier=identifier,
        stage=stage,
        mode=mode,
        detail=detail,
        paths=paths,
    )
    return send_wechat_markdown(content)


def _format_flow_message(
    project_root: Path,
    *,
    workflow: str,
    status: str,
    identifier: str = "",
    stage: str = "",
    mode: str = "",
    detail: str = "",
    paths: Iterable[Path] = (),
) -> str:
    project_root = project_root.expanduser()
    project_name = _project_name(project_root)
    color = "warning" if status == "failed" else "info"
    time_label = "Started" if status == "started" else "Finished"
    lines = [
        f"<font color=\"{color}\">**auto-agents {workflow} {status}**</font>",
        "",
        f"> Project: {project_name}",
        f"> {time_label}: {_format_timestamp()}",
    ]
    if identifier:
        lines.append(f"> ID: {identifier}")
    if mode:
        lines.append(f"> Mode: {mode}")
    if stage:
        lines.append(f"> Stage: {stage}")
    if detail:
        lines.extend(["", "**Detail**", _truncate_text(detail.strip(), 900)])
    return "\n".join(lines)


def _format_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _project_name(project_root: Path) -> str:
    config_file = project_root / ".auto-agents" / "config.json"
    try:
        payload = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return project_root.name or "unknown-project"
    if not isinstance(payload, dict):
        return project_root.name or "unknown-project"
    name = str(payload.get("project_name", "")).strip()
    return name or project_root.name or "unknown-project"


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 15)].rstrip() + "\n...[truncated]"


def _truncate_utf8(value: str, limit_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return value
    suffix = "\n...[truncated]"
    suffix_bytes = suffix.encode("utf-8")
    budget = max(0, limit_bytes - len(suffix_bytes))
    truncated = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return truncated + suffix
