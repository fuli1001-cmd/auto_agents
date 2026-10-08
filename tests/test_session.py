"""Tests for the lightweight Session (fix / collab) workflows."""
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.config import clear_sessions, create_session, delete_session, list_sessions, load_project_config, load_session_state, load_run_state, provider_references_lock_path, requirements_trace_path, save_project_config, save_session_state, save_run_state, session_state_path
from auto_agents.git_ops import commit_all, head_ref, working_tree_clean, worktree_fingerprint
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, CommandResult, DEFAULT_SESSION_MAX_ATTEMPTS, GateParallelGroup, GateResult, RunState, SessionState, TaskSpec, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.requirements import load_requirements_trace, stamp_requirement_contract_hashes
from auto_agents.session import Session
from auto_agents.workflow_chain import IssueBriefBuilder, WorkflowRef, WorkflowStore
from auto_agents.workflow_runtime import WorkflowCoordinator

def _make_project(tmp: str, name: str='demo') -> Path:
    project_root = Path(tmp) / name
    Orchestrator.init_project(project_root, name, 'mock')
    from auto_agents.config import load_run_state, save_run_state
    state = load_run_state(project_root)
    state.status = 'completed'
    save_run_state(project_root, state)
    return project_root

def _confirm_collab_state(state: SessionState, mode: str='simulated') -> SessionState:
    state.goal_execution_environment = {'schema_version': 1, 'mode': mode, 'source': 'test_fixture', 'summary': 'Confirmed environment for an existing workflow test.', 'confirmed': True}
    return state

def _make_provider_blocked_project(tmp: str, name: str='demo') -> tuple[Path, str]:
    project_root = Path(tmp) / name
    Orchestrator.init_project(project_root, name, 'mock')
    spec_file = project_root / 'spec.md'
    spec_file.write_text('# Spec\n', encoding='utf-8')
    reference = '.auto-agents/docs/provider_references/provider.md'
    write_json(requirements_trace_path(project_root), {'version': 1, 'requirements': [{'id': 'REQ-001', 'text': 'Use verified provider documentation.', 'source': 'spec', 'status': 'active', 'priority': 'mandatory', 'acceptance_oracles': ['provider reference is resolved'], 'oracle_type': 'deterministic_test', 'oracle_strength': 'behavioral', 'evidence_boundary': 'internal_state', 'forbidden_proxy_oracles': [], 'forbidden_patterns': [], 'external_docs_required': True, 'provider_reference': reference, 'notes': ''}]})
    write_text(project_root / reference, '# Provider Reference\n\n## Status\n\nambiguous\n')
    write_json(provider_references_lock_path(project_root), {'version': 1, 'references': {'provider': {'path': reference, 'status': 'ambiguous', 'retrieved_at': '2026-04-24T00:00:00Z', 'source_urls': ['https://example.com/official'], 'notes': 'Needs a user decision.'}}})
    state = load_run_state(project_root)
    state.status = 'failed'
    state.stage_summaries = {'clarify': 'done', 'design': 'done', 'plan': 'done'}
    state.last_error = f'provider research is blocked; provide official docs, defer the requirement, choose another provider, or explicitly approve assumptions before resuming.\n- REQ-001: {reference} is ambiguous'
    state.resume_context = {'spec_file': str(spec_file), 'auto_approve': True, 'allow_dirty_tree': False, 'max_tasks': None, 'skip_validate': False, 'print_agent_output': False, 'provider_kind': '', 'doc_language': ''}
    save_run_state(project_root, state)
    return (project_root, reference)

def _add_second_provider_requirement(project_root: Path) -> str:
    reference = '.auto-agents/docs/provider_references/provider-two.md'
    trace = load_requirements_trace(project_root, normalize=False)
    trace['requirements'].append({'id': 'REQ-002', 'text': 'Use a second verified provider dependency.', 'source': 'spec', 'status': 'active', 'priority': 'mandatory', 'acceptance_oracles': ['second provider reference is resolved'], 'oracle_type': 'deterministic_test', 'oracle_strength': 'behavioral', 'evidence_boundary': 'internal_state', 'forbidden_proxy_oracles': [], 'forbidden_patterns': [], 'external_docs_required': True, 'provider_reference': reference, 'notes': ''})
    write_json(requirements_trace_path(project_root), trace)
    write_text(project_root / reference, '# Second Provider Reference\n\n## Status\n\nambiguous\n')
    lock = json.loads(provider_references_lock_path(project_root).read_text(encoding='utf-8'))
    lock['references']['provider-two'] = {'path': reference, 'status': 'ambiguous', 'retrieved_at': '2026-04-24T00:00:00Z', 'source_urls': ['https://example.net/official'], 'notes': 'Needs a separate decision.'}
    write_json(provider_references_lock_path(project_root), lock)
    return reference

def _configure_git_identity(project_root: Path) -> None:
    subprocess.run(['git', 'config', 'user.name', 'test'], cwd=str(project_root), check=True, text=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(project_root), check=True, text=True, capture_output=True)

class SessionStateModelTests(unittest.TestCase):
    """Test SessionState serialization round-trip."""

    def test_round_trip(self) -> None:
        state = SessionState(session_id='abc123', mode='fix', status='conversing', goal='Button does not work', goal_execution_environment={'mode': 'real', 'confirmed': True, 'summary': 'Repair the actual button behavior.'}, authorization_policy={'schema_version': 1, 'mode': 'auto'}, conversation=[{'role': 'user', 'content': 'hello'}], execution_log=[{'attempt': 1, 'action': 'fix', 'result': 'ok', 'timestamp': 't'}], current_attempt=1, attempt_epoch=2, attempts_since_progress=3, max_attempts=4, created_at='2026-01-01T00:00:00Z', updated_at='2026-01-01T00:00:01Z')
        data = state.to_dict()
        restored = SessionState.from_dict(data)
        self.assertEqual(restored.session_id, 'abc123')
        self.assertEqual(restored.mode, 'fix')
        self.assertEqual(restored.goal, 'Button does not work')
        self.assertEqual(restored.goal_execution_environment['mode'], 'real')
        self.assertEqual(restored.authorization_policy['mode'], 'auto')
        self.assertEqual(len(restored.conversation), 1)
        self.assertEqual(len(restored.execution_log), 1)
        self.assertEqual(restored.current_attempt, 1)
        self.assertEqual(restored.attempt_epoch, 2)
        self.assertEqual(restored.attempts_since_progress, 3)
        self.assertEqual(restored.max_attempts, 4)

    def test_defaults(self) -> None:
        state = SessionState(session_id='x')
        self.assertEqual(state.mode, 'fix')
        self.assertEqual(state.status, 'conversing')
        self.assertEqual(state.conversation, [])
        self.assertEqual(state.max_attempts, 4)
        self.assertEqual(state.attempt_epoch, 0)
        self.assertEqual(state.attempts_since_progress, 0)

    def test_json_round_trip(self) -> None:
        state = SessionState(session_id='j1', mode='collab', goal='test')
        blob = json.dumps(state.to_dict())
        restored = SessionState.from_dict(json.loads(blob))
        self.assertEqual(restored.session_id, 'j1')
        self.assertEqual(restored.mode, 'collab')

class RunStateModelTests(unittest.TestCase):
    """Test RunState serialization for resume context."""

    def test_resume_context_round_trip(self) -> None:
        state = RunState(run_id='run-123', status='failed', current_stage='provider_research', implement_verify_baseline_failures=['tests/test_demo.py::test_example'], implement_verify_baseline_ref='deadbeef:e3b0c442', plan_task_replacements={'task-legacy': ['task-child-a', 'task-child-b']}, last_recovery_route={'task_id': 'task-child-a', 'lineage_id': 'task-child-a', 'outcome': 'requeued', 'epoch': 1, 'round': 2}, last_error='provider research is blocked', resume_context={'spec_file': '/tmp/demo/spec.md', 'auto_approve': True, 'allow_dirty_tree': False, 'max_tasks': 5, 'skip_validate': False, 'print_agent_output': True, 'provider_kind': 'copilot-cli', 'doc_language': 'zh-CN'})
        restored = RunState.from_dict(state.to_dict())
        self.assertEqual(restored.run_id, 'run-123')
        self.assertEqual(restored.current_stage, 'provider_research')
        self.assertEqual(restored.implement_verify_baseline_failures, ['tests/test_demo.py::test_example'])
        self.assertEqual(restored.implement_verify_baseline_ref, 'deadbeef:e3b0c442')
        self.assertEqual(restored.plan_task_replacements, {'task-legacy': ['task-child-a', 'task-child-b']})
        self.assertEqual(restored.last_recovery_route['outcome'], 'requeued')
        self.assertEqual(restored.last_recovery_route['epoch'], 1)
        self.assertEqual(restored.resume_context['spec_file'], '/tmp/demo/spec.md')
        self.assertEqual(restored.resume_context['provider_kind'], 'copilot-cli')

    def test_resume_context_defaults_to_empty_dict(self) -> None:
        restored = RunState.from_dict({'run_id': 'run-456'})
        self.assertEqual(restored.resume_context, {})
        self.assertEqual(restored.last_recovery_route, {})

    def test_task_recovery_lineage_round_trip(self) -> None:
        task = TaskSpec(task_id='task-271b', title='Split child', description='Implement the split slice.', acceptance=['The observable proof passes.'], parent_task_id='task-271', split_depth=1, task_origin='scope_split', recovery_epoch=2, recovery_round=1, verify_retry_epoch=3)
        restored = TaskSpec.from_dict(task.to_dict())
        self.assertEqual(restored.task_origin, 'scope_split')
        self.assertEqual(restored.recovery_epoch, 2)
        self.assertEqual(restored.recovery_round, 1)
        self.assertEqual(restored.verify_retry_epoch, 3)

class SessionResumeTests(unittest.TestCase):
    """Test session resume and persistence."""

    def test_fix_resolution_field_persisted(self) -> None:
        """Verify the resolution field survives serialization round-trip."""
        state = SessionState(session_id='test-res-001', mode='fix', status='completed', resolution='not_a_bug')
        data = state.to_dict()
        self.assertEqual(data['resolution'], 'not_a_bug')
        restored = SessionState.from_dict(data)
        self.assertEqual(restored.resolution, 'not_a_bug')
        state2 = SessionState(session_id='test-res-002')
        self.assertEqual(state2.resolution, '')

class SessionCLITests(unittest.TestCase):
    """Test that CLI properly parses fix/collab commands."""

class SessionListCommandTests(unittest.TestCase):
    """Test the sessions list command end-to-end."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        state_dir = self.tmpdir / '.auto-agents' / 'state'
        state_dir.mkdir(parents=True)
        (state_dir / 'run_state.json').write_text(json.dumps({'status': 'completed'}))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir)

class ErrorFeedbackTests(unittest.TestCase):
    """Test _build_error_feedback for stall/timeout/transient classification."""

    def _make_session(self, tmp: str) -> Session:
        project_root = _make_project(tmp)
        orchestrator = Orchestrator(project_root)
        return Session(orchestrator, mode='collab')

class TailLinesTests(unittest.TestCase):
    """Test the _tail_lines helper from base.py."""

    def test_tail_lines_basic(self) -> None:
        from auto_agents.adapters.base import _tail_lines
        chunks = ['line1\n', 'line2\n', 'line3\n', 'line4\n', 'line5\n']
        result = _tail_lines(chunks, 3)
        self.assertEqual(result, 'line3\nline4\nline5')

    def test_tail_lines_fewer_than_n(self) -> None:
        from auto_agents.adapters.base import _tail_lines
        chunks = ['line1\n', 'line2\n']
        result = _tail_lines(chunks, 5)
        self.assertEqual(result, 'line1\nline2')

    def test_tail_lines_empty(self) -> None:
        from auto_agents.adapters.base import _tail_lines
        result = _tail_lines([], 5)
        self.assertEqual(result, '')

class ProcessGroupKillTests(unittest.TestCase):
    """Test _kill_process_group handles various process states."""

    def test_kill_already_exited(self) -> None:
        from auto_agents.adapters.base import _kill_process_group
        process = subprocess.Popen(['true'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        process.wait()
        _kill_process_group(process)

    def test_kill_running_process(self) -> None:
        from auto_agents.adapters.base import _kill_process_group
        process = subprocess.Popen(['sleep', '60'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        _kill_process_group(process)
        self.assertIsNotNone(process.returncode)

class CodexJsonStreamFilterTests(unittest.TestCase):
    """Test that CodexAdapter._make_json_stream_filter parses JSON lines correctly."""

    def test_agent_message_forwarded(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        line = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Hello world'}})
        cb('stdout', line + '\n')
        self.assertEqual(received, [('stdout', 'Hello world\n')])

    def test_non_message_events_suppressed(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb('stdout', json.dumps({'type': 'turn.started'}) + '\n')
        cb('stdout', json.dumps({'type': 'turn.completed', 'usage': {}}) + '\n')
        self.assertEqual(received, [])

    def test_error_events_forwarded_as_stderr(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb('stdout', json.dumps({'type': 'error', 'message': 'quota exceeded'}) + '\n')
        self.assertEqual(received, [('stderr', 'quota exceeded\n')])

    def test_stderr_passthrough(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb('stderr', 'Reading prompt from stdin...\n')
        self.assertEqual(received, [('stderr', 'Reading prompt from stdin...\n')])

    def test_non_json_forwarded_as_is(self) -> None:
        from auto_agents.adapters.codex import CodexAdapter
        received = []
        cb = CodexAdapter._make_json_stream_filter(lambda s, c: received.append((s, c)))
        cb('stdout', 'plain text output\n')
        self.assertEqual(received, [('stdout', 'plain text output\n')])

class SessionStateNewFieldsTests(unittest.TestCase):
    """Test new convergence-related fields on SessionState."""

    def test_new_fields_defaults(self) -> None:
        state = SessionState(session_id='x')
        self.assertEqual(state.stall_count, 0)
        self.assertEqual(state.last_diff_hash, '')
        self.assertEqual(state.last_verify_sig, '')
        self.assertEqual(state.consecutive_agent_errors, 0)
        self.assertEqual(state.hard_ceiling, 15)
        self.assertEqual(state.attempt_epoch, 0)
        self.assertEqual(state.attempts_since_progress, 0)
        self.assertEqual(state.fix_verify_command, '')
        self.assertEqual(state.baseline_failures, [])
        self.assertEqual(state.baseline_git_ref, '')

    def test_new_fields_round_trip(self) -> None:
        state = SessionState(session_id='rt1', stall_count=2, last_diff_hash='abc', last_verify_sig='def', consecutive_agent_errors=3, hard_ceiling=20, attempt_epoch=4, attempts_since_progress=5, fix_verify_command='pytest -k test_bug', baseline_failures=['tests/test_a.py::test_x', 'cmd:npm test'], baseline_git_ref='abc123')
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.stall_count, 2)
        self.assertEqual(restored.last_diff_hash, 'abc')
        self.assertEqual(restored.last_verify_sig, 'def')
        self.assertEqual(restored.consecutive_agent_errors, 3)
        self.assertEqual(restored.hard_ceiling, 20)
        self.assertEqual(restored.attempt_epoch, 4)
        self.assertEqual(restored.attempts_since_progress, 5)
        self.assertEqual(restored.fix_verify_command, 'pytest -k test_bug')
        self.assertEqual(restored.baseline_failures, ['tests/test_a.py::test_x', 'cmd:npm test'])
        self.assertEqual(restored.baseline_git_ref, 'abc123')

    def test_backward_compat_missing_new_fields(self) -> None:
        """Old session_state.json without new fields should deserialize with defaults."""
        old_data = {'session_id': 'old1', 'mode': 'fix', 'status': 'executing', 'goal': 'some bug', 'conversation': [], 'execution_log': [], 'current_attempt': 2, 'max_attempts': 4, 'resolution': '', 'created_at': 't', 'updated_at': 't'}
        restored = SessionState.from_dict(old_data)
        self.assertEqual(restored.stall_count, 0)
        self.assertEqual(restored.last_diff_hash, '')
        self.assertEqual(restored.consecutive_agent_errors, 0)
        self.assertEqual(restored.hard_ceiling, 15)
        self.assertEqual(restored.attempt_epoch, 0)
        self.assertEqual(restored.attempts_since_progress, 2)
        self.assertEqual(restored.fix_verify_command, '')
        self.assertEqual(restored.baseline_failures, [])
        self.assertEqual(restored.baseline_git_ref, '')

class ConvergenceHelperTests(unittest.TestCase):
    """Test convergence detection helper methods."""

    @staticmethod
    def _make_stub_session():
        """Build a minimal Session with a stub orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            project_root = _make_project(tmp)
            orchestrator = Orchestrator(project_root, user_input_fn=lambda _: '')
            return Session(orchestrator, mode='fix')

class SessionsListSlimTests(unittest.TestCase):
    """Test that the sessions command omits verbose fields."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        state_dir = self.tmpdir / '.auto-agents' / 'state'
        state_dir.mkdir(parents=True)
        (state_dir / 'run_state.json').write_text(json.dumps({'status': 'completed'}))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir)

class GatesCollectAllTests(unittest.TestCase):
    """Test run_commands_collect_all and extract_failure_ids."""

    def test_collect_all_does_not_short_circuit(self) -> None:
        from auto_agents.gates import run_commands_collect_all
        with tempfile.TemporaryDirectory() as tmp:
            result = run_commands_collect_all(['exit 1', 'echo ok', 'exit 2'], Path(tmp))
            self.assertFalse(result.ok)
            self.assertEqual(len(result.commands), 3)
            self.assertFalse(result.commands[0].ok)
            self.assertTrue(result.commands[1].ok)
            self.assertFalse(result.commands[2].ok)

    def test_extract_pytest_failure_ids(self) -> None:
        from auto_agents.gates import extract_failure_ids
        from auto_agents.models import CommandResult, GateResult
        cmd = CommandResult(command='pytest', ok=False, returncode=1, stdout='FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n', stderr='')
        gate = GateResult(ok=False, commands=[cmd])
        ids = extract_failure_ids(gate)
        self.assertEqual(ids, ['tests/test_a.py::test_x', 'tests/test_b.py::test_y'])

    def test_extract_non_pytest_failure_ids(self) -> None:
        from auto_agents.gates import extract_failure_ids
        from auto_agents.models import CommandResult, GateResult
        cmd = CommandResult(command='npm test', ok=False, returncode=1, stdout='Error: build failed', stderr='')
        gate = GateResult(ok=False, commands=[cmd])
        ids = extract_failure_ids(gate)
        self.assertEqual(ids, ['cmd:npm test'])
if __name__ == '__main__':
    unittest.main()
