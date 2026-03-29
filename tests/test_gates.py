import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gates import run_commands


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


if __name__ == "__main__":
    unittest.main()

