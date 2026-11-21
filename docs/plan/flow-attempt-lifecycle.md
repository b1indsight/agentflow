# 用 Attempt 表示 Flow 的一次开工

**状态：** 已实现，未部署。[PR #190](https://github.com/b1indsight/agentflow/pull/190) · 基线：[main #189](https://github.com/b1indsight/agentflow/commit/007a86ab380bf850edd18983a1a7de8565211a5b) · [需求讨论](https://ampcode.com/threads/T-01a1fd4b-ae11-73c8-a250-42fd27c0b216)。

## 1. 初始计划

### 目标与范围

保留 Flow 的 ID、链接、Issue 归属、任务正文和完整历史。一个 Flow 可以先后开工多次；每次有一个 Attempt，同一时刻至多一个能推进该 Flow。用已有的 Attempt ID 标识这次执行，删除运行时轮号。维持现有 Flow 六态和同一 Issue 最多一个活跃 Flow 的规则。不引入并行方案、第二套 Attempt 状态机或新的会话系统。

今天 `resume` 已经在事务中开启新轮并入队 `initial`，但 Attempt 到准备 `initial` 时才创建；任务以轮号归属，编码会话却以 Attempt 归属。详见 [工作流入口](../../app/controler/workflow/service.py)、[initial 准备](../../app/controler/workflow/initial.py)和[状态账本](../architecture/flow-ledger.md)。本计划把这两个起点合并为一次事务提交。

### 用户可观察的行为

| 操作 | Attempt 和结果 |
| --- | --- |
| 创建 Issue Flow 或直接输入任务 | 返回时 A 已存在，首个 Job 已属于 A；可以还没有 session、分支或 PR。入队失败则整笔回滚。 |
| 修订、重试失败 Job、恢复 worker、编码 session 不可用 | 仍是 A。允许创建替代 Job、换 session 和递增 `session_generation`；不产生开工记录。 |
| 结束 A | 保留既有成功/放弃、PR 合并和清理条件。归档后仍指向 A，详情可查看其结论。补记 merge 不授予编码权限。 |
| 显式重新开工 | 校验请求针对已归档的 A、原绑定已释放且 Issue 没有其他活跃 Flow；原子创建 B、选中 B、清除当前归档投影、重置活动同步游标并入队。Flow ID 不变。并发提交两次，只有一次能开工。 |
| B 运行后收到 A 的消息 | 重放 A 已结算的 Job 不重复产生副作用；旧 PR 的 merge 只更正 A 的历史；还停在 A 的页面不能结束 B。 |

### 数据、接口与代码边界

1. **Flow 和 Attempt。** 保留 `flows.current_attempt_id`；创建事务内部可以暂空，任何可见的新 Flow 必有归属本 Flow 的当前 Attempt。`attempts.id` 是执行身份，`flow_id` 是父任务，`created_at` 为开工时间；会话、修订字段保留。归档事实存账本，`flows.state/archive_*` 继续服务当前查询。移除 `current_round_no` 和 `attempts.round_no`。不让两个表各写一份历史状态。[模型](../../app/domain/models.py) · [表结构](../../app/persistence/schema.py)
2. **事务与调用。** `create_flow(...)` 保持入口不变，在现有事务内复用 `create_current_attempt(flow: Flow) -> Attempt`；`resume(flow: Flow, *, expected_attempt_id: int) -> Flow`、`finish(flow: Flow, *, reason: ArchiveReason, summary: str, expected_attempt_id: int) -> None` 均在锁内核对旧指针。保留 Issue 序列化和现有锁顺序。`initial` 只读取该 Job 的 Attempt；缺失或不匹配不能现场补建。排查原先靠“指针为空”表示尚未准备好的分支，改查其真正前提（例如 delivery 是否打开、session 是否存在）。[Flow 存储](../../app/persistence/flows.py) · [工作流入口](../../app/controler/workflow/service.py)
3. **Job、结果和副作用。** 新增 `jobs.attempt_id: bigint?`；所有带 Flow 的 Job 必填，只允许无 Flow 的独立 inference 为空。保留 `jobs.attempt` 派发计数。约束 `(flow_id, attempt_id)` 必须匹配；派生和替代 Job 沿用源 Job 的身份，命名和 review 推理在入队时冻结来源。`ResultPlan` 从原 Job 取 Attempt，禁止在恢复时从当前 Flow 补填。领取、准备、工具调用、远程操作前和提交后果时分别核对相应身份；不再当前的结果可结算原 Job，但不能写 B 的 session、状态、标签或排队新工作。保留 token、worker epoch、workspace generation、回执去重及已发出操作的对账。[入队](../../app/persistence/jobs.py) · [结果处理](../../app/controler/workflow/results.py) · [副作用](../../app/controler/workflow/effects.py)
4. **交付与资源走不同的路径。** 基线已将当前交付并入 `flows.delivery_*`（[既有决策](flow-event-table-merge.md)）。将这一组字段及完整性约束移至 `attempts`，保留 `DeliveryStore` 接口层，不另建表。`get_delivery(attempt_id: int) -> FlowDelivery | None`、`get_or_create_delivery(attempt_id: int, branch: str) -> FlowDelivery`、`update_delivery(delivery: FlowDelivery) -> None` 都定位具体 Attempt；交付对象增加 `attempt_id: int`。PR 事件按 `(repo, pr_number)` 找原 Attempt，只有当前交付能推进 Flow。重开取消覆盖旧交付的 `reset_round`；新分支按 Attempt ID 消歧（在长度限制内生成），保留同名远端拒绝接管，旧分支不重命名。`flow_executions` 仍按 Flow 约束活跃位置；停止确认、待应用结果和待对账副作用未结清时，不能过早释放或把旧释放动作应用到新绑定。[交付存储](../../app/persistence/deliveries.py) · [资源释放](../../app/persistence/flows.py)
5. **账本、评审与同步。** `console_activity.attempt_id: bigint?` 替换轮号。首次 `created` 携 A，重开记录 `attempt_started` 携 B；`archive/merge/archive_correction` 在每个 Attempt 内各自唯一。按版本重放，不比较 ID 大小或假设 ID 连续；对旧 A 的更正不修改当前 B。同步请求发出前冻结 Attempt 和 PR，返回后复核；worker 历史保留原 Attempt，归属不明的共享 Issue 内容仍为空。review batch、待处理反馈、待交付的用户消息按 Attempt 隔离；首个 Attempt 保留原 Issue 历史，后续用**开工时间**沿用现有的秒级截断，不能把旧评论当作 B 的新工作。检查对账游标、Projects 和 Console 投影以及更新触发器的读取路径。[账本重放](../../app/domain/flow_policy.py) · [活动投影](../../app/persistence/console_schema.py) · [反馈](../../app/persistence/reviews.py) · [同步](../../app/persistence/console_sync.py)
6. **对外契约。** 保留现有 Flow URL 和 finish/resume 路由，输入改为 `expected_attempt_id`，旧表单或失效 ID 拒绝并提示刷新，不猜测新 ID。详情返回当前 Attempt，活动返回可空 Attempt；CLI 改用 `--expected-attempt`、`history --attempt`。页面用“重新开工”、开工时间、分支/PR 和归档结论区分执行，不把全局 ID 当作第几次。同步发布 Console 客户端、CLI 和 API，更新架构与升级文档。Worker 回复仍按 Job/token 路由；只有实际修改 wire 格式才变更协议版本。[API 契约](../../app/contracts/console.py) · [CLI](../../app/cli.py) · [Web 控制台](../../app/controler/web/App.tsx)

### 实施顺序、迁移及回退

1. **先做只读预检。** 在数据库副本盘点 `(flow_id, round_no)`、现有 Attempt、首个 initial、归档/更正、当前交付以及持久化结果。覆盖从未派发的任务、快照起点、无 Flow 推理和活动归属为空。输出能唯一映射、需要补建、存在矛盾三类清单。目前未检查共享数据库，不假定历史轮次一对一。
2. **扩展与回填。** 先加可空字段，保留旧列。唯一匹配时保留已有 Attempt ID；尚未到 initial 准备的轮次按可信创建/重开事实补建。历史 Attempt 的开工时间从原始创建/重开边界恢复，不能直接沿用较晚的编码准备时间。多个候选、有孤儿或缺少可靠时间边界时停止并列出证据，不静默合并或按最大 ID 选择。当前交付按原样搬迁；历史上被覆盖的 delivery 字段仅保留有证据的历史 PR/归档事实，不伪造 branch、head 或完整交付。迁移可重复执行，补建映射、已有活动 key/version、父子引用均稳定。
3. **切换写入和读取。** 先实现开工事务和 Job 归属，再改交付、结果、副作用和资源释放；最后改账本重放、同步、Console/CLI。旧 `MergeArchiveNotice`、冻结计划和已签摘要保留原始字节及版本，按旧格式核验后用迁移映射读取；新消息使用新版本。不得直接改 JSON 后重算 digest 来通过校验。预检、迁移和运行程序用明确的 schema 版本识别新旧库，替换[当前按轮号列判断旧库](../../app/persistence/database.py)的逻辑。
4. **上线门槛。** 在隔离 PostgreSQL 完成冷库、旧库、二次迁移和备份恢复演练。正式切换需另行授权，暂停全部写入方，排空或记录在途 Job/外部操作，备份并验证可恢复，执行回填与一致性检查。核对当前指针、每个 Job 的归属、历史结论、回执 digest 和新旧账本重放结果，再切读和启用约束。验证通过后才删旧列、旧索引和旧运行时协议字段；历史解码可保留。恢复写入前失败则回滚事务或恢复备份并启动旧版本；恢复写入后不直接降级或覆盖新数据，先停写、保存增量，再决定前向修复或对账恢复。

### 替代方案与成本

仅在界面隐藏轮号改动最小，但业务仍需维护两套执行身份。归档后改建新 Flow 更简单，但会改变同一任务的链接与历史入口。保留 Flow 级当前交付并只依赖归档快照能减少迁移，却无法让不同 Attempt 保有各自的完整新交付。因此选择一次移动交付数据和查询；代价是迁移、接口切换，以及读取当前交付多一次到当前 Attempt 的关联。执行位置仍留在 Flow，避免无需求地改动 worker 模型。

### 验收与简化对照

| 代表场景 | 必须观察到的结果 |
| --- | --- |
| 首次入队失败；同一旧 ID 同时重开两次；原 workspace 尚未释放 | 分别是整笔回滚、仅一个新 Attempt/initial、拒绝开工。retry 和 session fallback 前后 Attempt 不变。 |
| A 用 PR 17，B 用 PR 29；A 的结果、PR 合并、分页响应、待发消息和旧 finish 表单分别晚到 | A 的合法历史更正只写一次；B 的状态、PR、session、消息、Job 和 workspace 均不变。B 自己的有效结果仍正常推进；当前已归档 Attempt 的更正仍能更新其当前投影。 |
| 首次 Issue 历史、重开时同秒评论、无归属的共享活动、直接输入且无 PR | 首次输入不丢；重开不消费旧反馈；共享活动不伪造归属；无 PR 仍可按原条件完成。 |
| 旧库含多轮、无 Attempt 和歧义数据，旧回执含轮号，迁移中断再跑 | 无歧义部分保留 ID、历史和 digest；歧义报告可定位且不半切换；重跑不重复创建；新旧投影与历史更正结果一致。 |

扩展现有[状态测试](../../tests/test_flow_states.py)、[迟到 merge 测试](../../tests/test_flow_archive_activity.py)、[同步归属测试](../../tests/test_console_attribution.py)和控制台 E2E。实施后按 CI 运行真实 PostgreSQL 回归、静态检查及 Web 构建、E2E。代码审查前在临时副本做两个简化对照：让迟到结果从 Flow 读取当前 Attempt；去掉重开请求的 expected-ID 校验。相同 A/B 数据应能分别检出错归属和旧请求在 B 也已归档后意外开启 C；若已有其他条件足以阻止，记录实际路径并删除冗余检查。此处是验收设计，尚无执行结果。

### 待确认与非目标

方案本身无前置产品问题。存量歧义数量、需要保留的历史解码版本，以及在途操作能否排空，是上线前必须由预检和演练回答的问题。生产迁移、部署、并行 Attempt 和补造缺失的历史交付均不在本次计划交付范围内。

## 2. PR 变更时间线

### 2026-11-21：一次开工只有一个执行身份

创建和重开都在同一事务内选中 Attempt、追加起点并入队。Job 归属由组合外键和不可变约束保护；`jobs.attempt` 仍是派发次数。交付保存在 Attempt 上。当前交付保留 `get_delivery(flow_id)` 便捷读法，历史由 `get_attempt_delivery(attempt_id)` 读取；创建和更新始终指定 Attempt。会话历史按开工时间排序，通过原 Attempt 的编码 Job 查当时的 worker。工作区仍由 Flow 持有。

迟到的 PR 17 合并能改正 A；若 B 已有 PR 29，B 的任务、交付、归档投影都不变。恢复分页成功与失败都必须复核原 Attempt，旧表单返回冲突。见[历史合并测试](../../tests/test_flow_archive_activity.py)、[分页测试](../../tests/test_reconcile_threads.py)和[跨 worker 历史测试](../../tests/test_single_session_history.py)。

### 2026-11-21：两个身份校验的对照实验

在临时源码副本中使用相同的 PostgreSQL 测试数据和命令，分别比较以下路径：

| 测试（`uv run pytest -q tests/<模块>::<用例>`） | 简化实现的结果 | 保留的约束 |
|---|---|---|
| `test_archive_status.py::test_unprepared_merge_notice_after_reopen_keeps_original_attempt` | `ResultPlan.attempt_id` 从当前 Flow 取值时，A 的通知被持久化为 B（2 ≠ 1）；不需要真的写坏远端即可发现错归属。 | 从原 Job 冻结身份，旧通知不向新执行发写操作。 |
| `test_store.py::test_stale_reopen_after_second_archive_cannot_start_a_third_attempt` | 只移除 `resume` 锁内的预期身份比较，A 的已归档事实仍存在，B 结束后 A 的旧请求成功。 | 即使当前也已归档，仍必须校验收到的 `expected_attempt_id`。 |

原实现通过两项测试。两处校验分别保护持久归属和命令授权，不能互相替代。

### 2026-11-21：停写切换和恢复演练

用基线控制端在本机独立 PostgreSQL 创建 Issue Flow、创建旧 Attempt、取消后重开，另建尚未派发的直接任务。对该库运行 `pg_dump -Fc`、`pg_restore` 到另一空库后，以新脚本依次预检、`--apply`、再次 `--apply`。预检无冲突，首次切换成功，第二次报告 `current, applied=false`；两个 Flow 重放均无差异，三个 Job 分别保留对应的执行归属。[迁移用例](../../tests/test_flow_attempt_migration.py)另覆盖回执、歧义及事务回滚。未读取共享库；正式部署必须重新预检并验证备份，参见[升级说明](../operations/upgrade.md)。

旧活动摘要包含轮号，重复采集时需用原始输入及迁移映射比对，否则同一条历史评论会多出一个版本。旧冻结计划的交付快照则缺少 `attempt_id`：迁移预检和恢复读取共用同一解码方法，按原 Job 补入内存副本。已验证实际读取返回 A 的交付，虽然 Flow 已指向 B；数据库中的原 JSON 和回执摘要保持不变。

最终源码快照通过 PostgreSQL 全量 pytest；Git 后端不适用的 `.jj` 元数据用例按测试设计跳过。Ruff、mypy、前端单测、Playwright、生产构建和含 Console 静态资源的 wheel 均通过。桌面和手机检查了结束表单、收尾、归档详情及重新开工；浏览器使用测试 API 响应，未对真实 GitHub 或生产环境执行写入。

### 控制台不展示内部执行编号

用户通过开工时间、分支和结论区分执行。结束表单、任务信息、执行记录和来源信息不展示 Attempt ID。页面仍记住打开表单时的执行身份，控制端据此拒绝迟到的请求。

### 控制台归档改为单击操作

「归档」直接请求停止当前执行，不再选择结果、填写结论或二次确认。控制台使用现有 `abandoned` 请求并留空摘要，不代用户声明任务成功；合并证据仍由控制端判定。请求使用点击时页面上的执行身份，保留过期请求和重复提交保护。
