"""Translations for built-in user messages; provider prose remains verbatim."""
from __future__ import annotations

import re


ZH_MESSAGES = {
    "Agent:": "Agent：",
    "Agent is thinking, please wait...": "Agent 正在处理，请稍候…",
    "Agent has understood the goal. Proceeding to execution.": "已明确目标，开始执行。",
    "Capturing baseline gate snapshot...": "正在准备验证基线…",
    "Baseline source captured; gate execution is deferred until a candidate shard fails.": "验证基线已保存，将在需要比较失败结果时执行。",
    "Baseline snapshot: all gate commands pass.": "验证基线检查全部通过。",
    "Git HEAD changed since last baseline — re-capturing baseline snapshot.": "代码版本已变化，正在更新验证基线。",
    "Verification passed!": "验证通过。",
    "Final verification passed!": "最终验证通过。",
    "Verified progress committed before continuing.": "已保存并提交通过验证的进展，继续执行。",
    "Continuing the loop to fix verification issues.": "正在修正验证发现的问题。",
    "Resuming execution from the saved goal clarification.": "正在从已保存的目标继续执行。",
    "Session interrupted by user. Progress saved.": "执行已中断，进展已保存。",
    "Agent stalled (no output for extended period).": "Agent 长时间没有输出，执行已停止。",
    "Agent timed out.": "Agent 执行超时。",
    "No input provided. Exiting.": "未收到输入，已结束本次执行。",
    "Retrying clarification...": "正在重新梳理需求…",
    "Will retry on next attempt.": "将在下一次尝试中重试。",
    "Will retry on next iteration.": "将在下一轮重试。",
    "Quick verify failure is deterministic. Stopping without retry.": "验证已确认失败原因，本次执行已停止。",
    "Verification could not establish a comparable regression; stopping without another fix-agent attempt.": "无法确认可比较的回归结果，已停止自动修正。",
    "Verification passed, but the parent fix receipt was not committed.": "验证通过，但父修复流程的结果尚未完成提交。",
    "Verification passed, but the fix commit did not complete.": "验证通过，但修复提交尚未完成。",
    "Final verification passed, but the collab receipt was not committed.": "最终验证通过，但协作流程的结果尚未完成提交。",
    "The routed iteration resolved the original issue.": "子流程已解决原问题。",
    "Fix session stopped (no further progress). Session marked as failed.": "修复会话未取得进一步进展，已停止。",
    "Collab session stopped (no further progress). Session marked as failed.": "协作会话未取得进一步进展，已停止。",
    "Provider recovery session stopped (no further progress). Session marked as failed.": "外部服务恢复未取得进一步进展，已停止。",
    "Provider recovery stopped because the downstream verification contract is unchanged.": "下游验证要求尚未调整，外部服务恢复已停止。",
    "Provider recovery remains blocked because its consumer contract has not changed.": "使用方要求尚未调整，外部服务恢复仍受阻。",
    "Provider references and full preflight now pass. Resuming run...": "外部服务资料与预检查均已通过，正在恢复任务…",
    "Provider-research recovery completed and the run was resumed.": "外部服务准备已恢复，原任务已继续执行。",
    "Loaded current provider_research blockers into a recovery session.": "已将外部服务准备中的问题载入恢复会话。",
    "Could not establish the goal's execution environment; stopping before implementation.": "无法明确目标的执行环境，已在实现前停止。",
    "Max clarification rounds reached. Proceeding with current understanding.": "需求讨论轮数已达到上限，将按已明确的内容继续。",
    "Invalid selection. Enter a listed number, a session ID, or 'n' for a new session.": "选择无效。请输入列表编号、会话 ID，或输入 n 新建会话。",
    "[Resuming previous conversation]": "正在恢复之前的对话。",
    "Generating project_brief.md, please wait...": "正在生成项目需求说明…",
    "Agent is ready to generate project_brief.md.": "已准备好生成项目需求说明。",
    "Generating README.md, please wait...": "正在生成 README.md…",
    "Agent is updating the plan, please wait...": "正在更新计划…",
    "Entering interactive clarify session, please wait for the agent to analyze the spec...": "正在分析需求，准备开始讨论…",
    "Entering README preparation, please wait for the agent to analyze the project...": "正在分析项目，准备编写 README…",
    "Generation automatically confirmed by --auto-approve.": "已按自动确认设置开始生成。",
    "Project execution is already completed. Do you want to start a new iteration for further development? [y/N]": "项目执行已完成。是否开始新一轮开发？[y/N]",
    "Your reply:": "请输入回复：",
    "Confirm generation? (y/n) [y]:": "确认生成？(y/n) [y]：",
    "Please provide your thoughts:": "请提供你的想法：",
    "Do you have anything to add or modify? (y/n) [n]:": "是否有需要补充或修改的内容？(y/n) [n]：",
    "Please describe what to add or change:": "请说明需要补充或修改的内容：",
    "Describe the bug you want to address:": "请描述需要修复的问题：",
    "Describe the goal you want to address:": "请描述希望完成的目标：",
    "Run hit a provider_research blocker. Starting automatic provider recovery...": "外部服务准备暂时受阻，正在启动自动恢复…",
    "Run hit an auto_agents-owned failure. Starting automatic auto_agents self-repair...": "已确认 auto_agents 运行异常，正在启动自动修复…",
}
REPAIR_PHASES = {
    "diagnosing": ("定位故障原因", "diagnosing the failure"),
    "starting": ("准备自动修复", "preparing automatic repair"),
    "generating_candidate": ("处理修复代码", "working on repair code"),
    "validating_boundary_replay": ("检查原任务恢复结果", "checking original task recovery"),
    "validating_diagnosis_differential": ("验证故障修复效果", "verifying the defect fix"),
    "reviewing_candidate": ("审查修复方案", "reviewing the repair"),
    "validating_focused_tests": ("执行针对性验证", "running focused checks"),
    "validating_integration": ("验证集成结果", "checking integration"),
    "reviewing_integration": ("审查集成结果", "reviewing integration"),
    "validating_full_suite": ("执行完整验证", "running full validation"),
    "sealing_proof": ("保存验证结果", "saving validation results"),
    "correcting_deterministic_violations": ("修正已发现的问题", "correcting identified issues"),
}


def user_text(message: str, language: str) -> str:
    if language != "zh":
        return message
    # Translate exact built-in templates only, without interpreting provider prose.
    stripped = message.strip()
    if stripped in ZH_MESSAGES:
        return ZH_MESSAGES[stripped]
    for pattern, replacement in (
        (r"^Session (.+) started in (.+) mode\.$", r"已开始 \2 会话：\1。"),
        (r"^Session (.+) is already completed\.$", r"会话 \1 已完成。"),
        (r"^--- (Fix|Collab) iteration (\d+) ---$", r"第 \2 轮处理"),
        (r"^Invalid answer: (.*)$", r"输入无效：\1"),
    ):
        if re.match(pattern, stripped):
            return re.sub(pattern, replacement, stripped)
    if stripped.startswith("Agent:\n"):
        return "Agent：\n" + stripped[len("Agent:\n"):]
    return message
