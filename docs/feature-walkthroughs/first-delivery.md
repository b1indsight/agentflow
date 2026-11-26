# 提出需求后，第一份交付是什么？

要让 AgentFlow 给导出功能加日期选项，先选一个已安装 GitHub App 的测试仓库，确认有能处理该仓库的 worker。你需要有触发任务的权限；如果走控制台，则要以部署指定的所有者身份登录。两种入口任选一种，否则你可能是在观察两项不同的任务。

## 从任务详情辨认是否开工

在 Issue 中说明日期格式、时区和预期输出，由允许触发的人加上触发标签（默认 `flow`）。没有合适的 Issue 时，可在控制台选择仓库直接提交相同的正文，必要时附图。打开新任务的详情并记下 Flow ID。排队表示服务端已经接下任务；在 worker 领取之前，远端没有新 PR 是正常的。

[Issue 的处理方法 `_on_issues`](../../app/controler/events/router.py#L60-L76) 从标签事件取仓库名、Issue 号和安装 ID，核对标签和操作者；不在允许名单内，方法就返回，不创建 Flow。控制台走[另一个入口 `create_flow`](../../app/controler/console/api.py#L225-L253)：先异步向 GitHub 核对仓库、按需读取 Issue，再把正文和附件一起交给 `ControlService.create_flow`。两条路在[同一段事务](../../app/controler/workflow/service.py#L106-L130)汇合：写入 Flow、保存附件、排入首个 `INITIAL` 作业，成功才通知调度。若同一 Issue 已有活跃 Flow，服务返回 `None`，控制台报 `flow_conflict`；没有 Issue 号的两次独立创建不按正文去重。

有 worker 可用时，[连接上的 `poll`](../../app/controler/workers/callbacks.py#L325-L335)用 worker ID、分配令牌和当前连接向 `assign_job` 领取作业，取不到就结束本次查找，取到才异步准备。[`InitialPreparation.prepare(job, flow)`](../../app/controler/workflow/initial.py#L71-L118)先确认作业属于本轮、工作区仍归这台 worker，再读任务正文和远端仓库；首次开工还要等分支命名结果。[返回 `InitialTask` 前](../../app/controler/workflow/initial.py#L119-L156)，它查询远端同名分支，并在锁内再次确认 Attempt、记下目标分支、合入已登记的要求。

准备好后，[`_on_prepared`](../../app/controler/workers/callbacks.py#L818-L840)检查是否仍允许派发，才发出带任务及工作区身份的 `worker.execute`。工作端的 [`on_execute`](../../app/worker/transport/state.py#L324-L345)读取本地执行记录；若已开始过，重收同一指令不会再启一个 Agent。

## 路径一：有明确改动，需要你审阅

编码工具在分配到的目录里工作，提交并推送到本次指定的分支，然后请求开 PR。等详情出现 PR 链接，再点开 GitHub 检查：目标仓库和分支应当正确，PR 顶端应是这次交付的提交。此时任务进入 `review`，由你审核。工具调用的回执或会话里一句“已经完成”，都不能代替这张实际的 PR。[远程交付测试](../../tests/test_remote_e2e.py#L167)检查从领取到关联 PR 的整条路径。

为什么工具成功了还要等？第一次调用 [`_open_pr`](../../app/controler/workflow/tools.py#L226-L266) 时，控制端复核本次分配仍有效，把已核对的提交 SHA、标题、正文存下，返回 `PreparedDelivery`，里面还没有 PR 号。等 worker 交回终态结果，[`initial_plan`](../../app/controler/workflow/initial.py#L254-L293) 才把结果和保存的工具调用对上，生成创建请求。控制端[先存计划，离开事务再执行远端操作](../../app/controler/workflow/results.py#L225-L233)：按分支查找已有 PR，确实尚未发出请求才调用 [`repository.open_pull_request`](../../app/controler/workflow/effects.py#L392-L405)。上次已发出却无法确认结果时会等待核对，不盲目再开一张。[测试最后逐项核对](../../tests/test_remote_e2e.py#L225-L238)了 PR 号、远端 SHA、工具回执以及实际开 PR 的次数。

## 路径二：还缺一个人的决定

例如需求只写了“当地时间”，却没说是读者所在时区还是仓库配置的时区。编码工具可以留下这个问题，暂停而不创建 PR。详情会显示待办消息；此时同样可能是 `review`，成功的标志是**人看得到问题，而且没有凭空多出一张 PR**。在同一个 Flow 里补充答案，原会话可接着处理，已经留下的本地工作也不应被丢弃；若决定不做，可结束本轮。[等待输入的行为测试](../../tests/test_remote_e2e.py#L248)覆盖继续交付和直接归档两种结果。

这条路在同一个 [`initial_plan` 的 `input_request` 分支](../../app/controler/workflow/initial.py#L260-L281)分开：返回的问题和已推送的提交一起记入等待状态，计划里没有开 PR 的动作。所有者补答后，[`_queue_agent_inputs`](../../app/controler/workflow/service.py#L362-L377) 为这次等待中的交付排一个带 `owner_messages` 的 `INITIAL`；[worker 用原 session 调用 `resume`](../../app/worker/execution/executor.py#L183-L198)。所以测试里有两个首次交付作业，却只启动过一次新会话，最后也只有一张 PR；若选择归档，则始终没有 PR。[两条分支的断言](../../tests/test_remote_e2e.py#L296-L322)

还有一种情况确实是失败：首次准备时，远端已经有同名分支。系统不会把它当作自己的工作，原分支保持原样，也不创建 PR。详情里的准备错误应与暂停提问区别处理；不要为了让任务通过就删除或强推那条分支。见[同名分支保护测试](../../tests/test_remote_e2e.py#L326)。
