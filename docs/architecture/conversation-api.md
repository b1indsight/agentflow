# Flow 会话查询：按执行归属读取编码记录

要核对「Agent 看到了哪条评审意见，又调用了什么工具」，需要找到执行那轮任务的编码后端。`GET /api/flows/{flow_id}/conversation` 让客户端只凭 Flow ID 就能查询：control 负责找出会话和 Worker，Worker 负责读记录。这个 GET 不保存正文、不发送任务，也不改变 [Flow 账本](flow-ledger.md)。日常在网页上读到的「会话」则来自 control 已经采集的活动快照；两处内容可能暂时不同。[实时路由](../../app/controler/api.py#L139-L171) · [控制台活动路由](../../app/controler/console/api.py#L394-L422)

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
| [Amp](../../app/worker/execution/amp_agent.py#L580-L638) | 在 Worker 上用 `amp threads export` 读取指定 thread 的公开消息块。导出上限 2 MiB、超时 20 秒，短暂缓存 2 秒（最多 16 个会话）；已读前缀被编辑时原游标失效。[限制常量](../../app/worker/execution/amp_agent.py#L52-L61) |

两端读取器的条目页预算为 256 KiB，文本和工具参数最多 8,192 字符；已知凭据会脱敏，但不要把响应当作已排除所有秘密的公开日志。Broker 限全局 64 个、每连接 4 个待响应查询；回复条目加共享协议头上限 512 KiB，单次往返预算 30 秒。工具或结果处理占用连接时会拖慢历史查询，查询超时不说明编码任务失败。[Codex 测试](../../tests/test_conversation_history.py#L136-L160) · [Amp 测试](../../tests/test_conversation_history.py#L247-L275) · [慢回调测试](../../tests/test_conversation_e2e.py#L104-L145)

## 分页与轮询

每个 HTTP 页最多检查一个 session。内层游标指向该源记录的位置；外层游标绑定 Flow、有序会话及代次。`sessions` 只描述本页：`starts_at_beginning` 表示是否从该会话开头查起，`reached_end` 表示返回的源页已无下一页。最后一条记录来自旧 session 时，可以同时看到 `reached_end: true` 和 `has_more: true`，此时下一次 GET 会切换到另一个 session。最终 `has_more: false` 表示本次已经追到尾部；保留 EOF 游标稍后再查，可以读到新追加的记录。[续读实现](../../app/controler/workers/conversation.py#L141-L227) · [跨轮续读测试](../../tests/test_conversation_e2e.py#L119-L145)

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
