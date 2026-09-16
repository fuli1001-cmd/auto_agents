"""Public session recovery regressions with real Git and pytest execution."""
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from execution_marker import ExecutionMarker

from auto_agents.config import (load_project_config, save_project_config, save_session_state,
                                load_session_state, save_task_plan)
from auto_agents.git_ops import head_ref
from auto_agents.models import AgentResult, SessionState, VerificationStep
from auto_agents.orchestrator import Orchestrator
from auto_agents.session import Session
from test_session import _make_project


def git(root, *args):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def project(tmp_path, *, missing=False):
    root = _make_project(str(tmp_path))
    (root / '.conda').symlink_to(sys.prefix, target_is_directory=True)
    (root / 'value.py').write_text('VALUE = 0\n')
    (root / 'foreign.py').write_text('VALUE = 7\n')
    (root / 'tests').mkdir(exist_ok=True)
    (root / 'tests/test_owned.py').write_text(
        'from pathlib import Path\n'
        'def test_owned():\n'
        '    assert "VALUE = 1" in Path("value.py").read_text()\n'
        '    assert "VALUE = 7" in Path("foreign.py").read_text()\n'
        '    assert not Path("foreign-note.txt").exists()\n')
    config = load_project_config(root)
    config.gates.steps = [VerificationStep(
        runner='pytest', targets=['tests/test_owned.py::test_missing' if missing else 'tests/test_owned.py::test_owned'],
        proof_id='owned.contract', levels=['affected', 'release'], impact_paths=['value.py'],
    )]
    config.gates.verification_policy_version = 4
    config.gates.release_worker.enabled = False
    config.gates.release_worker.auto_start = False
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-owned', 'title': 'Owned contract', 'requirement_ids': ['REQ-owned'],
                                   'verification_refs': config.gates.steps[0].targets}],
                          'verification_steps': [s.to_dict() for s in config.gates.steps],
                          'verification_policy_version': 4})
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'owned contract')
    state = SessionState(session_id='owned-child', status='failed', mode='fix',
                         goal='Repair the existing value', auto_approve=True,
                         baseline_head_ref=head_ref(root), baseline_git_ref=head_ref(root))
    save_session_state(root, state)
    return root, state


def run_session(root, monkeypatch, *, mutate=True):
    orch = Orchestrator(root, user_input_fn=lambda *_args, **_kw: 'y')
    calls = []
    def agent(request):
        calls.append(request.purpose)
        if mutate:
            (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Repaired value.\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', agent)
    result = Session(orch, mode='fix', auto_approve=True).resume('owned-child')
    return result, calls, orch


def test_session_gate_selection_binds_authorization_candidate_and_contract(tmp_path, monkeypatch):
    root, state = project(tmp_path)
    result, calls, _ = run_session(root, monkeypatch)
    assert result.status == 'completed', result.to_dict()
    saved = load_session_state(root, state.session_id)
    assert saved.verification_binding['session_id'] == state.session_id
    assert saved.verification_binding['contract_fingerprint']
    assert set(saved.candidate_paths) == {'value.py'}
    assert len(calls) == 1
    shared_head = head_ref(root)
    delivered = saved.candidate_custody['delivered_revision']
    resumed, repeated_calls, _ = run_session(root, monkeypatch)
    assert resumed.status == 'completed', resumed.to_dict()
    assert repeated_calls == []
    assert resumed.candidate_custody['delivered_revision'] == delivered
    assert head_ref(root) == shared_head


def test_resumed_fix_ignores_foreign_pending_plan_without_weakening_its_gates(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    plan = {'tasks': [{'task_id': 'task-foreign', 'title': 'Foreign task', 'status': 'pending', 'verification_refs': ['tests/test_future.py::test_future']}],
            'verification_steps': [VerificationStep(runner='pytest', targets=['tests/test_future.py::test_future'],
                                  proof_id='foreign.future', levels=['affected', 'release'], impact_paths=['**']).to_dict()],
            'verification_policy_version': 4}
    save_task_plan(root, plan)
    config = load_project_config(root)
    config.gates.steps = [VerificationStep.from_dict(plan['verification_steps'][0])]
    config.gates.commands = [shlex.join([sys.executable, '-m', 'pytest', 'tests/test_future.py::test_future'])]
    save_project_config(root, config)
    before = (root / '.auto-agents/state/task_plan.json').read_bytes()
    result, _, _ = run_session(root, monkeypatch)
    assert result.status == 'completed', result.to_dict()
    assert (root / '.auto-agents/state/task_plan.json').read_bytes() == before
    assert load_project_config(root).gates.steps[0].proof_id == 'foreign.future'


def test_candidate_verification_excludes_foreign_dirty_changes(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00bytes')
    index = git(root, 'show', ':foreign.py')
    original_head = head_ref(root)
    result, _, _ = run_session(root, monkeypatch)
    assert result.status == 'completed', result.to_dict()
    assert git(root, 'show', ':foreign.py') == index
    assert (root / 'foreign.py').read_text() == 'VALUE = 9\n'
    assert (root / 'foreign-note.txt').read_bytes() == b'foreign\x00bytes'
    assert head_ref(root) == original_head
    delivery = result.candidate_custody
    assert 'foreign.py' not in git(Path(delivery['checkout']), 'diff', '--name-only',
                                   delivery['base_revision'], delivery['delivered_revision'])


@pytest.mark.parametrize('routed', [False, True])
def test_resume_recovers_expired_snapshot_contract_from_saved_head(tmp_path, monkeypatch, routed):
    from test_engine_child_recovery import parent_workflow, resume_to_observation

    root, state = project(tmp_path)
    original_gates = load_project_config(root).gates.to_dict()
    state.baseline_git_ref = 'refs/auto-agents/gate-snapshots/expired'
    state.baseline_failures = ['tests/test_old.py::test_old']
    save_session_state(root, state)
    if routed:
        from auto_agents.repair_control import digest
        _, _, handoff = parent_workflow(root, state, engine=True)
        receipt = tmp_path / 'route-probe.json'
        receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
        monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    config = load_project_config(root)
    config.gates.steps[0].targets = ['tests/test_future.py::test_future']
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-foreign', 'title': 'Foreign task', 'status': 'pending',
                                   'verification_refs': ['tests/test_future.py::test_future']}]})
    plan_bytes = (root / '.auto-agents/state/task_plan.json').read_bytes()
    # Advance HEAD too: recovery must use the saved source, not the current plan.
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'start another workflow contract')
    calls = []
    if routed:
        def action(child, prompt, candidate_root):
            calls.append(child.session_id)
            (candidate_root / 'value.py').write_text('VALUE = 1\n')
            return 'Repaired value.\nCOMMIT_MESSAGE: Repair owned value'
        resume_to_observation(root, monkeypatch, action)
    else:
        _, calls, _ = run_session(root, monkeypatch)
    saved = load_session_state(root, state.session_id)
    assert saved.status == 'completed', saved.to_dict()
    assert len(calls) == 1
    assert saved.baseline_git_ref != state.baseline_git_ref
    assert saved.baseline_failures == []
    assert saved.verification_binding['gates'] == original_gates
    assert saved.verification_binding['tasks'][0]['task_id'] == 'task-owned'
    assert (root / '.auto-agents/state/task_plan.json').read_bytes() == plan_bytes
    assert load_project_config(root).gates.steps[0].targets == ['tests/test_future.py::test_future']


def test_child_resume_without_contract_history_blocks_before_agent_work(tmp_path, monkeypatch):
    from test_engine_child_recovery import parent_workflow, resume_to_observation
    from auto_agents.repair_control import digest

    root, child = project(tmp_path)
    child.baseline_git_ref = 'refs/auto-agents/gate-snapshots/expired'
    child.baseline_head_ref = ''
    child.lineage_head_ref = ''
    _, _, handoff = parent_workflow(root, child, engine=True)
    receipt = tmp_path / 'route-probe.json'
    receipt.write_text(json.dumps({'route_digest': digest(handoff.payload)}))
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_ROUTE_PROBE', str(receipt))
    before = head_ref(root)
    def action(state, prompt, candidate_root):
        pytest.fail('A child without recoverable contract history must not execute')
    resume_to_observation(root, monkeypatch, action)
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'blocked'
    assert saved.resolution == 'verification_ownership'
    assert saved.verification_binding == {}
    assert any('contract revision is unavailable' in str(entry) for entry in saved.execution_log)
    assert head_ref(root) == before
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'


def test_missing_entry_preflight_reports_session_task_requirement_and_contract(tmp_path, monkeypatch):
    root, _ = project(tmp_path, missing=True)
    result, calls, _ = run_session(root, monkeypatch)
    assert result.status != 'completed'
    saved = load_session_state(root, 'owned-child')
    diagnostic = next(iter(saved.verification_diagnostics.values()))['diagnostic']
    assert diagnostic['session_id'] == 'owned-child'
    assert diagnostic['owners'][0]['task_id'] == 'task-owned'
    assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
    assert diagnostic['contract_fingerprint']
    assert 'test_missing' in diagnostic['output']
    assert len(calls) == 1


def test_missing_entry_stops_before_targeted_and_affected_execution(tmp_path, monkeypatch):
    root, state = project(tmp_path, missing=True)
    state.fix_verify_command = shlex.join([sys.executable, '-c', 'raise RuntimeError("targeted must not run")'])
    save_session_state(root, state)
    result, _, _ = run_session(root, monkeypatch)
    saved = load_session_state(root, 'owned-child')
    failure = next(iter(saved.verification_diagnostics.values()))
    assert failure['failure_kind'] == 'verification_entry_unavailable'
    assert failure['executed_commands'] == 1
    assert 'targeted must not run' not in failure['diagnostic']['output']


@pytest.mark.parametrize('failure', ['collection', 'command'])
def test_incomparable_commands_have_bounded_diagnostics_across_resume(tmp_path, monkeypatch, failure):
    root, state = project(tmp_path, missing=failure == 'collection')
    if failure == 'command':
        state.fix_verify_command = shlex.join([sys.executable, '-c', 'raise SystemExit(3)'])
        save_session_state(root, state)
    result, _, _ = run_session(root, monkeypatch)
    before = load_session_state(root, 'owned-child').verification_diagnostics
    result, _, _ = run_session(root, monkeypatch, mutate=False)
    assert result.status != 'completed'
    assert load_session_state(root, 'owned-child').verification_diagnostics == before
    assert result.execution_log[-2]['action'] != 'commit'


def test_overlapping_ownership_blocks_without_discarding_foreign_changes(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    (root / 'value.py').write_text('VALUE = 30\n')
    git(root, 'add', 'value.py')
    (root / 'value.py').write_text('VALUE = 31\n')
    result, _, _ = run_session(root, monkeypatch)
    assert result.status == 'completed', result.to_dict()
    delivery = result.candidate_custody
    assert git(Path(delivery['checkout']), 'show', delivery['delivered_revision'] + ':value.py') == 'VALUE = 1\n'
    assert git(root, 'show', ':value.py') == 'VALUE = 30\n'
    assert (root / 'value.py').read_text() == 'VALUE = 31\n'


@pytest.mark.parametrize('contract', ['missing', 'changed'])
def test_ownership_filter_preserves_refs_contracts_and_pending_task_scope(tmp_path, monkeypatch, contract):
    root, state = project(tmp_path, missing=contract == 'missing')
    if contract == 'changed':
        from auto_agents.config import load_task_plan, requirements_trace_path
        from auto_agents.requirements import requirement_contract_sha256
        row = {'id': 'REQ-owned', 'text': 'Repair the owned value', 'source': 'spec'}
        trace_path = requirements_trace_path(root)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps({'requirements': [row]}))
        plan = load_task_plan(root)
        plan['tasks'][0]['requirement_proofs'] = [{
            'requirement_id': row['id'], 'requirement_contract_sha256': requirement_contract_sha256(row),
            'evidence_refs': ['tests/test_owned.py::test_owned']}]
        save_task_plan(root, plan)
        git(root, 'add', '-A')
        git(root, 'commit', '-m', 'bind requirement contract')
        state.baseline_git_ref = state.baseline_head_ref = head_ref(root)
        save_session_state(root, state)
        row['text'] = 'A changed product requirement'
        trace_path.write_text(json.dumps({'requirements': [row]}))
    before = (root / '.auto-agents/state/task_plan.json').read_bytes()
    result, _, _ = run_session(root, monkeypatch)
    assert result.status != 'completed'
    assert (root / '.auto-agents/state/task_plan.json').read_bytes() == before
    if contract == 'missing':
        assert load_project_config(root).gates.steps[0].targets == ['tests/test_owned.py::test_missing']
    else:
        assert any('no longer matches' in str(entry) for entry in result.execution_log)


def test_missing_or_unexecuted_required_checks_cannot_attest_success(tmp_path, monkeypatch):
    root, _ = project(tmp_path, missing=True)
    before = head_ref(root)
    result, _, _ = run_session(root, monkeypatch)
    assert result.status != 'completed'
    assert head_ref(root) == before


@pytest.mark.parametrize('candidate', ['product', 'provider_doc'])
def test_child_rollback_preserves_foreign_index_worktree_and_untracked_bytes(tmp_path, monkeypatch, candidate):
    from test_engine_child_recovery import parent_workflow, resume_to_observation
    root, child = project(tmp_path, missing=True)
    store, _, handoff = parent_workflow(root, child)
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00bytes')
    original_quick = Orchestrator._quick_verify_failure_details
    def concurrent_work(self, *args, **kwargs):
        (root / 'late-foreign.txt').write_bytes(b'concurrent\x00bytes')
        return original_quick(self, *args, **kwargs)
    monkeypatch.setattr(Orchestrator, '_quick_verify_failure_details', concurrent_work)
    def action(state, prompt, candidate_root):
        (candidate_root / 'value.py').write_text('VALUE = 1\n')
        if candidate == 'provider_doc':
            doc = candidate_root / '.auto-agents/docs/provider_references/candidate.md'
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text('Unverified candidate provider reference')
        return 'Repair candidate ready'
    resume_to_observation(root, monkeypatch, action)
    returned = store.load_handoff(handoff.handoff_id)
    expected = ['value.py']
    if candidate == 'provider_doc':
        expected.insert(0, '.auto-agents/docs/provider_references/candidate.md')
        assert not (root / expected[0]).exists()
    assert returned.result['rolled_back_paths'] == []
    custody = load_session_state(root, child.session_id).candidate_custody
    assert sorted(custody['receipt']['manifest']) == expected
    assert (Path(custody['checkout']) / 'value.py').read_text() == 'VALUE = 1\n'
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    assert git(root, 'show', ':foreign.py') == 'VALUE = 8\n'
    assert (root / 'foreign.py').read_text() == 'VALUE = 9\n'
    assert (root / 'foreign-note.txt').read_bytes() == b'foreign\x00bytes'
    assert (root / 'late-foreign.txt').read_bytes() == b'concurrent\x00bytes'


# Retained repair contracts use these public node IDs. Reuse the complete
# regressions, including their parametrization, without replacing either name.
test_session_selection_requires_authorization_candidate_ownership_and_valid_contract = test_session_gate_selection_binds_authorization_candidate_and_contract
test_missing_pytest_entry_preflight_reports_owner_before_execution = test_missing_entry_preflight_reports_session_task_requirement_and_contract
test_missing_pytest_entry_does_not_repeat_noncomparable_commands = test_incomparable_commands_have_bounded_diagnostics_across_resume
test_verification_snapshot_contains_only_child_owned_candidate = test_candidate_verification_excludes_foreign_dirty_changes
test_failed_child_rollback_preserves_foreign_worktree_and_index = test_child_rollback_preserves_foreign_index_worktree_and_untracked_bytes
test_resumed_fix_ignores_foreign_pending_plan_after_global_plan_switch = test_resumed_fix_ignores_foreign_pending_plan_without_weakening_its_gates
test_resumed_child_rollback_preserves_foreign_changes_created_after_checkpoint = test_child_rollback_preserves_foreign_index_worktree_and_untracked_bytes


@pytest.mark.parametrize('owned_status', ['done', 'pending'])
def test_session_selection_preserves_regressions_and_other_workflow_release_gates(tmp_path, monkeypatch, owned_status):
    from test_session_review_regressions import test_resumed_snapshot_repair_excludes_foreign_pending_gates_already_in_baseline

    test_resumed_snapshot_repair_excludes_foreign_pending_gates_already_in_baseline(tmp_path, monkeypatch, owned_status)


@pytest.mark.parametrize('owned_status', ['done', 'pending'])
def test_foreign_pending_proofs_remain_intact_without_implementing_their_requirements(tmp_path, monkeypatch, owned_status):
    from test_session_review_regressions import test_resumed_snapshot_repair_excludes_foreign_pending_gates_already_in_baseline

    test_resumed_snapshot_repair_excludes_foreign_pending_gates_already_in_baseline(tmp_path, monkeypatch, owned_status)


@pytest.mark.parametrize('ownership', ['overlapping', 'unknown'])
def test_overlapping_or_unknown_ownership_blocks_destructive_rollback(tmp_path, monkeypatch, ownership):
    if ownership == 'overlapping':
        test_overlapping_ownership_blocks_without_discarding_foreign_changes(tmp_path, monkeypatch)
    else:
        test_child_resume_without_contract_history_blocks_before_agent_work(tmp_path, monkeypatch)


@pytest.mark.parametrize('waiver', ['reference_deletion', 'cached_success', 'empty_owned', 'artifact_only',
                                  'sibling_evidence', 'retained_sibling_evidence'])
def test_missing_owned_proof_cannot_be_waived_by_reference_deletion_or_cached_success(tmp_path, monkeypatch, waiver):
    from auto_agents.models import CommandResult, GateResult
    import auto_agents.session as session_module

    root, child = project(tmp_path, missing=True)
    if waiver in {'empty_owned', 'artifact_only', 'sibling_evidence', 'retained_sibling_evidence'}:
        from auto_agents.config import requirements_trace_path
        from auto_agents.requirements import requirement_contract_sha256
        config = load_project_config(root)
        config.gates.steps = []
        config.gates.commands = []
        config.gates.parallel_groups = []
        row = {'id': 'REQ-owned', 'text': 'Repair the owned value', 'source': 'spec'}
        requirements_trace_path(root).write_text(json.dumps({'requirements': [row]}))
        refs = ['artifact:review.md'] if waiver == 'artifact_only' else []
        plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Owned requirement without evidence',
            'requirement_ids': ['REQ-owned'], 'verification_refs': refs,
            'requirement_proofs': [{'requirement_id': 'REQ-owned',
                'requirement_contract_sha256': requirement_contract_sha256(row), 'evidence_refs': []}]}]}
        if waiver.endswith('sibling_evidence'):
            # One task's evidence union must not discharge its empty sibling.
            sibling = {'id': 'REQ-empty', 'text': 'A separate owned obligation', 'source': 'spec'}
            requirements_trace_path(root).write_text(json.dumps({'requirements': [row, sibling]}))
            (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
            config.gates.steps = [VerificationStep(proof_id='one.proof', runner='pytest',
                targets=['tests/test_control.py'], levels=['affected', 'release'])]
            task = plan['tasks'][0]
            task['requirement_ids'].append('REQ-empty')
            task['requirement_proofs'][0]['evidence_refs'] = ['one.proof']
            task['requirement_proofs'].append({'requirement_id': 'REQ-empty',
                'requirement_contract_sha256': requirement_contract_sha256(sibling), 'evidence_refs': []})
            plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
        _retain_contract(root, child, config, plan)
        issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
        issue.write_text(json.dumps({'task_id': 'task-owned'}))
        retained = {}
        if waiver == 'retained_sibling_evidence':
            from copy import deepcopy
            import auto_agents.session_verification as verification
            # Reconstruct the old task-wide admission without changing the
            # retained requirement records or their valid contract hashes.
            with monkeypatch.context() as old:
                old.setattr(verification, '_owned_inventory', lambda *_: (['one.proof'], {}))
                old.setattr(verification, '_PROOF_INVENTORY_VERSION', 2)
                _binding_fixture(root, child)
            retained = deepcopy(child.verification_binding)
            save_session_state(root, child)
        ambient = _switch_ambient_binding_plan(root)
        def reject_gate(*args, **kwargs):
            pytest.fail('Empty requirement evidence must block before collection or cached execution')
        monkeypatch.setattr(session_module, 'run_gate_plan', reject_gate)
        saved = _assert_binding_blocked_before_execution(root, monkeypatch)
        diagnostic = saved.execution_log[-1]['diagnostic']
        assert diagnostic['task_id'] == 'task-owned'
        assert diagnostic['requirement_ids'] == (['REQ-empty'] if waiver.endswith('sibling_evidence')
                                                 else ['REQ-owned'])
        assert diagnostic['owners'][0]['task_id'] == 'task-owned'
        assert diagnostic['task_scope'] == {'task_ids': ['task-owned'], 'requirement_ids': []}
        assert diagnostic['contract_fingerprint'] and diagnostic['retry_fix'] is False
        assert saved.verification_binding == retained, 'rejection must preserve the prior binding'
        assert {name: (root / name).read_bytes() for name in ambient} == ambient
        return
    if waiver == 'reference_deletion':
        save_task_plan(root, {'tasks': [{'task_id': 'task-owned', 'title': 'Owned contract',
                                       'requirement_ids': ['REQ-owned'], 'verification_refs': []}]})
        config = load_project_config(root)
        config.gates.steps = []
        config.gates.commands = []
        save_project_config(root, config)
    original = session_module.run_gate_plan
    cache_hits = []

    def cached_execution(commands, *args, **kwargs):
        if waiver == 'cached_success' and commands and not any('--collect-only' in command for command in commands):
            cache_hits.extend(commands)
            return GateResult(ok=True, commands=[CommandResult(
                command=command, ok=True, returncode=0, cached=True,
            ) for command in commands], summary='cached success')
        return original(commands, *args, **kwargs)

    monkeypatch.setattr(session_module, 'run_gate_plan', cached_execution)
    before = head_ref(root)
    saved, _, _ = run_session(root, monkeypatch)
    assert saved.status != 'completed'
    assert head_ref(root) == before
    failure = next(iter(saved.verification_diagnostics.values()))
    assert failure['failure_kind'] == 'verification_entry_unavailable'
    assert 'tests/test_owned.py::test_missing' in failure['diagnostic']['command']
    assert cache_hits == [], 'cached execution must not bypass missing-entry preflight'


@pytest.mark.parametrize('evidence_source', ['requirement', 'task', 'explicit_fix'])
def test_public_resume_accepts_evidence_for_each_owned_requirement(tmp_path, monkeypatch, evidence_source):
    from auto_agents.config import requirements_trace_path
    from auto_agents.requirements import requirement_contract_sha256

    root, child = project(tmp_path)
    config = load_project_config(root)
    rows = [{'id': key, 'text': 'Retain ' + key, 'source': 'spec'}
            for key in ('REQ-one', 'REQ-two')]
    requirements_trace_path(root).write_text(json.dumps({'requirements': rows}))
    task = {'task_id': 'task-owned', 'title': 'Two owned requirements',
            'requirement_ids': [row['id'] for row in rows],
            'verification_refs': ['owned.contract'] if evidence_source == 'task' else [],
            'requirement_proofs': [{'requirement_id': row['id'],
                'requirement_contract_sha256': requirement_contract_sha256(row),
                'evidence_refs': ['owned.contract'] if evidence_source == 'requirement' else []}
                for row in rows]}
    if evidence_source == 'explicit_fix':
        child.fix_verify_command = './.conda/bin/python -m pytest -q tests/test_owned.py::test_owned'
    _retain_contract(root, child, config, {'tasks': [task],
        'verification_steps': [step.to_dict() for step in config.gates.steps]})
    ambient = _switch_ambient_binding_plan(root)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    assert saved.verification_binding['requirement_ids'] == ['REQ-one', 'REQ-two']
    assert saved.verification_binding['tasks'][0]['requirement_proofs'] == task['requirement_proofs']
    assert saved.verification_binding['required_proof_ids'] == ['owned.contract']
    custody = saved.candidate_custody
    assert git(Path(custody['checkout']), 'show', custody['delivered_revision'] + ':value.py') == 'VALUE = 1\n'
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('shape', ['empty_impact', 'final_only', 'overlapping_release', 'deduplicated'])
def test_public_resume_executes_complete_owned_proof_inventory(tmp_path, monkeypatch, shape):
    """Impact, cadence and command coalescing cannot discharge owned proofs."""
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'owned-proof-executed')
    proof = root / 'tests/test_owned.py'
    proof.write_text(proof.read_text() +
                     '    ' + marker.source(repr('executed')) + '\n')
    config = load_project_config(root)
    config.gates.release_blocking_paths = []
    config.gates.unmapped_change_policy = 'fallback'
    config.gates.fallback_proof_ids = []
    owned = config.gates.steps[0]
    owned.impact_paths = []
    if shape == 'final_only':
        owned.cadence, owned.levels = 'final_only', ['release']
    if shape in {'overlapping_release', 'deduplicated'}:
        owned.targets = ['tests/test_owned.py']
        config.gates.steps.append(VerificationStep(
            proof_id='owned.release', runner='pytest', targets=['tests/test_owned.py'],
            levels=['release'], cadence='final_only',
            args=['-x'] if shape == 'overlapping_release' else [],
        ))
    save_project_config(root, config)
    plan = load_task_plan(root)
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    save_task_plan(root, plan)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain complete owned proof inventory')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    observations = []
    resolve = Session._session_gate_plan
    def observe(self, scope):
        selected = resolve(self, scope)
        observations.append(selected)
        return selected
    monkeypatch.setattr(Session, '_session_gate_plan', observe)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    assert marker.read_text() == 'executed'
    binding = saved.verification_binding
    assert binding['repository'] == str(root.resolve())
    assert binding['schema_version'] == 13
    assert binding['baseline_identity']['head_ref'] == child.baseline_head_ref
    assert binding['authorization'] == saved.authorization_policy
    required = {'owned.contract'}
    if shape in {'overlapping_release', 'deduplicated'}:
        required.add('owned.release')
    assert set(binding['required_proof_ids']) == required
    for selected in observations:
        covered = {key for value in selected.metadata.values() for key in value.proof_ids}
        assert required <= covered
        assert not any('--deselect' in command for command in selected.commands)
        if shape == 'deduplicated':
            assert len(selected.commands) == 1
            assert set(next(iter(selected.metadata.values())).proof_ids) == required


def test_public_resume_blocks_owned_missing_prerequisite_before_writer(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    config = load_project_config(root)
    config.gates.steps[0].depends_on_proofs = ['missing.setup']
    save_project_config(root, config)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain missing prerequisite')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'blocked', saved.to_dict()
    assert calls == []
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['proof_id'] == 'owned.contract'
    assert diagnostic['missing_prerequisite'] == 'missing.setup'
    assert diagnostic['owners'][0]['task_id'] == 'task-owned'
    assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
    assert diagnostic['contract_fingerprint']
    assert diagnostic['retry_fix'] is False


def test_public_resume_cannot_waive_owned_failure_with_empty_candidate_or_baseline(tmp_path, monkeypatch):
    root, _ = project(tmp_path)
    saved, calls, _ = run_session(root, monkeypatch, mutate=False)
    assert saved.status != 'completed'
    assert any('mandatory owned verification failed' in str(entry) for entry in saved.execution_log)


@pytest.mark.parametrize('edit', [
    'replace', 'early_return', 'duplicate_definition', 'class_rebinding',
    'setup_replacement', 'default_rebinding', 'test_replacement', 'independent_addition',
])
def test_public_resume_rejects_candidate_weakening_before_cached_execution(tmp_path, monkeypatch, edit):
    import auto_agents.session as session_module
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    root, _ = project(tmp_path)
    orch = Orchestrator(root)
    def provider(request):
        proof = request.cwd / 'tests/test_owned.py'
        source = proof.read_text()
        if edit == 'replace':
            source = 'def test_owned(): pass\n'
        elif edit == 'early_return':
            source = source.replace('def test_owned():\n', 'def test_owned():\n    return\n')
        elif edit == 'duplicate_definition':
            source += '\ndef test_owned():\n    pass\n'
        elif edit == 'class_rebinding':
            source += "\nclass AddedHelper:\n    globals()['test_owned'] = lambda: None\n"
        elif edit == 'setup_replacement':
            source += '\ndef setup_module():\n    test_owned.__code__ = (lambda: None).__code__\n'
        elif edit == 'default_rebinding':
            source += "\ndef test_added(value=globals().update(test_owned=lambda: None)):\n    assert True\n"
        elif edit == 'test_replacement':
            source += '\ndef test_added():\n    test_owned.__code__ = (lambda: None).__code__\n'
        else:
            source += '\ndef test_independent():\n    assert 2 + 2 == 4\n'
            (request.cwd / 'value.py').write_text('VALUE = 1\n')
        proof.write_text(source)
        request.output_path.write_text('Repaired\nCOMMIT_MESSAGE: Repair owned value')
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary='Repaired', stdout=request.output_path.read_text(), returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume('owned-child')
    if edit == 'independent_addition':
        assert saved.status == 'completed', saved.to_dict()
    else:
        assert saved.status != 'completed', saved.to_dict()
        assert dispatched == [], 'source validation must precede collection, execution and cache lookup'
        assert any('required proof source was removed or changed' in str(entry) for entry in saved.execution_log)
        assert any(entry.get('diagnostic', {}).get('verification_ref') == 'tests/test_owned.py'
                   for entry in saved.execution_log)


@pytest.mark.parametrize('style', ['command_only', 'manual_group'])
def test_public_resume_retains_owned_legacy_commands(tmp_path, monkeypatch, style):
    from auto_agents.models import GateParallelGroup
    root, child = project(tmp_path)
    command = './.conda/bin/python -m pytest -q tests/test_owned.py::test_owned'
    config = load_project_config(root)
    if style == 'command_only':
        config.gates.steps = []
        config.gates.commands = [command]
    else:
        config.gates.steps[0].impact_paths = []
        config.gates.parallel_groups = [GateParallelGroup(name='manual', commands=[command])]
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-owned', 'title': 'Owned legacy check', 'requirement_ids': ['REQ-owned'],
                                   'verification_refs': ['cmd:' + command]}]})
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain legacy command obligation')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    saved, _, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert command in saved.verification_binding['required_commands']
    assert saved.verification_binding['task_ids'] == ['task-owned']
    assert saved.verification_binding['requirement_ids'] == ['REQ-owned']


def test_public_resume_keeps_foreign_completed_proof_as_regression(tmp_path, monkeypatch):
    from auto_agents.config import load_task_plan
    from auto_agents.release_attestation import current_release_attestation

    root, child = project(tmp_path)
    config = load_project_config(root)
    config.gates.steps.append(VerificationStep(
        proof_id='foreign.completed', runner='pytest', targets=['tests/test_foreign.py'],
        impact_paths=['foreign.py'], levels=['affected', 'release'],
    ))
    (root / 'tests/test_foreign.py').write_text('def test_foreign(): assert False\n')
    save_project_config(root, config)
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Foreign completed task',
                          'workflow_id': 'another-workflow', 'status': 'done',
                          'verification_refs': ['tests/test_foreign.py']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    save_task_plan(root, plan)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain unrelated completed proof')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    saved, _, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert saved.verification_binding['required_proof_ids'] == ['owned.contract']
    assert 'foreign.completed' in {step['proof_id'] for step in saved.verification_binding['gates']['steps']}
    assert load_project_config(root).gates.steps[-1].targets == ['tests/test_foreign.py']
    release = current_release_attestation(Path(saved.candidate_custody['checkout']))['latest']
    assert current_release_attestation(root)['latest'] == {}
    assert release['status'] == 'pending'
    assert release['affected_proof_ids'] == []


def _retain_contract(root, child, config, plan):
    child.hard_ceiling = 1
    save_project_config(root, config)
    save_task_plan(root, plan)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain session regression contract')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)


def test_public_resume_recovers_release_proof_removed_from_generated_config(tmp_path, monkeypatch):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'executed-invocations')
    (root / 'tests/test_owned.py').write_text(
        'from pathlib import Path\ndef test_owned(request):\n'
        '    assert "VALUE = 1" in Path("value.py").read_text()\n'
        '    ' + marker.source('str(bool(request.config.getoption("strict_markers"))) + chr(10)', append=True) + '\n')
    config = load_project_config(root)
    config.gates.steps[0].targets = ['tests/test_owned.py']
    config.gates.steps[0].levels = ['affected']
    release = VerificationStep(proof_id='owned.release', runner='pytest',
        targets=['tests/test_owned.py'], args=['--strict-markers'], levels=['release'], cadence='final_only')
    plan = load_task_plan(root)
    plan['verification_steps'] = [config.gates.steps[0].to_dict(), release.to_dict()]
    # Historical generation has already subtracted the overlapping release
    # file. Only the retained plan still has the distinct strict invocation.
    _retain_contract(root, child, config, plan)
    foreign_config = load_project_config(root)
    foreign_config.gates.steps = [VerificationStep(proof_id='foreign.future', runner='pytest',
        targets=['tests/test_future.py::test_future'], levels=['release'])]
    save_project_config(root, foreign_config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-foreign', 'title': 'Foreign pending work', 'status': 'pending',
        'verification_refs': ['tests/test_future.py::test_future']}]})
    ambient = {path: (root / path).read_bytes() for path in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    assert set(marker.read_text().splitlines()) == {'False', 'True'}
    assert set(saved.verification_binding['required_proof_ids']) == {'owned.contract', 'owned.release'}
    assert saved.verification_binding['plan']['verification_steps'] == plan['verification_steps']
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('command_source', ['legacy', 'manual', 'fix'])
@pytest.mark.parametrize('reference_kind', ['selector', 'proof', 'command', 'directory', 'vitest_file',
                                           'command_directory', 'command_vitest_file', 'file', 'command_node',
                                           'cwd_node', 'env_node', 'delimited_node', 'empty_fix',
                                           'conda_node', 'nested_node', 'command_vitest_selector', 'expanded_report',
                                           'command_vitest_basename', 'command_vitest_filter',
                                           'command_vitest_configured_filter', 'command_vitest_redirect',
                                           'command_vitest_conda_cwd', 'command_vitest_shell_cwd',
                                           'command_vitest_named_conda_cwd', 'command_vitest_active_conda_cwd'])
def test_public_resume_accepts_typed_executable_reference(tmp_path, monkeypatch, command_source, reference_kind):
    from auto_agents.models import GateParallelGroup

    root, child = project(tmp_path)
    config = load_project_config(root)
    command_only_target = reference_kind.startswith('command_')
    target_kind = reference_kind.removeprefix('command_')
    active_conda = target_kind == 'vitest_active_conda_cwd'
    if active_conda:
        target_kind = 'vitest_named_conda_cwd'
    command = './.conda/bin/python -m pytest -q tests/test_owned.py --junitxml report.xml'
    reference = {'selector': 'tests/test_owned.py::test_owned', 'proof': 'owned.contract',
                 'command': 'cmd:' + command, 'directory': 'tests',
                 'vitest_file': 'tests/owned.test.ts', 'file': 'tests/test_owned.py',
                 'node': 'tests/test_owned.py::test_owned', 'cwd_node': 'tests/test_owned.py::test_owned',
                 'env_node': 'tests/test_owned.py::test_owned',
                 'delimited_node': 'tests/test_owned.py::test_owned', 'empty_fix': '',
                 'conda_node': 'tests/test_owned.py::test_owned',
                 'nested_node': 'tests/test_owned.py::test_owned',
                 'vitest_selector': 'tests/owned.test.ts::owned value',
                 'vitest_basename': 'owned.test.ts', 'vitest_filter': 'OWNED.TEST',
                 'vitest_configured_filter': 'owned.check', 'vitest_redirect': 'owned.test.ts',
                 'vitest_conda_cwd': 'web/owned.test.ts', 'vitest_shell_cwd': 'web/owned.test.ts',
                 'vitest_named_conda_cwd': 'web/owned.test.ts',
                 'expanded_report': 'tests/test_owned.py::test_owned'}[target_kind]
    if target_kind == 'node':
        command = './.conda/bin/python -m pytest -q tests/test_owned.py::test_owned --junit-prefix owned'
    elif target_kind == 'cwd_node':
        # Keep test-body cwd semantics while exercising an explicit shell transition.
        command = 'cd tests/.. && ./.conda/bin/python -m pytest -q tests/test_owned.py::test_owned'
    elif target_kind == 'env_node':
        command = "PYTEST_ADDOPTS='--junit-xml report.xml' ./.conda/bin/python -m pytest -q tests/test_owned.py::test_owned"
    elif target_kind == 'delimited_node':
        command = './.conda/bin/python -m pytest -q -- tests/test_owned.py::test_owned'
    elif target_kind == 'conda_node':
        import shutil
        conda = shutil.which('conda')
        assert conda, 'the trusted test environment must provide Conda'
        (root / '.conda').unlink()
        (root / '.conda/conda-meta').mkdir(parents=True)
        (root / '.conda/conda-meta/history').write_text('')
        (root / '.conda/bin').mkdir()
        (root / '.conda/bin/python').symlink_to(sys.executable)
        command = shlex.join([conda, 'run', '-p', './.conda',
                              'python', '-m', 'pytest', '-q', '--', 'tests/test_owned.py::test_owned'])
    elif target_kind == 'nested_node':
        proof = root / 'tests/test_owned.py'
        proof.write_text(proof.read_text().replace('Path("', 'Path("../'))
        command = 'cd tests && ../.conda/bin/python -m pytest -q test_owned.py::test_owned'
    elif target_kind == 'expanded_report':
        monkeypatch.setenv('OWNED_REPORT', 'owned report.log')
        proof = root / 'tests/test_owned.py'
        proof.write_text(proof.read_text().replace('from pathlib import Path',
            'from pathlib import Path\nimport os\nassert Path(os.environ["OWNED_REPORT"]).is_file()'))
        command = './.conda/bin/python -m pytest -q tests/test_owned.py::test_owned --log-file "$OWNED_REPORT"'
    marker = ExecutionMarker(tmp_path / 'vitest-executed')
    vitest_source = 'tests/owned.check.ts' if target_kind == 'vitest_configured_filter' else 'tests/owned.test.ts'
    if target_kind in {'vitest_conda_cwd', 'vitest_shell_cwd', 'vitest_named_conda_cwd'}:
        (root / 'web/tests').mkdir(parents=True)
        vitest_source = 'web/tests/owned.test.ts'
    if target_kind == 'directory':
        config.gates.steps[0].targets = [reference]
    elif target_kind in {'vitest_file', 'vitest_selector', 'vitest_basename', 'vitest_filter', 'vitest_configured_filter',
                         'vitest_redirect', 'vitest_conda_cwd', 'vitest_shell_cwd', 'vitest_named_conda_cwd'}:
        from test_vitest_selector_execution import _prepare_real_vitest
        _prepare_real_vitest(root, monkeypatch)
        if target_kind == 'vitest_configured_filter':
            # Model a local installation with writable bundler scratch while
            # reusing the provisioned packages without changing their bytes.
            modules = root / 'node_modules'
            packages = modules.resolve()
            modules.unlink()
            modules.mkdir()
            for package in packages.iterdir():
                if package.name != '.vite-temp':
                    (modules / package.name).symlink_to(package, target_is_directory=package.is_dir())
            (root / 'vitest.config.js').write_text('export default ' + json.dumps(
                {'test': {'include': ['tests/*.check.ts'], 'exclude': ['**/control*']}}) + ';\n')
        (root / vitest_source).write_text(
            'import { test, expect } from "vitest";\n'
            'import { readFileSync } from "node:fs";\n'
            'test("owned value", async () => {\n'
            f'  const value = readFileSync({json.dumps("../value.py" if target_kind in {"vitest_conda_cwd", "vitest_shell_cwd", "vitest_named_conda_cwd"} else "value.py")}, "utf8");\n'
            f'  await {marker.javascript_source("value", append=True)};\n'
            '  expect(value).toBe("VALUE = 1\\n");\n'
            '});\n')
        config.gates.steps[0].runner = 'vitest'
        config.gates.steps[0].targets = [reference]
        config.gates.steps[0].args = ['--reporter=json', '--maxWorkers=1']
    elif reference_kind != 'proof':
        config.gates.steps = []
    if command_only_target:
        config.gates.steps = []
        if target_kind == 'directory':
            command = './.conda/bin/python -m pytest -q tests --junitxml report.xml'
        elif target_kind in {'vitest_file', 'vitest_selector', 'vitest_basename', 'vitest_filter', 'vitest_configured_filter',
                         'vitest_redirect', 'vitest_conda_cwd', 'vitest_shell_cwd', 'vitest_named_conda_cwd'}:
            launcher = 'npm exec --' if command_source == 'manual' else 'npx --no-install'
            selector = reference if target_kind in {'vitest_basename', 'vitest_filter', 'vitest_configured_filter',
                         'vitest_redirect', 'vitest_conda_cwd', 'vitest_shell_cwd', 'vitest_named_conda_cwd'} else 'tests/owned.test.ts'
            command = launcher + ' vitest run ' + selector + ' --reporter=json --maxWorkers=1'
            if target_kind in {'vitest_redirect', 'vitest_conda_cwd', 'vitest_shell_cwd', 'vitest_named_conda_cwd'}:
                command = launcher + ' vitest run owned.test.ts --reporter=json --maxWorkers=1 --outputFile=report.json'
            if target_kind == 'vitest_redirect':
                (root / 'run.log').write_text('ambient log must survive\n')
                command += ' > run.log 2>&1'
            elif target_kind == 'vitest_shell_cwd':
                command = 'cd web && ' + command
            elif target_kind == 'vitest_named_conda_cwd':
                import hashlib
                import shutil
                conda = shutil.which('conda')
                assert conda, 'the trusted test environment must provide Conda'
                info = subprocess.run([conda, 'info', '--json'], capture_output=True, text=True, check=True)
                info = json.loads(info.stdout)
                environments = [Path(value) for value in info['envs']
                                if Path(value).parent in map(Path, info['envs_dirs'])
                                and (Path(value) / 'conda-meta/history').is_file()]
                assert environments, 'a provisioned named Conda environment is required'
                shared_environment = environments[0]
                assert not shared_environment.is_relative_to(root)
                def environment_snapshot():
                    return {path.relative_to(shared_environment).as_posix(): (
                        path.lstat().st_mode,
                        str(path.readlink()) if path.is_symlink() else
                        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
                        for path in shared_environment.rglob('*')}
                shared_before = environment_snapshot()
                if active_conda:
                    monkeypatch.setenv('CONDA_PREFIX', str(shared_environment))
                    monkeypatch.setenv('CONDA_SHLVL', '1')
                    # Real installed Conda reactivation succeeds without a
                    # CONDA_PREFIX delta; the session must still resolve it.
                    activation = subprocess.run([conda, 'shell.posix', 'activate', shared_environment.name],
                                                capture_output=True, text=True, check=True)
                    assert not any(line.startswith('export CONDA_PREFIX=')
                                   for line in activation.stdout.splitlines())
                selector = (['-n', shared_environment.name] if command_source == 'legacy' else
                            ['--name', shared_environment.name] if command_source == 'manual' else
                            ['--name=' + shared_environment.name])
                command = shlex.join([conda, 'run', '--cwd', 'web', *selector]) + ' ' + command
            elif target_kind == 'vitest_conda_cwd':
                import shutil
                conda = shutil.which('conda')
                assert conda
                (root / '.conda').unlink()
                (root / '.conda/conda-meta').mkdir(parents=True)
                (root / '.conda/conda-meta/history').write_text('')
                (root / '.conda/bin').mkdir()
                (root / '.conda/bin/python').symlink_to(sys.executable)
                command = shlex.join([conda, 'run', '--cwd', 'web', '-p', './.conda']) + ' ' + command
            if target_kind == 'vitest_selector':
                command += ' -t ' + shlex.quote('owned value')
    config.gates.commands = []
    config.gates.parallel_groups = []
    if target_kind == 'empty_fix':
        child.fix_verify_command = command
    elif command_source == 'legacy':
        config.gates.commands = [command]
    elif command_source == 'manual':
        config.gates.parallel_groups = [GateParallelGroup(name='manual', commands=[command])]
    else:
        child.fix_verify_command = command
    plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Executable owned proof',
                      'requirement_ids': ['REQ-owned'], 'verification_refs': [reference] if reference else []}],
            'verification_steps': [step.to_dict() for step in config.gates.steps]}
    _retain_contract(root, child, config, plan)
    ambient = {path: (root / path).read_bytes() for path in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    if target_kind == 'vitest_redirect':
        ambient['run.log'] = (root / 'run.log').read_bytes()
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    binding = saved.verification_binding
    if reference:
        assert binding['required_references'][reference]['kind'] == (
            reference_kind if reference_kind in {'proof', 'command'} else 'selector')
    else:
        assert binding['required_references'] == {}
        assert binding['fix_verify_command'] == command
    if command_only_target:
        assert binding['required_proof_ids'] == []
        assert binding['proof_graph']['gates']['steps'] == []
        assert binding['proof_graph']['commands'][command][0]['task_id'] == 'task-owned'
        if command_source != 'fix':
            assert command in binding['required_commands']
    elif reference_kind in {'directory', 'vitest_file'}:
        assert binding['required_proof_ids'] == ['owned.contract']
        assert binding['proof_owners']['owned.contract'][0]['task_id'] == 'task-owned'
    if target_kind in {'vitest_file', 'vitest_selector', 'vitest_basename', 'vitest_filter', 'vitest_configured_filter',
                         'vitest_redirect', 'vitest_conda_cwd', 'vitest_shell_cwd', 'vitest_named_conda_cwd'}:
        assert 'VALUE = 1' in marker.read_text().splitlines(), 'the retained Vitest proof must execute'
        assert vitest_source in binding['proof_sources']
    if target_kind == 'vitest_named_conda_cwd':
        assert environment_snapshot() == shared_before
        assert binding['fix_verify_command'] == child.fix_verify_command
    assert binding['task_ids'] == ['task-owned']
    assert binding['requirement_ids'] == ['REQ-owned']
    assert any(entry.get('result') == 'pass' for entry in saved.execution_log)
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('edit', ['delete', 'skip', 'replace', 'independent_addition'])
def test_public_resume_protects_default_target_sources(tmp_path, monkeypatch, edit):
    from auto_agents.config import load_task_plan
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
    config = load_project_config(root)
    config.gates.steps[0].targets = []
    plan = load_task_plan(root)
    plan['tasks'][0]['verification_refs'] = ['owned.contract']
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        proof = request.cwd / 'tests/test_owned.py'
        if edit == 'delete':
            proof.unlink()
        elif edit == 'skip':
            # Only the temporary adversarial proof is marked; this regression
            # must execute and reject the mutation before gate dispatch.
            proof.write_text('from pytest import mark\npytestmark = mark.skip\n' + proof.read_text())
        elif edit == 'replace':
            proof.write_text('def test_owned(): assert True\n')
        else:
            proof.write_text(proof.read_text() + '\ndef test_independent(): assert 2 + 2 == 4\n')
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    if edit == 'independent_addition':
        assert saved.status == 'completed', saved.to_dict()
        assert dispatched
    else:
        assert saved.status != 'completed', saved.to_dict()
        assert dispatched == [], 'retained source checks must precede collection and certificates'
        diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                          if entry.get('diagnostic', {}).get('verification_ref') == 'tests/test_owned.py')
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['owners'][0]['task_id'] == 'task-owned'
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
    assert 'tests/test_owned.py' in saved.verification_binding['proof_sources']
    assert 'tests/test_control.py' in saved.verification_binding['proof_sources']


@pytest.mark.parametrize('shape', ['whole_file', 'directory', 'deduplicated',
                                  'policy_blocking', 'policy_deferred'])
@pytest.mark.parametrize('contract', ['changed', 'missing', 'unchanged',
    'receipt_changed', 'receipt_missing', 'delivered_changed', 'delivered_missing',
    'completed_changed', 'completed_missing'])
def test_public_resume_validates_contract_owners_after_command_expansion(tmp_path, monkeypatch, shape, contract):
    from auto_agents.config import load_task_plan, requirements_trace_path
    from auto_agents.requirements import requirement_contract_sha256
    import auto_agents.session as session_module

    if shape.startswith('policy_'):
        prefix, _, change = contract.rpartition('_')
        _assert_receipt_policy_mismatch(tmp_path, monkeypatch, shape.removeprefix('policy_'),
            change, boundary={'delivered': 'delivery', 'completed': 'completed'}.get(prefix, 'verification'),
            switch_before=not prefix)
        return
    root, child = project(tmp_path)
    config = load_project_config(root)
    config.gates.steps[0].targets = ['tests' if shape == 'directory' else 'tests/test_owned.py']
    config.gates.steps[0].max_batches = 1
    if shape == 'deduplicated':
        config.gates.steps.append(VerificationStep(
            proof_id='owned.second', runner='pytest', targets=['tests/test_owned.py'], levels=['release']))
    rows = [{'id': 'REQ-owned', 'text': 'Repair the owned value', 'source': 'spec'}]
    plan = load_task_plan(root)
    if shape == 'deduplicated':
        rows.append({'id': 'REQ-second', 'text': 'Retain the second contract', 'source': 'spec'})
        plan['tasks'].append({'task_id': 'task-second', 'title': 'Second owned contract',
                             'requirement_ids': ['REQ-second'], 'verification_refs': ['owned.second']})
    for task, row in zip(plan['tasks'], rows):
        task['requirement_proofs'] = [{'requirement_id': row['id'],
            'requirement_contract_sha256': requirement_contract_sha256(row),
            'evidence_refs': task['verification_refs']}]
    trace = requirements_trace_path(root)
    trace.write_text(json.dumps({'requirements': rows}))
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    expected_task = plan['tasks'][-1]['task_id']
    expected_requirement = rows[-1]['id']
    recovering = contract.startswith(('receipt_', 'delivered_', 'completed_'))
    if recovering:
        import auto_agents.session_candidate as candidate
        with monkeypatch.context() as interrupt:
            if contract.startswith('delivered_'):
                deliver = candidate.deliver_candidate
                def stop(session, state, message):
                    deliver(session, state, message)
                    raise KeyboardInterrupt()
                interrupt.setattr(candidate, 'deliver_candidate', stop)
            elif contract.startswith('receipt_'):
                def stop(self, state):
                    raise KeyboardInterrupt()
                interrupt.setattr(Session, '_run_session_persistence_action', stop)
            paused, calls, _ = run_session(root, interrupt)
        assert paused.status == ('completed' if contract.startswith('completed_') else 'paused')
        assert calls == ['fix']
        if contract.startswith('completed_'):
            assert any(entry.get('action') == 'receipt_completion' for entry in paused.execution_log)
        assert any(entry.get('action') == 'receipt_verification' and entry['verification']['ok']
                   for entry in paused.execution_log)
        from copy import deepcopy
        custody = deepcopy(paused.candidate_custody)
        attempts = paused.current_attempt
        ambient = _switch_ambient_binding_plan(root)
        contract = contract.split('_', 1)[1]
        def forbidden(*args, **kwargs):
            pytest.fail('Changed receipt authority must block before baseline, verification or delivery')
        monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
        monkeypatch.setattr(Session, '_run_verify', forbidden)
        monkeypatch.setattr(candidate, 'deliver_candidate', forbidden)
    if contract == 'changed':
        rows[-1]['text'] = 'A different requirement'
    elif contract == 'missing':
        rows.pop()
    trace.write_text(json.dumps({'requirements': rows}))
    dispatched, selected = [], []
    execute, resolve = session_module.run_gate_plan, Session._session_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    def observe_plan(self, scope):
        plan = resolve(self, scope)
        selected.append(plan)
        return plan
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    monkeypatch.setattr(Session, '_session_gate_plan', observe_plan)
    saved, _, _ = run_session(root, monkeypatch)
    assert selected
    assert all('::test_owned' not in command for plan in selected for command in plan.commands)
    if shape == 'deduplicated':
        assert all(len(plan.commands) == 1 for plan in selected)
        assert all(set(plan.metadata[plan.commands[0]].proof_ids) == {'owned.contract', 'owned.second'}
                   for plan in selected)
    if contract == 'unchanged':
        assert saved.status == 'completed', saved.to_dict()
        assert dispatched
    else:
        assert saved.status != 'completed', saved.to_dict()
        assert dispatched == [], 'contract ownership must be checked before collection or cached success'
        diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                          if entry.get('diagnostic', {}).get('requirement_id') == expected_requirement)
        assert diagnostic['task_id'] == expected_task
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
    if recovering:
        for _ in range(2):
            saved, calls, _ = run_session(root, monkeypatch)
            assert saved.status == 'blocked' and calls == []
            assert saved.candidate_custody == custody
            assert saved.current_attempt == attempts
        assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('prerequisite', ['candidate_failure', 'unavailable', 'passing'])
def test_public_resume_derived_release_test_retains_prerequisites(tmp_path, monkeypatch, prerequisite):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'setup-executions')
    (root / 'tests/test_release.py').write_text('def test_release(): assert True\n')
    (root / 'tests/test_setup.py').write_text(
        'from pathlib import Path\ndef test_setup():\n'
        '    ' + marker.source('Path("value.py").read_text()', append=True) + '\n'
        + ('    assert "VALUE = 0" in Path("value.py").read_text()\n'
           if prerequisite == 'candidate_failure' else '    assert True\n'))
    config = load_project_config(root)
    config.gates.release_blocking_paths = []
    config.gates.unmapped_change_policy = 'fallback'
    config.gates.fallback_proof_ids = []
    config.gates.steps.append(VerificationStep(
        proof_id='release.regression', runner='pytest', targets=['tests/test_release.py'],
        levels=['release'], depends_on_proofs=['regression.setup']))
    if prerequisite != 'unavailable':
        config.gates.steps.append(VerificationStep(
            proof_id='regression.setup', runner='pytest', targets=['tests/test_setup.py'], levels=['release']))
    plan = load_task_plan(root)
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    observed = []
    resolve = Session._session_gate_plan
    def observe(self, scope):
        selected = resolve(self, scope)
        if self._current_state.candidate_paths:
            observed.append(selected)
        return selected
    monkeypatch.setattr(Session, '_session_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        proof = request.cwd / 'tests/test_release.py'
        proof.write_text(proof.read_text() + '\ndef test_independent(): assert 1 + 1 == 2\n')
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    if prerequisite == 'unavailable':
        assert saved.status != 'completed', saved.to_dict()
        assert not marker.exists()
        diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                          if 'unavailable prerequisite regression.setup' in str(entry))
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
    else:
        assert observed
        assert all('regression.setup' in plan.proof_ids for plan in observed)
        assert any(key.startswith('affected.changed-test.') for plan in observed for key in plan.proof_ids)
        assert 'VALUE = 1' in marker.read_text()
        if prerequisite == 'candidate_failure':
            assert saved.status != 'completed', saved.to_dict()
            assert 'VALUE = 0' in marker.read_text(), 'saved baseline must execute the same prerequisite'
        else:
            assert saved.status == 'completed', saved.to_dict()


@pytest.mark.parametrize('escalation', ['critical', 'unmapped', 'blocking', 'full_verify'])
def test_public_resume_escalation_retains_failing_affected_regression_and_setup(tmp_path, monkeypatch, escalation):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    markers = {key: ExecutionMarker(tmp_path / key) for key in ('setup', 'regression', 'release')}
    (root / 'tests/test_regression.py').write_text(
        'from pathlib import Path\ndef test_setup():\n'
        '    ' + markers['setup'].source(repr('ran')) + '\n'
        'def test_regression():\n'
        '    ' + markers['regression'].source(repr('ran')) + '\n'
        '    assert "VALUE = 0" in Path("value.py").read_text()\n'
        'def test_release():\n'
        '    ' + markers['release'].source(repr('ran')) + '\n')
    config = load_project_config(root)
    config.gates.release_blocking_paths = ['value.py'] if escalation == 'blocking' else []
    config.gates.unmapped_change_policy = 'release'
    config.gates.steps[0].impact_paths = []
    config.gates.steps[0].risk = 'critical' if escalation == 'critical' else 'medium'
    config.gates.steps.extend([
        VerificationStep(proof_id='affected.regression', runner='pytest', levels=['affected'],
            targets=['tests/test_regression.py::test_regression'], impact_paths=['value.py'],
            depends_on_proofs=['regression.setup']),
        VerificationStep(proof_id='regression.setup', runner='pytest', levels=['affected'],
            targets=['tests/test_regression.py::test_setup']),
        VerificationStep(proof_id='release.gate', runner='pytest', levels=['release'],
            targets=['tests/test_regression.py::test_release']),
    ])
    plan = load_task_plan(root)
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    observed = []
    resolve = Session._session_gate_plan
    def observe(self, scope):
        if escalation == 'full_verify':
            self._full_verify = True
        selected = resolve(self, scope)
        if self._current_state.candidate_paths:
            observed.append(selected)
        return selected
    monkeypatch.setattr(Session, '_session_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        if escalation == 'unmapped':
            (request.cwd / 'unmapped.txt').write_text('additional owned change')
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    assert saved.status != 'completed', saved.to_dict()
    assert observed
    for selected in observed:
        assert {'owned.contract', 'affected.regression', 'regression.setup', 'release.gate'} <= set(selected.proof_ids)
    assert all(path.read_text() == 'ran' for path in markers.values())
    assert any('test_regression' in str(entry) for entry in saved.execution_log)


@pytest.mark.parametrize('reference', ['node', 'proof_id'])
@pytest.mark.parametrize('shape', ['prerequisite', 'direct-impact', 'default-target-release'])
def test_public_resume_preserves_existing_foreign_pending_regression_prerequisite(tmp_path, monkeypatch, shape, reference):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'foreign-prerequisite-executed')
    (root / 'tests/test_shared.py').write_text(
        'from pathlib import Path\ndef test_shared():\n'
        '    ' + marker.source(repr('ran')) + '\n'
        '    assert "VALUE = 0" in Path("value.py").read_text()\n'
        'def test_regression():\n    assert True\n')
    config = load_project_config(root)
    config.gates.release_blocking_paths = []
    linked = shape != 'direct-impact'
    default_release = shape == 'default-target-release'
    if default_release:
        config.gates.steps[0].risk = 'critical'
    config.gates.steps.extend([
        VerificationStep(proof_id='affected.regression', runner='pytest',
            levels=['release'] if default_release else ['affected'],
            targets=[] if default_release else ['tests/test_shared.py::test_regression'],
            args=['-k', 'regression'] if default_release else [],
            impact_paths=[] if default_release else ['value.py'],
            depends_on_proofs=['foreign.check'] if linked else []),
        VerificationStep(proof_id='foreign.check', runner='pytest', levels=['affected'],
            targets=['tests/test_shared.py::test_shared'], impact_paths=['shared.py'] if linked else ['value.py']),
    ])
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Foreign pending work', 'status': 'pending', 'workflow_id': 'foreign-workflow',
        'verification_refs': ['foreign.check' if reference == 'proof_id' else 'tests/test_shared.py::test_shared'],
        'requirement_ids': ['REQ-foreign']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    ambient = {path: (root / path).read_bytes() for path in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    saved, _, _ = run_session(root, monkeypatch)
    assert saved.status != 'completed', saved.to_dict()
    assert marker.read_text() == 'ran'
    assert saved.verification_binding['regression_dependencies']['affected.regression'] == (['foreign.check'] if linked else [])
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('history', ['retained', 'unavailable', 'missing_plan', 'unmatched_requirement'])
def test_public_resume_before_first_baseline_uses_child_history(tmp_path, monkeypatch, history):
    from test_engine_child_recovery import parent_workflow, resume_to_observation

    root, child = project(tmp_path)
    if history == 'missing_plan':
        git(root, 'rm', '.auto-agents/state/task_plan.json')
        git(root, 'commit', '-m', 'retain legacy revision without task history')
    if history == 'unmatched_requirement':
        issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
        issue.write_text(json.dumps({'requirement_ids': ['REQ-unresolved']}))
    # Legacy failures are reopened by the public resume protocol; retain the
    # pre-baseline blocker without inventing an already captured baseline.
    child.status = 'failed'
    child.resolution = 'verification_ownership'
    child.baseline_git_ref = child.baseline_head_ref = ''
    child.lineage_head_ref = 'refs/missing/child-history' if history == 'unavailable' else head_ref(root)
    child.current_attempt = 0
    store, snapshot, handoff = parent_workflow(root, child)
    if history == 'unmatched_requirement':
        handoff.payload.pop('task_id')
        store.save_handoff(handoff)
    store.record_result(snapshot, handoff, status='failed',
                        result={'status': 'failed', 'resolution': child.resolution})
    store.consume_result(snapshot, handoff, operation_id='pre-baseline-failure')
    resumed_handoff = store.prepare_handoff(snapshot, parent=snapshot.root, target='resume',
        goal=child.goal, reason='Retry the retained child',
        payload={'resume_handoff_id': handoff.handoff_id})
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = resumed_handoff.handoff_id
    save_session_state(root, parent)
    # Stop only after production handoff consumption returns to the parent;
    # the parent's next implementation cycle is outside this child proof.
    from test_engine_child_recovery import ObservationBoundary
    collab_loop = Session._phase_collab_loop
    def parent_boundary(self, state):
        if state.session_id == 'parent':
            raise ObservationBoundary()
        return collab_loop(self, state)
    monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)
    config = load_project_config(root)
    config.gates.steps = [VerificationStep(
        proof_id='foreign.future', runner='pytest', targets=['tests/test_future.py::test_future'],
        levels=['affected', 'release'], impact_paths=['**'])]
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-foreign', 'title': 'Other pending work',
        'workflow_id': 'foreign-workflow', 'status': 'pending',
        'verification_refs': ['tests/test_future.py::test_future']}],
        'verification_steps': [step.to_dict() for step in config.gates.steps]})
    before_head = head_ref(root)
    ambient = {name: (root / name).read_bytes() for name in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    calls = []
    dispatched = []
    import auto_agents.session as session_module
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    def provider(state, prompt, candidate_root):
        calls.append(state.session_id)
        (candidate_root / 'value.py').write_text('VALUE = 1\n')
        return 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
    resume_to_observation(root, monkeypatch, provider)
    saved = load_session_state(root, child.session_id)
    assert {name: (root / name).read_bytes() for name in ambient} == ambient
    assert store.load_handoff(handoff.handoff_id).child.native_id == child.session_id
    if history == 'retained':
        assert saved.status == 'completed', saved.to_dict()
        assert calls == [child.session_id]
        assert saved.verification_binding['tasks'][0]['task_id'] == 'task-owned'
        assert saved.verification_binding['required_proof_ids'] == ['owned.contract']
        assert saved.verification_binding['contract_revision'] == before_head
    else:
        assert saved.status == 'blocked', saved.to_dict()
        assert calls == []
        assert dispatched == [], 'unresolved ownership must block before baseline execution'
        diagnostic = saved.execution_log[-1]['diagnostic']
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['handoff_id'] == child.parent_handoff_id
        assert diagnostic['retry_fix'] is False
        if history == 'unmatched_requirement':
            assert diagnostic['missing_requirement_ids'] == ['REQ-unresolved']
        if history == 'missing_plan':
            assert 'retained task plan ownership is unavailable' in saved.execution_log[-1]['result']
        assert head_ref(root) == before_head


@pytest.mark.parametrize('source', ['legacy', 'explicit_fix'])
@pytest.mark.parametrize('target', ['implicit', 'directory', 'implicit_config', 'implicit_override',
                                    'implicit_toml', 'implicit_pyproject', 'implicit_addopts',
                                    'implicit_addopts_toml', 'implicit_addopts_override',
                                    'inline_addopts', 'inline_env_wrapper', 'inline_override',
                                    'inline_config'])
@pytest.mark.parametrize('edit', ['delete', 'skip', 'replace', 'independent_addition'])
def test_public_resume_protects_command_discovered_sources(tmp_path, monkeypatch, source, target, edit):
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
    proof_path = 'tests/test_owned.py'
    control_path = 'tests/test_control.py'
    custom_discovery = target in {'implicit_config', 'implicit_override', 'implicit_toml', 'implicit_pyproject',
                                  'implicit_addopts', 'implicit_addopts_toml', 'implicit_addopts_override',
                                  'inline_addopts', 'inline_env_wrapper', 'inline_override', 'inline_config'}
    if custom_discovery:
        proof_path, control_path = 'check_owned.py', 'check_control.py'
        (root / 'tests/test_owned.py').rename(root / proof_path)
        (root / 'tests/test_control.py').rename(root / control_path)
    config = load_project_config(root)
    command = './.conda/bin/python -m pytest -q' + (' tests' if target == 'directory' else '')
    if target == 'implicit_config':
        (root / 'checks.ini').write_text('[pytest]\npython_files = check_*.py\n')
        command += ' -c checks.ini' if source == 'legacy' else ' --config-file=checks.ini'
    elif target in {'implicit_toml', 'implicit_pyproject'}:
        name = 'pytest.toml' if target == 'implicit_toml' else 'pyproject.toml'
        section = '[pytest]' if target == 'implicit_toml' else '[tool.pytest]'
        (root / name).write_text(section + '\npython_files = ["check_*.py"]\n')
    elif target == 'implicit_override':
        command += " -o 'python_files=check_*.py'" if source == 'legacy' else " --override-ini='python_files=check_*.py'"
    elif target == 'implicit_addopts_toml':
        (root / 'pytest.toml').write_text('[pytest]\naddopts = ["-o", "python_files=check_*.py"]\n')
    elif target in {'implicit_addopts', 'implicit_addopts_override'}:
        pattern = 'excluded_*.py' if target == 'implicit_addopts_override' else 'check_*.py'
        (root / 'pytest.ini').write_text('[pytest]\naddopts = -o python_files=' + pattern + '\n')
        if target == 'implicit_addopts_override':
            command += " --override-ini='python_files=check_*.py'"
    elif target.startswith('inline_'):
        # Environment discovery takes precedence over retained addopts, and
        # explicit CLI overrides still take precedence over the environment.
        (root / 'pytest.ini').write_text('[pytest]\naddopts = -o python_files=excluded_*.py\n')
        options = '-o python_files=check_*.py'
        if target == 'inline_override':
            options = '-o python_files=excluded_*.py'
            command += " -o 'python_files=check_*.py'"
        elif target == 'inline_config':
            (root / 'checks.ini').write_text('[pytest]\npython_files = check_*.py\n')
            options = '-c checks.ini'
        command = 'PYTEST_ADDOPTS=' + shlex.quote(options) + ' ' + command
        if target == 'inline_env_wrapper':
            command = 'env ' + command
    config.gates.steps = []
    config.gates.commands = [command] if source == 'legacy' else []
    child.fix_verify_command = command if source == 'explicit_fix' else ''
    plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Owned check',
                      'requirement_ids': ['REQ-owned'], 'verification_refs': ['cmd:' + command]}]}
    _retain_contract(root, child, config, plan)
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        proof = request.cwd / proof_path
        if edit == 'delete':
            proof.unlink()
        elif edit == 'skip':
            proof.write_text('from pytest import mark\npytestmark = mark.skip\n' + proof.read_text())
        elif edit == 'replace':
            proof.write_text('def test_owned(): assert True\n')
        else:
            proof.write_text(proof.read_text() + '\ndef test_independent(): assert 3 * 3 == 9\n')
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    if edit == 'independent_addition':
        assert saved.status == 'completed', saved.to_dict()
        assert dispatched
    else:
        assert saved.status != 'completed', saved.to_dict()
        assert dispatched == [], 'source checks must precede collection, execution and certificate lookup'
        diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                          if entry.get('diagnostic', {}).get('verification_ref') == proof_path)
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['owners'][0]['task_id'] == 'task-owned'
        assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
    assert {proof_path, control_path} <= saved.verification_binding['proof_sources'].keys()
    assert 'value.py' not in saved.verification_binding['proof_sources']


@pytest.mark.parametrize('hook', ['skip', 'deselect'])
@pytest.mark.parametrize('control', ['conftest.py', 'tests/conftest.py'])
@pytest.mark.parametrize('existing', [False, True])
def test_public_resume_rejects_pytest_control_file_weakening(tmp_path, monkeypatch, hook, control, existing):
    from auto_agents.config import load_task_plan
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    if existing:
        (root / control).write_text('# Retained pytest control file\n')
    config = load_project_config(root)
    _retain_contract(root, child, config, load_task_plan(root))
    original_proof = (root / 'tests/test_owned.py').read_bytes()
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        source = 'from pytest import mark\ndef pytest_collection_modifyitems(items):\n'
        if hook == 'skip':
            source += '    for item in items:\n        item.add_marker(mark.skip)\n'
        else:
            source += '    items[:] = []\n'
        (request.cwd / control).write_text(source)
        assert (request.cwd / 'tests/test_owned.py').read_bytes() == original_proof
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    assert saved.status != 'completed', saved.to_dict()
    assert dispatched == [], 'control-file validation must precede collection and certificate acceptance'
    diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                      if entry.get('diagnostic', {}).get('verification_ref') == control)
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['owners'][0]['task_id'] == 'task-owned'
    assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
    assert diagnostic['contract_fingerprint']
    assert diagnostic['retry_fix'] is False


@pytest.mark.parametrize('passing_proof', [False, True])
@pytest.mark.parametrize('reference', ['owned.contract', 'owned.report.json', 'tests/test_owned.py::test_owned',
                                     'tests/owned.test.ts'])
@pytest.mark.parametrize('command_source', ['none', 'legacy', 'manual', 'fix'])
@pytest.mark.parametrize('runner,report_option', [
    ('pytest', '--junitxml'), ('pytest', '--junit-xml'), ('pytest', '--junit-prefix'),
    ('pytest', '--override-ini'), ('pytest', '--unknown-plugin-option'), ('vitest', '--outputFile'),
    ('pytest', '-r'), ('pytest', '>'), ('pytest', '2>'),
])
def test_public_resume_blocks_unresolved_owned_proof_id(tmp_path, monkeypatch, passing_proof, reference, command_source, runner, report_option, source_case='absent'):
    import auto_agents.session as session_module
    from auto_agents.models import GateParallelGroup

    root, child = project(tmp_path)
    config = load_project_config(root)
    config.gates.steps = []
    config.gates.commands = []
    if passing_proof:
        (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
        config.gates.steps = [VerificationStep(proof_id='unrelated.control', runner='pytest',
            targets=['tests/test_control.py'], levels=['affected', 'release'], impact_paths=['**'])]
    if command_source != 'none':
        # Supply a valid local conda prefix so explicit-command environment
        # admission cannot substitute for the missing-reference diagnostic.
        (root / '.conda').unlink()
        (root / '.conda/conda-meta').mkdir(parents=True)
        (root / '.conda/bin').mkdir()
        (root / '.conda/bin/python').symlink_to(sys.executable)
        (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
        command = 'conda run -p ./.conda python -m pytest -q tests/test_control.py ' + report_option + ' ' + reference
        if runner == 'vitest':
            command = 'npx --no-install vitest run tests/control.test.ts ' + report_option + ' ' + reference
            if source_case != 'absent':
                from test_vitest_selector_execution import _prepare_real_vitest
                _prepare_real_vitest(root, monkeypatch)
                owned_source = 'tests/' + reference + '.test.ts'
                (root / owned_source).write_text('import { test, expect } from "vitest";\n'
                    'test("owned failure", () => expect(false).toBe(true));\n')
                (root / 'tests/control.test.ts').write_text('import { test, expect } from "vitest";\n'
                    'test("passing control", () => expect(true).toBe(true));\n')
                if source_case == 'config_excluded':
                    (root / 'vitest.config.js').write_text('export default ' + json.dumps(
                        {'test': {'exclude': [owned_source]}}) + ';\n')
                else:
                    pattern = '**/' + reference + '.*' if source_case == 'excluded_glob' else owned_source
                    command += (' --exclude=' if source_case == 'excluded_equals' else ' --exclude ') + shlex.quote(pattern)
                command += ' --reporter=json'
        if command_source == 'legacy':
            config.gates.commands = [command]
        elif command_source == 'manual':
            config.gates.parallel_groups = [GateParallelGroup(name='manual', commands=[command])]
        else:
            child.fix_verify_command = command
    plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Missing owned proof',
                      'requirement_ids': ['REQ-owned'], 'verification_refs': [reference]}],
            'verification_steps': [step.to_dict() for step in config.gates.steps]}
    _retain_contract(root, child, config, plan)
    if source_case == 'config_excluded':
        # Ambient discovery would admit this reference. Recovery must use the
        # retained exclusion, and leave the other workflow's config intact.
        (root / 'vitest.config.js').write_text('export default {test: {exclude: []}};\n')
    ambient = {path: (root / path).read_bytes() for path in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    if source_case != 'absent':
        ambient.update({path: (root / path).read_bytes() for path in
                        ('.git/index', owned_source, 'tests/control.test.ts')})
    if source_case == 'config_excluded':
        ambient['vitest.config.js'] = (root / 'vitest.config.js').read_bytes()
    def reject_baseline(*args, **kwargs):
        pytest.fail('Unresolved references must be rejected before baseline admission')
    monkeypatch.setattr(Session, '_ensure_baseline', reject_baseline)
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'blocked', saved.to_dict()
    assert calls == []
    assert dispatched == [], 'missing proof definitions must block before baseline or certificate lookup'
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['verification_ref'] == reference
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['owners'][0]['task_id'] == 'task-owned'
    assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
    assert diagnostic['contract_fingerprint']
    assert diagnostic['retry_fix'] is False
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('command_source', ['legacy', 'manual', 'fix'])
@pytest.mark.parametrize('runner,source_case', [('pytest', 'absent'), ('vitest', 'absent'),
    ('vitest', 'excluded'), ('vitest', 'excluded_equals'), ('vitest', 'excluded_glob'),
    ('vitest', 'config_excluded')])
@pytest.mark.parametrize('reference', ['owned.contract', 'owned.report.json'])
@pytest.mark.parametrize('passing_proof', [False, True])
def test_public_resume_blocks_positional_owned_proof_id(
        tmp_path, monkeypatch, command_source, runner, source_case, reference, passing_proof):
    test_public_resume_blocks_unresolved_owned_proof_id(
        tmp_path, monkeypatch, passing_proof, reference, command_source, runner, '', source_case)


@pytest.mark.parametrize('command_source,runner,report_option', [
    ('legacy', 'pytest', '--junitxml'),
    *((source, runner, '') for source in ('legacy', 'manual', 'fix') for runner in ('pytest', 'vitest')),
])
def test_public_resume_rechecks_retained_unresolved_proof_inventory(
        tmp_path, monkeypatch, command_source, runner, report_option):
    from copy import deepcopy
    import auto_agents.session_verification as verification
    from auto_agents.models import GateParallelGroup

    root, child = project(tmp_path)
    (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
    config = load_project_config(root)
    config.gates.steps = []
    command = './.conda/bin/python -m pytest -q tests/test_control.py ' + report_option + ' owned.contract'
    if runner == 'vitest':
        command = 'npx --no-install vitest run tests/control.test.ts owned.contract'
    config.gates.commands = []
    if command_source == 'legacy':
        config.gates.commands = [command]
    elif command_source == 'manual':
        config.gates.parallel_groups = [GateParallelGroup(name='manual', commands=[command])]
    else:
        child.fix_verify_command = command
    plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Retained unresolved proof',
                      'requirement_ids': ['REQ-owned'], 'verification_refs': ['owned.contract']}],
            'verification_steps': []}
    _retain_contract(root, child, config, plan)
    # Encode the old inventory, where argument equality removed the mandatory
    # ID. Restore production resolution before exercising public resume.
    covers = verification._command_covers
    classify = verification._reference_kind
    with monkeypatch.context() as legacy:
        legacy.setattr(verification, '_owned_inventory', lambda *_: ([], {}))
        legacy.setattr(verification, '_command_covers',
                       lambda command, ref: ref in shlex.split(command) or covers(command, ref))
        legacy.setattr(verification, '_reference_kind', lambda ref, gates, **kwargs:
                       'proof' if ref == 'owned.contract' else classify(ref, gates, **kwargs))
        _binding_fixture(root, child)
    retained = deepcopy(child.verification_binding)
    assert retained['required_references']['owned.contract']['kind'] == 'proof'
    assert retained['required_proof_ids'] == []
    if command_source == 'fix':
        assert retained['fix_verify_command'] == command
    else:
        assert command in retained['required_commands']
    save_session_state(root, child)
    ambient = _switch_ambient_binding_plan(root)
    def reject_execution(*args, **kwargs):
        pytest.fail('Unresolved retained proof must block before baseline or verification')
    monkeypatch.setattr(Session, '_ensure_baseline', reject_execution)
    monkeypatch.setattr('auto_agents.session.run_gate_plan', reject_execution)
    for _ in range(2):
        saved, calls, _ = run_session(root, monkeypatch)
        assert saved.status == 'blocked' and saved.resolution == 'verification_ownership'
        assert calls == []
        # An unchanged blocked resume reuses the retained diagnostic and may
        # append a resume log entry; it need not emit the same failure again.
        diagnostic = next(entry['diagnostic'] for entry in reversed(saved.execution_log)
                          if 'diagnostic' in entry)
        assert diagnostic['verification_ref'] == 'owned.contract'
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
        assert diagnostic['owners'][0]['task_id'] == 'task-owned'
        assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
        assert saved.verification_binding == retained
        assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('control,existing,edit', [
    (control, existing, 'deselect')
    for control in ['pytest.toml', '.pytest.toml', 'pytest.ini', '.pytest.ini',
                    'pyproject.toml', 'tox.ini', 'setup.cfg', 'checks.ini']
    for existing in [False, True]
] + [(control, True, 'build_metadata') for control in ['pyproject.toml', 'tox.ini', 'setup.cfg']])
def test_public_resume_rejects_pytest_configuration_deselection(tmp_path, monkeypatch, control, existing, edit):
    from auto_agents.config import load_task_plan
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    # The unchanged whole-file command could otherwise pass only the control.
    proof = root / 'tests/test_owned.py'
    proof.write_text(proof.read_text() + '\ndef test_control(): assert True\n')
    section = '[tool.pytest.ini_options]' if control == 'pyproject.toml' else (
        '[tool:pytest]' if control == 'setup.cfg' else '[pytest]')
    if existing:
        (root / control).write_text(section + '\n')
    config = load_project_config(root)
    config.gates.steps[0].targets = ['tests/test_owned.py']
    if control == 'checks.ini':
        config.gates.steps[0].args = ['-c', control]
    plan = load_task_plan(root)
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    original = proof.read_bytes()
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        option = '--deselect=tests/test_owned.py::test_owned'
        value = json.dumps([option]) if control.endswith('.toml') else option
        if edit == 'build_metadata':
            (request.cwd / control).write_text(section + '\n[build_metadata]\nlabel = \"updated\"\n')
            (request.cwd / 'value.py').write_text('VALUE = 1\n')
        else:
            (request.cwd / control).write_text(section + '\naddopts = ' + value + '\n')
        assert (request.cwd / 'tests/test_owned.py').read_bytes() == original
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    if edit == 'build_metadata':
        assert saved.status == 'completed', saved.to_dict()
        assert dispatched
        return
    assert saved.status != 'completed', saved.to_dict()
    assert dispatched == [], 'configuration validation must precede collection, execution and cached acceptance'
    diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                      if entry.get('diagnostic', {}).get('verification_ref') == control)
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['owners'][0]['task_id'] == 'task-owned'
    assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
    assert diagnostic['contract_fingerprint']
    assert diagnostic['retry_fix'] is False


@pytest.mark.parametrize('source', ['structured', 'legacy', 'explicit_fix', 'manual'])
@pytest.mark.parametrize('selector', ['deselect', 'name_filter', 'attached_name_filter',
                                     'marker_filter', 'override', 'config', 'unfiltered',
                                     'discovery_cli', 'discovery_attached', 'discovery_long',
                                     'discovery_config', 'discovery_toml', 'discovery_addopts',
                                     'discovery_other_config', 'discovery_inclusive',
                                     'norecurse_cli', 'norecurse_config', 'norecurse_toml',
                                     'norecurse_path', 'norecurse_override', 'norecurse_explicit',
                                     'norecurse_unrelated', 'norecurse_default',
                                     'filename_default', 'filename_explicit', 'filename_config',
                                     'filename_addopts', 'filename_config_addopts_excluded',
                                     'filename_config_addopts_included',
                                     'filename_config_env_excluded', 'filename_config_env_included',
                                     'filename_config_cli_excluded', 'filename_config_cli_included',
                                     'nested_config_excluded', 'nested_config_included',
                                     'nested_config_later_excluded', 'nested_config_later_included',
                                     'nested_config_ancestor_excluded', 'nested_config_ancestor_included',
                                     'nested_config_setup_included', 'nested_config_rootdir_included',
                                     'nested_config_bare_pyproject_included',
                                     'nested_config_bare_pyproject_overridden_excluded',
                                     'nested_config_bare_pyproject_overridden_included',
                                     'inline_deselect', 'setup_only', 'setup_only_addopts',
                                     'setup_only_config', 'setup_only_env', 'setup_only_inherited', 'setup_plan',
                                     'fixtures', 'fixtures_per_test', 'funcargs', 'version',
                                     'help', 'short_help', 'setup_show'])
def test_public_resume_requires_executable_owned_node_coverage(tmp_path, monkeypatch, source, selector,
                                                             retained=False):
    """Command success cannot certify an excluded or unexecuted owned node."""
    from auto_agents.config import load_task_plan
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'required-node-executed')
    proof = root / 'tests/test_owned.py'
    proof.write_text(proof.read_text() + '    ' + marker.source(repr('owned')) + '\n'
                     '\ndef test_control(): assert True\n')
    inclusive = selector in {'unfiltered', 'setup_show', 'discovery_inclusive', 'norecurse_override',
                             'norecurse_explicit', 'norecurse_unrelated',
                             'filename_explicit', 'filename_config', 'filename_addopts'}
    inclusive |= selector.endswith('_included')
    if not inclusive:
        proof.write_text(proof.read_text().replace('def test_owned():\n',
                                                  'def test_owned():\n    assert False\n'))
    config = load_project_config(root)
    args = {
        'deselect': ['--deselect=tests/test_owned.py::test_owned'],
        'name_filter': ['-k', 'test_control'],
        'attached_name_filter': ['-ktest_control'],
        'marker_filter': ['-m', 'control'],
        'override': ['-o', 'addopts=--deselect=tests/test_owned.py::test_owned'],
        'config': ['-c', 'checks.ini'],
        'discovery_cli': ['-o', 'python_functions=test_control'],
        'discovery_attached': ['-opython_functions=test_control'],
        'discovery_long': ['--override-ini=python_functions=test_control'],
        'discovery_config': ['-c', 'checks.ini'],
        'discovery_toml': [],
        'discovery_addopts': ['-o', 'addopts=-o python_functions=test_control'],
        'discovery_other_config': ['-c', 'checks.ini'],
        'discovery_inclusive': ['-o', 'python_functions=test_*'],
        'norecurse_cli': ['-o', 'norecursedirs=owned'],
        'norecurse_config': [],
        'norecurse_toml': [],
        'norecurse_path': ['--override-ini=norecursedirs=tests/owned'],
        'norecurse_override': ['-o', 'norecursedirs='],
        'norecurse_explicit': ['-o', 'norecursedirs=owned'],
        'norecurse_unrelated': ['-o', 'norecursedirs=other'],
        'norecurse_default': [],
        'filename_default': [],
        'filename_explicit': [],
        'filename_config': [],
        'filename_addopts': [],
        'filename_config_addopts_excluded': [],
        'filename_config_addopts_included': [],
        'filename_config_env_excluded': [],
        'filename_config_env_included': [],
        'filename_config_cli_excluded': [],
        'filename_config_cli_included': [],
        'nested_config_excluded': [],
        'nested_config_included': [],
        'nested_config_later_excluded': [],
        'nested_config_later_included': [],
        'nested_config_ancestor_excluded': [],
        'nested_config_ancestor_included': [],
        'nested_config_setup_included': [],
        'nested_config_rootdir_included': ['--rootdir=.'],
        'nested_config_bare_pyproject_included': [],
        'nested_config_bare_pyproject_overridden_excluded': [],
        'nested_config_bare_pyproject_overridden_included': [],
        'inline_deselect': [],
        'setup_only': ['--setup-only'],
        'setup_only_addopts': ['-o', 'addopts=--setup-only'],
        'setup_only_config': [],
        'setup_only_env': [],
        'setup_only_inherited': [],
        'setup_only_inherited_cleared_included': [],
        'setup_plan': ['--setup-plan'],
        'fixtures': ['--fixtures'],
        'fixtures_per_test': ['--fixtures-per-test'],
        'funcargs': ['--funcargs'],
        'version': ['--version'],
        'help': ['--help'],
        'short_help': ['-h'],
        'setup_show': ['--setup-show'],
        'unfiltered': [],
    }[selector]
    if selector == 'marker_filter':
        proof.write_text('import pytest\n' + proof.read_text().replace(
            'def test_control():', '@pytest.mark.control\ndef test_control():'))
    if selector == 'config':
        (root / 'checks.ini').write_text(
            '[pytest]\naddopts = --deselect=tests/test_owned.py::test_owned\n')
    if selector == 'setup_only_config':
        (root / 'pytest.ini').write_text('[pytest]\naddopts = --setup-only\n')
    if selector in {'discovery_config', 'discovery_other_config'}:
        (root / 'checks.ini').write_text('[pytest]\npython_functions = test_control\n')
    if selector == 'discovery_toml':
        (root / 'pytest.toml').write_text('[pytest]\npython_functions = ["test_control"]\n')
    required_node = 'tests/test_owned.py::test_owned'
    target = 'tests/test_owned.py'
    if selector.startswith('setup_') or selector in {
            'fixtures', 'fixtures_per_test', 'funcargs', 'version', 'help', 'short_help'}:
        target = required_node
    if selector.startswith('filename_'):
        proof.rename(root / 'tests/check_owned.py')
        (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
        required_node = 'tests/check_owned.py::test_owned'
        target = 'tests/check_owned.py' if selector == 'filename_explicit' else 'tests'
        if selector == 'filename_config':
            (root / 'pytest.ini').write_text('[pytest]\npython_files = check_*.py test_*.py\n')
        if selector.startswith(('filename_config_addopts_', 'filename_config_env_',
                                'filename_config_cli_')):
            final_pattern = 'check_*.py test_*.py' if inclusive else 'test_*.py'
            opposite_pattern = 'test_*.py' if inclusive else 'check_*.py test_*.py'
            # Make each successive precedence layer disagree. The control
            # passes even when the required failing node is not discovered.
            config_pattern, addopts_pattern = opposite_pattern, final_pattern
            if '_env_' in selector or '_cli_' in selector:
                config_pattern, addopts_pattern = final_pattern, opposite_pattern
            (root / 'pytest.ini').write_text(
                '[pytest]\npython_files = ' + config_pattern + '\naddopts = '
                + shlex.join(['-o', 'python_files=' + addopts_pattern]) + '\n')
            if '_cli_' in selector:
                args = ['-o', 'python_files=' + final_pattern]
    if selector.startswith('norecurse_'):
        directory = 'build' if selector == 'norecurse_default' else 'owned'
        nested = root / 'tests' / directory / 'test_owned.py'
        nested.parent.mkdir()
        proof.rename(nested)
        (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
        required_node = nested.relative_to(root).as_posix() + '::test_owned'
        target = nested.relative_to(root).as_posix() if selector == 'norecurse_explicit' else 'tests'
        if selector in {'norecurse_config', 'norecurse_override'}:
            (root / 'pytest.ini').write_text('[pytest]\nnorecursedirs = owned\n')
        if selector == 'norecurse_toml':
            (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\nnorecursedirs = ["owned"]\n')
    targets = [target]
    if selector.startswith('nested_config_'):
        (root / 'tests/a').mkdir()
        (root / 'tests/b').mkdir()
        proof.rename(root / 'tests/a/test_owned.py')
        (root / 'tests/b/test_control.py').write_text('def test_control(): assert True\n')
        required_node = 'tests/a/test_owned.py::test_owned'
        targets = ['tests/a/test_owned.py', 'tests/b/test_control.py']
        selected_patterns = 'test_*' if inclusive else 'test_control'
        opposite_patterns = 'test_control' if inclusive else 'test_*'
        nested_config = root / 'tests/a/pytest.ini'
        if '_bare_pyproject_' in selector:
            (root / 'pyproject.toml').write_text('[build-system]\nrequires = []\n')
            if '_overridden_' in selector:
                (root / 'tox.ini').write_text(
                    '[pytest]\npython_functions = ' + selected_patterns + '\n')
            # The build-only root config supplies empty defaults unless a
            # substantive ancestor config wins; neither uses nested settings.
            selected_patterns = opposite_patterns
        elif '_later_' in selector:
            nested_config = root / 'tests/b/pytest.ini'
        elif '_ancestor_' in selector:
            (root / 'tests/pytest.ini').write_text(
                '[pytest]\npython_functions = ' + selected_patterns + '\n')
            selected_patterns = opposite_patterns
        elif '_setup_' in selector or '_rootdir_' in selector:
            selected_patterns = 'test_control'
            if '_setup_' in selector:
                (root / 'setup.py').write_text('# Retained project root marker.\n')
        nested_config.write_text('[pytest]\npython_functions = ' + selected_patterns + '\n')
    command = shlex.join(['./.conda/bin/python', '-m', 'pytest', '-q', *args, *targets])
    if selector.startswith('filename_config_env_'):
        command = ('PYTEST_ADDOPTS=' + shlex.quote(shlex.join(['-o', 'python_files=' + final_pattern]))
                   + ' ' + command)
        # Structured runner steps carry effective options in args; their
        # command is generated from those fields rather than shell text.
        args = ['-o', 'python_files=' + final_pattern]
    if selector == 'filename_addopts':
        command = "PYTEST_ADDOPTS='-o python_files=check_*.py' " + command
        # Structured steps express their effective runner options as args.
        args = ['-o', 'python_files=check_*.py']
    elif selector == 'inline_deselect':
        command = "PYTEST_ADDOPTS='--deselect=tests/test_owned.py::test_owned' " + command
        args = ['--deselect=tests/test_owned.py::test_owned']
    elif selector == 'setup_only_env':
        command = "PYTEST_ADDOPTS='--setup-only' " + command
        args = ['--setup-only']
    elif selector == 'setup_only_inherited_cleared_included':
        command = "PYTEST_ADDOPTS='' " + command
    config.gates.steps[0].targets = targets
    config.gates.steps[0].args = args
    if source != 'structured':
        config.gates.steps = []
        config.gates.commands = [command] if source == 'legacy' else []
        child.fix_verify_command = command if source == 'explicit_fix' else ''
        if source == 'manual':
            from auto_agents.models import GateParallelGroup
            config.gates.parallel_groups = [GateParallelGroup(name='manual', commands=[command])]
    if selector == 'discovery_other_config':
        from auto_agents.models import GateParallelGroup
        (root / 'z-other.ini').write_text('[pytest]\npython_functions = test_*\n')
        other_command = './.conda/bin/python -m pytest -c z-other.ini tests/test_owned.py::test_control'
        config.gates.parallel_groups.append(GateParallelGroup(name='manual-control', commands=[other_command]))
    plan = load_task_plan(root)
    # Retain the exact node obligation for every command representation.
    plan['tasks'][0]['verification_refs'] = [required_node]
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    if retained:
        import auto_agents.session_verification as verification
        # Model an authenticated older inventory that admitted this excluded
        # node. Resume must recheck it before baseline or writer admission.
        with monkeypatch.context() as old:
            old.setattr(verification, '_validate_required_node_selection', lambda *_: None)
            _binding_fixture(root, child)
        save_session_state(root, child)
        retained_binding = json.loads(json.dumps(child.verification_binding))
    if selector.startswith('setup_only_inherited'):
        # Change the actual inherited environment after constructing the
        # retained binding. No setup-only option is added to its command.
        assert '--setup-only' not in command
        monkeypatch.setenv('PYTEST_ADDOPTS', '--setup-only')
    ambient = {path: (root / path).read_bytes() for path in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        if selector == 'filename_default':
            from auto_agents.models import CommandResult, GateResult
            return GateResult(ok=True, commands=[CommandResult(
                command=command, ok=True, returncode=0, cached=True,
            ) for command in commands], summary='cached passing control')
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    if not inclusive:
        def reject_baseline(*args, **kwargs):
            pytest.fail('Known exclusions must block before baseline admission')
        monkeypatch.setattr(Session, '_ensure_baseline', reject_baseline)
    saved, calls, _ = run_session(root, monkeypatch)
    if inclusive:
        assert saved.status == 'completed', saved.to_dict()
        assert marker.read_text() == 'owned'
        assert dispatched
        assert calls == ['fix']
    else:
        assert saved.status == 'blocked', saved.to_dict()
        assert calls == []
        assert dispatched == [], 'coverage must be established before baseline, collection or cache lookup'
        assert not marker.exists()
        diagnostic = saved.execution_log[-1]['diagnostic']
        assert diagnostic['verification_ref'] == required_node
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['workflow_id'] == saved.workflow_id
        assert diagnostic['handoff_id'] == child.parent_handoff_id
        assert diagnostic['owners'][0]['task_id'] == 'task-owned'
        assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
        if retained:
            assert saved.verification_binding == retained_binding
            resumed, repeated_calls, _ = run_session(root, monkeypatch)
            assert resumed.status == 'blocked'
            assert repeated_calls == dispatched == []
            assert resumed.verification_binding == retained_binding
    assert {path: (root / path).read_bytes() for path in ambient} == ambient
    assert load_task_plan(root)['tasks'][0]['verification_refs'] == [required_node]


@pytest.mark.parametrize('source', ['structured', 'legacy', 'explicit_fix', 'manual'])
@pytest.mark.parametrize('selector', [
    'filename_default', 'filename_config_addopts_excluded', 'filename_config_addopts_included',
    'filename_config_env_excluded', 'filename_config_env_included',
    'filename_config_cli_excluded', 'filename_config_cli_included',
    'nested_config_excluded', 'nested_config_included',
    'nested_config_later_excluded', 'nested_config_later_included',
    'nested_config_ancestor_excluded', 'nested_config_ancestor_included',
    'nested_config_setup_included', 'nested_config_rootdir_included',
    'nested_config_bare_pyproject_included',
    'nested_config_bare_pyproject_overridden_excluded',
    'nested_config_bare_pyproject_overridden_included',
    'setup_only', 'setup_only_addopts', 'setup_only_config', 'setup_only_env', 'setup_only_inherited',
    'setup_plan', 'fixtures_per_test', 'setup_show',
])
def test_public_resume_rechecks_retained_default_filename_exclusion(tmp_path, monkeypatch, source, selector):
    test_public_resume_requires_executable_owned_node_coverage(
        tmp_path, monkeypatch, source, selector, retained=True)


@pytest.mark.parametrize('source', ['legacy', 'explicit_fix', 'manual'])
@pytest.mark.parametrize('retained', [False, True])
def test_public_resume_inline_options_clear_inherited_setup_only(tmp_path, monkeypatch, source, retained):
    test_public_resume_requires_executable_owned_node_coverage(
        tmp_path, monkeypatch, source, 'setup_only_inherited_cleared_included', retained=retained)


@pytest.mark.parametrize('reference', ['node', 'proof_id'])
def test_public_resume_excludes_unrelated_foreign_pending_proof_identity(tmp_path, monkeypatch, reference):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    config = load_project_config(root)
    config.gates.steps[0].risk = 'critical'
    config.gates.steps.append(VerificationStep(
        proof_id='foreign.future', runner='pytest', levels=['release'],
        targets=['tests/test_future.py::test_future'], impact_paths=[],
    ))
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Unimplemented foreign work',
        'workflow_id': 'foreign-workflow', 'status': 'pending', 'requirement_ids': ['REQ-foreign'],
        'verification_refs': ['foreign.future' if reference == 'proof_id' else 'tests/test_future.py::test_future']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    # The legacy child must recover the original mixed plan after an ambient
    # switch, and project by its own ownership without modifying either plan.
    ambient_config = load_project_config(root)
    ambient_config.gates.steps = [config.gates.steps[-1]]
    save_project_config(root, ambient_config)
    ambient_plan = {**plan, 'tasks': [plan['tasks'][-1]],
                    'verification_steps': [config.gates.steps[-1].to_dict()]}
    save_task_plan(root, ambient_plan)
    ambient = {path: (root / path).read_bytes() for path in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    observed = []
    resolve = Session._session_gate_plan
    def observe(self, scope):
        selected = resolve(self, scope)
        observed.append(selected)
        return selected
    monkeypatch.setattr(Session, '_session_gate_plan', observe)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    assert observed and any(selected.verification_level == 'release' for selected in observed)
    assert all('foreign.future' not in selected.proof_ids for selected in observed)
    assert all('owned.contract' in selected.proof_ids for selected in observed)
    assert not (root / 'tests/test_future.py').exists()
    assert saved.verification_binding['plan']['tasks'] == plan['tasks']
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


@pytest.mark.parametrize('style', ['manual_group', 'legacy_commands'])
@pytest.mark.parametrize('edit', ['replace', 'skip', 'intact'])
def test_public_resume_protects_manual_regression_sources(tmp_path, monkeypatch, style, edit):
    from auto_agents.config import load_task_plan
    from auto_agents.models import GateParallelGroup
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'manual-regression-executed')
    path = 'tests/test_regression.py'
    (root / path).write_text(
        'from pathlib import Path\ndef test_regression():\n'
        '    ' + marker.source('Path("value.py").read_text()') + '\n'
        '    assert "VALUE = 0" in Path("value.py").read_text()\n')
    command = './.conda/bin/python -m pytest -q ' + path
    config = load_project_config(root)
    config.gates.steps[0].risk = 'critical'
    if style == 'manual_group':
        config.gates.parallel_groups = [GateParallelGroup(name='manual-release', commands=[command])]
    else:
        config.gates.steps = []
        config.gates.commands = ['./.conda/bin/python -m pytest -q tests/test_owned.py::test_owned', command]
    plan = load_task_plan(root)
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    orch = Orchestrator(root)
    def provider(request):
        # Baseline execution belongs to the retained source; assertions below
        # concern candidate dispatch and certificate lookup after the writer.
        dispatched.clear()
        marker.unlink(missing_ok=True)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        proof = request.cwd / path
        if edit == 'replace':
            proof.write_text(proof.read_text().replace(
                'assert "VALUE = 0" in Path("value.py").read_text()', 'assert True'))
        elif edit == 'skip':
            # Mark only the temporary adversarial proof, as in the other
            # source-protection fixtures; this regression must still execute.
            proof.write_text('from pytest import mark\n' + proof.read_text().replace(
                'def test_regression():', '@mark.skip\ndef test_regression():'))
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    saved = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    assert saved.status != 'completed', saved.to_dict()
    assert path in saved.verification_binding['proof_sources']
    assert command not in saved.verification_binding['required_commands'], 'regression is outside explicit task scope'
    if edit == 'intact':
        assert dispatched
        assert marker.exists(), 'intact manual regression must execute'
        assert any('test_regression' in str(entry) for entry in saved.execution_log)
    else:
        assert dispatched == [], 'source validation must precede candidate collection and cached acceptance'
        assert not marker.exists()
        diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                          if entry.get('diagnostic', {}).get('verification_ref') == path)
        assert diagnostic['session_id'] == child.session_id
        assert diagnostic['owners'][0]['task_id'] == 'task-owned'
        assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False


@pytest.mark.parametrize('style', ['manual_group', 'legacy_commands'])
@pytest.mark.parametrize('target', ['whole_file', 'directory'])
@pytest.mark.parametrize('reference', ['node', 'proof_id'])
@pytest.mark.parametrize('regression', ['fails_candidate', 'passes_candidate'])
def test_public_resume_retains_manual_regression_sharing_foreign_future_file(
        tmp_path, monkeypatch, style, target, reference, regression):
    from auto_agents.config import load_task_plan
    from auto_agents.models import GateParallelGroup

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'shared-regression-executed')
    path = 'tests/test_regression.py'
    (root / path).write_text(
        'from pathlib import Path\ndef test_regression():\n'
        '    ' + marker.source('Path("value.py").read_text()', append=True) + '\n'
        + ('    assert "VALUE = 0" in Path("value.py").read_text()\n'
           if regression == 'fails_candidate' else '    assert True\n'))
    retained_source = (root / path).read_bytes()
    command = './.conda/bin/python -m pytest -q ' + (path if target == 'whole_file' else 'tests')
    future = VerificationStep(proof_id='foreign.future', runner='pytest', levels=['release'],
                              targets=[path + '::test_future'], impact_paths=[])
    config = load_project_config(root)
    config.gates.steps[0].risk = 'critical'
    if style == 'manual_group':
        config.gates.steps.append(future)
        config.gates.parallel_groups = [GateParallelGroup(name='manual-release', commands=[command])]
    else:
        config.gates.steps = []
        config.gates.commands = ['./.conda/bin/python -m pytest -q tests/test_owned.py::test_owned', command]
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Future check',
        'workflow_id': 'foreign-workflow', 'status': 'pending', 'requirement_ids': ['REQ-foreign'],
        'verification_refs': ['foreign.future' if reference == 'proof_id' else path + '::test_future']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    ambient_config = load_project_config(root)
    ambient_config.gates.steps = [future]
    save_project_config(root, ambient_config)
    save_task_plan(root, {**plan, 'tasks': [plan['tasks'][-1]], 'verification_steps': [future.to_dict()]})
    ambient = {name: (root / name).read_bytes() for name in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    observed = []
    resolve = Session._session_gate_plan
    def observe(self, scope):
        selected = resolve(self, scope)
        observed.append(selected)
        return selected
    monkeypatch.setattr(Session, '_session_gate_plan', observe)
    saved, calls, _ = run_session(root, monkeypatch)
    assert calls == ['fix']
    assert 'VALUE = 1\n' in marker.read_text().splitlines(keepends=True), 'the candidate must execute the retained regression'
    if style == 'manual_group' and target == 'whole_file' and regression == 'fails_candidate':
        # Lazy baseline comparison follows the failed candidate invocation.
        # Keep evidence for both sources instead of overwriting the sentinel.
        assert 'VALUE = 0\n' in marker.read_text().splitlines(keepends=True)
    assert (saved.status == 'completed') == (regression == 'passes_candidate'), saved.to_dict()
    assert observed and all(command in [*p.commands, *(cmd for g in p.parallel_groups for cmd in g.commands)]
                            for p in observed)
    assert all('foreign.future' not in p.proof_ids for p in observed)
    assert path in saved.verification_binding['proof_sources']
    assert saved.verification_binding['plan']['tasks'] == plan['tasks']
    assert (root / path).read_bytes() == retained_source, 'do not implement the future node'
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('imports', ['direct', 'pythonpath', 'reexport',
                                   'inheritance', 'inheritance_local', 'inheritance_reexport',
                                   'inheritance_generic', 'inheritance_generic_named',
                                   'inheritance_generic_reexport', 'inherited_pythonpath',
                                   'inherited_pythonpath_v6'])
@pytest.mark.parametrize('outcome', ['weakening', 'intact_failure', 'passing'])
def test_public_resume_protects_imported_release_regression(tmp_path, monkeypatch, legacy, imports, outcome):
    from copy import deepcopy
    from auto_agents.config import load_task_plan
    from auto_agents.authorization import authorization_policy_for_state
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore
    import auto_agents.session as session_module
    import auto_agents.session_verification as verification
    from auto_agents.gate_execution import LocalGatePlanExecutor

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'imported-regression-executed')
    name = 'test_regression' if imports == 'direct' else 'check_regression'
    inherited_options = imports.startswith('inherited_pythonpath')
    if inherited_options:
        name = 'test_regression'
    inherited = imports.startswith('inheritance')
    generic = imports.startswith('inheritance_generic')
    helper = 'regression_helpers.py' if imports == 'direct' else 'qa/regression_helpers.py'
    (root / helper).parent.mkdir(exist_ok=True)
    expected = 1 if outcome == 'passing' else 0
    helper_source = ('from pathlib import Path\n'
        f'def {name}():\n'
        '    ' + marker.source('Path("value.py").read_text()', append=True) + '\n'
        f'    assert "VALUE = {expected}" in Path("value.py").read_text()\n')
    if inherited:
        helper_source = ('from pathlib import Path\nclass BaseRegression:\n'
                         f'    def {name}(self):\n'
                         '        ' + marker.source('Path("value.py").read_text()', append=True) + '\n'
                         f'        assert "VALUE = {expected}" in Path("value.py").read_text()\n')
    if generic:
        helper_source = helper_source.replace('class BaseRegression:',
            'from typing import Generic, TypeVar\nT = TypeVar("T")\nclass BaseRegression(Generic[T]):')
    (root / helper).write_text(helper_source)
    if imports != 'direct' and not inherited_options:
        (root / 'pytest.ini').write_text('[pytest]\npython_functions = test_* check_*\npythonpath = qa\n')
    elif inherited_options:
        # Establish only the config root. The import path must come solely
        # from the inherited environment, not configuration or inline args.
        (root / 'pytest.ini').write_text('[pytest]\n')
        monkeypatch.setenv('PYTEST_ADDOPTS', '-o pythonpath=qa')
    module = 'regression_helpers'
    if imports in {'reexport', 'inheritance_reexport', 'inheritance_generic_reexport'}:
        exported = 'BaseRegression' if inherited else name
        (root / 'qa/regression_exports.py').write_text(f'from regression_helpers import {exported}\n')
        module = 'regression_exports'
    path = 'tests/test_regression.py'
    (root / path).write_text(f'from {module} import {name}\n')
    node = name
    if inherited:
        preamble = f'import {module} as helpers\n'
        declaration = 'class TestRegression(helpers.BaseRegression): pass\n'
        if imports == 'inheritance_local':
            declaration = ('class RetainedBase(helpers.BaseRegression): pass\n'
                           'class TestRegression(RetainedBase): pass\n')
        if generic:
            declaration = 'class TestRegression(helpers.BaseRegression[int]): pass\n'
            if imports == 'inheritance_generic_named':
                preamble = f'from {module} import BaseRegression\n'
                declaration = 'class TestRegression(BaseRegression[int]): pass\n'
        (root / path).write_text(preamble + declaration)
        node = 'TestRegression::' + name
    foreign = VerificationStep(proof_id='foreign.release', runner='pytest', levels=['release'],
                               targets=[path + '::' + node], impact_paths=[])
    config = load_project_config(root)
    config.gates.steps[0].risk = 'critical'
    config.gates.steps.append(foreign)
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'workflow_id': 'foreign-workflow',
                         'title': 'Pending work with an existing regression', 'status': 'pending',
                         'requirement_ids': ['REQ-foreign'], 'verification_refs': ['foreign.release']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    if legacy:
        child.workflow_id = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id)).workflow_id
        child.authorization_policy = authorization_policy_for_state(auto_approve=True).to_dict()
        with monkeypatch.context() as previous:
            previous_version = 6 if imports == 'inherited_pythonpath_v6' else 5 if generic else 4 if inherited else 3
            previous.setattr(verification, '_PROOF_INVENTORY_VERSION', previous_version)
            verification.bind_session(Session(Orchestrator(root), mode='fix', auto_approve=True), child)
        # Reconstruct the old inventory that did not protect imported bodies.
        for entry in (helper, 'qa/regression_exports.py'):
            child.verification_binding['proof_sources'].pop(entry, None)
        child.verification_binding.pop('proof_source_owners', None)
        if inherited_options:
            child.verification_binding.pop('proof_execution_context', None)
        child.verification_binding['binding_fingerprint'] = verification.fingerprint({
            key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
        save_session_state(root, child)
    ambient_config = load_project_config(root)
    ambient_config.gates.steps = [foreign]
    save_project_config(root, ambient_config)
    save_task_plan(root, {**plan, 'tasks': [plan['tasks'][-1]], 'verification_steps': [foreign.to_dict()]})
    ambient = {entry: (root / entry).read_bytes() for entry in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json', path, helper)}
    dispatched, cache_accesses, writers = [], [], []
    execute, cached = session_module.run_gate_plan, LocalGatePlanExecutor.cached_result
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    def observe_cache(self, command):
        cache_accesses.append(command)
        return cached(self, command)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    monkeypatch.setattr(LocalGatePlanExecutor, 'cached_result', observe_cache)
    orch = Orchestrator(root)
    def provider(request):
        writers.append(request.purpose)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        if outcome == 'weakening':
            (request.cwd / helper).write_text(helper_source.replace(
                'assert "VALUE = 0" in Path("value.py").read_text()', 'assert True'))
        reply = 'Repaired value\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    def resume():
        return Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    saved = resume()
    assert writers == ['fix']
    assert saved.verification_binding['proof_inventory_version'] == 6
    assert saved.verification_binding['proof_sources'][helper] == helper_source
    assert saved.verification_binding['proof_source_owners'][helper][0]['task_id'] == 'task-foreign'
    if inherited_options:
        assert saved.verification_binding['proof_execution_context']['schema_version'] == 1
        assert 'pythonpath' not in (root / 'pytest.ini').read_text()
        assert 'qa' not in foreign.targets[0]
    if outcome == 'weakening':
        assert saved.status != 'completed' and not dispatched and not cache_accesses
        assert not marker.exists()
        diagnostic = next(entry['diagnostic'] for entry in saved.execution_log
                          if entry.get('diagnostic', {}).get('verification_ref') == helper)
        assert diagnostic['owners'][0]['task_id'] == 'task-foreign'
        assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-foreign']
        assert diagnostic['session_id'] == child.session_id and diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
        custody, attempt = deepcopy(saved.candidate_custody), saved.current_attempt
        for _ in range(2):
            repeated = resume()
            assert repeated.status == 'blocked'
            assert repeated.candidate_custody == custody and repeated.current_attempt == attempt
            assert writers == ['fix'] and not dispatched and not cache_accesses
    else:
        assert (saved.status == 'completed') == (outcome == 'passing'), saved.to_dict()
        assert 'VALUE = 1' in marker.read_text().splitlines()
        assert dispatched
    assert {entry: (root / entry).read_bytes() for entry in ambient} == ambient


@pytest.mark.parametrize('boundary', ['receipt', 'completed'])
@pytest.mark.parametrize('outcome', ['weakening', 'intact_failure', 'passing'])
def test_public_resume_refreshes_proof_sources_after_environment_change(tmp_path, monkeypatch, boundary, outcome):
    from copy import deepcopy
    from auto_agents.config import load_task_plan
    from auto_agents.gate_execution import LocalGatePlanExecutor
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    old_marker = ExecutionMarker(tmp_path / 'old-context')
    new_marker = ExecutionMarker(tmp_path / 'new-context')
    old_helper, new_helper = 'qa_before/regression_helpers.py', 'qa_after/regression_helpers.py'
    for path, marker, expected in ((old_helper, old_marker, 1),
                                  (new_helper, new_marker, 1 if outcome == 'passing' else 0)):
        (root / path).parent.mkdir()
        (root / path).write_text('from pathlib import Path\ndef test_regression():\n'
            '    ' + marker.source('Path("value.py").read_text()', append=True) + '\n'
            f'    assert "VALUE = {expected}" in Path("value.py").read_text()\n')
    old_source, new_source = (root / old_helper).read_text(), (root / new_helper).read_text()
    (root / 'pytest.ini').write_text('[pytest]\n')
    (root / 'tests/test_regression.py').write_text('from regression_helpers import test_regression\n')
    foreign = VerificationStep(proof_id='foreign.release', runner='pytest', levels=['release'],
                               targets=['tests/test_regression.py::test_regression'])
    config = load_project_config(root)
    config.gates.steps[0].risk = 'critical'
    config.gates.steps.append(foreign)
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Retained regression',
                         'workflow_id': 'foreign-workflow', 'status': 'pending',
                         'requirement_ids': ['REQ-foreign'], 'verification_refs': ['foreign.release']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    monkeypatch.setenv('PYTEST_ADDOPTS', '-o pythonpath=qa_before')
    writers = []
    orch = Orchestrator(root)
    def provider(request):
        writers.append(request.purpose)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        if outcome == 'weakening':
            (request.cwd / new_helper).write_text(new_source.replace(
                'assert "VALUE = 0" in Path("value.py").read_text()', 'assert True'))
        reply = 'Repaired value\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    def resume():
        return Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    with monkeypatch.context() as interrupted:
        if boundary == 'receipt':
            def pause(*args, **kwargs):
                raise KeyboardInterrupt()
            interrupted.setattr(Session, '_run_session_persistence_action', pause)
        saved = resume()
    assert saved.status == ('paused' if boundary == 'receipt' else 'completed'), saved.to_dict()
    assert writers == ['fix'] and 'VALUE = 1' in old_marker.read_text().splitlines()
    assert not new_marker.exists()
    assert any(entry.get('action') == 'receipt_verification' and entry['verification']['ok']
               for entry in saved.execution_log)
    previous_context = deepcopy(saved.verification_binding['proof_execution_context'])
    custody = deepcopy(saved.candidate_custody)
    assert new_helper not in saved.verification_binding['proof_sources']
    ambient_config = load_project_config(root)
    ambient_config.gates.steps = [foreign]
    save_project_config(root, ambient_config)
    save_task_plan(root, {**plan, 'tasks': [plan['tasks'][-1]], 'verification_steps': [foreign.to_dict()]})
    protected = {path: (root / path).read_bytes() for path in (
        '.auto-agents/config.json', '.auto-agents/state/task_plan.json', '.git/index',
        'value.py', old_helper, new_helper)}
    dispatched, caches = [], []
    run, lookup = session_module.run_gate_plan, LocalGatePlanExecutor.cached_result
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return run(commands, *args, **kwargs)
    def observe_cache(self, command):
        caches.append(command)
        return lookup(self, command)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
    monkeypatch.setattr(LocalGatePlanExecutor, 'cached_result', observe_cache)
    monkeypatch.setenv('PYTEST_ADDOPTS', '-o pythonpath=qa_after')
    saved = resume()
    assert saved.verification_binding['proof_execution_context'] != previous_context
    assert saved.verification_binding['proof_sources'][old_helper] == old_source
    assert saved.verification_binding['proof_sources'][new_helper] == new_source
    assert saved.candidate_custody['receipt'] == custody['receipt']
    assert saved.candidate_custody['checkout'] == custody['checkout']
    assert writers == ['fix']
    if outcome == 'weakening':
        assert saved.status == 'blocked' and not dispatched and not caches and not new_marker.exists()
        diagnostic = next(entry['diagnostic'] for entry in reversed(saved.execution_log)
                          if entry.get('diagnostic', {}).get('verification_ref') == new_helper)
        assert diagnostic['owners'][0]['task_id'] == 'task-foreign'
        assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-foreign']
        assert diagnostic['retry_fix'] is False
    else:
        assert (saved.status == 'completed') == (outcome == 'passing'), saved.to_dict()
        assert dispatched and 'VALUE = 1' in new_marker.read_text().splitlines()
    if outcome != 'intact_failure':
        count = len(dispatched)
        after = deepcopy(saved.candidate_custody)
        for _ in range(2):
            repeated = resume()
            assert repeated.status == saved.status
            assert repeated.candidate_custody == after
            assert len(dispatched) == count and writers == ['fix']
            if outcome == 'weakening':
                assert not caches and not new_marker.exists()
    assert {path: (root / path).read_bytes() for path in protected} == protected


@pytest.mark.parametrize('reference', ['proof_id', 'target'])
@pytest.mark.parametrize('coverage', ['whole_file', 'directory', 'node', 'mixed_future',
                                     'parameterized_node', 'parameterized_legacy_binding'])
@pytest.mark.parametrize('regression', ['fails_candidate', 'passes_candidate'])
def test_public_resume_retains_existing_foreign_release_regression(
        tmp_path, monkeypatch, reference, coverage, regression):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'structured-regression-executed')
    path = 'tests/test_regression.py'
    (root / path).write_text(
        'from pathlib import Path\ndef test_regression():\n'
        '    ' + marker.source('Path("value.py").read_text()', append=True) + '\n'
        + ('    assert "VALUE = 0" in Path("value.py").read_text()\n'
           if regression == 'fails_candidate' else '    assert True\n'))
    if coverage.startswith('parameterized_'):
        (root / path).write_text('import pytest\n' + (root / path).read_text().replace(
            'def test_regression():',
            '@pytest.mark.parametrize("value", [0], ids=["zero"])\ndef test_regression(value):'))
    retained_source = (root / path).read_bytes()
    target = {'whole_file': path, 'directory': 'tests', 'node': path + '::test_regression',
              'mixed_future': path, 'parameterized_node': path + '::test_regression[zero]',
              'parameterized_legacy_binding': path + '::test_regression[zero]'}[coverage]
    targets = [target]
    if coverage == 'mixed_future':
        targets.append('tests/test_future.py::test_future')
    foreign = VerificationStep(proof_id='foreign.future', runner='pytest', levels=['release'],
                               targets=targets, impact_paths=[])
    config = load_project_config(root)
    config.gates.steps[0].risk = 'critical'
    config.gates.steps.append(foreign)
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Pending foreign check',
        'workflow_id': 'foreign-workflow', 'status': 'pending', 'requirement_ids': ['REQ-foreign'],
        'verification_refs': ['foreign.future'] if reference == 'proof_id' else targets})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    if coverage == 'parameterized_legacy_binding':
        from auto_agents.authorization import authorization_policy_for_state
        from auto_agents.session_verification import bind_session, fingerprint
        from auto_agents.workflow_chain import WorkflowRef, WorkflowStore

        # Retain the earlier binding format with its missing source inventory.
        # Resume must rebuild it from child history after the ambient switch.
        child.workflow_id = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id)).workflow_id
        child.authorization_policy = authorization_policy_for_state(auto_approve=True).to_dict()
        bind_session(Session(Orchestrator(root), mode='fix', auto_approve=True), child)
        binding = child.verification_binding
        binding['schema_version'] = 11
        del binding['proof_sources'][path]
        binding['binding_fingerprint'] = fingerprint({
            key: value for key, value in binding.items() if key != 'binding_fingerprint'})
        save_session_state(root, child)
    # Only dynamic file access relates this retained regression to the repair;
    # no impact declaration or Python import can rescue an incorrect projection.
    ambient_config = load_project_config(root)
    ambient_config.gates.steps = [foreign]
    save_project_config(root, ambient_config)
    save_task_plan(root, {**plan, 'tasks': [plan['tasks'][-1]],
                         'verification_steps': [foreign.to_dict()]})
    ambient = {name: (root / name).read_bytes() for name in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    observed = []
    resolve = Session._session_gate_plan
    def observe(self, scope):
        selected = resolve(self, scope)
        observed.append(selected)
        return selected
    monkeypatch.setattr(Session, '_session_gate_plan', observe)
    saved, calls, _ = run_session(root, monkeypatch)
    assert calls == ['fix']
    assert marker.exists(), 'retained release regression must execute'
    assert 'VALUE = 1\n' in marker.read_text().splitlines(keepends=True)
    if regression == 'fails_candidate' and coverage != 'directory':
        assert 'VALUE = 0\n' in marker.read_text().splitlines(keepends=True), 'baseline must pass the regression'
    assert (saved.status == 'completed') == (regression == 'passes_candidate'), saved.to_dict()
    assert observed and all('foreign.future' in selected.proof_ids for selected in observed)
    assert all('test_future.py' not in command for selected in observed
               for command in [*selected.commands, *(cmd for group in selected.parallel_groups for cmd in group.commands)])
    assert path in saved.verification_binding['proof_sources']
    assert saved.verification_binding['plan']['tasks'] == plan['tasks']
    assert (root / path).read_bytes() == retained_source
    assert not (root / 'tests/test_future.py').exists()
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


def _binding_fixture(root, child):
    from auto_agents.authorization import authorization_policy_for_state
    from auto_agents.session_verification import bind_session
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore

    if not child.workflow_id:
        child.workflow_id = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id)).workflow_id
    child.authorization_policy = authorization_policy_for_state(auto_approve=True).to_dict()
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    bind_session(session, child)
    return session


def _switch_ambient_binding_plan(root):
    from auto_agents.config import load_run_state, save_run_state

    config = load_project_config(root)
    config.gates.steps = [VerificationStep(proof_id='foreign.future', runner='pytest',
        targets=['tests/test_future.py::test_future'], levels=['affected', 'release'], impact_paths=['**'])]
    save_project_config(root, config)
    save_task_plan(root, {'tasks': [{'task_id': 'task-foreign', 'title': 'Foreign pending task',
        'workflow_id': 'foreign-workflow', 'status': 'pending', 'verification_refs': ['foreign.future']}],
        'verification_steps': [step.to_dict() for step in config.gates.steps]})
    run = load_run_state(root)
    run.resume_context['workflow_id'] = 'foreign-workflow'
    save_run_state(root, run)
    return {name: (root / name).read_bytes() for name in
            ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}


def _assert_binding_blocked_before_execution(root, monkeypatch, *, parent=False):
    def baseline(*args, **kwargs):
        pytest.fail('Unresolved session authority must block before baseline capture')
    monkeypatch.setattr(Session, '_ensure_baseline', baseline)
    if parent:
        from test_engine_child_recovery import ObservationBoundary, resume_to_observation
        collab_loop = Session._phase_collab_loop
        def parent_boundary(self, state):
            if state.session_id == 'parent':
                raise ObservationBoundary()
            return collab_loop(self, state)
        monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)
        def writer(*args):
            pytest.fail('Unresolved session authority must block before the writer')
        resume_to_observation(root, monkeypatch, writer)
        saved = load_session_state(root, 'owned-child')
    else:
        saved, calls, _ = run_session(root, monkeypatch)
        assert calls == []
    assert saved.status == 'blocked', saved.to_dict()
    assert saved.resolution == 'verification_ownership'
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['session_id'] == saved.session_id
    assert diagnostic['workflow_id'] == saved.workflow_id
    assert diagnostic['retry_fix'] is False
    return saved


def _prepare_binding_child_resume(root, store, snapshot, handoff):
    store.record_result(snapshot, handoff, status='failed',
                        result={'status': 'failed', 'resolution': 'verification_ownership'})
    store.consume_result(snapshot, handoff, operation_id='retained-binding-failure')
    resume = store.prepare_handoff(snapshot, parent=snapshot.root, target='resume',
        goal=handoff.goal, reason='Resume retained child authority',
        payload={'resume_handoff_id': handoff.handoff_id})
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = resume.handoff_id
    save_session_state(root, parent)


@pytest.mark.parametrize('has_workflow', [True, False])
@pytest.mark.parametrize('entrypoint', ['session', 'coordinator'])
def test_public_legacy_resume_without_history_rejects_ambient_plan(tmp_path, monkeypatch, has_workflow, entrypoint):
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore

    root, child = project(tmp_path)
    if has_workflow:
        child.workflow_id = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id)).workflow_id
    child.baseline_git_ref = child.baseline_head_ref = child.lineage_head_ref = ''
    save_session_state(root, child)
    ambient = _switch_ambient_binding_plan(root)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'switch global contract without child history')
    from auto_agents.workflow_runtime import WorkflowCoordinator
    def resume():
        if entrypoint == 'session':
            return _assert_binding_blocked_before_execution(root, monkeypatch)
        def forbidden(*args, **kwargs):
            pytest.fail('Missing retained authority must precede baseline and writer')
        monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
        monkeypatch.setattr(Orchestrator, '_call_with_failover', forbidden)
        coordinator = WorkflowCoordinator(Orchestrator(root))
        if has_workflow:
            result = coordinator.resume_workflow(child.workflow_id)
        else:
            result = coordinator.resume_session(Session(coordinator.orch, mode='fix'), child.session_id)
        assert result.status == 'blocked'
        assert result.execution_log[-1]['retry_fix'] is False
        return result
    saved = resume()
    assert saved.verification_binding == {}
    assert 'contract revision is unavailable' in saved.execution_log[-1]['result']
    assert saved.workflow_id == child.workflow_id
    assert saved.baseline_git_ref == saved.baseline_head_ref == saved.lineage_head_ref == ''
    repeated = resume()
    assert repeated.verification_binding == {}
    assert repeated.lineage_head_ref == ''
    assert {name: (root / name).read_bytes() for name in ambient} == ambient
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'


@pytest.mark.parametrize('conflict', ['issue_handoff', 'issue_handoff_requirements',
                                     'nested_issue', 'foreign_task', 'foreign_requirement'])
def test_public_child_binding_rejects_conflicting_task_authority(tmp_path, monkeypatch, conflict):
    from auto_agents.config import load_task_plan
    from test_engine_child_recovery import parent_workflow

    root, child = project(tmp_path)
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Foreign owned proof',
        'workflow_id': 'foreign-workflow', 'requirement_ids': ['REQ-foreign'],
        'verification_refs': ['owned.contract']})
    _retain_contract(root, child, load_project_config(root), plan)
    store, snapshot, handoff = parent_workflow(root, child)
    handoff.payload['task_id'] = 'task-owned' if conflict == 'issue_handoff' else 'task-foreign'
    issue_scope = {'task_id': 'task-foreign'}
    if conflict == 'issue_handoff_requirements':
        handoff.payload.pop('task_id')
        handoff.payload['requirement_ids'] = ['REQ-owned']
        issue_scope = {'requirement_ids': ['REQ-foreign']}
    elif conflict == 'nested_issue':
        handoff.payload['task_id'] = 'task-owned'
        issue_scope = {'task_id': 'task-owned', 'issue_seed': {'task_id': 'task-foreign'}}
    elif conflict == 'foreign_requirement':
        handoff.payload.pop('task_id')
        handoff.payload['requirement_ids'] = ['REQ-foreign']
        issue_scope = {'requirement_ids': ['REQ-foreign']}
    store.save_handoff(handoff)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text(json.dumps(issue_scope))
    _prepare_binding_child_resume(root, store, snapshot, handoff)
    ambient = _switch_ambient_binding_plan(root)
    saved = _assert_binding_blocked_before_execution(root, monkeypatch, parent=True)
    assert saved.verification_binding == {}
    assert 'conflict' in saved.execution_log[-1]['result']
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['handoff_id'] == handoff.handoff_id
    if conflict in {'foreign_task', 'foreign_requirement'}:
        assert diagnostic['conflicting_task_id'] == 'task-foreign'
        assert diagnostic['task_workflow_id'] == 'foreign-workflow'
    elif conflict in {'issue_handoff', 'issue_handoff_requirements'}:
        prefix = 'REQ-' if conflict == 'issue_handoff_requirements' else 'task-'
        assert diagnostic['retained_scope'] == [prefix + 'foreign']
        assert diagnostic['conflicting_scope'] == [prefix + 'owned']
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('binding_version', [None, 11, 13])
def test_public_resume_rejects_conflicting_handoff_child_identities(tmp_path, monkeypatch, binding_version):
    from copy import deepcopy
    from auto_agents.session_verification import fingerprint
    from test_engine_child_recovery import parent_workflow

    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    if binding_version is not None:
        _binding_fixture(root, child)
        child.verification_binding['schema_version'] = binding_version
        child.verification_binding['binding_fingerprint'] = fingerprint({
            key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
        save_session_state(root, child)
    retained = deepcopy(child.verification_binding)
    # The bound child ref agrees, but the same handoff names another child
    # in its payload. Neither representation may silently override the other.
    handoff.payload['child_session_id'] = 'another-child'
    store.save_handoff(handoff)
    _prepare_binding_child_resume(root, store, snapshot, handoff)
    ambient = _switch_ambient_binding_plan(root)
    handoff_path = root / '.auto-agents/state/handoffs' / (handoff.handoff_id + '.json')
    handoff_bytes = handoff_path.read_bytes()
    saved = _assert_binding_blocked_before_execution(root, monkeypatch, parent=True)
    assert saved.verification_binding == retained
    diagnostic = saved.execution_log[-1]['diagnostic']
    assert diagnostic['handoff_id'] == handoff.handoff_id
    assert diagnostic['child_session_ids'] == [child.session_id, 'another-child']
    assert handoff_path.read_bytes() == handoff_bytes
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('matching', [False, True, 'unscoped'])
@pytest.mark.parametrize('binding_version', [None, 11, 13])
def test_public_child_reconciles_task_and_requirement_authority(
        tmp_path, monkeypatch, reverse, matching, binding_version):
    from copy import deepcopy
    from auto_agents.config import load_task_plan
    from auto_agents.session_verification import fingerprint
    from test_engine_child_recovery import parent_workflow, resume_to_observation

    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    config = load_project_config(root)
    (root / 'tests/test_other.py').write_text('def test_other():\n    assert True\n')
    config.gates.steps.append(VerificationStep(proof_id='other.contract', runner='pytest',
        targets=['tests/test_other.py::test_other'], levels=['affected'], impact_paths=['other.py']))
    plan = load_task_plan(root)
    plan['tasks'][0]['workflow_id'] = child.workflow_id
    plan['tasks'].append({'task_id': 'task-other', 'title': 'Another child obligation',
        'workflow_id': child.workflow_id, 'requirement_ids': ['REQ-other'],
        'verification_refs': ['other.contract']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    task_scope = {'task_id': 'task-owned'}
    requirement_scope = {'requirement_ids': ['REQ-owned']}
    handoff.payload.pop('task_id', None)
    if matching != 'unscoped':
        handoff.payload.update(requirement_scope if reverse else task_scope)
    store.save_handoff(handoff)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text(json.dumps({} if matching == 'unscoped' else
                                task_scope if reverse else requirement_scope))
    if binding_version is not None:
        if matching == 'unscoped':
            # Reproduce the old workflow-only binding, then exercise recovery
            # with the real authority validator restored.
            with monkeypatch.context() as legacy:
                legacy.setattr('auto_agents.session_verification._validate_task_authority', lambda state: None)
                _binding_fixture(root, child)
        else:
            _binding_fixture(root, child)
        child.verification_binding['schema_version'] = binding_version
        child.verification_binding['binding_fingerprint'] = fingerprint({
            key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
        save_session_state(root, child)
    prior_binding = deepcopy(child.verification_binding)
    if not matching:
        issue.write_text(json.dumps({'task_id': 'task-other'} if reverse
                                    else {'requirement_ids': ['REQ-other']}))
    _prepare_binding_child_resume(root, store, snapshot, handoff)
    ambient = _switch_ambient_binding_plan(root)
    handoff_bytes = (root / '.auto-agents/state/handoffs' / (handoff.handoff_id + '.json')).read_bytes()
    if matching is True:
        from test_engine_child_recovery import ObservationBoundary
        # Stop after the public parent consumes the child result, before
        # starting the ambient workflow's separate implementation phase.
        collab_loop = Session._phase_collab_loop
        def parent_boundary(self, state):
            if state.session_id == 'parent':
                raise ObservationBoundary()
            return collab_loop(self, state)
        monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)
        calls = []
        def writer(state, prompt, candidate_root):
            calls.append(state.session_id)
            (candidate_root / 'value.py').write_text('VALUE = 1\n')
            return 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        resume_to_observation(root, monkeypatch, writer)
        saved = load_session_state(root, child.session_id)
        assert saved.status == 'completed', saved.to_dict()
        assert calls == [child.session_id]
        assert saved.verification_binding['required_proof_ids'] == ['owned.contract']
        assert saved.verification_binding['task_scope'] == {
            'task_ids': ['task-owned'], 'requirement_ids': ['REQ-owned']}
    else:
        saved = _assert_binding_blocked_before_execution(root, monkeypatch, parent=True)
        assert ('unresolved' if matching == 'unscoped' else 'conflict') in saved.execution_log[-1]['result']
        diagnostic = saved.execution_log[-1]['diagnostic']
        assert diagnostic['handoff_id'] == handoff.handoff_id
        if binding_version is None and matching != 'unscoped':
            assert diagnostic['retained_task_ids'] == ['task-other' if reverse else 'task-owned']
            assert diagnostic['requirement_task_ids'] == ['task-owned' if reverse else 'task-other']
        assert saved.verification_binding == prior_binding
        if matching == 'unscoped':
            assert diagnostic['task_scope'] == {'task_ids': [], 'requirement_ids': []}
            assert diagnostic['retry_fix'] is False
            repeated = _assert_binding_blocked_before_execution(root, monkeypatch, parent=True)
            assert repeated.verification_binding == prior_binding
        assert (root / 'value.py').read_text() == 'VALUE = 0\n'
        assert (root / '.auto-agents/state/handoffs' / (handoff.handoff_id + '.json')).read_bytes() == handoff_bytes
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


def test_public_legacy_upgrade_preserves_conflicting_retained_handoff(tmp_path, monkeypatch):
    from copy import deepcopy
    from auto_agents.session_verification import fingerprint
    from auto_agents.workflow_chain import WorkflowRef
    from test_engine_child_recovery import parent_workflow

    root, child = project(tmp_path)
    store, snapshot, original = parent_workflow(root, child)
    _binding_fixture(root, child)
    child.verification_binding['schema_version'] = 11
    child.verification_binding['binding_fingerprint'] = fingerprint({
        key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
    retained = deepcopy(child.verification_binding)
    replacement = store.prepare_handoff(snapshot, parent=snapshot.root, target='fix',
        goal=child.goal, reason='Conflicting legacy recovery',
        payload={'child_session_id': child.session_id, 'head_before': child.baseline_head_ref, 'auto_approve': True})
    store.bind_child(snapshot, replacement, WorkflowRef('fix', child.session_id))
    child.parent_handoff_id = replacement.handoff_id
    save_session_state(root, child)
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id = replacement.handoff_id
    save_session_state(root, parent)
    _prepare_binding_child_resume(root, store, snapshot, replacement)
    ambient = _switch_ambient_binding_plan(root)
    saved = _assert_binding_blocked_before_execution(root, monkeypatch, parent=True)
    assert saved.verification_binding == retained
    assert saved.execution_log[-1]['diagnostic']['handoff_id'] == original.handoff_id
    assert saved.execution_log[-1]['diagnostic']['resumed_handoff_id'] == replacement.handoff_id
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('legacy', [False, True, 'custody', 'missing_scope',
    'missing_scope_current', 'missing_scope_requirement', 'missing_scope_unresolved',
    'missing_scope_conflict', 'missing_scope_fingerprint', 'reuse_sessions'])
def test_binding_round_trip_and_legacy_recovery_preserve_original_authority(tmp_path, monkeypatch, legacy):
    from copy import deepcopy
    from auto_agents.session_verification import fingerprint

    if legacy == 'reuse_sessions':
        _assert_reused_session_authority(tmp_path, monkeypatch)
        return

    if isinstance(legacy, str) and legacy.startswith('missing_scope'):
        _assert_public_missing_scope_recovery(tmp_path, monkeypatch, legacy)
        return

    root, child = project(tmp_path)
    child.goal_execution_environment = {'mode': 'real', 'confirmed': True, 'source': 'explicit_goal'}
    if legacy == 'custody':
        prerequisite_marker = _retain_foreign_prerequisite(root, child)
        child.baseline_git_ref = 'refs/auto-agents/gate-snapshots/retained-baseline'
        git(root, 'update-ref', child.baseline_git_ref, child.baseline_head_ref)
    _binding_fixture(root, child)
    if legacy == 'custody':
        original_baseline = deepcopy(child.verification_binding['baseline_identity'])
        git(root, 'update-ref', '-d', child.baseline_git_ref)
        child, calls, _ = run_session(root, monkeypatch)
        assert child.status == 'completed' and calls == ['fix']
        assert child.baseline_git_ref and child.baseline_git_ref != original_baseline['git_ref']
        assert child.verification_binding['baseline_identity'] == original_baseline
        refreshed_baseline = child.baseline_git_ref
    if legacy:
        child.verification_binding['schema_version'] = 11
        for key in ('execution_environment', 'source_provenance', 'session_mode'):
            child.verification_binding.pop(key)
        child.verification_binding['binding_fingerprint'] = fingerprint({
            key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
    if legacy == 'custody':
        _retain_legacy_prerequisite_owners(child)
        assert prerequisite_marker.read_text() == 'VALUE = 1\n'
        prerequisite_marker.unlink()
    retained_custody = deepcopy(child.candidate_custody)
    retained = deepcopy(child.verification_binding)
    save_session_state(root, child)
    assert load_session_state(root, child.session_id).verification_binding == retained
    ambient = _switch_ambient_binding_plan(root)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ([] if legacy == 'custody' else ['fix'])
    binding = saved.verification_binding
    for key in ('repository', 'contract_revision', 'authorization', 'gates', 'plan', 'required_proof_ids',
                'session_id', 'workflow_id', 'original_handoff_id', 'task_scope', 'baseline_identity', 'tasks'):
        assert binding[key] == retained[key]
    assert binding['schema_version'] == 13
    assert binding['execution_environment'] == child.goal_execution_environment
    assert binding['source_provenance']['revision'] == child.baseline_head_ref
    if legacy == 'custody':
        assert saved.baseline_git_ref == refreshed_baseline
        _assert_prerequisite_owner_enrichment(saved, prerequisite_marker)
        _assert_migrated_candidate_reused(root, monkeypatch, saved, retained_custody)
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('scope', ['observed_inputs', 'source_auto'])
def test_public_resume_authenticated_history_with_supervised_input_tracing(tmp_path, monkeypatch, scope):
    _supervised_public_resume(tmp_path, monkeypatch, scope, recover=True)


@pytest.mark.parametrize('scope', ['observed_inputs', 'source_auto'])
def test_public_resume_global_plan_switch_with_supervised_input_tracing(tmp_path, monkeypatch, scope):
    _supervised_public_resume(tmp_path, monkeypatch, scope, recover=False)


def _supervised_public_resume(tmp_path, monkeypatch, scope, *, recover):
    from copy import deepcopy
    import shutil
    from auto_agents.config import load_task_plan
    from auto_agents.gate_execution import LocalGatePlanExecutor
    from auto_agents.session_verification import fingerprint
    from auto_agents.verification_input_trace import owner_identity, resolved_trace
    from auto_agents.verification_supervisor_checks import observation, LEGACY_SHA256
    from test_engine_child_recovery import (
        configure_local_writer, parent_workflow, resume_to_observation, ObservationBoundary,
    )

    # Legacy executors already supply an owner. A standalone V2 container
    # establishes the real launcher before rerunning this exact case; never
    # replace an inherited supervisor or emulate its protocol/observations.
    owner = owner_identity()
    if not owner['metadata']:
        import os
        import auto_agents.verification_sandbox as sandbox
        name = ('test_public_resume_authenticated_history_with_supervised_input_tracing' if recover else
                'test_public_resume_global_plan_switch_with_supervised_input_tracing')
        node = str(Path(__file__).resolve()) + '::' + name + '[' + scope + ']'
        command = [sys.executable, str(Path(sandbox.__file__).resolve()), '--metadata',
                   json.dumps({'roots': [str(tmp_path), '/tmp'], 'supervisor_checks': True}),
                   sys.executable, '-m', 'pytest', '-q', '--basetemp', str(tmp_path / 'supervised'), node]
        result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=240,
                                env={**os.environ, 'AUTO_AGENTS_VERIFICATION_SANDBOX': '1'})
        assert result.returncode == 0, result.stdout + result.stderr
        return
    assert owner['metadata'] == owner['trace'] == 1, owner
    assert shutil.which('strace'), 'the retained tracing acceptance requires strace'
    if recover:
        legacy = observation('legacy_owner')
        assert legacy['launcher_pid'] > 0 and legacy['source_root']
        assert legacy['returncode'] == 0 and legacy['count'] == 'executed\n'
        assert legacy['legacy_sha256'] == LEGACY_SHA256 and legacy['shared_unchanged']
        record = legacy['trace']
        assert record['owner']['metadata'] == 1 and record['owner']['trace'] == 0
        assert record['reason'] == 'live owner has no input tracing'
        assert record['complete'] is False
        assert resolved_trace(json.dumps(record)) is None

    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    shared = [root / 'foreign.py', root / '.git/index']
    controls = '''import errno, os, tempfile
from pathlib import Path
with tempfile.NamedTemporaryFile(dir=os.environ['TMPDIR']) as private:
    path = Path(private.name)
    path.chmod(0o750)
    assert path.stat().st_mode & 0o777 == 0o750
    os.fchmod(private.fileno(), 0o640)
    os.utime(path, ns=(123, 456))
    assert path.stat().st_mtime_ns == 456
    os.utime(private.fileno(), ns=(123, 789))
    assert path.stat().st_mtime_ns == 789
    assert path.stat().st_mode & 0o777 == 0o640
for name in SHARED:
    path = Path(name)
    before = path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns
    fd = os.open(path, os.O_RDONLY)
    try:
        for action in (lambda: path.write_bytes(b'forbidden'),
                       lambda: path.chmod(0o777), lambda: os.fchmod(fd, 0o777),
                       lambda: os.chmod('/proc/self/fd/' + str(fd), 0o777),
                       lambda: Path('/proc/self/fd/' + str(fd)).write_bytes(b'forbidden'),
                       lambda: os.utime(path, ns=(1, 1)), lambda: os.utime(fd, ns=(1, 1))):
            try:
                action()
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
            else:
                raise AssertionError('shared input escaped confinement')
    finally:
        os.close(fd)
    assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before
'''
    # Both the actual collected gate and the real provider subprocess exercise
    # metadata success and shared denial, before reporting their own success.
    probe = 'SHARED = ' + repr(list(map(str, shared))) + '\n' + controls
    owned_test = root / 'tests/test_owned.py'
    owned_test.write_text(owned_test.read_text() + '\n'
        + 'def test_confinement():\n' + ''.join('    ' + line + '\n' for line in probe.splitlines()))
    (root / 'tests/test_setup.py').write_text(
        'from pathlib import Path\ndef test_setup():\n'
        '    assert Path("value.py").read_text() == "VALUE = 1\\n"\n')
    config, plan = load_project_config(root), load_task_plan(root)
    step = config.gates.steps[0]
    step.targets.append('tests/test_owned.py::test_confinement')
    step.depends_on_proofs = ['shared.setup']
    step.cache_scope = 'source'
    step.result_cache_scope = 'observed_inputs' if scope == 'observed_inputs' else 'auto'
    assert not step.artifact_globs and not step.exclusive_resources and not step.dynamic_ports
    config.gates.steps.append(VerificationStep(proof_id='shared.setup', runner='pytest',
        targets=['tests/test_setup.py::test_setup'], levels=['affected', 'release']))
    plan['tasks'][0]['workflow_id'] = child.workflow_id
    plan['tasks'][0]['verification_refs'] = list(step.targets)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Retained prerequisite owner',
        'workflow_id': 'foreign-workflow', 'status': 'pending',
        'requirement_ids': ['REQ-foreign'], 'verification_refs': ['shared.setup']})
    plan['verification_steps'] = [item.to_dict() for item in config.gates.steps]
    _retain_contract(root, child, config, plan)
    configure_local_writer(root, child, probe + '\nPath("value.py").write_text("VALUE = 1\\n")')
    child.baseline_git_ref = 'refs/auto-agents/gate-snapshots/retained-supervised'
    git(root, 'update-ref', child.baseline_git_ref, child.baseline_head_ref)
    _binding_fixture(root, child)
    retained = deepcopy(child.verification_binding)
    if recover:
        child.verification_binding['schema_version'] = 11
        for key in ('execution_environment', 'source_provenance', 'session_mode'):
            child.verification_binding.pop(key)
        child.verification_binding['binding_fingerprint'] = fingerprint({
            key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
    save_session_state(root, child)
    _prepare_binding_child_resume(root, store, snapshot, handoff)
    ambient = (_switch_ambient_binding_plan(root) if not recover else
        {name: (root / name).read_bytes() for name in
         ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')})
    before = {str(path): (path.read_bytes(), path.stat().st_mode) for path in shared}
    calls, results, resumed = [], [], []
    run, retain_authority = LocalGatePlanExecutor.run, Session._retain_resume_authority
    def observe_run(executor, command, **kwargs):
        result = run(executor, command, **kwargs)
        results.append((command, executor.metadata.get(command), result, owner_identity()))
        return result
    def observe_authority(session, state):
        resumed.append(state.session_id)
        return retain_authority(session, state)
    monkeypatch.setattr(LocalGatePlanExecutor, 'run', observe_run)
    monkeypatch.setattr(Session, '_retain_resume_authority', observe_authority)
    collab_loop = Session._phase_collab_loop
    def parent_boundary(session, state):
        if state.session_id == 'parent':
            raise ObservationBoundary()
        return collab_loop(session, state)
    monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)
    def observe_writer(state, prompt, candidate_root):
        calls.append(state.session_id)
        assert state.goal_execution_environment == child.goal_execution_environment
        assert state.goal == child.goal and state.parent_handoff_id == handoff.handoff_id
        assert candidate_root != root
        assert (candidate_root / 'value.py').read_text() == 'VALUE = 0\n'
        # configure_local_writer's retained subprocess performs the only edit.
    resume_to_observation(root, monkeypatch, observe_writer, real_dispatch=True)
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == [child.session_id] and child.session_id in resumed
    binding = saved.verification_binding
    for key in ('repository', 'session_id', 'workflow_id', 'original_handoff_id',
                'authorization', 'task_scope', 'task_ids', 'requirement_ids', 'tasks',
                'contract_revision', 'contract_fingerprint', 'gates', 'plan', 'baseline_identity',
                'required_proof_ids', 'proof_owners', 'regression_dependencies'):
        assert binding[key] == retained[key], key
    assert binding['schema_version'] == 13
    assert binding['execution_environment'] == child.goal_execution_environment
    assert binding['source_provenance']['revision'] == child.baseline_head_ref
    assert binding['required_proof_ids'] == ['owned.contract', 'shared.setup']
    assert {item['task_id'] for item in binding['proof_owners']['shared.setup']} == {'task-owned', 'task-foreign'}
    selected = [(command, result, negotiated) for command, metadata, result, negotiated in results
        if metadata and metadata.cache_scope == 'source'
        and metadata.result_cache_scope == step.result_cache_scope and result.ok and not result.cached]
    assert selected, [(command, result.to_dict()) for command, _, result, _ in results]
    assert any('test_confinement' in command for command, _, _ in selected)
    assert any('test_setup' in command and result.ok and not result.cached
               for command, _, result, _ in results)
    for command, result, negotiated in selected:
        assert negotiated == owner
        assert result.returncode == 0 and result.backend == 'local-isolated'
        assert result.input_trace_complete or result.input_trace_reason
        print(json.dumps({'command': command, 'owner': negotiated, 'returncode': result.returncode,
            'input_trace_complete': result.input_trace_complete, 'input_trace_reason': result.input_trace_reason}))
    if recover:
        custody = deepcopy(saved.candidate_custody)
        assert saved.baseline_git_ref.startswith('refs/auto-agents/gate-snapshots/')
        for repository in (root, Path(custody['checkout'])):
            for ref in {child.baseline_git_ref, saved.baseline_git_ref}:
                git(repository, 'update-ref', '-d', ref)
            expired = subprocess.run(['git', 'rev-parse', '--verify', saved.baseline_git_ref],
                cwd=repository, capture_output=True, text=True)
            assert expired.returncode != 0, 'the disposable baseline must really be unavailable'
            assert git(repository, 'rev-parse', '--verify', child.baseline_head_ref).strip() == child.baseline_head_ref
        _prepare_binding_child_resume(root, store, store.load(snapshot.workflow_id), store.load_handoff(handoff.handoff_id))
        parent = load_session_state(root, 'parent')
        parent.status = 'waiting_child'
        save_session_state(root, parent)
        resumed.clear()
        resume_to_observation(root, monkeypatch, observe_writer, real_dispatch=True)
        repeated = load_session_state(root, child.session_id)
        assert repeated.status == 'completed' and child.session_id in resumed
        assert calls == [child.session_id]
        assert repeated.candidate_custody == custody
        assert repeated.verification_binding == binding
    else:
        assert not (root / 'tests/test_future.py').exists()
        assert all('test_future' not in command for command, _, _, _ in results)
        assert load_project_config(root).gates.steps[0].proof_id == 'foreign.future'
        assert load_task_plan(root)['tasks'][0]['status'] == 'pending'
    assert {name: (root / name).read_bytes() for name in ambient} == ambient
    assert {str(path): (path.read_bytes(), path.stat().st_mode) for path in shared} == before


def _assert_reused_session_authority(tmp_path, monkeypatch):
    from copy import deepcopy
    from auto_agents.config import load_task_plan
    import auto_agents.session as module

    root, first = project(tmp_path)
    second = deepcopy(first)
    second.session_id = 'second-child'
    config = load_project_config(root)
    config.gates.steps[0].args = ['--strict-markers']
    plan = load_task_plan(root)
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, second, config, plan)
    save_session_state(root, second)
    assert first.baseline_head_ref != second.baseline_head_ref
    ambient = _switch_ambient_binding_plan(root)
    orch = Orchestrator(root)
    session = Session(orch, mode='fix', auto_approve=True)
    observed, captures, writers = [], [], []
    execute = module.run_gate_plan
    retain = session._retain_resume_authority
    def observe(commands, *args, **kwargs):
        observed.extend((session._current_state.session_id, command) for command in commands)
        return execute(commands, *args, **kwargs)
    def capture(state):
        result = retain(state)
        original = session._resumed_verification_state
        captures.append((state.session_id, original.session_id, original.baseline_head_ref))
        assert original is not state
        return result
    def writer(request):
        writers.append(session._current_state.session_id)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(module, 'run_gate_plan', observe)
    monkeypatch.setattr(session, '_retain_resume_authority', capture)
    monkeypatch.setattr(orch, '_call_with_failover', writer)
    # Exercise fresh distinct histories, then repeated public calls on one object.
    for child in (first, second, first, first, second, second):
        result = session.resume(child.session_id)
        assert result.status == 'completed', result.to_dict()
        assert result.verification_binding['contract_revision'] == child.baseline_head_ref
        args = result.verification_binding['gates']['steps'][0]['args']
        assert ('--strict-markers' in args) == (child is second)
    assert session._resumed_verification_state is None
    assert writers == [first.session_id, second.session_id]
    assert all(requested == captured for requested, captured, _ in captures)
    for child in (first, second):
        commands = [command for sid, command in observed if sid == child.session_id and '--collect-only' not in command]
        assert commands
        assert all(('--strict-markers' in command) == (child is second) for command in commands)
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


def _assert_public_missing_scope_recovery(tmp_path, monkeypatch, shape):
    from copy import deepcopy
    from auto_agents.session_verification import fingerprint
    from test_engine_child_recovery import parent_workflow, resume_to_observation, ObservationBoundary

    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    if shape == 'missing_scope_requirement':
        handoff.payload.pop('task_id')
        handoff.payload['requirement_ids'] = ['REQ-owned']
        store.save_handoff(handoff)
    _binding_fixture(root, child)
    expected_scope = deepcopy(child.verification_binding.pop('task_scope'))
    child.verification_binding['schema_version'] = 13 if shape == 'missing_scope_current' else 11
    child.verification_binding['binding_fingerprint'] = fingerprint({
        key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
    if shape == 'missing_scope_unresolved':
        handoff.payload.pop('task_id')
        store.save_handoff(handoff)
    elif shape == 'missing_scope_conflict':
        handoff.payload['child_session_id'] = 'another-child'
        store.save_handoff(handoff)
    elif shape == 'missing_scope_fingerprint':
        child.verification_binding['binding_fingerprint'] = 'invalid-fingerprint'
    retained = deepcopy(child.verification_binding)
    save_session_state(root, child)
    _prepare_binding_child_resume(root, store, snapshot, handoff)
    ambient = _switch_ambient_binding_plan(root)
    handoff_path = root / '.auto-agents/state/handoffs' / (handoff.handoff_id + '.json')
    handoff_bytes = handoff_path.read_bytes()
    if shape in {'missing_scope_unresolved', 'missing_scope_conflict', 'missing_scope_fingerprint'}:
        for _ in range(2):
            saved = _assert_binding_blocked_before_execution(root, monkeypatch, parent=True)
            assert saved.verification_binding == retained
            diagnostic = saved.execution_log[-1]['diagnostic']
            assert diagnostic['handoff_id'] == handoff.handoff_id
            assert diagnostic['contract_fingerprint'] == retained['contract_fingerprint']
            assert diagnostic['retry_fix'] is False
        assert (root / 'value.py').read_text() == 'VALUE = 0\n'
        assert handoff_path.read_bytes() == handoff_bytes
    else:
        collab_loop = Session._phase_collab_loop
        def parent_boundary(self, state):
            if state.session_id == 'parent':
                raise ObservationBoundary()
            return collab_loop(self, state)
        monkeypatch.setattr(Session, '_phase_collab_loop', parent_boundary)
        calls = []
        def writer(state, prompt, candidate_root):
            calls.append(state.session_id)
            (candidate_root / 'value.py').write_text('VALUE = 1\n')
            return 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        resume_to_observation(root, monkeypatch, writer)
        saved = load_session_state(root, child.session_id)
        assert saved.status == 'completed', saved.to_dict()
        assert calls == [child.session_id]
        binding = saved.verification_binding
        assert binding['task_scope'] == expected_scope
        assert binding['required_proof_ids'] == ['owned.contract']
        for key in ('repository', 'session_id', 'workflow_id', 'original_handoff_id',
                    'authorization', 'contract_revision', 'contract_fingerprint', 'tasks', 'plan'):
            assert binding[key] == retained[key]
        assert binding['binding_fingerprint'] == fingerprint({
            key: value for key, value in binding.items() if key != 'binding_fingerprint'})
        # The recovered binding must round-trip and remain usable after the
        # parent has consumed delivery, with the ambient plan still switched.
        resume_to_observation(root, monkeypatch, writer)
        assert calls == [child.session_id]
        assert load_session_state(root, child.session_id).verification_binding == binding
    assert store.load_handoff(handoff.handoff_id).payload == handoff.payload
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('mismatch', ['authorization', 'environment', 'contract', 'candidate'])
def test_public_resume_rejects_mismatched_session_binding(tmp_path, monkeypatch, mismatch):
    root, child = project(tmp_path)
    _binding_fixture(root, child)
    if mismatch == 'authorization':
        child.authorization_policy['source'] = 'different authority'
    elif mismatch == 'environment':
        child.goal_execution_environment = {'mode': 'simulated', 'confirmed': True}
    elif mismatch == 'contract':
        child.verification_binding['gates']['steps'][0]['targets'] = []
    else:
        child.candidate_paths = {'value.py': 'unreceipted-foreign-content'}
        (root / 'value.py').write_text('VALUE = 99\n')
    save_session_state(root, child)
    ambient = _switch_ambient_binding_plan(root)
    saved = _assert_binding_blocked_before_execution(root, monkeypatch)
    assert {name: (root / name).read_bytes() for name in ambient} == ambient
    if mismatch == 'candidate':
        assert (root / 'value.py').read_text() == 'VALUE = 99\n'


@pytest.mark.parametrize('context_case', ['authorized', 'cwd_only', 'wrong_session', 'wrong_directory', 'environment'])
def test_public_resume_private_checkout_requires_original_execution_authority(tmp_path, monkeypatch, context_case):
    import shutil
    from dataclasses import replace
    from auto_agents.execution_binding import SessionExecutionBinding

    root, child = project(tmp_path)
    original_session = _binding_fixture(root, child)
    private = tmp_path / 'private-checkout'
    context = SessionExecutionBinding.for_checkout(original_session, child, private)
    shutil.copytree(root, private, symlinks=True)
    if context_case == 'cwd_only':
        context = None
    elif context_case == 'wrong_session':
        context = replace(context, session_id='foreign-child')
    elif context_case == 'wrong_directory':
        context = replace(context, execution_root=str(tmp_path / 'another-checkout'))
    elif context_case == 'environment':
        copied = load_session_state(private, child.session_id)
        copied.goal_execution_environment = {'mode': 'simulated', 'confirmed': True}
        save_session_state(private, copied)
    before = {name: (root / name).read_bytes() for name in
              ('value.py', '.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    calls = []
    def writer(self, request):
        calls.append(request.purpose)
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Repaired\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', writer)
    if context_case != 'authorized':
        def baseline(*args, **kwargs):
            pytest.fail('Private checkout authority must be validated before baseline capture')
        monkeypatch.setattr(Session, '_ensure_baseline', baseline)
    saved = Session(Orchestrator(private), mode='fix', auto_approve=True,
                    execution_binding=context).resume(child.session_id)
    if context_case == 'authorized':
        assert saved.status == 'completed', saved.to_dict()
        assert calls == ['fix']
        assert saved.verification_binding == child.verification_binding
        assert (private / 'value.py').read_text() == 'VALUE = 0\n'
        delivery = saved.candidate_custody
        assert git(Path(delivery['checkout']), 'show', delivery['delivered_revision'] + ':value.py') == 'VALUE = 1\n'
    else:
        assert saved.status == 'blocked', saved.to_dict()
        assert saved.resolution == 'verification_ownership'
        assert saved.execution_log[-1]['diagnostic']['session_id'] == child.session_id
        assert calls == []
        assert (private / 'value.py').read_text() == 'VALUE = 0\n'
    assert {name: (root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('boundary', ['before_record', 'after_snapshot', 'after_record'])
def test_candidate_receipt_excludes_intervening_foreign_content_index_and_modes(tmp_path, monkeypatch, boundary, failure):
    import base64
    import stat
    import auto_agents.session as session_module
    from auto_agents.session_candidate import GateSnapshotManager
    from test_engine_child_recovery import parent_workflow, resume_to_observation, configure_local_writer

    root, child = project(tmp_path, missing=failure)
    owned_test = root / 'tests/test_owned.py'
    owned_test.write_text(owned_test.read_text() +
        '    assert Path("value.py").stat().st_mode & 0o7777 == 0o750\n'
        '    assert Path("writer-link").is_symlink()\n'
        '    assert not Path("obsolete.txt").exists()\n')
    (root / 'obsolete.txt').write_bytes(b'retained deletion preimage')
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain file kind and mode contract')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    configure_local_writer(root, child, '''
assert Path('value.py').read_text() == 'VALUE = 0\\n'
Path('value.py').write_text('VALUE = 1\\n')
Path('value.py').chmod(0o640)
subprocess.run(['git','add','value.py'],check=True)
Path('value.py').chmod(0o750)
Path('writer-link').symlink_to('value.py')
Path('obsolete.txt').unlink()
''')
    store, _, handoff = parent_workflow(root, child)
    shared_head = head_ref(root)
    shared_refs = git(root, 'show-ref')
    observations = []
    def foreign_edit():
        (root / 'value.py').write_text('VALUE = 88\n')
        git(root, 'add', 'value.py')
        (root / 'value.py').write_text('VALUE = 99\n')
        (root / 'value.py').chmod(0o711)
        (root / 'late-foreign.txt').write_bytes(b'foreign\x00untracked')
        observations.append((root / '.git/index').read_bytes())
    record = session_module.record_candidate
    def record_with_interleaving(session, state, before):
        if boundary == 'before_record':
            foreign_edit()
        record(session, state, before)
        if boundary == 'after_record':
            # Include a same-path mode-only edit after receipt acceptance.
            (root / 'value.py').chmod(0o711)
            observations.append((root / '.git/index').read_bytes())
    monkeypatch.setattr(session_module, 'record_candidate', record_with_interleaving)
    create = GateSnapshotManager.create
    def snapshot_with_interleaving(manager, **kwargs):
        result = create(manager, **kwargs)
        if boundary == 'after_snapshot' and manager.plan_id.startswith('candidate-'):
            foreign_edit()
        return result
    monkeypatch.setattr(GateSnapshotManager, 'create', snapshot_with_interleaving)
    def writer(state, prompt, candidate_root):
        assert candidate_root != root
        assert (candidate_root / '.git').is_dir()
        assert (candidate_root / 'value.py').read_text() == 'VALUE = 0\n'
        return 'Fixed\nCOMMIT_MESSAGE: Repair owned value'
    resume_to_observation(root, monkeypatch, writer, real_dispatch=True)
    saved = load_session_state(root, child.session_id)
    assert (saved.status == 'completed') == (not failure), saved.to_dict()
    assert len(observations) == 1
    assert (root / '.git/index').read_bytes() == observations[0]
    assert head_ref(root) == shared_head
    if not failure:
        assert git(root, 'show-ref') == shared_refs
    assert stat.S_IMODE((root / 'value.py').stat().st_mode) == 0o711
    if boundary != 'after_record':
        assert (root / 'value.py').read_text() == 'VALUE = 99\n'
        assert git(root, 'show', ':value.py') == 'VALUE = 88\n'
        assert (root / 'late-foreign.txt').read_bytes() == b'foreign\x00untracked'
    else:
        assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    receipt = saved.candidate_custody['receipt']
    assert set(receipt['manifest']) == {'value.py', 'writer-link', 'obsolete.txt'}
    assert receipt['manifest']['obsolete.txt']['postimage']['worktree']['kind'] == 'absent'
    assert (root / 'obsolete.txt').read_bytes() == b'retained deletion preimage'
    entry = receipt['manifest']['value.py']
    assert base64.b64decode(entry['preimage']['worktree']['bytes']) == b'VALUE = 0\n'
    assert base64.b64decode(entry['postimage']['worktree']['bytes']) == b'VALUE = 1\n'
    assert entry['postimage']['worktree']['mode'] == 0o750
    assert entry['postimage']['index'] != entry['preimage']['index']
    assert receipt['manifest']['writer-link']['postimage']['worktree']['target'] == 'value.py'
    assert receipt['binding_fingerprint'] == saved.verification_binding['binding_fingerprint']
    assert git(Path(saved.candidate_custody['checkout']), 'show', receipt['source_revision'] + ':value.py') == 'VALUE = 1\n'
    if failure:
        assert store.load_handoff(handoff.handoff_id).result['rolled_back_paths'] == []


def test_candidate_publication_preserves_foreign_edit_after_final_snapshot(tmp_path, monkeypatch):
    test_candidate_receipt_excludes_intervening_foreign_content_index_and_modes(
        tmp_path, monkeypatch, 'after_snapshot', False)


def test_verification_snapshot_contains_only_owned_candidate_changes(tmp_path, monkeypatch):
    from test_engine_child_recovery import configure_local_writer, REAL_PROVIDER_CALL
    root, child = project(tmp_path)
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')")
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    index = (root / '.git/index').read_bytes()
    shared_head, refs = head_ref(root), git(root, 'show-ref')
    writer_roots = []
    def writer(self, request):
        # This public boundary assertion also runs against the base engine:
        # it requires no new custody implementation symbols or state fields.
        assert request.cwd != root, 'the child writer must own a private repository'
        assert not request.cwd.is_relative_to(root), 'new custody must use external managed runtime storage'
        writer_roots.append(request.cwd)
        assert (request.cwd / '.git').is_dir()
        assert (request.cwd / 'foreign.py').read_text() == 'VALUE = 7\n'
        (root / 'foreign-note.txt').write_bytes(b'concurrent foreign bytes')
        assert request.writer_boundary is not None
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', writer)
    result = Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
    assert result.status == 'completed', result.to_dict()
    assert len(writer_roots) == 1
    assert set(result.candidate_custody['receipt']['manifest']) == {'value.py'}
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    assert (root / 'foreign.py').read_text() == 'VALUE = 99\n'
    assert (root / 'foreign-note.txt').read_bytes() == b'concurrent foreign bytes'
    assert (root / '.git/index').read_bytes() == index
    assert head_ref(root) == shared_head
    assert git(root, 'show-ref') == refs
    registrations = list((root / '.auto-agents/state/custody').glob('*.json'))
    assert len(registrations) == 1
    registration = json.loads(registrations[0].read_text())
    assert registration['checkout'] == str(writer_roots[0])
    assert registration['repository'] == str(root)
    assert registration['session_id'] == child.session_id
    assert registration['inode'] == writer_roots[0].stat().st_ino


@pytest.mark.parametrize('candidate', ['product', 'provider_doc'])
def test_child_rollback_preserves_concurrent_foreign_index_worktree_and_untracked_bytes(
        tmp_path, monkeypatch, candidate):
    test_child_rollback_preserves_foreign_index_worktree_and_untracked_bytes(tmp_path, monkeypatch, candidate)


@pytest.mark.parametrize('location', ['runtime', 'legacy', 'unregistered', 'replaced'])
def test_public_resume_validates_registered_runtime_and_retains_legacy_custody(tmp_path, monkeypatch, location):
    import shutil

    root, child = project(tmp_path)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed' and calls == ['fix']
    checkout = Path(saved.candidate_custody['checkout'])
    assert not checkout.is_relative_to(root)
    receipt = saved.candidate_custody['receipt']
    registration = next((root / '.auto-agents/state/custody').glob('*.json'))
    if location == 'legacy':
        legacy = root / '.auto-agents/candidate-custody/retained/project'
        legacy.parent.mkdir(parents=True)
        shutil.move(checkout, legacy)
        saved.candidate_custody['checkout'] = str(legacy)
        registration.unlink()
    elif location == 'unregistered':
        registration.unlink()
    elif location == 'replaced':
        previous = checkout.with_name('retained-original')
        checkout.rename(previous)
        shutil.copytree(previous, checkout, symlinks=True)
    saved.status = 'failed'
    save_session_state(root, saved)
    before = {name: (root / name).read_bytes() for name in (
        '.git/index', 'value.py', 'foreign.py', '.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    if location in {'unregistered', 'replaced'}:
        def forbidden(*args, **kwargs):
            pytest.fail('Unregistered custody must block before baseline or writer')
        monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
    resumed, calls, _ = run_session(root, monkeypatch)
    if location in {'runtime', 'legacy'}:
        assert resumed.status == 'completed', resumed.to_dict()
        assert calls == []
        assert resumed.candidate_custody['receipt'] == receipt
        assert resumed.candidate_custody['checkout'] == saved.candidate_custody['checkout']
    else:
        assert resumed.status == 'blocked', resumed.to_dict()
        assert calls == []
        assert resumed.execution_log[-1]['diagnostic']['retry_fix'] is False
        assert resumed.candidate_custody['receipt'] == receipt
    assert {name: (root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize('handoff_history', ['expired', 'absent', 'legacy_receipt_missing'])
def test_public_unbound_handoff_preserves_foreign_work_after_checkpoint(tmp_path, monkeypatch, handoff_history):
    from auto_agents.workflow_runtime import WorkflowCoordinator
    from auto_agents.session_verification import candidate_snapshot
    from test_engine_child_recovery import parent_workflow, ObservationBoundary

    root, child = project(tmp_path)
    store, snapshot, handoff = parent_workflow(root, child)
    WorkflowCoordinator(Orchestrator(root))._ensure_handoff_checkpoint(snapshot, handoff)
    if handoff_history == 'legacy_receipt_missing':
        _binding_fixture(root, child)
        assert child.verification_binding
    else:
        child.baseline_git_ref = 'refs/auto-agents/gate-snapshots/expired'
        child.baseline_head_ref = child.lineage_head_ref = ''
        save_session_state(root, child)
        handoff.payload['head_before'] = child.baseline_git_ref if handoff_history == 'expired' else ''
        store.save_handoff(handoff)
    # The handoff predates these changes, and no writer has acquired ownership.
    (root / 'value.py').write_text('VALUE = 88\n')
    git(root, 'add', 'value.py')
    (root / 'value.py').write_text('VALUE = 99\n')
    if handoff_history == 'legacy_receipt_missing':
        child.candidate_paths = {'value.py': candidate_snapshot(Orchestrator(root))['value.py']}
        assert not child.candidate_custody
        save_session_state(root, child)
    (root / 'value.py').chmod(0o711)
    if handoff_history == 'legacy_receipt_missing':
        # Legacy hashes still match after foreign chmod; they cannot supply
        # the missing frozen writer receipt or authorize shared rollback.
        assert candidate_snapshot(Orchestrator(root))['value.py'] == child.candidate_paths['value.py']
    (root / 'late-foreign.txt').write_bytes(b'foreign\x00untracked')
    before = {name: (root / name).read_bytes() for name in (
        'value.py', 'late-foreign.txt', '.git/index',
        '.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    head, refs = head_ref(root), git(root, 'show-ref')
    def forbidden(*args, **kwargs):
        pytest.fail('Unbound recovery must block before baseline or provider execution')
    monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', forbidden)
    def parent_observation(self, state):
        assert state.session_id == 'parent'
        raise ObservationBoundary()
    monkeypatch.setattr(Session, '_phase_collab_loop', parent_observation)
    for _ in range(2):
        with pytest.raises(ObservationBoundary):
            Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
        saved = load_session_state(root, child.session_id)
        assert saved.status == 'blocked'
        assert saved.execution_log[-1]['diagnostic']['retry_fix'] is False
        assert bool(saved.verification_binding) == (handoff_history == 'legacy_receipt_missing')
        assert not saved.candidate_custody
        assert store.load_handoff(handoff.handoff_id).result.get('rolled_back_paths', []) == []
        assert {name: (root / name).read_bytes() for name in before} == before
        assert (root / 'value.py').stat().st_mode & 0o7777 == 0o711
        assert saved.candidate_paths == child.candidate_paths
        assert head_ref(root) == head
        assert git(root, 'show-ref') == refs


@pytest.mark.parametrize('change', ['content', 'index', 'unknown_receipt'])
def test_overlapping_or_unknown_ownership_blocks_without_overwriting_foreign_work(
        tmp_path, monkeypatch, change):
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign-note.txt').write_bytes(b'foreign untracked')
    shared = {path: (root / path).read_bytes() for path in (
        '.git/index', 'value.py', 'foreign.py', 'foreign-note.txt',
        '.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    shared_head = head_ref(root)
    record = session_module.record_candidate
    def overlap(session, state, before):
        assert session.project_root != root
        if change == 'unknown_receipt':
            session._candidate_receipt = None
        elif change == 'index':
            git(session.project_root, 'add', 'value.py')
        else:
            (session.project_root / 'value.py').write_text('VALUE = 44\n')
        record(session, state, before)
    monkeypatch.setattr(session_module, 'record_candidate', overlap)
    result, calls, _ = run_session(root, monkeypatch)
    assert result.status == 'blocked', result.to_dict()
    assert result.resolution == 'verification_ownership'
    diagnostic = result.execution_log[-1]['diagnostic']
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['retry_fix'] is False
    assert len(calls) == 1
    assert head_ref(root) == shared_head
    assert {path: (root / path).read_bytes() for path in shared} == shared


def test_parent_consumes_delivered_child_revision_without_shared_copyback(tmp_path, monkeypatch):
    from test_engine_child_recovery import parent_workflow, ObservationBoundary
    from test_engine_child_recovery import configure_local_writer, REAL_PROVIDER_CALL
    root, child = project(tmp_path)
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')")
    store, _, handoff = parent_workflow(root, child)
    before = head_ref(root)
    index = (root / '.git/index').read_bytes()
    observed = []
    def agent(self, request):
        if request.purpose.startswith('collab'):
            parent = load_session_state(root, 'parent')
            observed.append(request.cwd)
            assert request.cwd != root
            assert (root / 'value.py').read_text() == 'VALUE = 0\n'
            delivery = store.load_handoff(handoff.handoff_id).result['candidate_delivery']
            assert request.cwd != Path(delivery['checkout'])
            assert head_ref(request.cwd) == delivery['delivered_revision']
            assert parent.candidate_custody['consumed_delivery']['revision'] == head_ref(request.cwd)
            assert (request.cwd / 'value.py').read_text() == 'VALUE = 1\n'
            assert (root / 'value.py').read_text() == 'VALUE = 0\n'
            assert (root / '.git/index').read_bytes() == index
            raise ObservationBoundary()
        assert request.writer_boundary is not None
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', agent)
    for _ in range(2):
        with pytest.raises(ObservationBoundary):
            Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
        assert head_ref(root) == before
    assert len(observed) == 2
    assert observed[0] == observed[1]


def test_expired_legacy_baseline_cannot_adopt_migrated_ambient_lineage(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    child.baseline_git_ref = 'refs/auto-agents/gate-snapshots/expired'
    child.baseline_head_ref = child.lineage_head_ref = ''
    save_session_state(root, child)
    ambient = _switch_ambient_binding_plan(root)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'foreign current contract')
    before = head_ref(root)
    def forbidden(*args, **kwargs):
        pytest.fail('Unrecoverable retained authority must block before baseline or writer')
    monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', forbidden)
    for _ in range(2):
        result = Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
        assert result.status == 'blocked'
        assert result.execution_log[-1]['retry_fix'] is False
        assert result.execution_log[-1]['diagnostic']['session_id'] == child.session_id
        assert not result.verification_binding
        assert not result.lineage_head_ref
        assert head_ref(root) == before
        assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('baseline_ref', ['', 'refs/auto-agents/gate-snapshots/expired'])
@pytest.mark.parametrize('initial_layout', ['plain', 'file', 'symlink'])
def test_unborn_session_freezes_initial_source_without_shared_publication(
        tmp_path, monkeypatch, baseline_ref, initial_layout):
    from copy import deepcopy
    import auto_agents.session_verification as verification
    import os
    import shutil
    import stat

    root = _make_project(str(tmp_path))
    (root / '.conda').symlink_to(sys.prefix, target_is_directory=True)
    (root / 'value.py').write_text('VALUE = 0\n')
    (root / 'foreign.txt').write_bytes(b'initial staged\x00bytes')
    git(root, 'add', 'value.py', 'foreign.txt')
    (root / 'foreign.txt').write_bytes(b'initial worktree\x00bytes')
    (root / 'foreign.txt').chmod(0o640)
    foreign = tmp_path / 'foreign-assets'
    foreign.mkdir()
    (foreign / 'old.bin').write_bytes(b'foreign target\x00never seed')
    (foreign / 'old.bin').chmod(0o711)
    if initial_layout != 'plain':
        (root / 'assets').mkdir()
        (root / 'assets/old.bin').write_bytes(b'initial staged descendant')
        git(root, 'add', 'assets/old.bin')
        shutil.rmtree(root / 'assets')
        if initial_layout == 'symlink':
            (root / 'assets').symlink_to(foreign, target_is_directory=True)
        else:
            (root / 'assets').write_bytes(b'initial replacement\x00bytes')
            (root / 'assets').chmod(0o750)
    foreign_before = ((foreign / 'old.bin').read_bytes(), (foreign / 'old.bin').stat().st_mode)
    (root / 'tests').mkdir()
    (root / 'tests/test_initial.py').write_text(
        'from pathlib import Path\n'
        'def test_repair():\n'
        '    assert Path("value.py").read_text() == "VALUE = 1\\n"\n'
        '    assert Path("foreign.txt").read_bytes() == b"initial worktree\\x00bytes"\n'
        '    assert not Path("late-foreign.txt").exists()\n')
    config = load_project_config(root)
    config.gates.steps = [VerificationStep(runner='pytest', targets=['tests/test_initial.py::test_repair'],
        proof_id='initial.repair', levels=['affected', 'release'], impact_paths=['value.py'])]
    config.gates.verification_policy_version = 4
    config.gates.release_worker.enabled = False
    config.gates.release_worker.auto_start = False
    save_project_config(root, config)
    state = SessionState(session_id='initial-child', mode='fix', status='failed',
                         goal='Repair the existing value', auto_approve=True, baseline_git_ref=baseline_ref)
    save_session_state(root, state)
    initial_index = (root / '.git/index').read_bytes()
    initial_head = (root / '.git/HEAD').read_bytes()
    original_config = (root / '.auto-agents/config.json').read_bytes()
    calls = []
    def provider(request):
        calls.append(request.cwd)
        assert request.cwd != root
        assert (request.cwd / 'value.py').read_text() == 'VALUE = 0\n'
        assert (request.cwd / '.git').is_dir()
        assert not (request.cwd / '.git/objects/info/alternates').exists()
        assert stat.S_IMODE((request.cwd / 'foreign.txt').stat().st_mode) == 0o640
        if initial_layout == 'symlink':
            assert (request.cwd / 'assets').is_symlink()
            assert os.readlink(request.cwd / 'assets') == str(foreign)
            assert git(request.cwd, 'ls-tree', 'HEAD', '--', 'assets').startswith('120000 blob ')
        elif initial_layout == 'file':
            assert (request.cwd / 'assets').read_bytes() == b'initial replacement\x00bytes'
            assert stat.S_IMODE((request.cwd / 'assets').stat().st_mode) == 0o750
        if initial_layout != 'plain':
            assert git(request.cwd, 'ls-tree', '-r', 'HEAD', '--', 'assets/old.bin') == ''
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        (root / 'late-foreign.txt').write_bytes(b'concurrent\x00bytes')
        content = 'Repaired value.\nCOMMIT_MESSAGE: Repair initial value'
        request.output_path.write_text(content)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=content, stdout=content, returncode=0)
    orch = Orchestrator(root)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    with monkeypatch.context() as old:
        old.setattr(verification, '_PROOF_INVENTORY_VERSION', 2)
        result = Session(orch, mode='fix', auto_approve=True).resume(state.session_id)
    assert result.status == 'completed', result.to_dict()
    assert len(calls) == 1
    authority = deepcopy(result.verification_binding)
    retained = deepcopy(result.candidate_custody)
    assert authority['contract_revision'] == ''
    assert authority['proof_inventory_version'] == 2
    contexts = []
    executor = Orchestrator._gate_executor_context
    def observe(self, *args, **kwargs):
        contexts.append(kwargs.get('contract_fingerprint'))
        return executor(self, *args, **kwargs)
    monkeypatch.setattr(Orchestrator, '_gate_executor_context', observe)
    for resume_index in range(2):
        before_contexts = len(contexts)
        before_log = len(result.execution_log)
        result = Session(orch, mode='fix', auto_approve=True).resume(state.session_id)
        assert result.status == 'completed', result.to_dict()
        assert len(calls) == 1, 'inventory recovery must reuse the frozen candidate'
        binding = result.verification_binding
        assert binding['proof_inventory_version'] == 6
        for key in ('repository', 'authorization', 'tasks', 'task_scope', 'contract_revision',
                    'original_handoff_id', 'baseline_identity', 'execution_environment'):
            assert binding[key] == authority[key]
        assert {key: value for key, value in result.candidate_custody.items()
                if key != 'binding_migration'} == retained
        assert result.candidate_custody['binding_migration']['receipt'] == retained['receipt']
        fresh_verification = any(entry['action'] == 'inventory_migration_verify' and entry['result'] == 'pass'
                                 for entry in result.execution_log[before_log:])
        assert fresh_verification == (resume_index == 0)
        expected = verification.fingerprint([
            binding['binding_fingerprint'], retained['receipt']['fingerprint'],
            'session-write-boundary-v1', binding.get('proof_execution_context')])
        if resume_index == 0:
            assert contexts and all(identity == expected for identity in contexts)
        else:
            assert len(contexts) == before_contexts, 'matching durable verification must not execute again'
        assert binding['proof_sources']['tests/test_initial.py'] == (root / 'tests/test_initial.py').read_text()
    custody = result.candidate_custody
    private = Path(custody['checkout'])
    assert custody['initial_source'] is True
    assert list(custody['receipt']['manifest']) == ['value.py']
    assert custody['preimages']['foreign.txt']['worktree']['mode'] == 0o640
    if initial_layout != 'plain':
        assert 'assets/old.bin' not in custody['preimages']
        assert custody['preimages']['assets']['worktree']['kind'] == initial_layout
    assert git(private, 'show', custody['base_revision'] + ':value.py') == 'VALUE = 0\n'
    assert git(private, 'show', custody['delivered_revision'] + ':value.py') == 'VALUE = 1\n'
    assert result.baseline_git_ref != baseline_ref
    assert not head_ref(root)
    assert (root / '.git/HEAD').read_bytes() == initial_head
    assert (root / '.git/index').read_bytes() == initial_index
    assert (root / 'value.py').read_text() == 'VALUE = 0\n'
    assert (root / 'foreign.txt').read_bytes() == b'initial worktree\x00bytes'
    assert (root / 'late-foreign.txt').read_bytes() == b'concurrent\x00bytes'
    assert (root / '.auto-agents/config.json').read_bytes() == original_config
    assert ((foreign / 'old.bin').read_bytes(), (foreign / 'old.bin').stat().st_mode) == foreign_before
    assert stat.S_IMODE((root / 'foreign.txt').stat().st_mode) == 0o640
    if initial_layout == 'symlink':
        assert (root / 'assets').is_symlink()
        assert os.readlink(root / 'assets') == str(foreign)
    elif initial_layout == 'file':
        assert (root / 'assets').read_bytes() == b'initial replacement\x00bytes'
        assert stat.S_IMODE((root / 'assets').stat().st_mode) == 0o750


@pytest.mark.parametrize('entrypoint', ['session', 'workflow'])
def test_completed_parent_resume_preserves_shared_work_after_private_delivery(tmp_path, monkeypatch, entrypoint):
    from auto_agents.workflow_runtime import WorkflowCoordinator
    from test_engine_child_recovery import parent_workflow

    root, child = project(tmp_path)
    # Retain the executable proof unchanged while exercising completion recovery.
    config = load_project_config(root)
    config.gates.allow_agent_updates = False
    save_project_config(root, config)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain completion verification contract')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    store, snapshot, handoff = parent_workflow(root, child)
    parent = load_session_state(root, 'parent')
    parent.goal = 'Confirm the delivered value against the existing checks.'
    save_session_state(root, parent)
    shared_head = head_ref(root)
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        assert request.cwd != root
        if request.purpose == 'fix':
            (request.cwd / 'value.py').write_text('VALUE = 1\n')
            reply = 'Fixed value.\nCOMMIT_MESSAGE: Repair owned value'
        else:
            assert request.purpose.startswith('collab')
            assert (request.cwd / 'value.py').read_text() == 'VALUE = 1\n'
            reply = 'GOAL_ACHIEVED: The delivered value passes its retained checks.\n'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    result = Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    assert result.status == 'completed', result.to_dict()
    assert store.load(snapshot.workflow_id).status == 'completed'
    assert store.events(snapshot.workflow_id)[-1]['kind'] == 'workflow_terminal'
    assert store.load_handoff(handoff.handoff_id).result['status'] == 'completed'
    assert calls == ['fix', 'collab']
    private = Path(result.candidate_custody['checkout'])
    completion_revision = head_ref(private)
    committed = json.loads(git(private, 'show',
        completion_revision + ':.auto-agents/state/sessions/parent/session_state.json'))
    assert committed['status'] == 'completed'
    assert head_ref(root) == shared_head

    (root / 'value.py').write_text('VALUE = 77\n')
    (root / 'foreign.py').write_text('VALUE = 8\n')
    git(root, 'add', 'value.py', 'foreign.py')
    (root / 'value.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').write_text('VALUE = 9\n')
    (root / 'late-foreign.txt').write_bytes(b'late foreign\x00bytes')
    index = (root / '.git/index').read_bytes()
    shared = {name: (root / name).read_bytes() for name in (
        'value.py', 'foreign.py', 'late-foreign.txt', '.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    def no_input(*args, **kwargs):
        pytest.fail('Completed private recovery must not request another confirmation')
    for _ in range(2):
        orch = Orchestrator(root, user_input_fn=no_input)
        if entrypoint == 'session':
            resumed = Session(orch, mode='collab', auto_approve=True).resume('parent')
        else:
            resumed = WorkflowCoordinator(orch).resume_workflow(snapshot.workflow_id)
        assert resumed.status == 'completed', resumed.to_dict()
        assert head_ref(root) == shared_head
        assert head_ref(private) == completion_revision
        assert (root / '.git/index').read_bytes() == index
        assert {name: (root / name).read_bytes() for name in shared} == shared
        assert calls == ['fix', 'collab']
        assert store.load(snapshot.workflow_id).status == 'completed'
        assert store.active() is None


@pytest.mark.parametrize('staged', [False, True])
def test_public_child_receipt_materializes_directory_to_file_replacement(tmp_path, monkeypatch, staged):
    import base64
    import shutil
    from test_engine_child_recovery import parent_workflow, resume_to_observation, configure_local_writer

    root, child = project(tmp_path)
    (root / 'assets').mkdir()
    (root / 'assets/old.json').write_bytes(b'{"retained": true}\n')
    check = root / 'tests/test_owned.py'
    check.write_text(check.read_text() +
        '    assert Path("assets").is_file()\n'
        '    assert Path("assets").read_bytes() == b"replacement\\x00bytes"\n'
        '    assert not Path("assets/old.json").exists()\n')
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain directory replacement contract')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    configure_local_writer(root, child, '''
assert Path('assets/old.json').read_bytes() == b'{"retained": true}\\n'
shutil.rmtree('assets')
Path('assets').write_bytes(b'replacement\\x00bytes')
Path('value.py').write_text('VALUE = 1\\n')
if STAGED: subprocess.run(['git','add','-A','--','assets'],check=True)
'''.replace('STAGED', repr(staged)))
    store, _, handoff = parent_workflow(root, child)
    (root / 'assets/old.json').write_bytes(b'foreign staged')
    git(root, 'add', 'assets/old.json')
    (root / 'assets/old.json').write_bytes(b'foreign worktree')
    shared_index = (root / '.git/index').read_bytes()
    shared_head = head_ref(root)
    observed = []
    def writer(state, prompt, candidate_root):
        assert (candidate_root / 'assets/old.json').read_bytes() == b'{"retained": true}\n'
        return 'Replaced the directory.\nCOMMIT_MESSAGE: Replace owned assets'
    def observe(request):
        observed.append(request.cwd)
        assert request.cwd != root
        assert (request.cwd / 'assets').is_file()
        assert (request.cwd / 'assets').read_bytes() == b'replacement\x00bytes'
        assert not (request.cwd / 'assets/old.json').exists()
    resume_to_observation(root, monkeypatch, writer, observe=observe, real_dispatch=True)
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'completed', saved.to_dict()
    assert len(observed) == 1
    receipt = saved.candidate_custody['receipt']
    assert receipt['manifest']['assets']['preimage']['worktree']['kind'] == 'directory'
    replacement = receipt['manifest']['assets']['postimage']
    assert replacement['worktree']['kind'] == 'file'
    assert base64.b64decode(replacement['worktree']['bytes']) == b'replacement\x00bytes'
    removed = receipt['manifest']['assets/old.json']
    assert base64.b64decode(removed['preimage']['worktree']['bytes']) == b'{"retained": true}\n'
    assert removed['postimage']['worktree'] == {'kind': 'absent'}
    assert bool(replacement['index']) == staged
    assert bool(removed['postimage']['index']) != staged
    assert store.load_handoff(handoff.handoff_id).result['status'] == 'completed'
    assert head_ref(root) == shared_head
    assert (root / '.git/index').read_bytes() == shared_index
    assert (root / 'assets').is_dir()
    assert (root / 'assets/old.json').read_bytes() == b'foreign worktree'


@pytest.mark.parametrize('staged', [False, True])
@pytest.mark.parametrize('boundary', ['after_record', 'before_verify', 'before_consume'])
def test_directory_symlink_receipt_never_claims_or_chmods_foreign_descendants(
        tmp_path, monkeypatch, staged, boundary):
    import base64
    import os
    import shutil
    import stat
    import auto_agents.session_candidate as custody
    import auto_agents.gate_execution as gates
    from test_engine_child_recovery import parent_workflow, resume_to_observation, configure_local_writer

    root, child = project(tmp_path)
    (root / 'assets/nested').mkdir(parents=True)
    old = root / 'assets/nested/old.json'
    old.write_bytes(b'retained private entry')
    check = root / 'tests/test_owned.py'
    check.write_text(check.read_text() +
        '    assert Path("assets").is_symlink()\n')
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'retain directory to symlink contract')
    child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
    save_session_state(root, child)
    configure_local_writer(root, child, '''
assert Path('assets/nested/old.json').read_bytes() == b'retained private entry'
shutil.rmtree('assets')
Path('assets').symlink_to(SHARED_ASSETS,target_is_directory=True)
Path('value.py').write_text('VALUE = 1\\n')
if STAGED: subprocess.run(['git','add','-A','--','assets'],check=True)
'''.replace('STAGED', repr(staged)).replace('SHARED_ASSETS', repr(str(root / 'assets'))))
    store, _, handoff = parent_workflow(root, child)
    old.write_bytes(b'foreign staged')
    git(root, 'add', 'assets/nested/old.json')
    old.write_bytes(b'foreign worktree')
    old.chmod(0o600)
    shared_head, shared_refs = head_ref(root), git(root, 'show-ref')
    shared_index = (root / '.git/index').read_bytes()
    injections = []
    def late_foreign_edit():
        if injections:
            return
        old.write_bytes(b'late foreign worktree')
        old.chmod(0o711)
        (root / 'assets/late.txt').write_bytes(b'late foreign untracked')
        injections.append(True)

    validate = custody.validate_receipt
    def validation(state):
        validate(state)
        if boundary == 'after_record' and state.candidate_custody.get('receipt'):
            late_foreign_edit()
    monkeypatch.setattr(custody, 'validate_receipt', validation)
    install = gates.install_dependency_links
    def prepare_sandbox(sandbox, dependencies):
        install(sandbox, dependencies)
        if boundary == 'before_verify' and (sandbox / 'assets').is_symlink():
            late_foreign_edit()
    monkeypatch.setattr(gates, 'install_dependency_links', prepare_sandbox)
    clone = custody._clone
    def clone_for_consumption(source, revision, destination):
        result = clone(source, revision, destination)
        if boundary == 'before_consume' and (destination / 'assets').is_symlink():
            late_foreign_edit()
        return result
    monkeypatch.setattr(custody, '_clone', clone_for_consumption)

    # Observe the real chmod operations as well as final bytes: restoring a
    # mode and changing it back would still violate private custody.
    chmod, fchmod = os.chmod, os.fchmod
    foreign_inode = (old.stat().st_dev, old.stat().st_ino)
    def assert_private(info):
        assert (info.st_dev, info.st_ino) != foreign_inode
    def checked_chmod(path, mode, *args, **kwargs):
        if path != old:
            assert_private(os.stat(path, dir_fd=kwargs.get('dir_fd'),
                                   follow_symlinks=kwargs.get('follow_symlinks', True)))
        return chmod(path, mode, *args, **kwargs)
    def checked_fchmod(descriptor, mode):
        assert_private(os.fstat(descriptor))
        return fchmod(descriptor, mode)
    monkeypatch.setattr(os, 'chmod', checked_chmod)
    monkeypatch.setattr(os, 'fchmod', checked_fchmod)

    def writer(state, prompt, candidate_root):
        assert (candidate_root / 'assets/nested/old.json').read_bytes() == b'retained private entry'
        return 'Replaced owned directory.\nCOMMIT_MESSAGE: Replace owned assets'
    observed = []
    def observe(request):
        observed.append(request.cwd)
        assert request.cwd != root
        assert (request.cwd / 'assets').is_symlink()
        assert os.readlink(request.cwd / 'assets') == str(root / 'assets')
        assert old.read_bytes() == b'late foreign worktree'
        assert stat.S_IMODE(old.stat().st_mode) == 0o711
    resume_to_observation(root, monkeypatch, writer, observe=observe, real_dispatch=True)
    saved = load_session_state(root, child.session_id)
    assert saved.status == 'completed', saved.to_dict()
    assert len(observed) == len(injections) == 1
    receipt = saved.candidate_custody['receipt']
    manifest = receipt['manifest']
    assert set(manifest) == {'assets', 'assets/nested', 'assets/nested/old.json', 'value.py'}
    assert manifest['assets']['preimage']['worktree']['kind'] == 'directory'
    assert manifest['assets']['postimage']['worktree']['target'] == str(root / 'assets')
    for path in ('assets/nested', 'assets/nested/old.json'):
        assert manifest[path]['postimage']['worktree'] == {'kind': 'absent'}
    removed = manifest['assets/nested/old.json']
    assert base64.b64decode(removed['preimage']['worktree']['bytes']) == b'retained private entry'
    assert bool(removed['postimage']['index']) != staged
    assert bool(manifest['assets']['postimage']['index']) == staged
    assert git(Path(saved.candidate_custody['checkout']), 'ls-tree', '-r',
               receipt['source_revision'], '--', 'assets').split()[0] == '120000'
    assert store.load_handoff(handoff.handoff_id).result['status'] == 'completed'
    assert head_ref(root) == shared_head
    assert git(root, 'show-ref') == shared_refs
    assert (root / '.git/index').read_bytes() == shared_index
    assert (root / 'assets/late.txt').read_bytes() == b'late foreign untracked'
    assert old.read_bytes() == b'late foreign worktree'
    assert stat.S_IMODE(old.stat().st_mode) == 0o711


@pytest.mark.parametrize('field,value', [
    ('kind', 'lint'), ('requires', ['network']),
    ('operator_input_bindings', [{'name': 'retained-input', 'value': 'different'}]),
    ('command', 'python -m pytest tests/test_owned.py --strict-markers'),
])
def test_public_resume_rejects_conflicting_retained_proof_definitions(tmp_path, monkeypatch, field, value):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    config = load_project_config(root)
    plan = load_task_plan(root)
    plan['verification_steps'][0][field] = value
    _retain_contract(root, child, config, plan)
    ambient = _switch_ambient_binding_plan(root)
    saved = _assert_binding_blocked_before_execution(root, monkeypatch)
    assert saved.verification_binding == {}, 'rejected inventory must not be partially persisted'
    assert saved.execution_log[-1]['diagnostic']['proof_id'] == 'owned.contract'
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


@pytest.mark.parametrize('legacy', [False, True, 'custody'])
def test_public_resume_seals_unprojected_proof_graph(tmp_path, monkeypatch, legacy):
    from copy import deepcopy
    from auto_agents.config import load_task_plan
    from auto_agents.models import GateParallelGroup
    from auto_agents.session_verification import fingerprint

    root, child = project(tmp_path)
    config = load_project_config(root)
    future = VerificationStep(proof_id='foreign.future', runner='pytest',
        targets=['tests/test_future.py::test_future'], levels=['release'])
    config.gates.steps.append(future)
    command = './.conda/bin/python -m pytest -q tests/test_owned.py'
    config.gates.parallel_groups = [GateParallelGroup(name='manual-retained', commands=[command])]
    child.fix_verify_command = command
    plan = load_task_plan(root)
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Foreign pending obligation',
        'workflow_id': 'foreign-workflow', 'status': 'pending', 'requirement_ids': ['REQ-foreign'],
        'verification_refs': ['foreign.future']})
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    if legacy == 'custody':
        prerequisite_marker = _retain_foreign_prerequisite(root, child)
        child.lineage_head_ref = child.baseline_head_ref
        child.baseline_git_ref = child.baseline_head_ref = ''
        _binding_fixture(root, child)
        original_baseline = deepcopy(child.verification_binding['baseline_identity'])
        assert original_baseline['git_ref'] == original_baseline['head_ref'] == ''
        child, calls, _ = run_session(root, monkeypatch)
        assert child.status == 'completed' and calls == ['fix']
        assert child.baseline_git_ref and child.baseline_head_ref
        assert child.verification_binding['baseline_identity'] == original_baseline
        captured_baseline = child.baseline_git_ref
    if legacy:
        _binding_fixture(root, child)
        for key in ('proof_graph', 'proof_inventory_version', 'required_references'):
            child.verification_binding.pop(key, None)
        child.verification_binding['binding_fingerprint'] = fingerprint({
            key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
        if legacy == 'custody':
            _retain_legacy_prerequisite_owners(child)
            assert prerequisite_marker.read_text() == 'VALUE = 1\n'
            prerequisite_marker.unlink()
        save_session_state(root, child)
    retained_custody = deepcopy(child.candidate_custody)
    ambient = _switch_ambient_binding_plan(root)
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00untracked')
    index = (root / '.git/index').read_bytes()
    shared_head, shared_refs = head_ref(root), git(root, 'show-ref')
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ([] if legacy == 'custody' else ['fix'])
    binding = saved.verification_binding
    assert 'proof_graph' in binding, 'public resume must recover the retained proof inventory'
    graph = binding['proof_graph']
    assert graph['source']['revision'] == child.baseline_head_ref
    assert {step['proof_id'] for step in graph['gates']['steps']} == (
        {'owned.contract', 'foreign.future', 'shared.setup'} if legacy == 'custody'
        else {'owned.contract', 'foreign.future'})
    assert graph['proof_owners']['foreign.future'][0]['task_id'] == 'task-foreign'
    assert graph['commands'][command][0]['task_id'] == 'task-owned'
    assert binding['required_proof_ids'] == (['owned.contract', 'shared.setup'] if legacy == 'custody'
                                             else ['owned.contract'])
    assert binding['task_ids'] == (['task-foreign', 'task-owned'] if legacy == 'custody' else ['task-owned'])
    assert binding['required_references']['tests/test_owned.py::test_owned']['kind'] == 'selector'
    assert not (root / 'tests/test_future.py').exists()
    if legacy == 'custody':
        assert binding['baseline_identity'] == original_baseline
        assert saved.baseline_git_ref == captured_baseline
        _assert_prerequisite_owner_enrichment(saved, prerequisite_marker)
        _assert_migrated_candidate_reused(root, monkeypatch, saved, retained_custody)
    assert (root / '.git/index').read_bytes() == index
    assert (root / 'foreign.py').read_text() == 'VALUE = 99\n'
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert (root / 'foreign-note.txt').read_bytes() == b'foreign\x00untracked'
    assert head_ref(root) == shared_head and git(root, 'show-ref') == shared_refs
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


def _retain_foreign_prerequisite(root, child):
    from auto_agents.config import load_task_plan

    marker = ExecutionMarker(root.parent / 'shared-prerequisite-executed')
    (root / 'tests/test_shared.py').write_text(
        'from pathlib import Path\ndef test_setup():\n'
        '    value = Path("value.py").read_text()\n'
        '    assert value == "VALUE = 1\\n"\n'
        f'    {marker.source("value")}\n')
    config, plan = load_project_config(root), load_task_plan(root)
    config.gates.steps[0].depends_on_proofs = ['shared.setup']
    config.gates.steps.append(VerificationStep(proof_id='shared.setup', runner='pytest',
        targets=['tests/test_shared.py::test_setup'], levels=['affected', 'release']))
    foreign = next((task for task in plan['tasks'] if task['task_id'] == 'task-foreign'), None)
    if foreign is None:
        foreign = {'task_id': 'task-foreign', 'title': 'Existing shared prerequisite',
                   'workflow_id': 'foreign-workflow', 'status': 'pending',
                   'requirement_ids': ['REQ-foreign'], 'verification_refs': []}
        plan['tasks'].append(foreign)
    foreign['verification_refs'].append('shared.setup')
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text(json.dumps({'task_id': 'task-owned'}))
    return marker


def _retain_legacy_prerequisite_owners(state):
    """The original inventory required prerequisites without propagating owners."""
    from auto_agents.session_verification import fingerprint

    binding = state.verification_binding
    assert binding['required_proof_ids'] == ['owned.contract', 'shared.setup']
    for key in ('proof_graph', 'proof_inventory_version', 'required_references'):
        binding.pop(key, None)
    binding['proof_owners'] = {'owned.contract': binding['proof_owners']['owned.contract']}
    binding['task_ids'], binding['requirement_ids'] = ['task-owned'], ['REQ-owned']
    binding['binding_fingerprint'] = fingerprint({k: v for k, v in binding.items() if k != 'binding_fingerprint'})
    _retain_candidate_binding_identity(state)


def _assert_prerequisite_owner_enrichment(saved, marker):
    binding = saved.verification_binding
    original = saved.candidate_custody['binding_migration']['original_binding']
    assert original['task_ids'] == ['task-owned']
    assert original['requirement_ids'] == ['REQ-owned']
    assert binding['task_ids'] == ['task-foreign', 'task-owned']
    assert binding['requirement_ids'] == ['REQ-foreign', 'REQ-owned']
    assert binding['required_proof_ids'] == original['required_proof_ids'] == ['owned.contract', 'shared.setup']
    assert binding['regression_dependencies']['owned.contract'] == ['shared.setup']
    assert {owner['task_id'] for owner in binding['proof_owners']['shared.setup']} == {'task-owned', 'task-foreign'}
    assert binding['task_scope'] == original['task_scope'] == {'task_ids': ['task-owned'], 'requirement_ids': []}
    for key in ('tasks', 'plan', 'gates', 'authorization', 'original_handoff_id', 'contract_revision'):
        assert binding[key] == original[key]
    assert marker.read_text() == 'VALUE = 1\n', 'recovered prerequisite must execute on the retained candidate'


def _assert_receipt_policy_mismatch(tmp_path, monkeypatch, policy, change, *, boundary, switch_before):
    from copy import deepcopy
    from auto_agents.config import load_task_plan, requirements_trace_path
    from auto_agents.requirements import requirement_contract_sha256
    import auto_agents.session_candidate as candidate

    root, child = project(tmp_path)
    child.goal_execution_environment = {'mode': 'real', 'confirmed': True, 'source': 'explicit_goal'}
    config, plan = load_project_config(root), load_task_plan(root)
    marker = ExecutionMarker(tmp_path / 'release-runs')
    (root / 'tests/test_release.py').write_text(
        'from pathlib import Path\ndef test_release():\n'
        '    ' + marker.source(repr('release\n'), append=True) + '\n'
        '    assert True\n')
    config.gates.release_verification_mode = policy
    config.gates.distributed.mode = 'off'
    config.gates.steps.append(VerificationStep(proof_id='foreign.release', runner='pytest',
        targets=['tests/test_release.py'], levels=['release'], impact_paths=['unrelated.py']))
    plan['tasks'].append({'task_id': 'task-foreign', 'title': 'Existing release regression',
        'workflow_id': 'foreign-workflow', 'status': 'pending',
        'requirement_ids': ['REQ-foreign'], 'verification_refs': ['foreign.release']})
    rows = [{'id': 'REQ-owned', 'text': 'Repair owned value', 'source': 'spec'},
            {'id': 'REQ-foreign', 'text': 'Existing release contract', 'source': 'spec'}]
    for task, row in zip(plan['tasks'], rows):
        task['requirement_proofs'] = [{'requirement_id': row['id'],
            'requirement_contract_sha256': requirement_contract_sha256(row),
            'evidence_refs': task['verification_refs']}]
    trace = requirements_trace_path(root)
    trace.write_text(json.dumps({'requirements': rows}))
    plan['verification_steps'] = [step.to_dict() for step in config.gates.steps]
    _retain_contract(root, child, config, plan)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text(json.dumps({'task_id': 'task-owned'}))

    def switch():
        _switch_ambient_binding_plan(root)
        ambient = load_project_config(root)
        ambient.gates.release_verification_mode = 'deferred' if policy == 'blocking' else 'blocking'
        ambient.gates.distributed.mode = 'auto'
        ambient.gates.distributed.extra_environment_denylist = ['LANG']
        save_project_config(root, ambient)
    if switch_before:
        switch()
    identities = []
    original_identity = candidate.verification_identity
    def identity(session, state, **kwargs):
        ambient = session.config.gates
        before = deepcopy(ambient.to_dict())
        try:
            result = original_identity(session, state, **kwargs)
            identities.append(result)
            return result
        finally:
            assert session.config.gates is ambient
            assert ambient.to_dict() == before
    monkeypatch.setattr(candidate, 'verification_identity', identity)
    with monkeypatch.context() as interrupt:
        if boundary == 'delivery':
            deliver = candidate.deliver_candidate
            def stop(session, state, message):
                deliver(session, state, message)
                raise KeyboardInterrupt()
            interrupt.setattr(candidate, 'deliver_candidate', stop)
        elif boundary != 'completed':
            def stop(self, state):
                raise KeyboardInterrupt()
            interrupt.setattr(Session, '_run_session_persistence_action', stop)
        paused, calls, _ = run_session(root, interrupt)
    assert paused.status == ('completed' if boundary == 'completed' else 'paused'), paused.to_dict()
    assert calls == ['fix'] and paused.full_verify is False
    assert marker.exists() == (policy == 'blocking'), 'actual initial runner scope must use retained policy'
    assert paused.verification_binding['task_ids'] == ['task-owned']
    assert paused.verification_binding['required_proof_ids'] == ['owned.contract']
    evidence = [entry for entry in paused.execution_log if entry.get('action') == 'receipt_verification']
    assert len(evidence) == 1 and evidence[0]['verification']['ok']
    original_key = evidence[0]['identity']
    if boundary == 'legacy_pass':
        # Preserve the genuine old result, without attesting its scope as current.
        evidence[0]['identity'] = 'older-receipt-context'
        save_session_state(root, paused)
    if not switch_before:
        switch()
    if change == 'changed':
        rows[-1]['text'] = 'Changed release contract'
    elif change == 'missing':
        rows.pop()
    trace.write_text(json.dumps({'requirements': rows}))
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00work')
    protected = {path: (root / path).read_bytes() for path in (
        'value.py', 'foreign.py', 'foreign-note.txt', '.git/index',
        '.auto-agents/config.json', '.auto-agents/state/task_plan.json',
        trace.relative_to(root).as_posix())}
    refs = git(root, 'show-ref')
    custody = deepcopy(paused.candidate_custody)
    retained_fields = {key: deepcopy(getattr(paused, key)) for key in (
        'current_attempt', 'auto_approve', 'authorization_policy', 'goal',
        'goal_execution_environment', 'full_verify', 'verification_binding')}
    if marker.exists():
        marker.unlink()
    verified, delivered = [], []
    verify, deliver = Session._run_verify, candidate.deliver_candidate
    blocked = policy == 'blocking' and change != 'unchanged'
    def forbidden(*args, **kwargs):
        pytest.fail('Required release authority must reject before execution or completion side effects')
    def observe_verify(self, *args, **kwargs):
        assert boundary == 'legacy_pass', 'matching evidence must be reused'
        result = verify(self, *args, **kwargs)
        verified.append(result)
        return result
    def observe_delivery(session, state, message):
        delivered.append(state.session_id)
        return deliver(session, state, message)
    monkeypatch.setattr(Session, '_run_verify', forbidden if blocked else observe_verify)
    monkeypatch.setattr(candidate, 'deliver_candidate', forbidden if blocked else observe_delivery)
    if blocked:
        monkeypatch.setattr(Session, '_ensure_baseline', forbidden)
        monkeypatch.setattr(Session, '_run_session_persistence_action', forbidden)
    for _ in range(2):
        saved, calls, orch = run_session(root, monkeypatch)
        assert calls == []
        assert saved.status == ('blocked' if blocked else 'completed'), saved.to_dict()
        assert orch.config.gates.to_dict() == load_project_config(root).gates.to_dict()
        assert {key: getattr(saved, key) for key in retained_fields} == retained_fields
        assert saved.candidate_custody['receipt'] == custody['receipt']
        if blocked:
            assert saved.candidate_custody == custody
            diagnostic = saved.execution_log[-1]['diagnostic']
            assert diagnostic['task_id'] == 'task-foreign'
            assert diagnostic['requirement_id'] == 'REQ-foreign'
            assert diagnostic['session_id'] == child.session_id
            assert diagnostic['contract_fingerprint'] and diagnostic['retry_fix'] is False
        else:
            assert identities[-1] == original_key
    assert len(verified) == (1 if boundary == 'legacy_pass' else 0)
    assert len(delivered) == (0 if blocked or boundary == 'completed' else 1)
    if boundary == 'legacy_pass':
        assert verified[0]['ok']
        assert ('foreign.release' in verified[0]['proof_ids']) == (policy == 'blocking')
        records = [entry for entry in saved.execution_log if entry.get('action') == 'receipt_verification']
        assert len(records) == 2 and records[0]['identity'] == 'older-receipt-context'
        assert records[1]['identity'] == original_key and records[1]['verification'] == verified[0]
        if policy == 'blocking' and not marker.exists():
            assert verified[0]['certificate_hits'] > 0
    else:
        assert not marker.exists(), 'matching receipts must not replay test bodies'
    assert {path: (root / path).read_bytes() for path in protected} == protected
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert git(root, 'show-ref') == refs


def _assert_receipt_current_authority(tmp_path, monkeypatch, outcome):
    from copy import deepcopy
    from auto_agents.config import load_task_plan, requirements_trace_path
    from auto_agents.requirements import requirement_contract_sha256
    from auto_agents.workflow_runtime import WorkflowCoordinator
    import auto_agents.session_candidate as candidate

    root, child = project(tmp_path)
    config, plan = load_project_config(root), load_task_plan(root)
    owned = {'id': 'REQ-owned', 'text': 'Repair the owned value', 'source': 'spec'}
    foreign = {'id': 'REQ-foreign', 'text': 'Separate work', 'source': 'spec'}
    trace = requirements_trace_path(root)
    trace.write_text(json.dumps({'requirements': [owned, foreign]}))
    plan['tasks'][0]['requirement_proofs'] = [{
        'requirement_id': owned['id'], 'requirement_contract_sha256': requirement_contract_sha256(owned),
        'evidence_refs': plan['tasks'][0]['verification_refs']}]
    marker = ExecutionMarker(tmp_path / 'release-executions')
    policy_case = outcome in {'full_failure', 'full_pass', 'persisted_full', 'affected_policy'}
    if policy_case:
        (root / 'tests/test_release.py').write_text(
            'from pathlib import Path\ndef test_release():\n'
            '    ' + marker.source("Path('value.py').read_text()", append=True) + '\n'
            + ('    assert "VALUE = 0" in Path("value.py").read_text()\n'
               if outcome == 'full_failure' else '    assert True\n'))
        config.gates.steps.append(VerificationStep(proof_id='release.regression', runner='pytest',
            targets=['tests/test_release.py'], levels=['release'], impact_paths=['unrelated.py']))
        config.gates.release_verification_mode = 'deferred'
    _retain_contract(root, child, config, plan)
    child.full_verify = outcome == 'persisted_full'
    save_session_state(root, child)
    with monkeypatch.context() as interrupt:
        if outcome == 'restored_contract':
            record_receipt = candidate.record_receipt
            def missing_contract(session, state):
                record_receipt(session, state)
                trace.write_text(json.dumps({'requirements': [foreign]}))
            interrupt.setattr(candidate, 'record_receipt', missing_contract)
            record = candidate.record_verification
            def stop(session, state, result, **kwargs):
                record(session, state, result, **kwargs)
                assert result['retry_fix'] is False and not result['ok']
                raise KeyboardInterrupt()
            interrupt.setattr(candidate, 'record_verification', stop)
        else:
            def stop(self, state):
                raise KeyboardInterrupt()
            interrupt.setattr(Session, '_run_session_persistence_action', stop)
        paused, calls, _ = run_session(root, interrupt)
    assert paused.status == 'paused' and calls == ['fix'], paused.to_dict()
    evidence = [entry for entry in paused.execution_log if entry.get('action') == 'receipt_verification']
    assert len(evidence) == 1
    assert evidence[0]['verification']['ok'] == (outcome != 'restored_contract')
    retained = deepcopy(paused.candidate_custody)
    attempts = paused.current_attempt
    # A failed release check must stop on the retained attempt, without a writer retry.
    if outcome == 'full_failure':
        paused.hard_ceiling = attempts
        save_session_state(root, paused)
    ambient = _switch_ambient_binding_plan(root)
    if outcome == 'foreign_contract':
        trace.write_text(json.dumps({'requirements': [dict(owned, status='done', notes='non-normative'),
                                                     dict(foreign, text='Different foreign scope')]}))
    protected_trace = trace.read_bytes()
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00bytes')
    protected = {name: (root / name).read_bytes() for name in (*ambient, '.git/index', 'foreign.py', 'foreign-note.txt')}
    refs = git(root, 'show-ref')
    if marker.exists():
        marker.unlink()
    observed = []
    verify = Session._run_verify
    def observe(self, *args, **kwargs):
        observed.append(self._current_state.full_verify)
        return verify(self, *args, **kwargs)
    monkeypatch.setattr(Session, '_run_verify', observe)
    def resume():
        if not policy_case:
            saved, calls, _ = run_session(root, monkeypatch)
            assert calls == []
            return saved
        orch = Orchestrator(root)
        def forbidden(*args, **kwargs):
            pytest.fail('Receipt policy recovery must not replay the writer')
        monkeypatch.setattr(orch, '_call_with_failover', forbidden)
        coordinator = WorkflowCoordinator(orch, full_verify=outcome in {'full_failure', 'full_pass'})
        saved = coordinator.resume_workflow(paused.workflow_id)
        expected = outcome != 'affected_policy'
        assert saved.full_verify is expected
        assert load_session_state(root, saved.session_id).full_verify is expected
        assert saved.auto_approve == paused.auto_approve
        assert orch._force_full_verify is expected
        return saved
    saved = resume()
    if outcome == 'restored_contract':
        assert saved.status == 'blocked' and observed == []
        diagnostic = saved.execution_log[-1]['diagnostic']
        assert diagnostic['requirement_id'] == 'REQ-owned' and diagnostic['retry_fix'] is False
        assert resume().status == 'blocked' and observed == []
        trace.write_text(json.dumps({'requirements': [owned, foreign]}))
        protected_trace = trace.read_bytes()
        saved = resume()
        assert observed == [False], 'restoring owned authority must release the stale inconclusive result'
    elif outcome in {'full_failure', 'full_pass'}:
        assert observed == [True], 'a prior affected pass cannot attest requested release scope'
        expected_runs = ['VALUE = 1', 'VALUE = 0'] if outcome == 'full_failure' else ['VALUE = 1']
        assert marker.read_text().splitlines() == expected_runs
    else:
        assert observed == [], 'unchanged relevant authority must reuse its durable pass'
        assert not marker.exists()
    assert saved.status == ('failed' if outcome == 'full_failure' else 'completed'), saved.to_dict()
    assert saved.current_attempt == attempts
    assert saved.candidate_custody['receipt'] == retained['receipt']
    count = len(observed)
    delivered = saved.candidate_custody.get('delivered_revision')
    repeated = resume()
    assert len(observed) == count
    assert repeated.candidate_custody.get('delivered_revision') == delivered
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert trace.read_bytes() == protected_trace
    assert git(root, 'show-ref') == refs


def _retain_candidate_binding_identity(state):
    """Represent a frozen writer receipt created with the older binding format."""
    from auto_agents.session_verification import fingerprint
    custody = state.candidate_custody
    custody['binding_fingerprint'] = state.verification_binding['binding_fingerprint']
    receipt = custody['receipt']
    previous_fingerprint = receipt['fingerprint']
    receipt['binding_fingerprint'] = custody['binding_fingerprint']
    receipt['fingerprint'] = fingerprint({k: v for k, v in receipt.items() if k != 'fingerprint'})
    for entry in state.execution_log:
        if entry.get('action') == 'receipt_writer_result' and entry.get('receipt_fingerprint') == previous_fingerprint:
            entry['receipt_fingerprint'] = receipt['fingerprint']


@pytest.mark.parametrize('inventory_version,boundary,outcome', [
    (None, 'receipt', 'pass'), (1, 'receipt', 'pass'), (2, 'receipt', 'pass'),
    (3, 'verification', 'pass'), (3, 'delivery', 'pass'), (3, 'legacy_pass', 'pass'),
    (3, 'receipt', 'failure'), (3, 'receipt', 'exhausted'),
    (3, 'receipt', 'inconclusive'), (3, 'delivery', 'missing_disposition'),
    (3, 'verification', 'foreign_contract'), (3, 'verification', 'restored_contract'),
    (3, 'verification', 'full_failure'), (3, 'verification', 'full_pass'),
    (3, 'verification', 'persisted_full'), (3, 'verification', 'affected_policy'),
    (3, 'verification', 'policy_blocking'), (3, 'delivery', 'policy_blocking'),
    (3, 'completed', 'policy_blocking'), (3, 'verification', 'policy_deferred'),
    (3, 'delivery', 'policy_deferred'), (3, 'completed', 'policy_deferred'),
    (3, 'legacy_pass', 'policy_blocking'), (3, 'legacy_pass', 'policy_deferred'),
])
def test_public_inventory_migration_resumes_existing_undelivered_receipt(
        tmp_path, monkeypatch, inventory_version, boundary, outcome):
    from copy import deepcopy
    import auto_agents.session_candidate as candidate
    from auto_agents.session_verification import fingerprint

    if outcome.startswith('policy_'):
        _assert_receipt_policy_mismatch(tmp_path, monkeypatch, outcome.removeprefix('policy_'),
                                        'unchanged', boundary=boundary, switch_before=False)
        return
    if outcome in {'foreign_contract', 'restored_contract', 'full_failure', 'full_pass',
                   'persisted_full', 'affected_policy'}:
        _assert_receipt_current_authority(tmp_path, monkeypatch, outcome)
        return
    root, child = project(tmp_path)
    if outcome == 'inconclusive':
        path = root / 'tests/test_owned.py'
        path.write_text('import os\nif os.environ.get("OWNED_PREREQUISITE") != "ready":\n'
                        '    raise ImportError("required runtime input unavailable")\n' + path.read_text())
        git(root, 'add', 'tests/test_owned.py')
        git(root, 'commit', '-m', 'retain prerequisite dependent proof')
        child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
        save_session_state(root, child)
    # Interrupt production at a durable boundary; retain actual Git, baseline,
    # pytest, writer confinement and public Session.resume execution.
    with monkeypatch.context() as interrupt:
        if boundary == 'receipt':
            original = candidate.record_receipt
            def stop(session, state):
                original(session, state)
                raise KeyboardInterrupt()
            interrupt.setattr(candidate, 'record_receipt', stop)
        elif boundary == 'verification':
            original = Session._run_session_persistence_action
            def stop(self, state):
                raise KeyboardInterrupt()
            interrupt.setattr(Session, '_run_session_persistence_action', stop)
        else:
            original = candidate.deliver_candidate
            def stop(session, state, message):
                original(session, state, message)
                raise KeyboardInterrupt()
            interrupt.setattr(candidate, 'deliver_candidate', stop)
        orch = Orchestrator(root)
        def writer(request):
            value = 2 if outcome in {'failure', 'exhausted'} else 1
            (request.cwd / 'value.py').write_text(f'VALUE = {value}\n')
            reply = 'Fixed owned value.\nCOMMIT_MESSAGE: Repair owned value'
            request.output_path.write_text(reply)
            return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                               summary=reply, stdout=reply, returncode=0)
        interrupt.setattr(orch, '_call_with_failover', writer)
        child = Session(orch, mode='fix', auto_approve=True).resume(child.session_id)
    assert child.status == 'paused', child.to_dict()
    if boundary == 'legacy_pass':
        child.execution_log = [entry for entry in child.execution_log if entry.get('action') != 'receipt_verification']
        assert any(entry.get('action') == 'verify' and entry.get('result') == 'pass' for entry in child.execution_log)
    if outcome == 'missing_disposition':
        child.execution_log = [entry for entry in child.execution_log
                               if entry.get('action') not in {'receipt_writer_result', 'fix'}]
        child.status = 'completed'
    if inventory_version != 3:
        for key in ('proof_graph', 'proof_inventory_version', 'required_references'):
            child.verification_binding.pop(key, None)
        if inventory_version is not None:
            child.verification_binding['proof_inventory_version'] = inventory_version
        child.verification_binding['binding_fingerprint'] = fingerprint({
            k: v for k, v in child.verification_binding.items() if k != 'binding_fingerprint'})
        _retain_candidate_binding_identity(child)
    if outcome == 'exhausted':
        child.hard_ceiling = child.current_attempt
    retained = deepcopy(child.candidate_custody)
    attempts = child.current_attempt
    save_session_state(root, child)
    ambient = _switch_ambient_binding_plan(root)
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign.py').chmod(0o711)
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00bytes')
    protected = {name: (root / name).read_bytes() for name in (
        '.git/index', 'value.py', 'foreign.py', 'foreign-note.txt', *ambient)}
    refs = git(root, 'show-ref')
    verified = []
    verify = Session._run_verify
    def observe(self, *args, **kwargs):
        verified.append(self._current_state.candidate_custody['receipt']['source_revision'])
        return verify(self, *args, **kwargs)
    monkeypatch.setattr(Session, '_run_verify', observe)
    saved, calls, _ = run_session(root, monkeypatch)
    if outcome in {'pass', 'failure'}:
        assert saved.status == 'completed', saved.to_dict()
        assert calls == (['fix'] if outcome == 'failure' else [])
        assert saved.current_attempt == attempts + len(calls)
    else:
        assert saved.status in {'failed', 'blocked'}, saved.to_dict()
        assert calls == []
        if outcome == 'missing_disposition':
            assert saved.candidate_custody['delivered_revision'] == retained['delivered_revision']
            assert saved.execution_log[-1]['retry_fix'] is False
        else:
            assert not saved.candidate_custody.get('delivered_revision')
        assert saved.current_attempt == attempts
    custody = deepcopy(saved.candidate_custody)
    assert custody['checkout'] == retained['checkout']
    assert custody['base_revision'] == retained['base_revision']
    assert custody['binding_fingerprint'] == retained['binding_fingerprint']
    if outcome != 'failure':
        assert custody['receipt'] == retained['receipt']
    else:
        archive = next(entry for entry in saved.execution_log if entry['action'] == 'candidate_superseded')
        assert archive['receipt'] == retained['receipt']
    if boundary in {'receipt', 'legacy_pass'}:
        assert verified[0] == retained['receipt']['source_revision']
    else:
        assert verified == [], 'durable keyed pass must resume delivery without verification'
    assert any(entry.get('action') == 'receipt_verification' for entry in saved.execution_log)
    count = len(verified)
    (root / 'foreign-note.txt').write_bytes(b'new ambient bytes')
    protected['foreign-note.txt'] = b'new ambient bytes'
    repeated, calls, _ = run_session(root, monkeypatch)
    assert calls == [] and len(verified) == count
    assert repeated.candidate_custody == custody
    assert {name: (root / name).read_bytes() for name in protected} == protected
    assert (root / 'foreign.py').stat().st_mode & 0o777 == 0o711
    assert git(root, 'show-ref') == refs
    if outcome == 'inconclusive':
        monkeypatch.setenv('OWNED_PREREQUISITE', 'ready')
        resolved, calls, _ = run_session(root, monkeypatch)
        assert resolved.status == 'completed', resolved.to_dict()
        assert calls == [] and len(verified) == count + 1
        assert resolved.candidate_custody['receipt'] == retained['receipt']


@pytest.mark.parametrize('previous_version,intermediate_writer', [
    pytest.param(1, False, id='False'), pytest.param(1, True, id='True'),
    pytest.param(2, False, id='v2-False'), pytest.param(2, True, id='v2-True'),
    pytest.param(4, False, id='v4-False'), pytest.param(4, True, id='v4-True'),
    pytest.param(5, False, id='v5-False'), pytest.param(5, True, id='v5-True'),
])
def test_public_parser_inventory_upgrade_preserves_previous_custody_bridge(tmp_path, monkeypatch, intermediate_writer, previous_version):
    from copy import deepcopy
    import auto_agents.session_verification as verification

    root, child = project(tmp_path)
    if intermediate_writer:
        path = root / 'tests/test_owned.py'
        path.write_text(path.read_text().replace('"VALUE = 1"', '"VALUE = " + __import__("os").environ.get("OWNED_EXPECTED_VALUE", "1")'))
        git(root, 'add', 'tests/test_owned.py')
        git(root, 'commit', '-m', 'retain environment dependent owned assertion')
        child.baseline_git_ref = child.baseline_head_ref = head_ref(root)
        save_session_state(root, child)
    with monkeypatch.context() as old:
        old.setattr(verification, '_PROOF_INVENTORY_VERSION', previous_version)
        child, calls, _ = run_session(root, old)
        assert child.status == 'completed' and calls == ['fix']
        child.verification_binding.pop('proof_inventory_version')
        child.verification_binding['binding_fingerprint'] = verification.fingerprint({
            k: v for k, v in child.verification_binding.items() if k != 'binding_fingerprint'})
        _retain_candidate_binding_identity(child)
        save_session_state(root, child)
        child, calls, _ = run_session(root, old)
        assert child.status == 'completed' and calls == []
        assert child.candidate_custody['binding_migration']
        if intermediate_writer:
            # A fresh writer runs after the original custody upgrade.
            # The new inventory must accept its receipt through the bridge.
            child.status = 'failed'
            old.setenv('OWNED_EXPECTED_VALUE', '2')
            original_call = Session._call_agent
            def repair(self, state, label, prompt):
                assert 'verification' in prompt.lower()
                from auto_agents.session_candidate import completed_delivery
                assert state.candidate_custody['delivered_revision']
                assert not completed_delivery(state), 'failed inventory must make old delivery ineligible'
                assert any(entry.get('action') == 'receipt_verification' and not entry['verification']['ok']
                           for entry in state.execution_log)
                old.setenv('OWNED_EXPECTED_VALUE', '1')
                return original_call(self, state, label, prompt)
            old.setattr(Session, '_call_agent', repair)
            save_session_state(root, child)
            child, calls, _ = run_session(root, old)
            assert child.status == 'completed' and calls == ['fix']
            assert any(entry['action'] == 'candidate_superseded' and entry['delivered_revision']
                       for entry in child.execution_log)
            assert child.candidate_custody['receipt']['binding_fingerprint'] == child.verification_binding['binding_fingerprint']
            assert child.candidate_custody['receipt']['binding_fingerprint'] != child.candidate_custody['binding_fingerprint']
    retained = deepcopy(child.candidate_custody)
    authority = deepcopy(child.verification_binding)
    ambient = _switch_ambient_binding_plan(root)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed' and calls == []
    assert saved.verification_binding['proof_inventory_version'] == 6
    assert saved.verification_binding['binding_fingerprint'] != authority['binding_fingerprint']
    for key in ('authorization', 'tasks', 'task_scope', 'contract_revision', 'original_handoff_id'):
        assert saved.verification_binding[key] == authority[key]
    assert {k: v for k, v in saved.candidate_custody.items() if k != 'binding_migration'} == {
        k: v for k, v in retained.items() if k != 'binding_migration'}
    assert saved.candidate_custody['binding_migration']['original_binding'] == retained['binding_migration']['original_binding']
    new_entries = saved.execution_log[len(child.execution_log):]
    assert any(entry['action'] == 'inventory_migration_verify' and entry['result'] == 'pass'
               for entry in new_entries), 'the new inventory must obtain fresh verification evidence'
    assert {name: (root / name).read_bytes() for name in ambient} == ambient


def _assert_migrated_candidate_reused(root, monkeypatch, saved, retained):
    from auto_agents.session_verification import fingerprint
    custody = saved.candidate_custody
    assert {k: v for k, v in custody.items() if k != 'binding_migration'} == retained
    bridge = custody['binding_migration']
    assert bridge['original_binding']['binding_fingerprint'] == retained['binding_fingerprint']
    assert bridge['inventory_fingerprint'] == saved.verification_binding['binding_fingerprint']
    assert bridge['inventory_fingerprint'] != retained['binding_fingerprint']
    assert bridge['receipt'] == retained['receipt']
    assert any(entry['action'] == 'inventory_migration_verify' and entry['result'] == 'pass'
               for entry in saved.execution_log)
    contexts = []
    executor = Orchestrator._gate_executor_context
    def observe(self, *args, **kwargs):
        contexts.append(kwargs.get('contract_fingerprint'))
        return executor(self, *args, **kwargs)
    monkeypatch.setattr(Orchestrator, '_gate_executor_context', observe)
    repeated, calls, _ = run_session(root, monkeypatch)
    assert repeated.status == 'completed' and calls == []
    assert repeated.candidate_custody == custody
    expected = fingerprint([bridge['inventory_fingerprint'], retained['receipt']['fingerprint']])
    assert repeated.verification_binding == saved.verification_binding
    assert repeated.baseline_git_ref == saved.baseline_git_ref
    assert repeated.baseline_head_ref == saved.baseline_head_ref
    assert not contexts, 'matching durable verification must not execute again'
    assert git(Path(custody['checkout']), 'show', custody['receipt']['source_revision'] + ':value.py') == 'VALUE = 1\n'


@pytest.mark.parametrize('conflict', ['receipt', 'custody', 'history', 'authority',
                                     'task_scope', 'task_contract', 'bridge_source'])
def test_public_inventory_migration_rejects_conflicts_without_partial_persistence(tmp_path, monkeypatch, conflict):
    from copy import deepcopy
    import auto_agents.session_verification as verification

    root, child = project(tmp_path)
    child, calls, _ = run_session(root, monkeypatch)
    assert child.status == 'completed' and calls == ['fix']
    for key in ('proof_graph', 'proof_inventory_version', 'required_references'):
        child.verification_binding.pop(key, None)
    child.verification_binding['binding_fingerprint'] = verification.fingerprint({
        k: v for k, v in child.verification_binding.items() if k != 'binding_fingerprint'})
    _retain_candidate_binding_identity(child)
    if conflict == 'receipt':
        child.candidate_custody['receipt']['attempt_id'] = 'changed-writer'
    elif conflict == 'custody':
        child.candidate_custody['binding_fingerprint'] = 'another-binding'
    elif conflict == 'history':
        child.verification_binding['plan']['tasks'][0]['title'] = 'substituted retained history'
        child.verification_binding['binding_fingerprint'] = verification.fingerprint({
            k: v for k, v in child.verification_binding.items() if k != 'binding_fingerprint'})
        _retain_candidate_binding_identity(child)
    elif conflict in {'authority', 'task_scope', 'task_contract'}:
        seal = verification._seal_inventory
        def conflicting_upgrade(session, state):
            seal(session, state)
            if conflict == 'authority':
                state.verification_binding['authorization']['source'] = 'changed during enrichment'
            elif conflict == 'task_scope':
                state.verification_binding['task_scope']['task_ids'] = ['task-foreign']
            else:
                state.verification_binding['tasks'][0]['requirement_ids'] = ['REQ-replaced']
        monkeypatch.setattr(verification, '_seal_inventory', conflicting_upgrade)
    else:
        save_session_state(root, child)
        child, calls, _ = run_session(root, monkeypatch)
        assert child.status == 'completed' and calls == []
        child.candidate_custody['base_revision'] = child.candidate_custody['receipt']['source_revision']
    child.status = 'failed'
    retained = deepcopy(child.verification_binding)
    custody = deepcopy(child.candidate_custody)
    save_session_state(root, child)
    ambient = _switch_ambient_binding_plan(root)
    (root / 'foreign.py').write_text('VALUE = 88\n')
    git(root, 'add', 'foreign.py')
    (root / 'foreign.py').write_text('VALUE = 99\n')
    (root / 'foreign-note.txt').write_bytes(b'foreign\x00untracked')
    index = (root / '.git/index').read_bytes()
    for _ in range(2):
        saved = _assert_binding_blocked_before_execution(root, monkeypatch)
        assert saved.verification_binding == retained
        assert saved.candidate_custody == custody
        assert (root / '.git/index').read_bytes() == index
        assert (root / 'foreign.py').read_text() == 'VALUE = 99\n'
        assert (root / 'foreign-note.txt').read_bytes() == b'foreign\x00untracked'
        assert {name: (root / name).read_bytes() for name in ambient} == ambient


# These frozen g04 entrypoints reuse the complete public-resume regressions.
test_selection_binds_authorization_owned_candidate_and_current_contract = test_session_gate_selection_binds_authorization_candidate_and_contract
test_resumed_fix_uses_owned_contract_after_global_plan_switch = test_resumed_fix_ignores_foreign_pending_plan_without_weakening_its_gates
test_public_legacy_resume_requires_resolved_contract_ownership = test_public_resume_before_first_baseline_uses_child_history
test_retained_plan_proofs_survive_generated_gate_overlap = test_public_resume_recovers_release_proof_removed_from_generated_config
test_covering_commands_validate_bound_requirement_hashes = test_public_resume_validates_contract_owners_after_command_expansion
# Preserve the frozen acceptance entrypoint using the existing public regression.
test_missing_owned_proof_cannot_be_replaced_by_empty_selection_or_cached_success = test_missing_owned_proof_cannot_be_waived_by_reference_deletion_or_cached_success
