# Worker / server 公共通信 Session（第二阶段）— Feature record

**Stage:** Planning — 待审批；未编写实现或测试
**PR:** [#122](https://github.com/b1indsight/agentflow/pull/122) — docs-only Draft，尚未批准实现
**Bookmark:** `refactor/shared-communication-session`，计划与实现沿用同一 bookmark / PR
**Workspace:** `.amp/in/shared-session-plan`

## 1. Initial plan

### 1.1 动机、依据与范围

最终结构已确定为两端共用 Gateway + Session：worker Gateway 当前连接一个 server，server
Gateway 管多个 worker；双方具有主动发送和回复能力。本阶段抽公共 Session，公共 Gateway
注册/路由、统一 envelope、outbox / event sourcing 留给后续阶段。

规划基线是重新 fetch 后的 `main@origin`：
[`bbf8504`](https://github.com/b1indsight/agentflow/commit/bbf85048c432bf3d0e2960d36adde758eaeba9ea)，
已包含第一阶段 [PR #120](https://github.com/b1indsight/agentflow/pull/120)。本阶段使用独立 jj
workspace，不采用共享根目录的旧 detached HEAD、本地工作树或第一阶段计划副本；不接管
`.amp/in/worker-execution-lifecycle-pr`，不修改 `app/controler/web/` 或
`docs/plan/web-console-kanban.md` 的其他工作。

已核对的现状：

- server `WorkerConnection` 有唯一 reader；历史回复直接交给 Broker，其余 raw message 进入
  32 条 / 16 MiB FIFO。Gateway 取出时再解析一次。reader 失败立即失败临时查询，但已入队的
  执行消息仍先按序交给消费者。[S1]
- `ConversationBroker` 同时承担 worker 路由、全局 64 / 单连接 4 准入、30 秒总超时、
  request_id + 连接对象 + epoch 关联和历史页业务校验。断连/替换不重放查询。[S2]
- worker 在调用 `_receive_message()` 时分流查询，最多运行四个历史任务；历史、心跳、执行
  路径分别发送。持续 reader 尚未独立于协议驱动，例如等待 stop 清理时不能继续读取查询。[S3]
- 第一阶段的 `WorkerExecution` 已拥有执行 Task、原工具 Future 和 shielded stop；
  `ExecutionState` 保留 journal。替换通信消费者前必须完全关闭并 join 旧消费者。[S4][S5]

具体动机：server 正在等待工具或结果应用时，用户仍能读取会话历史；worker 正在停止执行时，
查询也应能由相同的连接级分派机制处理。两端不再各维护一套 reader、关联和查询任务清理代码。

**包含：** 共用 Session 核心、必要的 socket 适配、历史双端接入、所有既有执行消息经过
Session 收发、对应业务回归与当前架构说明。

**不包含：** 公共 Gateway、迁移所有业务处理器、改名 WorkerClient、通用消息注册框架、
新 wire 字段/方向、持久消息队列、重试/outbox、历史服务拆分、执行 owner/journal/DB 重构。
不部署，不执行真实 GitHub 业务副作用。

### 1.2 成功标准与明确不变的合同

1. server 与 worker 实际使用同一个 `Session` 类；每个 socket 在任一时刻只有一个 reader。
   查询和回复绕过慢业务；其余消息保持 FIFO，不能任意并发执行业务 handler。
2. 全局 64、单 Session 4 个临时请求、worker 四个历史任务、查询 30 秒总超时继续成立。
   超限立即报 busy，不排队等配额；超时/取消/断连/替换归还全部临时配额，无跨连接重放。
3. wire v15 字段及方向白名单、journal v4 实际 JSON、数据库格式、身份/fencing 不变。
   同一核心支持双向通信，不代表本批让 worker 向 server 发历史查询。[S6]
4. `FlowToolResponse`、`ResultAccepted`、`StopAccepted` 继续交原协议驱动验证和应用。
   普通通信 reply 匹配不代表业务结果已应用，也不授权释放 slot 或 Flow binding。
5. `record_received → await 实际发送 Started → record_started → create_task` 保持。
   工具原 call_id/Future 属于执行；发送前已取消的调用不写 socket，已开始发送的调用仍消费
   匹配回复。Session 关闭不能取消执行或中断 owner 的 shielded stop。[S3][S4]
6. `Registered` 不一定是首个业务响应。恢复/停止握手可以在它之前完成，甚至直接以
   `ResumeAssignment` / `ReplayResult` 进入工作状态。[S3][S7]

### 1.3 提议结构与职责

```diagram
┌──────────────────────┐              ┌──────────────────────┐
│ Gateway + Broker     │              │ WorkerClient         │
└──────────┬───────────┘              └─────┬─────────┬──────┘
           │                               │         │
           ↓                               ↓         │
┌──────────────────────┐              ┌────────────┐  │
│ Session              │←─── wire ───→│ Session    │  │
│ reader/FIFO/pending  │              │ same core  │  │
└──────────────────────┘              └────────────┘  │
                                                     ↓
                                     ┌──────────────────────┐
                                     │ WorkerExecution      │
                                     │ Task/tools/stop      │
                                     │ ExecutionState       │
                                     └──────────────────────┘
```

两端 Runtime 装配这些对象。WorkerClient 分别持有 Session 和执行 owner，Session 不持有 owner。
Session 不知道 execution、database、Flow、worker 注册表或历史页语义，也不保存整个 transcript。

| 模块 | 本阶段责任与改动 |
| --- | --- |
| 新增 `app/communication/__init__.py`、`session.py` | 中立通信包。Session、Transport Protocol、Responder 和连接级异常；包入口只说明职责，不转发导出。 |
| 删除 `app/controler/workers/connection.py` | 单 reader/FIFO/发送/关闭全部由 Session 替代；`WorkerSocket` 与业务 `ProtocolViolation` 移入 `gateway.py`，不保留兼容壳。 |
| `controler/workers/conversation.py` | Broker 保留 worker 路由、全局准入、请求构造、错误映射和历史页校验。删除 `PendingQuery`、pending Future 表及 `accept()`。ConversationService 的 DB/Flow/Attempt 归属与分页不动。 |
| `controler/workers/gateway.py` | 使用 `Session[WorkerMessage, ServerMessage]`；原鉴权、registry、调度、恢复、停止、结果应用逻辑保留；所有 Session 建立后的消息由它发送/接收。 |
| `worker/transport/client.py` | 使用 `Session[ServerMessage, WorkerMessage]`；保留拨号/重连、心跳、worker 协议驱动。历史 handler 返回结果，由 Session 发送。增加唯一必要的 websockets 适配器。 |
| 两端 `runtime.py` | 仅检查/调整装配类型，仍共享原 Broker、owner 与 `read_history=state.read`；不往 Runtime 搬业务循环。若构造参数不需变化，不作机械修改。 |
| `tests/test_worker_connection.py` 等 | 将已有行为测试迁移到公共 Session 接口；可以重命名为 `test_communication_session.py`，不保留失效的旧包装层测试。 |
| `tests/test_boundaries.py`、当前架构文档 | 验证公共包的静态和传递导入边界，更新现状；不批量改写历史计划。 |

公共层只依赖标准库和既有 `contracts.protocol.Message` 类型/序列化合同；禁止导入
`controler`、`worker`、`persistence`、`fastapi`、`starlette`、`websockets`、GitHub SDK 或执行器。
因此 server socket 可结构性满足 Transport；worker 的方法命名及 bytes 解码由本地适配器处理。

### 1.4 核心类型：全部字段与精确接口

以下是待批准 API，不是已实现代码。`Incoming`、`Outgoing` 均以既有 `Message` 为类型上界，
使用实际方向联合类型；不采用 `Any`、cast 或字符串 handler registry。将只供一个固定场景使用
的限额保留为模块常量，不增加每个调用者都传相同值的配置参数。

**`ReplyKey = tuple[str, str]`**：依次为 request_id、connection_epoch。所属 Session 实例隐含在
pending 表所在对象中。协议侧只为 `ConversationResult` 提取 key；不能按“包含 request_id”判定，
因为 stop 系列也有这个字段。调用方为每次临时请求生成新 ID，不在同一 epoch 复用。[S6]

**`Transport` Protocol** 无状态要求，仅定义：

```python
async def send_text(self, data: str) -> None: ...
async def receive_text(self) -> str: ...
async def close(self, code: int = 1000) -> None: ...
```

`WorkerSocket` 在该协议之外保留 `headers` 和 `accept`；这些不是公共 Session 的职责。
worker 私有 `_WebSocketTransport` 仅有 `connection: Connection` 字段，构造签名
`__init__(self, connection: Connection) -> None`，实现上述三方法；原 `Connection` Protocol
增加 `async def close(self, code: int = 1000) -> None`。适配器不读取第二遍、不持有 Task 或业务状态。

**`Responder[Incoming, Outgoing]`** 是不可变的三个 callable，最多注入一组，不构建处理器注册系统：

| 字段 | 类型与意义 |
| --- | --- |
| `matches` | `Callable[[Incoming], bool]`，同步判定该消息是否走连接级查询分派。 |
| `handle` | `Callable[[Incoming], Awaitable[Outgoing]]`，异步返回应答；业务错误由业务方编码进结果。 |
| `busy` | `Callable[[Incoming], Outgoing]`，同步构造 busy 应答，保留原查询 ID/epoch。 |

类型宽度与 Session 相同；handler 内用 `isinstance` 窄化到 ConversationQuery。server 当前无需
Responder；worker 不需要临时 reply 提取器。双向核心验证时，两端同时配置两种能力。

**公共异常：** `SessionClosed(ConnectionError)` 表示连接关闭/不可继续写；
`SessionBusy(RuntimeError)` 表示四个临时请求槽已满；`SessionOverflow(RuntimeError)` 表示
入站缓存超过有界容量。业务方先捕获 Busy/Overflow，再处理一般错误；保留异常 cause。
解析失败仍向业务接收边界传播原 `ValidationError`，不能藏进成功回复。

**`Session[Incoming, Outgoing]` 字段：**

| 字段 | 类型与意义 |
| --- | --- |
| `_transport` | `Transport`，一个物理连接。 |
| `_parse` | `Callable[[str], Incoming]`，既有方向 parser，每条消息仅解析一次。 |
| `_reply_key` | `Callable[[Incoming], ReplyKey \| None] \| None`，只识别临时 reply，None 表示该接入当前无此能力。 |
| `_responder` | `Responder[Incoming, Outgoing] \| None`，上述连接级查询处理器。 |
| `_epoch` | `str \| None`，server 创建时已知；worker 从 None 开始，由协议驱动确认一次。 |
| `_reader` | `asyncio.Task[None] \| None`，唯一 reader，显式 start 后创建。 |
| `_inbox`、`_inbox_bytes` | `deque[tuple[Incoming, int]]`、`int`，已解析消息与原 UTF-8 字节数；保留消息/字节双限额。 |
| `_ready`、`_receiving` | `asyncio.Event`、`bool`，唤醒 receive，并拒绝同 Session 并行的业务 receive 消费者。 |
| `_read_error` | `Exception \| None`，reader 结束原因；FIFO drain 后传播。 |
| `_pending` | `dict[ReplyKey, asyncio.Future[Incoming]]`，唯一临时请求关联表，最多四项。 |
| `_request_sends` | `set[asyncio.Task[bool]]`，最多四个 request 自己的 send 等待者；用于同时观察发送与 pending 失败，并在 close 中 join。没有额外消息队列。 |
| `_send_lock` | `asyncio.Lock`，所有发送入口共用，禁止并发底层 write。 |
| `_write` | `asyncio.Task[bool] \| None`，仅当前底层 write 的临时任务；使 close 能终止 I/O，而不取消调用 send 的整个业务 Task。无 writer 循环、无发送队列。 |
| `_write_error` | `SessionClosed \| None`，写入失败/取消后关闭写侧的原因，禁止后续发送。 |
| `_handlers` | `set[asyncio.Task[None]]`，最多四个入站查询任务，包含生成及发送应答的整个过程。 |
| `_busy` | `deque[Outgoing]`，最多四个很小的 busy 应答，含当前正在发送的队头。 |
| `_busy_sender` | `asyncio.Task[None] \| None`，至多一个，串行发送 busy 应答。 |
| `_closed`、`_close_task` | `bool`、`asyncio.Task[None] \| None`，同步封闭入口与幂等、受保护的连接清理。 |

不另存“registered/ready”状态、不加全局 pending，不把 `PendingToolCall` 放进 Session。
`_write` 与 `_close_task` 都是通信任务，不能包含执行/业务 stop 工作。

| 公开签名 | 责任 |
| --- | --- |
| `def __init__(self, transport: Transport, *, parse: Callable[[str], Incoming], reply_key: Callable[[Incoming], ReplyKey \| None] \| None = None, responder: Responder[Incoming, Outgoing] \| None = None, epoch: str \| None = None) -> None` | 保存依赖，不在构造器启动 reader，不注册 worker。 |
| `@property def epoch(self) -> str \| None` | 只读连接 epoch；不代表业务已 ready。 |
| `def bind_epoch(self, epoch: str) -> None` | None→非空 epoch；相同值幂等；不同值抛 ValueError，由业务驱动转为协议错误。 |
| `def start(self) -> None` | 启动唯一 reader；重复启动或已关闭时失败。 |
| `async def receive(self) -> Incoming` | 取有序业务消息；取消观察者不取消 reader、不丢已取出的消息。 |
| `async def send(self, message: Outgoing, *, is_current: Callable[[], bool] \| None = None) -> bool` | 串行实际发送；返回 True 表示底层 await 成功返回，False 仅表示发送前 guard 不成立而跳过。 |
| `async def request(self, message: Outgoing, *, key: ReplyKey, timeout: float) -> Incoming` | 在发送前登记临时 Future，timeout 覆盖等锁、write、等待 reply；所有退出路径清理关联。 |
| `async def close(self, code: int = 1000) -> None` | 立即封闭连接级入口，关闭底层 socket 并完整 join 自己的任务；幂等，首个关闭原因/代码生效。 |

私有实现只需 reader、查询运行、busy drain、受保护 write、pending 失败和统一关闭几个职责：

```python
async def _read(self) -> None: ...
async def _respond(self, message: Incoming) -> None: ...
async def _send_busy(self) -> None: ...
async def _write_message(self, raw: str, is_current: Callable[[], bool] | None) -> bool: ...
def _fail_pending(self, error: SessionClosed) -> None: ...
def _begin_close(self, code: int) -> asyncio.Task[None]: ...
async def _finish_close(self, code: int) -> None: ...
```

这些签名定义责任，不要求为每个 helper 留测试。若实现需要新增持久状态、通用 registry 或
更多能力对象，应先修订计划，不能通过堆包装层绕过边界。

### 1.5 单 reader、执行 FIFO 与查询分派

reader 顺序：读原始消息 → 方向 parser → 临时 reply 匹配 → Responder 查询分派 → 其余进 FIFO。
分派过程不 await 慢业务或发送锁。Future 已完成/不存在、旧 epoch 的历史回复都直接消费，
不落到执行状态机。stop/工具/结果 ACK 从不进入临时 reply 匹配。

**FIFO：** 保留 32 条 / 16 MiB，容量检查计入当前待入队消息的原 UTF-8 字节数；溢出即停止读取，
立即失败 pending，不无限等业务消费。worker 新增相同有界 FIFO，这是持续 reader 带来的明确
保护策略变化。它限制缓存，不能代替底层 WebSocket 的单帧大小限制。

**查询：** 四个任务槽覆盖 history handler 和回复发送。满时使用一个 busy sender 和最多四条
小应答；队头发送完成才出队，不另留一个未计数的在途项。第五条尚未发送的 busy 应答触发
SessionOverflow，关闭连接。reader 不 inline await busy send，也不每次 create_task。

四条 busy 缓存容纳“旧四个查询处理仍未结束，对端超时后又发一批四个查询”的正常短突发。
它不是对端违规的证明：连续超时轮次或持续写阻塞仍可能超过这个上限，此时选择有界内存和
断连，调用方收到 worker_unavailable。该策略需随计划审批，不声称合法流量永不溢出。
busy 仅用于拒绝请求，不是通用 outbox；不持久化、不重试、不跨连接迁移。

reader 读断开/解析失败时保留已入队业务消息，按原行为 drain 后再传播原因；马上失败并清空
pending。查询和 busy 任务最迟在统一 close 中取消并 join，不由 reader 持锁等待它们结束。
业务可能还要发送最终 ProtocolError，故单纯“读侧已结束”不等于写侧已封闭。外部显式
close/替换则封闭写侧并丢弃 FIFO，不再交付旧连接缓存。

### 1.6 发送完成语义、取消与背压

**采用一把发送锁，无通用 writer 队列。** `send` 序列化后等锁，在锁内创建唯一临时 write Task。
该 Task 在真正调用底层 `send_text` 之前检查 `_closed`、`_write_error` 和 `is_current`；
guard 与调用底层 send 之间没有 await。guard 必须同步、无副作用，不能捕获长期网络对象。
因此新增加的取锁等待和 Task 调度都不会扩大工具发送前取消漏洞。

- 返回 True 仅说明底层发送调用返回；可能只是交给传输缓冲，不证明 peer 已收到或完成业务提交。
- 等锁时取消：退出自己的等待，不损坏 Session，不触碰执行工具原 Future。
- 原工具 Future 在等锁/调度期间取消：guard 返回 False，不写；Client 重新观察队头/终态。
- 原工具 Future 在底层发送已开始后取消：不撤回发送；原驱动继续消费匹配回复或 StopExecution。
- 发送调用者在 write 阶段取消，或底层发送失败：发送结果可能未知，封闭该 Session 写侧、失败
  pending，并发起统一关闭；不继续复用状态不明的连接。正常业务去重/恢复继续用原 call_id/journal。
- close 能取消并 join `_write`，使等锁/发送中的协程醒来失败；不通过 cancel 调用者 Task 来实现。
  write 任务的失败/取消要检索，通信任务不能等待自身 join。

**背压边界：** 不新增统一的“10 秒/30 秒 socket write timeout”。原查询 30 秒总预算继续包括
等待锁和写入；其余写等待沿用原 transport、心跳/连接退出机制。当前调用拓扑中的发送生产者有界：
server 为业务驱动一条 + 最多四个查询；worker 为驱动、心跳、四个查询和一个 busy sender。
这保证本批没有无界发送缓存，**不承诺正常 send 的绝对时长上界**。持续 transport 阻塞时，查询
可能超时；被取消的在途 write 会结束连接。若要添加全局写时限，另作行为策略决定，不能默选数值。

不引入自动重试。server 既有 Flow tool 去重仍由 `FlowToolService` / delivery 存储处理，
Session 不把 transport 不确定性解释为“远端没有执行”。[S13]

### 1.7 临时关联、全局准入与业务验证

`request` 要求已 start、无读/写错误、有 reply 提取器、已确认 epoch，且 key 的 epoch 相符。
它同步检查最多四项的容量和重复 key，并在任何发送 await 前登记 Future；最快回包也不能漏配。
同 Session 超时/取消只删除自己的 Future；读失败/close 则立即失败全部 Future 并清表。
finally 使用对象身份检查避免重复删除，取消未完成 Future 或检索已有异常，不留下未检索异常。

request 同时观察自己创建的 send 等待 Task 与 reply Future，不能仅 `await send; await future`：
否则 reader 已失败但请求仍卡在写锁时，外层查询不能及时失败。pending 失败立即取消/join 自己的
send 等待者并传播连接错误；成功回复先到时仍须等待实际 write 成功，不能把 send 偷换成入队确认。
局部超时/调用者取消优先保留其 TimeoutError/CancelledError，内部关闭取消 send 等待者则返回
SessionClosed；统一 finally 回收局部 Task。四个请求槽同时约束这些 Task，不新增无限并发来源。

`ConversationBroker` 拟保留字段：`timeout: float = 30.0`、
`connections: dict[str, Session[WorkerMessage, ServerMessage]]`、`_inflight: int = 0`。
不再保存 epoch 副本或 PendingQuery/Future。

| Broker 接口 | 拟定合同 |
| --- | --- |
| `def __init__(self) -> None` | 初始化路由和全局计数。 |
| `def attach(self, worker_id: str, session: Session[WorkerMessage, ServerMessage]) -> None` | 仅发布已确认 epoch 的当前 Session；替换/关闭由既有 Gateway 负责。 |
| `def detach(self, worker_id: str, session: Session[WorkerMessage, ServerMessage]) -> None` | 只有仍指向该实例时删除路由，不能删除新连接。 |
| `async def query(self, worker_id: str, query: HistoryQuery) -> HistoryPage` | 既有公开业务入口不变。查路由、同步检查全局 64 并加一，构造 UUID 请求，调用 Session.request；校验结果，finally 减一。 |
| `def _validated(self, request: ConversationQuery, response: ConversationResult) -> HistoryPage` | 原 accept 中的业务错误与页面验证迁到这里，不进入公共层。 |

`conversation_result_key(message: WorkerMessage) -> ReplyKey | None` 只提取 ConversationResult。
Session.request 返回 WorkerMessage 后，Broker 用类型检查收窄；不通过 cast 声称已验证业务结果。

业务验证逐项保留：error code、目标历史 session、页条数不超请求 limit、512 KiB 序列化结果上限、
has_more/next_cursor、一致的 item ID 前缀与合法的后续游标。Flow/session 归属仍由
ConversationService 从可信数据库元数据确定。不能因为 Future 已完成就把页面当作可展示数据。[S2]

错误映射：SessionBusy→query_busy；总预算到期→query_timeout；断连/替换/写失败→worker_unavailable；
内容错误→invalid_response；调用者取消继续传播 CancelledError。全局计数在唯一 finally 归还，
不复制成第二套 pending 或共享 Budget 框架；结果验证完成前仍算本次调用占用全局配额。

worker `_answer_history(self, message: ServerMessage) -> WorkerMessage` 只负责窄化查询、构造
HistoryQuery、`asyncio.to_thread(read_history, request)` 和 ConversationFailure 映射，返回
ConversationResult；不发送、不创建 task、不访问 execution。`_is_history_query(message:
ServerMessage) -> bool` 与 `_history_busy(message: ServerMessage) -> WorkerMessage` 只提供
Responder 的两个同步回调。worker 当前无需 reply 提取器；不加 max_pending=0 之类的角色特例。

### 1.8 握手、epoch 与连接替换

**server：** 认证 header → accept → 用裸 socket 读唯一 Register 并验证身份 → 创建带随机 epoch
的 Session → start。Register 的读取已经返回后才能启动 reader，不能并行抢包。
沿用现有 registry 替换旧连接；旧 Session 先封闭并 close/join，完成后发布新 Broker 路由，
再执行 stop/recovery/Registered。等待旧 close 后复查当前 registry 仍是本实例且本 Session
未关闭，避免重叠替换把已经淘汰的连接重新发布；不抽公共 registry 或重新设计调度。

发布路由仍早于业务恢复完成，保留查询可能早于 Registered 的合法时序。旧路由在关闭窗口中
至多快速失败为 unavailable；旧 handler 的 finally detach 必须检查实例身份。
Session.close 只 join 通信；不等待或取消 server 的长业务 I/O，也不把新连接接管当作释放 binding。

**worker：** 创建未绑定 epoch 的 Session → `await send(Register)` → start → 由同一 FIFO
驱动握手。reader 在 Register 发送返回后启动，保证 worker 第一帧仍是 Register；期间到达的帧
留在 transport。握手与常态无第二个 recv 入口。

| 握手消息 | epoch / ready 处理 |
| --- | --- |
| ConversationQuery | 由 Responder 在原 Session 回显其 request_id/epoch；不确认 epoch，不置 connected，不赋予执行权限。 |
| StopExecution | 协议驱动先 bind_epoch，再调用原 stop 流程并等 StopAccepted；之后继续握手。 |
| RecoveryRejected | 无 epoch，沿用原拒绝恢复清理；不能把 None 当成重置旧 epoch 的指令。 |
| ResultAccepted | 在合法恢复位置 bind_epoch，交 owner.on_result_accepted，然后继续握手。 |
| Registered / ResumeAssignment / ReplayResult | bind_epoch；与之前合法响应已确认的值必须一致；随后按原语义 connected / heartbeat / 恢复执行。 |
| ProtocolError | 原协议错误退出。 |

epoch 一经确认不再覆盖。预读的普通消息保持 FIFO，业务取出时再做原身份/fencing 检查；
Session 不猜测 Assignment 是否有执行权限。早到历史查询的响应始终留在原连接，不迁移到新
Session；旧 epoch 回复无法通过新请求的关联验证。FlowToolResponse 没有 epoch，仍靠当前
连接的协议驱动与 owner 的 call_id 校验；不能为了“统一”补字段。[S6]

worker 每次 `run_connection` 在 finally 完整 close/join Session。整个旧 Client 的心跳、
协议驱动与通信任务也必须完全 join，才允许新 Client 借用同一 owner。Runtime 原有
“join 通信 → execution.shutdown → 释放 root lock”不变。[S5]

### 1.9 关闭与失败矩阵

close 先同步标记 closed、失败/清空 pending、清 FIFO/busy 缓存、唤醒接收者，并取消 reader、
query、busy sender、request send 等待者和当前 write。唯一 close Task 负责底层 close，并在
finally 完整 join 上述任务；不能先等待底层 close 返回才唤醒被阻塞的 write。重复或外层反复
取消仍要完成通信清理，最终按原取消语义返回；不要持发送锁等待持锁者退出，也不要 self-join。

| 事件 | 临时请求与业务消费者 | 执行/持久事实 |
| --- | --- | --- |
| 读断开/解析失败 | pending 立即失败；已收到 FIFO 按序 drain 后传播原异常；禁止新 request。 | 不取消执行、不释放 binding。 |
| 执行 FIFO / busy 缓存溢出 | 同上；业务入口转为协议错误并关闭连接。 | 不合成结果或停止证据。 |
| 显式 close/替换 | pending 立即失败，旧 FIFO 不再交付，连接任务完整 join。 | 工具原 Future 和原 Task 留在 owner。 |
| 请求超时/调用者取消 | 自己的 pending 清理；全局计数归还；已发的远端查询不伪称已取消。 | 无执行影响；wire 不增加 Cancel 消息。 |
| 写阶段取消/异常 | 发送状态未知，封闭连接、失败全部临时请求，不自动重试。 | 原执行恢复/去重证据保留。 |
| 旧连接、错 epoch、重复/迟到 reply | 消费并忽略；不进入执行 FIFO，不完成新请求。 | 不触发 ACK 或授权。 |
| 历史业务错误 | 仍返回对应 ConversationResult.error。非预期 handler 异常检索并记录，沿用无成功回复/调用方超时。 | 不转成 Failed 执行结果。 |
| stop 等待期间断线 | 只取消通信观察者；后继连接 join 原授权 stop。 | 原 shielded stop 继续完成持久证据与回收。 |

`asyncio.to_thread` 的等待 Task 被取消不保证底层同步历史读取立即停止。旧读取结束后不能发送
到新 Session；跨重连的线程/历史缓存生命周期保持既有实现，不宣称本批解决线程强制取消或
进程级历史读取预算。Session 没有接管 ExecutionState 的历史缓存。[S3]

### 1.10 要删除的重复实现与执行消息边界

- 删除 WorkerConnection 的 reader、deque、错误/关闭逻辑和构造器对 Broker 的注册副作用。
- 删除 Broker.PendingQuery、pending 表、accept/关联/逐连接计数扫描和其中的 Future 清理；
  页面校验迁出保留，connect/disconnect 改为纯 attach/detach。
- 删除 Client 的 `_history_tasks`、`_history_finished`、查询分流版 `_receive_message`、
  `_connection_epoch` 和每连接历史任务清理循环；由 Session 字段/接口接管。
- 删除 Gateway 二次 parse；Session 存在后禁止旁路裸 socket 发送（包括 ProtocolError）。
  只有鉴权/首个 Register 之前的失败处理可直接使用 socket。
- Client 的握手、心跳、Assignment/Started、工具、终态及 stop/replay 全部调用 Session
  send/receive，但 `_drive_assignment`、`_handle_stop` 等业务顺序保持在 Client。
- Gateway 的工具 execute、result apply、StopStore.confirm 等仍由原串行驱动 await，
  只有 ConversationResult 快路径绕过它们。通信匹配绝不代替结果应用或停止确认。

不留下旧收发路径作为 fallback，也不同时保留两套 pending。所有直接引用和测试 fake 一起迁移，
不是让旧类委托新类后宣称抽取结束。

### 1.11 方案比较、成本和风险

| 选择 | 理由、代价与未选方案 |
| --- | --- |
| 中立 Session + 三个 Responder callable | 分离实际变化的 parser、reply 分类和查询业务；复用 reader/pending/关闭。不硬编码历史页业务，也不建 handler registry、Codec 类树或消息总线。 |
| send lock + 一个当前 write Task | 串行化与真实完成语义清楚；当前 write 可由 close 独立 join。比直接 await 多一个短 Task，但避免 close 去取消整个业务调用者；不新增 writer 队列、发送持久化或自动重试。 |
| Broker 一个全局计数 | 保留跨 worker 上限；减少重复 pending/关联。只用每 Session 限流会丢全局预算；共享 Budget 类在本批只有一个调用方，暂不增加。 |
| 持续 reader + worker 有界 FIFO | 慢 stop/recovery 不再停读；增加缓存和超限断连行为，使用相同限额和确定性测试确认。保持拉取式 recv 无法真正共用分派生命周期。 |
| 单 busy sender + 四条小缓存 | 防止拒绝回复阻塞 reader 或无限创建任务；容量有限，持续写阻塞可能断连。只留一个 busy 任务会拒绝正常短突发，通用 writer 优先级队列又扩大了范围。 |
| 业务驱动确认 epoch | 支持 Registered 前恢复/停止，不让历史查询冒充注册。新增同连接 epoch 不一致拒绝；不允许默认覆盖，避免把旧回复绑定新状态。 |
| 固定 v15，能力对称但消息权限保留 | 同核心能双向发请求/应答，实际类型方向不扩大；不以第二阶段为由预先实现 envelope。 |

主要风险为：锁等待重新引入工具取消窗口、预读改变旧测试脚本节奏、query 超时与 write 取消交错、
关闭 self-join、registry 重叠替换、业务 ACK 被误分类、线程取消保证夸大。下面用行为测试验证。
不承诺任意数量发送调用者的资源上界；本阶段公开给既有、有界的协议驱动。未来若新增生产者，
必须重新核对发送背压，不能默认这个证明自动成立。

### 1.12 获批后的实施顺序与交付

1. 确认计划及 docs-only Draft PR 发布权限；从最新 main@origin 再核对漂移，在本 workspace
   使用一个 bookmark / PR。计划 PR 后续直接承载实现，不关闭重开。审批前不写测试/产品代码。
2. 抽公共 Session 的唯一 reader、typed FIFO、串行发送与临时关联；迁移原连接行为测试，
   用同核心的内存双向通信验证发送/请求/应答合同。
3. server 接入：Broker 去掉关联表，保留全局准入与校验；Gateway 全部经 Session 收发，
   移除 WorkerConnection，保留注册/调度/结果应用/停止业务。
4. worker 接入：最小适配器、查询 Responder、握手 bind_epoch、工具写入前 guard、完整关闭。
   更新直接依赖旧 Client 私有 epoch/recv 的测试装配，保留原执行和停止证据断言。
5. 双端回归、真实 PostgreSQL、静态/构建/导入检查、简化对照；同步当前架构文档与本记录。
   验证通过后才进入项目代码审查 gate；后续代码 push 沿用同一 bookmark / PR。

没有 wire 或存储迁移、双写和兼容壳。实施只改变内部结构；发布/部署仍需对应授权。回滚遵循
原进程停止/恢复规则，不能因回滚重跑丢失 Task 的 started 编码任务。

### 1.13 验收与简化对照

以下是未来验收，不是第二阶段已经通过的结果。优先扩展现有行为测试，避免为每个 helper 或
属性建立测试；可用参数化覆盖错误时机。对内部表的机械断言不替代“下一查询确实能发出/完成”。

| 验收场景 | 观察与主要起点 |
| --- | --- |
| 双端共用与对称核心 | 两个同类 Session 同时 request/respond、同时发送普通消息，reply 不堵 FIFO。测试专用最小 Message 类型仅留测试内，不更改 v15 联合类型；真实两端仍分别校验方向。 |
| 单 reader / 业务 FIFO | 原并发 recv 探针、取消 idle receive 后 reader 仍工作；消息条数和 UTF-8 字节两个容量边界；reader 失败 drain 与显式 close 丢弃分别验证。原 `test_worker_connection.py` 行为保留。[S8] |
| 慢执行、preparation、tool、result | HTTP→Broker→两端 Session→历史读取真实路径在释放慢操作前完成；保留 Codex/Amp 及 slow_export 场景。`test_conversation_e2e.py`。[S9] |
| worker 慢 stop / recovery | 清理尚未完成时查询仍返回；不并行应用执行消息，stop/ACK 语义不变。扩展既有 worker/stop 场景。 |
| 全局/局部限流 | 同 Session 第五个即 busy；16 个 Session 各占四个槽时，第 17 个 Session 的首个查询触发全局 busy；结束后新查询获准。worker 四个 handler 满时合法 busy 短突发得到回复；持续超限可控断连，无 task/缓存增长。 |
| 超时/取消/断连清理 | 等锁、write、等 reply 三处分别注入取消/超时；快速回复、重复回复、reader 失败、显式 close 与完成同轮；原等待者得到正确错误，所有配额可复用，无未检索 Future 异常。 |
| 替换与旧回复隔离 | 强制两个不同 Session 用相同 request_id，分别改变 epoch 或保持相同键，旧连接回复都不能完成新请求；旧 detach 不移除新路由；替换期间立刻失败旧查询。 |
| 历史业务验证 | 保留 cursor/HTTP 归属断言，补足 session、limit、512 KiB、has_more、ID 前缀与 next_cursor 校验缺口；错误留在查询响应，不改变执行结果。`test_conversation_proxy.py` / worker 用例。 |
| 注册/恢复顺序 | query→Registered；StopExecution→StopAccepted→Registered；RecoveryRejected→Registered；ResultAccepted→Registered；直接 Resume/Replay；绑定后不同 epoch 拒绝；预读不改变 FIFO。 |
| 工具与 Started | 原发送失败/取消不启动、借出后取消、完整 Client 替换及原 Future 重放；新增被其他 write 占锁时取消，未写工具请求必须跳过；已发后取消仍消费正确 call_id。`test_worker_client.py`。[S10] |
| owner / stop / shutdown | 离线完成落盘、Register/Resume 交错、两处 stop 清理断点通信替换、原 nonce 回执/丢 ACK、无 Task 不推断 stopped、真实父子进程退出和 root lock 顺序。`test_stop_protocol.py` / `test_worker_runtime.py`。[S11][S12] |
| 原双端业务与存储 | `test_remote_e2e.py`、`test_remote_asgi.py` 及 archive/result/recovery 相关回归，实际 PostgreSQL，Flow binding 与结果/停止 ACK 事实不变。 |
| 公共包边界 | AST 静态导入 + 新解释器 import 公共 Session / worker runtime，不加载 controller DB / executor / WebSocket SDK 到公共层；build 包含新模块、旧 connection 模块消失。 |

既有 fake 有些按 recv 次数返回消息，真实 reader 预读会改变调用时机；必要时改为以实际已发送
消息/同步事件为依据。不能通过删除 Started/ACK、call_id、journal 或进程事实断言让测试迁移通过。
现有测试名称不等于覆盖事实：目前“invalid_response”命名的一个用例只测试取消，需要补齐真实
坏页面验证；全局 64、16 MiB 和等待写锁取消也需要实际场景。[S14]

检查顺序：定向通信/生命周期 → 双端 PostgreSQL 与 archive/recovery → 完整 pytest →
`uv run ruff check app tests` → `uv run mypy app tests` → 变更文件格式检查及
`uv build --no-sources`；按 CI 校验需要时先在隔离 workspace 构建 console，再确认 wheel 内容。
不格式化共享根目录，不把 .amp/in 产物加入 PR。格式基线若已有差异，区分旧问题与本批新增，
不得借重构全仓重排。没有安全 PostgreSQL 时应明确验收受阻，不能拿 skipped 当通过。

**实现后、代码审查前的简化对照：** 使用临时变体/故障注入，相同输入一次只移除一个机制，
不把变体提交为永久测试：

1. 将历史处理/匹配重新放回串行业务 recv：慢 tool/result/stop 查询应失败；证明持续 reader 的必要性。
2. 去掉发送锁；再单独把 guard 移到取锁/调度前：分别应暴露并发 write 与已取消工具发送。
   当前 write Task 若可被更简单方案移除，必须同时通过 blocked write 下 close 不取消业务调用者、
   join 完整、Started 顺序的验证，才采用简化。
3. busy 改 inline await 或无限 create_task：阻塞 write 时分别应暴露 reply 停读或任务增长；
   四条短突发与第五条溢出必须使用确定性屏障，不依赖随机 sleep。
4. 删除全局计数或把它下放到每 Session：跨 worker 的第 65 个请求应错误获准；正常方案应拒绝。
5. 去掉 epoch/实例隔离、将 stop 的 request_id 当临时 reply，或让读失败延迟到业务 receive 才
   清 pending：对应旧回复误配、业务 stop 丢失和慢业务期间查询泄漏的场景应失败。

若删除机制后全部合同仍成立，先简化再审查；无法稳定触发的对照不能作为必要性证据。

### 1.14 规划期实际核查与 Oracle 取舍

本次只读取源代码、依赖、既有测试与第一阶段交接，并运行现有定向测试建立未改动基线。
包括通信、历史 E2E、Client、stop、remote ASGI、导入边界及 runtime；使用独立 UTF8 PostgreSQL
经 loopback TCP 实际执行数据库用例，全部通过、无跳过。首次 PostgreSQL 启动因本机 locale
失败，设置 `LC_ALL=C` 后正常启动并完成测试；没有修改代码绕过问题。测试库已停止。
这不代替未来实现验收，也没有在规划期跑完整 suite、静态检查或构建。

依照 consulting-claude-oracle，通过其限定脚本传入编号源码材料生成计划草案；已核验 CLI
初始化与实际响应模型均为 `claude-opus-5-5`，tools/mcp_servers 均为空，结果 success 且进程退出 0。
原始流不提交。证据与基线日志位于
[本阶段 thread](https://ampcode.com/threads/T-01a18268-4062-7317-a519-abd87ee8d147)。

主线程核对后采用单锁、显式 epoch、业务校验留外层、全局计数与有界 busy 缓存建议，并作以下取舍：

- 不采用无源码依据的统一 10 秒写超时；明确仅查询有 30 秒总预算，不许声称锁等待绝对有界。
- 使用一个可 join 的当前 write Task，使 close 不必取消业务调用者；发送前 guard 放在实际 write
  Task 内，覆盖 Task 调度窗口。
- 不采用 max_pending=0 或可调限额参数来模拟 worker 角色；合法消息方向由原类型/parser 保持。
- busy 容量四只保证有限短突发，溢出不能推断对方违反四个 pending 的协议。
- 不仅取消 reader：关闭还必须 join 当前 write/查询/busy 任务并隔离旧 route；不混入公共 Gateway。

### 1.15 审批事项与延后工作

**需要批准的实质选择：**

1. 采用以上中立 Session / Responder 边界；全部既有消息迁入公共收发层，执行业务状态机留两端。
2. 采用单锁、可 join 当前 write、写入前 guard；保留现有查询超时，不新增统一 write 时限。
3. 接受 worker 持续 reader 的 32 条 / 16 MiB FIFO、四条 busy 缓存及超限断连；显式 close
   不再交付旧 FIFO；同连接 epoch 确认后不允许覆盖。
4. 确认本阶段 docs-only Draft PR 发布，以及是否在计划批准后开始实现。展示计划或发布草案
   均不自动等于实现授权；未批准前不写产品代码或测试。

没有尚未查清且阻塞选方案的源码问题。具体泛型写法、fake 装配、日志位置和确定性屏障可在
实施时解决；若实际需要改 wire/journal/DB、增加长期 owner 或改变 ACK/执行权限，先追加计划
修订审批。公共 Gateway / envelope / outbox / 跨进程工具恢复、进程级历史读取预算均延后。

## 2. PR change timeline

<!-- 初始方案分享后，只追加真实的需求变化、实质设计决定和实施结果；不记录单纯审批。 -->

## 源码依据（规划固定基线）

[S1]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/controler/workers/connection.py#L33-L97
[S2]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/controler/workers/conversation.py#L33-L163
[S3]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/worker/transport/client.py#L108-L319
[S4]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/worker/execution/lifecycle.py#L65-L374
[S5]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/worker/runtime.py#L96-L171
[S6]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/contracts/protocol.py#L243-L425
[S7]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/controler/workers/gateway.py#L112-L180
[S8]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/tests/test_worker_connection.py#L47-L100
[S9]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/tests/test_conversation_e2e.py#L49-L162
[S10]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/tests/test_worker_client.py#L587-L724
[S11]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/tests/test_stop_protocol.py#L54-L200
[S12]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/tests/test_worker_runtime.py
[S13]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/app/controler/workflow/tools.py#L55-L139
[S14]: https://github.com/b1indsight/agentflow/blob/bbf85048c432bf3d0e2960d36adde758eaeba9ea/tests/test_conversation_proxy.py#L26-L95
