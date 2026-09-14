"""Frozen verifier capabilities, compact inputs and native reply schemas."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_agents.adapters.codex import CodexAdapter
from auto_agents.models import AgentRequest, ProviderConfig
from auto_agents.repair_context import writer_packet, review_packet
from auto_agents.repair_capability_checks import production_capabilities
from auto_agents.repair_response_schema import schema_for, normalize_reply
from auto_agents.repair_work import memory
from test_repair_planning import setup
from test_repair_workflow_engine import approved, candidate


def test_candidate_edits_do_not_renegotiate_outer_verifier_but_protocol_changes_do(approved, tmp_path):
    runner, state, _, _ = approved
    trusted = tmp_path / 'trusted-controller'
    identity = {'version': 1, 'observer': 'fixed', 'environment': 'fixed', 'runtime': str(trusted)}
    observed = []
    def probe(_runner, workspace):
        observed.append(workspace)
        assert workspace == trusted
        return {'supported': True, 'acceptance_proof': False}
    with (patch('auto_agents.repair_validation_protocol.controller_runtime', return_value=trusted),
          patch('auto_agents.repair_validation_protocol.binding', side_effect=lambda _: deepcopy(identity)),
          patch('auto_agents.repair_capability_checks.namespace_observation', side_effect=probe),
          patch('auto_agents.repair_capability_checks.metadata_observation', side_effect=probe)):
        first = production_capabilities(runner, tmp_path / 'candidate-a')
        assert production_capabilities(runner, tmp_path / 'candidate-b') == first
        assert len(observed) == 2
        identity['environment'] = 'changed'
        production_capabilities(runner, tmp_path / 'candidate-b')
        assert len(observed) == 4
        identity['observer'] = 'new-protocol-implementation'
        production_capabilities(runner, tmp_path / 'candidate-b')
        assert len(observed) == 6
    assert len([key for key in state.planning_receipts if key.startswith('validation-protocol:')]) == 3
    assert not state.progress_credits


def test_writer_packet_keeps_current_evidence_and_exact_full_inventory(approved):
    runner, state, plan, _ = approved
    record, reference = candidate(runner, plan, 1)
    active = deepcopy(runner._candidate_group)
    active['finding_ids'] = ['retained-owner']
    active['finding_scenario_bindings'] = {'retained-owner': ['failure']}
    active['implementation_steps'] = ['old mechanism ' * 5000]
    active['retained_acceptance'] = ['python -m pytest -q tests/test_full.py::test_matrix --maxfail=1']
    current = state.findings['retained-owner'].to_dict()
    search = {'open_contract_findings': [current], 'next_action': {'kind': 'repair_code', 'review_findings': [current]},
              'old_context': 'historical evidence ' * 10000}
    projected, group, refs = writer_packet(runner, search, active, {'diagnosis': {'detail': 'old diagnosis' * 1000}})
    assert len(json.dumps(projected)) + len(json.dumps(group)) < len(json.dumps(search)) / 5
    assert projected['current_review_findings'][0]['counterexample'] == current['counterexample']
    assert projected['counterexample_controls'][0]['controls']['positive']['command'] == plan['quick_checks'][1]
    complete = json.loads(Path(projected['complete_context']).read_text())
    assert complete['component']['retained_acceptance'] == active['retained_acceptance']
    assert complete['search']['old_context'] == search['old_context']
    assert refs['diagnosis']['complete_context'] == projected['complete_context']
    assert runner._candidate_group['implementation_steps'] != group['implementation_steps']


def test_review_packet_keeps_every_scenario_oracle_and_full_checks(approved):
    runner, _, _, _ = approved
    group = runner._candidate_group
    compact = review_packet(group, {}, '/complete-review.json', incremental=True)
    assert [(s['scenario_id'], s['trigger'], s['expected'], s['check']) for s in compact['scenarios']] == [
        (s['scenario_id'], s['trigger'], s['expected'], s['check']) for s in group['scenarios']]
    assert compact['implementation_steps']['section'] == 'component.implementation_steps'


@pytest.mark.parametrize('interrupted', [False, True])
def test_codex_structured_reply_uses_read_only_request_and_cleans_schema(tmp_path, interrupted):
    schema = schema_for('self_repair_plan_review')
    request = AgentRequest(stage='self_repair_plan_review', effort='max', prompt='review the proposal',
        cwd=tmp_path, output_path=tmp_path / 'reply.json', sandbox_mode='read-only', response_schema=schema)
    paths = []
    def execute(command, *args, **kwargs):
        path = Path(command[command.index('--output-schema') + 1])
        paths.append(path)
        assert json.loads(path.read_text()) == schema
        assert command[command.index('--sandbox') + 1] == 'read-only'
        if interrupted:
            raise KeyboardInterrupt
        return ('', '', 0, False, False)
    with patch('auto_agents.adapters.codex.run_subprocess_with_optional_streaming', side_effect=execute):
        adapter = CodexAdapter(ProviderConfig(kind='codex', binary='codex', profile_map={}))
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                adapter.run(request)
        else:
            assert adapter.run(request).ok
    assert len(paths) == 1 and not paths[0].exists()


def test_native_schema_absent_values_do_not_change_scope_format_semantics():
    original = {'decisions': [{'finding_id': 'f', 'verdict': 'required'}]}
    assert normalize_reply('self_repair_scope_review', {**original, 'probes': None}) == original
    malformed = {**original, 'probes': False}
    assert normalize_reply('self_repair_scope_review', malformed) == malformed
    one, two = schema_for('self_repair_plan_review'), schema_for('self_repair_plan_review')
    one['properties']['decision']['enum'].append('WAIVE')
    assert 'WAIVE' not in two['properties']['decision']['enum']


@pytest.mark.parametrize('trigger', ['independent_review', 'failed_check'])
def test_current_repair_authority_overrides_old_no_writer_mode(setup, trigger):
    from auto_agents.models import AgentResult
    from auto_agents.repair_planning import prepare_component
    runner, state, plan, calls = setup
    plan['mode'] = 'verify_existing'
    original = runner.target_orchestrator._call_with_failover
    def provider(request):
        result = original(request)
        if request.stage == 'self_repair_plan_review':
            payload = json.loads(result.summary)
            payload.update(implementation_required=trigger == 'independent_review',
                           remaining_changes=['repair the active mechanism'] if trigger == 'independent_review' else [])
            result.summary = json.dumps(payload)
        return result
    runner.target_orchestrator._call_with_failover = provider
    with patch('auto_agents.repair_planning._probe', return_value={'matches': True, 'outcome': 'pass'}):
        prepare_component(runner, runner.repo_root)
        if trigger == 'failed_check':
            assert runner._candidate_group['mode'] == 'verify_existing'
            runner._candidate_next_action = {'kind': 'repair_code', 'evidence_ids': ['controller-check-failure']}
            prepare_component(runner, runner.repo_root)
    assert runner._candidate_group['mode'] == 'implement'
    assert len(calls) == 2
