# 三个容易混淆的完成时刻

一项任务可能同时显示「PR 已合并」「Flow 已归档」和「工作区仍在收尾」。这三条记录看起来矛盾，其实分别回答了三个问题：GitHub 上发生了什么？用户的工作是否已经结束？执行代码的机器是否安全空闲？AgentFlow 把这些问题分开回答，原因可以从三个常见的中断说起。

## 一、代码已经推送，但 PR 还没有创建

工作端负责改代码和推分支；控制端持有 GitHub App 身份，负责创建 PR。若工作端刚推送就断开连接，仅凭分支存在，控制端无法知道这是最终交付，还是编码者打算继续修改的中间提交。首轮因此要求明确选择：申请开 PR，或者留言请求人来决定。搜索同仓库任务是只读操作，不代表完成了上述任一种交付。

1. **工作端证明推了什么。** 编码者只给 `open_pr` 标题和正文。[`WorkerMCP._dispatch`](../../app/worker/transport/mcp.py#L166-L186) 调 [`verify_pushed_outcome`](../../app/worker/execution/vcs.py#L319-L352)，从本次工作区核验 Git 或 jj 的分支、提交和远端 ref；没有通过的后端，或两个后端给出不同结果，就不发请求。通过后生成 `call_id`，连同核验结果发送 `flow.open_pr`。
2. **控制端只确认本次开工的交付意图。** [`on_tool`](../../app/controler/workers/flow.py#L62-L76) 检查 Job 与 Flow，带上连接的 worker ID 和 epoch 调 `FlowToolService.execute`。[`_open_pr`](../../app/controler/workflow/tools.py#L109-L168) 还核对 assignment token、当前 INITIAL Job 和 Attempt、未释放的绑定及交付分支。[同一调用已有回执就返回](../../app/controler/workflow/tools.py#L183-L202)；首次准备则读 GitHub ref，若 SHA 相同才在锁内复核执行权和交付版本，把 `opening`、标题、正文、head SHA 与工具回执一起提交。核验中被取消或换了连接，不能提交这次准备。此时拿到的 `prepared` 回执还没有 PR 号。见[保存回执的事务](../../app/controler/workflow/tools.py#L211-L266)。
3. **终态通过核验，才有创建 PR 的计划。** [`ResultApplication._prepare` 调 `initial_plan`](../../app/controler/workflow/results.py#L353-L367)。[`initial_plan`](../../app/controler/workflow/initial.py#L240-L293) 要求成功退出且有会话；[`initial_delivery_mismatch`](../../app/controler/workflow/initial.py#L296-L343) 将最终结果的 Attempt、分支、SHA、调用 ID 和回执与持久记录比对。对不上就记录失败，不授权创建。通过才由[外部操作执行器](../../app/controler/workflow/effects.py#L392-L415)按 head 查 PR，找不到且尚未派发才发创建请求。

行为测试：[准备成功及重放均不创建 PR](../../tests/test_flow_tools.py#L63-L90)、[远端读取期间取消](../../tests/test_flow_tools.py#L150-L172)。完整例子见[首轮交付走查](../feature-walkthroughs/first-delivery.md)。

## 二、对方做完了，但回信在路上丢了

这里有两种可以找回的回执：编码任务的最终结果，以及 GitHub 已执行操作的证据。两者分开保存。

**工作端 → 控制端。** [执行结束](../../app/worker/execution/lifecycle.py#L175)时调用 [`ExecutionState.complete`](../../app/worker/state/execution.py#L194-L220)，保存去掉临时推送凭据的终态；[重传](../../app/worker/execution/lifecycle.py#L264)调用 [`replay`](../../app/worker/state/execution.py#L188-L192)，缺少终态便报错。控制端[接收回调](../../app/controler/workers/callbacks.py#L567-L588)调用 [`record_assignment_result`](../../app/persistence/assignments.py#L429-L482)：按 Job、worker、token、当前 epoch 查记录，首次接收还检查 assignment 有效，写 payload、摘要和接收时间。重复终态摘要相同就返回已应用或待处理；摘要不同返回冲突。接收成功不代表 GitHub 操作已完成。

**控制端 → GitHub。** [恢复循环](../../app/controler/runtime.py#L253-L258)从未应用的持久结果继续。[`ResultApplication._apply`](../../app/controler/workflow/results.py#L220-L232)先在事务内保存计划并核对结果摘要，事务外才调用 `ResultEffects.run`。以评论为例，[`_execute`](../../app/controler/workflow/effects.py#L363-L391)先按稳定标记查找已有评论；尚未派发才经 [`_dispatch`](../../app/controler/workflow/effects.py#L224-L285)复核当前工作并写派发标记，然后发送。若评论已发但回信丢了，找到评论就用其 ID 补回执；已有派发记录却查不到时，保持 `ResultPending`，人的重试也不会再发一遍。见[补回执](../../tests/test_result_recovery.py#L292-L323)和[结果未知时不重发](../../tests/test_result_recovery.py#L401-L418)两种测试。

两端都没有终态时，不能把重传换成重新执行命令。只有证明未启动的任务才可能在租约到期后重排；可能已执行的任务超过恢复期限会受阻，见[恢复判定](../../app/domain/recovery_policy.py#L61-L77)。另一个容易误读的超时是编码器的[空闲计时](../../app/worker/execution/idle.py#L7)：[Codex 等待 I/O 时](../../app/worker/execution/codex.py#L258)限制的是连续无输出时间，不是整项工作的总时长。

## 三、人的工作结束了，机器还没有交还目录

**合并结束业务。** [`ControlService.on_merged`](../../app/controler/workflow/service.py#L263-L300)在 Flow 锁内核对 webhook 的仓库、installation、事件动作及 PR 所属交付，并按事件 ID 去重，再调用 [`record_merge`](../../app/controler/workflow/lifecycle.py#L268-L303)。它拒绝矛盾的合并时间或提交；旧 Attempt 的迟到事件只补历史。当前 Attempt 则记录合并事实，请求停止、撤下排队工作、登记合并通知并成功归档，无需等离线 worker 回来。

**停止证据和结果决定能否交还目录。** [worker 回执处理](../../app/controler/workers/callbacks.py#L625-L656)调用 [`StopStore.confirm`](../../app/persistence/stops.py#L119-L178)。它核对原请求、worker、epoch、Flow、Job、token；首次确认还要匹配原绑定和工作区代次。进程退出的证据可先入库，有待应用结果或未确认的相关副作用则仍不完成确认。对已归档任务，[`try_archive` 调 `release_flow_execution`](../../app/persistence/flows.py#L394-L404)；[释放事务](../../app/persistence/flows.py#L356-L392)检查该代次槽位空闲、无未确认停止及悬而未决的执行或操作，才写 `released_at` 并把代次加一。绑定未释放时，[重新开工会被拒绝](../../app/controler/workflow/service.py#L191-L212)。见[归档无需等 worker](../../tests/test_merge.py#L141)、[未证明退出不可解锁](../../tests/test_archive_recovery.py#L128)和[续办走查](../feature-walkthroughs/continuation.md)。

目录归属解决的是调度冲突，并不提供操作系统级的隔离。工作端使用限时、限仓库的推送授权，不接收 App 私钥；更新 skill 仓库使用独立的只读授权。Codex 和 Amp 当前拒绝受限的执行配置，运行者仍需用专用账户、容器或虚拟机保护控制端机密。不要把两项工作分配到不同路径就视为可以安全接入不互信的租户。详见[凭据注入](../../app/worker/execution/credentials.py)、[执行权限](../../app/worker/execution/permissions.py)和[部署说明](../deployment/README.md)。
