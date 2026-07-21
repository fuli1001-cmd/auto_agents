import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.gate_baseline_cache import GateBaselineCache, make_cache_key
from auto_agents.models import CommandResult


class GateBaselineCacheTests(unittest.TestCase):
    def test_round_trip_by_baseline_ref_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            cache = GateBaselineCache(project_root, project_root / "cache.json")

            cache.put(
                "abc123:e3b0c442",
                ["pytest -q", "ruff check ."],
                collect_all=True,
                failure_ids=["tests/test_demo.py::test_one"],
                summary="cached",
            )

            self.assertEqual(
                cache.get(
                    "abc123:e3b0c442",
                    ["pytest -q", "ruff check ."],
                    collect_all=True,
                ),
                ["tests/test_demo.py::test_one"],
            )

    def test_returns_none_for_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            cache_path = project_root / "cache.json"
            cache_path.write_text(
                json.dumps({"version": 999, "entries": {}}),
                encoding="utf-8",
            )
            cache = GateBaselineCache(project_root, cache_path)
            self.assertIsNone(cache.get("abc", ["pytest -q"], collect_all=True))

    def test_make_cache_key_changes_with_collect_all(self) -> None:
        key_a = make_cache_key("abc", ["pytest -q"], collect_all=True)
        key_b = make_cache_key("abc", ["pytest -q"], collect_all=False)
        self.assertNotEqual(key_a, key_b)

    def test_does_not_cache_terminated_command_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            cache = GateBaselineCache(project_root, project_root / "cache.sqlite3")
            command = "pytest -q"

            cache.put(
                "abc123",
                [command],
                collect_all=True,
                failure_ids=[f"cmd-timeout:{command}"],
                summary="timed out",
                command_results=[
                    CommandResult(
                        command=command,
                        ok=False,
                        returncode=124,
                        termination_reason="timeout",
                        timeout_seconds=1,
                    )
                ],
            )

            self.assertIsNone(cache.get("abc123", [command], collect_all=True))


if __name__ == "__main__":
    unittest.main()
