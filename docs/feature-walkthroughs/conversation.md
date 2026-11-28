# 读一段 Flow 会话，再给 Agent 补一句话

假设 Agent 已经开始处理一项任务，你想先看它试过什么，再告诉它「添加键盘导航，保留现有行为」。在控制台打开这个 Flow 的「会话」即可。这里有两个不同的确认点：看见一条用户消息，说明 control 已经保存它；要确认 Agent 做了什么，还得看对应执行的回复、工具活动或结果。[消息保存与修订任务的行为测试](../../tests/test_console_api.py#L396-L451)

## 这页由谁提供内容，谁接收你的输入？

先把 Flow 理解成一项持续跟踪的任务，Attempt 是任务的一轮执行，Job 是这轮中某一次可调度的工作。看会话时可以往回读旧轮次；发消息时，服务端按**提交当时的当前轮次**记录。下面的步骤会用到三处，认清它们后更容易判断一次操作有没有成功。

| 模块 | 你会在什么时候用到它 |
|---|---|
| 浏览器里的「会话」和 control 的活动接口 | 打开 Flow、滚动、展开工具结果时。它们展示已保存的活动，可以同时读到 GitHub 讨论、用户补充要求和 Worker 的公开记录。只读已发布的快照，即使别处正同步新资料，翻这一页仍有明确的版本范围。[本地分页](../../app/persistence/activity.py#L303-L338) |
| 后台采集器与执行机上的编码后端 | 核对来源是否齐全时。采集器按来源取下一页，Worker 能提供指定 session 的编码记录，GitHub 提供评论等其他资料；一次 Worker 读取成功不代表 GitHub 也同步好了。读 Worker 的实时接口可以单独核对一段。[分来源采集](../../app/controler/console/sync.py#L102-L145) |
| 输入存储与工作流服务 | 点击「发送」时。服务先确保消息在当前 Flow 下只记录一次，再判断当前轮次是否可以开始工作。Agent 的工作结果沿作业和采集链路返回，输入框收到的响应本身不含一次新的 Agent 答复。[消息记录](../../app/persistence/agent_inputs.py#L41-L67) · [派发判断](../../app/controler/workflow/service.py#L309-L375) |

## 进入条件

用配置好的 [Access owner 账号](../deployment/access.md) 登录，选中已有 Flow。读先前保存的内容不要求 Worker 此刻在线，也不要求任务已有 PR。要查 Worker 的最新原始记录，则需要该轮已有会话 ID、能定位到负责的 Worker，且源文件或 thread 仍可读。已经归档的任务可以阅读；收尾中的任务可以留言，归档后不能再追加。[无 PR 时的采集测试](../../tests/test_console_sync.py#L70-L85) · [收尾与归档的界面测试](../../app/controler/web/e2e/console.spec.ts#L586-L655)

## 一、在「会话」里还原当时发生了什么

1. **读已保存的页面。** 进入 `/#flow/42/conversation`（将 42 换成实际 ID）。浏览器请求 `/api/flows/42/activity`，读的是 control 本地已发布的快照。同一页可以有用户的追加要求、从 Worker 采集的公开消息和 GitHub 讨论。界面按当前快照分页，滚动到边界会继续读取；看到「已到当前本地快照末尾」只是读完当前版本。[活动 API](../../app/controler/console/api.py#L394-L422) · [分页和快照末尾的界面测试](../../app/controler/web/e2e/console.spec.ts#L2645-L2699)
2. **把答复和工具操作对起来。** 先读助手的说明，再展开「工具活动」查看调用和结果。较长的记录可以单独续读正文；正文加载失败可点「重试正文读取」，无需丢弃整页。工具摘要可能已经截断，没显示的字符不能据此推断。[工具折叠测试](../../app/controler/web/e2e/console.spec.ts#L2645-L2699) · [正文续读与原位置重试](../../app/controler/web/e2e/console.spec.ts#L3580-L3650) · [工具摘要截断测试](../../tests/test_console_sync.py#L148-L161)
3. **确认记录是否追到最新。** 点「刷新当前视图」会请求后台同步，同时读当前已提交的活动；并不等待远端把所有消息传完。「会话数据状态」会区分「尚无执行会话」和「执行会话：会话读取超时」。后者表示这一次没读成功，已经保存的正文仍可见。等 Worker 恢复后再刷新；如果来源一直缺失，保留这段缺口，不把空白解释为 Agent 什么也没做。[后台读一页并保存游标](../../app/controler/console/sync.py#L102-L121) · [状态提示与正文同时可见的测试](../../app/controler/web/e2e/console.spec.ts#L2137-L2165)
4. **对照消息里的文件。** 直接点击 Worker 消息里的文件链接，或聚焦后按 Enter。详情区有 960 px 宽时，原消息留在左边，文件在右边独立滚动；达到 1440 px 时可以再打开一栏。栏位不足时，在最右栏的文件名页签之间切换。手机上仍是单栏，选「会话」回到阅读位置。[并排预览与窄屏切换测试](../../app/controler/web/e2e/console.spec.ts#L7168-L7281)
5. **把文件移到想读的位置。** 按住侧栏文件的页签头，拖到「执行记录」右边的插入线处松开。若这是唯一的侧栏文件，正文会占满详情区；点「会话」回到原消息。三个区域并排时，也可以从右栏拖到中栏的某个文件前后，左栏不变。[跨栏拖放浏览器用例](../../app/controler/web/e2e/console.spec.ts#L7046-L7164)

翻页和刷新都从已读页尾读最新已提交的活动；显式传 `snapshot` 可从同一位置读指定版本。只有 `refresh=true` 会在[活动接口](../../app/controler/console/api.py#L395-L420)登记后台同步。后台稍后领取任务，[`ConsoleSynchronizer.sync`](../../app/controler/console/sync.py#L102-L121)用另一套游标向 worker 读一页，将记录、下一游标和缺口说明交给 [`ConsoleSyncStore.finish`](../../app/persistence/console_sync.py#L98-L144)。后者在事务内核对自己仍有权发布本页，再写活动；旧任务迟到会返回 `False`。[未提交页面对读者不可见的测试](../../tests/test_console_sync.py#L700)解释了为什么同一页不会显示一半新、一半旧的同步结果。

### 实例：刷新之前和之后各看什么

假设第一次打开页面时已经有「修复键盘导航」这段讨论，但没有测试输出。先记下你查看的是哪个 Flow、哪次执行以及来源提示。如果状态是「尚无执行会话」，即使 GitHub 评论已经可见，采集器也还拿不到编码记录；如果是「会话读取超时」，说明最近一次读取没有按时完成。两种情况都可以继续读已保存的评论，不要先重发任务来试探 Agent 是否在线。[来源状态与已存正文的界面断言](../../app/controler/web/e2e/console.spec.ts#L2137-L2165)

在浏览器 Network 面板中，假设版本 605 已读到 `a,b,c`，版本 734 补录了 `x`、追加了 `d`。刷新发出 `GET /api/flows/42/activity?cursor=C&refresh=true&since=605`，其中 `C` 是 `c` 的游标。响应只返回 `d`，不会为了找回阅读位置而从头翻页。向上翻回旧页时，浏览器才在 734 中读这段区间，显示 `a,x,b,c`。如果只有补录而没有追加，刷新返回空页和新版本，上翻仍能看到 `x`。[游标和快照的数据库回归](../../tests/test_console_archive.py#L63-L87) · [浏览器请求与阅读位置回归](../../app/controler/web/e2e/console.spec.ts)

如果旧快照的工具调用没有结果，先展开同一执行里相邻的工具活动，确认缺的是结果还是折叠的正文。若只有较长正文加载失败，点正文自己的重试；若当前已发布页本身没有后续记录，看来源状态并刷新。两个操作影响的范围不同：前者尝试读回已存的文字，后者让后台有机会发现新的来源内容。若仍有缺口，再用第三节的方法查原 session。[工具折叠行为](../../app/controler/web/e2e/console.spec.ts#L2645-L2699) · [重试读取正文](../../app/controler/web/e2e/console.spec.ts#L3580-L3650)

## 二、把补充要求送给这一轮执行

在「与 Agent 交流」输入框写下要求并发送；也可以用 `Ctrl+Enter`。页面发出 `POST /api/flows/{id}/agent-tasks`，携带本次的 `request_id`。成功后输入框清空，状态显示「消息已记录」，会话中增加一条 `source=agent_task` 的用户消息。它和 Worker 后续回传的 `user` 输入属于不同来源。[界面发送与确认测试](../../app/controler/web/e2e/console.spec.ts#L536-L577) · [服务端记录归属](../../tests/test_console_api.py#L401-L435)

[输入框的 `onSubmit`](../../app/controler/web/App.tsx#L1128-L1148)把原文、请求 ID 和文件交给 [`appendAgentTask`](../../app/controler/web/api.ts#L269-L287)。浏览器等到的是更新后的 `FlowDetail`。[服务端入口](../../app/controler/console/api.py#L341-L363)在数据库工作线程里调用 [`ControlService.append_agent_task`](../../app/controler/workflow/service.py#L309-L334)：锁住这个 Flow，按请求 ID 对比含附件引用的正文；新请求才调用 [`AgentInputStore.record`](../../app/persistence/agent_inputs.py#L56-L67)写入当前 Attempt 的用户消息。未开始收尾的消息还会标成待交给 Agent。记录与当下可安排的作业在同一事务内完成，事务后才通知 worker。[写入失败的测试](../../tests/test_console_api.py#L649)检查了两者一起回滚。

接下来取决于 Flow 的状态。在一个已有可继续评审的 PR、Worker 和原会话的例子里，这条要求会形成修订作业，继续使用原分支、PR 和 session。若尚未首次执行，control 可以先保存多条要求，等首次准备任务时按接收顺序交付。若正在运行或收尾，保存成功也不代表立刻并行派发第二份作业。[原会话修订](../../tests/test_console_api.py#L428-L451) · [只记录、不触发不安全派发](../../tests/test_console_api.py#L473-L514) · [首次执行的消息顺序](../../tests/test_console_api.py#L516-L532)

**如果发送后断网，使用「重试发送」。** 页面保留原输入、附件和请求 ID；同一个 ID 携带相同内容再次提交，只会记录一次。不要改一个新 ID 来试探上次是否成功；同一 ID 改了内容则会收到 `409 agent_task_request_conflict`。刷新会话或重试读取是读操作，都不会替你重发这条消息。[客户端重用原请求](../../app/controler/web/e2e/console.spec.ts#L541-L562) · [服务端去重与冲突测试](../../tests/test_console_api.py#L398-L427)

### 在网络面板核对一次发送

以下示例只展示已认证、同源请求的业务字段。假设 Flow 42 有一个仍打开的 PR，当前轮次处于评审阶段，有可继续的执行绑定，无等待中的作业、停止请求或活动评审批次。用户输入两行文字并发送：

```http
POST /api/flows/42/agent-tasks
Content-Type: application/json

{"request_id":"12d32941-85ef-4b1c-bbbd-25b710b9e577","instruction":"添加键盘导航\n保留现有行为"}
```

这次操作可以分三步核对。**接收：**返回 200，响应是 Flow 详情；在具备修订条件的测试里 `phase` 变成 `queued`，原分支未变。**记录：**随后从 `/api/flows/42/activity` 可以读到一条 `source=agent_task`、`author=user`、正文为那两行文字的消息，`attempt_id` 属于当前执行。**交付：**等 Worker 领取并准备修订作业，任务指令才包含这条消息；在测试用的 `flow/7` 分支、PR 17 上，准备结果仍指向原来的分支、PR 和 session。即使前两个确认点都满足，也不代表修订已通过测试或推送到远端。[HTTP、活动与准备结果的连续断言](../../tests/test_console_api.py#L401-L451)

对于新发的一句话，界面应生成新的 `request_id`；对于同一次发送的网络重试，则始终用原 UUID。举例说，上面请求在服务端已经写成功，只是用户关掉了网络：再提交完全相同的 JSON 应返回 200，读活动仍然只有一条，这项请求也只会安排一次修订。若同一 ID 的正文改为「只做鼠标交互」，返回 409，原要求不会被覆盖。如果确实要变更指令，应把变更作为新的消息说明清楚，让旧消息与新决定都可追溯。[重复、冲突与并发请求测试](../../tests/test_console_api.py#L411-L432) · [两个并发提交](../../tests/test_console_api.py#L603-L619)

收到 422 则先查输入。`instruction` 会去首尾空白，最多 16,000 字符，不能含空字符；没有图片时必须有实际文字。可选 `images` 最多四项，传空字符串作为指令且有合法图片与发送一条什么都没有的消息不同。请求 ID 必须是 UUID；不能自行加 `author` 指定发送人。未认证、非 owner 或跨来源写请求还有各自的认证与来源检查，修正文案不会解决这些问题。[请求字段与非空规则](../../app/contracts/console.py#L124-L142) · [拒绝示例](../../tests/test_console_api.py#L585-L601)

### 同一句话何时才会交给 Agent？

- **还没开始首次执行。** 即使没有 Worker 或 PR，先后发来的 `First note` 与 `Second note` 也可以记录。Worker 第一次准备任务时会在原要求后按顺序附上它们；同一次准备重放时仍得到同样内容，不会把两句再附一遍。这种情况下等原任务开始即可，不需要额外建立一个 PR。[首次执行及重复准备](../../tests/test_console_api.py#L516-L532)
- **已有作业排队、正在运行或停止尚未确认。** 服务会保存可接受的消息，但不会为它立刻再启动一个冲突作业。之后工作流可以重新检查当前轮次待交付的输入，领取时按活动版本顺序取出；是否能继续仍取决于那时的状态。看到「消息已记录」后等当前工作处理完，比点第二次「发送」更能保留清晰的请求记录。[不安全派发的反例](../../tests/test_console_api.py#L473-L514) · [补派条件](../../app/controler/workflow/service.py#L336-L367)
- **正在收尾或已经归档。** 收尾中若已有 `archive_reason`，可以留下这次执行的说明，但消息不会标记为等 Agent 处理；即使随后重新开工，取待处理项时也只取新 Attempt 的记录。归档完成后发**新**请求得到 `409 agent_task_unavailable`，活动中不会因此多出一条。需要后续工作时先确认是否真的要重新开工，阅读旧记录不需要这个写操作。[待处理标记与按轮次领取](../../app/persistence/agent_inputs.py#L56-L85) · [归档拒绝](../../app/controler/workflow/service.py#L318-L334)

## 三、网页缺一段时，按原会话核对

以下是直接查询 [Worker 会话 API](../architecture/conversation-api.md) 的步骤。它读取编码记录，不合并 GitHub 活动。一个覆盖两轮执行的[端到端测试](../../tests/test_conversation_e2e.py#L104-L145)给出了可观察的例子：

| 动作 | 测试中的结果 | 下一步 |
|---|---|---|
| 在已认证的连接上 `GET /api/flows/{id}/conversation?limit=1` | 首条是旧会话的 `user` 输入；`starts_at_beginning=true`，`reached_end=false`。 | 取响应的 `next_cursor`，原样作为下一次 GET 的 `cursor`。 |
| 读完旧会话 | 收到 `Fixed`，`reached_end=true`，但 Flow 页的 `has_more=true`。 | 继续翻页。即使某页 `items` 为空，只要还有下一页也要推进游标。 |
| 进入新会话 | 收到 `Follow-up`，标有新 Attempt、新 session 和代次；`has_more=false`。 | 保留页尾游标，下次从这里查追加的完整记录。不要据此判断任务已结束。 |

这次 GET 会等待远端一页：[HTTP 入口](../../app/controler/api.py#L141-L160)调用 `ConversationService.read(flow_id, cursor, limit)`，由控制端拆开游标、确定本页属于哪个 Attempt，再把**单个** session ID、内部游标和条数发给[对应 worker 的 `ConversationBroker.query`](../../app/controler/workers/conversation.py#L48-L77)。worker [异步处理 `worker.history`](../../app/worker/transport/business.py#L53-L62)，[`read_history`](../../app/worker/runtime.py#L268-L278)在后台线程读取 Amp 或 Codex 的原始记录，返回 `HistoryPage`。控制端[给记录加回 Attempt、session 和代次并生成下一页游标](../../app/controler/workers/conversation.py#L183-L226)。因此「读完这个 session」与「读完这个 Flow」可以同时得到不同答案。

遇到 `409 cursor_invalid`（例如已读源被替换），清空这次分页结果，不带游标重新开始。遇到 503 `query_busy`、末尾 Worker 暂时离线或 504 `query_timeout`，等待后重试同一页即可；不必点击任务的「重试」或「重新开工」。如果离线的是历史 Worker 且后面还有会话，旧页会以 `worker_unavailable` 标出缺口，仍可继续读新会话。`flow_read_only` 则表示只能读已保存活动，重连 Worker 无法解除这项限制。[历史 Worker 离线与页尾轮询测试](../../tests/test_single_session_history.py#L63-L105) · [游标失效测试](../../tests/test_conversation_history.py#L95-L108) · [只读 Flow 的边界测试](../../tests/test_console_sync.py#L41-L67)
