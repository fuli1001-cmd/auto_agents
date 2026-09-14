#!/usr/bin/env python3
"""Opt-in native-provider pilot on a disposable alias-reuse repair component.

Uses real planner, writer, independent review, probes, quick checks and complete
component acceptance. It never resumes a user project or publishes an engine.
The original project's configuration is read only to retain provider/effort settings.
"""
import argparse
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

BROKEN = '''from hashlib import sha256
from pathlib import Path

def reusable(path, registered, expected):
    path = Path(path)
    if path.is_symlink():
        return False
    return sha256(path.read_bytes()).hexdigest() == expected
'''

TESTS = '''from hashlib import sha256
import pytest
from source import reusable

@pytest.fixture
def aliases(tmp_path):
    target = tmp_path / "dependency"
    target.write_bytes(b"original")
    link = tmp_path / "registered"
    link.symlink_to(target)
    return target, link, {str(link): str(target)}, sha256(b"original").hexdigest()

def test_registered_alias_reuses(aliases):
    target, link, registered, expected = aliases
    assert reusable(link, registered, expected)

def test_unregistered_alias_denied(aliases):
    target, link, registered, expected = aliases
    assert not reusable(link, {}, expected)
    assert reusable(target, {}, expected)

def test_changed_real_input_invalidates(aliases):
    target, link, registered, expected = aliases
    target.write_bytes(b"changed")
    assert not reusable(link, registered, expected)
    target.write_bytes(b"original")
    assert reusable(link, registered, expected)

def test_retargeted_alias_denied_and_recovery(aliases):
    target, link, registered, expected = aliases
    other = target.with_name("other")
    other.write_bytes(b"original")
    link.unlink()
    link.symlink_to(other)
    assert not reusable(link, registered, expected)
    link.unlink()
    link.symlink_to(target)
    assert reusable(link, registered, expected)
'''


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def run(output, configuration_project, provider, proposal=None):
    from auto_agents.adapters.codex import CodexAdapter
    from auto_agents.config import load_project_config
    from auto_agents.models import ProjectConfig
    from auto_agents.repair_actions import prepare_action
    from auto_agents.repair_planning import history_report
    from auto_agents.self_repair import AutoAgentsSelfRepairRunner, SelfRepairDecision
    from auto_agents.self_repair_search import SelfRepairExperiment, SelfRepairExperimentStore, SelfRepairCandidateRecord

    output.mkdir(parents=True, exist_ok=False)
    for name in ('AUTO_AGENTS_STORAGE_ROOT', 'AUTO_AGENTS_VERIFICATION_ROOT', 'AUTO_AGENTS_WORKER_ROOT', 'AUTO_AGENTS_CLUSTER_HOME'):
        os.environ[name] = str(output / name.lower())
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    engine, target = output / 'engine', output / 'target'
    for root in (engine, target):
        root.mkdir()
        git(root, 'init', '-q')
        git(root, 'config', 'user.name', 'self-repair-pilot')
        git(root, 'config', 'user.email', 'pilot@example.invalid')
        (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n.auto-agents/\n.auto-agents-gate-runtime/\n')
    (target / 'sentinel.txt').write_text('preserve original target\n')
    (engine / 'source.py').write_text(BROKEN)
    (engine / 'conftest.py').write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n')
    (engine / 'tests').mkdir()
    (engine / 'tests/test_alias_reuse.py').write_text(TESTS)
    for root in (engine, target):
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'freeze isolated pilot baseline')
    selected = load_project_config(configuration_project)
    config = ProjectConfig(project_name='self-repair-convergence-pilot')
    config.providers, config.efforts = selected.providers, selected.efforts
    config.active_provider, config.execution = provider, selected.execution
    if config.providers[provider].kind != 'codex':
        raise ValueError('this native pilot currently uses the Codex adapter')
    adapter = CodexAdapter(config.providers[provider])
    calls, events = [], []
    def invoke(request):
        start = time.monotonic()
        print('provider start: ' + request.stage, flush=True)
        result = adapter.run(request)
        calls.append({'stage': request.stage, 'seconds': time.monotonic() - start, 'ok': result.ok,
            'prompt_chars': len(request.prompt), 'native_schema': '--output-schema' in result.command,
            'usage': asdict(result.usage) if is_dataclass(result.usage) else result.usage})
        (output / 'provider_calls.json').write_text(json.dumps(calls, indent=2))
        (output / ('provider-' + str(len(calls)) + '-stderr.log')).write_text(result.stderr)
        print('provider finish: ' + request.stage + ' ok=' + str(result.ok), flush=True)
        return result
    orchestrator = SimpleNamespace(config=config, _current_provider=provider, _call_with_failover=invoke)
    required = 'Registered dependency aliases preserve valid reuse; unregistered aliases, changed inputs and retargeted aliases cannot authorize reuse. Restoring the original input/target restores valid reuse.'
    full = 'python -m pytest -q tests/test_alias_reuse.py'
    diagnosis = SimpleNamespace(to_dict=lambda: {'expected_postconditions': [required],
        'causal_chain': ['reusable rejects every symlink before consulting the registered dependency identity'],
        'proposed_fix_scope': ['source.py'], 'verification_commands': [full]},
        final=SimpleNamespace(expected_postconditions=[required]))
    runner = AutoAgentsSelfRepairRunner(orchestrator, target_project_root=target,
        error=RuntimeError('registered dependency alias incorrectly disables reuse'),
        decision=SelfRepairDecision(True, category='pilot_alias_reuse', reason='isolated c94-pattern reproduction'),
        diagnosis=diagnosis)
    runner.repo_root = engine
    runner._verification_python_cache = sys.executable
    runner._verification_fresh = True
    runner._real_project_root = target
    runner._continuous_workspace = output / 'continuous'
    runner._candidate_is_final_group = False
    state = SelfRepairExperiment.create(run_id='pilot', root_fingerprint='alias-reuse', category='pilot',
        base_commit=git(engine, 'rev-parse', 'HEAD'), expected_postconditions=[required])
    root_id = next(identity for identity in state.contract_obligation_ids if identity.startswith('root:'))
    group = {'group_id': 'alias_reuse', 'title': 'Registered dependency alias reuse', 'status': 'pending',
             'contract_obligation_ids': [root_id], 'finding_ids': [], 'depends_on': [],
             'touched_paths': ['source.py'], 'focused_tests': [full],
             'implementation_steps': ['Repair registered alias handling while preserving all four existing tests unchanged.']}
    state.finding_groups = [group]
    state.active_finding_group_id = group['group_id']
    runner._experiment = state
    runner._experiment_store = SelfRepairExperimentStore(target, 'pilot', 'alias-reuse')
    runner._candidate_group = dict(group)
    runner._experiment_store.save(state)
    def event(kind, payload):
        events.append({'kind': kind, 'payload': payload, 'at': time.time()})
        with (output / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(events[-1]) + '\n')
    runner._control_phase_callback = event
    negative_command = 'python -m pytest -q tests/test_alias_reuse.py::test_registered_alias_reuses'
    positive_command = 'python -m pytest -q tests/test_alias_reuse.py::test_unregistered_alias_denied'
    runner._candidate_failure_evidence = []
    negative = runner._run_verification_commands([negative_command], engine)
    if negative.ok or not any(row.get('failure_kind') == 'assertion' for row in runner._candidate_failure_evidence):
        raise RuntimeError('pilot negative control did not produce a behavioral assertion failure')
    positive = runner._run_verification_commands([positive_command], engine)
    if not positive.ok:
        raise RuntimeError('pilot compatibility control failed before any model request')
    (output / 'baseline.json').write_text(json.dumps({'negative': negative.to_dict(), 'positive': positive.to_dict()}, indent=2))
    if proposal:
        from auto_agents.repair_memory import save_record, remember_revision
        from auto_agents.repair_control import digest
        from auto_agents.verification_ledger import source_identity
        draft = json.loads(proposal.read_text())
        origin = save_record(runner, 'retained_model_proposal', {'path': str(proposal), 'draft': draft,
                            'acceptance_proof': False})
        remember_revision(runner, group, {'draft': draft, 'component': group, 'status': 'recovered_draft',
            'source': source_identity(engine), 'source_commit': state.base_commit,
            'environment': digest(runner._full_suite_environment_fingerprint()),
            'planner_request': origin['id'], 'requires_independent_review': True})
    results, phase_calls = [], []
    seen = set()
    start = time.monotonic()
    for phase in ('retained_plan' if proposal else 'initial', 'injected_regression'):
        if phase == 'injected_regression':
            workspace = runner._continuous_workspace / 'repair'
            (workspace / 'source.py').write_text(BROKEN)
            git(workspace, 'add', 'source.py')
            git(workspace, 'commit', '-qm', 'pilot: inject the retained counterexample')
            runner._candidate_failure_evidence = []
            failure = runner._run_verification_commands([
                'python -m pytest -q tests/test_alias_reuse.py::test_registered_alias_reuses'], workspace)
            if failure.ok:
                raise AssertionError('fault injection did not reproduce the original negative control')
            record = SelfRepairCandidateRecord('injected', candidate_commit=git(workspace, 'rev-parse', 'HEAD'),
                finding_group_id=group['group_id'], status='candidate_verification_failed',
                failure_evidence=list(runner._candidate_failure_evidence))
            state.candidates[record.candidate_id] = record
            group['status'] = 'pending'
        before_calls = len(calls)
        for _ in range(3):
            runner._candidate_group = dict(group)
            runner._candidate_next_action = prepare_action(runner, state)
            if runner._candidate_next_action.get('kind') == 'blocked':
                raise RuntimeError(runner._candidate_next_action['cause'])
            runner._candidate_failure_evidence = []
            runner._candidate_verified_check_ids = set()
            runner._candidate_prepared_dependencies = set()
            runner._candidate_started_at = time.monotonic()
            result = runner._run_candidate(experiment_id=state.experiment_id, attempt=state.attempt_count + 1,
                deadline=None, prior_failures=[], seen_fingerprints=seen)
            runner._decorate_candidate_result(result, attempt=state.attempt_count + 1)
            runner._register_search_result(result)
            results.append({'phase': phase, 'status': result.status, 'reason': result.reason, 'candidate': result.candidate_id})
            (output / 'results.json').write_text(json.dumps(results, indent=2))
            if result.status == 'candidate_group_completed':
                break
        else:
            raise RuntimeError('pilot did not complete its retained component acceptance')
        phase_calls.append({'phase': phase, 'stages': [row['stage'] for row in calls[before_calls:]]})
    workspace = runner._continuous_workspace / 'repair'
    assert (workspace / 'tests/test_alias_reuse.py').read_text() == TESTS, 'original acceptance was modified'
    assert (target / 'sentinel.txt').read_text() == 'preserve original target\n'
    final = runner._run_verification_commands([full], workspace)
    report = {'ok': final.ok, 'scope': 'two real-provider component repair cycles and complete fixture acceptance',
        'original_project_resumed': False, 'engine_published': False, 'final_engine_handoff_exercised': False,
        'retained_untrusted_plan': str(proposal) if proposal else None,
        'seconds': time.monotonic() - start, 'phase_calls': phase_calls, 'calls': calls, 'results': results,
        'acceptance_sha256': hashlib.sha256(TESTS.encode()).hexdigest(), 'history': history_report(state),
        'final_verification': final.to_dict()}
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({key: report[key] for key in ('ok', 'seconds', 'phase_calls')}), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new disposable directory; must not exist')
    parser.add_argument('--configuration-project', type=Path, required=True, help='read provider and effort settings only')
    parser.add_argument('--provider', required=True)
    parser.add_argument('--proposal', type=Path, help='optional retained model plan as untrusted data; requires fresh probes and independent review')
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve(), args.configuration_project.resolve(), args.provider, args.proposal))
