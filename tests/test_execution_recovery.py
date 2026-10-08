import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.execution_recovery import BASELINE_FAILURE_IDENTITY_INCIDENT_KIND, BASELINE_FAILURE_IDENTITY_SNAPSHOT_KEY, ExecutionIncident, ExecutionIncidentStore, IncidentDiagnosis, ParallelLaneFailure, command_incident, deterministic_diagnosis, parse_incident_diagnosis, provider_incident, recovery_task_marker
from auto_agents.models import AgentRequest, AgentResult, AgentTermination, CommandResult, GateResult, RunState, TaskSpec, VerificationStep
from auto_agents.gates import GateCommandBaselineIdentityError, GateCommandInfrastructureError, GateCommandTimeoutError, build_failure_identity_diagnostic_command, classify_reported_infrastructure_failure
from auto_agents.infrastructure_repair import InfrastructureRepairResult
from auto_agents.git_ops import commit_all, commit_changed_paths, head_ref, worktree_fingerprint
from auto_agents.orchestrator import Orchestrator
from auto_agents.validation import validate_task_dependencies, validate_task_plan_payload
from auto_agents.config import load_run_state, load_task_plan, save_run_state, save_task_plan

class ExecutionRecoveryTests(unittest.TestCase):

    def test_parallel_lane_infrastructure_incident_round_trips(self) -> None:
        payload = ParallelLaneFailure(task={'task_id': 'lane-a', 'status': 'blocked'}, operation='verification', owner='verification_infrastructure', automatic_retryable=False, resumable=True, reason='verification token=classified was unavailable', redacted_evidence='API_KEY=classified\nservice unavailable', current_failure_ids=['tests/test_boundary.py::test_service'], baseline_failure_ids=['tests/test_boundary.py::test_service'], new_failure_ids=[], owned_failure_ids=['tests/test_boundary.py::test_service'], failure_class='baseline_only_owned', baseline_comparison_comparable=True, base_ref='abc123', checkpoint={'status': 'recoverable', 'resume_mode': 'gate_recheck'}, command_incident={'context': 'parallel verification', 'baseline': False}, implementation_completed=True).to_dict()
        restored = ParallelLaneFailure.from_dict(payload)
        round_tripped = restored.to_dict()
        self.assertEqual(round_tripped['schema_version'], 1)
        self.assertEqual(round_tripped['kind'], 'parallel_lane_failure')
        self.assertEqual(round_tripped['new_failure_ids'], [])
        self.assertEqual(round_tripped['current_failure_ids'], ['tests/test_boundary.py::test_service'])
        self.assertEqual(round_tripped['checkpoint']['resume_mode'], 'gate_recheck')
        self.assertTrue(round_tripped['baseline_comparison_comparable'])
        self.assertTrue(round_tripped['implementation_completed'])
        self.assertNotIn('classified', round_tripped['reason'])
        self.assertNotIn('classified', round_tripped['redacted_evidence'])

    def test_command_incident_redacts_secrets_and_is_stable(self) -> None:
        result = CommandResult(command='pytest -q --token=abc', ok=False, returncode=124, stderr='API_KEY=secret-value', termination_reason='stalled', timeout_seconds=7200, last_activity_seconds=42, activity_kind='output', process_snapshot={'pgid': 12})
        incident = command_incident(run_id='run-1', stage='implement', context='baseline', result=result, baseline=True)
        self.assertEqual(incident.kind, 'gate_stall')
        self.assertNotIn('secret-value', incident.stderr_tail)
        self.assertEqual(incident.process_snapshot['pgid'], 12)

    def test_command_incident_dynamic_output_changes_evidence_not_identity(self) -> None:
        first = command_incident(run_id='run-1', stage='implement', context='baseline', result=CommandResult(command='npm exec -- vitest run browser.test.ts', ok=False, returncode=125, stderr='worker memory capacity remained unavailable for 30.0s: 4597 MiB available, 6144 MiB required', termination_reason='remote_lane_state_lost'), baseline=True, head_ref='head-1')
        second = command_incident(run_id='run-1', stage='implement', context='baseline', result=CommandResult(command='npm exec -- vitest run browser.test.ts', ok=False, returncode=125, stderr='worker memory capacity remained unavailable for 30.0s: 4590 MiB available, 6144 MiB required', termination_reason='remote_lane_state_lost'), baseline=True, head_ref='head-1')
        self.assertEqual(first.incident_fingerprint, second.incident_fingerprint)
        self.assertNotEqual(first.evidence_fingerprint, second.evidence_fingerprint)

    def test_gate_root_cause_identity_ignores_volatile_task_context(self) -> None:
        result = CommandResult(command='python -m pytest -q tests/test_owned.py::test_contract', ok=False, returncode=4, stderr='ERROR: not found: tests/test_owned.py::test_contract', process_snapshot={'baseline_failure_identity': {'status': 'unresolved', 'contract': 'stable_test_failure_ids'}})
        first = command_incident(run_id='run-1', stage='implement', context='lazy task baseline verification (repair-task-r1)', result=result, baseline=True)
        second = command_incident(run_id='run-1', stage='implement', context='lazy task baseline verification (repair-task-r2)', result=result, baseline=True)
        self.assertNotEqual(first.incident_fingerprint, second.incident_fingerprint)
        self.assertEqual(first.root_cause_fingerprint, second.root_cause_fingerprint)

    def test_reported_infrastructure_incident_preserves_worker_evidence(self) -> None:
        incident = command_incident(run_id='run-1', stage='implement', context='task verification', result=CommandResult(command='npm test', ok=False, returncode=1, infrastructure_error=True, infrastructure_failure_id='browser_launch_failed', infrastructure_attempts=[{'worker_id': 'worker-1', 'returncode': 1}, {'worker_id': 'worker-2', 'returncode': 1}]))
        self.assertEqual(incident.kind, 'gate_reported_infrastructure_error')
        self.assertEqual(len(incident.process_snapshot['infrastructure_attempts']), 2)

    def test_provider_incident_dynamic_output_changes_evidence_not_identity(self) -> None:
        first = provider_incident(run_id='run-1', stage='implement', provider='codex', result=AgentResult(ok=False, command=['codex'], output_path=Path('first.md'), stderr='provider request req-123 failed after 5.1s', returncode=1, termination=AgentTermination(reason='provider_error', elapsed_seconds=5.1)), head_ref='head-1')
        second = provider_incident(run_id='run-1', stage='implement', provider='codex', result=AgentResult(ok=False, command=['codex'], output_path=Path('second.md'), stderr='provider request req-456 failed after 6.2s', returncode=1, termination=AgentTermination(reason='provider_error', elapsed_seconds=6.2)), head_ref='head-1')
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.incident_fingerprint, second.incident_fingerprint)
        self.assertNotEqual(first.evidence_fingerprint, second.evidence_fingerprint)

    def test_deterministic_routes_watch_mode_to_plan(self) -> None:
        incident = ExecutionIncident(incident_id='i1', run_id='r1', source='gate', kind='gate_stall', stage='implement', context='baseline', command='npm exec vitest watch', termination_reason='stalled', baseline=True)
        diagnosis = deterministic_diagnosis(incident)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis.owner, 'verification_contract')
        self.assertEqual(diagnosis.action, 'REWIND_PLAN')

    def test_cleanup_uncertainty_stops_automatic_recovery(self) -> None:
        incident = ExecutionIncident(incident_id='i2', run_id='r1', source='gate', kind='gate_timeout', stage='implement', context='baseline', termination_reason='timeout', cleanup_incomplete=True)
        diagnosis = deterministic_diagnosis(incident)
        self.assertEqual(diagnosis.action, 'STOP')
        self.assertEqual(diagnosis.confidence, 1.0)

    def test_worker_allocation_failure_has_deterministic_actionable_diagnosis(self) -> None:
        message = 'Verification could not start: no eligible worker can run this command.\nRequired worker: 2 slot(s); capabilities: docker, ffmpeg.\nSuggested actions:\n- Start the Docker daemon.'
        incident = ExecutionIncident(incident_id='worker-pool-1', run_id='run-1', source='gate', kind='gate_infrastructure_error', stage='implement', context='task verification', command='pytest tests/integration', stderr_tail=message, process_snapshot={'worker_allocation': {'status': 'no_eligible_worker', 'user_message': message, 'workers': [{'worker_id': 'local-worker', 'status': 'ineligible', 'reasons': ['missing capabilities: docker']}]}})
        diagnosis = deterministic_diagnosis(incident)
        self.assertIsNotNone(diagnosis)
        assert diagnosis is not None
        self.assertEqual(diagnosis.owner, 'verification_infrastructure')
        self.assertEqual(diagnosis.action, 'REPAIR_INFRASTRUCTURE')
        self.assertEqual(diagnosis.cause_status, 'confirmed')
        self.assertEqual(diagnosis.failure_domain, 'worker_pool')
        self.assertIn('Required worker: 2 slot(s)', diagnosis.reason)
        self.assertIn('missing capabilities: docker', diagnosis.evidence[0])

    def test_store_persists_incident_and_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = RunState(run_id='run-1')
            incident = ExecutionIncident(incident_id='i3', run_id='run-1', source='provider', kind='provider_tool_stalled', stage='design', context='provider:codex')
            store = ExecutionIncidentStore(root, state.run_id)
            store.save(incident, state)
            self.assertEqual(state.active_execution_incident_id, 'i3')
            self.assertEqual(store.load('i3').kind, 'provider_tool_stalled')
            incident.status = 'resolved'
            store.save(incident, state)
            self.assertEqual(state.active_execution_incident_id, '')

    def test_run_state_round_trips_incident_budget_and_blocker(self) -> None:
        state = RunState(run_id='run-1', execution_incident_budget_epoch=3, execution_incident_budget_checkpoint={'epoch': 3, 'reason': 'baseline passed'}, active_blocker={'owner': 'auto_agents', 'category': 'gate_timeout'})
        restored = RunState.from_dict(state.to_dict())
        self.assertEqual(restored.execution_incident_budget_epoch, 3)
        self.assertEqual(restored.execution_incident_budget_checkpoint['reason'], 'baseline passed')
        self.assertEqual(restored.active_blocker['owner'], 'auto_agents')

    def test_agent_diagnosis_requires_strict_bounded_json(self) -> None:
        diagnosis = parse_incident_diagnosis('{"owner":"target_project","action":"RECOVER_TARGET","confidence":0.91,"reason":"deadlock","evidence":["two workers"]}')
        self.assertEqual(diagnosis.action, 'RECOVER_TARGET')
        with self.assertRaises(ValueError):
            parse_incident_diagnosis('{"owner":"target_project","action":"DELETE_TESTS","confidence":1,"reason":"fast","evidence":[]}')

    def test_agent_diagnosis_normalizes_nested_reason_and_evidence(self) -> None:
        diagnosis = parse_incident_diagnosis('{"owner":"verification_infrastructure","action":"REPAIR_INFRASTRUCTURE","confidence":0.98,"reason":{"cause_status":"confirmed","causation":"No eligible worker has Docker."},"evidence":{"observed":["command did not start"],"inferred":["worker pool needs repair"]}}')
        self.assertEqual(diagnosis.reason, 'No eligible worker has Docker.')
        self.assertEqual(diagnosis.cause_status, 'confirmed')
        self.assertEqual(diagnosis.evidence, ['observed: command did not start', 'inferred: worker pool needs repair'])

    def _current_selector_reconciliation_case(self, root: Path, *, run_id: str='', source_task_id: str='selector-source', incident_id: str='selector-incident') -> dict:
        Orchestrator.init_project(root, 'project', 'mock')
        test_path = root / 'tests' / 'test_selector_contract.py'
        owner_path = root / 'owner.py'
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text('import unittest\n', encoding='utf-8')
        owner_path.write_text("VALUE = 'base'\n", encoding='utf-8')
        commit_all(root, 'test: add selector reconciliation baseline')
        test_name = 'test_public_projection'
        selector_path = test_path.relative_to(root).as_posix()
        selector = f'{selector_path}::{test_name}'
        command = f'{Path(sys.executable).as_posix()} -m pytest -q -s {selector}'
        retained_body = f"class ContractTests(unittest.TestCase):\n    def {test_name}(self):\n        print('RETAINED_ASSERTION_BODY_EXECUTED')\n        self.assertEqual({{'state': 'recovering'}}['state'], 'recovering')\n"
        test_path.write_text('import unittest\n\n' + retained_body, encoding='utf-8')
        owner_path.write_text("VALUE = 'retained'\n", encoding='utf-8')
        subprocess.run(['git', 'add', '--', selector_path, 'owner.py'], cwd=root, check=True, capture_output=True)
        orchestrator = Orchestrator(root)
        orchestrator.config.gates.steps = []
        orchestrator.config.gates.distributed.mode = 'off'
        orchestrator.config.gates.adaptive_timeout_enabled = False
        orchestrator.config.gates.command_timeout_seconds = 30
        orchestrator.config.gates.command_idle_timeout_seconds = 30
        state = load_run_state(root)
        if run_id:
            state.run_id = run_id
        source = TaskSpec(task_id=source_task_id, title='Retain the source candidate', description='Own the complete class-scoped selector proof.', acceptance=['The exact selector resolves.'], status='in_progress', verification_refs=[selector])
        state.tasks = [source]
        orchestrator._persist_tasks(state.tasks)
        orchestrator._set_implementation_ready_marker(state, source, True)
        self.assertIn(source.task_id, state.resume_context['retained_worktree_ownership'])
        incident = ExecutionIncident(incident_id=incident_id, run_id=state.run_id, source='gate', kind=CURRENT_VERIFICATION_CONTRACT_INCIDENT_KIND, stage='implement', context='task verification', command=command, origin_command=command, task_id=source.task_id, baseline=False, recovery_round=1, status='recovering', evidence_fingerprint='selector-evidence', head_ref=head_ref(root), worktree_fingerprint=worktree_fingerprint(root), process_snapshot={CURRENT_VERIFICATION_CONTRACT_SNAPSHOT_KEY: {'status': 'target_not_found', 'contract': 'exact_pytest_target', 'repair_scope': 'verification_contract'}})
        orchestrator._merge_or_save_execution_incident(state, incident)
        orchestrator._schedule_prebaseline_recovery_task(state, incident)
        tasks = orchestrator._load_tasks_from_plan()
        recovery = next((task for task in tasks if task.task_origin == 'stage_recovery'))
        recovery.status = 'in_progress'
        state.tasks = tasks
        orchestrator._persist_tasks(tasks)
        wrapper = f"\n\ndef {test_name}():\n    ContractTests('{test_name}').{test_name}()\n"
        with test_path.open('a', encoding='utf-8') as handle:
            handle.write(wrapper)
        return {'orchestrator': orchestrator, 'state': state, 'tasks': tasks, 'source': source, 'recovery': recovery, 'test_path': test_path, 'owner_path': owner_path, 'selector_path': selector_path, 'command': command, 'retained_body': retained_body}

    def _record_selector_recovery_gate_evidence(self, case: dict) -> dict:
        orchestrator = case['orchestrator']
        state = case['state']
        recovery = case['recovery']
        orchestrator._record_verify_result(recovery, 1, 'pass', 'focused selector passed')
        state.task_review_cache[recovery.task_id] = {'fingerprint': worktree_fingerprint(orchestrator.project_root), 'prompt_policy_hash': orchestrator._review_prompt_policy_hash(), 'decision': 'pass', 'summary': 'reviewed selector correction'}
        return {'ok': True, 'review': 'reviewed selector correction', 'verify_current_failure_ids': []}

    def _legacy_selector_resume_case(self, root: Path, *, representative_ids: bool=False) -> dict:
        case = self._current_selector_reconciliation_case(root, run_id='f6cee14fdf8e' if representative_ids else '', source_task_id='task-454' if representative_ids else 'selector-source', incident_id='77588c034a2e' if representative_ids else 'selector-incident')
        orchestrator = case['orchestrator']
        state = case['state']
        recovery = case['recovery']
        marker = orchestrator._execution_recovery_marker(recovery)
        marker.pop('selector_owner_transfer')
        marker.pop('mutable_paths')
        marker['worktree_handoff'].pop('immutable_borrowed_paths')
        recovery.mutable_artifacts = []
        recovery.scope_boundaries = ''
        recovery.status = 'blocked'
        self._record_selector_recovery_gate_evidence(case)
        state.task_review_cache[recovery.task_id].pop('prompt_policy_hash')
        owner = state.resume_context['retained_worktree_ownership'][case['source'].task_id]
        owner.pop('index_fingerprints', None)
        mismatch = orchestrator._execution_recovery_borrowed_worktree_mismatch(recovery)
        marker['borrowed_worktree_validation'] = {'status': 'blocked', **mismatch, 'updated_at': 'legacy-checkpoint'}
        state.tasks = case['tasks']
        orchestrator._persist_tasks(case['tasks'])
        orchestrator._block_run(state, owner='auto_agents', category='execution_recovery_borrowed_worktree_mutation', reason='borrowed selector changed before recovery completion')
        state.active_blocker['execution_recovery_borrowed_worktree'] = dict(mismatch)
        state.status = 'blocked'
        state.active_blocker['status'] = 'blocked'
        save_run_state(root, state)
        case['incident'] = ExecutionIncidentStore(root, state.run_id).load('77588c034a2e' if representative_ids else 'selector-incident')
        case['mismatch'] = mismatch
        return case
if __name__ == '__main__':
    unittest.main()
