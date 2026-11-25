# Flow 事实账本与状态表

一个 Flow 是一项任务；一次 Attempt 是这项任务的一轮执行。假设一项任务已经归档，后来又重新开工：调度器需要立刻知道该派发哪一轮，排查问题的人却需要看到上一轮为什么结束。前者读 PostgreSQL 的 `flows` 当前行，后者读同一 Flow 按版本追加的事实。重新开工会改变当前行，原来的归档事实仍属于原 Attempt。[创建与状态转换](../../app/persistence/flows.py#L62-L91) · [重新开工后保留原归档的测试](../../tests/test_console_archive.py#L219-L269)

## 一条状态、一本账、几种不同的历史

| 想知道什么 | 应该读哪里 |
|---|---|
| 任务现在能不能继续？当前执行是哪一轮？ | `flows.state` 和 `flows.current_attempt_id`。工作流在事务里锁住当前行后作决定。 |
| 哪个业务事件改变了任务状态？ | `console_activity` 中 `source='flow'` 的事实行，排除 `state`、`baseline`（展示快照）和 `inference_exhausted`（推理通知）。 |
| 哪个 PR 属于这轮执行？作业失败的原始结果是什么？ | 交付字段归 `attempts`，派发和终态结果归 `jobs`。心跳、租约、同步游标用于协调工作，不参与 Flow 重放。 |
| 用户和 Agent 说了什么？ | 控制台读已采集的活动正文，实时编码记录从 [Worker 会话 API](conversation-api.md) 读取。待交付的追加任务也保存在活动表中，但由[追加任务存储](../../app/persistence/agent_inputs.py)负责交付。 |

表名中的 `console_` 容易让人误以为整张表都能重建。实际上，`console_flows` 是看板投影；`console_activity` 混存不可变事实、展示快照和从外部采集的记录。应按 `source` 和 `kind` 判断一行的用途。[事实筛选与历史读取](../../app/persistence/activity.py#L150-L186) · [数据库约束](../../app/persistence/console_schema.py#L72-L92)

<a id="5-状态表-flows"></a>

## 当前行中哪些字段能由账本解释？

`created` 给出 `repo`、`issue_number`、`installation_id`，选中首个 Attempt，并以发生时间确定 `created_at`。直接创建的任务另有 `task_content`；事实只存其 SHA-256 摘要 `task_digest`，校验时仍需提供原文。`titled` 给出 `title`。状态转换决定 `state`、`archive_reason`、`archive_summary` 和 `updated_at`；`archive` 设置 `archived_at`，保留当前 Attempt；`attempt_started` 选中新 Attempt 并清空归档字段。`titled`、`merge`、`archive_correction` 不推进生命周期的 `updated_at`。[比较列与重放规则](../../app/domain/flow_policy.py#L85-L185)

`id` 是数据库分配的身份。命名作业指针 `title_job_id`、Issue 展示归属 `is_issue_owner`、历史只读标志 `history_only` 和 Flow 引用 `child_flow_id` 都不在比较列里。因此 `verify` 成功只证明账本解释了指定字段，不能用它证明 Worker、PR 或展示同步正常。[校验实现](../../app/persistence/flows.py#L209-L232)

<a id="6-事实目录"></a>

## 事实目录：每个种类记录了什么？

| `kind` | `metadata` 的关键字段与含义 |
|---|---|
| `created` | `repo`、`issue_number`、`installation_id`、`task_digest`；从 `queued` 开始。`snapshot` 是重放器兼容的另一种起点，携带比较列（原文用摘要代替）。当前服务不会补造缺失的起点。 |
| `job_enqueued`、`job_claimed`、`assignment_requeued`、`execution_uncertain`、`result_failed`、`delivery_baseline_rejected`、`delivery_applied`、`finalization_requested`、`cleanup_applied`、`retry_requested` | 保存写入后的 `state`、`archive_reason`、`archive_summary`。合法的前置状态由领域规则决定。 |
| `attempt_started` | 同上，但 `attempt_id` 指向新开的一轮。 |
| `archive` | `repo`、`pr_number`、`archive_reason`、`archive_summary`、`merged_at`、`merge_commit_sha`。它本身就是 `attempt_archived` 的证据，不另写同义行。 |
| `merge` | `repo`、`pr_number`、`merged_at`、`merge_commit_sha`、`origin`；补记已归档 PR 的合并证据，不改变 Flow 状态。 |
| `archive_correction` | 合并证据加 `before`、`after`、`archived_at`。对仍为当前且已归档的 Attempt，更正当前行的归档原因。 |
| `titled` | `title`、`job_id`；仅采纳仍属于当前 Attempt 的指定命名作业的结果。 |

[事件与状态规则](../../app/domain/flow_policy.py#L13-L93) · [转换字段](../../app/persistence/flows.py#L177-L207) · [归档证据](../../app/persistence/flows.py#L434-L463) · [迟到合并与更正](../../app/persistence/flows.py#L482-L550)

每行有 `flow_id`、`attempt_id`、`kind` 和该 Flow 的活动 `version`。**重放按版本，不按时间**：`occurred_at` 是业务发生时间，迟到的合并证据可以比已写入的归档更早；`observed_at` 是观察时间。`author` 固定为 `AgentFlow`，`title` 用于展示，`body` 不存任务原文，`digest` 是活动内容摘要，重放读取结构化的 `metadata`。[事实的构造与排序](../../app/persistence/activity.py#L105-L160)

`key` 有三种形状：起点为 `flow:<id>:<kind>`，`attempt_started` / `archive` / `merge` / `archive_correction` 为 `flow:<id>:attempt:<id>:<kind>`，可重复的转换为 `flow:<id>:fact:<version>`。同一 Flow 至多一个起点；上述四种按 Attempt 标识的事实每轮每种至多一条。数据库拒绝修改、删除事实；同一轮同种事实重复追加时，时间和元数据相同则不再写，不同则报冲突。[写入去重](../../app/persistence/activity.py#L116-L149) · [索引与触发器](../../app/persistence/console_schema.py#L72-L92)

<a id="7-写入路径"></a>

## 一次状态变化如何落库？

1. 工作流持有 Flow 行锁，把业务事件交给 `FlowStore.transition`。`decide_flow_state` 区分三件事：非法转换报错，守卫判定无动作则原样返回，合法事件即使目标状态同名也会写入并更新时间。[三种情况的测试](../../tests/test_flow_states.py#L36-L106)
2. 存储层以当前状态和 Attempt 为条件更新 `flows`，再由 `ActivityStore.append_flow` 锁住 `console_flows` 的版本头，追加事实。事务失败时两者一起回滚；不会留下只有状态没有解释的提交。[普通转换](../../app/persistence/flows.py#L123-L186)
3. 创建时先拿到 Flow 和首个 Attempt 的 ID 才能写 `created`。归档顺序相反：先写带结论的 `archive`，再用同一业务时间推进当前状态。归档证明业务已结束，不能据此推断 Worker 已经完成清理。[归档事务](../../app/persistence/flows.py#L434-L463) · [归档证据失败时回滚](../../tests/test_flow_archive_activity.py#L182-L218)

例如，迟到的 webhook 表明一条此前判为 `abandoned` 的 PR 其实在归档前已合并。系统按原仓库、PR、合并时间和提交核对证据；只有 `merged_at + 1 秒 < archived_at` 才据此更正原结论。如果用户已经重新开工，旧轮次保留 `merge` 和 `archive_correction`，新轮次的状态、交付不受影响。冲突的合并证据不能覆盖旧行。[按实际合并时间判断的测试](../../tests/test_flow_archive_activity.py#L100-L180)

<a id="8-重放与校验"></a>

## 排查：先看历史，再校验当前行

```sh
agentflow flow history 42
agentflow flow verify 42
```

`history` 按版本列出跨 Attempt 的事件摘要，也会包含快照和通知。`verify` 则在共享锁下取出当前行，只把事实交给 `replay_flow`：从起点核对任务摘要，依序检查 Attempt 归属和状态转换是否合法，再比较上文列出的字段。省略 ID 会检查全部 Flow；有差异或无法重放时以非零退出，只报告不修复。`history_only` 的旧 Flow 单独报告为跳过，不计入通过数。[CLI](../../app/cli.py#L410-L453) · [重放函数](../../app/domain/flow_policy.py#L117-L185)

<a id="12-代价与边界"></a>
<a id="13-后续方向"></a>

## 读不到的证据不能从状态反推

账本可以解释为什么 Flow 进入某个状态，却无法从任务摘要恢复丢失的正文，也不能重建 Worker 上的工具输出。若缺少起点，重启不会用当前状态伪造一段历史；若怀疑作业错误，继续查对应 Attempt 的 `jobs`，若想看 Agent 当时的对话，转到[会话走查](../feature-walkthroughs/conversation.md)。[缺失起点的回归测试](../../tests/test_flow_states.py#L319-L337)
