"""Offline proof of retained diagnostic submission over the real control socket.

Mounted with the trusted boundary driver. Only worker scheduling is fenced;
registration, lock transfer, scope admission, RPC and job persistence are real.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace


def retained_diagnosis(target, request):
    from auto_agents.root_cause import RootCauseDiagnosis

    target = Path(target).resolve()
    project = Path(request.get('project') or target).resolve()
    supplied = request.get('diagnosis') or {}
    refs = supplied.get('final', {}).get('necessity', {}).get('evidence_refs', [])
    certificates = []
    for ref in refs:
        if isinstance(ref, str) and ref.startswith('target:'):
            ref = {'origin': 'target', 'path': ref[len('target:'):]}
        if not isinstance(ref, dict) or ref.get('origin') != 'target':
            continue
        name = str(ref.get('path', '')).partition('#')[0]
        if not name.startswith('.auto-agents/state/root_cause_certificates/'):
            continue
        relative = Path(name)
        path = target / relative
        if '..' in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(target):
            raise ValueError('retained diagnosis certificate escapes the frozen project')
        document = json.loads(path.read_text())
        if document.get('certificate_key') != path.stem:
            raise ValueError('retained diagnosis certificate identity mismatch')
        certificates.append(document['diagnosis'])
    if len(certificates) > 1:
        raise ValueError('retained diagnostic submission has ambiguous certificates')
    diagnosis = RootCauseDiagnosis.from_dict(certificates[0] if certificates else supplied)
    if not diagnosis.repair_approved or diagnosis.final.necessity.get('decision') != 'required':
        raise ValueError('retained diagnosis does not authorize this repair submission')
    # Keep the report unchanged. The Docker scene is mounted at its original
    # project identity; only copying resolves this absolute path in frozen input.
    relative = Path(diagnosis.evidence_path).relative_to(project)
    evidence = target / relative
    if '..' in relative.parts or evidence.is_symlink() or not evidence.resolve().is_relative_to(target):
        raise ValueError('retained diagnostic evidence escapes the frozen project')
    if not evidence.is_file():
        raise FileNotFoundError('retained diagnosis evidence is absent: ' + str(relative))
    return diagnosis, relative


def copy_submission_evidence(source, destination, request):
    """Retain only the cited document before sealing a diagnostic-repair scene."""
    source, destination = Path(source), Path(destination)
    state = source / '.auto-agents/state/run_state.json'
    if (not state.is_file() or json.loads(state.read_text()).get('active_blocker', {}).get('category')
            != 'diagnostic_evidence_reference_binding_gap'):
        return
    _, relative = retained_diagnosis(source, request)
    target = destination / relative
    if target.is_symlink() or not target.resolve().is_relative_to(destination.resolve()):
        raise ValueError('diagnostic submission snapshot escapes its destination')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / relative, target)


def observe_submission(orchestrator, original, diagnosis, request, runtime, output):
    from auto_agents.repair_client import submit_and_wait
    from auto_agents.repair_control import Supervisor, digest, rpc, socket_path, start_ticks
    from auto_agents.repair_v2.scope import ScopeGuard
    from auto_agents.run_lock import ProjectRunLock
    from auto_agents.self_repair import SelfRepairDecision

    target = orchestrator.project_root
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    workflow = original.resume_context.get('workflow_id', '')
    if not workflow or request.get('invocation', {}).get('run_id') != original.run_id:
        raise ValueError('submission is not bound to the retained run and workflow')
    report = diagnosis.to_dict()
    event_path = target / '.auto-agents/runs' / original.run_id / 'events.jsonl'
    offset = event_path.stat().st_size if event_path.exists() else 0
    write_event = orchestrator.reporter.event
    observed = []

    class SubmissionObserved(BaseException):
        pass

    def observe(kind, data, **options):
        write_event(kind, data, **options)
        if kind == 'repair.submitted':
            observed.append(data)
            raise SubmissionObserved()

    previous = {key: getattr(orchestrator, key, None)
                for key in ('_repair_registration', '_invocation_context')}
    with tempfile.TemporaryDirectory(prefix='diagnostic-rpc-') as sockets:
        config = {'root': str(output / 'submission-control'), 'socket_dir': sockets,
                  'identity': digest(str(output))[:24], 'source_root': runtime['runtime_root'],
                  'publish': False, 'repair_engine': 'v2'}
        supervisor = Supervisor(config)
        # This is a boundary probe, not another paid repair implementation.
        supervisor.tick = lambda: None
        errors = []

        def serve():
            try:
                supervisor.serve()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not socket_path(config).exists():
                if errors or time.monotonic() >= deadline:
                    raise RuntimeError('isolated submission supervisor failed to start')
                time.sleep(0.01)
            with ProjectRunLock(target) as lock:
                registration = rpc(config, {'op': 'register', 'payload': {
                    'project': str(target), 'token': lock.run_token, 'pid': os.getpid(),
                    'ticks': start_ticks(os.getpid()), 'command': ['run', '--project', str(target)]}},
                    [lock.fileno])
                orchestrator._repair_registration = {'config': config, 'subscriber': registration['subscriber']}
                orchestrator._invocation_context = {**request.get('invocation', {}),
                                                   'run_id': original.run_id, 'workflow_id': workflow}
                orchestrator.reporter.event = observe
                blocker = original.active_blocker
                args = SimpleNamespace(command='run', project=str(target),
                    spec_file=original.resume_context.get('spec_file'),
                    auto_approve=original.resume_context.get('auto_approve', False),
                    provider=request.get('provider'), autonomy=request.get('autonomy'))
                try:
                    submit_and_wait(target, orchestrator, RuntimeError(original.last_error),
                        SelfRepairDecision(True, category=blocker['category'],
                            reason=blocker.get('reason', ''), fingerprint=blocker['fingerprint']),
                        args, lock, diagnosis=diagnosis)
                except SubmissionObserved:
                    pass
                if len(observed) != 1:
                    raise RuntimeError('retained diagnosis did not receive a submission acknowledgment')
                event = observed[0]
                job = supervisor.store.job(event['job_id'])
                subscribers = supervisor.store.subscriptions(job['id'])
                payload = job['payload']
                receipt = payload.get('scope_receipt')
                guard = ScopeGuard(output / 'submission-revalidation', payload, target, runtime['runtime_root'])
                if (job['state'] != 'queued' or job['result']
                        or not any(row['id'] == event['subscriber_id'] and row['state'] == 'waiting'
                                   for row in subscribers)
                        or payload.get('diagnosis') != report
                        or event.get('diagnosis_digest') != digest(report)
                        or event.get('scope_receipt_digest') != digest(receipt)
                        or payload['invocation'].get('run_id') != original.run_id
                        or payload['invocation'].get('workflow_id') != workflow
                        or event.get('engine_commit') != runtime['commit']
                        or not guard.import_receipt(receipt)):
                    raise RuntimeError('submission acknowledgment lacks a matching durable job and scope receipt')
                events = [json.loads(line) for line in event_path.read_bytes()[offset:].splitlines()]
                persisted = [row for row in events if row.get('type') == 'repair.submitted']
                if len(persisted) != 1 or persisted[0].get('data') != event or not persisted[0].get('event_id'):
                    raise RuntimeError('submission acknowledgment was not persisted')
                result = {**event, 'accepted': True, 'job_state': job['state'],
                          'job_generation': job['generation'], 'request_digest': digest(payload),
                          'event': persisted[0], 'event_ref': str(event_path.relative_to(target))}
                (output / 'submission.json').write_text(json.dumps(result, ensure_ascii=False))
                return result
        finally:
            orchestrator.reporter.event = write_event
            for key, value in previous.items():
                if value is None:
                    orchestrator.__dict__.pop(key, None)
                else:
                    setattr(orchestrator, key, value)
            supervisor.halt = True
            thread.join(timeout=6)
            for registration in supervisor.registrations.values():
                for fd in registration['fds']:
                    os.close(fd)
            if thread.is_alive() or errors:
                raise RuntimeError('isolated submission supervisor did not shut down cleanly')
