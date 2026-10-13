# Persistence 职责与事务边界整理

**状态：** 已实施并完成本地验证；独立审查与 CI 见 PR。
**PR：** [#82](https://github.com/b1indsight/agentflow/pull/82)。
**Bookmark：** `refactor/persistence-simplification`
**基线：** `main` / `f601472a`（已合并 [PR #81](https://github.com/b1indsight/agentflow/pull/81)），2026-10-13。

## 1. 初始计划

### 动机与目标

沿用 [control 简化与归组](control-simplification.md) 的思路：具体对象拥有自己的操作，
调用者直接使用所属对象，删除职责重叠与含糊接口；按实际职责组织代码。

当前 `Store` 的 1537 行混合连接池、事务、建表、Flow/Attempt、交付账本、Job、Worker、
派发准备、租约恢复及事件审计；功能仓储已使用共享 Store，但 Projects/Webhook 的装配
另由调用者重复完成。部分创建、更新及事件写入直接调用 `utcnow()`，绕过构造时注入的
时钟；`ProjectStore.save(**values: Any)` 无法在类型边界限制可更新字段。
依据：[Store 与事务](https://github.com/b1indsight/agentflow/blob/f601472a6133966e0647aa7c666bdd5c71952804/app/persistence/store.py#L210)、
[功能入口](https://github.com/b1indsight/agentflow/blob/f601472a6133966e0647aa7c666bdd5c71952804/app/persistence/store.py#L379)、
[Projects 更新](https://github.com/b1indsight/agentflow/blob/f601472a6133966e0647aa7c666bdd5c71952804/app/persistence/project_store.py#L159)、
[运行时装配](https://github.com/b1indsight/agentflow/blob/f601472a6133966e0647aa7c666bdd5c71952804/app/controler/runtime.py)、
[CLI](https://github.com/b1indsight/agentflow/blob/f601472a6133966e0647aa7c666bdd5c71952804/app/cli.py)。

### 范围、行为与验收

范围为 `app/persistence`、必要的 controler/CLI/telemetry 调用迁移、现有测试适配及当前
架构说明。验收看职责、类型边界、共享事务和实际行为，分别报告生产、测试及文档增删。

- 业务 SQL 归实际功能对象；`Store` 保留共享连接池、显式事务、时钟、初始化和共用锁协议。
- 所有功能对象通过当前 Store 取得连接；事务视图内的功能操作在同一连接提交或回滚。
- 继续使用同步 Psycopg、原生 SQL、`class_row()` 和现有领域模型。
- 数据库表、索引、约束、持久 JSON、wire 消息、返回值语义及错误/重试规则保持现有合同。
- 注入时钟时，Flow/Attempt/Job 创建与更新时间、事件处理时间也遵循该时钟；默认仍使用 UTC。
- 本次迁移内部 Python 路径和业务方法的接收对象，不保留旧路径别名或 Store 转发入口。

### 拟议行为与调用方式

例如，触发流程在 `with store.transaction() as tx:` 内，依次执行
`tx.serialize_issue(...)`、`tx.flows.create_flow(...)`、`tx.jobs.enqueue_job(...)`；
派发调用 `store.workers.assign_job(...)`，交付读取调用 `store.deliveries.get_delivery(...)`。
保留现有业务方法名称，避免同时改变每个方法的含义与命名。

配置、租约窗口和连接池仍由一个 Store 持有。事务外访问连接绑定的 Store 或其功能对象
继续立即失败；根 Store 关闭连接池，事务视图不能关闭连接池。跨对象调用显式使用 `tx`
及其功能入口，不能从事务内回到根 Store 取得另一条连接。

### 技术设计与模块归属

先保持单层模块。各功能已有一个完整实现，无需为单个文件新增目录；未来出现独立的
第二项同类职责时，再按实际依赖决定是否归组。包入口仅保留职责说明，直接导入所属模块。

| 目标模块 / 对象 | 归属与迁移内容 |
| --- | --- |
| `store.py` / `Store` | 连接池、连接借用、事务视图、时钟、初始化与关闭；issue/workflow/job 及 worker 共用锁；功能对象入口 |
| `schema.py` | 集中现有七段完整建表 SQL，按当前依赖顺序初始化；SQL 内容和约束原样迁移 |
| `records.py` | 共用原有插入回读必需记录校验及 Psycopg 原始行类型；模型查询继续使用 `class_row()` |
| `flows.py` / `FlowStore` | 从 Store 移入 Flow 与 Attempt 的创建、读取、列表和更新 |
| `deliveries.py` / `DeliveryStore` | 从 Store 移入 FlowDelivery 与 FlowToolCall 的读取、更新、去重及账本操作 |
| `jobs.py` / `JobStore` | Job 创建/查询/取消、归档 payload、生命周期与未决检查；控制端结果入库、未应用结果分页与完成提交 |
| `workers.py` / `WorkerStore` | Worker 连接/断线/列表、Flow execution binding、派发/start/heartbeat/resume/fencing、Worker 结果接收与 assignment 完成、准备重试及租约恢复；接收原 StopStore 的 drain 操作 |
| `stops.py` / `StopStore` | 原 `stop_store.py`；持久停止意图、发送授权和停止回执，使用当前事务的 Job/Worker/Delivery 对象 |
| `results.py` / `ResultStore` | 原 `result_store.py`；冻结结果计划、有序 effects、回执和恢复，明确区别于 Job 的原始结果 inbox |
| `reviews.py` / `ReviewStore` | 原 `review_store.py`；反馈版本、批次、上下文和线程事实继续作为同一审核生命周期管理 |
| `webhooks.py` / `WebhookStore` | 原 `webhook_store.py`；接收、领取、重试和保留策略；同时接收 Store 的 events 去重与审计方法 |
| `reconciliation.py` / `ReconcileStore` | 原 `reconcile_store.py`；PR 与反馈对账的持久进度、hint 和期限 |
| `projects.py` / `ProjectStore` | 原 `project_store.py`；Projects 投影、配置 fencing、同步检查点和独立 session advisory lock |
| `metrics.py`、`readiness.py` | 前者由 `metrics_store.py` 改名，后者保留；继续分别拥有指标读取和只读就绪探针 |

`Store` 以 `flows/deliveries/jobs/workers/stops/results/reviews/webhooks/reconcile/projects`
提供实际功能对象。沿用当前 `store.results` 的按需视图方式，不缓存绑定到根 Store 的
子对象；`copy(self)` 创建事务视图时，各入口都绑定到该视图。无需新增 Protocol、基类、
Mixin、通用 repository factory、动态转发或依赖容器。

跨表操作按完整的不变量归属：Worker 派发与恢复继续在一个函数、一个事务内维护
Worker/Job/binding；Job 完成结果继续原子更新 inbox、Worker slot 和 binding；停止回执
继续联合检查未应用结果及未决 effects。不按表拆开这些提交。
共用锁仍显式使用当前连接，保留 worker → workflow/job/binding 的既有顺序、
`FOR UPDATE SKIP LOCKED`、token/epoch fencing 和 I/O 后状态复查。

`ReviewStore` 的反馈、批次与线程操作共享版本和消费规则，本次保持一个对象；移出 schema
已能分离建表与运行操作，无需按文件行数增加多个相互转发的审核仓储。

Projects 更新保留局部字段写入及 `WHERE id/config_key` fencing，使用 `TypedDict` 和
`Unpack` 描述实际调用的可写字段：`content_id/item_id/cursor/scan_incomplete/add_started/`
`blocked/last_error/failures/next_at`。继续使用 `sql.Identifier` 构造列名，不允许通过该入口
更新配置身份或 desired/synced version，也不改成整行保存，以免覆盖刷新期间的新事实。
依据：[实际更新组合](https://github.com/b1indsight/agentflow/blob/f601472a6133966e0647aa7c666bdd5c71952804/app/controler/reconciliation/projects.py#L77)。

时间读取统一到所属 Store 的 `now()`；保留 `clock` 的实际注入用途，删除各业务模块直接
读取全局时钟的分叉。SQL 驱动原始行的动态值类型留在数据库边界，由现有模型解码，不引入
为了隐藏驱动类型而增加的转换层。

### 方案比较、成本与风险

仅改目录和文件名的迁移成本最低，但不能解决 Store 同时拥有全部业务 SQL，以及时钟和
Projects 写入边界的问题。通过继承或 Mixin 保留全部旧方法可以减少调用迁移，却继续保留
大对象的职责混杂和隐式方法来源。为每张表新增 repository 又会拆散原子操作。
选择沿用现有功能仓储的显式组合，新增四个有实际 SQL 所有权的对象，迁移调用者。

拆分增加少量对象入口和文件，收益是 SQL 与状态合同各有唯一归属。主要风险是新对象绑定
错误连接、事务退出后仍可使用、锁顺序变化、漏迁移 monkeypatch/动态路径、静态类型变弱，
以及统一时钟影响既有 fake-clock 场景。通过事务/并发回归、针对性边界检查、原始 SQL/schema
对照及类型检查控制风险；必要时先保留复杂操作的完整函数，再做局部简化。

### 实施顺序

1. 原样集中 schema 和原生行辅助，保留初始化依赖顺序；明确 Store 的共用连接及锁合同。
2. 迁出 Flow、Delivery、Job、Worker 完整操作，增加绑定当前 Store 的功能入口，并同步调用者。
3. 迁移现有功能模块与 import/monkeypatch 路径；将 events 审计归 Webhook、drain 归 Worker，统一 Projects/Webhook 装配。
4. 统一时间来源并收紧 Projects 更新字段；复用既有回归，只补保护实际行为缺口的最小测试。
5. 更新当前架构、目录和实施记录，完成验证与复杂度复查；在同一 bookmark 和 PR 交付。

### 验证与消融

- **行为验证：** 复用 Store、workflow transactions、fair assignment、preparation retry、stop/cancel/drain、result recovery、archive recovery、review/feedback/thread、Projects、webhook inbox 和可观测性回归。重点核对同一事务跨对象回滚、并发领取与 issue 去重、过期 epoch 拒绝、已接收结果不被恢复覆盖、取消/合并后的派发与停止、未知远端写入保留证据。
- **新增测试：** 只在既有覆盖不足时补最小回归；注入时钟覆盖实际创建/更新时间。事务视图或锁绑定若存在缺口，使用真实 PostgreSQL 与行为场景验证，不测试属性数量、接口计数或简单转发。
- **结构对照：** 比较迁移前后完整 schema、复杂跨表操作的 SQL、默认时钟下的持久模型/JSON 合同；迁移各入口和方法接收对象后，确认目录边界与 wheel 包含全部实现。
- **完整检查：** 使用 `FLOWTOOL_TEST_DATABASE_URL` 运行 PostgreSQL 全量 `uv run pytest`，不得以数据库跳过代替验证；运行 `uv run ruff check app tests`、`uv run mypy app tests`、`uv build --no-sources` 及差异检查。依据：[CI](../../.github/workflows/ci.yml)、[数据库 fixture](../../tests/conftest.py#L58)。
- **消融适用性：** 本次删除职责混杂并收紧边界，没有新增调度、恢复或锁机制需要独立消融；不为纯移动虚构性能实验。实现后、自动审查前检查每个新增对象和辅助是否有实际调用与独立职责，撤去无收益的间接层并重跑受影响检查。若发现必须新增运行机制，先记录方案变更再确定相应行为对照。

### 未决问题与后续

没有阻碍审批的未决问题。事务锁继续由 Store 统一维护，功能模块之间只通过当前 Store 的
实际对象协作；字段更新和异常处理在上述边界内按最少改动处理。
异步数据库改造、ORM、历史迁移、新调度策略、数据库索引性能调优及领域模型拆分留待独立需求。

## 2. PR 变更时间线

<!-- 初始计划提交后，按实际发生顺序追加实质需求、决策及实施记录。 -->

### 2026-10-13 — 独立仓储与共享事务实施

**边界与取舍。** 用户讨论并认可数据与持久化操作分离：领域类型保留字段、校验和自身纯规则，
功能仓储拥有数据库操作；事务涉及多种实体时，由完整操作共同维护，不为每个表增加仓储。
本条明确初始方案的类型边界，继续使用既有领域模型，不引入 Active Record 或绑定实体副本。

**实施。** Store 的 53 个操作迁入 Flow/Delivery/Job/Worker/Webhook；Store 从 1537 行
收敛到 196 行，保留连接池、事务视图、时钟、完整初始化、共用锁和十个功能入口。
现有七段 schema 原样移入 `schema.py`；原生行和必需记录校验复用 `records.py`。
七个 `*_store.py` 按职责改名；events 审计归 Webhook，drain 归 Worker，Projects/Webhook
通过当前 Store 装配。controler、CLI、telemetry、live smoke 与恢复 bundle 脚本同步迁移。
Projects 更新使用九个实际可写字段的 `ProjectionUpdate`，继续局部写入及 config fencing；
创建、更新和事件时间统一使用 Store 时钟。当前架构、目录、CHANGELOG 与历史代码链接同步更新。
依据：[共享 Store](../../app/persistence/store.py)、[Worker 操作](../../app/persistence/workers.py)、
[Projects 更新](../../app/persistence/projects.py)、[当前组件职责](../architecture/components.md#47-storepostgresql)。

**测试与适配。** 新增两项最小回归：注入时钟覆盖创建/更新/事件，以及事务子仓储不能在退出后
取得连接。迁移前分别因时间来源与不存在功能入口失败，实施后通过。原有模拟状态以有 spec 的
Mock 提供分组 API，不增加生产兼容入口；模拟 Job 集合使用 `job_records`，避免混淆真实 JobStore。
迁移现有 patch 路径、运行时替身和历史查询替身；保留所有既有用例及跨表/并发回归。
依据：[时钟回归](../../tests/test_store.py)、[事务回归](../../tests/test_workflow_transactions.py)、
[模拟入口](../../tests/persistence_fakes.py)。

**验证。** PostgreSQL 全量 **736 passed、无跳过**，保留既有 Starlette 弃用提示。
`uv run --no-sync ruff check app tests scripts/smoke/recovery.py`、`uv run --no-sync mypy app tests`
（177 个源文件）、`uv build --no-sources` 及差异检查通过。全量命令为
`uv run --no-sync --env-file .cache/2026-10-13/persistence-test.env pytest -q --tb=short`，该本地
env 文件提供 `FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres`；
fixture 仍为每项集成测试创建独立 schema。wheel 包含全部 16 个 persistence 模块，独立
`.venv/bin/python -I` 从 wheel 成功导入 15 个实现模块（不计包说明入口），无旧路径模块。
七段 schema 的完整文本与执行顺序一致；53 个迁移操作和 87 个既有操作在统一接收对象、
等价功能入口、必需记录辅助名称与已批准时钟来源后，业务 AST 一致。Projects 的 typed/nonempty
更新入口为明确变更，单独由类型检查及既有同步/丢失响应回归验证。

**复杂度与消融。** 四个新增对象各自实现真实 SQL；功能入口直接构造当前 Store 的视图，
没有旧方法转发、缓存子对象、Mixin、工厂或新增运行模式。复杂跨表操作保留完整事务和锁顺序，
数据模型继续独立使用；这次没有新增调度/恢复机制需要消融。上述结构对照和真实行为回归验证
迁移与时间来源，类型检查约束局部写入字段，不增加接口计数或实现镜像测试。

**变化。** 生产（app 与 scripts）增加 1967 行、删除 1815 行，净增加 **152 行**，用于功能对象入口、职责模块和明确类型边界。
测试增加 1091 行、删除 1015 行，净增加 **76 行**，新增两项回归，其余为入口/替身适配。
文档增加 226 行、删除 12 行，净增加 214 行；统计相对本 PR 的 `f601472a` 基线。
