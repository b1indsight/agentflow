# 沿着一条任务读代码

沿着一个 Job 读代码时，先确认上一个模块交出了什么，以及下一个模块还需要验证什么。HTTP 入口交给消费者的是通知，调度器交给 worker 的是带身份的分配，worker 交回的是执行结果。以“给一个 Issue 加标签，最后得到一张 PR”为例：从收件记录确认 GitHub 的通知到没到，从分配记录确认谁领了任务，最后从交付记录确认 PR 开在了哪里。

## 包依赖边界

[领域层 `domain`](../../app/domain)定义 Flow、Attempt、Job 的含义，以及给定一组事实后可以做什么决定。它不用连数据库，也不需要知道 GitHub 的网址。[契约层 `contracts`](../../app/contracts)约定两端能交换哪些任务、结果和会话数据；[通信层 `communication`](../../app/communication)负责把这些消息送到正确的连接，把回复配回原来的请求。传输成功只能说明消息送达，不能替代业务层的交付确认。

真正组合这些能力的是[控制端 `controler`](../../app/controler)。它一侧依赖[持久层](../../app/persistence)提交本地事务，另一侧依赖[GitHub 客户端](../../app/github)访问安装了 App 的仓库。[工作端 `worker`](../../app/worker)隔着连接接任务，不直接改业务表。这个分工也由[包依赖测试](../../tests/test_boundaries.py)约束。

哪些模块还不能按「高层导入底层」排列，见[反向导入清单](import-direction-audit.md)。

### 跟踪一张任务卡，应该记住哪些编号？

先记 **Flow ID**，再分清要查的是哪次开工、哪份作业或哪张 PR。Issue 编号帮助定位仓库里的输入；控制台也能直接用一段文字创建 Flow，此时没有 Issue 编号。[Flow 的输入检查](../../app/domain/models.py#L118-L149)要求 Issue 编号和直接任务正文恰好二选一。两种入口都可以产生后续交付，届时从交付记录读取 PR 编号即可。

| 手头的问题 | 查哪个对象 | 它能回答什么 |
| --- | --- | --- |
| 原任务归档后又开工了，当前在做哪一轮？ | `Flow.current_attempt_id` → `Attempt` | Flow 保持同一个任务身份。Attempt 保存这一次开工的会话 ID 和修订轮次；网络重连无需更换 Attempt。 |
| 编码已经完成，为什么详情页还有待处理的工作？ | `Job` | 每份 Job 有自己的类型、分配和结果；可以分开查看首轮编码、命名推理及后续修订。收到结果和应用结果各有一个时间戳。 |
| 可以到另一台 worker 的同名目录继续吗？ | `FlowExecution` | 绑定同时记录 worker、仓库身份、位置及代次。路径字符串本身不能证明这个目录仍归当前任务。 |
| 链接里展示的 PR 属于哪次开工？ | `FlowDelivery` | 顺着 Attempt 查分支、提交和 PR 编号；旧 Attempt 的交付资料不应该充当本轮交付。 |

这些字段可以直接对照[开工记录、位置与交付模型](../../app/domain/models.py#L181-L218)和[作业模型](../../app/domain/models.py#L245-L266)。举个有意把编号错开的例子：仓库 `octo/repo` 的 Issue 7 第一次交付到 PR 17，随后任务归档。重新开工后，原 Flow ID 不变，但新 Attempt 的当前 PR 为空，旧归档仍记录 PR 17。若只按 Flow ID 找到第一张 PR 就拿去更新，会把新工作写到旧交付上。[归档后重新开工测试](../../tests/test_console_archive.py#L219-L260)

包边界还有一个可观察的作用：**工作机可以读取任务数据而不加载数据库驱动**。[独立解释器测试](../../tests/test_boundaries.py#L12-L56)分别导入通信网关、worker 启动模块和 CLI，并在另一个示例中从 JSON 恢复仓库、Issue、PR 等资源。测试检查实际加载的模块，预期没有 `psycopg`、`githubkit` 和持久层。如果改一个共享数据结构却导致启动 worker 时需要数据库配置，应该先检查它的导入依赖，再排查连接地址。通信消息即使带有仓库和 PR 信息，也不因此拥有访问 GitHub 的凭据。

## 从收到通知，到允许执行

[事件入口](../../app/controler/events/webhooks.py)先保留原始通知，再由[路由器](../../app/controler/events/router.py)识别它的含义：Issue 标签可能触发创建，评论进入反馈缓冲，合并交给生命周期处理。同一个通知可以多次送达；重复投递不会因此变成多项新任务。浏览器则经过[控制台 API](../../app/controler/console/api.py)校验登录身份和写请求来源，直接提出创建、补充指令或重新开工等命令。

两条路都需要[工作流服务](../../app/controler/workflow/service.py)判断当前 Attempt 能不能接受操作。比如人在工作端正忙时补充一条要求，服务可以先保存输入，等能派发时一起交付。收到浏览器请求与编码进程读到新要求并非同一时刻。[任务协调器](../../app/controler/workflow/coordinator.py)为已领取的 Job 准备具体输入，包括仓库、交付分支和本次执行配置；只有分配仍有效，工作才可发出。

### 一张收据跨过了哪些事务？

以 Issue 标签通知为例。[HTTP 入口 `webhook`](../../app/controler/api.py#L219-L251)检查原始请求体的签名、投递编号、事件名和 JSON 格式，把这次投递的编号、类型、正文及内容摘要交给 [`ServerRuntime.receive_webhook`](../../app/controler/runtime.py#L300-L302)。它等待 `WebhookStore.receive` 提交收件记录，再唤醒消费者。相同编号和内容重复到达不会重新启动处理；同一编号配上不同内容会被拒绝。数据库无法接收时返回 503，不向 GitHub 假报 202。[收件契约](../../app/persistence/webhooks.py#L38-L62)

接下来是**另一笔事务**。[`WebhookConsumer.sweep`](../../app/controler/events/webhooks.py#L53-L110)领取一条待处理记录，把事务绑定的服务交给 `EventRouter.dispatch(event_type, delivery_id, payload)`。只有认出符合条件的触发动作，才会进入创建任务的路径；路由成功与标记收件已处理一起提交。异常会回滚业务写入，由收件箱记录失败。唤醒 worker 的通知在提交之后发送，即使这次通知没送到，已经排队的 Job 仍在。[事务回滚测试](../../tests/test_webhook_inbox.py#L422)

创建本身还有一层约束。[`ControlService.create_flow`](../../app/controler/workflow/service.py#L106-L130)接收仓库、安装身份，以及 Issue 编号或任务正文。在同一个事务中，[`FlowStore.create_flow`](../../app/persistence/flows.py#L62-L89)创建 Flow、首次 Attempt 及创建事实，随后 `JobStore.enqueue_job` 放入首轮工作。函数返回 `Flow`，不是执行结果；若该 Issue 已经有活跃 Flow，服务返回 `None`。读者在浏览器里再创建同一 Issue 时，[控制台入口](../../app/controler/console/api.py#L225-L254)会把这个返回值变成冲突，而不是再新增一张卡片。

**没看到卡片时，沿着投递编号查到业务事务为止。** [消费失败后重试的用例](../../tests/test_webhook_inbox.py#L165-L184)让消费者照常创建 `owner/repo` 的 Issue 7 任务，随即在提交前抛出异常。第一次扫描后，按 Issue 查不到活跃 Flow，按投递编号也查不到“已处理”标记，但收件记录还在，并保存了异常类型。恢复正常处理逻辑、等到可重试时间，再扫同一张收据，就能同时查到处理标记和活跃 Flow。这能区分收件失败（入口不应答应接单）与业务失败（控制端已经负责稍后处理）；后一种情况下从控制台手动创建新任务，反而会让人难以判断原通知后来做了什么。

收件箱还决定**哪条通知可以先处理**。同仓库的后续通知要等前一条完成，另一个仓库可以继续。[并发领取测试](../../tests/test_webhook_inbox.py#L350-L370)把两条同仓库通知记为 `head` 和 `tail`；一个消费者持有 `head` 时，另一个能领到其他仓库的通知，却领不到 `tail`。如果前一个消费者挂起，租约过后新消费者会领到同一个 `head` 和新的领取令牌；旧令牌不能再标记完成。[故障隔离测试](../../tests/test_webhook_inbox.py#L212-L242)还展示了值班时可能看到的三种状态：故障通知进入 `blocked`，同仓库后一条留在 `pending`，另一仓库的通知已经 `done`。此时应先处理堵住该仓库的那张收据，无需重启所有工作端。

## 工作端怎样选目录、工具和模型

工作端上报的是具体[工作区库存](../../app/worker/execution/workspaces.py)。控制端在[领取事务](../../app/persistence/assignments.py)中保存 worker 身份、位置和该次绑定的代次。后续修订由原位置继续，另一项 Flow 则可以用同机其他位置。代次用于区分同一路径先后两位使用者：旧任务迟到的结果不能仅因目录名相同就被接纳。

[执行器](../../app/worker/execution/executor.py)通过同一个[编码接口](../../app/worker/coding_agent.py)启动或恢复 Codex、Amp。新会话使用本次任务指定的设置，恢复已有会话不会把后来的全局默认设置强加给它。项目也可配置 skill 仓库：工作端用单独的只读授权获取快照，再把常驻或选中的 skill 链接到工作区，见[安装逻辑](../../app/worker/execution/skills.py)。

只需要模型判断的工作有另一条路径。比如给任务命名，或判断评论是在提问还是要求修改，都不需要占用原 Flow 的代码目录。[推理调度](../../app/controler/workflow/inference.py)将它们作为推理 Job 派发给可用执行者：符合条件时优先使用控制端托管的 bub 推理 worker，也可以由支持推理的普通 worker 在独立会话中完成。如何由判断变成下一轮编码，见[评审循环](review-loop.md)。

### 分到一个位置，不等于已经启动进程

1. **领取。** 控制端轮询时调用 [`AssignmentStore.assign_job(worker_id, token, connection_epoch, slot=…)`](../../app/persistence/assignments.py#L89-L108)。三个身份参数分别表示哪台机器、哪次分配、哪条连接。`execute` 选一份可写工作区并沿用原 Flow 的绑定；`infer` 选推理位置，不占仓库。返回 `None` 表示本次没有领到匹配的工作；返回 `Job` 只意味着分配已记录。
2. **准备与授权。** [`JobCoordinator.prepare_for_assignment`](../../app/controler/workflow/coordinator.py#L138-L181)用该 Job 和同一组身份准备任务、模型设置和临时授权。成功返回 `AssignmentArguments`，携带工作区和代次；也可能返回 `JobState`，此时连接处理器应重新排队或结束这次准备，不能把状态值发给编码器。即便准备成功，发送前仍须经过 [`StopStore.authorize_dispatch` 的调用点](../../app/controler/workers/callbacks.py#L818-L841)：准备期间收到的取消，不能因为参数已经组装好就被忽略。
3. **执行。** [`WorkerStateCallbacks.on_execute`](../../app/worker/transport/state.py#L324-L360)核对连接已就绪、调用编号与分配凭据匹配，再交给本机执行对象。重复送达且已越过接收阶段的分配不会再启动编码器。首次工作由 [`RepositoryWorkspaces.claim`](../../app/worker/execution/workspaces.py#L341-L366)按仓库、Flow、位置及代次取回 `WorkspaceBinding`；旧代次或同代次已被别人占用都不能写。后续修订使用原工作区，而非就近找一个空目录。
4. **调用编码后端。** [`WorkerExecutor.execute`](../../app/worker/execution/executor.py#L96-L119)接收有类型的任务、可选临时授权和工具调用接口，返回对应的 `ExecutionResult`。首轮与修订分别走自己的核验路径；归档任务核验归属后执行目录清理，不调用模型。[`CodingAgent.start` 和 `resume`](../../app/worker/coding_agent.py#L64-L95)只在调用方指定的目录运行后端；后者还需要明确的会话 ID。它们返回编码进程的结果，解释最终 JSON 是否满足业务契约仍是执行器的责任。

推理调用者看到的是另一种返回语义。[`JobInference.infer(task)`](../../app/controler/workflow/inference.py#L25-L56)先查找或登记对应的持久推理 Job。若仍在排队或执行，它唤醒调度并抛出带 Job ID 的 `InferencePending`，让扫描器先处理其他批次；下次以同一请求回来，可读到保存的 `InferenceOutput`。失败则携带已有失败分类，不把重扫一轮当作重新获得一份推理预算。

### 一个 Flow 为什么在首轮编码前就有两份作业？

直接从控制台提交需求时，命名推理为首轮编码准备标题和分支。沿着[命名与首次派发的测试](../../tests/test_coordinator.py#L154-L204)看两个位置如何协作：先登记一台能访问仓库的 worker 并领取编码 Job。这时 `prepare` 还没返回，Flow 上却已经有了 `title_job_id`，交付记录仍为空。worker 上报推理能力后，可以用 `infer` 位置领走这个编号对应的 Job。推理输入含原任务正文，输出同时给标题和符合格式的 `branch_slug`；控制端应用这份结果后才把确定的分支交给编码执行器。仓库目录由原分配使用，推理回复无需在那里创建文件。

核对结果时，顺着这两个编号分别看：Flow 的标题应当来自推理输出，`InitialTask.branch` 应当等于输出的分支名。给原编码 Job 再做一次准备，仍会得到同一个分支，列表中也仍然只有一份首轮 Job 和一份推理 Job。若只有编码能力、一直没有执行者能在期限内完成命名，则出现完全不同的结果：[命名超时测试](../../tests/test_coordinator.py#L397-L417)观察到 Flow 进入 `blocked`，编码 Job 没有启动时间，也没有产生交付。维护者应先查推理能力和命名错误；重试该 Flow 后，控制端保留 Attempt，给新一轮命名分配另一个 Job ID。

## 从本地提交，到 GitHub 上的一张 PR

首轮编码可以通过[本地工具接口](../../app/worker/transport/mcp.py)申请开 PR，也可以请人补充输入。工作端核对 Git 或 jj 的实际提交和远端分支；[控制端工具服务](../../app/controler/workflow/tools.py)记录意图，[结果处理](../../app/controler/workflow/results.py)结合最终结果决定哪些操作可以生效。GitHub 评论、开 PR 等写入由[远端操作模块](../../app/controler/workflow/effects.py)完成。这样既不用把 App 私钥放进编码环境，也能在响应丢失时回头查清发生过什么。

这一段有三个容易混用的返回值。**工具回复**来自 [`FlowToolService.execute(request, worker_id=…, connection_epoch=…)`](../../app/controler/workflow/tools.py#L81-L105)，回答这次 `flow.open_pr` 请求是否被接纳。只读的 [`search`](../../app/controler/workflow/tools.py#L54-L79) 则从活跃首轮分配推导允许搜索的仓库，返回一页结果或错误；它不会完成交付。**执行终态**由 [`ExecutionState.complete`](../../app/worker/state/execution.py#L194-L219) 把编码结果或异常连同 Job ID 和分配凭据写入本地日志。**业务结果**由控制端依据已接收终态处理；若没有这最后一步，不能据工具回复或本机日志宣布 PR 已经出现。

细看最后一步：[`ServerCallbacks.on_result`](../../app/controler/workers/callbacks.py#L547-L573)核对回复属于哪次执行，调用 [`AssignmentStore.record_assignment_result`](../../app/persistence/assignments.py#L429-L482)。相同摘要重传得到 `pending` 或 `applied`，取决于是否已经处理；同一分配交回不同摘要得到 `conflict`，不覆盖旧结果。接下来 [`ResultApplication.apply(job_id)`](../../app/controler/workflow/results.py#L112-L133)提交本地变化，或让未完成的远端操作留待恢复。已处理的 Job 返回保存的 `JobState`；仍须等待时抛出 `ResultPending`。恢复循环继续处理**这个 Job ID**，不会创建第二个编码任务。

**准备开 PR 以后，有三种不同的现场。** 可先看编码结果，再看控制端还缺哪种 GitHub 回执：

1. 工具已经接纳开 PR 意图，编码器最终却以退出码 1 结束。[首轮失败用例](../../tests/test_coordinator.py#L420-L435)得到失败的 Job、仍为 `opening` 的交付记录和空的开 PR 调用列表。控制端再次应用结果仍然返回失败。维护者可在本地活动里用 Job ID 找到退出码，不必从交付状态推测模型是否成功。
2. PR 已经创建，请求评审人时 GitHub 明确拒绝请求并返回 429。[必要操作重试用例](../../tests/test_result_recovery.py#L185-L217)首次应用后仍为 `opening`，`result_applied_at` 仍为空。退避后控制端完成缺少的邀请，交付进入 `open`。此后重复应用，原始结果摘要、接收时间都不变，开 PR 总数仍是一张。遇到这种状态，查控制端的远端操作记录，比让 worker 再次推送分支更有用。
3. PR 已经创建，失败的只是展示用的状态标签。[标签失败用例](../../tests/test_result_recovery.py#L221-L258)让读取或写入标签出错，预期 Job 完成、Flow 进入评审，交付仍记 PR 17。具体错误留在标签操作上，状态为 `skipped`。若仅从仓库看不到预期标签就判定“没开 PR”，会漏掉数据库里已确认的交付。

<a id="47-storepostgresql"></a>

## PostgreSQL 里保存的是哪一种历史？

[数据库模块](../../app/persistence/database.py)管理共享连接池和事务；服务按职责使用具体仓储。在同一事务里，可以把阶段变化写入 `flows` 并追加一条解释变化的[Flow 事实](flow-ledger.md)。Job 的结果、工作区的占用和 GitHub 写入回执另有记录，不靠解释一段活动文本来决定是否重试。管理命令和测试可通过聚合的 [Store](../../app/persistence/store.py)访问这些仓储。

控制台看板和「会话」页都能读数据库里已发布的记录。后台刷新或 API 客户端要读取尚未采集的编码对话时，控制端才需要联系原 worker，因为原始会话保存在执行机器上。见[会话查询](conversation-api.md)。

**读取历史时，先按记录的用途选择入口。** 要解释“为什么从评审转入归档”，从 Flow 事实及对应 Attempt 查起；要解释“GitHub 已经有评论，为什么修订还没有完成”，应查 Job 和远端操作。比如[最终提交中断的用例](../../tests/test_result_recovery.py#L419-L445)让评论先成功，再在本地完成 Job 的事务中抛错。回滚后评审记录保持原样，Job 的应用时间为空，但评论操作已有成功回执。控制端再应用原结果，便能填上完成时间；GitHub 上还是一条评论，数据库也没多出一份修订 Job。若只看一张表，就无法区分“还要发评论”和“只要完成本地记账”。

若要读的是**编码者当时说了什么**，则查原 worker 与会话 ID。[查询服务测试](../../tests/test_conversation_proxy.py#L327-L353)用同一个 Flow 逐步补全线索：没有绑定时读到 `no_worker`；已分配位置、还没有会话 ID 时读到 `no_session`；补上会话 ID 后，即使取消任务、释放位置，查询服务仍把请求交给记录中的原 worker，并携带原会话 ID。这个例子验证了查询发往何处；原机器是否在线、是否仍保留相应文件，还要由实际读取来确认。释放写入位置和删除历史对话是两件不同的事。

独立运行的[对账模块](../../app/controler/reconciliation)定期核对已知 PR、反馈及可选的 GitHub Projects 看板，远端看板写入失败不会倒过来改变 Flow。类似地，[生命周期插件](../../app/plugins/hookspecs.py)可观察业务事件，插件执行成功不能充当 PR 已创建的证据。
