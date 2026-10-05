from __future__ import annotations
from .business_state import entry as kernel_entry

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

from .authorization import authorization_policy_for_state
from .config import (
    create_session,
    load_run_state,
    load_session_state,
    save_run_state,
    save_session_state,
    save_task_plan,
    task_plan_path,
)
from .git_ops import (
    amend_only_paths,
    changed_entries,
    changed_paths,
    commit_only_paths,
    head_ref,
)
from .io_utils import read_json, write_json
from .models import RunState
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
        self._preserve_engine_resume_budget = False

    def _create_session(self, session: object, *, workflow_id='', parent_handoff_id='', session_id=None, persist=True):
        state = create_session(
            self.project_root,
            session.mode,
            hard_ceiling=session.config.execution.session_limits.for_mode(
                session.mode
            ),
            workflow_id=workflow_id,
            parent_handoff_id=parent_handoff_id,
            session_id=session_id,
            persist=persist,
        )

        session._fresh_session_id = state.session_id
        return state

    def _bind_focused_seed_command(self, state, handoff, *, recovering=False):
        """Bind only the command recorded by this child's original focused handoff."""
        from .session_verification import ownership_error, preimplementation_failure

        seed = handoff.payload.get('issue_seed', {})
        if not isinstance(seed, dict) or not isinstance(seed.get('verification_scope'), dict) or seed['verification_scope'].get('mode') != 'focused_fix':
            return
        if (state.mode != 'fix' or state.workflow_id != handoff.workflow_id
                or state.parent_handoff_id != handoff.handoff_id
                or handoff.child != WorkflowRef('fix', state.session_id)
                or handoff.payload.get('child_session_id') != state.session_id
                or state.source_descriptor != dict(handoff.payload.get('source_descriptor', {}))):
            raise ownership_error(state, 'focused fix child identity conflicts with seeded handoff')
        try:
            issue = read_json(self.project_root / '.auto-agents/state/sessions'
                              / state.session_id / 'issue.json', default={})
        except (OSError, ValueError, TypeError) as error:
            raise ownership_error(state, 'focused fix issue is unreadable') from error
        command = seed.get('verification_command', '')
        if not isinstance(command, str):
            raise ownership_error(state, 'focused fix handoff command is malformed')
        routed_command = handoff.payload.get('verification_command')
        if routed_command is not None and routed_command != command:
            raise ownership_error(state, 'focused fix handoff declares conflicting verification commands')
        if any(handoff.payload.get(key) for key in ('task_id', 'task_ids', 'requirement_ids')) or any(
                source.get('task_id') or source.get('task_ids') for source in (seed, issue if isinstance(issue, dict) else {})):
            raise ownership_error(state, 'focused fix conflicts with explicit task adoption')
        if issue and (not isinstance(issue, dict) or issue.get('source_handoff_id') != handoff.handoff_id
                or issue.get('issue_id') != f'issue-{state.session_id}'
                or issue.get('verification_scope') != seed.get('verification_scope')
                or issue.get('verification_command') != command.strip()
                or any(issue.get(key) != seed.get(key) for key in
                       ('task_id', 'task_ids', 'requirement_ids', 'retained_task_relation'))):
            raise ownership_error(state, 'focused fix issue conflicts with original handoff command or source')
        if state.fix_verify_command:
            if command.strip() and state.fix_verify_command != command.strip():
                raise ownership_error(state, 'focused fix command conflicts with original handoff')
            return
        if not command.strip():
            return  # The existing scope preflight rejects a missing command.
        if not issue:
            raise ownership_error(state, 'focused fix issue is unavailable for command recovery')
        if (state.verification_binding or state.candidate_custody or state.candidate_paths
                or state.lineage_changed_paths or state.current_attempt
                or state.persistence_actions
                or (recovering and preimplementation_failure(state) is None)):
            raise ownership_error(state, 'focused fix command cannot be restored after implementation')
        state.fix_verify_command = command.strip()
        save_session_state(self.project_root, state)

    @staticmethod
    def _engine_blocker_error(state: object) -> RuntimeError | None:
        blocker = (
            dict(getattr(state, "active_blocker", {}) or {})
            if isinstance(getattr(state, "active_blocker", {}), dict)
            else {}
        )
        if (
            str(getattr(state, "status", "")) == "blocked"
            and str(blocker.get("owner", "")) == "auto_agents"
        ):
            return RuntimeError(
                "auto_agents engine self-repair required before workflow "
                f"routing: category={blocker.get('category', 'auto_agents_error')} "
                f"reason={blocker.get('reason', getattr(state, 'last_error', ''))}"
            )
        return None

    def _apply_authorization_policy(
        self,
        state: object,
        payload: object = None,
    ) -> None:
        existing = (
            dict(payload)
            if isinstance(payload, dict)
            else dict(getattr(state, "authorization_policy", {}) or {})
        )
        policy = authorization_policy_for_state(
            auto_approve=bool(self.auto_approve or getattr(state, "auto_approve", False)),
            payload=existing,
        )
        state.authorization_policy = policy.to_dict()

    def start_session(self, session: object):
        active = self.store.active()
        if (
            active is not None
            and active.root.kind == "run"
            and session.mode == "provider_resolve"
        ):
            state = self._create_session(session,workflow_id=active.workflow_id)
            state.workflow_id = active.workflow_id
            state.auto_approve = bool(self.auto_approve)
            self._apply_authorization_policy(state)
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
        self._apply_authorization_policy(state)
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
        from .session_verification import SessionOwnershipError

        child_id = str(handoff.payload.get("child_session_id", "")).strip()
        retained_child = False
        if child_id:
            try:
                state = load_session_state(self.project_root, child_id)
                retained_child = True
            except FileNotFoundError:
                state = self._create_session(session,workflow_id=snapshot.workflow_id,parent_handoff_id=handoff.handoff_id,
                                             session_id=child_id,persist=False)
                handoff.payload["child_session_id"] = state.session_id
                self.store.save_handoff(handoff)
        else:
            state = self._create_session(session,workflow_id=snapshot.workflow_id,parent_handoff_id=handoff.handoff_id,persist=False)
            handoff.payload["child_session_id"] = state.session_id
            self.store.save_handoff(handoff)
        if retained_child:
            from .session_verification import ownership_error
            expected = {'workflow_id': snapshot.workflow_id, 'parent_handoff_id': handoff.handoff_id,
                        'mode': session.mode, 'source_descriptor': dict(handoff.payload.get('source_descriptor', {}))}
            for key in ('authorization_policy', 'goal_execution_environment'):
                if handoff.payload.get(key):
                    expected[key] = handoff.payload[key]
            if any(getattr(state, key) != value for key, value in expected.items()):
                return session._block_execution_binding(state, ownership_error(
                    state, 'retained child identity conflicts with seeded handoff'), 'verification_ownership')
            try:
                self._bind_focused_seed_command(state, handoff, recovering=True)
            except SessionOwnershipError as error:
                return session._block_execution_binding(state, error, 'verification_ownership')
            if not session._retain_resume_authority(state):
                return state
            return self._drive_session(session, state, snapshot, root=False)
        self.auto_approve = bool(
            self.auto_approve or handoff.payload.get("auto_approve", False)
        )
        session._auto_approve = self.auto_approve
        state.workflow_id = snapshot.workflow_id
        state.parent_handoff_id = handoff.handoff_id
        source = dict(handoff.payload.get('source_descriptor', {}))
        if state.source_descriptor and state.source_descriptor != source:
            raise RuntimeError('retained child source conflicts with handoff')
        state.source_descriptor = source
        state.goal = handoff.goal
        self._apply_authorization_policy(
            state,
            handoff.payload.get("authorization_policy", {}),
        )
        raw_goal_environment = handoff.payload.get(
            "goal_execution_environment",
            {},
        )
        if isinstance(raw_goal_environment, dict) and raw_goal_environment:
            state.goal_execution_environment = dict(raw_goal_environment)
        if not state.conversation and state.goal:
            state.conversation.append({"role": "user", "content": state.goal})
        state.auto_approve = bool(self.auto_approve)
        state.full_verify = bool(session._full_verify)
        if not state.lineage_head_ref:
            # A retained handoff checkpoint is recovery evidence; today's
            # ambient HEAD is not provenance for an existing child.
            state.lineage_head_ref = str(handoff.payload.get("head_before", "")) or (
                "" if retained_child else head_ref(self.project_root))
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
            try:
                self._bind_focused_seed_command(state, handoff)
            except SessionOwnershipError as error:
                return session._block_execution_binding(state, error, 'verification_ownership')
        return self._drive_session(session, state, snapshot, root=False)

    def resume_session(self, session: object, session_id: str):
        state = load_session_state(self.project_root, session_id)
        with session._resume_authority_context(state):
            return self._resume_session_state(session, state)

    def _resume_session_state(self, session, state):
        session_id = state.session_id
        if state.mode != session.mode:
            raise ValueError(
                f"session {session_id} is {state.mode}, not {session.mode}"
            )
        from .artifact_workflow import archived_session
        if archived_session(self.project_root, state):
            return state
        authority_valid = session._retain_resume_authority(state)
        registration = getattr(self.orch, '_repair_registration', None)
        if authority_valid and registration and state.workflow_id:
            root = self.store.load(state.workflow_id).root
            from .proof_support.incidents import migrate
            migrate(registration['config'], {'project': str(self.project_root), 'invocation': {
                'session_id' if root.kind in {'collab', 'fix', 'provider_resolve'} else 'run_id': root.native_id}})
        engine_resume = os.environ.get('AUTO_AGENTS_MAINTENANCE_RESUME') == '1'
        from .business_state import installed
        kernel_managed = installed(self.project_root) is not None
        self._preserve_engine_resume_budget = bool(engine_resume or kernel_managed)
        # Retain explicitly requested policies even when missing authority
        # blocks execution. Policy persistence cannot supply that authority.
        self.auto_approve = bool(state.auto_approve if engine_resume else self.auto_approve or state.auto_approve)
        if not engine_resume:
            self._apply_authorization_policy(state)
        self.full_verify = bool(state.full_verify if engine_resume else self.full_verify or state.full_verify)
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
        if not authority_valid:
            return state
        resume_state_changed = False
        from .session_acceptance import resumable_blocker
        if (not engine_resume and state.status != "completed"
                and not state.candidate_custody.get("receipt")):
            resume_state_changed = session._invalidate_provider_continuations(
                state,
                reason="process-level session resume uses the durable transcript",
            )
            if not (
                state.status == "failed"
                or resumable_blocker(state)
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
        pass
        return self._drive_session(session, state, snapshot, root=True)



    def _retained_proof_child(self, payload, snapshot):
        """A current product proof amendment is not an engine repair request."""
        from .proof_amendments import pending
        child_id = str((payload.get("source_candidate") or {}).get("session_id", ""))
        if not child_id:
            return None
        try:
            child = load_session_state(self.project_root, child_id)
        except FileNotFoundError:
            return None  # Missing custody is not an amendment; keep the engine binding check.
        return child if pending(child) else None

    def _returned_blocked_handoff(self, state, snapshot):
        """Resolve only this parent's durable blocked return, without changing it."""
        if (state.status != "blocked" or state.resolution not in {
                'execution_binding_mismatch', 'verification_ownership', 'verification_execution_binding'}
                or state.active_handoff_id or not state.last_child_result_ref):
            return
        reference = Path(state.last_child_result_ref)
        # Diagnostic copies and moved projects retain the original absolute
        # receipt path. Resolve its project-relative identity in this store;
        # never read the original project to authorize a copied workflow.
        if reference.parts[-4:] != (".auto-agents", "state", "handoffs", reference.stem + ".json"):
            return
        handoff = self.store.load_handoff(reference.stem)
        if (handoff.handoff_id != reference.stem
                or handoff.workflow_id != snapshot.workflow_id
                or handoff.parent != WorkflowRef(state.mode, state.session_id)
                or not handoff.returned_at or handoff.status != "blocked"
                or handoff.result.get("resolution") != state.resolution):
            return
        return handoff


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
            self._inherit_root_policies(snapshot)
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

    @kernel_entry
    def resume_workflow(self, workflow_id: str):
        snapshot = self.store.load(workflow_id)
        if snapshot.status == 'completed' and snapshot.root.kind != 'run':
            from .artifact_workflow import archived_session
            state = load_session_state(self.project_root, snapshot.root.native_id)
            if archived_session(self.project_root, state):
                return state
        self.store.activate(workflow_id)
        self._reconcile_open_operations(snapshot)
        self.store.begin_resume(snapshot)
        root = snapshot.root
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
            with session._resume_authority_context(state):
                if not session._retain_resume_authority(state):
                    return state
                self._inherit_root_policies(snapshot, root_session=state)
                session._auto_approve = self.auto_approve
                session._full_verify = self.full_verify
                self.orch._force_full_verify = bool(self.full_verify and session.mode == "fix")
                self._ensure_completed_session_commit(session, state)
                snapshot = self.store.load(snapshot.workflow_id)
                return self._drive_session(
                    session,
                    state,
                    snapshot,
                    root=True,
                )
        self._inherit_root_policies(snapshot)
        return self._resume_run_root(snapshot)

    def _inherit_root_policies(self, snapshot: WorkflowSnapshot, *, root_session=None) -> None:
        """Restore durable approval and verification policies from the root."""

        inherited = False
        inherited_full_verify = False
        if snapshot.root.kind in {"collab", "fix", "provider_resolve"}:
            try:
                if root_session is None:
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
            self._apply_authorization_policy(root_session)
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

    def _ensure_completed_session_commit(self, session: object, state: object) -> None:
        from .session_verification import SessionOwnershipError
        try:
            self._ensure_completed_session_commit_owned(session, state)
        except SessionOwnershipError as error:
            session._block_execution_binding(state, error, 'verification_ownership')

    def _ensure_completed_session_commit_owned(
        self,
        session: object,
        state: object,
    ) -> None:
        if state.status != "completed" or state.mode not in {"fix", "collab"}:
            return
        from contextlib import nullcontext
        from .session_candidate import completed_delivery, execution_checkout

        # Completion recovery runs before _drive_session. Resolve its durable
        # source inside custody here too; shared HEAD is not its commit log.
        context = execution_checkout(session, state) if state.candidate_custody else nullcontext()
        with context:
            if state.status != "completed":
                return
            if state.mode == 'collab' and state.acceptance_execution.get('phase') == 'completed':
                from .session_acceptance import completed
                if completed(session, state):
                    return
                from .session_verification import ownership_error
                raise ownership_error(state, '已保存的验收证据不完整或发生变化，不能复用完成结果。')
            def committed():
                return (bool(state.candidate_custody) and completed_delivery(state)
                        or _head_contains_completed_session(session.project_root, state.session_id))
            if committed():
                return
            session._coordinator = self
            session._coordinator_managed = True
            session._git_commit(state, state.mode)
            if not committed():
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
        if (getattr(session, "_fresh_session_id", None) != state.session_id
                and not session._retain_resume_authority(state)):
            return state
        session._coordinator = self
        session._coordinator_managed = True
        from .session_acceptance import begin_recovery
        # Explicit resume shares the automatic diagnosis boundary without
        # replaying acceptance execution or resetting its budget.
        begin_recovery(session, state)
        if state.status == "failed":
            session._invalidate_provider_continuations(
                state,
                reason="failed session started a fresh durable resume boundary",
            )
            if not state.candidate_custody.get("receipt") and not self._preserve_engine_resume_budget:
                if state.mode == 'fix' and state.current_attempt:
                    # A crash can persist the attempt counter before the
                    # writer result/receipt. A new local epoch must not erase
                    # that evidence and manufacture a zero-candidate exit.
                    state.execution_log.append({'action': 'implementation_attempts_retained',
                        'attempt': state.current_attempt, 'timestamp': parent_session_now()})
                state.current_attempt = 0
                session._begin_attempt_epoch(state, reason="failed session resumed")
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
            if not state.candidate_custody.get("receipt") and not self._preserve_engine_resume_budget:
                session._begin_attempt_epoch(state, reason="interrupted session resumed")
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
            if root and state.status == "completed" and state.candidate_custody:
                # The private commit appended operation receipts through the
                # shared coordinator. Preserve that journal head on completion.
                snapshot = self.store.load(snapshot.workflow_id)
                self.store.complete(snapshot, status="completed")
            elif root and state.status == "completed":
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
            elif root and state.status in {"failed", "blocked"}:
                snapshot.status = state.status
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
        chain = self._resolved_handoff_chain(parent_state.active_handoff_id, snapshot.workflow_id)
        handoff = chain[0]
        if handoff.parent != WorkflowRef(parent_state.mode, parent_state.session_id):
            from .session_verification import SessionOwnershipError
            raise SessionOwnershipError('resumed handoff belongs to another parent session')
        binding_payload = chain[-1].payload
        from .execution_binding import repository_binding_error
        if (repository_binding_error(self.project_root, binding_payload)
                and self._retained_proof_child(binding_payload, snapshot)):
            if not handoff.returned_at:
                self.store.record_result(snapshot, handoff, status='paused', result={
                    'status': 'paused', 'resolution': 'proof_review_migrated',
                    'summary': '当前候选需要测试修订审核，继续原产品子流程。'})
            self.store.consume_result(snapshot, handoff, operation_id='proof-review-' + handoff.handoff_id)
            parent_state.active_handoff_id = ''
            parent_session._prepare_workflow_handoff(parent_state, target='fix',
                reason='继续审核原候选', payload=binding_payload)
            return None
        if handoff.returned_at:
            return self._apply_child_result(parent_state, handoff)
        # A saved digest describes a route; it is not a verified repair receipt.
        blocked = self._execution_binding_result(binding_payload)
        if blocked:
            # Reject legacy/restored foreign handoffs before checkpoints,
            # rollback, ambient run recovery, or a new provider call.
            self.store.record_result(snapshot, handoff, status=str(blocked["status"]), result=blocked)
            if blocked["status"] in {"paused", "waiting_user", "waiting_child"}:
                parent_state.status = "waiting_child"
                save_session_state(self.project_root, parent_state)
                return None
            self.store.consume_result(snapshot, handoff, operation_id=f"binding-{handoff.handoff_id}")
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

        if result.get('status') == 'completed' and result.get('candidate_ownership') == 'unknown':
            # A native delegate may finish without returning an owned result.
            # Preserve that native status at the dispatch API, but do not
            # consume it as a completed workflow handoff without ownership.
            result.update(status='blocked', resolution='verification_ownership',
                          summary=str(result.get('ownership_diagnostic', {}).get('reason')
                                      or 'child completion has no bound ownership'))
        native_status = str(result.get("status", "failed"))
        discard_child_mutations = bool(result.get("discard_child_mutations", False))
        if discard_child_mutations or (
            native_status in {"failed", "blocked"}
            and str(result.get("resolution", "")) != "active_run_conflict"
        ):
            self._finish_failed_handoff(snapshot, handoff, result)
            native_status = str(result['status'])
        self.store.record_result(snapshot, handoff, status=native_status, result=result)
        if native_status in {"paused", "waiting_user", "waiting_child"}:
            parent_state.status = "waiting_child"
            save_session_state(self.project_root, parent_state)
            return None

        operation_id = f"return-{handoff.handoff_id}-{uuid4().hex[:8]}"
        self.store.consume_result(snapshot, handoff, operation_id=operation_id)
        return self._apply_child_result(parent_state, handoff)

    def _resolved_handoff_chain(self, handoff, workflow_id):
        from .session_verification import SessionOwnershipError
        try:
            return self.store.resolve_handoff_chain(handoff, workflow_id=workflow_id)
        except (OSError, ValueError, TypeError, KeyError) as error:
            diagnostic = {'workflow_id': workflow_id,
                          'handoff_id': handoff if isinstance(handoff, str) else handoff.handoff_id,
                          'retry_fix': False, **getattr(error, 'diagnostic', {})}
            identity = diagnostic.get('session_id', '')
            if isinstance(identity, str) and identity and all(c.isalnum() or c in '-_' for c in identity):
                try:
                    candidate = self.project_root / '.auto-agents/state/sessions' / identity / 'session_state.json'
                    if not candidate.resolve().is_relative_to(self.project_root.resolve()):
                        raise ValueError('diagnostic session leaves its project')
                    retained = load_session_state(self.project_root, diagnostic['session_id'])
                    if retained.workflow_id == workflow_id and retained.parent_handoff_id == diagnostic['handoff_id']:
                        diagnostic['contract_fingerprint'] = retained.verification_binding.get('contract_fingerprint', '')
                except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
                    pass  # Diagnostic enrichment never grants execution authority.
            raise SessionOwnershipError('original child handoff is unavailable or conflicting: ' + str(error),
                                        diagnostic=diagnostic) from error

    def _validated_child_handoff(self, state, handoff):
        from .session_verification import ownership_error
        chain = self._resolved_handoff_chain(handoff, state.workflow_id)
        original = chain[-1]
        if (original.target != 'fix' or original.child != WorkflowRef('fix', state.session_id)
                or original.workflow_id != state.workflow_id
                or original.handoff_id != state.parent_handoff_id
                or original.payload.get('child_session_id', state.session_id) != state.session_id):
            raise ownership_error(state, 'child exit identity conflicts with its handoff')
        from .execution_binding import route_sources
        for entry in chain:
            for source in route_sources(entry.payload):
                for key in ('authorization_policy', 'goal_execution_environment', 'auto_approve'):
                    if key in source and source[key] != getattr(state, key):
                        raise ownership_error(state, 'child handoff has conflicting ' + key,
                                              conflicting_handoff_id=entry.handoff_id)
        return original

    def _handoff_exit_ownership(self, state, handoff):
        from .session_verification import (
            _validate_binding_identity, ownership_error, preimplementation_exit,
        )
        self._validated_child_handoff(state, handoff)
        if state.verification_binding:
            _validate_binding_identity(self.orch, state)
        if state.candidate_custody:
            from .session_source import validate_checkout
            from .session_candidate import validate_receipt
            if not state.verification_binding:
                raise ownership_error(state, 'candidate custody has no verification authority')
            try:
                validate_checkout(self.project_root, state, Path(state.candidate_custody['checkout']))
                validate_receipt(state)
            except (KeyError, TypeError, ValueError, OSError) as error:
                raise ownership_error(state, 'candidate custody is unavailable or malformed') from error
            return 'private'
        if preimplementation_exit(state) is not None:
            return 'none'
        raise ownership_error(state, 'no child-owned receipt authorizes shared rollback',
                              conflicting_paths=sorted(state.candidate_paths))

    def _finish_failed_handoff(self, snapshot, handoff, result):
        from .session_verification import SessionOwnershipError
        try:
            if handoff.child is not None and handoff.child.kind == 'fix':
                try:
                    state = load_session_state(self.project_root, handoff.child.native_id)
                except (OSError, ValueError) as error:
                    raise SessionOwnershipError('child ownership is unavailable; refusing shared rollback',
                        diagnostic={'session_id': handoff.child.native_id,
                                    'handoff_id': handoff.handoff_id, 'retry_fix': False}) from error
                ownership = self._handoff_exit_ownership(state, handoff)
                result['candidate_ownership'] = ownership
                if ownership == 'none':
                    result.update(changed_paths=[], commit_shas=[], head_after='', rolled_back_paths=[])
                    return
            result['rolled_back_paths'] = self._rollback_handoff_uncommitted(snapshot, handoff)
        except SessionOwnershipError as error:
            # Keep the child's original structured failure. A rollback refusal
            # is a second failure, never a replacement for that diagnostic.
            if result.get('status') not in {'failed', 'blocked'}:
                result.update(resolution='verification_ownership', summary=str(error))
            result.update(status='blocked', rolled_back_paths=[], candidate_ownership='unknown',
                          rollback_diagnostic={'reason': str(error), **error.diagnostic})

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
        if handoff.child is not None and handoff.child.kind == 'fix':
            from .session_verification import SessionOwnershipError
            try:
                child_state = load_session_state(self.project_root, handoff.child.native_id)
            except (OSError, ValueError) as error:
                raise SessionOwnershipError('child ownership is unavailable; refusing shared rollback',
                    diagnostic={'session_id': handoff.child.native_id,
                                'handoff_id': handoff.handoff_id, 'retry_fix': False}) from error
            # A proved empty exit needs no rollback. A validated private
            # candidate stays in custody and never authorizes shared copyback.
            self._handoff_exit_ownership(child_state, handoff)
            return []
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
        owned = None
        if not current or handoff.child is None:
            return []
        if owned is None:
            from .session_verification import SessionOwnershipError
            raise SessionOwnershipError("no child-owned receipt authorizes shared rollback",
                diagnostic={"handoff_id": handoff.handoff_id, "retry_fix": False})
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
        unexpected = sorted(path for path in remaining if path not in preexisting
                            and (owned is None or path in owned))
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
        if owned is not None:
            child_state.candidate_paths = {}
            save_session_state(self.project_root, child_state)
        return sorted(current)



    def _drive_fix_child(self, handoff: WorkflowHandoff, snapshot: WorkflowSnapshot) -> Dict[str, object]:
        from .session import Session

        blocked = self._execution_binding_result(handoff.payload)
        if blocked:
            return blocked

        current = load_run_state(self.project_root)
        if False:
            save_run_state(self.project_root, current)
            blocker = (
                dict(current.active_blocker)
                if isinstance(current.active_blocker, dict)
                else {}
            )
            return {
                "status": "completed",
                "resolution": "installed_engine_recovery_prepared",
                "summary": (
                    "The installed auto_agents revision contains the approved "
                    "self-repair and reopened the blocked run through its durable "
                    "retry lifecycle."
                ),
                "run_id": current.run_id,
                "self_repair_commit": str(
                    blocker.get("self_repair_commit", "")
                ),
                "head_before": str(handoff.payload.get("head_before", "")),
                "head_after": head_ref(self.project_root),
                "changed_paths": [],
                "discard_child_mutations": handoff.child is not None,
            }
        engine_error = self._engine_blocker_error(current)
        if engine_error is not None:
            raise engine_error

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

    def _execution_binding_result(self, payload):
        from .execution_binding import repository_binding_error
        from .engine_fault import request_engine_repair
        error = repository_binding_error(self.project_root, payload)
        if error:
            request_engine_repair(self.orch, payload)
        return {'status':'blocked','resolution':'execution_binding_mismatch','summary':error,'retry_fix':False,'changed_paths':[]} if error else {}

    def _unstarted_run(self, current: RunState) -> bool:
        """A pristine placeholder has no execution or workflow owner to resume."""
        if current.to_dict() != RunState(current.run_id, workflow_version=current.workflow_version).to_dict():
            return False
        base = self.project_root / '.auto-agents/state'
        try:
            plan = read_json(base / 'task_plan.json', default={})
            if not isinstance(plan, dict) or plan.get('tasks'):
                return False
            # Ownership can exist outside the run projection, including after
            # a crash before resume_context was saved. Never adopt such a run.
            reference = WorkflowRef('run', current.run_id).to_dict()
            for path in [*(base / 'workflows').glob('*/workflow.json'),
                         *(base / 'handoffs').glob('*.json')]:
                if path.is_symlink():
                    return False
                row = read_json(path)
                if not isinstance(row, dict) or any(row.get(key) == reference
                        for key in ('root', 'active_frame', 'parent', 'child')):
                    return False
            run_root = self.project_root / '.auto-agents/runs' / current.run_id
            if run_root.exists() or run_root.is_symlink():
                return False
        except (OSError, ValueError, RuntimeError):
            return False
        return True

    def recover_unstarted_run_route(self, session, state):
        """Replay a controller-refused route after fixing placeholder admission.

        No new provider turn or route permit is needed to consume this retained
        response. A later user message or child entry supersedes the refusal.
        """
        if (state.active_handoff_id or state.acceptance_execution
                and state.acceptance_execution.get('phase') not in {'blocked', 'completed'}):
            return None
        if not self._unstarted_run(load_run_state(self.project_root)):
            return None
        refusal = next((row for row in reversed(state.execution_log)
                        if row.get('action') in {'run_route_deferred', 'run_route_preflight_passed', 'child_returned'}), None)
        if not refusal or refusal.get('action') != 'run_route_deferred':
            return None
        current = load_run_state(self.project_root)
        expected = f'run {current.run_id} remains pending; no new run handoff was created'
        if refusal.get('result') != expected:
            return None
        for index in range(len(state.conversation) - 1, 0, -1):
            message = state.conversation[index]
            if message.get('role') == 'user':
                return None
            if message.get('role') != 'orchestrator' or message.get('content') != expected:
                continue
            previous = state.conversation[index - 1]
            if previous.get('role') not in {'agent', 'assistant'}:
                return None
            route, error = session._parse_workflow_route(previous.get('content', ''))
            if error or not route or route.get('target') != 'run':
                return None
            routed, error = session._route_collab_workflow_reply(state, previous['content'])
            if routed is not None and not error and state.active_handoff_id:
                state.execution_log.append({'action': 'run_route_placeholder_reconciled',
                    'result': current.run_id, 'handoff_id': state.active_handoff_id,
                    'timestamp': session._now()})
                session._save(state)
                return routed
            return None
        return None

    def _unstarted_routed_run(self, current, handoff):
        """Prove that binding and startup preflight are this run's only work."""
        expected_error = 'preflight validation failed:\n- missing task plan file: ' + str(task_plan_path(self.project_root))
        if (handoff.target != 'run' or handoff.child != WorkflowRef('run', current.run_id)
                or current.resume_context.get('parent_handoff_id') != handoff.handoff_id
                or current.resume_context.get('workflow_id') != handoff.workflow_id
                or current.status not in {'pending', 'failed'}
                or current.status == 'failed' and current.last_error != expected_error):
            return False
        payload = current.to_dict()
        payload.update(resume_context={}, status='pending', last_error='')
        return payload == RunState(current.run_id, workflow_version=current.workflow_version).to_dict()

    def _initialize_routed_run_plan(self, current, handoff):
        if not self._unstarted_routed_run(current, handoff):
            return False
        path = task_plan_path(self.project_root)
        if path.is_symlink():
            return False
        if not path.exists():
            # The adopted placeholder has nothing to archive. Create only the
            # controller scaffold; the plan stage still owns actual tasks.
            save_task_plan(self.project_root, {'tasks': []})
        try:
            return read_json(path, default=None) == {'tasks': []}
        except (OSError, ValueError):
            return False

    def recover_missing_run_plan(self, session, state):
        """Resume the exact child refused before its first provider turn."""
        if (state.active_handoff_id or not state.workflow_id
                or state.acceptance_execution and state.acceptance_execution.get('phase') not in {'blocked', 'completed'}):
            return None
        current = load_run_state(self.project_root)
        if current.status != 'failed':
            return None
        expected_error = 'preflight validation failed:\n- missing task plan file: ' + str(task_plan_path(self.project_root))
        latest = next((row for row in reversed(state.execution_log)
                       if row.get('action') in {'child_returned', 'workflow_routed', 'run_route_deferred'}), None)
        if not latest or latest.get('action') != 'child_returned' or latest.get('result') != expected_error[:500]:
            return None
        try:
            handoff = self.store.load_handoff(str(latest.get('handoff_id', '')))
            if (handoff.parent != WorkflowRef(state.mode, state.session_id)
                    or handoff.workflow_id != state.workflow_id or handoff.status != 'failed'
                    or handoff.goal != state.goal or not handoff.returned_at
                    or handoff.result.get('summary') != expected_error
                    or handoff.result.get('run_id') != current.run_id
                    or not self._unstarted_routed_run(current, handoff)):
                return None
            # Later user input supersedes the retained route.
            returned = next((index for index in range(len(state.conversation) - 1, -1, -1)
                             if f'Child workflow run returned handoff_id={handoff.handoff_id} '
                             in str(state.conversation[index].get('content', ''))), None)
            if returned is None or any(row.get('role') == 'user' for row in state.conversation[returned + 1:]):
                return None
            spec = Path(str(current.resume_context.get('spec_file', '')))
            if (not spec.is_file() or spec.is_symlink()
                    or hashlib.sha256(spec.read_bytes()).hexdigest() != current.resume_context.get('iteration_spec_sha256')):
                return None
            if not self._initialize_routed_run_plan(current, handoff):
                return None
            from .validation import validation_report
            if not validation_report(self.project_root)['ok']:
                return None
        except (OSError, ValueError, RuntimeError):
            return None
        from .local_io import digest
        snapshot = self.store.load(state.workflow_id)
        # A deterministic continuation preserves the failed return and all
        # attempt/acceptance budgets, including across interrupted recovery.
        continuation = self.store.prepare_handoff(snapshot, parent=handoff.parent, target='resume',
            goal=handoff.goal, reason='Resume the bound run after task-plan initialization',
            payload={'resume_handoff_id': handoff.handoff_id},
            handoff_id='hf-' + digest(['missing-run-plan', handoff.handoff_id])[:12])
        state.active_handoff_id = continuation.handoff_id
        state.status, state.resolution, state.return_phase = 'waiting_child', '', ''
        state.execution_log.append({'action': 'run_plan_preflight_reconciled',
            'handoff_id': continuation.handoff_id, 'result': current.run_id, 'timestamp': session._now()})
        session._save(state)
        return state

    def prepare_run_route(self, payload: Optional[Dict[str, object]] = None) -> tuple[bool, str]:
        """Clear a verified engine blocker before a run handoff is created."""

        blocked = self._execution_binding_result(payload or {})
        if blocked:
            return False, str(blocked["summary"])

        current = load_run_state(self.project_root)
        if current.status == "completed":
            return True, ""
        if self._unstarted_run(current):
            return True, f'run {current.run_id} is an unstarted placeholder'
        if not False:
            engine_error = self._engine_blocker_error(current)
            if engine_error is not None:
                raise engine_error
            return False, (
                f"run {current.run_id} remains {current.status}; no new run "
                "handoff was created"
            )
        save_run_state(self.project_root, current)
        if self.run_lock is not None:
            self.run_lock.bind_subject("run", current.run_id)
        if self.health_runtime is not None:
            self.health_runtime.bind_subject(current.run_id)
            self.health_runtime.set_phase("run")
        context = dict(current.resume_context)
        spec_file = Path(
            str(context.get("spec_file", self.project_root / "spec.md"))
        )
        try:
            resumed = self.orch.run(
                spec_file=spec_file,
                auto_approve=bool(
                    self.auto_approve or context.get("auto_approve", False)
                ),
                print_agent_output=bool(
                    self.print_agent_output
                    or context.get("print_agent_output", False)
                ),
                provider_kind=(
                    str(context.get("provider_kind", "")).strip() or None
                ),
                autonomy_mode=getattr(self.orch, "_autonomy_mode", None),
            )
        except RuntimeError as error:
            resumed = load_run_state(self.project_root)
            return False, (
                f"run {resumed.run_id} recovery failed before routing: {error}"
            )
        if resumed.status == "completed":
            return True, (
                f"recovered and completed prior run {resumed.run_id} before routing"
            )
        return False, (
            f"run {resumed.run_id} recovery ended with status {resumed.status}; "
            "no new run handoff was created"
        )

    def _drive_run_child(self, handoff: WorkflowHandoff, snapshot: WorkflowSnapshot) -> Dict[str, object]:
        blocked = self._execution_binding_result(handoff.payload)
        if blocked:
            return blocked
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
            and False
        ):
            save_run_state(self.project_root, current)
            if self.run_lock is not None:
                self.run_lock.bind_subject("run", current.run_id)
            if self.health_runtime is not None:
                self.health_runtime.bind_subject(current.run_id)
                self.health_runtime.set_phase("run")
            context = dict(current.resume_context)
            existing_spec_file = Path(
                str(context.get("spec_file", self.project_root / "spec.md"))
            )
            try:
                current = self.orch.run(
                    spec_file=existing_spec_file,
                    auto_approve=bool(
                        self.auto_approve or context.get("auto_approve", False)
                    ),
                    print_agent_output=bool(
                        self.print_agent_output
                        or context.get("print_agent_output", False)
                    ),
                    provider_kind=(
                        str(context.get("provider_kind", "")).strip() or None
                    ),
                    autonomy_mode=getattr(self.orch, "_autonomy_mode", None),
                )
            except RuntimeError:
                current = load_run_state(self.project_root)
        if (
            handoff.child is None
            and current.status != "completed"
            and not existing_same_handoff
            and not self._unstarted_run(current)
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
            self._record_completed_run_archive_recovery(
                snapshot,
                handoff,
                current,
            )
            self.store.bind_child(snapshot, handoff, child)
            state = current
        elif handoff.child is None:
            seed = dict(handoff.payload.get("spec_seed", {}))
            raw_goal_environment = handoff.payload.get(
                "goal_execution_environment",
                {},
            )
            raw_authorization_policy = handoff.payload.get(
                "authorization_policy",
                {},
            )
            if isinstance(raw_goal_environment, dict) and raw_goal_environment:
                seed.setdefault(
                    "goal_execution_environment",
                    dict(raw_goal_environment),
                )
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
            from .run_handoff_context import existing_scope
            successor_context = {
                **existing_scope(handoff, seed),
                "spec_file": str(self.project_root / spec["path"]),
                "workflow_id": snapshot.workflow_id,
                "parent_handoff_id": handoff.handoff_id,
                "iteration_spec_sha256": spec["sha256"],
                "iteration_spec_commit": spec["commit_sha"],
                "auto_approve": self.auto_approve,
                "print_agent_output": self.print_agent_output,
                **(
                    {
                        "goal_execution_environment": dict(
                            raw_goal_environment
                        )
                    }
                    if isinstance(raw_goal_environment, dict)
                    and raw_goal_environment
                    else {}
                ),
                **(
                    {
                        "authorization_policy": dict(
                            raw_authorization_policy
                        )
                    }
                    if isinstance(raw_authorization_policy, dict)
                    and raw_authorization_policy
                    else {}
                ),
            }
            if self._unstarted_run(current):
                # Bind the existing unused identity directly. It has no
                # completed work to archive and must never be marked complete.
                current.resume_context = successor_context
                save_run_state(self.project_root, current)
                state = current
            else:
                state = self.orch._start_new_iteration(
                    current,
                    resume_context_updates=successor_context,
                )
            self._record_completed_run_archive_recovery(
                snapshot,
                handoff,
                state,
            )
            child = WorkflowRef("run", state.run_id)
            self.store.bind_child(snapshot, handoff, child)
        else:
            state = load_run_state(self.project_root)

        if handoff.target == 'run':
            self._initialize_routed_run_plan(state, handoff)

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

    def _record_completed_run_archive_recovery(
        self,
        snapshot: WorkflowSnapshot,
        handoff: WorkflowHandoff,
        state: RunState,
    ) -> None:
        recovery_receipts = state.resume_context.get(
            "previous_run_archive_recovery_receipts",
            [],
        )
        if not isinstance(recovery_receipts, list) or not recovery_receipts:
            return
        if any(
            event.get("kind") == "completed_run_archive_recovered"
            and event.get("operation_id") == handoff.handoff_id
            for event in self.store.events(snapshot.workflow_id)
        ):
            return
        self.store.append_event(
            snapshot,
            "completed_run_archive_recovered",
            operation_id=handoff.handoff_id,
            details={
                "predecessor_run_id": state.resume_context.get(
                    "previous_run_id",
                    "",
                ),
                "successor_run_id": state.run_id,
                "receipts": recovery_receipts,
            },
        )

    def _resume_existing_child(
        self,
        handoff: WorkflowHandoff,
        snapshot: WorkflowSnapshot,
    ) -> Dict[str, object]:
        original = self._resolved_handoff_chain(handoff, snapshot.workflow_id)[-1]
        if original.child is None:
            raise RuntimeError(f"handoff {original.handoff_id} has no child to resume")
        if original.child.kind == "run":
            handoff.child = original.child
            self.store.save_handoff(handoff)
            return self._drive_run_child(handoff, snapshot)
        if original.child.kind == "fix":
            from .session import Session

            retained = load_session_state(self.project_root, original.child.native_id)
            self._validated_child_handoff(retained, handoff)
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
                retained,
                snapshot,
                root=False,
            )
            return self._session_result(state, handoff)
        raise RuntimeError(f"unsupported resumable child kind: {original.child.kind}")

    def _apply_child_result(self, parent_state: object, handoff: WorkflowHandoff):
        result = dict(handoff.result)
        delivery = result.get('candidate_delivery')
        if result.get('status') == 'completed' and delivery:
            from .session_candidate import consume_delivery
            consumed = parent_state.candidate_custody.get('consumed_delivery', {})
            if consumed.get('revision') != delivery['delivered_revision']:
                consume_delivery(self.project_root, parent_state, delivery,
                                 child_id=str(result.get('session_id', '')))
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
        if (result.get("resolution") in {"execution_binding_mismatch", "verification_execution_binding"}
                or str(result.get('resolution', '')).startswith('kernel_')):
            parent_state.status = "blocked"
            parent_state.resolution = str(result["resolution"])
        if result.get('status') == 'completed' and delivery:
            parent_state.consecutive_agent_errors = 0
        if not self._preserve_engine_resume_budget:
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

    def _legacy_completed_result(self, state, handoff):
        """Read a completed legacy commit range; never authorize shared rollback.

        Before private receipts, a persisted completed child and its handoff
        baseline identified the result already committed in the shared repo.
        This compatibility result is only for reporting that committed range.
        It cannot recover missing modern custody or adopt uncommitted files.
        """
        if (state.mode != 'fix' or state.status != 'completed' or state.resolution != 'fixed'
                or state.verification_binding or state.candidate_custody or state.candidate_paths
                or state.source_descriptor or state.lineage_changed_paths or state.persistence_actions):
            return None
        from .session_verification import SessionOwnershipError
        try:
            original = self._validated_child_handoff(state, handoff)
        except SessionOwnershipError:
            return None
        try:
            retained = load_session_state(self.project_root, state.session_id)
        except (OSError, ValueError):
            return None
        # Do not turn a caller's stale/edited view into a completion receipt.
        for field in ('session_id', 'mode', 'workflow_id', 'parent_handoff_id', 'status', 'resolution',
                      'authorization_policy', 'goal_execution_environment', 'verification_binding',
                      'candidate_custody', 'candidate_paths', 'source_descriptor', 'lineage_changed_paths',
                      'persistence_actions', 'execution_log'):
            if getattr(retained, field) != getattr(state, field):
                return None
        if any(entry.get('action') in {
                'receipt_writer_result', 'receipt_verification', 'receipt_completion', 'candidate_superseded',
                'implementation_attempts_retained', 'not_a_bug', 'execution_preflight_blocked',
                'engine_preflight_recheck'} for entry in retained.execution_log):
            return None
        before = str(original.payload.get('head_before', ''))
        if not before:
            return None
        resolved = subprocess.run(['git', 'rev-parse', '--verify', '--end-of-options', before + '^{commit}'],
                                  cwd=self.project_root, capture_output=True, text=True)
        if resolved.returncode:
            return None
        base, after = resolved.stdout.strip(), head_ref(self.project_root)
        if not after or base == after:
            return None
        ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', base, after],
                                  cwd=self.project_root, capture_output=True)
        if ancestor.returncode:
            return None
        commits = _commits_between(self.project_root, base, after)
        paths = subprocess.run(['git', 'diff', '--name-only', '-z', base, after, '--'],
                               cwd=self.project_root, capture_output=True, text=True)
        if not commits or paths.returncode:
            return None
        return {'candidate_ownership': 'legacy_committed', 'candidate_delivery': {},
                'head_before': base, 'head_after': after, 'commit_shas': commits,
                'changed_paths': sorted(path for path in paths.stdout.split('\0')
                                        if path and not path.startswith(('.auto-agents/', '.antigravitycli/'))),
                'rolled_back_paths': []}

    def _session_result(self, state: object, handoff: WorkflowHandoff) -> Dict[str, object]:
        from .session_verification import SessionOwnershipError
        before = str(handoff.payload.get("head_before", ""))
        delivery = dict(getattr(state, 'candidate_custody', {}))
        ownership = 'unknown'
        ownership_error = None
        if state.mode == 'fix':
            try:
                original = self._validated_child_handoff(state, handoff)
                before = str(original.payload.get('head_before', ''))
                ownership = self._handoff_exit_ownership(state, handoff)
            except SessionOwnershipError as error:
                ownership_error = error
        after = delivery.get('delivered_revision', '') if ownership == 'private' else ''
        source = Path(delivery['checkout']) if ownership == 'private' else self.project_root
        reported_paths = sorted(set(state.candidate_paths) | set(state.lineage_changed_paths)) if ownership == 'private' else []
        commit_base = delivery.get('base_revision', before)
        if state.mode != 'fix':
            # Preserve the other session protocols; only fix handoffs use
            # the private-writer/positive-preflight ownership contract above.
            ownership = 'private' if delivery else 'legacy'
            after = delivery.get('delivered_revision') or head_ref(self.project_root)
            source = Path(delivery['checkout']) if delivery else self.project_root
            commit_base = before
            reported_paths = (sorted(set(state.candidate_paths) | set(state.lineage_changed_paths))
                              if state.verification_binding else _paths_between(self.project_root, before, after))
        failure = next((entry for entry in reversed(state.execution_log)
                        if entry.get('failure_kind') and entry.get('retry_fix') is False), {}) if state.status in {'failed', 'blocked'} else {}
        summary = failure.get('result') or state.resolution or f"{state.mode} status={state.status}"
        result = {
            "candidate_delivery": delivery if state.status == "completed" and ownership == 'private' and after else {},
            "candidate_ownership": ownership,
            "status": state.status,
            "resolution": state.resolution,
            "summary": summary,
            "session_id": state.session_id,
            "state_ref": str(
                Path(".auto-agents") / "state" / "sessions" / state.session_id / "session_state.json"
            ),
            "head_before": before,
            "head_after": after,
            "commit_shas": _commits_between(source, commit_base, after) if after else [],
            "changed_paths": reported_paths,
            "failure_fingerprint": _failure_fingerprint(state.status, str(summary)),
        }
        if failure:
            result.update(diagnostic=failure.get('diagnostic', {}),
                          failure_kind=failure.get('failure_kind'), retry_fix=failure.get('retry_fix'))
        if ownership == 'none':
            result['rolled_back_paths'] = []
        if ownership_error is not None:
            legacy = self._legacy_completed_result(state, handoff)
            if legacy is not None:
                result.update(legacy)
                return result
            result['ownership_diagnostic'] = {'reason': str(ownership_error), **ownership_error.diagnostic}
            # Native completion and candidate ownership are distinct. A
            # completion-only, unbound delegate return has no paths, commits,
            # or delivery to consume, and cannot authorize any rollback.
            unbound_return = (handoff.child is None and not any((
                state.workflow_id, state.parent_handoff_id, state.verification_binding,
                state.candidate_custody, state.candidate_paths, state.lineage_changed_paths,
                state.source_descriptor, state.persistence_actions, state.execution_log,
                state.current_attempt, state.active_handoff_id, state.last_child_result_ref,
            )))
            if state.status == 'completed' and not unbound_return:
                result.update(status='blocked', resolution='verification_ownership', summary=str(ownership_error))
        return result

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
