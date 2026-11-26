# 工作端断开连接，任务会再跑一遍吗？

假设一个 Flow 正在编码，控制端在维护时重启。详情页可能一时没有新消息，但编码进程和本地仓库仍在 worker 上。要区分“看不到进度”和“需要再执行”，可以在独立测试环境按下面两种场景观察，生产环境不要为了验证而中断服务。

## 场景一：保留原工作端，让它重新连接

1. 选择已被 worker 领取并开始编码的测试任务，记下 Flow ID、当前 Job 和仓库分支。保留原 worker 进程、工作区和本地执行记录，只让它到控制端的连接中断。
2. 恢复同一数据库上的控制服务，让同一身份的 worker 重连。控制端和工作端会核对原来的分配记录，再继续接收工具请求和最终结果。若编码已在本机完成，需要补传的是结果；不应看到第二次独立的编码执行。
3. 对照原来的 Flow 和分支检查交付：最终只有一次对应的编码和一张 PR。[控制端更换后只完成一次的测试](../../tests/test_controller_update_boundary.py#L38)故意在编码已经开始、结果尚未交回时断开连接，正好可以区别正常恢复与“从头再跑一次”。

断线后，两边各自掌握一部分证据。新控制端启动时[调用 `recover_abandoned_assignments`](../../app/controler/runtime.py#L185)，[在数据库事务内](../../app/persistence/workers.py#L194-L214)把 worker 标为离线，并给可能已经执行的编码作业与未释放的绑定留下恢复期限。原 worker 重新注册时，[`WorkerExecution.recovery()`](../../app/worker/execution/lifecycle.py#L179-L192)先收集刚刚结束的执行，再从本地记录生成恢复说明，包括执行器是否还在；[注册消息](../../app/worker/transport/state.py#L177-L191)只带上尚未获确认的记录。断开网络没有删除这些记录。[同一场景的断言](../../tests/test_controller_update_boundary.py#L64-L98)检查断线时执行器仍活着，重连后原结果获确认，编码调用总数仍是一。

对每条恢复说明，[控制端的 `_recover`](../../app/controler/workers/callbacks.py#L939-L973)按 Job ID 查库，比较 worker 身份、这次分配的令牌以及双方已有的结果摘要。对不上就拒绝恢复，不会把别的 Job 当作这次交付。核对通过、[重新绑定到新连接后](../../app/controler/workers/callbacks.py#L986-L1006)，还在运行的作业收到 `resume`，本机已结束的收到 `replay`。[worker 收到决定](../../app/worker/transport/state.py#L237-L251)后，前者调用 [`WorkerExecution.resume`](../../app/worker/execution/lifecycle.py#L194-L213)接回同一个活执行器，后者补传原结果。这里传输可以重试，编码无需重跑。

若原机器不能回来，情况不同：控制端可能已经允许执行，只是没有收到启动确认。此时等待租约过期也不能证明命令从未运行。工作区会继续保留，任务可能需要人工核查；不要删除本地记录或另起一份任务来制造一个“空闲”位置。见[丢失启动确认的测试](../../tests/test_cancel_drain.py#L220)。

## 场景二：准备维护，先停止接新任务

有原工作区的 Flow 可能还会收到评审意见。如果只是准备更新机器，在控制端对指定 worker 执行 `agentflow worker-drain <worker_id>`，再用 `agentflow workers` 查看。此时它不再接新 Flow，已有绑定的工作仍能修订或收尾；重连不会抹掉这个设置。待现有工作真正结束后再维护；确认可以接单时用 `--undo` 撤销 drain。[排空与重连测试](../../tests/test_cancel_drain.py#L81)比较了旧 Flow、新 Flow 和撤销设置后的分配结果。

[CLI 的 `worker_drain`](../../app/cli.py#L209-L213)把 worker ID 和 `not undo` 交给 [`WorkerStore.drain`](../../app/persistence/workers.py#L234-L240)，命令输出的是保存的开关。以后调度器[用 `_assign_coding` 选作业](../../app/persistence/assignments.py#L141-L160)时，原绑定按 worker、目录和代次匹配；一个新 Flow 要占空目录，还必须检查这个开关没打开。这就是为什么同一台机器可以在排空时完成旧 Flow 的修订，新 Flow 却仍留在队列里。

若明确要结束正在执行的任务，才使用取消。取消会请求停止原进程，不能撤回已经推送的提交或已发送的评论。离线时，发出请求和确认停止之间可能相隔很久；[取消后仍保留位置的测试](../../tests/test_cancel_drain.py#L31)覆盖了这段时间。值班处理可从[运维手册](../operations/runbook.md)查看分配与远端回写记录。

取消走的是 [`ControlService.cancel` → `finish`](../../app/controler/workflow/service.py#L132-L145)：以当前 Attempt 请求放弃，在[保存结论时登记停止请求](../../app/controler/workflow/service.py#L175-L186)。worker 返回 `ExecutionStopped` 后，[`StopStore.confirm`](../../app/persistence/stops.py#L119-L165)还要在锁内逐项核对请求 ID、当前连接、Job 和分配令牌，首次确认还要匹配工作区代次。过期回执返回 `False`，不能据此释放原工作区；[确认测试](../../tests/test_cancel_drain.py#L47-L65)覆盖了用错连接或请求的情况。
