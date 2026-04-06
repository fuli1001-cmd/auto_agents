import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.orchestrator import Orchestrator


class PromptUserTests(unittest.TestCase):
    def _make_orchestrator(self) -> Orchestrator:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name) / "demo"
        Orchestrator.init_project(root, "demo", "mock")
        return Orchestrator(root)

    def test_single_line_eof_returns_default(self) -> None:
        orchestrator = self._make_orchestrator()

        with mock.patch("builtins.input", side_effect=EOFError):
            with mock.patch.object(orchestrator, "_reopen_stdin_from_tty", return_value=False):
                result = orchestrator._read_single_line_input("Confirm? ", default="y")

        self.assertEqual(result, "y")

    def test_single_line_eof_retries_after_reopen(self) -> None:
        orchestrator = self._make_orchestrator()

        with mock.patch("builtins.input", side_effect=[EOFError(), "n"]):
            with mock.patch.object(orchestrator, "_reopen_stdin_from_tty", return_value=True) as reopen:
                result = orchestrator._read_single_line_input("Confirm? ", default="y")

        self.assertEqual(result, "n")
        self.assertEqual(reopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
