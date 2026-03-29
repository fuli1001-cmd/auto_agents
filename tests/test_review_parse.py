import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.orchestrator import Orchestrator


class ReviewParseTests(unittest.TestCase):
    def test_parse_review_pass(self) -> None:
        decision, summary = Orchestrator._parse_review_decision("DECISION: pass\nLooks good.\n")
        self.assertEqual(decision, "pass")
        self.assertEqual(summary, "Looks good.")

    def test_parse_review_fail_for_invalid_prefix(self) -> None:
        decision, summary = Orchestrator._parse_review_decision("Looks good.\n")
        self.assertEqual(decision, "fail")
        self.assertEqual(summary, "Looks good.")


if __name__ == "__main__":
    unittest.main()
