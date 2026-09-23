"""Fresh verification evidence, distinct from readiness and synthetic routing."""
import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_agents.config import load_run_state, load_task_plan, save_project_config, save_run_state
from auto_agents.models import RunState, TaskSpec
from auto_agents.orchestrator import Orchestrator
from auto_agents.repair_runtime_identity import observe_engine
from auto_agents.repair_v2.boundary_driver import (
    METADATA_CHECKPOINT_CATEGORY, metadata_continuation_complete,
    metadata_execution_reports_published, publish_metadata_execution_reports,
    observe_run_continuation, run_input_hashes,
)
from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
from test_metadata_schema_checkpoint_recovery import _project, _commit, _write, _git


@pytest.mark.parametrize('verification_passes', [True, False])
def test_saved_workflow_produces_real_managed_verification_receipt(tmp_path, verification_passes):
    root = tmp_path / 'project'
    orch = _project(root)
    refs = ['tests/test_candidate.py::test_contract', 'tests/test_candidate.py::test_second_contract']
    _write(root, 'spec.md', '# Synthetic independent iteration\nVerify the retained candidate.\n')
    _write(root, 'app.py', 'VALUE = 1\n')
    _write(root, 'tests/test_candidate.py',
           "import app\n"
           "from pathlib import Path\n"
           f"def test_contract():\n    assert app.VALUE == {2 if verification_passes else 1}\n"
           "def test_second_contract():\n    assert Path('spec.md').is_file()\n")
    _write(root, '.auto-agents/state/sessions/stopped/session_state.json', '{"status":"stopped"}\n')
    # Use the image interpreter through the project's declared environment
    # alias, as required by the real verification admission contract.
    (root / '.conda').symlink_to(Path(sys.prefix))
    with (root / '.gitignore').open('a') as stream:
        stream.write('\n.conda\n')
    command = './.conda/bin/python -B -m pytest -p no:cacheprovider -q ' + ' '.join(refs)
    orch.config.gates.commands = [command]
    orch.config.gates.steps = []
    orch.config.gates.verification_policy_version = 2
    orch.config.gates.isolation.enabled = True
    orch.config.gates.distributed.mode = 'off'
    orch.config.execution.parallel_tasks.enabled = False
    orch.config.execution.health_watch.enabled = False
    save_project_config(root, orch.config)
    owner = TaskSpec('task-pp-07', 'Verify retained implementation', 'Preserve the candidate and run both checks',
                     ['Both retained contracts pass'], status='in_progress', verify_retry_epoch=1,
                     verification_refs=refs, persistence_change={
                         'storage_transition': 'none', 'compatibility_policy': 'not_applicable'})
    state = RunState(run_id='82288622684f', workflow_version=2, current_stage='implement', tasks=[owner])
    workflow = WorkflowStore(root).create_root(WorkflowRef('run', state.run_id))
    state.resume_context.update(workflow_id=workflow.workflow_id, spec_file=str(root / 'spec.md'),
                                auto_approve=True, provider_kind='mock', parallel_sequential_retry_tasks=[owner.task_id])
    state.stage_summaries = {stage: 'Retained accepted evidence' for stage in ('clarify', 'prototype', 'design', 'plan', 'provider_research')}
    state.agent_attempts = {'plan': 3, 'implement-task-pp-07': 2}
    orch._persist_tasks(state.tasks)
    plan = load_task_plan(root)
    plan['verification_commands'] = [command]
    (root / '.auto-agents/state/task_plan.json').write_text(json.dumps(plan))
    base = _commit(root)
    orch = Orchestrator(root)
    _write(root, 'app.py', 'VALUE = 2\n')
    _write(root, '.auto-agents/state/retained-diagnostic.json', '{"quote":"DROP TABLE example"}\n')
    orch._set_task_attempt_base_ref(state, owner, base)
    orch._set_implementation_ready_marker(state, owner, True)
    state.task_failure_checkpoints[owner.task_id] = {
        'task_id': owner.task_id, 'status': 'unavailable', 'ref': '', 'has_candidate_changes': False,
        'verify_retry_epoch': 0, 'reason': 'ignored dependency rejection',
    }
    orch._block_run(state, owner='auto_agents', category=METADATA_CHECKPOINT_CATEGORY,
                    reason='metadata and checkpoint failure', fingerprint='synthetic-metadata-failure')
    save_run_state(root, state)
    original = copy.deepcopy(state)
    original_plan = load_task_plan(root)
    frozen = run_input_hashes(root, original)
    identity = orch._auto_agents_runtime_identity()
    runtime = observe_engine(Path(identity['repository_root']), expected_commit=identity['repository_head'])
    request = {'commit': runtime['commit'], 'invocation': {'run_id': original.run_id, 'workflow_id': workflow.workflow_id}}
    stopped = root / '.auto-agents/state/sessions/stopped/session_state.json'
    protected = {p: p.read_bytes() for p in (root / 'app.py', root / 'spec.md', stopped)}
    index_before = _git(root, 'ls-files', '--stage', '-z')
    with patch.object(orch, '_call_with_failover', side_effect=AssertionError('no provider during offline recovery')):
        prepared = orch.mark_self_repair_applied(runtime['commit'])
        assert orch._resume_blocked_run(prepared)
        # No admission, baseline, verifier, subprocess or workflow method is replaced.
        observed = observe_run_continuation(orch, original, original_plan, request, runtime, frozen)
    if not verification_passes:
        assert not observed['ok']
        receipt = observed['recovery_observation']['verification_receipt']
        assert receipt['commands']
        assert any(not command['ok'] for command in receipt['commands'])
        assert 'offline boundary verification' not in json.dumps(observed)
        assert {p: p.read_bytes() for p in protected} == protected
        assert not metadata_continuation_complete(observed, original.to_dict())
        return
    assert metadata_continuation_complete(observed, original.to_dict())
    assert observed['recovery_observation']['review_entered'] is True
    evidence = tmp_path / 'controller-proof'
    publish_metadata_execution_reports(root, evidence, observed)
    assert metadata_execution_reports_published(observed, evidence)
    receipt = observed['recovery_observation']['verification_receipt']
    assert receipt['verification_refs'] == refs
    assert all(row['job_id'] and row['proof_ref'] and not row['cached'] for row in receipt['commands'])
    assert set(refs).issubset({node for row in receipt['commands'] for node in row['executed_tests']})
    assert {p: p.read_bytes() for p in protected} == protected
    assert _git(root, 'ls-files', '--stage', '-z') == index_before
    resumed = load_run_state(root)
    assert resumed.agent_attempts == original.agent_attempts
    assert resumed.tasks[0].status == 'in_progress' and not resumed.tasks[0].commit_sha
    report_path = evidence / observed['recovery_observation']['execution_reports'][0]['published_path']
    report_path.write_text('{}')
    assert not metadata_execution_reports_published(observed, evidence)


def test_trusted_controller_rejects_metadata_clearance_only_report(tmp_path, monkeypatch):
    import threading
    from auto_agents.repair_v2 import docker
    from auto_agents.repair_v2.recovery import classify
    from auto_agents.repair_v2.store import atomic_json
    from auto_agents.repair_v2.workspace import source_identity
    from test_metadata_schema_checkpoint_recovery import _ready_metadata_repair_scene

    target, _, original, _ = _ready_metadata_repair_scene(tmp_path / 'scene')
    source = tmp_path / 'engine'
    source.mkdir()
    _git(source, 'init', '-q')
    _git(source, 'config', 'user.name', 'Test')
    _git(source, 'config', 'user.email', 'test@example.invalid')
    _write(source, 'boundary_driver.py', 'raise AssertionError("candidate harness must not run")\n')
    _commit(source)
    payload = {'project': str(target), 'invocation': {'run_id': original.run_id,
                'workflow_id': original.resume_context['workflow_id']}}
    calls = []

    def run(args, **kwargs):
        if args[:2] == ['docker', 'run']:
            calls.append(args)
            assert 'seccomp=unconfined' in args and '--privileged' not in args
            assert args[args.index('--network') + 1] == 'none'
            assert args[args.index('--cap-drop') + 1] == 'ALL'
            trusted = Path(docker.__file__).with_name('boundary_driver.py')
            assert f'type=bind,src={trusted},dst=/opt/repair/boundary_driver.py,readonly' in args
            output = Path(next(arg for arg in args if arg.endswith(',dst=/result')).split(',')[1][4:])
            atomic_json(output / 'boundary.json', {'ok': True, 'run_id': original.run_id,
                        'status': 'pending', 'same_blocker': False, 'remaining_blocked': []})
        return 0, ''

    monkeypatch.setattr(docker, 'run', run)
    verifier = docker.DockerVerifier(tmp_path / 'verifier', image='pinned')
    before = (target / '.auto-agents/state/run_state.json').read_bytes()
    result = verifier.boundary(source_identity(source), source, target, payload, threading.Event())
    assert len(calls) == 1
    assert not result['ok'] and result['proof_incomplete']
    failure = classify(result, payload)
    assert (failure['domain'], failure['owner'], failure['code']) == (
        'controller', 'controller', 'recovery_proof_incomplete')
    assert (target / '.auto-agents/state/run_state.json').read_bytes() == before


def test_fresh_recovery_recording_uses_managed_local_lane():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from auto_agents.distributed_gates import DistributedGatePlanExecutor
    from auto_agents.models import CommandResult

    executor = DistributedGatePlanExecutor.__new__(DistributedGatePlanExecutor)
    executor.environment_overrides = {}
    result = CommandResult(command='python -m pytest tests/test_a.py', ok=True, returncode=0)
    executor.local = SimpleNamespace(record_pytest_execution=True, run=Mock(return_value=result))
    executor._run_uncached = Mock(side_effect=AssertionError('worker protocol lacks recorder receipts'))
    assert executor.run(result.command, timeout_seconds=30, adaptive_timeout_enabled=False,
                        idle_timeout_seconds=10) is result
    executor.local.run.assert_called_once()
    executor._run_uncached.assert_not_called()


def _synthetic_complete_receipt():
    """Validator input only; never used as actual recovery evidence."""
    original = {'run_id': 'run', 'resume_context': {'workflow_id': 'wf', 'implementation_ready_tasks': {'task': True}},
                'tasks': [{'task_id': 'task', 'status': 'in_progress', 'verify_retry_epoch': 1,
                           'verification_refs': ['tests/test_a.py::test_one', 'tests/test_a.py::test_two']}]}
    identity = {'run_id': 'run', 'workflow_id': 'wf', 'task_id': 'task', 'engine_runtime': {'repository_head': 'engine'}}
    started = {**identity, 'verification_id': 'verify', 'candidate_fingerprint': 'candidate', 'started_at': 'start'}
    receipt = {**started, 'repair_commit': 'engine', 'verify_retry_epoch': 1,
               'verification_refs': original['tasks'][0]['verification_refs'], 'completed_at': 'end',
               'candidate_unchanged': True, 'ok': True, 'commands': [
                   {'ok': True, 'returncode': 0, 'cached': False, 'job_id': 'job', 'proof_ref': 'proof',
                    'artifacts': {'pytest-execution-0.json': 'a' * 64},
                    'executed_tests': original['tasks'][0]['verification_refs']} ]}
    events = [{'event_id': str(i), 'subject_id': 'run', 'type': kind, 'data': data}
              for i, (kind, data) in enumerate([
                  ('implementation.entered', identity), ('task.verification.entered', started),
                  ('task.verification.completed', receipt)])]
    review = {'event_id': '3', 'subject_id': 'run', 'type': 'task.started',
              'data': {'task_id': 'task', 'action': 'review'}}
    observed = {'ok': True, 'run_id': 'run', 'workflow_id': 'wf', 'engine_runtime': {'ok': True, 'commit': 'engine'},
                'recovery_observation': {**identity, 'ok': True, 'implementation_entered': True,
                    'verification_completed': True, 'retained_constraints': True, 'entry_event_ref': 'events.jsonl',
                    'event_order': ['0', '1', '2', '3'], 'implementation_entry': events[0], 'verification_entry': events[1],
                    'review_entered': True, 'review_entry': review,
                    'execution_reports': [{'path': 'pytest-execution-0.json', 'sha256': 'a' * 64,
                        'job_id': 'job', 'proof_ref': 'proof', 'passed': list(original['tasks'][0]['verification_refs'])}],
                    'verification_event': events[2], 'verification_receipt': receipt}}
    return observed, original


@pytest.mark.parametrize('defect', ['clearance', 'cached', 'no_tests', 'missing_selector', 'wrong_workflow',
                                  'wrong_task', 'wrong_engine', 'wrong_candidate', 'old_entry', 'no_job', 'failed',
                                  'no_report', 'wrong_report_hash', 'no_review', 'wrong_review_task', 'review_before_verify'])
def test_metadata_verifier_rejects_incomplete_recovery_receipts(defect):
    observed, original = _synthetic_complete_receipt()
    assert metadata_continuation_complete(observed, original)
    proof = observed['recovery_observation']
    receipt = proof['verification_receipt']
    if defect == 'clearance': observed = {'ok': True, 'run_id': 'run', 'status': 'pending', 'same_blocker': False}
    elif defect == 'cached': receipt['commands'][0]['cached'] = True
    elif defect == 'no_tests': receipt['commands'][0]['executed_tests'] = []
    elif defect == 'missing_selector': receipt['commands'][0]['executed_tests'] = ['tests/test_a.py::test_one']
    elif defect == 'wrong_workflow': observed['workflow_id'] = 'other'
    elif defect == 'wrong_task': receipt['task_id'] = 'other'
    elif defect == 'wrong_engine': receipt['repair_commit'] = 'other'
    elif defect == 'wrong_candidate': receipt['candidate_fingerprint'] = 'other'
    elif defect == 'old_entry': proof['event_order'] = ['1', '0', '2']
    elif defect == 'no_job': receipt['commands'][0]['job_id'] = ''
    elif defect == 'failed': receipt['commands'][0].update(ok=False, returncode=1)
    elif defect == 'no_report': proof['execution_reports'] = []
    elif defect == 'wrong_report_hash': proof['execution_reports'][0]['sha256'] = 'b' * 64
    elif defect == 'no_review': proof.pop('review_entry')
    elif defect == 'wrong_review_task': proof['review_entry']['data']['task_id'] = 'another-task'
    elif defect == 'review_before_verify': proof['event_order'] = ['0', '1', '3', '2']
    assert not metadata_continuation_complete(observed, original)


@pytest.mark.parametrize('layout', ['module', 'package', 'root_precedence', 'src_not_on_path'])
@pytest.mark.parametrize('import_mode', ['prepend', 'importlib'])
def test_instrumented_pytest_preserves_module_import_path(tmp_path, layout, import_mode):
    from auto_agents.verification_pytest import prepare_execution_receipts

    root = tmp_path / 'project'
    root.mkdir()
    if layout == 'package':
        _write(root, 'app/__init__.py', "VALUE = 'root'\n")
    elif layout != 'src_not_on_path':
        _write(root, 'app.py', "VALUE = 'root'\n")
    if layout in {'root_precedence', 'src_not_on_path'}:
        _write(root, 'src/app.py', "VALUE = 'src'\n")
    if layout == 'src_not_on_path':
        test = ("import pytest\n"
                "def test_import_contract():\n"
                "    with pytest.raises(ModuleNotFoundError):\n"
                "        import app\n")
    else:
        test = "import app\ndef test_import_contract():\n    assert app.VALUE == 'root'\n"
    _write(root, 'tests/test_app.py', test)
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': '', 'PYTEST_ADDOPTS': ''}
    command = shlex.join([sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-q',
                          '--import-mode=' + import_mode, '--rootdir=' + str(root),
                          '--confcutdir=' + str(root), '-c', '/dev/null', 'tests/test_app.py'])
    baseline = subprocess.run(command, cwd=root, env=environment, shell=True, capture_output=True, text=True)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    scratch = tmp_path / 'receipts'
    scratch.mkdir()
    instrumented, reports = prepare_execution_receipts(command, root, scratch, environment)
    observed = subprocess.run(instrumented, cwd=root, env=environment, shell=True, capture_output=True, text=True)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report['passed'] == ['tests/test_app.py::test_import_contract']
    assert report['failures'] == []


def test_retained_workflow_clearance_is_not_fresh_verification():
    # The supplied controller counterexample, not a fabricated successful replay.
    original = {
        'run_id': '82288622684f',
        'resume_context': {'workflow_id': 'wf-cea9506a7499',
                           'implementation_ready_tasks': {'task-pp-07': True}},
        'tasks': [{'task_id': 'task-pp-07', 'status': 'in_progress', 'verify_retry_epoch': 1,
                   'verification_refs': [
                       'tests/test_text_protocol_boundary_api.py::TextProtocolBoundaryTests::test_req_284_planning_progress_wire_is_positive_typed_and_deduplicated',
                       'tests/test_text_protocol_boundary_api.py::TextProtocolBoundaryTests::test_req_284_planning_safety_body_precedes_http_without_unchanged_retry',
                   ]}],
    }
    report = {'ok': True, 'run_id': '82288622684f', 'status': 'pending',
              'same_blocker': False, 'remaining_blocked': []}
    assert not metadata_continuation_complete(report, original)
