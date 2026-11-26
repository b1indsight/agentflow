# Flow 事实账本与状态表

一个 Flow 是一项任务；一次 Attempt 是这项任务的一轮执行。假设一项任务已经归档，后来又重新开工：调度器需要立刻知道该派发哪一轮，排查问题的人却需要看到上一轮为什么结束。前者读 PostgreSQL 的 `flows` 当前行，后者读同一 Flow 按版本追加的事实。重新开工会改变当前行，原来的归档事实仍属于原 Attempt。[创建与状态转换](../../app/persistence/flows.py#L62-L91) · [重新开工后保留原归档的测试](../../tests/test_console_archive.py#L219-L269)

## 从哪个模块开始读？

以「领取排队任务」为例。Worker 的领取动作可以让 Flow 从 `queued` 变成 `running`；排查时需要把动作、判断和记录区分开。下面四块代码各管一部分：

1. **工作流服务**处理有业务含义的操作。例如「重新开工」要核实当前确实已归档、旧执行已释放，然后安排新的首轮作业。它知道操作涉及哪些表、该把哪些动作放进同一个事务；使用者不应把重新开工简化成改一个状态值。[重新开工入口](../../app/controler/workflow/service.py#L189-L227)
2. **领域规则**把当前状态、业务事件和归档原因作为输入，决定该写什么状态，或本次不写。这里没有数据库查询，也不会唤醒 Worker。同一套规则在历史重放时再用一次，检查历史记录能否按所写的次序发生。[决策函数](../../app/domain/flow_policy.py#L33-L86) · [重放时的调用](../../app/domain/flow_policy.py#L153-L185)
3. **Flow 存储与活动存储**分担一次写入：前者持有当前行和 Attempt 归属，按规则更新；后者把发生过的事按版本追加。归档、命名和创建带有不同的元数据。普通转换只要记录状态及归档字段，不需要把整个 Flow 对象在每一步都复制进账本。[状态写入](../../app/persistence/flows.py#L123-L186) · [事实写入](../../app/persistence/activity.py#L105-L149)
4. **读取与校验工具**服务于两类读者。控制台展示当前投影及活动；运维者用 `flow history` 看记录顺序、用 `flow verify` 比对当前行。校验不调用工作流服务重新执行事件，不会再次安排作业，也不会让 PR 再合并一次。[两条 CLI 命令](../../app/cli.py#L410-L453)

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

### 同一任务里，哪几个标识会变？

假设 `octo/repo` 的 Issue 7 创建了一个 Flow。仓库和 Issue 描述任务的来源，Flow ID 是这次任务在本系统中的标识；重新开工会保留这个 Flow ID。当前 Attempt ID 则会切换，每个 Job 仍属于创建它的那一轮。归档事实中的 PR 号回答「结束时交付到哪里」，与新 Attempt 的 PR 字段可以不同。若调查的是旧 PR，先找到旧归档所属的 Attempt，不要拿今天的 `current_attempt_id` 过滤全部历史。[保留旧 PR、作业和归档的测试](../../tests/test_console_archive.py#L219-L269)

创建还区分 Issue 任务和直接提交的任务：`create_flow(repo, issue_number, installation_id, task_content=...)` 要求 `issue_number` 与 `task_content` 恰好给一个。比如 Issue 任务传 `issue_number=7` 而不传正文；直接任务传 `issue_number=None` 和正文。两者都缺或两者都有，会在插入之前被拒绝。合法创建的首个 `created` 使用同一个业务时间设定创建与更新时间，归属数据库刚分配的首个 Attempt。这样从起点往后重放时，不需要靠「最小作业 ID」猜测第一轮。[创建方法](../../app/persistence/flows.py#L62-L91)

`task_digest` 可用来验证一份已有的原文。如果读库拿到的正文与起点摘要不符，校验会先报告内容不匹配，根本走不到后面的状态比较。反过来，若只有账本备份、没有直接任务的原文，摘要不能补全任务内容。做恢复检查时应当把正文是否还在、账本是否能重放列作两个问题。[起点与摘要检查](../../app/domain/flow_policy.py#L117-L150)

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

### 按同一轮的先后顺序读状态

以下用一项因执行失败而重试、随后交付的任务说明普通转换。表中「前 → 后」是 Flow 的状态；假设全程还没有请求归档，作业已经分别走到能触发对应事件的阶段，省略与这条状态线无关的展示和命名记录。[合法前置条件](../../app/domain/flow_policy.py#L43-L85)

| 按顺序读到的事实 | 前 → 后 | 此时能确认什么 |
|---|---|---|
| `created` | 无 → `queued` | 选中了第一轮；不能据此说 Worker 已执行。 |
| `job_claimed` | `queued` → `running` | 领取事件被采纳。这条事实没有工具输出，若需要具体命令须另查会话。 |
| `result_failed` | `running` → `blocked` | 这轮执行遇到失败；失败原因和结果正文要找对应的 Job。 |
| `retry_requested` | `blocked` → `queued` | 没有归档原因，重试回到排队，同一 Attempt 继续。已有归档原因时合法重试会回到 `finalizing`。 |
| `job_claimed`、`delivery_applied` | `queued` → `running` → `review` | 新作业被领取，交付结果随后被采纳，可以开始评审。没有发生 `attempt_started`。 |

读相邻两条同状态的记录时，先比较事件。比如同一轮已经 `running` 又收到 `job_claimed`，守卫让本次不写，所以即使通知确实到过，也不应在账本里找到第二个有效领取。相比之下，连续两个 `result_failed` 可以是两条有效写入：第二次虽然仍为 `blocked`，仍会更新发生时间并追加事实。诊断「多写了一次」需要先看事件语义，不能按目标状态去重。[时间戳与守卫测试](../../tests/test_flow_states.py#L66-L91)

活动版本由整个 Flow 的活动共享。上表连续的两项事实之间完全可能夹有用户留言或展示快照，所以查询事实得到的版本号不一定相邻。版本空一格本身不是丢失事实的证据；先核对所用查询是否筛掉了其他 `source` 或 `kind`。同理，跨 Flow 的版本也没有全局先后含义。[版本头与事实查询](../../app/persistence/activity.py#L121-L159)

<a id="7-写入路径"></a>

## 一次状态变化如何落库？

1. 工作流持有 Flow 行锁，把业务事件交给 `FlowStore.transition`。`decide_flow_state` 区分三件事：非法转换报错，守卫判定无动作则原样返回，合法事件即使目标状态同名也会写入并更新时间。[三种情况的测试](../../tests/test_flow_states.py#L36-L106)
2. 存储层以当前状态和 Attempt 为条件更新 `flows`，再由 `ActivityStore.append_flow` 锁住 `console_flows` 的版本头，追加事实。事务失败时两者一起回滚；不会留下只有状态没有解释的提交。[普通转换](../../app/persistence/flows.py#L123-L186)
3. 创建时先拿到 Flow 和首个 Attempt 的 ID 才能写 `created`。归档顺序相反：先写带结论的 `archive`，再用同一业务时间推进当前状态。归档证明业务已结束，不能据此推断 Worker 已经完成清理。[归档事务](../../app/persistence/flows.py#L434-L463) · [归档证据失败时回滚](../../tests/test_flow_archive_activity.py#L182-L218)

**例：重新开工时为什么要带旧 Attempt ID？** 假设 Flow 42 的第 3 号 Attempt 已归档。[控制台](../../app/controler/console/api.py#L299)调用 [ControlService.resume](../../app/controler/workflow/service.py#L189-L227)，传入 Flow 和 `expected_attempt_id=3`。服务在事务内重新读并锁住当前行，核对归档证据、工作区是否已释放，以及同一 Issue 有没有其他活动 Flow。随后调用 [FlowStore.transition(fresh, ATTEMPT_STARTED)](../../app/persistence/flows.py#L123-L155)，返回的 `Flow` 已指向新 Attempt；服务用这个返回值安排首轮 Job，提交后才唤醒调度。若同一请求因响应丢失又到达，旧 ID 已不匹配，调用者应先重新读取 Flow，不能再替用户开一轮。

上述事务也限定了谁能追加事实：[ActivityStore.append_flow](../../app/persistence/activity.py#L105-L148) 接受 `flow_id`、`attempt_id`、种类、业务时间和结构化事实。对每轮只记一次的事件，`True` 代表新增，`False` 代表已有完全相同的证据；时间或内容冲突则抛错，不能靠更换时间重试来覆盖它。如果后续入队失败，外层事务会连同新 Attempt 和 `attempt_started` 一起回滚。

例如，迟到的 webhook 表明一条此前判为 `abandoned` 的 PR 其实在归档前已合并。系统按原仓库、PR、合并时间和提交核对证据；只有 `merged_at + 1 秒 < archived_at` 才据此更正原结论。如果用户已经重新开工，旧轮次保留 `merge` 和 `archive_correction`，新轮次的状态、交付不受影响。冲突的合并证据不能覆盖旧行。[按实际合并时间判断的测试](../../tests/test_flow_archive_activity.py#L100-L180)

### 写入方要满足的前置条件

`FlowStore.transition` 接受已在工作流事务里锁定的最新 Flow，以及一个 `FlowEvent`；可传 `now` 指定本次业务时间。直接拿普通存储对象调用会因缺少事务而失败。即使已经进入新的事务，也要使用锁内读出的对象：假设调用者留着早先的 `queued` 副本，而当前行已经 `blocked`，按旧状态和 Attempt 匹配不到一行，存储层报告已非当前状态。失败后应重新读当前行，再决定原请求是否仍适用。[方法合同与条件更新](../../app/persistence/flows.py#L123-L176) · [旧对象测试](../../tests/test_flow_states.py#L83-L105)

同一个事务内的前一步也不保证能独立留下记录。一个可验证的反例是先对排队任务应用 `JOB_CLAIMED`，随后在同一事务里错误地对运行中的 Flow 应用 `ATTEMPT_STARTED`。后一步只允许从已归档开始，会抛异常；事务退出后仍能读到最初的排队状态。把第一步成功返回的内存对象或日志行当成已经提交的数据库证据，会得出与事后校验相反的结论。[回滚用例](../../tests/test_flow_states.py#L92-L105)

外部来源走的是另一个入口 `ActivityStore.append(flow_id, observation)`。例如采集 GitHub 讨论时可以保存评论正文，却不能声称 `source='flow'` 或占用 `flow:` 前缀；入口会报错。只有工作流内部写入器能新增账本事实，并要求正的 Attempt ID 和已知的事实种类。因此即使一条评论正文是「已归档」，也不构成结束任务的证据。[普通活动与内部事实入口](../../app/persistence/activity.py#L98-L123)

<a id="8-重放与校验"></a>

## 排查：先看历史，再校验当前行

```sh
agentflow flow history 42
agentflow flow verify 42
```

`history` 按版本列出跨 Attempt 的事件摘要，也会包含快照和通知。`verify` 则在共享锁下取出当前行，只把事实交给 `replay_flow`：从起点核对任务摘要，依序检查 Attempt 归属和状态转换是否合法，再比较上文列出的字段。省略 ID 会检查全部 Flow；有差异或无法重放时以非零退出，只报告不修复。`history_only` 的旧 Flow 单独报告为跳过，不计入通过数。[CLI](../../app/cli.py#L410-L453) · [重放函数](../../app/domain/flow_policy.py#L117-L185)

在源码里，[CLI 调用](../../app/cli.py#L419)的 [FlowStore.verify(42)](../../app/persistence/flows.py#L209-L232) 返回空字典才表示比较通过。有差异时，每列对应一对值，顺序是 **重放值、数据库当前值**；返回 `None` 表示跳过历史只读 Flow。找不到 Flow 或不能重放则抛出异常，不能把这两种情况统计为「无差异」。

### 看一份输出时，先判断是哪类结果

`history` 的每一行是一份便于阅读的 JSON 摘要，只包含版本、发生时间、标题以及存在的若干业务字段。比如一项任务以「调查完成」为摘要归档后又重新开工，历史输出中应先有标题「Flow 归档」且有 `archive_summary`，再有「重新开工」；「Flow 创建」只有一次。展示命令省略内部 Attempt ID 等字段，不能把这份打印输出直接当作 `replay_flow` 的完整输入。重放需要存储层返回的种类、归属、时间和结构化元数据。[实际命令断言](../../tests/test_console_archive.py#L202-L218) · [历史打印字段](../../app/cli.py#L442-L453)

对 `agentflow flow verify 42`，可以用下面三种例子区分「比较相等」「比较不等」「没能比较」：

- 普通 Flow 42 有合法起点和整段事实，重放列与当前行一致，CLI 输出 `1 flow(s) match their ledger`，正常退出。若 42 是历史只读 Flow，会单独提示跳过，即使正常退出也不应把它算入这个 1。[计数和退出逻辑](../../app/cli.py#L414-L438)
- 若事实能重放出标题 `A`，而当前行意外是 `B`，比较结果是 `title: ('A', 'B')`，CLI 的差异行将写 `ledger='A' stored='B'`，以 1 退出。若不同的是当前执行 ID，CLI 使用「当前执行与账本不一致」的专门提示。读者应先找出差异属于哪一列，保留这次查验结果；该命令不会把 `B` 改成 `A`。[比较列的生成](../../app/persistence/flows.py#L209-L232) · [差异展示](../../app/cli.py#L424-L438)
- 若缺少起点、任务正文摘要不符，或历史中把旧 Attempt 的普通转换记到了新一轮，重放没有可信的最终行。此时看到的是异常说明而非逐列差异。先查起点与按版本排列的归属；只有查明并处理数据来源问题后，再跑比较才有意义。旧轮次的 `merge` 等专门记录有自己的规则，不能仅凭「不是当前 Attempt」就把所有此类记录判作损坏。[重放起点与归属规则](../../app/domain/flow_policy.py#L117-L185)

<a id="12-代价与边界"></a>
<a id="13-后续方向"></a>

## 读不到的证据不能从状态反推

账本可以解释为什么 Flow 进入某个状态，却无法从任务摘要恢复丢失的正文，也不能重建 Worker 上的工具输出。若缺少起点，重启不会用当前状态伪造一段历史；若怀疑作业错误，继续查对应 Attempt 的 `jobs`，若想看 Agent 当时的对话，转到[会话走查](../feature-walkthroughs/conversation.md)。[缺失起点的回归测试](../../tests/test_flow_states.py#L319-L337)
