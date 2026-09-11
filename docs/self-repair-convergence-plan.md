# Self-repair convergence and private source handoffs

Self-repair retains one serial implementation workspace and chooses its next
step from verification evidence. There is no root-level or candidate wall-clock
budget. A step without meaningful progress can still be diagnosed and stopped.

## Incremental planning and independent review

Experiment schema 7 retains immutable plan revisions, stable step/scenario IDs,
parent revisions, independent review results, bounded repair episodes and
verification schedules. The latest draft survives rejection, malformed replies,
interruption and restart. Complete historical inputs remain in protected artifacts;
the working input supplies the active obligations, previous plan/review, unresolved
feedback and relevant changes. It does not require rereading all history each turn.
A full response or a parent-bound amendment can produce the next complete plan.
Stale amendments and removal of existing acceptance scenarios are rejected.

The controller, rather than a prompt-only convention, distinguishes these actions:

* Quantity overruns are scheduling decisions. Commands remain atomic and all
  acceptance is retained; nine commands do not force another global design.
* Protocol errors identify the field, actual value, constraint and evidence.
  At most two local format corrections are allowed per semantic round, including
  interrupted calls. They cannot change mechanisms or remove acceptance. Historical
  finding and planning-review labels are references, not new blocking finding IDs.
* Independent plan review has at most three semantic attempts for the same
  component/contract/evidence/environment episode. Commit and display-label changes
  do not replenish it. The previous draft remains usable when review artifacts
  require another independent review.
* A code review can identify an implementation error already covered by the approved
  mechanisms and scenarios. Verified scenario/obligation/path mappings allow direct
  code correction without replanning. A new mechanism or scenario needs a local
  amendment and independent review of its effects.
* Count exhaustion alone cannot discard the whole design. One bounded local
  diagnosis must identify a disproved assumption or dependency conflict before
  component/global redesign. Unsupported or exhausted recovery returns a structured
  blocker without manufacturing empty code candidates.

Planning requests have separate identities. A candidate is created only when
writing or `verify_existing` validation is admitted. Planning is not a successful
repair, and format correction, scope reclassification and introduced-regression
repair do not mint achievement credits. Existing cumulative counts remain intact.

Scope decisions remain independent of code review. `required`, `follow_up`,
`not_applicable` and `unknown` preserve their previous meaning. A safety violation
or introduced regression requires explicit disproof to become nonblocking.
Current facts retain their original request/result artifacts and a controller-built
file/import/configuration dependency manifest. Relevant changes invalidate them;
unrelated files need not. Dynamic calls, external imports or incomplete dependency
knowledge conservatively require a fact-level review. Symbol locations help target
inspection, but never authorize reuse of a safety decision in a changed file.
Independent code reviewers receive the previous verdict, unresolved findings and
changes; final integration still reviews the complete repair and interactions.

Small probes still execute in disposable checkouts with a 60-second limit and do
not issue candidate proof. Full probe results remain referenced when the working
input uses an explicit excerpt. Models and provider settings remain unchanged.

Quick selection chooses a negative oracle and a compatible positive control for
each current finding, plus explicitly required safety oracles. Its defaults are
three commands, twelve collected cases and 180 estimated seconds. These are
selection/batching targets, not runtime deadlines or permission to drop necessary
checks. A scenario may name an exact parameter case in `quick_check`; its original
`check` remains expanded acceptance. Unknown collection sizes and mandatory atomic
commands exceeding targets are reported. Commands with different target cohorts,
flags or shell effects are never treated as interchangeable proof.

Schema 6 migration backs up the old state and preserves candidates, histories,
proof schema 4 and old counters. Matching original request/input/result artifacts
can recover a legacy draft and an interrupted review slot; an old approval flag
never manufactures a new approval. New memory artifacts use the existing
transactional restart-copy mechanism. Future-schema state cannot be overwritten.
The existing trusted verification ledger continues to govern all proof reuse;
cross-environment or unproved cross-snapshot reuse is not enabled by this work.

### Offline acceptance and performance

`python scripts/benchmark_repair_incremental.py --experiment FILE --output FILE`
replays archived planning admission and scheduling without model calls, test
execution or job mutations. It reports comparable command counts separately from
plans still needing reference/format correction. It does not approve those plans
or predict end-to-end model speedups. `benchmark_verification.py` independently
measures fresh and warm trusted checks in a fixed isolated snapshot.

Planning request metrics record complete/working input size, prompt size and call
time. History reports include semantic/format counts, revision/fact counts and
blocked episodes. Quick schedules record mandatory exceptions, deferred acceptance,
actual collected-case counts, queue/execution time and certificate hits. Verification
batches are serial; adding their durations to enclosing phase durations would
count the same wall time twice.

The implementation and offline validation do not resume the stopped collab job.
A subsequent operator-requested restart still imports the retained private source
and validates actual engine/project recovery. Faster scheduling or an earlier
bounded stop alone is not evidence of faster successful end-to-end repair.

## Failure evidence and verification

Command output is retained in redacted diagnostic artifacts. Failed checks also
produce structured records containing the command, stage, environment, test
identity, assertion and termination reason. Managed verification returns this
information to its caller, including during candidate generation. Large records
have a complete artifact reference and an explicit count of details omitted from
the inline view; they are not silently reduced to output head/tail fragments.

With an independent component receipt, quick counterexample checks run before
semantic code review. A quick failure prevents review; a semantic rejection
prevents expanded regression and boundary work. After approval, the full active
component and retained regressions still have to pass before component completion.
The original integration, boundary, differential, full-suite and sealing gates
remain required. Pending-candidate recovery uses the same early check/review gate.
Checks belonging to future components
are deferred without deleting their final obligations. Normal and pending
candidate recovery share the same replay/differential prerequisite checks.
Failed prerequisites stop later acceptance stages. Baseline attribution remains
a separate diagnostic and cannot issue candidate progress credit.

Diagnostic logs use their own artifact namespace and do not masquerade as gate
output artifacts. Source mutation during verification invalidates the proof.
Successful certificates retain the existing source, environment and observed-input
checks. New engine revisions reuse the original acceptance mapping when the
request and retained inputs still match; they do not regenerate test names merely
because the revision changed.

## Progress, diagnosis and restart

Experiment schema 7 separates historical achievement from currently reusable
proof. Existing proof schema 4 is not invalidated just by adding progress fields.
Repeated check identities, regrouping, candidate labels and restarting cannot
credit the same achievement again. Review-only resolutions require completed
review and actual passing check identities. Fixing an introduced regression does
not buy another search window. Verified environment preparation is credited once.

Three unsuccessful attempts allow one evidence-driven local strategy adjustment.
Global redesign requires a concrete invalidated assumption or dependency conflict.
A second exhausted window at the same achievement state reports `search_stalled`
and preserves the code and evidence. Stopped and failed jobs can import their
latest checkpoint transactionally; exhausted state and historical credits survive
proof invalidation. The last completed review has a separate receipt so later
pre-review failures cannot overwrite it.

Introduced regressions remain blocking across component transitions. A completed
review can identify another approved component as their repair owner; the serial
scheduler prioritizes that owner when its prerequisites are complete. If the owner
depends on the blocked component, the minimal regression correction remains in
the active scope to avoid a dependency cycle. Routing never approves the rejected
candidate, expands the frozen contract, or earns progress credit. A known owner
can consume the one strategy-adjustment window without regenerating the design.
Its required reproduction is included in focused verification, and the regression
must be explicitly resolved by a completed review.

Executable references extracted from review prose exclude sentence punctuation.
Quoted IDs and parameter contents remain literal, including spaces and periods;
incomplete parameter selections cannot broaden into whole-function checks.
Scheduling and progress identities share the same extraction rules.

Previously generated commands retained in the regression list are migrated only
when the retained review and a native collection failure establish their origin.
The migration records the original command, corrected command, review digest and
failure evidence IDs. Loading, selector preflight, execution and retry guidance
use that correction; historical failure records and progress credits stay intact.

An unchanged candidate can undergo a different component's verification; it does
not need a fabricated code edit. Duplicate attempts in the same component are
still rejected, and equivalent checks cannot earn credit again after regrouping.

Unknown execution failures receive bounded read-only diagnosis of the latest
retained candidate, not the original engine base. Diagnosis reuse is bound to the
source, environment and evidence artifacts. Provider errors are not cached as
permanent source defects. Missing software continues through the trusted recipe
mechanism; an undeclared or unavailable prerequisite reports an environment block.

The command supervisor distinguishes activity from verified stage observations.
Repeated output, CPU activity or rewriting files cannot renew a self-repair
provider's semantic progress lease. Registered verification reports are written
outside candidate storage, and only the selected check identities qualify.
Ordinary workflow timeout policy remains compatible.

Native file-update events can omit patch contents. Their actual workspace state
distinguishes different edits for loop detection, while neither those edits nor
ordinary output renews the trusted semantic-progress lease.

## Candidate ownership and source delivery

Candidate capture and permission restoration open paths relative to an anchored
private directory without following links. Directory-to-file and directory-to-link
replacements record displaced descendants as deletions, preserving index and
worktree distinctions. Restored permissions apply only to private materialized
inodes; shared hardlinks and replaced paths cannot receive those writes.

Session completion records and verified code revisions are separate. Candidate
commits remain in private Git storage and are exposed by `candidate_custody`;
shared HEAD and index are not the delivery transport. A coordinator-generated
`source_descriptor` binds a session handoff to its repository, owner, exact code
revision, contract revision and registered provenance. Subsequent fix children
materialize that source before executing, including during clarification. They
retain independent objects for restart even after the earlier checkout is retired.
Missing, altered or conflicting provenance blocks execution instead of falling
back to ambient HEAD. A verification command alone cannot grant task ownership.

## Compatibility and validation

Explicit interpreter commands are not automatically wrapped in another Conda
launcher. Declared pytest ini overrides are made explicit while retaining
CLI-over-environment-over-config precedence, so configuration discovery and
execution agree even with parsers that read config addopts after ini overrides.

Regression coverage includes middle-of-output failures, large evidence packets,
read-only diagnosis of retained revisions, interrupted and failed imports, review
receipt preservation, progress deduplication, sustained progress versus repeated
output, and proof invalidation on source changes. Public session tests cover
successive private child deliveries, parent restart, retired sources, tampering,
directory replacement races and preservation of foreign content, permissions,
HEAD, refs and index. Performance comparisons must distinguish faster successful
repair from merely stopping unproductive work earlier.

## 2026-09-11：范围纠错、执行保护及可核对的成本

本轮日志中，4 次组件方案审核全部批准，耗时约 19.6 分钟；7 次范围审核约
24.2 分钟。它们不是同一阶段，也不能据此推出整体净加速。已观察到的收益是
两个后续候选复用获批方案，以及已有组件走 verify_existing；没有对照运行，
不计算假设的“节省时间”。

- 范围审核明确要求 obligation_id 等于问题的 causal_obligation_id。缺失字段、
  未知编号、已有编号与绑定不一致分别提供字段、实际值和约束。错误输出先保存，
  最多两次局部格式纠正；纠正不能改变其他结论或证据，不能通过改为 follow_up
  绕过安全问题。次数在调用前持久化，中断与重启不增加预算；耗尽时保存明确阻塞。
- 范围工作输入保留原始要求、合同及当前问题；历史方案和代码审核通过完整输入的
  定位引用获取。范围复验记录具体失效原因。依赖闭包不完整仍要求复验，不能仅凭
  文件未变猜测外部输入也未变。
- 绑定会话从真实 gate executor 入口启动受限验证，覆盖收集和执行。验证工作目录
  位于独立临时目录，旧私有目录位于项目内部时也不授权写入整个项目。旧的无边界
  验证结果通过新的合同上下文指纹失效。远程或未隔离的执行器不能静默替代本地保护。
- 项目验证保留操作员环境和网络连接；环境值不拼入审计命令参数。共享依赖只读，
  私有工作目录、报告和临时目录可写。正常启动使用已有本地沙箱/命名空间机制；
  嵌套执行使用 Landlock 并补充元数据系统调用限制。后者也禁止对私有文件显式
  chmod/chown/修改时间及扩展属性；创建文件与正常内容写入可用。无法安装限制时
  验证失败，不回退到不受限执行。
- 快速、扩展验证都归档逐命令实际耗时、缓存命中和慢命令。缓存查找耗时不覆盖
  真实执行成本。快速选择在同等必需反例和兼容场景中优先选择已有记录的低成本
  命令，不删除完整验收、不拆分原有 pytest 调用的 fixture 集合。
- 实验阻塞状态、详细错误和控制进程终态保持一致；终态返回结束时间及下一步，
  不把最后一个 phase_started 当作仍在执行。

只读成本报告：

```bash
python scripts/report_repair_performance.py \
  --control-root /path/to/repair-control/identity \
  --job JOB_ID --output /tmp/repair-performance.json
```

报告区分阶段耗时、候选结果和可取得的验证命令成本；嵌套阶段不能直接相加。
本次改动不重启 collab，不改动既有作业的源码或验收状态。恢复时仍须由控制器
按可信新版本接续保留候选，并完成必要复验。

验证中的观测兼容性：旧测试曾通过向宿主机 `/tmp` 写文件记录执行。私有 `/tmp`
启用后，这些标记无法被测试主进程读取。相关夹具改为向仅接受预注册标记的本机
测试服务同步发送事件，由夹具保存观测数据；验证进程不获得共享文件写权限。
两份迁移测试文件中的 512 条外层断言保持不变，仍检查必须执行、不得执行以及
基线和候选的实际内容。与保留候选源码的临时合并检查用于验证恢复兼容性，
不会发布未完成验收的候选，也不会改写原作业记录。

## 2026-09-11：批准凭据恢复与减少重复规划

真实作业 `edece04d` 在第 73 轮通过方案和代码审核后，扩展验收失败。
下一轮的方案复用比较把原始探针代码与脱敏后的审核输入直接比较：
普通 `marker.token` 赋值被当作敏感值处理，导致有效批准无法复用。
随后已用完的设计轮次触发停止，终态又显示了已被批准取代的旧拒绝意见。

审核请求现在同时保存原始方案、探针结果和脱敏输入的摘要。复用既校验原始
摘要，也校验脱敏投影；不能仅因两个脱敏字符串相等就批准不同的执行内容。
历史缺少摘要且投影不一致的记录只作为草案，需要新的独立审核。
同一组件、合同、环境及引擎下，已获批草案的凭据恢复最多额外调用一次独立审核，
在调用前持久化用量；拒绝、失败和重启都不能增加该次数或设计/进展预算。
终态区分批准凭据失效与方案被拒，显示实际累计设计次数和最新意见。

已有最新方案时，新引擎不再优先导入更早的 legacy 草案。组件范围相同时，
源码或环境变化优先执行当前草案的探针和独立复审；范围变化仍需方案修订。
增量规划工作输入包含稳定步骤/场景索引、当前问题和未决意见，完整草案通过
明确引用读取。方案审核包含字段和场景差异；范围复验保留原来的反证、事实依据
以及实际变更路径。依赖闭包不完整仍须独立复验，这些摘要不签发安全结论。

最近一次活动组件验收失败的原子命令优先进入下一轮快速检查，避免再次先支付
代码审核成本再发现相同失败。原命令的参数、测试集合及 fixture 生命周期保留，
未授权和未来组件的检查不能仅凭失败文本进入快速集合，完整验收仍然保留。
规划/审核提示同时要求检查已发现兼容问题在相关验收夹具中的同类模式。

只读输入基准：

```bash
python scripts/benchmark_planning_inputs.py --experiment /path/to/experiment.json \
  --request 24454dc963a54cb6a1babde21fb9399f \
  --request 55692ef7d9f14e408f02cc8d547a88b5 --output /tmp/planning-inputs.json
```

这两份真实组件规划输入原有约 64,593 / 97,044 字符；重建后的工作输入约
17,781 / 22,411 字符。完整证据仍可读取，字符数下降不代表模型耗时同比下降。
使用归档 c73 状态和模拟审核结果的隔离准入重放，仅调用范围复验和现有方案
复审，没有重新生成方案。此重放不构成实际候选批准。

保留候选中的共享前置检查、release 策略和收据恢复还存在三处目录外执行标记。
这些夹具改用已有 ExecutionMarker，保留原断言及隔离限制；26 个相关恢复测试通过。
修复单独提交为私有候选 `9c65eefb1c43bfbb86ed396509769cc3ccfa1295`，并快进保存到
停止作业的保留源码。作业仍为 blocked，未重新启动、未变更原审核和验收状态。
主分支优化与该候选的隔离合并检查通过 60 项规划、增量恢复及重启测试。
