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


@pytest.mark.parametrize('shape', ['whole_file', 'directory', 'deduplicated'])
@pytest.mark.parametrize('contract', ['changed', 'missing', 'unchanged'])
def test_public_resume_validates_contract_owners_after_command_expansion(tmp_path, monkeypatch, shape, contract):
    from auto_agents.config import load_task_plan, requirements_trace_path
    from auto_agents.requirements import requirement_contract_sha256
    import auto_agents.session as session_module

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
def test_public_resume_blocks_unresolved_owned_proof_id(tmp_path, monkeypatch, passing_proof):
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    config = load_project_config(root)
    config.gates.steps = []
    config.gates.commands = []
    if passing_proof:
        (root / 'tests/test_control.py').write_text('def test_control(): assert True\n')
        config.gates.steps = [VerificationStep(proof_id='unrelated.control', runner='pytest',
            targets=['tests/test_control.py'], levels=['affected', 'release'], impact_paths=['**'])]
    plan = {'tasks': [{'task_id': 'task-owned', 'title': 'Missing owned proof',
                      'requirement_ids': ['REQ-owned'], 'verification_refs': ['owned.contract']}],
            'verification_steps': [step.to_dict() for step in config.gates.steps]}
    _retain_contract(root, child, config, plan)
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
    assert diagnostic['verification_ref'] == 'owned.contract'
    assert diagnostic['session_id'] == child.session_id
    assert diagnostic['owners'][0]['task_id'] == 'task-owned'
    assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
    assert diagnostic['contract_fingerprint']
    assert diagnostic['retry_fix'] is False


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


@pytest.mark.parametrize('source', ['structured', 'legacy', 'explicit_fix'])
@pytest.mark.parametrize('selector', ['deselect', 'name_filter', 'attached_name_filter',
                                     'marker_filter', 'override', 'config', 'unfiltered',
                                     'discovery_cli', 'discovery_attached', 'discovery_long',
                                     'discovery_config', 'discovery_toml', 'discovery_addopts',
                                     'discovery_other_config', 'discovery_inclusive',
                                     'norecurse_cli', 'norecurse_config', 'norecurse_toml',
                                     'norecurse_path', 'norecurse_override', 'norecurse_explicit',
                                     'norecurse_unrelated', 'norecurse_default'])
def test_public_resume_requires_executable_owned_node_coverage(tmp_path, monkeypatch, source, selector):
    """A passing control cannot certify an explicitly excluded owned node."""
    from auto_agents.config import load_task_plan
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    marker = ExecutionMarker(tmp_path / 'required-node-executed')
    proof = root / 'tests/test_owned.py'
    proof.write_text(proof.read_text() + '    ' + marker.source(repr('owned')) + '\n'
                     '\ndef test_control(): assert True\n')
    inclusive = selector in {'unfiltered', 'discovery_inclusive', 'norecurse_override',
                             'norecurse_explicit', 'norecurse_unrelated'}
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
        'unfiltered': [],
    }[selector]
    if selector == 'marker_filter':
        proof.write_text('import pytest\n' + proof.read_text().replace(
            'def test_control():', '@pytest.mark.control\ndef test_control():'))
    if selector == 'config':
        (root / 'checks.ini').write_text(
            '[pytest]\naddopts = --deselect=tests/test_owned.py::test_owned\n')
    if selector in {'discovery_config', 'discovery_other_config'}:
        (root / 'checks.ini').write_text('[pytest]\npython_functions = test_control\n')
    if selector == 'discovery_toml':
        (root / 'pytest.toml').write_text('[pytest]\npython_functions = ["test_control"]\n')
    required_node = 'tests/test_owned.py::test_owned'
    target = 'tests/test_owned.py'
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
    command = shlex.join(['./.conda/bin/python', '-m', 'pytest', '-q', *args, target])
    config.gates.steps[0].targets = [target]
    config.gates.steps[0].args = args
    if source != 'structured':
        config.gates.steps = []
        config.gates.commands = [command] if source == 'legacy' else []
        child.fix_verify_command = command if source == 'explicit_fix' else ''
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
    ambient = {path: (root / path).read_bytes() for path in
               ('.auto-agents/config.json', '.auto-agents/state/task_plan.json')}
    dispatched = []
    execute = session_module.run_gate_plan
    def observe(commands, *args, **kwargs):
        dispatched.extend(commands)
        return execute(commands, *args, **kwargs)
    monkeypatch.setattr(session_module, 'run_gate_plan', observe)
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
        assert diagnostic['owners'][0]['task_id'] == 'task-owned'
        assert diagnostic['owners'][0]['requirement_ids'] == ['REQ-owned']
        assert diagnostic['contract_fingerprint']
        assert diagnostic['retry_fix'] is False
    assert {path: (root / path).read_bytes() for path in ambient} == ambient


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
def test_public_legacy_resume_without_history_rejects_ambient_plan(tmp_path, monkeypatch, has_workflow):
    from auto_agents.workflow_chain import WorkflowRef, WorkflowStore

    root, child = project(tmp_path)
    if has_workflow:
        child.workflow_id = WorkflowStore(root).create_root(WorkflowRef('fix', child.session_id)).workflow_id
    child.baseline_git_ref = child.baseline_head_ref = child.lineage_head_ref = ''
    save_session_state(root, child)
    ambient = _switch_ambient_binding_plan(root)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', 'switch global contract without child history')
    saved = _assert_binding_blocked_before_execution(root, monkeypatch)
    assert saved.verification_binding == {}
    assert 'contract revision is unavailable' in saved.execution_log[-1]['result']
    assert saved.workflow_id == child.workflow_id
    assert saved.baseline_git_ref == saved.baseline_head_ref == saved.lineage_head_ref == ''
    repeated = _assert_binding_blocked_before_execution(root, monkeypatch)
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


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('matching', [False, True])
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
    handoff.payload.update(requirement_scope if reverse else task_scope)
    store.save_handoff(handoff)
    issue = root / '.auto-agents/state/sessions' / child.session_id / 'issue.json'
    issue.write_text(json.dumps(task_scope if reverse else requirement_scope))
    if binding_version is not None:
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
    if matching:
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
        assert 'conflict' in saved.execution_log[-1]['result']
        diagnostic = saved.execution_log[-1]['diagnostic']
        assert diagnostic['handoff_id'] == handoff.handoff_id
        if binding_version is None:
            assert diagnostic['retained_task_ids'] == ['task-other' if reverse else 'task-owned']
            assert diagnostic['requirement_task_ids'] == ['task-owned' if reverse else 'task-other']
        assert saved.verification_binding == prior_binding
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


@pytest.mark.parametrize('legacy', [False, True])
def test_binding_round_trip_and_legacy_recovery_preserve_original_authority(tmp_path, monkeypatch, legacy):
    from copy import deepcopy
    from auto_agents.session_verification import fingerprint

    root, child = project(tmp_path)
    child.goal_execution_environment = {'mode': 'real', 'confirmed': True, 'source': 'explicit_goal'}
    _binding_fixture(root, child)
    if legacy:
        child.verification_binding['schema_version'] = 11
        for key in ('execution_environment', 'source_provenance', 'session_mode'):
            child.verification_binding.pop(key)
        child.verification_binding['binding_fingerprint'] = fingerprint({
            key: value for key, value in child.verification_binding.items() if key != 'binding_fingerprint'})
    retained = deepcopy(child.verification_binding)
    save_session_state(root, child)
    assert load_session_state(root, child.session_id).verification_binding == retained
    ambient = _switch_ambient_binding_plan(root)
    saved, calls, _ = run_session(root, monkeypatch)
    assert saved.status == 'completed', saved.to_dict()
    assert calls == ['fix']
    binding = saved.verification_binding
    for key in ('repository', 'contract_revision', 'authorization', 'gates', 'plan', 'required_proof_ids',
                'session_id', 'workflow_id', 'original_handoff_id', 'task_scope'):
        assert binding[key] == retained[key]
    assert binding['schema_version'] == 13
    assert binding['execution_environment'] == child.goal_execution_environment
    assert binding['source_provenance']['revision'] == child.baseline_head_ref
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
    from test_engine_child_recovery import parent_workflow, resume_to_observation

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
        (candidate_root / 'value.py').write_text('VALUE = 1\n')
        (candidate_root / 'value.py').chmod(0o640)
        git(candidate_root, 'add', 'value.py')
        (candidate_root / 'value.py').chmod(0o750)
        (candidate_root / 'writer-link').symlink_to('value.py')
        (candidate_root / 'obsolete.txt').unlink()
        return 'Fixed\nCOMMIT_MESSAGE: Repair owned value'
    resume_to_observation(root, monkeypatch, writer)
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
    root, child = project(tmp_path)
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
        writer_roots.append(request.cwd)
        assert (request.cwd / '.git').is_dir()
        assert (request.cwd / 'foreign.py').read_text() == 'VALUE = 7\n'
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        (root / 'foreign-note.txt').write_bytes(b'concurrent foreign bytes')
        reply = 'Fixed value.\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
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
    root, child = project(tmp_path)
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
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        reply = 'Fixed\nCOMMIT_MESSAGE: Repair owned value'
        request.output_path.write_text(reply)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=reply, stdout=reply, returncode=0)
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
def test_unborn_session_freezes_initial_source_without_shared_publication(tmp_path, monkeypatch, baseline_ref):
    root = _make_project(str(tmp_path))
    (root / '.conda').symlink_to(sys.prefix, target_is_directory=True)
    (root / 'value.py').write_text('VALUE = 0\n')
    (root / 'foreign.txt').write_bytes(b'initial staged\x00bytes')
    git(root, 'add', 'value.py', 'foreign.txt')
    (root / 'foreign.txt').write_bytes(b'initial worktree\x00bytes')
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
        (request.cwd / 'value.py').write_text('VALUE = 1\n')
        (root / 'late-foreign.txt').write_bytes(b'concurrent\x00bytes')
        content = 'Repaired value.\nCOMMIT_MESSAGE: Repair initial value'
        request.output_path.write_text(content)
        return AgentResult(ok=True, command=['fixture'], output_path=request.output_path,
                           summary=content, stdout=content, returncode=0)
    orch = Orchestrator(root)
    monkeypatch.setattr(orch, '_call_with_failover', provider)
    result = Session(orch, mode='fix', auto_approve=True).resume(state.session_id)
    assert result.status == 'completed', result.to_dict()
    assert len(calls) == 1
    custody = result.candidate_custody
    private = Path(custody['checkout'])
    assert custody['initial_source'] is True
    assert list(custody['receipt']['manifest']) == ['value.py']
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
    from test_engine_child_recovery import parent_workflow, resume_to_observation

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
    store, _, handoff = parent_workflow(root, child)
    (root / 'assets/old.json').write_bytes(b'foreign staged')
    git(root, 'add', 'assets/old.json')
    (root / 'assets/old.json').write_bytes(b'foreign worktree')
    shared_index = (root / '.git/index').read_bytes()
    shared_head = head_ref(root)
    observed = []
    def writer(state, prompt, candidate_root):
        assert (candidate_root / 'assets/old.json').read_bytes() == b'{"retained": true}\n'
        shutil.rmtree(candidate_root / 'assets')
        (candidate_root / 'assets').write_bytes(b'replacement\x00bytes')
        (candidate_root / 'value.py').write_text('VALUE = 1\n')
        if staged:
            git(candidate_root, 'add', '-A', '--', 'assets')
        return 'Replaced the directory.\nCOMMIT_MESSAGE: Replace owned assets'
    def observe(request):
        observed.append(request.cwd)
        assert request.cwd != root
        assert (request.cwd / 'assets').is_file()
        assert (request.cwd / 'assets').read_bytes() == b'replacement\x00bytes'
        assert not (request.cwd / 'assets/old.json').exists()
    resume_to_observation(root, monkeypatch, writer, observe=observe)
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
    from test_engine_child_recovery import parent_workflow, resume_to_observation

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
        shutil.rmtree(candidate_root / 'assets')
        (candidate_root / 'assets').symlink_to(root / 'assets', target_is_directory=True)
        (candidate_root / 'value.py').write_text('VALUE = 1\n')
        if staged:
            git(candidate_root, 'add', '-A', '--', 'assets')
        return 'Replaced owned directory.\nCOMMIT_MESSAGE: Replace owned assets'
    observed = []
    def observe(request):
        observed.append(request.cwd)
        assert request.cwd != root
        assert (request.cwd / 'assets').is_symlink()
        assert os.readlink(request.cwd / 'assets') == str(root / 'assets')
        assert old.read_bytes() == b'late foreign worktree'
        assert stat.S_IMODE(old.stat().st_mode) == 0o711
    resume_to_observation(root, monkeypatch, writer, observe=observe)
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
