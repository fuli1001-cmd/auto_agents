# Auto Agents 0.2：单一业务内核

`run`、`fix`、`collab` 和 `provider-resolve` 现在使用同一个控制器。原来的
Orchestrator、Session、Workflow 和后台发布修复循环已经删除，Python 文件只留下
薄入口。独立监督程序只处理有证据的引擎异常，不决定业务路线。

## 执行与恢复

业务只有六种状态：READY、RUNNING、WAITING、BLOCKED、COMPLETED、CANCELLED。
唯一状态来源是项目本机的 `.auto-agents/state/business.sqlite3`。目标、源码、权限、
验证命令和额度以不可变合同保存；分类和规划只能补全尚未绑定的范围和验证。
模型提供建议和产物，不能修改状态、额度或执行身份。

每次外部操作先登记稳定 ID，再记录结果。已确认结果可以重放；DISPATCHED/UNKNOWN
必须对账，不能换 provider 重发。明确的额度或不可用错误才允许降级。
修复是否继续由真实验证改善和连续无进展判定，原有调用上限始终有效。

流程分别为：

- run：澄清 → 原型（有前端）→ 架构 → 计划 → provider 文档 → 依赖任务 →
  README → 实际验证 → 视觉审查（有前端）→ 证据检查 → 交付。
- fix：分类 → 实施 → 实际验证 → 独立审查 → 证据检查 → 交付。
- collab：诊断 → 自己拥有的 run/fix 子任务 → 原目标验收 → 独立审查 → 交付。
- provider-resolve：文档研究 → 独立审查 → 交付；不接管产品代码。

子任务使用独立工作目录，结果以 Git 差异整合。并行只选择无交叉的路径范围；
根任务会重新验证整合后的结果。模型和测试写入限制在自己的目录，测试不能改共享
项目或账号。验证拒绝零测试、全跳过和被 shell 退出码掩盖的失败。

原型审批始终需要用户决定，`--auto-approve` 也不会略过。审批锁定整个原型、设计
和 manifest 的实际字节。秘密输入保存在 operator 存储，业务数据库保存引用。

## CLI

原有命令名称和参数保留。常用运行方式：

```bash
auto-agents run --project /path/to/project --provider configured-name --auto-approve
auto-agents fix --project /path/to/project --goal '具体问题' --auto-approve
auto-agents collab --project /path/to/project --goal '原始目标' --auto-approve
auto-agents collab --project /path/to/project --session existing-id --auto-approve
auto-agents resume --project /path/to/project --workflow workflow-id
```

`--session`/`--workflow` 继续原任务和原额度；新目标创建新任务。显式 `--provider`
覆盖保留的降级选择，不会重新授予额度。`run --provider` 和 `--doc-language` 仍会
保存默认设置。`--max-tasks` 在当前调用完成指定数量后暂停，下一次继续剩余任务。

不带会话选择参数的 `collab`、`fix` 和 `provider-resolve` 每次创建新任务，监督程序也
创建新作业；不会因命令文字相同而恢复历史失败。`run` 默认续跑唯一未完成的产品任务，
不选择发布检查或原型变体任务；存在多个候选时要求明确指定 ID。业务和监督共用内核的
只读启动选择结果，恢复按实际根任务身份关联，修改 provider 或日志参数不会重置额度。

批准或拒绝后用原运行命令继续；回答默认自动续跑，`--no-resume` 只保存回答。待决定的子任务可通过根任务 ID 定位。

```bash
auto-agents approve --project /path/to/project --session existing-id --gate prototype
auto-agents reject --project /path/to/project --session existing-id --reason '具体调整'
auto-agents answer --project /path/to/project --session existing-id --from-env ANSWER --no-resume
auto-agents cancel --project /path/to/project --session existing-id
auto-agents business-status --project /path/to/project --json
```

`prototype generate/list/preview`、`inputs`、`sync-agent-instructions`、
`audit-requirements`、persistence 配置、worker/cluster 和 storage 工具仍是独立的领域
能力，不含另一套恢复控制器。发布检查是同一数据库中的只验证任务；后台检查失败
保留诊断，不会自动编辑代码。`attest` 只接受完整发布证明。

## 监督

`execution.supervision.mode` 默认为 `auto`。安装监督程序后，执行业务命令会自动
启动它；`--no-supervisor` 或 `mode: off` 可独立运行。业务运行不依赖 Docker，
监督修复的隔离验收需要 Docker。修复使用命令指定的 provider 或 active_provider，
以及 `efforts.self_repair` / `self_repair_review`，没有 repair_provider 配置。

监督程序通过公开快照、checkpoint、resume-check 接口工作。离线验收不带凭据、
不连接网络、不调用模型，必须真正经过原阻塞步骤并到达后续边界。只是在同一
步骤遇到“下一次模型调用”不能算修复成功。业务问题、验证环境问题、权限问题和
未知调用结果会停止并保留证据，不能借此自动编辑引擎。

代码修复和业务交付使用独立 Git 索引及 CAS 更新，保护用户已有的暂存内容。
两台电脑有分叉时，监督程序仅整合可通过原固定检查的合并；冲突保留并停止。
业务代码交付发现共享 HEAD 前进也会保留候选，不擅自覆盖。监督程序是否发布由 `execution.supervision.publish` 控制；配置允许且有 remote 时可发布通过固定检查的整合。

## 一次迁移

先升级本机安装，再检查和迁移每台电脑自己的状态：

```bash
auto-agents migrate-state --project /path/to/project --check --json
auto-agents migrate-state --project /path/to/project --json
```

`already_current` 表示已经使用新格式。`migrated_with_blocks` 表示迁移成功，但有
历史任务缺少或冲突证据，这些任务保留为 BLOCKED；不会伪造完成或重发未知调用。
未实施阶段允许原任务要求新增的回归；已验证阶段缺失的测试不会被当成新测试跳过。迁移保留共享仓库的交付基线与私有候选差异，保存原始数据库备份，验证过的进度、原问题和原命令保持不变。不把运行中数据
当成跨电脑共享数据库，也不使用旧的维护作业恢复内核。

## 自动回收

已确认调用的私有 HOME、缓存、响应临时目录及时删除。完成任务的工作目录和不再
被活动任务使用的私有 Git 引用回收；大段重复数据库结果压缩为审计记录，并保留
操作 ID、调用计数、合同与最终证明。未知调用和活动任务的证据不回收。

监督程序清理自己创建并有归属标签的容器、沙箱和旧工具镜像，保留当前工具配方和
活动租约；不删除用户镜像、基础镜像、源码、媒体、账号或数据库。不做 Docker
全局 prune，也不压缩 WSL 虚拟磁盘。
