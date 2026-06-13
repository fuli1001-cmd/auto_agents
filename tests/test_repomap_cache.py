import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.repomap.cache import RepoMapCache, compute_cache_key
from auto_agents.repomap.parser import PythonAstParser


class CountingParser(PythonAstParser):
    def __init__(self) -> None:
        self.calls = []

    def parse(self, project_root: Path, rel_path: str):
        self.calls.append(rel_path)
        return super().parse(project_root, rel_path)


class CountingCache(RepoMapCache):
    def __init__(self, project_root: Path, cache_path: Path) -> None:
        super().__init__(project_root, cache_path=cache_path)
        self.writes = 0

    def _write(self, entries):
        self.writes += 1
        return super()._write(entries)


class RepoMapCacheTests(unittest.TestCase):
    def test_hits_when_files_unchanged_and_reparses_only_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f(): return 1\n", encoding="utf-8")
            (root / "b.py").write_text("def g(): return 2\n", encoding="utf-8")
            cache = RepoMapCache(root, cache_path=root / "cache.json")
            parser = CountingParser()
            rels = ["a.py", "b.py"]

            first = cache.get_or_build(rels, parser)
            self.assertEqual(cache.last_hit, False)
            self.assertEqual(cache.last_hits, 0)
            self.assertEqual(cache.last_misses, 2)
            self.assertEqual(len(first), 2)
            self.assertEqual(parser.calls, ["a.py", "b.py"])

            parser.calls = []
            second = cache.get_or_build(rels, parser)
            self.assertEqual(cache.last_hit, True)
            self.assertEqual(cache.last_hits, 2)
            self.assertEqual(cache.last_misses, 0)
            self.assertEqual([s.path for s in second], ["a.py", "b.py"])
            self.assertEqual(second[0].symbols[0].name, "f")
            self.assertEqual(parser.calls, [])

            (root / "a.py").write_text("def f(): return 99\ndef extra(): pass\n", encoding="utf-8")
            parser.calls = []
            third = cache.get_or_build(rels, parser)
            self.assertEqual(cache.last_hit, False)
            self.assertEqual(cache.last_hits, 1)
            self.assertEqual(cache.last_misses, 1)
            self.assertEqual(parser.calls, ["a.py"])
            symbol_names = [sym.name for sym in third[0].symbols]
            self.assertIn("extra", symbol_names)

    def test_cache_file_is_not_rewritten_on_full_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f(): return 1\n", encoding="utf-8")
            cache = CountingCache(root, root / "cache.json")

            cache.get_or_build(["a.py"], CountingParser())
            cache.get_or_build(["a.py"], CountingParser())

            self.assertEqual(cache.writes, 1)

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

    def test_compute_cache_key_changes_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x=1\n", encoding="utf-8")
            k1 = compute_cache_key(root, ["a.py"])
            (root / "a.py").write_text("x=2\n", encoding="utf-8")
            k2 = compute_cache_key(root, ["a.py"])
            self.assertNotEqual(k1, k2)

    def test_legacy_payload_is_treated_as_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f(): pass\n", encoding="utf-8")
            cache_path = root / "cache.json"
            cache_path.write_text(
                "{\"key\": \"legacy\", \"summaries\": []}\n",
                encoding="utf-8",
            )
            cache = RepoMapCache(root, cache_path=cache_path)
            parser = CountingParser()

            cache.get_or_build(["a.py"], parser)

            self.assertEqual(cache.last_hit, False)
            self.assertEqual(parser.calls, ["a.py"])

    def test_deleted_files_are_pruned_from_cache_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def f(): pass\n", encoding="utf-8")
            (root / "b.py").write_text("def g(): pass\n", encoding="utf-8")
            cache_path = root / "cache.json"
            cache = RepoMapCache(root, cache_path=cache_path)

            cache.get_or_build(["a.py", "b.py"], CountingParser())
            (root / "b.py").unlink()
            cache.get_or_build(["a.py"], CountingParser())

            payload = cache_path.read_text(encoding="utf-8")
            self.assertIn("\"a.py\"", payload)
            self.assertNotIn("\"b.py\"", payload)


if __name__ == "__main__":
    unittest.main()
