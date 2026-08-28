# FlowTool 架构文档

> 状态：M0 已验收、M1+ 仍为目标设计 · 初始日期：2026-08-02 · M0 验收：2026-08-26 ·
> 文档核对：2026-08-28。

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
| 配置与领域契约 | 已验证 | CSV/JSON allowlist、路径展开、枚举和结构化 Judge/Codex 结果有自动化覆盖 |
| PostgreSQL Store | 已验证 | flow/attempt/job 往返、跨 Store 可见性、并发 delivery 去重/claim、active 唯一约束、review 与恢复有覆盖 |
| Webhook Ingress | 已验证（M0） | `/healthz`、HMAC 拒绝/接受、后台交接和 202 响应有覆盖；health 仅表示进程可响应 |
| Event Router | 已验证（M0） | delivery 去重、bot/allowlist、触发、取消和 inline review 入库有覆盖；同 flow 串行锁尚未实现 |
| Worker | 已验证（M0） | claim/dispatch、DONE/FAILED、heartbeat 清理、瞬时续租失败重试和启动恢复有覆盖 |
| Flow Engine | 部分 | 状态转换和外部端口调用已写；issue 拉取、thread 映射、取消 kill、冲突处理与真实副作用未完成 |
| Codex / Context / Sandbox / VCS | 部分 | CLI 命令构造、prompt、workspace 拷贝和策略接缝存在；commit/push、网络与凭据未端到端验证 |
| GitHub Client | 桩 | githubkit 调用形态已写，认证、REST/GraphQL 方法与权限尚未在真实 App 上校准 |
| Judge | 桩 | `bub run` subprocess 与保守解析存在，实际命令/API 和模型输出尚未校准 |
| Reconciler | 桩 | 5 分钟循环存在，`_sweep()` 目前只记录 active flow，不拉取远端状态 |
| CLI | 部分验证 | help、serve 和 health smoke 已验证；flows/show/cancel/retry 命令已写但无自动化覆盖 |
| 插件 | 部分 | 五个生命周期 hook 已接线并隔离异常；post-merge-summary 仍只写日志 |

## 当前验证基线

- 测试套件包含 24 项 pytest，其中持久层场景使用真实 PostgreSQL；
- `uv sync --locked`、`ruff check app tests`、`mypy app tests` 与 `flowtool --help` 通过；
- PostgreSQL-backed `flowtool serve` 在 CI 启动后 `/healthz` 返回 200，并能正常关闭；
- 测试不访问 GitHub、Codex 或 bub，也不使用真实凭据和真实轮询等待。

M0 在 2026-08-26 使用 SQLite 和 19 项测试完成原始验收；该记录保留在
[`m0.md`](../plan/m0.md)与[`m0-closeout.md`](../plan/m0-closeout.md)。当前 PostgreSQL 替换不证明
完整产品闭环，也不表示控制面/worker 已经分离。

## 阅读导航

- [总览](overview.md)：定位、运行形态、平面边界与组件总览；
- [组件职责](components.md)：端口、适配器、状态机与持久层；
- [端到端流程](workflows.md)：M0 已验证切片与目标闭环；
- [关键决策、风险与短板](decisions.md)：已定方案和开放问题；
- [路线图](roadmap.md)：M0–M5 里程碑与扩展预留；
- [GitHub App 接入](github-app.md)：认证、权限、Webhook 和密钥；
- [Review 修订循环](review-loop.md)：triage、revise 与并发语义；
- [架构可视化](diagram.html)：手工维护的状态图，信息以本页和代码为准。
