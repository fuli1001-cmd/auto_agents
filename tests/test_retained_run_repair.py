"""Explicit source upgrades resume the original repair before model diagnosis."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_agents import cli, repair_client
from auto_agents.config import load_run_state, save_run_state
from auto_agents.repair_control import Store, Supervisor, VERSION, start_ticks
from auto_agents.repair_v2.diagnostic_replay import observe_submission
from auto_agents.repair_v2.scope import context, same_context
from auto_agents.repair_v2.store import Store as TransactionStore, atomic_json
from auto_agents.repair_v2.transaction import transaction_root
from auto_agents.repair_v2.workspace import git
from auto_agents.run_lock import ProjectRunLock
from test_diagnostic_recovery_boundary import retained_submission_scene


@contextmanager
def stopped_repair(tmp_path, monkeypatch):
    monkeypatch.delenv('AUTO_AGENTS_REPAIR_CONTROL_DISABLED', raising=False)
    with retained_submission_scene() as (root, orch, original, _, request, runtime, _, diagnosis):
        workflow_file = '.auto-agents/state/workflows/' + original.resume_context['workflow_id'] + '/workflow.json'
        diagnosis.final.necessity['evidence_refs'].append({'origin': 'target', 'path': workflow_file})
        request['diagnosis'] = diagnosis.to_dict()
        proof = tmp_path / 'previous'
        submission = observe_submission(orch, original, diagnosis, request, runtime, proof)
        public = Store(proof / 'submission-control')
        old = public.job(submission['job_id'])
        payload = old['payload']
        # The real supervisor stamps this field before accepting submission.
        payload['repair_engine'] = 'v2'
        payload['error'] = 'original pre-submit evidence resolution exception'
        payload['symptom_key'] = repair_client.symptom_key(payload['error'], root)
        with public.connect() as db:
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload), old['id']))
        transaction = transaction_root({'root': str(public.root)}, payload)
        atomic_json(transaction / 'original-payload.json', payload)
        from auto_agents.root_cause import RootCauseCoordinator
        RootCauseCoordinator._copy_diagnostic_tree(root, transaction / 'target-evidence')
        usage = {'status': 'blocked', 'attempts': 4, 'calls': 9, 'replans': 1,
                 'blocker': {'code': 'no_progress'}}
        TransactionStore(transaction).save(usage)
        public.transition(old['id'], 'blocked', {'ok': False, 'engine': 'v2', 'status': 'v2_blocked',
                                               'v2_transaction': str(transaction)})
        source = tmp_path / 'corrected-engine'
        source.mkdir()
        git(source, 'init', '-q')
        (source / 'fix.py').write_text('corrected = True\n')
        git(source, 'add', '.')
        git(source, 'commit', '-qm', 'operator correction')
        monkeypatch.setattr('auto_agents.self_repair.auto_agents_repo_root', lambda: source)
        config = {'root': str(public.root), 'source_root': str(source), 'repair_engine': 'v2'}
        supervisor = Supervisor(config)
        with ProjectRunLock(root) as lock:
            fds = [os.dup(lock.fileno)]
            registration = supervisor.register({'payload': {'project': str(root), 'token': lock.run_token,
                'pid': os.getpid(), 'ticks': start_ticks(os.getpid()), 'command': 'run'}}, fds)
            orch._repair_registration = {'config': config, 'subscriber': registration['subscriber']}
            lock.repair_registration = orch._repair_registration
            def rpc(config, message, fds=()):
                return supervisor.dispatch({**message, 'version': VERSION, '_peer_pid': os.getpid()}, list(fds))
            monkeypatch.setattr(repair_client, 'rpc', rpc)
            try:
                yield root, orch, original, source, payload, supervisor, lock, transaction, usage
            finally:
                for record in supervisor.registrations.values():
                    for fd in record['fds']:
                        os.close(fd)


def test_normal_run_reuses_stopped_contract_and_budget_after_source_upgrade(tmp_path, monkeypatch):
    with stopped_repair(tmp_path, monkeypatch) as (root, orch, original, source, payload, supervisor, lock, transaction, usage):
        state = load_run_state(root)
        state.active_blocker['self_repair_triage'] = {'decision': {'eligible': False},
            'reason': 'source is already corrected; no new implementation is justified'}
        save_run_state(root, state)
        workflow_path = root / '.auto-agents/state/workflows' / state.resume_context['workflow_id'] / 'workflow.json'
        workflow = json.loads(workflow_path.read_text())
        atomic_json(workflow_path, {**workflow, 'updated_at': '2026-09-22T14:31:22+00:00'})
        monkeypatch.setattr(cli, 'adjudicate_auto_agents_error',
                            lambda *a, **k: pytest.fail('the same failure was diagnosed again'))
        result = cli._triage_terminal_run_error(root, orch, RuntimeError(original.last_error))
        assert result.source == 'retained_repair_contract'
        assert result.decision.eligible and result.root_cause.to_dict() == payload['diagnosis']
        assert load_run_state(root).active_blocker == state.active_blocker
        # Observe the real supervisor acknowledgment before any worker runs.
        event = orch.reporter.event
        submitted = []
        class Submitted(BaseException):
            pass
        def observe(kind, data, **options):
            event(kind, data, **options)
            if kind == 'repair.submitted':
                submitted.append(data)
                raise Submitted()
        monkeypatch.setattr(orch.reporter, 'event', observe)
        args = SimpleNamespace(command='run', project=str(root), spec_file=state.resume_context['spec_file'],
                               auto_approve=True, provider='codex', autonomy='max')
        with pytest.raises(Submitted):
            cli._auto_repair_auto_agents_and_resume(root, orch, RuntimeError(original.last_error),
                result.decision, args, lock, diagnosis=result.root_cause)
        assert len(submitted) == 1
        renewed = supervisor.store.job(submitted[0]['job_id'])
        assert renewed['state'] == 'queued'
        assert renewed['payload']['base'] == git(source, 'rev-parse', 'HEAD')
        assert renewed['payload']['scope_receipt']['proposal'] == payload['scope_receipt']['proposal']
        assert renewed['payload']['scope_receipt']['witnesses'] != payload['scope_receipt']['witnesses']
        assert renewed['payload']['error'] == payload['error']
        assert renewed['payload']['symptom_key'] == payload['symptom_key']
        assert renewed['payload']['diagnosis'] == payload['diagnosis']
        assert renewed['payload']['invocation'] == payload['invocation']
        assert transaction_root(supervisor.config, renewed['payload']) == transaction
        after = TransactionStore(transaction).load()
        assert {key: after[key] for key in ('attempts', 'calls', 'replans')} == {key: usage[key] for key in ('attempts', 'calls', 'replans')}
        assert load_run_state(root).status == 'blocked'  # Acceptance has not happened yet.


@pytest.mark.parametrize('change', ['run', 'fingerprint', 'goal', 'witness', 'approval', 'workflow', 'workflow_document', 'dirty', 'same_source', 'unrelated_error'])
def test_retained_repair_requires_current_owner_witnesses_and_committed_upgrade(tmp_path, monkeypatch, change):
    with stopped_repair(tmp_path, monkeypatch) as (root, orch, original, source, payload, supervisor, lock, transaction, usage):
        state = load_run_state(root)
        if change == 'run': state.run_id = 'another-run'
        elif change == 'fingerprint': state.active_blocker['fingerprint'] = 'new-failure'
        elif change == 'approval': state.pending_approval = 'implement'
        elif change == 'workflow':
            payload['invocation']['workflow_id'] = 'foreign-workflow'
            with supervisor.store.connect() as db:
                db.execute('UPDATE jobs SET payload=?', (json.dumps(payload),))
        elif change == 'workflow_document':
            path = root / '.auto-agents/state/workflows' / state.resume_context['workflow_id'] / 'workflow.json'
            document = json.loads(path.read_text())
            document['active_frame'] = {'kind': 'run', 'native_id': 'other-run'}
            atomic_json(path, document)
        elif change == 'goal': Path(state.resume_context['spec_file']).write_text('Different requested work')
        elif change == 'witness':
            ref = payload['scope_receipt']['proposal']['evidence_refs'][0]['path']
            (root / ref).write_text('{}')
        elif change == 'dirty': (source / 'fix.py').write_text('uncommitted = True\n')
        elif change == 'same_source':
            payload['base'] = git(source, 'rev-parse', 'HEAD')
            with supervisor.store.connect() as db:
                db.execute('UPDATE jobs SET payload=?', (json.dumps(payload),))
        save_run_state(root, state)
        error = RuntimeError('unrelated fresh exception' if change == 'unrelated_error' else original.last_error)
        assert repair_client.retained_run_contract(orch, root, error) is None
        assert TransactionStore(transaction).load()['attempts'] == 4


def test_triage_metadata_does_not_change_incident_but_checkpoint_changes_do(tmp_path, monkeypatch):
    with stopped_repair(tmp_path, monkeypatch) as (root, _, _, _, payload, _, _, _, _):
        before = context(root, payload)
        state = load_run_state(root)
        state.active_blocker['self_repair_triage'] = {'diagnosis_id': 'new-observation', 'eligible': False}
        save_run_state(root, state)
        assert same_context(before, context(root, payload))
        assert context(root, payload)['incident']['identity'] == before['incident']['identity']
        state.active_blocker['checkpoint'] = {'head': 'changed-task-candidate'}
        save_run_state(root, state)
        assert not same_context(before, context(root, payload))


def test_retained_lookup_requires_registered_owner(tmp_path):
    supervisor = Supervisor({'root': str(tmp_path / 'control')})
    with pytest.raises(RuntimeError, match='registered workflow owner'):
        supervisor.dispatch({'version': VERSION, 'op': 'lookup-retained-run-repair',
                             'subscriber': 'foreign', '_peer_pid': os.getpid()}, [])
