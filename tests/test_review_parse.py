import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.config import requirements_trace_path
from auto_agents.io_utils import write_json
from auto_agents.models import AgentResult, TaskSpec
from auto_agents.orchestrator import Orchestrator


class ReviewParseTests(unittest.TestCase):
    def test_parse_review_pass(self) -> None:
        decision, summary = Orchestrator._parse_review_decision("DECISION: pass\nLooks good.\n")
        self.assertEqual(decision, "pass")
        self.assertEqual(summary, "Looks good.")

    def test_parse_review_pass_with_preface_before_decision(self) -> None:
        decision, summary = Orchestrator._parse_review_decision(
            "Wrote the review to review.md.\n\nDECISION: pass\nLooks good.\n"
        )
        self.assertEqual(decision, "pass")
        self.assertEqual(summary, "Looks good.")

    def test_parse_review_fail_for_invalid_prefix(self) -> None:
        decision, summary = Orchestrator._parse_review_decision("Looks good.\n")
        self.assertEqual(decision, "fail")
        self.assertEqual(summary, "Looks good.")

    def test_review_validation_requires_explicit_decision_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            result = AgentResult(
                ok=True,
                command=["fake"],
                output_path=project_root / "out.md",
                summary="I wrote `DECISION: pass` on the first line.\n",
                returncode=0,
            )

            issue = orchestrator._review_validation_feedback(result)

            self.assertIsNotNone(issue)

    def test_review_validation_rejects_orchestrator_state_snapshot_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            result = AgentResult(
                ok=True,
                command=["fake"],
                output_path=project_root / "out.md",
                summary=(
                    "DECISION: fail\n"
                    "`.auto-agents/state/task_plan.json` and `.auto-agents/state/run_state.json` "
                    "still show task-001 as `in_progress` instead of `done`.\n"
                ),
                returncode=0,
            )

            issue = orchestrator._review_validation_feedback(result)

            self.assertIsNotNone(issue)
            assert issue is not None
            self.assertIn("orchestrator-owned", issue)

    def test_review_prompt_forbids_preamble_and_file_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            prompt = orchestrator._build_task_prompt(task, "review")

            self.assertIn("Do not include any preamble, file path note, or tool narration.", prompt)
            self.assertIn("The first non-empty line must be exactly 'DECISION: pass' or 'DECISION: fail'.", prompt)
            self.assertNotIn("Write the review summary to:", prompt)

    def test_review_prompt_can_embed_changed_file_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]
            (project_root / "artifact.txt").write_text("hello\n", encoding="utf-8")

            review_context = orchestrator._build_review_context("all commands passed")
            prompt = orchestrator._build_task_prompt(task, "review", review_context=review_context)

            self.assertIn("Use the supplied changed-file and diff context first.", prompt)
            self.assertIn("Local verification summary:", prompt)
            self.assertIn("Changed files:", prompt)
            self.assertIn("artifact.txt", prompt)

    def test_implement_prompt_protects_conda_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            prompt = orchestrator._build_task_prompt(task, "implement")

            self.assertIn("It must remain a real conda prefix", prompt)
            self.assertIn(".conda/conda-meta", prompt)

    def test_implement_prompt_allows_tightly_coupled_regression_fixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            prompt = orchestrator._build_task_prompt(task, "implement")

            self.assertIn("tightly coupled regression", prompt)
            self.assertIn("slightly outside the nominal task slice", prompt)

    def test_review_prompt_contains_scope_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]

            prompt = orchestrator._build_task_prompt(task, "review")

            self.assertIn("SCOPE RULE:", prompt)
            self.assertIn("DECISION: fail' is warranted ONLY", prompt)
            self.assertIn("[NON-BLOCKING]", prompt)
            self.assertIn("cite the specific acceptance criterion", prompt)

    def test_review_prompt_includes_scope_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]
            task.scope_boundaries = "Performance optimization and caching are out of scope."

            prompt = orchestrator._build_task_prompt(task, "review")

            self.assertIn("SCOPE BOUNDARIES", prompt)
            self.assertIn("Performance optimization and caching are out of scope.", prompt)

    def test_review_prompt_convergence_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            orchestrator = Orchestrator(project_root)
            task = orchestrator._load_tasks_from_plan()[0]
            task.review_history = [
                {"attempt": 1, "summary": "Missing test for edge case."},
            ]

            prompt = orchestrator._build_task_prompt(task, "review")

            self.assertIn("RETRY review", prompt)
            self.assertIn("previous review have been addressed", prompt)
            self.assertIn("Do NOT fail for newly-discovered scope-expansion concerns", prompt)

    def test_format_retry_feedback_with_review_history(self) -> None:
        history = [
            {"attempt": 1, "summary": "Missing null check."},
            {"attempt": 2, "summary": "Still missing error handling."},
        ]
        feedback = Orchestrator._format_retry_feedback(
            "review_rejected",
            reason="review rejected the task",
            review_history=history,
            review_summary="Still missing error handling.",
        )
        self.assertIn("[ADDRESSED in later attempt]", feedback)
        self.assertIn("[CURRENT - must fix]", feedback)
        self.assertIn("Attempt 1", feedback)
        self.assertIn("Attempt 2", feedback)

    def test_format_retry_feedback_single_review_uses_summary(self) -> None:
        history = [
            {"attempt": 1, "summary": "Missing null check."},
        ]
        feedback = Orchestrator._format_retry_feedback(
            "review_rejected",
            reason="review rejected the task",
            review_history=history,
            review_summary="Missing null check.",
        )
        # Single entry: no ADDRESSED/CURRENT annotations, just review summary
        self.assertNotIn("[ADDRESSED", feedback)
        self.assertIn("Missing null check.", feedback)

    def test_format_retry_feedback_includes_verify_triage_and_evidence(self) -> None:
        feedback = Orchestrator._format_retry_feedback(
            "local_verification",
            reason="command failed: conda run -p ./.conda python -m unittest discover -s tests (...)",
            verification_summary=(
                "New failures vs task baseline (2): test_publish_flow, test_subtitles\n"
                "Likely root causes:\n"
                "  - RuntimeError: boom"
            ),
            implicated_paths=["app/api/routes/projects.py", "app/application/media.py"],
            raw_excerpts=[
                "ERROR: test_publish_flow\nTraceback ...\nRuntimeError: boom",
            ],
        )

        self.assertIn("Verification triage:", feedback)
        self.assertIn("New failures vs task baseline", feedback)
        self.assertIn("Implicated paths: app/api/routes/projects.py, app/application/media.py", feedback)
        self.assertIn("Key verify evidence:", feedback)
        self.assertIn("--- Excerpt 1 ---", feedback)

    def test_task_review_retry_feedback_omits_obsolete_proof_review(self) -> None:
        task = TaskSpec(
            task_id="task-113",
            title="Build six-step detail",
            description="Current split owns only the first three REQ-074 oracles.",
            acceptance=["six steps"],
            requirement_ids=["REQ-074"],
            requirement_proofs=[
                {"requirement_id": "REQ-074", "oracle_index": 1, "status": "verified"},
                {"requirement_id": "REQ-074", "oracle_index": 2, "status": "verified"},
                {"requirement_id": "REQ-074", "oracle_index": 3, "status": "verified"},
            ],
        )

        feedback = Orchestrator._format_task_review_retry_feedback(
            task,
            reason="review rejected the task",
            review_summary="REQ-074 的第 4 条验收 oracle 仍没有单独的已验证 proof。",
            review_history=[
                {"attempt": 1, "summary": "REQ-074 的第 4 条验收 oracle 仍没有单独的已验证 proof。"},
            ],
        )

        self.assertIn("Persisted review feedback was omitted", feedback)
        self.assertIn("current requirement_proofs", feedback)
        self.assertNotIn("第 4 条验收 oracle 仍没有单独", feedback)

    def test_task_review_retry_feedback_keeps_current_proof_review(self) -> None:
        task = TaskSpec(
            task_id="task-113",
            title="Build six-step detail",
            description="Current split owns REQ-074 oracle 2.",
            acceptance=["single panel"],
            requirement_ids=["REQ-074"],
            requirement_proofs=[
                {"requirement_id": "REQ-074", "oracle_index": 2, "status": "planned"},
            ],
        )

        feedback = Orchestrator._format_task_review_retry_feedback(
            task,
            reason="review rejected the task",
            review_summary="REQ-074 oracle #2 is still missing evidence.",
            review_history=[
                {"attempt": 1, "summary": "REQ-074 oracle #2 is still missing evidence."},
            ],
        )

        self.assertIn("REQ-074 oracle #2 is still missing evidence.", feedback)
        self.assertNotIn("Persisted review feedback was omitted", feedback)

    def test_task_prompts_scope_requirement_oracles_to_current_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            Orchestrator.init_project(project_root, "demo", "mock")
            write_json(
                requirements_trace_path(project_root),
                {
                    "version": 1,
                    "requirements": [
                        {
                            "id": "REQ-001",
                            "text": "Feature must satisfy two independently owned oracles.",
                            "source": "test",
                            "status": "active",
                            "priority": "mandatory",
                            "acceptance_oracles": ["owned by this task", "owned by another task"],
                            "oracle_type": "mixed",
                            "oracle_strength": "behavioral",
                            "evidence_boundary": "system_boundary",
                            "forbidden_proxy_oracles": [],
                            "forbidden_patterns": [],
                            "external_docs_required": False,
                            "provider_reference": "",
                            "notes": "",
                        }
                    ],
                },
            )
            orchestrator = Orchestrator(project_root)
            task = TaskSpec(
                task_id="task-001",
                title="Owned slice",
                description="Own only oracle 1.",
                acceptance=["owned slice works"],
                requirement_ids=["REQ-001"],
                requirement_proofs=[
                    {"requirement_id": "REQ-001", "oracle_index": 1, "status": "planned"},
                ],
            )

            prompt = orchestrator._build_task_prompt(task, "review")

            self.assertIn("requirement_proofs, those entries define the current task's", prompt)
            self.assertIn("do not implement, review-fail, or emit ORACLE_PROOF_UPDATES", prompt)
            self.assertIn("owned requirement proof entries", prompt)


if __name__ == "__main__":
    unittest.main()
