# Session 事件驱动的双端统一 — Feature record

**Stage:** Review — 实现、本地全量与消融验证完成；review gate 与发布的最新结果见 PR 和验收摘要。
**PR:** [#127 — 沿用同一方案/实施 PR](https://github.com/b1indsight/agentflow/pull/127)
**Bookmark:** `refactor/session-event-architecture`
**Baseline:** [main / PR #125 合并版本](https://github.com/b1indsight/agentflow/commit/086e68efe55403e2c7c66b23771e2db3cf78bcbd)

**当前修订：**可关联的 payload 校验失败返回 `tool_result`，沿用原 method，`reply_to` 指向被拒项，
`error` 仅含固定 `code` 和 `message`，不含 details、truncated 或任何诊断内容
（[修订 005](#005-错误响应不暴露校验详情)）；不新增错误通知路由。该 wire 合同已实现为 v17，持久格式不变
（[修订 004](#004-用-tool_result-返回消息验证错误)）。消息方法按 `包名.方法名` 精确路由，所有业务回调对象统一用
`register(route, model, callback)` 注册（[修订 003](#003-统一包名方法名路由与注册)）。
两端业务回调统一命名为 `WorkerCallbacks` / `ServerCallbacks`，模块均用 `callbacks.py`
（[修订 002](#002-两端业务回调对称命名)）。用户已要求去掉 `Delivery`；Session 直接交付既有 msg，接口为
`next_event(...) -> Incoming | Ready | None`。字节计数与剩余 payload 留在 Session 内部。
[修订 001](#001-去掉-delivery直接交付-msg) 撤销初稿中的 Delivery 类型、队列结构及相关流程。
下方 Initial plan 保留为历史基线，其中被后续修订替代的内容不再适用。实际实现与验证见时间线 006–008。

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
