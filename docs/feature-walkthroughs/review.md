# 评审交付：在同一张 PR 上先核对解释，再验收修订

评审模块把 GitHub 上的反馈跟具体 PR、代码版本和讨论线程联系起来。控制端判断这一批意见需要回答、修改、等待还是结束处理；worker 回到相应工作区回答或改代码。最后，控制端核查现在的 PR 是否仍对应当时审阅的内容。作为评审者，你负责给出问题或修改标准，并核对收到的回复、推送的代码和线程状态。三者有时会在不同时间出现。

## 做一轮可复查的评审

从一个已交付、尚未合并的测试 PR 开始。假设它给 CSV 增加了按 UTC 计算的日期列，现在产品方想改为按仓库配置计算。在 Flow 详情点开 PR；在 GitHub 的 Commits 页复制最新提交的完整 SHA，同时记下 PR 号和目标分支。选一段可以定位日期转换的 diff，以它为准开展下面两次交互。如果项目没有这个功能，用你实际看到的逻辑和一组能区分新旧行为的数据替换示例。

1. **只问现有行为。** 在 PR 对话中写：「请只解释当前代码：输入 `2026-01-01T02:15:00+00:00` 时，`export_date` 取哪一天？请指出决定时区的代码位置，本条不用改代码。」让这次答疑结束再提修改要求。几条紧挨着的评论可能属于同一个处理窗口；同时发送会让两类验收标准混在一个批次里。
2. **核对答复所指的版本。** 对照回复打开文件并定位转换表达式；如果实现确实用 UTC，这条记录的日期应为一月一日。回到 Commits，比较 PR 顶端与刚才记下的 SHA。在没有其他人推送的条件下，两者应相同。如果已有新提交，先看作者和 diff，重新确认问题还适用于哪一版。单凭“没有新增提交”不能判定答疑成功，你还需要找到那段解释。
3. **在具体行要求修订。** 切到 Files changed，在计算日期的行添加评审意见：「请从仓库配置取时区，再计算日期。仓库时区为 `America/Los_Angeles` 时，上面的记录应输出 `2025-12-31`；请补一条用例，保留 UTC 配置的行为。」提交这次评审；留在 GitHub 草稿里的意见不宜作为交班依据。需要明确请求修改时，有相应权限的评审者可以选择 Request changes。在允许名单内的评审者本人提交这种 review，才满足路由中该强信号的身份和事件条件；普通行内讨论也能进入评估，但没有相同的强信号。[事件分类与权限判断](../../app/controler/events/router.py#L116-L148)
4. **验收原 PR。** 在原 PR 查看最新提交，打开这次变更涉及的实现和测试，找出仓库时区到底从哪里读入。让测试同时覆盖默认配置和跨年输入。回到提出需求的行内线程，看回复是否说清楚改动位置和测试结果；如果只回复了第一步的问题，还欠这条修订的验收。请等实际反馈，不要为了让系统“听见”而往每个页面重复贴一遍。

下面的小程序让评审者独立算出预期日期，避免用实现自己的输出当作测试标准。用带 IANA 时区数据的 Python 3.9 或更高版本运行。程序只读本机时区数据库，不连接仓库，也不会修改 PR。

```bash
python3 - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo

instant = datetime.fromisoformat("2026-01-01T02:15:00+00:00")
for name, expected in [("UTC", "2026-01-01"), ("America/Los_Angeles", "2025-12-31")]:
    actual = instant.astimezone(ZoneInfo(name)).date().isoformat()
    print(f"{name}: {actual}")
    assert actual == expected, (name, actual, expected)
PY
```

应当看到 `UTC: 2026-01-01` 和 `America/Los_Angeles: 2025-12-31`，随后命令正常退出。洛杉矶的本地时间还在上一年；先截取日期再处理时区就会丢掉这层差别。这个结果验证的是意见里的期望值。要证明 PR 做对了，还要在项目测试中把同一瞬间交给真实导出入口、读取导出的列，再比较字符串。如果本机报告 `ZoneInfoNotFoundError`，先补齐运行环境的时区数据；该错误不能证明 PR 把日期算错了。

## 评论已经发送，为什么还没看到改动？

一条行内评论到达时，[`EventRouter._on_feedback`](../../app/controler/events/router.py#L116-L153) 把 GitHub 评论 ID、正文、文件路径、原提交及 diff 片段组装成 `FeedbackUpdate`，连同本次 webhook 的投递 ID 交给 [`ReviewStore.ingest`](../../app/persistence/reviews.py#L48-L105)。后者在一个事务内核对 Flow、记下这次投递、保存意见版本，再根据事件类型设置最早处理时间。重复投递返回 `False`，不会重新安排这一轮。这样，评审者编辑旧意见时可以留下新版本，网络重送同一事件却不会算第二条意见。

## 回答问题和修改代码有不同的验收方式

如果这轮需要解释，编码工具会读原工作区，给出针对现有实现的答复。你应能在 GitHub 找到对应回复，PR 顶端不应因为回答问题而改变。负责判断意图的模型只选择接下来要做的事，不代替编码工具编写最终答复。除了回答和修订，遇到信息不足时也可以等待；无须行动的反馈则可以结束处理。[反馈派发测试](../../tests/test_control_triage.py#L42)用不同任务目的检查，同一批反馈不会重复创建工作。

到处理时间，[后台调用 `coordinator.triage(batch, judge)`](../../app/controler/runtime.py#L275-L285)。[协调器](../../app/controler/workflow/coordinator.py#L79-L110)在锁外向 GitHub 读取 PR 和讨论，在锁内保存本批看到的内容，再放开锁等待 `judge.triage(context)` 的决定。资料不全、判断失败或修订把握不足会转为等待。若决定答复或修订，它先重读 PR；[回到事务内](../../app/controler/workflow/coordinator.py#L111-L135)仍是这批意见且两端提交都没变，才把决定、作业和批次进度一起写下。两类工作都是 `REVISION`，worker 读作业里的 `purpose=answer` 或 `revise` 来区分；过时的上下文会失效，不派发旧决定。

如果仓库时区的意见进入修订，worker 在原仓库和原分支继续。有代码修改时，新提交应出现在原 PR，不会另开一张。若其他协作者先推了提交，新一轮要以实际远端内容为准；依据旧版本生成的回答不能照发。[评审期间有新推送的测试](../../tests/test_control_triage.py#L139-L181)刻意让推理完成后、创建作业前的顶端变化：协调器丢弃旧上下文，重新判断，最终只把基于新版的解释指令交给 worker。两次判断之间，GitHub 上都看不到那段过期答复。

到了原工作区，[worker 的 `_revision`](../../app/worker/execution/executor.py#L320-L350)按任务目的准备输入：答疑先记住当前本地提交，修订则用原分支与预期提交同步工作区，记下实际起点。有原 session 时把任务提示交给 `code_agent.resume`。编码工具返回后，[worker 检查并填入结果](../../app/worker/execution/executor.py#L389-L412)：答疑期间本地提交变了就报错；修订则调用 `verify_revision_outcome`，把核实的分支、本地和远端提交交回控制端。

收到 worker 结果后，[`verify_answer_remote` 与 `verify_revision_remote`](../../app/controler/workflow/revisions.py#L385-L419)有不同的检查。前者读原 PR，确认它仍然打开，顶端仍是提问时的提交；后者还读分支，核对 PR 和分支现在都指向 worker 报告的 SHA，有新提交时检查它是否从实际同步的版本向前推进。两者用 `None` 表示通过，失败则返回一条具体原因。[`revision_plan`](../../app/controler/workflow/revisions.py#L229-L275)连同批号、意见版本、原会话和工作区一起核对；检查不通过就不会据此声明意见已解决。

## 如何判断一条意见确实处理完了？

看原线程里的说明，再看提交是否解决了具体问题。自动关闭线程需要完整、仍然有效的意见覆盖；评审者在修订途中增加了意见，或线程没有读全，先保留线程打开。评论已回复、新代码已推送和线程已关闭是三个不同的观察结果。[线程覆盖测试](../../tests/test_review_resolution.py#L31)展示何时必须保留未解决状态。

具体到这条行内意见，控制端[先用 `thread.find_reply(marker)` 查自己的处理说明](../../app/controler/workflow/effects.py#L365-L391)。查到就用 GitHub 评论 ID 形成回执；首次发送才调用 `thread.reply`。已经发出但尚未找到标记，就保留未决。[`resolve_thread`](../../app/controler/workflow/thread_effects.py#L82-L108)随后重读线程，核对覆盖、说明的回执和 PR 版本；不满足时返回带 `pending_reason` 的回执，只有全部通过才请求 GitHub 标记已解决。

如果 GitHub 实际收到了回复但网络丢了响应，控制端会先查找原回复。能够核对时继续，不能确认时保留未决状态，不要求评审者再发一次相同意见。[响应丢失后恢复评论](../../tests/test_result_recovery.py#L292)与[无法确认时不重发](../../tests/test_result_recovery.py#L401)分别覆盖这两条路径。最后仍由人依据代码、评审与 CI 决定是否合并；详情中的后续变化见[归档走查](continuation.md)。

交班时写下意见链接、对应提交和仍待处理的问题。例如：「跨年的日期用例已在新提交通过；行内线程后来又问了缺少时区配置时的默认值，仍要回答。」若有人在关闭线程的瞬间追加评论，GitHub 的关闭动作本身没有条件检查；控制端会在操作后重新读取，发现未覆盖意见就留下未决原因，不会擅自把远端线程重新打开。即使 GitHub 显示已解决，也请读一下最后一位评审者写了什么。[关闭后的二次核对](../../app/controler/workflow/thread_effects.py#L103-L128)
