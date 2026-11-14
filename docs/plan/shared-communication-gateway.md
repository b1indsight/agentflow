# Worker / server 公共 Gateway（第三阶段）— Feature record

**Stage:** PR review — 实现与本地验证完成，独立代码评审门禁通过
**PR:** [#124](https://github.com/b1indsight/agentflow/pull/124)
**Bookmark:** `refactor/shared-communication-gateway`
**Workspace:** `.amp/in/workspaces/unified-gateway-plan`（独立 jj workspace）

> **当前审阅入口：§2.3–2.5 实施记录，[R2 修订](#r2) 为获准结构。**
> WorkerGateway 已删除，不引入 WorkerEndpoint、WorkerRun 或 enter/run/finish；
> 两端使用同类 Gateway / Session，Runtime 装配完全对称后置。
> §1、R1、R2 保留历史。§1.8 关闭适配已获准并实现，§2.5 记录回归及真实 Uvicorn 验收，
> 取代 §2.3 的阻塞状态。所有改动均来自独立 workspace，已提交 PR；尚未合并或部署。

## 1. Initial plan

### 1.1 目标、基线与范围

两端组合相同的进程级 `Gateway` 与连接级 `Session`：worker 只连接一个配置的 server，
server 管理多个 worker。Gateway 统一 peer/session 登记、查找、替换、路由和通信关闭；
拨号/接受连接、认证和业务驱动保留角色差异。

本阶段重新执行 `jj git fetch --remote origin`，并用 GitHub commits API 交叉核对：最新
`main@origin` 为 [`cc796c6`](https://github.com/b1indsight/agentflow/commit/cc796c62a5b3c93711314263ba0a77688c4b3f86)。
已包含 [#120](https://github.com/b1indsight/agentflow/pull/120) 与
[#122](https://github.com/b1indsight/agentflow/pull/122)。本 workspace 的父版本即该提交；
不采用根 checkout 的旧 detached HEAD，不编辑根目录的 Web 工作及其他线程文件。
jj workspace 没有独立 `.git`，版本确认以该 workspace 的 jj revision 为准。

前序依据：[WorkerExecution](worker-execution-lifecycle.md)、[公共 Session](shared-communication-session.md)。
讨论来源：[来源 thread](https://ampcode.com/threads/T-01a16fd3-4a30-7198-9401-c27c98620c27)。
本阶段记录与证据：[规划/实施 thread](https://ampcode.com/threads/T-01a18390-31fc-7772-ac25-fbadec873770)。

**包含：** 公共 Gateway、两端组合接入、删除重复路由表、连接替换/退出合同、runtime 接线、
相应行为回归与当前架构文档。已知正常断连 close 异常的窄修复列为明确待批项（§1.8）。

**不包含：** wire v15、journal v4、DB、envelope 或方向白名单变更；统一消息协议、可靠领域
事件/outbox、自动重试、跨进程 Future 恢复、消息总线、多 server/HA、通用 handler 框架、
新的业务任务 supervisor、全局写超时、ASGI 强制退出重构、UI 改动、部署或真实 GitHub 业务操作。

### 1.2 当前职责与实际重复

| 当前代码 | 已核实的职责与问题 |
| --- | --- |
| server `WorkerGateway._sessions` | 按 worker_id 保存 `(wake, Session)`，表示最新认领者；替换时先覆盖，再 await 旧连接关闭，最后重查实例。[S1] |
| `ConversationBroker.connections` | 又按 worker_id 保存 Session，表示可路由连接；由 Gateway attach/detach。替换存在两张表同步和旧 finally 误删新路由的风险，当前实现已有实例检查。[S1][S2] |
| worker `WorkerClient` | 没有第二套 peer 字典。当前连接生命周期由 `_connection_task`、串行重连循环和 `run_connection` finally 隐式管理。[S3] |
| 公共 `Session` | 已拥有单 reader、有界 FIFO、串行发送、临时查询关联及通信任务清理；不应再复制到 Gateway。[S4] |
| 进程退出 | worker 已先 join 通信，再 shutdown execution，最后释放 root lock；server 的 runtime 未关闭通信 registry，ASGI handler 生命周期由 Uvicorn 管理。[S5][S6] |

本次最直接的去重是合并 server 的权威登记与查询路由；worker 接入同一核心后，显式得到
单 peer 准入、停止后拒绝新连接登记及统一 Session 清理。不会声称两端原来各有一套相同 registry。

例如 A 是旧 worker 连接，B 正等待 A 关闭，C 又接入：C 允许先于 A 完成关闭而成为当前连接；
B 后续醒来不能更新路由或执行 `worker_connected`。当前已有这一行为回归。[S7]

### 1.3 成功标准与保留的业务合同

1. 两端实例化同一个 `app.communication.gateway.Gateway`；server 的 Broker 不再持有自己的
   peer→Session 表，worker 当前只用一个本地 peer key。
2. 同 peer 只发布一个当前 Session；候选替换、旧 finally、取消、关闭错误和进程 seal 均不能
   重新发布旧连接、误删新连接或遗失仍需关闭的 Session。跨 peer 替换不互相等网络 close。
3. Session 仍是一条物理连接/epoch：唯一 reader、32 条 / 16 MiB FIFO、串行实际发送、
   四个临时请求、四个查询 handler 与既有 busy 上限不变；不新增第二个 reader 或请求表。[S4]
4. worker 旧协议消费者、心跳和观察者完整 join 后，新消费者才可借用同一个 WorkerExecution。
   原执行 Task、工具 call_id/Future、离线完成收集、shielded stop 均不随 Gateway/Session 销毁。
   “跨重连保留”仅指同进程，不把内存 Future 写入 journal。[S5][S8]
5. `record_received → await Started 实际发送 → record_started → 创建执行 Task` 不变。
   恢复/停止可以在 Registered 前发生；直接 ResumeAssignment/ReplayResult 仍合法。
   Gateway 发布不授予业务执行权限，也不等于 `connected=True`。[S3]
6. server 持久接收结果、完成应用并核验提交后才发 ResultAccepted；StopStore.confirm 成功
   后才发 StopAccepted。worker ACK 仍由原 owner 应用；关闭不释放 Flow binding。[S9]
7. 临时查询固定在查到的原 Session，断开/替换即失败，不跨连接重放。工具/结果/停止 ACK
   不进入临时 reply 机制；所有业务回复固定回原连接，不按 peer 重新路由。

### 1.4 提议结构与模块

```text
ServerRuntime                          WorkerRuntime
  ├─ Gateway[WorkerMessage,              ├─ WorkerExecution → ExecutionState
  │          ServerMessage]             └─ WorkerClient（角色拨号/协议驱动）
  ├─ WorkerGateway（认证/调度/业务）          └─ Gateway[ServerMessage,
  │    └─ 同一 Gateway                         WorkerMessage]
  └─ ConversationBroker                       └─ Session（至多一个当前 server）
       └─ 同一 Gateway
            └─ Session × worker

Gateway：进程登记/当前路由/替换/通信关闭
Session：单连接 reader/FIFO/write/request/查询分派
业务 owner：执行、journal、Flow binding、提交与恢复
```

| 文件 | 拟改动 |
| --- | --- |
| 新增 `app/communication/gateway.py` | Gateway、Registration 及纯通信生命周期逻辑；只依赖标准库、Session 和既有 Message 上界。 |
| `app/communication/session.py` | 核心机制不改；必要时补充 Transport close 和调用方生命周期的合同说明。 |
| `app/controler/workers/gateway.py` | 组合公共 Gateway，保留鉴权、接受连接、epoch、调度、工具/结果/停止/恢复驱动；引入窄 server transport 适配器（待批）。 |
| `app/controler/workers/conversation.py` | 注入同一 Gateway，删除私有路由表与 attach/detach；保留 64 全局准入、30 秒总预算、页面校验和错误映射。 |
| `app/controler/runtime.py` | 创建/共享 Gateway；shutdown 封闭准入并关闭其 Session，保持 ASGI 与业务任务所有权边界。 |
| `app/worker/transport/client.py` | 保留 WorkerClient 名称、拨号/退避和协议驱动；持有公共 Gateway，显式登记/退休单 server Session。 |
| `app/worker/runtime.py` | 不搬业务循环，不重构 owner；若组合无需改变则不机械编辑。保持健康判断与关闭顺序。 |
| 测试及当前文档 | 更新构造接线与已失效的私有 registry 断言；补核心行为及关闭回归；同步 architecture/components、overview、conversation-api 和必要运维说明。历史计划不批量改写。 |

保留角色类名能减少无关迁移；公共模块和字段明确区分核心 Gateway 与 server 业务 WorkerGateway。
不引入继承基类、旧 facade、兼容导出或 fallback 收发路径。

### 1.5 新核心类型：字段与精确接口

以下为待审批 API，尚未实现。泛型采用仓库已有 Python 3.12+ 语法，Incoming/Outgoing 的
上界为 `Message`；两端分别使用现有方向联合类型，不使用 Any、cast 或字符串 handler registry。

**`Registration[Incoming: Message, Outgoing: Message]`** 是 `dataclass(eq=False)`：按实例身份
比较和哈希，供所属 Gateway 与角色 finally 标识一次登记。它不是 wire Register，也不是数据库租约。

| 字段 | 类型、含义 |
| --- | --- |
| `peer_id` | `str`，server 为已认证 worker_id；worker 固定为本地 key `"server"`。不上 wire，不使用 URL/token 作 key。 |
| `session` | `Session[Incoming, Outgoing]`，唯一的原连接对象；epoch 仍只在 Session。 |
| `_published` | `bool = False`（不作为构造参数），只由 Gateway 修改；替换前任的通信关闭及当前性复核完成后置真，不代表握手/业务 ready。 |

调用方只读 peer_id/session，不构造或修改 Registration。构造形状为
`Registration(peer_id: str, session: Session[Incoming, Outgoing])`；不新增 released 标志、
前任链、Task、Future、wake 或 epoch 副本。

**`Gateway[Incoming: Message, Outgoing: Message]` 的全部字段：**

| 字段 | 类型、含义 |
| --- | --- |
| `_current` | `dict[str, Registration[Incoming, Outgoing]]`，唯一按 peer 索引的权威登记，允许尚未发布的候选。 |
| `_owned` | `set[Registration[Incoming, Outgoing]]`，所有未退休登记，包括候选、当前和已被替换但尚未清理的连接；不是第二张路由表。 |
| `_sealed` | `bool = False`，进程通信准入/查找已封闭；不可重新打开。 |
| `_close_task` | `asyncio.Task[None] \| None`，唯一的进程级 Session 关闭任务；不持有协议消费者或业务 Task。 |

| 精确签名 | 责任 |
| --- | --- |
| `def __init__(self) -> None` | 初始化字段；不创建 Task 或绑定事件循环，允许 Runtime 在循环外装配。 |
| `async def register(self, peer_id: str, session: Session[Incoming, Outgoing], *, replace: bool) -> Registration[Incoming, Outgoing]` | 预留、必要时关闭直接前任、复核并发布。server 明确传 True；worker 明确传 False。 |
| `def get(self, peer_id: str) -> Session[Incoming, Outgoing] \| None` | 只返回未 sealed、当前、已发布、未 closed 的 Session；否则 None。不判断 epoch/业务 ready。 |
| `async def unregister(self, registration: Registration[Incoming, Outgoing], code: int = 1000) -> None` | 撤回可路由性，完整 close/join 此 Session，再按实例身份退休登记；重复调用幂等。 |
| `def seal(self) -> None` | 同步拒绝新登记并让查找返回 None；不取消业务、不自行启动网络清理。 |
| `async def close(self, code: int = 1000) -> None` | seal 后关闭所有 owned Session，受保护地 join；首个进程关闭调用决定其默认 code。 |

私有职责仅需：

```python
def _retire(self, registration: Registration[Incoming, Outgoing]) -> None: ...
async def _close_owned(self, code: int) -> None: ...
```

`_retire` 按身份删除 current 并 discard owned；只在该 Session 通信清理已结束后调用。
`_close_owned` 快照 owned、并发 join 所有 Session.close、检索并汇总异常。
不加 `is_current` 转发：调度可用 `peers.get(reg.peer_id) is reg.session`。
不加 `send(peer, ...)` / `request(peer, ...)`：调用方一次 get 后持有 Session，直接使用已获批的
Session API，避免构造 payload 的 epoch 与实际发送连接不同。

### 1.6 替换、路由与取消的精确合同

**所有 registry 操作限单一事件循环。** 状态检查/修改均在无 await 的同步段中完成；不保留
现有 `_registry_lock`。当前两段锁内本身也没有 await。[S1] 跨线程通知仍走既有 loop 调度。

`register` 顺序：

1. 第一个 await 前检查 sealed、Session.closed、Session 是否已被登记；拒绝重复登记同一
   Session 或跨 peer 复用。`replace=False` 时，只要该 peer 还有任何 owned 登记就拒绝，
   包括正在退出者；不能仅检查当前路由。sealed/closed 抛 `SessionClosed`，重复 Session 抛
   `ValueError`，非替换模式的重叠消费者抛 `RuntimeError`；不新增异常类或准入配置框架。
2. 同步捕获直接前任，建立新 Registration，写入 current/owned。新候选 `_published=False`，
   替换窗口内 `get(peer)` 返回 None；不回退到旧连接。
3. 若有直接前任，await 其 `Session.close(1012)`。仅保留既有 `ConnectionError` 容忍；
   框架特定正常断连由角色 adapter 处理。其他异常和取消不吞掉。
4. await 后重新核验 current 仍是该 Registration、未 sealed、Session 未 closed；否则抛
   `SessionClosed`。成功时同步发布并返回，然后角色才执行 worker_connected/握手等业务。
5. 预留后任意失败/取消都关闭候选并按身份退休，不恢复前任。清理不能因第二次取消而提前
   放弃 join；原异常及清理异常必须可观察，必要时保留 cause/context 或汇总，不能 `except Exception: pass`。
   预留前被拒绝的新 Session 仍由创建方 finally 关闭；已登记对象的重复提交属于调用方错误。
   候选失败清理沿用默认 1000，不额外改成 1012；已被后继替换而开始关闭的 Session 保留首个 1012。

**A→B→C：** B 等 A；C 只等它直接取代的 B，允许先完成登记。A/B 仍在 owned 中。
A 完成后 B 复核失败，不能发送 Registered 或覆盖 DB epoch。任何旧 unregister 都不能移除 C。
C 若先退出，A/B 也不会重新成为 current。这延续已有 latest-wins 合同，不承诺 server 同 peer
瞬间只有一个尚未完成关闭的物理 socket。[S7]

**`unregister`：** 先同步置该登记 `_published=False`，但在 close/join 完成前保留其 current
身份（若它仍是 current），使此时到达的新登记仍能等待这个直接前任。随后 await Session.close，
最后无论成功/异常/取消都执行身份安全的 `_retire`。已被其他登记取代时绝不改动替代者。
已经退休的重复 unregister 不关闭别的 Gateway 的对象，也不恢复旧路由。

**`close`：** 同步 seal；一个受 shield 保护的清理 Task 关闭 owned 快照，包含当前、候选、
被替换但尚未退休的 A/B。seal 后没有新预留，因此快照不会遗漏后来加入的已接纳连接。
使用 collect-all 的 join，单个 close 错误不能跳过其余连接；全部完成后用 Python 3.12 支持的
ExceptionGroup 汇总普通异常。反复取消外层仍等通信清理结束，再传播取消；已发生的关闭错误
仍须检索并保留上下文，不成为未检索 Task 异常。Session 自己已有的首个 close code 优先。

进程 close 完成只表示所有登记 Session 的通信任务已 join。它不提前退休角色仍持有的登记；
角色 finally 的 unregister 负责退休。表中可能暂留 closed 的登记，但 get 恒为 None。
不因一次 Session.close 失败就重新创建底层清理任务。

**调用边界：** register/unregister/close 由角色驱动或 Runtime 调用；不能在 Session 自有的
reader、responder、busy sender 或 write Task 内回调这些关闭入口，避免它们等待自身所属
Session 的 join。公共 Gateway 不把自己注入 Responder，不增加循环等待的 callback。

### 1.7 两端接入与进程关闭

**server：** ServerRuntime 创建 `peers: Gateway[WorkerMessage, ServerMessage]`；
`WorkerGateway.__init__` 用同类型的 keyword-only `peers` 取代 `conversations`，其余参数不变。
`ConversationBroker.__init__(self, peers: Gateway[WorkerMessage, ServerMessage]) -> None`
保存 `_peers`；其他业务字段不变。

WorkerGateway 的调整后构造签名：

```python
def __init__(
    self, coordinator: JobCoordinator, *, worker_tokens: Mapping[str, str],
    heartbeat_interval: float = 15.0, heartbeat_timeout: float = 45.0,
    poll_interval: float = 2.0, metrics: Metrics | None = None,
    database: Database, tools: FlowToolService, results: ResultApplication,
    peers: Gateway[WorkerMessage, ServerMessage],
) -> None: ...
```

- 删除 WorkerGateway 的 `_sessions`、`_registry_lock`、`_register_session`、`_unregister_session`
  和 `conversations` 属性；删除 Broker 的 `connections`、`attach`、`detach`。
- WorkerGateway 保存 `peers` 和
  `_wakes: dict[Registration[WorkerMessage, ServerMessage], asyncio.Event]`。
  wake 是调度数据，不按 peer 索引 Session。notify_jobs 只唤醒仍被 get 返回的原 Session。
- 认证 → accept → 首个 Register 身份校验 → 构造/启动 Session →
  `await peers.register(worker_id, session, replace=True)` → 同步登记 wake → 原 worker_connected、
  stop/recovery/Registered/调度流程。查询发布时机不推迟到 Registered 之后。[S10]
- handler finally 调用 unregister；尚无 Registration 时仍直接关闭候选 Session 或原 socket。
  嵌套 finally 始终清 wake，并在已 connected 时执行带原 epoch 的 worker_disconnected。
  关闭错误不跳过 DB 断连记录；DB 错误也不能留下核心登记。
- Broker 一次 `get(worker_id)`，用这个 Session 的 epoch 构造查询并 request。空路由或未确认
  epoch 映射 worker_unavailable。替换窗口恰逢全局 64 满额时，新顺序优先 unavailable，
  旧代码可能优先 busy；记录这一错误优先级差异，不改任何响应格式。[S2]

**worker：** WorkerClient 新增 `_peers: Gateway[ServerMessage, WorkerMessage]`，内部构造，
不新增 server_id 配置或构造参数。每次 run_connection 的 Session 先以固定 `"server"`、
`replace=False` 登记，再发 Register，之后启动 reader/运行原握手；epoch=None 的初始状态合法。
原 `_run_connection` finally 先 join 心跳/assignment 驱动及观察者，外层才 unregister。
重连循环仍串行；不引入并行 worker Client 热替换管理 API。

WorkerClient.stop 同步 seal、置 stop/connected，再沿用对连接任务的取消。停止若落在拨号
握手期间，后续登记被拒绝，不能继续发送 Register；这是关闭边界收紧，待审批后补确定性回归。
不声称 seal 会立刻取消尚未完成的底层 dial，也不新增拨号超时。run 最外层 finally 在 join
连接任务后调用 Gateway.close；外层执行 owner 关闭和 root-lock 顺序完全保留。[S3][S5]

**server 进程：** Runtime.shutdown 入口 seal，再按原顺序停止并 join 自己的后台业务任务，
之后 `await peers.close(1012)`；以嵌套 finally 保证无论通信关闭结果如何，仍调用 access.close
和 database.close，并传播错误。它不 cancel/join ASGI socket handler 或它们的工具/结果 I/O。

正常启动路径在 CLI 已配置 Uvicorn 20 秒 graceful shutdown；已核对安装版本与 uv.lock 一致：
Uvicorn 正常先停止 accept、关闭连接并 drain ASGI tasks，然后发送 lifespan shutdown，后者才
调用 Runtime.shutdown。[S11][S14][^dependency-source] 因此正常路径 DB 释放晚于 handler 清理。

**明确保留的限制：** Uvicorn 超过既有 20 秒后只取消 ASGI tasks，再进入 lifespan；其代码没有
在该分支显式完整 join 所有被取消的 handler。Gateway.close 只 join Session 通信，不能保证
超时 handler 的 DB finally 在 pool.close 前结束。force-exit 不保证运行 lifespan。Register 前
的原 socket 也仍归 ASGI。直接调用 Runtime.shutdown 不构成新的 ASGI drain API。本阶段记录
这些既有边界，不改变强制退出策略、不承诺任意慢业务的优雅停机。[S11][^dependency-source]

### 1.8 已知 low：正常断连后的 close（待审批纳入）

最新 main 的 `WorkerGateway.handle` 在捕获 WebSocketDisconnect 后，finally 仍执行
Session.close；Session._finish_close 直接调用底层 close，且只在 finally join 通信任务。
该路径尚未修复。[S12] 使用安装的 Starlette/Uvicorn 方法、模拟已断开的 transport，已观察到
正常断开码 1000 后 close 再抛 WebSocketDisconnect(1006)。这不是 live socket E2E。
嵌套 finally 仍会运行断连/注销，不能由此推断 Flow binding 清理丢失。[^close-probe]

**建议本阶段获批后纳入最小修复：** server 侧新增 `_SocketTransport`，唯一字段
`socket: WorkerSocket`，构造及方法为：

```python
def __init__(self, socket: WorkerSocket) -> None: ...
async def send_text(self, data: str) -> None: ...
async def receive_text(self) -> str: ...
async def close(self, code: int = 1000) -> None: ...
```

send/receive 透传；仅 close 将 WebSocketDisconnect 视为 peer 已离开，正常完成。
不吞 RuntimeError 或任意 Exception，不向公共包导入 Starlette。不修改 pre-Session 原 socket
失败路径；若那里验证出独立问题，另报范围与审批。worker 既有 websockets.close 已幂等。[^dependency-source]
Session 仍只创建一个底层 close Task，并保留实际 join 行为。

这项建议还使公共替换代码保持原 `ConnectionError` 容忍而无需依赖框架异常。
此前 low 建议不是本阶段修复授权；本文件明确把适配器处理列入待审批内容。
若用户要求保留该 low，需先修订异常适配方案，不能直接移动代码造成正常替换回归。

### 1.9 失败矩阵与业务边界

| 场景 | 拟定结果 |
| --- | --- |
| A→B→C 重叠，B 晚醒 | C 保持 current；B 失败并清理，不登记 DB epoch/发业务消息。 |
| 候选注册被取消/旧 close 出错 | 完整清理候选，只退休自己；不恢复旧路由、不吞非预期错误。 |
| 旧 finally 在新连接后执行 | 只移除旧 Registration；DB 断连记录仍按 epoch fencing。[S13] |
| worker 重叠连接消费者 | replace=False 在碰到执行 owner 前拒绝；调用方关闭新候选，不取消原 owner。 |
| 正在替换时进程 close | seal 后候选无法发布；owned 包括已被覆盖者，全部通信清理被 join。 |
| 单个 close 抛错或关闭调用被反复取消 | 其他 Session 仍被清理；全部完成后报告错误/取消，不中断 owner 的 shielded stop。 |
| 路由已固定在 A 后发生替换 | 请求仍属于 A，失败而不改投 B；迟到 reply 不完成新 Session 的请求。 |
| 早到 query / StopExecution / ResultAccepted | 不把 Gateway 发布当 Registered；沿用第二阶段合法握手与 epoch 确认顺序。 |
| 结果/工具/停止正在慢 I/O | Gateway 仅封闭通信；已开始的 server 业务仍由角色/ASGI 管，原提交检查决定 ACK。 |
| 协议错误、读失败、有界缓存溢出 | 保留原 Session 与角色处理；Gateway 不改写为成功/重试或生成持久事实。 |

### 1.10 方案比较、成本与取舍

| 方案/机制 | 选择理由及代价 |
| --- | --- |
| 组合公共 Gateway，保留角色类 | 删除实际重复 registry/route，而不重写两端不同的业务驱动；代价是须明确核心与业务类的命名区别。 |
| Registration + current/owned | 一个 peer 索引负责路由；owned 必须覆盖被覆盖但未退休的连接，支持进程关闭。无额外 pending Future 或第二个 epoch。 |
| register 内部完成预留/发布 | 少于公开 attach/publish 两阶段 API；调用方无需维护候选状态。异常/取消清理须严格实现。 |
| latest-wins，无网络锁 | 保留现有 C 越过慢 A 的行为；代价是短时存在多个 closing Session。逐 peer 串行关闭会改变此合同且可能长期阻塞同 peer 重连，暂不采用。 |
| get 后固定 Session | 直接复用既有 request/send；避免 Gateway 重查路由造成 payload/epoch 或 ACK 目标不一致。 |
| Gateway.close 只关通信 | 给进程一个完整的 Session 清理入口，不引入业务 supervisor；ASGI 超时退出限制继续存在。 |
| 窄 server transport adapter | 规范化已断连 close，与中立核心边界一致；比注入任意可忽略异常列表更容易审核。需显式批准已知 low 的处理。 |

主要成本是新增一个小通信模块、构造接线迁移，以及注册取消/进程关闭交错的验证。
不承诺本阶段降低总体行数；成功以一个路由事实源和行为合同为准。owned/wake 保留到角色退出，
不声称可强制终止永不退出的业务 handler，也不新增历史读取线程的进程级限额。

### 1.11 获批后的实施顺序

1. 确认文档发布授权，用本 feature record 和一个 bookmark 创建 docs-only Draft PR；计划审批
   前不写产品/测试代码。实现沿用该 PR，不先合并计划、不另开实现 PR。
2. 明确批准本方案及 §1.8 处理范围后，再 fetch 核对相关漂移；有实质冲突先更新计划。
3. 实现公共 Gateway 的登记/查找/替换/通信关闭；通过公共接口验证关键生命周期合同。
4. server 接入：窄 transport adapter、唯一 Gateway、Broker/调度接线，删除重复字段/方法；
   保留工具、结果、停止业务顺序，并迁移现有构造调用方。
5. worker 接入：单 server 登记、seal、连接退休与进程 close；验证借用 owner 的串行边界。
6. 执行下面的定向、双端、真实 PostgreSQL 全量、静态/构建与简化对照；同步当前文档和本记录。
7. 代码通过仓库 review gate 后才按授权更新同一 PR；后续发布/合并/部署状态另行报告。

无数据迁移、双写、wire 版本切换或旧兼容壳。回滚是内部代码回滚，但必须沿用既有进程停止/
journal 恢复规则，不能把没有执行 Task 的普通 started 编码任务盲目重跑。

### 1.12 验证与简化对照（全部为实施验收计划）

优先复用/扩展既有公共行为测试；不为每个 helper、字段或异常矩阵行新增独立永久测试。
新 Gateway 合同在 `tests/test_communication_gateway.py` 以少量参数化场景覆盖；其他类名与
构造迁移不额外增加实现细节测试。异步交错用确定性事件屏障，不靠随机 sleep。

| 验收 | 可观察事实与起点 |
| --- | --- |
| 双端同核心与多 peer | 双向 Session 测试叠加 Gateway 登记/路由；两 worker 查询互不串线，worker 第二消费者拒绝；真实消息方向不变。 |
| 重叠替换 | 原 `test_overlapping_replacements_do_not_republish_old_route` 改由公共 get 查询；C 先于 A close 完成登记，B 无 Registered/DB 覆盖；旧 finally 不删 C。[S7] |
| 候选/退休/关闭竞态 | 注册取消、close 错误、C 先退出、seal 同轮、pending 期间 close；下一合法查询确实能成功，无旧路由复活。 |
| 进程全量关闭 | A/B 被 current 覆盖仍被 join；多个 peer 中一个 close 抛错，其余仍关闭；反复取消不提前返回，不留未检索 Task 异常。 |
| worker owner 不受通信影响 | 继续原 `test_worker_client` 完整 Client 替换、原 call_id/Future、离线落盘、发送前取消/已发迟到回复；stop 两个清理断点和真实子进程/root lock 回归保留。 |
| 停止期间迟到拨号成功 | stop 发生在连接建立屏障前；释放后不发 Register、不借用 execution，最终通信完整退出。 |
| 历史代理 | `test_conversation_proxy` 的 64/4 限额、30 秒预算、取消/超时、页面校验；`test_conversation_e2e` 的慢 preparation/tool/result 下查询继续完成。 |
| 恢复和 ACK | Stop/ResultAccepted 早于 Registered、直接 Resume/Replay；未提交结果不 ACK、未确认 stop 不释放 binding；archive/recovery/stop/remote E2E 均实际运行。 |
| 正常断连 close（待批） | 在未改动代码先复现，再以真实 Uvicorn + websockets 本地连接正常断开，验证无该 ASGI 清理异常；保留 handler 的 DB 断连记录和通信 join 断言。TestClient/no-op close 单独不足。 |
| server 进程边界 | 正常 ASGI drain 下 handler 完成后 DB 才关闭；Runtime 的通信关闭抛错仍释放 access/DB。强制/超时分支明确验证限制，不伪称有新的业务 join 保证。 |
| 导入和包内容 | 扩展 fresh-interpreter import 到公共 Gateway；沿用已有 AST 规则，不重复静态边界用例；公共层不加载 worker/controler/persistence 或 WebSocket SDK。wheel 含新模块。 |

实现期检查顺序与命令（在本独立 workspace、隔离依赖/测试库运行）：

- 定向运行公共通信、Gateway、worker、conversation、stop、runtime/边界测试；保留真实业务断言。
- 使用隔离 UTF8 PostgreSQL、loopback TCP，设置 `FLOWTOOL_TEST_DATABASE_URL` 后运行
  `uv run pytest -q`；数据库用例必须实际执行，skipped 不能当验收通过。
- `uv run ruff check app tests`、`uv run mypy app tests`；对变更文件检查格式，与父版本区分
  既有整文件排版差异，不借本批重排全仓。
- 按 CI 执行 `python scripts/build_console.py`、`uv build --no-sources`，核对 wheel 模块与
  `app/controler/console_static/index.html`；不编辑其他线程的 Web 代码。

**实现后、自动代码审查前做临时简化/消融对照：** 相同输入、一次只改一个机制，变体不提交。

1. 去掉 await 后当前性复核：延迟 B 应错误发出注册/覆盖 epoch；控制组必须拒绝。
2. 去掉候选发布门槛：替换窗口应错误暴露 B 路由；控制组在该窗口返回 unavailable。
3. 仅关闭 current、移除 owned 跟踪：A/B 隐藏的 blocked close 应被遗漏，进程关闭提前返回；
   控制组必须等它们的通信清理。若更简单表示能保留合同，先简化再审查。
4. 去掉身份安全退休、改为按 peer 无条件删除：旧 finally 应使新连接查询失败。
5. 去掉 worker seal 或 replace=False 准入：分别应出现停止后 Register 或重叠借用；控制组拒绝。
6. 若批准 §1.8，移除 close 的窄异常规范化应重新出现已复现的断连异常；不扩展成全异常忽略。

### 1.13 本轮实际核查与 Oracle 取舍

本轮只新增该计划；未修改实现或测试。重新 fetch、创建独立 workspace、读取前两阶段记录、
当前两端 Session/业务驱动、runtime、调用方、fencing、测试与 CI。运行现有公共 Session、
Gateway、worker client、conversation worker 与边界测试，全部通过且无跳过。测试从独立
workspace 加载源码，解释器为本机 Python 3.13。未运行本阶段 PostgreSQL 全量、静态检查、
构建或真实网络 E2E；前两阶段通过记录均不作为第三阶段验收。

正常关闭风险另用已安装依赖做方法级复现，未改任何源码；其范围与局限见 §1.8。
日志保存在本 thread 的 `.amp/in/artifacts/unified-gateway/`，不加入 PR。

按 consulting-claude-oracle 使用本机限定 `consult.sh`：先基于编号源码生成初稿，再补充实际
Uvicorn shutdown、CLI 20 秒配置和依赖源码，集中澄清进程 close 边界。两次均核验初始化及
实际响应为 `claude-opus-5-5`、tools/mcp_servers 为空、result success、进程退出 0。
未启用工具或扩展资料访问；原始输入/事件流不提交。Oracle 建议不是执行验证。

主线程核对后的取舍：采用组合、固定 Session 路由、latest-wins、显式准入和窄 adapter；
合并公开 attach/publish，去掉 Lease/_Peer 层级、前任链和 released 副本。保留只关闭通信的
Gateway.close，拒绝初稿把它等同于业务 supervisor 的推论；拒绝后续建议的任意 Exception
吞掉策略。unregister 在完成 close 前保留未发布的 current 身份，维持直接前任的 join 边界。
协议/存储不改，ASGI 强制退出限制明确保留。

### 1.14 审批事项、实现期问题与延后工作

**需要明确审批：**

1. 采用上述公共 Gateway/Registration、单一 peer 路由、latest-wins 替换和只关通信的进程
   close；保留角色类名和所有业务所有权。接受替换不可用优先于全局 busy 的小范围错误优先级变化。
2. 将 §1.8 的 server close 窄适配纳入本阶段；此前报告不是授权，本轮也尚未修复。
3. 接受 worker 停止后不再登记/发送 Register 的边界收紧；server 仍沿用 ASGI 的既有
   20 秒 graceful/强制退出策略，本阶段不接管业务 handler 的关闭。
4. 确认 docs-only Draft PR 发布权限，以及是否在方案明确获批后开始实现。发布计划与
   批准实现是两个动作，均未由当前本地计划自动获得。

没有尚未查清且必须先做实现才能决定的架构问题。确定性测试装配、局部 helper 排列和日志
格式可在实施时处理；若需要改变以上 API/所有权合同或修复范围，先在时间线追加修订供审批。
统一消息协议、可靠领域事件/outbox、历史读取线程的进程级预算及强制退出 DB/handler 完整
join 仍是后续工作，不以本计划记录它们为实施授权。

## 2. PR change timeline

<!-- 初始方案共享后，仅追加真实的需求修订、实质设计决定和实施结果；不记录单纯审批。 -->

<a id="r1"></a>

### 2.1 R1 — 删除 server 专用 WorkerGateway（2026-10-29，UTC+08）

#### R1.1 需求、状态与覆盖范围

用户明确要求去掉 `WorkerGateway`，本次只修订计划。拟删除该类及
`app/controler/workers/gateway.py`，不保留别名、兼容导出、`ServerGateway` 替身或仍包办
认证/连接生命周期/调度的改名类。两端唯一的 Gateway 实现为 `app.communication.gateway.Gateway`。

本条覆盖 §1.4 的 server 结构、文件表与保留类名理由，§1.7 的 server 构造/wake/清理接线，
§1.10 首行、§1.11 第 4 步、§1.12 中旧构造入口，以及 §1.14 第 1 项的“保留角色类名”。
§1.5–1.6 的公共核心 API/替换合同、所有业务所有权、worker 接入和 §1.8 待批状态不变。
本次没有要求移除 WorkerClient；它继续承担 worker 拨号、重连和协议驱动。

现有 WorkerGateway 同时持有四个 Store、token、会话表与 Broker，handle 又包办接入和业务
循环，直接改名会保留原职责混合。[S18][S10][S12] 本次以真实调用边界拆开，并明确关闭后的
DB 清理由谁执行。以下均为待实施提议。

#### R1.2 职责与文件

| 模块 / 对象 | 负责 | 明确不负责 |
| --- | --- | --- |
| `app/communication/gateway.py` / `Gateway` | 单一 peer/session 登记、get、替换、seal、通信关闭；沿用 §1.5–1.6 | 认证、业务调度、在线状态 DB、业务 Task |
| 新 `app/controler/workers/endpoint.py` / `WorkerEndpoint` | 认证、accept、首个 Register、epoch、Session 装配、调用公共登记/关闭；原错误到 close code/ProtocolError 的适配；按顺序调用业务生命周期 | Store/Flow 持久化、结果应用、wake 集合、另建路由表 |
| 新 `app/controler/workers/handler.py` / `WorkerHandler` | worker 在线/断连记录、调度、preparation、恢复、工具、结果、停止；业务 wake 与 notify_jobs | token/accept、登记或替换 Session、socket/Session/Gateway 关闭 |
| `ConversationBroker` | 从同一 Gateway 取一次 Session，固定该连接查询；保留限额与结果校验 | 私有 connections、attach/detach |

`WorkerSocket` Protocol 移到 endpoint，接口不变。§1.8 的 `_SocketTransport` 若获批，也放在
endpoint；接口和只处理 close 的 WebSocketDisconnect 范围不变。`ProtocolViolation` 移到
handler。删除旧 gateway 模块，修改全部导入方；历史 feature record 保留原文。

```text
ServerRuntime
  ├─ gateway: Gateway[WorkerMessage, ServerMessage] → Session × worker
  ├─ worker_endpoint: WorkerEndpoint → 同一 Gateway + WorkerHandler
  ├─ worker_handler: WorkerHandler → 业务 Store/service + Gateway.get
  └─ ConversationBroker → 同一 Gateway.get

WorkerRuntime → WorkerClient → Gateway[ServerMessage, WorkerMessage] → Session ≤ 1
              → WorkerExecution → ExecutionState（原长期 owner 不变）
```

#### R1.3 新角色 API 与字段

`WorkerRun` 是一次 server 业务消费者的临时上下文；由 handler 创建和退休，不是第二个
Session、连接登记或 worker 执行 Task。`WorkerRecord` 沿用现有领域类型。[S20]

```python
@dataclass(eq=False)
class WorkerRun:
    worker: WorkerRecord
    session: Session[WorkerMessage, ServerMessage]
    wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)

class WorkerHandler:
    def __init__(
        self, coordinator: JobCoordinator, *,
        peers: Gateway[WorkerMessage, ServerMessage], database: Database,
        tools: FlowToolService, results: ResultApplication,
        heartbeat_interval: float = 15.0, heartbeat_timeout: float = 45.0,
        poll_interval: float = 2.0, metrics: Metrics | None = None,
    ) -> None: ...
    def enter(
        self, worker_id: str, register: Register,
        session: Session[WorkerMessage, ServerMessage],
    ) -> WorkerRun: ...
    async def run(self, run: WorkerRun, register: Register) -> None: ...
    def finish(self, run: WorkerRun, reason: str | None) -> None: ...
    def notify_jobs(self) -> None: ...

class WorkerEndpoint:
    def __init__(
        self, handler: WorkerHandler, *, peers: Gateway[WorkerMessage, ServerMessage],
        worker_tokens: Mapping[str, str],
    ) -> None: ...
    async def handle(self, socket: WorkerSocket) -> None: ...
    def _authenticated_identity(self, headers: Mapping[str, str]) -> str | None: ...
```

WorkerHandler 的全部字段：`coordinator: JobCoordinator`、`tools: FlowToolService`、
`results: ResultApplication`、`metrics: Metrics | None`，三个原 `float` 间隔/超时字段，
`deliveries: DeliveryStore`、`jobs: JobStore`、`stops: StopStore`、`workers: WorkerStore`，
以及 `_peers: Gateway[WorkerMessage, ServerMessage]`、`_runs: set[WorkerRun]`。
它只调用 `_peers.get`，不调用核心生命周期方法。WorkerEndpoint 的全部字段为
`_handler: WorkerHandler`、`_peers: Gateway[WorkerMessage, ServerMessage]`、
`_worker_tokens: dict[str, str]`。构造函数不启动 Task。

- `enter` 先确认 Session 已有 epoch；无 await 地执行原 worker_connected，成功后创建并登记
  WorkerRun。返回上下文即原 connected=True 的边界；DB 调用抛错仍沿用原未 connected 处理，
  不把未知事务结果描述为已在线。Endpoint 不读写上下文字段。
- `run` 接续原 stop/recovery/Registered/assignment 循环；原 `current` 仍是该协程局部变量。
  worker_id 来自 `run.worker.worker_id`，通信/持久化 fencing 使用原 `run.session.epoch`，
  缺失 epoch 是调用错误，不能回退为空字符串。首个 Register 作为参数，不长期另存副本。
- `finish` 同步先 discard `_runs`，再执行带原 epoch 的 worker_disconnected；无通信关闭。
  DB 清理失败也不会遗留 wake。它由 Endpoint 在 enter 成功后的 finally 调用一次。
- `notify_jobs` 遍历 `_runs`，仅在 `_peers.get(run.worker.worker_id) is run.session` 时 set wake。
  `_runs` 不按 peer 索引，不能查找或路由连接；查询仍只用 Gateway。被替换、已关闭、撤回发布或
  seal 后的消费者不会被主动唤醒。注册窗口内没有 wake 也不漏掉首次调度：run 进入 idle 分支
  会先 clear 再立即检查待停/待分配工作，之后才等待通知。[S21]

原 `_prepare_assignment`、`_stop_worker`、`_wait_while_idle`、`_receive`、
`_validate_assignment_message`、`_recover`、`_reject_missing_journal` 的签名和算法迁到 handler。
不增加额外的“非当前连接”调度异常；既有 assign_job/断连 DB epoch fencing 保留。[S19][S13]
wake 过滤不承诺强制停止已开始的业务；Session 关闭与业务提交边界仍按 §1.3/§1.7 执行。

#### R1.4 接入、异常与清理顺序

Endpoint 顺序为：认证 → accept → 校验首个 Register → 创建带 epoch 的 Session 并 start →
`await gateway.register(..., replace=True)` → 同步 `handler.enter(...)` → `await handler.run(...)`。
register 返回到 enter 之间不新增 await；失去当前身份的候选不能写入 worker_connected。
恢复/停止仍可早于 Registered，Gateway 发布仍不代表业务 ready。[S7][S10]

Endpoint 保留原明确的 except 分支、日志和 reason：ResultPending 对应 1013；
WebSocketDisconnect/ConnectionError/TimeoutError 对应原 connection_lost 记录；
ProtocolViolation/SessionOverflow/ValidationError 对应 1008 与原 ProtocolError。
未建 Session 的错误仍从原 socket 回复；已有 Session 的错误和业务回复只用原连接。
SessionClosed 属于 ConnectionError；未知异常和 CancelledError 继续传播，不新增通用吞错。
这些分支只把既有失败适配为协议响应，不决定结果提交或修改 DB。[S4][S12]

所有权和顺序按下面的嵌套 finally 固定；`run_ctx`、`registration`、`session` 都是 handle
局部变量，初值 None，`close_code=1000`、`reason=None`。Endpoint 只持有业务句柄并转交 reason：

```python
finally:
    try:
        if registration is not None:
            await self._peers.unregister(registration, close_code)
        elif session is not None:
            await session.close(close_code)
        else:
            await socket.close(close_code)
    finally:
        if run_ctx is not None:
            self._handler.finish(run_ctx, reason)
```

unregister 先撤回路由，再完整 join 通信并按身份退休；通信关闭失败/取消仍执行业务 finish，
DB finish 失败不能阻止已完成的登记退休。原始错误及清理错误保留传播/异常上下文，不吞掉。
register 失败后再次 close 同一候选只 join Session 原关闭 Task，不再启动底层关闭；先前的
close 错误仍可能再次传播。[S4] 尚未登记的 socket 仍归 Endpoint/ASGI。

**顺序差异明确记录：** 当前代码是 close → DB 断连 → 注销。[S12] 新结构是撤回路由 →
close/join → 登记退休 → DB 断连。通信清理仍早于 DB；退休不依赖 DB 成功，旧 epoch 的
DB finally 不能覆盖后继在线状态。[S13] 不因此声称 Gateway.close 已 join ASGI 业务 handler；
正常 drain 与超时/强制退出限制仍沿用 §1.7。

#### R1.5 全部生产接线与迁移

| 当前入口 | 修订后的接线 |
| --- | --- |
| Runtime 创建 WorkerGateway 与 Broker [S15] | `self.gateway` 直接是公共 Gateway；创建 `self.worker_handler`、`self.worker_endpoint`，二者与 Broker 使用同一实例；不保留第二个核心别名字段 |
| `runtime.gateway.handle(socket)` [S16] | `runtime.worker_endpoint.handle(socket)` |
| ControlService、WebhookConsumer 回调与 Runtime 内部 notify [S15] | 全部改为 `self.worker_handler.notify_jobs`；既有 `Callable[[], None]` 回调合同不变。[S22][S23] |
| console 的线程安全通知 [S17] | `loop.call_soon_threadsafe(runtime.worker_handler.notify_jobs)`；只调整依赖接线，不改 Web/UI 行为 |
| Broker 查询 [S2] | 构造注入 `self.gateway`，删除 connections/attach/detach；get 后固定原 Session；保留 §1.7 已列出的 unavailable/busy 优先级差异 |
| Runtime.shutdown [S6] | 入口 `self.gateway.seal()`，原后台任务停止/join，然后 `await self.gateway.close(1012)`；嵌套 finally 释放 access/DB，不接管 ASGI Task |

实现时先完成公共核心，再按上述边界拆 server、迁移 Broker/调用方，随后接入 worker。
`tests/test_gateway.py` 改名为 `tests/test_worker_endpoint.py`，保留业务行为断言，改用三对象装配；
公共核心用 §1.12 的 `test_communication_gateway.py`。其余旧构造涉及 preparation retry、
controller update boundary、conversation E2E、remote ASGI/E2E、ingress、PR lifecycle、stop、
archive recovery/worker recovery 与 console API fixture，均迁到新入口，不留旧类测试替身。
同步当前 architecture/components、overview、conversation-api 的职责说明；不批量改写历史计划。

#### R1.6 验收与取舍

§1.12 的全部业务、真实 PostgreSQL、Ruff/Mypy、构建与消融验收继续适用，新增拆分验收：

| 边界 | 实施期可观察事实 |
| --- | --- |
| 彻底移除旧角色 Gateway | 搜索 app/tests 和当前架构文档无 WorkerGateway、旧导入或兼容导出；公共包仍不导入 server/worker/DB/SDK。删除名称用审查检查，不新增永久“旧模块导入必须失败”测试 |
| 关闭与业务清理 | 卡住 Session.close 时尚未记录 DB 断连；close 抛错或反复取消后仍记录原 epoch；DB 清理抛错不留下旧路由，也不影响后继连接查询/调度 |
| 准入失败 | 认证/首包失败不登记；失去当前身份或 enter 未成功的连接不假造 DB 断连；A→B→C 回归继续验证 B 无 Registered/DB 覆盖 |
| 错误适配 | 原 1013、connection_lost reason、1008/ProtocolError、pre-Session 错误回复与未知异常传播不变 |
| 调度与接线 | 当前连接能收到入队唤醒，旧/退出连接不被主动唤醒；线程安全 console、service/webhook 回调及 shutdown 真实入口有效 |

优先更新已有行为测试，必要时合并成少量参数化回归；字段/集合和重命名不各建永久测试。
临时消融补两项：去掉 notify 的当前性过滤应唤醒旧消费者；把 finish 提到通信 close 前应破坏
关闭屏障下的 DB 顺序。原提交/停止/恢复与 owner 存活合同必须实际验证，不能仅凭拆分后 diff。

**代价与备选：** 两个明确的角色对象加一个三字段业务上下文，使 Endpoint 能在通信退出后
调用业务 finish，handler 无需获得关闭回调。保留原大类或整类改名不满足职责拆分要求；
本案不采用新的 context-manager/callback 框架，避免引入另一套错误状态传递。Endpoint
保存三项装配依赖，供 API 直接调用；无工厂或转发 facade。增加的生命周期接线是主要成本。

#### R1.7 本次核查、Oracle 取舍与待审批事项

本次再次 fetch，main@origin 和本 workspace 父版本仍为 §1.1 的同一提交；只改该计划。
本机只读咨询已核验初始化与实际响应模型均为 `claude-opus-5-5`，tools/mcp_servers 为空，
result success、进程退出 0。主线程核对源码后采用分离接入/业务与 enter/run/finish；
把 Oracle 建议的连接句柄缩为三字段 WorkerRun，去掉新增的 worker_id/epoch/Register 副本。
错误到协议响应的适配留在 Endpoint，不额外加 classify API/结果类型。查明现有 assign_job
已有 epoch fencing 后，不采纳额外调度异常建议；不把纯名称删除做成永久测试。[S19]

正常断连 low 在此次 fetch 的 main 仍未修复，§1.8 的窄 adapter 修复继续单独待批。
本次只做源码/计划核对、初稿保真与文档结构检查；未修改或运行新的实现/测试，不把先前
基线测试或 Oracle 意见当本阶段实现验收。咨询证据留在本 thread 的本地产物，未加入计划发布内容。

**待审批：** 按 R1 覆盖初稿的 server 结构、API、清理顺序及调用方迁移；§1.8 窄修复是否纳入；
方案明确获批后的实施授权。docs-only Draft PR 发布仍未授权，本计划仅在独立 workspace 本地；
未推送、未开 PR，后续沿用一个文档/bookmark/PR。此次“改”仅落实计划修订，不启动实现。

<a id="r2"></a>

### 2.2 R2 — 取消 WorkerEndpoint 与 WorkerRun（2026-10-29，UTC+08）

#### R2.1 需求、状态与覆盖范围

用户认为 Endpoint 与 Session 职责重叠，要求取消 Endpoint，并进一步明确“WorkerRun 也取消”。
本次从提案删除 WorkerEndpoint、endpoint.py、WorkerRun、`_runs` 和 enter/run/finish 拆分；
不换成其他上下文类、tuple/dict 包装或回调框架。WorkerGateway 仍按 R1 删除。
WorkerClient 未被要求删除，继续负责 worker 拨号、重连和协议驱动。这里只修改计划。

本条取代 R1.2–R1.4 的 server 角色结构、API 和清理安排，R1.5 的 Runtime/API 入口与测试命名，
R1.6 的拆分验收与取舍。R1.3 的 handler 构造依赖、业务私有方法迁移、首次调度不漏通知的
说明继续适用；R1.5 的 Broker、notify、console 和 shutdown 接线保留。R1.7 保留为历史记录。
§1.6 中 server 未登记 Session 的关闭责任，按本条明确移交给 handler；核心算法不变。

Gateway/Session 的其余合同、wire v15、journal v4、DB/envelope 均不改。WorkerExecution
长期持有执行 Task、原工具调用/Future、离线完成与 shielded stop；ExecutionState 管 journal；
server 持有 Flow binding/业务持久化。这些状态不进入 Gateway，也不随连接重建销毁。

#### R2.2 职责、文件与调用入口

| 模块 / 对象 | 负责 | 明确不负责 |
| --- | --- | --- |
| 公共 `Gateway` | 唯一 peer/session 登记、查找、替换、退休与通信关闭；沿用 §1.5–1.6 | 认证、业务、DB、业务 Task |
| 公共 `Session` | 单连接 reader/FIFO、串行写、临时请求/查询、底层 close/join | peer 表、调度、持久执行状态 |
| 新 `workers/transport.py` / 函数 `serve_worker` | 认证、accept、首帧 Register 校验、epoch/Session 装配和 start、移交前清理与握手错误映射 | Endpoint 对象、Store、wake、peer 表、移交后的回复或关闭 |
| 新 `workers/handler.py` / `WorkerHandler` | 委托 Gateway 登记/注销，在线与断连 DB、调度、恢复、工具/结果/停止、业务错误映射与 wake | 认证/socket、Session 构造、登记/替换算法、私有 peer 路由表 |

WorkerSocket Protocol 原样迁到 transport.py；ProtocolViolation 迁到 handler.py。transport
单向导入 WorkerHandler/ProtocolViolation，handler 不导入 transport。§1.8 的 `_SocketTransport`
若获批，也放在 transport.py；仅包装交给 Session 的 transport.close，范围不扩到首帧前的 raw socket。

**明确调整 R1 的 get-only 限制：** handler 可以调用 Gateway.register/unregister/get；
登记/替换/关闭算法仍归公共核心。handler 在独立的 ASGI 业务协程中调用它们，不在 Session
reader/responder/write Task 中调用关闭入口，不让通信任务等待自己。

```text
ServerRuntime
  ├─ gateway: 公共 Gateway ──管理── Session × worker
  ├─ worker_handler: WorkerHandler ──业务 Store/service
  │       ├─ 调用同一 Gateway 的 register/get/unregister
  │       └─ 一个 handle 协程：业务循环 + finally 清理
  ├─ worker_tokens（原 token 缓存）
  └─ ConversationBroker ──同一 Gateway.get → 原 Session.request

WebSocket API → serve_worker（函数：认证/首帧/Session 装配）
                           → WorkerHandler.handle（移交 Session）
```

```python
# transport.py；无对象字段或长寿命上下文
async def serve_worker(
    socket: WorkerSocket, *, worker_tokens: Mapping[str, str], handler: WorkerHandler,
) -> None: ...
def _authenticated_identity(
    headers: Mapping[str, str], worker_tokens: Mapping[str, str],
) -> str | None: ...

# handler.py；__init__ 与 R1.3 的精确签名相同
async def handle(
    self, worker_id: str, register: Register,
    session: Session[WorkerMessage, ServerMessage],
) -> None: ...
def notify_jobs(self) -> None: ...
```

认证保留原 bearer/hmac 比较。handler 的字段沿用 R1.3，唯独将 `_runs` 换为
`_wakes: dict[Registration[WorkerMessage, ServerMessage], asyncio.Event]`。
Registration 是 §1.5 已有的通信登记身份；此表仅保存通知等待器，不保存 WorkerRecord、
assignment、connected、reason 或 close_code，也不能按 peer 查找连接。
`notify_jobs` 只在 `gateway.get(reg.peer_id) is reg.session` 时 set 对应 Event。
这些业务状态全部留在 handle 的局部变量里；原业务 helper 的签名/算法不因删除 WorkerRun 改写。

#### R2.3 移交、首帧与关闭顺序

`serve_worker` 保留认证 → accept → 直接读取/校验首帧 Register → 创建带 epoch 的 Session →
start 的顺序。[S10] 首帧读取结束后才启动 Session reader，两者不并发。不能直接改成
`Session.start(); await Session.receive()`：当前 reader 会先分派 reply-key 消息并丢弃无等待者的
回复；实测首帧 ConversationResult、次帧 Register 会只向 receive 暴露后者，错误首帧被隐藏。
本轮保留原首帧行为，不增加 Session 握手模式或新 API。[S4][^first-message-probe]

**清理责任在直接调用 handle 时移交：** 接入函数的握手 try/except 只覆盖 accept、首帧和
Session 构造/start。其 else 分支同步设置局部 `handed_off=True`，紧接着直接
`await handler.handle(...)`，中间无 await 或新 Task。外层 finally 只在未移交时关闭已创建的
Session，尚未创建则关闭 raw socket；移交后不再回复、记第二次错误或兜底 close。
handler 从入口起以 try/finally 接管 Session，包括 register 尚未返回 Registration 的失败路径。
这样业务/DB 清理异常不会重新进入握手错误映射。

handler 的 `registration=None`、`current=None`、`connected=False`、`reason=None`、
`close_code=1000` 均为局部变量；epoch 取自 Session，不保存另一份对象字段。
在 try 内检查 epoch 非 None，缺失是调用错误，仍要进入 finally。正常顺序为：

```text
await gateway.register(worker_id, session, replace=True)
  → 同步创建/登记 wake → 同步 worker_connected → connected=True
  → 原 stop/recovery/Registered/assignment 循环
finally
  → 同步移除 wake
  → 有 Registration：await gateway.unregister(registration, close_code)
    无 Registration：await session.close(close_code)
  → 嵌套 finally：仅 connected 时，以原 epoch 执行 worker_disconnected
```

发布与 worker_connected 之间不增加 await；A→B→C 的 B 复核失败时不能写在线 DB 或发 Registered。
恢复/停止仍可早于 Registered，直接 Resume/Replay 不变；ResultAccepted 仍在结果提交核验后，
StopAccepted 仍在 StopStore.confirm 后发送。[S7][S9][S10]

- **异常适配：** 握手保留原认证拒绝 1008、connection_lost 日志与协议错误 1008/ProtocolError；
  handler 保留 ResultPending→1013、connection_lost、协议错误三组分支和 reason。
  handler 回复只经原 Session；SessionClosed 属于 ConnectionError。未知异常、CancelledError
  及清理错误照常传播，不新增兜底吞错或把 DB 失败解释为握手失败。[S12]
- **register 失败：** 预留前拒绝的新 Session 由 handler finally 关闭；预留后失败按 §1.6
  由 Gateway 清理并退休候选，handler 再 close 只 join 原关闭 Task，不重复 transport.close。
  首个 close code 保留；原关闭错误可能再次传播，沿用 R1 已记录的代价。[S4]
- **清理屏障：** wake 在通信/DB 清理前同步移除。unregister 撤回路由 → close/join → 身份安全
  退休后，嵌套 finally 才记 DB 断连；关闭失败或反复取消仍不跳过 DB，DB 失败不留下登记或 wake。
  旧 epoch 的断连不能覆盖后继连接；Gateway.close 仍不 join ASGI/业务 Task。[S13]

#### R2.4 生产接线与验收调整

Runtime 创建 `self.gateway`、`self.worker_handler`，以及从现有 SecretStr settings 提取的
`self.worker_tokens: dict[str, str]`，取代原 WorkerGateway 中的同一 token 缓存；不设
worker_endpoint，不记录 token 日志。API 改为
`await serve_worker(socket, worker_tokens=runtime.worker_tokens, handler=runtime.worker_handler)`。
Broker 使用同一 Gateway；service/webhook/console 及内部通知使用 worker_handler.notify_jobs；
shutdown 沿用 R1.5 的 seal/close 与 DB 释放顺序。[S15][S16][S17]

`tests/test_gateway.py` 拟改名为 `tests/test_worker_handler.py`，通过
serve_worker + WorkerHandler + Gateway 装配，取代 R1 的 Endpoint 三对象装配。
R1.5 列出的其他 fixture 全部迁到该入口；当前架构文档同步，历史记录不改写。
§1.12 的定向、真实 PostgreSQL 全量、静态/构建和业务所有权验收继续适用，追加以下边界：

| 验收 | 实施期可观察事实 |
| --- | --- |
| 移交与异常隔离 | handler 关闭后 DB 清理抛错，原异常传播；没有额外 ProtocolError/connection_lost 映射；transport.close 只执行一次 |
| 登记失败与取消 | sealed 拒绝、替换候选失败或取消时 Session 完整关闭；未成功 worker_connected 不假造 DB 断连；A→B→C 回归继续成立 |
| 严格首帧 | 首帧为未请求的 ConversationResult、随后 Register，仍回复原 ProtocolError/1008 且不登记；保留认证/身份/解析失败断言 |
| 业务与关闭顺序 | close 被屏障卡住时 DB 未断连；关闭错误或反复取消后仍清理原 epoch；DB 抛错不遗留 wake/路由，后继查询/调度可用 |
| 简化检查 | 实现与当前架构文档无 WorkerGateway/WorkerEndpoint/WorkerRun 及其旧入口或兼容替身；用审查检查，不为删除名称新增永久测试 |

优先扩展已有行为测试。R1 的通知过滤消融保留；将“finish 提到 close 前”的消融改成“DB 断连
提到 unregister 前”。另临时把 handle 调用移进握手 try，验证错误隔离断言会失败。
本条没有运行这些未来实现验收，也不把先前探针/基线测试算成第三阶段通过。

#### R2.5 取舍、实际核查与待审批

单个 handler 协程能用局部变量和嵌套 finally 表达业务生命周期。代价是 handler 需要调用公共
登记/注销 API；算法仍在 Gateway，避免新增上下文与关闭后回调。接入函数只处理角色接入，
不持有 registry/Store，也不复刻旧大类。首帧暂留角色接入代码以保留严格验证；若以后要放入
Session，须单独设计不跳过首帧的 API，不能借此次删除 Endpoint 悄悄改变分派行为。

再次执行 `jj git fetch --remote origin` 后，main@origin 与 workspace 父版本仍为 §1.1 的
同一提交。读取当前 Session、server 驱动、API/runtime、epoch fencing 和既有回归后核查：
正常断连再次 close 的 low 仍未修复，§1.8 窄适配继续单独待批。[S4][S12]
本机只读咨询的初始化/实际响应均为 `claude-opus-5-5`，tools/mcp_servers 为空，result success、
进程退出 0。采用其明确移交点和握手 else 隔离建议，去掉初步候选中外层无条件二次 close。
咨询产物留在本 thread 的本地 artifacts；没有把模型建议当成执行验证。

**实际交付：** 独立 jj workspace 中仅修订本计划；初稿和 R1 原文保留，未修改实现或测试，
未提交/推送/开 PR。待审批 R2 的结构与清理安排、§1.8 窄修复是否纳入及实施授权；
docs-only Draft PR 发布仍未授权，后续继续同一个文档/bookmark/PR。

### 2.3 R2 实施与关闭契约阻塞（2026-10-29，UTC+08）

**触发与决定：** 用户选择两端公共 Gateway / Session，取消 Endpoint 和 Run，并明确装配结构
可以以后再调整。保留 WorkerClient 内部构造 Gateway、ServerRuntime 构造并共享 Gateway；
不为表面对称新增壳类。初稿和 R1 中被 R2 取代的结构不再实施。

**已实施：**

- 新增公共 `communication/gateway.py`，以 Registration 身份、current/owned、发布门槛、seal
  和 shielded collect-all close 管理通信；没有业务 Store、第二个 reader、pending 表或执行 Task。
- 删除 server WorkerGateway；`workers/transport.py` 只负责认证、实际首帧、Session 装配和
  移交前清理。`workers/handler.py` 单个 handle 协程持有业务局部变量，并委托 Gateway 登记/注销；
  `_wakes` 仅保存通知等待器。Broker 删除自己的连接表，固定查到的 Session 执行原 request。
- WorkerClient 在 Register 与 execution.recovery 之前完成非替换准入，stop 同步 seal，连接退出
  先 join 原观察者再注销；重叠候选失败不清掉原连接的 connected 状态。run 退出关闭所有通信，
  不关闭执行 owner。Runtime/API、console 通知和既有测试构造同步迁移。
- Session、WorkerExecution、ExecutionState、协议、DB 及业务工具/结果/停止提交算法无改动。
  同步当前架构文档和 changelog，历史计划保留。架构 HTML 的通信、server、worker 卡片已渲染检查。

**测试反馈与修正：** 先加入 worker 准入回归，未接入 Gateway 时出现停止后 Register 和第二连接
借用 owner；接入后通过。首次集成暴露 archive helper 的旧返回变量，首次全量暴露 triage fixture
的旧 gateway 通知字段，均只修正迁移遗漏。补充真实首帧、A→B→C 不覆盖 DB 在线状态、close/join
先于 DB 断连、关闭/DB 异常与反复取消、Runtime 释放资源等契约回归；扩展 fresh-interpreter
导入测试到 Gateway。定向测试、Ruff/Mypy、新增文件格式和 console/distribution 构建通过；
最终真实 PostgreSQL 全量复跑全部通过且无跳过（`pytest-full-final.log`）；现有 Starlette
TestClient 的 httpx 弃用警告仍在。格式对照保留父版本已有排版，未使原已格式化文件退化。
构建核对 wheel 与当前 Gateway/handler/transport/Session/client 源码一致，含 console，
不再包含旧 WorkerGateway 模块。不把前阶段测试结果当成本阶段证据。

**未解决的阻塞：** 复核原 `_register_session` 后确认它容忍
`(WebSocketDisconnect, ConnectionError)`；公共 Gateway 只容忍中立 ConnectionError。
在同一旧 Session.close 抛 WebSocketDisconnect(1006) 的输入下，固定基线仍允许后继登记，
当前无 server adapter 版本拒绝后继、关闭候选并留下无可路由连接。方法级对照已执行，
证据为本 thread 的 `.amp/in/artifacts/unified-gateway/replacement-close-gap.log`。
这已超出“保留原 low 而不修复”的无行为变化假设；不能将当前实现标记为完成。

已向用户和来源 thread 提出：是否批准 §1.8 的 server transport.close 窄适配，
只将 WebSocketDisconnect 当作已断连，其他异常继续传播。**尚未收到该项明确答复，未实施。**
获批后还须增加该异常的替换回归，并以真实 Uvicorn + websockets 验证正常断连与 ASGI 退出；
不能仅依赖 TestClient 或 no-op close。若不批准，需要另定保留异常语义的角色适配，不能发布当前缺口。

**交付边界：** 基线仍为 §1.1 提交，使用隔离 venv 和本地 UTF8 PostgreSQL；根 checkout 的 Web 工作
未动。未提交、移动 bookmark、推送、开 PR 或部署。发布前 review gate 尚未运行，须在关闭契约解决、
重新验证并获得发布授权后执行；当前没有把测试或消融当作该 review gate。

### 2.4 实施简化 / 消融对照（2026-10-29，UTC+08）

同一解释器/工作区，以临时 pytest plugin 在运行时替换一个方法，**不改写仓库源码**。
控制组执行公共 Gateway、handler、client 契约测试及两个临时屏障/通知探针；全部通过。
逐一去掉机制后，以下变体均由行为断言捕获，因此保留对应机制：

| 变体 | 实际失败事实 |
| --- | --- |
| `latest`：去掉 await 后准入/当前性复核 | B 未抛 SessionClosed，过时候选错误返回登记成功；C 保留/先退出两种输入均失败。 |
| `publication`：查找忽略发布门槛 | B 等待 A 时 get 返回候选 Session，原不可用窗口被破坏。 |
| `owned`：只关闭 current | A 被 B 隐藏且 close 被屏障阻塞时，进程关闭提前返回。 |
| `identity`：按 peer 直接退休 | A/B 的 finally 删掉 C，get 返回 None。 |
| `worker-seal`：stop 不 seal | 尚未完成的拨号在 stop 后发送了 Register。 |
| `worker-overlap`：worker 使用 replace=True | 第二连接调用 execution.recovery，触发明确的“second connection borrowed owner”断言。 |
| `presence-order`：DB 断连移到 unregister 前 | DB 回调观察到通信 close 尚未 join；无重复 DB 调用作为失败替代。 |
| `handshake-scope`：handle 移入握手 try | DB 清理的 ConnectionError 被握手异常映射吞掉，原异常传播断言失败。 |
| `notify`：唤醒不检查当前登记 | 业务尚未退出的旧 Registration 等待器被错误 set。 |

§1.8 尚未获批，没有伪造其 adapter 移除实验。已知关闭失败的基线/当前差异另见 §2.3。
本轮没有为临时通知探针增加永久实现细节测试；保留公开准入/替换/关闭合同回归。

复现输入与完整输出保存在本 thread 的 `.amp/in/artifacts/unified-gateway/`：
`ablation-plugin.txt`、`ablation-probes.txt`、`ablation-control.log`、`ablation-<variant>.log`。
临时可执行 plugin/test 文件在验证后删除；要重做可将两份 txt 分别复制为 scratch 中的
`gateway_ablation.py`、`test_gateway_ablation_probes.py`，从本 workspace 执行：

```bash
PYTHONPATH="$PWD:$scratch" GATEWAY_ABLATION=<variant> \
  uv run --no-sync pytest -q -c pyproject.toml -p gateway_ablation <同一验收测试>
```

其中 scratch 指原仓库 `.amp/in`，使用本轮隔离 `UV_PROJECT_ENVIRONMENT`。latest/publication/
identity 对应 `test_overlapping_replacements_never_republish_a_superseded_candidate`；owned/notify
对应两份临时探针；worker 变体对应 stop_during_dial/overlapping_connection；presence-order/
handshake-scope 对应 handler_cleanup 的 none/database 参数。变体预期失败，控制组必须成功。

### 2.5 server 关闭异常适配与真实网络验收（2026-10-29，UTC+08）

**触发、决定与实现：** 用户了解 §2.3 的替换回归后要求修复。按 §1.8 的窄边界，在 server
`workers/transport.py` 增加 `_SocketTransport` 并交给 Session；仅 close 把 WebSocketDisconnect
当作 peer 已离开。send/receive 原样透传，RuntimeError、OSError 与取消继续传播。
Session 仍负责 join 通信任务，公共 Gateway 无框架依赖。认证/首帧失败时的原 socket 路径不改，
执行、journal、协议、DB、业务持久化及 Runtime 装配不变。此记录解除 §2.3 的待批及替换阻塞。

**永久回归与简化对照：** 扩展已有重复 worker 连接测试，等待真实 Registered 和旧处理器退出，
核对新 Session/epoch 与 DB presence 登记，替代仅检查 accept 加 sleep(0) 的弱断言。
旧 socket.close 正常返回、抛 WebSocketDisconnect 两种输入均覆盖；既有清理测试增加已断连
成功与 RuntimeError 传播，保留 OSError、DB 失败、取消、seal 和清理顺序断言。
在 Session 直接持有原 socket 的未适配版本，替换及正常断连回归失败；同一输入加入适配后通过。
这也验证移除该角色边界的简化版本会丢失关闭契约，不需要把框架异常引入公共 Session/Gateway。
证据：`close-adapter-before.log`、`close-adapter-after.log`、`close-fixed-post-format.log`。

**真实网络验收：** 临时探针使用实际 FastAPI worker 路由、Uvicorn 的 WebSocketsSansIOProtocol、
websockets 客户端、WorkerHandler、Gateway/Session 和独立 UTF8 PostgreSQL。无任务的 worker
不调用 GitHub/agent，不启动周期业务 sweep；调用原 ServerRuntime.shutdown 完成实际通信及 DB 关闭。

| 场景 | 未适配 | 修复后 |
| --- | --- | --- |
| 正常断开后重连 | 旧 handler 退出传播 WebSocketDisconnect。 | 新 epoch 登记成功，后续正常离线。 |
| 旧连接已断开、close 尚未完成时替换 | 新连接被关闭，收不到 Registered。 | 后继收到 Registered，旧 finally 不覆盖新 epoch 的 online 状态。 |
| 带在线 worker 的 Uvicorn 退出 | handler 清理向 ASGI 传播 WebSocketDisconnect。 | 客户端收到 1012，handler drain 后才执行 lifespan/DB 关闭。 |

替换窗口只用事件屏障延迟 close 调度，实际 Starlette/Uvicorn close 没有改为 no-op，也没有
注入断连异常；各场景均观察到真实底层 WebSocketDisconnect(1006)。修复后没有 ASGI 处理异常，
DB 最终 offline，无残留 ASGI/Session/Gateway 任务。使用测试专用 graceful timeout，未改生产
CLI 的既有设置，也不扩大到强制退出或任意慢业务的保证。证据：`live-close-before.log`、
`live-close-after.log`；探针源码保留为 `live-close-probe.txt`，临时可执行测试在验收后删除。

**综合验证：** 本轮重新运行真实 PostgreSQL 全量且无跳过，新增断连与替换回归通过；格式化后
单独复跑 handler 测试。Ruff/Mypy、transport 与变更测试范围的格式检查通过。sdist/wheel
重新构建，并核对包内 Gateway、Session、transport、handler、client 和 console 与当前文件一致，
旧 WorkerGateway 模块不在包中。测试保留原 Starlette TestClient 的 httpx 弃用警告。
证据：`pytest-full-close-fixed.log`、`close-fixed-static.log`、`close-fixed-build.log`。
以上本地产物均位于本 thread 根目录 `.amp/in/artifacts/unified-gateway/`。

**复现临时探针：** 将 `live-close-probe.txt` 复制为 scratch 下的 `test_gateway_live_close.py`，
从本 workspace 使用隔离 venv 和有效 `FLOWTOOL_TEST_DATABASE_URL` 执行：

```bash
PYTHONPATH="$PWD" uv run --no-sync pytest -q -s -c pyproject.toml -p tests.conftest \
  "$scratch/test_gateway_live_close.py"
```

**交付边界：** 本地实现、行为验证及自查完成；根 checkout 的其他线程工作未改动，未提交、
移动 bookmark、推送、开 PR 或部署。发布前 review gate 仍待后续运行，当前结果不代替该门禁。

### 2.6 PR 测试精简与关闭回归补强（2026-10-29，UTC+08）

**触发与范围：** 用户指出 PR #124 中仍有无效测试。本轮只整理该阶段新增、迁移的测试及
相关测试辅助代码，不改 Gateway、Session、两端业务实现、协议或持久化。

**删减决定：** 删除 Gateway 中重复 Session 双向请求分派的混合 smoke test；worker 准入由
现有 stop-during-dial / overlapping-connection 回归保护。删除仅等 accept 的多 worker 用例，
保留 ConversationBroker 跨多个 peer 的并发查询与全局准入测试。健康连接替换由 server 的
A→B→C 用例覆盖，独立替换测试只保留已断连 predecessor；公共 Gateway 只保留 C 先退出后
B 仍不得复活的额外边界。Gateway 导入已传递导入 Session，移除重复的新解释器导入实例。

清理用例合并重复成功、普通关闭异常及取消参数，保留正常断连、RuntimeError、DB 失败、取消、
sealed 拒绝以及通信先 join、DB 后离线的合同。unregister 用例只检查 predecessor 屏障，
Session 的重复取消保护继续由其自身测试覆盖。移除 `_wakes`、socket close 次数等内部断言；
server 替换直接核对存储中的 epoch，不再依赖 worker_connected spy 的次数与参数。
这些调整收窄 §2.3–§2.5 的永久测试矩阵，不改变其实现决定。

**发现并修正漏检：** 运行时把 Gateway._close_owned 改为只遍历 current 后，原 Gateway 测试
仍全部通过。旧断言在 socket 刚进入 close 时立即检查 task.done()，未等其他关闭完成，漏掉了
被替换旧连接尚未退出、Gateway.close 已提前结束的错误。现有回归改为保持旧连接的事件屏障，
有界等待 shield 后的关闭任务，验证其在放行之前不能完成；同一场景覆盖重复取消和关闭异常
的保留。不新增临时探针到永久测试中。正确实现与 current-only 变体的重复对照结果一致。

**验证与证据：** 精简后的永久测试通过控制组；latest、publication、owned、identity、
worker-seal、worker-overlap、presence-order、handshake-scope、close-adapter、shutdown-cancel
运行时故障变体均触发对应回归。真实 PostgreSQL 全量无失败、无跳过，Ruff、Gateway 测试格式
及 app/tests 的 Mypy 通过；保留原 Starlette/httpx 弃用警告。没有改生产代码，未重做构建或
真实网络探针，不把 §2.5 的结果当作本轮新执行的验证。

产物位于本 thread 的 `.amp/in/artifacts/unified-gateway/`：`test-cleanup-before-*.log`、
`test-cleanup-focused.log`、`test-cleanup-ablation-*.log`、`test-cleanup-close-repeat.log`、
`test-cleanup-full.log`、`test-cleanup-static.log`。本轮运行时变体源码保存为
`test-cleanup-ablation-plugin.txt`，复现方式同 §2.4，输入为 Gateway、handler、client 的永久测试；
临时可执行 plugin 在验收后删除。清理在独立 workspace 完成，根工作区及其他线程变更未动。
发布沿用 PR #124 和既有 bookmark，并执行针对本轮清理的 review gate。

## 源码依据（规划固定基线）

[S1]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workers/gateway.py#L403-L431
[S2]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workers/conversation.py#L40-L99
[S3]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/worker/transport/client.py#L80-L220
[S4]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/communication/session.py#L20-L328
[S5]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/worker/runtime.py#L93-L182
[S6]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/runtime.py#L229-L240
[S7]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/tests/test_gateway.py#L603-L650
[S8]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/worker/execution/lifecycle.py#L65-L374
[S9]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workers/gateway.py#L269-L372
[S10]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workers/gateway.py#L125-L186
[S11]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/cli.py#L111-L126
[S12]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workers/gateway.py#L308-L344
[S13]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/persistence/workers.py#L120-L168
[S14]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/api.py#L62-L77
[S15]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/runtime.py#L64-L240
[S16]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/api.py#L214-L217
[S17]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/console/api.py#L93-L101
[S18]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workers/gateway.py#L70-L101
[S19]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/persistence/workers.py#L177-L225
[S20]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/domain/models.py#L194-L206
[S21]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workers/gateway.py#L188-L213
[S22]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/workflow/service.py#L59-L80
[S23]: https://github.com/b1indsight/agentflow/blob/cc796c62a5b3c93711314263ba0a77688c4b3f86/app/controler/events/webhooks.py#L21-L35

[^dependency-source]: 本轮用 `inspect.getsource` 读取安装的 uvicorn 0.52.1
    `Server.shutdown` / `_wait_tasks_to_complete` / `WebSocketsSansIOProtocol.send`、
    starlette 1.3.1 `WebSocket.send/close`、websockets 17.0.1 `Connection.close`。
    版本与 uv.lock 一致。源码摘录存于本 thread 的本地产物
    `.amp/in/artifacts/unified-gateway/dependency-close-contract.txt`；未把依赖代码加入仓库。

[^close-probe]: 方法级探针使用真实 `WebSocket.receive_text/close`、
    `WebSocketsSansIOProtocol.send` 与当前 Session，只模拟 transport 的 disconnected 状态。
    输出为 `normal peer disconnect observed: code=1000`、
    `unchanged Session.close propagated WebSocketDisconnect: code=1006`、`Session closed flag: True`。
    日志为本 thread 的 `.amp/in/artifacts/unified-gateway/closed-transport-probe.log`。

[^first-message-probe]: 未改动 Session 的内存 transport 探针依次送入 ConversationResult、Register，
    实际首帧是 ConversationResult，但 start 后首次 receive 返回 Register；旧首帧检查会拒绝该输入。
    日志为本 thread 的 `.amp/in/artifacts/unified-gateway/endpoint-removal-first-message-probe.log`。
    只验证当前 reader 的分派行为，无网络/DB，也未修改实现或测试文件。
