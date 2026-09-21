import json
from pathlib import Path

import pytest

from auto_agents.config import load_session_state, save_session_state
from auto_agents.models import AgentResult
from auto_agents.orchestrator import Orchestrator
from auto_agents.proof_amendments import admissible
from auto_agents.session import Session
from test_engine_child_recovery import configure_local_writer, REAL_PROVIDER_CALL
from test_session_verification_ownership import project


BEFORE = 'import unittest\nclass Tests(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 1)\n'


@pytest.fixture
def retained_review(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from auto_agents import proof_amendments as review
    from auto_agents.repair_v2.store import digest
    verdict = json.loads((Path(__file__).parent / 'fixtures/proof_review_related_config.json').read_text())
    value = {'owner': {'subject': 'session:original'}, 'candidate': 'sealed-candidate',
             'changes': {'tests/test_provider_capability_snapshot.py': {}},
             'delta': {row['path']: {} for row in verdict['change_coverage']},
             'requirements': [{'requirement_ids': ['REQ-275']}]}
    monkeypatch.setattr(review, 'inputs', lambda *args: value)
    messages, calls = [], []
    def provider(request):
        calls.append(request.purpose)
        return AgentResult(True, [], request.output_path, summary=json.dumps(verdict))
    session = SimpleNamespace(project_root=tmp_path, _save=lambda state: None, _print=messages.append,
        orch=SimpleNamespace(config=SimpleNamespace(efforts={}), _call_with_failover=provider))
    state = SimpleNamespace(proof_review={}, workflow_id='', session_id='child',
                            execution_log=[], status='paused', resolution='proof_review_invalid', resume_phase='executing')
    store = review._store(session, value)
    saved = {'status': 'invalid', 'inputs': digest(value), 'owner': value['owner'], 'model_calls': 1,
             'reply': store.artifact('reply', {'text': json.dumps(verdict)}), 'error': 'incomplete amendment coverage'}
    store.save(saved)
    return SimpleNamespace(session=session, state=state, store=store, value=value,
                           verdict=verdict, messages=messages, calls=calls)


def test_real_reply_previously_invalid_is_reused_without_model_call(retained_review):
    from auto_agents import proof_amendments as review
    r = retained_review
    old_reply = r.store.load()['reply']
    assert review.ensure(r.session, r.state) == 'approved'
    saved = r.store.load()
    assert saved['reply'] == old_reply and saved['model_calls'] == 1 and saved['error'] is None
    assert r.store.read(saved['receipt'])['decision'] == 'approve'
    assert r.calls == []
    assert any('复用已保存的审核回答' in message for message in r.messages)
    assert review.ensure(r.session, r.state) == 'approved'
    assert r.calls == []


@pytest.mark.parametrize('defect,message', [
    ('missing_test', '测试覆盖说明缺少文件'),
    ('foreign_path', '候选改动之外'),
    ('unknown_requirement', '未绑定的需求'),
    ('empty_reason', '缺少原因'),
    ('missing_product', '全部改动'),
    ('malformed_path', '候选改动之外'),
    ('malformed_requirement', '未绑定的需求'),
    ('malformed_product', '全部改动'),
])
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
    assert any(message in m for m in r.messages)
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


@pytest.mark.parametrize('after', [
    BEFORE.replace('assertEqual(1, 1)', 'skipTest("bypass")'),
    BEFORE.replace('test_value', 'disabled_value'),
    BEFORE + '    def setUp(self):\n        self.skipTest("bypass")\n',
    BEFORE + '    def test_added(self=print("side effect")):\n        pass\n',
    BEFORE + '    def test_value(self):\n        pass\n',
    BEFORE.replace('import unittest', 'import unittest\nimport pytest'),
])
def test_review_cannot_override_execution_controls(after):
    assert not admissible(BEFORE, after)


@pytest.mark.parametrize('output,ok', [
    ('PASSED tests/test_value.py::test_a\nPASSED tests/test_value.py::test_b[p]\n', True),
    ('2 skipped\n', False), ('2 tests collected\n', False),
    ('PASSED tests/test_value.py::test_a\nSKIPPED tests/test_value.py::test_b\n', False),
])
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
    config = GateConfig(steps=[VerificationStep(proof_id='release.all', runner='pytest', targets=['tests'], levels=['release'])],
                        unmapped_change_policy='fallback', fallback_proof_ids=['release.all'])
    value = {'changes': {'tests/test_value.py': {'before': 'def test_value():\n    assert value == 0\n',
        'after': 'def test_value():\n    assert value == 1\n'}},
        'delta': {'value.py': {}, 'tests/test_value.py': {}}}
    monkeypatch.setattr(review, 'inputs', lambda *args: value)
    monkeypatch.setattr(review, 'identities', lambda *args: [])
    session = SimpleNamespace(project_root=tmp_path)
    assert review.covered_gates(session, None, config) is config
    monkeypatch.setattr(review, 'identities', lambda *args: ['approved-receipt'])
    covered = review.covered_gates(session, None, config)
    def select(level, paths):
        return select_verification_steps(covered.steps, tmp_path, covered, level=level, changed_paths=paths)
    focused = select('affected', value['delta'])
    assert 'release.all' not in focused.proof_ids and not focused.unmapped_paths
    assert any(p.startswith('proof-amendment.') for p in focused.proof_ids)
    assert 'release.all' in select('affected', [*value['delta'], 'unreviewed.py']).proof_ids
    assert 'release.all' in select('release', value['delta']).proof_ids
    covered.release_blocking_paths = ['value.py']
    assert 'release.all' in select('affected', value['delta']).proof_ids
    assert [s.proof_id for s in config.steps] == ['release.all']


@pytest.mark.parametrize('interrupt', ['', 'received', 'approved', 'invalid'])
def test_real_candidate_review_verification_delivery_and_restart(tmp_path, monkeypatch, interrupt):
    root, child = project(tmp_path)
    configure_local_writer(root, child, """
Path('value.py').write_text('VALUE = 1\\n')
with Path('tests/test_owned.py').open('a') as f:
    f.write('\\ndef test_added_regression():\\n    from value import VALUE\\n    assert VALUE == 1\\n')
""")
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        if request.purpose == 'proof_review':
            assert request.sandbox_mode == 'read-only' and not request.resume_session_id
            value = json.loads(request.prompt.split('\n', 1)[1])
            return AgentResult(True, [], request.output_path, summary=json.dumps({
                'decision': 'approve', 'reason': 'New regression preserves the original value requirement',
                'change_coverage': [{'path': p, 'reason': 'Required candidate repair'} for p in value['delta']],
                'coverage': [{'path': path, 'requirement': 'original_goal', 'reason': 'Verifies the requested value'}
                             for path in value['delta']]}))
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    if interrupt:
        from auto_agents.repair_v2.store import Store
        transition = Store.transition
        interrupted = []
        def crash(store, state, **updates):
            result = transition(store, state, **updates)
            boundary = 'received' if interrupt == 'invalid' else interrupt
            if 'proof-reviews' in str(store.root) and updates.get('status') == boundary and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()
            return result
        monkeypatch.setattr(Store, 'transition', crash)
    session = Session(Orchestrator(root), mode='fix', auto_approve=True)
    result = session.resume(child.session_id)
    if interrupt:
        assert result.status == 'paused'
        receipt = result.candidate_custody['receipt']['fingerprint']
        if interrupt == 'invalid':
            # Simulate the old validator rejecting this already received approval.
            store = Store(root / '.auto-agents/state/proof-reviews' / result.proof_review['inputs'])
            with store.locked():
                store.transition(store.load(), status='invalid', error='incomplete amendment coverage')
        result = Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
        assert result.candidate_custody['receipt']['fingerprint'] == receipt
    assert result.status == 'completed', [(r.get('action'), r.get('result')) for r in result.execution_log]
    assert calls == ['fix', 'proof_review']
    assert result.current_attempt == 1
    verified = [r for r in result.execution_log if r.get('action') in {'verify', 'inventory_migration_verify'}]
    assert verified and verified[-1]['result'] == 'pass' and verified[-1]['executed_commands'] > 0
    assert result.proof_review['candidate'] == result.candidate_custody['receipt']['fingerprint']
    assert result.candidate_custody['delivered_revision']
    before = list(calls)
    result = Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
    assert result.status == 'completed' and calls == before
    assert load_session_state(root, child.session_id).current_attempt == 1


@pytest.mark.parametrize('review_decision,assertion', [('reject', 1), ('approve', 99)])
def test_review_or_new_regression_failure_cannot_be_delivered(tmp_path, monkeypatch, review_decision, assertion):
    root, child = project(tmp_path)
    child.max_attempts = child.hard_ceiling = 1
    save_session_state(root, child)
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')\n"
        "with Path('tests/test_owned.py').open('a') as f:\n"
        "    f.write('\\ndef test_added_regression():\\n    from value import VALUE\\n    assert VALUE == " + str(assertion) + "\\n')\n")
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        if request.purpose == 'proof_review':
            value = json.loads(request.prompt.split('\n', 1)[1])
            return AgentResult(True, [], request.output_path, summary=json.dumps({
                'decision': review_decision, 'reason': 'Review of retained value requirement',
                'change_coverage': [{'path': p, 'reason': 'Required candidate repair'} for p in value['delta']],
                'coverage': [{'path': 'tests/test_owned.py', 'requirement': 'original_goal', 'reason': 'Value requirement'}]}))
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    result = Session(Orchestrator(root), mode='fix', auto_approve=True).resume(child.session_id)
    assert result.status != 'completed'
    assert not result.candidate_custody.get('delivered_revision')
    assert calls == ['fix', 'proof_review'] and result.current_attempt == 1
    if review_decision == 'approve':
        failures = [r for r in result.execution_log if r.get('action') == 'verify']
        assert failures[-1]['executed_commands'] > 0
        assert failures[-1]['result'] != 'pass'


def test_user_choice_survives_restart_without_reimplementing_candidate(tmp_path, monkeypatch):
    root, child = project(tmp_path)
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')\n"
        "with Path('tests/test_owned.py').open('a') as f:\n"
        "    f.write('\\ndef test_added_regression():\\n    from value import VALUE\\n    assert VALUE == 1\\n')\n")
    calls, prompts = [], []
    def provider(self, request):
        calls.append(request.purpose)
        if request.purpose == 'proof_review':
            value = json.loads(request.prompt.split('\n', 1)[1])
            verdict = ({'decision': 'approve', 'reason': 'User clarified the required value',
                'change_coverage': [{'path': p, 'reason': 'Required candidate repair'} for p in value['delta']],
                'coverage': [{'path': 'tests/test_owned.py', 'requirement': 'original_goal', 'reason': 'Value remains 1'}]}
                if value['owner']['approved_changes'] else
                {'decision': 'needs_user', 'reason': 'Requirement needs clarification',
                 'question': '这个测试应该验证返回值为 1 吗？', 'suggestion': '按返回值为 1 的要求继续验证。'})
            return AgentResult(True, [], request.output_path, summary=json.dumps(verdict))
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    def answer(value):
        def respond(prompt, **kwargs):
            prompts.append(prompt)
            return value
        return respond
    waiting = Session(Orchestrator(root, user_input_fn=answer('3')), mode='fix', auto_approve=True).resume(child.session_id)
    assert waiting.status == 'waiting_user' and calls == ['fix', 'proof_review']
    receipt = waiting.candidate_custody['receipt']['fingerprint']
    result = Session(Orchestrator(root, user_input_fn=answer('1')), mode='fix', auto_approve=True).resume(child.session_id)
    assert result.status == 'completed', [(r.get('action'), r.get('result')) for r in result.execution_log[-4:]]
    assert result.candidate_custody['receipt']['fingerprint'] == receipt and result.current_attempt == 1
    assert calls == ['fix', 'proof_review', 'proof_review']
    assert any('这个测试应该验证返回值为 1' in p for p in prompts)


def test_later_engine_failure_recovers_at_verification_without_another_writer(tmp_path, monkeypatch):
    from test_engine_child_recovery import parent_workflow, ObservationBoundary
    from test_multilayer_engine_recovery import replay, ENGINE
    root, child = project(tmp_path)
    configure_local_writer(root, child, "Path('value.py').write_text('VALUE = 1\\n')")
    workflows, workflow, original = parent_workflow(root, child)
    calls = []
    def provider(self, request):
        calls.append(request.purpose)
        assert request.purpose == 'fix'
        return REAL_PROVIDER_CALL(self, request)
    monkeypatch.setattr(Orchestrator, '_call_with_failover', provider)
    phase = Session._phase_collab_loop
    def observe(self, state):
        if load_session_state(root, child.session_id).status == 'completed':
            raise ObservationBoundary()
        return phase(self, state)
    monkeypatch.setattr(Session, '_phase_collab_loop', observe)
    with pytest.raises(ObservationBoundary):
        Session(Orchestrator(root), mode='collab', auto_approve=True).resume('parent')
    child = load_session_state(root, child.session_id)
    receipt = child.candidate_custody['receipt']['fingerprint']
    assert child.status == 'completed' and calls == ['fix']
    # The next engine failure has the same child and handoff but occurs after
    # the earlier implementation/verification/delivery actually completed.
    child.status, child.resolution = 'failed', 'verification_execution_binding'
    child.execution_log.append({'action': 'verify', 'failure_kind': child.resolution,
                               'result': 'later engine verification return failed', 'retry_fix': False})
    save_session_state(root, child)
    payload = {'child_session_id': child.session_id, 'issue_seed': {'target_repository': str(ENGINE),
               'failed_handoff_id': original.handoff_id, 'original_handoff_id': original.handoff_id}}
    workflow = workflows.load(workflow.workflow_id)
    handoff = workflows.prepare_handoff(workflow, parent=original.parent, target='fix', goal='Repair verification return',
                                       reason='new execution failure', payload=payload)
    parent = load_session_state(root, 'parent')
    parent.active_handoff_id, parent.status, parent.return_phase = handoff.handoff_id, 'waiting_child', ''
    save_session_state(root, parent)
    result = replay(root, payload, tmp_path)
    assert result['ok'], result
    observed = result['recovery_observation']
    assert observed['boundary_kind'] == 'verification'
    assert observed['candidate_fingerprint'] == receipt
    assert observed['budget_reserved'] == observed.get('provider_boundary_calls', 0) == 0
    assert observed['verification_identity']
