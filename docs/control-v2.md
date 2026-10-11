# Auto Agents 0.2：单一业务内核

`run`、`fix`、`collab` 和 `provider-resolve` 现在使用同一个控制器。原来的
Orchestrator、Session、Workflow 和后台发布修复循环已经删除，Python 文件只留下
薄入口。独立监督程序只处理有证据的引擎异常，不决定业务路线。

## 执行与恢复

默认屏幕日志显示阶段的实际动作和当前任务目标，启动时显示一次根会话 ID，
暂停或失败时提供完整恢复命令。子任务 ID 和内部阶段名通过 `--log-mode debug`
查看；结构化状态、事件和诊断仍保留完整 ID。失败摘要有长度限制，完整证据保留在
诊断文件；监督器不重复已经实时输出的同一业务失败原因。

业务只有六种状态：READY、RUNNING、WAITING、BLOCKED、COMPLETED、CANCELLED。
唯一状态来源是项目本机的 `.auto-agents/state/business.sqlite3`。目标、源码、权限、
验证命令和额度以不可变合同保存；分类和规划只能补全尚未绑定的范围和验证。
模型提供建议和产物，不能修改状态、额度或执行身份。

控制器通过不可变的 WorkflowPolicy 区分普通任务、已有功能验收和验收阻断修复，
StageSpec 统一约束模式、阶段后继、等待原因、写入范围和验证工作区。仅验收目标
不能创建或恢复开发 run；有实际验收证据、有限路径和精确回归目标的 fix 可以在
原授权内继续。用户明确禁止修改源码时保存阻断，不创建修复子任务。

schema 2 保持原合同序列化与身份摘要，首次写入在项目锁下备份并增设
contract_policies、operation_outcomes；只读状态和启动选择不触发升级。
原始调用结果和后置校验分别记录：路径、协议或证据校验失败不会覆盖已确认结果。
修正规则后可对相同输入、冻结源码和结果证据做本地重验，不再次调用模型或重跑
已有检查。未知调用、被修改的输入以及缺少原始结果的历史失败仍需对账。

文件分类区分保护路径、源码、运行产物和证据。私有数据库、临时工具链接及合法的
未跟踪 Next.js 声明可作为运行产物，但不进入交付。验证目标中的临时脚本仍被冻结。
验收验证固定使用同一运行工作区；其他检查保留候选隔离，模型不能选择验证工作区。

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

原型范围中的新增改动及含 `new` 检查的流程必须绑定当前有效需求，并为每个改动
编号提供新的行为检查。仅保留旧行为的流程可引用冻结历史 manifest 中的编号；
没有需求编号的辅助流程可使用空列表，但必须有已有实现或原型证据及可执行的
`preservation` 检查。当前已失效的编号不会从历史记录重新取得新增改动权限。

流程和检查可使用同一 HTML 文件的查询参数或片段表达状态，例如
`home.html?state=error`。完整状态 URL 会冻结为测试输入，文件定位、修改区域、
几何和保留约束仍绑定到对应 HTML 文件。基线也观察新状态 URL 的原有界面，
防止状态参数绕过外观保护；新增状态断言仍由候选原型实际执行。

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

模型预算分为每个工作项的调用上限，以及可选的工作流总上限。每次预留新的模型
业务调用计一次，失败、取消、重试和 provider 切换都不退回计数；复用已有回执及
本地浏览器检查不新增模型调用。Token、HTTP 请求数量和供应商账号额度另行计算。
`--max-provider-calls` 在新建工作流时设置总上限，恢复旧会话时不修改已接纳预算。

用户明确授权追加当前工作项预算后，使用独立的操作命令：

```bash
auto-agents extend-budget --project /path/to/project --session existing-id --limit 30 --reason '用户明确授权将总调用上限追加到30次，保留已用次数'
```

这是把指定工作项的总上限改为 30 次；已用 13 次时剩余 17 次。命令仅提高该工作项
上限并追加审计记录，保留目标、合同、调用回执、已用次数、重试计数、子任务上限及
工作流总上限；重复同一上限不会再增加额度。它不启动模型或批准待审原型。只有该
工作项自身的调用上限耗尽、且没有其它阻塞时，才恢复为 `READY`；未知调用、审批、
其它验证失败和独立总上限仍分别处理。保存预算后使用原会话运行命令继续。

不带会话选择参数的 `collab`、`fix` 和 `provider-resolve` 每次创建新任务，监督程序也
创建新作业；不会因命令文字相同而恢复历史失败。`run` 默认续跑唯一未完成的产品任务，
不选择发布检查或原型变体任务；存在多个候选时要求明确指定 ID。业务和监督共用内核的
只读启动选择结果，恢复按实际根任务身份关联，修改 provider 或日志参数不会重置额度。

显式恢复在项目锁内重新读取同一任务。监督器缓存的故障只保留为历史观察；只有
当前执行再次观察到引擎故障才进入维护。活跃进程、未知调用和取消保护仍生效。
`capabilities` 和 `auto-agents-watch capabilities --json` 声明兼容特性；不兼容的
核心/监督器组合在派发前拒绝。`business-status.execution` 提供策略、结果校验状态
和下一步动作，新增信息不参与旧 schema-2 身份摘要。

新建 `collab` 未传 `--goal` 或 `--spec-file` 时，交互终端先询问本次目标；项目已有
`spec.md` 也不会跳过询问，该文件仍留在工作目录中作为参考。自动化调用必须明确提供
目标或目标文件。`--session` 继续已冻结的原目标，不重新输入；要更换目标须新建会话。
终端目标支持多行与空段落：Enter 换行，最后在新行按 Ctrl+D 提交；Windows 控制台
使用 Ctrl+Z 后按 Enter。Ctrl+C 取消当前执行；空输入不创建任务、不调用模型。
提示写入 stderr，`--json` 的 stdout 保持机器可读。Ctrl+D 不替换 stdin，后续读取
仍使用原终端。
提交后立即显示目标回执，阶段消息实时刷新；分析尚未完成时也能看到当前阶段。
运行配置按“显式进程环境、项目根 `.env`、启动目录 `.env`”的顺序读取，前者优先。
私有工作区不复制凭据文件或原始运行数据库，配置通过进程环境继承；缺少工作区内的
`.env` 或 `.data` 本身不代表项目没有配置。配置值不加入模型提示或操作回执。

批准或拒绝后用原运行命令继续；回答默认自动续跑，`--no-resume` 只保存回答。待决定的子任务可通过根任务 ID 定位。

供应商明确返回额度不足、且进程已清理完成时，记为 `provider_quota` 的失败调用，保留原有预算与已配置的切换规则。进程未清理完成、超时、中断或连接丢失仍按未确认调用处理，不能根据日志中出现“额度”字样自动重试。

原型范围或行为检查校验失败后，也可用 `answer --value ... --no-resume` 提交修正
意见，再运行原命令继续；这类反馈不要求另有待答问题。它保留原合同、已用调用数、
历史回执和已冻结的保留检查，只消费已确认结束的结果，并重新进入范围准备。
未知调用仍须先核对；审批、预算、迁移及其它故障保留各自的处理入口。

等待时终端会列出每个待回答问题或审批事项及具体处理命令；并行子任务显示各自的 ID。
保存回答后用原命令加 `--session` 继续。普通回答按已保存的输入引用提供给模型，秘密值
通过声明的绑定提供，不写入模型提示；回答被另外修改时会要求核对，而不会悄悄换用新值。
输入变更导致暂停时，可用原 request ID 再次明确回答；新引用替换同一问题的旧引用，
不会重置调用数。通过父会话处理子任务问题时，只选择实际拥有问题的子任务。
并行子任务同时等待时，可通过父会话 ID 配合 `--request-id` 指定一个问题，其他待办
继续保留；无需另建会话。

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

供应商限流、传输断连或服务不可用本身不会启动监督器的引擎修复。产品中的有界
重试由产品负责；有证据的产品缺陷由 `collab` 在原合同授权范围内派生聚焦 `fix`。
监督器只为 `engine` / `unknown` 类故障进入维护流程，业务验收失败不会被自动
改写成引擎缺陷，也不会因此扩张供应商调用额度或重发结果未知的请求。

代码修复和业务交付使用独立 Git 索引及 CAS 更新，保护用户已有的暂存内容。
用户已安装的补丁可以经原会话的有界修复子任务重新验证、独立审查和合入；修复
不会改写原会话合同、源码身份或历史调用。最终交付只接纳与该子任务冻结声明、
验证及审查回执和候选提交逐文件相同的已安装内容；暂存的重叠改动、未经登记的
补丁或审查后改变的内容仍会阻塞。它不放宽普通工作区的脏文件保护。
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
