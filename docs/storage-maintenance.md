# 文件占用与清理

auto-agents 的执行模块在正常结束时清理本次 scratch；独立维护进程补充处理过期资源和崩溃遗留。它不调用模型、不重新安装依赖、不触发供应商请求，也不结束业务任务。

日常清理只需一个命令：

```bash
auto-agents storage clean
```

它处理当前用户的全部本机已管理资源，包括项目执行、self-repair、worker、共享缓存和可核验的旧修复缓存。不需要 `--scope`、`--project`、预览或确认参数；这些选项不属于 `clean`。后台维护调用相同的清理引擎，只增加每轮时间预算。

`clean` 直接删除符合现有保留期限或容量策略、且没有使用者或引用的资源，不额外放入隔离区等待一天。既有隔离区中尚未过恢复期的对象仍保留。命令遍历本次启动时的全部登记项，不受旧预览接口的 1,000 项上限限制。

它输出 JSON 摘要和详细 JSONL 记录路径，包括 `counts`、`freed_bytes`（文件分配空间回收估算）、`retained_reasons`、`sizes_complete`、`complete`。错误和未完成的检查会明确报告，单个对象失败不会跳过其他对象。报告自身也登记为日志，按日志保留策略清理。Docker 镜像和容器的删除数量单独记录，无法确定的空间不会冒充已回收字节数。

它不调用全局 `docker system prune` 或 `docker builder prune`，不操作业务数据卷、用户源码和凭据，不连接远端 Docker/worker。只有本机已登记且归属匹配的镜像，以及标签和内核租约均匹配的孤立修复容器、副本进入处理范围。

`clean` 不检查 WSL 虚拟磁盘、不执行压缩、不统计宿主盘增量，也不关闭 WSL 或 Docker。

## 查看与手动维护

```bash
auto-agents storage status --project /path/to/project
auto-agents storage plan --project /path/to/project
auto-agents storage apply --plan <上一步返回的id>

auto-agents storage status --scope user
auto-agents storage status --scope repair
auto-agents storage status --scope worker
auto-agents storage maintain --project /path/to/project

auto-agents storage pin <artifact-id> --reason "保留用于问题复现"
auto-agents storage unpin <artifact-id>
auto-agents storage restore <artifact-id>
```

这些命令输出 JSON。`status` 只读登记库，不扫描工作目录、不启动控制器、不删除文件；大小和保留原因是上次扫描的结果。`plan` 更新扫描索引和计划，但不删除业务文件；计划有效期 10 分钟。`apply` 会重新核验资源身份、进程与引用，发生变化的项目跳过。

`--scope user` 汇总当前用户登记的资源；`repair` 和 `worker` 限定本机相应资源。远端 worker 用已有的认证接口执行本机清理，`auto-agents workers cleanup` 可以触发已连接 worker 的维护；不支持新清理协议的旧 worker 会跳过，升级后再维护。原有 `--max-age-seconds` 参数保留兼容，但不会绕过新的按类型保留策略，也不会删除核心 job 记录。

`registered_bytes` 是已测量的登记资源占用；`size_complete/inventory_complete` 为 false 时不是完整磁盘账单。未知历史目录不计入可删除空间。普通目录进入 quarantine 后尚未释放磁盘空间，结果中的 `freed_bytes` 只在删除完成后计入，仍属于基于文件分配块的估算。

## 保留规则

| 对象 | 自动规则 |
| --- | --- |
| tempfile 沙箱、probe、浏览器临时 profile、临时文件 | 原有正常退出清理继续执行；已登记的崩溃遗留无使用者后保留 24 小时 |
| 引擎/worker 失败安装目录 | 无使用者、无相关阻塞保护后保留 24 小时 |
| 可重建缓存 | 闲置 14 天；容量压力下可提前选择无引用项 |
| 依赖环境、历史工具版本 | 闲置 30 天，当前使用或锁定版本保留 |
| 普通独立日志 | 默认保留 30 天；人类可读 run.log 由 writer 按 20 MiB × 5 个备份轮转 |
| 已释放的干净 worktree | 7 天后可回收；明确的 worker plan 结束可提前释放其登记 worktree |
| 阶段输入输出、验收截图、诊断包、恢复点、候选实验 | 作为证据登记；默认保护。只有有明确 disposable 合同且无引用的证据才按 TTL 回收 |
| 核心状态、审计事件、证明索引、操作者配置、密钥 | 不作为普通文件删除 |

兼容接口 `plan / apply` 仍将普通缓存、日志等先移入同一父目录下的私有命名 quarantine，保留 24 小时后删除；期间可用 `restore` 恢复。`clean` 和后台维护对已经符合回收条件的对象直接删除。Git worktree 通过 Git 删除，脏目录不强制清除。SQLite 缓存通过事务裁剪记录，由 SQLite 管理 WAL/checkpoint；只清理不再被有效证书引用的缓存 blob。

证据仍被恢复、验收或发布引用时，原路径保持可读；不会为了降低占用而只留下摘要或把裸路径指向删除后的文件。`blocked/waiting_child` 不等于可以放弃恢复。跨项目共享环境要等所有使用者释放；同一个 worker 进程的不同线程也分别记录使用权。

引擎修复中，`cancelled` 也不表示候选可以删除。保留候选直到成功的后续作业已接续
其提交历史、完成本地回写，并在私有 Git 仓库中持久保留交付提交；仅有 JSON 标记
不足以解除保护。发布或原业务进程仍有引用时继续保留。解除引用后的干净登记
worktree 才进入现有 7 天保留策略；不会在修复成功的瞬间删除仍需恢复的源码。

主控完成 artifact 文件及目录的持久化后发送接收 ACK，worker 校验 job ID 与 archive hash。无 ACK、断连、残留子进程、无法解析的状态和未知引用均保留。清理失败返回逐项原因，不伪报成功。

## 自动维护与配置

登记库默认在 `~/.local/state/auto-agents/storage`，遵循 `XDG_STATE_HOME`；`AUTO_AGENTS_STORAGE_ROOT` 可指定其他私有目录。

正常命令启动、结束以及长时间运行服务的轻量定时器会请求维护；每小时最多启动一次。磁盘工作在独立 Python 进程执行，每轮约 30 秒，登记项、旧缓存和 Docker 维护分别分配预算；增量继续未扫描资源。没有常驻进程时，下一次命令启动补做。手动 `clean` 不采用这一后台批次时间限制；两种入口共用内核锁，避免同时回收。

默认软预算：project 10 GiB、repair 20 GiB、worker 30 GiB、user 共享缓存 5 GiB。超过预算或所在磁盘剩余空间低于 10% 时，优先选择未引用旧缓存，目标回到预算的 80%。预算不能越过保护规则；受保护数据可以使占用超过预算。

可在登记库根目录的 `policy.json` 配置字节预算和保留天数，例如：

```json
{
  "budgets": {"project": 10737418240, "repair": 21474836480, "worker": 32212254720, "user": 5368709120},
  "retention_days": {"scratch": 1, "incomplete": 1, "cache": 14, "environment": 30, "log": 30}
}
```

`AUTO_AGENTS_STORAGE_MAINTENANCE=off` 关闭自动调度，仍可手动查看和执行。`AUTO_AGENTS_STORAGE_DISABLED=1` 关闭新产物登记及自动调度；不改变已登记资源的持久状态。

## 旧文件与版本升级

本机制从生产者登记开始保护资源，**不会因名称像临时目录就追溯删除历史文件**。原型批准文件、用户源码与改动、`.data`、真实媒体、用户 Conda/node_modules、全局包缓存和 provider HOME 均不自动认领。

旧 self-repair 的 `evidence`、`working-evidence`、`continuous/target-evidence` 中的 Next.js 构建输出由专门的适配器核验：控制器目录及文件必须属于当前用户，操作者配置与目录匹配，任务已停止且没有存活进程/进程组，目录具有构建产物特征，Git 确认它被忽略且没有任何跟踪文件。执行前再次核验身份、锁和引用；活动任务、原始源码、当前 V2 冻结输入及候选均不因此删除。

开始删除旧缓存前持久保存其目录身份；中断后即使构建标志文件已被删掉，也能在重新核验任务、Git 和原目录身份后继续清理。替换后的目录不能使用旧删除凭据；单份缓存失败不阻止其他可清理项。验证副本的 source 已删除、target 尚有残留时，也能在确认内核租约和 Docker 挂载均已释放后单独回收 target。

旧候选和唯一恢复证据继续保留；缓存适配器不会删除整个旧任务目录。具体 job 的日志按该 job 的恢复、订阅和发布状态判断，不再因同一个控制目录中存在无关阻塞任务而全部保留。共享运行时的跨任务保护继续有效。

旧控制器目录从登记库关联记录和当前配置的 `repair-control` 根发现，不遍历整个 HOME 或 `/tmp`。显式使用独立 `AUTO_AGENTS_STORAGE_ROOT` 时，仅处理该登记库关联的旧目录或显式配置的 `AUTO_AGENTS_REPAIR_CONTROL_ROOT`，不会认领另一套安装的数据。

历史资源在相应生产者重新使用并核验后可以登记；无法确认用途的旧文件保留。旧进程没有热加载能力，需在下次启动后使用这些生产者钩子。维护不会主动重启现有业务或清理其唯一恢复证据。

详细生命周期设计与保守迁移原则见 [全流程清理方案](artifact-cleanup-design.md)。
