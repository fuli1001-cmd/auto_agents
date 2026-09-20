"""Trusted offline resume probe, executed inside a disposable Docker container."""
import json
import os
from pathlib import Path
import runpy
import sys
import subprocess


class ReplayEnvironmentUnavailable(RuntimeError):
    pass


def check_environments(request):
    result = []
    program = 'import json,sys;print(json.dumps({"prefix":sys.prefix,"version":list(sys.version_info[:3])}))'
    for environment in request.get('replay_environments', []):
        prefix = Path(environment['prefix'])
        try:
            completed = subprocess.run([str(prefix / 'bin/python'), '-I', '-S', '-c', program],
                                       capture_output=True, text=True, timeout=20)
            observed = json.loads(completed.stdout)
            if completed.returncode or Path(observed['prefix']).resolve() != prefix.resolve():
                raise ValueError('interpreter does not belong to the captured prefix')
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            raise ReplayEnvironmentUnavailable('隔离验证环境中的 Python 无法启动或环境归属不匹配：' + str(prefix)) from error
        result.append({**environment, 'interpreter': observed})
    return result


def emit(payload):
    Path('/result/boundary.json').write_text(json.dumps(payload, ensure_ascii=False))
    print(json.dumps(payload, ensure_ascii=False))


def requires_child_recovery(route):
    sources = [route]
    while sources:
        source = sources.pop()
        if source.get('failed_handoff_id') or source.get('child_session_id'):
            return True
        sources.extend(source[key] for key in ('issue_seed', 'spec_seed', 'fix_disposition')
                       if isinstance(source.get(key), dict))
    return False


def main():
    home = Path(os.environ['HOME'])
    home.mkdir(parents=True, exist_ok=True)
    (home / '.gitconfig').write_text('[user]\n name = auto-agents verification\n email = verification@localhost\n')
    request = json.loads(Path('/result/request.json').read_text())
    environment_inputs = check_environments(request)
    invocation = request.get('invocation', {})
    sys.path.insert(0, '/work/src')
    target = Path(request.get('_replay_project', '/target'))
    case = request.get('repair_case') or {}
    bound_child = requires_child_recovery(invocation.get('engine_route') or {})
    if bound_child and not invocation.get('session_id'):
        raise RuntimeError('bound child recovery requires the retained session entrypoint')
    if case.get('progress_history') and not bound_child:
        from auto_agents.health_watch import replay_health_events
        items = replay_health_events(case['progress_history'], progress_lease_seconds=60)
        emit({'ok': True, 'status': 'health_trajectory_replayed',
              'anomalies': [item.to_dict() for item in items]})
        return
    if invocation.get('session_id'):
        if invocation.get('engine_route'):
            import hashlib
            def digest(value):
                return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            marker = target / '.auto-agents/engine-route-probe.json'
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({'route_digest': digest(invocation['engine_route']),
                                          'engine_route': invocation['engine_route'],
                                          'engine_commit': request['commit']}))
            os.environ['AUTO_AGENTS_REPAIR_ROUTE_PROBE'] = str(marker)
        sys.argv = ['session_replay', '/work', str(target), invocation['session_id'],
                    invocation.get('command', 'collab').replace('provider-resolve', 'fix')]
        # This copy of the harness belongs to the pinned controller, not /work.
        namespace = runpy.run_path('/opt/repair/session_replay.py', run_name='repair_boundary_harness')
        observed = namespace['main']()
        observed['environment_inputs'] = environment_inputs
        emit(observed)
        return
    if invocation.get('run_id'):
        from auto_agents.config import load_run_state, save_run_state
        from auto_agents.orchestrator import Orchestrator
        original = load_run_state(target)
        if original.run_id != invocation['run_id']:
            raise RuntimeError('frozen run identity changed')
        blocked = {t.task_id for t in original.tasks if t.status in ('blocked', 'failed')}
        before = dict(original.active_blocker)
        if not before and not blocked:
            raise RuntimeError('frozen run has no original blocked boundary')
        orchestrator = Orchestrator(target)
        def forbidden(*args, **kwargs):
            raise RuntimeError('offline boundary verification must not invoke providers')
        orchestrator._call_with_failover = forbidden
        state = orchestrator.mark_self_repair_applied(request['commit'])
        changed = orchestrator._resume_blocked_run(state)
        save_run_state(target, state)
        after = state.active_blocker or {}
        remains = {t.task_id for t in state.tasks if t.status in ('blocked', 'failed')}
        same = bool(after and any(after.get(k) and after.get(k) == before.get(k)
                                  for k in ('fingerprint', 'category')))
        ok = not same and (bool(blocked - remains) if blocked else bool(changed))
        emit({'ok': ok, 'run_id': state.run_id, 'status': state.status,
              'remaining_blocked': sorted(remains), 'same_blocker': same})
        return
    # Explicit engine-only repairs have no project session to resume. Their
    # requested behavior is checked by mandatory tests and independent review.
    if invocation.get('engine_route'):
        emit({'ok': True, 'status': 'engine_only', 'resume_required': False})
        return
    raise RuntimeError('repair lacks a supported original resume boundary')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit({'ok': False, 'error': str(error), 'error_type': type(error).__name__,
              'infrastructure': isinstance(error, ReplayEnvironmentUnavailable)})
        raise SystemExit(1)
