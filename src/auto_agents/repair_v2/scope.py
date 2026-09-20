"""One goal-bound necessity receipt, shared by routing, planning and review."""
import hashlib
import json
from pathlib import Path
import re

from .store import Store, atomic_json, digest
from .types import RepairBlocked

POLICY = 'goal-scope-v1'
MARKER = re.compile(r'^REPAIR_SCOPE v1:\s*(\{.*\})\s*$', re.MULTILINE)
INSTRUCTION = (
    'Judge necessity while doing this existing diagnosis, not in a separate review. '
    'Ignore unrelated old defects and improvement suggestions entirely; do not list or retain them. '
    'Bind every required change to original_goal and the observed blocked step. '
    'Return REPAIR_SCOPE v1: {"decision":"required|needs_user|insufficient|skip",'
    '"blocked_step":"...","consequence":"why this prevents the original goal",'
    '"evidence_refs":["repository-relative path"],"recovery_check":"observable recovery",'
    '"question":"plain user-facing explanation, only for needs_user",'
    '"suggestion":"specific goal change, only for needs_user"} on one line. '
    'Use needs_user only for a new user goal/product requirement or resuming another task '
    'the user stopped and has not authorized resuming. Technical repair choices are internal. '
    'Explain the user-visible problem and exact suggestion in the user\'s language, in one or two short sentences; '
    'do not put transaction IDs, hashes, stack traces or internal routing terms in the question. '
    'Use required for necessary dependencies and regressions introduced by this patch, '
    'never turn them into independent maintenance. An unchanged valid necessity receipt '
    'must be reused. Do not fabricate execution observations or user authorization.'
)


class ScopeDecisionRequired(RepairBlocked):
    def __init__(self, proposal, context):
        super().__init__('scope_decision', proposal['question'])
        self.proposal, self.context = proposal, context


def read_json(root, relative):
    root, path = Path(root).resolve(), Path(root) / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise RepairBlocked('scope_evidence', '范围依据引用了当前工作流以外的文件。')
    return json.loads(path.read_text())


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value):
        raise RepairBlocked('scope_identity', '无法确认当前任务的身份，已保留执行现场。')
    return value


def failure_condition(value):
    if isinstance(value, dict):
        return {k: failure_condition(v) for k, v in value.items() if k not in {
            'timestamp', 'at', 'attempt', 'created_at', 'updated_at', 'pid', 'start_ticks'}}
    if isinstance(value, list):
        return [failure_condition(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'/tmp/[^\s]+|0x[0-9a-f]+', '<ephemeral>', value)
    return value


def context(target, payload):
    """Read goal/ownership from durable state, never the model's issue summary."""
    invocation = payload.get('invocation', {})
    session = invocation.get('session_id')
    project = str(Path(payload['project']).resolve())
    workflow = invocation.get('workflow_id') or ''
    prefix = '.auto-agents/state/'
    if session:
        state = read_json(target, prefix + f'sessions/{safe_id(session)}/session_state.json')
        workflow = state.get('workflow_id') or workflow
        if workflow:
            try:
                tree = read_json(target, prefix + f'workflows/{safe_id(workflow)}/workflow.json')
            except FileNotFoundError:
                tree = None
            if tree and tree.get('root', {}).get('kind') in ('collab', 'fix', 'provider_resolve'):
                session = safe_id(tree['root']['native_id'])
                state = read_json(target, prefix + f'sessions/{session}/session_state.json')
            elif tree and tree.get('root', {}).get('kind') == 'run':
                return context(target, {**payload, 'invocation': {
                    **invocation, 'session_id': '', 'run_id': safe_id(tree['root']['native_id']), 'workflow_id': workflow}})
        goal = state.get('goal', '')
        subject = 'session:' + safe_id(session)
    else:
        state = read_json(target, prefix + 'run_state.json')
        run = safe_id(invocation.get('run_id') or state.get('run_id'))
        if state.get('run_id') != run:
            raise RepairBlocked('scope_identity', '保存的执行记录属于另一项任务。')
        # Accepted task contracts are the durable goal for run entrypoints.
        tasks = [{**{k: task.get(k) for k in ('title', 'description', 'acceptance')},
                  'task_id': task.get('task_id') or task.get('id')}
                 for task in state.get('tasks', [])]
        spec = str(state.get('resume_context', {}).get('spec_file') or 'spec.md')
        relative = Path(spec)
        if relative.is_absolute():
            try: relative = relative.relative_to(project)
            except ValueError: relative = Path('__unavailable_spec__')
        path = Path(target) / relative
        spec_goal = ''
        if (relative.suffix in ('.md', '.txt', '.rst') and path.is_file() and not path.is_symlink()
                and path.resolve().is_relative_to(Path(target).resolve()) and path.stat().st_size <= 4 * 1024 * 1024):
            spec_goal = path.read_text()
        goal = state.get('goal') or spec_goal or (json.dumps(tasks, ensure_ascii=False) if tasks else '')
        subject = 'run:' + run
        from ..scope_decisions import Decisions
        goal = Decisions(target).original_goal({'project': project, 'subject': subject, 'workflow_id': workflow}) or goal
    if not goal:
        raise RepairBlocked('scope_goal_missing', '无法从原任务记录确认目标，暂不扩大修复范围。')
    route = invocation.get('engine_route') or {}
    from ..execution_binding import route_sources
    sources = list(route_sources(route))
    handoff = next((source.get('failed_handoff_id') or source.get('resume_handoff_id')
                    for source in sources if source.get('failed_handoff_id') or source.get('resume_handoff_id')),
                   state.get('active_handoff_id', ''))
    visited = set()
    while handoff and handoff not in visited:
        visited.add(handoff)
        transfer = read_json(target, prefix + f'handoffs/{safe_id(handoff)}.json')
        if workflow and transfer.get('workflow_id') != workflow:
            raise RepairBlocked('scope_identity', '修复引用的子任务不属于当前目标。')
        nested = transfer.get('payload', {}).get('resume_handoff_id')
        if nested:
            handoff = nested
            continue
        child = transfer.get('child') or {}
        if child.get('kind') in ('fix', 'collab', 'provider_resolve'):
            state = read_json(target, prefix + f"sessions/{safe_id(child['native_id'])}/session_state.json")
        break
    observation = {k: state.get(k) for k in ('last_error', 'active_blocker') if state.get(k)}
    diagnostic = state.get('verification_diagnostics') or {}
    if diagnostic.get('error') or diagnostic.get('failure_kind') or diagnostic.get('ok') is False:
        observation['verification_diagnostics'] = diagnostic
    failures = [row for row in state.get('execution_log', []) if any(
        part in str(row.get('action', '')).lower() for part in ('fail', 'error', 'blocked', 'preflight'))
        and 'auto_agents engine self-repair required' not in str(row.get('result', ''))]
    if failures:
        observation['failure'] = failures[-1]
    if not route and payload.get('error'):
        observation['exception'] = payload['error']
    observation = failure_condition(observation)
    owner = {'project': project, 'subject': subject, 'workflow_id': workflow}
    from ..scope_decisions import Decisions
    approvals = Decisions(target).approvals(owner)
    operation = handoff or state.get('active_execution_incident_id') or state.get('session_id') or subject
    return {'policy': POLICY, 'owner': owner, 'original_goal': goal,
            'approved_changes': approvals, 'goal_version': digest([goal, approvals]),
            'blocker': {'operation': operation, 'boundary': payload.get('boundary', {}).get('kind', 'engine_route'),
                        'condition': digest(failure_condition(observation))},
            'observation': observation}


def proposal(text):
    matches = MARKER.findall(text)
    if len(matches) != 1:
        return None
    try:
        result = json.loads(matches[0])
        return result if isinstance(result, dict) else None
    except ValueError:
        return None


def witnesses(refs, target, source):
    if not isinstance(refs, list) or not refs:
        raise RepairBlocked('scope_evidence', '缺少能检查的失败依据，请补充当前阻塞的证据。')
    result = []
    for ref in refs:
        if not isinstance(ref, str):
            raise RepairBlocked('scope_evidence', '范围证据路径无效。')
        name = re.sub(r':\d+(?::\d+)?$', '', ref)
        name = name.removeprefix('/repair-evidence/original/')
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts
                or relative.name == '.env' or name.startswith('.auto-agents/operator/')):
            raise RepairBlocked('scope_evidence', '范围证据必须来自当前任务或引擎的可检查文件。')
        found = False
        for kind, root in [('target', target), ('source', source)]:
            root = Path(root).resolve()
            path = root / relative
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise RepairBlocked('scope_evidence', '请引用更精确的失败依据。')
                result.append({'kind': kind, 'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                found = True
                break
        if not found:
            raise RepairBlocked('scope_evidence', f'无法读取当前阻塞的依据：{name}')
    return result


class ScopeGuard:
    def __init__(self, root, payload, target, source):
        self.store = Store(root)
        self.payload, self.target, self.source = payload, Path(target), Path(source)
        self.context = context(target, payload)

    def current(self):
        path = self.store.root / 'scope.json'
        if not path.exists():
            return None
        saved = read_json(self.store.root, 'scope.json')
        record = self.store.read(saved)
        if record.get('context') != self.context:
            return None
        try:
            current = witnesses(record['proposal']['evidence_refs'], self.target, self.source)
        except (RepairBlocked, OSError):
            return None
        return saved if current == record['witnesses'] else None

    def import_receipt(self, record):
        if not isinstance(record, dict) or record.get('context') != self.context:
            return None
        try:
            if (record.get('policy') != POLICY or record['proposal'].get('decision') != 'required'
                    or witnesses(record['proposal']['evidence_refs'], self.target, self.source) != record['witnesses']):
                return None
        except (KeyError, TypeError, OSError, RepairBlocked):
            return None
        ref = self.store.artifact('scope', record)
        atomic_json(self.store.root / 'scope.json', ref)
        return ref

    def admit(self, value):
        existing = self.current()
        if existing and (not isinstance(value, dict) or value.get('decision') not in ('skip', 'needs_user')):
            return existing
        if not isinstance(value, dict):
            raise RepairBlocked('scope_missing', '尚未说明这项修复如何阻碍原目标，请补充已有诊断。')
        decision = value.get('decision')
        if decision == 'skip':
            # Deliberately no issue, backlog, or skip receipt.
            raise RepairBlocked('scope_skipped', '继续原任务。')
        if decision == 'needs_user':
            if not all(isinstance(value.get(k), str) and value[k].strip() for k in ('question', 'suggestion')):
                raise RepairBlocked('scope_missing', '需要用清楚的语言说明建议改变的目标范围。')
            raise ScopeDecisionRequired(value, self.context)
        if decision != 'required' or not all(isinstance(value.get(k), str) and value[k].strip()
                                             for k in ('blocked_step', 'consequence', 'recovery_check')):
            raise RepairBlocked('scope_missing', '当前证据不足以开始实施，请继续定位原任务的阻塞。')
        if not self.context['observation']:
            raise RepairBlocked('scope_evidence', '没有当前任务的失败记录，不能凭维护建议启动自修复。')
        evidence = witnesses(value.get('evidence_refs'), self.target, self.source)
        reference = self.store.artifact('scope', {'policy': POLICY, 'context': self.context,
                                                'proposal': value, 'witnesses': evidence})
        atomic_json(self.store.root / 'scope.json', reference)
        return reference


def changes(snapshot, base, parents=()):
    """Stable hunk IDs force review coverage of the actual delta, including metadata."""
    from .workspace import git
    def hunks(diff):
        result = {}
        for chunk in filter(str.strip, re.split(r'(?=^diff --git )', diff, flags=re.MULTILINE)):
            pieces = re.split(r'(?=^@@ )', chunk, flags=re.MULTILINE)
            header = pieces[0]
            for item in pieces[1:] or [header]:
                text = header + item if item != header else item
                result[digest(text)[:24]] = text
        return result
    def signature(text):
        text = re.sub(r'^index .*\n', '', text, flags=re.MULTILINE)
        return re.sub(r'^@@ .*?@@', '@@', text, flags=re.MULTILINE)
    inherited = set()
    for parent in parents:
        git(snapshot, 'merge-base', '--is-ancestor', parent, 'HEAD')
        inherited.update(signature(text) for text in hunks(git(snapshot, 'diff', '--binary',
                          '--no-ext-diff', '--unified=3', base, parent, '--')).values())
    actual = hunks(git(snapshot, 'diff', '--binary', '--no-ext-diff', '--unified=3', base, '--'))
    return {key: text for key, text in actual.items() if signature(text) not in inherited}
