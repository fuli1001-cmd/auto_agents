# 支持半成品与任意存量项目持续迭代的改进方案 (终版)

你的三点补充全都 **非常可行且逻辑自洽**。这样一来，不仅仅是用 `auto_agents` 生成一半的项目能继续开发，连以前甚至没用过 `auto_agents` 的传统老项目（Foreign Projects）也能在 `init` 引导后，丝滑地接入框架的迭代开发能力！

以下是经过你补充后的完整落地实施方案，作为本次代码改造的最终蓝图。

## 1. 彻底移除全流程的 "MVP" 硬约束
在所有底层和 `_build_prompt` 方法中去除 "MVP" 的词汇绑定。
将原有的偏向“第一期极简架构”的表述（如 `"Your goal is to extract the MVP scope..."`、`"Keep the brief compact and scoped to the MVP."` 等）全部替换为中性且聚焦**“当前目标范围 (target scope / project goals)”**的通用表述。
这能确保新旧项目或后续迭代在使用 `auto_agents` 生成文档和计划时，都不会受到强制裁剪范围的负面影响。

## 2. 增强 `run` 命令的迭代智能拦截
修改 `src/auto_agents/orchestrator.py` 里的 `run()` 方法。针对启动 `run` 时的不同状态：
- **2.1 `state.status != "completed"`**：维持现状，平滑执行现有流程。
- **2.2 `state.status == "completed"`**：不再如目前那样直接静默 `return`，而是：
  - 打印提示：`"Project execution is already completed. Do you want to start a new iteration for further development? [y/N]"`
  - 通过 `self._prompt_user` 接收输入。若用户同意（Y）：
    - 赋值 `state.status = "pending"`
    - 赋值 `state.current_stage = "clarify"`
    - 从 `state.stage_summaries` 中清理掉此前迭代过的 `"clarify", "design", "plan"`。
    - （已有的 `任务 1~N` 原样保留在 `tasks` 中）。
    - 随后逻辑自然走到 `_run_interactive_clarify`，与用户沟通本轮新需求，接着重跑设计与规划，并向后追加新任务。

## 3. 改造 `init` 以支持原生接入非 `auto_agents` 存量项目
修改 `src/auto_agents/config.py` 中的 `bootstrap_project` 逻辑。在进行初始化写入前做环境判定：
- **3.1 目标是空目录 或 完全不存在**：走现有正常 `init` 流程。
- **3.2 目标目录已存在 `.auto-agents` 文件夹**：代表已经初始化过，直接打印警告并 `return` 不做任何破坏操作。
- **3.3 目标存在且有存量代码（不存在 `.auto-agents` 但存在非隐藏文件）**：
  - 执行基础的 `.auto-agents` 库和配置拉起。
  - **核心动作**：显式创建一个 `run_state.json`，并将其初始状态直接置为：`{"status": "completed", "current_stage": "readme"}` 等完毕状态标识。
  - 这样一来，当紧接着你在这种老项目里执行 `python3 -m auto_agents run` 时，必然会直接命中上面 **第 2.2 条的完成拦截判定**，询问你是否开启迭代。一旦你选 Y，它就会主动阅读你的老代码并跟你商量你接下来想让它写什么，无缝切入自动工作流！
