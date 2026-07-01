import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.env import load_dotenv


class DotenvTests(unittest.TestCase):
    def test_load_dotenv_supports_basic_export_and_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "PLAIN=value",
                        "export EXPORTED=ok",
                        "DOUBLE=\"hello # world\"",
                        "SINGLE='raw # value'",
                        "INLINE=value # comment",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                load_dotenv([env_path])
                self.assertEqual(os.environ["PLAIN"], "value")
                self.assertEqual(os.environ["EXPORTED"], "ok")
                self.assertEqual(os.environ["DOUBLE"], "hello # world")
                self.assertEqual(os.environ["SINGLE"], "raw # value")
                self.assertEqual(os.environ["INLINE"], "value")

    def test_load_dotenv_does_not_override_existing_env_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("WECHAT_WEBHOOK_URL=https://from-dotenv\n", encoding="utf-8")

            with patch.dict(os.environ, {"WECHAT_WEBHOOK_URL": "https://from-env"}, clear=True):
                load_dotenv([env_path])
                self.assertEqual(os.environ["WECHAT_WEBHOOK_URL"], "https://from-env")
