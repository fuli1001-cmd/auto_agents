import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.repomap.parser import FileSummary, Symbol
from auto_agents.repomap.ranker import KeywordRanker


def _make_summary(path: str, *, imports=None, classes=None, functions=None) -> FileSummary:
    syms = []
    for cls_name, methods in (classes or []):
        syms.append(
            Symbol(
                kind="class",
                name=cls_name,
                signature=f"class {cls_name}",
                children=[
                    Symbol(kind="method", name=m, signature=f"def {m}(self)")
                    for m in methods
                ],
            )
        )
    for fn_name in functions or []:
        syms.append(Symbol(kind="function", name=fn_name, signature=f"def {fn_name}()"))
    return FileSummary(path=path, imports=list(imports or []), symbols=syms)


class KeywordRankerTests(unittest.TestCase):
    def test_anchor_outranks_keyword_match(self) -> None:
        summaries = [
            _make_summary("src/foo.py", classes=[("Foo", ["bar"])]),
            _make_summary("src/orch.py", classes=[("Orchestrator", ["run"])]),
        ]
        ranked = KeywordRanker().rank(
            summaries,
            keywords=["orchestrator"],
            anchor_paths=["src/foo.py"],
        )
        self.assertEqual(ranked[0].path, "src/foo.py")
        self.assertTrue(ranked[0].is_anchor)

    def test_keyword_in_path_and_symbol_boost(self) -> None:
        summaries = [
            _make_summary("src/random.py", classes=[("Foo", ["bar"])]),
            _make_summary("src/repomap.py", classes=[("RepoMapBuilder", ["build"])]),
        ]
        ranked = KeywordRanker().rank(summaries, keywords=["repomap"])
        self.assertEqual(ranked[0].path, "src/repomap.py")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_reference_count_breaks_ties(self) -> None:
        summaries = [
            _make_summary("src/util.py", functions=["helper"]),
            _make_summary("src/a.py", imports=["util"]),
            _make_summary("src/b.py", imports=["util"]),
        ]
        ranked = KeywordRanker().rank(summaries, keywords=[])
        self.assertEqual(ranked[0].path, "src/util.py")


if __name__ == "__main__":
    unittest.main()
