# Flow 事实账本与状态表

> 状态：已实现（自动化验证），真实部署待验收 · 设计 2026-11-15，实现 2026-11-17。
>
> `flows` 的每次业务列变化都在同一事务内追加一条 Flow 事实；`flowtool flow verify` 重放事实并与
> `flows` 比较。实施过程、取舍和验证见 [feature record](../plan/flow-activity-ledger.md)。

## 1. 目标与范围

目标是让每个 Flow 的业务事实只有一个可信来源：

- **账本**：`console_activity` 中由业务事务写入的 Flow 事实行，只追加、不可改；
- **状态表**：`flows`，是账本在同一事务内更新的投影，业务判断、加锁和唯一约束继续读它；
- **其他状态库**：Job、交付、反馈、Worker、对账进度等表保持各自权威，不由账本重建。

范围只覆盖 `flows` 这一张表。按顺序重放某个 Flow 的事实，得到与 `flows` 当前行在比较列上一致的结果。

不在范围内：

- 重建 `jobs`、`flow_deliveries`、`review_*`、`workers` 等表；
- 把租约、心跳、claim、分页游标、退避时间写入账本；
- 把账本当作灾难恢复手段。其他表丢失后，只凭账本无法恢复执行与交付；
- 改变控制台时间线、HTTP API、Worker 协议或 GitHub 行为。

## 2. 数据分类

PostgreSQL 仍是唯一运行时数据库。全部表按职责分为五类，每张表只属于一类：

| 类别 | 含义 | 表 |
|---|---|---|
| Flow 事实账本 | 只追加的业务事实，Flow 状态的唯一可信来源 | `console_activity` 中 `source='flow'` 且 `kind` 属于[事实目录](#6-事实目录)的行 |
| Flow 状态表 | 账本的同事务投影，供判断、加锁和约束使用 | `flows` |
| 业务状态与专用证据 | 各自领域的权威事实，账本只引用其 ID | `attempts`、`jobs`、`flow_deliveries`、`flow_executions`、`flow_tool_calls`、`execution_stops`、`result_applications`、`result_effects`、`events`、`review_versions`、`review_buffers`、`review_batches`、`webhook_inbox`、`issue_flow_owners`、`runtime_settings`、`project_destinations`、`worker_file_links` |
| 协调数据 | 谁占用什么、处理到哪里、下次何时处理；不是业务事实 | `workers`、`flow_reconcile`、`feedback_reconcile`、`project_projections`、`console_sync`、`console_installation_backoff`，以及 `jobs` 的租约、心跳、准备和核验退避列、`webhook_inbox` 的消费 claim 列 |
| 展示投影与采集归档 | 只为控制台读取服务，不参与业务判断 | `console_flows`，以及 `console_activity` 中的其余行 |

几点说明：

- `jobs` 同时含业务事实（种类、终态结果、接收与应用时间）和协调列（lease、heartbeat、
  assignment token）；`webhook_inbox` 同时含原始事件和消费 claim。两张表整体归“业务状态与专用证据”，
  其中的协调列按“协调数据”对待。
- 协调数据并非都可以随意丢弃。例如 `workers` 的 generation 参与派发围栏。它们不进入账本的原因是
  不描述 Flow 的业务进展，而且写入频繁。
- `events`、`jobs.result_payload`、`result_applications`、`result_effects`、`flow_tool_calls`、
  `review_versions` 已经是各自领域的只追加证据。Flow 事实只保存它们的 ID，不复制正文，
  避免同一事实存两份。
- `console_activity` 是混合表。本设计只把其中一部分行定义为账本，其余行的写入与读取保持不变，
  见[第 3 节](#3-账本之前的实现)。

## 3. 账本之前的实现

实施前，`console_activity` 有四类行，依据见[建表脚本](../../app/persistence/console_schema.py)、
[ActivityStore](../../app/persistence/activity.py)和[推理通知](../../app/persistence/inference_notices.py)：

| 行 | `source` / `kind` | 写入方 | 性质 |
|---|---|---|---|
| Flow 证据 | `flow` / `archive`、`merge`、`archive_correction` | 业务事务调用 `ActivityStore.append_flow` | 不可变事实；触发器拒绝更新与删除，每轮每种至多一条 |
| 状态快照 | `flow` / `state`、`baseline` | 数据库触发器 `console_refresh` | 展示数据；正文是 `console_facts` 视图的前后快照 |
| 推理通知 | `flow` / `inference_exhausted` | `InferenceNoticeStore.append` | 按批次保留最新版本的通知 |
| 采集内容 | `issue.*`、`pr.*`、`worker` 等 | 控制台同步器与 webhook 观察 | GitHub 与 Worker 内容的展示版本，按 digest 去重 |

PR #171 已把 Flow 状态写入收拢到 `FlowStore.transition(flow, FlowEvent)`，合法转换由领域函数
`decide_flow_state` 决定；但除归档外，转换只更新 `flows`，记录仍由触发器事后生成的 `state` 快照代替，
不知道是哪个事件引起变化。标题、命名 Job 指针和 attempt 指针仍由各处直接 `UPDATE flows`。

其他相关事实：

- 每个 Flow 的行按 `version` 严格递增。`version` 在锁住 `console_flows` 对应行后分配，
  与该行的 `activity_version` 同事务更新。
- `console_activity.flow_id` 的外键指向 `console_flows`。`flows` 插入时，触发器在同一事务内
  创建 `console_flows` 行，所以新 Flow 的第一条事实可以紧跟插入写入。
- 控制台时间线查询排除 `source='flow'` 的全部行。

## 4. 账本

### 4.1 哪些行是账本

账本是 `console_activity` 中 `source='flow'`、且 `kind` 不是 `state`、`baseline`、`inference_exhausted`
的行。种类有四组：

- **起点**：`created`（新 Flow）、`snapshot`（账本之前的存量 Flow）；
- **状态转换**：`FlowEvent` 的值，如 `job_enqueued`、`result_failed`、`round_resumed`；
  其中 `ROUND_ARCHIVED` 由已有的 `archive` 证据行记录，不另写一行；
- **归档后证据**：`merge`、`archive_correction`；
- **标题**：`titled`。

`FlowEvent` 的值因此是持久化的种类名，不能改名。

### 4.2 行格式

事实沿用表的现有列，不新增列：

| 列 | 事实行中的含义 |
|---|---|
| `flow_id`、`version` | 所属 Flow 与该 Flow 内的严格递增序号；重放顺序即 `version` 顺序 |
| `round_no` | 事实所属轮次；`round_resumed` 属于它开启的新轮次 |
| `key` | 事实身份：`archive`、`merge`、`archive_correction`、`round_resumed` 为 `flow:<id>:round:<n>:<kind>`；起点为 `flow:<id>:<kind>`；其余为 `flow:<id>:fact:<version>` |
| `kind` | 事实种类，决定如何改变 `flows` 行 |
| `occurred_at` | 业务时间：即该次写入的 `updated_at`；`archive` 的该值同时是 `archived_at` |
| `observed_at` | 数据库写入时间 |
| `author` | 固定为 `FlowTool` |
| `title` | 供 CLI 阅读的固定中文标题，不参与重放 |
| `body` | 空。任务原文只在详情中读取，不进入活动正文；起点事实用 `task_digest` 记录它的 SHA-256 |
| `metadata` | 该种类的结构化字段，见[第 6 节](#6-事实目录) |
| `digest` | 内容摘要，沿用现有计算方式 |

事件名本身就是起因，所以不再单独记录 `cause`。首期不记录 `job_id`、`pr_number`、webhook 投递 ID
或操作者，理由见 feature record。

### 4.3 约束

- **不可变**：触发器 `console_preserve_evidence` 拒绝更新和删除任一账本行；判断方式与
  `ActivityStore` 一致，按“除展示快照和通知外的 `source='flow'` 行”识别。
- **唯一性**：`created`、`snapshot` 每个 Flow 合计至多一条（`ux_console_flow_origin`）；
  `round_resumed` 每轮至多一条（`ux_console_flow_round_start`）；`archive`、`merge`、
  `archive_correction` 沿用每轮至多一条的既有索引。其余转换一轮内可以出现多次。
- **不需要幂等键**：事实只在持有 Flow 行锁、条件更新成功后写入，与状态同事务提交。

## 5. 状态表 `flows`

| 列 | 归属 | 由哪种事实改变 |
|---|---|---|
| `id` | 身份 | 插入时由数据库分配 |
| `repo`、`issue_number`、`task_content`、`installation_id`、`created_at` | 账本 | `created`；存量 Flow 为 `snapshot` |
| `state`、`archive_reason`、`archive_summary` | 账本 | 状态转换事实；`archive_correction` 更正结论 |
| `current_round_no` | 账本 | `round_resumed` |
| `archived_at` | 账本 | `archive` 设置，`round_resumed` 清空 |
| `title` | 账本 | `titled` |
| `updated_at` | 账本 | 最后一条状态转换事实的 `occurred_at`；`titled`、`merge`、`archive_correction` 不推进 |
| `current_attempt_id` | 指针 | `FlowStore.set_current_attempt`，不由账本重建；`round_resumed` 时清空 |
| `title_job_id` | 指针 | `FlowStore.set_title_job`，不由账本重建 |

唯一行为变化：设置 `current_attempt_id` 不再推进 `updated_at`，也不再回写未变化的 `installation_id`。
控制台卡片的更新时间取多表最大值，已包含 `attempts.updated_at`。

表上的约束全部保留。投影更新违反约束时整个事务回滚，事实也不会留下。

## 6. 事实目录

| `kind` | `metadata` | 对 `flows` 的改变 | 写入方 |
|---|---|---|---|
| `created` | `repo`、`issue_number`、`installation_id`、`task_digest` | 插入行：`queued`、第 1 轮 | `FlowStore.create_flow` |
| `snapshot` | 除 `task_content` 外账本拥有的全部列，以及 `task_digest` | 无；存量 Flow 的重放起点 | schema 初始化的最后一步 |
| `FlowEvent` 的值（`round_archived` 除外） | `state`、`archive_reason`、`archive_summary`（写入后的值） | 按 `decide_flow_state`；`updated_at=occurred_at` | `FlowStore.transition` |
| `round_resumed` | `state` | 轮次加一，清空归档字段 | `FlowStore.transition` |
| `archive` | 现有字段：`repo`、`pr_number`、`archive_reason`、`archive_summary`、`merged_at`、`merge_commit_sha` | `archived`，`archived_at=occurred_at` | `FlowStore.archive_round` |
| `merge` | 现有字段 | 无 | `FlowStore.observe_archived_merge` |
| `archive_correction` | 现有字段 | 该轮仍是当前轮且已归档时 `archive_reason=succeeded` | `FlowStore.observe_archived_merge` |
| `titled` | `title`、`job_id` | `title` | `FlowStore.set_title` |

各事件的合法来源状态见 [`flow_policy.py`](../../app/domain/flow_policy.py)；状态转换事实只保存结果列，
来源状态由重放时的前一状态给出。

## 7. 写入路径

`flows` 的写入全部位于 [`app/persistence/flows.py`](../../app/persistence/flows.py)，由边界测试检查：

```text
FlowStore.transition(flow, event)          调用方已持有 lock_workflow
1. state = decide_flow_state(...)           None：守卫空操作，不写任何行
2. 条件 UPDATE flows                        行数不符：报错，事务回滚
3. append_flow(kind=event, metadata=结果)   锁 console_flows → 分配 version → 插入
```

- **先状态、后事实**：条件更新成功后才追加事实，所以调用方捕获“状态已变化”错误时不会留下孤立事实。
  两者同事务提交，顺序不影响一致性。
- **创建**：`flows` 的 ID 由数据库分配，同一事务内先插入行，再追加 `created`。
- **归档**：`archive_round` 先追加 `archive`，再以同一时间调用 `transition(ROUND_ARCHIVED)`。
- **标题与指针**：`set_title` 在条件更新成功后追加 `titled`；`set_current_attempt`、`set_title_job`
  只改指针列，不产生事实。
- **锁顺序不变**：Worker 行 → `flows` 行 → `console_flows` 行。
- **同事务多条事实**：如合并时先 `finalization_requested` 再 `archive`，两条事实的 `version` 连续。

## 8. 重放与校验

领域层的纯函数 `replay_flow(flow_id, facts, task_content=...)` 从最后一条起点开始依次应用事实。
`task_content` 由调用方传入当前行的值，重放先核对它的摘要与起点的 `task_digest` 一致。状态转换事实用
`decide_flow_state` 重新判断：前一状态下该事件必须生效，且结果等于记录的 `state`，否则报错。

`FlowStore.verify(flow_id)` 在 `FOR SHARE` 锁住 `flows` 行后读取事实并重放，返回以下列中不一致的项：
`repo`、`issue_number`、`task_content`、`installation_id`、`title`、`state`、`current_round_no`、
`archive_reason`、`archive_summary`、`archived_at`、`created_at`、`updated_at`。

`flowtool flow verify [FLOW_ID]` 不带参数时检查全部 Flow，只报告差异并以非零状态退出，不自动修复。
测试夹具在每个 PostgreSQL 用例结束时对全部 Flow 运行同一校验。

## 9. 存量数据

schema 初始化的最后一步（`FLOW_LEDGER_BACKFILL`）为每个既没有 `created` 也没有 `snapshot` 的 Flow
补一条 `snapshot`，内容是当时 `flows` 行的账本列。重复初始化不新增行。它不放在 `CONSOLE_SCHEMA` 里：
旧版迁移脚本会单独执行 `CONSOLE_SCHEMA`，随后才补写 `archive` 证据；快照必须排在这些证据之后。

- 重放从 `snapshot` 开始；它之前的状态变化不可还原，与现有 `baseline` 行的说明一致。
- 已有的 `archive`、`merge`、`archive_correction` 事实保持原样，仍用于历史 PR 归属和归档检查。
- 回滚到旧版本后，旧代码继续写 `flows` 而不写事实；再次升级时这些 Flow 已有起点，不会重新补录，
  `flow verify` 会报告差异。处理方式见 feature record。

## 10. 读路径

读路径不变：

- 调度、对账、API 和 CLI 继续读 `flows`；
- 控制台卡片继续读 `console_flows.facts`，时间线继续排除 `source='flow'` 的行；
- `flowtool flow history` 按轮次列出 `source='flow'` 的行，包括事实、状态快照和通知。

## 11. 不变量

1. `flows` 的账本列只经写入口改变；每次改变恰好对应一条事实，两者同事务提交。
2. 事实行不可更新、不可删除。
3. 同一 Flow 的事实 `version` 严格递增，重放顺序唯一。
4. 从起点重放得到的结果与 `flows` 行在比较列上一致。
5. 守卫空操作不写事实；账本里没有“什么都没改变”的行。
6. 事实只引用其他表的 ID，不复制 Job 结果、PR 正文或反馈内容。
7. 协调数据不进入账本。

## 12. 代价与边界

- 每次状态转换多写一行事实。触发器的 `state` 展示行暂时保留，所以一次转换写两行；
  停止生成 `state` 行属于后续工作。
- 账本表名带 `console_` 前缀，外键指向投影表 `console_flows`；`FlowStore` 通过
  `ActivityStore` 写账本，而该模块同时引用控制台合同。这些是复用现有表的代价，
  拆成独立表属于后续工作。
- 事实的 `metadata` 永久保留。字段含义以后需要变化时只能新增种类或字段，旧行由读取方兼容。
- 账本不记录 Job、交付和反馈的内部变化。“为什么进入 blocked”的完整答案仍需要读 `jobs`。

## 13. 后续方向

以下各项需要各自的计划，不属于本设计的承诺范围：

- 停止生成触发器 `state` 行，或在控制台“执行记录”中展示事实；
- 把交付（PR 打开、关闭、合并）和 Job 的粗粒度节点（入队、结果接收、结果应用）纳入账本；
- 把 Flow 事实迁到独立的表，改名并让外键指向 `flows`；
- 为[可派生 Flow](derivable-flows.md) 的 `FlowLink`、`FlowWait`、`FlowResult` 增加事实种类。
