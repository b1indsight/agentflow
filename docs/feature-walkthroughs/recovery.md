# 工作端断开连接，任务会再跑一遍吗？

连接中断不会让已经启动的编码自动重跑。控制端重启后，原 worker 可以接回还在运行的进程，也可以补传已经完成的结果。若无法证明原执行器已经停止，先保留工作区。下面两个场景分别演示怎样确认一次交付，以及怎样为计划内维护停止接单；请在独立测试环境观察，不要中断生产服务。

## 场景一：保留原工作端，让它重新连接

1. 选择已被 worker 领取并开始编码的测试任务，记下 Flow ID、当前 Job 和仓库分支。保留原 worker 进程、工作区和本地执行记录，只让它到控制端的连接中断。
2. 恢复同一数据库上的控制服务，让同一身份的 worker 重连。控制端和工作端会核对原来的分配记录，再继续接收工具请求和最终结果。若编码已在本机完成，需要补传的是结果；不应看到第二次独立的编码执行。
3. 对照原来的 Flow 和分支检查交付：最终只有一次对应的编码和一张 PR。[控制端更换后只完成一次的测试](../../tests/test_controller_update_boundary.py#L38)故意在编码已经开始、结果尚未交回时断开连接，正好可以区别正常恢复与“从头再跑一次”。

断线后，两边各自掌握一部分证据。新控制端启动时[调用 `recover_abandoned_assignments`](../../app/controler/runtime.py#L185)，[在数据库事务内](../../app/persistence/workers.py#L194-L214)把 worker 标为离线，并给可能已经执行的编码作业与未释放的绑定留下恢复期限。原 worker 重新注册时，[`WorkerExecution.recovery()`](../../app/worker/execution/lifecycle.py#L179-L192)先收集刚刚结束的执行，再从本地记录生成恢复说明，包括执行器是否还在；[注册消息](../../app/worker/transport/state.py#L177-L191)只带上尚未获确认的记录。断开网络没有删除这些记录。[同一场景的断言](../../tests/test_controller_update_boundary.py#L64-L98)检查断线时执行器仍活着，重连后原结果获确认，编码调用总数仍是一。

对每条恢复说明，[控制端的 `_recover`](../../app/controler/workers/callbacks.py#L939-L973)按 Job ID 查库，比较 worker 身份、这次分配的令牌以及双方已有的结果摘要。对不上就拒绝恢复，不会把别的 Job 当作这次交付。核对通过、[重新绑定到新连接后](../../app/controler/workers/callbacks.py#L986-L1006)，还在运行的作业收到 `resume`，本机已结束的收到 `replay`。[worker 收到决定](../../app/worker/transport/state.py#L237-L251)后，前者调用 [`WorkerExecution.resume`](../../app/worker/execution/lifecycle.py#L194-L213)接回同一个活执行器，后者补传原结果。这里传输可以重试，编码无需重跑。

若原机器不能回来，情况不同：控制端可能已经允许执行，只是没有收到启动确认。此时等待租约过期也不能证明命令从未运行。工作区会继续保留，任务可能需要人工核查；不要删除本地记录或另起一份任务来制造一个“空闲”位置。见[丢失启动确认的测试](../../tests/test_cancel_drain.py#L220)。

### 同样是没有新消息，控制端在等什么？

先以 **Job ID** 对照两端的记录。worker 的本地日志说明这个执行器做到哪一步，数据库里的 Job 说明控制端已经接收、应用了什么。下面三个观察点都可能出现在同一轮编码里，但处理方式不同。

- **数据库没有启动时间。** 看控制端是否已经授权派发。工作端先发 `Started` 消息，再持久记录启动，最后才调用执行器。[启动顺序测试](../../tests/test_worker_client.py#L225-L261)分别让发送失败、发送中被取消和本地记录写入失败，三种情况的执行次数都是零，本地仍记为 `received`。控制端却不能仅凭一个空的 `started_at` 得出相同结论，因为它看不到本地发生了哪一种失败。[超时判定](../../app/domain/recovery_policy.py#L61-L77)只在能排除执行、没有待确认停止、且领取租约已过期时才选择重排。
- **日志记录启动，但重连时找不到活进程。** 对普通编码 Job，工作端无法交出原执行器，又没有最终结果。控制端[保留这次分配](../../app/controler/workers/callbacks.py#L974-L995)，不会用相同的任务描述启动一个新进程来代替旧的。排查时请保留原日志、工作区和分支，以便核对是否已经产生提交或远端写入。恢复期限告诉控制端什么时候标记问题，不能告诉它磁盘上发生过什么。
- **控制端已经接收结果，却没有标记应用完成。** 此时检查同一 Job 的远端操作。例如 GitHub 收下了修订评论，连接恰好在回传响应时断开。第一次应用留下状态 `uncertain`；稍后控制端查询原 PR，找到那条评论，补全记录，Job 才变成 `done`。[丢失评论响应的用例](../../tests/test_result_recovery.py#L292-L323)记录到的调用依次是查找、发表评论、再次查找，最终只有一条评论。若一直查不到，又不能证明请求遭到拒绝，重复扫描会保留待处理结果；[三轮扫描的断言](../../tests/test_result_recovery.py#L401-L416)只数到一次发送。

还可以用两个独立信号核对场景一是否真的结束。控制端的 `result_received_at` 说明结果已入库；工作端日志的 `acknowledged` 说明它已收到控制端的业务确认，之后不必再上报这份终态。若数据库已经应用、只是确认没回来，原 worker 可凭相同摘要重放结果。回到[更换控制端的测试末尾](../../tests/test_controller_update_boundary.py#L89-L99)，核对本地收据的提交 SHA 与交付记录相同，同时观察 `released_at` 仍为空：本轮编码已经结束，Flow 还要保留这个位置接评审后的修改。不要把“释放工作区”当作重连成功的验收条件。

## 场景二：准备维护，先停止接新任务

有原工作区的 Flow 可能还会收到评审意见。如果只是准备更新机器，在控制端对指定 worker 执行 `agentflow worker-drain <worker_id>`，再用 `agentflow workers` 查看。此时它不再接新 Flow，已有绑定的工作仍能修订或收尾；重连不会抹掉这个设置。待现有工作真正结束后再维护；确认可以接单时用 `--undo` 撤销 drain。[排空与重连测试](../../tests/test_cancel_drain.py#L81)比较了旧 Flow、新 Flow 和撤销设置后的分配结果。

[CLI 的 `worker_drain`](../../app/cli.py#L209-L213)把 worker ID 和 `not undo` 交给 [`WorkerStore.drain`](../../app/persistence/workers.py#L234-L240)，命令输出的是保存的开关。以后调度器[用 `_assign_coding` 选作业](../../app/persistence/assignments.py#L141-L160)时，原绑定按 worker、目录和代次匹配；一个新 Flow 要占空目录，还必须检查这个开关没打开。这就是为什么同一台机器可以在排空时完成旧 Flow 的修订，新 Flow 却仍留在队列里。

若明确要结束正在执行的任务，才使用取消。取消会请求停止原进程，不能撤回已经推送的提交或已发送的评论。离线时，发出请求和确认停止之间可能相隔很久；[取消后仍保留位置的测试](../../tests/test_cancel_drain.py#L31)覆盖了这段时间。值班处理可从[运维手册](../operations/runbook.md)查看分配与远端回写记录。

取消走的是 [`ControlService.cancel` → `finish`](../../app/controler/workflow/service.py#L132-L145)：以当前 Attempt 请求放弃，在[保存结论时登记停止请求](../../app/controler/workflow/service.py#L175-L186)。worker 返回 `ExecutionStopped` 后，[`StopStore.confirm`](../../app/persistence/stops.py#L119-L165)还要在锁内逐项核对请求 ID、当前连接、Job 和分配令牌，首次确认还要匹配工作区代次。[确认测试](../../tests/test_cancel_drain.py#L47-L65)先用旧连接提交回执，得到 `False`；换回当前连接才成功，重复提交同一回执也能得到确认。

**在准备阶段取消，要看上一个实际派发过的 Job。** 如果这是第一次领取，且控制端还没有授权派发，那么 worker 没有可停的写入者。[首轮准备中取消的用例](../../tests/test_cancel_drain.py#L238-L256)先领取，再立即取消；预期停止队列为空、原 Job 失败、位置可供另一项任务领取。即使旧的准备流程稍后返回，它也既拿不到派发许可，又不能清掉新任务的占用。但如果首轮编码已经结束，控制端正在为同一 Flow 准备修订，[修订准备中取消的用例](../../tests/test_cancel_drain.py#L67-L79)得到的却是指向上一次已派发 Job 的停止请求，位置仍然保留。只读最新一行修订 Job 容易漏掉这段历史。

**维护交接时，分别报告业务状态与机器占用。** [归档后等待停止的场景](../../tests/test_cancel_drain.py#L198-L217)取消一个有 PR 的 Flow 后，按仓库和 Issue 查询已经找不到活跃 Flow；按 Flow ID 检查机器绑定，却仍能看到尚未释放的位置。在停止得到确认并完成后续清理前，不要把这个目录交给另一个执行器。反过来，旧请求若只是迟到了，也不能改变新分配：[重新绑定后的回执测试](../../tests/test_cancel_drain.py#L258-L278)先确认原停止请求，再重新开工并分配给另一台 worker；原 worker 重连时重复确认成功，新绑定及令牌都保持原样。确认回复针对的是原请求，和当前谁能写工作区是两次判断。
