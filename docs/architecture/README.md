# FlowTool 架构文档

> 状态：M0 已验收、M1 已实现但真实 E2E 待验收 · 初始日期：2026-08-02 · M0 验收：2026-08-26 ·
> 文档核对：2026-09-07；M2 Feature 1 持久反馈批次已实现并通过 PostgreSQL 自动化。

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
| 配置与领域契约 | 已验证 | serve/worker 独立配置、v6 task/result/tool/recovery message、批次/item-version/context digest 身份、分类 tool error 和 Flow lifecycle/delivery 分轴有覆盖 |
| PostgreSQL Store | 已验证（自动化） | flow/attempt/job、binding、结果账本与反馈版本/buffer/batch 事务、并发和恢复通过实际 PostgreSQL 测试 |
| Webhook Ingress | 已验证（M0） | `/healthz`、HMAC 拒绝/接受、后台交接和 202 响应有覆盖；health 仅表示进程可响应 |
| Event Router | 已验证（M2 F1 自动化） | 反馈 delivery/版本/deadline 原子写入，持久 settle、强信号授权、显式 action、同 Flow 冻结批次串行和重启恢复已覆盖 |
| Control/Worker 边界 | 已验证（Features 1–3） | 多 worker 鉴权/调度、Flow 生命周期 binding、tool multiplex、busy reconnect、result replay 与 journal secret 负向覆盖 |
| Control Service / Job Coordinator | 部分 | 真实 issue/default branch prepare、tool fencing/幂等 PR delivery 已接线；thread 映射、取消 kill、冲突处理与真实副作用 E2E 未完成 |
| Codex / Context / Sandbox / VCS | 部分 | Flow repo、短时 credential helper、STDIO MCP、Git/jj commit/clean/remote 双端核验有自动化；GitHub-only egress 与真实 run 未验证 |
| GitHub Client | 部分 | githubkit 0.16 App/installation 认证、Issue/repository/ref/PR、单仓库 Contents capability 与错误映射有隔离测试；真实 App smoke 和 M2 review threads 待验证 |
| Judge | 已验证（adapter 自动化） | bub 0.4.1 受控无工具单次调用、有界冻结上下文、严格判定、直接答复及持久恢复已覆盖；真实 provider 语义 smoke 待执行 |
| Reconciler | 桩 | 5 分钟循环存在，`_sweep()` 目前只记录 active flow，不拉取远端状态 |
| CLI | 部分验证 | `serve` 与 `worker` 双进程 smoke、help 和 health 已验证；flows/show/cancel/retry 尚无 CLI 级覆盖 |
| 插件 | 部分 | 五个生命周期 hook 已接线并隔离异常；post-merge-summary 仍只写日志 |

## 当前验证基线

- pytest 覆盖协议、角色边界、远程执行闭环与真实 PostgreSQL 持久层场景；
- 2026-09-07 M2 F1 本机临时 PostgreSQL 完整测试为 196 passed、0 skipped；外部模型/GitHub 使用替身；
- `uv sync --locked`、`ruff check app tests`、`mypy app tests` 与 `flowtool --help` 通过；
- PostgreSQL-backed `flowtool serve` 与两个 worker 在 CI 中独立启动，并在 serve 重启后自动重新注册；
- 测试不访问 GitHub、Codex 或 bub，也不使用真实凭据和真实轮询等待；M1 Feature 3 的自动化为
  95 passed、22 个 PostgreSQL 场景因本机无数据库而 skipped。

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
- [可派生 Flow 编排](derivable-flows.md)：Flow 独占执行环境、未来派生、上下文/skills 与父级验收；
- [架构可视化](diagram.html)：手工维护的状态图，信息以本页和代码为准。
