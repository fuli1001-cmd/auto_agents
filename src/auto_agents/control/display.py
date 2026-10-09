"""Human progress messages derived from existing controller records."""

import shlex

from ..diagnostic_redaction import sanitize


def summary(value, limit=120):
    text = " ".join(sanitize(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def progress(work, contract, *, debug=False):
    phase, mode = work["phase"], work["mode"]
    blocker = work["context"].get("acceptance_blocker") or {}
    labels = {
        "clarify": "正在明确需求",
        "prototype": "正在制作界面原型",
        "design": "正在设计实现方案",
        "plan": "正在安排实现任务",
        "provider_research": "正在核对供应商能力",
        "research": "正在调查供应商问题",
        "classify": "正在分析修复范围",
        "implement": "正在修复" if mode == "fix" else "正在实现",
        "persistence": "正在检查数据兼容性",
        "verify": "正在运行验证",
        "review": "正在审查修复结果" if mode == "fix" else "正在审查结果",
        "visual_judge": "正在检查界面效果",
        "readme": "正在更新使用说明",
        "finalize": "正在整理交付结果",
        "artifact_verify": "正在核对交付内容",
        "diagnose": "正在诊断验收阻塞" if blocker else "正在分析目标",
        "acceptance": "正在继续原项目验收"
        if blocker.get("external_ids")
        else "正在执行验收",
        "acceptance_review": "正在审查验收证据",
        "deliver": "正在交付修复" if mode == "fix" else "正在交付结果",
    }
    target = blocker.get("reason") if phase == "diagnose" else None
    target = target or next((s.strip() for s in contract.goal.splitlines() if s.strip()), "")
    message = labels.get(phase, "正在处理任务")
    if target:
        message += "：" + summary(target)
    if debug:
        message = f"[{mode}] {phase} ({work['id']}) " + message
    return message


def resume_command(project, work, *, provider=None, auto_approve=False):
    command = [
        "auto-agents", work["mode"].replace("_", "-"),
        "--project", str(project), "--session", work["id"],
    ]
    provider = provider or work["context"].get("provider")
    if provider:
        command += ["--provider", provider]
    if auto_approve:
        command.append("--auto-approve")
    return shlex.join(command)
