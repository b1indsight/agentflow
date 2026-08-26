# M0 收尾与验收计划

> 状态：已实施 · 日期：2026-08-26 · 基线：`main@origin` (`e65d534b`)
>
> 本计划收尾 [`m0.md`](m0.md) 已定义的 M0 范围；不扩展到真实
> GitHub/Codex/bub 闭环。实施前需在 draft PR 上获得明确批准。

## 1. 目标与边界

计划制定时，M0 脚手架已经能够安装、启动 CLI 和常驻服务，`GET /healthz` 也已实测返回 200；
但仓库没有测试，Ruff 有 5 个问题，Mypy 有 20 个错误，因此仍不能按原 M0 计划验收。

本次目标是建立一套可重复执行的 M0 验收基线：

- 用自动化测试证明配置/契约、SQLite 持久层、webhook 验签与路由、job worker/恢复骨架；
- 修正测试暴露的 M0 范围内缺陷，并让 Ruff、Mypy 全部通过；
- 复跑 CLI、服务启动和 `/healthz` smoke check；
- 将 README、架构状态和原 M0 计划同步为实际验证结果。

明确不在本次范围内：

- 校准 githubkit API、GitHub App token 或真实 PR/review 操作；
- 校准 bub 调用、真实 Codex 编码/续接/push 闭环；
- 实现 reconcile 的远端对账、post-merge-summary 插件或 M1–M5 功能；
- 重构既有架构、引入异步 SQLite 或 CI 平台。

## 2. 技术方案

### 2.1 测试隔离

- 在 `tests/` 建立 pytest 测试，全部使用 `tmp_path` 下的 SQLite 数据库。
- 通过 settings override/缓存复位或直接装配测试对象，避免读取开发者真实 `.env`、home 目录和凭据。
- GitHub、Codex、Judge、插件和 engine 边界使用小型 fake/stub；不访问网络、不启动真实 agent。
- worker 测试直接驱动单次 dispatch/仓储状态转换，mock 时间与 sleep，保持确定、快速。
- 回归测试只覆盖能证明真实风险的最小场景，并说明触发机制和实际场景。

### 2.2 契约与持久层

覆盖配置解析、核心枚举/Pydantic 契约和 SQLite schema/repository 行为，重点验证：

- `FLOWTOOL_` 配置、逗号 allowlist 和路径展开；
- active flow 唯一约束，同时允许终态 flow 后重新触发；
- flow/attempt/job 的创建与往返、当前 attempt 关联；
- job enqueue/claim/heartbeat/finish，以及 stale running job 重新排队；
- webhook delivery 去重与 review item upsert/relay/resolve 状态。

当前 `id: int | None` 在“尚未持久化”和“已经从数据库返回”的对象之间混用，造成大部分
Mypy 错误。实施时在持久化边界做显式校验/收窄，让需要数据库 ID 的路径只接收已持久化对象；
不使用大范围 `type: ignore`，也不为通过类型检查而改变领域语义。

### 2.3 Webhook 与路由

通过 FastAPI 测试客户端和隔离的组合根验证：

- `/healthz` 返回 200；
- 缺失/错误 HMAC 返回 401，正确 HMAC 秒回 202；
- bot 事件和重复 delivery 不产生重复 flow/job；
- allowlist 用户给 issue 添加 `flow` 标签时创建 PENDING flow 和 initial job；
- 去标签或 `/flow stop` 将 active flow 取消；
- review/comment 只落到当前 attempt，并触发可控的 settle 调度。

若当前全局 settings 单例或模块级 app 妨碍隔离，只做最小的装配/依赖注入调整，使生产行为不变、
测试能提供临时配置和 fake 组件。

### 2.4 Worker 与运行时 smoke

验证 worker 能 claim queued job、调用 engine、成功置 DONE、异常置 FAILED，并在启动时恢复 stale
job。测试不等待真实轮询间隔。随后执行真实 CLI/runtime smoke：

1. `flowtool --help`；
2. 使用临时数据库启动 `flowtool serve`；
3. 请求 `/healthz` 并确认 200；
4. 关闭服务并确认正常清理。

Ruff 的现有 5 项问题随其所属模块修正；Mypy 错误随行为和测试对应的 change 修正，最终运行全量
`pytest`、`ruff check` 和 `mypy`。

## 3. 计划中的 change stack

计划 change 始终保留为栈底。获批后，按下列自然功能边界实施；如果测试证明两个边界无法独立保持
可运行状态，可在不制造破损中间态的前提下合并：

1. **`test(core): validate M0 contracts and persistence`**
   - 增加配置、模型、Store 测试；
   - 修复持久化 ID 收窄、Store 返回类型和相关 Ruff/Mypy 问题；
   - 结果可独立证明数据地基与崩溃恢复数据操作。
2. **`test(webhook): verify signed event ingestion and routing`**
   - 增加 HMAC、health、dedup、allowlist、取消与反馈路由测试；
   - 只做测试隔离所需的最小组合根/调度调整；
   - 结果可独立证明 webhook 到持久化 job 的 M0 路径。
3. **`test(worker): complete the M0 runtime acceptance`**
   - 增加 worker dispatch/失败/恢复测试，完成剩余静态检查修复；
   - 执行完整自动化与真实服务 smoke；
   - 同步累计 M0 状态文档，使“已验证/待验证”与最终结果一致。

每个行为修复与保护它的测试放在同一 change；不会按文件类型拆出测试提交，也不会为了固定提交数量
保留无独立价值的 change。

## 4. 验证标准

以下条件全部满足才视为 M0 收尾完成：

- `pytest` 收集到实际测试且全绿；
- `ruff check app tests` 通过；
- `mypy app tests` 通过；
- HMAC/health/store/worker 的 M0 验收场景由自动化测试覆盖；
- `flowtool --help` 成功；临时数据库下 `flowtool serve` 可启动且 `/healthz` 返回 200；
- 测试不访问网络、不使用真实 GitHub/Codex/bub 凭据，不依赖 sleep；
- 最终 diff 只包含 M0 验收所需代码、测试和文档。

## 5. 文档影响

实施阶段预计同步：

- [`README.md`](../../README.md)：更新“已验证/待验证”和可运行的验证命令；
- [`m0.md`](m0.md)：保留原计划作为决策记录，追加实际完成状态与验收结果；
- [架构文档](../architecture/README.md)：仅更新里程碑状态和仍存在的集成风险，不改架构决策；
- [`diagram.html`](../architecture/diagram.html)：同步可视化中的 M0 状态、测试缺口和验收 callout。

仓库目前没有 changelog、文档索引生成器或 CI 配置，本次不为 M0 收尾新增这些体系。
本计划文件作为实现决策记录保留；若实施与计划发生实质偏差，会在这里记录。

## 6. 实施结果

计划范围已完成，无实质性范围偏差。HTTP 测试具体使用 `httpx.ASGITransport` 驱动 FastAPI
lifespan，而不是已弃用的同步 TestClient；这保持了原计划的隔离目标，并避免测试依赖警告。

最终结果：

- 19 个 pytest 测试全部通过；
- `ruff check app tests` 与 `mypy app tests` 通过；
- `uv sync --locked`、`flowtool --help` 通过；
- 使用临时 SQLite 启动真实服务后，`GET /healthz` 返回 200，服务正常关闭；
- 测试未访问 GitHub/Codex/bub，也未使用真实凭据或真实轮询 sleep。

测试优先实施共暴露并修复：allowlist CSV 的 source 解码错误、持久化实体 ID 的可空类型扩散、
worker heartbeat 取消后未等待清理。随后两轮审查补充修复了 heartbeat task 异常向 dispatch 传播，
以及单次 heartbeat 持久化失败会终止续租的问题；对应回归测试把最终测试数增至 19。

真实 GitHub/Codex/bub 集成仍按 M1+ 和 V1–V9 跟踪。持续性的 heartbeat 持久化失败目前会无限重试，
尚无失败上限或 worker unhealthy 信号，作为后续运维风险保留。
