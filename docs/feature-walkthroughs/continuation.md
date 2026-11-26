# 继续昨天的工作，还是重新开工？

沿用同一个 Flow 时，有三种看起来相似的操作：补充要求、重试、重新开工。它们保留的历史不同。先打开详情，记下当前开工记录（Attempt）和关联 PR，再根据手里的任务选择操作。

## 工作没结束：补一条要求

例如评审中才发现导出的日期还需要显示时区。用控制台的追加任务入口写清楚要求，可附图；已有 PR 时，新要求会进入这次开工的修订流程。详情先显示输入已登记，等允许派发时才会交给编码工具。它不会把用户刚输入的字直接插进一个正在运行的命令。

通过 API 提交时，同一个请求 ID 和同样的输入重试，只应留下一条记录；沿用同一个 ID 却改了内容，会收到冲突响应。这个区别在网络超时后尤其重要：重试原请求和提出第二项新要求应当能区分。见[追加只记录一次](../../tests/test_console_api.py#L396)和[不能立即执行时先保存](../../tests/test_console_api.py#L473)。若原 Job 执行失败而目标不变，使用重试继续当前 Attempt，无需创建新任务。

追加时，[`ControlService.append_agent_task`](../../app/controler/workflow/service.py#L309-L334)在 Flow 锁内比较旧请求、保存新消息，接着询问 [`_queue_agent_inputs`](../../app/controler/workflow/service.py#L344-L378)能否安排工作。如果本轮还有排队或运行中的作业，它返回 `False`，已记录的消息仍保留。已有打开的 PR 且可以派发时，它按顺序取出本轮消息、建评审批次，把 `purpose=revise` 的作业与批次关联后返回 `True`，提交后才唤醒调度。

点「重试」则不带新正文：[API 调用 `ControlService.retry(flow)`](../../app/controler/console/api.py#L309-L338)。例如首次编码失败后，服务会在[当前 Attempt 内创建替代作业](../../app/controler/workflow/service.py#L480-L512)，带上失败作业的输入，再通知调度。[连点两次的测试](../../tests/test_console_api.py#L907-L926)检查了第一次返回 `queued`、Attempt ID 不变，第二次因已在排队而收到 `409 retry_no_change`，没有第三个作业。若已归档，仅有符合条件的归档通知可补做，否则收到 `retry_archived`；两者都不会开始新 Attempt。[归档后的重试分支](../../app/controler/workflow/service.py#L405-L433)

## 合并后：先看结论，再看能否重新开工

在 GitHub 合并当前 PR。控制端确认合并后，详情应显示 `archived`，原因为 `succeeded`；该任务从活跃列表移出。即使原 worker 当时离线，这个已经发生的合并也不会撤销。反过来，归档结论不能证明旧进程已停止：未确认安全释放时请求重新开工，服务会拒绝（HTTP 409）。[合并但清理尚未完成的测试](../../tests/test_console_api.py#L744)同时检查了详情、活跃列表和拒绝结果。

[收到合并事件的 `on_merged`](../../app/controler/workflow/service.py#L263-L300)在锁内校验仓库、安装、PR 归属和投递 ID，再把 GitHub 的合并时间和提交交给 [`record_merge`](../../app/controler/workflow/lifecycle.py#L280-L303)。对当前一轮，它在同一事务里保存事实、发起停止、撤下未执行作业、记录通知和归档结论；提交后才通知后台。若事件属于旧 Attempt，则只更新历史。因此不必等 worker 在线才能确认合并，旧 PR 的迟到事件也不会结束新一轮。

没有 PR 的工作也可以由所有者结束，记录成功或放弃及相应说明；这不会制造一次 GitHub 合并。若原执行还需停止，先完成收尾。不要把“重试”当作撤销归档：它不能重新打开一个已归档 Flow。

[归档按钮](../../app/controler/console/api.py#L266-L281)把页面看到的 Attempt ID、原因和结论交给 [`ControlService.finish`](../../app/controler/workflow/service.py#L141-L186)。选择成功却没写结论，或已有 PR 但尚未合并，都会返回 `archive_rejected` 并保留原状态。可以结束时，服务写下结论并尝试收尾，接口重新读取详情。在[放弃运行中任务的测试](../../tests/test_console_api.py#L853-L889)里，响应已经是 `archived`，但停止请求仍待 worker 确认、工作区尚未释放。

## 有了新目标：在同一个 Flow 下重新开工

当旧绑定等收尾条件已满足，从归档详情重新开工。服务核对你看到的 Attempt 仍是当前这一轮；过时的页面不能替新的 Attempt 再开一轮。成功后比较前后的详情：Flow ID 相同，Attempt ID 已改变，任务回到排队；旧的归档说明仍属于原 Attempt，新工作不会接管原来的 PR。[归档与重新开工测试](../../tests/test_console_api.py#L765)也检查重复归档、误用重试和过期请求。

这里的 [`resume` 接口](../../app/controler/console/api.py#L285-L306)等的是控制端的事务。[`ControlService.resume(flow, expected_attempt_id=…)`](../../app/controler/workflow/service.py#L189-L227)重新锁定 Issue 和 Flow，检查归档记录、原执行已释放、同一 Issue 没有别的活跃任务；任一前提变化，接口返回 `resume_rejected`。通过后，服务重置本轮同步进度、开始新 Attempt；原来有交付分支时，为新一轮预留带新 Attempt ID 的分支，然后排入 `INITIAL`。服务返回更新后的 Flow，接口再读出详情。此时可核对的是**新一轮已排队**，要确认工作已发生仍需等 worker 交付。
