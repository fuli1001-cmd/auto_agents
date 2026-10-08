from __future__ import annotations
import json
import io
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.config import load_run_state, save_run_state
from auto_agents.cli import build_parser, main
from auto_agents.io_utils import write_text
from auto_agents.gate_execution import LocalGatePlanExecutor
from auto_agents.models import AgentResult, GateConfig, TaskSpec
from auto_agents.operator_inputs import OperatorInputStore, UserInputRequest, prompt_for_request

def _request(**updates):
    payload = {'key': 'youtube.authorization', 'kind': 'attestation', 'question': '你是否确认：你或项目团队拥有该视频，或已获得权利人的明确授权，可以下载、处理、生成衍生内容，并长期用于自动化测试？', 'purpose': '证明真实视频测试已获得操作者授权。', 'why_required': '真实系统边界不能使用未经确认的第三方素材。', 'how_to_obtain': ['只有确实拥有权利或明确许可时选择 y。'], 'recommended_answer': '不确定时选择 n。', 'default': False, 'persistence': 'project', 'sensitivity': 'private', 'subject_fingerprint': 'video-abc', 'question_version': 1, 'validation': {'claims': ['download', 'processing', 'derivative_creation', 'automated_testing'], 'subject': {'source_url_input_key': 'youtube.source_url'}, 'stable_test_use': True}, 'bindings': [{'env': 'SDGLOBAL_TEST_YOUTUBE_AUTHORIZATION_EVIDENCE', 'projection': 'artifact_path'}]}
    payload.update(updates)
    return UserInputRequest.from_dict(payload)

class OperatorInputStoreTests(unittest.TestCase):

    def test_attestation_is_one_safe_default_no_question(self):
        request = _request()
        self.assertEqual(request.kind, 'attestation')
        self.assertFalse(request.default)
        self.assertIn('不确定时选择 n', request.render())
        self.assertIn('作用：', request.render())
        self.assertNotIn('为什么需要：', request.render())

    def test_echo_mode_is_selectable(self):
        request = _request(key='provider.api_token', kind='secret', question='请输入 token', sensitivity='secret', validation={})
        calls = []
        visible = prompt_for_request(request, echo_mode='visible', input_fn=lambda prompt: calls.append('visible') or 'a', secret_input_fn=lambda prompt: calls.append('hidden') or 'b')
        hidden = prompt_for_request(request, echo_mode='hidden', input_fn=lambda prompt: calls.append('visible') or 'a', secret_input_fn=lambda prompt: calls.append('hidden') or 'b')
        self.assertEqual((visible, hidden), ('a', 'b'))
        self.assertEqual(calls, ['visible', 'hidden'])

    def test_cli_parser_exposes_interaction_and_answer_commands(self):
        parser = build_parser()
        run = parser.parse_args(['run', '--project', '/tmp/demo', '--interaction-mode', 'pause', '--secret-echo', 'visible'])
        answer = parser.parse_args(['answer', '--project', '/tmp/demo', '--yes'])
        self.assertEqual(run.interaction_mode, 'pause')
        self.assertEqual(run.secret_echo, 'visible')
        self.assertTrue(answer.yes)
if __name__ == '__main__':
    unittest.main()
