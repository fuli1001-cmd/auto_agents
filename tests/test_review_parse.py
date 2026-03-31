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


if __name__ == "__main__":
    unittest.main()
