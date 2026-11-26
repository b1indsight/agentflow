# 读一段 Flow 会话，再给 Agent 补一句话

假设 Agent 已经开始处理一项任务，你想先看它试过什么，再告诉它「添加键盘导航，保留现有行为」。在控制台打开这个 Flow 的「会话」即可。这里有两个不同的确认点：看见一条用户消息，说明 control 已经保存它；要确认 Agent 做了什么，还得看对应执行的回复、工具活动或结果。[消息保存与修订任务的行为测试](../../tests/test_console_api.py#L396-L451)

## 进入条件

用配置好的 [Access owner 账号](../deployment/access.md) 登录，选中已有 Flow。读先前保存的内容不要求 Worker 此刻在线，也不要求任务已有 PR。要查 Worker 的最新原始记录，则需要该轮已有会话 ID、能定位到负责的 Worker，且源文件或 thread 仍可读。已经归档的任务可以阅读；收尾中的任务可以留言，归档后不能再追加。[无 PR 时的采集测试](../../tests/test_console_sync.py#L70-L85) · [收尾与归档的界面测试](../../app/controler/web/e2e/console.spec.ts#L586-L655)

## 一、在「会话」里还原当时发生了什么

1. **读已保存的页面。** 进入 `/#flow/42/conversation`（将 42 换成实际 ID）。浏览器请求 `/api/flows/42/activity`，读的是 control 本地已发布的快照。同一页可以有用户的追加要求、从 Worker 采集的公开消息和 GitHub 讨论。界面按当前快照分页，滚动到边界会继续读取；看到「已到当前本地快照末尾」只是读完当前版本。[活动 API](../../app/controler/console/api.py#L394-L422) · [分页和快照末尾的界面测试](../../app/controler/web/e2e/console.spec.ts#L2645-L2699)
2. **把答复和工具操作对起来。** 先读助手的说明，再展开「工具活动」查看调用和结果。较长的记录可以单独续读正文；正文加载失败可点「重试正文读取」，无需丢弃整页。工具摘要可能已经截断，没显示的字符不能据此推断。[工具折叠测试](../../app/controler/web/e2e/console.spec.ts#L2645-L2699) · [正文续读与原位置重试](../../app/controler/web/e2e/console.spec.ts#L3580-L3650) · [工具摘要截断测试](../../tests/test_console_sync.py#L148-L161)
3. **确认记录是否追到最新。** 点「刷新当前视图」会请求后台同步，同时读当前已提交的活动；并不等待远端把所有消息传完。「会话数据状态」会区分「尚无执行会话」和「执行会话：会话读取超时」。后者表示这一次没读成功，已经保存的正文仍可见。等 Worker 恢复后再刷新；如果来源一直缺失，保留这段缺口，不把空白解释为 Agent 什么也没做。[后台读一页并保存游标](../../app/controler/console/sync.py#L102-L121) · [状态提示与正文同时可见的测试](../../app/controler/web/e2e/console.spec.ts#L2137-L2165)

翻页和刷新在[活动接口 `activity`](../../app/controler/console/api.py#L395-L420)分开：有 `refresh` 时先登记要同步这个 Flow，再调用 [`ActivityStore.page`](../../app/persistence/activity.py#L303-L338)。它用 Flow ID、页大小、快照版本和游标读出 `ActivityPage`；首次读取以已提交的活动版本为上限，续页沿用游标中的版本。后台稍后领取同步任务，[`ConsoleSynchronizer.sync`](../../app/controler/console/sync.py#L102-L121)用另一套游标向 worker 读一页，将记录、下一游标和缺口说明交给 [`ConsoleSyncStore.finish`](../../app/persistence/console_sync.py#L98-L144)。后者在事务内核对自己仍有权发布本页，再写活动；旧任务迟到会返回 `False`。[未提交页面对读者不可见的测试](../../tests/test_console_sync.py#L700)解释了为什么同一页不会显示一半新、一半旧的同步结果。

## 二、把补充要求送给这一轮执行

在「与 Agent 交流」输入框写下要求并发送；也可以用 `Ctrl+Enter`。页面发出 `POST /api/flows/{id}/agent-tasks`，携带本次的 `request_id`。成功后输入框清空，状态显示「消息已记录」，会话中增加一条 `source=agent_task` 的用户消息。它和 Worker 后续回传的 `user` 输入属于不同来源。[界面发送与确认测试](../../app/controler/web/e2e/console.spec.ts#L536-L577) · [服务端记录归属](../../tests/test_console_api.py#L401-L435)

[输入框的 `onSubmit`](../../app/controler/web/App.tsx#L1128-L1148)把原文、请求 ID 和文件交给 [`appendAgentTask`](../../app/controler/web/api.ts#L269-L287)。浏览器等到的是更新后的 `FlowDetail`。[服务端入口](../../app/controler/console/api.py#L341-L363)在数据库工作线程里调用 [`ControlService.append_agent_task`](../../app/controler/workflow/service.py#L309-L334)：锁住这个 Flow，按请求 ID 对比含附件引用的正文；新请求才调用 [`AgentInputStore.record`](../../app/persistence/agent_inputs.py#L56-L67)写入当前 Attempt 的用户消息。未开始收尾的消息还会标成待交给 Agent。记录与当下可安排的作业在同一事务内完成，事务后才通知 worker。[写入失败的测试](../../tests/test_console_api.py#L649)检查了两者一起回滚。

接下来取决于 Flow 的状态。在一个已有可继续评审的 PR、Worker 和原会话的例子里，这条要求会形成修订作业，继续使用原分支、PR 和 session。若尚未首次执行，control 可以先保存多条要求，等首次准备任务时按接收顺序交付。若正在运行或收尾，保存成功也不代表立刻并行派发第二份作业。[原会话修订](../../tests/test_console_api.py#L428-L451) · [只记录、不触发不安全派发](../../tests/test_console_api.py#L473-L514) · [首次执行的消息顺序](../../tests/test_console_api.py#L516-L532)

**如果发送后断网，使用「重试发送」。** 页面保留原输入、附件和请求 ID；同一个 ID 携带相同内容再次提交，只会记录一次。不要改一个新 ID 来试探上次是否成功；同一 ID 改了内容则会收到 `409 agent_task_request_conflict`。刷新会话或重试读取是读操作，都不会替你重发这条消息。[客户端重用原请求](../../app/controler/web/e2e/console.spec.ts#L541-L562) · [服务端去重与冲突测试](../../tests/test_console_api.py#L398-L427)

## 三、网页缺一段时，按原会话核对

以下是直接查询 [Worker 会话 API](../architecture/conversation-api.md) 的步骤。它读取编码记录，不合并 GitHub 活动。一个覆盖两轮执行的[端到端测试](../../tests/test_conversation_e2e.py#L104-L145)给出了可观察的例子：

| 动作 | 测试中的结果 | 下一步 |
|---|---|---|
| 在已认证的连接上 `GET /api/flows/{id}/conversation?limit=1` | 首条是旧会话的 `user` 输入；`starts_at_beginning=true`，`reached_end=false`。 | 取响应的 `next_cursor`，原样作为下一次 GET 的 `cursor`。 |
| 读完旧会话 | 收到 `Fixed`，`reached_end=true`，但 Flow 页的 `has_more=true`。 | 继续翻页。即使某页 `items` 为空，只要还有下一页也要推进游标。 |
| 进入新会话 | 收到 `Follow-up`，标有新 Attempt、新 session 和代次；`has_more=false`。 | 保留页尾游标，下次从这里查追加的完整记录。不要据此判断任务已结束。 |

这次 GET 会等待远端一页：[HTTP 入口](../../app/controler/api.py#L141-L160)调用 `ConversationService.read(flow_id, cursor, limit)`，由控制端拆开游标、确定本页属于哪个 Attempt，再把**单个** session ID、内部游标和条数发给[对应 worker 的 `ConversationBroker.query`](../../app/controler/workers/conversation.py#L48-L77)。worker [异步处理 `worker.history`](../../app/worker/transport/business.py#L53-L62)，[`read_history`](../../app/worker/runtime.py#L268-L278)在后台线程读取 Amp 或 Codex 的原始记录，返回 `HistoryPage`。控制端[给记录加回 Attempt、session 和代次并生成下一页游标](../../app/controler/workers/conversation.py#L183-L226)。因此「读完这个 session」与「读完这个 Flow」可以同时得到不同答案。

遇到 `409 cursor_invalid`（例如已读源被替换），清空这次分页结果，不带游标重新开始。遇到 503 `query_busy`、末尾 Worker 暂时离线或 504 `query_timeout`，等待后重试同一页即可；不必点击任务的「重试」或「重新开工」。如果离线的是历史 Worker 且后面还有会话，旧页会以 `worker_unavailable` 标出缺口，仍可继续读新会话。`flow_read_only` 则表示只能读已保存活动，重连 Worker 无法解除这项限制。[历史 Worker 离线与页尾轮询测试](../../tests/test_single_session_history.py#L63-L105) · [游标失效测试](../../tests/test_conversation_history.py#L95-L108) · [只读 Flow 的边界测试](../../tests/test_console_sync.py#L41-L67)
