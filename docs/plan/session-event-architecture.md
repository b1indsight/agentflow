# Session 事件驱动的双端统一 — Feature record

**Stage:** Implementation verified — 修订 021–023 的 client 模块搬迁、结果接纳类型命名与测试精简已实现并通过本地验收；修订 020 的类型归属拆分仍待审批，未实施。发布、独立 review 和精确 head CI 状态见下方 PR 与验收摘要，不沿用旧 head 结论。
**PR:** [#127 — 沿用同一方案/实施 PR](https://github.com/b1indsight/agentflow/pull/127)
**Bookmark:** `refactor/session-event-architecture`
**Baseline:** [main / PR #125 合并版本](https://github.com/b1indsight/agentflow/commit/086e68efe55403e2c7c66b23771e2db3cf78bcbd)

**当前修订：**可关联的 payload 校验失败返回 `tool_result`，沿用原 method，`reply_to` 指向被拒项，
`error` 仅含固定 `code` 和 `message`，不含 details、truncated 或任何诊断内容
（[修订 005](#005-错误响应不暴露校验详情)）；不新增错误通知路由。该 wire 合同已实现为 v17，持久格式不变
（[修订 004](#004-用-tool_result-返回消息验证错误)）。请求按 `包名.方法名` 精确路由，统一用
`register(route, model, callback)` 注册；普通回复由 Dispatcher 关联原调用，匹配后执行可选
`on_reply`，成功后再交付给调用方（[修订 017](#017-dispatcher-关联回复on_reply-在交付前执行)）。
请求回调只接收注册的模型，验证错误由 `configure(validation_error=self.on_error, ...)` 统一交给
连接业务对象（[修订 009](#009-集中验证错误分派收窄业务回调类型)）；wire method/reply_to 不变。
结果接纳类型现为 `AcceptResultRequest`、`AcceptResultResponse`、`ResultAcceptance`，
分别表示请求、回复和回复数据；替代历史记录中的 `AcceptResult`、`ResultAccepted`、
`AcceptedResult`（[修订 022](#022-区分结果接纳请求回复与回复数据的类型命名)）。
worker 按状态协议与业务分类，分别为 `transport/state.py` 中的 `WorkerStateCallbacks` 和
`transport/business.py` 中的 `WorkerBusinessCallbacks`；server 仍为 `ServerCallbacks` 组合
`ServerFlowCallbacks`，只有 server 注册 Flow 工具请求路由。该分类替代
[修订 002](#002-两端业务回调对称命名) 与 [修订 012](#012-分离-flow-路由与工具回调) 的 worker 名称。
用户已要求去掉 `Delivery`；Session 直接交付既有 message，接口为
`next_event(...) -> Incoming | Ready | None`。字节记账与剩余 payload 留在 Session 内部，
不再保存派生的队列总计字段（[修订 011](#011-删除-session-的派生队列计数)）。
[修订 001](#001-去掉-delivery直接交付-msg) 撤销初稿中的 Delivery 类型、队列结构及相关流程。
通信代码沿用 `Message` / `message` 命名，完整消息钩子为 `on_message` / `on_message_done`，
无控制变体为 `WorkerMessage` / `ServerMessage`；含控制变体的方向联合为
`WorkerMessageUnion` / `ServerMessageUnion`，不保留 frame 或 PayloadMsg 旧名别名
（[修订 015](#015-沿用-message-命名并同步调整联合类型)）。
`WorkerClient` 已删除，拨号、认证、重连退避与连接任务回收直接归 `WorkerRuntime`；
Session 和执行 owner 的边界不变（[修订 016](#016-移除-workerclient由-runtime-直接管理连接)）。
`Connection` 和 `WebSocketTransport` 也已归入 `worker/runtime.py`，删除残留的
`transport/client.py`（[修订 021](#021-删除-client-模块将连接接口与适配器移入-runtime)）。
WorkerRuntime 独立创建并平级挂载状态、业务两对象，两者互不持有或调用，沿用
[修订 018](#018-worker-协议与-flow-回调独立挂载) 的独立挂载。业务类自行管理工具观察并注册
worker.history；所有 payload 与验证拒收统一 FIFO，慢 history 与其他慢回调互相等待。
Broker 持有临时查询 Future、配额和发送加等待预算，Dispatcher 是唯一回复关联表；Session
不再有查询表、Responder 或 handler/busy 旁路。具体职责、关闭/EOF 合同及验证见
[修订 019](#019-worker-状态与业务分类删除-history-旁路)。
下方 Initial plan 保留为历史基线，其中被后续修订替代的内容不再适用。实际实现与验证见时间线 006–019、021–023；020 仍为提案。

## 1. Initial plan

### 1.1 动机与目标

用户在[来源讨论](https://ampcode.com/threads/T-01a1a199-8e0a-7690-9c0e-3b4a4bf22852)中的最新要求：

> 开一个新session先统一worker和server架构，目标是修改为由session传递的事件驱动的整体架构，worker和server在处理事件之间的区别是上面挂载的回调对象之间的差异

此前还明确提出路由和 Session 管理归 Gateway。本方案将前序记录中“Runtime 装配完全对称后置”
推进到当前范围；保留前序安全合同，不沿用该后置决定。

**已核实的现状：**

- 两端共用 Gateway 的登记、查找、替换、关闭，以及 Session 的 reader、FIFO、写入、临时查询。
  Gateway 尚不驱动事件。worker Gateway 在 Client 内部构造，server Gateway 由 Runtime 构造。[S1]、[S2]、[S3]、[S13]
- worker `_run_connection` 自己等待 incoming、terminal、next_tool、heartbeat；server `handle`
  自己调度、等待帧或 wake、检查时限。两端还各自逐项调用 Dispatcher。[S3]、[S4]
- server 业务状态存在 `handle` 的局部变量中；worker 同类状态在 `_run_connection` 的闭包中。
  仅移动文件或改构造器不能消除这两套驱动。[S3]、[S4]

**目标与成功标准：**

1. 双端 Runtime 直接持有同类 Gateway；每条连接挂载一个真正执行角色业务的回调对象。
2. Session 提供同一个有界事件入口；Gateway 用同一个驱动处理网络交付、本地就绪、唤醒和时限。
3. Gateway 统一建立方法路由、登记/替换/退出；业务回调没有私有 receive/wait 大循环。
4. worker/server 的协议决策和连接局部状态只在各自回调对象中；跨重连执行及持久证据仍归原 owner。
5. 双端真实入口走新路径，删掉旧驱动和旧内部 API；运行 §1.10 的行为验证后才能声称完成。

**范围与非目标：**保持 wire v16、持久格式、SQL 业务授权、CLI 与部署方式不变。不引入依赖、
outbox、event sourcing、HA、消息总线、中间件链、查询重放或通用业务 supervisor。
worker 主动拨号/退避和 server 认证/accept 仍各用物理适配器。角色差异不要求把这两种接入方式写成同一函数。

### 1.2 事件的含义

这里的事件是**当前进程、当前连接的可处理输入**，没有持久投递或 exactly-once 承诺。

| 来源 | 共同驱动看到的内容 | 顺序、数据与完成含义 |
| --- | --- | --- |
| 网络帧 | `Delivery(frame, items, size)` | 一个帧一次交付；保留原帧，`items` 是仍未旁路消费的条目引用；帧内串行处理完才取下一事件 |
| 执行终态、工具可发送 | `Ready`，即携带类型安全结果的异步回调闭包 | Session 的观察任务只报告就绪；Gateway 串行调用闭包；终态/工具原始证据继续由 WorkerExecution 持有 |
| server jobs/stop 通知 | 合并 wake 标志，`next_event` 返回 `None` | 表示需要重新检查业务状态；不携带 Job，不保存另一份业务事实 |
| server 轮询/接收时限 | 等待到 `until` 后返回 `None` | 回到角色的 `poll`；由角色决定是轮询还是心跳超时，不创建 Tick 类型 |
| 连接开始、失败、结束 | Gateway 的生命周期回调 | 不进入可重放队列；结束清理即使异常或取消也执行 |

以下保留专用通信路径：纯控制 ACK 只确认收件；独立 history 查询/回复有界旁路；worker 心跳
使用通信侧周期发送器，避免被慢 stop 回调阻塞。它们不成为持久业务事件。混合控制/payload 帧中的
history 必须先经过控制授权。**不增加 sent 事件**：Started 的实际写入与后续启动仍内联执行。[S2]、[S3]

### 1.3 对象、生命周期与状态边界

| 对象 | 生命周期与职责 | 不拥有的内容 |
| --- | --- | --- |
| 两端 Runtime | 进程级组合根；直接创建 Gateway、业务依赖和接入器；创建每连接回调 | 单独一套消息消费循环 |
| Gateway | 进程级 peer 表；当前 Session 路由；每次 `serve` 的 Dispatcher、共同驱动及登记/退休 | Job/lease/Flow 状态、执行 Task；不成为 ASGI 任务 supervisor |
| Session | 一条物理连接；reader、FIFO、写入、ACK/query、就绪观察、时限、通信任务 join | ready/starting/stopping、结果接纳状态、业务权限、真实执行任务 |
| worker `WorkerCallbacks` | 每连接；握手、执行/停止/工具/结果回调，借用进程级 WorkerExecution | socket 拨号、peer 表、跨重连执行所有权 |
| server `WorkerHandler` | 改为每连接的业务回调；调度、恢复、Flow 工具、结果与停止校验 | socket 认证/accept、Session 登记算法、私有 wake/Session 表 |
| WorkerExecution / ExecutionState | 沿用进程级执行与持久证据所有权 | Gateway/Session 引用、网络重连策略 |
| server stores / coordinator / tools / results | 原业务事务、Flow binding、权限与外部效果 | 事件投递保证 |

不增加 Endpoint、Run、Context 或与回调对象并存的连接业务容器。server 直接改造现有
`WorkerHandler`，不再增加 `ConnectedWorker → WorkerHandler` 或 `ServerCallbacks → WorkerHandler` 层。
worker 只新增一个有实际业务方法的 `WorkerCallbacks`；`WorkerClient` 收缩为拨号器。

**连接局部字段（拟定完整业务字段）：**

| WorkerCallbacks 字段 | 类型与含义 |
| --- | --- |
| `worker_id`, `session`, `execution` | `str`、`Session[ServerMessage, WorkerMessage]`、`WorkerExecution`；身份、固定原连接、借用执行 owner |
| `on_ready` | `Callable[[bool], None]`；向 Runtime 投影健康状态，不授予业务权限 |
| `register` | `Register \| None`；本连接实际发送的注册帧，供恢复拒绝关联 |
| `ready` | `bool`；合法 Registered 已处理 |
| `expected_result`, `expected_stop` | `AcceptResult \| None`、`AcceptStop \| None`；本连接正在等 A/B 回复 |
| `pending_tool` | `PendingToolCall \| None`；实际写出的原工具条目/Future 引用；不取得 Future 所有权 |

| WorkerHandler 字段 | 类型与含义 |
| --- | --- |
| `worker_id`, `register`, `session` | `str`、`Register`、`Session[WorkerMessage, ServerMessage]`；认证身份、原始首帧、固定原连接 |
| `worker` | `WorkerRecord \| None`；同步 `worker_connected` 成功后的记录，之前为 None |
| `current` | `tuple[Job, str, ExecutionTask \| None] \| None`；原 job/token/task 三元组 |
| `starting` | `bool`；已派发，尚未收到 Started |
| `stopping`, `stopped` | `StopIntent \| None`、`StopResult \| None`；停止意图与真实校验后的 R |
| `initial_stop` | `StopIntent \| None`；注册时发现的停止，保留缺 journal 的原处理分支 |
| `registered` | `bool`；已发送 Registered/恢复决定 |
| `receive_deadline` | `float`；单调时钟接收截止；stop 硬截止由 Session 保存，不重复存一份 |
| `disconnect_reason` | `str \| None`；最终以原 epoch 记录断连 |
| 既有依赖 | `DeliveryStore`、`JobStore`、`StopStore`、`WorkerStore`、`JobCoordinator`、`FlowToolService`、`ResultApplication`、`Metrics \| None`；保持原服务，无新 facade |
| 配置和通知 | `heartbeat_interval: float`、`heartbeat_timeout: float`、`poll_interval: float`、`notify_jobs: Callable[[], None]`；通知直接注入 Gateway 的 `wake_all` |

epoch 始终取固定原 Session；worker 路由获得原帧参数，不再用共享的 `frame_epoch` 临时变量。
回调构造只装配引用与初始状态；执行借用、DB 在线写入和网络 IO 都在 Gateway 准入成功之后。

### 1.4 公共接口与路由注册

以下是拟定接口，不是本阶段加入的实现。`Incoming/Outgoing` 均约束为既有 `Message`。
`Transport`、`ReplyKey`、`Responder` 继续复用；不新增可序列化协议模型。

```python
@dataclass(frozen=True, slots=True)
class Delivery[Incoming: Message]:
    frame: Incoming
    items: tuple[ToolCall | ToolResult, ...]
    size: int

type Ready = Callable[[], Awaitable[None]]

class Session[Incoming: Message, Outgoing: Message]:
    async def next_event(self, *, until: float | None = None) -> Delivery[Incoming] | Ready | None: ...
    def watch[T](self, name: str, source: Callable[[], Awaitable[T]],
                 callback: Callable[[T], Awaitable[None]]) -> None: ...
    async def unwatch(self, *names: str) -> None: ...
    def wake(self) -> None: ...
    def periodic(self, interval: float, build: Callable[[], Outgoing | None]) -> None: ...
    def driving(self) -> AbstractAsyncContextManager[None]: ...
    def handling(self) -> AbstractContextManager[None]: ...
    def set_deadline(self, at: float | None) -> None: ...

class SessionCallbacks[Incoming: Message](Protocol):
    def bind(self, routes: Dispatcher[Incoming]) -> None: ...

class Gateway[Incoming: Message, Outgoing: Message]:
    async def serve(self, peer_id: str, session: Session[Incoming, Outgoing],
                    callbacks: SessionCallbacks[Incoming], *, replace: bool) -> None: ...
    def wake_all(self) -> None: ...
```

`serve` 从进入时拥有候选 Session 清理责任；接入器在移交前失败仍自行关闭。`register/get/unregister/
seal/close` 的现有语义不变。`wake_all` 只唤醒当前已发布、未关闭的 Session，不建立第二张业务唤醒表。
外部调用只用 `get` 选一次 Session 后直接 send/request；不增加会重查后继连接的 send(peer) 包装。

**Dispatcher 由 Gateway 在每次 serve 内创建并冻结。** 回调对象实现一个 `bind` 方法，登记绑定方法。
现有 typed `isinstance` 窄化和有限 Flow fallback 保留；未知 wire 方法不反射成 Python 方法。[S7]

```python
class Dispatcher[Incoming: Message]:
    def register[Item: ToolCall | ToolResult](
        self, kind: str, method: str, model: type[Item],
        callback: Callable[[Item, Incoming], Awaitable[None]],
    ) -> None: ...
    def configure(
        self, *, opened: Callable[[], Awaitable[None]],
        frame: Callable[[Incoming], Awaitable[None]],
        published: Callable[[], None] | None = None,
        poll: Callable[[], Awaitable[float | None]] | None = None,
        before_item: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None,
        frame_done: Callable[[], None] | None = None,
        failed: Callable[[Exception], Awaitable[int]] | None = None,
        closed: Callable[[], None] | None = None,
    ) -> None: ...
    def freeze(self) -> None: ...
    async def dispatch(self, item: ToolCall | ToolResult, frame: Incoming) -> None: ...
```

生命周期槽名称、类型和作用即上面的 configure 参数；Dispatcher 保存这八个字段、
`_callbacks: dict[tuple[str, str], Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]]]`、
`_configured: bool`、`_frozen: bool`。configure 只允许一次，freeze 要求配置完整，随后禁止变更或重复注册。
保留 `frame_done` 以直接表达既有“整帧成功处理后续接收时限”的位置，不引入额外 `_frame_seen` 状态。
无用钩子为 None；worker 不必实现 server 的 poll/gate/DB 方法。没有另一个 Hooks/Context 数据包装。

`Dispatcher` 不再以独立 Payload 泛型约束入参；严格帧解析和注册模型窄化继续限制合法类型。
这是内部 API 变更，不用 Any、cast、getattr 或 type-ignore 绕过泛型问题。

**角色注册表：**

| 回调对象 | `(type, method)` → 绑定方法（均为 `async (具体模型, 原帧) -> None`） |
| --- | --- |
| WorkerCallbacks | `call worker.execute → on_execute(Assignment, ServerMessage)`；`call worker.stop → on_stop(StopExecution, ServerMessage)`；`call worker.reject_recovery → on_reject(RecoveryRejected, ServerMessage)` |
| WorkerCallbacks | `result server.accept_result → on_accept_result(ResultAccepted, ServerMessage)`；`result server.accept_stop → on_accept_stop(StopAccepted, ServerMessage)`；`result * → on_tool(FlowToolResponse, ServerMessage)` |
| WorkerHandler | `call server.execution_started → on_started(Started, WorkerMessage)`；`call * → on_tool(FlowToolRequest, WorkerMessage)`；`result worker.execute → on_result(ExecuteResult, WorkerMessage)` |
| WorkerHandler | `call server.accept_result → on_accept_result(AcceptResult, WorkerMessage)`；`result worker.stop → on_stopped(StopResult, WorkerMessage)`；`call server.accept_stop → on_accept_stop(AcceptStop, WorkerMessage)` |

两端都有 `bind(routes: Dispatcher[本端Incoming]) -> None`、`async on_opened() -> None`、
`async on_frame(frame: 本端Incoming) -> None`。worker 另有 `on_closed() -> None` 更新健康状态，
以及 `async send_terminal(value: TerminalPayload) -> None`、`async send_tool(call: PendingToolCall)
-> None`、`heartbeat() -> WorkerMessage | None`，分别供 watch/periodic 调用。
server 另有 `on_published() -> None`、`async poll() -> float | None`、
`async before_item(item: ToolCall | ToolResult, frame: WorkerMessage) -> None`、
`on_frame_done() -> None`、`async on_failed(error: Exception) -> int`、`on_closed() -> None`。
它们逐一对应上述生命周期槽；业务内部 helper 复用原准备、恢复与证据校验，不变成公共框架接口。

表中 call/result 分别是 wire 的 `tool_call/tool_result`。history 的匹配、应答、busy builder 与
reply_key 仍由角色显式提供，挂载到 Session 的既有 Responder/request 机制；不能把 Flow 工具或 A/B
误注册为临时 query。worker 的 `history_responder(read: Callable[[HistoryQuery], HistoryPage])
-> Responder[WorkerMessage]` 放在 callbacks 模块，取代 Client 中的历史业务方法。

### 1.5 共同驱动与有界就绪机制

**Gateway 唯一驱动顺序：**

1. 接管候选清理；创建 Dispatcher，调用 callbacks.bind 并 freeze；登记 peer。
2. register 成功后不插入 await，调用可选同步 published。server 在此写在线 epoch；只有完成这一阶段
   才在退出时调用 closed。落选候选不能写 presence 或更改 worker 健康状态。
3. 在一个 `session.driving()` 硬截止作用域内，先 opened，再反复执行：
   - 可选 poll：推进调度、检查业务接收截止，返回下次等待的绝对 until；
   - `next_event(until=...)`；None 只导致再 poll；Ready 由当前驱动串行调用；
   - Delivery 先执行 frame（epoch/控制授权），再逐项旁路 history、before_item、typed dispatch；
     全部条目完成后调用 frame_done。不会把业务列表 gather，也不会一次只消费首项。
4. 驱动异常交可选 failed 映射；没有 failed 则原样抛出。取消不转成协议错误。
5. finally 撤回路由、close/join、按实例身份退休；即使 close 抛错，也执行已进入连接的 closed。
   关闭/断连记录的异常不重新进入 failed 或握手错误处理。

register 失败也必须清理候选；server 的既有 ConnectionError 分类可记录它，不能为落选候选写 DB 断连。
配置错误和未知异常直接传播。Gateway 不导入 FastAPI 或任一角色业务类型。

**Session 事件簿记（新增/改变字段）：**

| 字段 | 类型与用途 |
| --- | --- |
| `_inbox` | `deque[Delivery[Incoming]]`；替代原 tuple FIFO，保留原始帧，不再复制/改写 payload |
| `_watches` | `dict[str, asyncio.Task[Ready]]`；至多 4 个活动或待交付观察；当前只用 terminal/tool |
| `_done` | `deque[tuple[str, asyncio.Task[Ready]]]`；至多 4 个就绪项，unwatch 必须主动移除旧项 |
| `_woken`, `_last_was_delivery`, `_handling` | `bool`；合并唤醒、跨来源公平、禁止回调嵌套消费 |
| `_periodic` | `asyncio.Task[None] \| None`；至多一个通信周期发送器 |
| `_fault` | `Exception \| None`；周期 builder 的失败，唤醒共同驱动并传播 |
| `_deadline`, `_scope` | `float \| None`、`asyncio.Timeout \| None`；绝对硬截止和当前作用域 |
| `_driver` | `asyncio.Task[object] \| None`；仅身份校验，绝不 cancel/join，不表示任务所有权 |

沿用 reader、ACK、query、write、FIFO 字节/条目计数及既有上限。完整帧仍先预检容量，再登记 ACK
或执行旁路；保存原帧和未消费条目引用不另造一份 wire 数据。清理任务集合可显式联合
`Task[None] | Task[bool] | Task[Ready]`，不引入 Any。[S2]

**watch 的精确定义：**`source()` 是一次可取消的观察，不是实际执行 Task。
泛型局部函数 await 得到 `T` 后，返回捕获 `T` 的 `Ready` 闭包；闭包内才 await typed callback(T)。
done callback 只记录 Task 身份、检索异常并唤醒，绝不执行业务。普通活动观察的异常在共同驱动中传播。
消费时核验 `_watches[name] is task`，移除后才交付。unwatch 先使名字失效、清除对应 `_done` 项，
再 cancel/join；重复取消或重新 watch 同名不能交付旧闭包。不能只靠跳过旧项而留下无界 deque。
被撤销观察的结果丢弃、异常检索并按需要记录；它们不再代表活动事件。通信退出也检索全部观察异常。

`wait_terminal` 返回持久终态，`next_tool_call` 只是借队头；取消观察不会取消执行/工具 Future，
也不移除原队头。terminal 回调先拿到 typed 结果，再撤销 tool 观察，最后发 `[T,A]`。[S5]

**next_event 优先级：**显式 closed → 通信 fault → 读失败分支 → 等待已到期 → 正常事件。
读失败立即失败临时查询；该分支只顺序交付已缓冲 Delivery，排空后抛原读异常，保持现有错误分类，
不让 Ready/wake 延迟断连。硬截止仍可中断 drain。正常时网络 FIFO 内部保持顺序；首选 Delivery，
但连续 Delivery 之间给一个待就绪 Ready 机会，防止终态饿死。wake 合并且不能推迟绝对时限。
读失败时不再启动新的本地观察回调；保留的终态/工具由后继连接重新观察。

跨来源不存在全局时间顺序：若 terminal 在 StopExecution 被处理前先交付，可以先发 T；server
stopping 仍按原规则处理。**一旦 stop 回调进入并撤销观察，旧 terminal/tool 不能再交付。**
不在公共 Session 中识别 worker.stop、Job 或 Flow。

### 1.6 截止、关闭与错误合同

- `driving()` 仅登记调用者身份，建立唯一 timeout 作用域；覆盖 opened、poll、等待和所有串行回调。
  `set_deadline(at)` 只允许驱动 Task 调用，同步 reschedule，生效不必等回调返回。
- server 保留**StopExecution 实际写出成功后 +20 秒**的原起点；成功返回后立即 set_deadline，
  中间不加 await。后续慢回调或连续 wake 不延长截止；B 成功写出并退休 stop 后清除截止。
  本次不额外给 StopExecution 的写入等待增加新时限，这是保留的现有限制。[S4]
- poll 返回 idle 轮询或 active receive 的软截止；frame_done 按旧位置更新 receive_deadline。
  server 的 starting/stop gate 仍在正确的条目位置执行，不能被共同驱动跳过。
- periodic 的 build 只同步查看 lease 并构造 Heartbeat/返回 None；独立通信任务完成实际 send。
  发送仍受同一写锁，不能宣称可越过阻塞的 socket write。失败封闭通信；慢 stop/history 不占用该驱动。
- Session.close 取消/join 的仅是通信任务及观察任务；不 join Gateway.serve 调用者。
  旧业务回调挂在 apply 时，新连接只等旧通信 close，可以继续 latest-wins 登记；旧业务继续受原 DB fence。
  Gateway.close 保持通信级语义，不新增强制 ASGI 退出的业务 join 保证。[S1]、[S8]
- server failed 保留：ResultPending → 1013；断连/超时 → 原日志与 1000；协议违规/容量/校验错误 →
  尝试 ProtocolError、1008；未知异常继续抛。worker 不配置 failed，交拨号器既有退避路径。
  server transport.close **仅吞 WebSocketDisconnect**，RuntimeError、其他异常和取消继续传播。[S4]、[S9]
- `handling()` 使回调内 next_event/再次 driving 立即报错；删除 receive 后没有旧入口可绕过。
  request 仍只用于显式独立 history，不能用它等 Flow 工具或接纳回复。

### 1.7 Runtime 装配与三个业务例子

**拟定角色构造与入口签名：**

```python
WorkerCallbacks(*, worker_id: str, session: Session[ServerMessage, WorkerMessage],
                execution: WorkerExecution, on_ready: Callable[[bool], None])
WorkerHandler(coordinator: JobCoordinator, *, worker_id: str, register: Register,
              session: Session[WorkerMessage, ServerMessage], database: Database,
              tools: FlowToolService, results: ResultApplication,
              notify_jobs: Callable[[], None], heartbeat_interval: float = 15.0,
              heartbeat_timeout: float = 45.0, poll_interval: float = 2.0,
              metrics: Metrics | None = None)
WorkerClient(*, worker_id: str, worker_token: str, serve_ws_url: str,
             serve: Callable[[Transport], Awaitable[None]],
             reconnect_min_backoff: float = 1.0, reconnect_max_backoff: float = 30.0,
             reconnect_jitter: float = 0.2)
async def WorkerRuntime._serve_connection(self, transport: Transport) -> None: ...
def WorkerRuntime._set_connected(self, connected: bool) -> None: ...
async def ServerRuntime.serve_worker_session(self, worker_id: str, register: Register,
                     session: Session[WorkerMessage, ServerMessage]) -> None: ...
async def serve_worker(socket: WorkerSocket, *, worker_tokens: Mapping[str, str],
    serve: Callable[[str, Register, Session[WorkerMessage, ServerMessage]], Awaitable[None]],
) -> None: ...
```

WorkerRuntime 增加 `gateway: Gateway[ServerMessage, WorkerMessage]`、`_connected: bool`，保留
`execution`、状态/历史依赖；Client 只得到 serve 回调、拨号认证和退避参数，不导入业务 owner。
Runtime 组合方法创建 Session（含 history responder）和 WorkerCallbacks，再调用 gateway.serve。
合法 Registered 才 on_ready(True)；已进入连接退出后 on_ready(False)。拒绝的重叠候选不调用这些钩子，
不覆盖活跃连接健康状态。停机先 seal/stop/join 通信，再 execution.shutdown，最后释放 root lock。[S6]

ServerRuntime 保留现有 gateway、coordinator/results/database 等依赖；把 tools 保留为组合依赖，
移除进程级 worker_handler。每次 serve_worker_session 构造一个 WorkerHandler，调用 gateway.serve。
ControlService、WebhookConsumer 和 Runtime 自身的 jobs 通知都直接用同一 `gateway.wake_all`；
ConversationBroker 仍使用同一 Gateway。API 的 WebSocket 入口注入 Runtime 的 serve_worker_session。
两端组合方法在回调构造/移交前失败时关闭候选；一旦进入 gateway.serve，清理只由 Gateway 负责。

**例 1：首次执行到终态。** server poll 领取/准备/授权后发送 Assignment；worker execute 回调
record_received → await send(Started) → 成功后同步 execution.start（record_started/launch）→ 注册
terminal/tool 观察并返回。工具结果由后续 Delivery 驱动原 Future；terminal Ready 发送 `[T,A]`。
server 先记录 T、apply，再校验 A，只有已提交才实际写 ResultAccepted 并释放当前 assignment。

**例 2：重连、Register+A。** server 原始首帧在创建 reader 前严格验证；该 Register 只交给 opened
恢复路径一次，绝不再经普通 payload 循环。已提交且实际携带 A 才回复；没有 A 不编造；未提交走
Registered(replay)，不在注册路径 apply。第二个 Register 是违规。worker 的拒绝恢复仍可先于
Registered，并按原 Register ID 校验；真实 stop 或已提交 A 回复可提前按各自凭据绑定 epoch。
新连接重建回调状态，不继承旧 pending query。

**例 3：停止与替换。** server 发现 stop 后发送 StopExecution 并设置硬截止；worker 撤销执行观察，
等待原 owner 的 shielded stop，之后发送 `[R,B]`。server 对 R 校验真实 receipt/身份及完整 confirm，
B 再核对 receipt 并 confirm 后才能接纳。停止阶段 Flow tool 不创建新效果，A 不替代 B。
中途断线只丢通信观察；真实 stop 继续，由重连上报同一证据。[S10]

所有回复固定原 Session；Gateway 的新路由不改变正在执行回调的回复目标。旧 finally 只退休自己，
presence/派工仍使用 SQL epoch fence，不能清理新 epoch 的在线状态。[S1]、[S8]

### 1.8 替代方案、代价与取舍

| 方案 | 取舍 |
| --- | --- |
| 只抽共同 dispatch 函数，保留两个 wait 循环 | 无法达到统一事件驱动目标，拒绝 |
| 把 WorkerClient 整体改名/搬文件 | 保留了错误的职责分配，拒绝 |
| 每个业务条目或本地结果进入新的通用队列 | 需要重复容量/关联/状态管理；采用原帧 FIFO + 有界 Ready |
| 给角色增加执行状态槽，再用无数据 signal 通知 | 需分别处理 observer 结果、异常和取消；采用泛型 Ready 闭包保存一次观察结果 |
| 所有回调并发执行，或把 heartbeat 也塞入串行业务队列 | 前者破坏协议顺序，后者阻塞心跳；保留串行业务与有限通信旁路 |
| 统一执行 Task 与 Session 生命周期 | 断线会影响实际执行/恢复，拒绝 |
| 再建 ServerCallbacks/ConnectedWorker 包装既有 Handler | 本方案直接改造 Handler，每连接一个有实际业务的回调对象 |

代价包括：Session 增加观察/截止等通信簿记；Dispatcher 扩大到可选生命周期槽；内部 receive 和
Client API 迁移；跨来源公平策略必须有可复现的竞态检查。收益应通过删除两套驱动和重复任务清理来
体现，不能只增加抽象。FIFO/结果提交/权限/持久兼容的任何回归都不以“重构”名义接受。

不在本次扩大历史读取线程预算，也不增加数据库/外部效果事务范围。旧 journal/inbox decoder 继续
严格、只读、惰性转换，保留原 digest/tool ledger；不因事件化重写存量记录。[S11]

### 1.9 批准后的实施顺序与删除清单

1. 在同一 bookmark/PR 定义 Delivery、typed Ready、Session 单事件入口、观察/时限/关闭，以及
   Dispatcher 的原帧与生命周期注册。先验证共同机制，再接两端；中间兼容入口只允许迁移期存在。
2. Gateway 接入唯一 serve 驱动、wake_all 和回调生命周期；保留原登记/替换算法。
3. worker 新增 `app/worker/transport/callbacks.py`，把真实业务转换为绑定方法；修改 Runtime/Client
   和健康状态接线。删除 Client._run_connection、run_connection、私有 Gateway、执行/历史依赖和 heartbeat 循环。
4. server 在现 `app/controler/workers/handler.py` 中将 WorkerHandler 改为每连接业务回调；修改
   transport.py、runtime.py、api.py、service/webhook 通知接线。删除 handle、_wait_for_message、_wakes、
   旧 notify_jobs 转发和 Handler 对 Gateway 的依赖；helper 留在它实际服务的业务类，不再加中间层。
5. 同一变更中删 Session.receive 及全部旧调用；更新既有测试接入方式，复用黑盒断言。同步当前架构文档
   components/overview/conversation-api 与 diagram；前序 feature 记录保持原样，本记录说明新取舍。
6. 跑下述验证与消融，按结果简化，再过项目 code-bearing review gate；继续使用本 Draft PR，不能另开实现 PR。

只做内部迁移，不需要 wire/DB rollout。双端可按现有部署方式升级；回退是回退同一内部重构，
不转换存量数据。本阶段不执行任何部署。上一线程的未推送测试精简不 cherry-pick、不自动并入。

### 1.10 验证与消融计划

以下均为**批准后拟执行**的验收；旧 PR 的 CI、旧 workspace 的全量测试不能替代它。
优先迁移已有公开入口用例；只有新的公共事件/取消合同缺少覆盖时才保留新回归，不为每个钩子各造测试。

| 验收 | 入口与必须观察的结果 |
| --- | --- |
| 真正统一 | 两端真实 runtime/transport 都进入同一个 Gateway.serve；角色没有 receive/asyncio.wait 消费循环；Client 不依赖业务 owner |
| 控制与列表顺序 | 无效实际首帧拒绝；混合控制先授权，history 不绕过；独立 history 不被 starting gate 拦；同帧 T/A、R/B 尾项不丢 |
| 临时查询/容量 | 固定原 Session；替换即失败不重放；坏 epoch/reply_to、共享头字节、4 pending/handler/busy 与 32 帧/条目/ACK、16 MiB 约束 |
| 执行因果顺序 | Started 的写失败、取消、journal 失败均不启动；恢复途中离线完成不重执行；工具保留原 Future/ID，取消后借用/写锁窗口有 guard |
| 结果/停止证据 | A 只在提交后；Register+A 三场景；R 必须真实、B 须 confirm；stopping 无新 Flow effects；迟到 A 不退休 stop |
| 新就绪合同 | 同名观察重挂无旧事件；连续 unwatch/rearm 的队列有界；控制 stop 与 terminal 同时就绪；活跃 observer 异常传播；nested next_event 拒绝 |
| 关闭与恢复 | A→B→C latest-wins；旧回调挂起不 self-join；旧 finally/DB 不影响新 epoch；重复取消/close 错误仍完成清理；断线不取消执行和 shielded stop |
| 时限与唤醒 | 持续 wake 不延迟 stop 绝对截止；截止在回调内部 await 中也生效；慢 stop/history 时心跳任务仍运行，lease 取消后不伪造心跳 |
| reader 失败 | 临时查询立即失败；已缓冲业务按序 drain 后抛原异常；Ready 不饿死断连；显式 close 丢旧 FIFO |
| 持久格式 | 协议/receipts/journal/恢复/工具账本现有合同仍通过；不新增迁移写入 |

主要已有测试：`test_communication_{session,gateway}.py`、`test_worker_{client,handler,runtime,session}.py`、
`test_conversation_proxy.py`、`test_stop_protocol.py`、`test_cancel_drain.py`、协议/receipts/recovery 测试。
定向后跑真实 PostgreSQL 全量、Ruff、Mypy；按 CI Python 3.12 环境检查并构建 console 与发行包，
执行两个 worker + server 重启 smoke。文档架构图若修改，需实际渲染并检查。[S12]

**消融（实现后、review gate 前，以临时变体执行，不提交故障代码）：**

- 去掉共用驱动，保留角色私有循环：以真实入口调用轨迹和源码所有权检查证明是否仍有重复驱动。
- 去掉控制前置/逐项处理：使用混合 Registered/history、Heartbeat/history、T/A、R/B，观察未授权旁路或丢尾项。
- 去掉 watch identity 和 unwatch 主动清队列：重复完成/撤销/重挂并替换连接，检查旧 Ready、队列增长和错误 Task 泄漏。
- 把 heartbeat 改为串行事件，或把硬截止仅放在 next_event 等待：门控慢 stop/回调，检查通信进度与实际超时点。
- 去掉公平让步：连续网络帧期间观察终态最终能否交付；用屏障构造 stop/terminal 的两种先后并确认停止后无旧 T。
- 将 Started 后启动改为排队 sent 回调：在实际写成功与 sent 消费之间插入 stop/关闭，检查启动因果顺序。
  这是拟定强制交错实验，现有 send/cancel/journal 断言不保证捕获每个排队实现；不预报未执行结果。

各对照保持相同输入、屏障和限额，记录通过/失败及精简结论；若较简单版本同样满足合同则选它。

### 1.11 审批事项、未知与实际状态

**请求批准的整体取舍：**

1. Gateway 共同驱动/路由/生命周期，Session 交付 Delivery/Ready/wake；两端只挂业务回调。
2. worker Runtime 直接持有 Gateway；Client 限拨号；server WorkerHandler 改为每连接对象，不加包装层。
3. 连接局部状态在回调中，真实执行与持久证据不迁移；帧内串行、history/心跳有界旁路。
4. 类型安全一次观察闭包、原 Session 绑定、跨来源公平；读失败放弃本地 Ready、保留业务 FIFO drain。
5. 删除旧内部 API；wire/持久格式不变；stop 时限保留原实际发送后起算，采用同步更新的整个驱动截止。

没有需要先改代码才能回答的审批阻塞。具体私有方法拆分、泛型注解在项目 mypy 下的表达、最小测试
归组可在实施中核定；不得借此退回 Any/cast、角色私有循环或新增业务容器。若公共合同需要改变，
先在本记录追加修订并再获批准。

**方案形成证据：**本线程已 fetch 并核对上述 main，在独立 `session-event-architecture` jj workspace
规划；检查现有记录/bookmark/开放 PR，未发现本 feature 的已有记录。源 workspace 的未推送变更未导入。
按 consulting-claude-oracle 通过受限本地 CLI 生成初稿与核心修订；两次均核验初始化及响应为
`claude-opus-5-5`、tools/MCP 为空、退出成功。主线程核对代码后采用直接 Runtime 装配、每连接 Handler、
有界队列清除，并保留原 deadline 起点与 post-frame 时机。顾问建议不是执行验证。

本阶段只运行已有通信/角色定向基线；PostgreSQL 项因环境未配置跳过。没有新架构实现、全量验收、
review gate 或部署结果。详细本地基线结果见[当前工作线程](https://ampcode.com/threads/T-01a1a4ee-b7a2-750f-ba8f-f57b37f928c1)。

## 2. PR change timeline

<!-- 初始计划共享后，按发生顺序追加实质需求/方案/实现变化；不记录纯审批或阶段切换。 -->

### 001 去掉 Delivery，直接交付 msg

**日期与状态：**2026-11-04；用户在[本线程](https://ampcode.com/threads/T-01a1a4ee-b7a2-750f-ba8f-f57b37f928c1)
明确要求“去掉”。此决定仅批准删除方案中的 Delivery 包装；整体架构与实现仍待批准。

**原因与取舍：**初稿把一次 msg 交付包装为 `Delivery(frame, items, size)`，重复了现有消息及其
payload 的表达。当前 Session 已用私有 `(message, size)` 记账，出队只返回 message；独立查询旁路
使用 `model_copy(update={"payload": remaining})` 表示剩余条目，临时回复 Future 在复制前拿到原 msg。
Gateway 没有需要同时读取完整原 payload、剩余 items 和 size 的已知职责。[S2]
按 consulting-claude-oracle 的受限本地 Opus 5.5 咨询并核对上述源码后，采用直接交付 msg 的方案，
不保留 Delivery 作为别名、可选路径或另一个包装。

**替代后的当前合同：**

- 本记录中的“网络帧/原帧”指已经解析的应用层协议消息 msg，即既有 `ServerMessage` 或
  `WorkerMessage`；没有独立的网络帧模型，也不指 TCP 包或 WebSocket 分片。
- Session 的网络输入直接返回 `Incoming`。`Ready` 和 `None` 的含义、watch 生命周期及有界策略不变。
  `frame`/`on_frame` 等初稿钩子名中的参数也就是这个 msg，不代表额外对象。

```python
class Session[Incoming: Message, Outgoing: Message]:
    async def next_event(self, *, until: float | None = None) -> Incoming | Ready | None: ...
```

- `_inbox` 保持 `deque[tuple[Incoming, int]]`。入队记录完整原 msg 的实际编码字节数，出队扣减后
  仅交付 msg；不向 Gateway 或业务回调暴露 size。条目计数以实际入队的 `msg.payload` 为准。
  无 control 的 msg 可先旁路独立查询/回复，再按现有方式复制为只含剩余 payload 的同类型 msg；
  保留 epoch、control 等头部字段。临时查询 Future 仍拿到对应条目与原 msg，不重新交付已旁路条目。
- Gateway 对 msg 先执行控制/epoch 钩子，再按 `msg.payload` 顺序执行旁路、before_item 与 typed
  dispatch，全部成功后调用 frame_done。带 control 的 msg 不在 reader 中旁路，必须先获控制授权；
  完整 msg 的容量预检仍先于 ACK 登记和任何旁路副作用。[S2]
- `_last_was_delivery` 改称 `_last_was_message`，只记录公平轮转。正常时保留 msg FIFO，连续 msg
  之间给已就绪 Ready 一次机会；reader 失败时只顺序排空已缓冲 msg，再抛原异常。其余优先级、
  有界计数、单消费者和禁止嵌套消费合同不变。
- 工具回复直接从后续 msg 路由到原 Future。回复仍固定原 Session；控制授权、T/A、R/B、Started
  写后启动、stopping gate、latest-wins 和断线不取消执行的安全合同不变。

**明确覆盖的初稿内容：**

| 初稿位置 | 本修订替代内容 |
| --- | --- |
| §1.2 事件表、§1.4 公共接口 | 网络来源为 msg；删除 Delivery 定义；`next_event` 返回 `Incoming \| Ready \| None` |
| §1.5 驱动、字段、容量与公平/drain | 消费 `msg.payload`；保持私有 tuple FIFO 和剩余 payload 复制；使用 `_last_was_message` |
| §1.7 业务例子、§1.8 FIFO 取舍 | 工具回复由 msg 驱动；采用现有 msg FIFO 加有界 Ready，无原始消息包装 |
| §1.9 实施第一步 | 直接定义 typed Ready 与 Session 单事件入口，随后按原顺序迁移；不定义 Delivery |
| §1.11 审批第一项 | Gateway 统一驱动 msg/Ready/wake；删除包装已确定，其余整体取舍仍待批准 |

**验收与实际状态：**只修订本 feature record、同一 Draft PR 的描述和审批摘要，没有实现或测试变更。
批准后的验收继续使用 §1.10，并检查部分旁路后剩余 payload 的顺序、原 msg 回复关联、原始字节计数、
控制前置、读失败 FIFO drain 及两端真实入口直接消费 msg；优先复用现有公开入口测试。
本次文档与源码核对不代表新架构已经通过运行验证。

### 002 两端业务回调对称命名

**日期与要求：**2026-11-04；用户在[本线程](https://ampcode.com/threads/T-01a1a4ee-b7a2-750f-ba8f-f57b37f928c1)
要求“worker和server的特有相关逻辑命名对称一点”。据此统一业务回调类及模块的命名；整体实现仍待批准。

**命名决定：**类名前缀表示业务逻辑所在端，两端使用同一个 `Callbacks` 后缀。

| 所在端 | 业务回调类 | 模块 | 迁移来源 |
| --- | --- | --- | --- |
| worker | `WorkerCallbacks` | `app/worker/transport/callbacks.py` | 从 WorkerClient 中提取真实业务逻辑 |
| server | `ServerCallbacks` | `app/controler/workers/callbacks.py` | 将现有 `handler.py` / `WorkerHandler` 直接改造并重命名 |

两端 Runtime 每条连接均创建局部 `callbacks` 对象并传给 `Gateway.serve`，通过同一 `SessionCallbacks`
接口的 `bind` 注册业务。同义生命周期方法继续共用 `on_opened`、`on_frame`、`on_closed` 等名称；
执行、调度、结果接纳等角色特有方法按真实职责命名，不为凑对称增加空方法或改动 wire 方法名。

**边界与替代范围：**

- `ServerCallbacks` 直接承载现有 server 业务及连接局部状态，不持有或转发到另一个 WorkerHandler。
  实施完成后删除旧类、旧模块及其导入，不保留兼容别名。现有测试随真实入口迁移更新引用。
- 本修订覆盖 §1.3 的 server 对象/字段表、§1.4 的注册表、§1.7 的构造与装配、§1.9 的 server
  迁移路径和 §1.11 的角色命名；这些位置拟定的 WorkerHandler 统一改用 ServerCallbacks。
  §1.3/§1.8 对新增包装层的拒绝继续生效，不限制直接改造后的业务类使用 ServerCallbacks 这个名字。
- 目录沿用现有项目布局；worker 拨号器 `WorkerClient` 和 server 接入函数 `serve_worker` 继续表达
  实际不同的接入职责，不增加对应的空壳类型。共享 Gateway/Session、回调状态所有权与安全合同不变。
- [修订 001](#001-去掉-delivery直接交付-msg) 的直接 msg 交付继续生效。本次只更新方案、PR 描述与
  审批摘要，未重命名代码文件或修改测试；实现仍须获得整体方案批准。

### 003 统一包名.方法名路由与注册

**日期与要求：**2026-11-04；用户在[本线程](https://ampcode.com/threads/T-01a1a4ee-b7a2-750f-ba8f-f57b37f928c1)
要求“统一一下路由规则，暂时先按包名.方法名 路由，要求各回调处理对象按照相同格式注册回调”。
以下将“包名”解释为协议命名空间，如 `worker`、`server`、`flow`，不解释为 Python 模块路径。
这是本阶段的注册约定；本条修订不代表整体实现已获批准，也不引入此前仅讨论可行性的 pluggy 接入。

**统一规则：**

1. 所有可注册的业务路由都是完整的 `package.method` 字符串，两段均使用小写字母、数字、下划线，
   首字符为小写字母，恰好一个点。注册时不补前缀、不改大小写、不 trim；拒绝 `*`、`flow.*`、空段和多级路径。
   该格式约束注册名称；不收窄既有线上解析器对未知方法的类型化错误处理。[S14]、[S16]
2. 每条连接的入站表只按 `item.method` 精确查找。`type` 继续由具体 model 校验，不再作为第二个路由键
   或单独的注册参数。先严格解析 msg，命中后再做 `isinstance(item, model)` 窄化；类型不匹配立即报错，不回退。
3. 每个回调处理对象都在同一个 `bind(routes)` 入口，使用同一种 `register(route, model, callback)` 格式。
   同一表内同名注册即报错，即使 model 不同也不能覆盖。Gateway 在连接准入前完成 bind/freeze，运行中禁止增删。
4. 命名空间表示协议操作，不由回调类名推导；回复沿用请求的 method。两端表彼此独立，因此
   `ServerCallbacks` 可以注册 `worker.execute` 的结果处理，`WorkerCallbacks` 可以注册
   `server.accept_result` 的回复处理。不得改成隐含的“当前端前缀”。[S3]、[S4]、[S14]
5. 不根据路由名 import 模块、反射 Python 方法或自动扫描业务类。路由只指向显式登记的绑定方法。

**替代后的消息注册接口与示例：**

```python
class Dispatcher[Incoming: Message]:
    def register[Item: ToolCall | ToolResult](
        self, route: str, model: type[Item],
        callback: Callable[[Item, Incoming], Awaitable[None]],
    ) -> None: ...

# WorkerCallbacks.bind(routes)
routes.register("worker.execute", Assignment, self.on_execute)
routes.register("server.accept_result", ResultAccepted, self.on_accept_result)
routes.register("flow.open_pr", FlowToolResponse, self.on_tool)

# ServerCallbacks.bind(routes)
routes.register("worker.execute", ExecuteResult, self.on_result)
routes.register("server.accept_result", AcceptResult, self.on_accept_result)
routes.register("flow.open_pr", FlowToolRequest, self.on_tool)
```

初稿 §1.4 角色表中的其余精确方法保持原名，统一删除注册参数中的 call/result；它们仍由对应模型限制。
原来两端 `*` 的正常工具处理改为显式注册 `flow.open_pr`，这是当前唯一有实现的 Flow 工具。[S15]
`_callbacks` 改为 `dict[str, Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]]]`；
固定 method Literal 与 route 的一致性校验、重复拒绝、冻结和类型窄化继续保留，不增加第二份路由目录。

**未命中处理：只保留现有拒绝/回复关联合同，不作为成功路由。**

- 精确查找未命中时，交给固定的 `unrouted` 策略钩子；它不接收注册用的通配字符串。
  `configure` 增加可选参数
  `unrouted: Callable[[ToolCall | ToolResult, Incoming], Awaitable[None]] | None = None`，
  两端均绑定 `async on_unrouted(item, msg)`；缺省直接抛未注册错误。该字段与其他固定钩子一起冻结。
- server 只允许已通过严格解析的 `FlowToolRequest` 进入原 assignment/Flow binding 校验与 stopping gate，
  仅获准处理时在原 Session 返回关联的 `INVALID_REQUEST`；stopping 时保持原停止分支，不发送工具回复。
  不得经此调用工具执行入口或产生新 Flow effects。
  即使漏注册的是 `flow.open_pr`，也只能报错，不能靠未命中钩子补执行；不另建“已知方法集合”来掩盖漏注册。
- worker 只允许 `FlowToolResponse` 进入原 pending 工具回复校验，按 `(method, reply_to, call_id)`
  完成原 Future；没有 pending 或身份不匹配继续报错，不创建新 Future，不改变执行所有权。[S5]
- 其他未命中类型直接报错。`flow.future_tool`、`worker.unknown`、`custom` 等既有未知输入仍走上述
  类型化拒绝/关联路径，不因此自动注册为业务方法。已知方法的畸形 payload 仍在解析时拒绝。[S14]、[S16]、[S17]

**范围与安全边界：**本条统一带 method 的消息条目路由。生命周期固定钩子、typed Ready、wake 和
截止没有 wire method，继续按原类型合同调用；不为它们制造假 RPC 或新事件包装。`worker.history`
沿用相同的协议命名格式，但仍在 Session 的独立有界查询/回复路径处理，不挪入串行业务队列。
混合控制/payload 必须先授权，逐项 before_item 在精确分派或 unrouted 之前执行；回复不重查 Gateway
最新连接。其余执行、提交、停止证据、同步发布、关闭与持久格式合同不变。

**替代位置与验收：**本条替代 §1.4 的 `(type, method)` 键、register 签名、Flow `*` 注册与回退，
并为 configure/字段表增加上述 unrouted；§1.5 的消息分派、§1.9 的路由迁移、修订 002 的 bind
统一采用此合同。修订 001/002 其余决定继续有效。批准后复用公开入口用例检查两端注册覆盖、重复/非法
名称、同名请求与回复的方向隔离、模型不匹配不回退、冻结、漏注册不得产生工具效果、未知工具关联错误
及原 Future 完成；继续执行 §1.10 全部安全验收，不为每个注册辅助函数增加永久测试。

**实际状态：**受限本地 Claude Opus 5.5 草拟后已对照源码核对；采用精确注册与固定未命中策略，
不增加独立的已知方法目录。只追加方案并运行既有协议/未知工具基线及注册名审计，没有新增
实现或测试，也没有把这些基线结果当作新路由实现验收。

### 004 用 tool_result 返回消息验证错误

**日期与要求：**2026-11-04；用户先要求“改成返回消息验证错误的payload”，随后明确
“你返回一个toolresult的payload，内容是error”。采用后一条指定的形态：普通 `tool_result`
携带 `error`，沿用原方法及逐项关联。不采用额外 `message.validation_error` 方法、通知型 tool_call，
也不把错误包装成对整个外层 msg 的另一种消息。整体架构仍待批准，本次不改实现或测试。

**错误 payload 合同：**

```json
{
  "type": "tool_result",
  "message_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "reply_to": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "method": "worker.execute",
  "error": {
    "code": "validation_error",
    "message": "message payload failed validation",
    "details": [
      {"path": ["payload", 0, "arguments", "job_id"], "code": "missing"}
    ],
    "truncated": false
  }
}
```

- 这是 `msg.payload` 中的一项。`message_id` 为新 ID，`reply_to` 为被拒 payload 项的 ID，
  `method` 原样沿用该项的方法。拒绝一条正常回复时，关联的是该回复的 ID，不能误用它的 `reply_to`
  去结束另一笔请求。外层 msg 仍由当前 Session 构造，使用同一 epoch、`control=None`，不重查最新连接。
- 以一个共享的 `ValidationErrorResult(ToolResult)` 模型表达这个失败分支，wire 的 `type` 仍只有
  `tool_result`。不伪造缺失的 job/token/call_id，不附加成功字段，不调用 `terminal()`，不把错误本身
  写入 journal/inbox；它不是执行结果 T、结果接纳 A、停止证据 R 或停止接纳 B。[S14]
- `error.code` 和 `message` 为固定值。`details` 至多 8 条，只含规范化的字段路径和封闭错误码
  `missing` / `unexpected_field` / `wrong_type` / `invalid_value`；路径仅保留 schema 声明的字段名
  及有界索引，远端自定义 key 被替换。不直接序列化 Pydantic 的 loc、msg、input、ctx 或异常文本，
  不回显参数、token、secret 或工具内容。单个 error 对象编码上限 4 KiB，裁剪后置 `truncated=true`。
- 原有正常结果模型与业务错误合同保留。`tool_result` 的 `error` 为对象且 code 是 `validation_error`
  时选择共享失败模型，否则继续使用原有结果模型；共享分支不通过时直接拒绝，不能尝试另一个业务模型。
  现有 ConversationError 是字符串 Literal，与此对象分支不冲突。[S14]、[S18]

**路由与回调仍统一：**

```python
def register[Item: ToolCall | ToolResult](
    self, route: str, model: type[Item],
    callback: Callable[[Item | ValidationErrorResult, Incoming], Awaitable[None]],
) -> None: ...
```

只按 `item.method` 查一次。普通输入必须匹配注册的 model；已通过共享模型校验的错误进入同一个
回调的失败分支。两端回调一致使用这个签名，先辨别错误，再读正常参数；不增加 kind 参数、第二张
错误路由表、同名双重注册或 `*`。例如 server 的 `worker.execute` 回调接收正常 ExecuteResult，
也接收对原 Assignment 的校验拒绝；worker 的同名回调接收 Assignment，也处理其 ExecuteResult
被拒的情况，必须用本地保存的原项 ID 区分。[修订 003](#003-统一包名方法名路由与注册) 的精确路由不变。

原先单向操作的反向失败入口显式注册：例如 worker 的 `server.execution_started`、server 的
`worker.reject_recovery` 使用 `ValidationErrorResult` 作为 model。history 错误继续由其有界
查询/回复路径处理。未命中的共享错误只允许通过既有 unrouted 策略失败一个精确匹配的原工具 Future，
或作限速诊断；不得执行工具或借此补齐漏注册的正常方法。

**可恢复范围与整条 msg 的处理：**

1. 正常输入仍完整严格解析。仅当已认证并完成握手、外层头和每项关联头均合法、容量符合限制、
   epoch 与控制 owner 校验通过，而 payload 的具体 schema 失败时，才返回上述错误并继续连接。
   关联头包括 type、method、message_id、reply_to；重复项 ID、无法解析 JSON、坏头、错误 epoch、
   握手/授权/容量错误仍是连接级故障，不能捏造一个 ID 回错。注册配置或回调程序错误不转成远端参数错误。
2. 保留整条 msg 校验失败时的零业务效果：所有 payload 均不执行，包括本身合法的兄弟项。
   为各被拒项返回一条关联的错误，按原顺序合并为一个有界回复批；details 指明实际失败项的位置，
   合法兄弟项也收到整条 msg 被拒的结果，避免调用方悬挂。最多沿用 32 项上限。[S2]、[S19]
3. 混合 control/payload 的拒收先经过角色的只校验、不提交状态的控制检查。不能因 payload 非法
   而先续租、绑定 epoch、登记 ACK 或调用正常 control handler；整个坏 msg 不续接收/停止期限。
   控制语义自身非法时保留协议错误，不借参数错误路径绕过 starting/stopping 或 Flow 权限。
4. 协议层产出只含已验证关联头与脱敏诊断的类型化拒收信息；reader 不调用业务回调。
   普通业务拒收以现有 `Ready` 闭包占据该输入在网络 FIFO 的位置，私有队列可存 `Incoming | Ready`
   并按原输入字节/项数计费，仍返回 `Incoming | Ready | None`，不引入 Delivery。
   这种拒收与本地 observer Ready 分开记账：前者随网络 FIFO 排空，后者仍在读失败时放弃，不能把拒收
   丢进公平 observer 队列而乱序或在 EOF 时遗失。history-only 的错误返回及查询失败沿用独立有界路径；
   混合 control/history 仍先经控制 owner。发送错误使用原 Session 写锁，失败仍封闭通信。
5. 不对 error 再回 error。错误 payload 本身畸形，或包含错误项的整条 msg 校验失败，按连接级故障
   处理，不发另一组校验错误；合法错误的重复、过期或未知关联仅作限速诊断，不能完成其他请求。

**发送方关联与业务收尾：**

关联只使用本连接的 epoch、精确 method 和 `reply_to`，再匹配本地原始对象。错误里没有业务授权。
沿用当前 Assignment、expected_result/expected_stop、pending_tool 的原项 ID；对于 Started 和已发
结果，只保存本次业务需要的有限 ID，随对应状态退休或连接退出清理。记录先于实际写入预约，写失败撤销；
共享错误不能被早到的响应误判成未知。history 的 4 个 pending 槽沿用原 call ID，不新增完整发送日志、
持久 outbox 或重放错误的机制。原工具的 call_id 从本地 PendingToolCall 获取，不从错误内容猜测。[S3]、[S5]

| 被拒内容 | 本次拟定的 owner 行为 |
| --- | --- |
| Assignment | worker 不 receive/record/start。server 验证匹配当前 starting 后，按下述围栏处理派发拒绝；不继续等 Started，不自动重发相同坏数据，普通参数拒绝不关闭连接 |
| Flow 工具请求 | WorkerExecution 完成原 Future 为不可自动重试的校验失败，恰好一次出队；不产生工具效果，不取消整个执行，连接继续 |
| history 请求或回复 | 原查询失败为既有 `invalid_response`，不结束其他查询/业务，不重放查询 |
| Flow 工具回复 | 拒收方也用已验证的 method/reply_to 失败本地原 Future；报告回复不可用、远端效果未知，不假定工具没有执行或自动重做 |
| Started 已实际写出后被拒，或 T/A、R/B、stop 被拒 | 不清理原证据、不造接纳/停止确认；结束本次连接的关键协议等待并走显式恢复关闭，不能把它归入普通参数拒绝后继续成功推进 |

同一拒收批若同时影响普通 Future 和关键状态，先完成匹配的普通失败，再执行恢复关闭；不能把已经
被拒的工具请求带到新连接重放。对收到坏回复的一端，仅向对方发 error 不够，还必须失败自己已关联的
查询/工具等待。若坏回复无法匹配任何原等待，按协议身份错误处理，不能猜测要失败哪个 Future。

**Assignment 的持久围栏与 stop 竞态：**

- 新增一个 server-owned 的预启动拒绝入口，在同一 workflow/job/worker 事务保护下核对 RUNNING、
  原 worker/token/当前 epoch/Flow binding、未 started、未接收或应用结果、有效租约、Flow ACTIVE、
  没有 pending stop、未被 delivery 状态取代。条件满足后用真实 token 和固定原因写既有
  `source=control` 结果，继续走 result application；只有提交完成且身份仍匹配才释放派发等待/slot。
  不把收到的 error 当作 worker 终态，也不能直接调用缺少这些围栏的 `coordinator.fail` 或裸
  `finish_assignment`。结果应用尚未提交时沿用 ResultPending 的保留与恢复路径。[S20]、[S21]
- stop 已竞争成功时，保留 slot、intent 与已有证据，不新增 Flow effects，不用校验错误调用
  StopStore.confirm。转交原停止/安全阻塞路径；不得在拒绝写入失败时简单清空 current。
  **已核实的限制：**worker 对被拒 Assignment 没有 journal；现有 `stop()` 会拒绝缺 journal 的停止，
  不能保证这一竞态会自动得到 R。此时只能保留 binding/intent 并按原安全故障处理，可能需要人工恢复；
  本修订不新造“从未执行”的停止凭据，也不宣称重连能修复证据缺失。[S10]、[S22]
- epoch、started 或结果竞争使围栏失效时，由原 owner 重新核对最新状态；不能覆盖成 FAILED、释放
  新连接 slot 或无限等待旧 Started。不增加任意次数断连、worker 隔离或新的跨重连计数策略。

**范围、实施与验收：**

新增通用错误结果改变 wire schema，明确把初稿“wire v16 不变”替换为 **wire v17，双端协调升级**；
不保留双版本 wire fallback。旧 journal/inbox 版本、只读惰性转换、digest、工具 ledger 与真实停止
证据不变，不能为了接受新 wire 错误而放宽持久解码器。[S11]、[S23]

批准后先在 protocol/Session/Dispatcher 定义错误结果、判别、拒收及同路由类型合同，再迁移两端
Callbacks、原工具 Future/history 失败路径和 server 预启动围栏；接入器保留致命协议错误分类。
这覆盖初稿 §1.1/§1.11 的 wire 范围、§1.4/修订 003 的回调入参、§1.5/修订 001 的私有网络 FIFO、
§1.6 的 ValidationError 一律断连与对应实现/验收步骤；其他架构和安全合同继续有效。

验收通过双端真实入口覆盖：坏 Assignment 收到同 method/reply_to 的 error 且不启动；同连接后续
合法调用；同批兄弟项零效果且均结束等待；history/工具坏请求与坏回复的 Future 恰好一次失败；同名
请求/结果共存和单向方法反向失败；旧 epoch、未知/重复 error 无业务作用；不回显秘密/任意 key；
错误批、FIFO 字节/项数有界，拒收与 EOF 的顺序；无 error 循环；混合控制拒收不续租/ACK；真实
PostgreSQL 下拒绝与 Started/result/stop/重连的围栏竞态，以及 T/A、R/B、持久证据不退化。

**实际状态：**受限本地 Opus 5.5 咨询后按用户最新 tool_result 要求修订，并对照源码剔除顾问草稿
中的额外错误通知路由、旧 kind/通配注册、部分提交 heartbeat、任意断连计数及假设存在的无 journal
停止能力。只改本记录、同一 Draft PR 与审批摘要，运行的是既有基线；未编写实现或测试，不把顾问
意见或基线通过当作新错误合同已实现。待整体审批时需一并确认上述 wire 升级和关键证据故障边界。

### 005 错误响应不暴露校验详情

**日期与要求：**2026-11-04；用户明确要求“不要加detail暴露更多内容”。据此删除修订 004
拟定的详情与裁剪字段。该修订是用户指定的响应收敛，不改变路由、关联、拒收范围或业务收尾；
整体方案仍待批准，未开始实现。

**当前唯一错误内容：**

```json
{
  "code": "validation_error",
  "message": "message payload failed validation"
}
```

- `tool_result.error` 严格只允许上述两个固定字段和值。删除 `details` 和 `truncated`，不保留
  可选字段、空数组、脱敏版、debug 开关或另一种名称的详情入口；带额外字段的错误不属于合法模型。
- 不返回字段路径、缺失字段名、失败项位置、子错误码、校验器文本、输入值、堆栈或其他诊断内容；
  固定 message 不拼接异常文本。本地诊断也不能自动序列化进 wire，不为本次修改新增原始消息日志。
- 整批拒收时，各项的 error 内容完全相同，只用原 method 和 reply_to 关联；正常的消息 ID 等
  协议头不变。共享拒收路径仅需已验证关联头和固定失败标记，不再收集、规范化或传递字段诊断列表。
- 本条替代修订 004 JSON 示例中的 error 对象、详情白名单/裁剪/4 KiB 诊断额度，以及“指出实际
  失败项位置”“脱敏诊断”的实现要求。修订 004 保留为历史记录，旧详情格式不再是可实现的备选。
  wire v17、同路由失败回调、整批零效果、Future 收尾和关键证据边界均不变。

**核对与后续验收：**本次只同步 feature record、同一 Draft PR 和审批摘要；核对新 JSON 恰好两个
固定字段及原始记录保留。批准后的协议验收应断言不同字段/不同原因的校验失败只返回同一固定 error，
无任何详情字段或输入泄露，并拒绝附加详情的错误模型；不把本次文档核对当作实现验证。

### 006 共享驱动、角色回调与错误围栏实现中

**日期：**2026-11-04。按批准的初稿及修订 001–005 实施，当前仍是本地未验收工作。

- `communication/gateway.py` 驱动统一生命周期与串行分派；`session.py` 交付原 msg 或 Ready，
  管理有界、按身份撤销的观察、通信心跳及覆盖回调 await 的硬截止。执行任务仍由 WorkerExecution 持有。
- 两端 Runtime 持有 Gateway，并为每条连接创建 `WorkerCallbacks` / `ServerCallbacks`。
  WorkerClient 只负责拨号与退避；旧 server handler 正在随测试入口迁移移除。
- `contracts/validation.py` 先严格解析，只有关联头和外层控制结构合法的 payload 失败进入整批拒收；
  回调沿原 method/reply_to 返回固定两字段 error。失败批中普通工具 Future 先收尾，再进入关键证据恢复。
- `WorkerStore.reject_assignment` 在 worker/workflow/job 锁及当前 epoch、租约、未 Started、无结果、
  未停止、有效 binding/dispatch 围栏内保存 control-source Failed；提交和释放仍走 ResultApplication。
  dispatch 时间继续直接读 SQL，不为这一路径扩展通用 Job 数据模型或更改持久格式。
- 测试迁移使用真实 Runtime 的连接入口与 Gateway/Session 驱动，仅在测试工厂注入原模拟依赖；
  不在生产代码保留 `run_connection`、旧 `receive` 或旧 Handler 兼容 API。

**阶段验证：**Session 就绪/公平/硬截止及已有 FIFO/ACK/query 场景已执行；worker/server 回调与
history 代理的既有回归已在新入口运行；真实 PostgreSQL 的 Assignment 拒绝围栏场景已执行。
尚需完成全部入口迁移、新验证错误合同的端到端覆盖、全量检查、消融实验及代码 review gate。
这些局部结果不表示新架构已完成验收；远端 CI 目前仅验证此前方案文档。

### 007 双端入口完成迁移与验证错误边界修正

**日期：**2026-11-04。承接 006，全部生产调用和测试已迁移至 Runtime 的连接入口。
移除旧 WorkerHandler、角色私有 receive/wait 循环及兼容入口；两端通过同一 Gateway 驱动，
业务回调均在 `bind` 中显式注册。README、组件说明、会话 API、概览和架构图同步到 wire17。
收尾补齐运维/部署文档：区分 v16→v17 仅 wire 合同升级与早期持久格式迁移；双端须协调，
不因持久格式相同承诺无损滚动回退。
无新增依赖；journal5、ResultInbox v1、旧格式 decoder、摘要与 tool ledger 没有变更。

**本轮实测触发的修正：**

- 无匹配或重复的验证错误不能续期 server 的接收截止；仅有 error 的帧不刷新 deadline。
- 同一坏批中多个 history 回复可关联同一个请求；先验证全部关联，再按 Future 去重完成，
  避免重复 `set_exception` 让错误回包中断。
- 畸形 history 回复与 EOF 连续到达时，原异步拒收任务来不及执行，reader 的断线处理会先把
  Future 标为 `worker_unavailable`。现由回调对象同步校验权限、关联并完成等待，返回 Ready
  闭包执行有界异步回包。独立 history 在 reader 下一次读之前完成这一阶段；普通业务坏帧仍在
  网络 FIFO 原位置处理。拒绝改成等待所有 history handler 结束，以免慢查询拖住 reader/断线。
  对应 EOF 回归修改前失败、修改后通过；不增加消息包装、持久事件或额外执行 owner。

**已执行验证：**

- 新协议回归覆盖整批 Assignment 零效果拒收后同连接继续、普通工具 Future 失败、同批关键
  错误与普通工具错误收尾、混合 heartbeat 坏 payload 不 ACK/不续租/不执行工具、history
  错误与重复关联、固定两字段 error，以及断线不取消执行。
- PostgreSQL 验证 Assignment 拒绝遇到已 Started、已有结果、stop、新 epoch、租约过期或
  binding 释放时不改既有证据。执行既有真实数据库恢复、停止与提交回归。
- 临时 TCP/WebSocket smoke 使用真实 Runtime 连接方法、两个未重启的 worker 拨号 Task、
  uvicorn 重启及真实 PostgreSQL；检查两端重连后 epoch 更新、Gateway 路由恢复。业务依赖为
  空闲模拟对象，不将其描述为完整生产进程、外部模型或真实 GitHub 联调。
- 坏 ID、重复项、错误对象携带详情、错误 epoch、容量超限及普通拒收后 EOF 的边界 smoke 已执行。
- Ruff/Mypy、变更文件格式检查、前端 TypeScript/Vite 与 sdist/wheel 构建通过；架构图已渲染并检查。

**环境与限制：**本地以 Python 3.12 和 PostgreSQL 17 验证。曾把 pytest basetemp 放在 jj
workspace 内，已有 VCS 检测会错误读到父 jj remote；改用 pytest 默认的仓库外临时目录后全量
通过，未把独立 VCS 问题混入本 feature。测试仍有 Starlette 弃用及 subprocess transport 回收
时 event loop 已关闭的警告，后者来源尚未确定。保留日志，不将警告归零或旧方案 CI 当作本实现验收。

### 008 共享调度机制的消融验证

**日期：**2026-11-04。在同一输入、同一 Python 环境下，逐项比较本实现与简化变体。
变体仅替换独立子进程内导入的模块源码，不写 tracked 文件；基线均通过，简化变体均违背对应合同。

| 机制与输入 | 单项简化 | 实测结果与保留理由 |
| --- | --- | --- |
| 控制 owner 优先；已有混合控制/history 场景 | reader 无条件先分派 payload | history 在控制校验前完成；保留控制门控 |
| FIFO 与 Ready 公平；连续消息夹已完成 observer | 仅 inbox 为空才交付 Ready | 第二条消息越过应交付的 Ready；保留轮换 |
| observer 身份与撤销清理；同名反复注册/撤销 | 完成回调无条件入队、撤销不清队列 | 完成队列超过上界；保留身份判定和清理 |
| 回调范围硬截止；回调永久等待 Event | 不调用 timeout scope 的 reschedule | 简化版超过实验截止仍未退出；保留覆盖 await 的 deadline |
| 独立心跳；业务回调已阻塞 | 周期发送改挂普通 watch | 未能在限时内发出心跳；保留有限通信发送任务 |
| Started 写成功后立即 start；随后 EOF | start 另排本地 Ready | EOF 丢弃 Ready，执行未启动；保留连续 record_started/launch |
| 预启动拒绝；SQL 中已经 Started | 删除 started_at 围栏 | 已启动 assignment 被错误接受为拒绝；保留事务围栏 |
| 独立 history 拒收；畸形回复后立即 EOF | 将 prepare 推迟到异步回包任务 | 查询错误被断线覆盖；保留同步 owner 校验与 Future 收尾 |

**复现方式：**常规基线运行 `FLOWTOOL_TEST_DATABASE_URL=<本地测试库> python -m pytest -q`；
消融逐进程执行 `python -m pytest --noconftest -p tests.conftest -q -x <case>`，分别在模块加载后
用上表变体替换对应分支，再以相同 case 比较。永久 case 为
`test_communication_session.py::test_control_payload_waits_for_owner_before_query_dispatch`、
`test_session_events.py::test_ready_fairness_rearm_and_read_failure_drain`、
`test_session_events.py::test_gateway_deadline_covers_callback_and_cleanup`、
`test_assignment_rejection.py::test_assignment_rejection_preserves_competing_evidence[started]` 和
`test_payload_validation.py::test_history_invalid_response_finishes_original_query_and_allows_next[eof]`。
其余临时探针分别循环撤销同名观察、阻塞业务后限时读取 heartbeat、Started 写完成后立刻读 EOF
并检查 executor 已启动。临时测试/替换脚本在取证后删除，不作为永久测试负担。

日志保存在本 workspace 的 `.amp/in/artifacts/`：`ablation-summary.log`、各
`ablation-<机制>-baseline/simplified.log`、`history-eof-before/after.log`、`smoke.log` 与全量日志。
[本线程的验收摘要与日志链接](https://ampcode.com/artifacts/session-70d95eae-bb58-4554-8799-467c6b88ec5c)
记录精确结果及交付状态；代码 review gate 结果不以消融结果替代。未进行部署或生产外部副作用验证。

### 009 集中验证错误分派，收窄业务回调类型

**日期与触发：**2026-11-04。独立 review 报告 LOW：普通回调的 `Item | ValidationErrorResult`
签名使两端重复转发错误。用户随后要求“修复”，本条按该要求实施；不扩展到其他 review 建议或功能。

**决策与实现：**选择在 Dispatcher 中统一分派验证错误，删除逐方法错误分支和错误专用路由注册，
不另建错误路由表。`register(route, model, callback)` 的 callback 收窄为
`Callable[[Item, Incoming], Awaitable[None]]`；`configure` 必须提供一次
`validation_error: Callable[[ValidationErrorResult, Incoming], Awaitable[None]]`。
`WorkerCallbacks` / `ServerCallbacks` 均绑定已有的 `on_error`，普通处理方法只接收各自模型。
未注册方法和仅反向失败的方法仍交由同一 owner 检查 method/reply_to、epoch 和业务身份，
不以普通路由是否存在代替错误关联校验。

本条替代[修订 004](#004-用-tool_result-返回消息验证错误)中的联合类型回调和以
`ValidationErrorResult` 注册错误专用方法的内部 API 要求；原 wire 合同和失败处理职责保留。
Gateway 仍先执行控制 owner、`before_item`，再调用 Dispatcher，最后执行 `frame_done`；
`on_error` 的关联、预启动围栏、普通 Future 收尾及关键证据恢复代码未改。Session 的同步
`on_rejected` 准备阶段、history 有界旁路和固定两字段 error 也未改。

**验证与复杂度：**这是删除重复分支的行为保持重构，没有增加持久状态、任务或调度机制，
不新增消融变体或永久测试。已有测试仅更新必需钩子的装配；定向协议、Session、双端入口、停止、
history 和 Assignment 拒绝回归通过，Python 3.12 / PostgreSQL 全量、Ruff/Mypy 与变更文件格式检查通过。
临时内联探针通过真实 Gateway/Session 驱动检查控制帧、`before_item`、普通/错误回调、`frame_done`
及 EOF 清理顺序，覆盖已注册、单向和未知方法错误，并确认普通路由仍拒绝错误模型；未保留探针源码。
日志位于本 workspace `.amp/in/artifacts/error-routing-{focused,full,lint,mypy,contract}.log`。
全量仍出现此前的 Starlette 弃用和 subprocess transport 关闭警告；本次没有修改其来源。
本地修复已验证；随后的一次复审及交付状态见验收摘要。本条本身不表示已推送、合并或部署。

### 010 原地减少 serve 的异常与上下文嵌套

**日期与要求：**2026-11-05。用户要求降低 `Gateway.serve()` 嵌套，随后明确“不用拆分方法”，
重点处理嵌套的 `try` 和 `with`。本次保持单个方法，不增加私有辅助方法、包装对象或业务状态类型。

**实现与边界：**合并外内两层异常处理为一个 `try/except/finally`。仅在 bind、freeze 和
拒收回调装配成功后启用局部 `failed` 回调，因此配置错误继续直接传播，准入和业务错误继续交给
业务 owner 映射。以标准库 `ExitStack` 保存成功发布后才注册的 `closed` 钩子，替代 `entered`
标志及清理阶段嵌套的 `try/finally`；注销或关闭抛错时仍执行该钩子，配置/发布失败不会误调用。

`None` 唤醒判断提前到 `handling()` 外；`driving()` 仍覆盖整个消费循环，`handling()` 仍覆盖
opened、poll、Ready 和消息处理，等待 `next_event()` 始终在业务处理范围外。保留控制优先、
逐项串行、异常关闭码、取消传播、原 Session 回复及旧连接退出的身份安全。

**验证：**重构前后以同一临时探针调用真实 Gateway/Session，逐阶段注入配置、准入、发布、
回调、关闭故障及取消，比较回调顺序、异常类型和关闭码；普通消息、Ready、唤醒和 EOF 也逐项
对照，日志完全一致。已有 Gateway/Session、双端入口、验证错误、停止、history 和数据库拒绝
回归，以及 Ruff/Mypy、格式检查通过。临时探针已删除，保留 `serve-lifecycle-before/after.log`、
`serve-focused.log`、`serve-lint.log`、`serve-mypy.log` 和 `serve-structure.log` 于本 workspace
的 `.amp/in/artifacts/`。没有引入新的调度或持久机制，不新增消融变体或永久测试。
最新全量与交付状态见验收摘要；修订 009 的独立 review 不覆盖本次改动，发布前需评审当前版本。

### 011 删除 Session 的派生队列计数

**日期与要求：**2026-11-05。用户确认去掉 `_inbox_bytes`、`_inbox_items`；本次只精简这两个
字段，不调整其他通信状态，也不增加包装类、属性或辅助方法。

**实现与取舍：**普通输入与验证失败输入的容量检查，均直接对 `_inbox` 已保存的字节数和
payload 数量求和。删除构造、入队、出队及关闭时的计数维护。队列上限不变，求和最多遍历
32 项；用有界的计算替代重复状态。仍在 ACK 和查询旁路副作用前检查整个输入，入队后仍按
实际剩余 payload 计数，原始字节占用、拒收顺序及 EOF 排空规则不变。

**验证：**修改前的 Session、事件和验证错误回归通过；修改后扩展到 Gateway、双端入口、
history、停止与 Assignment 拒绝回归，全部通过。Ruff/Mypy 与变更文件格式检查通过，未新增
永久测试。日志为 `.amp/in/artifacts/session-counters-{focused,lint,format,mypy}.log`。
本次未重跑全量或发布 review gate；仅为本地修改，未推送，不以修订 010 的全量结果替代本次检查。

### 012 分离 Flow 路由与工具回调

**日期与要求：**2026-11-05。用户要求“把flow下的路由分离出去”，并询问为何 worker 注册
`flow.open_pr`。该 method 来自现有 MCP `open_pr` 工具；worker 注册回复，server 注册请求。
协议方法表示同一操作，不表示当前执行端；本次保留名称、请求/回复模型及关联身份。
当前工具服务校验远端 SHA 并保存 PR 意图，或返回既有 PR；实际创建 PR 仍由结果 effects 执行。

**决策与实现：**两端分别组合独立的 `WorkerFlowCallbacks` / `ServerFlowCallbacks`，模块均为
相邻的 `flow.py`。各自通过 `bind(routes)` 在原 Dispatcher 精确注册 `flow.open_pr`，连接回调
只调用该注册入口，并将未注册工具处理委托给 Flow 对象。不增加第二张路由表、通配注册、
动态扫描、pluggy 或 Gateway/Session 包装；本次不修改公共通信层与 Runtime 的装配接口。

- worker Flow 对象保存连接借出的 pending 调用，负责观察工具队头、发送和完成匹配回复。
  原工具 Future、队列与执行 Task 仍属于 WorkerExecution，断线不取消它们；回复仍使用
  `(method, reply_to, call_id)` 关联。连接回调继续处理 epoch、整批拒收和关键协议错误，
  先完成整批校验，再调用 Flow 对象拒绝普通工具 Future，不在扫描中提前改变状态。
- server Flow 对象检查 Flow/job 归属并调用原工具服务，通过绑定的 `_validate_active` 和
  `is_stopping` 回调读取连接 owner 的实时状态，不复制 assignment/stopping。仍先检查身份与
  Flow，再执行停止限制；stopping 不调用工具也不回复。正常回复固定原 Session。
- 未注册的类型化工具请求保留关联错误处理；即使漏注册 `flow.open_pr` 也不得补执行。
  未知命名空间仍按原类型化规则处理，不额外按 `flow.` 前缀过滤。worker 的未知工具回复
  仍只能完成原 pending；其他类型继续报协议错误。
- `ProtocolViolation` 移至 `workers/errors.py`，避免两个 server 回调模块循环导入。
  transport 和现有测试更新导入，不保留旧位置别名。Started、T/A、R/B、history、
  控制优先与持久证据格式未改。架构文档和 changelog 同步更新。

本条替代修订 002/003 中由单个连接回调直接持有 Flow 注册和处理方法的组织方式；统一注册
格式、角色命名与生命周期合同不变。已按 consulting-claude-oracle 使用受限本地 Claude
Opus 5.5 核对边界，确认初始化与响应模型、无工具/MCP、成功退出；主线程采用上述拆分，
未采用额外发送/清空包装方法。顾问意见不作为运行验证。

**验证与复杂度：**相同的双端入口、Flow、MCP、停止和验证错误定向回归在拆分前后均通过；
Python 3.12 / PostgreSQL 全量、Ruff/Mypy 与变更文件格式检查通过。临时探针使用真实
Dispatcher/Session 与模拟仓储/工具服务，检查正常注册、漏注册、未知方法、实时 stopping、
错误 Flow 和原 Session 回复关联；没有调用外部 GitHub。测试仅调整已有异常导入，未新增永久
测试或调度/持久机制；此行为保持的职责拆分不另做机制消融。
日志为 `.amp/in/artifacts/flow-callbacks-{before,after,full,lint,mypy,format,boundaries}.log`。
全量仍有此前的 Starlette 弃用和 subprocess transport 回收警告，后者来源未确认。
最新精确结果见[验收摘要](https://ampcode.com/artifacts/session-70d95eae-bb58-4554-8799-467c6b88ec5c)。
当前修改仅在独立 workspace，未重新运行发布 review gate、移动 bookmark、推送、合并或部署；
此前的独立审查与 CI 不覆盖这些新增修改。

### 013 分离请求路由与回复关联

**日期与要求：**2026-11-05。用户指出 worker 只需处理返回的 tool_result，无需注册
`flow.open_pr`，随后明确要求“按这个规则修改”：tool_call 按方法路由，tool_result 按
reply_to 关联原调用。本次应用到双端所有普通结果，包括工具、执行终态和停止/结果确认，
不只删除 worker 的一条 Flow 路由。该要求已批准实施。

**实现与替代范围：**

- `Dispatcher.register` 的泛型约束和运行时检查均只接受 ToolCall；路由表与 unrouted
  也收窄为请求。新增必需的 `configure(result=self.on_reply, ...)`，普通 ToolResult
  在查方法表之前交给连接 owner；ValidationErrorResult 仍先进入独立 validation_error
  钩子，因为它也可拒绝先前发送的结果。不会为回复新增方法表、Future 或 pending registry。
- worker 删除 `server.accept_result`、`server.accept_stop` 的回复注册，以及 Flow 对象的
  bind/未命中回复处理。`WorkerCallbacks.on_reply` 按 expected 调用的 message_id 选择结果或
  停止确认；工具回复交 `WorkerFlowCallbacks.on_reply`，先匹配 pending 的请求 ID，再检查
  FlowToolResponse 模型及原 method/call_id，交 WorkerExecution 完成原 Future。
  工具名无需预注册，未知请求继续直接拒绝。
- server 删除 `worker.execute`、`worker.stop` 的结果注册。`ServerCallbacks.on_reply`
  先匹配当前 stop/execution 的稳定调用 ID，再校验对应结果模型。旧任务终态可在重连后或
  新任务开始后到达；若未命中当前调用，仅将 ExecuteResult.job_id 作为数据库查找线索，
  必须用持久 job 的 worker/token 计算出匹配的 reply_to，才进入原 on_result 校验。
  持久来源分支不缓存数据；额外读取只用于不属于当前调用的重放，原结果 owner 仍复核
  payload token、digest、旧结果证据与 SQL 围栏。不能凭 payload 自报的身份接纳结果。
- server 的验证错误同样先定位原 message_id，再核对 method；保留固定大小的已发送关键
  消息记录及原错误策略。不匹配的错误不会改变业务状态或续租。
- 不修改 Gateway/Session、Runtime、wire v17、journal5/inbox1 或业务服务。history 已有的
  reply_to/epoch 临时关联和有界旁路保持不变；普通业务回复仍经控制、before_item 和串行驱动。
  Started 写后启动、T/A 提交后接纳、真实 R/完整 confirm、stopping 无新 effects、原 Session
  回复以及跨重连的原 Task/Future/stop 所有权不变。

本条替代修订 003/012 中“请求和回复都按 method 注册”的要求，以及 worker Flow 对象必须
提供 bind 的组织方式；对称命名不要求两端拥有相同的入站业务方法。模型解析仍可使用 method
选择严格 schema，这与注册业务处理路由不同。主线程核对所有调用点、history 旁路、稳定 ID
和旧任务重放回归后采用上述实现；受限本地 Claude Opus 5.5 提供实施草案，初始化/响应模型、
无工具/MCP 和成功退出均已核验，顾问意见不作为运行证据。

**验证：**增加一个公开 Dispatcher 合同回归，覆盖有/无同名请求路由时回复均进入原调用
结果入口；现有注册测试增加拒绝 ToolResult 模型的检查。测试在改实现前失败、修改后通过。
其余长期测试复用现有的重连、旧任务重复终态、停止凭据、校验失败、history、真实双端入口
及 PostgreSQL 场景。定向与完整测试、Ruff/Mypy、变更文件格式检查通过。临时探针另检查任意
工具名回复、错误 reply_to/method/call_id/model/epoch、原 Future 保留，以及当前/历史任务
的错误 owner/token/调用 ID 和缺少持久结果时零写入；使用真实 worker Runtime/Session 和
Dispatcher，外部执行/仓储使用测试替身，不声称真实 GitHub 或模型联调。探针源码验证后删除。

**消融与取舍：**以相同输入分别在独立进程中替换一个分支，不改 tracked 文件：

| 简化变体 | 实测结果与决定 |
| --- | --- |
| 删除 Dispatcher 的普通 ToolResult 分支，恢复按方法查请求表 | 同名回复触发请求模型不匹配，未注册回复落入未命中策略；保留独立结果入口 |
| server 只匹配当前调用，删除持久调用关联 | 旧任务结果重放导致连接报协议错误，同帧新任务结果也未接纳；保留持久身份关联 |

基线执行 `python -m pytest --noconftest -p tests.conftest -q`，用例为
`test_session_events.py::test_reply_reaches_its_owner_without_a_method_route` 和
`test_worker_handler.py::test_duplicate_prior_job_result_does_not_retire_current_assignment`；
变体通过 inspect 读取方法、只删除表中分支，在子进程内重新编译替换该方法，再运行同一用例。
完整测试使用 `FLOWTOOL_TEST_DATABASE_URL=<本地测试库> python -m pytest -q`。
日志位于 `.amp/in/artifacts/request-reply-{before,red,focused,full,boundaries,lint,mypy,format}.log`
及 `request-reply-ablation-{baseline,method_dispatch,current_only}.log`。
全量仍有既有的 Starlette 弃用及 subprocess transport 回收警告，后者来源未确认。

**实际状态：**实现、文档和本地验证完成；沿用同一 feature record、bookmark 和 PR。
本轮未运行发布 review gate、移动 bookmark、推送、合并或部署；精确结果与当前状态见
[验收摘要](https://ampcode.com/artifacts/session-70d95eae-bb58-4554-8799-467c6b88ec5c)。

### 014 通信命名统一为 msg

**日期与要求：**2026-11-05。用户要求“都统一成msg相关的命名，把frame相关概念删掉”。
本次清除应用通信代码、测试及当前架构文档中的 frame 术语；不更改连接建立、重连策略、
Session 生命周期或事件语义。此前修订 013 已随
[请求/回复分离提交](https://github.com/b1indsight/agentflow/commit/186dcbbe8f8dc83337e28ec3448d0a817d8c2809)
推送，本轮从该版本新建本地 change，未纳入其他 workspace 的工作。

**命名与取舍：**

- `WorkerFrame` / `ServerFrame` 改为 `WorkerPayloadMsg` / `ServerPayloadMsg`；
  `FrameHeader` / `frame_header` 改为 `MsgHeader` / `msg_header`，内部校验方法同步改名。
  两个具体变体仍表示 `control=None` 的 msg，包括空 payload 的纯收件 ACK。
  保留既有共同模型 `Message` 和方向联合 `WorkerMessage` / `ServerMessage`，用 PayloadMsg
  明确具体变体，避免与整个方向联合混淆；不新增包装层或兼容别名。
- `Dispatcher.configure` 的 `frame` / `frame_done` 改为 `msg` / `msg_done`；
  两端回调统一为 `on_msg` / `on_msg_done`，Gateway 及全部注册、调用点同步修改。
  完整 msg 仍先经过控制和 epoch 授权，再逐项处理 payload，全部成功后才执行 msg_done。
- 相关局部变量、测试构造器、用例名、注释、当前架构文档及类级数据流图同步更新。
  保留 Python 栈 `FrameType`、浏览器动画、CSS/CSP 等无关用法；历史方案记录保持原文，
  本条替代其中的通信命名，不追溯修改原决策。
- wire 字段、版本、验证约束及序列化内容不变。生成的 JSON Schema 类型标题和定义引用会
  随 Python 类名改变，但数据约束不变。Started 写后启动、T/A、R/B、history 旁路、原
  Session 回包、旧 finally 身份检查、执行 Task/Future 及持久证据所有权均保持原合同。

本轮按 consulting-claude-oracle 使用受限本地 Claude Opus 5.5 核对命名边界，已确认
初始化/响应模型、无工具/MCP、成功退出；采纳具体变体包含 ACK 和 schema 名称变化的提示。
顾问不作为运行验证或发布 review gate；最终命名与范围由主线程依据现有调用和模型确定。

**验证：**同一组协议、Session、双端入口、停止、history 和验证错误回归在修改前后通过；
Python 3.12 / PostgreSQL 全量、Ruff 与 Mypy 通过。核心变更文件格式检查通过；未整体重排
本来不符合当前 formatter 的旧持久化与测试文件。所有变更 Python 文件的 AST 与基线比较，
除预期改名、docstring 和导入排序外相同；原 wire 样例序列化逐字节一致，方向 JSON Schema
在归一化新类型引用和说明文字后约束一致。未增加永久测试或新的协议机制。
日志为 `.amp/in/artifacts/msg-naming-{before,after,full,lint,mypy,format,contract}.log`；
全量仍报告既有的 Starlette 弃用与 subprocess transport 回收警告。当前架构卡片和更新后的
类级图已用本地 Chromium 渲染检查；图的源码链接仍固定在已推送基线，并注明本地名称尚未推送。

**实际状态：**本地实现与验证完成；沿用同一记录、bookmark 和 PR。本轮未运行发布 review
gate、移动 bookmark、推送、合并或部署。前次 CI 不覆盖本地命名修订，精确结果见
[验收摘要](https://ampcode.com/artifacts/session-70d95eae-bb58-4554-8799-467c6b88ec5c)。

### 015 沿用 Message 命名并同步调整联合类型

**日期与要求：**2026-11-05。用户指出 msg 与 message 应按已有风格自动适配，随后要求
“调整”。修订 014 把概念统一机械地做成 Msg 缩写，并用 PayloadMsg 回避既有联合类型名称；
本条撤销该命名选择，保留去掉通信 frame 术语的要求和所有原行为合同。

**实现：**具体消息类直接使用 `WorkerMessage` / `ServerMessage`；原方向联合类型同步改名为
`WorkerMessageUnion` / `ServerMessageUnion`，仍是带 control 判别的 `Annotated` 联合类型，
没有新增类或包装。共同模型仍为 `Message`。连接头使用 `MessageHeader` / `message_header`，
Dispatcher 使用 `message` / `message_done`，双端回调使用 `on_message` / `on_message_done`。
类型注解、解析器、Runtime 装配、所有调用点、局部变量和测试构造器同步调整，不保留旧名。
外部 Codex 数据格式中的 `event_msg`、Python 栈帧与浏览器术语不属于此次内部命名修改。

当前架构文档和类级图同步，历史记录保持原文。协议字段、JSON 内容、验证规则、Session
生命周期、请求/回复路径及业务安全边界均不变；生成 schema 中的类型名称随具体类改名。
只调整已有测试，不新增永久测试。通过现有协议/双端回归、静态检查、wire 样例与 AST 对比
验证纯命名性质；精确运行结果和警告见验收摘要，日志使用 `message-naming-*.log`。
本轮沿用原本地 change，未重新运行发布 review gate、移动 bookmark、推送、合并或部署。

### 016 移除 WorkerClient，由 Runtime 直接管理连接

**要求与决定：**2026-11-05，用户要求“移除 workerClient 类，先把职责交给 Runtime”。
本条取代初稿及修订 002、006 中保留独立 WorkerClient 拨号器的决定；按明确要求直接收回
Runtime，不另建拨号器包装，也不在此阶段把建连策略下放到 Session。

**实现：**删除 WorkerClient 类和 runtime.client。WorkerRuntime._run_connections 直接读取
settings，负责 URL、认证头、指数退避/jitter、连接成功后的退避重置，以及连接任务的取消与等待。
原 shutdown 标记改为可等待 Event，兼供退避唤醒；仅显式 stop 设置，调用方取消仍按原规则传播。
进程 run 的 finally 取消并等待连接循环，后者等待当前连接清理。transport/client.py 只保留
Connection Protocol 与 WebSocketTransport，职责是 IO 适配，没有连接策略或业务回调注入层。

**保留边界：**迟到拨号在停止后不得创建 Session 或借用执行；普通断线只结束通信。
WorkerExecution 继续跨重连持有执行 Task、工具 Future 和 shielded stop。进程退出仍先 join
通信，再完成 execution.shutdown，最后释放 root lock；外部重复取消不能提前释放根锁。
Gateway、Session、双端业务回调、协议及持久化格式不变。

**验证与影响：**更新已有测试入口及 connect mock 的归属；不新增永久测试。复用拨号期间停止、
执行跨连接恢复、重复取消和子进程清理的回归；临时场景从 Runtime.run 验证认证头、退避增长/
上限/jitter/成功后重置、退避中停止，以及拨号中 stop 与 cancel。执行 PostgreSQL 全量、
Ruff/Mypy 与变更范围格式检查；精确结果见验收摘要及 runtime-dialing-*.log。
本次删除对象层，未引入需独立消融的新机制。当前架构文档与类级图同步，历史记录保留。
沿用同一记录、bookmark、PR 和本地 change；未运行新的发布 review gate，未推送、合并或部署。

### 017 Dispatcher 关联回复，on_reply 在交付前执行

**要求与决定：**2026-11-05，用户要求“把关联回复的转发给对应toolcall的逻辑上移到dispatcher，
on_reply设计成路由之后，转发给调用方之间的回调”。本条替代修订 013 中由连接 owner 选择普通
回复接收方的实现；请求仍按 `package.method` 注册，不恢复逐工具回复方法表。

**实现：**Dispatcher 提供 `expect_reply(call, model, deliver, *, on_reply=None)`，调用方在发送
请求前登记原 payload 的 message_id、method、结果模型及接收函数。收到普通 ToolResult 后，先
按 reply_to 查找，再校验 method/模型；随后 await 可选 on_reply，成功返回才 await deliver。
hook 抛错则不交付。请求表与生命周期配置仍在准入前冻结；回复关联随当前调用增减。

- WorkerFlowCallbacks 在 send_tool 登记绑定原 PendingToolCall 的 deliver；on_reply 只核对
  Flow call_id，deliver 才调用 WorkerExecution.finish_tool_call。删除 WorkerCallbacks.on_reply
  的二次选择与 Flow 转发，WorkerFlowCallbacks 不再自行匹配 reply_to 或结果模型。
- 执行、停止、结果接纳和停止接纳调用也在 Dispatcher 登记。server 的恢复仅恢复原调用头身份，
  不重造/重发 Assignment；旧任务结果在无当前关联时仍经 on_unmatched_reply 核对持久 worker/token
  与 execution_call_id，再进入原结果证据/提交流程。worker 的未匹配回复仍为协议错误。
- forget_reply 用于完成、弃用、guard 跳过、发送失败及停止收尾；Gateway finally 清空本连接关联。
  表只保存通信接收函数，不接管 Future 生命周期。WorkerExecution 的队列、Task、工具 Future、
  shielded stop 均不迁移，重连按原调用 ID 重新登记。已取消 caller 的匹配迟到回复仍可消费。
- T/R 的接收函数保留至 A/B 业务接纳，因此重复凭据仍按原规则核验；不在 Dispatcher 收到第一条
  回复时自动删除。Register 携带 A 后需要重放 T/A 时，owner 明确撤销旧 A 关联再登记，避免重复键。
- 验证错误仍首先进入 owner 的 on_error；整批拒收先完成控制/epoch 授权，用 reply_model 做
  非消费查询。拒绝已发送结果、Started 等无需普通回复的条目仍以原证据记录判断。
- Session.request/history 保留独立的有界查询旁路、timeout、原 Session/epoch 和断线失败语义。
  本次统一普通业务回复的分派，未把临时查询塞入串行业务 FIFO，也未合并两种 Future 生命周期。

**顾问与取舍：**按 consulting-claude-oracle 使用受限本地 Claude Opus 5.5 生成修订建议；已核对
初始化和响应模型、空 tools/MCP、成功退出。采纳每调用 hook、发送前登记、持久重放与临时查询
边界。没有采用建议中的“一次性出表”及把 on_reply 与交付合成同一函数：前者改变重复停止凭据
语义，后者不能表达用户要求的交付前拦截。本实现明确区分 hook 与 deliver，未新增包装类。

**验证：**已有定向回归先通过；更新公开 Dispatcher 用例并新增“同方法不同调用、逆序回复、
hook 拒绝不交付”的合同测试，旧实现失败后新实现通过。Python 3.12/PostgreSQL 全量、Ruff、
Mypy 和变更 Python 文件格式检查通过；沿用既有两类全量警告。临时场景验证 ID/method/model、
重复登记、错误不消费关联、撤销/关闭、真实 WorkerExecution Future、发送失败/guard、取消迟到
回复和重连。首次临时脚手架误用了空非 ACK 消息和未启动执行 owner，修正后才取得有效结果。

**消融：**同一输入下分别省略 on_reply、首次回复即删除关联、不撤销关联、去掉持久重放入口；
四个变体均触发对应合同失败，基线通过。内存替换单个方法、不改 tracked 源码。可用
`test_reply_hook_runs_after_correlation_before_original_caller` 重现 hook 顺序检查，用
`test_duplicate_prior_job_result_does_not_retire_current_assignment` 重现持久重放检查；其余
临时场景与日志说明见[验收摘要](https://ampcode.com/artifacts/session-70d95eae-bb58-4554-8799-467c6b88ec5c)。
日志位于 `.amp/in/artifacts/dispatcher-replies-*.log`；临时测试和消融脚本不进入提交。

**实际状态：**本地实现与验证完成；同一记录、bookmark、PR。没有新一轮发布 review gate，
未推送、合并或部署。历史 head 的 CI 不覆盖这次修改。

### 018 Worker 协议与 Flow 回调独立挂载

**要求与决定：**2026-11-05，用户要求“把worker独有的和WorkerFlow的两个对象调整成并行独立的挂载”。
本条替代修订 012/017 中 WorkerCallbacks 创建并协调 WorkerFlowCallbacks 的部分；平级挂载仍使用
同一串行 driver，不增加并发业务消费或协调器包装，也不扩大为 server Flow 装配重构。

**实现：**WorkerRuntime 分别创建两个对象，传给 `Gateway.serve(..., *callbacks, replace=...)`。
Gateway 对所有对象调用同一 `bind(routes)`，再统一 freeze；连接协议仍只有一个 configure owner，
重复配置或路由冲突在准入前失败。WorkerCallbacks 不再导入、持有或调用 Flow 对象，也不管理
tool watch 或判断 FlowToolResponse；自身只管理连接协议、terminal 观察与关键持久证据。

- Dispatcher 增加冻结前注册的 `before_wait(callback)`；共同 driver 在处理完整事件后、下次
  next_event 前 await prepare，仍受 handling 与绝对截止保护，不插入消息批次中间。
- WorkerFlowCallbacks.bind 登记自身 prepare，按已绑定 epoch、原执行 lease、assignment 和 stopping
  事实管理 tool watch、借用槽及关联退休。WorkerExecution 仅新增只读 stopping 投影，不迁移任务
  或增加共享状态；Flow 的 watching 标志防止重复注册和重复借用队首。
- 正常完成保留原调用，允许消费已取消 caller 的迟到回复；停止或 assignment 退休/更换时撤销。
  不采用“lease 消失就立即删表”，也不无限保留旧 receiver。检查/拒收/交付均绑定原 PendingToolCall，
  清理只清对应调用；发送前及实际写入 guard 复查 lease，避免 executor 已结束但 collector 尚未运行
  时发送工具。Flow 自行重挂观察，不经 WorkerCallbacks 转发生命周期。
- expect_reply 可登记同步 on_rejected；WorkerCallbacks 在 epoch 与整批关联头授权后调用通用
  reject_reply，Dispatcher 按 ID/method 通知原调用。畸形结果不按结果模型验证；关键执行/停止错误
  仍按已有证据策略处理，错误回包保持固定两字段。未知后续 header 不得让前面的工具 Future 提前失败。
- Registered 缺少恢复决定而执行仍存活时拒绝绑定 epoch；server 的正常恢复路径发送明确 resume/replay，
  拒绝恢复则先撤销旧执行。不得把“收到某个新 epoch”当作旧执行获得授权。

**顾问与核对：**受限本地 Claude Opus 5.5 咨询完成，实际模型、空 tools/MCP 和成功退出已核验。
采纳事件间协调点、每调用身份及迟到回复风险；没有采用顾问的“停止也保留 receiver”建议。
进一步读取 Session._queue_rejection 确认 worker 业务拒收整体位于 FIFO driver；不采纳其关于这里
必在 reader 执行业务的推测，不引入额外 TOCTOU 分支。独立 history 保留原同步授权旁路。

**验证与取舍：**修改前定向基线通过。扩展既有 Gateway 截止/清理合同用例，以正反挂载顺序验证
独立 bind、hook 冻结、单 driver 与 deadline；旧实现因不接受多个回调失败，修改后通过。
更新一个 idle execution mock，使 lease 明确为 None；首次定向失败源于该 mock 默认返回对象，
未绕过生产授权检查。扩大的通信/执行/停止定向回归、PostgreSQL 全量、Ruff/Mypy 及变更范围格式
检查通过，全量仍有既有 Starlette 弃用和 subprocess transport 清理警告。
临时真实执行 owner 场景覆盖挂载顺序、未授权重放、未知整批 header、错误 epoch/method、停止后
下一任务、迟到回复及 executor 完成与 collector 之间的写入窗口；无真实远端副作用。

**消融：**各变体独立进程、仅内存替换，基线通过；移除 before_wait、拒收分派、epoch 检查、
stopping 区分或正常终态关联保留，分别触发预期合同失败。因此保留这五个机制，不引入
执行状态事件总线或额外包装类。公开回归入口为 `test_gateway_deadline_covers_callback_and_cleanup`、
`test_mcp_shutdown_consumes_late_tool_response_before_delivering_result` 和既有 payload validation 用例；
临时场景及消融证据见[验收摘要](https://ampcode.com/artifacts/session-70d95eae-bb58-4554-8799-467c6b88ec5c)，
日志为 `.amp/in/artifacts/independent-mount-*.log`，临时脚本不保留为永久测试。

**实际状态：**仅本地独立 workspace；沿用同一记录、bookmark 与 PR，未运行本轮发布 review gate，
未推送、合并或部署；此前 CI 不覆盖本轮。Started、T/A、R/B、Session 生命周期、持久格式和 history
旁路不变。当前架构文档和类级图同步，旧时间线保留。

### 019 Worker 状态与业务分类，删除 history 旁路

**要求与状态：**2026-11-05，用户要求“将worker 的状态相关的回调和业务相关的回调分离成2个独立的处理类，
把 worker.history 也加入到分类里，彻底清理掉旁路逻辑”。按批准方案完成本地实现与验证。
本条替代初稿 §1.2/1.3 以及修订 017/018 中保留独立 history 旁路的决定；旧记录保留为历史。

**职责划分：**这里的“状态”包括连接与执行状态协议，不限于连接是否在线。

| 独立挂载对象 | 处理内容 | 注册方式 |
| --- | --- | --- |
| `WorkerStateCallbacks`，`transport/state.py` | Register/Registered、epoch、heartbeat；execute/stop/reject_recovery；Started、terminal 观察、结果/停止接纳；连接级授权、验证错误及清理 | `bind(routes)`；唯一 `configure` owner；注册 `worker.execute`、`worker.stop`、`worker.reject_recovery` |
| `WorkerBusinessCallbacks`，`transport/business.py` | Flow 工具发送、回复 hook、原调用交付及退休；`worker.history` 的读取与回复 | `bind(routes)`；注册 `before_wait(prepare)` 及 `register("worker.history", ConversationQuery, self.on_history)` |

Runtime 分别构造并平级挂载这两个对象；互不导入、持有或调用。现有 WorkerCallbacks 和
WorkerFlowCallbacks 迁入这两个文件，不保留别名、旧文件或第三个转发对象。真实执行、工具 Future、
shielded stop 继续归 WorkerExecution，持久证据继续归 ExecutionState。server 只调整查询接入和授权，
不扩大为 server 回调分类重构。

**统一处理与删除范围：**

- 所有入站 payload 均保留在 Session FIFO，经 Gateway 的连接/控制授权、逐项检查和 Dispatcher 处理。
  history 请求、回复与验证错误不再提前消费；畸形消息的拒收也在 FIFO 原位置完成整批授权后处理。
- 删除 `Responder`、`ReplyKey`、Session 的 `responder/reply_key` 注入、`request`、`reject_queries`、
  `dispatch_item`、临时查询关联表、handler/busy 任务和队列；删除 reader 中的独立分流、
  Gateway 的混合消息 shortcut、worker 的 `history_responder` 和 server 的 `conversation_result_key`。
- history 回调内直接 await 线程中的只读操作，再通过原 Session 回复请求 epoch；不创建独立 handler Task、
  优先队列或另一条消费循环。现有 FIFO 容量限制保留，超限仍结束连接；删除 worker 侧并行 responder 的 busy 机制。
- 传输收件 ACK 和出站周期 heartbeat 保留，不处理业务 payload。HTTP 发起查询仍用选定 Session 的发送锁；
  回包统一经过 driver，不能在 driver 内等待排在自己后面的回复。

**查询关联与生命周期：**

1. 复用 Gateway 现有 `Registration`，增加本连接 Dispatcher 引用；`get` 一次返回已发布的 Registration，
   调用方从中取得配对的 Session/Dispatcher。`wake_all` 与直接 register 的测试同步适配。
   不新增 Endpoint/Context，不另建 peer 表，不在请求中重新选择后继连接；latest-wins 与旧 finally 身份检查不变。
2. 将临时查询 Future 和每连接配额从 Session 迁至 ConversationBroker；Broker 继续管理进程配额及查询总预算。
   发送前使用既有 `expect_reply` 登记 ConversationResult、交付函数和拒收函数；成功、失败、timeout、
   caller cancel 均撤销本次关联。配额按原 Session 统计，不承担入站回复匹配；唯一匹配表仍在 Dispatcher。
   不增加 `Gateway.request` / `Dispatcher.request` 或新异常包装层。
3. 给每调用接收登记增加可选的关闭通知；Session 的实际关闭通知 Gateway 所挂的 Dispatcher，连接清表时
   只通知显式登记该通知的临时调用。没有关闭通知的持久接收者只释放连接引用，不取消 Task/工具 Future。
   明确关闭、替换和写失败及时失败查询；EOF 先按 FIFO 处理已经收到的回复/拒收，再失败剩余查询。
4. 保留每连接 4、进程 64 的查询额度、30 秒总预算及页内容/大小校验。发送与等待的关闭/取消竞争仍需覆盖：
   取消锁等待时不发送；已经进入实际写入时若结果不确定，沿用 Session 的封闭连接策略。
   沿用 Session 现有 driver 身份做等待守卫，不复制 driver Task 字段；明确禁止回调内阻塞等待查询结果。
5. 同 epoch 的迟到 history 回复在普通 unmatched_reply 策略中丢弃，不能关闭健康连接或匹配下一查询；
   不增加 tombstone 缓存。错误 epoch 先按连接授权规则处理。其他未匹配业务回复及持久重放的既有策略不变。
   畸形回复和 ValidationErrorResult 经过 owner 授权后由 `reject_reply` 通知原调用，映射为 invalid_response；
   错误回包仍只有固定 code/message，不附诊断内容，不对错误再次回错误。

**权限与行为取舍：**

- 保留 Registered 前的合法只读 history 查询，但也必须经过状态 owner，再进入业务路由；查询只借用请求 epoch
  回复，不绑定 Session epoch、不设 ready、不授权执行恢复。混合控制/payload 必须先接受控制决定。
  无效控制、错误 epoch、关键恢复围栏和整批拒收检查不得因 history 被跳过。
- server 将只读查询回复与执行状态消息分类：history 回复不要求先收到 Started，也不能代替 Started、
  触发 Flow effects 或刷新执行存活期限。检查发生在正常 driver 中，不恢复 reader 旁路。
- 慢 stop、preparation、工具/结果回调会延迟 history；history 读取也会延迟后续 stop 或工具回复。
  超过查询预算返回 query_timeout；超时不重放。ACK/已启用的周期 heartbeat 仍可运行，但不能据此保证业务回调及时完成。
- 只读操作使用 `to_thread` 避免阻塞事件循环；取消 await 不能强制终止已运行线程。
  预期 ConversationFailure 保留原错误码；未预期异常交给现有连接失败处理，不继续吞掉并留下悬挂查询。

**实施顺序与验收合同：**先更新串行及查询生命周期合同用例，再迁移回调分类和 Broker/Dispatcher 关联，
删除 Session/接入器/Gateway 旁路，最后同步当前架构文档、类级图和 changelog。永久测试聚焦公共合同，
其他边界用临时验证，禁止靠保留隐藏分流满足旧旁路用例。

- 更新 `test_conversation_worker` / `test_conversation_e2e` 中“慢回调期间 history 仍可完成”的断言，
  验证双向等待顺序、混合消息控制优先、握手前只读及 ACK/heartbeat。
- 复用查询 proxy、payload validation 和 Session/Gateway 用例，验证原连接/epoch、不同 ID、迟到回复、
  配额释放、send+wait timeout/cancel、实际关闭/替换、reply 或错误后 EOF，以及 pre-Started 合法 history 回复。
- 回归 Started 写后 launch、T/A、R/B、stopping、执行跨重连与旧持久格式；运行 PostgreSQL 全量、
  Ruff/Mypy、变更范围格式检查和剩余符号搜索。当前历史测试结果不作为此修订的验收。
- 对新增的关闭通知和 caller 生命周期适配做小范围消融：去掉通知应暴露慢 driver 下等待者不及时失败；
  去掉关联撤销/发送取消应暴露迟到回复或取消后发送问题。只保留有合同证据支持的机制。

**顾问与核对：**本地受限 Claude Opus 5.5 已生成修订建议；初始化与响应模型一致、tools/MCP 为空，
最终 result 成功且进程退出码为 0。采纳状态协议/业务分类、统一 FIFO、关闭通知和迟到回复风险。
未采用顾问建议的两层 request API、新异常，以及 Registration 上重复的 opened/driver 字段；
临时 Future 由 Broker 持有，关联复用 Dispatcher，选择复用原 Registration。也不采纳 history 刷新执行
接收期限的建议，避免只读查询被误当作执行存活证据。顾问报告把 ExitStack 的关闭 hook 顺序读反，
源码实际是 transport 清理后执行 hook；本修订不改变这一顺序。咨询不是实现验收或发布 review。

**实现与验证：**旧类和 history 旁路均已移除，当前架构文档、类级图及 changelog 已同步。
迁移现有测试以验证统一 FIFO，并在 ConversationBroker 的公开 query 接口覆盖发送锁等待、实际写入、
等待回复三个阶段的取消、超时、断连与配额归还，以及慢 driver 中的显式关闭。
回复紧跟 EOF 的回归先失败后通过：关闭通知曾取消已写完但尚未返回的 send Task；现仅通知临时 Future，
Session 关闭未完成的实际写入，Broker 仍要求 send 成功后才返回已收到的回复。未写完的请求仍失败。
首次全量暴露一个旧测试仍假定 history 阻塞时工具可绕过 driver 发出；改为验证 history 结束前
被取消的工具不会发出，没有保留旧分流。修改后的定向回归、PostgreSQL 全量、Ruff/Mypy 及变更范围
格式检查通过；全量仍有既有 Starlette 弃用与 subprocess transport 回收警告。

临时场景验证 driver 自等守卫、迟到回复、pre-Started history 不刷新接收期限，以及拒收紧跟 EOF
仍按 FIFO 顺序处理。类级图用 Chromium 检查交互、源码符号、360/720px 溢出和连线，并检查渲染截图。
可复现命令为 `FLOWTOOL_TEST_DATABASE_URL=postgresql://127.0.0.1:55617/agentflow_test .amp/in/venv312/bin/python -m pytest -q`、
`.amp/in/venv312/bin/ruff check app tests` 与 `.amp/in/venv312/bin/mypy app tests`；查询生命周期入口为
`tests/test_conversation_proxy.py`，FIFO 入口为 `tests/test_conversation_worker.py` 与 `tests/test_communication_session.py`。

**消融与取舍：**同一临时探针内独立运行基线与逐项内存替换，不改生产文件。移除实际关闭通知后，
慢 driver 中的查询仍悬挂；移除 forget_reply 后保留已取消接收者；移除发送任务回收后取消查询仍遗留
等待发送锁的任务。三项简化均未满足生命周期合同，因此保留关闭通知、关联撤销和发送取消/join；
不恢复 Session 请求表或独立业务消费者。探针与消融脚本不保留为永久测试，证据保留在
`.amp/in/artifacts/state-business-*.log` 和[验收摘要](https://ampcode.com/artifacts/session-70d95eae-bb58-4554-8799-467c6b88ec5c)。

**交付范围：**修改仅来自本独立 workspace，保留此前修订；沿用同一记录、bookmark、PR，
不包含源 workspace 的测试精简。发布仅使用 review gate 返回的精确提交；结果与 CI 状态见
[PR #127](https://github.com/b1indsight/agentflow/pull/127) 和验收摘要，不沿用旧 head 结论。
跨重连执行与持久证据合同不变；本轮不包含合并或部署。

### 020 按职责拆分 protocol 的类型归属

**要求与状态：**用户要求将 `app.contracts.protocol` 中属于其他职责的类移至对应文件。
本节为待审批的迁移方案；尚未修改实现或测试。沿用本 feature record、bookmark 和 PR，
不另开工作线程或 PR，不把本地方案自动推送。核对基线为已推送的
[df9b5f19](https://github.com/b1indsight/agentflow/commit/df9b5f190098f9001c5a79240cac12910bf901c4)。
本修订只改变定义与导入位置，不替代 019 的运行时、FIFO、权限或生命周期决定。

**归属原则：**保留 `contracts` 对双端共享消息、执行任务和冻结结果的所有权；不因消费者是 worker
就把共享类型放入 worker 实现包，也不让 worker 导入 controler。已经属于 domain 的类型仍从其
定义模块导入，不借本次搬迁改造领域模型或继承体系。`protocol.py` 收敛为双端协议的方向联合、
握手/控制消息及解析入口；业务方法的 payload 不再集中定义在那里。

**完整类迁移表：**目标路径均位于 `app/contracts/`；未标新建的文件复用现有模块。

| 目标文件 | 从 protocol 迁入或保留的类 | 职责 |
| --- | --- | --- |
| `message.py`（新建） | `ProtocolModel`、`ToolCall`、`ToolResult`、`PayloadValidationError`、`ValidationErrorResult`、`Message`、`MessageHeader`、`CallHeader`、`ResultHeader` | 公共消息合同、调用/回复头与固定校验错误；不含具体业务方法 |
| `tasks.py`（新建） | `InitialTask`、`RevisionTask`、`RevisionExecutionRecord`、`ArchiveIdentity`、`ArchiveTask`、`InitialTaskResult`、`RevisionTaskResult`、`ArchiveResult`、`ExecutionIdentity`、`StopFence` | 跨进程执行任务、结果与派发/停止授权身份 |
| `receipts.py` | `Succeeded`、`Failed`、`ExecutionStopped` | 持久终态及停止证据，与已有旧格式读取器和摘要校验归在一起 |
| `execution.py`（新建） | `PushSecret`、`AssignmentSecret`、`AssignmentArguments`、`RecoveryRejectedArguments`、`AcceptResultArguments`、`AcceptStopArguments`、`AcceptedResult`、`Assignment`、`StopExecution`、`RecoveryRejected`、`Started`、`AcceptResult`、`AcceptStop`、`ExecuteResult`、`StopResult`、`ResultAccepted`、`StopAccepted` | 两个方向的执行控制请求/回复；凭据只随指派消息传递，不进入持久 task/receipt |
| `conversation.py` | `HistoryArguments`、`ConversationQuery`、`ConversationResult` | 与现有 history 页、游标和查询错误合同同处一处 |
| `flow_tools.py`（新建） | `FlowToolArguments`、`FlowToolRequest`、`FlowToolErrorKind`、`FlowToolResponse` | Flow 工具调用合同；不拥有 Flow 生命周期或服务实现 |
| `protocol.py`（保留） | `RecoveryDescriptor`、`RecoveryDecision`、`WorkerMessage`、`ServerMessage`、`Register`、`Registered`、`Heartbeat`、`ProtocolError` | 注册/恢复控制、方向消息和跨 payload 约束 |

类型关联定义一起归位：`PROTOCOL_VERSION`、`MAX_PAYLOAD_ITEMS`、`MessageId`、`ConnectionEpoch`
以及 `message_header` / `call_header` / `result_header` 随公共类型移至 `message.py`；
`ExecutionTask` / `ExecutionResult` 移至 `tasks.py`；`TerminalPayload` 与 `terminal_digest`
移至 `receipts.py`。全部类名、字段、默认值、字段顺序、基类与校验函数保持不变，不新增业务类。

`protocol.py` 保留 `_FIXED_METHODS`、两个 discriminator、Worker/Server 的 Call/Result/Payload
及 MessageUnion、两个 TypeAdapter 和方向解析函数。E/T/A/C/R/B 六个稳定 ID 函数及
`_ID_NAMESPACE` / `_stable_id` / `_terminal_id` 也整体保留：它们定义协议重放/接纳身份，
Register 校验仍在这里使用它们；下层数据模型无需反向导入这些函数。此次不新建 ID 工具模块。

**依赖与导入边界：**

- `message` 不导入其他应用业务模块；`tasks` 只引用公共消息基类与已有 domain 类型。
- `receipts` 引用 `message` / `tasks`，彻底删除对 `protocol` 的反向导入。
- `execution` 引用 `message` / `tasks` / `receipts`；`conversation` 引用 `message`；
  `flow_tools` 引用 `message` / `tasks` / domain。上述模块均不引用 `protocol`，不存在方向联合循环。
- `protocol` 聚合这些合同；内部以模块限定引用使用移出的类，避免保留同名隐式转导出。
  不添加旧路径别名、`__getattr__`、包入口导出或兼容 facade。
- 全部应用和测试调用方直接从定义文件导入。通信核心的 `Message` / `ToolCall` / `ToolResult`
  改从 `message` 导入；方向类型和解析器仍从 `protocol` 导入。现有 `validation` 仍消费方向解析器，
  本次不声称或扩展为删除它的全部传递依赖。
- 修正 `worker/execution/prompts.py` 从 protocol 间接导入 `ReviewFeedback` 的用法，直接引用
  `domain/review_models.py`。不搬动这些已有 domain 定义，不新增导入聚合层。

**保留合同与风险：**wire 仍为 v17，journal5 / inbox1 不变；JSON 字段、顺序、别名、
discriminator、extra 禁止规则和固定校验错误保持一致。`HistoryArguments` 不合并为
`HistoryQuery`，游标解码错误继续由后者转换为查询错误；`HistoryArguments` 的基类不变。
`PushSecret` 保持 repr 脱敏和 JSON 显式序列化行为，不能意外进入 journal。
终态/停止摘要和稳定 ID 的输入投影、编码与 namespace 不改。Pydantic 字段类型必须保持运行时
可见，不用 TYPE_CHECKING 或延迟 model_rebuild 掩盖循环。导入 protocol 会新增加载 receipts
及其只读 legacy adapters，需检查独立解释器的导入边界；不引入 SQL、SDK 或运行时初始化。

**实施与验收（批准后执行）：**

1. 先在未改实现上运行协议/收据/验证错误/边界的现有用例，并保存临时差分基线：方向 JSON schema、
   迁移模型的字段顺序/config、代表性消息及 journal/inbox 序列化、终态/停止摘要与 E/T/A/C/R/B，
   包括凭据脱敏、payload 拒收和 history 游标错误分类。样例用固定 ID，不记录真实凭据。
2. 先移公共消息与任务，再移持久证据，再移执行/history/Flow payload，最后收敛方向协议并更新
   全部调用方导入。纯迁移不新增永久测试；现有测试只调整所属模块的导入和必要的 patch 路径。
3. 用同一临时脚本复跑，要求结构、JSON、错误分类、摘要和 ID 一致。仅允许 Python 模块路径及
   预期导入集合变化；用 AST 清单核对每个原类只定义一次、调用方直接导入真实 owner，检查旧符号
   不再经 protocol 暴露。独立解释器验证 message/tasks/receipts 等没有反向加载 protocol。
4. 运行 protocol、payload validation、receipts、delivery receipts、stop、Session/Gateway、
   history 和 import boundaries 相关用例，再运行 PostgreSQL 全量、Ruff、Mypy 与变更范围格式检查。
   历史 CI 不作为这次搬迁验收；同步架构说明、当前类级图源码链接、changelog 与本记录。
5. 这是类型定义归位，没有新增运行时机制，消融不适用；以行为差分和依赖检查验证迁移。
   后续推送仍需新请求和精确提交 review gate；本次不改远端 PR 状态、不合并或部署。

**取舍与咨询：**受限 Claude Opus 5.5 已生成方案并按职责拆分补充材料复核；两次初始化/响应
模型一致，tools/MCP 为空，result 成功且进程退出码为 0。未采用首稿将 `ProtocolModel`
放入执行任务模块、为避循环而保留 history/Flow 类型集中的建议；公共消息模块已有完整实际职责，
无需空的 base 文件。采用 `flow_tools.py` 的明确命名、持久证据归 receipts、ID 函数集中保留及
模块限定引用。未采用大范围领域迁移、重复全量基线测试和为文件位置新增永久测试。
主线程已静态核对原类清单无遗漏/重复；咨询与清单核对均不代表实现或行为验证已经完成。

**源码依据：**[原类型与协议](https://github.com/b1indsight/agentflow/blob/df9b5f190098f9001c5a79240cac12910bf901c4/app/contracts/protocol.py)、
[持久读取器](https://github.com/b1indsight/agentflow/blob/df9b5f190098f9001c5a79240cac12910bf901c4/app/contracts/receipts.py)、
[history 合同](https://github.com/b1indsight/agentflow/blob/df9b5f190098f9001c5a79240cac12910bf901c4/app/contracts/conversation.py)、
[包边界](https://github.com/b1indsight/agentflow/blob/df9b5f190098f9001c5a79240cac12910bf901c4/tests/test_boundaries.py)。

### 021. 删除 client 模块，将连接接口与适配器移入 Runtime

**要求与状态：**用户要求删除 `app/worker/transport/client.py`，将其中类移入 runtime。
按小改动路径完成本地实现与验证，未推送；本次不实施仍待审批的修订 020。

**实现：**将 `Connection(Protocol)` 和 `WebSocketTransport` 原样移到
`app/worker/runtime.py` 的模块级定义，删除 client 文件，不保留兼容别名或转导出。
更新测试辅助对象导入、review 路径夹具及架构说明、changelog。拨号、认证和重连仍归
WorkerRuntime；适配器的文本/字节收取、发送、关闭 code 与 I/O 异常传播均不变，
Session、Dispatcher 和执行生命周期未修改。

**验证：**PostgreSQL 下的 worker runtime/client、包边界、conversation worker、payload
validation 和 router 现有用例通过；`ruff check app tests`、`mypy app tests` 与 Runtime
变更范围格式检查通过。临时 AST 对比确认迁移的两类及原有 Runtime 类均保持不变，旧模块
已不可导入，新路径类型注解可解析；直接执行适配器收发、关闭和异常传播检查通过。
纯位置调整不增加永久测试；本次未重跑全量测试，历史 CI 不作为此次验收。
类级图仍标记为上次已推送版本，本地源码以本节及当前 Runtime 文件为准。

### 022. 区分结果接纳请求、回复与回复数据的类型命名

**要求与状态：**用户要求区分 `AcceptResult`、`ResultAccepted`、`AcceptedResult` 的命名。
按小改动路径完成重命名和本地验证，尚未推送。

**命名：**`AcceptResult` 改为 `AcceptResultRequest`，`ResultAccepted` 改为
`AcceptResultResponse`，`AcceptedResult` 改为 `ResultAcceptance`。请求/回复用一致后缀区分，
数据体直接表达接纳事实，避免仅靠交换 Result/Accepted 单词顺序区别层级。
双端构造、分派、回复关联、执行状态消费及现有测试统一使用新类型，不保留旧名别名。
`AcceptResultArguments` 和回调函数名不变；不扩大到停止等其他操作的重命名。

**合同：**`server.accept_result`、tool_call/tool_result、字段、稳定 ID 和结果提交条件不变，
wire 版本不变；JSON schema 中的类名标题与对应引用随重命名更新。原初方案和历史修订保留
原名，按本节映射理解；修订 020 中对应类型也采用此映射，其包归属拆分仍待审批、未实施。

**验证：**相关 PostgreSQL 用例覆盖协议解析、校验拒收、双方回调、执行生命周期、停止、
断线/归档恢复、history、ASGI、Session/Gateway 和包边界，全部通过。Ruff、Mypy 与变更范围
格式检查通过；临时对比确认源码/测试 AST 仅有这三个标识符及导入排序变化，请求与 done/failed
回复的 wire JSON、错误分类和归一化 schema 一致，旧名已不可导入。未新增永久测试，
未重跑全量测试；TestClient 依赖有弃用警告，未在本次调整依赖。历史 CI 不覆盖此次修改。

### 023. 精简重复测试，保留公开行为与故障边界

**要求与状态：**用户要求清理多余测试。本次仅调整本 workspace 的测试与本记录，不改应用
实现，不引入新设计，不实施待审批的修订 020，也不纳入其他 workspace 的测试精简；尚未推送。

**取舍与实现：**

- 删除 worker、server 的重复执行/工具调用成功路径及专用 `ToolSocket`。保留
  `test_remote_e2e.py::test_control_gateway_and_worker_client_complete_one_remote_job`，
  它连接真实双方 Runtime/Session，覆盖工具回复、结果提交直到 worker journal 确认；
  保留真实 ASGI/PostgreSQL 接入测试，以及 Started 写入、未提交结果、停止、重连、旧任务重放回归。
- 删除 history 单次成功查询测试；其关联结果与配额回收已由同文件的错误关联、取消/超时/断连后
  再查询、配额复用场景覆盖，不删各个实际 I/O 阶段的生命周期检查。
- 将修订 013 的独立请求/回复路由测试并入
  `test_session_events.py::test_reply_hook_runs_after_correlation_before_original_caller`，
  参数化有/无同名请求路由，继续检查按调用关联、hook 拒绝、先 hook 后交付、撤销后的迟到回复。
- 认证失败发生在读取第一条消息之前，删除永远不会被读取的 Register payload 参数组合及
  无关 Runtime 构造，保留不 accept、不读取、不调用业务入口的断言。版本拒收并入实际首消息
  验证用例，只改变版本号，避免旧样例同时缺少多个必填字段而掩盖版本检查；同步检查错误分类。
- 清除失去使用者的测试状态、事件和导入；仍被恢复并发测试使用的记录字段不删除。

**验证：**修改前相关测试通过；修改后相同范围、PostgreSQL 全量、`ruff check app tests`、
`mypy app tests` 与新增/修改代码范围的 Ruff 格式检查通过。临时在独立解释器中分别让回复
按请求方法路由、跳过 `on_reply`、跳过 worker 的结果确认，保留的对应测试全部按预期失败；
正常代码复跑通过。实验仅修改进程内对象，不修改应用文件，不保留新的永久探针。
逐项审查清理 diff，并与清理前快照比较，确认此前的应用修改完整保留。
日志为 `.amp/in/artifacts/test-cleanup-{before,after,full,ruff,mypy,format}.log` 和
`test-cleanup-mutation-{method_route,skip_reply_hook,skip_worker_ack}.log`。
全量仍报告既有 Starlette 弃用和 subprocess transport 回收警告，本轮未调整依赖或进程清理。
未运行发布 review gate、推送、合并或部署。

## 3. 源码依据

以下链接固定在本次核对的基线，不把方案当作已实现架构。

[S1]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/communication/gateway.py#L26-L136
[S2]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/communication/session.py#L60-L426
[S3]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/worker/transport/client.py#L131-L509
[S4]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/controler/workers/handler.py#L134-L746
[S5]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/worker/execution/lifecycle.py#L77-L451
[S6]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/worker/runtime.py#L93-L182
[S7]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/communication/dispatch.py#L9-L51
[S8]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/persistence/workers.py#L83-L225
[S9]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/controler/workers/transport.py#L35-L139
[S10]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/persistence/stops.py#L105-L158
[S11]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/contracts/receipts.py#L1-L85
[S12]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/.github/workflows/ci.yml
[S13]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/controler/runtime.py#L66-L105
[S14]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/contracts/protocol.py#L286-L484
[S15]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/controler/workflow/tools.py#L46-L71
[S16]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/tests/test_protocol.py#L185-L193
[S17]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/tests/test_flow_tools.py#L383-L391
[S18]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/contracts/conversation.py#L106-L117
[S19]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/tests/test_protocol.py#L299-L325
[S20]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/persistence/workers.py#L479-L527
[S21]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/controler/workflow/coordinator.py#L505-L520
[S22]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/worker/execution/lifecycle.py#L347-L404
[S23]: https://github.com/b1indsight/agentflow/blob/086e68efe55403e2c7c66b23771e2db3cf78bcbd/app/worker/state/record.py#L114-L128
