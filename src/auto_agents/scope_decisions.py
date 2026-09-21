"""Durable, explicit goal choices rendered by the single foreground terminal."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path

from .repair_v2.store import atomic_json, digest


class Decisions:
    def __init__(self, project):
        self.root = Path(project) / '.auto-agents/state/scope-decisions'

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / 'decisions.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self, identity):
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('invalid decision identity')
        path = self.root / (identity + '.json')
        if path.is_symlink():
            raise ValueError('invalid decision path')
        return json.loads(path.read_text())

    def create(self, context, proposal, *, continuation=None):
        request = {'context': context, 'question': proposal['question'], 'suggestion': proposal['suggestion'],
                   'continuation': continuation or {}}
        identity = digest(request)
        with self.locked():
            path = self.root / (identity + '.json')
            if path.exists():
                return self.read(identity)
            # A rejected expansion of this same blocked step cannot be
            # reworded into another approval prompt without new user input.
            for previous in self.root.glob('*.json'):
                old = json.loads(previous.read_text())
                if (old.get('answer') == 'approve' and old.get('context', {}).get('owner') == context.get('owner')
                        and old['context'].get('original_goal') == context.get('original_goal')
                        and old['context'].get('blocker') == context.get('blocker')
                        and old.get('suggestion') == proposal['suggestion']
                        and old.get('continuation') == request['continuation']):
                    return old
                if (old.get('answer') == 'keep' and old.get('context', {}).get('owner') == context.get('owner')
                        and old['context'].get('goal_version') == context.get('goal_version')
                        and old['context'].get('blocker') == context.get('blocker')):
                    return old
            request.update(id=identity, version=1, status='pending', answer='', user_text='')
            atomic_json(path, request)
            return request

    def answer(self, identity, version, answer, *, user_text='', goal_version=None):
        if answer not in ('approve', 'keep', 'comment'):
            raise ValueError('a goal change requires an explicit choice')
        with self.locked():
            request = self.read(identity)
            if request['version'] != version:
                raise ValueError('问题已更新，请回答当前显示的问题。')
            if request['status'] == 'answered':
                if request['answer'] != answer or request['user_text'] != user_text:
                    raise ValueError('这项决定已经保存，不能用过期回答覆盖。')
                return request
            from .repair_v2.scope import context as goal_context
            owner = request['context']['owner']
            kind, native = owner['subject'].split(':', 1)
            project = self.root.parents[2]
            actual = goal_context(project, {'project': str(project), 'invocation': {
                'session_id' if kind == 'session' else 'run_id': native,
                'workflow_id': owner.get('workflow_id', '')}})
            if (actual['owner'] != owner or actual['goal_version'] != request['context']['goal_version']
                    or goal_version is not None and goal_version != request['context']['goal_version']):
                raise ValueError('任务目标已变化，请先更新问题。')
            request.update(status='answered', answer=answer, user_text=user_text)
            atomic_json(self.root / (identity + '.json'), request)
            return request

    def pending(self, subject):
        if not self.root.exists():
            return []
        return [row for path in sorted(self.root.glob('*.json'))
                for row in [json.loads(path.read_text())]
                if row.get('context', {}).get('owner', {}).get('subject') == subject and row.get('status') == 'pending']

    def approvals(self, owner):
        if not self.root.exists():
            return []
        return [{'id': row['id'], 'change': row['suggestion']} for path in sorted(self.root.glob('*.json'))
                for row in [json.loads(path.read_text())]
                if row.get('context', {}).get('owner') == owner and row.get('answer') == 'approve']

    def original_goal(self, owner):
        if not self.root.exists():
            return ''
        for path in sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime_ns):
            row = json.loads(path.read_text())
            if row.get('context', {}).get('owner') == owner:
                return row['context'].get('original_goal', '')
        return ''


def choose(orchestrator, request):
    """Return None for detach/EOF; blank input never approves anything."""
    if request['status'] == 'answered':
        return request['answer'], request.get('user_text', '')
    prompt = (f"\n需要你决定：{request['question']}\n建议：{request['suggestion']}\n\n"
              '1. 同意上述变更并继续\n2. 保持原范围（没有可行替代方案时暂停）\n'
              '3. 保存进度，稍后决定\n请输入 1、2、3，或补充你的要求：')
    health = getattr(orchestrator, '_workflow_health_runtime', None)
    if health:
        health.set_phase('waiting_user')
        health.set_active_operation('waiting_user', '等待用户选择，不调用模型')
    while True:
        try:
            reply = orchestrator._prompt_user(prompt, default='__no_scope_input__', multiline=False).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if reply in ('3', '__no_scope_input__'):
            return None
        if reply == '1':
            return 'approve', ''
        if reply == '2':
            return 'keep', ''
        if reply:
            return 'comment', reply


def session_choice(session, state, context, proposal, continuation):
    """Persist before displaying; a restart consumes exactly this same choice."""
    decisions = Decisions(getattr(session, '_custody_control_root', session.project_root))
    request = decisions.create(context, proposal, continuation=continuation)
    state.status = 'waiting_user'
    state.resolution = 'scope_decision:' + request['id']
    session._save(state)
    registration = getattr(session.orch, '_repair_registration', None)
    import os
    if registration and os.environ.get('AUTO_AGENTS_REPAIR_SUBSCRIBER'):
        from .repair_control import rpc
        rpc(registration['config'], {'op': 'request-decision', 'subscriber': registration['subscriber'],
                                    'decision': request['id']})
        # The resumed process has no terminal. Wait for the foreground relay,
        # which owns the explicit user response; do not create another agent.
        import time
        while request['status'] == 'pending':
            try:
                status = rpc(registration['config'], {'op': 'decision-status', 'subscriber': registration['subscriber'],
                                                      'decision': request['id']})
            except OSError:
                # A foreground relay reconnects the supervisor after restart.
                # This is an idle user wait, not another model/repair attempt.
                time.sleep(0.5)
                continue
            if status.get('detached'):
                return None
            request = decisions.read(request['id'])
            if request['status'] == 'pending':
                time.sleep(0.5)
        choice = (request['answer'], request.get('user_text', ''))
    else:
        choice = choose(session.orch, request)
    if choice is None:
        return None
    request = decisions.answer(request['id'], request['version'], choice[0], user_text=choice[1],
                               goal_version=context['goal_version'])
    if choice[0] == 'approve':
        content = '我同意这项具体变更：' + request['suggestion']
    elif choice[0] == 'comment':
        content = choice[1]
    else:
        content = '保持原目标和范围；不要再次提出这项范围变更。'
    if not any(row.get('scope_decision') == request['id'] for row in state.conversation):
        state.conversation.append({'role': 'user', 'content': content, 'scope_decision': request['id']})
    state.status = 'executing' if choice[0] != 'keep' else 'blocked'
    state.resolution = '' if choice[0] != 'keep' else 'scope_change_declined'
    session._save(state)
    health = getattr(session.orch, '_workflow_health_runtime', None)
    if health:
        health.set_phase(state.mode)
    return choice[0]


def apply_repair_choice(project, identity):
    """Return a paused repair to its root; never mark its child repaired."""
    request = Decisions(project).read(identity)
    if request.get('answer') not in ('approve', 'comment'):
        return None
    content = ('我同意这项具体变更：' + request['suggestion'] if request['answer'] == 'approve'
               else request['user_text'])
    return return_to_goal(project, request['context']['owner'], content=content, identity=identity)


def return_to_goal(project, owner, *, content='', identity=None):
    from .config import load_session_state, save_session_state
    from .workflow_chain import WorkflowRef, WorkflowStore
    subject = owner['subject']
    if not subject.startswith('session:'):
        return None
    state = load_session_state(Path(project), subject.split(':', 1)[1])
    if identity and any(row.get('scope_decision') == identity for row in state.conversation):
        return state
    if state.active_handoff_id:
        store = WorkflowStore(Path(project))
        snapshot = store.load(state.workflow_id)
        handoff = store.load_handoff(state.active_handoff_id)
        if handoff.parent != WorkflowRef(state.mode, state.session_id):
            raise RuntimeError('scope decision cannot take over another workflow frame')
        if not handoff.result:
            store.record_result(snapshot, handoff, status='paused', result={
                'status': 'paused', 'resolution': 'goal_decision', 'scope_decision': identity})
        store.consume_result(snapshot, handoff, operation_id='scope-' + (identity or digest(handoff.handoff_id))[:24])
        state.last_child_result_ref = str(store.handoff_path(handoff.handoff_id))
        state.active_handoff_id = ''
    if identity:
        state.conversation.append({'role': 'user', 'content': content, 'scope_decision': identity})
    state.conversation.append({'role': 'orchestrator', 'content':
        'Continue the original goal within its existing authorization and explicit user decisions. Route any product work through '
        'the existing product workflow. The previous engine candidate is retained, not verified or repaired.'})
    state.status, state.resolution, state.return_phase = 'executing', '', ''
    save_session_state(Path(project), state)
    return state


def resume_original(orchestrator, project, identity, args, lock, *, owner=None):
    import hashlib
    import os
    from .cli import main
    state = apply_repair_choice(project, identity) if identity else return_to_goal(project, owner)
    if state is None:
        if identity:
            apply_run_choice(orchestrator, identity)
        argv = ['resume', '--project', str(project)]
    else:
        argv = [state.mode.replace('_', '-'), '--project', str(project), '--session', state.session_id]
        if getattr(args, 'provider', None): argv += ['--provider', args.provider]
        if getattr(args, 'auto_approve', False): argv.append('--auto-approve')
        if getattr(args, 'full_verify', False): argv.append('--full-verify')
    fd = os.dup(lock.fileno)
    environment = {'AUTO_AGENTS_RUN_LOCK_FD': str(fd),
                   'AUTO_AGENTS_RUN_LOCK_KEY': hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest(),
                   'AUTO_AGENTS_RUN_TOKEN': lock.run_token, 'AUTO_AGENTS_SELF_REPAIR_HEALTH_REBASE': '1'}
    previous = {key: os.environ.get(key) for key in environment}
    try:
        os.environ.update(environment)
        return main(argv)
    finally:
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        try: os.close(fd)
        except OSError: pass  # The inherited ProjectRunLock may already own/close it.


def apply_run_choice(orchestrator, identity):
    from .config import load_run_state, save_run_state
    request = Decisions(orchestrator.project_root).read(identity)
    state = load_run_state(orchestrator.project_root)
    if request['context']['owner']['subject'] != 'run:' + state.run_id:
        raise RuntimeError('scope decision belongs to another run')
    applied = list(state.resume_context.get('scope_decisions', []))
    if identity in applied:
        return state
    if request['answer'] not in ('approve', 'comment'):
        raise RuntimeError('scope change has not been approved')
    # Use the existing product clarification path for changed requirements;
    # an engine repair must not rewrite product contracts itself.
    orchestrator._rewind_state_from_stage(state, 'clarify')
    state.rejected_stage = 'clarify'
    state.rejection_reason = ('用户同意的具体范围变更：' + request['suggestion'] if request['answer'] == 'approve'
                              else '用户补充的要求（未批准扩大范围）：' + request['user_text'])
    state.resume_context['scope_decisions'] = [*applied, identity]
    save_run_state(orchestrator.project_root, state)
    return state


def resume_run_choice(orchestrator):
    from .config import load_run_state, save_run_state
    decisions = Decisions(orchestrator.project_root)
    if not decisions.root.exists():
        return None
    state = load_run_state(orchestrator.project_root)
    waiting = decisions.pending('run:' + state.run_id)
    if not waiting:
        return None
    request = waiting[0]
    choice = choose(orchestrator, request)
    if choice is None:
        state.status = 'waiting_user'
    else:
        decisions.answer(request['id'], request['version'], choice[0], user_text=choice[1])
        if choice[0] != 'keep':
            apply_run_choice(orchestrator, request['id'])
            return None
        state.status = 'blocked'
        state.last_error = '已保留原目标范围；当前没有已确认可行的继续方案。'
    save_run_state(orchestrator.project_root, state)
    return state


def resume_session_choice(session, state):
    decisions = Decisions(session.project_root)
    waiting = decisions.pending('session:' + state.session_id)
    if state.resolution.startswith('scope_decision:'):
        request = decisions.read(state.resolution.split(':', 1)[1])
        waiting = [request]
    if not waiting:
        return state
    request = waiting[0]
    continuation = request['continuation']
    if continuation.get('kind') == 'repair':
        choice = choose(session.orch, request)
        if choice is None:
            state.status, state.resolution = 'waiting_user', 'scope_decision:' + request['id']
            session._save(state)
            return state
        decisions.answer(request['id'], request['version'], choice[0], user_text=choice[1],
                         goal_version=request['context']['goal_version'])
        if choice[0] == 'keep':
            state.status, state.resolution = 'blocked', 'scope_change_declined'
            session._save(state)
            return state
        return apply_repair_choice(session.project_root, request['id'])
    choice = session_choice(session, state, request['context'], request, continuation)
    if choice in {'approve', 'comment'} and continuation.get('kind') == 'proof_review':
        from .config import load_session_state, save_session_state
        child = load_session_state(session.project_root, continuation['session_id'])
        if child.workflow_id != state.workflow_id:
            raise RuntimeError('proof choice belongs to another workflow')
        child.status, child.resolution = 'executing', ''
        save_session_state(session.project_root, child)
        if child.session_id == state.session_id:
            return child
        state.status = 'waiting_child' if state.active_handoff_id else 'executing'
        session._save(state)
    if choice == 'approve' and continuation.get('kind') == 'route':
        return session._prepare_workflow_handoff(state, target=continuation['target'],
                    reason=continuation['reason'], payload=continuation['payload'])
    return state
