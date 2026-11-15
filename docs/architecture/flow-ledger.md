# Flow 事实账本与状态表

> 状态：目标设计，尚未实现 · 设计日期：2026-11-15。
>
> 当前代码只有归档、合并证据和归档更正三种事实遵循“先写账本、再写状态”，其余 Flow 状态变化仍由
> 各仓储直接更新 `flows`。本页标为“当前”的小节描述代码事实，标为“目标”的小节描述待实施设计；
> 实施步骤、取舍和审批状态见 [feature record](../plan/flow-activity-ledger.md)。

## 1. 目标与范围

目标是让每个 Flow 的业务事实只有一个可信来源：

- **账本**：`console_activity` 中由业务事务写入的 Flow 事实行，只追加、不可改；
- **状态表**：`flows`，是账本在同一事务内更新的投影，业务判断、加锁和唯一约束继续读它；
- **其他状态库**：Job、交付、反馈、Worker、对账进度等表保持各自权威，不由账本重建。

范围只覆盖 `flows` 这一张表。完成后可以按顺序重放某个 Flow 的事实，得到与 `flows` 当前行
逐列一致的结果。

不在范围内：

- 重建 `jobs`、`flow_deliveries`、`review_*`、`workers` 等表；
- 把租约、心跳、claim、分页游标、退避时间写入账本；
- 把账本当作灾难恢复手段。其他表丢失后，只凭账本无法恢复执行与交付；
- 改变控制台时间线、HTTP API、Worker 协议或 GitHub 行为。

## 2. 数据分类

PostgreSQL 仍是唯一运行时数据库。全部表按职责分为五类，每张表只属于一类：

| 类别 | 含义 | 表 |
|---|---|---|
| Flow 事实账本 | 只追加的业务事实，Flow 状态的唯一可信来源 | `console_activity` 中 `source='flow'` 且 `kind` 属于[事实目录](#6-事实目录目标)的行 |
| Flow 状态表 | 账本的同事务投影，供判断、加锁和约束使用 | `flows` |
| 业务状态与专用证据 | 各自领域的权威事实，账本只引用其 ID | `attempts`、`jobs`、`flow_deliveries`、`flow_executions`、`flow_tool_calls`、`execution_stops`、`result_applications`、`result_effects`、`events`、`review_items`、`review_versions`、`review_pending_edits`、`review_buffers`、`review_batches`、`review_thread_observations`、`webhook_inbox`、`issue_flow_owners`、`runtime_settings`、`project_destinations`、`worker_file_links` |
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
  见[第 3 节](#3-当前实现)。

## 3. 当前实现

`console_activity` 当前有四类行，依据见[建表脚本](../../app/persistence/console_schema.py)、
[ActivityStore](../../app/persistence/activity.py)和[推理通知](../../app/persistence/inference_notices.py)：

| 行 | `source` / `kind` | 写入方 | 性质 |
|---|---|---|---|
| Flow 证据 | `flow` / `archive`、`merge`、`archive_correction` | 业务事务调用 `ActivityStore.append_flow` | 不可变事实；触发器拒绝更新与删除，每轮每种至多一条 |
| 状态快照 | `flow` / `state`、`baseline` | 数据库触发器 `console_refresh` | 展示数据；正文是 `console_facts` 视图的前后快照 |
| 推理通知 | `flow` / `inference_exhausted` | `InferenceNoticeStore.append` | 按批次保留最新版本的通知 |
| 采集内容 | `issue.*`、`pr.*`、`worker` 等 | 控制台同步器与 webhook 观察 | GitHub 与 Worker 内容的展示版本，按 digest 去重 |

当前的写入方向与目标相反：

- `flows`、`jobs`、`attempts`、`flow_deliveries`、`flow_executions`、`result_effects` 任一表写入后，
  触发器刷新 `console_flows.facts`，并追加一条 `state` 行。这条行只记录“视图变了”，
  不记录由哪条业务命令、因为什么引起。
- `flows` 的状态由多处直接写入：经 `FlowStore.update_flow` 的模型写入，以及 `jobs.py`、
  `workers.py`、`results.py`、`coordinator.py`、`flows.py` 中的 `UPDATE flows` 语句。
  其中租约清扫用一条语句批量修改多个 Flow。
- 归档是例外：`FlowStore.archive_round` 先追加 `archive` 事实，再写 `state='archived'`；
  `update_flow` 发现本轮没有 `archive` 事实时拒绝写入 `archived`。

其他相关事实：

- 每个 Flow 的行按 `version` 严格递增。`version` 在锁住 `console_flows` 对应行后分配，
  与该行的 `activity_version` 同事务更新。
- `console_activity.flow_id` 的外键指向 `console_flows`。`flows` 插入时，触发器在同一事务内
  创建 `console_flows` 行，所以新 Flow 的第一条事实可以紧跟插入写入。
- 控制台时间线查询排除 `source='flow'` 的全部行；这些行目前由 `flowtool flow history`、
  归档相关的业务检查和推理通知查询读取。
- 存量 Flow 只有一条 `baseline` 行，标题为“迁移基线：此前业务历史不可还原”。

## 4. 账本（目标）

### 4.1 哪些行是账本

账本是 `console_activity` 中满足以下条件的行：

```text
source = 'flow'
kind ∈ { created, snapshot, round_started, queued, running, review, blocked,
         finalizing, archive, merge, archive_correction, titled }
```

`state`、`baseline`、`inference_exhausted` 虽然也是 `source='flow'`，但不属于账本，重放时跳过。

### 4.2 行格式

事实沿用表的现有列，不新增列：

| 列 | 事实行中的含义 |
|---|---|
| `flow_id`、`version` | 所属 Flow 与该 Flow 内的严格递增序号；重放顺序即 `version` 顺序 |
| `round_no` | 事实所属轮次，事实行必填 |
| `key` | 事实身份。每轮至多一条的种类沿用 `flow:<id>:round:<n>:<kind>`；`created`、`snapshot` 为 `flow:<id>:<kind>`；可重复的种类为 `flow:<id>:fact:<version>` |
| `kind` | 事实种类，决定如何改变 `flows` 行 |
| `occurred_at` | 业务时间，取事务时钟；`archive` 的该值即 `archived_at` |
| `observed_at` | 数据库写入时间 |
| `author` | 固定为 `FlowTool` |
| `title` | 供 CLI 阅读的固定中文标题，不参与重放 |
| `body` | 仅 `created` 与 `snapshot` 使用，保存 `task_content` 原文；其余为空 |
| `metadata` | 该种类的结构化字段，取值限于字符串、整数、布尔和空；必含 `cause` |
| `digest` | 内容摘要，沿用现有计算方式 |

`metadata.cause` 说明起因，取值见事实目录。起因涉及其他表的记录时，同时写入对应的 `job_id`
或 `pr_number`。webhook 投递 ID 和操作者身份目前没有传到状态转换处，首期不记录。

### 4.3 约束

- **不可变**：现有触发器 `console_preserve_evidence` 的保护范围从三种扩大到 4.1 的全部种类，
  更新和删除一律拒绝。
- **唯一性**：`created`、`snapshot` 每个 Flow 至多一条；`round_started`、`archive`、`merge`、
  `archive_correction` 每轮至多一条。`queued`、`running`、`review`、`blocked`、`finalizing`、
  `titled` 一轮内可以出现多次，不加唯一约束。
- **不需要幂等键**：事实只在持有 Flow 行锁、且前置条件成立时写入。重复的命令在锁内看到
  状态已经改变，不会再写。事实与状态同事务提交，不存在只写了一半的情况。

## 5. 状态表 `flows`（目标）

`flows` 的每一列归属如下：

| 列 | 归属 | 由哪种事实改变 |
|---|---|---|
| `id` | 身份 | 插入时由数据库分配，写入 `created` 的 `flow_id` |
| `repo`、`issue_number`、`task_content`、`installation_id`、`created_at` | 账本 | `created`；存量 Flow 为 `snapshot` |
| `state` | 账本 | 除 `merge`、`titled`、`archive_correction` 外的全部种类 |
| `current_round_no` | 账本 | `round_started` |
| `archive_reason`、`archive_summary` | 账本 | `finalizing` 设置，`round_started` 清空，`archive_correction` 更正结果 |
| `archived_at` | 账本 | `archive` 设置，`round_started` 清空 |
| `title` | 账本 | `titled` |
| `updated_at` | 账本 | 最后一条会推进它的事实的 `occurred_at`；`titled`、`merge`、`archive_correction` 不推进 |
| `current_attempt_id` | 指针 | 指向 `attempts` 行，不由账本重建；`round_started` 时清空 |
| `title_job_id` | 指针 | 指向命名用的推理 Job，不由账本重建 |

`updated_at` 的规则与当前行为一致：标题回写和归档更正今天也不修改它。唯一差别是仅更新
`current_attempt_id` 时不再推进 `updated_at`；控制台卡片的更新时间取多表最大值，已包含
`attempts.updated_at`，不受影响。

表上的约束全部保留：六态 CHECK、归档字段之间的 CHECK、`ux_flows_active`、Issue 与任务内容二选一。
投影更新违反约束时整个事务回滚，事实也不会留下。

## 6. 事实目录（目标）

“前置条件”来自现有代码里的条件，“现有写入点”是实施时需要改为调用写入口的位置。

| `kind` | `metadata` 字段 | 对 `flows` 的改变 | 前置条件 | 现有写入点 |
|---|---|---|---|---|
| `created` | `cause=created`、`repo`、`issue_number`、`installation_id` | 插入行：`state=queued`、`current_round_no=1` | 同一 Issue 没有未归档 Flow | `FlowStore.create_flow` |
| `snapshot` | `cause=migration`，以及账本拥有的全部列 | 无；作为存量 Flow 的重放起点 | 该 Flow 没有 `created` 或 `snapshot` | 启动时的一次性补录 |
| `round_started` | `cause=resume` | `state=queued`、轮次加一、清空归档字段和 `current_attempt_id` | `state=archived` 且上一轮有 `archive` 事实 | `FlowStore.start_round` |
| `queued` | `cause=job_enqueued\|assignment_requeued\|retry`、`job_id` | `state=queued` | `job_enqueued`：`review`；`assignment_requeued`：`running`；`retry`：`blocked`。均要求 `archive_reason` 为空 | `JobStore.enqueue_job`、`WorkerStore.requeue_assignment`、`WorkerStore.sweep_expired_assignments`、`ControlService.retry` |
| `running` | `cause=job_assigned`、`job_id` | `state=running` | `queued` 或 `review`，`archive_reason` 为空 | `WorkerStore._claim_job` |
| `review` | `cause=result_applied`、`job_id`、`pr_number` | `state=review` | 未归档，结果成功且存在交付 | `ResultApplication` 的结果提交 |
| `blocked` | `cause=result_failed\|result_unverified\|archive_failed\|recovery_blocked\|delivery_branch_changed`、`job_id`、`pr_number` | `state=blocked`；保留已有 `archive_reason` | 未归档 | `ResultApplication`（三处）、`WorkerStore.sweep_expired_assignments`、PR 对账 |
| `finalizing` | `cause=pr_merged\|pr_closed\|operator\|archive_result\|retry`、`archive_reason`、`archive_summary`、`pr_number` | `state=finalizing`；`archive_reason` 为空时写入结论与摘要 | 未归档 | `lifecycle.record_merge`、`lifecycle.close_delivery`、`ControlService.finish`、`ControlService.retry`、`ResultApplication` |
| `archive` | 现有字段：`repo`、`pr_number`、`archive_reason`、`archive_summary`、`merged_at`、`merge_commit_sha` | `state=archived`、`archived_at=occurred_at` | `finalizing` 且收尾条件全部成立 | `FlowStore.archive_round`（已实现） |
| `merge` | 现有字段：`repo`、`pr_number`、`merged_at`、`merge_commit_sha`、`origin` | 无；归档后到达的合并证据 | 该轮已有 `archive` 事实 | `FlowStore.observe_archived_merge`（已实现） |
| `archive_correction` | 现有字段：`before`、`after`、`archived_at` 及合并字段 | 该轮仍是当前轮且已归档时，`archive_reason=succeeded` | 合并时间早于归档时间一秒以上，且原结论为 `abandoned` | `FlowStore.observe_archived_merge`（已实现） |
| `titled` | `cause=naming_result`、`title`、`job_id` | `title` | `title_job_id` 指向该 Job | `ResultApplication` 的推理结果处理 |

`blocked` 的错误详情、`review` 的交付内容、`running` 的 Worker 身份都留在 `jobs`、
`flow_deliveries`、`flow_executions` 中，通过 `job_id` 或 `pr_number` 查到。

## 7. 写入路径（目标）

`flows` 的账本列只有一个写入口：`FlowStore.record(flow_id, kind, metadata)`。它在调用方的事务内执行：

```text
1. 取得 Flow 行锁
   调用方已持有 lock_workflow 的直接复用；否则入口自己锁定该行
2. 用当前轮次和事务时钟构造事实
3. 计算新行 apply_fact(flow, fact)
   前置条件不成立：返回 None，不写任何行
4. 追加事实
   锁定 console_flows 行 → 分配 version → 插入 console_activity
5. 用新行更新 flows 的账本列
6. 返回新的 Flow
```

创建 Flow 是唯一的顺序例外：`flows` 的 ID 由数据库在插入时分配，所以同一事务内先插入行，
再追加 `created`。

第 5 步的更新照常触发 `console_refresh`，控制台投影与 `state` 展示行不受影响。

几条规则：

- **锁顺序不变**：Worker 行 → `flows` 行 → `console_flows` 行。`lock_workflow`、
  `archive_round` 和现有触发器已经按这个顺序加锁。
- **条件转换**：今天用 `UPDATE … WHERE state IN (…)` 加 rowcount 判断的位置，改为第 3 步的
  前置条件。前置条件写在领域层的 `apply_fact` 里，在线写入和重放共用。
- **锁的变化很小**：派发、入队和租约清扫今天在条件成立时已经因 `UPDATE flows` 持有该行的行锁，
  并经触发器锁住 `console_flows` 行；改为调用写入口后这部分相同。区别是条件不成立时，
  写入口为了判断前置条件也会持有 Flow 行锁到事务结束。
- **批量转换**：租约清扫先锁定候选 Job，再按 `flow_id` 升序逐个调用写入口，每个 Flow 各得一条事实。
- **同事务多条事实**：`record_merge` 在一个事务内先后写 `finalizing` 和 `archive`，两条事实的
  `version` 连续，重放结果与一步到位相同。
- **附带动作保留**：进入 `finalizing` 或 `archived` 时退休活动反馈批次的行为，从 `update_flow`
  移入写入口，语义不变。
- **指针列**：`current_attempt_id`、`title_job_id` 由各自的窄方法更新，不产生事实。

`FlowStore.update_flow` 删除。其他模块不再出现 `UPDATE flows` 或 `INSERT INTO flows`，
由边界测试检查。

## 8. 重放与校验（目标）

领域层提供纯函数 `apply_fact(flow, fact)`，不读时钟、不执行 SQL。它返回新的 Flow；
前置条件不成立时返回空，事实内容不合法时报错。

重放一个 Flow：

```text
1. 读取该 Flow 的全部事实行，按 version 升序
2. 从最后一条 created 或 snapshot 开始
3. 依次 apply_fact，得到重建的 Flow
```

`flowtool flow verify [FLOW_ID]` 重放后与 `flows` 当前行比较以下列：`repo`、`issue_number`、
`task_content`、`installation_id`、`title`、`state`、`current_round_no`、`archive_reason`、
`archive_summary`、`archived_at`、`created_at`、`updated_at`。不带参数时检查全部 Flow。

校验只报告差异并以非零状态退出，不自动修复。出现差异说明存在绕过写入口的写入或投影函数有缺陷，
需要人工判断；按账本重写 `flows` 行的修复命令不在首期范围内。

## 9. 存量数据

启动初始化时，为每个既没有 `created` 也没有 `snapshot` 的 Flow 补一条 `snapshot`，内容是当时
`flows` 行的账本列。这一步幂等，与现有“只为缺失投影补种”的初始化方式一致。

- 重放从 `snapshot` 开始；它之前的状态变化不可还原，与现有 `baseline` 行的说明一致。
- 存量的 `archive`、`merge`、`archive_correction` 事实保持原样，仍用于历史 PR 归属和归档检查。
- 不回填、不改写任何已有行。

## 10. 读路径

读路径不变：

- 调度、对账、API 和 CLI 继续读 `flows`；
- 控制台卡片继续读 `console_flows.facts`，时间线继续排除 `source='flow'` 的行；
- `flowtool flow history` 按轮次列出 `source='flow'` 的行，实施后会多出新的事实种类。

## 11. 不变量

实施后长期成立：

1. `flows` 的账本列只经写入口改变；每次改变恰好对应一条事实，两者同事务提交。
2. 事实行不可更新、不可删除。
3. 同一 Flow 的事实 `version` 严格递增，重放顺序唯一。
4. 从起点重放得到的结果与 `flows` 行在比较列上一致。
5. 前置条件不成立时不写事实；账本里没有“什么都没改变”的行。
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
