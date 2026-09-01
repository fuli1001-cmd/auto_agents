from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from auto_agents.cli import main
from auto_agents.config import load_run_state
from auto_agents.orchestrator import Orchestrator
from auto_agents.performance_trace import PerformanceTrace


class PerformanceTraceTests(unittest.TestCase):
    def test_trace_summary_persists_active_and_wait_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            state = load_run_state(root)
            trace = PerformanceTrace(
                root,
                workflow_kind="run",
                subject_id=state.run_id,
            )
            trace.event(
                "gate",
                "proof-a",
                duration_seconds=3,
                active_seconds=2,
                wait_seconds=1,
            )
            trace.event("gate", "proof-a", duration_seconds=4)

            summary = trace.summary()

            self.assertEqual(summary["events"], 2)
            total = summary["totals"]["gate:proof-a"]
            self.assertEqual(total["count"], 2)
            self.assertEqual(total["duration_seconds"], 7.0)
            self.assertEqual(total["active_seconds"], 6.0)
            self.assertEqual(total["wait_seconds"], 1.0)
            self.assertEqual(summary["metrics"]["gate_calls"], 2)
            self.assertEqual(
                summary["metrics"]["gate_cache_miss_reasons"],
                {"unknown": 2},
            )

    def test_performance_cli_reports_current_run_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            Orchestrator.init_project(root, "demo", "mock")
            state = load_run_state(root)
            PerformanceTrace(
                root,
                workflow_kind="run",
                subject_id=state.run_id,
            ).event("stage", "clarify", duration_seconds=1.5)
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(["performance", "--project", str(root)])

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["events"], 1)
            self.assertIn("stage:clarify", payload["totals"])
            self.assertTrue(payload["trace_path"].endswith("performance_trace.jsonl"))


if __name__ == "__main__":
    unittest.main()
