"""Explicit revisions stay unapproved and cannot silently lose retained acceptance."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from auto_agents.git_ops import head_ref
from auto_agents.repair_control import digest
from auto_agents.repair_manual_plan import propose, validate_revision
from auto_agents.repair_memory import latest_revision, save_record
from auto_agents.repair_planning import PlanningBlocked, prepare_component, _retained_review
from auto_agents.repair_work import memory
from auto_agents.verification_ledger import source_identity
from test_repair_planning import setup


def document_for(runner):
    group = runner._experiment.finding_groups[0]
    parent = latest_revision(runner, group)
    plan = deepcopy(parent['draft'])
    plan['implementation_steps'] = ['retain existing behavior and correct the registered dependency binding']
    return {'version': 1, 'group_id': group['group_id'],
        'contract_fingerprint': runner._experiment.contract_fingerprint,
        'source': source_identity(runner.repo_root), 'source_commit': head_ref(runner.repo_root),
        'parent_plan_digest': digest(parent['draft']), 'reason': 'explicit correction after independent review',
        'plan': plan}


def initial(setup):
    runner, state, plan, calls = setup
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
    runner._candidate_group = deepcopy(state.finding_groups[0])
    return runner, state, calls, receipt


def test_manual_revision_supersedes_old_approval_and_requires_new_independent_review(setup):
    runner, state, calls, old = initial(setup)
    before_attempts = deepcopy(state.planning_attempts)
    before_approvals = deepcopy(state.planning_receipts)
    before_source = source_identity(runner.repo_root)
    doc = document_for(runner)
    result = propose(runner, runner.repo_root, doc)
    assert result['status'] == 'draft' and result['requires_independent_review']
    assert state.planning_attempts == before_attempts and state.planning_receipts == before_approvals
    assert len(calls) == 2 and not state.progress_credits and state.attempt_count == 0
    assert source_identity(runner.repo_root) == before_source
    runner._experiment = runner._experiment_store.load()
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        receipt = prepare_component(runner, runner.repo_root)
        assert receipt['request_id'] != old['request_id']
        assert receipt['planner_request'] == result['request_id']
        assert receipt['planner_request'] != receipt['request_id']
        assert _retained_review(runner, receipt, receipt['component'])
        assert prepare_component(runner, runner.repo_root)['request_id'] == receipt['request_id']
    assert [r.stage for r, _ in calls] == [
        'self_repair_component_plan', 'self_repair_plan_review', 'self_repair_plan_review']
    assert receipt['plan']['implementation_steps'] == doc['plan']['implementation_steps']
    assert receipt['plan']['scenarios'] == old['plan']['scenarios']
    assert not runner._experiment.progress_credits and source_identity(runner.repo_root) == before_source


@pytest.mark.parametrize('changed', ['source', 'commit', 'contract', 'parent', 'paths', 'checks', 'scenario', 'quick_required'])
def test_revision_rejects_stale_inputs_and_acceptance_loss_without_writing(setup, changed):
    runner, state, calls, _ = initial(setup)
    doc = document_for(runner)
    if changed in {'source', 'commit', 'contract', 'parent'}:
        key = {'source': 'source', 'commit': 'source_commit', 'contract': 'contract_fingerprint',
               'parent': 'parent_plan_digest'}[changed]
        doc[key] = 'changed'
    elif changed == 'paths':
        doc['plan']['touched_paths'].append('unrelated.py')
    elif changed == 'checks':
        doc['plan']['quick_checks'].append('python -m pytest -q tests/test_other.py::test_other')
    elif changed == 'scenario':
        doc['plan']['scenarios'][0]['check'] = 'python -m pytest -q tests/test_other.py::test_other'
    else:
        doc['plan']['scenarios'][0]['quick_required'] = False
    before = runner._experiment_store.path.read_bytes()
    with pytest.raises(PlanningBlocked):
        propose(runner, runner.repo_root, doc)
    assert runner._experiment_store.path.read_bytes() == before
    assert len(calls) == 2 and not state.progress_credits


def test_missing_manual_provenance_cannot_fall_back_to_old_approval(setup):
    runner, state, calls, _ = initial(setup)
    result = propose(runner, runner.repo_root, document_for(runner))
    directory = runner._experiment_store.root / 'planning' / result['request_id']
    (directory / 'input.json').write_text('{}')
    runner._experiment = runner._experiment_store.load()
    with pytest.raises(PlanningBlocked, match='provenance'):
        prepare_component(runner, runner.repo_root)
    assert len(calls) == 2


def test_revision_retires_old_completion_without_destroying_its_evidence(setup):
    runner, state, calls, _ = initial(setup)
    group = state.finding_groups[0]
    reference = save_record(runner, 'component_completion', {'old': 'retained evidence'})
    memory(runner, group)['completion'] = reference
    propose(runner, runner.repo_root, document_for(runner))
    stored = memory(runner, group)
    assert 'completion' not in stored
    assert stored['completion_history'][-1] == reference
    assert stored['completion_assessment']['state'] == 'needs_revalidation'
    assert (runner._experiment_store.root / 'planning' / reference['id'] / 'memory.json').exists()
    assert not state.progress_credits
