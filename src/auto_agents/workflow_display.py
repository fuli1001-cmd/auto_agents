"""Short descriptions of persisted workflow actions, never execution authority."""
from __future__ import annotations

import re
from pathlib import Path

from .diagnostic_output import plain_text, redact
from .execution_binding import route_sources


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
    # A deliberately written user explanation takes precedence over technical
    # diagnosis. Older records retain their existing issue description.
    for key in ('user_summary', 'summary', 'title'):
        for source in reversed(sources):
            value = summary(source.get(key), language, limit=48 if language == 'zh' else 96)
            if value:
                return value
    return ''


def repair_problem(payload, language='zh'):
    route = (payload.get('invocation') or {}).get('engine_route') or {}
    value = problem(route, language)
    if value:
        return value
    final = (payload.get('diagnosis') or {}).get('final') or {}
    value = problem(final, language)
    if not value:
        causes = final.get('causal_chain') or []
        value = summary(next((c for c in causes if isinstance(c, str)), ''), language,
                        limit=48 if language == 'zh' else 96)
    return value


def session_view(root, state, read_json, language='zh'):
    zh = language == 'zh'
    directory = root / '.auto-agents/state'
    issue = read_json(directory / 'sessions' / state.session_id / 'issue.json')
    detail = problem(issue, language)
    suffix = ('：' if zh else ': ') + detail if detail else ''
    status, mode = state.status, state.mode
    acceptance = getattr(state, 'acceptance_execution', {}) or {}
    if status == 'waiting_child':
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
        detail = problem(handoff.get('payload') or {}, language)
        suffix = ('：' if zh else ': ') + detail if detail else ''
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
        suffix = ''
        if status == 'conversing':
            pair = ('确认这次要完成的目标', 'Clarifying the goal')
        elif status == 'executing':
            phase = acceptance.get('phase')
            if phase in {'pending', 'executing', 'waiting_user'}:
                pair = ('通过实际操作验收现有功能', 'Checking existing behavior through actual use')
            elif phase == 'reviewing':
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
            if detail:
                suffix = ('：' if zh else ': ') + detail
        elif status == 'completed':
            pair = ('已完成本次目标', 'Goal completed')
        elif status == 'blocked' and acceptance:
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
    return detail, pair[0 if zh else 1] + suffix
