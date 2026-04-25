import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.repomap.anchors import (
    extract_anchor_paths,
    extract_dotted_names,
    extract_keywords,
    matches_anchor,
)


class _FakeTask:
    def __init__(self, **kw):
        self.title = kw.get("title", "")
        self.description = kw.get("description", "")
        self.acceptance = kw.get("acceptance", [])
        self.scope_boundaries = kw.get("scope_boundaries", "")
        self.commit_message = kw.get("commit_message", "")


class AnchorExtractTests(unittest.TestCase):
    def test_extracts_explicit_paths_and_filenames(self) -> None:
        task = _FakeTask(
            description="Update src/auto_agents/orchestrator.py and config.py",
            acceptance=["Touch tests/test_x.py"],
        )
        paths = extract_anchor_paths(task)
        self.assertIn("src/auto_agents/orchestrator.py", paths)
        self.assertIn("tests/test_x.py", paths)
        self.assertIn("config.py", paths)

    def test_extracts_dotted_names_excluding_self(self) -> None:
        task = _FakeTask(description="Use auto_agents.session.Session and self.foo.bar")
        names = extract_dotted_names(task)
        self.assertIn("auto_agents.session.Session", names)
        self.assertNotIn("self.foo.bar", names)

    def test_extract_keywords_filters_stopwords_and_short(self) -> None:
        task = _FakeTask(
            title="Implement orchestrator repo map",
            description="The orchestrator must call the builder",
        )
        kws = extract_keywords(task)
        self.assertIn("orchestrator", kws)
        self.assertIn("builder", kws)
        self.assertNotIn("the", kws)
        self.assertNotIn("must", kws)
        self.assertNotIn("implement", kws)  # stopword

    def test_matches_anchor_basename_and_full_path(self) -> None:
        self.assertTrue(matches_anchor("config.py", "src/auto_agents/config.py"))
        self.assertTrue(matches_anchor("src/auto_agents/config.py", "src/auto_agents/config.py"))
        self.assertFalse(matches_anchor("config.py", "src/auto_agents/orchestrator.py"))


if __name__ == "__main__":
    unittest.main()
