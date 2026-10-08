import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.agent_instructions import ensure_agent_instructions_synced, load_current_normalized_project_rules, normalized_project_rules_current, normalized_project_rules_identifier_feedback, parse_normalized_project_rules_output, project_rules_source_sha256, sync_agent_instructions, write_normalized_project_rules
from auto_agents.config import agent_instructions_lock_path, design_md_path, frontend_design_lock_path, frontend_prototype_dir, load_project_config, normalized_project_rules_path, project_rules_path, save_project_config
from auto_agents.frontend_design import frontend_design_artifact_hashes
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, TaskSpec

class RecordingNormalizeAdapter:

    def __init__(self, hard_rule: str='Normalized output review pass proceeds directly to export.') -> None:
        self.hard_rule = hard_rule
        self.requests = []

    def available(self) -> bool:
        return True

    def run(self, request):
        self.requests.append(request)
        content = json.dumps({'rules': {'hard_rules': [self.hard_rule], 'workflow_contracts': ['output_review pass -> export'], 'engineering_validation': ['Intermediate artifacts must be structurally valid before composition.'], 'testing_contracts': ['Tests must not expect awaiting_output_confirmation on the default path.']}}, ensure_ascii=False)
        write_text(request.output_path, content)
        return AgentResult(ok=True, command=['recording-normalize'], output_path=request.output_path, summary=content, stdout=content, returncode=0)

class SequentialNormalizeAdapter:

    def __init__(self, payloads) -> None:
        self.payloads = list(payloads)
        self.requests = []

    def available(self) -> bool:
        return True

    def run(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.payloads) - 1)
        content = json.dumps(self.payloads[index], ensure_ascii=False)
        write_text(request.output_path, content)
        return AgentResult(ok=True, command=['sequential-normalize'], output_path=request.output_path, summary=content, stdout=content, returncode=0)

class AgentInstructionSyncTests(unittest.TestCase):

    @staticmethod
    def _commit_all(project_root: Path) -> None:
        subprocess.run(['git', 'config', 'user.name', 'test'], cwd=str(project_root), check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(project_root), check=True)
        subprocess.run(['git', 'add', '-A'], cwd=str(project_root), check=True)
        subprocess.run(['git', 'commit', '-m', 'baseline'], cwd=str(project_root), check=True)

    def test_parse_normalized_project_rules_output_accepts_fenced_json(self) -> None:
        payload = parse_normalized_project_rules_output('```json\n{\n  "rules": {\n    "hard_rules": ["A"],\n    "workflow_contracts": ["B"],\n    "engineering_validation": ["C"],\n    "testing_contracts": ["D"]\n  }\n}\n```')
        self.assertEqual(payload['hard_rules'], ['A'])
        self.assertEqual(payload['workflow_contracts'], ['B'])
        self.assertEqual(payload['engineering_validation'], ['C'])
        self.assertEqual(payload['testing_contracts'], ['D'])

    def test_normalized_identifier_feedback_rejects_identifiers_missing_from_source(self) -> None:
        feedback = normalized_project_rules_identifier_feedback(source_text='默认合同是 `process_review` pass -> `output_review` pass -> export。\n', rules={'hard_rules': ['Do not keep `process_moderation` in the default path.'], 'workflow_contracts': ['`output_review` pass -> export.'], 'engineering_validation': [], 'testing_contracts': []})
        self.assertIsNotNone(feedback)
        self.assertIn('process_moderation', feedback or '')
        self.assertNotIn('output_review', feedback or '')
if __name__ == '__main__':
    unittest.main()
