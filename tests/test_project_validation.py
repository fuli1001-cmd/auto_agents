import contextlib
import copy
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.cli import build_parser, main
from auto_agents.adapters.codex import CodexAdapter
from auto_agents.reporting import get_reporter
from auto_agents.config import DEFAULT_CONFIG, auto_dir, config_path, create_session, ensure_auto_gitignore, load_project_config, load_run_state, migrate_project_config, save_session_state, requirements_trace_path, run_path, save_project_config, save_run_state, save_task_plan, task_plan_path
from auto_agents.git_ops import head_ref, working_tree_clean, worktree_fingerprint
from auto_agents.gates import GateCommandTimeoutError
from auto_agents.io_utils import read_json, write_json, write_text
from auto_agents.models import AgentRequest, AgentResult, AgentUsage, AutonomyConfig, CommandResult, GateResult, ProjectConfig, ProviderConfig, RunState, SessionState, TaskSpec, VerificationStep
from auto_agents.requirements import AMBIGUOUS_REQUIREMENT_CONTRACT_RECOVERY_CATEGORY, NonAmendableRequirementContractRecoveryError
from auto_agents.run_lock import RUN_LOCK_FD_ENV, RUN_LOCK_KEY_ENV, RUN_LOCK_TOKEN_ENV, ProjectRunLock, RunAlreadyActiveError, runtime_status, stop_project_run
from auto_agents.process_supervision import RunInterruptedError
from auto_agents.validation import validate_required_document, validate_project_config_payload, validate_task_dependencies, validate_task_plan_payload, validation_report
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore

class ProjectRunLockTests(unittest.TestCase):

    def test_rejects_a_second_external_run_for_same_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'demo'
            project_root.mkdir()
            with ProjectRunLock(project_root):
                with self.assertRaises(RunAlreadyActiveError) as ctx:
                    ProjectRunLock(project_root, environ={}).acquire()
            self.assertIn('another auto_agents run is already active', str(ctx.exception))
            self.assertIn(str(project_root.resolve()), str(ctx.exception))

    def test_accepts_explicit_inherited_self_repair_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'demo'
            project_root.mkdir()
            with ProjectRunLock(project_root) as parent_lock:
                inherited_fd = os.dup(parent_lock.fileno)
                inherited = ProjectRunLock(project_root, environ={RUN_LOCK_FD_ENV: str(inherited_fd), RUN_LOCK_KEY_ENV: parent_lock.key, RUN_LOCK_TOKEN_ENV: parent_lock.run_token})
                try:
                    inherited.acquire()
                    self.assertEqual(inherited.fileno, inherited_fd)
                finally:
                    inherited.release()

    def test_acquire_reports_stale_control_as_interrupted_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'demo'
            project_root.mkdir()
            first = ProjectRunLock(project_root)
            first.acquire()
            owner_payload = first.owner_payload()
            control_payload = json.loads(first.control_path.read_text(encoding='utf-8'))
            control_path = first.control_path
            first.release()
            write_json(control_path, control_payload)
            second = ProjectRunLock(project_root, environ={})
            try:
                second.acquire()
                snapshot = second.interrupted_snapshot
            finally:
                second.release()
            self.assertEqual(snapshot['owner']['pid'], owner_payload['pid'])
            self.assertEqual(snapshot['control']['project'], str(project_root.resolve()))

    def test_acquire_ignores_retired_health_control_without_business_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'demo'
            project_root.mkdir()
            health_control = project_root / '.auto-agents' / 'state' / 'health-watch-control.json'
            write_json(health_control, {'schema_version': 1, 'project': str(project_root.resolve()), 'workflow_kind': 'run', 'subject_id': 'run-1', 'run_token': 'old-token', 'owner_pid': 999999, 'owner_start_ticks': 1, 'process_phase': 'self_repair', 'updated_at': '2026-08-31T00:00:00+00:00'})
            lock = ProjectRunLock(project_root, environ={})
            try:
                lock.acquire()
                snapshot = lock.interrupted_snapshot
            finally:
                lock.release()
            self.assertEqual(snapshot, {})

    def test_runtime_status_and_stop_terminate_external_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'demo'
            project_root.mkdir()
            script = Path(tmp) / 'owner.py'
            script.write_text(f"import os, subprocess, sys, time\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\nfrom auto_agents.process_supervision import ACTIVE_PROCESSES\nfrom auto_agents.run_lock import ProjectRunLock\nroot = __import__('pathlib').Path(sys.argv[1])\nwith ProjectRunLock(root):\n    child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n    ACTIVE_PROCESSES.register(child, kind='test-child')\n    print('ready', flush=True)\n    time.sleep(60)\n", encoding='utf-8')
            owner = subprocess.Popen([sys.executable, str(script), str(project_root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'ready')
                status = runtime_status(project_root)
                self.assertTrue(status['active'])
                self.assertEqual(status['owner_pid'], owner.pid)
                self.assertEqual(status['active_process_groups'], 1)
                payload, exit_code = stop_project_run(project_root, grace_seconds=1)
                owner.wait(timeout=5)
                self.assertEqual(exit_code, 0)
                self.assertEqual(payload['status'], 'stopped')
                self.assertFalse(runtime_status(project_root)['active'])
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)

    def test_stop_escalates_to_sigkill_for_term_ignoring_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'demo'
            project_root.mkdir()
            script = Path(tmp) / 'stubborn_owner.py'
            script.write_text(f"import signal, subprocess, sys, time\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\nfrom auto_agents.process_supervision import ACTIVE_PROCESSES\nfrom auto_agents.run_lock import ProjectRunLock\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nroot = __import__('pathlib').Path(sys.argv[1])\nwith ProjectRunLock(root):\n    child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'], start_new_session=True)\n    ACTIVE_PROCESSES.register(child, kind='stubborn-child')\n    print('ready', flush=True)\n    time.sleep(60)\n", encoding='utf-8')
            owner = subprocess.Popen([sys.executable, str(script), str(project_root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'ready')
                payload, exit_code = stop_project_run(project_root, grace_seconds=0.1, kill_grace_seconds=2)
                owner.wait(timeout=5)
                self.assertEqual(exit_code, 0)
                self.assertEqual(payload['status'], 'stopped')
                self.assertTrue(payload['forced'])
                self.assertFalse(runtime_status(project_root)['active'])
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)

class ProjectValidationTests(unittest.TestCase):

    def test_removed_self_repair_budget_fields_fail_during_config_load(self) -> None:
        with self.assertRaisesRegex(ValueError, 'max_candidates_per_root was removed'):
            AutonomyConfig.from_dict({'max_candidates_per_root': 3})
        with self.assertRaisesRegex(ValueError, 'total_timeout_seconds was removed'):
            AutonomyConfig.from_dict({'total_timeout_seconds': 3600})

    def test_cli_recover_command_is_removed(self) -> None:
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            main(['recover', '--project', '/tmp/demo'])
        self.assertEqual(raised.exception.code, 2)

    @staticmethod
    def _configure_git_identity(project_root: Path) -> None:
        subprocess.run(['git', 'config', 'user.name', 'test'], cwd=str(project_root), check=True, text=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(project_root), check=True, text=True, capture_output=True)

    def test_validate_project_config_payload_rejects_bad_effort_and_template(self) -> None:
        payload = {'project_name': 'demo', 'providers': {'codex': {'kind': 'codex', 'binary': 'codex', 'profile_map': {}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}, 'copilot-cli': {'kind': 'copilot-cli', 'binary': 'copilot', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}}, 'active_provider': 'codex', 'docs': {'language': 'jp'}, 'efforts': {'clarify': 'deep', 'design': 'wrong', 'plan': 'balanced', 'implement': 'balanced', 'review': 'deep', 'verify': 'balanced'}, 'gates': {'commands': [], 'require_clean_git_before_task': True, 'allow_agent_updates': True}, 'git': {'auto_init_repo': True, 'commit_message_template': 'feat: missing placeholders'}, 'approvals': {'enabled': ['requirements', 'bad']}, 'retries': {'default_max_attempts': 0, 'per_stage': {'plan': 2, 'sync-agent-instructions': 2, 'unknown': 1}}}
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('efforts.design' in item for item in errors)))
        self.assertTrue(any(("commit_message_template must contain '{task_id}'" in item for item in errors)))
        self.assertTrue(any(('invalid values' in item for item in errors)))
        self.assertTrue(any(('default_max_attempts' in item for item in errors)))
        self.assertTrue(any(('unknown stage' in item for item in errors)))
        self.assertTrue(any(('docs.language' in item for item in errors)))

    def test_validate_project_config_rejects_invalid_infrastructure_and_failover_settings(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload['gates']['reported_infrastructure_markers'] = [{'id': 'Bad ID', 'contains': ''}, {'id': 'browser_failed', 'contains': 'one'}, {'id': 'browser_failed', 'contains': 'two'}]
        payload['gates']['distributed']['reported_infrastructure_max_workers'] = 0
        payload['execution']['provider_failover'] = {'probe_enabled': 'yes', 'probe_timeout_seconds': 0, 'connection_cooldown_seconds': 60, 'pressure_cooldown_seconds': 300, 'timeout_cooldown_seconds': 1800, 'quota_cooldown_seconds': 3600, 'max_cooldown_seconds': 300}
        payload['execution']['supervision'] = {'mode': 'invalid'}
        payload['execution']['self_repair_diagnosis'] = {'mode': 'sometimes', 'investigator_timeout_seconds': 0, 'reviewer_timeout_seconds': 600, 'arbiter_timeout_seconds': 600, 'command_timeout_seconds': 300, 'max_dynamic_commands': 0, 'confidence_threshold': 1.5, 'arbiter_confidence_threshold': 0.9, 'max_repair_cycles': 0, 'network_enabled': 'yes'}
        payload['execution']['autonomy'] = {'mode': 'unbounded', 'max_candidates_per_root': 0, 'total_timeout_seconds': 1, 'max_consecutive_non_improving_candidates': 0, 'max_frontier_candidates': 0, 'candidate_timeout_seconds': 1, 'candidate_review_timeout_seconds': 1, 'replay_timeout_seconds': 1, 'continue_independent_tasks': 'yes', 'allow_isolated_dirty_checkout': True, 'require_remote_publish': False}
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('execution.supervision.mode' in item for item in errors)))
        self.assertTrue(any(('reported_infrastructure_markers[0].id' in item for item in errors)))
        self.assertTrue(any(('reported_infrastructure_markers[0].contains' in item for item in errors)))
        self.assertTrue(any(('reported_infrastructure_markers[2].id must be unique' in item for item in errors)))
        self.assertTrue(any(('reported_infrastructure_max_workers' in item for item in errors)))
        self.assertTrue(any(('provider_failover.probe_enabled' in item for item in errors)))
        self.assertTrue(any(('provider_failover.probe_timeout_seconds' in item for item in errors)))
        self.assertTrue(any(('max_cooldown_seconds must be' in item for item in errors)))
        self.assertTrue(any(('execution.autonomy.mode' in item for item in errors)))
        self.assertTrue(any(('execution.autonomy.max_candidates_per_root' in item for item in errors)))
        self.assertTrue(any(('execution.autonomy.total_timeout_seconds' in item for item in errors)))
        self.assertTrue(any(('execution.autonomy.max_consecutive_non_improving_candidates' in item for item in errors)))
        self.assertTrue(any(('execution.autonomy.max_frontier_candidates' in item for item in errors)))
        self.assertTrue(any(('execution.autonomy.candidate_timeout_seconds' in item for item in errors)))
        self.assertTrue(any(('execution.autonomy.continue_independent_tasks' in item for item in errors)))

    def test_task_plan_validation_rejects_invalid_recovery_lineage(self) -> None:
        task = {'task_id': 'task-271b', 'title': 'Split child', 'description': 'Implement the split slice.', 'acceptance': ['Observable proof passes.'], 'status': 'pending', 'commit_message': '', 'task_origin': 'generated-by-id-guess', 'recovery_epoch': -1, 'recovery_round': True, 'verify_retry_epoch': -1}
        errors = validate_task_plan_payload({'tasks': [task]})
        self.assertTrue(any(('task_origin must be one of' in item for item in errors)))
        self.assertTrue(any(('recovery_epoch must be an integer >= 0' in item for item in errors)))
        self.assertTrue(any(('recovery_round must be an integer >= 0' in item for item in errors)))
        self.assertTrue(any(('verify_retry_epoch must be an integer >= 0' in item for item in errors)))

    def test_validate_project_config_payload_rejects_non_isolated_python_commands(self) -> None:
        payload = {'project_name': 'demo', 'providers': {'codex': {'kind': 'codex', 'binary': 'codex', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}, 'copilot-cli': {'kind': 'copilot-cli', 'binary': 'copilot', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}}, 'active_provider': 'codex', 'docs': {'language': 'en'}, 'efforts': {'clarify': 'deep', 'design': 'deep', 'plan': 'balanced', 'provider_research': 'deep', 'implement': 'balanced', 'review': 'deep', 'verify': 'balanced', 'readme': 'balanced'}, 'gates': {'commands': ['python3 -m unittest discover -s tests', 'python3 -m pip install requests'], 'require_clean_git_before_task': True, 'allow_agent_updates': True}, 'git': {'auto_init_repo': True, 'commit_message_template': 'feat({task_id}): {title}'}, 'approvals': {'enabled': ['requirements', 'architecture', 'release']}, 'retries': {'default_max_attempts': 2, 'per_stage': {'clarify': 2, 'design': 2, 'plan': 3, 'provider_research': 2, 'implement': 2, 'review': 2}}}
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('project-local conda env' in item for item in errors)))
        self.assertTrue(any(('must not modify shared system environments' in item for item in errors)))

    def test_validate_project_config_payload_accepts_isolated_python_commands(self) -> None:
        payload = {'project_name': 'demo', 'providers': {'codex': {'kind': 'codex', 'binary': 'codex', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}, 'copilot-cli': {'kind': 'copilot-cli', 'binary': 'copilot', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}}, 'active_provider': 'codex', 'docs': {'language': 'zh'}, 'efforts': {'clarify': 'deep', 'design': 'deep', 'plan': 'balanced', 'provider_research': 'deep', 'implement': 'balanced', 'review': 'deep', 'verify': 'balanced', 'readme': 'balanced'}, 'gates': {'commands': ['conda run -p ./.conda python -m pytest -q tests'], 'require_clean_git_before_task': True, 'allow_agent_updates': True}, 'git': {'auto_init_repo': True, 'commit_message_template': 'feat({task_id}): {title}'}, 'approvals': {'enabled': ['requirements', 'architecture', 'release']}, 'retries': {'default_max_attempts': 2, 'per_stage': {'clarify': 2, 'design': 2, 'plan': 3, 'provider_research': 2, 'implement': 2, 'review': 2}}}
        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_accepts_parallel_gate_groups(self) -> None:
        payload = {'project_name': 'demo', 'providers': {'codex': {'kind': 'codex', 'binary': 'codex', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}, 'copilot-cli': {'kind': 'copilot-cli', 'binary': 'copilot', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}}, 'active_provider': 'codex', 'docs': {'language': 'en'}, 'efforts': {'clarify': 'deep', 'design': 'deep', 'plan': 'balanced', 'provider_research': 'deep', 'implement': 'balanced', 'review': 'deep', 'verify': 'balanced', 'readme': 'balanced'}, 'gates': {'commands': ['conda run -p ./.conda python -m pytest -q tests'], 'parallel_groups': [{'name': 'quality', 'commands': ['conda run -p ./.conda python -m pytest -q tests', 'conda run -p ./.conda python -m pytest -q tests/test_ok.py']}], 'require_clean_git_before_task': True, 'allow_agent_updates': True}, 'git': {'auto_init_repo': True, 'commit_message_template': 'feat({task_id}): {title}'}, 'approvals': {'enabled': ['requirements', 'architecture', 'release']}, 'retries': {'default_max_attempts': 2, 'per_stage': {'clarify': 2, 'design': 2, 'plan': 3, 'provider_research': 2, 'implement': 2, 'review': 2}}}
        self.assertEqual(validate_project_config_payload(payload), [])

    def test_validate_project_config_payload_rejects_invalid_parallel_gate_groups(self) -> None:
        payload = {'project_name': 'demo', 'providers': {'codex': {'kind': 'codex', 'binary': 'codex', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}, 'copilot-cli': {'kind': 'copilot-cli', 'binary': 'copilot', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}}, 'active_provider': 'codex', 'docs': {'language': 'en'}, 'efforts': {'clarify': 'deep', 'design': 'deep', 'plan': 'balanced', 'provider_research': 'deep', 'implement': 'balanced', 'review': 'deep', 'verify': 'balanced', 'readme': 'balanced'}, 'gates': {'commands': [], 'parallel_groups': [{'name': '', 'commands': ['']}], 'require_clean_git_before_task': True, 'allow_agent_updates': True}, 'git': {'auto_init_repo': True, 'commit_message_template': 'feat({task_id}): {title}'}, 'approvals': {'enabled': ['requirements', 'architecture', 'release']}, 'retries': {'default_max_attempts': 2, 'per_stage': {'clarify': 2, 'design': 2, 'plan': 3, 'provider_research': 2, 'implement': 2, 'review': 2}}}
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('gates.parallel_groups[1].name' in item for item in errors)))
        self.assertTrue(any(('gates.parallel_groups[1].commands' in item for item in errors)))

    def test_validate_project_config_payload_accepts_parallel_task_execution(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload['execution'] = {'parallel_tasks': {'enabled': True, 'workers': 'auto', 'max_auto_workers': 3, 'adaptive': True, 'strict': False, 'worktree_root': ''}}
        self.assertEqual(validate_project_config_payload(payload), [])

    def test_acceleration_defaults_enable_safe_parallel_execution_and_release_prewarm(self) -> None:
        config = ProjectConfig.from_dict(copy.deepcopy(DEFAULT_CONFIG))
        self.assertTrue(config.execution.acceleration.enabled)
        self.assertTrue(config.execution.parallel_tasks.enabled)
        self.assertTrue(config.gates.release_worker.enabled)
        self.assertTrue(config.gates.release_worker.auto_start)

    def test_validate_project_config_payload_rejects_unquoted_pytest_marker_expression(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload['gates']['commands'] = ['conda run -p ./.conda python -m pytest -q -m not storage_real_smoke and not real_provider_smoke tests']
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('pytest -m expression must be one shell argument' in error for error in errors)))

    def test_validate_project_config_payload_rejects_invalid_parallel_workers(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload['execution'] = {'parallel_tasks': {'enabled': True, 'workers': 'many', 'max_auto_workers': 2, 'adaptive': True, 'strict': True, 'worktree_root': ''}}
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('execution.parallel_tasks.workers' in item for item in errors)))

    def test_gate_command_timeout_defaults_to_7200_seconds(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload['gates'].pop('command_timeout_seconds', None)
        payload['gates'].pop('worker_slot_wait_timeout_seconds', None)
        config = ProjectConfig.from_dict(payload)
        self.assertEqual(config.gates.command_timeout_seconds, 7200)
        self.assertEqual(config.gates.worker_slot_wait_timeout_seconds, 7200)
        self.assertEqual(config.gates.command_idle_timeout_seconds, 900)
        self.assertTrue(config.gates.adaptive_timeout_enabled)
        self.assertEqual(config.execution.recovery.max_occurrences_per_root_cause, 3)

    def test_validate_project_config_payload_rejects_invalid_gate_timeout(self) -> None:
        for field in ('command_timeout_seconds', 'worker_slot_wait_timeout_seconds'):
            for invalid in (0, -1, True, '1800'):
                with self.subTest(field=field, value=invalid):
                    payload = copy.deepcopy(DEFAULT_CONFIG)
                    payload['gates'][field] = invalid
                    errors = validate_project_config_payload(payload)
                    self.assertTrue(any((f'gates.{field}' in item for item in errors)))

    def test_validate_project_config_payload_rejects_invalid_sync_agent_instructions_effort(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload['efforts']['sync-agent-instructions'] = 'wrong'
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('efforts.sync-agent-instructions' in item for item in errors)))

    def test_validate_project_config_payload_accepts_config_without_docs(self) -> None:
        payload = {'project_name': 'demo', 'providers': {'codex': {'kind': 'codex', 'binary': 'codex', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}, 'copilot-cli': {'kind': 'copilot-cli', 'binary': 'copilot', 'profile_map': {'balanced': 'balanced', 'deep': 'deep', 'max': 'max'}, 'extra_args': [], 'cwd_flag': '-C', 'prompt_via_stdin': True, 'output_flag': '-o'}}, 'active_provider': 'codex', 'efforts': {'clarify': 'deep', 'design': 'deep', 'plan': 'balanced', 'provider_research': 'deep', 'implement': 'balanced', 'review': 'deep', 'verify': 'balanced', 'readme': 'balanced'}, 'gates': {'commands': ['conda run -p ./.conda python -m pytest -q tests'], 'require_clean_git_before_task': True, 'allow_agent_updates': True}, 'git': {'auto_init_repo': True, 'commit_message_template': 'feat({task_id}): {title}'}, 'approvals': {'enabled': ['requirements', 'architecture', 'release']}, 'retries': {'default_max_attempts': 2, 'per_stage': {'clarify': 2, 'design': 2, 'plan': 3, 'provider_research': 2, 'implement': 2, 'review': 2}}}
        self.assertEqual(validate_project_config_payload(payload), [])

    def test_legacy_efforts_missing_defaulted_stages_are_accepted(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        del payload['efforts']['provider_research']
        del payload['efforts']['sync-agent-instructions']
        del payload['efforts']['self_repair']
        del payload['efforts']['self_repair_review']
        self.assertEqual(validate_project_config_payload(payload), [])
        config = ProjectConfig.from_dict(payload)
        self.assertEqual(config.efforts['provider_research'], 'deep')
        self.assertEqual(config.efforts['sync-agent-instructions'], 'deep')
        self.assertEqual(config.efforts['self_repair'], 'deep')
        self.assertEqual(config.efforts['self_repair_review'], 'max')

    def test_project_config_rejects_legacy_agent_instructions_node(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        payload['agent_instructions'] = {'normalize_with_llm': False, 'normalization_effort_stage': 'design'}
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('agent_instructions is no longer supported' in item for item in errors)))
        with self.assertRaisesRegex(ValueError, 'agent_instructions'):
            ProjectConfig.from_dict(payload)

    def test_validate_project_config_payload_still_requires_other_effort_stages(self) -> None:
        payload = copy.deepcopy(DEFAULT_CONFIG)
        del payload['efforts']['readme']
        errors = validate_project_config_payload(payload)
        self.assertTrue(any(('efforts missing stages: readme' in item for item in errors)))

    def test_validate_required_document_reports_missing_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'architecture.md'
            write_text(path, '# Architecture\n\nOnly one heading\n')
            errors = validate_required_document(path, 'architecture.md')
            self.assertTrue(any(('## System Boundary' in item for item in errors)))

    def test_autonomy_cli_override_is_available_to_all_stateful_commands(self) -> None:
        parser = build_parser()
        for command in ('run', 'fix', 'collab', 'provider-resolve', 'answer'):
            args = parser.parse_args([command, '--project', '/tmp/demo', '--autonomy', 'guarded'])
            self.assertEqual(args.autonomy, 'guarded')

    def test_cli_init_defaults_name_from_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'aa-demo'
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(['init', '--project', str(project_root)])
            self.assertEqual(exit_code, 0)
            config = load_project_config(project_root)
            self.assertEqual(config.project_name, 'aa-demo')
            self.assertEqual(config.provider.kind, 'codex')
            self.assertEqual(config.docs.language, 'en')

    def test_cli_init_can_set_document_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / 'aa-demo'
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(['init', '--project', str(project_root), '--doc-language', 'zh'])
            self.assertEqual(exit_code, 0)
            config = load_project_config(project_root)
            self.assertEqual(config.docs.language, 'zh')

    def test_codex_adapter_parses_usage_from_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            output_path = project_root / 'agent.md'
            write_text(output_path, 'final summary\n')
            adapter = CodexAdapter(ProviderConfig())
            request = AgentRequest(stage='clarify', effort='deep', prompt='prompt', cwd=project_root, output_path=output_path, attachments=[project_root / 'prototype.png', project_root / 'actual.png'])
            with patch('auto_agents.adapters.codex.run_subprocess_with_optional_streaming') as run_mock:
                run_mock.return_value = ('{"type":"thread.started","thread_id":"t"}\n{"type":"item.completed","item":{"type":"agent_message","text":"final summary"}}\n{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":50,"output_tokens":25}}\n', '', 0, False, False)
                result = adapter.run(request)
            self.assertTrue(result.ok)
            command = run_mock.call_args.args[0]
            self.assertIn('--sandbox', command)
            self.assertIn('workspace-write', command)
            self.assertNotIn('--full-auto', command)
            image_paths = [command[index + 1] for index, value in enumerate(command[:-1]) if value == '--image']
            self.assertEqual(image_paths, [str(project_root / 'prototype.png'), str(project_root / 'actual.png')])
            self.assertEqual(result.model, 'profile:deep')
            self.assertIsNotNone(result.usage)
            usage = result.usage
            self.assertEqual(usage.input_tokens if usage else None, 200)
            self.assertEqual(usage.cached_input_tokens if usage else None, 50)
            self.assertEqual(usage.output_tokens if usage else None, 25)
            self.assertEqual(usage.total_tokens if usage else None, 225)

    def test_codex_adapter_honors_diagnostic_sandbox_and_mandatory_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            output_path = project_root / 'diagnosis.json'
            write_text(output_path, '{}\n')
            adapter = CodexAdapter(ProviderConfig(timeout_seconds=1800))
            request = AgentRequest(stage='self_repair_investigator', effort='max', prompt='inspect only', cwd=project_root, output_path=output_path, sandbox_mode='read-only')
            with patch('auto_agents.adapters.codex.run_subprocess_with_optional_streaming') as run_mock:
                run_mock.return_value = ('', '', 0, False, False)
                adapter.run(request)
            command = run_mock.call_args.args[0]
            sandbox_index = command.index('--sandbox')
            self.assertEqual(command[sandbox_index + 1], 'read-only')
            self.assertNotIn('timeout', run_mock.call_args.kwargs)
            self.assertIsNotNone(run_mock.call_args.kwargs['smart_timeout'])

    def test_copilot_cli_adapter_builds_command_with_profile_config_dir(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        config = ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={'balanced': 'balanced', 'deep': 'deep', 'max': 'max'})
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(stage='implement', effort='deep', prompt='do something', cwd=Path('/tmp/test'), output_path=Path('/tmp/test/out.md'))
        with patch.object(adapter, 'environment', return_value={'HOME': '/tmp/copilot-profile-test'}):
            cmd = adapter._build_command(request)
        self.assertEqual(cmd[0], 'copilot')
        self.assertIn('--config-dir', cmd)
        config_dir_index = cmd.index('--config-dir')
        resolved = cmd[config_dir_index + 1]
        self.assertEqual(resolved, '/tmp/copilot-profile-test/.copilot/profiles/deep')
        self.assertIn('--allow-all', cmd)
        self.assertIn('--no-ask-user', cmd)
        self.assertIn('--no-color', cmd)
        self.assertIn('-s', cmd)

    def test_provider_image_attachment_capabilities_fail_closed(self) -> None:
        from auto_agents.adapters.antigravity import AntigravityAdapter
        from auto_agents.adapters.mock import MockAdapter
        from auto_agents.adapters.shell import ShellAdapter
        self.assertTrue(CodexAdapter(ProviderConfig(kind='codex')).supports_image_attachments())
        self.assertFalse(AntigravityAdapter(ProviderConfig(kind='antigravity', binary='agy')).supports_image_attachments())
        self.assertFalse(ShellAdapter(ProviderConfig(kind='shell', binary='provider-wrapper')).supports_image_attachments())
        self.assertFalse(MockAdapter().supports_image_attachments())

    def test_copilot_cli_adapter_detects_native_attachment_support(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter, _copilot_cli_supports_image_attachments
        config = ProviderConfig(kind='copilot-cli', binary='copilot-test')
        adapter = CopilotCliAdapter(config)
        _copilot_cli_supports_image_attachments.cache_clear()
        try:
            with patch('auto_agents.adapters.copilot_cli.shutil.which', return_value='/tmp/copilot-test'), patch('auto_agents.adapters.copilot_cli.subprocess.run', return_value=subprocess.CompletedProcess(['/tmp/copilot-test', '--help'], 0, stdout='  --attachment <path>  Attach an image\n', stderr='')):
                self.assertTrue(adapter.supports_image_attachments())
        finally:
            _copilot_cli_supports_image_attachments.cache_clear()

    def test_copilot_cli_adapter_rejects_cli_without_attachment_flag(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter, _copilot_cli_supports_image_attachments
        config = ProviderConfig(kind='copilot-cli', binary='copilot-old')
        adapter = CopilotCliAdapter(config)
        _copilot_cli_supports_image_attachments.cache_clear()
        try:
            with patch('auto_agents.adapters.copilot_cli.shutil.which', return_value='/tmp/copilot-old'), patch('auto_agents.adapters.copilot_cli.subprocess.run', return_value=subprocess.CompletedProcess(['/tmp/copilot-old', '--help'], 0, stdout='Usage: copilot-old [options]\n', stderr='')):
                self.assertFalse(adapter.supports_image_attachments())
        finally:
            _copilot_cli_supports_image_attachments.cache_clear()

    def test_copilot_cli_adapter_attaches_images_with_noninteractive_prompt(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            attachments = [project_root / 'prototype.png', project_root / 'actual.png']
            adapter = CopilotCliAdapter(ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={}, prompt_via_stdin=True))
            request = AgentRequest(stage='visual_judge', effort='balanced', prompt='compare the screenshots', cwd=project_root, output_path=project_root / 'judge.md', attachments=attachments)
            with patch('auto_agents.adapters.copilot_cli.run_subprocess_with_optional_streaming', return_value=('', '', 0, False, False)) as run_mock:
                result = adapter.run(request)
            self.assertTrue(result.ok)
            command = run_mock.call_args.args[0]
            attachment_paths = [command[index + 1] for index, value in enumerate(command[:-1]) if value == '--attachment']
            self.assertEqual(attachment_paths, [str(path) for path in attachments])
            self.assertEqual(command[-2:], ['-p', 'compare the screenshots'])
            self.assertEqual(run_mock.call_args.kwargs['stdin_input'], '')

    def test_copilot_cli_adapter_keeps_stdin_for_text_only_request(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            adapter = CopilotCliAdapter(ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={}, prompt_via_stdin=True))
            request = AgentRequest(stage='review', effort='balanced', prompt='review this', cwd=project_root, output_path=project_root / 'review.md')
            with patch('auto_agents.adapters.copilot_cli.run_subprocess_with_optional_streaming', return_value=('', '', 0, False, False)) as run_mock:
                adapter.run(request)
            command = run_mock.call_args.args[0]
            self.assertNotIn('--attachment', command)
            self.assertNotIn('-p', command)
            self.assertEqual(run_mock.call_args.kwargs['stdin_input'], 'review this')

    def test_copilot_cli_adapter_forwards_model_from_profile_config(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / 'deep-profile'
            profile_dir.mkdir(parents=True, exist_ok=True)
            write_text(profile_dir / 'config.json', '{"model": "gpt-4.1"}\n')
            config = ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={'deep': str(profile_dir)})
            adapter = CopilotCliAdapter(config)
            request = AgentRequest(stage='implement', effort='deep', prompt='do something', cwd=Path('/tmp/test'), output_path=Path('/tmp/test/out.md'))
            cmd = adapter._build_command(request)
            self.assertIn('--model', cmd)
            model_index = cmd.index('--model')
            self.assertEqual(cmd[model_index + 1], 'gpt-4.1')

    def test_copilot_cli_adapter_skips_allow_all_when_explicit(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        config = ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={'balanced': 'balanced'}, extra_args=['--deny-tool', 'dangerous-tool'])
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(stage='implement', effort='balanced', prompt='do something', cwd=Path('/tmp/test'), output_path=Path('/tmp/test/out.md'))
        cmd = adapter._build_command(request)
        self.assertNotIn('--allow-all', cmd)
        self.assertIn('--deny-tool', cmd)

    def test_copilot_cli_adapter_model_label_uses_profile(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        config = ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={'deep': 'unit-test-profile-without-config'})
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(stage='implement', effort='deep', prompt='do something', cwd=Path('/tmp/test'), output_path=Path('/tmp/test/out.md'))
        self.assertEqual(adapter._model_label(request), 'profile:unit-test-profile-without-config')

    def test_copilot_cli_adapter_model_label_explicit_model(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        config = ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={'deep': 'deep'}, extra_args=['--model', 'gpt-4o'])
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(stage='implement', effort='deep', prompt='do something', cwd=Path('/tmp/test'), output_path=Path('/tmp/test/out.md'))
        self.assertEqual(adapter._model_label(request), 'gpt-4o')

    def test_copilot_cli_adapter_no_config_dir_for_unmapped_effort(self) -> None:
        from auto_agents.adapters.copilot_cli import CopilotCliAdapter
        config = ProviderConfig(kind='copilot-cli', binary='copilot', profile_map={})
        adapter = CopilotCliAdapter(config)
        request = AgentRequest(stage='implement', effort='balanced', prompt='do something', cwd=Path('/tmp/test'), output_path=Path('/tmp/test/out.md'))
        cmd = adapter._build_command(request)
        self.assertNotIn('--config-dir', cmd)
if __name__ == '__main__':
    unittest.main()
