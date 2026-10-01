# collab 091707202dc3 审查协议与诊断身份修复

现场命令：

```sh
auto-agents collab --project /home/fuli/projects/sdgp --provider codex-fuli0110 --auto-approve --session 091707202dc3
```

2026-09-30 18:37 的停止包含两个连续问题。

1. 候选恢复完成验证后，独立审查返回 `APPROVE`，但
   `coverage[].nodes` 使用变更 ID 或观察证据摘要，而不是测试路径。
   第一次审查只覆盖四项；格式纠正补齐 185 项，但仍全部使用摘要。
   `repair_v2.controller.review_result` 因而拒绝该协议，抛出
   `approval needs concrete test coverage for every requirement`。
   当时 manifest 的 schema 未描述 nodes 格式，纠正诊断只解释变更覆盖和
   findings，没有指出测试覆盖中的非法 nodes。
2. 自修复诊断请求没有稳定的故障证据身份。异常分支没有
   `controlled_failure` 时，辅助调查按原会话、角色和通用 `diagnosis`
   目的共用 anchor。历史 investigator/reviewer 已各使用两次额度，新的
   审查协议错误因此在模型调用之前被拒绝，保存
   `Auxiliary role exhausted its bounded evidence review`。

关键证据：

- `/home/fuli/projects/sdgp/.auto-agents/runs/_commands/135d00774154/events.jsonl`
  的 `diagnostic.exception` 保留了审查阶段的完整异常栈。
- `/tmp/auto-agents-candidate-ghvwadvq/project/.auto-agents/recovery-reviews/ba43b551454efce90a6a9ef3e5c15769a0da768db133e72801d937078827d0e1:format.json`
  是第二次审查的原始响应。
- `/home/fuli/projects/sdgp/.auto-agents/runs/session-091707202dc3/root-cause/ed24f2952a68/reviewer-incomplete.json`
  保存了辅助诊断额度拒绝原因。

修复使 schema 与提示显式说明测试节点格式，纠正消息指出非法测试覆盖行；
本地校验继续拒绝摘要、绝对路径、路径穿越及无关前端测试。
调查调用使用根因证书的证据、源码和策略摘要作为 logical_call_id，
不同故障分别计数，同一证据的临时快照路径变化复用持久响应。
总调用预算、未知结果对账、独立审查与候选交付约束仍生效。

现场协议复现输出位于
`/tmp/autoagents-collab-091707202dc3-protocol-replay.json`。
这只是只读协议复现，不构成项目候选的审查、验证或交付。

回归覆盖：

- `tests/test_review_manifest_contract.py`：哈希与具体测试节点的区别、
  声明的前端目标、纠正消息中的非法行。
- `tests/test_recovery_native.py`：实际 journal/outbox 的候选恢复、
  非法覆盖纠正后交付、再次恢复不重复调用。
- `tests/test_recovery_auxiliary.py`：历史通用额度耗尽后新证据仍可诊断，
  同一证据复用响应，未知结果不能因快照路径变化重新调用。
- `tests/test_root_cause.py`：coordinator 将相同证据身份绑定到各独立角色。

测试日志保存于 `/tmp/autoagents-collab-091707202dc3-*-tests.log`。
本次复核还覆盖恢复收敛、修复控制器、provider 与提示证据相关回归，
共 188 项测试及 2 项子测试通过。
WSL 存储检查在受限沙箱内无法调用 PowerShell；根因与修复控制器测试
在沙箱外重跑通过，没有修改生产存储检查逻辑。
