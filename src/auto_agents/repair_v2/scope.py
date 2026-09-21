"""One goal-bound necessity receipt, shared by routing, planning and review."""
import hashlib
import json
from pathlib import Path
import re

from .store import Store, atomic_json, digest
from .types import RepairBlocked

POLICY = 'goal-scope-v2'
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
    'must be reused. For a large log cite the exact goal_scope.incident.event as '
    '{origin:"target",path,pointer,sha256}; for current engine code use {origin:"source",path}. '
    'Do not fabricate execution observations or user authorization.'
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
    from .incidents import resolve_subject, observation as failure_observation
    state, operation = resolve_subject(target, state, invocation.get('engine_route') or {}, workflow)
    operation = operation or subject
    owner = {'project': project, 'subject': subject, 'workflow_id': workflow}
    observation, incident = failure_observation(state, owner, operation)
    if not invocation.get('engine_route') and payload.get('error') and not observation:
        observation = {'exception': failure_condition(payload['error'])}
    from ..scope_decisions import Decisions
    approvals = Decisions(target).approvals(owner)
    from .incidents import condition
    current_condition = condition(observation['failure'], incident['phase']) if incident and 'failure' in observation else observation
    return {'policy': POLICY, 'owner': owner, 'original_goal': goal,
            'approved_changes': approvals, 'goal_version': digest([goal, approvals]),
            'blocker': {'operation': operation, 'boundary': payload.get('boundary', {}).get('kind', 'engine_route'),
                        'condition': digest(current_condition),
                        'incident_id': incident['identity'] if incident else digest([owner, observation])},
            'incident': incident, 'observation': observation}


def same_context(a, b):
    return (isinstance(a, dict) and isinstance(b, dict)
            and all(a.get(k) == b.get(k) for k in ('policy', 'owner', 'original_goal', 'goal_version', 'blocker'))
            and all((a.get('incident') or {}).get(k) == (b.get('incident') or {}).get(k)
                    for k in ('domain', 'candidate')))


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
        if not isinstance(ref, (str, dict)):
            raise RepairBlocked('scope_evidence', '范围证据路径无效。')
        selected = ref if isinstance(ref, dict) else {}
        if isinstance(ref, dict) and (
                not isinstance(ref.get('path'), str) or not ref['path']
                or ref.get('origin') not in {'source', 'target'}
                or not isinstance(ref.get('pointer', ''), str)
                or any(k in ref and (not isinstance(ref[k], str) or not re.fullmatch(r'[0-9a-f]{64}', ref[k]))
                       for k in ('sha256', 'snapshot'))):
            raise RepairBlocked('scope_evidence', '证据引用必须包含明确来源、路径和有效的内容摘要。')
        name = re.sub(r':\d+(?::\d+)?$', '', selected.get('path', '') if selected else ref)
        name = name.removeprefix('/repair-evidence/original/')
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts
                or relative.name == '.env' or name.startswith('.auto-agents/operator/')):
            raise RepairBlocked('scope_evidence', '范围证据必须来自当前任务或引擎的可检查文件。')
        found = []
        for kind, root in [('target', target), ('source', source)]:
            if selected and selected.get('origin') != kind:
                continue
            root = Path(root).resolve()
            path = root / relative
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                pointer = selected.get('pointer', '')
                if pointer:
                    # Exact event extraction, not a larger model-input limit.
                    if kind != 'target' or not re.fullmatch(r'/execution_log/\d+', pointer):
                        raise RepairBlocked('scope_evidence', '失败事件定位无效。')
                    try:
                        value = json.loads(path.read_text())['execution_log'][int(pointer.rsplit('/', 1)[1])]
                    except (ValueError, KeyError, IndexError, TypeError) as error:
                        raise RepairBlocked('scope_evidence', '失败事件不存在。') from error
                    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
                    checksum = digest(value)
                else:
                    if path.stat().st_size > 4 * 1024 * 1024:
                        raise RepairBlocked('scope_evidence', '请引用更精确的失败依据。')
                    raw = path.read_bytes()
                    checksum = hashlib.sha256(raw).hexdigest()
                if len(raw) > 4 * 1024 * 1024:
                    raise RepairBlocked('scope_evidence', '请引用更精确的失败依据。')
                if selected.get('sha256') and checksum != selected['sha256']:
                    raise RepairBlocked('scope_evidence', '失败证据内容已变化，不能复用旧依据。')
                found.append({'kind': kind, 'origin': kind, 'path': name, 'sha256': checksum,
                              'snapshot': checksum, 'pointer': pointer})
        if not found:
            raise RepairBlocked('scope_evidence', f'无法读取当前阻塞的依据：{name}')
        if len(found) > 1 and found[0]['sha256'] != found[1]['sha256']:
            raise RepairBlocked('scope_evidence', '证据在任务与引擎中含义不同，请指定来源：' + name)
        if selected.get('snapshot') and selected['snapshot'] != found[0]['snapshot']:
            raise RepairBlocked('scope_evidence', '证据快照与声明版本不一致。')
        result.append(found[0])
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
        if not same_context(record.get('context'), self.context):
            return None
        try:
            current = witnesses(record['proposal']['evidence_refs'], self.target, self.source)
        except (RepairBlocked, OSError):
            return None
        return saved if current == record['witnesses'] else None

    def import_receipt(self, record):
        if not isinstance(record, dict) or not same_context(record.get('context'), self.context):
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
        domain = (self.context.get('incident') or {}).get('domain')
        if domain in {'product', 'proof_review', 'environment'}:
            raise RepairBlocked('scope_failure_owner', {
                'product': '当前是产品候选验证失败，应修正原候选，不能据此修改引擎。',
                'proof_review': '当前候选需要测试修订审核，请继续原子流程的审核与验证。',
                'environment': '当前阻塞属于验证环境，应恢复环境后重试原检查。',
            }[domain])
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
