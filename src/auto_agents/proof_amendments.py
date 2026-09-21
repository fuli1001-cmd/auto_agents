"""Controller-owned review of a retained candidate's exact test amendments.

An approval is an overlay on immutable proof sources, never a replacement of
the writer receipt or a claim that tests passed. No candidate code is imported.
"""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .repair_v2.store import Store, digest
from .session_verification import SessionOwnershipError, ownership_error

POLICY = 'proof-amendment-v2'


class ProofReviewRequired(SessionOwnershipError):
    pass


def required(state, path):
    error = ownership_error(state, '测试文件发生修订，需要独立审核：' + path,
                            verification_ref=path, failure_kind='proof_review_required')
    return ProofReviewRequired(str(error), diagnostic=error.diagnostic)


def pending(state):
    from .repair_v2.incidents import latest_failure
    failure = latest_failure(state.to_dict())
    if not failure or not state.candidate_custody.get('receipt'):
        return False
    row = failure[1]
    path = (row.get('diagnostic') or {}).get('verification_ref', '')
    return (row.get('failure_kind') in {'verification_ownership', 'proof_review_required', 'proof_review_unavailable'}
            and path in state.verification_binding.get('proof_sources', {})
            and path in state.candidate_paths and path.endswith('.py')
            and path not in state.verification_binding.get('proof_control_paths', []))


def _root(session):
    return Path(getattr(session, '_custody_control_root', session.project_root))


def _goal(session, state):
    from .config import load_session_state
    from .workflow_chain import WorkflowStore
    original = state
    run_context = None
    if state.workflow_id:
        workflow = WorkflowStore(_root(session)).load(state.workflow_id)
        if workflow.root.kind in {'collab', 'fix', 'provider_resolve'}:
            original = load_session_state(_root(session), workflow.root.native_id)
        elif workflow.root.kind == 'run':
            from .repair_v2.scope import context
            run_context = context(_root(session), {'project': str(_root(session)), 'invocation': {
                'run_id': workflow.root.native_id, 'workflow_id': state.workflow_id}})
    from .scope_decisions import Decisions
    owner = {'project': str(_root(session).resolve()), 'subject': 'session:' + original.session_id,
             'workflow_id': state.workflow_id}
    if run_context:
        owner = run_context['owner']
    return {**owner,
            'workflow_id': state.workflow_id, 'goal': run_context['original_goal'] if run_context else original.goal,
            'authorization': original.authorization_policy,
            'environment': original.goal_execution_environment,
            'approved_changes': Decisions(_root(session)).approvals(owner),
            'clarifications': [row for row in state.conversation if row.get('scope_decision')]}


def _definitions(tree):
    """Collected entries may be revised but not removed, duplicated or skipped."""
    result = {}
    def visit(body, prefix=''):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                if name in result:
                    raise ValueError('duplicate definition: ' + name)
                result[name] = node
                if isinstance(node, ast.ClassDef):
                    visit(node.body, name + '.')
    visit(tree.body)
    return result


def admissible(before, after):
    """Only test bodies/additions; imports, fixtures, decorators and hooks stay sealed."""
    try:
        old, new = ast.parse(before), ast.parse(after)
        previous, current = _definitions(old), _definitions(new)
        if not previous.keys() <= current.keys():
            return False
        def skeleton(tree):
            value = deepcopy(tree)
            def strip(body):
                kept = []
                for node in body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                        if node.decorator_list:
                            # Existing decorators must remain byte-for-byte AST equivalent.
                            node.body = []
                            kept.append(node)
                        continue
                    if isinstance(node, ast.ClassDef):
                        node.body = strip(node.body)
                    kept.append(node)
                return kept
            value.body = strip(value.body)
            return ast.dump(value)
        if skeleton(old) != skeleton(new):
            return False
        for name, node in current.items():
            if not node.name.startswith('test_') or isinstance(node, ast.ClassDef):
                continue
            if node.decorator_list and name not in previous:
                return False
            if name not in previous:
                if (node.args.defaults or node.args.kw_defaults or node.args.posonlyargs
                        or node.args.kwonlyargs or node.args.vararg or node.args.kwarg
                        or any(a.annotation is not None for a in node.args.args)
                        or node.returns is not None and not (isinstance(node.returns, ast.Constant) and node.returns.value is None)):
                    return False
            # Skip/xfail/collection control cannot be approved by a model.
            for item in ast.walk(node):
                if isinstance(item, ast.Call):
                    target = item.func
                    called = target.attr if isinstance(target, ast.Attribute) else target.id if isinstance(target, ast.Name) else ''
                    if called in {'skip', 'skipTest', 'xfail', 'exit', 'exec', 'eval'}:
                        return False
            if name in previous:
                def checks(body):
                    return sum(isinstance(n, ast.Assert) or isinstance(n, ast.Call) and
                               ((isinstance(n.func, ast.Attribute) and (n.func.attr.startswith('assert') or n.func.attr == 'raises'))
                                or isinstance(n.func, ast.Name) and n.func.id == 'raises') for n in ast.walk(body))
                if checks(previous[name]) and not checks(node):
                    return False
                def tautologies(body):
                    return sum(isinstance(n, ast.Assert) and isinstance(n.test, ast.Constant)
                               and bool(n.test.value) for n in ast.walk(body))
                if tautologies(node) > tautologies(previous[name]) and checks(node) <= checks(previous[name]):
                    return False
                a, b = deepcopy(previous[name]), deepcopy(node)
                a.body = b.body = []
                if ast.dump(a) != ast.dump(b):
                    return False
        return True
    except (ValueError, SyntaxError, TypeError):
        return False


def inputs(session, state):
    from .session_candidate import validate_receipt
    from .session_verification import _preserves_python_checks
    validate_receipt(state)
    binding = state.verification_binding
    changes = {}
    for name, before in binding.get('proof_sources', {}).items():
        if name not in state.candidate_paths:
            continue
        path = session.project_root / name
        try:
            after = path.read_text()
        except (OSError, UnicodeError):
            raise ownership_error(state, '候选证明不可读：' + name, verification_ref=name)
        if before == after:
            continue
        if (not isinstance(before, str) or not name.endswith('.py')
                or name in binding.get('proof_control_paths', [])
                or name in binding.get('proof_config_paths', [])):
            raise ownership_error(state, '测试执行控制不能通过证明修订审核变更：' + name, verification_ref=name)
        if _preserves_python_checks(ast.parse(before), ast.parse(after)):
            continue
        if not admissible(before, after):
            raise ownership_error(state, '测试修订改变了受保护的执行控制或删除了原测试：' + name, verification_ref=name)
        changes[name] = {'before': before, 'after': after,
                         'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
                         'after_sha256': hashlib.sha256(after.encode()).hexdigest()}
    receipt = state.candidate_custody['receipt']
    scope = binding.get('task_scope', {})
    related = set(scope.get('requirement_ids', [])) | set(scope.get('associated_requirement_ids', []))
    requirements = [t for t in binding.get('tasks', []) if t.get('task_id') in scope.get('task_ids', [])
                    or related.intersection(t.get('requirement_ids', []))]
    # The review sees every actual product delta, not just the author's summary.
    import base64
    delta = {}
    for path, entry in receipt['manifest'].items():
        delta[path] = {}
        for label in ('preimage', 'postimage'):
            image = entry[label]['worktree']
            data = base64.b64decode(image.get('bytes', ''))
            delta[path][label] = {'kind': image['kind'], 'mode': image.get('mode'), 'target': image.get('target'),
                'sha256': hashlib.sha256(data).hexdigest(),
                'text': data.decode('utf-8', errors='replace') if len(data) <= 1024 * 1024 else '<large content; requires inspection>'}
    return {'policy': POLICY, 'owner': _goal(session, state), 'session_id': state.session_id,
        'binding': binding['binding_fingerprint'], 'contract_revision': binding['contract_revision'],
        'candidate': receipt['fingerprint'], 'source_revision': receipt['source_revision'],
        'requirements': requirements, 'changes': changes, 'delta': delta,
        'reviewer': {'provider': session.orch.config.active_provider,
                     'configuration': digest(session.orch.config.providers[session.orch.config.active_provider].to_dict()),
                     'effort': session.orch.config.efforts.get('self_repair_review', 'max')}}


def _store(session, value):
    return Store(_root(session) / '.auto-agents/state/proof-reviews' / digest(value))


def approved(session, state, path):
    if not state.candidate_custody.get('receipt'):
        return False
    value = inputs(session, state)
    store = _store(session, value)
    saved = store.load() or {}
    reference = saved.get('receipt')
    if saved.get('status') != 'approved' or not reference:
        return False
    receipt = store.read(reference)
    return (receipt.get('policy') == POLICY and receipt.get('inputs') == digest(value) and receipt.get('decision') == 'approve'
            and path in receipt.get('changes', {}) and receipt['changes'] == value['changes'])


def identities(session, state):
    receipt = state.candidate_custody.get('receipt')
    if not receipt or state.proof_review.get('candidate') != receipt['fingerprint']:
        return []
    value = inputs(session, state)
    store = _store(session, value)
    saved = store.load() or {}
    if saved.get('status') != 'approved':
        return []
    receipt = store.read(saved['receipt'])
    if (receipt.get('policy') != POLICY or receipt.get('inputs') != digest(value)
            or receipt.get('decision') != 'approve' or receipt.get('changes') != value['changes']):
        raise ownership_error(state, '审核凭据与当前验证输入不一致。')
    return [saved['receipt']['digest']]


def verification_step(session, state):
    if not identities(session, state):
        return None
    from .models import VerificationStep
    from glob import escape
    value = inputs(session, state)
    nodes = []
    for path, change in value['changes'].items():
        old, new = _definitions(ast.parse(change['before'])), _definitions(ast.parse(change['after']))
        nodes.extend(path + '::' + name.replace('.', '::') for name, node in new.items()
                     if node.name.startswith('test_') and not isinstance(node, ast.ClassDef)
                     and (name not in old or ast.dump(old[name]) != ast.dump(node)))
    if not nodes:
        raise ownership_error(state, '审核未对应任何可执行测试，不能继续交付。')
    return VerificationStep(proof_id='proof-amendment.' + digest(value)[:24], runner='pytest',
        targets=sorted(nodes), args=['-rA'], levels=['affected', 'release'],
        impact_paths=sorted(escape(path) for path in value['delta']), result_cache_scope='off')


def covered_gates(session, state, gates):
    step = verification_step(session, state)
    if step is None:
        return gates
    result = deepcopy(gates)
    result.steps = [s for s in result.steps if s.proof_id != step.proof_id] + [step]
    return result


def augment_plan(session, state, plan):
    session._amendment_commands = {}
    step = verification_step(session, state)
    if step is None:
        return plan
    from .gates import command_from_verification_step, GateCommandMetadata
    command = command_from_verification_step(step, session.project_root)
    nodes = step.targets
    session._amendment_commands = {command: nodes}
    plan = deepcopy(plan)
    if command not in plan.commands:
        plan.commands.append(command)
        plan.raw_command_count += 1
        # Amendment regressions are mandatory; they cannot be waived by a
        # failing historical baseline that could not contain the new test.
        plan.metadata[command] = GateCommandMetadata(proof_ids=list(state.verification_binding.get('required_proof_ids', [])),
                                                     result_cache_scope='off')
        plan.result_cache_scopes[command] = 'off'
    return plan


def execution_evidence(session, gate):
    """A successful process/collection is not proof that amended tests ran."""
    import re
    expected = getattr(session, '_amendment_commands', {})
    for command, nodes in expected.items():
        result = next((r for r in gate.commands if r.command == command), None)
        if result is None or not result.ok:
            return False
        output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', result.stdout + '\n' + result.stderr)
        passed = result.executed_tests or re.findall(r'^PASSED\s+(.+)$', output, re.MULTILINE)
        if not all(any(found == node or found.startswith(node + '[') for found in passed) for node in nodes):
            return False
        if re.search(r'^(?:SKIPPED|XFAIL|XPASS)\b', output, re.MULTILINE):
            return False
    return True


def ensure(session, state):
    """Return approved/rejected/paused; reserve before a fresh independent call."""
    from .models import AgentRequest
    value = inputs(session, state)
    if not value['changes']:
        return 'approved'
    store = _store(session, value)
    with store.locked():
        saved = store.load() or {'status': 'pending', 'inputs': digest(value), 'owner': value['owner'], 'model_calls': 0}
        state.proof_review = {'inputs': digest(value), 'candidate': value['candidate']}
        session._save(state)
        if saved.get('status') in {'approved', 'rejected'}:
            receipt = store.read(saved['receipt'])
            if receipt.get('policy') != POLICY or receipt.get('inputs') != digest(value):
                raise ownership_error(state, '审核凭据与当前候选不一致。')
            return saved['status']
        output = store.root / 'provider-output.txt'
        if saved.get('status') == 'dispatched' and not saved.get('reply'):
            state.status, state.resolution, state.resume_phase = 'paused', 'proof_review_interrupted', 'executing'
            session._save(state)
            return 'paused'
        if saved.get('status') == 'invalid':
            # A known invalid reply may be retried on explicit continuation;
            # an unknown in-flight outcome above must never be redispatched.
            store.transition(saved, status='pending', reply=None)
        session._print('正在独立审核测试修订；保留现有候选。')
        if not saved.get('reply'):
            registration = getattr(session.orch, '_repair_registration', None)
            if registration:
                from .repair_v2.chain import RepairChain
                kind, native = value['owner']['subject'].split(':', 1)
                payload = {'project': str(_root(session)), 'invocation': {
                    'session_id' if kind == 'session' else 'run_id': native}}
                chain = RepairChain(registration['config'], payload, store.root)
                from .repair_v2.types import RepairBlocked
                try:
                    chain.reserve_review(digest(value), saved['model_calls'] + 1)
                except RepairBlocked as error:
                    state.status, state.resolution, state.resume_phase = 'paused', error.code, 'executing'
                    session._print(str(error))
                    session._save(state)
                    return 'paused'
            store.transition(saved, status='dispatched', model_calls=saved['model_calls'] + 1)
            request = AgentRequest(stage='proof_review', purpose='proof_review',
                effort=session.orch.config.efforts.get('self_repair_review', 'max'),
                prompt=('Independently review this exact test amendment against the original approved goal and frozen '
                    'requirements. Do not edit files, run product code, or contact content providers. Inspect the entire '
                    'delta. Approve only if all revised expectations are justified by existing requirements and the '
                    'original obligations remain covered. Reject weakened assertions or changes merely to pass. '
                    'Return JSON {"decision":"approve|reject|needs_user", "reason":"...", '
                    '"coverage":[{"path":"...", "requirement":"original_goal or bound requirement ID", "reason":"..."}], '
                    '"change_coverage":[{"path":"every path in delta", "reason":"why necessary for the original goal"}], '
                    '"question":"plain user explanation when ambiguous", "suggestion":"specific choice"}.\n'
                    + json.dumps(value, ensure_ascii=False)), cwd=session.project_root,
                output_path=output, sandbox_mode='read-only', resume_session_id='',
                logical_call_id='proof-review:' + digest(value) + ':' + str(saved['model_calls']),
                usage_context={'project_root': str(_root(session)), 'workflow_kind': 'proof_review',
                               'subject_id': value['owner']['subject'].split(':', 1)[1], 'workflow_id': state.workflow_id})
            try:
                result = session.orch._call_with_failover(request)
                if not result.ok or getattr(result, 'cleanup_incomplete', False):
                    from .repair_environment_log import sanitize
                    raise RuntimeError(sanitize(getattr(result, 'error', '') or result.stderr or '审核服务未完成'))
                reply = store.artifact('reply', {'text': result.summary or result.stdout})
                store.transition(saved, reply=reply, status='received')
            except (KeyboardInterrupt, SystemExit):
                store.transition(saved, status='interrupted')
                raise
            except Exception as error:
                store.transition(saved, status='interrupted', error=str(error))
                state.status, state.resolution, state.resume_phase = 'paused', 'proof_review_unavailable', 'executing'
                state.execution_log.append({'action': 'proof_review_failed', 'failure_kind': 'proof_review_unavailable',
                    'result': str(error), 'retry_fix': False,
                    'diagnostic': {'verification_ref': next(iter(value['changes']))}})
                session._save(state)
                return 'paused'
        # A read-only provider declaration is insufficient; recheck the receipt
        # and every review input before making its reply effective.
        if inputs(session, state) != value:
            raise ownership_error(state, '审核期间候选或证明输入发生变化。')
        text = store.read(saved['reply'])['text'].strip()
        if text.startswith('```'):
            text = '\n'.join(text.splitlines()[1:-1])
        try:
            verdict = json.loads(text)
            decision = verdict['decision']
            if (decision not in {'approve', 'reject', 'needs_user'}
                    or not isinstance(verdict.get('reason'), str) or not verdict['reason'].strip()):
                raise ValueError('missing decision')
            coverage = verdict.get('coverage') or []
            if not isinstance(coverage, list) or any(not isinstance(r, dict) for r in coverage):
                raise ValueError('invalid amendment coverage')
            if decision == 'needs_user' and any(not isinstance(verdict.get(k), str) or not verdict[k].strip()
                                              for k in ('question', 'suggestion')):
                raise ValueError('missing user-facing choice')
            requirements = {'original_goal'} | {r for task in value['requirements'] for r in task.get('requirement_ids', [])}
            if decision == 'approve' and ({r['path'] for r in coverage if r.get('requirement') in requirements and r.get('reason')}
                                         != set(value['changes'])):
                raise ValueError('incomplete amendment coverage')
            delta_coverage = verdict.get('change_coverage') or []
            if decision == 'approve' and (not isinstance(delta_coverage, list)
                    or any(not isinstance(r, dict) or not isinstance(r.get('reason'), str) or not r['reason'].strip()
                           for r in delta_coverage)
                    or {r.get('path') for r in delta_coverage} != set(value['delta'])):
                raise ValueError('incomplete product change coverage')
        except (ValueError, TypeError, KeyError) as error:
            store.transition(saved, status='invalid')
            state.status, state.resolution, state.resume_phase = 'paused', 'proof_review_invalid', 'executing'
            session._print('审核结果格式不完整，已保留候选；继续时仅重试审核，不重新实施。')
            session._save(state)
            return 'paused'
        if decision == 'needs_user':
            from .scope_decisions import session_choice
            from .repair_v2.scope import context
            current = context(_root(session), {'project': str(_root(session)),
                'invocation': {'session_id': state.session_id, 'workflow_id': state.workflow_id}})
            proposal = {'question': verdict.get('question') or verdict['reason'],
                        'suggestion': verdict.get('suggestion') or '明确此测试对应的预期行为。'}
            answer = session_choice(session, state, current, proposal,
                                    {'kind': 'proof_review', 'review': digest(value), 'session_id': state.session_id})
            store.transition(saved, status='waiting_user')
            if answer in {'approve', 'comment'}:
                return ensure(session, state)
            return 'paused'
        from dataclasses import asdict
        from .repair_v2.types import ProofAmendmentReceipt
        receipt = store.artifact('amendment', asdict(ProofAmendmentReceipt(
            POLICY, digest(value), decision, value['changes'], verdict)))
        store.transition(saved, status='approved' if decision == 'approve' else 'rejected', receipt=receipt)
        state.execution_log.append({'action': 'proof_review_approved' if decision == 'approve' else 'proof_review_rejected',
                                    'review': digest(value), 'receipt': receipt, 'candidate': value['candidate'],
                                    'result': verdict['reason']})
        session._save(state)
        return saved['status']
