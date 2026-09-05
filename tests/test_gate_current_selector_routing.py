import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import load_run_state, load_task_plan, save_run_state
from auto_agents.execution_recovery import (
    BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
    BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY,
    ExecutionIncident,
    ExecutionIncidentStore,
)
from auto_agents.gates import (
    GateCommandBaselineIdentityError,
    GateCommandExecutionError,
)
from auto_agents.git_ops import head_ref, worktree_fingerprint
from auto_agents.models import CommandResult, GateResult, TaskSpec, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.self_repair import self_repair_error_fingerprint


CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND = (
    "gate_current_verification_contract_invalid"
)
CURRENT_VERIFICATION_CONTRACT_SNAPSHOT_KEY = "current_verification_contract"


class GateCurrentSelectorRoutingTests(unittest.TestCase):
    @staticmethod
    def _orchestrator(root: Path, command: str) -> Orchestrator:
        Orchestrator.init_project(root, "project", "mock")
        orchestrator = Orchestrator(root)
        orchestrator.config.gates.verification_policy_version = 3
        orchestrator.config.gates.incremental_mode = "auto"
        orchestrator.config.gates.steps = [
            VerificationStep(proof_id="focused-selector", command=command)
        ]
        return orchestrator

    @staticmethod
    def _task(command: str, *, task_id: str = "source-task") -> TaskSpec:
        return TaskSpec(
            task_id=task_id,
            title="Verify an exact focused selector",
            description="Keep exact verification references executable.",
            acceptance=["The configured proof resolves."],
            status="in_progress",
            verification_refs=[f"cmd:{command}"],
            verify_baseline_ref="baseline-ref",
        )

    @staticmethod
    def _missing_result(command: str, node_id: str) -> CommandResult:
        return CommandResult(
            command=command,
            ok=False,
            returncode=4,
            stdout=(
                f"ERROR: not found: {node_id}\n"
                f"(no match in any of [<Module {Path(node_id.split('::')[0]).name}>])\n"
            ),
        )

    @staticmethod
    def _failed_result(command: str, node_id: str) -> CommandResult:
        return CommandResult(
            command=command,
            ok=False,
            returncode=1,
            stdout=f"FAILED {node_id} - AssertionError\n",
        )

    @staticmethod
    def _git(root: Path, *args: str) -> bytes:
        process = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=True,
        )
        return process.stdout

    @classmethod
    def _target_snapshot(cls, root: Path, paths: list[Path]) -> dict:
        relatives = [path.relative_to(root).as_posix() for path in paths]
        return {
            "head": head_ref(root),
            "bytes": {
                relative: (root / relative).read_bytes()
                for relative in relatives
            },
            "index": cls._git(
                root,
                "ls-files",
                "--stage",
                "-z",
            ),
            "status": cls._git(
                root,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            "worktree": worktree_fingerprint(root),
        }

    def _persisted_selector_case(
        self,
        root: Path,
        *,
        staged_tooling: bool = True,
        renderer_drift: bool = False,
    ) -> dict:
        test_path = root / "tests" / "test_recovery_contract.py"
        tooling_path = root / "retained.tsbuildinfo"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("import unittest\n", encoding="utf-8")
        if renderer_drift:
            conda_history = root / ".conda" / "conda-meta" / "history"
            conda_history.parent.mkdir(parents=True, exist_ok=True)
            conda_history.write_text("created-by=test\n", encoding="utf-8")
        if staged_tooling:
            tooling_path.write_text("head tooling state\n", encoding="utf-8")
        self._git(root, "init", "-q")
        self._git(root, "config", "user.name", "Selector Tests")
        self._git(root, "config", "user.email", "selector-tests@example.com")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "baseline")

        method = "test_public_recovery_projection"
        malformed_node = f"{test_path.relative_to(root).as_posix()}::{method}"
        qualified_node = (
            f"{test_path.relative_to(root).as_posix()}::"
            f"ProjectApiTests::{method}"
        )
        python = Path(sys.executable).as_posix()
        malformed_command = f"{python} -m pytest -q {malformed_node}"
        qualified_command = f"{python} -m pytest -q {qualified_node}"
        test_path.write_text(
            "\n".join(
                [
                    "import unittest",
                    "",
                    "",
                    "class ProjectApiTests(unittest.TestCase):",
                    f"    def {method}(self):",
                    "        self.assertEqual({'state': 'recovering'}['state'], 'recovering')",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._git(root, "add", "--", test_path.relative_to(root).as_posix())
        with test_path.open("a", encoding="utf-8") as handle:
            handle.write("# retained unstaged task work\n")
        retained_paths = [test_path]
        if staged_tooling:
            tooling_path.write_text("staged tooling state\n", encoding="utf-8")
            self._git(root, "add", "--", tooling_path.name)
            with tooling_path.open("a", encoding="utf-8") as handle:
                handle.write("unstaged tooling state\n")
            retained_paths.append(tooling_path)

        orchestrator = Orchestrator(root)
        orchestrator.config.gates.steps = []
        orchestrator.config.gates.distributed.mode = "off"
        orchestrator.config.gates.adaptive_timeout_enabled = False
        orchestrator.config.gates.command_timeout_seconds = 30
        orchestrator.config.gates.command_idle_timeout_seconds = 30
        state = load_run_state(root)
        state.run_id = "persisted-selector-run"
        state.current_stage = "implement"
        source_task = self._task(malformed_command)
        source_task.verification_refs = [malformed_node]
        state.tasks = [source_task]
        orchestrator._persist_tasks(state.tasks)
        orchestrator._set_implementation_ready_marker(state, source_task, True)
        checkpoint = self._target_snapshot(root, retained_paths)

        baseline_output = (
            f"ERROR: not found: {malformed_node}\n"
            f"(no match in any of [<Module {test_path.name}>])\n"
        )
        incident = ExecutionIncident(
            incident_id="persisted-selector",
            run_id=state.run_id,
            source="gate",
            kind=BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
            stage="implement",
            context=f"lazy task baseline verification ({source_task.task_id})",
            command=malformed_command,
            task_id=source_task.task_id,
            baseline=True,
            returncode=4,
            stdout_tail=baseline_output,
            process_snapshot={
                BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY: {
                    "status": "unresolved",
                    "contract": "stable_test_failure_ids",
                    "repair_scope": "verification_contract",
                }
            },
            head_ref=checkpoint["head"],
            worktree_fingerprint=checkpoint["worktree"],
            origin_command=malformed_command,
            incident_fingerprint="legacy-baseline-identity",
            root_cause_fingerprint="legacy-baseline-root",
            evidence_fingerprint="legacy-baseline-evidence",
            status="self_repair",
            diagnosis={
                "owner": "auto_agents",
                "action": "SELF_REPAIR",
                "reason": "legacy baseline identity routing",
            },
            repair_history=[{"event": "legacy_repair_audit"}],
            history=[{"event": "legacy_incident_audit"}],
        )
        ExecutionIncidentStore(root, state.run_id).save(incident, state)
        state.status = "blocked"
        state.last_error = "legacy baseline identity blocker"
        state.active_blocker = {
            "owner": "auto_agents",
            "category": BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
            "incident_id": incident.incident_id,
            "fingerprint": incident.evidence_fingerprint,
            "reason": state.last_error,
            "status": "blocked",
            "checkpoint": {
                "stage": "implement",
                "head": checkpoint["head"],
                "worktree": checkpoint["worktree"],
            },
        }
        save_run_state(root, state)
        return {
            "orchestrator": orchestrator,
            "state": state,
            "source_task": source_task,
            "incident": incident,
            "test_path": test_path,
            "retained_paths": retained_paths,
            "malformed_command": malformed_command,
            "qualified_command": qualified_command,
            "qualified_node": qualified_node,
            "checkpoint": checkpoint,
        }

    def _assert_target_snapshot_unchanged(
        self,
        case: dict,
    ) -> None:
        self.assertEqual(
            self._target_snapshot(
                case["orchestrator"].project_root,
                case["retained_paths"],
            ),
            case["checkpoint"],
        )

    @staticmethod
    def _diagnosed_selector_blocker(case: dict) -> dict:
        incident = case["incident"]
        reason = (
            f"task {case['source_task'].task_id} used malformed selector "
            f"{case['malformed_command'].split()[-1]}"
        )
        return {
            "owner": "auto_agents",
            "category": "diagnosed_engine_failure",
            "fingerprint": self_repair_error_fingerprint(
                incident.diagnosis["reason"],
                "provider_judged_auto_agents",
            ),
            "status": "blocked",
            "reason": reason,
            "incident_id": "",
            "checkpoint": copy.deepcopy(case["state"].active_blocker["checkpoint"]),
            "root_cause_diagnosis": {
                "diagnosis_id": "selector-root-cause-diagnosis",
                "evidence_path": ".auto-agents/runs/run/root-cause/evidence.json",
                "final": {
                    "owner": "auto_agents",
                    "category": "diagnosed_engine_failure",
                    "verdict": "FINAL",
                    "generic": True,
                    "resume_strategy": "repair_and_resume",
                    "causal_chain": [reason],
                },
            },
        }

    def _check_current_and_baseline_missing_routes_target_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            node_id = "tests/test_contract.py::ContractTests::test_projection"
            command = f"python -m pytest -q {node_id}"
            orchestrator = self._orchestrator(root, command)
            state = load_run_state(root)
            task = self._task(command)
            state.tasks = [task]
            orchestrator._persist_tasks([task])
            retained = root / "retained.py"
            retained.write_text("VALUE = 'ready'\n", encoding="utf-8")
            orchestrator._set_implementation_ready_marker(state, task, True)
            original_head = head_ref(root)
            original_worktree = (
                orchestrator._worktree_fingerprint_excluding_agent_instructions()
            )
            original_full_worktree = worktree_fingerprint(root)
            original_bytes = retained.read_bytes()
            current_gate = GateResult(
                ok=False,
                commands=[self._missing_result(command, node_id)],
                summary="current target is absent",
            )
            baseline_gate = GateResult(
                ok=False,
                commands=[self._missing_result(command, node_id)],
                summary="baseline target is absent",
            )

            with (
                patch.object(orchestrator, "_quick_verify_failure", return_value=None),
                patch.object(
                    orchestrator,
                    "_build_task_verify_commands",
                    return_value=[command],
                ),
                patch.object(
                    orchestrator,
                    "_run_task_gate_commands_for_commands",
                    side_effect=[(current_gate, ""), (baseline_gate, "")],
                ),
                patch.object(
                    orchestrator,
                    "_run_verify_failure_identity_diagnostic",
                    side_effect=AssertionError(
                        "a raw missing current selector must be classified first"
                    ),
                ),
                self.assertRaises(GateCommandExecutionError) as raised,
            ):
                orchestrator._run_task_verify(task, state=state)

            error = raised.exception
            self.assertFalse(error.baseline)
            self.assertEqual(error.task_id, task.task_id)
            marker = error.result.process_snapshot[
                CURRENT_VERIFICATION_CONTRACT_SNAPSHOT_KEY
            ]
            self.assertEqual(marker["status"], "target_not_found")
            self.assertEqual(
                marker["baseline_observation"]["status"],
                "target_not_found",
            )
            with patch.object(
                orchestrator,
                "_agent_diagnose_execution_incident",
                side_effect=AssertionError("current selector routing is deterministic"),
            ):
                recovered = orchestrator._handle_gate_execution_incident(
                    state,
                    "implement",
                    error,
                )

            self.assertTrue(recovered)
            incident = state.execution_incidents[-1]
            self.assertEqual(
                incident["kind"], CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND
            )
            self.assertEqual(incident["task_id"], task.task_id)
            self.assertEqual(incident["diagnosis"]["owner"], "verification_contract")
            self.assertEqual(incident["diagnosis"]["action"], "RECOVER_TARGET")
            self.assertNotEqual(
                incident["kind"], BASELINE_FAILURE_IDENTITY_INCIDENT_KIND
            )
            recovery = load_task_plan(root)["tasks"][0]
            self.assertEqual(recovery["title"], "Repair current verification contract")
            handoff = recovery["recovery_history"][0]["worktree_handoff"]
            self.assertEqual(handoff["source_task_id"], task.task_id)
            self.assertEqual(handoff["worktree_fingerprint"], original_worktree)
            self.assertEqual(head_ref(root), original_head)
            self.assertEqual(
                orchestrator._worktree_fingerprint_excluding_agent_instructions(),
                original_worktree,
            )
            self.assertEqual(worktree_fingerprint(root), original_full_worktree)
            self.assertEqual(retained.read_bytes(), original_bytes)

    def test_baseline_only_missing_pytest_target_remains_not_applicable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            node_id = "tests/test_contract.py::ContractTests::test_projection"
            command = f"python -m pytest -q {node_id}"
            orchestrator = self._orchestrator(root, command)
            task = self._task(command)
            current_gate = GateResult(
                ok=False,
                commands=[self._failed_result(command, node_id)],
                summary="one current semantic failure",
            )
            baseline_gate = GateResult(
                ok=False,
                commands=[self._missing_result(command, node_id)],
                summary="baseline target is absent",
            )

            with (
                patch.object(orchestrator, "_quick_verify_failure", return_value=None),
                patch.object(
                    orchestrator,
                    "_build_task_verify_commands",
                    return_value=[command],
                ),
                patch.object(
                    orchestrator,
                    "_run_task_gate_commands_for_commands",
                    side_effect=[(current_gate, ""), (baseline_gate, "")],
                ),
                patch.object(
                    orchestrator,
                    "_run_verify_failure_identity_diagnostic",
                    side_effect=AssertionError(
                        "stable exact current evidence needs no diagnostic"
                    ),
                ),
            ):
                result = orchestrator._run_task_verify(task)

            self.assertFalse(result["ok"])
            self.assertEqual(result["current_failure_ids"], [node_id])
            self.assertEqual(result["baseline_failure_ids"], [])
            self.assertEqual(result["baseline_not_applicable_commands"], [command])
            self.assertEqual(task.verify_baseline_failures, [])

    def test_mixed_lazy_baseline_absence_preserves_other_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            absent_id = "tests/test_new.py::ContractTests::test_new_proof"
            stable_id = "tests/test_existing.py::test_existing_failure"
            absent_command = f"python -m pytest -q {absent_id}"
            stable_command = f"python -m pytest -q {stable_id}"
            orchestrator = self._orchestrator(root, absent_command)
            task = self._task(absent_command)
            state = load_run_state(root)
            state.tasks = [task]
            current_gate = GateResult(
                ok=False,
                commands=[
                    self._failed_result(absent_command, absent_id),
                    self._failed_result(stable_command, stable_id),
                ],
                summary="two current semantic failures",
            )
            baseline_gate = GateResult(
                ok=False,
                commands=[
                    self._missing_result(absent_command, absent_id),
                    self._failed_result(stable_command, stable_id),
                ],
                summary="one absent target and one semantic failure",
            )

            with (
                patch.object(orchestrator, "_quick_verify_failure", return_value=None),
                patch.object(
                    orchestrator,
                    "_build_task_verify_commands",
                    return_value=[absent_command, stable_command],
                ),
                patch.object(
                    orchestrator,
                    "_run_task_gate_commands_for_commands",
                    side_effect=[(current_gate, ""), (baseline_gate, "")],
                ),
                patch.object(
                    orchestrator,
                    "_run_verify_failure_identity_diagnostic",
                    side_effect=AssertionError(
                        "per-command stable evidence must remain comparable"
                    ),
                ),
            ):
                result = orchestrator._run_task_verify(task, state=state)

            self.assertFalse(result["ok"])
            self.assertEqual(
                result["baseline_not_applicable_commands"], [absent_command]
            )
            self.assertEqual(result["baseline_failure_ids"], [stable_id])
            self.assertEqual(result["new_failure_ids"], [absent_id])
            self.assertEqual(task.verify_baseline_failures, [stable_id])
            self.assertFalse(
                any(
                    item.get("kind") == BASELINE_FAILURE_IDENTITY_INCIDENT_KIND
                    for item in state.execution_incidents
                )
            )
            self.assertEqual(state.active_blocker, {})

    def test_valid_current_selector_proves_immutable_baseline_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            node_id = "tests/test_contract.py::test_fixture"
            command = f"python -m pytest -q {node_id}"
            orchestrator = self._orchestrator(root, command)
            state = load_run_state(root)
            task = self._task(command)
            state.tasks = [task]
            orchestrator._persist_tasks([task])
            current_gate = GateResult(
                ok=False,
                commands=[self._failed_result(command, node_id)],
                summary="current selector has a semantic failure",
            )
            baseline_result = CommandResult(
                command=command,
                ok=False,
                returncode=1,
                stdout="pytest stopped before publishing a test identity\n",
            )
            baseline_gate = GateResult(
                ok=False,
                commands=[baseline_result],
                summary="baseline identity is unresolved",
            )

            with (
                patch.object(orchestrator, "_quick_verify_failure", return_value=None),
                patch.object(
                    orchestrator,
                    "_build_task_verify_commands",
                    return_value=[command],
                ),
                patch.object(
                    orchestrator,
                    "_run_task_gate_commands_for_commands",
                    side_effect=[(current_gate, ""), (baseline_gate, "")],
                ),
                patch.object(
                    orchestrator,
                    "_run_verify_failure_identity_diagnostic",
                    return_value=baseline_gate,
                ),
                self.assertRaises(GateCommandBaselineIdentityError) as raised,
            ):
                orchestrator._run_task_verify(task, state=state)

            marker = raised.exception.result.process_snapshot[
                BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY
            ]
            self.assertTrue(marker["immutable_baseline_only"])
            self.assertEqual(marker["current_semantic_failure_ids"], [node_id])
            with patch.object(
                orchestrator,
                "_agent_diagnose_execution_incident",
                side_effect=AssertionError("immutable baseline proof is deterministic"),
            ):
                recovered = orchestrator._handle_gate_execution_incident(
                    state,
                    "implement",
                    raised.exception,
                )

            self.assertFalse(recovered)
            self.assertEqual(state.active_blocker["owner"], "auto_agents")
            self.assertEqual(
                state.active_blocker["category"],
                BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
            )

    def _check_persisted_missing_selector_reclassifies_without_losing_task_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value="engine-with-selector-reclassification",
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.incident_id, case["incident"].incident_id)
            self.assertEqual(
                persisted.kind,
                CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND,
            )
            self.assertFalse(persisted.baseline)
            self.assertEqual(persisted.task_id, case["source_task"].task_id)
            self.assertEqual(persisted.repair_history, case["incident"].repair_history)
            self.assertEqual(persisted.history[0]["event"], "legacy_incident_audit")
            migration = next(
                entry
                for entry in persisted.history
                if entry.get("event") == "persisted_baseline_selector_reclassified"
            )
            self.assertEqual(
                migration["baseline_observation"]["status"],
                "target_not_found",
            )
            self.assertEqual(
                persisted.process_snapshot[
                    CURRENT_VERIFICATION_CONTRACT_SNAPSHOT_KEY
                ]["status"],
                "target_not_found",
            )
            self.assertEqual(persisted.diagnosis["owner"], "verification_contract")
            self.assertEqual(persisted.diagnosis["action"], "RECOVER_TARGET")
            tasks = load_task_plan(root)["tasks"]
            recovery = next(
                task
                for task in tasks
                if task["task_origin"] == "stage_recovery"
            )
            handoff = recovery["recovery_history"][0]["worktree_handoff"]
            self.assertEqual(handoff["source_task_id"], case["source_task"].task_id)
            self.assertEqual(
                handoff["worktree_fingerprint"],
                case["checkpoint"]["worktree"],
            )
            self._assert_target_snapshot_unchanged(case)

    def _check_corrected_class_qualified_selector_resumes_pending_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]
            case["incident"].status = "recovering"
            ExecutionIncidentStore(root, state.run_id).save(
                case["incident"],
                state,
            )
            state.status = "pending"
            state.active_blocker = self._diagnosed_selector_blocker(case)
            state.active_blocker["status"] = "retrying"
            save_run_state(root, state)
            revision = "engine-with-selector-reclassification"
            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value=revision,
            ):
                self.assertTrue(orchestrator._resume_blocked_run(state))

            tasks = orchestrator._load_tasks_from_plan()
            source_task = next(
                task
                for task in tasks
                if task.task_id == case["source_task"].task_id
            )
            source_task.verification_refs = [case["qualified_node"]]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            save_run_state(root, state)

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    wraps=orchestrator._run_persisted_selector_probe,
                ) as probe,
            ):
                changed = orchestrator._resume_blocked_run(state)
                probe_count = probe.call_count
                repeated = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertFalse(repeated)
            self.assertEqual(probe.call_count, probe_count)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})
            recovery = next(
                task
                for task in state.tasks
                if task.task_origin == "stage_recovery"
            )
            self.assertEqual(
                recovery.verification_refs,
                [case["qualified_node"]],
            )
            self.assertEqual(recovery.status, "in_progress")
            marker = orchestrator._execution_recovery_marker(recovery)
            self.assertEqual(marker["selector_correction_result"], "verified")
            self.assertEqual(
                marker["verification_command"],
                case["malformed_command"],
            )
            self.assertEqual(
                marker["implementation_completed_round"],
                marker["implementation_required_round"],
            )
            self.assertTrue(
                state.resume_context["implementation_ready_tasks"][recovery.task_id]
            )
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            correction = next(
                entry
                for entry in persisted.history
                if entry.get("event") == "persisted_selector_correction_probed"
            )
            self.assertEqual(correction["result"], "accepted")
            self.assertEqual(correction["commands"][0]["status"], "resolved_pass")
            self.assertIn(case["qualified_node"], case["qualified_command"])
            self._assert_target_snapshot_unchanged(case)

    def _check_recovery_task_correction_resumes_pending_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]
            revision = "engine-with-recovery-owned-selector-correction"

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value=revision,
            ):
                self.assertTrue(orchestrator._resume_blocked_run(state))

            tasks = orchestrator._load_tasks_from_plan()
            source_task = next(
                task
                for task in tasks
                if task.task_id == case["source_task"].task_id
            )
            recovery_task = next(
                task for task in tasks if task.task_origin == "stage_recovery"
            )
            self.assertEqual(
                source_task.verification_refs,
                [case["malformed_command"].split()[-1]],
            )
            recovery_task.verification_refs = [case["qualified_node"]]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            save_run_state(root, state)

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value=revision,
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})
            persisted_tasks = orchestrator._load_tasks_from_plan()
            recovery_task = next(
                task
                for task in persisted_tasks
                if task.task_origin == "stage_recovery"
            )
            self.assertEqual(
                recovery_task.verification_refs,
                [case["qualified_node"]],
            )
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            correction = next(
                entry
                for entry in persisted.history
                if entry.get("event") == "persisted_selector_correction_probed"
            )
            self.assertEqual(
                correction["correction_task_id"],
                recovery_task.task_id,
            )
            self.assertEqual(correction["result"], "accepted")
            self._assert_target_snapshot_unchanged(case)

    def _check_noncollecting_pytest_correction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]
            revision = "engine-rejecting-noncollecting-corrections"

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value=revision,
            ):
                self.assertTrue(orchestrator._resume_blocked_run(state))

            tasks = orchestrator._load_tasks_from_plan()
            recovery_task = next(
                task for task in tasks if task.task_origin == "stage_recovery"
            )
            version_command = f"{Path(sys.executable).as_posix()} -m pytest --version"
            self.assertFalse(
                orchestrator._is_persisted_selector_pytest_command(
                    "echo pytest 1 passed"
                )
            )
            self.assertFalse(
                orchestrator._is_persisted_selector_pytest_command(
                    f"{version_command} && printf '1 passed'"
                )
            )
            recovery_task.verification_refs = [f"cmd:{version_command}"]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            bound_blocker = {
                "owner": "auto_agents",
                "category": BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                "incident_id": case["incident"].incident_id,
                "fingerprint": case["incident"].evidence_fingerprint,
                "reason": "selector correction is awaiting verified collection",
                "status": "blocked",
                "checkpoint": {
                    "stage": "implement",
                    "head": case["checkpoint"]["head"],
                    "worktree": case["checkpoint"]["worktree"],
                },
            }
            state.status = "blocked"
            state.last_error = bound_blocker["reason"]
            state.active_blocker = copy.deepcopy(bound_blocker)
            save_run_state(root, state)

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    wraps=orchestrator._run_persisted_selector_probe,
                ) as probe,
            ):
                changed = orchestrator._resume_blocked_run(state)
                repeated = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertFalse(repeated)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(state.status, "blocked")
            observed_blocker = dict(state.active_blocker)
            observed_blocker.pop("updated_at", None)
            self.assertEqual(observed_blocker, bound_blocker)
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            correction = next(
                entry
                for entry in persisted.history
                if entry.get("event") == "persisted_selector_correction_probed"
            )
            self.assertEqual(correction["result"], "rejected")
            self.assertEqual(correction["commands"][0]["returncode"], 0)
            self.assertEqual(
                correction["commands"][0]["status"],
                "collection_unproven",
            )
            persisted_recovery = next(
                task
                for task in orchestrator._load_tasks_from_plan()
                if task.task_origin == "stage_recovery"
            )
            marker = orchestrator._execution_recovery_marker(persisted_recovery)
            self.assertNotIn("selector_correction_result", marker)
            self.assertNotEqual(
                marker.get("implementation_completed_round", 0),
                marker["implementation_required_round"],
            )
            self.assertFalse(
                state.resume_context.get("implementation_ready_tasks", {}).get(
                    persisted_recovery.task_id,
                    False,
                )
            )

            marker_text = "markers=proof: 1 passed"
            markers_command = (
                f"{Path(sys.executable).as_posix()} -m pytest --markers "
                f"-o '{marker_text}'"
            )
            informational = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--markers",
                    "-o",
                    marker_text,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(informational.returncode, 0)
            self.assertIn("@pytest.mark.proof: 1 passed", informational.stdout)
            self.assertNotIn(case["qualified_node"], informational.stdout)

            tasks = orchestrator._load_tasks_from_plan()
            persisted_recovery = next(
                task for task in tasks if task.task_origin == "stage_recovery"
            )
            persisted_recovery.verification_refs = [f"cmd:{markers_command}"]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            save_run_state(root, state)

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    wraps=orchestrator._run_persisted_selector_probe,
                ) as probe,
            ):
                spoofed = orchestrator._resume_blocked_run(state)
                spoofed_repeated = orchestrator._resume_blocked_run(state)

            self.assertFalse(spoofed)
            self.assertFalse(spoofed_repeated)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(state.status, "blocked")
            observed_blocker = dict(state.active_blocker)
            observed_blocker.pop("updated_at", None)
            self.assertEqual(observed_blocker, bound_blocker)
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            marker_correction = [
                entry
                for entry in persisted.history
                if entry.get("event") == "persisted_selector_correction_probed"
            ][-1]
            self.assertEqual(marker_correction["result"], "rejected")
            self.assertEqual(marker_correction["commands"][0]["returncode"], 0)
            self.assertEqual(
                marker_correction["commands"][0]["status"],
                "collection_unproven",
            )
            persisted_recovery = next(
                task
                for task in orchestrator._load_tasks_from_plan()
                if task.task_origin == "stage_recovery"
            )
            marker = orchestrator._execution_recovery_marker(persisted_recovery)
            self.assertNotIn("selector_correction_result", marker)
            self.assertNotEqual(
                marker.get("implementation_completed_round", 0),
                marker["implementation_required_round"],
            )
            self.assertFalse(
                state.resume_context.get("implementation_ready_tasks", {}).get(
                    persisted_recovery.task_id,
                    False,
                )
            )
            self._assert_target_snapshot_unchanged(case)

    def _check_corrected_selector_requires_matching_blocker_checkpoint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]
            revision = "engine-with-checkpoint-bound-selector-correction"

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value=revision,
            ):
                self.assertTrue(orchestrator._resume_blocked_run(state))

            tasks = orchestrator._load_tasks_from_plan()
            recovery_task = next(
                task for task in tasks if task.task_origin == "stage_recovery"
            )
            recovery_task.verification_refs = [case["qualified_node"]]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            unrelated_checkpoint = copy.deepcopy(
                case["state"].active_blocker
            )
            unrelated_checkpoint.update(
                {
                    "owner": "auto_agents",
                    "category": BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
                    "incident_id": case["incident"].incident_id,
                    "status": "blocked",
                    "reason": "a different checkpoint still owns this blocker",
                    "checkpoint": {
                        "stage": "implement",
                        "head": case["checkpoint"]["head"],
                        "worktree": "different-worktree-checkpoint",
                    },
                }
            )
            state.status = "blocked"
            state.last_error = unrelated_checkpoint["reason"]
            state.active_blocker = copy.deepcopy(unrelated_checkpoint)
            save_run_state(root, state)

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    wraps=orchestrator._run_persisted_selector_probe,
                ) as probe,
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertEqual(probe.call_count, 0)
            observed = dict(state.active_blocker)
            observed.pop("updated_at", None)
            unrelated_checkpoint.pop("updated_at", None)
            self.assertEqual(observed, unrelated_checkpoint)
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.last_error, unrelated_checkpoint["reason"])
            self._assert_target_snapshot_unchanged(case)

    def _check_diagnosed_selector_blocker_reclassifies_bound_incident(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(
                root,
                staged_tooling=False,
                renderer_drift=True,
            )
            orchestrator = case["orchestrator"]
            state = case["state"]
            state.active_blocker = self._diagnosed_selector_blocker(case)
            save_run_state(root, state)

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value="engine-with-selector-reclassification",
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(
                persisted.kind,
                CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND,
            )
            migration = next(
                entry
                for entry in persisted.history
                if entry.get("event")
                == "persisted_baseline_selector_reclassified"
            )
            self.assertEqual(
                migration["prior_blocker"]["category"],
                "diagnosed_engine_failure",
            )
            self._assert_target_snapshot_unchanged(case)

    def _check_persisted_selector_uses_recorded_command_after_renderer_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(
                root,
                staged_tooling=False,
                renderer_drift=True,
            )
            orchestrator = case["orchestrator"]
            state = case["state"]
            rendered = orchestrator._build_task_verify_commands(
                case["source_task"]
            )
            self.assertEqual(len(rendered), 1)
            self.assertNotEqual(rendered[0], case["malformed_command"])
            self.assertIn(case["malformed_command"].split()[-1], rendered[0])

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value="engine-after-command-renderer-drift",
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertTrue(changed)
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(
                persisted.kind,
                CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND,
            )
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})
            self._assert_target_snapshot_unchanged(case)

    def _check_selector_reclassification_preempts_generic_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(
                root,
                staged_tooling=False,
                renderer_drift=True,
            )
            orchestrator = case["orchestrator"]
            state = case["state"]
            blocker = self._diagnosed_selector_blocker(case)
            final = blocker["root_cause_diagnosis"]["final"]
            final.update(
                {
                    "confidence": 0.99,
                    "verification_commands": [
                        "python -m pytest -q tests/test_selector_contract.py"
                    ],
                    "expected_postconditions": [
                        "the persisted selector is routed by current evidence"
                    ],
                }
            )
            state.active_blocker = blocker
            save_run_state(root, state)

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value="engine-before-generic-self-repair-resume",
                ),
                patch.object(
                    orchestrator,
                    "_verify_installed_generic_self_repair",
                    side_effect=AssertionError(
                        "selector evidence must be handled before generic resume"
                    ),
                ),
            ):
                changed = orchestrator._prepare_installed_generic_self_repair_resume(
                    state
                )

            self.assertTrue(changed)
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(
                persisted.kind,
                CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND,
            )
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.active_blocker, {})
            self._assert_target_snapshot_unchanged(case)

    def _check_rejected_probe_runs_once_per_installed_engine_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root, staged_tooling=False)
            orchestrator = case["orchestrator"]
            state = case["state"]
            blocker = copy.deepcopy(state.active_blocker)
            revision = "engine-with-rejected-selector-probe"
            rejected_gate = GateResult(
                ok=False,
                commands=[
                    CommandResult(
                        command=case["malformed_command"],
                        ok=False,
                        returncode=4,
                        stdout=(
                            "ERROR: not found: "
                            f"{case['malformed_command'].split()[-1]}\n"
                        ),
                        mutation_paths=["probe-output.txt"],
                    )
                ],
                summary="selector probe mutated its sandbox",
            )

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_cleanup_ephemeral_tooling_artifacts",
                ),
                patch(
                    "auto_agents.orchestrator.run_gate_plan",
                    return_value=rejected_gate,
                ) as probe,
            ):
                first = orchestrator._resume_blocked_run(state)
                reloaded = load_run_state(root)
                second = orchestrator._resume_blocked_run(reloaded)

            self.assertFalse(first)
            self.assertFalse(second)
            self.assertEqual(probe.call_count, 1)
            observed_blocker = dict(reloaded.active_blocker)
            observed_blocker.pop("updated_at", None)
            self.assertEqual(observed_blocker, blocker)
            recovery_key = (
                f"persisted_current_selector:{case['incident'].incident_id}"
            )
            self.assertEqual(
                reloaded.resume_context[
                    Orchestrator.INSTALLED_ENGINE_RECOVERY_CONTEXT
                ][recovery_key],
                revision,
            )
            self._assert_target_snapshot_unchanged(case)

    def _check_valid_current_probe_preserves_baseline_self_repair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root, staged_tooling=False)
            orchestrator = case["orchestrator"]
            state = case["state"]
            blocker = copy.deepcopy(state.active_blocker)
            current_failure = self._failed_result(
                case["malformed_command"],
                case["malformed_command"].split()[-1],
            )

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value="engine-with-valid-current-selector",
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    return_value=GateResult(
                        ok=False,
                        commands=[current_failure],
                    ),
                ) as probe,
            ):
                changed = orchestrator._resume_blocked_run(state)
                repeated = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertFalse(repeated)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(state.status, "blocked")
            observed_blocker = dict(state.active_blocker)
            observed_blocker.pop("updated_at", None)
            self.assertEqual(observed_blocker, blocker)
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(
                persisted.kind,
                BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
            )
            self.assertTrue(persisted.baseline)
            observation = next(
                entry
                for entry in persisted.history
                if entry.get("event") == "persisted_current_selector_probed"
            )
            self.assertEqual(
                observation["disposition"],
                "preserved_baseline_identity",
            )
            self._assert_target_snapshot_unchanged(case)

    def _check_correction_preflight_retries_after_checkpoint_restore(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]
            revision = "engine-with-checkpoint-qualified-selector-probe"

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value=revision,
            ):
                self.assertTrue(orchestrator._resume_blocked_run(state))

            tasks = orchestrator._load_tasks_from_plan()
            source_task = next(
                task
                for task in tasks
                if task.task_id == case["source_task"].task_id
            )
            source_task.verification_refs = [case["qualified_node"]]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            save_run_state(root, state)

            test_path = case["test_path"]
            retained_bytes = test_path.read_bytes()
            with test_path.open("ab") as handle:
                handle.write(b"# temporary checkpoint mismatch\n")
            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    wraps=orchestrator._run_persisted_selector_probe,
                ) as probe,
            ):
                self.assertFalse(orchestrator._resume_blocked_run(state))

            self.assertEqual(probe.call_count, 0)
            correction_keys = [
                key
                for key in state.resume_context.get(
                    Orchestrator.INSTALLED_ENGINE_RECOVERY_CONTEXT,
                    {},
                )
                if key.startswith(
                    f"persisted_selector_correction:{case['incident'].incident_id}:"
                )
            ]
            self.assertEqual(correction_keys, [])

            test_path.write_bytes(retained_bytes)
            self._assert_target_snapshot_unchanged(case)
            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    wraps=orchestrator._run_persisted_selector_probe,
                ) as probe,
            ):
                self.assertTrue(orchestrator._resume_blocked_run(state))

            self.assertEqual(probe.call_count, 1)
            correction_keys = [
                key
                for key, claimed_revision in state.resume_context[
                    Orchestrator.INSTALLED_ENGINE_RECOVERY_CONTEXT
                ].items()
                if key.startswith(
                    f"persisted_selector_correction:{case['incident'].incident_id}:"
                )
                and claimed_revision == revision
            ]
            self.assertEqual(len(correction_keys), 1)
            recovery = next(
                task
                for task in state.tasks
                if task.task_origin == "stage_recovery"
            )
            self.assertEqual(recovery.verification_refs, [case["qualified_node"]])
            self.assertEqual(state.status, "pending")
            self._assert_target_snapshot_unchanged(case)

    def _check_correction_preserves_unrelated_auto_agents_blocker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]
            revision = "engine-with-strict-selector-blocker-binding"

            with patch.object(
                orchestrator,
                "_installed_engine_revision",
                return_value=revision,
            ):
                self.assertTrue(orchestrator._resume_blocked_run(state))

            tasks = orchestrator._load_tasks_from_plan()
            source_task = next(
                task
                for task in tasks
                if task.task_id == case["source_task"].task_id
            )
            source_task.verification_refs = [case["qualified_node"]]
            state.tasks = tasks
            orchestrator._persist_tasks(tasks)
            unrelated = {
                "owner": "auto_agents",
                "category": "scheduler_invariant",
                "status": "blocked",
                "reason": "an unrelated scheduler invariant remains unresolved",
                "root_cause_diagnosis": {},
            }
            state.status = "blocked"
            state.last_error = unrelated["reason"]
            state.active_blocker = copy.deepcopy(unrelated)
            save_run_state(root, state)

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value=revision,
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    wraps=orchestrator._run_persisted_selector_probe,
                ) as probe,
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertEqual(probe.call_count, 0)
            observed = dict(state.active_blocker)
            observed.pop("updated_at", None)
            self.assertEqual(observed, unrelated)
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.last_error, unrelated["reason"])
            self.assertFalse(
                any(
                    key.startswith(
                        "persisted_selector_correction:"
                        f"{case['incident'].incident_id}:"
                    )
                    for key in state.resume_context.get(
                        Orchestrator.INSTALLED_ENGINE_RECOVERY_CONTEXT,
                        {},
                    )
                )
            )
            self._assert_target_snapshot_unchanged(case)

    def _check_legacy_reclassification_preserves_unrelated_diagnosed_blocker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            Orchestrator.init_project(root, "project", "mock")
            case = self._persisted_selector_case(root)
            orchestrator = case["orchestrator"]
            state = case["state"]
            reason = (
                "an unrelated scheduler invariant remains unresolved while "
                f"task {case['source_task'].task_id} processed "
                f"{case['malformed_command'].split()[-1]}"
            )
            unrelated = {
                "owner": "auto_agents",
                "category": "scheduler_invariant",
                "fingerprint": "0123456789abcdef01234567",
                "status": "blocked",
                "reason": reason,
                "checkpoint": {
                    "stage": "implement",
                    "head": case["checkpoint"]["head"],
                    "worktree": case["checkpoint"]["worktree"],
                },
                "root_cause_diagnosis": {
                    "diagnosis_id": "unrelated-scheduler-diagnosis",
                    "evidence_path": ".auto-agents/runs/run/root-cause/evidence.json",
                    "final": {
                        "owner": "auto_agents",
                        "category": "scheduler_invariant",
                        "verdict": "FINAL",
                        "generic": True,
                        "resume_strategy": "repair_and_resume",
                        "causal_chain": [reason],
                    },
                },
            }
            state.status = "blocked"
            state.last_error = reason
            state.active_blocker = copy.deepcopy(unrelated)
            save_run_state(root, state)
            original_history = copy.deepcopy(case["incident"].history)
            self.assertFalse(
                orchestrator._persisted_baseline_identity_blocker_matches(
                    state,
                    case["incident"],
                    unrelated,
                )
            )

            with (
                patch.object(
                    orchestrator,
                    "_installed_engine_revision",
                    return_value="engine-with-selector-reclassification",
                ),
                patch.object(
                    orchestrator,
                    "_run_persisted_selector_probe",
                    return_value=GateResult(
                        ok=False,
                        commands=[
                            self._missing_result(
                                case["malformed_command"],
                                case["malformed_command"].split()[-1],
                            )
                        ],
                    ),
                ) as probe,
            ):
                changed = orchestrator._resume_blocked_run(state)

            self.assertFalse(changed)
            self.assertEqual(probe.call_count, 0)
            self.assertEqual(state.active_blocker, unrelated)
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.last_error, reason)
            persisted = ExecutionIncidentStore(root, state.run_id).load(
                case["incident"].incident_id
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(
                persisted.kind,
                BASELINE_FAILURE_IDENTITY_INCIDENT_KIND,
            )
            self.assertEqual(persisted.history, original_history)
            self.assertNotIn(
                f"persisted_current_selector:{case['incident'].incident_id}",
                state.resume_context.get(
                    Orchestrator.INSTALLED_ENGINE_RECOVERY_CONTEXT,
                    {},
                ),
            )
            self._assert_target_snapshot_unchanged(case)


def test_current_and_baseline_missing_pytest_target_routes_target_recovery() -> None:
    GateCurrentSelectorRoutingTests()._check_current_and_baseline_missing_routes_target_recovery()


def test_persisted_missing_selector_reclassifies_without_losing_task_worktree() -> None:
    GateCurrentSelectorRoutingTests()._check_persisted_missing_selector_reclassifies_without_losing_task_worktree()


def test_corrected_class_qualified_selector_resumes_pending_verification() -> None:
    GateCurrentSelectorRoutingTests()._check_corrected_class_qualified_selector_resumes_pending_verification()


def test_recovery_task_correction_resumes_pending_verification() -> None:
    GateCurrentSelectorRoutingTests()._check_recovery_task_correction_resumes_pending_verification()


def test_noncollecting_pytest_correction_is_rejected() -> None:
    GateCurrentSelectorRoutingTests()._check_noncollecting_pytest_correction_is_rejected()


def test_corrected_selector_requires_matching_blocker_checkpoint() -> None:
    GateCurrentSelectorRoutingTests()._check_corrected_selector_requires_matching_blocker_checkpoint()


def test_diagnosed_selector_blocker_reclassifies_bound_incident() -> None:
    GateCurrentSelectorRoutingTests()._check_diagnosed_selector_blocker_reclassifies_bound_incident()


def test_persisted_selector_uses_recorded_command_after_renderer_drift() -> None:
    GateCurrentSelectorRoutingTests()._check_persisted_selector_uses_recorded_command_after_renderer_drift()


def test_selector_reclassification_preempts_generic_resume() -> None:
    GateCurrentSelectorRoutingTests()._check_selector_reclassification_preempts_generic_resume()


def test_rejected_probe_runs_once_per_installed_engine_revision() -> None:
    GateCurrentSelectorRoutingTests()._check_rejected_probe_runs_once_per_installed_engine_revision()


def test_valid_current_probe_preserves_baseline_self_repair() -> None:
    GateCurrentSelectorRoutingTests()._check_valid_current_probe_preserves_baseline_self_repair()


def test_corrected_selector_preflight_retries_after_checkpoint_restore() -> None:
    GateCurrentSelectorRoutingTests()._check_correction_preflight_retries_after_checkpoint_restore()


def test_corrected_selector_does_not_clear_unrelated_auto_agents_blocker() -> None:
    GateCurrentSelectorRoutingTests()._check_correction_preserves_unrelated_auto_agents_blocker()


def test_legacy_selector_reclassification_preserves_unrelated_diagnosed_blocker() -> None:
    GateCurrentSelectorRoutingTests()._check_legacy_reclassification_preserves_unrelated_diagnosed_blocker()


if __name__ == "__main__":
    unittest.main()
