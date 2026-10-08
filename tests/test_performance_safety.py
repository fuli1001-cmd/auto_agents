import json
import tempfile
import threading
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from auto_agents.gate_baseline_cache import GateBaselineCache
from auto_agents.gates import gate_plan_from_verification_steps, run_gate_plan
from auto_agents.io_utils import write_json, write_text
from auto_agents.models import AgentResult, CommandResult, GateParallelGroup, GateResult, PersistenceTargetConfig, RunState, TaskSpec, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.provider_limits import ParallelTuningStore
from auto_agents.requirements import requirements_audit_context_sha256, run_requirements_audit

def _requirement(pattern: str) -> dict:
    return {'id': 'REQ-001', 'text': 'Remove the legacy behavior.', 'source': 'spec.md', 'status': 'active', 'priority': 'mandatory', 'acceptance_oracles': ['The legacy behavior is absent.'], 'oracle_type': 'deterministic_test', 'oracle_strength': 'behavioral', 'evidence_boundary': 'internal_state', 'forbidden_proxy_oracles': [], 'forbidden_patterns': [pattern], 'external_docs_required': False, 'provider_reference': '', 'notes': ''}

class RequirementsAuditPerformanceTests(unittest.TestCase):

    def _project(self, root: Path, pattern: str) -> TaskSpec:
        from auto_agents.models import ProjectConfig
        from auto_agents.config import save_project_config
        root.mkdir(parents=True, exist_ok=True)
        save_project_config(root, ProjectConfig('demo'))
        write_json(root / '.auto-agents' / 'state' / 'requirements_trace.json', {'version': 1, 'requirements': [_requirement(pattern)]})
        return TaskSpec(task_id='task-001', title='Done', description='Done', acceptance=['done'], requirement_ids=['REQ-001'], status='done')

    def test_dangerous_pattern_fails_closed_without_running_matcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            task = self._project(root, '(?s)for\\s+.*check.*(?:retry|attempt).*for\\s+.*(?:all_checks|checks)')
            write_text(root / 'large.ts', 'for check ' + 'x' * 500000)
            started = time.monotonic()
            result = run_requirements_audit(root, [task])
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertFalse(result['ok'])
            blockers = result['issues'][0]['blockers']
            self.assertTrue(any((item['kind'] == 'forbidden_pattern_safety' for item in blockers)))
            self.assertEqual(result['metrics']['matcher_calls'], 0)

    def test_incremental_cache_rescans_only_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            task = self._project(root, 'legacy_gateway')
            write_text(root / 'one.py', "print('ok')\n")
            write_text(root / 'two.py', "print('ok')\n")
            first = run_requirements_audit(root, [task])
            second = run_requirements_audit(root, [task])
            write_text(root / 'one.py', "print('changed')\n")
            third = run_requirements_audit(root, [task])
            self.assertGreater(first['metrics']['matcher_calls'], 0)
            self.assertEqual(second['metrics']['matcher_calls'], 0)
            self.assertEqual(third['metrics']['matcher_calls'], 1)
            self.assertGreater(third['metrics']['cache_hits'], 0)

    def test_audit_context_ignores_runtime_commit_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'demo'
            task = self._project(root, 'legacy_gateway')
            before = requirements_audit_context_sha256(root, [task])
            task.commit_sha = 'a' * 40
            after = requirements_audit_context_sha256(root, [task])
            self.assertEqual(before, after)

class ParallelTuningTests(unittest.TestCase):

    def test_worker_one_recovers_with_two_worker_canary_after_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = [1000]
            store = ParallelTuningStore(Path(tmp), time_fn=lambda: now[0])
            store.put_workers('new', 1, event='hard_pressure')
            now[0] = 4599
            active = store.resolve_workers('new', initial_workers=3, cooldown_seconds=3600)
            now[0] = 4600
            canary = store.resolve_workers('new', initial_workers=3, cooldown_seconds=3600)
            self.assertEqual(active['workers'], 1)
            self.assertTrue(active['cooldown_active'])
            self.assertEqual(canary['workers'], 2)
            self.assertEqual(canary['event'], 'canary')

    def test_legacy_tuning_key_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ParallelTuningStore(Path(tmp), time_fn=lambda: 10)
            store.put_workers('legacy', 3, event='success')
            result = store.resolve_workers('new', initial_workers=2, cooldown_seconds=3600, legacy_keys=['legacy'])
            self.assertEqual(result['workers'], 3)
            self.assertEqual(result['source_key'], 'legacy')

class GateOptimizationTests(unittest.TestCase):

    def test_only_explicitly_safe_steps_are_grouped(self) -> None:
        steps = [VerificationStep(runner='pytest', targets=['tests/a.py']), VerificationStep(runner='pytest', targets=['tests/b.py'], parallel_safe=True), VerificationStep(runner='pytest', targets=['tests/c.py'], parallel_safe=True)]
        commands, groups = gate_plan_from_verification_steps(steps, Path('/tmp/demo'))
        self.assertEqual(len(commands), 1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].commands), 2)

    def test_parallel_gate_respects_worker_cap_and_preserves_order(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        def fake_run(command: str, cwd: Path, **_kwargs) -> CommandResult:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return CommandResult(command=command, ok=True, returncode=0, stdout=command)
        with patch('auto_agents.gates._run_command', side_effect=fake_run):
            result = run_gate_plan([], [GateParallelGroup(name='safe', commands=['a', 'b', 'c', 'd'])], Path('/tmp'), collect_all=True, parallel_workers=2)
        self.assertEqual(peak, 2)
        self.assertEqual([item.stdout for item in result.commands], ['a', 'b', 'c', 'd'])

    def test_command_cache_runs_only_new_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = GateBaselineCache(Path(tmp), Path(tmp) / 'cache.sqlite3')
            first = CommandResult(command='check-a', ok=True, returncode=0)
            cache.put('head', ['check-a'], collect_all=True, failure_ids=[], command_results=[first])
            self.assertEqual(cache.missing_commands('head', ['check-a', 'check-b'], collect_all=True), ['check-b'])
            second = CommandResult(command='check-b', ok=False, returncode=1, stdout='FAILED tests/test_b.py::test_b')
            cache.put('head', ['check-a', 'check-b'], collect_all=True, failure_ids=['tests/test_b.py::test_b'], command_results=[second])
            self.assertEqual(cache.get('head', ['check-a', 'check-b'], collect_all=True), ['tests/test_b.py::test_b'])

    def test_command_cache_promotes_resume_baseline_and_leaves_new_command_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = GateBaselineCache(Path(tmp), Path(tmp) / 'cache.sqlite3')
            cache.put('old', ['check-a'], collect_all=True, failure_ids=[], command_results=[CommandResult(command='check-a', ok=True, returncode=0)])
            promoted = cache.promote('old', 'new', ['check-a', 'check-b'], collect_all=True)
            self.assertEqual(promoted, 1)
            self.assertEqual(cache.missing_commands('new', ['check-a', 'check-b'], collect_all=True), ['check-b'])

class EvidencePreflightTests(unittest.TestCase):

    def _artifact_publication_gap_task(self, root: Path) -> tuple[Orchestrator, TaskSpec, str]:
        from auto_agents.models import ProjectConfig
        from auto_agents.config import save_project_config
        root.mkdir(parents=True, exist_ok=True)
        save_project_config(root, ProjectConfig('demo'))
        producer_ref = 'tests/test_receipt.py::test_publishes_receipt'
        artifact_ref = '.tmp-tests/receipts/runs/*/receipt.json'
        failure_id = f'verification_contract:nonportable_ignored_evidence:{artifact_ref}'
        task = TaskSpec(task_id='task-receipt', title='Publish receipt', description='Publish an isolated verification receipt.', acceptance=['The receipt is portable.'], verification_refs=[producer_ref], requirement_proofs=[{'evidence_refs': [producer_ref, artifact_ref]}], verify_history=[{'attempt': 1, 'decision': 'fail', 'summary': 'ignored evidence was not published', 'failure_ids': [failure_id], 'comparable_failures': True}])
        write_json(root / '.auto-agents' / 'state' / 'task_plan.json', {'verification_steps': [{'kind': 'test', 'runner': 'pytest', 'targets': ['tests/test_receipt.py'], 'artifact_globs': ['.tmp-tests/receipts/runs/*/summary.json']}], 'tasks': [task.to_dict()]})
        return (Orchestrator(root), task, artifact_ref)

    def _provider_contract_task(self, root: Path, *, valid_reference: bool) -> tuple[Orchestrator, TaskSpec]:
        from auto_agents.models import ProjectConfig
        from auto_agents.config import save_project_config
        root.mkdir(parents=True, exist_ok=True)
        save_project_config(root, ProjectConfig('demo'))
        reference = '.auto-agents/docs/provider_references/image.md'
        requirement = _requirement('')
        requirement.update(external_docs_required=True, provider_reference=reference, forbidden_patterns=[])
        write_json(root / '.auto-agents' / 'state' / 'requirements_trace.json', {'version': 1, 'requirements': [requirement]})
        if valid_reference:
            headings = ('Status', 'Retrieved at', 'Official sources', 'Authentication', 'Request', 'Response', 'Prompt / Content Construction', 'Safety / Content Policy', 'Semantic Error Routing', 'Retry / Recovery Matrix', 'Contract Test Requirements', 'Unknowns / Ambiguities')
            markdown = '# Provider\n' + ''.join((f'\n## {heading}\n\nNot applicable: covered by this fixture.\n' for heading in headings))
        else:
            markdown = '# Broken provider reference\n'
        write_text(root / reference, markdown)
        write_json(root / '.auto-agents' / 'state' / 'provider_references.lock.json', {'version': 1, 'references': {'image': {'path': reference, 'status': 'verified', 'contract_version': 2, 'retrieved_at': '2026-08-19T00:00:00Z', 'source_urls': ['https://provider.example/docs'], 'notes': 'fixture'}}})
        return (Orchestrator(root), TaskSpec(task_id='task-provider', title='Provider contract', description='Consume the provider reference and add its contract test.', acceptance=['Provider contract is proven.'], requirement_ids=['REQ-001']))
if __name__ == '__main__':
    unittest.main()
