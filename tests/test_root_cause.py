from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.io_utils import write_json, write_text
from auto_agents.config import load_run_state
from auto_agents.models import (
    AccelerationConfig,
    AgentResult,
    RunState,
    SelfRepairDiagnosisConfig,
)
from auto_agents.root_cause import RootCauseCoordinator
from auto_agents.repair_cases import RepairCase
from auto_agents.process_supervision import RunInterruptedError
from auto_agents.self_repair import (
    AutoAgentsSelfRepairRunner,
    SelfRepairDecision,
    SelfRepairResult,
    _FullSuiteShard,
    _VerificationResult,
    adjudicate_repair_case,
    self_repair_verification_command,
)
from auto_agents.self_repair_search import (
    SelfRepairCandidateRecord,
    SelfRepairExperiment,
    SelfRepairExperimentStore,
)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
    )
    write_text(root / "README.md", "baseline\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)


def _report(
    *,
    role: str,
    verdict: str,
    owner: str = "auto_agents",
    category: str = "retry_restore_invariant",
    confidence: float = 0.96,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "role": role,
        "verdict": verdict,
        "owner": owner,
        "confidence": confidence,
        "category": category,
        "generic": owner == "auto_agents",
        "safe_to_repair": owner == "auto_agents",
        "causal_chain": [
            "restore omitted the Git index",
            "the next retry inherited a protected staged path",
        ],
        "evidence": [
            {
                "kind": "source",
                "ref": "src/auto_agents/orchestrator.py:4500",
                "claim": "restore copied worktree bytes without index state",
            },
            {
                "kind": "test",
                "ref": "tests/test_retry_flow.py",
                "claim": "focused reproduction retained the staged path",
            },
        ],
        "rejected_hypotheses": ["the provider independently edited the file four times"],
        "reproduction_commands": ["pytest -q tests/test_retry_flow.py"],
        "reproduction_outcome": "the invariant failure reproduced",
        "proposed_fix_scope": ["src/auto_agents/orchestrator.py"],
        "verification_commands": [
            "python -m pytest -q tests/test_retry_flow.py -k restore"
        ],
        "resume_strategy": (
            "repair_and_resume" if owner == "auto_agents" else "target_recovery"
        ),
    }


class _FakeOrchestrator:
    def __init__(
        self,
        responses,
        *,
        mutate_target: Optional[Path] = None,
        investigator_tool_count: int = 0,
        acceleration: Optional[AccelerationConfig] = None,
    ) -> None:
        self.responses = list(responses)
        self.requests = []
        self.mutate_target = mutate_target
        self.investigator_tool_count = investigator_tool_count
        self.config = type(
            "Config",
            (),
            {
                "efforts": {
                    "self_repair": "deep",
                    "self_repair_review": "max",
                },
                "execution": type(
                    "Execution",
                    (),
                    {"acceleration": acceleration},
                )(),
            },
        )()

    def _call_with_failover(self, request):
        self.requests.append(request)
        if self.mutate_target is not None and len(self.requests) == 1:
            write_text(self.mutate_target / "README.md", "mutated\n")
        if (
            request.stage == "self_repair_investigator"
            and self.investigator_tool_count
        ):
            write_json(
                request.output_path.parent
                / "provider-attempts"
                / "root-cause-investigator-fake-resume-0.json",
                {
                    "events": [
                        {"kind": "tool_completed"}
                        for _ in range(self.investigator_tool_count)
                    ]
                },
            )
        payload = self.responses.pop(0)
        return AgentResult(
            ok=True,
            command=[],
            output_path=request.output_path,
            summary=json.dumps(payload),
        )


class RootCauseCoordinatorTests(unittest.TestCase):
    def assert_parseable_utc(self, value: object) -> None:
        timestamp = datetime.fromisoformat(str(value))
        self.assertIsNotNone(timestamp.tzinfo)
        self.assertEqual(timestamp.utcoffset(), timedelta(0))

    def test_diagnostic_snapshot_keeps_archived_requirement_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "snapshot"
            write_json(
                source
                / ".auto-agents"
                / "history"
                / "task_plans"
                / "old-run.json",
                {"tasks": [{"task_id": "task-old", "status": "done"}]},
            )
            write_json(
                source
                / ".auto-agents"
                / "runs"
                / "run-123"
                / "large-generated.json",
                {"generated": True},
            )
            write_text(source / ".env", "API_KEY=secret\n")
            write_text(
                source / ".auto-agents" / "operator" / "answers.json",
                "secret\n",
            )

            RootCauseCoordinator._copy_diagnostic_tree(source, destination)

            self.assertTrue(
                (
                    destination
                    / ".auto-agents"
                    / "history"
                    / "task_plans"
                    / "old-run.json"
                ).is_file()
            )
            self.assertFalse((destination / ".auto-agents" / "runs").exists())
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / ".auto-agents" / "operator").exists())

    def test_diagnostic_snapshot_preserves_read_only_git_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "snapshot"
            _init_repo(source)
            write_text(source / "history.py", "VALUE = 1\n")
            subprocess.run(["git", "add", "-A"], cwd=source, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "history evidence"],
                cwd=source,
                check=True,
            )

            RootCauseCoordinator._copy_diagnostic_tree(source, destination)

            log = subprocess.run(
                ["git", "log", "--oneline"],
                cwd=destination,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
            ).stdout
            self.assertIn("history evidence", log)
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--short"],
                    cwd=source,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=True,
                ).stdout,
                "",
            )

    def test_self_repair_pytest_command_avoids_root_module_shadowing(self):
        repo_root = Path(__file__).resolve().parents[1]
        command = self_repair_verification_command(
            "python -m pytest -q -p no:cacheprovider "
            "tests/test_provider_contract_policy.py "
            "-k composite_recovery_provenance_header",
            repo_root,
        )

        result = subprocess.run(
            command,
            cwd=repo_root,
            shell=True,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("1 passed", result.stdout)
        self.assertIn("pytest.main", command)
        self.assertNotIn(" -m pytest ", command)

    def test_self_repair_verification_preserves_non_pytest_commands(self):
        self.assertEqual(
            self_repair_verification_command(
                "python -m unittest -q",
                Path("/tmp/repo"),
            ),
            "python -m unittest -q",
        )

    def test_self_repair_verification_strips_redundant_repository_cd(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp) / "repair"
            repair_root.mkdir()

            command = self_repair_verification_command(
                "cd auto_agents && test -d .",
                repair_root,
                repository_aliases={"auto_agents"},
            )

            self.assertEqual(command, "test -d .")
            result = subprocess.run(
                command,
                cwd=repair_root,
                shell=True,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_self_repair_verification_keeps_real_nested_repository_cd(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp) / "repair"
            (repair_root / "auto_agents").mkdir(parents=True)

            command = self_repair_verification_command(
                "cd auto_agents && test -d .",
                repair_root,
                repository_aliases={"auto_agents"},
            )

            self.assertEqual(command, "cd auto_agents && test -d .")

    def test_supplemental_pytest_with_no_selected_tests_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp)
            write_text(
                repair_root / "test_sample.py",
                "def test_present():\n    assert True\n",
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=repair_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )

            result = runner._run_verification_commands(
                ["python -m pytest -q test_sample.py -k missing_selector"],
                repair_root,
                allow_pytest_no_tests=True,
            )

            self.assertTrue(result.ok, result.summary)
            self.assertIn("exit=5", result.summary)
            self.assertIn("nonfatal=supplemental pytest selector", result.summary)

    def test_required_pytest_with_no_selected_tests_remains_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp)
            write_text(
                repair_root / "test_sample.py",
                "def test_present():\n    assert True\n",
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=repair_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )

            result = runner._run_verification_commands(
                ["python -m pytest -q test_sample.py -k missing_selector"],
                repair_root,
            )

            self.assertFalse(result.ok)
            self.assertIn("exit=5", result.summary)

    def test_supplemental_pytest_failure_remains_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp)
            write_text(
                repair_root / "test_sample.py",
                "def test_failure():\n    assert False\n",
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=repair_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )

            result = runner._run_verification_commands(
                ["python -m pytest -q test_sample.py"],
                repair_root,
                allow_pytest_no_tests=True,
            )

            self.assertFalse(result.ok)
            self.assertIn("exit=1", result.summary)

    def test_target_placeholder_supplement_is_skipped_before_candidate_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp) / "repair"
            _init_repo(repair_root)
            diagnosis = type(
                "Diagnosis",
                (),
                {
                    "final": type(
                        "Final",
                        (),
                        {
                            "verification_commands": [
                                "auto-agents validate --project /path/to/target",
                                f"auto-agents validate --project {repair_root}",
                                (
                                    "After supplying the approved URL through WAIT_USER, "
                                    "rerun target/.conda/bin/python -m pytest -q "
                                    "target/tests/system/test_boundary.py"
                                ),
                                "git status --short",
                                "cd auto_agents && python -m unittest -q",
                            ]
                        },
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=repair_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
                diagnosis=diagnosis,
            )

            with patch(
                "auto_agents.self_repair.self_repair_verify_commands",
                return_value=["python -m unittest -q"],
            ):
                result = runner._run_verification(repair_root)

            self.assertTrue(result.ok, result.summary)
            self.assertIn("skipped=supplemental unresolved example path", result.summary)
            self.assertIn(
                "skipped=supplemental target-project validation belongs to post-resume verification",
                result.summary,
            )
            self.assertIn(
                "skipped=supplemental unsupported candidate-worktree verification command",
                result.summary,
            )
            self.assertIn("$ git status --short", result.summary)
            self.assertIn("$ python -m unittest -q", result.summary)
            self.assertNotIn("missing config file", result.summary)

    def test_unsupported_supplemental_shell_command_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp) / "repair"
            _init_repo(repair_root)
            diagnosis = type(
                "Diagnosis",
                (),
                {
                    "final": type(
                        "Final",
                        (),
                        {"verification_commands": ["touch should-not-exist"]},
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=repair_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
                diagnosis=diagnosis,
            )

            with patch(
                "auto_agents.self_repair.self_repair_verify_commands",
                return_value=["python -m unittest -q"],
            ):
                result = runner._run_verification(repair_root)

            self.assertTrue(result.ok, result.summary)
            self.assertFalse((repair_root / "should-not-exist").exists())
            self.assertIn(
                "skipped=supplemental unsupported candidate-worktree verification command",
                result.summary,
            )

    def test_supplemental_read_only_prefix_cannot_hide_shell_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repair_root = Path(tmp) / "repair"
            _init_repo(repair_root)
            diagnosis = type(
                "Diagnosis",
                (),
                {
                    "final": type(
                        "Final",
                        (),
                        {
                            "verification_commands": [
                                "git status --short && touch should-not-exist"
                            ]
                        },
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=repair_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
                diagnosis=diagnosis,
            )

            with patch(
                "auto_agents.self_repair.self_repair_verify_commands",
                return_value=["python -m unittest -q"],
            ):
                result = runner._run_verification(repair_root)

            self.assertTrue(result.ok, result.summary)
            self.assertFalse((repair_root / "should-not-exist").exists())
            self.assertIn(
                "skipped=supplemental shell control operators are not allowed",
                result.summary,
            )

    def _coordinator(
        self,
        root: Path,
        responses,
        *,
        mutate=False,
        investigator_tool_count=0,
        repair_case=None,
    ):
        auto_root = root / "auto"
        target_root = root / "target"
        _init_repo(auto_root)
        _init_repo(target_root)
        state = RunState(run_id="run-123", status="blocked")
        write_json(
            target_root / ".auto-agents" / "state" / "run_state.json",
            state.to_dict(),
        )
        fake = _FakeOrchestrator(
            responses,
            mutate_target=(target_root if mutate else None),
            investigator_tool_count=investigator_tool_count,
        )
        coordinator = RootCauseCoordinator(
            fake,
            auto_agents_root=auto_root,
            target_root=target_root,
            error=RuntimeError("terminal failure"),
            state=state,
            traceback_text="traceback",
            heuristic={"eligible": False},
            runtime_evidence={},
            config=SelfRepairDiagnosisConfig(),
            repair_case=repair_case,
        )
        return coordinator, fake, target_root

    def test_dual_agent_consensus_approves_concrete_auto_agents_root_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, fake, target = self._coordinator(
                Path(tmp),
                [
                    _report(role="investigator", verdict="ROOT_CAUSE"),
                    _report(role="reviewer", verdict="AGREE"),
                ],
            )

            diagnosis = coordinator.run()

            self.assertTrue(diagnosis.repair_approved)
            self.assertEqual(len(fake.requests), 2)
            self.assertEqual(
                [item.stage for item in fake.requests],
                ["self_repair_investigator", "self_repair_reviewer"],
            )
            self.assertTrue(
                all(item.sandbox_mode == "read-only" for item in fake.requests)
            )
            self.assertTrue(all(item.effort == "max" for item in fake.requests))
            self.assertTrue(
                all(
                    not item.record_execution_incidents
                    for item in fake.requests
                )
            )
            self.assertTrue(Path(diagnosis.evidence_path).is_file())
            artifact = json.loads(
                (
                    target
                    / ".auto-agents"
                    / "runs"
                    / "run-123"
                    / "root-cause"
                    / diagnosis.diagnosis_id
                    / "diagnosis.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(artifact["repair_approved"])

    def test_exact_diagnosis_certificate_skips_repeated_agent_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            state = RunState(run_id="run-123", status="blocked")
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                state.to_dict(),
            )
            fake = _FakeOrchestrator(
                [
                    _report(role="investigator", verdict="ROOT_CAUSE"),
                    _report(role="reviewer", verdict="AGREE"),
                ],
                acceleration=AccelerationConfig(
                    parallel_diagnosis_enabled=False,
                ),
            )

            def coordinator() -> RootCauseCoordinator:
                return RootCauseCoordinator(
                    fake,
                    auto_agents_root=auto_root,
                    target_root=target_root,
                    error=RuntimeError("terminal failure"),
                    state=state,
                    traceback_text="traceback",
                    heuristic={"eligible": False},
                    runtime_evidence={},
                    config=SelfRepairDiagnosisConfig(),
                )

            first = coordinator().run()
            second = coordinator().run()

            self.assertTrue(first.repair_approved)
            self.assertTrue(second.repair_approved)
            self.assertEqual(second.reason, "reused root-cause diagnosis certificate")
            self.assertEqual(len(fake.requests), 2)

    def test_independent_diagnoses_run_in_parallel_and_require_consensus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            state = RunState(run_id="run-123", status="blocked")
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                state.to_dict(),
            )
            arrived = {
                "self_repair_investigator": threading.Event(),
                "self_repair_reviewer": threading.Event(),
            }
            responses = {
                "self_repair_investigator": _report(
                    role="investigator", verdict="ROOT_CAUSE"
                ),
                "self_repair_reviewer": _report(
                    role="reviewer", verdict="ROOT_CAUSE"
                ),
            }

            class ParallelFake(_FakeOrchestrator):
                def _call_with_failover(self, request):
                    self.requests.append(request)
                    arrived[request.stage].set()
                    peer = (
                        "self_repair_reviewer"
                        if request.stage == "self_repair_investigator"
                        else "self_repair_investigator"
                    )
                    if not arrived[peer].wait(timeout=2):
                        raise RuntimeError("diagnosis roles did not overlap")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=json.dumps(responses[request.stage]),
                    )

            fake = ParallelFake(
                [],
                acceleration=AccelerationConfig(
                    diagnosis_cache_enabled=False,
                    parallel_diagnosis_enabled=True,
                ),
            )
            diagnosis = RootCauseCoordinator(
                fake,
                auto_agents_root=auto_root,
                target_root=target_root,
                error=RuntimeError("terminal failure"),
                state=state,
                traceback_text="traceback",
                heuristic={"eligible": False},
                runtime_evidence={},
                config=SelfRepairDiagnosisConfig(),
            ).run()

            self.assertTrue(diagnosis.repair_approved)
            self.assertEqual(len(fake.requests), 2)

    def test_run_interruption_bypasses_root_cause_failure_conversion(self):
        repair_case = RepairCase(
            case_id="interrupt-case",
            run_id="run-123",
            source="health_watch",
            kind="goal_stalled",
            severity="confirmed",
            symptom="run was interrupted",
        )
        state = RunState(run_id="run-123", status="pending")
        diagnosis_config = SelfRepairDiagnosisConfig()
        orchestrator = type(
            "Orchestrator",
            (),
            {
                "config": type(
                    "Config",
                    (),
                    {
                        "execution": type(
                            "Execution",
                            (),
                            {"self_repair_diagnosis": diagnosis_config},
                        )()
                    },
                )()
            },
        )()

        with patch.object(
            RootCauseCoordinator,
            "run",
            side_effect=RunInterruptedError(signal.SIGINT),
        ):
            with self.assertRaises(RunInterruptedError):
                adjudicate_repair_case(
                    orchestrator,
                    target_project_root=Path("/tmp/target"),
                    repair_case=repair_case,
                    state=state,
                )

    def test_health_case_reuses_consensus_and_requires_boundary_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            health_case = RepairCase(
                case_id="health-case",
                run_id="run-123",
                source="health_watch",
                kind="goal_stalled",
                severity="confirmed",
                symptom="activity without goal progress",
                expected_postconditions=["durable progress resumes"],
            )
            investigator = _report(role="investigator", verdict="ROOT_CAUSE")
            reviewer = _report(role="reviewer", verdict="AGREE")
            for report in (investigator, reviewer):
                report["expected_postconditions"] = ["durable progress resumes"]
            coordinator, _fake, _target = self._coordinator(
                Path(tmp),
                [investigator, reviewer],
                repair_case=health_case,
            )
            self.assertTrue(coordinator.run().repair_approved)

        with tempfile.TemporaryDirectory() as tmp:
            investigator = _report(role="investigator", verdict="ROOT_CAUSE")
            reviewer = _report(role="reviewer", verdict="AGREE")
            for report in (investigator, reviewer):
                report["expected_postconditions"] = ["durable progress resumes"]
                report["verification_commands"] = []
            coordinator, _fake, _target = self._coordinator(
                Path(tmp),
                [investigator, reviewer],
                repair_case=health_case,
            )
            self.assertFalse(coordinator.run().repair_approved)

    def test_max_autonomy_attempts_reversible_candidate_before_safety_is_proven(self):
        with tempfile.TemporaryDirectory() as tmp:
            investigator = _report(role="investigator", verdict="ROOT_CAUSE")
            reviewer = _report(role="reviewer", verdict="AGREE")
            for report in (investigator, reviewer):
                report["safe_to_repair"] = False
                report["safe_to_attempt"] = True
                report["repair_risk"] = "reversible_code"
            coordinator, _fake, _target = self._coordinator(
                Path(tmp),
                [investigator, reviewer],
            )

            diagnosis = coordinator.run()

            self.assertTrue(diagnosis.repair_approved)
            self.assertTrue(diagnosis.final.effective_safe_to_attempt)

    def test_human_boundary_never_enters_candidate_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            investigator = _report(role="investigator", verdict="ROOT_CAUSE")
            reviewer = _report(role="reviewer", verdict="AGREE")
            for report in (investigator, reviewer):
                report["safe_to_attempt"] = True
                report["human_boundary"] = True
                report["repair_risk"] = "credential_required"
            coordinator, _fake, _target = self._coordinator(
                Path(tmp),
                [investigator, reviewer],
            )

            diagnosis = coordinator.run()

            self.assertFalse(diagnosis.repair_approved)

    def test_disagreement_requires_high_confidence_arbiter(self):
        with tempfile.TemporaryDirectory() as tmp:
            investigator = _report(
                role="investigator",
                verdict="ROOT_CAUSE",
            )
            reviewer = _report(
                role="reviewer",
                verdict="DISAGREE",
                owner="target_project",
                category="target_contract_failure",
            )
            arbiter = _report(
                role="arbiter",
                verdict="FINAL",
                confidence=0.94,
            )
            coordinator, fake, _target = self._coordinator(
                Path(tmp),
                [investigator, reviewer, arbiter],
            )

            diagnosis = coordinator.run()

            self.assertTrue(diagnosis.repair_approved)
            self.assertIsNotNone(diagnosis.arbiter)
            self.assertEqual(len(fake.requests), 3)

    def test_generic_or_safety_disagreement_requires_arbiter(self):
        with tempfile.TemporaryDirectory() as tmp:
            investigator = _report(
                role="investigator",
                verdict="ROOT_CAUSE",
            )
            investigator["generic"] = False
            reviewer = _report(
                role="reviewer",
                verdict="AGREE",
            )
            arbiter = _report(
                role="arbiter",
                verdict="FINAL",
                confidence=0.94,
            )
            coordinator, fake, _target = self._coordinator(
                Path(tmp),
                [investigator, reviewer, arbiter],
            )

            diagnosis = coordinator.run()

            self.assertTrue(diagnosis.repair_approved)
            self.assertEqual(len(fake.requests), 3)

    def test_target_project_consensus_does_not_start_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, _fake, _target = self._coordinator(
                Path(tmp),
                [
                    _report(
                        role="investigator",
                        verdict="ROOT_CAUSE",
                        owner="target_project",
                        category="target_contract_failure",
                    ),
                    _report(
                        role="reviewer",
                        verdict="AGREE",
                        owner="target_project",
                        category="target_contract_failure",
                    ),
                ],
            )

            diagnosis = coordinator.run()

            self.assertFalse(diagnosis.repair_approved)
            self.assertEqual(diagnosis.final.owner, "target_project")

    def test_diagnostic_agent_mutation_of_original_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, _fake, _target = self._coordinator(
                Path(tmp),
                [
                    _report(role="investigator", verdict="ROOT_CAUSE"),
                    _report(role="reviewer", verdict="AGREE"),
                ],
                mutate=True,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "diagnostic mutation invariant failed",
            ):
                coordinator.run()

    def test_vim_swap_disappearing_during_diagnosis_does_not_trip_invariant(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, fake, target = self._coordinator(
                Path(tmp),
                [
                    _report(role="investigator", verdict="ROOT_CAUSE"),
                    _report(role="reviewer", verdict="AGREE"),
                ],
            )
            swap_path = target / ".README.md.swp"
            swap_path.write_bytes(b"vim recovery data")
            original_call = fake._call_with_failover

            def remove_swap_then_call(request):
                swap_path.unlink(missing_ok=True)
                return original_call(request)

            fake._call_with_failover = remove_swap_then_call

            diagnosis = coordinator.run()

            self.assertTrue(diagnosis.repair_approved)
            self.assertFalse(swap_path.exists())

    def test_terminal_evidence_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, _fake, target = self._coordinator(
                Path(tmp),
                [
                    _report(
                        role="investigator",
                        verdict="ROOT_CAUSE",
                        owner="target_project",
                        category="target_contract_failure",
                    ),
                    _report(
                        role="reviewer",
                        verdict="AGREE",
                        owner="target_project",
                        category="target_contract_failure",
                    ),
                ],
            )
            write_text(target / "README.md", "api_key = super-secret-token\n")

            diagnosis = coordinator.run()

            evidence = Path(diagnosis.evidence_path).read_text(encoding="utf-8")
            self.assertNotIn("super-secret-token", evidence)
            self.assertIn("[REDACTED]", evidence)

    def test_one_command_over_soft_budget_keeps_valid_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, _fake, _target = self._coordinator(
                Path(tmp),
                [
                    _report(role="investigator", verdict="ROOT_CAUSE"),
                    _report(role="reviewer", verdict="AGREE"),
                ],
                investigator_tool_count=13,
            )

            diagnosis = coordinator.run()

            self.assertTrue(diagnosis.repair_approved)

    def test_failure_signature_normalizes_isolated_worktree_paths(self):
        base = (
            "$ /env/python -c \"import sys; sys.path.insert(0, "
            "'/tmp/auto-agents-remote-repair-check-aaa/verification/src')\" "
            "-q tests\nexit=-15\ncommand timed out after 900s"
        )
        candidate = (
            "$ /env/python -c \"import sys; sys.path.insert(0, "
            "'/tmp/auto-agents-self-repair-worktree-bbb/repair/src')\" "
            "-q tests\nexit=-15\ncommand timed out after 900s"
        )

        self.assertEqual(
            AutoAgentsSelfRepairRunner._verification_failure_signature(base),
            AutoAgentsSelfRepairRunner._verification_failure_signature(candidate),
        )

    def test_self_repair_refreshes_changed_engine_base_before_candidate_generation(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            old_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            write_text(auto_root / "engine.py", "REVISION = 2\n")
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "advance engine"],
                cwd=auto_root,
                check=True,
            )
            live_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                RunState(run_id="target-run").to_dict(),
            )
            store = SelfRepairExperimentStore(
                target_root,
                "target-run",
                "revision-refresh",
            )
            experiment = SelfRepairExperiment.create(
                run_id="target-run",
                root_fingerprint="revision-refresh",
                category="revision-refresh",
                base_commit=old_head,
            )
            experiment.attempt_count = 2
            experiment.consecutive_non_improvements = 2
            experiment.candidates["stale"] = SelfRepairCandidateRecord(
                candidate_id="stale",
                candidate_ref="refs/stale",
                candidate_commit=old_head,
            )
            experiment.frontier = ["stale"]
            experiment.best_safe_candidate_id = "stale"
            experiment.best_safe_ref = "refs/stale"
            experiment.best_search_candidate_id = "stale"
            experiment.best_search_ref = "refs/stale"
            store.save(experiment)
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(
                    True,
                    category="revision-refresh",
                ),
            )
            runner.repo_root = auto_root
            observed = {}

            def generate_candidate(**_kwargs):
                persisted = store.load()
                self.assertIsNotNone(persisted)
                observed["experiment"] = persisted
                return SelfRepairResult(
                    ok=True,
                    status="approved_candidate",
                    reason="candidate lifecycle reached",
                    candidate_id="refreshed-candidate",
                    candidate_ref=live_head,
                    candidate_commit=live_head,
                )

            with (
                patch.object(
                    runner,
                    "_run_candidate",
                    side_effect=generate_candidate,
                ) as run_candidate,
                patch.object(runner, "_record_candidate_result"),
            ):
                result = runner.run()

            self.assertTrue(result.ok)
            run_candidate.assert_called_once()
            self.assertEqual(run_candidate.call_args.kwargs["attempt"], 3)
            refreshed = observed["experiment"]
            self.assertEqual(refreshed.base_commit, live_head)
            self.assertEqual(refreshed.best_safe_candidate_id, "base")
            self.assertEqual(refreshed.best_search_candidate_id, "base")
            self.assertEqual(refreshed.frontier, [])
            self.assertEqual(refreshed.attempt_count, 2)
            self.assertTrue(refreshed.candidates["stale"].fatal)
            self.assertEqual(refreshed.candidates["base"].status, "base_refresh")
            revision_change = refreshed.health_history[-1]
            self.assertEqual(revision_change["anomaly"], "base_revision_changed")
            self.assertEqual(revision_change["from"], old_head)
            self.assertEqual(revision_change["to"], live_head)
            self.assert_parseable_utc(revision_change["at"])

    def test_self_repair_reopens_needs_human_experiment_when_evidence_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            live_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                RunState(run_id="target-run").to_dict(),
            )
            store = SelfRepairExperimentStore(
                target_root,
                "target-run",
                "evidence-refresh",
            )
            experiment = SelfRepairExperiment.create(
                run_id="target-run",
                root_fingerprint="evidence-refresh",
                category="evidence-refresh",
                base_commit=live_head,
                evidence_fingerprint="stale-evidence",
            )
            experiment.status = "needs_human"
            experiment.attempt_count = 4
            experiment.consecutive_non_improvements = 3
            store.save(experiment)
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(
                    True,
                    category="evidence-refresh",
                ),
            )
            runner.repo_root = auto_root
            observed = {}

            def generate_candidate(**_kwargs):
                persisted = store.load()
                self.assertIsNotNone(persisted)
                observed["experiment"] = persisted
                return SelfRepairResult(
                    ok=True,
                    status="approved_candidate",
                    reason="candidate lifecycle reached",
                    candidate_id="reopened-candidate",
                    candidate_ref=live_head,
                    candidate_commit=live_head,
                )

            with (
                patch.object(
                    runner,
                    "_run_candidate",
                    side_effect=generate_candidate,
                ) as run_candidate,
                patch.object(runner, "_record_candidate_result"),
            ):
                result = runner.run()

            self.assertTrue(result.ok)
            run_candidate.assert_called_once()
            self.assertEqual(run_candidate.call_args.kwargs["attempt"], 5)
            reopened = observed["experiment"]
            self.assertEqual(reopened.status, "active")
            self.assertEqual(reopened.consecutive_non_improvements, 0)
            self.assertEqual(reopened.attempt_count, 4)
            self.assertNotEqual(reopened.evidence_fingerprint, "stale-evidence")
            evidence_change = reopened.health_history[-1]
            self.assertEqual(
                evidence_change["anomaly"],
                "operator_or_external_evidence_changed",
            )
            self.assert_parseable_utc(evidence_change["at"])

    def test_runner_automatically_corrects_non_progress_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)

            autonomy = type(
                "Autonomy",
                (),
                {
                    "mode": "max",
                    "max_consecutive_non_improving_candidates": 3,
                    "max_frontier_candidates": 8,
                    "candidate_timeout_seconds": 300,
                    "candidate_review_timeout_seconds": 60,
                    "replay_timeout_seconds": 60,
                    "allow_isolated_dirty_checkout": True,
                },
            )()
            orchestrator = type(
                "Orchestrator",
                (),
                {
                    "config": type(
                        "Config",
                        (),
                        {
                            "efforts": {
                                "self_repair": "deep",
                                "self_repair_review": "max",
                            },
                            "execution": type(
                                "Execution", (), {"autonomy": autonomy}
                            )(),
                        },
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                orchestrator,
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True, category="candidate-ranking"),
            )
            outcomes = [
                SelfRepairResult(
                    False,
                    "candidate_replay_failed",
                    "did not cross replay",
                    candidate_id="c1",
                    candidate_ref="HEAD",
                ),
                SelfRepairResult(
                    False,
                    "candidate_full_suite_failed",
                    "full suite changed",
                    candidate_id="c2",
                    candidate_commit=subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=auto_root,
                        check=True,
                        text=True,
                        capture_output=True,
                    ).stdout.strip(),
                    candidate_ref="HEAD",
                ),
                SelfRepairResult(
                    False,
                    "candidate_replay_failed",
                    "later replay failed",
                    candidate_id="c3",
                    candidate_ref="HEAD",
                ),
                SelfRepairResult(
                    False,
                    "candidate_replay_failed",
                    "another replay failed",
                    candidate_id="c4",
                    candidate_ref="HEAD",
                ),
                SelfRepairResult(
                    False,
                    "candidate_replay_failed",
                    "final replay failed",
                    candidate_id="c5",
                    candidate_ref="HEAD",
                ),
                SelfRepairResult(
                    True,
                    "approved_candidate",
                    "automatic redesign converged",
                    candidate_id="c6",
                    candidate_ref="HEAD",
                    candidate_commit=subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=auto_root,
                        check=True,
                        text=True,
                        capture_output=True,
                    ).stdout.strip(),
                ),
            ]
            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch.object(runner, "_run_candidate", side_effect=outcomes),
                patch.object(runner, "_record_candidate_result"),
            ):
                runner.repo_root = auto_root
                result = runner.run()

            self.assertEqual(result.candidate_id, "c6")
            self.assertEqual(result.status, "approved_candidate")
            state = load_run_state(target_root)
            store = SelfRepairExperimentStore(
                target_root,
                state.run_id,
                "candidate-ranking",
            )
            experiment = store.load()
            self.assertIsNotNone(experiment)
            self.assertTrue(experiment.automatic_corrections)
            self.assertNotEqual(experiment.status, "needs_human")

    def test_self_repair_health_preemption_is_infrastructure_interruption(self):
        self.assertTrue(
            AutoAgentsSelfRepairRunner._is_infrastructure_candidate_error(
                RuntimeError("self_repair_stagnation")
            )
        )
        self.assertTrue(
            AutoAgentsSelfRepairRunner._is_infrastructure_candidate_error(
                RuntimeError("health_quiesce")
            )
        )

    def test_recoverable_candidate_stops_new_generation_and_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            autonomy = type(
                "Autonomy",
                (),
                {
                    "mode": "max",
                    "max_consecutive_non_improving_candidates": 3,
                    "max_frontier_candidates": 8,
                    "candidate_timeout_seconds": 300,
                    "candidate_review_timeout_seconds": 60,
                    "replay_timeout_seconds": 60,
                    "allow_isolated_dirty_checkout": True,
                },
            )()
            orchestrator = type(
                "Orchestrator",
                (),
                {
                    "config": type(
                        "Config",
                        (),
                        {
                            "efforts": {"self_repair": "max"},
                            "execution": type(
                                "Execution", (), {"autonomy": autonomy}
                            )(),
                        },
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                orchestrator,
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True, category="candidate-ranking"),
            )
            recoverable = SelfRepairResult(
                False,
                "candidate_full_suite_inconclusive",
                "needs extended validation",
                candidate_id="c1",
                recoverable_validation=True,
            )
            with (
                patch.object(runner, "_run_candidate", return_value=recoverable) as run,
                patch.object(runner, "_record_candidate_result"),
            ):
                result = runner.run()

            self.assertEqual(run.call_count, 1)
            self.assertEqual(result.candidate_id, "c1")
            self.assertEqual(result.validation_rank, 90)
            self.assertTrue(result.recoverable_validation)
            self.assertFalse(result.infrastructure_failure)
            self.assertEqual(result.status, "candidate_full_suite_inconclusive")

    def test_candidate_search_uses_best_history_and_reaches_third_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                RunState(run_id="target-run").to_dict(),
            )
            autonomy = type(
                "Autonomy",
                (),
                {
                    "mode": "max",
                    "max_consecutive_non_improving_candidates": 3,
                    "max_frontier_candidates": 8,
                    "candidate_timeout_seconds": 300,
                    "candidate_review_timeout_seconds": 60,
                    "replay_timeout_seconds": 60,
                    "allow_isolated_dirty_checkout": True,
                },
            )()
            orchestrator = type(
                "Orchestrator",
                (),
                {
                    "config": type(
                        "Config",
                        (),
                        {
                            "efforts": {"self_repair": "max"},
                            "execution": type(
                                "Execution", (), {"autonomy": autonomy}
                            )(),
                        },
                    )()
                },
            )()
            finding_one = {
                "finding_id": "malformed-fail-open",
                "status": "confirmed",
                "reason": "malformed state disarms the guard",
                "counterexample": "ownership context is a list",
                "required_test": "malformed context remains blocked",
                "evidence": ["candidate review"],
            }
            finding_two = {
                "finding_id": "prepared-resume",
                "status": "confirmed",
                "reason": "prepared marker is restored during merge",
                "counterexample": "prepared commit equals the old repair",
                "required_test": "run the public resume entrypoint",
                "evidence": ["sealed replay"],
            }
            outcomes = [
                SelfRepairResult(
                    False,
                    "candidate_review_rejected",
                    "first partial repair",
                    candidate_id="c1",
                    candidate_ref="HEAD",
                    candidate_commit="HEAD",
                    review_findings=[finding_one],
                    finding_ids=["malformed-fail-open"],
                ),
                SelfRepairResult(
                    False,
                    "candidate_review_rejected",
                    "second partial repair",
                    candidate_id="c2",
                    candidate_ref="HEAD",
                    candidate_commit="HEAD",
                    review_findings=[finding_two],
                    finding_ids=["prepared-resume"],
                    resolved_finding_ids=["malformed-fail-open"],
                ),
                SelfRepairResult(
                    True,
                    "approved_candidate",
                    "third candidate closes the root",
                    candidate_id="c3",
                    candidate_ref="HEAD",
                    candidate_commit="HEAD",
                    resolved_finding_ids=["prepared-resume"],
                ),
            ]
            runner = AutoAgentsSelfRepairRunner(
                orchestrator,
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True, category="progress-search"),
            )
            seen_parents = []

            def next_candidate(**_kwargs):
                seen_parents.append(runner._experiment.best_search_candidate_id)
                return outcomes.pop(0)

            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch.object(runner, "_run_candidate", side_effect=next_candidate),
                patch.object(runner, "_record_candidate_result"),
            ):
                runner.repo_root = auto_root
                result = runner.run()

            self.assertTrue(result.ok)
            self.assertEqual(seen_parents, ["base", "c1", "c2"])
            self.assertEqual(runner._experiment.consecutive_non_improvements, 0)
            self.assertEqual(runner._experiment.status, "approved")

    def test_full_suite_uses_symmetric_progress_managed_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            (candidate_root / "tests").mkdir(parents=True)
            orchestrator = type(
                "Orchestrator",
                (),
                {
                    "config": type(
                        "Config",
                        (),
                        {"execution": object()},
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                orchestrator,
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            base_failure = _VerificationResult(
                False,
                "FAILED tests/test_existing.py::test_existing",
                returncodes=(1,),
                termination_reasons=("",),
            )
            candidate_passed = _VerificationResult(
                True,
                "$ <self-repair-worktree>/python -q tests\nexit=0\npassed",
                returncodes=(0,),
                termination_reasons=("",),
            )
            with (
                patch.object(
                    runner,
                    "_run_full_suite_at_ref",
                    return_value=base_failure,
                ) as base_verify,
                patch.object(
                    runner,
                    "_run_full_suite_shards",
                    return_value=candidate_passed,
                ) as candidate_verify,
            ):
                result = runner._full_suite_differential(
                    "base",
                    candidate_root,
                    deadline=time.monotonic() + 1500,
                )

            self.assertTrue(result.ok, result.summary)
            base_verify.assert_called_once_with("base")
            candidate_verify.assert_called_once_with(candidate_root)
            self.assertIn("candidate full suite", result.summary)

    def test_self_repair_prompt_delegates_broad_suite_to_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root

            prompt = runner._build_prompt()

            self.assertIn("Do not run the broad auto_agents suite", prompt)
            self.assertIn("orchestrator owns authoritative full-suite", prompt)

    def test_base_full_suite_prewarm_is_reused_by_differential(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            (auto_root / "tests").mkdir()
            (candidate_root / "tests").mkdir(parents=True)
            runner = AutoAgentsSelfRepairRunner(
                type(
                    "Orchestrator",
                    (),
                    {
                        "config": type(
                            "Config",
                            (),
                            {"execution": object()},
                        )()
                    },
                )(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            passed = _VerificationResult(True, "passed")
            with (
                patch.object(
                    runner,
                    "_run_full_suite_at_ref",
                    return_value=passed,
                ) as base_verify,
                patch.object(
                    runner,
                    "_run_full_suite_shards",
                    return_value=passed,
                ),
            ):
                runner._start_base_full_suite_prewarm("base")
                result = runner._full_suite_differential(
                    "base",
                    candidate_root,
                )

            self.assertTrue(result.ok)
            base_verify.assert_called_once_with("base")

    def test_equivalent_full_suite_timeouts_are_inconclusive_not_regressions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            (candidate_root / "tests").mkdir(parents=True)
            orchestrator = type(
                "Orchestrator",
                (),
                {
                    "config": type(
                        "Config",
                        (),
                        {"execution": object()},
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                orchestrator,
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            timeout = _VerificationResult(
                False,
                "$ /tmp/auto-agents-self-repair-worktree-x/repair/python -q tests\n"
                "exit=-15\ncommand timed out after 900s",
                returncodes=(-15,),
                termination_reasons=("timeout",),
                recoverable=True,
            )
            base_timeout = _VerificationResult(
                False,
                "$ /tmp/auto-agents-remote-repair-check-y/verification/python -q tests\n"
                "exit=-15\ncommand timed out after 900s",
                returncodes=(-15,),
                termination_reasons=("timeout",),
                recoverable=True,
            )
            with (
                patch.object(
                    runner,
                    "_run_full_suite_at_ref",
                    return_value=base_timeout,
                ),
                patch.object(
                    runner,
                    "_run_full_suite_shards",
                    return_value=timeout,
                ) as verify,
            ):
                result = runner._full_suite_differential(
                    "base",
                    candidate_root,
                    deadline=time.monotonic() + 100,
                )

            self.assertFalse(result.ok)
            self.assertTrue(result.recoverable)
            self.assertEqual(verify.call_count, 1)
            self.assertIn("checkpoint remains resumable", result.summary)

    def test_full_suite_shards_resume_from_persisted_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            _init_repo(candidate_root)
            write_text(
                candidate_root / "tests" / "test_a.py",
                "# subprocess-sensitive: keep this fixture serial\n"
                "def test_a():\n    assert True\n",
            )
            write_text(
                candidate_root / "tests" / "test_b.py",
                "# subprocess-sensitive: keep this fixture serial\n"
                "def test_b():\n    assert True\n",
            )
            subprocess.run(["git", "add", "-A"], cwd=candidate_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add tests"],
                cwd=candidate_root,
                check=True,
            )
            orchestrator = type(
                "Orchestrator",
                (),
                {
                    "config": type(
                        "Config",
                        (),
                        {"execution": object()},
                    )()
                },
            )()
            runner = AutoAgentsSelfRepairRunner(
                orchestrator,
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            runner._experiment_store = SelfRepairExperimentStore(
                target_root,
                "run",
                "root",
            )
            passed = _VerificationResult(
                True,
                "passed",
                commands=("pytest shard",),
                returncodes=(0,),
                termination_reasons=("",),
            )
            timeout = _VerificationResult(
                False,
                "command timed out",
                commands=("pytest shard",),
                returncodes=(-15,),
                termination_reasons=("timeout",),
            )
            with patch.object(
                runner,
                "_run_verification_commands",
                side_effect=[passed, timeout],
            ) as first_verify:
                first = runner._run_full_suite_shards(candidate_root)

            self.assertFalse(first.ok)
            self.assertTrue(first.recoverable)
            self.assertEqual(first_verify.call_count, 2)
            first_call = first_verify.call_args_list[0]
            self.assertEqual(first_call.kwargs["command_timeout_seconds"], 14_400)
            self.assertTrue(first_call.kwargs["adaptive_timeout_enabled"])
            self.assertEqual(
                first_call.kwargs["command_idle_timeout_seconds"],
                900,
            )
            with patch.object(
                runner,
                "_run_verification_commands",
                return_value=passed,
            ) as resumed_verify:
                resumed = runner._run_full_suite_shards(candidate_root)

            self.assertTrue(resumed.ok, resumed.summary)
            resumed_verify.assert_called_once()
            self.assertIn("test_a.py cached=true", resumed.summary)
            self.assertIn("completed=2/2", resumed.summary)

    def test_full_suite_collection_classifies_resources_per_node_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            tests = "\n\n".join(
                f"def test_{index:02d}():\n    assert True"
                for index in range(25)
            )
            write_text(auto_root / "tests" / "test_pure.py", tests + "\n")
            write_text(
                auto_root / "tests" / "test_recovery_process.py",
                "import subprocess\n\n"
                "def test_process_contract():\n    assert subprocess is not None\n",
            )
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add shard fixtures"],
                cwd=auto_root,
                check=True,
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            runner._experiment_store = SelfRepairExperimentStore(
                target_root,
                "run",
                "batching",
            )
            write_json(
                runner._full_suite_timing_path(),
                {
                    "schema_version": 1,
                    "samples": {"tests/test_pure.py": [180.0]},
                },
            )

            shards = runner._collect_full_suite_shards(auto_root)

            pure = [item for item in shards if item.test_file == "tests/test_pure.py"]
            unsafe = [
                item
                for item in shards
                if item.test_file == "tests/test_recovery_process.py"
            ]
            self.assertEqual(len(pure), 2)
            self.assertTrue(all(item.parallel_safe for item in pure))
            self.assertEqual(len(unsafe), 1)
            self.assertTrue(unsafe[0].parallel_safe)
            self.assertTrue(unsafe[0].isolated)
            self.assertEqual(unsafe[0].resource_locks, ())
            self.assertLess(unsafe[0].priority, pure[0].priority)

    def test_full_suite_scheduler_serializes_only_conflicting_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "target"
            _init_repo(target_root)
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            shards = [
                _FullSuiteShard(
                    "docker-a",
                    "tests/test_a.py",
                    ("tests/test_a.py",),
                    parallel_safe=True,
                    resource_locks=("service:docker",),
                    priority=0,
                ),
                _FullSuiteShard(
                    "docker-b",
                    "tests/test_b.py",
                    ("tests/test_b.py",),
                    parallel_safe=True,
                    resource_locks=("service:docker",),
                    priority=0,
                ),
                _FullSuiteShard(
                    "pure",
                    "tests/test_c.py",
                    ("tests/test_c.py",),
                    parallel_safe=True,
                    priority=0,
                ),
            ]
            state_lock = threading.Lock()
            active = {"docker": 0, "all": 0, "max": 0}
            conflicts = []

            def execute(_root, shard):
                with state_lock:
                    active["all"] += 1
                    active["max"] = max(active["max"], active["all"])
                    if "service:docker" in shard.resource_locks:
                        active["docker"] += 1
                        if active["docker"] > 1:
                            conflicts.append(shard.shard_id)
                time.sleep(0.05)
                with state_lock:
                    active["all"] -= 1
                    if "service:docker" in shard.resource_locks:
                        active["docker"] -= 1
                return _VerificationResult(True, shard.shard_id)

            with (
                patch.object(runner, "_collect_full_suite_shards", return_value=shards),
                patch.object(runner, "_execute_full_suite_shard", side_effect=execute),
            ):
                result = runner._run_full_suite_shards(root)

            self.assertTrue(result.ok)
            self.assertEqual(conflicts, [])
            self.assertGreater(active["max"], 1)

    def test_full_suite_isolates_repository_mutating_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            shard = _FullSuiteShard(
                "git-mutating",
                "tests/test_git.py",
                ("tests/test_git.py",),
                parallel_safe=True,
                isolated=True,
            )
            observed = {}

            def execute(verification_root, _shard):
                observed["root"] = verification_root
                self.assertNotEqual(verification_root, auto_root)
                self.assertEqual(
                    subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=verification_root,
                        check=True,
                        text=True,
                        capture_output=True,
                    ).stdout.strip(),
                    subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=auto_root,
                        check=True,
                        text=True,
                        capture_output=True,
                    ).stdout.strip(),
                )
                return _VerificationResult(True, "isolated")

            with patch.object(
                runner,
                "_execute_full_suite_shard_command",
                side_effect=execute,
            ):
                result = runner._execute_full_suite_shard(auto_root, shard)

            self.assertTrue(result.ok)
            self.assertFalse(observed["root"].exists())

    def test_full_suite_shard_plan_is_stable_for_same_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            tests = "\n\n".join(
                f"def test_{index:02d}():\n    assert True"
                for index in range(25)
            )
            write_text(auto_root / "tests" / "test_pure.py", tests + "\n")
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add tests"],
                cwd=auto_root,
                check=True,
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            runner._experiment_store = SelfRepairExperimentStore(
                target_root,
                "run",
                "stable-plan",
            )
            initial = runner._collect_full_suite_shards(auto_root)
            write_json(
                runner._full_suite_timing_path(),
                {
                    "schema_version": 1,
                    "samples": {"tests/test_pure.py": [180.0]},
                },
            )

            restored = runner._collect_full_suite_shards(auto_root)

            self.assertEqual(len(initial), 1)
            self.assertEqual(restored, initial)
            write_text(auto_root / "README.md", "next candidate tree\n")
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "advance tree"],
                cwd=auto_root,
                check=True,
            )

            replanned = runner._collect_full_suite_shards(auto_root)

            self.assertEqual(len(replanned), 2)

    def test_full_suite_proof_cache_reuses_only_unchanged_dependency_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_text(
                auto_root / "src" / "auto_agents" / "value.py",
                "VALUE = 1\n",
            )
            write_text(
                auto_root / "tests" / "test_value.py",
                "from auto_agents.value import VALUE\n\n"
                "def test_value():\n    assert VALUE == 1\n",
            )
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add pure proof"],
                cwd=auto_root,
                check=True,
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True),
            )
            runner.repo_root = auto_root
            runner._experiment_store = SelfRepairExperimentStore(
                target_root,
                "run",
                "proof-cache",
            )
            shard = runner._collect_full_suite_shards(auto_root)[0]
            passed = _VerificationResult(
                True,
                "passed",
                commands=("pytest proof",),
                returncodes=(0,),
                termination_reasons=("",),
            )
            runner._full_suite_proof_cache_store(auto_root, shard, passed)
            write_text(auto_root / "README.md", "unrelated documentation\n")
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "change unrelated docs"],
                cwd=auto_root,
                check=True,
            )

            reused = runner._full_suite_proof_cache_lookup(auto_root, shard)

            self.assertIsNotNone(reused)
            self.assertTrue(reused.ok)
            write_text(
                auto_root / "src" / "auto_agents" / "value.py",
                "VALUE = 2\n",
            )
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "change proof dependency"],
                cwd=auto_root,
                check=True,
            )

            invalidated = runner._full_suite_proof_cache_lookup(auto_root, shard)

            self.assertIsNone(invalidated)

    def test_differential_applies_candidate_tests_to_base_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_text(auto_root / "engine.py", "VALUE = 0\n")
            write_text(
                auto_root / "tests" / "test_existing.py",
                "def test_existing():\n    assert True\n",
            )
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add engine baseline"],
                cwd=auto_root,
                check=True,
            )
            base_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "worktree", "add", "-q", str(candidate_root), base_head],
                cwd=auto_root,
                check=True,
            )
            try:
                write_text(candidate_root / "engine.py", "VALUE = 1\n")
                write_text(
                    candidate_root / "tests" / "test_repair.py",
                    "from engine import VALUE\n\ndef test_repair():\n    assert VALUE == 1\n",
                )
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=candidate_root,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-qm", "candidate repair"],
                    cwd=candidate_root,
                    check=True,
                )
                diagnosis = type(
                    "Diagnosis",
                    (),
                    {
                        "final": type(
                            "Final",
                            (),
                            {
                                "verification_commands": [
                                    "python -m pytest -q tests"
                                ]
                            },
                        )()
                    },
                )()
                runner = AutoAgentsSelfRepairRunner(
                    object(),
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(True),
                    diagnosis=diagnosis,
                )
                runner.repo_root = auto_root

                result = runner._diagnosis_differential(
                    base_head,
                    candidate_root,
                )

                self.assertTrue(result.ok, result.summary)
                self.assertIn("base with candidate tests differential", result.summary)
                self.assertIn("1 failed", result.summary)
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(candidate_root)],
                    cwd=auto_root,
                    check=True,
                )

    def test_pending_validation_candidate_is_resumed_before_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "pending-candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            base_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "worktree", "add", "-q", str(candidate_root), base_head],
                cwd=auto_root,
                check=True,
            )
            try:
                write_text(candidate_root / "fixed.py", "FIXED = True\n")
                subprocess.run(
                    ["git", "add", "-A"], cwd=candidate_root, check=True
                )
                subprocess.run(
                    ["git", "commit", "-qm", "pending repair"],
                    cwd=candidate_root,
                    check=True,
                )
                candidate_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=candidate_root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(candidate_root)],
                    cwd=auto_root,
                    check=True,
                )
            pending_ref = (
                "refs/auto-agents/self-repair/pending-validation/"
                "resume-pending/c1"
            )
            subprocess.run(
                ["git", "update-ref", pending_ref, candidate_commit],
                cwd=auto_root,
                check=True,
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(
                    True,
                    category="resume-pending",
                    reason="resume retained validation",
                ),
            )
            runner.repo_root = auto_root
            with (
                patch.object(
                    runner,
                    "_replay_candidate",
                    return_value=_VerificationResult(True, "replay crossed"),
                ),
                patch.object(
                    runner,
                    "_diagnosis_differential",
                    return_value=_VerificationResult(True, "differential crossed"),
                ),
                patch.object(
                    runner,
                    "_full_suite_differential",
                    return_value=_VerificationResult(True, "full suite passed"),
                ),
                patch.object(
                    runner,
                    "_run_verification",
                    return_value=_VerificationResult(True, "focused passed"),
                ),
                patch.object(
                    runner,
                    "_review_candidate",
                    return_value=_VerificationResult(
                        True,
                        "semantic review approved",
                        payload={"findings": [], "resolved_finding_ids": []},
                    ),
                ),
                patch.object(
                    runner,
                    "_deterministic_proof_seal",
                    return_value=_VerificationResult(True, "proof sealed"),
                ),
            ):
                result = runner._resume_pending_validation_candidate(
                    experiment_id="experiment",
                    deadline=time.monotonic() + 300,
                )

            self.assertIsNotNone(result)
            self.assertTrue(result.ok)
            self.assertEqual(result.candidate_commit, candidate_commit)
            self.assertEqual(result.status, "approved_candidate")
            self.assertTrue((Path(result.runtime_root) / "fixed.py").is_file())
            pending_probe = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", pending_ref],
                cwd=auto_root,
            )
            self.assertNotEqual(pending_probe.returncode, 0)
            runner.cleanup_runtime(result)

    def test_run_prioritizes_pending_validation_over_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            base_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            experiment = SelfRepairExperiment.create(
                run_id="run",
                root_fingerprint="pending-priority",
                category="pending-priority",
                base_commit=base_head,
            )
            experiment.candidates["c1"] = SelfRepairCandidateRecord(
                candidate_id="c1",
                candidate_ref="refs/pending/c1",
                candidate_commit=base_head,
                status="candidate_full_suite_inconclusive",
                validation_stage="full_suite",
                validation_rank=90,
            )
            experiment.best_search_candidate_id = "c1"
            experiment.best_search_ref = "refs/pending/c1"
            store = SelfRepairExperimentStore(
                target_root,
                "run",
                "pending-priority",
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True, category="pending-priority"),
            )
            runner.repo_root = auto_root
            runner._experiment = experiment
            runner._experiment_store = store
            approved = SelfRepairResult(
                ok=True,
                status="approved_candidate",
                category="pending-priority",
                reason="pending validation passed",
                candidate_id="c1",
                candidate_ref="refs/approved/c1",
                candidate_commit=base_head,
                base_commit=base_head,
            )
            with (
                patch.object(
                    runner,
                    "_load_or_create_experiment",
                    return_value=(store, experiment),
                ),
                patch.object(
                    runner,
                    "_migrate_recoverable_candidate_to_pending",
                ),
                patch.object(
                    runner,
                    "_resume_pending_validation_candidate",
                    return_value=approved,
                ) as resume,
                patch.object(runner, "_run_candidate") as generate,
            ):
                result = runner.run()

            self.assertTrue(result.ok)
            resume.assert_called_once()
            generate.assert_not_called()
            self.assertEqual(experiment.status, "approved")

    def test_legacy_inconclusive_candidate_migrates_to_pending_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            base_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "worktree", "add", "-q", str(candidate_root), base_head],
                cwd=auto_root,
                check=True,
            )
            try:
                write_text(candidate_root / "first.py", "FIRST = True\n")
                subprocess.run(["git", "add", "-A"], cwd=candidate_root, check=True)
                subprocess.run(
                    ["git", "commit", "-qm", "first candidate"],
                    cwd=candidate_root,
                    check=True,
                )
                write_text(candidate_root / "second.py", "SECOND = True\n")
                subprocess.run(["git", "add", "-A"], cwd=candidate_root, check=True)
                subprocess.run(
                    ["git", "commit", "-qm", "second candidate"],
                    cwd=candidate_root,
                    check=True,
                )
                candidate_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=candidate_root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(candidate_root)],
                    cwd=auto_root,
                    check=True,
                )
            old_ref = "refs/auto-agents/self-repair/candidates/legacy/c4"
            subprocess.run(
                ["git", "update-ref", old_ref, candidate_commit],
                cwd=auto_root,
                check=True,
            )
            experiment = SelfRepairExperiment.create(
                run_id="run",
                root_fingerprint="legacy",
                category="legacy",
                base_commit=base_head,
            )
            experiment.candidates["c4"] = SelfRepairCandidateRecord(
                candidate_id="c4",
                candidate_ref=old_ref,
                candidate_commit=candidate_commit,
                status="infrastructure_blocked",
                validation_stage="full_suite",
                validation_rank=90,
                infrastructure_failure=True,
            )
            experiment.best_search_candidate_id = "c4"
            experiment.best_search_ref = old_ref
            experiment.infrastructure_failures = 1
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True, category="legacy"),
            )
            runner.repo_root = auto_root
            runner._experiment = experiment
            runner._experiment_store = SelfRepairExperimentStore(
                target_root,
                "run",
                "legacy",
            )

            runner._migrate_recoverable_candidate_to_pending(experiment)

            migrated = experiment.candidates["c4"]
            self.assertEqual(
                migrated.status,
                "candidate_full_suite_inconclusive",
            )
            self.assertFalse(migrated.infrastructure_failure)
            self.assertIn("/pending-validation/", migrated.candidate_ref)
            parent = subprocess.run(
                ["git", "rev-parse", f"{migrated.candidate_ref}^"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(parent, base_head)
            self.assertNotEqual(
                subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet", old_ref],
                    cwd=auto_root,
                ).returncode,
                0,
            )

    def test_recoverable_candidate_rebases_onto_advanced_live_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            candidate_root = root / "candidate"
            _init_repo(auto_root)
            _init_repo(target_root)
            old_base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "worktree", "add", "-q", str(candidate_root), old_base],
                cwd=auto_root,
                check=True,
            )
            try:
                write_text(candidate_root / "candidate.py", "CANDIDATE = True\n")
                subprocess.run(["git", "add", "-A"], cwd=candidate_root, check=True)
                subprocess.run(
                    ["git", "commit", "-qm", "candidate"],
                    cwd=candidate_root,
                    check=True,
                )
                candidate_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=candidate_root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(candidate_root)],
                    cwd=auto_root,
                    check=True,
                )
            write_text(auto_root / "live.py", "LIVE = True\n")
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "advance live head"],
                cwd=auto_root,
                check=True,
            )
            new_base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            old_ref = "refs/auto-agents/self-repair/candidates/rebase/c4"
            subprocess.run(
                ["git", "update-ref", old_ref, candidate_commit],
                cwd=auto_root,
                check=True,
            )
            record = SelfRepairCandidateRecord(
                candidate_id="c4",
                candidate_ref=old_ref,
                candidate_commit=candidate_commit,
                status="candidate_full_suite_inconclusive",
                validation_stage="full_suite",
                validation_rank=90,
            )
            runner = AutoAgentsSelfRepairRunner(
                object(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True, category="rebase"),
            )
            runner.repo_root = auto_root

            rebased = runner._rebase_recoverable_candidate(
                record,
                old_base=old_base,
                new_base=new_base,
            )

            self.assertTrue(rebased)
            self.assertIn("/pending-validation/", record.candidate_ref)
            parent = subprocess.run(
                ["git", "rev-parse", f"{record.candidate_ref}^"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(parent, new_base)
            for path in ("candidate.py", "live.py"):
                probe = subprocess.run(
                    ["git", "show", f"{record.candidate_ref}:{path}"],
                    cwd=auto_root,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_candidate_experiment_uses_feedback_and_approves_second_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                RunState(run_id="target-run").to_dict(),
            )

            class Diagnosis:
                final = type(
                    "Final",
                    (),
                    {"verification_commands": ["test -f fixed.py"]},
                )()

                def to_dict(self):
                    return {"final": {"verification_commands": ["test -f fixed.py"]}}

            class RepairOrchestrator:
                def __init__(self):
                    self.repair_calls = 0
                    self.generation_requests = []
                    self.design_requests = []
                    self.review_requests = []
                    autonomy = type(
                        "Autonomy",
                        (),
                        {
                            "mode": "max",
                            "max_consecutive_non_improving_candidates": 3,
                            "max_frontier_candidates": 8,
                            "candidate_timeout_seconds": 300,
                            "candidate_review_timeout_seconds": 60,
                            "replay_timeout_seconds": 60,
                            "allow_isolated_dirty_checkout": True,
                        },
                    )()
                    self.config = type(
                        "Config",
                        (),
                        {
                            "efforts": {"self_repair": "max"},
                            "execution": type(
                                "Execution", (), {"autonomy": autonomy}
                            )(),
                        },
                    )()

                def _call_with_failover(self, request):
                    if request.stage == "self_repair_design_review":
                        self.design_requests.append(request)
                        contract = json.loads(
                            request.prompt.split("FROZEN_CONTRACT:\n", 1)[1].split(
                                "\nROOT_CAUSE:", 1
                            )[0]
                        )
                        payload = {
                            "decision": "APPROVE",
                            "reason": "minimal causal design is complete",
                            "strategy_id": "bounded-candidate-retry",
                            "summary": "repair and prove the bounded retry",
                            "exclusions": ["unrelated hardening"],
                            "components": [
                                {
                                    "group_id": "root-repair",
                                    "title": "Repair root cause",
                                    "contract_obligation_ids": [
                                        item["obligation_id"]
                                        for item in contract
                                        if item["obligation_id"].startswith("root:")
                                    ],
                                    "finding_ids": [],
                                    "depends_on": [],
                                    "touched_paths": ["fixed.py"],
                                    "implementation_steps": ["create fixed.py"],
                                    "focused_tests": ["test -f fixed.py"],
                                }
                            ],
                            "issues": [],
                        }
                        return AgentResult(
                            ok=True,
                            command=[],
                            output_path=request.output_path,
                            summary=json.dumps(payload),
                        )
                    if request.stage == "self_repair_candidate_review":
                        self.review_requests.append(request)
                        payload = {
                            "decision": "APPROVE",
                            "reason": "candidate code and scope are sound",
                            "findings": [],
                            "resolved_finding_ids": [],
                        }
                        return AgentResult(
                            ok=True,
                            command=[],
                            output_path=request.output_path,
                            summary=json.dumps(payload),
                        )
                    self.repair_calls += 1
                    self.generation_requests.append(request)
                    if self.repair_calls == 2:
                        self.assert_feedback(request.prompt)
                        write_text(request.cwd / "fixed.py", "FIXED = True\n")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=(
                            "candidate generated\n"
                            "COMMIT_MESSAGE: prove bounded candidate retry"
                        ),
                    )

                @staticmethod
                def assert_feedback(prompt):
                    if "candidate=" not in prompt:
                        raise AssertionError("second candidate did not receive prior proof")

            orchestrator = RepairOrchestrator()
            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                runner = AutoAgentsSelfRepairRunner(
                    orchestrator,
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(
                        True,
                        category="candidate_experiment",
                        reason="bounded repair is required",
                    ),
                    diagnosis=Diagnosis(),
                )
                result = runner.run()

            self.assertTrue(result.ok, f"{result.reason}\n{result.summary}")
            self.assertEqual(orchestrator.repair_calls, 2)
            self.assertEqual(len(orchestrator.design_requests), 1)
            self.assertTrue(orchestrator.generation_requests)
            self.assertTrue(
                all(
                    request.progress_managed_timeout
                    and request.progress_lease_seconds == 300
                    for request in orchestrator.generation_requests
                )
            )
            self.assertEqual(len(orchestrator.review_requests), 2)
            review_request = orchestrator.review_requests[0]
            self.assertEqual(review_request.timeout_seconds, 60)
            self.assertEqual(review_request.effort, "max")
            self.assertEqual(review_request.progress_lease_seconds, 60)
            self.assertTrue(review_request.progress_managed_timeout)
            self.assertIn(
                "REVIEW_PHASE: pre_validation",
                orchestrator.review_requests[0].prompt,
            )
            self.assertTrue(
                all(
                    "REVIEW_PHASE: post_full_suite" not in request.prompt
                    for request in orchestrator.review_requests
                )
            )
            self.assertIn(
                "REVIEW_PHASE: integration",
                orchestrator.review_requests[1].prompt,
            )
            self.assertIn("PROOF_SEAL:", result.verification)
            self.assertEqual(result.status, "approved_candidate")
            self.assertTrue((Path(result.runtime_root) / "fixed.py").is_file())
            runner.cleanup_runtime(result)

    def test_infrastructure_interruption_preserves_and_resumes_partial_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=auto_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            repair_one = root / "repair-one"
            repair_two = root / "repair-two"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(repair_one), base],
                cwd=auto_root,
                check=True,
                capture_output=True,
            )
            try:
                write_text(repair_one / "README.md", "unfinished repair\n")
                write_text(repair_one / "new_test.py", "PROOF = True\n")
                runner = AutoAgentsSelfRepairRunner(
                    object(),
                    target_project_root=target_root,
                    error=RuntimeError("provider timed out"),
                    decision=SelfRepairDecision(True, category="timeout"),
                )
                runner.repo_root = auto_root
                experiment = SelfRepairExperiment.create(
                    run_id="run-1",
                    root_fingerprint="root-1",
                    category="timeout",
                    base_commit=base,
                    expected_postconditions=("repair completes",),
                    max_consecutive_non_improvements=3,
                    max_frontier_candidates=8,
                )
                store = SelfRepairExperimentStore(
                    target_root,
                    "run-1",
                    "root-1",
                )
                runner._experiment = experiment
                runner._experiment_store = store

                runner._preserve_interrupted_candidate(
                    repair_one,
                    base_head=base,
                    candidate_id="c1-timeout",
                )

                partial = store.candidate_root("c1-timeout") / "partial-candidate.diff"
                self.assertTrue(partial.is_file())
                self.assertIn("new_test.py", partial.read_text(encoding="utf-8"))
                experiment.candidates["c1-timeout"] = SelfRepairCandidateRecord(
                    candidate_id="c1-timeout",
                    parent_candidate_id="base",
                    parent_ref=base,
                    status="infrastructure_blocked",
                    infrastructure_failure=True,
                )
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(repair_two), base],
                    cwd=auto_root,
                    check=True,
                    capture_output=True,
                )
                try:
                    resumed = runner._resume_interrupted_candidate(
                        repair_two,
                        base_head=base,
                    )
                    self.assertEqual(resumed, "c1-timeout")
                    self.assertEqual(
                        (repair_two / "README.md").read_text(encoding="utf-8"),
                        "unfinished repair\n",
                    )
                    self.assertTrue((repair_two / "new_test.py").is_file())
                finally:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(repair_two)],
                        cwd=auto_root,
                        check=False,
                        capture_output=True,
                    )
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(repair_one)],
                    cwd=auto_root,
                    check=False,
                    capture_output=True,
                )

    def test_design_review_cannot_reject_for_unrelated_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents/state/run_state.json",
                RunState(run_id="target-run").to_dict(),
            )

            class Diagnosis:
                final = type(
                    "Final",
                    (),
                    {"expected_postconditions": ["root cause is repaired"]},
                )()

                def to_dict(self):
                    return {
                        "final": {
                            "expected_postconditions": ["root cause is repaired"],
                            "proposed_fix_scope": ["src/auto_agents/example.py"],
                        }
                    }

            class DesignOrchestrator:
                def __init__(self):
                    autonomy = type(
                        "Autonomy",
                        (),
                        {"candidate_review_timeout_seconds": 60},
                    )()
                    self.config = type(
                        "Config",
                        (),
                        {
                            "efforts": {"self_repair_review": "max"},
                            "execution": type(
                                "Execution", (), {"autonomy": autonomy}
                            )(),
                        },
                    )()

                def _call_with_failover(self, request):
                    contract = json.loads(
                        request.prompt.split("FROZEN_CONTRACT:\n", 1)[1].split(
                            "\nROOT_CAUSE:", 1
                        )[0]
                    )
                    payload = {
                        "decision": "REJECT",
                        "reason": "a generic subsystem could also be hardened",
                        "strategy_id": "minimal-root-fix",
                        "summary": "repair only the frozen root",
                        "exclusions": ["generic hardening"],
                        "components": [
                            {
                                "group_id": "root",
                                "title": "Root repair",
                                "contract_obligation_ids": [
                                    item["obligation_id"]
                                    for item in contract
                                    if item["obligation_id"].startswith("root:")
                                ],
                                "finding_ids": [],
                                "depends_on": [],
                                "touched_paths": ["src/auto_agents/example.py"],
                                "implementation_steps": ["repair the root"],
                                "focused_tests": ["git diff --check"],
                            }
                        ],
                        "issues": [
                            {
                                "disposition": "unrelated_observation",
                                "causal_obligation_id": "",
                                "reason": "unrelated generic hardening",
                                "evidence": ["src/other.py:1"],
                            }
                        ],
                    }
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=json.dumps(payload),
                    )

            with patch(
                "auto_agents.self_repair.auto_agents_repo_root",
                return_value=auto_root,
            ):
                runner = AutoAgentsSelfRepairRunner(
                    DesignOrchestrator(),
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(True, category="design-scope"),
                    diagnosis=Diagnosis(),
                )
                _, experiment = runner._load_or_create_experiment()
                experiment.freeze_contract()

                approved = runner._ensure_approved_repair_design(experiment)

            self.assertTrue(approved)
            self.assertEqual(experiment.repair_design["strategy_id"], "minimal-root-fix")
            self.assertEqual(experiment.findings, {})
            self.assertEqual(
                experiment.design_history[-1]["ignored_unrelated_observations"],
                1,
            )

    def test_finding_groups_complete_before_one_full_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents/state/run_state.json",
                RunState(run_id="target-run").to_dict(),
            )

            class Diagnosis:
                final = type(
                    "Final",
                    (),
                    {
                        "expected_postconditions": ["first", "second"],
                        "verification_commands": ["test -f second.py"],
                    },
                )()

                def to_dict(self):
                    return {
                        "final": {
                            "expected_postconditions": ["first", "second"],
                            "verification_commands": ["test -f second.py"],
                        }
                    }

            class GroupOrchestrator:
                def __init__(self):
                    self.generated_groups = []
                    self.review_phases = []
                    autonomy = type(
                        "Autonomy",
                        (),
                        {
                            "mode": "max",
                            "max_consecutive_non_improving_candidates": 3,
                            "max_frontier_candidates": 8,
                            "candidate_timeout_seconds": 300,
                            "candidate_review_timeout_seconds": 60,
                            "replay_timeout_seconds": 60,
                            "allow_isolated_dirty_checkout": True,
                        },
                    )()
                    self.config = type(
                        "Config",
                        (),
                        {
                            "efforts": {
                                "self_repair": "max",
                                "self_repair_review": "max",
                            },
                            "execution": type(
                                "Execution", (), {"autonomy": autonomy}
                            )(),
                        },
                    )()

                def _call_with_failover(self, request):
                    if request.stage == "self_repair_design_review":
                        contract = json.loads(
                            request.prompt.split("FROZEN_CONTRACT:\n", 1)[1].split(
                                "\nROOT_CAUSE:", 1
                            )[0]
                        )
                        roots = [
                            item["obligation_id"]
                            for item in contract
                            if item["obligation_id"].startswith("root:")
                        ]
                        payload = {
                            "decision": "APPROVE",
                            "reason": "two independent components",
                            "strategy_id": "split-root-repair",
                            "summary": "repair roots in dependency order",
                            "exclusions": [],
                            "components": [
                                {
                                    "group_id": "first",
                                    "title": "First",
                                    "contract_obligation_ids": [roots[0]],
                                    "finding_ids": [],
                                    "depends_on": [],
                                    "touched_paths": ["first.py"],
                                    "implementation_steps": ["create first.py"],
                                    "focused_tests": ["test -f first.py"],
                                },
                                {
                                    "group_id": "second",
                                    "title": "Second",
                                    "contract_obligation_ids": [roots[1]],
                                    "finding_ids": [],
                                    "depends_on": ["first"],
                                    "touched_paths": ["second.py"],
                                    "implementation_steps": ["create second.py"],
                                    "focused_tests": ["test -f second.py"],
                                },
                            ],
                            "issues": [],
                        }
                        return AgentResult(
                            ok=True,
                            command=[],
                            output_path=request.output_path,
                            summary=json.dumps(payload),
                        )
                    if request.stage == "self_repair_candidate_review":
                        phase = request.prompt.split("REVIEW_PHASE: ", 1)[1].splitlines()[0]
                        self.review_phases.append(phase)
                        return AgentResult(
                            ok=True,
                            command=[],
                            output_path=request.output_path,
                            summary=json.dumps(
                                {
                                    "decision": "APPROVE",
                                    "reason": "component is sound",
                                    "findings": [],
                                    "resolved_finding_ids": [],
                                }
                            ),
                        )
                    active = json.loads(
                        request.prompt.split(
                            "Active approved design component:\n", 1
                        )[1].split("\n\nOriginal run error:", 1)[0]
                    )
                    group_id = active["group_id"]
                    self.generated_groups.append(group_id)
                    write_text(request.cwd / f"{group_id}.py", "FIXED = True\n")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=f"implemented {group_id}\nCOMMIT_MESSAGE: repair {group_id}",
                    )

            orchestrator = GroupOrchestrator()
            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                runner = AutoAgentsSelfRepairRunner(
                    orchestrator,
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(True, category="split-repair"),
                    diagnosis=Diagnosis(),
                )
                full_suite_calls = []

                def full_suite(*_args, **_kwargs):
                    full_suite_calls.append("full")
                    return _VerificationResult(True, "full suite passed")

                with patch.object(
                    runner,
                    "_full_suite_differential",
                    side_effect=full_suite,
                ):
                    result = runner.run()

            self.assertTrue(result.ok, result.reason)
            self.assertEqual(orchestrator.generated_groups, ["first", "second"])
            self.assertEqual(full_suite_calls, ["full"])
            self.assertEqual(
                orchestrator.review_phases,
                ["pre_validation", "pre_validation", "integration"],
            )

    def test_stalled_search_only_admits_causally_anchored_contract_amendment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents/state/run_state.json",
                RunState(run_id="target-run").to_dict(),
            )

            class Diagnosis:
                final = type(
                    "Final",
                    (),
                    {
                        "expected_postconditions": ["root cause is repaired"],
                        "verification_commands": ["test -f missing-proof.py"],
                    },
                )()

                def to_dict(self):
                    return {
                        "final": {
                            "expected_postconditions": ["root cause is repaired"],
                            "verification_commands": ["test -f missing-proof.py"],
                        }
                    }

            class ArbiterOrchestrator:
                def __init__(self):
                    autonomy = type(
                        "Autonomy",
                        (),
                        {"candidate_review_timeout_seconds": 60},
                    )()
                    self.config = type(
                        "Config",
                        (),
                        {
                            "efforts": {"self_repair_review": "max"},
                            "execution": type(
                                "Execution", (), {"autonomy": autonomy}
                            )(),
                        },
                    )()

                def _call_with_failover(self, request):
                    contract = json.loads(
                        request.prompt.split("FROZEN_CONTRACT:\n", 1)[1].split(
                            "\nROOT_CAUSE:", 1
                        )[0]
                    )
                    root_id = next(
                        item["obligation_id"]
                        for item in contract
                        if item["obligation_id"].startswith("root:")
                    )
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=json.dumps(
                            {
                                "decision": "AMEND",
                                "reason": "the original differential exposes a missing sub-obligation",
                                "additions": [
                                    {
                                        "parent_obligation_id": root_id,
                                        "description": "the missing proof is produced",
                                        "causal_chain": [
                                            "the original path omits the proof",
                                            "the frozen postcondition therefore remains false",
                                        ],
                                        "reproduction_command": "test -f missing-proof.py",
                                        "evidence": ["tests/test_missing_proof.py:1"],
                                    },
                                    {
                                        "parent_obligation_id": root_id,
                                        "description": "unrelated command",
                                        "causal_chain": ["a", "b"],
                                        "reproduction_command": "test -f unrelated.py",
                                        "evidence": ["unrelated.py:1"],
                                    },
                                ],
                            }
                        ),
                    )

            with patch(
                "auto_agents.self_repair.auto_agents_repo_root",
                return_value=auto_root,
            ):
                runner = AutoAgentsSelfRepairRunner(
                    ArbiterOrchestrator(),
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(True, category="contract-reanalysis"),
                    diagnosis=Diagnosis(),
                )
                _, experiment = runner._load_or_create_experiment()
                experiment.freeze_contract()
                candidate = SelfRepairResult(
                    False,
                    "candidate_replay_failed",
                    "stalled",
                    verification="original differential still fails",
                )

                amended = runner._automatic_contract_reanalysis(
                    experiment,
                    candidate,
                )

            self.assertTrue(amended)
            additions = [
                item
                for item in experiment.contract_obligation_ids
                if item.startswith("root:amend:")
            ]
            self.assertEqual(len(additions), 1)
            self.assertEqual(
                experiment.obligations[additions[0]]["reproduction_command"],
                "test -f missing-proof.py",
            )

    def test_semantic_review_ignores_future_full_suite_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)

            class Diagnosis:
                def to_dict(self):
                    return {"final": {"expected_postconditions": ["full suite passes"]}}

            class ReviewOrchestrator:
                config = type(
                    "Config",
                    (),
                    {
                        "efforts": {"self_repair": "max"},
                        "execution": type("Execution", (), {})(),
                    },
                )()

                def _call_with_failover(self, request):
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=json.dumps(
                            {
                                "decision": "REJECT",
                                "reason": "the downstream full suite is pending",
                                "findings": [
                                    {
                                        "finding_id": "full-suite-pending",
                                        "severity": "hard",
                                        "obligation_id": "validation:full_suite",
                                        "reason": "full-suite proof is pending",
                                        "counterexample": "focused checks are incomplete",
                                        "required_test": "run both full suites",
                                        "evidence": ["review phase"],
                                        "defer_until": "post_full_suite",
                                    }
                                ],
                                "resolved_finding_ids": [],
                            }
                        ),
                    )

            runner = AutoAgentsSelfRepairRunner(
                ReviewOrchestrator(),
                target_project_root=target_root,
                error=RuntimeError("terminal"),
                decision=SelfRepairDecision(True, category="review-phase"),
                diagnosis=Diagnosis(),
            )
            runner.repo_root = auto_root

            pre = runner._review_candidate(
                auto_root,
                "HEAD",
                progress_lease_seconds=60,
                phase="pre_validation",
            )
            self.assertTrue(pre.ok)
            self.assertIn("candidate review=REJECT", pre.summary)
            self.assertEqual(pre.payload["findings"], [])
            self.assertEqual(
                pre.payload["ignored_unrelated_observations"][0]["defer_until"],
                "",
            )

    def test_self_repair_runs_in_isolated_worktree_and_integrates_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                RunState(run_id="target-run").to_dict(),
            )
            original_target = (target_root / "README.md").read_text(
                encoding="utf-8"
            )

            class RepairOrchestrator:
                config = type(
                    "Config",
                    (),
                    {"efforts": {"self_repair": "max"}},
                )()

                def _call_with_failover(self, request):
                    write_text(request.cwd / "fixed.py", "FIXED = True\n")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=(
                            "generic fix complete\n"
                            "COMMIT_MESSAGE: repair isolated root cause"
                        ),
                    )

            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                runner = AutoAgentsSelfRepairRunner(
                    RepairOrchestrator(),
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(
                        True,
                        category="retry_restore_invariant",
                        reason="restore defect",
                    ),
                )
                result = runner.run()

            self.assertTrue(result.ok, f"{result.reason}\n{result.summary}")
            self.assertEqual(result.status, "approved_candidate")
            self.assertFalse((auto_root / "fixed.py").is_file())
            self.assertTrue((Path(result.runtime_root) / "fixed.py").is_file())
            promoted = runner.promote_after_live_boundary(result)
            self.assertEqual(promoted.promotion_status, "promoted_local")
            self.assertTrue((auto_root / "fixed.py").is_file())
            runner.cleanup_runtime(result)
            self.assertEqual(
                (target_root / "README.md").read_text(encoding="utf-8"),
                original_target,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--short"],
                    cwd=auto_root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout,
                "",
            )

    def test_dirty_main_checkout_defers_candidate_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                RunState(run_id="target-run").to_dict(),
            )

            class RepairOrchestrator:
                config = type(
                    "Config", (), {"efforts": {"self_repair": "max"}}
                )()

                def _call_with_failover(self, request):
                    write_text(request.cwd / "fixed.py", "FIXED = True\n")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=(
                            "generic fix complete\n"
                            "COMMIT_MESSAGE: defer dirty checkout promotion"
                        ),
                    )

            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                runner = AutoAgentsSelfRepairRunner(
                    RepairOrchestrator(),
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(True, category="dirty_checkout"),
                )
                result = runner.run()
                write_text(auto_root / "user-change.txt", "preserve me\n")
                promoted = runner.promote_after_live_boundary(result)
                runner.cleanup_runtime(result)

            self.assertEqual(
                promoted.promotion_status,
                "pending_dirty_checkout",
            )
            self.assertFalse((auto_root / "fixed.py").exists())
            self.assertEqual(
                (auto_root / "user-change.txt").read_text(encoding="utf-8"),
                "preserve me\n",
            )
            state = RunState.from_dict(
                json.loads(
                    (
                        target_root
                        / ".auto-agents"
                        / "state"
                        / "run_state.json"
                    ).read_text(encoding="utf-8")
                )
            )
            self.assertEqual(len(state.pending_self_repair_promotions), 1)

    def test_self_repair_pulls_before_repair_and_pushes_completed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote.git"
            seed_root = root / "seed"
            auto_root = root / "auto"
            upstream_root = root / "upstream"
            published_root = root / "published"
            target_root = root / "target"

            subprocess.run(
                ["git", "init", "--bare", "-q", str(remote_root)],
                check=True,
            )
            _init_repo(seed_root)
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=seed_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote_root)],
                cwd=seed_root,
                check=True,
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", branch],
                cwd=seed_root,
                check=True,
            )
            subprocess.run(
                ["git", "clone", "-q", str(remote_root), str(auto_root)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "test"],
                cwd=auto_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=auto_root,
                check=True,
            )
            write_text(auto_root / "README.md", "local branch change\n")
            subprocess.run(["git", "add", "-A"], cwd=auto_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "local change"],
                cwd=auto_root,
                check=True,
            )
            subprocess.run(
                ["git", "clone", "-q", str(remote_root), str(upstream_root)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "test"],
                cwd=upstream_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=upstream_root,
                check=True,
            )
            write_text(upstream_root / "README.md", "remote branch change\n")
            write_text(upstream_root / "upstream.py", "LATEST = True\n")
            subprocess.run(["git", "add", "-A"], cwd=upstream_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "upstream change"],
                cwd=upstream_root,
                check=True,
            )
            subprocess.run(
                ["git", "push", "-q"],
                cwd=upstream_root,
                check=True,
            )

            _init_repo(target_root)
            write_json(
                target_root / ".auto-agents" / "state" / "run_state.json",
                RunState(run_id="target-run").to_dict(),
            )

            class RepairOrchestrator:
                config = type(
                    "Config",
                    (),
                    {"efforts": {"self_repair": "max"}},
                )()

                def _call_with_failover(self, request):
                    if request.stage == "self_repair_git_conflict":
                        if "README.md" in request.prompt:
                            write_text(
                                request.cwd / "README.md",
                                "local branch change\nremote branch change\n",
                            )
                        if "fixed.py" in request.prompt:
                            write_text(
                                request.cwd / "fixed.py",
                                "FIXED = True\nREMOTE_ADVANCED = True\n",
                            )
                        return AgentResult(
                            ok=True,
                            command=[],
                            output_path=request.output_path,
                            summary="merged compatible local and remote changes",
                        )
                    write_text(
                        upstream_root / "concurrent.py",
                        "REMOTE_ADVANCED = True\n",
                    )
                    write_text(
                        upstream_root / "fixed.py",
                        "REMOTE_ADVANCED = True\n",
                    )
                    subprocess.run(
                        ["git", "add", "-A"],
                        cwd=upstream_root,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "commit", "-qm", "concurrent remote change"],
                        cwd=upstream_root,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "push", "-q"],
                        cwd=upstream_root,
                        check=True,
                    )
                    write_text(request.cwd / "fixed.py", "FIXED = True\n")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=(
                            "generic fix complete\n"
                            "COMMIT_MESSAGE: synchronize repaired repository"
                        ),
                    )

                @staticmethod
                def assert_upstream_was_pulled(repair_root):
                    if not (repair_root / "upstream.py").is_file():
                        raise AssertionError(
                            "self-repair did not start from remote HEAD"
                        )

            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                result = AutoAgentsSelfRepairRunner(
                    RepairOrchestrator(),
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(
                        True,
                        category="retry_restore_invariant",
                        reason="restore defect",
                    ),
                ).run()

            self.assertTrue(result.ok, f"{result.reason}\n{result.summary}")
            runner = AutoAgentsSelfRepairRunner
            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                promotion_runner = runner(
                    RepairOrchestrator(),
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(
                        True,
                        category="retry_restore_invariant",
                        reason="restore defect",
                    ),
                )
                promoted = promotion_runner.promote_after_live_boundary(result)
                promotion_runner.cleanup_runtime(result)
            self.assertEqual(promoted.promotion_status, "promoted_local")
            self.assertEqual(promoted.publish_status, "publish_pending")
            subprocess.run(
                ["git", "clone", "-q", str(remote_root), str(published_root)],
                check=True,
            )
            self.assertTrue((published_root / "upstream.py").is_file())
            self.assertTrue((published_root / "fixed.py").is_file())
            self.assertTrue((published_root / "concurrent.py").is_file())
            self.assertEqual(
                (published_root / "fixed.py").read_text(encoding="utf-8"),
                "REMOTE_ADVANCED = True\n",
            )
            self.assertEqual(
                (published_root / "README.md").read_text(encoding="utf-8"),
                "remote branch change\n",
            )

    def test_self_repair_stops_when_remote_cannot_be_synchronized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_root = root / "auto"
            target_root = root / "target"
            _init_repo(auto_root)
            _init_repo(target_root)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    str(root / "missing-remote.git"),
                ],
                cwd=auto_root,
                check=True,
            )

            class RepairOrchestrator:
                calls = 0

                def _call_with_failover(self, request):
                    self.calls += 1
                    write_text(request.cwd / "fixed.py", "FIXED = True\n")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=(
                            "generic fix complete\n"
                            "COMMIT_MESSAGE: repair without remote availability"
                        ),
                    )

            orchestrator = RepairOrchestrator()
            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                runner = AutoAgentsSelfRepairRunner(
                    orchestrator,
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(
                        True,
                        category="retry_restore_invariant",
                        reason="restore defect",
                    ),
                )
                result = runner.run()
                promoted = runner.promote_after_live_boundary(result)
                runner.cleanup_runtime(result)

            self.assertTrue(result.ok, f"{result.reason}\n{result.summary}")
            self.assertEqual(orchestrator.calls, 1)
            self.assertEqual(promoted.promotion_status, "promoted_local")
            self.assertEqual(promoted.publish_status, "publish_pending")
            self.assertTrue((auto_root / "fixed.py").is_file())

    def test_self_repair_skips_repair_when_remote_update_resolves_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote.git"
            seed_root = root / "seed"
            auto_root = root / "auto"
            upstream_root = root / "upstream"
            target_root = root / "target"

            subprocess.run(
                ["git", "init", "--bare", "-q", str(remote_root)],
                check=True,
            )
            _init_repo(seed_root)
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=seed_root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote_root)],
                cwd=seed_root,
                check=True,
            )
            subprocess.run(
                ["git", "push", "-qu", "origin", branch],
                cwd=seed_root,
                check=True,
            )
            subprocess.run(
                ["git", "clone", "-q", str(remote_root), str(auto_root)],
                check=True,
            )
            subprocess.run(
                ["git", "clone", "-q", str(remote_root), str(upstream_root)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "test"],
                cwd=upstream_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=upstream_root,
                check=True,
            )
            write_text(upstream_root / "remote_fix.py", "FIXED = True\n")
            subprocess.run(["git", "add", "-A"], cwd=upstream_root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "fix diagnosed issue upstream"],
                cwd=upstream_root,
                check=True,
            )
            subprocess.run(
                ["git", "push", "-q"],
                cwd=upstream_root,
                check=True,
            )
            _init_repo(target_root)

            class RepairOrchestrator:
                calls = 0

                def _call_with_failover(self, request):
                    self.calls += 1
                    write_text(request.cwd / "remote_fix.py", "FIXED = True\n")
                    return AgentResult(
                        ok=True,
                        command=[],
                        output_path=request.output_path,
                        summary=(
                            "reconstructed upstream fix locally\n"
                            "COMMIT_MESSAGE: repair from verified local candidate"
                        ),
                    )

            diagnosis = type(
                "Diagnosis",
                (),
                {
                    "final": type(
                        "FinalReport",
                        (),
                        {
                            "verification_commands": [
                                (
                                    "After supplying approved input through WAIT_USER, "
                                    "rerun target/.conda/bin/python -m pytest -q "
                                    "target/tests/system/test_boundary.py"
                                ),
                                "test -f remote_fix.py",
                            ]
                        },
                    )()
                },
            )()
            orchestrator = RepairOrchestrator()
            with (
                patch(
                    "auto_agents.self_repair.auto_agents_repo_root",
                    return_value=auto_root,
                ),
                patch(
                    "auto_agents.self_repair.self_repair_verify_commands",
                    return_value=["true"],
                ),
            ):
                runner = AutoAgentsSelfRepairRunner(
                    orchestrator,
                    target_project_root=target_root,
                    error=RuntimeError("terminal"),
                    decision=SelfRepairDecision(
                        True,
                        category="retry_restore_invariant",
                        reason="restore defect",
                    ),
                    diagnosis=diagnosis,
                )
                result = runner.run()
                promoted = runner.promote_after_live_boundary(result)
                runner.cleanup_runtime(result)

            self.assertTrue(result.ok, f"{result.reason}\n{result.summary}")
            self.assertEqual(result.status, "approved_candidate")
            self.assertEqual(orchestrator.calls, 1)
            self.assertEqual(promoted.promotion_status, "promoted_local")
            self.assertEqual(promoted.publish_status, "publish_pending")
            self.assertTrue((auto_root / "remote_fix.py").is_file())
            self.assertIn("test -f remote_fix.py", result.verification)
            self.assertIn("skipped=supplemental", result.verification)


if __name__ == "__main__":
    unittest.main()
