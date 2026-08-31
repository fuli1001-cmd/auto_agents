from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.io_utils import write_json, write_text
from auto_agents.models import (
    AgentResult,
    RunState,
    SelfRepairDiagnosisConfig,
)
from auto_agents.root_cause import RootCauseCoordinator
from auto_agents.repair_cases import RepairCase
from auto_agents.self_repair import (
    AutoAgentsSelfRepairRunner,
    SelfRepairDecision,
    SelfRepairResult,
    _VerificationResult,
    self_repair_verification_command,
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
    ) -> None:
        self.responses = list(responses)
        self.requests = []
        self.mutate_target = mutate_target
        self.investigator_tool_count = investigator_tool_count
        self.config = type(
            "Config",
            (),
            {"efforts": {"self_repair": "max"}},
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

    def test_runner_returns_deepest_candidate_not_last_candidate(self):
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

            self.assertEqual(result.candidate_id, "c2")
            self.assertEqual(result.validation_stage, "full_suite")
            self.assertEqual(result.validation_rank, 80)
            self.assertEqual(result.status, "patience_exhausted")
            self.assertIn("3 consecutive", result.reason)
            self.assertIn("candidate=c5", result.summary)

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

    def test_full_suite_timeout_retries_candidate_with_remaining_budget(self):
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
            base_timeout = _VerificationResult(
                False,
                "$ <self-repair-worktree>/python -q tests\n"
                "exit=-15\ncommand timed out after 900s",
                returncodes=(-15,),
                termination_reasons=("timeout",),
            )
            candidate_timeout = _VerificationResult(
                False,
                "$ <self-repair-worktree>/python -q tests\n"
                "exit=-15\ncommand timed out after 900s",
                returncodes=(-15,),
                termination_reasons=("timeout",),
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
                    "_run_verification_at_ref",
                    return_value=base_timeout,
                ),
                patch.object(
                    runner,
                    "_run_verification_commands",
                    side_effect=[candidate_timeout, candidate_passed],
                ) as verify,
            ):
                result = runner._full_suite_differential(
                    "base",
                    candidate_root,
                    deadline=time.monotonic() + 1500,
                )

            self.assertTrue(result.ok, result.summary)
            self.assertEqual(verify.call_count, 2)
            self.assertGreater(
                verify.call_args_list[1].kwargs["command_timeout_seconds"],
                900,
            )
            self.assertIn("extended candidate full suite passed", result.summary)

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
            )
            base_timeout = _VerificationResult(
                False,
                "$ /tmp/auto-agents-remote-repair-check-y/verification/python -q tests\n"
                "exit=-15\ncommand timed out after 900s",
                returncodes=(-15,),
                termination_reasons=("timeout",),
            )
            with (
                patch.object(
                    runner,
                    "_run_verification_at_ref",
                    return_value=base_timeout,
                ),
                patch.object(
                    runner,
                    "_run_verification_commands",
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
            self.assertIn("inconclusive=full-suite timeout", result.summary)

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
                    if request.stage == "self_repair_candidate_review":
                        return AgentResult(
                            ok=True,
                            command=[],
                            output_path=request.output_path,
                            summary=json.dumps(
                                {
                                    "decision": "APPROVE",
                                    "reason": "focused regression and scope are sound",
                                }
                            ),
                        )
                    self.repair_calls += 1
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
                    if "candidate 1" not in prompt:
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
            self.assertEqual(result.status, "approved_candidate")
            self.assertTrue((Path(result.runtime_root) / "fixed.py").is_file())
            runner.cleanup_runtime(result)

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
