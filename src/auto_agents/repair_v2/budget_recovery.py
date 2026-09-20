"""Conservative migration of legacy restart resets; never grant fresh calls."""
import json
from pathlib import Path
import re

from .store import atomic_json, digest
from .types import RepairBlocked


FIELDS = ('current_attempt', 'attempt_epoch', 'attempts_since_progress', 'max_attempts', 'hard_ceiling')
AUTHORITY = ('session_id', 'workflow_id', 'goal', 'authorization_policy', 'goal_execution_environment')


def session_path(project, identity):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', identity):
        raise RepairBlocked('budget_history_conflict', '预算记录中的会话编号无效')
    return Path(project) / '.auto-agents/state/sessions' / identity / 'session_state.json'


def anchors(project, invocation):
    ids = {invocation['session_id']} if invocation.get('session_id') else set()
    pending = [invocation.get('engine_route') or {}]
    visited = set()
    while pending:
        row = pending.pop()
        if row.get('child_session_id'): ids.add(str(row['child_session_id']))
        for key in ('issue_seed', 'spec_seed', 'fix_disposition'):
            if isinstance(row.get(key), dict): pending.append(row[key])
        for key in ('failed_handoff_id', 'resume_handoff_id', 'original_handoff_id'):
            handoff = row.get(key)
            if not handoff or handoff in visited: continue
            visited.add(handoff)
            if not re.fullmatch(r'[A-Za-z0-9_-]+', handoff):
                raise RepairBlocked('budget_history_conflict', '预算交接引用无效')
            path = Path(project) / '.auto-agents/state/handoffs' / (handoff + '.json')
            saved = json.loads(path.read_text())
            child = saved.get('child') or {}
            if child.get('kind') == 'fix' and child.get('native_id'): ids.add(child['native_id'])
            pending.append(saved.get('payload') or {})
    result = {}
    for identity in sorted(ids):
        state = json.loads(session_path(project, identity).read_text())
        result[identity] = {key: state.get(key) for key in (*FIELDS, *AUTHORITY, 'execution_log')}
    return result


def _control_only(row, route):
    action, result = row.get('action'), row.get('result', '')
    if action == 'attempt_epoch_started':
        return result in {'failed session resumed', 'process-level session resume'}
    if action == 'provider_continuation_invalidated':
        return result.endswith('process-level session resume uses the durable transcript')
    prefix = 'auto_agents engine self-repair required for explicitly bound engine route: '
    if action == 'error' and result.startswith(prefix):
        try: return json.loads(result[len(prefix):]) == route
        except ValueError: return False
    return action == 'budget_history_reconciled'


def reconcile(project, original, invocation):
    """Caller holds the project lock, or owns a disposable verification copy."""
    updates = []
    for identity, anchor in original.items():
        path = session_path(project, identity)
        state = json.loads(path.read_text())
        if any(state.get(key) != anchor.get(key) for key in AUTHORITY):
            raise RepairBlocked('budget_history_conflict', '历史预算的目标或会话归属已变化：' + identity)
        history, current = anchor.get('execution_log') or [], state.get('execution_log') or []
        if current[:len(history)] != history:
            raise RepairBlocked('budget_history_conflict', '历史调用记录不完整，不能推定可用次数：' + identity)
        before = {key: state.get(key) for key in FIELDS}
        # These constraints apply even when no legacy counter reset needs
        # repair. An idempotent resume is not authority to change allowances
        # or to move the attempt epoch backwards.
        if any(state.get(key) != anchor.get(key) for key in ('max_attempts', 'hard_ceiling')):
            raise RepairBlocked('budget_history_conflict', '预算上限与原记录不一致，已停止自动恢复：' + identity)
        if int(state.get('attempt_epoch') or 0) < int(anchor.get('attempt_epoch') or 0):
            raise RepairBlocked('budget_history_conflict', '调用轮次早于原记录，已停止自动恢复：' + identity)
        deficits = any(int(state.get(key) or 0) < int(anchor.get(key) or 0)
                       for key in ('current_attempt', 'attempts_since_progress'))
        if not deficits: continue
        if not all(_control_only(row, invocation.get('engine_route', {})) for row in current[len(history):]):
            raise RepairBlocked('budget_history_conflict', '旧预算存在无法解释的变化，已停止自动恢复：' + identity)
        for key in ('current_attempt', 'attempts_since_progress'):
            state[key] = max(int(state.get(key) or 0), int(anchor.get(key) or 0))
        state.setdefault('execution_log', []).append({'action': 'budget_history_reconciled',
            'anchor': digest(anchor), 'before': before, 'after': {key: state.get(key) for key in FIELDS}})
        updates.append((path, state))
    # Validate every owner before writing any record. An interrupted migration
    # is safe to repeat: committed records already have monotonic counters.
    for path, state in updates: atomic_json(path, state)
    return [str(path) for path, _ in updates]
