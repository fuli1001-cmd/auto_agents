import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.orchestrator import Orchestrator, _utf8_safe_text


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

    def test_single_line_repairs_surrogateescaped_utf8(self) -> None:
        orchestrator = self._make_orchestrator()
        escaped = "".join(chr(0xDC00 + byte) for byte in "音频".encode("utf-8"))

        with mock.patch("builtins.input", return_value=f"中文{escaped}"):
            result = orchestrator._read_single_line_input("Audio? ", default="")

        self.assertEqual(result, "中文音频")
        result.encode("utf-8")

    def test_utf8_safe_text_combines_surrogate_pair_and_replaces_garbage(self) -> None:
        result = _utf8_safe_text("ok\ud83d\ude00\udcff\udcfe")

        self.assertTrue(result.startswith("ok😀"))
        self.assertNotRegex(result, r"[\ud800-\udfff]")
        result.encode("utf-8")

    def test_prompt_reopens_tty_when_stdin_is_not_tty(self) -> None:
        orchestrator = self._make_orchestrator()
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False

        with mock.patch.dict(sys.modules):
            sys.modules.pop("unittest", None)
            with mock.patch.object(sys, "stdin", fake_stdin):
                with mock.patch.object(orchestrator, "_reopen_stdin_from_tty", return_value=True) as reopen:
                    with mock.patch.object(orchestrator, "_read_single_line_input", return_value="y") as read:
                        result = orchestrator._prompt_user("Confirm? ", default="n")

        self.assertEqual(result, "y")
        self.assertEqual(reopen.call_count, 1)
        read.assert_called_once_with("Confirm? ", "n")


if __name__ == "__main__":
    unittest.main()
