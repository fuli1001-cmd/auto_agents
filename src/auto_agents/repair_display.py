"""User-facing repair observations; never a recovery or health decision."""
from __future__ import annotations


PHASES = {
    'plan': ('计划', 'Planning'),
    'implement': ('编码', 'Coding'),
    'audit': ('检查测试完整性', 'Checking test preservation'),
    'artifact': ('准备验证环境', 'Preparing verification'),
    'environment_preparation': ('准备环境', 'Preparing environment'),
    'preparing_verification_environment': ('准备环境', 'Preparing environment'),
    'engine_source_sync': ('同步引擎版本', 'Synchronizing engine'),
    'validate': ('验证', 'Verification'),
    'diagnose': ('复核失败原因', 'Checking failure causes'),
    'regression': ('验证修复效果', 'Checking the fix'),
    'baseline_comparison': ('对照原有行为', 'Comparing original behavior'),
    'boundary_preflight': ('检查任务恢复', 'Checking task recovery'),
    'boundary': ('验证任务恢复', 'Verifying task recovery'),
    'activation': ('准备恢复任务', 'Preparing task recovery'),
    'deliver': ('应用修复', 'Applying repair'),
    'source_conflicts': ('整合修复代码', 'Integrating repair'),
    'source_recheck': ('复核原有问题', 'Rechecking the original failure'),
    'request_contract_planning': ('规划检查', 'Planning checks'),
    'request_contract_ready': ('准备修复', 'Preparing repair'),
    'candidate_generation': ('编码', 'Coding'),
    'candidate_correction': ('修正代码', 'Correcting code'),
    'repair_design': ('计划', 'Planning'),
    'contract_reanalysis': ('重新规划', 'Replanning'),
    'focused_verification': ('针对性验证', 'Focused verification'),
    'validating_focused_tests': ('针对性验证', 'Focused verification'),
    'full_suite': ('完整验证', 'Full verification'),
    'boundary_replay': ('验证任务恢复', 'Verifying task recovery'),
    'validating_boundary_replay': ('验证任务恢复', 'Verifying task recovery'),
    'diagnosis_differential': ('验证修复效果', 'Checking the fix'),
    'integration_verification': ('验证集成结果', 'Verifying integration'),
    'proof_seal': ('保存验证结果', 'Saving verification results'),
    'scope_review': ('审查修复范围', 'Reviewing repair scope'),
    'plan_review': ('审查计划', 'Reviewing the plan'),
    'quick_verification': ('针对性验证', 'Focused verification'),
    'candidate_revalidation': ('复验保留代码', 'Rechecking retained code'),
    'component_plan': ('细化计划', 'Refining the plan'),
    'scope_format': ('整理审查结果', 'Preparing review results'),
    'component_revalidation_prepare': ('准备复验', 'Preparing revalidation'),
    'component_delta_review': ('审查变更影响', 'Reviewing changed behavior'),
    'component_delta_format': ('整理审查结果', 'Preparing review results'),
    'component_evidence_reused': ('接续已完成修复', 'Continuing retained repair'),
    'component_selected': ('准备修复', 'Preparing repair'),
    'plan_format': ('完善计划', 'Refining the plan'),
    'local_correction': ('分析修正方案', 'Planning corrections'),
    'planning_probe': ('检查修复方案', 'Checking the repair plan'),
    'candidate_result': ('检查修复结果', 'Checking repair results'),
}


def failure_summary(failures, language='zh'):
    """Short, conservative categories; never print arbitrary reviewer prose."""
    labels = []
    for failure in failures:
        if failure.get('unit') in {'original-boundary', 'subscriber-boundary'}:
            pair = ('原任务恢复检查未通过', 'Task recovery check failed')
        elif failure.get('infrastructure'):
            pair = ('验证环境不可用', 'Verification environment unavailable')
        elif failure.get('failed'):
            pair = ('测试未通过', 'Tests failed')
        elif failure.get('missing'):
            pair = ('部分测试未执行', 'Some tests did not run')
        elif failure.get('requirement'):
            pair = ('审查发现问题', 'Review found issues')
        elif failure.get('path'):
            pair = ('测试完整性检查未通过', 'Test preservation check failed')
        else:
            pair = ('验证未通过', 'Verification failed')
        label = pair[0 if language == 'zh' else 1]
        summary = failure.get('summary_zh' if language == 'zh' else 'summary_en')
        if isinstance(summary, str) and summary.strip():
            from .repair_environment_log import sanitize
            import re
            summary = ' '.join(sanitize(summary).split())
            # Invalid/technical summaries fall back to the stable category.
            limit = 48 if language == 'zh' else 96
            if len(summary) <= limit and not re.search(r'[/\\`]|[0-9a-f]{16,}|<redacted', summary):
                label = summary
        if label not in labels:
            labels.append(label)
    if len(labels) > 3:
        labels = labels[:3] + [('另有问题' if language == 'zh' else 'Other issues')]
    return '；'.join(labels) if language == 'zh' else '; '.join(labels)


def observation(job, subscriber, language='zh'):
    """Project a status response without exposing IDs, commands or model prose."""
    zh = language == 'zh'
    state, workflow = job.get('state'), subscriber.get('state')
    progress = job.get('display') or job.get('progress') or {}
    phase = progress.get('phase') or progress.get('kind', '')
    counts = progress.get('checks') or {}
    terminal = False
    if 'blocked' in (state, workflow):
        terminal = True
        error = str((job.get('result') or {}).get('error', ''))
        if 'usagelimitexceeded' in error.casefold() or 'usage limit' in error.casefold():
            label = ('模型额度已用尽；额度恢复后重试修复',
                     'Model usage limit reached; retry repair when usage resets')
        elif 'Insufficient disk space' in error:
            label = ('磁盘空间不足；清理空间后重新运行以继续', 'Disk space is low; free space and rerun to continue')
        elif 'recovery_proof_incomplete' in error or '恢复验证证据不完整' in error:
            label = ('原任务恢复证据不完整；更新验证控制器后重试',
                     'Task recovery evidence is incomplete; update the verifier and retry')
        elif 'no verified progress' in error or 'no_progress' in error:
            label = ('连续修正未取得新的验证进展；已停止自动修复，候选已保留',
                     'No new verified progress; automatic repair stopped, candidate retained')
        else:
            label = ('修复受阻；进度已保留，需处理阻塞后继续', 'Repair blocked; progress saved, resolve the blocker to continue')
    elif 'cancelled' in (state, workflow):
        terminal, label = True, ('修复已停止；进度已保留', 'Repair stopped; progress saved')
    elif 'waiting_user' in (state, workflow):
        terminal, label = True, ('等待你的选择', 'Waiting for your choice')
    elif workflow == 'finished':
        terminal, label = True, ('原任务已完成', 'Original task completed')
    elif workflow == 'resuming':
        confirmed = subscriber.get('payload', {}).get('recovery_confirmed')
        terminal = bool(confirmed and confirmed == {'job': job.get('id'), 'generation': job.get('generation')})
        label = ('已恢复原任务', 'Original task resumed') if terminal else ('恢复原任务', 'Resuming original task')
    elif workflow == 'validating':
        label = ('验证任务恢复', 'Verifying task recovery')
    elif workflow == 'verified' or state in {'ready', 'completed'}:
        label = ('准备恢复任务', 'Preparing task recovery')
    elif state == 'queued':
        label = ('等待开始修复', 'Waiting to start repair')
    else:
        label = PHASES.get(phase, ('处理修复', 'Working on repair'))
        if phase.startswith('review_'):
            label = ('审查', 'Review')
        if phase == 'validate':
            if progress.get('review_running'):
                label = (('审查', 'Review') if progress.get('checks_finished') else
                         ('验证与审查', 'Verification and review'))
        if phase == 'acceptance_failed':
            summary = failure_summary(progress.get('failures', []), language)
            count = len(progress.get('failures', []))
            label = (f'验收未通过：{summary}（{count}项）',
                     f'Acceptance failed: {summary} ({count} findings)')
        elif phase == 'implement' and (progress.get('correcting') or progress.get('attempt', 0) > 0):
            label = ('继续编码：修正上一轮未通过项', 'Coding: correcting the previous attempt')
        elif phase == 'plan' and progress.get('replans', 0):
            label = ('重新规划：调整修复方案', 'Replanning: revising the repair')
    name = label[0 if zh else 1]
    attempt = progress.get('attempt')
    if not terminal and active_phase(state, workflow) and isinstance(attempt, int):
        number = attempt + 1 if phase == 'implement' else attempt
        if number > 0 and phase != 'plan':
            name = (f'第{number}轮 · ' if zh else f'Attempt {number} · ') + name
    if terminal and state == 'blocked' and phase == 'acceptance_failed':
        if isinstance(attempt, int) and attempt > 0:
            name += f'（累计{attempt}轮）' if zh else f' ({attempt} attempts)'
        summary = failure_summary(progress.get('failures', []), language)
        if summary:
            name += ('；剩余：' if zh else '; Remaining: ') + summary
    if progress.get('historical'):
        name = ('历史记录 · ' if zh else 'History · ') + name
    active = state == 'repairing' and workflow not in {'validating', 'verified', 'resuming'} and not terminal
    # A subscriber recovery action must not inherit the last coding/check output.
    return {
        'name': name, 'terminal': terminal,
        'identity': (job.get('id'), job.get('generation'), state, workflow,
                     progress.get('sequence', (phase, progress.get('attempt'), progress.get('candidate'))), label),
        'checks': counts if active and not progress.get('checks_finished') else {},
        'output_at': progress.get('last_output_at') if active else None,
    }


def active_phase(state, workflow):
    return state == 'repairing' and workflow not in {'validating', 'verified', 'resuming', 'finished'}
