"""Authenticated transport for foreground choices; workers cannot approve themselves."""
import json
from pathlib import Path

from .scope_decisions import Decisions
from .repair_v2.scope import context


def dispatch(supervisor, request):
    from .repair_control import alive
    row = next((s for s in supervisor.store.subscriptions() if s['id'] == request.get('subscriber')), None)
    if row is None or row['state'] == 'cancelled':
        raise RuntimeError('decision has no active workflow owner')
    op = request['op']
    owner = supervisor.registrations.get(row['id'], {}).get('payload', row['payload'])
    foreground = row['payload'].get('foreground', {})
    expected = owner if op in ('request-decision', 'decision-status') else foreground
    if (request.get('_peer_pid') != expected.get('pid')
            or not alive(expected.get('pid', 0), expected.get('ticks', ''))):
        raise RuntimeError('only the foreground user can answer a goal decision')
    decisions = Decisions(row['project'])
    decision = decisions.read(request['decision'])
    if decision['context']['owner']['project'] != str(Path(row['project']).resolve()):
        raise RuntimeError('decision belongs to another workflow')
    actual = context(row['project'], row['payload']['repair'])
    if actual['owner'] != decision['context']['owner']:
        raise RuntimeError('user decision no longer belongs to this goal')
    pending = row['payload'].get('pending_decision', {})
    if op == 'request-decision':
        if pending and pending['id'] != decision['id'] and not pending.get('detached'):
            raise RuntimeError('another user decision is already pending')
        job = supervisor.store.job(row['job'])
        pending = {'id': decision['id'], 'source': 'session', 'return_state': row['state'],
                   'return_job_state': job['state']}
        payload = {**row['payload'], 'pending_decision': pending}
        with supervisor.store.connect() as db:
            db.execute("UPDATE subscribers SET state='waiting_user',payload=? WHERE id=?",
                       (json.dumps(payload), row['id']))
        return {'ok': True}
    if pending.get('id') != decision['id']:
        raise RuntimeError('stale user decision')
    if op == 'decision-status':
        return {'ok': True, 'detached': bool(pending.get('detached')), 'status': decision['status']}
    if op == 'detach-decision':
        pending['detached'] = True
        with supervisor.store.connect() as db:
            db.execute('UPDATE subscribers SET payload=? WHERE id=?',
                       (json.dumps({**row['payload'], 'pending_decision': pending}), row['id']))
        return {'ok': True}
    answered = decisions.answer(decision['id'], request['decision_version'], request['answer'],
                                user_text=request.get('user_text', ''), goal_version=actual['goal_version'])
    state = pending['return_state'] if pending['source'] == 'session' else (
        'registered' if answered['answer'] in ('approve', 'comment') else 'blocked')
    payload = {**row['payload'], 'pending_decision': {**pending, 'answered': True}}
    with supervisor.store.connect() as db:
        db.execute('UPDATE subscribers SET state=?,payload=? WHERE id=?', (state, json.dumps(payload), row['id']))
    if pending['source'] == 'repair':
        job = supervisor.store.job(row['job'])
        supervisor.store.transition(job['id'], 'blocked', {**job['result'], 'status': 'scope_return',
            'error': '用户已决定目标范围，修复候选保留；回到原任务继续。'}, generation=job['generation'])
    return {'ok': True, 'resume_original': pending['source'] == 'repair' and state == 'registered'}
