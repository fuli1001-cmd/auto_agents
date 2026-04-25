import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.repomap.cache import RepoMapCache, compute_cache_key
from auto_agents.repomap.parser import PythonAstParser


class RepoMapCacheTests(unittest.TestCase):
    def test_hits_when_files_unchanged_and_misses_after_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f(): return 1\n", encoding="utf-8")
            (root / "b.py").write_text("def g(): return 2\n", encoding="utf-8")
            cache = RepoMapCache(root, cache_path=root / "cache.json")
            parser = PythonAstParser()
            rels = ["a.py", "b.py"]

            first = cache.get_or_build(rels, parser)
            self.assertEqual(cache.last_hit, False)
            self.assertEqual(len(first), 2)

            second = cache.get_or_build(rels, parser)
            self.assertEqual(cache.last_hit, True)
            self.assertEqual([s.path for s in second], ["a.py", "b.py"])
            self.assertEqual(second[0].symbols[0].name, "f")

            # mtime change forces miss
            import time, os
            time.sleep(0.01)
            (root / "a.py").write_text("def f(): return 99\ndef extra(): pass\n", encoding="utf-8")
            os.utime(root / "a.py", None)
            third = cache.get_or_build(rels, parser)
            self.assertEqual(cache.last_hit, False)
            symbol_names = [sym.name for sym in third[0].symbols]
            self.assertIn("extra", symbol_names)

    def test_invalidate_removes_cache_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f(): pass\n", encoding="utf-8")
            cache_path = root / "c.json"
            cache = RepoMapCache(root, cache_path=cache_path)
            cache.get_or_build(["a.py"], PythonAstParser())
            self.assertTrue(cache_path.exists())
            cache.invalidate()
            self.assertFalse(cache_path.exists())

    def test_compute_cache_key_changes_with_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x=1\n", encoding="utf-8")
            k1 = compute_cache_key(root, ["a.py"])
            import os, time
            time.sleep(0.01)
            os.utime(root / "a.py", (1, 1))
            k2 = compute_cache_key(root, ["a.py"])
            self.assertNotEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
