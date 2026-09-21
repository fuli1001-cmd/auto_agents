"""Execute existing-behavior acceptance without adopting the project's saved run.

The session owns the request, evidence and review. Product/configuration writes
remain subject to the same restoration boundary used by collab diagnosis.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil

from .git_ops import head_ref
from .prompting.core import compose_prompt
from .repair_v2.store import digest


def is_request(target, payload):
    return target == 'acceptance' or (target == 'run' and
        payload.get('spec_seed', {}).get('scope') == 'existing_behavior_real_acceptance_only')


def prepare(session, state, payload):
    if not session._goal_environment_confirmed(state):
        state.status = 'conversing'
        session._save(state)
        return state
    seed = dict(payload.get('spec_seed', {}))
    prior = [row['directory'] for row in state.execution_log
             if row.get('action') == 'acceptance_result' and row.get('directory')]
    if state.acceptance_execution.get('directory'):
        prior.append(state.acceptance_execution['directory'])
    value = {'goal': state.goal, 'environment': state.goal_execution_environment,
             'authorization': state.authorization_policy, 'workflow_id': state.workflow_id,
             'session_id': state.session_id, 'revision': head_ref(session.project_root), 'request': seed,
             'prior_evidence_directories': list(dict.fromkeys(prior))}
    state.acceptance_execution = {'inputs': value, 'identity': digest(value), 'phase': 'pending'}
    state.status, state.return_phase, state.resolution = 'executing', '', ''
    state.execution_log.append({'action': 'acceptance_requested', 'result': 'Execute the existing goal without adopting saved development tasks',
                                'timestamp': session._now()})
    session._save(state)
    return state


def recover_deferred(session, state):
    """Upgrade the specific pre-entry pause emitted by the legacy run router."""
    if state.acceptance_execution or not state.conversation or state.conversation[-1].get('role') != 'agent':
        return False
    last = state.conversation[-1].get('content', '')
    if 'NEED_USER_ASSIST v1:' not in last:
        return False
    if not any(row.get('action') == 'run_route_deferred' for row in state.execution_log[-3:]):
        return False
    # Only a previously requested acceptance route is eligible, not an arbitrary
    # older route, a new product request, or an answered scope-change question.
    for row in reversed(state.conversation[:-1]):
        match = re.search(r'^ROUTE_WORKFLOW v1:\s*(\{.*\})\s*$', row.get('content', ''), re.MULTILINE)
        if match:
            try:
                route = json.loads(match.group(1))
            except ValueError:
                return False
            if not is_request(route.get('target'), route):
                return False
            prepare(session, state, route)
            return True
    return False


def _json(text):
    text = text.strip()
    if text.startswith('```'):
        text = '\n'.join(text.splitlines()[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('验收结果必须是 JSON 对象')
    return value


def _evidence(directory, result):
    if directory.is_symlink():
        raise ValueError('验收证据目录不能是符号链接')
    refs = result.get('evidence')
    if not isinstance(refs, list) or not refs:
        raise ValueError('验收结果缺少实际证据')
    evidence = {}
    for item in refs:
        if not isinstance(item, str):
            raise ValueError('验收证据路径无效')
        relative = Path(item)
        path = directory / relative
        if relative.is_absolute() or '..' in relative.parts or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('验收证据必须保存在本次验收目录')
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ValueError('验收证据不存在或为空：' + item)
        checksum = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(block)
        evidence[item] = checksum.hexdigest()
    return evidence


def _call(session, state, purpose, prompt):
    stop = session._should_stop(state, 'acceptance call budget')
    if stop:
        raise ValueError(stop)
    session._record_agent_attempt(state)
    session._save(state)
    before = session._supervised_worktree_snapshot()
    checkpoint = session.project_root / '.auto-agents/state/workflows' / state.workflow_id / 'checkpoints' / (
        'collab-' + state.session_id + '-' + str(state.current_attempt))
    session._capture_collab_restore_point(checkpoint, before)
    try:
        return session._call_agent(state, purpose + '-' + str(state.current_attempt), prompt)
    finally:
        offending = session._restore_collab_mutations(state, before, checkpoint)
        shutil.rmtree(checkpoint, ignore_errors=True)
        if offending:
            raise ValueError('验收修改了产品或任务配置，已撤销这些改动')


def drive(session, state):
    saved = state.acceptance_execution
    value = saved['inputs']
    if (saved['identity'] != digest(value) or value['goal'] != state.goal
            or value['revision'] != head_ref(session.project_root)
            or value['authorization'] != state.authorization_policy
            or value['environment'] != state.goal_execution_environment
            or value['workflow_id'] != state.workflow_id or value['session_id'] != state.session_id):
        state.status, state.resolution = 'blocked', 'acceptance_input_changed'
        session._save(state)
        return state
    directory = session.project_root / '.auto-agents/state/sessions' / state.session_id / 'acceptance'
    if not directory.resolve().is_relative_to(session.project_root.resolve()):
        state.status, state.resolution = 'blocked', 'acceptance_evidence_invalid'
        session._save(state)
        return state
    directory.mkdir(parents=True, exist_ok=True)
    saved['directory'] = str(directory)
    try:
        if saved.get('question'):
            session._handle_collab_assistance(state, '', saved['question'])
            saved.pop('question')
            saved['phase'] = 'pending'
            saved['inputs']['user_answers'] = [r for r in state.conversation if r.get('role') == 'user']
            saved['identity'] = digest(saved['inputs'])
            session._save(state)
            return state
        if not saved.get('result'):
            interrupted = saved['phase'] == 'executing'
            saved['phase'] = 'executing'
            session._save(state)
            prompt = compose_prompt([
                'Execute acceptance of the existing product for the original user goal below. This is NOT development, planning or a saved run resume.',
                'Use the current checkout, already containing delivered fixes. Start existing services and operate the actual browser/UI and configured providers as required by the goal.',
                'Do not implement features, edit product/config/test files, change requirements or .auto-agents control records, migrate storage, install dependencies or resume stopped tasks.',
                'Runtime data and evidence may be written. Respect the original authorization, spending limits and real/simulated environment. No fake media or proxy-only evidence.',
                'Inspect any existing execution ledger, project IDs and runtime evidence before making provider calls; reuse prior results. Never repeat an externally charged operation whose outcome is unknown.',
                'This execution was interrupted; reconcile prior outcomes before any new external operation.' if interrupted else '',
                'On a defect or missing prerequisite, return blocked with evidence; do not start implementation or broaden the goal.',
                'For a decision only the user can make, return needs_user with decision_class (goal_choice, credential, rights_attestation, unbudgeted_external_cost, destructive_change, irreversible_product_decision, or external_observation) and a short plain-language question and recommendation, without internal IDs, paths or implementation jargon.',
                'Return JSON {"status":"passed|blocked|needs_user","summary":"...","evidence":["relative path"],"question":"..."}. Passed requires all original obligations, with actual browser/media evidence where required.',
                'Copy evidence into ' + str(directory) + '; paths in evidence are relative to that directory. Record operations and their outcomes there as they happen.',
                json.dumps(value, ensure_ascii=False),
            ], purpose='acceptance_execute')
            result = _json(_call(session, state, 'acceptance-execute', prompt))
            if result.get('status') not in ('passed', 'blocked', 'needs_user') or not isinstance(result.get('summary'), str):
                raise ValueError('验收结果缺少有效状态或说明')
            if result['status'] == 'needs_user':
                question = result.get('question')
                if not isinstance(question, str) or not question.strip():
                    raise ValueError('验收需要用户输入，但没有提供问题')
                error = session._collab_assistance_error(state, question, decision_class=result.get('decision_class', ''))
                if error:
                    raise ValueError(error)
                saved['phase'], saved['question'] = 'waiting_user', question
                session._save(state)
                return drive(session, state)
            saved['result'] = result
            saved['phase'] = 'reviewing' if result['status'] == 'passed' else 'blocked'
            if result['status'] == 'passed':
                saved['evidence'] = _evidence(directory, result)
            session._save(state)
        if saved['result']['status'] == 'blocked':
            state.status, state.resolution = 'blocked', 'acceptance_blocked'
        else:
            evidence = _evidence(directory, saved['result'])
            if evidence != saved['evidence']:
                raise ValueError('验收证据已变化，需要重新核验')
            if not saved.get('review'):
                prompt = compose_prompt([
                    'Independently check whether the original goal was actually demonstrated by the supplied evidence. Inspect the evidence files; do not merely trust the executor summary.',
                    'Do not execute generation, change files, restore other tasks or repeat paid operations. Browser/media goals require actual observable content evidence, not just status, metadata or file existence.',
                    'Return JSON {"approved":true|false,"reason":"..."}. Approve only when every original obligation is proved; missing or ambiguous proof must be rejected.',
                    'Evidence directory: ' + str(directory),
                    json.dumps({'inputs': value, 'result': saved['result'], 'evidence': evidence}, ensure_ascii=False),
                ], purpose='acceptance_review')
                saved['review'] = _json(_call(session, state, 'acceptance-review', prompt))
                session._save(state)
            if _evidence(directory, saved['result']) != evidence:
                raise ValueError('审核期间验收证据发生变化')
            review = saved['review']
            if not isinstance(review.get('approved'), bool) or not isinstance(review.get('reason'), str) or not review['reason'].strip():
                raise ValueError('验收审核结果格式无效')
            state.status = 'completed' if review['approved'] else 'blocked'
            state.resolution = 'goal_achieved' if review['approved'] else 'acceptance_review_rejected'
            saved['phase'] = state.status
        state.execution_log.append({'action': 'acceptance_result', 'result': saved['result'],
                                    'review': saved.get('review'), 'directory': str(directory), 'timestamp': session._now()})
        state.conversation.append({'role': 'agent', 'content': 'Acceptance result: ' + json.dumps(
            {'result': saved['result'], 'review': saved.get('review')}, ensure_ascii=False)})
    except (ValueError, TypeError, KeyError, OSError) as error:
        saved['error'] = str(error)
        saved['phase'] = 'blocked'
        state.status, state.resolution = 'blocked', 'acceptance_evidence_invalid'
        state.conversation.append({'role': 'orchestrator', 'content': 'Acceptance blocked: ' + str(error)})
    session._save(state)
    return state


def completed(session, state):
    saved = state.acceptance_execution
    value = saved.get('inputs', {})
    directory = session.project_root / '.auto-agents/state/sessions' / state.session_id / 'acceptance'
    try:
        return (saved.get('phase') == 'completed' and saved.get('identity') == digest(value)
                and value.get('goal') == state.goal and value.get('workflow_id') == state.workflow_id
                and value.get('session_id') == state.session_id
                and value.get('environment') == state.goal_execution_environment
                and value.get('authorization') == state.authorization_policy
                and saved.get('review', {}).get('approved') is True
                and _evidence(directory, saved['result']) == saved['evidence'])
    except (ValueError, KeyError, TypeError, OSError):
        return False
