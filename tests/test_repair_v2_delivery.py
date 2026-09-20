import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from auto_agents.repair_v2 import AgentReply, ValidationResult
from auto_agents.repair_v2 import integration
from auto_agents.repair_v2.store import Store
from auto_agents.repair_v2.transaction import transaction_root
from auto_agents.repair_v2.types import RepairBlocked
from auto_agents.repair_v2.workspace import git, source_identity
from auto_agents.repair_control import Store as ControlStore


@pytest.fixture
def repair_request(tmp_path):
    source, project, remote = tmp_path / 'engine', tmp_path / 'project', tmp_path / 'remote.git'
    source.mkdir(); project.mkdir()
    session = project / '.auto-agents/state/sessions/existing-child/session_state.json'
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({'session_id': 'existing-child', 'goal': 'Restore the value to one',
                                   'last_error': 'value is zero', 'mode': 'fix'}))
    git(source, 'init', '-q', '-b', 'master')
    (source / 'source.py').write_text('value = 0\n')
    (source / 'tests').mkdir()
    (source / 'tests/test_value.py').write_text('from source import value\ndef test_value(): assert value == 1\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'baseline')
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    git(source, 'remote', 'add', 'origin', str(remote)); git(source, 'push', '-qu', 'origin', 'master')
    (project / 'keep.txt').write_text('original user data')
    controller_source = tmp_path / 'controller'
    subprocess.run(['git', 'clone', '-q', str(source), str(controller_source)], check=True)
    config = {'root': str(tmp_path / 'control'), 'source_root': str(source), 'implementation_root': str(controller_source),
              'remote': str(remote), 'ref': 'refs/heads/master', 'identity': 'test', 'python': 'python',
              'publish': True, 'repair_engine': 'v2'}
    payload = {'project': str(project), 'base': git(source, 'rev-parse', 'HEAD'), 'provider': 'fake',
               'contract': {'expected_postconditions': ['value is one']}, 'fingerprint': 'value-zero',
               'error': 'value is zero', 'autonomy': 'max', 'environment': 'same',
               'diagnosis': {'final': {'expected_postconditions': ['value is one']}},
               'invocation': {'command': 'collab', 'session_id': 'existing-child'}}
    store = ControlStore(config['root'])
    subscriber = store.register({'project': str(project), 'token': 'token'})
    identity = store.submit(subscriber, payload)
    return {'config': config, 'job': store.job(identity)}


class Driver:
    def __init__(self, after=None): self.calls, self.after = [], after
    def run(self, role, prompt, root, **kwargs):
        self.calls.append(role)
        if role == 'plan': return AgentReply(True, 'Fix value across the complete repair_request.\nREPAIR_SCOPE v1: ' + json.dumps({
            'decision': 'required', 'blocked_step': 'read the value', 'consequence': 'zero blocks the requested value',
            'evidence_refs': ['source.py'], 'recovery_check': 'value is one'}), 'writer')
        if role == 'implement':
            (Path(root) / 'source.py').write_text('value = 1\n')
            if self.after: self.after(); self.after = None
            return AgentReply(True, 'implemented', 'writer')
        # The frozen IDs come from the repair_request, never from a test-only alias.
        context = json.loads(prompt.split('\n')[1])
        identity = context['requirements'][0]['identity']
        from auto_agents.repair_v2.scope import changes
        base = git(root, 'rev-list', '--max-parents=0', 'HEAD')
        return AgentReply(True, json.dumps({'decision': 'APPROVE', 'findings': [],
            'change_coverage': [{'change': key, 'requirement': identity, 'reason': 'restore the required value',
                                 'evidence': 'test_value observes value one'} for key in changes(root, base, context.get('preserved_upstream', []))],
            'coverage': [{'requirement': identity, 'nodes': ['tests/test_value.py::test_value']}]}), 'reviewer')


class Verifier:
    runtime = 'fixed-verifier'
    def __init__(self): self.calls = []
    def prepare(self): pass
    def validate(self, identity, source, units, cancel):
        self.calls.append(identity)
        ok = (Path(source) / 'source.py').read_text() == 'value = 1\n'
        return ValidationResult(ok, identity,
            checks=[{'passed': ['tests/test_value.py::test_value'], 'inputs': {'runtime': self.runtime}}],
            failures=[] if ok else [{'unit': 'value', 'failed': ['tests/test_value.py::test_value'], 'reason': 'wrong'}])
    def regression(self, identity, source, base, commit, coverage, cancel):
        assert git(base, 'show', commit + ':source.py') == 'value = 0'
        return {'ok': True, 'snapshot': identity, 'demonstrated_regression': True}
    def boundary(self, identity, source, target, payload, cancel):
        assert (Path(target) / 'keep.txt').read_text() == 'original user data'
        return {'ok': True, 'snapshot': identity, 'observed': {'session_id': payload['invocation']['session_id']}}


def components(driver, verifier):
    return lambda repair_request, root, accepted, workspace, python: (Store(root), verifier, driver)


def test_complete_acceptance_delivers_without_loading_legacy_runner(repair_request):
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)), \
         patch('auto_agents.repair_worker.make_runner', side_effect=AssertionError('legacy runner was loaded')):
        result = integration.repair_entry(repair_request)
    assert result['ok'] and result['engine'] == 'v2'
    source = Path(repair_request['config']['source_root'])
    assert (source / 'source.py').read_text() == 'value = 1\n'
    assert git(source, 'rev-parse', 'HEAD') == result['commit']
    assert driver.calls == ['plan', 'implement', 'review']
    assert driver.calls.index('plan') < driver.calls.index('implement') < driver.calls.index('review')
    assert integration.verify_receipt(result)['boundary']
    assert (Path(repair_request['job']['payload']['project']) / 'keep.txt').read_text() == 'original user data'


def test_foreign_engine_edits_block_delivery_without_discarding_them(repair_request):
    source = Path(repair_request['config']['source_root'])
    driver = Driver(after=lambda: (source / 'foreign.txt').write_text('other process work'))
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, Verifier())):
        with pytest.raises(RuntimeError, match='uncommitted'):
            integration.repair_entry(repair_request)
    assert (source / 'foreign.txt').read_text() == 'other process work'
    assert (source / 'source.py').read_text() == 'value = 0\n'


def test_source_advance_gets_revalidated_without_a_new_plan(repair_request):
    source = Path(repair_request['config']['source_root'])
    def advance():
        (source / 'other.txt').write_text('concurrent upstream change')
        git(source, 'add', '.'); git(source, 'commit', '-qm', 'advance')
        git(source, 'push', '-q', 'origin', 'master')
    driver, verifier = Driver(after=advance), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        result = integration.repair_entry(repair_request)
    assert result['ok'] and (source / 'other.txt').read_text() == 'concurrent upstream change'
    assert driver.calls.count('plan') == 1 and driver.calls.count('review') == 2
    assert len(set(verifier.calls)) == 2


def test_new_job_and_engine_revision_do_not_create_a_new_transaction(repair_request):
    first = transaction_root(repair_request['config'], repair_request['job']['payload'])
    updated = {**repair_request['job']['payload'], 'base': 'new-engine', 'environment': 'new-env'}
    assert transaction_root(repair_request['config'], updated) == first
    updated['invocation'] = {**updated['invocation'], 'session_id': 'other-child'}
    assert transaction_root(repair_request['config'], updated) != first


def test_progress_uses_unified_phases_without_legacy_group_labels():
    from auto_agents.repair_client import _repair_progress_message
    job = {'state': 'repairing', 'progress': {'engine': 'v2', 'phase': 'validate', 'attempt': 1,
           'group_progress': {'index': 1, 'total': 9, 'title': 'old group'}},
           'prior_repair_input': {'completed_groups': 3}}
    text = _repair_progress_message(job, {'state': 'waiting'})
    assert '集中验收' in text and '1/9' not in text and 'old group' not in text


def test_cancelled_generation_cannot_update_the_installation(repair_request):
    source = Path(repair_request['config']['source_root'])
    def cancel():
        ControlStore(repair_request['config']['root']).cancel(job=repair_request['job']['id'])
    driver = Driver(after=cancel)
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, Verifier())):
        with pytest.raises(RepairBlocked, match='lost ownership'):
            integration.repair_entry(repair_request)
    assert (source / 'source.py').read_text() == 'value = 0\n'


def test_switching_provider_does_not_create_a_fresh_repair_budget(repair_request):
    first = transaction_root(repair_request['config'], repair_request['job']['payload'])
    other = {**repair_request['job']['payload'], 'provider': 'another-configured-provider'}
    assert transaction_root(repair_request['config'], other) == first


def test_recovery_identity_includes_git_ignored_workflow_state(tmp_path):
    from auto_agents.repair_v2.evidence import identity
    git(tmp_path, 'init', '-q')
    (tmp_path / '.gitignore').write_text('.auto-agents/\n')
    git(tmp_path, 'add', '.'); git(tmp_path, 'commit', '-qm', 'base')
    state = tmp_path / '.auto-agents/state'; state.mkdir(parents=True)
    (state / 'session.json').write_text('{"child":"first"}')
    before = identity(tmp_path)
    (state / 'session.json').write_text('{"child":"second"}')
    assert identity(tmp_path) != before


def test_v2_job_dedup_does_not_mix_different_project_recovery_inputs(tmp_path):
    store = ControlStore(tmp_path / 'control')
    payload = {'base': 'base', 'environment': 'env', 'contract': {'expected_postconditions': ['same engine behavior']},
               'fingerprint': 'same', 'repair_engine': 'v2', 'invocation': {'command': 'collab', 'session_id': 'same-id'}}
    jobs = []
    for name in ('first', 'second'):
        project = str(tmp_path / name)
        subscriber = store.register({'project': project, 'token': name})
        jobs.append(store.submit(subscriber, {**payload, 'project': project}))
    assert jobs[0] != jobs[1]


def test_new_installations_select_the_unified_engine_by_default(repair_request, tmp_path, monkeypatch):
    from auto_agents.repair_control import configure
    monkeypatch.setenv('AUTO_AGENTS_REPAIR_CONTROL_ROOT', str(tmp_path / 'settings'))
    configured = configure(repair_request['config']['source_root'])
    assert configured['repair_engine'] == 'v2'
    assert configure(repair_request['config']['source_root'])['repair_engine'] == 'v2'


def test_new_subscriber_counterexample_revokes_proof_and_resumes_implementation(repair_request):
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        approved = integration.repair_entry(repair_request)
    job = {**repair_request['job'], 'result': approved}
    subscriber = {'id': 'subscriber', 'project': job['payload']['project'], 'payload': {'repair': job['payload']}}
    def failure(identity, *args):
        return {'ok': False, 'snapshot': identity, 'observed': {
            'engine_runtime': {'ok': True}, 'recovery_observation': {
                'ok': False, 'parent_session_id': job['payload']['invocation']['session_id'],
                'child_session_id': 'bound-child', 'current_failure': {'error': 'original preflight failed'}}}}
    verifier.boundary = failure
    with patch.object(integration, 'DockerVerifier', return_value=verifier):
        result = integration.validate_subscriber({**repair_request, 'job': job, 'subscriber': subscriber})
    assert not result['ok']
    root = Path(approved['v2_transaction'])
    state = Store(root).load()
    assert state['phase'] == 'implement' and state['status'] == 'blocked'
    assert state['recovery_failure']['domain'] == 'candidate'
    assert state['plan'] and state['attempts'] == 1
    assert len(list((root / 'counterexamples').glob('*/payload.json'))) == 1
    with pytest.raises(RepairBlocked, match='counterexample'):
        integration.verify_receipt(approved)


def test_publication_uses_verified_receipt_and_only_updates_configured_remote(repair_request):
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        approved = integration.repair_entry(repair_request)
    store = ControlStore(repair_request['config']['root'])
    store.transition(repair_request['job']['id'], 'completed', approved)
    request = {**repair_request, 'job': store.job(repair_request['job']['id'])}
    with pytest.raises(RepairBlocked, match='接管确认'):
        integration.publish(request)
    confirm_recovery(request, approved, verifier)
    with patch('auto_agents.repair_worker.make_runner', side_effect=AssertionError('legacy publication loaded')):
        result = integration.publish(request)
    assert result['ok'] and result['status'] == 'published'
    assert git(repair_request['config']['remote'], 'rev-parse', 'refs/heads/master') == approved['commit']


def confirm_recovery(request, approved, verifier):
    job = {**request['job'], 'result': approved}
    subscriber = {'id': 'publication-owner', 'project': job['payload']['project'], 'payload': {'repair': job['payload']}}
    with patch.object(integration, 'DockerVerifier', return_value=verifier):
        assert integration.validate_subscriber({**request, 'job': job, 'subscriber': subscriber})['ok']
    integration.acknowledge_recovery(job, subscriber, {'session_id': job['payload']['invocation']['session_id']})


def test_publication_divergence_never_reopens_implementation(repair_request, tmp_path):
    driver, verifier = Driver(), Verifier()
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        approved = integration.repair_entry(repair_request)
    store = ControlStore(repair_request['config']['root'])
    store.transition(repair_request['job']['id'], 'completed', approved)
    request = {**repair_request, 'job': store.job(repair_request['job']['id'])}
    confirm_recovery(request, approved, verifier)
    upstream = tmp_path / 'upstream'
    subprocess.run(['git', 'clone', '--quiet', repair_request['config']['remote'], str(upstream)], check=True)
    (upstream / 'independent.txt').write_text('independent upstream work')
    git(upstream, 'add', '.'); git(upstream, 'commit', '-qm', 'upstream advance'); git(upstream, 'push', '-q')
    before = Store(approved['v2_transaction']).load()
    with pytest.raises(RepairBlocked, match='远端'):
        integration.publish(request)
    assert Store(approved['v2_transaction']).load() == before
    assert driver.calls == ['plan', 'implement', 'review']
    assert Path(approved['runtime'], 'source.py').read_text() == 'value = 1\n'


@pytest.mark.parametrize('correction_target', ['engine', 'controller'])
def test_new_invocation_separates_engine_and_controller_corrections(repair_request, monkeypatch, correction_target):
    driver, verifier = Driver(), Verifier()
    original = driver.run
    def no_fix(role, *args, **kwargs):
        if role == 'implement':
            driver.calls.append(role)
            return AgentReply(True, 'No correction found', 'writer')
        return original(role, *args, **kwargs)
    monkeypatch.setattr(driver, 'run', no_fix)
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        failed = integration.repair_entry(repair_request)
    root = Path(failed['v2_transaction'])
    before = Store(root).load()
    assert before['blocker']['code'] == 'no_progress'
    public = ControlStore(repair_request['config']['root'])
    public.transition(repair_request['job']['id'], 'blocked', failed)
    project = repair_request['job']['payload']['project']
    subscriber = public.register({'project': project, 'token': 'new-invocation',
                                 'repair': repair_request['job']['payload']})
    identity = public.submit(subscriber, repair_request['job']['payload'])
    # A new immutable controller checkout is pinned for this invocation.
    origin = Path(repair_request['config']['implementation_root'])
    corrected = origin.with_name('corrected-controller')
    subprocess.run(['git', 'clone', '-q', str(origin), str(corrected)], check=True)
    (corrected / 'source.py').write_text('value = 1\n')
    git(corrected, 'commit', '-qam', 'Correct the exhausted repair')
    if correction_target == 'engine':
        source = Path(repair_request['config']['source_root'])
        git(source, 'fetch', '-q', str(corrected), 'HEAD')
        git(source, 'merge', '--ff-only', 'FETCH_HEAD')
    renewed = {**repair_request, 'config': {**repair_request['config'], 'implementation_root': str(corrected)},
               'job': public.job(identity)}
    calls = len(driver.calls)
    with patch('auto_agents.repair_worker.engine_environment', return_value=('python', 'fixed-env')), \
         patch.object(integration, '_components', side_effect=components(driver, verifier)):
        accepted = integration.repair_entry(renewed)
    if correction_target == 'controller':
        assert not accepted['ok']
        assert len(driver.calls) == calls
        after = Store(root).load()
        assert after['calls'] == before['calls'] and after['attempts'] == before['attempts']
        assert (Path(repair_request['config']['source_root']) / 'source.py').read_text() == 'value = 0\n'
        return
    assert accepted['ok'], accepted
    assert driver.calls[calls:] == ['review']
    after = Store(root).load()
    assert after['attempts'] == before['attempts'] and after['replans'] == before['replans']
    assert (Path(repair_request['config']['source_root']) / 'source.py').read_text() == 'value = 1\n'
    assert (Path(project) / 'keep.txt').read_text() == 'original user data'
    assert integration.verify_receipt(accepted)['boundary']


# Retained contracts may still select this pre-separation node name.
test_new_invocation_imports_controller_correction_into_exhausted_candidate = test_new_invocation_separates_engine_and_controller_corrections
