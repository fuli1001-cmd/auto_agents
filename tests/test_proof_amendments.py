import json
from pathlib import Path
import pytest
from auto_agents.models import AgentResult
from auto_agents.proof_amendments import admissible
BEFORE = 'import unittest\nclass Tests(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 1)\n'

@pytest.fixture
def retained_review(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from auto_agents import proof_amendments as review
    from auto_agents.proof_support.store import digest
    verdict = json.loads((Path(__file__).parent / 'fixtures/proof_review_related_config.json').read_text())
    value = {'owner': {'subject': 'session:original'}, 'candidate': 'sealed-candidate', 'changes': {'tests/test_provider_capability_snapshot.py': {}}, 'delta': {row['path']: {} for row in verdict['change_coverage']}, 'requirements': [{'requirement_ids': ['REQ-275']}]}
    monkeypatch.setattr(review, 'inputs', lambda *args: value)
    messages, calls = ([], [])

    def provider(request):
        calls.append(request.purpose)
        return AgentResult(True, [], request.output_path, summary=json.dumps(verdict))
    session = SimpleNamespace(project_root=tmp_path, _save=lambda state: None, _print=messages.append, orch=SimpleNamespace(config=SimpleNamespace(efforts={}), _call_with_failover=provider))
    state = SimpleNamespace(proof_review={}, workflow_id='', session_id='child', execution_log=[], status='paused', resolution='proof_review_invalid', resume_phase='executing')
    store = review._store(session, value)
    saved = {'status': 'invalid', 'inputs': digest(value), 'owner': value['owner'], 'model_calls': 1, 'reply': store.artifact('reply', {'text': json.dumps(verdict)}), 'error': 'incomplete amendment coverage'}
    store.save(saved)
    return SimpleNamespace(session=session, state=state, store=store, value=value, verdict=verdict, messages=messages, calls=calls)

def test_real_reply_previously_invalid_is_reused_without_model_call(retained_review):
    from auto_agents import proof_amendments as review
    r = retained_review
    old_reply = r.store.load()['reply']
    assert review.ensure(r.session, r.state) == 'approved'
    saved = r.store.load()
    assert saved['reply'] == old_reply and saved['model_calls'] == 1 and (saved['error'] is None)
    assert r.store.read(saved['receipt'])['decision'] == 'approve'
    assert r.calls == []
    assert any(('复用已保存的审核回答' in message for message in r.messages))
    assert review.ensure(r.session, r.state) == 'approved'
    assert r.calls == []

@pytest.mark.parametrize('defect,message', [('missing_test', '测试覆盖说明缺少文件'), ('foreign_path', '候选改动之外'), ('unknown_requirement', '未绑定的需求'), ('empty_reason', '缺少原因'), ('missing_product', '全部改动'), ('malformed_path', '候选改动之外'), ('malformed_requirement', '未绑定的需求'), ('malformed_product', '全部改动')])
def test_invalid_coverage_remains_blocked_with_persisted_reason(retained_review, defect, message):
    from auto_agents import proof_amendments as review
    r = retained_review
    if defect == 'missing_test':
        r.verdict['coverage'] = r.verdict['coverage'][:1]
    elif defect == 'foreign_path':
        r.verdict['coverage'][0]['path'] = 'unrelated.py'
    elif defect == 'unknown_requirement':
        r.verdict['coverage'][0]['requirement'] = 'REQ-unapproved'
    elif defect == 'empty_reason':
        r.verdict['coverage'][0]['reason'] = '  '
    elif defect == 'missing_product':
        r.verdict['change_coverage'].pop()
    elif defect == 'malformed_path':
        r.verdict['coverage'][0]['path'] = []
    elif defect == 'malformed_requirement':
        r.verdict['coverage'][0]['requirement'] = []
    else:
        r.verdict['change_coverage'][0]['path'] = []
    saved = r.store.load()
    saved.update(status='received', reply=r.store.artifact('reply', {'text': json.dumps(r.verdict)}))
    r.store.save(saved)
    assert review.ensure(r.session, r.state) == 'paused'
    saved = r.store.load()
    assert saved['status'] == 'invalid' and message in saved['error']
    assert r.state.resolution == 'proof_review_invalid'
    assert any((message in m for m in r.messages))
    assert not saved.get('receipt') and r.calls == []

def test_genuinely_invalid_reply_can_retry_review_without_implementation(retained_review):
    from auto_agents import proof_amendments as review
    r = retained_review
    saved = r.store.load()
    saved['reply'] = r.store.artifact('reply', {'text': 'invalid JSON'})
    r.store.save(saved)
    assert review.ensure(r.session, r.state) == 'approved'
    assert r.calls == ['proof_review'] and r.store.load()['model_calls'] == 2

def test_changed_inputs_cannot_reuse_previous_approval(retained_review):
    from auto_agents import proof_amendments as review
    r = retained_review
    assert review.ensure(r.session, r.state) == 'approved'
    r.value['candidate'] = 'different-candidate'
    assert review.ensure(r.session, r.state) == 'approved'
    assert r.calls == ['proof_review']

def test_unittest_addition_and_requirement_revision_can_be_reviewed():
    assert admissible(BEFORE, BEFORE + '    def test_added(self) -> None:\n        self.assertTrue(True)\n')
    assert admissible(BEFORE, BEFORE.replace('assertEqual(1, 1)', 'assertEqual(2, 2)'))
    assert admissible(BEFORE, BEFORE + '\ndef test_added(tmp_path):\n    assert tmp_path.is_dir()\n')
    parameterized = "import pytest\n@pytest.mark.parametrize('value', [1, 2])\ndef test_value(value):\n    assert value > 0\n"
    assert admissible(parameterized, parameterized.replace('value > 0', 'value >= 1'))
    assert not admissible(parameterized, parameterized.replace('[1, 2]', '[1]'))
    meaningful = 'def test_value():\n    assert load_value() == 1\n'
    assert not admissible(meaningful, meaningful.replace('load_value() == 1', 'True'))

@pytest.mark.parametrize('after', [BEFORE.replace('assertEqual(1, 1)', 'skipTest("bypass")'), BEFORE.replace('test_value', 'disabled_value'), BEFORE + '    def setUp(self):\n        self.skipTest("bypass")\n', BEFORE + '    def test_added(self=print("side effect")):\n        pass\n', BEFORE + '    def test_value(self):\n        pass\n', BEFORE.replace('import unittest', 'import unittest\nimport pytest')])
def test_review_cannot_override_execution_controls(after):
    assert not admissible(BEFORE, after)

@pytest.mark.parametrize('output,ok', [('PASSED tests/test_value.py::test_a\nPASSED tests/test_value.py::test_b[p]\n', True), ('2 skipped\n', False), ('2 tests collected\n', False), ('PASSED tests/test_value.py::test_a\nSKIPPED tests/test_value.py::test_b\n', False)])
def test_amendment_requires_executed_passing_nodes(output, ok):
    from types import SimpleNamespace
    from auto_agents.models import CommandResult, GateResult
    from auto_agents.proof_amendments import execution_evidence
    session = SimpleNamespace(_amendment_commands={'pytest': ['tests/test_value.py::test_a', 'tests/test_value.py::test_b']})
    gate = GateResult(True, [CommandResult('pytest', True, 0, stdout=output)])
    assert execution_evidence(session, gate) is ok

def test_reviewed_coverage_maps_changes_without_bypassing_release_policy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from auto_agents import proof_amendments as review
    from auto_agents.models import GateConfig, VerificationStep
    from auto_agents.verification_selection import select_verification_steps
    from test_session_verification_ownership import git
    git(tmp_path, 'init', '-q')
    config = GateConfig(steps=[VerificationStep(proof_id='release.all', runner='pytest', targets=['tests'], levels=['release'])], unmapped_change_policy='fallback', fallback_proof_ids=['release.all'])
    value = {'changes': {'tests/test_value.py': {'before': 'def test_value():\n    assert value == 0\n', 'after': 'def test_value():\n    assert value == 1\n'}}, 'delta': {'value.py': {}, 'tests/test_value.py': {}}}
    monkeypatch.setattr(review, 'inputs', lambda *args: value)
    monkeypatch.setattr(review, 'identities', lambda *args: [])
    session = SimpleNamespace(project_root=tmp_path)
    assert review.covered_gates(session, None, config) is config
    monkeypatch.setattr(review, 'identities', lambda *args: ['approved-receipt'])
    covered = review.covered_gates(session, None, config)

    def select(level, paths):
        return select_verification_steps(covered.steps, tmp_path, covered, level=level, changed_paths=paths)
    focused = select('affected', value['delta'])
    assert 'release.all' not in focused.proof_ids and (not focused.unmapped_paths)
    assert any((p.startswith('proof-amendment.') for p in focused.proof_ids))
    assert 'release.all' in select('affected', [*value['delta'], 'unreviewed.py']).proof_ids
    assert 'release.all' in select('release', value['delta']).proof_ids
    covered.release_blocking_paths = ['value.py']
    assert 'release.all' in select('affected', value['delta']).proof_ids
    assert [s.proof_id for s in config.steps] == ['release.all']
