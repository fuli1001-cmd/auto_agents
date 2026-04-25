import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.repomap.parser import PythonAstParser


class PythonAstParserTests(unittest.TestCase):
    def _write(self, root: Path, rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_extracts_classes_methods_functions_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "pkg/foo.py", (
                "import os\n"
                "from .bar import Baz\n"
                "\n"
                "class Foo(Baz):\n"
                "    \"\"\"Foo handles bar.\"\"\"\n"
                "    def __init__(self, x: int) -> None:\n"
                "        self.x = x\n"
                "    def public(self, y: str = 'a') -> bool:\n"
                "        return True\n"
                "    def _hidden(self):\n"
                "        return None\n"
                "\n"
                "def helper(z: int) -> int:\n"
                "    \"\"\"Helper does work.\"\"\"\n"
                "    return z + 1\n"
            ))
            summary = PythonAstParser().parse(root, "pkg/foo.py")
            self.assertEqual(summary.path, "pkg/foo.py")
            self.assertIsNone(summary.parse_error)
            self.assertIn("os", summary.imports)
            self.assertIn("bar", summary.imports)

            kinds = [s.kind for s in summary.symbols]
            self.assertEqual(kinds, ["class", "function"])
            cls = summary.symbols[0]
            self.assertEqual(cls.name, "Foo")
            self.assertIn("class Foo", cls.signature)
            method_names = [c.name for c in cls.children]
            self.assertIn("__init__", method_names)
            self.assertIn("public", method_names)
            self.assertNotIn("_hidden", method_names)

            fn = summary.symbols[1]
            self.assertEqual(fn.name, "helper")
            self.assertIn("-> int", fn.signature)
            self.assertEqual(fn.docstring, "Helper does work.")

    def test_syntax_error_returns_summary_with_error_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "broken.py", "def(:\n")
            summary = PythonAstParser().parse(root, "broken.py")
            self.assertIsNotNone(summary.parse_error)
            self.assertTrue(summary.parse_error.startswith("syntax_error:"))
            self.assertEqual(summary.symbols, [])


if __name__ == "__main__":
    unittest.main()
