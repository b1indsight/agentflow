# Flow 会话查询

`GET /api/flows/{flow_id}/conversation` 按需代理对应 worker 读取 Codex 历史。
此接口本身仅查询现有 flow、attempt、session 和 worker 归属，不保存会话内容，也不维护队列。
Web 控制台的独立同步器会主动复用该服务，将展示正文归档到本地 Flow 时间线；
控制台 `/activity` 读取本地快照，不等同于此处的实时 Worker 代理读取。

worker 每次只接收 `session_id`、`cursor`、`limit`，由 `HistoryQuery` 校验后直接交给
`CodexHistory.query()`。返回的 `HistoryPage` 只含该会话的记录、状态及续读位置。
Flow/Attempt 归属和多会话顺序由 control 处理；无独立 SessionRef、HistoryCursor 或 spec 转换层。

## 配置与请求

在 control 配置 `FLOWTOOL_ACCESS_ISSUER`、`FLOWTOOL_ACCESS_AUDIENCE` 和唯一的
`FLOWTOOL_ACCESS_OWNER_EMAIL`，通过 Cloudflare Access 完成 GitHub 登录和 MFA。
后端仅接受 `Cf-Access-Jwt-Assertion`，独立验签并限制 owner 邮箱；不接受旧共享 Bearer Token、
邮箱 header 或直接传入的 Cookie。三项均未配置时返回 503，部分配置时启动失败。
所有 API 响应带 `Cache-Control: no-store`。配置、公钥缓存和真实验收见
[单账号 Access 部署](../deployment/access.md)。

```http
GET /api/flows/42/conversation?limit=100
Cf-Access-Jwt-Assertion: <Access 添加的已签名应用 JWT>
```

参数：`flow_id` 为正整数；`limit` 默认 100，范围 1–500；`cursor` 为上次返回的
`next_cursor`（最长 2048 字符）。首次请求不传 cursor。

```json
{
  "flow_id": 42,
  "items": [{
    "id": "019eac3b-a60c-7a93-a442-3718f660fc8c:1024",
    "attempt_id": 3,
    "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8c",
    "session_generation": 0,
    "timestamp": "2026-09-30T08:00:00Z",
    "role": "assistant",
    "kind": "tool_call",
    "call_id": "call-1",
    "content": {"text": "", "name": "exec_command", "arguments": "{\"cmd\":\"pytest\"}"},
    "truncated": false
  }],
  "sessions": [{
    "attempt_id": 3,
    "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8c",
    "session_generation": 0,
    "status": "available"
  }],
  "next_cursor": "<opaque-cursor>",
  "has_more": false,
  "unavailable_reason": null
}
```

角色为 `user`、`assistant`、`tool`、`system`、`developer`；类型为 `message`、`tool_call`、
`tool_result`。工具调用与结果通过 `call_id` 对应，工具 arguments 保留历史中的字符串。
`user` 指 Codex 收到的输入，可能是 agentflow 构造的任务/PR 反馈提示，并不等于 GitHub 评论作者。
control 自己发出的 PR 评论、状态更新不会显示；不合并完整 GitHub 活动时间线。

## 分页与轮询

先按 attempt 排序、去重 session；每个 HTTP 页只查询一个 session，页内按源文件顺序返回。
`sessions` 仅报告本页实际检查的会话状态。即使 limit 尚有余量，也通过下一页切换会话。
control 的外层游标绑定 flow、有序会话及代次；worker 的内层游标仅绑定 session 与文件位置。
旧版多会话游标返回 `409 cursor_invalid`，应不带 cursor 重新加载。条目 ID 由 session 与源
字节位置组成，可用于去重；不要自行解析或构造 cursor。`has_more=true` 时继续请求下一页，
即使 items 为空也应推进 cursor（该页可能只扫描了非展示记录）。`has_more=false` 时可保存
cursor，稍后轮询新增完整记录，不代表 agent 执行结束。

文件尾部尚未完成的 JSONL 行留到下一次读取。session 集合/代次变化、文件被替换或已读取位置
失效会返回 409，需要清空分页状态并从头查询。历史被移动到 Codex archived_sessions 且
文件身份不变时可继续读取。完整性不作保证：`available` 表示源存在，`unsupported` 表示本页
遇到不支持的展示记录或内容块，`not_found` 表示对应历史文件不存在。

## 错误与边界

错误 JSON 为 `{"detail": "<code>"}`；参数错误 422 使用 FastAPI 标准校验响应。

| HTTP | code / 情况 |
| --- | --- |
| 401 | `unauthorized` |
| 403 | `forbidden`（其他邮箱、非用户身份或非应用 token） |
| 404 | `flow_not_found` |
| 409 | `cursor_invalid` |
| 422 | 非法 flow_id、limit 或 cursor 长度 |
| 502 | `history_unreadable`、`history_limit`、`invalid_response` |
| 503 | `conversation_api_disabled`、`access_keys_unavailable`、`worker_unavailable`、`query_busy`、`database_unavailable` |
| 504 | `query_timeout`（worker 查询 10 秒超时） |

已有 flow 尚无 worker/session 时返回 200、空 items、`unavailable_reason: no_worker/no_session`。
部分文件不存在时逐 session 标记，其他 session 仍可读取。已归档 flow 使用保存的原 worker
绑定查询，但 worker 离线或本地历史清理后无法恢复内容。执行结果尚未回传的首次 session、
被 fallback 覆盖的旧 session ID 可能无法定位；本版不新增采集/索引来补齐。

worker 使用执行 Codex 时相同的 `CODEX_HOME`（默认 `~/.codex`），读取 `sessions` 与
`archived_sessions` 中的 rollout JSONL，核验 session_meta.id。仅映射公开 response_item
消息与 function/custom tool 调用、结果；跳过重复 event_msg、reasoning 和 analysis 记录。
依据本地 Codex 0.155.1 历史结构验证适配，不执行 Codex 命令或读取全局 history.jsonl。

每页最多扫描 8 MiB 完整记录；单行超过 256 KiB 返回 history_limit，文本/arguments 超过
8192 字符截断并标记，items 序列化预算为 256 KiB。control 每个 flow 最多关联 128 个 session，
每个 HTTP 请求仅发起一次 worker 查询。目录发现最多
50000 个条目；先查 sessions，再查 archived_sessions，找到即返回。两处均有副本时优先使用
活动目录；候选文件的符号链接和普通文件检查统一在打开时执行。control 全局最多 64 个
待响应查询、每连接 4 个，worker 最多同时读取 2 个。
control 每条连接使用单一持续接收器，在准备任务、处理工具和应用结果期间仍即时分流会话响应。
执行消息按原顺序消费，连接内临时缓冲最多 32 条/16 MiB，超限使连接失败；不持久化或跨连接重放。
已知环境凭据及常见 token 文本会脱敏，但不保证自动识别任意源码或文本中的所有秘密。

本功能最初使用 wire protocol v11；当前单会话查询采用 v13，control 和 worker 必须同步升级；不会迁移旧版未完成
worker journal。升级前完成/清理在途任务，不能把协议版本不兼容当成可恢复的会话查询失败。
