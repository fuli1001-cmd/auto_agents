import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gates import extract_failure_ids, run_commands
from auto_agents.models import CommandResult, GateResult


class GateTests(unittest.TestCase):
    def test_run_commands_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(["python3 -c \"print('ok')\""], Path(tmp))
            self.assertTrue(result.ok)
            self.assertEqual(len(result.commands), 1)
            self.assertEqual(result.commands[0].stdout, "ok")

    def test_run_commands_stops_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(
                [
                    "python3 -c \"print('before')\"",
                    "python3 -c \"import sys; sys.exit(3)\"",
                    "python3 -c \"print('after')\"",
                ],
                Path(tmp),
            )
            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 2)
            self.assertEqual(result.commands[1].returncode, 3)
            self.assertIn("command failed:", result.summary)
            self.assertIn("python3 -c \"import sys; sys.exit(3)\"", result.summary)

    def test_run_commands_includes_stderr_in_failure_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands(
                ["python3 -c \"import sys; sys.stderr.write('boom\\n'); sys.exit(2)\""],
                Path(tmp),
            )

            self.assertFalse(result.ok)
            self.assertIn("boom", result.summary)

    def test_extract_unittest_failure_ids(self) -> None:
        gate = GateResult(
            ok=False,
            commands=[
                CommandResult(
                    command="python -m unittest",
                    ok=False,
                    returncode=1,
                    stdout=(
                        "ERROR: test_publish_flow (tests.test_api.PublishTests.test_publish_flow)\n"
                        "FAIL: test_subtitles (tests.test_api.SubtitleTests.test_subtitles)\n"
                    ),
                    stderr="",
                )
            ],
        )

        ids = extract_failure_ids(gate)

        self.assertEqual(
            ids,
            [
                "test_publish_flow (tests.test_api.PublishTests.test_publish_flow)",
                "test_subtitles (tests.test_api.SubtitleTests.test_subtitles)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
