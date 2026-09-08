"""Public session recovery regressions with real Git and pytest execution."""
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

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
    result, _, _ = run_session(root, monkeypatch)
    assert result.status == 'completed', result.to_dict()
    assert git(root, 'show', ':foreign.py') == index
    assert (root / 'foreign.py').read_text() == 'VALUE = 9\n'
    assert (root / 'foreign-note.txt').read_bytes() == b'foreign\x00bytes'
    assert 'foreign.py' not in git(root, 'show', '--format=', '--name-only', 'HEAD')


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
    assert result.status == 'blocked'
    assert result.resolution == 'verification_ownership'
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
    assert returned.result['rolled_back_paths'] == expected
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


@pytest.mark.parametrize('waiver', ['reference_deletion', 'cached_success'])
def test_missing_owned_proof_cannot_be_waived_by_reference_deletion_or_cached_success(tmp_path, monkeypatch, waiver):
    from auto_agents.models import CommandResult, GateResult
    import auto_agents.session as session_module

    root, _ = project(tmp_path, missing=True)
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
