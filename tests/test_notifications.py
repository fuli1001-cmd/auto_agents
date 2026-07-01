import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_agents.notifications import (
    WECHAT_WEBHOOK_ENV,
    notify_flow_finished,
    send_wechat_markdown,
)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class WeChatNotificationTests(unittest.TestCase):
    def test_missing_webhook_skips_send(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(send_wechat_markdown("hello"))
            urlopen.assert_not_called()

    def test_send_wechat_markdown_posts_expected_payload(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"errcode": 0})

        with (
            patch.dict(os.environ, {WECHAT_WEBHOOK_ENV: "https://example.test/webhook"}),
            patch("urllib.request.urlopen", fake_urlopen),
        ):
            self.assertTrue(send_wechat_markdown("**done**"))

        self.assertEqual(captured["url"], "https://example.test/webhook")
        self.assertEqual(captured["timeout"], 10)
        self.assertEqual(
            captured["payload"],
            {
                "msgtype": "markdown",
                "markdown": {"content": "**done**"},
            },
        )

    def test_send_wechat_markdown_returns_false_for_api_error(self) -> None:
        with (
            patch.dict(os.environ, {WECHAT_WEBHOOK_ENV: "https://example.test/webhook"}),
            patch("urllib.request.urlopen", return_value=_FakeResponse({"errcode": 40001})),
        ):
            self.assertFalse(send_wechat_markdown("failed"))

    def test_send_wechat_markdown_returns_false_for_network_error(self) -> None:
        with (
            patch.dict(os.environ, {WECHAT_WEBHOOK_ENV: "https://example.test/webhook"}),
            patch("urllib.request.urlopen", side_effect=OSError("network down")),
        ):
            self.assertFalse(send_wechat_markdown("failed"))

    def test_notify_flow_finished_ignores_non_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("auto_agents.notifications.send_wechat_markdown") as send:
                result = notify_flow_finished(Path(tmp), workflow="run", status="paused")
            self.assertFalse(result)
            send.assert_not_called()

    def test_notify_flow_finished_formats_terminal_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "demo"
            (project_root / ".auto-agents").mkdir(parents=True)
            (project_root / ".auto-agents" / "config.json").write_text(
                json.dumps({"project_name": "demo-app"}),
                encoding="utf-8",
            )
            with patch("auto_agents.notifications.send_wechat_markdown", return_value=True) as send:
                result = notify_flow_finished(
                    project_root,
                    workflow="run",
                    status="completed",
                    identifier="run-123",
                    stage="readme",
                    paths=[project_root / ".auto-agents" / "state" / "run_state.json"],
                )

            self.assertTrue(result)
            content = send.call_args.args[0]
            self.assertIn("auto-agents run completed", content)
            self.assertIn("Project: demo-app", content)
            self.assertIn("ID: run-123", content)
            self.assertIn("Stage: readme", content)
