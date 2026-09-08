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


@pytest.mark.parametrize('shape', ['empty_impact', 'final_only', 'overlapping_release', 'deduplicated'])
def test_public_resume_executes_complete_owned_proof_inventory(tmp_path, monkeypatch, shape):
    """Impact, cadence and command coalescing cannot discharge owned proofs."""
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    marker = tmp_path / 'owned-proof-executed'
    proof = root / 'tests/test_owned.py'
    proof.write_text(proof.read_text() +
                     f'    Path({str(marker)!r}).write_text("executed")\n')
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
    assert binding['schema_version'] == 11
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
    release = current_release_attestation(root)['latest']
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
    marker = tmp_path / 'executed-invocations'
    (root / 'tests/test_owned.py').write_text(
        'from pathlib import Path\ndef test_owned(request):\n'
        '    assert "VALUE = 1" in Path("value.py").read_text()\n'
        f'    with Path({str(marker)!r}).open("a") as stream:\n'
        '        stream.write(str(bool(request.config.getoption("strict_markers"))) + "\\n")\n')
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
    marker = tmp_path / 'setup-executions'
    (root / 'tests/test_release.py').write_text('def test_release(): assert True\n')
    (root / 'tests/test_setup.py').write_text(
        'from pathlib import Path\ndef test_setup():\n'
        f'    with Path({str(marker)!r}).open("a") as stream:\n'
        '        stream.write(Path("value.py").read_text())\n'
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
    markers = {key: tmp_path / key for key in ('setup', 'regression', 'release')}
    (root / 'tests/test_regression.py').write_text(
        'from pathlib import Path\ndef test_setup():\n'
        f'    Path({str(markers["setup"])!r}).write_text("ran")\n'
        'def test_regression():\n'
        f'    Path({str(markers["regression"])!r}).write_text("ran")\n'
        '    assert "VALUE = 0" in Path("value.py").read_text()\n'
        'def test_release():\n'
        f'    Path({str(markers["release"])!r}).write_text("ran")\n')
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
    marker = tmp_path / 'foreign-prerequisite-executed'
    (root / 'tests/test_shared.py').write_text(
        'from pathlib import Path\ndef test_shared():\n'
        f'    Path({str(marker)!r}).write_text("ran")\n'
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
                                     'discovery_other_config', 'discovery_inclusive'])
def test_public_resume_requires_executable_owned_node_coverage(tmp_path, monkeypatch, source, selector):
    """A passing control cannot certify an explicitly excluded owned node."""
    from auto_agents.config import load_task_plan
    import auto_agents.session as session_module

    root, child = project(tmp_path)
    marker = tmp_path / 'required-node-executed'
    proof = root / 'tests/test_owned.py'
    proof.write_text(proof.read_text() + f'    Path({str(marker)!r}).write_text("owned")\n'
                     '\ndef test_control(): assert True\n')
    inclusive = selector in {'unfiltered', 'discovery_inclusive'}
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
    command = shlex.join(['./.conda/bin/python', '-m', 'pytest', '-q', *args, 'tests/test_owned.py'])
    config.gates.steps[0].targets = ['tests/test_owned.py']
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
        assert diagnostic['verification_ref'] == 'tests/test_owned.py::test_owned'
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
    marker = tmp_path / 'manual-regression-executed'
    path = 'tests/test_regression.py'
    (root / path).write_text(
        'from pathlib import Path\ndef test_regression():\n'
        f'    Path({str(marker)!r}).write_text(Path("value.py").read_text())\n'
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
    marker = tmp_path / 'shared-regression-executed'
    path = 'tests/test_regression.py'
    (root / path).write_text(
        'from pathlib import Path\ndef test_regression():\n'
        f'    with Path({str(marker)!r}).open("a") as evidence:\n'
        '        evidence.write(Path("value.py").read_text())\n'
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
@pytest.mark.parametrize('coverage', ['whole_file', 'directory', 'node', 'mixed_future'])
@pytest.mark.parametrize('regression', ['fails_candidate', 'passes_candidate'])
def test_public_resume_retains_existing_foreign_release_regression(
        tmp_path, monkeypatch, reference, coverage, regression):
    from auto_agents.config import load_task_plan

    root, child = project(tmp_path)
    marker = tmp_path / 'structured-regression-executed'
    path = 'tests/test_regression.py'
    (root / path).write_text(
        'from pathlib import Path\ndef test_regression():\n'
        f'    with Path({str(marker)!r}).open("a") as evidence:\n'
        '        evidence.write(Path("value.py").read_text())\n'
        + ('    assert "VALUE = 0" in Path("value.py").read_text()\n'
           if regression == 'fails_candidate' else '    assert True\n'))
    retained_source = (root / path).read_bytes()
    target = {'whole_file': path, 'directory': 'tests', 'node': path + '::test_regression',
              'mixed_future': path}[coverage]
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
