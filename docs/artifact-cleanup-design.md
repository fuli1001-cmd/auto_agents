# 全流程文件保留与清理方案

状态：生命周期设计基准。日期：2026-09-07。已接入的命令、生产者与默认保留边界见 [运行说明](storage-maintenance.md)；本文中的建议接口与分阶段目标以运行说明为准。

## 1. 目标与边界

让 auto-agents 在长期执行、失败恢复、进程中断和多项目共享环境时，都能回收自己产生且已不需要的文件。覆盖 `clarify → prototype → design → plan → provider_research → implement → visual_judge → verify → readme`，以及 `fix/collab`、引擎自修复、后台发布和本地/远程验证。

清理的基本条件是：**归属可证明、没有活跃使用者、没有必须保留的引用、达到保留策略**。目录名包含 `tmp`、被 Git 忽略、进程已退出或任务显示 `blocked`，单独都不构成删除条件。

本方案管理文件生命周期，不负责结束业务任务、放弃修复、取消发布、修改批准设计或清除业务数据。释放资源与改变任务状态必须分开。

## 2. 当前实现与缺口

下列是源码中已经存在的行为，不是拟议行为。

| 范围 | 现状与入口 | 缺口 |
| --- | --- | --- |
| 引擎修复控制器 | [repair_control.py](../src/auto_agents/repair_control.py) 保存 SQLite 状态、Git 缓存、运行副本、任务证据；[repair_worker.py](../src/auto_agents/repair_worker.py) 缓存依赖 venv | 没有环境、运行副本和任务大文件的统一回收；安装失败也会留下目录 |
| 本地 gate | [gate_execution.py](../src/auto_agents/gate_execution.py) 在 `finally/close` 中删除运行目录、worktree 和快照 ref | 异常退出可遗留；`short_job_runtime_root()` 目前按目录年龄超过 24 小时删除，缺少统一活跃引用判断 |
| 远程 worker | [workers.py](../src/auto_agents/workers.py) 有任务退出清理、`worker_cleanup_plan()`、`worker_gc()`；[distributed_gates.py](../src/auto_agents/distributed_gates.py) 结束时请求清理 plan | GC 主要检查年龄和进程组，清理任务 JSON 与 artifact archive；没有统一接收确认与跨任务引用检查，也没有完整覆盖环境、镜像和缓存 |
| 会话、阶段恢复 | [session.py](../src/auto_agents/session.py)、[orchestrator.py](../src/auto_agents/orchestrator.py)、[repair_checkpoint.py](../src/auto_agents/repair_checkpoint.py) 保存或删除恢复目录 | 有的恢复点必须跨进程保留；不能用普通临时目录 TTL 处理 |
| 自修复候选 | [self_repair_search.py](../src/auto_agents/self_repair_search.py) 成功时汇总并删除候选目录；健康快照有数量上限 | 汇总、恢复、发布、验收引用需要统一校验后才能回收 |
| 日志与缓存 | 失败验证日志按 7 天清理；baseline、result、timing、requirements audit 的 SQLite 缓存已有年龄/条数裁剪 | 日志未统一检查引用；表中删除记录不等于数据库文件或 `artifact-objects` 已释放空间；其他输出可持续增长 |
| 原型、工具 | [prototype_variants.py](../src/auto_agents/prototype_variants.py) 拒绝原型时删除候选目录；[project_runtime.py](../src/auto_agents/project_runtime.py) 清理安装临时目录 | 批准原型、未决候选、历史工具版本与下载缓存需要不同策略 |
| Git 工作目录 | [git_ops.py](../src/auto_agents/git_ops.py) 有受管 worktree 对账；[release_worker.py](../src/auto_agents/release_worker.py) 有后台发布工作目录 | 目录、Git 注册和恢复 ref 要成组处理；不能直接递归删整个根目录 |

现有局部清理不全部删除重写，而是接入同一套资格检查。迁移后不能继续保留绕过保护规则的独立年龄清理器。

## 3. 覆盖范围与默认保留策略

下列时间和容量都是拟议默认值，可以由操作者配置。时间从 `released_at/last_used_at` 计算；目录 mtime 仅用于展示。所有 TTL 都受第 4 节的引用和租约保护约束。

### 3.1 按阶段登记产物

| 阶段或流程 | 管理对象与典型位置 | 清理时机与必须保留的内容 |
| --- | --- | --- |
| 澄清、设计、规划、README | `.auto-agents/runs/<run>/prompts/outputs`、会话的 prompts/outputs、只读恢复副本、模型响应解析临时文件 | 解析完成、结构化结果及必要原文已持久化后释放临时响应；最终需求、设计、计划、README 属于交付物，长期保留 |
| 原型、视觉验收 | 原型生成 staging/backup、浏览器 profile、下载临时文件、截图、trace、视觉评分输入；`.auto-agents/docs/frontend_prototype*` | 浏览器进程及子进程退出后释放 profile；已批准原型和被验收引用的截图受保护；明确拒绝且无引用的候选保留 7 天；未决候选不按年龄自动淘汰 |
| 供应商研究与调用 | 研究 checkout/cache、解析输入、请求与输出日志、供应商引用锁、调用收据 | 可重建的未引用研究缓存闲置 14 天后回收；锁定版本、调用收据、付费生成结果受保护；清理不能触发重复真实调用 |
| 实施、任务并行、fix/collab | `.<project>-auto-agents-worktrees/`、`.tmp/` 中明确登记的 agent 目录、任务恢复 ref、`.auto-agents/state/workflows/<wf>/checkpoints/` | 合并或保存可恢复候选之后释放隔离 worktree；未提交改动未完整保存时保留；当前 baseline、恢复点、父子 handoff 链受保护 |
| 本地测试与验证 | `.<project>-auto-agents-gate-worktrees/`、`.auto-agents-gate-{runtime,tmp,cache}`、`/tmp/aag-<uid>-*`、验证沙箱、测试专属数据库/服务目录 | 成功结果和声明的证据完整接收后释放 scratch；失败 scratch 默认留 24 小时；任务运行或子进程未退出时保留 |
| 测试证据 | `.tmp-tests/` 中登记的 run 目录、截图、视频、coverage、trace、artifact bundle | 未引用成功原始证据留 7 天，失败诊断留 30 天；仍支撑验收、恢复或发布的证据受保护；不能清空整个 `.tmp-tests/` |
| 引擎自修复 | `repair-control/<identity>/jobs/`、`runtimes/`、`environments/`、`engine.git`、bootstrap、replay checkpoint | 按 3.2 分类；blocked 任务的合同、候选、冻结证据与恢复信息继续保留；仅无使用者且不含唯一证据的失败安装目录可独立回收 |
| 后台 release/publish | `/tmp/auto-agents-release-worktrees/`、release recovery refs、修复控制器 outbox、publication 副本 | 发布/验证完成且结果已保存后释放工作目录；待发布、等待凭据、待解决冲突均保留候选与证明链 |
| worker 与分布式 gate | worker 配置的 `managed_root` 下 `sandboxes/`、`runtime/`、`incoming/`、`artifacts/`、`diagnostic-output/`、`environments/`、`mirrors/`、`package-cache/` | 主控确认完整接收 artifact 后才可回收远端副本；运行中、未确认接收、断连无法核实的任务保留；共享环境按所有使用者判断 |
| 工具、验证缓存、日志 | `.auto-agents/runtime/`、`.auto-agents/cache/`、项目 state 下缓存数据库、用户 verification ledger、`artifact-objects/`、阶段及会话日志 | 当前工具版本与活动验证环境保留；可重建缓存按 TTL/LRU；日志轮转；证明与审计记录独立于可淘汰缓存 |
| 通用辅助文件 | 原子写入遗留 `.tmp`、传输 bundle、provider probe、诊断拷贝、基础设施检查和提示词评估 scratch | 创建时登记，正常退出释放；崩溃遗留经租约核实后 24 小时回收；不要全盘搜索并删除匹配名称的文件 |

worker 默认目录目前优先使用 `~/.local/share/auto-agents-worker`，也支持配置及 fallback；verification 默认在 `~/.local/state/auto-agents/verification`。实际根目录必须由现有配置解析器取得，不能只扫描一个写死路径。

### 3.2 按用途统一策略

| 类别 | 建议默认策略 |
| --- | --- |
| 已释放的纯 scratch | 正常退出即可删除；失败诊断依赖已提取后留 24 小时；启动扫描处理崩溃遗留 |
| 未完成的环境安装、下载、incoming bundle | 无构建租约且无唯一证据，最后活动后 24 小时回收；保留安装失败摘要及脱敏输出 |
| 完整依赖环境与工具版本 | 无强引用，闲置 30 天后进入候选；容量压力下按 LRU 提前回收未引用项；重建需要显式维护步骤，清理本身不安装包 |
| 研究、包、文件索引、可重建验证缓存 | 闲置 14 天；保留有效键、签名/校验规则；清理缓存不能扩大证明复用范围 |
| 不再引用的成功运行大文件 | 7 天；结构化结果、最终验收证据和审计索引保留 |
| 不再引用的失败运行大文件 | 30 天；最后失败原因、尝试历史和必要复现材料保留 |
| 已结束且无引用的恢复 checkpoint、候选工作目录 | 7 天；唯一候选先保存提交或覆盖 tracked/untracked/binary 的恢复包并验证，再释放工作目录 |
| `pending/blocked/waiting_child/retrying` 等可恢复流程的必要数据 | 持续保护；30 天无活动列为 `stale_recovery`，仅报告，不自动放弃或删除；由独立的关闭/放弃流程解除引用 |
| 活动 bootstrap/runtime、操作者配置、锁、状态数据库、最终交付与核心审计 | 自动清理不删除；历史大附件可以按引用关系归档 |

“依赖环境可重建”不意味着随时可删：被某次恢复合同固定指纹的环境是强引用。只有允许重新准备并重新验证的非活动缓存才可淘汰。

### 3.3 默认不纳入自动删除

- 用户已有源码、staged/unstaged/untracked 修改、批准设计、需求与产品文件。
- 项目 `.data/`、业务数据库、真实生成媒体和真实供应商调用结果；仅测试进程创建且登记为独立 disposable 的数据目录可纳入。
- 用户的 Conda 环境、项目 `.conda/.venv`、原有 `node_modules`、全局 pip/npm/浏览器缓存、provider 的 HOME、凭据、SSH 配置和签名密钥。
- 无法证明 auto-agents 所有权的 `.tmp/.tmp-tests` 子目录、旧脚本输出和其他用户的 `/tmp` 文件。

不能从 `.gitignore` 推导删除权限。测试运行优先把 Python、npm、浏览器和工具缓存重定向到本次受管目录，避免事后清理整个用户环境。

## 4. 统一资源登记与引用模型

引入 `ArtifactStore`（名称为提议）和类型适配器。各项目、修复控制器、worker 分别持有本地资源库；全局视图只汇总已登记根目录，不递归搜索 HOME 和所有仓库。资源库位于对应私有 state 下，不能放在被管理的临时目录内部。

每个资源至少登记：

```text
artifact_id, schema_version, kind, lifecycle_class
root_id, relative_path, device/inode, owner_uid, repo_common_dir
project_id, workflow/run/session/task/job_id, generation
created_at, last_used_at, released_at, size_bytes, size_measured_at
state: allocating | live | released | quarantining | quarantined | deleting | deleted
lease: host_id, boot_id, pid, process_start_ticks, token, last_heartbeat
references: owner_kind, owner_id, revision/generation, role, strength
rebuild_recipe_or_archive_ref, pinned_reason, retention_policy_version
```

身份和引用来自协调器状态与受信任的创建入口，不能让模型输出任意绝对路径直接获得清理权限。共享对象需要多个引用；同一物理路径采用唯一身份，不能给父目录和子目录分别登记相互冲突的删除所有权。

创建时先写 `allocating` 记录、获得租约、再创建目录并记录 inode，最后提交 `live`。消费者获取引用和租约后才能访问路径；结果持久化并确认接收后释放。崩溃留在 `allocating` 的资源可由对账任务识别，不能当作未知文件直接删除。

### 4.1 必须保留的引用根

1. 活跃进程、子进程组、worker lease、项目运行锁、未完成的验证上下文；使用 PID 与启动时间、host/boot 身份共同核验，避免 PID 复用。
2. 尚可恢复的 workflow/session/run 及其父子 handoff、当前 baseline、恢复 checkpoint、候选提交、待处理用户输入。
3. 修复任务的所有 subscribers；待验证、待恢复、待发布、等待权限和远程集成的记录。
4. 当前 engine bootstrap/runtime、候选和重放依赖环境；原型/供应商/工具版本锁定。
5. 当前验收、审查、发布、proof、diagnostics 索引引用的文件、Git ref、blob 和归档对象。
6. 操作者显式 pin 的材料。

以这些根做可达性分析，沿 typed reference 传播保护；不用“JSON 里搜索路径字符串”作为唯一判断。历史格式通过专门适配器读取。解析失败、缺失根、未知版本、旧消费者未参与租约协议时，相关范围 `skip_unknown`，不得推断为零引用。

引用分为强引用和可失效缓存引用。强引用必须先完成业务侧解除或可验证归档；缓存引用可由维护事务失效并降级为 cache miss。清理不能使已经报告成功的 proof 继续声称拥有被删除的证据。

### 4.2 活跃判断与并发

心跳过期只触发核查，不直接允许删除；本机同时核验进程/进程组与排他租约。远端进程由该 worker 本机核验，主控不能仅根据失联认定死亡。`cleanup_incomplete` 对相关目录强制保护。

清理器与资源创建、resume、环境构建、发布共用生命周期锁。顺序固定为“资源库维护锁 → 相关项目/plan 租约 → 仓库锁”，均用短时非阻塞获取；现有调用链按同一顺序改造。跨 host 不持有分布式长锁，只在 owner 节点执行并以 generation 对账。

初期可跳过整个正在运行的旧版项目/plan；完成全部消费者租约接入后，才允许删除活动项目里互不相关的已释放资源。

## 5. 清理协议

### 5.1 生成计划

`inventory → reconcile → mark referenced → select eligible → plan`。

计划包含精确资源 ID、物理身份、引用版本、大小及其测量时间、保留原因、拟用的删除适配器、策略版本、预计可释放空间。必须分开报告 `eligible/protected/unknown/error`；空间估算区分逻辑大小与实际分配块，避免重复计算 hardlink 或把 Git 对象估计成马上能释放的空间。

默认查看计划无副作用；内部自动维护使用同一计划生成器。大目录的大小由创建/释放事件更新，后台增量校正；不在每次 status 或 0.2 秒 tick 中全树遍历或重新哈希。

### 5.2 执行与崩溃恢复

1. 获取对应维护权，重新验证资源 inode、generation、引用和租约；变化则跳过，不使用过期计划强行删除。
2. 在资源库中将资源转为 `quarantining`，阻止新消费者获取旧路径。已知纯 scratch 可在相同检查下直接删除。
3. 普通目录移入**同一文件系统、已登记私有根下**的 quarantine；建议保留 24 小时。不同文件系统不做静默 copy+delete；无法安全隔离则延期。Git worktree 使用专用流程，不直接 rename。
4. quarantine 期间由 resolver 支持恢复；需要 resume 的流程先原子恢复资源并重新获取引用，不能拿到半删除路径。引用无法恢复时返回具体缺失资源，不能重跑付费业务来替代。
5. 到期后再次核验、标记 `deleting`、执行类型适配器、记录结果与实际回收量。只有删除完成才标记 `deleted`；留下 tombstone 与审计摘要。
6. 任一步中断，下次按资源状态与物理位置对账；幂等重试同一资源。失败记录 `permission_denied/busy/path_changed/delete_failed` 等原因，不能 `ignore_errors=True` 后报告成功。

计划建议 10 分钟有效，执行仍逐项重验。失败操作采用退避，不立刻重复扫描；同一维护批次失败不进入引擎自修复递归。

### 5.3 类型专用规则

**路径与文件。** 使用根目录句柄和不跟随 symlink 的路径操作检查每一层，拒绝 `..`、根目录本身、所有权不匹配和跨挂载点遍历。符号链接仅可删除已登记链接本身，绝不进入目标；hardlink 不做原地截断或改写共享内容。TOCTOU 防护依赖私有根、生命周期锁和句柄级复核，不能只做一次 `Path.resolve()`。

**Git。** 确认仓库 common-dir、worktree 登记、租约及恢复提交；未保存的 dirty 内容先保留。删除 worktree 使用 Git API，失败即保留并报错；删除受管 ref 使用 `update-ref -d <ref> <expected_sha>` 的比较检查。只处理明确登记的 `refs/auto-agents/...`，不删用户分支，不运行全仓库 `git clean/reset`。ref 删除后让 Git 常规维护回收对象，不使用 `prune --expire=now`；私有 bare repo 的压缩在无使用者时单独限速执行。

**SQLite 与证明 blob。** 通过各存储类的事务删除过期行，随后从全部有效证书/归档引用标记 `artifact-objects`，再清除无引用 blob。WAL checkpoint 与空间压缩只在对应数据库维护锁和可用窗口执行；绝不手删 `-wal/-shm/-journal`。核心状态库不整库删除，缓存裁剪也不得破坏运行中的快照或有效证明。

**日志、证据与归档。** 活动日志由 writer 自己轮转，拟议每段 20 MiB、最近 5 段；更老的普通诊断段按 7/30 天策略处理。核心事件链、调用收据、合同与最终 proof 不按该轮转配额丢弃。大附件压缩归档后先校验 hash 和读取能力，再释放原件；用版本化 locator 映射原 artifact ID 到归档位置，不重写带哈希链的历史事件。消费者还只认裸路径时，先保留原件，不能提前启用归档删除。

**远程 worker。** artifact 传输需增加“主控已持久化、校验通过”的 ACK，包含 job/generation/hash；worker 验证后解除传输保护。远程清理返回 receipt；断连报告 `pending_remote`，不冒充完成。worker 默认本机清理；跨主机维护通过已有认证连接显式选择范围。

**锁与 socket。** 清理器不删除可重用锁文件，避免不同进程锁住同名不同 inode。socket、队列、lease 标记由对应服务核验 owner 死亡后处理；不能当成普通过期文件。配置、cache HMAC key 和凭据保持保护。

## 6. 调度与容量控制

采用“操作结束释放 + 启动时对账 + 空闲维护”组合：

- 操作退出：只处理本次明确释放的 scratch；证据提交、进程清理完成在前。
- 进程启动：只做受管索引对账，前台预算 2 秒，剩余工作排队。
- 空闲维护：每个本地 owner 最多每小时一次；启动和结束事件合并去重，每轮扫描最多 1,000 条、最多 30 秒，保存游标；逐项删除另有超时，不影响状态查询。
- 独立维护进程执行磁盘工作；现有 supervisor tick 仅排队，不执行大目录遍历、压缩、哈希或 pip。
- 机器离线时无自动定时清理保证；下一次 CLI/服务启动补做。额外常驻定时服务作为可选部署，不把它作为正确性的唯一依赖。

建议先采用可调整的软预算：每项目 10 GiB、每个修复控制器 20 GiB、每个 worker 30 GiB、用户共享验证缓存 5 GiB；按文件系统汇总避免重复计费。达到预算或磁盘可用空间低于 10% 时，优先释放纯 scratch、失败安装和未引用 LRU 缓存，目标回到预算的 80%。

预算不覆盖强引用保护。空间仍不足时，报告占用最多的受保护对象及原因；可在新建大型隔离环境之前返回 `storage_pressure`，让流程可恢复地等待。清理器不取消活跃任务、不删除必要恢复证据，也不强行保证绝对容量上限。归档和 quarantine 都可能暂时不释放空间，必须如实计入，磁盘紧张时不先复制大包。

## 7. 操作接口（拟议，当前不可执行）

```text
auto-agents storage status --project PATH
auto-agents storage plan --project PATH --format json
auto-agents storage apply --plan PLAN_ID
auto-agents storage status --scope user
auto-agents storage plan --scope worker --worker WORKER_ID
auto-agents storage pin ARTIFACT_ID --reason TEXT
auto-agents storage unpin ARTIFACT_ID
auto-agents storage restore ARTIFACT_ID
```

`--scope user` 仅指当前用户已登记的资源根；不表示整个 HOME。人工查看 plan 不会删除；策略内自动清理无需逐文件确认。未登记资源只进入报告和迁移清单，不能通过 `--force` 绕过身份、活跃和引用保护。`unpin` 只解除手动 pin，不能解除业务强引用。

结果至少展示：总占用、可立即释放、quarantine 占用、受保护占用、未知占用、扫描完整度、最后维护时间、逐项保留/失败原因。正在扫描的数据标为估算；状态查看不隐式执行 GC。

`auto-agents workers cleanup` 后续委托同一策略；已有 session 删除/清空入口也应增加父子工作流引用检查，避免清掉状态后把资源误判成孤儿。显式删除业务历史与普通 storage 清理继续保持不同职责。

## 8. 历史文件迁移

1. 先只读盘点配置指定的根目录、当前 Git worktree/ref 登记、修复数据库、workflow/handoff、worker job 和 diagnostics 索引。
2. 对能交叉验证 owner、资源类型和引用的历史目录补登记；保留原路径，不移动用户工作。
3. 无 owner 的 legacy 临时文件标为 unknown，即使名称匹配也不自动删除。用户可审查精确清单，再由迁移入口登记为可回收；不能直接批量认领 `.tmp`。
4. 旧进程未使用新租约协议期间，相应项目或全局控制器根保持保守保护。确认进程结束并完成对账后再启用自动清理。
5. 先运行一个观察周期，对比 plan 与实际恢复需要，再开启 pure scratch/失败安装回收，最后启用缓存和证据生命周期。

针对当前 SDGP 会话：`blocked` 修复任务、父会话及 child 的恢复链继续保护；失败 venv 在构建进程退出、超过 24 小时且无唯一内容后可成为候选；当前 controller runtime 因仍在运行而保留。此处仅描述将来策略，本设计没有清理现场文件。

## 9. 实施顺序与模块边界

| 顺序 | 交付 | 验收门槛 |
| --- | --- | --- |
| P0 | 生命周期库、lease/ref API、类型注册、只读 inventory/plan；给各创建入口分配 owner | 可以解释每个候选的来源、引用、大小和保留原因；未知资源不删 |
| P1 | 接入 gate/worker/sandbox 的 scratch 与崩溃回收、修复失败安装、worktree 适配器；替换短路径年龄扫描和 worker 旧 GC | 活跃长任务、断连 worker、dirty worktree 均不误删；纯 scratch 正常退出回收 |
| P2 | engine/worker 环境、工具、包、验证数据库及 blob 缓存；引用图覆盖修复/release/跨项目共享 | 一个项目结束不影响其他项目；cache miss 不伪造证明；数据库与 Git 一致 |
| P3 | 阶段/会话日志、恢复 checkpoint、原型和验收附件、归档 resolver、远端 ACK；迁移旧删除入口 | 历史诊断与恢复能读取归档，证据链可校验，付费调用零新增 |
| P4 | 空闲调度、配额、状态视图、故障注入与观察部署 | 有界资源开销、维护失败可解释且不空转、不阻塞业务 |

建议新增 `artifact_store.py`、`artifact_policy.py`、`artifact_gc.py`、`artifact_adapters/`；业务模块只负责登记、获取、引用、释放，不再各自决定全局删除资格。独立修复控制器继续保持 stdlib-only，GC 核心与元数据协议不得依赖正在被修复的模型执行器或重型业务初始化；Git/归档等较慢操作由独立维护进程处理。

## 10. 必须通过的测试与验收

- 生命周期集成测试：覆盖每个阶段入口及 `fix/collab/self-repair/release`，检查创建、引用、退出、归档、删除的对应关系；正常退出与 SIGTERM/SIGKILL、重启均覆盖。
- 真实并发测试：维护与 resume、环境构建、发布、下载 ACK 同时发生；最多一方持有删除权；新引用不能获得 quarantine 中的旧路径。
- 活跃保护：超过 24 小时但仍活跃的 gate、PID 复用、缺失心跳、旧版进程、遗留子进程和远程失联均不会误删。
- 恢复保护：blocked/waiting_child 的 baseline、partial candidate、untracked/binary 改动、replay checkpoint 保留；最终 proof 仍可读取；不会因为清理而重复真实供应商调用。
- 路径攻击与权限：symlink 替换、根目录替换、`..`、挂载点、hardlink、错误 UID、越权路径全部拒绝或安全跳过；用户环境、源码和 `.data` 内容摘要保持不变。
- Git/SQLite 集成：worktree 移除失败不报成功，ref 更新并发受 expected SHA 保护；事务中断、WAL 活跃、共享 blob、多个证书引用均保持一致。
- 远端 ACK：主控收到但尚未持久化时不可删；ACK 重放/错误 generation/hash 被拒绝；断连后只报告待处理。
- 崩溃注入：`allocating/quarantining/deleting` 每个边界重启可幂等对账；失败重试有退避和审计，资源库损坏时停止删除。
- 性能：构造大索引与大目录，确认 status 不全树扫描、每轮预算有界、配额计算不重复统计、压缩不会拖住 supervisor tick。验证实际占用释放，同时解释 quarantine 和 Git 延迟回收。
- 迁移验收：已知遗留文件可识别、unknown 不自动删除；所有旧清理入口都经过同一保护检查，不出现两套规则互相绕过。

完成标准不是“目录变小”，而是无引用的受管资源能持续回收，同时活跃执行、恢复、验收证据和用户数据均保持可用。
