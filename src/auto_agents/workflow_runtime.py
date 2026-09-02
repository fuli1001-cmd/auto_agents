from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

from .config import (
    create_session,
    load_run_state,
    load_session_state,
    save_run_state,
    save_session_state,
)
from .git_ops import (
    amend_only_paths,
    changed_entries,
    changed_paths,
    commit_only_paths,
    head_ref,
)
from .io_utils import read_json, write_json
from .workflow_chain import (
    IssueBriefBuilder,
    IterationSpecBuilder,
    WorkflowHandoff,
    WorkflowRef,
    WorkflowSnapshot,
    WorkflowStore,
)


class WorkflowCoordinator:
    """Drive nested run/fix/collab frames without recursively invoking the CLI."""

    def __init__(
        self,
        orchestrator: object,
        *,
        print_agent_output: bool = False,
        full_verify: bool = False,
        auto_approve: bool = False,
        health_runtime: object = None,
        run_lock: object = None,
    ) -> None:
        self.orch = orchestrator
        self.project_root = Path(orchestrator.project_root).resolve()
        self.store = WorkflowStore(self.project_root)
        self.print_agent_output = bool(print_agent_output)
        self.full_verify = bool(full_verify)
        self.auto_approve = bool(auto_approve)
        self.health_runtime = health_runtime
        self.run_lock = run_lock

    def _create_session(self, session: object):
        return create_session(
            self.project_root,
            session.mode,
            hard_ceiling=session.config.execution.session_limits.for_mode(
                session.mode
            ),
        )

    def start_session(self, session: object):
        active = self.store.active()
        if (
            active is not None
            and active.root.kind == "run"
            and session.mode == "provider_resolve"
        ):
            state = self._create_session(session)
            state.workflow_id = active.workflow_id
            state.auto_approve = bool(self.auto_approve)
            state.full_verify = bool(session._full_verify)
            save_session_state(self.project_root, state)
            active.active_frame = WorkflowRef(session.mode, state.session_id)
            self.store.save(active)
            self.store.append_event(
                active,
                "provider_resolve_child_started",
                details={"session_id": state.session_id},
            )
            result = self._drive_session(session, state, active, root=False)
            run_state = load_run_state(self.project_root)
            active.active_frame = WorkflowRef("run", run_state.run_id)
            self.store.save(active)
            self.store.append_event(
                active,
                "provider_resolve_child_returned",
                details={"session_id": state.session_id, "status": result.status},
            )
            if run_state.status == "completed":
                self.store.complete(active, status="completed")
                if head_ref(self.project_root):
                    amend_only_paths(
                        self.project_root,
                        [
                            f".auto-agents/state/workflows/{active.workflow_id}",
                            ".auto-agents/state/workflows/active.json",
                        ],
                )
            return result
        if (
            active is not None
            and active.root.kind == "provider_resolve"
            and session.mode == "provider_resolve"
        ):
            try:
                prior_provider_state = load_session_state(
                    self.project_root, active.root.native_id
                )
            except FileNotFoundError:
                prior_provider_state = None
            if prior_provider_state is not None and prior_provider_state.status in {
                "blocked",
                "failed",
            }:
                active.status = "suspended"
                self.store.save(active)
                self.store.append_event(
                    active,
                    "workflow_suspended",
                    details={"reason": "new provider recovery audit"},
                )
                self.store.clear_active(active.workflow_id)
                active = None
        if active is not None and active.status not in {"completed", "suspended"}:
            if bool(getattr(session, "_replace_active_workflow", False)):
                active.status = "suspended"
                self.store.save(active)
                self.store.append_event(
                    active,
                    "workflow_suspended",
                    details={"reason": "user explicitly started a new workflow"},
                )
            else:
                raise RuntimeError(
                    f"workflow {active.workflow_id} is already active; resume it or explicitly choose a new session"
                )
        state = self._create_session(session)
        state.auto_approve = bool(self.auto_approve)
        state.full_verify = bool(session._full_verify)
        state.lineage_head_ref = head_ref(self.project_root)
        state.protected_preexisting_paths = list(changed_paths(self.project_root))
        snapshot = self.store.create_root(
            WorkflowRef(session.mode, state.session_id),
            workflow_id=state.workflow_id,
        )
        if self.run_lock is not None:
            self.run_lock.bind_subject(session.mode, state.session_id)
            self.orch._run_token = str(
                getattr(self.health_runtime, "run_token", "")
                or self.run_lock.run_token
            )
        if self.health_runtime is not None:
            self.health_runtime.bind_subject(state.session_id)
            self.health_runtime.set_phase(session.mode)
        state.workflow_id = snapshot.workflow_id
        save_session_state(self.project_root, state)
        session._print(f"Session {state.session_id} started in {state.mode} mode.")
        return self._drive_session(session, state, snapshot, root=True)

    def start_seeded_session(
        self,
        session: object,
        *,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
    ):
        child_id = str(handoff.payload.get("child_session_id", "")).strip()
        if child_id:
            try:
                state = load_session_state(self.project_root, child_id)
            except FileNotFoundError:
                state = self._create_session(session)
                handoff.payload["child_session_id"] = state.session_id
                self.store.save_handoff(handoff)
        else:
            state = self._create_session(session)
            handoff.payload["child_session_id"] = state.session_id
            self.store.save_handoff(handoff)
        self.auto_approve = bool(
            self.auto_approve or handoff.payload.get("auto_approve", False)
        )
        session._auto_approve = self.auto_approve
        state.workflow_id = snapshot.workflow_id
        state.parent_handoff_id = handoff.handoff_id
        state.goal = handoff.goal
        if not state.conversation and state.goal:
            state.conversation.append({"role": "user", "content": state.goal})
        state.auto_approve = bool(self.auto_approve)
        state.full_verify = bool(session._full_verify)
        state.lineage_head_ref = str(handoff.payload.get("head_before", "")) or head_ref(
            self.project_root
        )
        state.protected_preexisting_paths = [
            str(item) for item in handoff.payload.get("protected_preexisting_paths", [])
        ]
        save_session_state(self.project_root, state)
        child_ref = WorkflowRef(session.mode, state.session_id)
        if self.run_lock is not None:
            self.run_lock.bind_subject(session.mode, state.session_id)
        if self.health_runtime is not None:
            self.health_runtime.bind_subject(state.session_id)
            self.health_runtime.set_phase(session.mode)
        self.store.bind_child(snapshot, handoff, child_ref)
        if session.mode == "fix":
            seed = dict(handoff.payload.get("issue_seed", {}))
            seed.setdefault("reported_goal", state.goal)
            seed.setdefault("source_handoff_id", handoff.handoff_id)
            IssueBriefBuilder(self.project_root, state.session_id).materialize(seed)
        return self._drive_session(session, state, snapshot, root=False)

    def resume_session(self, session: object, session_id: str):
        state = load_session_state(self.project_root, session_id)
        if state.mode != session.mode:
            raise ValueError(
                f"session {session_id} is {state.mode}, not {session.mode}"
            )
        resume_state_changed = False
        if state.status != "completed":
            resume_state_changed = session._invalidate_provider_continuations(
                state,
                reason="process-level session resume uses the durable transcript",
            )
            if not (
                state.status == "failed"
                or (
                    state.status == "paused"
                    and state.resolution == "interrupted_by_user"
                )
            ):
                session._begin_attempt_epoch(
                    state,
                    reason="process-level session resume",
                )
                resume_state_changed = True
        if resume_state_changed:
            save_session_state(self.project_root, state)
        self.auto_approve = bool(self.auto_approve or state.auto_approve)
        self.full_verify = bool(self.full_verify or state.full_verify)
        session._auto_approve = self.auto_approve
        session._full_verify = self.full_verify
        self.orch._force_full_verify = bool(
            self.full_verify and session.mode == "fix"
        )
        if state.auto_approve != self.auto_approve:
            state.auto_approve = self.auto_approve
            save_session_state(self.project_root, state)
        if state.full_verify != self.full_verify:
            state.full_verify = self.full_verify
            save_session_state(self.project_root, state)
        if state.parent_handoff_id and not state.workflow_id:
            try:
                parent_handoff = self.store.load_handoff(
                    state.parent_handoff_id
                )
            except (FileNotFoundError, RuntimeError, ValueError) as error:
                raise RuntimeError(
                    f"child session {session_id} has no recoverable parent workflow"
                ) from error
            state.workflow_id = parent_handoff.workflow_id
            save_session_state(self.project_root, state)
        if not state.workflow_id:
            snapshot = self.store.create_root(WorkflowRef(state.mode, state.session_id))
            state.workflow_id = snapshot.workflow_id
            state.lineage_head_ref = state.lineage_head_ref or state.baseline_head_ref or head_ref(
                self.project_root
            )
            save_session_state(self.project_root, state)
        else:
            snapshot = self.store.load(state.workflow_id)
        if state.parent_handoff_id:
            # A child session is not an independent workflow root. Resume the
            # durable root so the child receipt is consumed and control
            # returns through every recorded parent frame automatically.
            return self.resume_workflow(snapshot.workflow_id)
        self.store.activate(snapshot.workflow_id)
        self._reconcile_open_operations(snapshot)
        self.store.begin_resume(snapshot)
        fresh_health_boundary = bool(
            getattr(self.health_runtime, "fresh_health_boundary", False)
        )
        if fresh_health_boundary and self.run_lock is not None:
            self.run_lock.bind_subject(state.mode, state.session_id)
            self.orch._run_token = str(
                getattr(self.health_runtime, "run_token", "")
                or self.run_lock.run_token
            )
        if fresh_health_boundary:
            self.health_runtime.set_phase(state.mode)
        self._ensure_completed_session_commit(session, state)
        snapshot = self.store.load(snapshot.workflow_id)
        return self._drive_session(session, state, snapshot, root=True)

    def resume_active(self):
        snapshot = self.store.active()
        if snapshot is None:
            candidates = self.store.resumable()
            if len(candidates) != 1:
                raise RuntimeError(
                    "no unique active workflow; pass --workflow to select one of: "
                    + ", ".join(item.workflow_id for item in candidates)
                )
            snapshot = candidates[0]
            self.store.activate(snapshot.workflow_id)
        root = snapshot.root
        self._inherit_root_policies(snapshot)
        if root.kind in {"collab", "fix", "provider_resolve"}:
            from .session import Session

            session = Session(
                self.orch,
                mode=root.kind,
                print_agent_output=self.print_agent_output,
                full_verify=self.full_verify,
                auto_approve=self.auto_approve,
                health_runtime=self.health_runtime,
                coordinator=self,
            )
            return self.resume_session(session, root.native_id)
        if root.kind == "run":
            self._reconcile_open_operations(snapshot)
            self.store.begin_resume(snapshot)
            return self._resume_run_root(snapshot)
        raise RuntimeError(f"unsupported workflow root: {root.kind}")

    def reconcile_interruption(self, snapshot_payload: Dict[str, object]) -> None:
        if not snapshot_payload:
            return
        snapshot = self.store.active()
        if snapshot is None:
            return
        owner = snapshot_payload.get("owner", {})
        control = snapshot_payload.get("control", {})
        details = {
            "detected_at": str(snapshot_payload.get("detected_at", "")),
            "previous_owner_pid": int(owner.get("pid", 0) or 0)
            if isinstance(owner, dict)
            else 0,
            "last_control_update": str(control.get("updated_at", ""))
            if isinstance(control, dict)
            else "",
            "head": head_ref(self.project_root),
            "changed_paths": list(changed_paths(self.project_root)),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "workflow_id": snapshot.workflow_id,
                    "active_frame": (
                        snapshot.active_frame.to_dict()
                        if snapshot.active_frame is not None
                        else None
                    ),
                    "active_handoff_id": snapshot.active_handoff_id,
                    "head": details["head"],
                    "changed_paths": details["changed_paths"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        details["fingerprint"] = fingerprint
        occurrences = 1 + sum(
            1
            for event in self.store.events(snapshot.workflow_id)
            if str(event.get("kind", "")) == "recovery_required"
            and isinstance(event.get("details"), dict)
            and str(event["details"].get("fingerprint", "")) == fingerprint
        )
        details["occurrence_count"] = occurrences
        self.store.mark_recovery_required(
            snapshot,
            reason="previous workflow owner disappeared before a terminal receipt",
            details=details,
        )
        max_rounds = max(
            1,
            int(
                getattr(
                    getattr(
                        getattr(self.orch.config, "execution", None),
                        "recovery",
                        None,
                    ),
                    "max_rounds",
                    3,
                )
            ),
        )
        if occurrences > max_rounds:
            snapshot.status = "blocked"
            self.store.save(snapshot)
            self.store.append_event(
                snapshot,
                "recovery_blocked",
                details={
                    "fingerprint": fingerprint,
                    "occurrence_count": occurrences,
                    "limit": max_rounds,
                },
            )
            raise RuntimeError(
                "the same workflow checkpoint was interrupted repeatedly without progress; "
                f"fingerprint={fingerprint} occurrences={occurrences} limit={max_rounds}"
            )
        if snapshot.active_frame and snapshot.active_frame.kind == "run":
            self.orch.reconcile_runtime_interruption(snapshot_payload)

    def resume_workflow(self, workflow_id: str):
        snapshot = self.store.load(workflow_id)
        self.store.activate(workflow_id)
        self._reconcile_open_operations(snapshot)
        self.store.begin_resume(snapshot)
        root = snapshot.root
        self._inherit_root_policies(snapshot)
        if root.kind in {"collab", "fix", "provider_resolve"}:
            from .session import Session

            session = Session(
                self.orch,
                mode=root.kind,
                print_agent_output=self.print_agent_output,
                full_verify=self.full_verify,
                auto_approve=self.auto_approve,
                health_runtime=self.health_runtime,
                coordinator=self,
            )
            state = load_session_state(
                self.project_root,
                root.native_id,
            )
            self._ensure_completed_session_commit(session, state)
            snapshot = self.store.load(snapshot.workflow_id)
            return self._drive_session(
                session,
                state,
                snapshot,
                root=True,
            )
        return self._resume_run_root(snapshot)

    def _inherit_root_policies(self, snapshot: WorkflowSnapshot) -> None:
        """Restore durable approval and verification policies from the root."""

        inherited = False
        inherited_full_verify = False
        root_session = None
        if snapshot.root.kind in {"collab", "fix", "provider_resolve"}:
            try:
                root_session = load_session_state(
                    self.project_root, snapshot.root.native_id
                )
                inherited = bool(root_session.auto_approve)
                inherited_full_verify = bool(root_session.full_verify)
            except FileNotFoundError:
                inherited = False
        elif snapshot.root.kind == "run":
            try:
                inherited = bool(
                    load_run_state(self.project_root).resume_context.get(
                        "auto_approve", False
                    )
                )
            except FileNotFoundError:
                inherited = False
        self.auto_approve = bool(self.auto_approve or inherited)
        self.full_verify = bool(self.full_verify or inherited_full_verify)
        if (
            root_session is not None
            and self.auto_approve
            and not root_session.auto_approve
        ):
            root_session.auto_approve = True
            save_session_state(self.project_root, root_session)
        if (
            root_session is not None
            and self.full_verify
            and not root_session.full_verify
        ):
            root_session.full_verify = True
            save_session_state(self.project_root, root_session)

    def _reconcile_open_operations(self, snapshot: WorkflowSnapshot) -> None:
        intents: Dict[str, Dict[str, object]] = {}
        completed = set()
        for event in self.store.events(snapshot.workflow_id):
            operation_id = str(event.get("operation_id", ""))
            if not operation_id:
                continue
            if str(event.get("kind", "")) == "operation_intent":
                intents[operation_id] = event
            elif str(event.get("kind", "")) == "operation_completed":
                completed.add(operation_id)
        for operation_id, event in intents.items():
            if operation_id in completed:
                continue
            commit_sha = _find_operation_commit(self.project_root, operation_id)
            if not commit_sha:
                continue
            details = (
                dict(event.get("details", {}))
                if isinstance(event.get("details"), dict)
                else {}
            )
            self.store.append_event(
                snapshot,
                "operation_completed",
                operation_id=operation_id,
                details={
                    "kind": str(details.get("kind", "commit")),
                    "commit_sha": commit_sha,
                    "reconciled_after_interruption": True,
                },
            )

    def _ensure_completed_session_commit(
        self,
        session: object,
        state: object,
    ) -> None:
        if (
            state.status != "completed"
            or state.mode not in {"fix", "collab"}
            or _head_contains_completed_session(
                self.project_root,
                state.session_id,
            )
        ):
            return
        session._coordinator = self
        session._coordinator_managed = True
        session._git_commit(state, state.mode)
        if not _head_contains_completed_session(
            self.project_root,
            state.session_id,
        ):
            raise RuntimeError(
                "completed session is missing its durable Git commit: "
                f"{state.session_id}"
            )

    def _drive_session(
        self,
        session: object,
        state: object,
        snapshot: WorkflowSnapshot,
        *,
        root: bool,
    ):
        session._coordinator = self
        session._coordinator_managed = True
        if state.status == "failed":
            session._invalidate_provider_continuations(
                state,
                reason="failed session started a fresh durable resume boundary",
            )
            state.current_attempt = 0
            session._begin_attempt_epoch(
                state,
                reason="failed session resumed",
            )
            state.status = (
                "waiting_child"
                if state.active_handoff_id
                else (
                    state.resume_phase
                    if state.resume_phase in {"conversing", "executing"}
                    else "executing"
                )
            )
            state.resume_phase = ""
            state.resolution = ""
            save_session_state(self.project_root, state)
        elif state.status == "paused" and state.resolution == "interrupted_by_user":
            session._invalidate_provider_continuations(
                state,
                reason="interrupted session started a fresh durable resume boundary",
            )
            session._begin_attempt_epoch(
                state,
                reason="interrupted session resumed",
            )
            state.status = (
                "waiting_child"
                if state.active_handoff_id
                else (
                    state.resume_phase
                    if state.resume_phase in {"conversing", "executing"}
                    else ("executing" if state.goal else "conversing")
                )
            )
            state.resume_phase = ""
            state.resolution = ""
            save_session_state(self.project_root, state)
        elif state.status == "waiting_user":
            # A process can exit while the interactive input prompt is open.
            # Re-enter execution so the saved assistance marker is validated
            # and, when still valid, presented to the user again.
            state.status = "executing"
            save_session_state(self.project_root, state)
        if bool(getattr(self.health_runtime, "fresh_health_boundary", False)):
            self.health_runtime.publish_session(state)
        while True:
            if state.status == "waiting_child" and state.active_handoff_id:
                returned = self._drive_handoff(session, state, snapshot)
                if returned is None:
                    return state
                state = returned
                continue
            state = session._drive_local(state)
            if state.status == "waiting_child" and state.active_handoff_id:
                continue
            if state.status in {"conversing", "executing"}:
                continue
            if root and state.status == "completed":
                self.store.complete(snapshot, status="completed")
                workflow_paths = [
                    f".auto-agents/state/workflows/{snapshot.workflow_id}",
                    ".auto-agents/state/workflows/active.json",
                    ".auto-agents/state/handoffs",
                ]
                if _head_contains_completed_session(
                    self.project_root, state.session_id
                ):
                    amend_only_paths(self.project_root, workflow_paths)
                else:
                    commit_only_paths(
                        self.project_root,
                        f"chore(workflow): finalize {snapshot.workflow_id}",
                        workflow_paths,
                    )
            elif root and state.status == "failed":
                snapshot.status = "failed"
                self.store.save(snapshot)
            elif state.status == "paused" and state.resolution == "interrupted_by_user":
                snapshot.status = "paused"
                snapshot.recovery_required = True
                self.store.save(snapshot)
                self.store.append_event(
                    snapshot,
                    "workflow_paused",
                    details={
                        "reason": "interrupted_by_user",
                        "active_frame": (
                            snapshot.active_frame.to_dict()
                            if snapshot.active_frame is not None
                            else None
                        ),
                    },
                )
            return state

    def _drive_handoff(self, parent_session: object, parent_state: object, snapshot: WorkflowSnapshot):
        handoff = self.store.load_handoff(parent_state.active_handoff_id)
        if handoff.returned_at:
            return self._apply_child_result(parent_state, handoff)
        self._ensure_handoff_checkpoint(snapshot, handoff)

        if handoff.target == "resume":
            result = self._resume_existing_child(handoff, snapshot)
        elif handoff.target == "fix":
            result = self._drive_fix_child(handoff, snapshot)
        elif handoff.target == "run":
            result = self._drive_run_child(handoff, snapshot)
        else:
            raise RuntimeError(f"unsupported handoff target: {handoff.target}")

        native_status = str(result.get("status", "failed"))
        if native_status in {"failed", "blocked"} and str(
            result.get("resolution", "")
        ) != "active_run_conflict":
            result["rolled_back_paths"] = self._rollback_handoff_uncommitted(
                snapshot,
                handoff,
            )
        self.store.record_result(snapshot, handoff, status=native_status, result=result)
        if native_status in {"paused", "waiting_user", "waiting_child"}:
            parent_state.status = "waiting_child"
            save_session_state(self.project_root, parent_state)
            return None

        operation_id = f"return-{handoff.handoff_id}-{uuid4().hex[:8]}"
        self.store.consume_result(snapshot, handoff, operation_id=operation_id)
        return self._apply_child_result(parent_state, handoff)

    def _handoff_checkpoint_root(
        self,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
    ) -> Path:
        return (
            self.store.workflow_root(snapshot.workflow_id)
            / "checkpoints"
            / handoff.handoff_id
        )

    def _ensure_handoff_checkpoint(
        self,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
    ) -> None:
        root = self._handoff_checkpoint_root(snapshot, handoff)
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            return
        entries = changed_entries(self.project_root, ignored_prefixes=())
        product_entries = [
            (status, path)
            for status, path in entries
            if not path.startswith(".auto-agents/")
            and not path.startswith(".antigravitycli/")
        ]
        files = root / "preexisting"
        for _status, relative in product_entries:
            _copy_path(self.project_root / relative, files / relative)
        index_path = _git_index_path(self.project_root)
        if index_path is not None and index_path.is_file():
            root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(index_path, root / "git-index.snapshot")
        write_json(
            manifest_path,
            {
                "schema_version": 1,
                "handoff_id": handoff.handoff_id,
                "head": head_ref(self.project_root),
                "preexisting_paths": [path for _status, path in product_entries],
                "preexisting_status": {
                    path: status for status, path in product_entries
                },
            },
        )
        self.store.append_event(
            snapshot,
            "handoff_checkpoint_created",
            operation_id=handoff.handoff_id,
            details={"preexisting_paths": [path for _status, path in product_entries]},
        )

    def _rollback_handoff_uncommitted(
        self,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
    ) -> list[str]:
        root = self._handoff_checkpoint_root(snapshot, handoff)
        manifest = read_json(root / "manifest.json", default={})
        if not isinstance(manifest, dict):
            raise RuntimeError(
                f"missing worktree checkpoint for failed handoff {handoff.handoff_id}"
            )
        preexisting = {
            str(item) for item in manifest.get("preexisting_paths", []) if str(item)
        }
        current = [
            path
            for _status, path in changed_entries(
                self.project_root, ignored_prefixes=()
            )
            if not path.startswith(".auto-agents/")
            and not path.startswith(".antigravitycli/")
        ]
        if not current:
            return []
        failure_root = root / "failed-candidate"
        for relative in current:
            _copy_path(self.project_root / relative, failure_root / relative)
        write_json(
            root / "failed-candidate.json",
            {
                "schema_version": 1,
                "head": head_ref(self.project_root),
                "paths": sorted(current),
            },
        )
        preimage_root = root / "preexisting"
        for relative in current:
            target = self.project_root / relative
            source = preimage_root / relative
            _remove_path(target)
            if relative in preexisting:
                _copy_path(source, target)
                continue
            tracked = subprocess.run(
                ["git", "cat-file", "-e", f"HEAD:{relative}"],
                cwd=str(self.project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
            if tracked.returncode == 0:
                restore = subprocess.run(
                    [
                        "git",
                        "restore",
                        "--source=HEAD",
                        "--staged",
                        "--worktree",
                        "--",
                        relative,
                    ],
                    cwd=str(self.project_root),
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                )
                if restore.returncode != 0:
                    raise RuntimeError(
                        restore.stderr.strip()
                        or f"failed to rollback child path {relative}"
                    )
        saved_index = root / "git-index.snapshot"
        if saved_index.is_file():
            _restore_index(self.project_root, saved_index, current)
        remaining = set(changed_paths(self.project_root))
        unexpected = sorted(path for path in remaining if path not in preexisting)
        if unexpected:
            raise RuntimeError(
                "child rollback left unowned worktree changes: "
                + ", ".join(unexpected[:10])
            )
        self.store.append_event(
            snapshot,
            "handoff_uncommitted_rollback",
            operation_id=handoff.handoff_id,
            details={"paths": sorted(current)},
        )
        return sorted(current)

    def _drive_fix_child(self, handoff: WorkflowHandoff, snapshot: WorkflowSnapshot) -> Dict[str, object]:
        from .session import Session

        session = Session(
            self.orch,
            mode="fix",
            print_agent_output=self.print_agent_output,
            full_verify=bool(self.full_verify and snapshot.root.kind == "fix"),
            auto_approve=self.auto_approve,
            health_runtime=self.health_runtime,
            coordinator=self,
        )
        state = self.start_seeded_session(session, snapshot=snapshot, handoff=handoff)
        return self._session_result(state, handoff)

    def _drive_run_child(self, handoff: WorkflowHandoff, snapshot: WorkflowSnapshot) -> Dict[str, object]:
        self.auto_approve = bool(
            self.auto_approve or handoff.payload.get("auto_approve", False)
        )
        current = load_run_state(self.project_root)
        existing_same_handoff = (
            str(current.resume_context.get("parent_handoff_id", ""))
            == handoff.handoff_id
        )
        if (
            handoff.child is None
            and current.status != "completed"
            and not existing_same_handoff
        ):
            return {
                "status": "blocked",
                "resolution": "active_run_conflict",
                "summary": (
                    f"Cannot start a routed iteration while run {current.run_id} "
                    f"is {current.status}."
                ),
                "run_id": current.run_id,
                "head_before": str(handoff.payload.get("head_before", "")),
                "head_after": head_ref(self.project_root),
                "changed_paths": [],
            }

        if handoff.child is None and existing_same_handoff:
            child = WorkflowRef("run", current.run_id)
            self.store.bind_child(snapshot, handoff, child)
            state = current
        elif handoff.child is None:
            seed = dict(handoff.payload.get("spec_seed", {}))
            self.store.append_event(
                snapshot,
                "operation_intent",
                operation_id=f"spec-{handoff.handoff_id}",
                details={"kind": "iteration_spec", "handoff_id": handoff.handoff_id},
            )
            spec = IterationSpecBuilder(self.project_root).materialize(handoff, seed)
            self.store.append_event(
                snapshot,
                "operation_completed",
                operation_id=f"spec-{handoff.handoff_id}",
                details=dict(spec),
            )
            state = self.orch._start_new_iteration(current)
            state.resume_context.update(
                {
                    "spec_file": str(self.project_root / spec["path"]),
                    "workflow_id": snapshot.workflow_id,
                    "parent_handoff_id": handoff.handoff_id,
                    "iteration_spec_sha256": spec["sha256"],
                    "iteration_spec_commit": spec["commit_sha"],
                    "auto_approve": self.auto_approve,
                    "print_agent_output": self.print_agent_output,
                }
            )
            save_run_state(self.project_root, state)
            child = WorkflowRef("run", state.run_id)
            self.store.bind_child(snapshot, handoff, child)
        else:
            state = load_run_state(self.project_root)

        if (
            handoff.child is not None
            and handoff.target != "resume"
            and state.status in {"completed", "failed", "blocked"}
        ):
            return self._run_result(state, handoff)

        if self.run_lock is not None:
            self.run_lock.bind_subject("run", state.run_id)
            self.orch._run_token = str(
                getattr(self.health_runtime, "run_token", "")
                or self.run_lock.run_token
            )
        if self.health_runtime is not None:
            self.health_runtime.bind_subject(state.run_id)
            self.health_runtime.set_phase("run")
        self.orch._force_full_verify = False
        context = dict(state.resume_context)
        spec_file = Path(str(context.get("spec_file", self.project_root / "spec.md")))
        try:
            result_state = self.orch.run(
                spec_file=spec_file,
                auto_approve=self.auto_approve,
                print_agent_output=self.print_agent_output,
                provider_kind=None,
                autonomy_mode=getattr(self.orch, "_autonomy_mode", None),
            )
        except RuntimeError as error:
            result_state = load_run_state(self.project_root)
            if result_state.status not in {"blocked", "waiting_user", "paused"}:
                result_state.status = "failed"
                result_state.last_error = str(error)
                save_run_state(self.project_root, result_state)
        return self._run_result(result_state, handoff)

    def _resume_existing_child(
        self,
        handoff: WorkflowHandoff,
        snapshot: WorkflowSnapshot,
    ) -> Dict[str, object]:
        resume_id = str(handoff.payload.get("resume_handoff_id", ""))
        if not resume_id:
            raise RuntimeError("resume handoff requires resume_handoff_id")
        original = self.store.load_handoff(resume_id)
        if original.child is None:
            raise RuntimeError(f"handoff {resume_id} has no child to resume")
        if original.child.kind == "run":
            handoff.child = original.child
            self.store.save_handoff(handoff)
            return self._drive_run_child(handoff, snapshot)
        if original.child.kind == "fix":
            from .session import Session

            handoff.child = original.child
            self.store.save_handoff(handoff)
            session = Session(
                self.orch,
                mode="fix",
                print_agent_output=self.print_agent_output,
                full_verify=bool(
                    self.full_verify and snapshot.root.kind == "fix"
                ),
                auto_approve=self.auto_approve,
                health_runtime=self.health_runtime,
                coordinator=self,
            )
            state = self._drive_session(
                session,
                load_session_state(self.project_root, original.child.native_id),
                snapshot,
                root=False,
            )
            return self._session_result(state, handoff)
        raise RuntimeError(f"unsupported resumable child kind: {original.child.kind}")

    def _apply_child_result(self, parent_state: object, handoff: WorkflowHandoff):
        result = dict(handoff.result)
        changed = {
            str(item) for item in parent_state.lineage_changed_paths if str(item).strip()
        }
        changed.update(
            str(item) for item in result.get("changed_paths", []) if str(item).strip()
        )
        parent_state.lineage_changed_paths = sorted(changed)
        parent_state.lineage_head_ref = str(result.get("head_after", "")) or parent_state.lineage_head_ref
        parent_state.last_child_result_ref = str(self.store.handoff_path(handoff.handoff_id))
        parent_state.active_handoff_id = ""
        parent_state.return_phase = "after_child"
        parent_state.status = "executing"
        parent_state.attempt_epoch += 1
        parent_state.attempts_since_progress = 0
        parent_state.consecutive_agent_errors = 0
        summary = str(result.get("summary") or result.get("resolution") or result.get("status", ""))
        parent_state.execution_log.append(
            {
                "attempt": parent_state.current_attempt,
                "attempt_epoch": parent_state.attempt_epoch,
                "action": "child_returned",
                "result": summary[:500],
                "handoff_id": handoff.handoff_id,
                "child_status": str(result.get("status", "")),
                "timestamp": parent_session_now(),
            }
        )
        parent_state.conversation.append(
            {
                "role": "agent",
                "content": (
                    f"Child workflow {handoff.target} returned handoff_id={handoff.handoff_id} with status "
                    f"{result.get('status')}: {summary}"
                ),
            }
        )
        save_session_state(self.project_root, parent_state)
        if self.run_lock is not None:
            self.run_lock.bind_subject(parent_state.mode, parent_state.session_id)
        if self.health_runtime is not None:
            self.health_runtime.bind_subject(parent_state.session_id)
            self.health_runtime.set_phase(parent_state.mode)
        return parent_state

    def _session_result(self, state: object, handoff: WorkflowHandoff) -> Dict[str, object]:
        before = str(handoff.payload.get("head_before", ""))
        after = head_ref(self.project_root)
        return {
            "status": state.status,
            "resolution": state.resolution,
            "summary": state.resolution or f"{state.mode} status={state.status}",
            "session_id": state.session_id,
            "state_ref": str(
                Path(".auto-agents") / "state" / "sessions" / state.session_id / "session_state.json"
            ),
            "head_before": before,
            "head_after": after,
            "commit_shas": _commits_between(self.project_root, before, after),
            "changed_paths": _paths_between(self.project_root, before, after),
            "failure_fingerprint": _failure_fingerprint(state.status, state.resolution),
        }

    def _run_result(self, state: object, handoff: WorkflowHandoff) -> Dict[str, object]:
        before = str(handoff.payload.get("head_before", ""))
        after = head_ref(self.project_root)
        return {
            "status": state.status,
            "resolution": "run_completed" if state.status == "completed" else "run_incomplete",
            "summary": state.last_error or f"run {state.run_id} status={state.status}",
            "run_id": state.run_id,
            "state_ref": str(Path(".auto-agents") / "state" / "run_state.json"),
            "head_before": before,
            "head_after": after,
            "commit_shas": _commits_between(self.project_root, before, after),
            "changed_paths": _paths_between(self.project_root, before, after),
            "failure_fingerprint": _failure_fingerprint(state.status, state.last_error),
        }

    def _resume_run_root(self, snapshot: WorkflowSnapshot):
        state = load_run_state(self.project_root)
        if state.status != "completed":
            state = self.orch.resume_saved_run()
        if state.status == "completed":
            self.store.complete(snapshot, status="completed")
            if head_ref(self.project_root):
                amend_only_paths(
                    self.project_root,
                    [
                        f".auto-agents/state/workflows/{snapshot.workflow_id}",
                        ".auto-agents/state/workflows/active.json",
                    ],
                )
        return state


def parent_session_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _paths_between(project_root: Path, before: str, after: str) -> list[str]:
    if not before or not after or before == after:
        return list(changed_paths(project_root))
    process = subprocess.run(
        ["git", "diff", "--name-only", f"{before}..{after}"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        return list(changed_paths(project_root))
    return sorted(
        {
            line.strip()
            for line in process.stdout.splitlines()
            if line.strip()
            and not line.strip().startswith(".auto-agents/")
            and not line.strip().startswith(".antigravitycli/")
        }
    )


def _commits_between(project_root: Path, before: str, after: str) -> list[str]:
    if not before or not after or before == after:
        return []
    process = subprocess.run(
        ["git", "rev-list", "--reverse", f"{before}..{after}"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        return []
    return [line.strip() for line in process.stdout.splitlines() if line.strip()]


def _failure_fingerprint(status: str, detail: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"status": str(status), "detail": " ".join(str(detail).split())},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]


def _head_contains_completed_session(project_root: Path, session_id: str) -> bool:
    relative = f".auto-agents/state/sessions/{session_id}/session_state.json"
    process = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        return False
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        return False
    return bool(
        isinstance(payload, dict)
        and str(payload.get("session_id", "")) == session_id
        and str(payload.get("status", "")) == "completed"
    )


def _find_operation_commit(project_root: Path, operation_id: str) -> str:
    process = subprocess.run(
        [
            "git",
            "log",
            "--all",
            "-100",
            "--format=%H%x00%B%x00%x00",
        ],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        return ""
    marker = f"Auto-Agents-Operation: {operation_id}"
    for record in process.stdout.split("\x00\x00"):
        commit_sha, separator, body = record.partition("\x00")
        if separator and marker in body:
            return commit_sha.strip()
    return ""


def _copy_path(source: Path, target: Path) -> None:
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.exists() or source.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _git_index_path(project_root: Path) -> Optional[Path]:
    process = subprocess.run(
        ["git", "rev-parse", "--git-path", "index"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if process.returncode != 0:
        return None
    path = Path(process.stdout.strip())
    return path if path.is_absolute() else project_root / path


def _restore_index(project_root: Path, saved_index: Path, paths: list[str]) -> None:
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = str(saved_index)
    for relative in paths:
        lookup = subprocess.run(
            ["git", "ls-files", "--stage", "--", relative],
            cwd=str(project_root),
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        if lookup.returncode != 0:
            raise RuntimeError(
                lookup.stderr.strip() or f"could not inspect saved index for {relative}"
            )
        stage_zero = None
        for line in lookup.stdout.splitlines():
            metadata, separator, entry_path = line.partition("\t")
            fields = metadata.split()
            if separator and entry_path == relative and len(fields) == 3 and fields[2] == "0":
                stage_zero = (fields[0], fields[1])
                break
        if stage_zero is None:
            update = subprocess.run(
                ["git", "update-index", "--force-remove", "--", relative],
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
        else:
            update = subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    stage_zero[0],
                    stage_zero[1],
                    relative,
                ],
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                capture_output=True,
            )
        if update.returncode != 0:
            raise RuntimeError(
                update.stderr.strip() or f"could not restore index for {relative}"
            )
