from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
from auto_agents.self_repair import (
    AutoAgentsSelfRepairRunner,
    SelfRepairDecision,
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
    def _coordinator(
        self,
        root: Path,
        responses,
        *,
        mutate=False,
        investigator_tool_count=0,
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

            self.assertTrue(result.ok, result.reason)
            self.assertTrue((auto_root / "fixed.py").is_file())
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


if __name__ == "__main__":
    unittest.main()
