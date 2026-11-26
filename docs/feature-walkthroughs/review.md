# 在同一张 PR 上提问，再要求改代码

从一个已经交付 PR、正在等待评审的 Flow 开始。先在 PR 里问：“导出日期为什么按 UTC 计算？”几条紧挨着的评论可能在提出同一个问题，控制端会等一个短窗口，把相关反馈合在一起判断。刷新后没有马上看到一个新提交，并不说明评论丢失。

一条行内评论到达时，[`EventRouter._on_feedback`](../../app/controler/events/router.py#L116-L153) 把 GitHub 评论 ID、正文、文件路径、原提交及 diff 片段组装成 `FeedbackUpdate`，连同本次 webhook 的投递 ID 交给 [`ReviewStore.ingest`](../../app/persistence/reviews.py#L48-L105)。后者在一个事务内核对 Flow、记下这次投递、保存意见版本，再根据事件类型设置最早处理时间。重复投递返回 `False`，不会重新安排这一轮。这样，评审者编辑旧意见时可以留下新版本，网络重送同一事件却不会算第二条意见。

## 回答问题和修改代码有不同的验收方式

如果这轮需要解释，编码工具会读原工作区，给出针对现有实现的答复。你应能在 GitHub 找到对应回复，PR 顶端不应因为回答问题而改变。负责判断意图的模型只选择接下来要做的事，不代替编码工具编写最终答复。除了回答和修订，遇到信息不足时也可以等待；无须行动的反馈则可以结束处理。[反馈派发测试](../../tests/test_control_triage.py#L42)用不同任务目的检查，同一批反馈不会重复创建工作。

到处理时间，[后台调用 `coordinator.triage(batch, judge)`](../../app/controler/runtime.py#L275-L285)。[协调器](../../app/controler/workflow/coordinator.py#L79-L110)在锁外向 GitHub 读取 PR 和讨论，在锁内保存本批看到的内容，再放开锁等待 `judge.triage(context)` 的决定。资料不全、判断失败或修订把握不足会转为等待。若决定答复或修订，它先重读 PR；[回到事务内](../../app/controler/workflow/coordinator.py#L111-L135)仍是这批意见且两端提交都没变，才把决定、作业和批次进度一起写下。两类工作都是 `REVISION`，worker 读作业里的 `purpose=answer` 或 `revise` 来区分；过时的上下文会失效，不派发旧决定。

接下来，在具体代码行指出“这里请改为使用仓库时区，并补测试”。如果本次判断需要修订，worker 在原仓库和原分支继续。观察原 PR：有代码修改时，新提交应出现在这里，不会另开一张 PR。若其他协作者先推了提交，新一轮要以实际远端内容为准；依据旧版本生成的回答不能照发。这个竞态见[评审期间有新推送的测试](../../tests/test_control_triage.py#L139)。

到了原工作区，[worker 的 `_revision`](../../app/worker/execution/executor.py#L320-L350)按任务目的准备输入：答疑先记住当前本地提交，修订则用原分支与预期提交同步工作区，记下实际起点。有原 session 时把任务提示交给 `code_agent.resume`。编码工具返回后，[worker 检查并填入结果](../../app/worker/execution/executor.py#L389-L412)：答疑期间本地提交变了就报错；修订则调用 `verify_revision_outcome`，把核实的分支、本地和远端提交交回控制端。

收到 worker 结果后，[`verify_answer_remote` 与 `verify_revision_remote`](../../app/controler/workflow/revisions.py#L385-L419)有不同的检查。前者读原 PR，确认它仍然打开，顶端仍是提问时的提交；后者还读分支，核对 PR 和分支现在都指向 worker 报告的 SHA，有新提交时检查它是否从实际同步的版本向前推进。两者用 `None` 表示通过，失败则返回一条具体原因。[`revision_plan`](../../app/controler/workflow/revisions.py#L229-L275)连同批号、意见版本、原会话和工作区一起核对；检查不通过就不会据此声明意见已解决。

## 如何判断一条意见确实处理完了？

看原线程里的说明，再看提交是否解决了具体问题。自动关闭线程需要完整、仍然有效的意见覆盖；评审者在修订途中增加了意见，或线程没有读全，先保留线程打开。评论已回复、新代码已推送和线程已关闭是三个不同的观察结果。[线程覆盖测试](../../tests/test_review_resolution.py#L31)展示何时必须保留未解决状态。

具体到这条行内意见，控制端[先用 `thread.find_reply(marker)` 查自己的处理说明](../../app/controler/workflow/effects.py#L365-L391)。查到就用 GitHub 评论 ID 形成回执；首次发送才调用 `thread.reply`。已经发出但尚未找到标记，就保留未决。[`resolve_thread`](../../app/controler/workflow/thread_effects.py#L82-L108)随后重读线程，核对覆盖、说明的回执和 PR 版本；不满足时返回带 `pending_reason` 的回执，只有全部通过才请求 GitHub 标记已解决。

如果 GitHub 实际收到了回复但网络丢了响应，控制端会先查找原回复。能够核对时继续，不能确认时保留未决状态，不要求评审者再发一次相同意见。[响应丢失后恢复评论](../../tests/test_result_recovery.py#L292)与[无法确认时不重发](../../tests/test_result_recovery.py#L401)分别覆盖这两条路径。最后仍由人依据代码、评审与 CI 决定是否合并；详情中的后续变化见[归档走查](continuation.md)。
