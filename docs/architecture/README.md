# FlowTool 架构文档

> 状态：M0 已验收、M1+ 仍为目标设计 · 初始日期：2026-08-02 · M0 验收：2026-08-26 ·
> 文档核对：2026-09-02。

本目录是 FlowTool 架构的唯一入口。代码、测试、配置示例和已执行的运行验证是“当前实现”的事实
来源；目标流程不会因为已经写入设计文档就被视为可用。

## 如何阅读

- 本页的状态表描述当前实现，是架构文档中的 current truth；
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
| 配置与领域契约 | 已验证 | serve/worker 独立配置、v2 task/result/recovery message、时间关系验证和结构化结果有覆盖 |
| PostgreSQL Store | 已验证 | flow/attempt/job、epoch/lease fencing、result inbox、跨 Store 可见性、并发 assign、review 与恢复有覆盖 |
| Webhook Ingress | 已验证（M0） | `/healthz`、HMAC 拒绝/接受、后台交接和 202 响应有覆盖；health 仅表示进程可响应 |
| Event Router | 已验证（M0） | delivery 去重、bot/allowlist、触发、取消和 inline review 入库有覆盖；同 flow 串行锁尚未实现 |
| Control/Worker 边界 | 已验证（Features 1–3） | 多 worker 鉴权/调度、affinity、busy reconnect、result replay、serve restart、journal/root lock 有覆盖 |
| Control Service / Job Coordinator | 部分 | 状态转换和外部端口调用已拆到 control；issue 拉取、thread 映射、取消 kill、冲突处理与真实副作用未完成 |
| Codex / Context / Sandbox / VCS | 部分 | CLI 命令构造、prompt、workspace 拷贝和策略接缝存在；commit/push、网络与凭据未端到端验证 |
| GitHub Client | 桩 | githubkit 调用形态已写，认证、REST/GraphQL 方法与权限尚未在真实 App 上校准 |
| Judge | 桩 | `bub run` subprocess 与保守解析存在，实际命令/API 和模型输出尚未校准 |
| Reconciler | 桩 | 5 分钟循环存在，`_sweep()` 目前只记录 active flow，不拉取远端状态 |
| CLI | 部分验证 | `serve` 与 `worker` 双进程 smoke、help 和 health 已验证；flows/show/cancel/retry 尚无 CLI 级覆盖 |
| 插件 | 部分 | 五个生命周期 hook 已接线并隔离异常；post-merge-summary 仍只写日志 |

## 当前验证基线

- pytest 覆盖协议、角色边界、远程执行闭环与真实 PostgreSQL 持久层场景；
- `uv sync --locked`、`ruff check app tests`、`mypy app tests` 与 `flowtool --help` 通过；
- PostgreSQL-backed `flowtool serve` 与两个 worker 在 CI 中独立启动，并在 serve 重启后自动重新注册；
- 测试不访问 GitHub、Codex 或 bub，也不使用真实凭据和真实轮询等待。

M0 在 2026-08-26 使用 SQLite 和 19 项测试完成原始验收；该记录保留在
[`m0.md`](../plan/m0.md)与[`m0-closeout.md`](../plan/m0-closeout.md)。当前 PostgreSQL 替换不证明
完整产品闭环。多 worker 恢复不等于跨 worker session 迁移或 control-plane HA；这两项仍属后续 feature。

## 阅读导航

- [总览](overview.md)：定位、运行形态、平面边界与组件总览；
- [组件职责](components.md)：端口、适配器、状态机与持久层；
- [端到端流程](workflows.md)：M0 已验证切片与目标闭环；
- [关键决策、风险与短板](decisions.md)：已定方案和开放问题；
- [路线图](roadmap.md)：M0–M5 里程碑与扩展预留；
- [GitHub App 接入](github-app.md)：认证、权限、Webhook 和密钥；
- [Review 修订循环](review-loop.md)：triage、revise 与并发语义；
- [架构可视化](diagram.html)：手工维护的状态图，信息以本页和代码为准。
