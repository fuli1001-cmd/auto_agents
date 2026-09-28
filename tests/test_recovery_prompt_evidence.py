"""Large verification receipts must remain readable without filling model input."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from auto_agents.recovery.engine import IsolatedEngineEffects
from auto_agents.recovery.model import Command, digest
from auto_agents.recovery.prompt_evidence import PromptEvidence, result_summary
from auto_agents.repair_v2.providers import AgentSandbox, NativeDriver
from auto_agents.repair_v2.workspace import git, source_identity
from test_recovery_kernel import scene


def large_verification(ok=False):
    nodes = ['tests/test_matrix.py::test_case[' + str(i) + ']' for i in range(5590)]
    return {
        'suite': {'ok': True, 'snapshot': 'a' * 64, 'failures': [], 'checks': [
            {'ok': True, 'collected': nodes, 'passed': nodes, 'failed': [], 'missing': [],
             'excerpt': 'successful test output\n' * 200_000}]},
        'boundary': {'ok': ok, 'cases': [{'ok': ok, 'observed': {
            'error': '' if ok else 'bound child did not pass its retained recovery preflight',
            'recovery_observation': {'child_session_id': 'retained-child',
                'preflight_outcome': 'passed' if ok else 'not_passed',
                'current_failure': {} if ok else {'result': 'retained pytest selection failed',
                    'diagnostic': {'stderr_tail': 'file not found: tests/test_planned.py'}}}}}]},
    }


def test_large_receipt_keeps_all_checks_and_failed_boundary_without_inline_logs(tmp_path):
    driver = Mock()
    evidence = PromptEvidence(tmp_path / 'evidence', driver)
    receipt = large_verification()
    before = digest(receipt)
    rendered = evidence.render(receipt, summary=result_summary(receipt))
    assert len(rendered) < 5000
    assert 'retained pytest selection failed' in rendered
    assert 'tests/test_planned.py' in rendered
    assert '"passed": 5590' in rendered
    assert 'successful test output' not in rendered
    descriptor = json.loads(rendered)
    saved = json.loads((evidence.root / Path(descriptor['file']).name).read_text())
    assert saved == receipt
    assert digest(receipt) == before == descriptor['content_digest']
    driver.set_prompt_evidence.assert_called_once_with(evidence.root)


def test_oversized_diagnosis_and_plan_are_referenced_intact_and_sanitized(tmp_path):
    evidence = PromptEvidence(tmp_path / 'evidence', Mock())
    value = {'error': 'large diagnostic\n' * 100_000, 'api_key': 'private-value'}
    rendered = evidence.render(value, summary={'diagnosis': value})
    assert len(rendered) < 2000
    assert 'private-value' not in rendered
    files = [json.loads(path.read_text()) for path in evidence.root.glob('*.json')]
    assert {'error': value['error'], 'api_key': '<redacted>'} in files
    assert any(row.get('diagnosis', {}).get('error') == value['error'] for row in files)
    assert all('private-value' not in path.read_text() for path in evidence.root.glob('*.json'))


class PromptCaptured(Exception):
    pass


@pytest.mark.parametrize('phase', ['plan', 'implement', 'review', 'review-correction'])
def test_real_engine_request_uses_mounted_receipts_in_every_model_phase(scene, tmp_path, monkeypatch, phase):
    store, contract = scene
    contract = replace(contract, kind='engine_repair', phases=('plan', 'implement', 'verify', 'review'))
    candidate = tmp_path / 'source'
    candidate.mkdir()
    git(candidate, 'init', '-q')
    (candidate / 'value.py').write_text('value = 1\n')
    git(candidate, 'add', '.')
    git(candidate, 'commit', '-qm', 'candidate')
    source = source_identity(candidate)
    receipt = large_verification(ok=phase.startswith('review'))
    receipt['suite']['snapshot'] = source
    reference = store.put(receipt)
    failure = {'kind': 'candidate_rejected', 'reason': 'Original failure recovery checked',
               'details': {'result_ref': reference}}
    commands = [{'task_id': contract.task_id, 'command_id': 'failed-check', 'sequence': 1,
                 'outcome': failure}]
    if phase == 'review-correction':
        previous = store.put({'reply': json.dumps({'decision': 'REJECT', 'findings': [
            {'reason': 'concrete counterexample\n' * 100_000}]}), 'diagnostic': 'Unexpected change ID'})
        commands.append({'task_id': contract.task_id, 'command_id': 'invalid-review', 'sequence': 2,
            'outcome': {'kind': 'protocol_invalid', 'details': {'result_ref': previous}}})
    monkeypatch.setattr(store, 'load', lambda stream: {'commands': {str(i): c for i, c in enumerate(commands)}})
    effects = IsolatedEngineEffects.__new__(IsolatedEngineEffects)
    effects.store, effects.stream, effects.contract = store, 'workflow', contract
    effects.root, effects.candidate = tmp_path / 'repair', candidate
    effects.payload = {'base': 'base'}
    effects.accepted = SimpleNamespace(acceptance=())
    effects.evidence_context = {'manifest': '/repair-evidence/manifest.json'}
    effects.cancel, effects.scope = None, Mock()
    effects.progress = Mock()
    effects.source = lambda: source_identity(candidate)
    effects._previous = lambda kind: {'verify': receipt, 'implement': {
        'artifact': {'path': str(effects.candidate)}}, 'plan': {'text': 'Retained plan\n' * 100_000}}[kind]
    monkeypatch.setattr('auto_agents.repair_v2.scope.changes', lambda *args: {})
    effects.driver = Mock()
    def capture(role, prompt, root, **kwargs):
        assert len(prompt) < 100_000
        assert '/repair-observations/' in prompt
        assert 'successful test output' not in prompt
        kwargs['progress']({'event': 'item/completed'})
        effects.progress.agent.assert_called_once_with({'event': 'item/completed'})
        mount = effects.driver.set_prompt_evidence.call_args.args[0]
        documents = [json.loads(p.read_text()) for p in mount.glob('*.json')]
        assert receipt in documents
        if phase == 'implement':
            assert 'retained pytest selection failed' in prompt
        if phase == 'review-correction':
            assert any(isinstance(d, str) and 'Preserve the substantive' in d
                       and 'concrete counterexample' in d for d in documents)
        raise PromptCaptured()
    effects.driver.run.side_effect = capture
    command = Command('current', 'workflow', contract.task_id, phase.split('-')[0], source,
                      contract.identity, 'b' * 64, 'c' * 64, 'current', True)
    with pytest.raises(PromptCaptured):
        effects._execute(command)


def test_prompt_evidence_is_read_only_for_writer_and_reviewer(tmp_path):
    sandbox = AgentSandbox(tmp_path / 'agent', 'pinned')
    driver = NativeDriver.__new__(NativeDriver)
    driver.sandbox = sandbox
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    driver.set_prompt_evidence(evidence)
    root = tmp_path / 'candidate'
    root.mkdir()
    mount = f'type=bind,src={evidence},dst=/repair-observations,readonly'
    with patch.object(sandbox, 'home', return_value=tmp_path / 'private'), \
         patch('auto_agents.repair_v2.docker.run', return_value=(0, '')):
        for role in ('plan', 'implement', 'review'):
            with sandbox.command(role, root, ['/usr/bin/tool']) as argv:
                assert mount in argv
                assert not any('control.sqlite3' in arg or 'kernel-objects' in arg for arg in argv)
