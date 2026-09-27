"""Short descriptions of persisted workflow actions, never execution authority."""
from __future__ import annotations

import re
from pathlib import Path

from .diagnostic_output import plain_text, redact
from .execution_binding import route_sources


USER_SUMMARY_INSTRUCTION = (
    'Include user_summary in the project documentation language. Write one short sentence for a '
    'person unfamiliar with the code: what they cannot do, and the confirmed reason if known. '
    'Describe the observed problem, not a proposed implementation or permission rule. '
    'Do not merely replace technical words with synonyms. Omit paths, identifiers, commands, '
    'protocols, secrets, ownership/binding jargon and speculative causes. Keep unknown causes explicitly unknown. '
    'This sentence is for display only and grants no scope, authorization or completion credit.'
)


def stage(name, language='zh'):
    labels = {'repair': ('自修复', 'Self-repair'), 'fix': ('修复', 'Fix'),
              'collab': ('分析', 'Analysis'), 'acceptance': ('验收', 'Acceptance'),
              'conversing': ('目标确认', 'Goal'), 'run': ('实现', 'Implementation'),
              'resume': ('恢复', 'Resume'), 'provider_resolve': ('服务恢复', 'Service recovery')}
    pair = labels.get(name, (name, name))
    return '[' + pair[0 if language == 'zh' else 1] + ']'


def _readable(value, language):
    if not isinstance(value, str) or not value.strip():
        return ''
    if language == 'zh' and not re.search(r'[\u4e00-\u9fff]', value):
        return ''
    # Never turn an implementation title into a purported user explanation
    # by substituting individual words. Keep such text in diagnostics instead.
    if re.search(r'[`{}]|\b\w+_\w+\b|\b\w+(?:Error|Exception|Exceeded)\b|\b\w+\([^)]*\)|[/\\]|::|\b(?:preflight|payload|digest|handoff)\b|'
                 r'归属|快照|预检|候选|验收合同|验证绑定|安全处理|^允许', value, re.I):
        return ''
    return summary(value, language, limit=64 if language == 'zh' else 140)


def _known_problem(text, language):
    # Translate failure families into symptom + impact, never into permission
    # to bypass the underlying check. Unknown causes get no invented diagnosis.
    cases = [
        (r'retained task plan ownership is unavailable|(?:缺少|不存在|无\s*|missing[^\n]*)(?:task_plan\.json|(?:旧|历史)?任务计划)',
         '无法读取所需的旧任务记录，项目修复无法开始。',
         'The required earlier task records cannot be read, so the project fix cannot start.'),
        (r'no explicit acceptance obligations|缺少[^。\n]*(?:验收要求|完成标准)',
         '自动修复缺少明确的完成标准，暂时无法继续。',
         'Automatic repair has no clear completion criteria and cannot continue yet.'),
        (r'SCHEMA_DATABASE_MISSING|database does not exist',
         '所需数据尚未准备好，服务无法启动。',
         'Required data is not ready, so the service cannot start.'),
        (r'ModuleNotFoundError|No module named|缺少(?:运行)?依赖',
         '运行所需的组件没有准备好，任务暂时无法继续。',
         'A required component is unavailable, so the task cannot continue yet.'),
        (r'usageLimitExceeded|usage limit|额度(?:已)?(?:用完|用尽|耗尽)',
         '当前助手的使用额度已用完，任务暂时无法继续。',
         'The assistant has reached its usage limit, so the task cannot continue yet.'),
        (r'ConnectionRefusedError|connection refused|连接被拒绝',
         '无法连接所需服务，任务暂时无法继续。',
         'A required service cannot be reached, so the task cannot continue yet.'),
        (r'TimeoutError|timed out|响应超时',
         '等待执行结果超时，任务暂时无法继续。',
         'Waiting for a result timed out, so the task cannot continue yet.'),
        (r'modified files outside its ownership|修改[^。\n]*超出[^。\n]*范围',
         '修改范围检查未通过，任务已停止。',
         'The changes exceeded the permitted scope, so the task stopped.'),
    ]
    for pattern, zh, en in cases:
        if re.search(pattern, text, re.I):
            return zh if language == 'zh' else en
    return ''


def summary(value, language='zh', limit=100):
    if not isinstance(value, str):
        return ''
    value = plain_text(redact(value))
    value = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', value)
    value = re.sub(r'(?:^|\s)/[^\s；，。]+|(?:\.auto-agents|src|tests|app|config)/[^\s；，。]+', '', value)
    value = re.sub(r'\b(?:hf|wf)-[\w-]+\b|\b[0-9a-f]{12,}\b', '', value)
    value = re.sub(r'`([^`]+)`', r'\1', value)
    if language == 'zh':
        for technical, readable in (
            ('retained task plan ownership is unavailable', '无法确认修复所需的任务记录'),
            ('engine request has no explicit acceptance obligations', '自动修复缺少明确的完成标准'),
            ('SCHEMA_DATABASE_MISSING', '所需数据尚未准备好'),
            ('FastAPI', '后端服务'),
            ('无任务归属', '不接管其他任务'), ('历史提交', '原有版本'),
            ('验证归属', '开始前的检查'),
            ('focused_fix', '局部修复'), ('task_plan.json', '任务计划'),
            ('verification_ownership', '修复开始前的检查'), ('预检', '开始前的检查'),
            ('引擎', '自动化工具'),
        ):
            value = value.replace(technical, readable)
    value = ' '.join(value.split()).strip(' ：:；;，,')
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def problem(payload, language='zh'):
    sources = list(route_sources(payload)) if isinstance(payload, dict) else []
    raw = []
    for source in reversed(sources):
        value = _readable(source.get('user_summary'), language)
        if value:
            return value
    for source in sources:
        for key in ('summary', 'title', 'reason', 'error', 'symptom', 'reproduction', 'causal_chain'):
            value = source.get(key, '')
            raw.extend(value if isinstance(value, list) else [value])
    known = _known_problem('\n'.join(item for item in raw if isinstance(item, str)), language)
    if known:
        return known
    for source in sources:
        necessity = source.get('necessity') or {}
        for value in [source.get('summary'), source.get('title'), source.get('symptom'), source.get('reason'),
                      necessity.get('consequence') if isinstance(necessity, dict) else None,
                      *(source.get('causal_chain') or [])]:
            readable = _readable(value, language)
            if readable:
                return readable
    if any(isinstance(item, str) and item.strip() for item in raw):
        return ('任务暂时无法继续，具体原因还需要检查。' if language == 'zh' else
                'The task cannot continue yet; its cause still needs investigation.')
    return ''


def repair_problem(payload, language='zh'):
    route = (payload.get('invocation') or {}).get('engine_route') or {}
    value = problem({'issue_seed': route, 'error': payload.get('error', '')}, language) if route else ''
    if value:
        return value
    final = (payload.get('diagnosis') or {}).get('final') or {}
    value = problem(final, language)
    return value or problem(payload, language)


def repair_topic(payload, job_id=''):
    import hashlib
    import json
    route = (payload.get('invocation') or {}).get('engine_route')
    if route:
        return 'route:' + hashlib.sha256(json.dumps(route, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return 'job:' + str(job_id) if job_id else ''


def session_view(root, state, read_json, language='zh'):
    zh = language == 'zh'
    directory = root / '.auto-agents/state'
    issue = read_json(directory / 'sessions' / state.session_id / 'issue.json')
    detail = problem(issue, language)
    tag = stage(state.mode, language)
    topic = getattr(state, 'parent_handoff_id', '') or state.session_id
    status, mode = state.status, state.mode
    acceptance = getattr(state, 'acceptance_execution', {}) or {}
    if status == 'waiting_child':
        topic = state.active_handoff_id
        handoff = read_json(directory / 'handoffs' / (state.active_handoff_id + '.json'))
        target = handoff.get('target')
        continuing = False
        seen = {state.active_handoff_id}
        for _ in range(16):
            payload = handoff.get('payload') or {}
            reference = payload.get('resume_handoff_id') or next((source.get('original_handoff_id')
                or source.get('failed_handoff_id') for source in route_sources(payload)
                if source.get('original_handoff_id') or source.get('failed_handoff_id')), '')
            if (not isinstance(reference, str) or not reference or reference in seen
                    or not re.fullmatch(r'[A-Za-z0-9_-]+', reference)):
                break
            seen.add(reference)
            retained = read_json(directory / 'handoffs' / (reference + '.json'))
            if not retained:
                break
            handoff, target, continuing = retained, retained.get('target'), True
            topic = reference
        detail = problem(handoff.get('payload') or {}, language)
        tag = stage(target or 'resume', language)
        labels = {'fix': ('开始修复项目问题', 'Starting a project fix'),
                  'run': ('开始实现所需功能', 'Starting the required work'),
                  'resume': ('继续之前未完成的工作', 'Continuing the previous work')}
        pair = labels.get(target, ('继续处理子任务', 'Continuing the subtask'))
        if continuing and target == 'fix':
            pair = ('继续修复项目问题', 'Continuing the project fix')
    elif mode == 'fix':
        labels = {'conversing': ('分析项目问题，确认修复方案', 'Investigating the project issue and planning a fix'),
                  'executing': ('准备修复项目问题', 'Preparing a project fix'),
                  'completed': ('项目问题已修复', 'Project issue fixed'),
                  'blocked': ('项目修复受阻', 'Project fix blocked'),
                  'failed': ('项目修复未完成', 'Project fix did not finish'),
                  'paused': ('项目修复已暂停', 'Project fix paused'),
                  'waiting_user': ('项目修复需要你的帮助', 'Project fix needs your help')}
        pair = labels.get(status, ('处理项目修复', 'Working on the project fix'))
    elif mode == 'collab':
        if status == 'conversing':
            tag = stage('conversing', language)
            pair = ('确认这次要完成的目标', 'Clarifying the goal')
        elif status == 'executing':
            phase = acceptance.get('phase')
            if phase in {'pending', 'executing', 'waiting_user'}:
                tag = stage('acceptance', language)
                pair = ('通过实际操作验收现有功能', 'Checking existing behavior through actual use')
            elif phase == 'reviewing':
                tag = stage('acceptance', language)
                pair = ('检查验收结果是否满足目标', 'Checking whether acceptance proves the goal')
            elif getattr(state, 'return_phase', '') == 'after_child':
                result = read_json(Path(state.last_child_result_ref)) if state.last_child_result_ref else {}
                child = result.get('result') or {}
                child_status = child.get('status') or next((row.get('child_status')
                    for row in reversed(getattr(state, 'execution_log', []))
                    if row.get('action') == 'child_returned'), '')
                pair = (('检查修复结果，准备继续验收', 'Checking the fix before continuing acceptance')
                        if child_status == 'completed' else
                        ('分析修复受阻的原因', 'Investigating why the fix is blocked'))
                if result.get('target') == 'run':
                    pair = (('检查功能实现结果，准备继续验收', 'Checking the implemented behavior before acceptance')
                            if child_status == 'completed' else
                            ('分析功能实现受阻的原因', 'Investigating why the implementation is blocked'))
            elif phase == 'blocked':
                pair = ('分析验收失败的原因', 'Investigating why acceptance failed')
            else:
                pair = ('分析当前问题，确定下一步', 'Investigating the issue and choosing the next step')
            detail = problem((acceptance.get('inputs') or {}).get('request') or {}, language)
        elif status == 'completed':
            pair = ('已完成本次目标', 'Goal completed')
        elif status == 'blocked' and acceptance:
            tag = stage('acceptance', language)
            pair = ('验收暂时无法继续', 'Acceptance cannot continue yet')
        else:
            labels = {'failed': ('本次任务未完成', 'The task did not finish'),
                      'paused': ('本次任务已暂停', 'The task is paused'),
                      'waiting_user': ('需要你的帮助才能继续', 'Your help is needed to continue')}
            pair = labels.get(status, ('正在处理当前问题', 'Working on the current issue'))
    else:
        labels = {'conversing': ('确认外部服务需要解决的问题', 'Clarifying the external service issue'),
                  'executing': ('处理外部服务问题', 'Resolving the external service issue'),
                  'completed': ('外部服务问题已解决', 'External service issue resolved'),
                  'blocked': ('外部服务问题暂时无法解决', 'External service issue is blocked'),
                  'failed': ('外部服务处理未完成', 'External service recovery did not finish'),
                  'paused': ('外部服务处理已暂停', 'External service recovery paused'),
                  'waiting_user': ('外部服务处理需要你的帮助', 'External service recovery needs your help')}
        pair = labels.get(status, ('处理外部服务问题', 'Resolving the external service issue'))
    return detail, tag + ' ' + pair[0 if zh else 1], topic
