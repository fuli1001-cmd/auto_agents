import json
from pathlib import Path
from unittest.mock import patch

from auto_agents.repair_v2.review_evidence import recovery_evidence
from test_repair_v2_controller import job, controller


def report(identity, target='original-scene'):
    return {'ok': True, 'snapshot': identity, 'runtime': '', 'cases': [{
        'ok': True, 'snapshot': identity, 'target': target, 'runtime': '', 'observed': {
            'route_consumed': True, 'engine_runtime': {'ok': True, 'commit': 'candidate-commit', 'mismatches': []},
            'recovery_observation': {'workflow_id': 'wf-original', 'original_handoff_id': 'hf-original',
                'child_session_id': 'child-original', 'parent_session_id': 'parent-original',
                'boundary_kind': 'implementation', 'preflight_rechecked': True,
                'parent_budget': {'before': {'attempt_epoch': 10}, 'after': {'attempt_epoch': 10}},
                'parent_constraints_preserved': True, 'child_constraints_preserved': True,
                'retained_constraints': True, 'diagnostic_provider_calls': 0}}}]}


def test_review_receives_matching_controller_evidence_and_does_not_reuse_another_scene(job):
    runner = controller(job)
    runner.preflight_boundary = True
    runner.boundary = lambda identity, *a: report(identity)
    prompts = []
    original = runner.driver.run
    def observe(role, prompt, *a, **k):
        if role == 'review': prompts.append(prompt)
        return original(role, prompt, *a, **k)
    runner.driver.run = observe
    state = runner.run()
    assert state['status'] == 'ready'
    evidence = recovery_evidence(runner, state['snapshot'])
    assert evidence is not None and evidence['artifact'] == state['boundary_preflight']
    assert json.dumps(evidence, ensure_ascii=False) in prompts[0]
    assert 'parent_budget' in prompts[0] and 'child-original' in prompts[0]
    runner.review(state['snapshot'], runner.workspace.candidate)
    assert len(prompts) == 1
    runner.checkpoint(boundary_preflight=runner.store.artifact('boundary', report(state['snapshot'], 'new-scene')))
    runner.review(state['snapshot'], runner.workspace.candidate)
    assert len(prompts) == 2 and 'new-scene' in prompts[-1]
    assert recovery_evidence(runner, 'another-snapshot') is None
    runner.checkpoint(verification_runtime='another-runtime')
    assert recovery_evidence(runner, state['snapshot']) is None


def test_reviewer_gets_full_report_readonly_and_writer_does_not(tmp_path):
    from auto_agents.repair_v2.providers import AgentSandbox
    candidate = tmp_path / 'candidate'; candidate.mkdir()
    evidence = tmp_path / 'boundary.json'; evidence.write_text(json.dumps(report('source')))
    sandbox = AgentSandbox(tmp_path / 'agent', 'pinned')
    sandbox.recovery_evidence = evidence
    mount = f'type=bind,src={evidence},dst=/repair-recovery/boundary.json,readonly'
    with patch.object(sandbox, 'home', return_value=tmp_path / 'home'), \
         patch('auto_agents.repair_v2.docker.run', return_value=(0, '')):
        for role in ('review', 'implement'):
            with sandbox.command(role, candidate, ['tool']) as argv:
                assert (mount in argv) == (role == 'review')


def test_controller_publishes_only_the_verified_artifact_to_reviewer(job):
    runner = controller(job)
    runner.preflight_boundary = True
    runner.boundary = lambda identity, *a: report(identity)
    published = []
    def publish(path):
        published.append(path)
        return path is not None
    runner.driver.set_review_evidence = publish
    state = runner.run()
    assert state['status'] == 'ready'
    assert json.loads(published[-1].read_text())['snapshot'] == state['snapshot']
    runner.checkpoint(verification_runtime='changed')
    runner.review(state['snapshot'], runner.workspace.candidate)
    assert published[-1] is None
