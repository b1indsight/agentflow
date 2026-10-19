# FlowTool 架构文档

> 状态：M0 已验收；M1–M4 功能及 M5 部署、接收、运维实现已落地，真实 E2E 与部署验收待完成。
> 初始日期：2026-08-02 · M0 验收：2026-08-26 · 文档核对：2026-10-15（最新 main，含 PR #82）。

本目录是 FlowTool 架构的唯一入口。代码、测试、配置示例和已执行的运行验证是“当前实现”的事实
来源；目标流程不会因为已经写入设计文档就被视为可用。

## 如何阅读

- 本页的状态表描述当前实现，是架构文档中的 current truth；
- [会话查询 API](conversation-api.md)描述按 flow 读取 worker Codex 历史的接口、分页与可用性边界；
- [已取消 Flow 手动归档](../plan/cancelled-flow-archive.md)描述控制台展示归档、持久化与恢复边界；
- [总览](overview.md)、[组件职责](components.md)和[端到端流程](workflows.md)同时记录目标设计，
  每处涉及未完成能力时明确标注当前边界；
- [关键决策](decisions.md)记录已定方案、开放风险和退出条件；
- [路线图](roadmap.md)与[计划目录](../plan/README.md)分别记录未来方向和具体实施决策。

状态术语：

- **已验证**：由当前自动化测试或记录在案的 runtime smoke 证明；
- **部分**：已有真实代码，但路径不完整或尚未端到端验证；
- **桩**：接口和调用位置已存在，外部契约或主要副作用尚未校准；
- **未来**：仅有架构预留，没有当前实现。

## 当前实现状态

| 范围 | 状态 | 当前事实 |
|---|---|---|
| 配置与领域契约 | 已验证（自动化） | serve/worker 独立配置、v13 task/result/tool/recovery/stop/conversation 消息、journal v3、修订基线与 session generation、批次/item-version/context digest 身份有覆盖 |
| PostgreSQL 持久层 | 已验证（自动化） | Runtime 管理共享 Database，服务自行创建具体仓储；显式事务与共用锁保护 flow/attempt/job、binding、结果回执与反馈批次；聚合 Store 用于 CLI、live smoke 和测试 |
| Webhook Ingress | 已验证（自动化） | HMAC 验签后、202 前持久化 inbox；后台消费者在每条 receipt 事务内创建服务/路由，提交后通知；失败退避、重放和请求体限制有覆盖 |
| Event Router | 已验证（M2 F1 自动化） | 反馈 delivery/版本/deadline 原子写入，持久 settle、强信号授权、显式 action、同 Flow 冻结批次串行和重启恢复已覆盖 |
| M3 合并与清理 | F1/F2/F3 已合并 | 原 worker 提交清理凭据后，持久恢复 Issue/PR 完成标签和固定归档说明；全部回写后原子成功、释放与 ACK；真实 M3 闭环待验收 |
| Control/Worker 边界 | 已验证（Features 1–3） | 多 worker 鉴权/调度、Flow 生命周期 binding、tool multiplex、busy reconnect、result replay 与 journal secret 负向覆盖 |
| Control Service / Job Coordinator | 部分 | Issue/default branch prepare、tool fencing、INITIAL 成功后创建 PR、原线程回复/保守 resolve 与持久恢复已接线；冲突自动处理与真实副作用 E2E 未完成 |
| Codex / Context / Sandbox / VCS | 部分 | Flow repo、临时凭据、首轮 MCP、修订 resume/一次性 fallback、Git/jj clean/remote/lineage 双端核验有自动化；GitHub-only egress 与真实多轮 run 未验证 |
| GitHub Client | 部分 | githubkit 0.16 App/installation 认证、Issue/repository/ref/PR、单仓库 Contents capability 与错误映射有隔离测试；M2 线程分页、回复、resolve 及竞态恢复有自动化；真实 App smoke 待验证 |
| Judge | 已验证（adapter 自动化） | control 本地 bub 0.4.1 无工具子进程调用；批次冻结上下文、严格判定、直接答复及持久恢复已覆盖；仅 revise 创建 worker job，真实 provider smoke 待执行 |
| PR / 反馈对账 | 已实现、有自动化覆盖 | 独立有界循环读取已追踪 PR 的 merge/close/reopen 及 Issue/PR/review/线程反馈；cursor、退避和诊断持久化，不补建漏失的新 Flow trigger |
| Projects v2 | 已实现、有自动化覆盖 | 可选组织看板配置、资源 API、独立投影同步及 check/status/retry；远端失败不改写 Flow，真实看板待验收 |
| 取消 / drain / 停机 | 已实现、有自动化覆盖 | 持久 stop intent、执行 nonce、进程组取消与停止凭据握手；未知执行/未决 effects 保留占用；drain 不派新 Flow，已有绑定继续收尾 |
| 会话查询 | 已实现、有自动化覆盖 | token 保护的 HTTP API 经 broker/WS 读取原 worker 的 Codex 历史；control 绑定 Flow/Attempt/session 并管理跨 session 分页 |
| Web 控制台 | 已实现、有自动化覆盖 | 单 owner Access 保护的真实看板、详情与持久时间线；支持 cancelled Flow 手动展示归档，保留取消状态和历史，不改写执行或交付；真实 Access 登录与部署仍待验收 |
| 可观测性 / 部署 | 部分 | 结构化日志、受保护 metrics、DB readiness、worker 状态文件、Docker/Compose、Kubernetes 与运维文档已落地；镜像发布、目标集群与恢复演练待验收 |
| CLI / 真实验收入口 | 部分验证 | Flow、worker-drain、inbox、project 运维命令及 `app.live_smoke` 已提供；真实 GitHub/Codex/模型闭环仍须按验收清单执行 |
| 插件 | 部分 | 五个生命周期 hook 已接线并隔离异常；post-merge-summary 仍只写日志 |

装配与运行依据：[ServerRuntime](../../app/controler/runtime.py)、
[事务内 Webhook 服务](../../app/controler/events/webhooks.py)、[Database](../../app/persistence/database.py)、
[协议](../../app/contracts/protocol.py)。对账与运维依据：
[PR 对账](../../app/controler/reconciliation/pull_requests.py)、
[反馈对账](../../app/controler/reconciliation/feedback.py)、
[Projects](../../app/controler/reconciliation/projects.py)、
[取消边界](../operations/cancel-drain.md)、[会话查询](conversation-api.md)、
[部署指南](../deployment/README.md)。

## M2 状态与待办

**F1–F4 均已实现并合并；M2 尚未通过真实环境验收。**

| Feature | 已合并 PR | 实现范围 |
|---|---|---|
| F1 | [#19](https://github.com/b1indsight/agentflow/pull/19) | 持久反馈版本、settle 与冻结批次 |
| F2 | [#20](https://github.com/b1indsight/agentflow/pull/20) | bub 语义决策、有界上下文与直接答复 |
| F3 | [#21](https://github.com/b1indsight/agentflow/pull/21) | 同 repo/branch/PR 修订、交付核验与一次性 session 兜底 |
| F4 | [#22](https://github.com/b1indsight/agentflow/pull/22) | 线程/comment 分页映射、原线程回复、保守 resolve 与持久恢复 |

F4 于 **2026-09-08 00:02:45（Asia/Singapore）**合并，对应 UTC 2026-09-07 16:02:45，
合并提交为 `5fd27f23584e`。[PR #22](https://github.com/b1indsight/agentflow/pull/22)
记录修复 head `0294c003128b` 的 377 passed / 0 skipped，
[对应 CI](https://github.com/b1indsight/agentflow/actions/runs/34140178991) 全部通过；这些结果覆盖自动化场景。

剩余工作：

- **真实验收**：GitHub App smoke、M1 Issue→PR 前置，以及两轮修订、answer/wait/ignore、session
  兜底、重启恢复和真实线程回复/resolve 均缺少真实服务运行证据，按 [F4 验收矩阵](../plan/m2-review-resolution.md#6-自动化与真实-m2-验收)逐项完成。

M4 F1 已修复同秒旧转换重放：历史中已表示的编辑及不能定序的后继保留待核对，只有新鲜
远端观察推进重复转换。原复现、重启/回滚及并发回归见 [F1 实施记录](../plan/m4-feedback-replay.md)。

## 当前验证基线

- pytest 覆盖协议、角色边界、远程执行闭环与真实 PostgreSQL 持久层场景；
- 最近完整记录为 2026-10-15 持久层简化后的 **735 passed、无跳过**，Ruff、Mypy 通过；
  这是既有实施记录，不代表本次文档更新重新运行了完整测试，见[验证与对照](../plan/persistence-simplification.md)；
- PostgreSQL-backed `flowtool serve` 与两个 worker 在 CI 中独立启动，并在 serve 重启后自动重新注册；
- 测试不访问真实 GitHub、Codex 或模型服务；Judge 契约测试运行真实 bub 库连接假模型端点。

真实验收入口和待办见 [M5 E2E 清单](../verification/m5-live-e2e.md)；
部署/恢复检查见[运维手册](../operations/runbook.md)。已有命令、清单和自动化不等于真实环境已通过。

M0 在 2026-08-26 使用 SQLite 和 19 项测试完成原始验收；该记录保留在
[`m0.md`](../plan/m0.md)与[`m0-closeout.md`](../plan/m0-closeout.md)。当前 PostgreSQL 替换不证明
完整产品闭环。多 worker 恢复不等于跨 worker session 迁移或 control-plane HA；这两项仍属后续 feature。

## 阅读导航

- [总览](overview.md)：定位、运行形态、平面边界与组件总览；
- [组件职责](components.md)：端口、适配器、状态机与持久层；
- [端到端流程](workflows.md)：M1/M2 自动化覆盖的切片与目标闭环；
- [关键决策、风险与短板](decisions.md)：已定方案和开放问题；
- [路线图](roadmap.md)：M0–M5 里程碑与扩展预留；
- [GitHub App 接入](github-app.md)：认证、权限、Webhook 和密钥；
- [Review 修订循环](review-loop.md)：triage、revise 与并发语义；
- [可派生 Flow 编排](derivable-flows.md)：Flow 独占执行环境、未来派生、上下文/skills 与父级验收；
- [架构可视化](diagram.html)：手工维护的状态图，信息以本页和代码为准。
