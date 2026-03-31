import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.models import AgentResult
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


if __name__ == "__main__":
    unittest.main()
