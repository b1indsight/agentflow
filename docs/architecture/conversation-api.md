# Flow 会话查询：按执行归属读取编码记录

要核对「Agent 看到了哪条评审意见，又调用了什么工具」，需要找到执行那轮任务的编码后端。`GET /api/flows/{flow_id}/conversation` 让客户端只凭 Flow ID 就能查询：control 负责找出会话和 Worker，Worker 负责读记录。这个 GET 不保存正文、不发送任务，也不改变 [Flow 账本](flow-ledger.md)。日常在网页上读到的「会话」则来自 control 已经采集的活动快照；两处内容可能暂时不同。[实时路由](../../app/controler/api.py#L139-L171) · [控制台活动路由](../../app/controler/console/api.py#L394-L422)

## 请求经过哪些模块？

用 Flow 查会话，要回答三个问题：谁有权读、该去哪里读、哪些记录属于这次查询。下面按请求经过的顺序列出各模块的职责；排查错误时也可以据此判断应该看 control 还是 Worker。

- **HTTP 入口**在 control 上。它识别 owner，校验 Flow ID 和分页参数，启动一次只读查询，把查不到 Flow、等待超时等结果译成 HTTP 状态。调用者只需要能访问 control，不需要知道执行机的地址，更不需要在浏览器里持有 Worker 的连接凭据。[入口实现](../../app/controler/api.py#L139-L171)
- **归属与分页服务**把一个 Flow 下的多轮执行整理成有序的会话清单。Flow 是用户跟踪的任务，Attempt 是该任务的一轮执行；后端 session 是编码工具实际写对话的地方。三者不能互换：一轮尚未启动时可能还没有 session，重新开工后还是原来的 Flow，却可能要去另一台机器找另一份记录。[清单构造](../../app/controler/workers/conversation.py#L114-L150)
- **Broker 和 Worker 传输层**负责一次有期限的问答。Broker 在 control 的已注册连接中找到指定 Worker；Worker 收到请求后交给自己的历史读取逻辑。连接上还可能有其他请求或业务结果，所以仅仅收到一个看起来像历史页的回复，并不代表能把它交给这次 GET。[发起请求](../../app/controler/workers/conversation.py#L48-L82) · [回包关联测试](../../tests/test_conversation_proxy.py#L429)
- **源记录读取器**在 Worker 进程一侧工作。Codex 读取本机 rollout，Amp 读取 worker 采集的公开事件；两者都返回 [`HistoryPage`](../../app/contracts/conversation.py)。消费者应原样使用 `next_cursor`；条目 ID 不是文件行号。

还有一个容易混淆的读者：control 的后台采集器也调用这个服务，但它会另外把归一化的条目交给活动存储。网页请求 `/activity` 时看的是那次发布的内容；直接请求 `/conversation` 时，则会触发本次 Worker 读取。比如 Worker 掉线后，前者仍能给出早先保存的说明，后者可能返回 503。选择接口取决于要核对「已保存了什么」还是「现在还能从源头读到什么」。[采集与发布](../../app/controler/console/sync.py#L102-L121)

## 配置与请求

请求经 Cloudflare Access 登录后到达 control。control 验证 `Cf-Access-Jwt-Assertion` 的签名和应用身份，并检查唯一 owner 邮箱；仅有邮箱请求头、Cookie 或普通 Bearer Token 不能直接通过后端验证。`AGENTFLOW_ACCESS_ISSUER`、`AGENTFLOW_ACCESS_AUDIENCE`、`AGENTFLOW_ACCESS_OWNER_EMAIL` 须一起配置；缺一部分会导致配置校验失败，全部未设时接口返回 503。API 响应标记 `Cache-Control: no-store`。[部署设置](../deployment/access.md) · [鉴权测试](../../tests/test_conversation_api.py#L28-L84)

```http
GET /api/flows/42/conversation?limit=1
```

将 `42` 换成正整数 Flow ID。`limit` 默认 100，可取 1–500；首次不要传 `cursor`，续读时原样传回服务返回的 `next_cursor`（1–2048 字符）。下面是响应形状示例，游标是占位符。[路由参数](../../app/controler/api.py#L139-L149) · [响应合同](../../app/contracts/conversation.py#L55-L104)

```json
{
  "flow_id": 42,
  "items": [{
    "id": "019eac3b-a60c-7a93-a442-3718f660fc8c:1024",
    "attempt_id": 3, "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8c", "session_generation": 0,
    "timestamp": null, "role": "assistant", "kind": "tool_call", "call_id": "call-1",
    "content": {"text": "", "name": "exec_command", "arguments": "{\"cmd\":\"pytest\"}"}, "truncated": false
  }],
  "sessions": [{
    "attempt_id": 3, "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8c", "session_generation": 0,
    "status": "available", "starts_at_beginning": true, "reached_end": true
  }],
  "next_cursor": "<opaque-cursor>", "has_more": false, "unavailable_reason": null
}
```

`items` 按源记录顺序返回，`kind` 是 `message`、`tool_call` 或 `tool_result`，可用 `call_id` 配对工具调用与结果。`role` 可为 `user`、`assistant`、`tool`、`system`、`developer`；其中 `user` 表示编码后端收到的输入，可能是一段由系统整理的任务提示，不足以识别 GitHub 评论作者。`truncated` 提醒调用者正文或参数被截短，不应当作完整原文。[条目定义](../../app/contracts/conversation.py#L66-L96) · [角色与工具记录测试](../../tests/test_conversation_history.py#L37-L68)

### 从一条工具记录能得出什么结论？

以上面的响应为例，`flow_id=42` 是整页的查询对象，`attempt_id=3` 是这条记录的执行归属。记录中出现 `exec_command` 和字符串形式的 `arguments`，说明历史里有一次调用；要知道测试有没有通过，应继续找同一会话、同一 `call_id` 的 `tool_result` 并读其正文。不能仅凭函数名或助手随后说的「完成」填一条成功结果。若调用和结果刚好在两页，保留调用直到读到下一页；若中间有缺口，标为未找到结果。`timestamp` 允许为空，不要因缺少时间就扔掉调用，或把它和另一轮恰好同名的命令配成一对。[字段约束](../../app/contracts/conversation.py#L66-L100)

一个实际的 [Codex 读取用例](../../tests/test_conversation_history.py#L37-L68)依次写入用户的 `Fix PR feedback`、对应的重复通知、内部推理、公开的 `Checking tests`、运行 `pytest` 的调用和 `passed` 结果。用两条为上限开始读，只得到用户与助手的公开消息；续页得到调用和结果，两者的 `call_id` 都是 `c1`。这里不能以「源文件行数减去显示条数」估计丢了几条消息，因为跳过重复通知和内部内容本来就是格式转换的一部分。

Amp 的例子展示了另一个区别。一条原生消息可以含多个块：同一助手消息里的公开文本和工具调用可以各占一个返回条目；工具结果即使在导出文件的 `user` 消息里，也会成为 `role=tool`。两条文字完全相同的 GitHub 更新，一条有自动化事件标记，另一条来自普通用户，在测试中分别得到 `system` 和 `user`。读取方应保留返回的角色和种类；按正文前缀猜作者会把两者混在一起。[原生块、事件来源及已读前缀修改的测试](../../tests/test_conversation_history.py#L197-L246)

## 谁找到原来的 Worker？

`ConversationService` 先查 Flow 和执行绑定，再按 Attempt 的创建时间、ID 排序，从 `codex_session_id` 取会话（该字段也承载 Amp thread ID），对相同 session 保留首次归属，最多 128 个。读每一页前，它按该 Attempt 最近一次有 Worker 的 `initial` / `revision` 作业选 Worker。于是即使重新开工时换了机器，也不会向新机器索取旧轮次的历史。[归属查询](../../app/persistence/flows.py#L273-L289) · [两台 Worker 的行为测试](../../tests/test_single_session_history.py#L39-L105)

`ConversationBroker` 在这台 Worker 的在线连接上发送一次 `worker.history`：只交给后端 `session_id`、`cursor` 和 `limit`。回复要匹配本次调用和连接，Broker 还检查会话 ID、条目数、ID 前缀和续读位置。只有 control 能给结果附上 `attempt_id`、`session_id`、`session_generation`。断连不会把旧请求改投另一台机器；查询取消或超时会释放名额。[请求与回包校验](../../app/controler/workers/conversation.py#L39-L110) · [标注与翻页](../../app/controler/workers/conversation.py#L179-L227)

以开头的请求为例，假设 Attempt 3 在 `worker-a` 上执行。沿一次读取看，三处的“页”各有用途：

| 调用边界 | 传什么，返回什么 |
|---|---|
| [HTTP 路由](../../app/controler/api.py#L139-L170) → [ConversationService.read](../../app/controler/workers/conversation.py#L141-L150) | 路由完成 owner 验证后传 `flow_id=42, cursor=None, limit=1`。服务负责返回整个 `ConversationPage`，客户端不提供 Worker 地址或后端会话 ID。 |
| [服务按 Attempt 查归属](../../app/controler/workers/conversation.py#L177-L198) → [ConversationBroker.query](../../app/controler/workers/conversation.py#L48-L81) | 服务用 `session_worker(3)` 找到 `worker-a`，传给 Broker 的是这个 Worker ID 和 `HistoryQuery`。数据库归属查询已结束，等 Worker 回包时不持有 Flow 事务。Broker 返回经过校验的 `HistoryPage`，它只描述这一份源记录。 |
| [WorkerBusinessCallbacks.on_history](../../app/worker/transport/business.py#L53-L62) → [WorkerRuntime.read_history](../../app/worker/runtime.py#L268-L278) | Worker 按会话 ID 选读取器，`T-` 开头交给 Amp，其余交给 Codex；只在发出的副本上处理文件链接。读到的页或 `ConversationFailure` 错误码由[回包逻辑](../../app/worker/transport/business.py#L86-L96)关联到原调用。control 再给条目标上 Attempt 3，并生成供下次 GET 使用的外层游标。 |

例如 Worker 的历史和模型目录查询共用两个异步查询名额，用满时 `on_history` 直接回 `query_busy`。即使 Broker 自己还有名额，这次 GET 也会返回 503：由 HTTP 客户端稍后按原游标重读，运行中的编码 Job 不参与重试。[Worker 名额检查](../../app/worker/transport/business.py#L53-L71) · [HTTP 错误映射](../../app/controler/api.py#L161-L166)

| Worker 上的读取器 | 读什么、何时返回不完整结果 |
|---|---|
| [Codex](../../app/worker/execution/codex.py#L315-L398) | 在执行所用的 `CODEX_HOME`（默认 `~/.codex`）先查 `sessions` 再查 `archived_sessions`，验证 rollout 文件身份和 `session_meta.id`。按 JSONL 行提取公开 `response_item` 消息和工具操作，跳过重复事件及内部推理；末尾尚未写完的一行留待下次查询。目录最多检查 50,000 项，单页扫描预算 8 MiB，单条记录上限 256 KiB。 |
| [Amp](../../app/worker/execution/amp_history.py) | 编码首轮与续聊把输入、助手消息和工具活动脱敏后追加到 `worker_root/amp-history/`，按字节游标分页。旧会话没有文件、文件损坏或写入中断时，空闲 worker 用 `threads export` 流式回填；原始导出和候选文件各限 64 MiB，导出限时 20 秒。只有覆盖已知条目且没有并发续写才替换。回填后的旧游标返回 `cursor_invalid`；失败保留旧数据，可再次查询。正常分页不调用 Amp，不跟踪其他客户端的新增或编辑。 |

两端读取器的条目页预算为 256 KiB，文本和工具参数最多 8,192 字符；已知凭据会脱敏，但不要把响应当作已排除所有秘密的公开日志。Broker 限全局 64 个、每连接 4 个待响应查询；回复条目加共享协议头上限 512 KiB，单次往返预算 30 秒。工具或结果处理占用连接时会拖慢历史查询，查询超时不说明编码任务失败。[Codex 测试](../../tests/test_conversation_history.py#L136-L160) · [Amp 测试](../../tests/test_conversation_history.py#L247-L275) · [慢回调测试](../../tests/test_conversation_e2e.py#L104-L145)

### 方法契约：什么样的回包可以交给调用者？

从 control 看，`query(worker_id, query)` 要么给出一个校验通过的源页，要么报告失败。`worker_id` 必须对应已注册且已建立连接代次的 Worker；它不是一个可随意换成其他在线节点的负载均衡提示。连接换代后，旧连接上迟到的回复不能完成新请求。等待期间连接关闭，调用者得到 `worker_unavailable`；等待到期得到 `query_timeout`；两条路径和成功路径都会在退出时归还并发名额。[Broker 的进入和退出条件](../../app/controler/workers/conversation.py#L48-L82) · [替换连接的测试](../../tests/test_conversation_proxy.py#L223-L251)

举个回包格式错误的例子：客户端要 `limit=1`，Worker 确实回答了这次请求，且条目也来自正确的 session，却送回两条。Broker 拒绝整页，HTTP 为 `502 invalid_response`，不会替它裁成一条后发布。否则下一游标可能已经越过第二条，客户将永远少读一条。同样，Worker 声明 `has_more=true` 却没给 `next_cursor` 时，control 也不能代猜下一位置。这里的 502 与检索成功但 `items=[]` 是两种结果；不要用吞掉异常并返回空数组的方式兼容它们。[源页校验](../../app/controler/workers/conversation.py#L84-L110) · [坏页用例](../../tests/test_conversation_proxy.py#L280-L325)

`read(flow_id, cursor, limit)` 在这层校验之外负责整个 Flow 的范围。外层游标包含与有序 Attempt、session 和代次有关的指纹。即使原来的后端文件还在，如果这份清单变了，服务也不能保证按照旧清单继续读不会漏掉一轮；旧游标会失效。内层游标则必须属于正在读的 session，源位置非负，身份字段合法。两层分别回答「清单里的哪一段」和「这一段的哪里」，客户端一律保存服务交付的外层值即可。[外层校验](../../app/controler/workers/conversation.py#L151-L186) · [内层校验](../../app/contracts/conversation.py#L19-L53)

## 分页与轮询

每个 HTTP 页最多检查一个 session。内层游标指向该源记录的位置；外层游标绑定 Flow、有序会话及代次。`sessions` 只描述本页：`starts_at_beginning` 表示是否从该会话开头查起，`reached_end` 表示返回的源页已无下一页。最后一条记录来自旧 session 时，可以同时看到 `reached_end: true` 和 `has_more: true`，此时下一次 GET 会切换到另一个 session。最终 `has_more: false` 表示本次已经追到尾部；保留 EOF 游标稍后再查，可以读到新追加的记录。[续读实现](../../app/controler/workers/conversation.py#L141-L227) · [跨轮续读测试](../../tests/test_conversation_e2e.py#L119-L145)

### 客户端如何保存进度

假设两个会话分别只有一条 `one` 和 `two`。首次请求不带游标，即使上限是 100，仍只收到旧会话的 `one`；`has_more=true` 告诉客户端有第二个会话。下一页拿到 `two`，`has_more=false`。此时可结束本次追读，但要留下第二页的游标。若新会话随后追加 `tail`，用这个游标再次查询就只收到 `tail`。如果从第一页重来，先前的 `one`、`two` 又会出现。这个行为在[双 Worker 与尾部追加测试](../../tests/test_single_session_history.py#L39-L84)中有完整的调用顺序。

实现消费者时，可以让一次页面处理同时保存四项：Flow 标识、本页条目、`sessions` 的覆盖说明和响应给出的续读游标。先处理完条目再确认进度：如果写入条目失败却提前推进游标，下次可能跳过整页；如果先保存条目、确认前断线，可以从原游标再读，并用条目 ID 在同一会话下识别重复。这里描述的是客户端的处理建议；GET 本身没有「已读回执」，也不会替读者记住读到了哪里。[服务构造返回页](../../app/controler/workers/conversation.py#L193-L227)

不要用 `len(items) < limit` 结束循环。读取预算可能先于条数上限用完：测试把扫描预算缩小到读完一个不展示的 `turn_context`，第一页没有条目却给出 `has_more=true`，按游标继续才读到 `later`。反过来，Codex 在尾部碰到一条只写了一半的消息时，此次看不到新内容；等换行和剩余字节齐全，继续轮询才会读到完整的 `Done`。两种空页的操作分别是立即翻页和保留末尾游标稍后再查。[扫描预算用例](../../tests/test_conversation_history.py#L151-L160) · [半行追加用例](../../tests/test_conversation_history.py#L55-L68)

## 错误与边界

看到空 `items` 时，先读同一响应的状态。有 Flow 但无执行绑定或无会话 ID，首次请求返回 200、`unavailable_reason: no_worker` 或 `no_session`。查到了会话但找不到源记录会标 `sessions[].status: not_found`；`unsupported` 表示有无法识别的内容。旧会话的 Worker 不可用且后面还有会话，则返回 `worker_unavailable` 的空页并允许继续；最后一个会话的 Worker 不可用才返回 HTTP 503。`reached_end` 不能单独证明整段历史完整。[空页与归属处理](../../app/controler/workers/conversation.py#L120-L151) · [旧会话缺失/离线测试](../../tests/test_single_session_history.py#L82-L105)

| HTTP | `detail` 或响应 | 如何处理 |
|---|---|---|
| 401 / 403 | `unauthorized` / `forbidden` | 重新登录或检查 owner、应用身份，避免原样反复请求。 |
| 404 / 422 | `flow_not_found` / FastAPI 参数校验详情 | 核对 ID、limit 和 cursor 长度。 |
| 409 | `cursor_invalid` / `flow_read_only` | 前者清空旧分页状态，从首屏重读；后者是只读历史 Flow，改读已经保存的活动。 |
| 502 | `history_unreadable`、`history_limit`、`invalid_response` | 分别检查源格式/权限、读取上限或 Worker 返回的页面；单纯增加重试次数不能修复坏文件。 |
| 503 | `conversation_api_disabled`、`access_keys_unavailable`、`database_unavailable`、`worker_unavailable`、`query_busy` | 配置缺失先修配置；依赖恢复或并发下降后可重试同页。 |
| 504 | `query_timeout` | 稍后用原游标重读；不必因此重开任务。 |

业务错误通常为 `{"detail":"<code>"}`。HTTP 客户端主动断开时，路由取消未完成的查询；本次 GET 不留下待交付的业务任务。需要对照网页和原始分页结果时，见[会话走查](../feature-walkthroughs/conversation.md)。[错误映射](../../app/controler/api.py#L139-L171) · [断连测试](../../tests/test_conversation_api.py#L181-L215)

### 用同一个 Flow 区分三种「目前读不到」

以一项新建任务为例，没有执行绑定时从第一页读，得到的是 200 空页，`sessions=[]`，并带 `no_worker`。作业分配后已有绑定，但 Attempt 尚未写入会话 ID，再读同一地址，原因变成 `no_session`。这两次返回都没有可推进的页；之后有了会话再从头查询。若此时反而带了来自别处的游标，就不能当作正常首屏，服务会拒绝这个续读位置。[无归属分支](../../app/controler/workers/conversation.py#L120-L151) · [从无绑定到有会话的测试](../../tests/test_conversation_proxy.py#L327-L354)

另一种情况是已经有合法会话 ID，Worker 也可以接受请求，但文件早已被移走，两个历史目录都找不到它。此时应查看会话条目上的 `not_found`，记录哪一轮缺资料。若找到的文件宣称属于另一个 session，读取器会报 `history_unreadable`，不能把该文件内容当成本轮历史。这能帮助排查人员区分「源不存在」与「不能确认所读的是正确的源」。释放工作区绑定本身也不等于查无此会话；原 Worker 和源仍可用时可继续读历史。[源缺失用例](../../tests/test_single_session_history.py#L92-L105) · [身份不符用例](../../tests/test_conversation_history.py#L69-L85) · [绑定释放后的查询](../../tests/test_conversation_proxy.py#L343-L354)
