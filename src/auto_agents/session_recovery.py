"""Recover an explicitly requested session from a consistent durable journal.

This module never restores product files or attaches an unrelated ambient run.
Discovery is read-only; application is restartable and refuses existing changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .git_ops import _git_bytes
from .models import SessionState
from .workflow_chain import WorkflowHandoff, WorkflowSnapshot, sha256_text


class SessionRecoveryError(FileNotFoundError):
    """A requested session has no complete, unambiguous recovery boundary."""


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise SessionRecoveryError("invalid session/workflow identifier")
    return value


def _bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class SessionRecoveryPlan:
    session_id: str
    workflow_id: str
    mode: str
    event_sequence: int
    files: dict[str, dict] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"schema_version": 1, **vars(self)}


def _inventory(project: Path) -> list[tuple[dict, str]]:
    records = []
    root = project / ".auto-agents/state/checkpoint_blobs"
    for path in sorted(root.glob("*/*")):
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", path.name):
            continue
        raw = path.read_bytes()
        if _sha(raw) != path.name:
            continue
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict):
            records.append((value, path.relative_to(project).as_posix()))
    return records


def _historical_records(project: Path, workflow_id: str, session_id: str) -> list[tuple[dict, str]]:
    prefix = f".auto-agents/state/workflows/{workflow_id}"
    session_path = f".auto-agents/state/sessions/{session_id}/session_state.json"
    history = _git_bytes(project, "log", "--all", "-n", "64", "--format=%H", "--", prefix, session_path)
    records = []
    seen = set()
    for revision in history.stdout.decode().splitlines():
        tree = _git_bytes(project, "ls-tree", "-r", "--name-only", revision, "--", prefix,
                          ".auto-agents/state/sessions", ".auto-agents/state/handoffs")
        names = tree.stdout.decode().splitlines()
        if not names:
            continue
        for name in names:
            if not name.endswith(".json") or "/checkpoints/" in name:
                continue
            if not (name.startswith(prefix + "/") or name.endswith("/session_state.json") or name.startswith(".auto-agents/state/handoffs/")):
                continue
            raw = _git_bytes(project, "show", f"{revision}:{name}")
            if raw.returncode or _sha(raw.stdout) in seen:
                continue
            seen.add(_sha(raw.stdout))
            try:
                value = json.loads(raw.stdout)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict):
                records.append((value, f"git:{revision}:{name}"))
        # The newest surviving journal supplies the missing committed prefix.
        if any("/events/" in name for name in names):
            break
    return records


def _latest(records: list[tuple[dict, str]], **identity: str) -> tuple[dict, str]:
    matches = [(v, source) for v, source in records if all(v.get(k) == expected for k, expected in identity.items())]
    if not matches:
        raise SessionRecoveryError(f"missing durable recovery record: {identity}")
    matches.sort(key=lambda pair: str(pair[0].get("updated_at", "")), reverse=True)
    newest = str(matches[0][0].get("updated_at", ""))
    if len({_sha(_bytes(v)) for v, _ in matches if str(v.get("updated_at", "")) == newest}) > 1:
        raise SessionRecoveryError(f"ambiguous durable recovery record: {identity}")
    return matches[0]


def plan_session_recovery(project: Path, session_id: str, mode: str) -> SessionRecoveryPlan:
    project = project.resolve()
    session_id = _identifier(session_id)
    receipt_path = project / f".auto-agents/state/session-restorations/{session_id}/manifest.json"
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_bytes())
        payload = receipt.get("plan", {})
        if receipt.get("plan_sha256") != _sha(_bytes(payload)):
            raise SessionRecoveryError("session restoration manifest digest mismatch")
        plan = SessionRecoveryPlan(**{key: payload[key] for key in SessionRecoveryPlan.__dataclass_fields__})
        if plan.session_id != session_id or plan.mode != mode:
            raise SessionRecoveryError("session restoration identity mismatch")
        if receipt.get("status") == "applying":
            return plan
    records = _inventory(project)
    try:
        root_session, _ = _latest(records, session_id=session_id)
    except SessionRecoveryError:
        relative = f".auto-agents/state/sessions/{session_id}/session_state.json"
        history = _git_bytes(project, "log", "--all", "-n", "64", "--format=%H", "--", relative)
        for revision in history.stdout.decode().splitlines():
            raw = _git_bytes(project, "show", f"{revision}:{relative}")
            if raw.returncode:
                continue
            try:
                payload = json.loads(raw.stdout)
            except (ValueError, UnicodeError):
                continue
            if isinstance(payload, dict) and payload.get("session_id") == session_id:
                records.append((payload, f"git:{revision}:{relative}"))
        root_session, _ = _latest(records, session_id=session_id)
    if root_session.get("mode") != mode:
        raise SessionRecoveryError(f"session {session_id} is not {mode}")
    workflow_id = _identifier(str(root_session.get("workflow_id", "")))
    records.extend(_historical_records(project, workflow_id, session_id))
    workflows = [(v, source) for v, source in records if v.get("workflow_id") == workflow_id and v.get("root") == {"kind": mode, "native_id": session_id}]
    workflows.sort(key=lambda pair: int(pair[0].get("event_sequence", 0)), reverse=True)
    errors = []
    for workflow, source in workflows:
        try:
            return _plan_boundary(records, workflow, source, session_id, mode)
        except (SessionRecoveryError, ValueError, TypeError, KeyError) as error:
            errors.append(str(error))
    raise SessionRecoveryError(f"session {session_id} has no complete recovery boundary: " + "; ".join(errors[:3]))


def _plan_boundary(records: list[tuple[dict, str]], workflow: dict, source: str, session_id: str, mode: str) -> SessionRecoveryPlan:
    snapshot = WorkflowSnapshot.from_dict(workflow)
    workflow_id = _identifier(snapshot.workflow_id)
    sequence = snapshot.event_sequence
    if sequence < 1:
        raise SessionRecoveryError("recovery journal has no events")
    plan = SessionRecoveryPlan(session_id, workflow_id, mode, sequence)

    def put(name: str, record: tuple[dict, str]) -> None:
        plan.files[name], plan.sources[name] = record

    put(f".auto-agents/state/workflows/{workflow_id}/workflow.json", (workflow, source))
    expected = snapshot.last_event_sha256
    journal = []
    for number in range(sequence, 0, -1):
        choices = []
        for event, event_source in records:
            if event.get("workflow_id") != workflow_id or event.get("sequence") != number or not event.get("event_id"):
                continue
            material = {k: v for k, v in event.items() if k != "event_sha256"}
            if event.get("event_sha256") == expected and expected == sha256_text(json.dumps(material, ensure_ascii=False, sort_keys=True)):
                choices.append((event, event_source))
        if len({v["event_sha256"] for v, _ in choices}) != 1:
            raise SessionRecoveryError(f"journal event {number} is missing, corrupt or ambiguous")
        event, event_source = choices[0]
        expected = event.get("previous_event_sha256", "")
        journal.append((number, event, event_source))
    if expected:
        raise SessionRecoveryError("workflow journal root mismatch")
    tip = journal[0][1]
    if (tip.get("active_frame") != workflow.get("active_frame")
            or str(tip.get("active_handoff_id", "")) != snapshot.active_handoff_id):
        raise SessionRecoveryError("workflow frame does not match its journal boundary")
    for number, event, event_source in reversed(journal):
        put(f".auto-agents/state/workflows/{workflow_id}/events/{number:08d}-{_identifier(event['event_id'])}.json", (event, event_source))
    cutoff = str(workflow.get("updated_at", ""))
    # A prepared handoff is persisted immediately after its workflow snapshot.
    # Its created_at binds it to that boundary even when updated_at is later.
    eligible = [(v, s) for v, s in records if v.get("workflow_id") == workflow_id and (str(v.get("updated_at", "")) <= cutoff or (v.get("handoff_id") == snapshot.active_handoff_id and str(v.get("created_at", "")) <= cutoff))]
    root_record = _latest(eligible, session_id=session_id)
    root = SessionState.from_dict(root_record[0])
    if root.mode != mode or root.active_handoff_id != snapshot.active_handoff_id:
        raise SessionRecoveryError("root session and workflow are from different boundaries")
    queue = [session_id]
    handoffs = {str(v.get("handoff_id")) for v, _ in eligible if v.get("handoff_id")}
    for handoff_id in sorted(handoffs):
        record = _latest(eligible, handoff_id=handoff_id)
        handoff = WorkflowHandoff.from_dict(record[0])
        _identifier(handoff.handoff_id)
        if handoff.target not in {"fix", "run", "resume"}:
            raise SessionRecoveryError("unknown handoff target")
        if handoff.parent.kind != "run":
            queue.append(_identifier(handoff.parent.native_id))
        if handoff.child and handoff.child.kind != "run":
            queue.append(_identifier(handoff.child.native_id))
        put(f".auto-agents/state/handoffs/{handoff_id}.json", record)
    if snapshot.active_handoff_id and snapshot.active_handoff_id not in handoffs:
        raise SessionRecoveryError("active handoff is missing")
    for identifier in sorted(set(queue)):
        record = _latest(eligible, session_id=identifier)
        session = SessionState.from_dict(record[0])
        if session.workflow_id != workflow_id:
            raise SessionRecoveryError("child session belongs to another workflow")
        if session.parent_handoff_id and session.parent_handoff_id not in handoffs:
            raise SessionRecoveryError("child session parent handoff is missing")
        put(f".auto-agents/state/sessions/{identifier}/session_state.json", record)
    if snapshot.active_handoff_id:
        active = WorkflowHandoff.from_dict(_latest(eligible, handoff_id=snapshot.active_handoff_id)[0])
        allowed_frames = [active.parent] if active.status == "prepared" else [active.child]
        if active.status == "returned":
            allowed_frames.append(active.parent)
        if snapshot.active_frame not in allowed_frames:
            raise SessionRecoveryError("active handoff and workflow frame disagree")
        if active.child and active.child.kind != "run":
            child = _latest(eligible, session_id=active.child.native_id)[0]
            if child.get("mode") != active.child.kind:
                raise SessionRecoveryError("active child mode mismatch")
            if active.target != "resume" and child.get("parent_handoff_id") != active.handoff_id:
                raise SessionRecoveryError("active child belongs to a different handoff")
    if snapshot.active_frame.kind != "run" and snapshot.active_frame.native_id not in queue:
        raise SessionRecoveryError("active frame has no session checkpoint")
    return plan


def apply_session_recovery(project: Path, plan: SessionRecoveryPlan) -> Path:
    """Apply under the caller's project lock; identical partial writes are safe."""
    project = project.resolve()
    session_id = _identifier(plan.session_id)
    _identifier(plan.workflow_id)
    workflow = plan.files.get(f".auto-agents/state/workflows/{plan.workflow_id}/workflow.json", {})
    frame = workflow.get("active_frame", {})
    if frame.get("kind") == "run":
        run_path = project / ".auto-agents/state/run_state.json"
        run = json.loads(run_path.read_bytes()) if run_path.is_file() else {}
        if (run.get("run_id") != frame.get("native_id")
                or run.get("resume_context", {}).get("workflow_id") != plan.workflow_id):
            raise SessionRecoveryError("active child run checkpoint is missing or belongs to another workflow")
    targets = []
    for relative, payload in plan.files.items():
        if not re.fullmatch(r"\.auto-agents/state/(?:sessions/[A-Za-z0-9_-]+/session_state\.json|handoffs/[A-Za-z0-9_-]+\.json|workflows/[A-Za-z0-9_-]+/(?:workflow\.json|events/[A-Za-z0-9_-]+\.json))", relative):
            raise SessionRecoveryError("restoration target is outside workflow control state")
        target = project / relative
        if target.is_symlink() or not target.resolve().is_relative_to(project):
            raise SessionRecoveryError("restoration target escapes project")
        raw = _bytes(payload)
        if target.exists() and json.loads(target.read_bytes()) != payload:
            raise SessionRecoveryError(f"restoration would overwrite newer or different state: {relative}")
        targets.append((target, raw))
    receipt = project / f".auto-agents/state/session-restorations/{session_id}/manifest.json"
    payload = plan.to_dict()
    record = {"schema_version": 1, "status": "applying", "plan_sha256": _sha(_bytes(payload)), "plan": payload}
    _atomic(receipt, _bytes(record))
    # The root session is the commit marker. Do not expose it before its graph.
    root_path = project / f".auto-agents/state/sessions/{session_id}/session_state.json"
    targets.sort(key=lambda item: item[0] == root_path)
    for target, raw in targets:
        if not target.exists():
            _atomic(target, raw)
    record["status"] = "completed"
    _atomic(receipt, _bytes(record))
    return receipt
