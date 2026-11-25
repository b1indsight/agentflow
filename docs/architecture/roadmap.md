# 路线图

## 8. 当前目录与里程碑

[组件职责](components.md)说明各包拥有的数据和允许的依赖；[运行走查](workflows.md)从用户操作追到实现和测试。阶段计划保留当时的设计和验收记录，不用阶段编号判断当前是否支持某个操作。

| 历史阶段 | 今天从哪里核对 |
|---|---|
| [M0 骨架](../plan/m0-closeout.md) | 当时使用 SQLite；现有服务使用 PostgreSQL，Flow 事实与状态表在同一事务更新，范围见[事实账本](flow-ledger.md)。 |
| M1 首轮交付、M2 评审修订 | [交付决策](decisions.md)说明 Attempt 与 PR 的归属；[评审循环](review-loop.md)说明反馈如何进入修订及如何保守处理线程。 |
| [M3 归档](../plan/m3-archive-status.md)、[M4 对账](../plan/m4-stage.md) | 已追踪的 PR 通过 webhook 和主动观察收敛。归档、清理工作区与重新开工是不同操作，见[走查](workflows.md)。 |
| [M5 部署与运维](../plan/m5-stage.md) | 仓库提供部署模板、恢复手册和 smoke 命令；目标部署的真实服务验收另记在[验收清单](../verification/m5-live-e2e.md)。 |

## 9. 未来方向（Roadmap）

| 已有起点 | 尚未实现或仍待验收的部分 |
|---|---|
| worker 可提供多个 workspace；绑定包含 identity、位置和 generation，支持同 identity 重连和结果重放。 | 可释放 checkpoint、跨 worker 恢复 session/repo、多实例控制端的 leader election。 |
| 现有 Flow 工具由控制端授权；提交修订沿用当前 Attempt 的交付。 | [派生编排提案](derivable-flows.md)：持久父子关系、spawn/send、等待、父级验收，以及派生深度、并发和预算限制。 |
| 编码 worker 在独立目录中运行 Codex 或 Amp；控制台可以排队追加指令。 | 目录所有权不能隔离恶意进程。每个 Flow 的容器或微 VM、默认拒绝出站流量、向正在生成的 CLI 实时注入指令仍待实现。 |
| [Flow 事实账本](flow-ledger.md)可重放 `flows` 的状态；生命周期插件可订阅既有 hook。 | 扩展到 Job、交付及父子关系的事实；让合并后总结插件实际生成并提交 skill 变更。 |
| 控制台与 GitHub App 有自动化覆盖。 | 真实 App 授权、模型闭环、Access 登录/MFA、目标集群与备份恢复需要环境证据；见[运维手册](../operations/runbook.md)。 |
