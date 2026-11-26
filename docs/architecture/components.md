# 沿着一条任务读代码

代码里有几个名字都像在“处理任务”：HTTP 路由接到请求，工作流服务创建任务，调度器准备输入，最后工作端启动编码工具。区分它们最简单的方法，是看一项工作从这一侧交到下一侧时，究竟交出了什么。

## 包依赖边界

[领域层 `domain`](../../app/domain)定义 Flow、Attempt、Job 的含义，以及给定一组事实后可以做什么决定。它不用连数据库，也不需要知道 GitHub 的网址。[契约层 `contracts`](../../app/contracts)约定两端能交换哪些任务、结果和会话数据；[通信层 `communication`](../../app/communication)负责把这些消息送到正确的连接，把回复配回原来的请求。传输成功只能说明消息送达，不能替代业务层的交付确认。

真正组合这些能力的是[控制端 `controler`](../../app/controler)。它一侧依赖[持久层](../../app/persistence)提交本地事务，另一侧依赖[GitHub 客户端](../../app/github)访问安装了 App 的仓库。[工作端 `worker`](../../app/worker)隔着连接接任务，不直接改业务表。这个分工也由[包依赖测试](../../tests/test_boundaries.py)约束。

## 从收到通知，到允许执行

[事件入口](../../app/controler/events/webhooks.py)先保留原始通知，再由[路由器](../../app/controler/events/router.py)识别它的含义：Issue 标签可能触发创建，评论进入反馈缓冲，合并交给生命周期处理。同一个通知可以多次送达；重复投递不会因此变成多项新任务。浏览器则经过[控制台 API](../../app/controler/console/api.py)校验登录身份和写请求来源，直接提出创建、补充指令或重新开工等命令。

两条路都需要[工作流服务](../../app/controler/workflow/service.py)判断当前 Attempt 能不能接受操作。比如人在工作端正忙时补充一条要求，服务可以先保存输入，等能派发时一起交付。收到浏览器请求与编码进程读到新要求并非同一时刻。[任务协调器](../../app/controler/workflow/coordinator.py)为已领取的 Job 准备具体输入，包括仓库、交付分支和本次执行配置；只有分配仍有效，工作才可发出。

### 一张收据跨过了哪些事务？

以 Issue 标签通知为例。[HTTP 入口 `webhook`](../../app/controler/api.py#L219-L251)检查原始请求体的签名、投递编号、事件名和 JSON 格式，把这次投递的编号、类型、正文及内容摘要交给 [`ServerRuntime.receive_webhook`](../../app/controler/runtime.py#L300-L302)。它等待 `WebhookStore.receive` 提交收件记录，再唤醒消费者。相同编号和内容重复到达不会重新启动处理；同一编号配上不同内容会被拒绝。数据库无法接收时返回 503，不向 GitHub 假报 202。[收件契约](../../app/persistence/webhooks.py#L38-L62)

接下来是**另一笔事务**。[`WebhookConsumer.sweep`](../../app/controler/events/webhooks.py#L53-L110)领取一条待处理记录，把事务绑定的服务交给 `EventRouter.dispatch(event_type, delivery_id, payload)`。只有认出符合条件的触发动作，才会进入创建任务的路径；路由成功与标记收件已处理一起提交。异常会回滚业务写入，由收件箱记录失败。唤醒 worker 的通知在提交之后发送，即使这次通知没送到，已经排队的 Job 仍在。[事务回滚测试](../../tests/test_webhook_inbox.py#L422)

创建本身还有一层约束。[`ControlService.create_flow`](../../app/controler/workflow/service.py#L106-L130)接收仓库、安装身份，以及 Issue 编号或任务正文。在同一个事务中，[`FlowStore.create_flow`](../../app/persistence/flows.py#L62-L89)创建 Flow、首次 Attempt 及创建事实，随后 `JobStore.enqueue_job` 放入首轮工作。函数返回 `Flow`，不是执行结果；若该 Issue 已经有活跃 Flow，服务返回 `None`。读者在浏览器里再创建同一 Issue 时，[控制台入口](../../app/controler/console/api.py#L225-L254)会把这个返回值变成冲突，而不是再新增一张卡片。

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

## 从本地提交，到 GitHub 上的一张 PR

首轮编码可以通过[本地工具接口](../../app/worker/transport/mcp.py)申请开 PR，也可以请人补充输入。工作端核对 Git 或 jj 的实际提交和远端分支；[控制端工具服务](../../app/controler/workflow/tools.py)记录意图，[结果处理](../../app/controler/workflow/results.py)结合最终结果决定哪些操作可以生效。GitHub 评论、开 PR 等写入由[远端操作模块](../../app/controler/workflow/effects.py)完成。这样既不用把 App 私钥放进编码环境，也能在响应丢失时回头查清发生过什么。

这一段有三个容易混用的返回值。**工具回复**来自 [`FlowToolService.execute(request, worker_id=…, connection_epoch=…)`](../../app/controler/workflow/tools.py#L81-L105)，回答这次 `flow.open_pr` 请求是否被接纳。只读的 [`search`](../../app/controler/workflow/tools.py#L54-L79) 则从活跃首轮分配推导允许搜索的仓库，返回一页结果或错误；它不会完成交付。**执行终态**由 [`ExecutionState.complete`](../../app/worker/state/execution.py#L194-L219) 把编码结果或异常连同 Job ID 和分配凭据写入本地日志。**业务结果**由控制端依据已接收终态处理；若没有这最后一步，不能据工具回复或本机日志宣布 PR 已经出现。

细看最后一步：[`ServerCallbacks.on_result`](../../app/controler/workers/callbacks.py#L547-L573)核对回复属于哪次执行，调用 [`AssignmentStore.record_assignment_result`](../../app/persistence/assignments.py#L429-L482)。相同摘要重传得到 `pending` 或 `applied`，取决于是否已经处理；同一分配交回不同摘要得到 `conflict`，不覆盖旧结果。接下来 [`ResultApplication.apply(job_id)`](../../app/controler/workflow/results.py#L112-L133)提交本地变化，或让未完成的远端操作留待恢复。已处理的 Job 返回保存的 `JobState`；仍须等待时抛出 `ResultPending`。恢复循环继续处理**这个 Job ID**，不会创建第二个编码任务。

<a id="47-storepostgresql"></a>

## PostgreSQL 里保存的是哪一种历史？

[数据库模块](../../app/persistence/database.py)管理共享连接池和事务；服务按职责使用具体仓储。在同一事务里，可以把阶段变化写入 `flows` 并追加一条解释变化的[Flow 事实](flow-ledger.md)。Job 的结果、工作区的占用和 GitHub 写入回执另有记录，不靠解释一段活动文本来决定是否重试。管理命令和测试可通过聚合的 [Store](../../app/persistence/store.py)访问这些仓储。

控制台看板和「会话」页都能读数据库里已发布的记录。后台刷新或 API 客户端要读取尚未采集的编码对话时，控制端才需要联系原 worker，因为原始会话保存在执行机器上。见[会话查询](conversation-api.md)。

独立运行的[对账模块](../../app/controler/reconciliation)定期核对已知 PR、反馈及可选的 GitHub Projects 看板，远端看板写入失败不会倒过来改变 Flow。类似地，[生命周期插件](../../app/plugins/hookspecs.py)可观察业务事件，插件执行成功不能充当 PR 已创建的证据。
