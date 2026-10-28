# Worker 执行生命周期解耦（第一阶段）— Feature record

**Stage:** Implementation — 已实现并完成本地验证；代码审查与发布状态见 PR
**PR:** [#120](https://github.com/b1indsight/agentflow/pull/120) — 沿用计划 PR 交付第一阶段实现
**Bookmark:** `refactor/worker-execution-lifecycle`（计划与实现沿用同一个 PR）

## 1. Initial plan

### 动机、基线和范围

最终方向为两端共用 Gateway + Session：Gateway 管认证对端与有效连接，worker 当前只连接一个
server，server 管多个 worker；Session 只属于一次连接 epoch。执行 Task、journal 和 server 的
Flow binding 必须独立于通信 Session 存活。本阶段先建立 worker 的执行所有权边界。

当前 `WorkerClient` 持有执行 Task、跨重连工具请求和 Future，并收集结果、准备恢复、处理停止及
进程退出。`run_connection()` 断开会保留执行，整个 `run()` 的 finally 却取消执行；执行器拿到的
`tool_caller` 也是 Client。因此仅替换 socket 的回归不能证明可以替换整个通信对象。[S1]

本次规划基于实际 fetch 后的远端
[`fd5c10f`](https://github.com/b1indsight/agentflow/commit/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60)。
共享 checkout 的 detached Git HEAD 仍为
[`ce89bab`](https://github.com/b1indsight/agentflow/commit/ce89babbec89c1f35dd93af5312a7734057bcfc8)，
落后五个提交。fetch 前本地 `main` 与缓存 `origin/main` 同为
[`619b278`](https://github.com/b1indsight/agentflow/commit/619b278a5aa7b0471f1ffce3f4766783d9d882cc)，
fetch 后两者均指向上述最新基线。已比较 worker、contracts、server worker handlers、persistence、
tests、scripts、CI 和依赖文件：这些相关内容在 checkout HEAD 与最新远端基线间没有差异。

本记录接续已完成的 [Worker 持久化会话](worker-session.md) 和
[Worker 目录归组](worker-layout.md)，属于新的重构阶段；保留前序记录原文。
讨论来源为[源 thread](https://ampcode.com/threads/T-01a16fd3-4a30-7198-9401-c27c98620c27)。

**包含：** 长寿命执行所有者；ExecutionState 定名；Client/Executor/Runtime 接线；执行完成、
工具等待、显式停止和 shutdown 的所有权；相应行为回归及直接引用方更新。

**不包含：** 公共 Session/Gateway、envelope 升级、消息总线/outbox、工具调用持久化、server
状态机或数据库格式改变、新进程 supervisor、多 Client 并发、worker 热替换管理 API、历史服务拆分。
下一阶段再以历史查询作为公共 Session 的双端试点。

### 预期行为和成功标准

1. 旧 Client 停止并被完全 join 后，新 Client 注入同一个执行所有者。原执行继续运行，
   `executor.execute` 不重复调用；原 Client 不再被 executor/MCP 的工具调用入口引用。
2. 已排队或已发送、尚未得到回执的工具调用保留原 `call_id` 和原 Future。重连仅重绑定发送副本的
   epoch；断线不取消工具等待者。调用者取消、任务结束或显式停止后，不把过期调用发送给后续 assignment。
3. 没有 Client 时执行自然完成，也由执行所有者脱敏并写入 journal。落盘失败保留内存结果，
   恢复注册和终态发送前重试；仍失败则不发布虚假的恢复描述、不发送终态、不启动替代执行。
4. 保持 `record_received → await send Started → record_started → create_task`。
   发送 Started 失败或被取消时不启动执行；后两步同步完成，中间没有 await。
5. 显式停止等待所拥有的执行及子进程清理，先保存停止证据，再回收 workspace，最后发送停止回执。
   丢失 StopAccepted 可以重放；没有原 Task 且 journal 只有 started 时仍拒绝推断已停止。
6. 普通进程 shutdown 等待所拥有的进程退出后才释放 root lock；不冒充 server 的业务停止请求，
   不凭空产生 ExecutionStopped，不主动释放 server Flow binding。

“跨重连保留”限于同一 worker 进程。Future/Task 不跨进程重启恢复；非 archive 编码任务不盲目重跑。
已有 archive 冻结任务恢复例外保持不变。[S2][S5]

### 技术设计：职责和模块

```diagram
当前
┌───────────────┐     ┌─────────────────────────┐
│ WorkerRuntime │────→│ WorkerClient            │
└───────────────┘     │ socket + Task + waiters │
                     └────────────┬────────────┘
                                  ↓
                     ┌─────────────────────────┐
                     │ WorkerSession / journal │
                     └─────────────────────────┘

本阶段提议
┌───────────────┐     ┌─────────────────────────┐
│ WorkerRuntime │────→│ WorkerClient / 通信     │
│ 装配、进程退出│     └────────────┬────────────┘
└───────┬───────┘                  │ 借用
        ┃                         ↓
        ┗━━━━━━━━━━━▶┏━━━━━━━━━━━━━━━━━━━━━━━━━┓
                     ┃ WorkerExecution         ┃
                     ┃ Task、工具等待、停止清理┃
                     ┗━━━━━━┳━━━━━━━━━┳━━━━━━━━┛
                            ↓         ↓
                    ExecutionState  WorkerExecutor
                    journal / 历史  agent / workspace
```

| 模块 | 拟调整职责 |
| --- | --- |
| 新增 `app/worker/execution/lifecycle.py` | `WorkerExecution` 拥有执行、完成收集、工具等待和停止清理；不接收 socket，不进行网络收发。 |
| `app/worker/state/session.py` → `state/execution.py` | `WorkerSession` → `ExecutionState`；`SessionState` → `ExecutionRecord`；`SessionError` → `ExecutionStateError`。保留真实 journal 实现，明确其不属于连接。 |
| `app/worker/transport/client.py` | 保留拨号/重连、认证头、当前 epoch、心跳、单一 reader、消息分流、协议顺序和 ACK fence；借用执行所有者。 |
| `app/worker/execution/executor.py` | `session` 参数/属性改为 `state: ExecutionState`；保留 initial/revision/archive、agent、MCP、VCS 和 workspace 编排。 |
| `app/worker/runtime.py` | 创建 state、executor、execution owner、client；拥有根锁、信号、健康任务以及进程关闭顺序，不接收业务循环。 |

`TaskExecutor` 协议、既有 `WorkerProtocolError`、`FlowToolCallError` 和 pending-tool 数据类型迁入
`lifecycle.py`，Client 使用这些定义，执行层不导入 Client。保留异常分类、继承关系和既有错误文本，
尤其避免进入 `Failed.error` 的文本因改名变化而改变 digest。`FlowToolCaller` 的调用签名保持不变。
不添加兼容构造器、旧字段代理或第二套执行状态。

历史读取暂保留 `ExecutionState.read()` 及 Codex/Amp 读取器。Client 通过注入的 `read_history`
可调用对象访问，查询 Task 仍属于连接，保留四个并发查询及 query_busy 行为。此处不新增历史 facade。

#### 核心类型的全部字段

**`ExecutionState`** 沿用 `path: Path`、`assignment: Assignment | None`、
`_history: CodexHistory`、`_amp_history: AmpHistory`。不增加 Task、socket 或 stop 控制字段。

**`ExecutionRecord`** 与现有 journal v4 完全同构，所有默认值、null、嵌套模型及序列化规则不变：[S2]

| 字段 | 类型与含义 |
| --- | --- |
| `version` | `Literal[4] = 4`，磁盘格式版本 |
| `job_id`, `flow_id` | `int`，执行身份 |
| `assignment_token` | `str`，assignment fence |
| `state` | `Literal["received", "started", "succeeded", "failed"]`，既有执行阶段 |
| `result_digest` | `str \| None`，现有 terminal digest |
| `terminal` | `Succeeded \| Failed \| None`，原 wire 终态对象 |
| `revision_phase` | `Literal["resume", "fallback"] \| None`，修订阶段 |
| `fallback_consumed` | `bool`，一次性 fallback 证据 |
| `invocations` | `list[RevisionExecutionRecord]`，调用记录 |
| `archive_task` | `ArchiveTask \| None`，可恢复的冻结归档任务 |
| `root_identity` | `tuple[str, int, int]`，路径、设备及 inode |
| `acknowledged` | `bool`，既有结果/停止确认标记 |
| `cleanup_verified` | `bool`，归档清理核验事实 |
| `workspace` | `WorkspaceBinding \| None`，分配 fence |
| `execution_nonce` | `str \| None`，执行所有权 nonce |
| `stopped` | `ExecutionStopped \| None`，持久停止回执 |

**`PendingToolCall`** 只包含 `request: FlowToolRequest` 和
`future: asyncio.Future[DeliveryReceipt]`；原请求身份固定，Future 由 owner 创建、完成和清理。
它是执行范围内的内存请求，不是通用 Session 的 pending reply，也不写入 journal。

**`WorkerExecution`** 一份实例对应一个 worker 进程的单执行槽：

| 字段 | 类型与含义 |
| --- | --- |
| `worker_id` | `str`，执行日志身份 |
| `executor` | `TaskExecutor`，真实执行及 workspace 回收适配器 |
| `state` | `ExecutionState`，同一 journal 实例 |
| `_execution` | `asyncio.Task[ExecutionResult] \| None`，拥有的执行及未成功落盘的完成结果 |
| `_cancel_requested` | `bool`，本执行由 owner 请求取消；阻止清理产出被误收集为业务终态 |
| `_tool_calls` | `deque[PendingToolCall]`，包括已借给通信方、尚未处理回执的队头 |
| `_tool_ready` | `asyncio.Event`，有可观察工具请求时唤醒连接观察者 |
| `_stop_task` | `asyncio.Task[ExecutionStopped] \| None`，owner 拥有的停止/回收操作 |
| `_stop_request` | `StopExecution \| None`，停止操作完整身份；换 epoch 不改变 job/flow/token/request_id |
| `_shutdown_task` | `asyncio.Task[None] \| None`，幂等进程关闭操作 |

不新增锁或连接注册表。检查/创建 Task、journal 修改和队列头操作在单事件循环的同步段进行；
**同一 owner 同时只有一个通信消费者，替换前必须取消/关闭并 join 旧 Client**。不保证并行挂两个 Client。

#### 拟定接口（签名为审批设计，不是已实现 API）

`TaskExecutor` 原样迁移：

```python
async def execute(self, task: ExecutionTask, secret: AssignmentSecret | None = None,
                  *, tool_caller: FlowToolCaller) -> ExecutionResult: ...
async def reclaim_workspace(self) -> None: ...
```

`WorkerExecution` 的公开边界：

| 精确签名 | 责任 |
| --- | --- |
| `def __init__(self, *, worker_id: str, executor: TaskExecutor, state: ExecutionState) -> None` | 组装长寿命 owner，不创建网络任务。 |
| `def receive(self, assignment: Assignment) -> None` | 拒绝忙碌、停止未确认或已 shutdown 的 owner；保存 received。 |
| `def start(self) -> None` | 同步 record_started 并创建唯一执行 Task，向 executor 传入 owner 作为 tool_caller。 |
| `def recovery(self) -> RecoveryDescriptor \| None` | 收集已完成执行后构造恢复描述；落盘失败则抛错。 |
| `def resume(self, message: ResumeAssignment) -> None` | 校验 job/token，处理运行中或刚完成的原 assignment；只有既有 archive 例外可创建新 Task。 |
| `async def reject_recovery(self, job_id: int) -> None` | 保留 clear_rejected 规则，取消并等待已被拒绝的执行，清理工具等待者。 |
| `def lease(self) -> Assignment \| None` | 仅有未结束执行时给心跳提供当前 assignment。 |
| `async def wait_terminal(self, connection_epoch: str) -> TerminalMessage` | 可取消的连接观察者：等待不传播取消给执行；收集后返回持久终态的 epoch 副本。无可证明终态时抛错，不合成结果。 |
| `def acknowledge_result(self, accepted: ResultAccepted) -> None` | 核对结果身份后调用原 acknowledge 规则；不无条件清除活跃 Task。 |
| `async def call_tool(self, name: str, arguments: dict[str, object], *, call_id: str \| None = None, vcs_outcome: VcsOutcome) -> DeliveryReceipt` | 实现既有 FlowToolCaller，创建固定身份请求及原 Future。 |
| `async def next_tool_call(self) -> PendingToolCall` | 等待并借出未完成队头；不出队；跳过已取消的未发送调用。 |
| `def finish_tool_call(self, call: PendingToolCall, response: FlowToolResponse) -> None` | 校验 call_id，消费匹配回执，按原分类完成 Future；已取消的 Future 不再写入。 |
| `async def stop(self, request: StopExecution) -> ExecutionStopped` | 校验完整身份，创建/复用 shielded stop Task；只在持久回执及回收成功后返回。 |
| `def acknowledge_stop(self, accepted: StopAccepted) -> None` | 原 acknowledge_stop 校验成功后退休内存停止操作。 |
| `async def shutdown(self) -> None` | 幂等等待停止操作及执行清理，消费并报告内部错误。 |

私有实现边界限定为以下职责；不再给每个 ExecutionState 方法增加转发包装：

```python
def _launch(self, assignment: Assignment) -> None: ...
def _execution_finished(self, task: asyncio.Task[ExecutionResult]) -> None: ...
def _collect_finished(self) -> None: ...
def _request_cancel(self) -> asyncio.Task[ExecutionResult] | None: ...
def _discard_tool_calls(self) -> None: ...
async def _stop_execution(self, request: StopExecution) -> ExecutionStopped: ...
def _stop_finished(self, task: asyncio.Task[ExecutionStopped]) -> None: ...
async def _shutdown(self) -> None: ...
```

`_collect_finished` 对尚未完成的 Task 不操作；自然完成时是唯一终态写入入口，成功后才清 Task。
`_request_cancel` 在第一次 cancel 前设置抑制收集标志；重试停止/关闭不得反复 cancel 正在清理的 Task。
done callback 先核对回调 Task 仍是 owner 当前持有的 Task，避免旧回调碰到后继 assignment；
每次新执行重新初始化取消标记。callback 必须消费异常并报告持久化失败，保留结果以供同步重试，
不让事件循环出现未检索异常。

`WorkerClient.__init__` 的 `executor/session` 两个参数替换为
`execution: WorkerExecution` 和 `read_history: Callable[[HistoryQuery], HistoryPage]`；其他参数与默认值不变。
删除 Client 的 `_execution`、`_tool_requests`、`call_tool`、结果收集、重排队和取消执行 helpers。
`run()`、`run_connection(connection: Connection)`、`stop()` 只关闭通信资源；通信观察 Task 的取消不传入 owner。
`_execute_assignment` 依次调用 `receive`、实际发送 Started、`start`，其余网络方法保留原签名。

`ExecutionState` 的既有状态 API 除类型改名外不变；`resume(self, connection_epoch: str,
*, executor_active: bool) -> Assignment` 增补下面的“注册后完成”分支。`read` 不改签名。
`WorkerExecutor.__init__` 只将 `session: WorkerSession` 改为 `state: ExecutionState`。
`WorkerRuntime.__init__` 新持有 `execution: WorkerExecution`；`run/stop` 签名不变。

### 关键时序、错误与竞态

**接收/开始。** Client 调用 owner.receive 写 received，await 当前连接发送 Started，再同步调用
owner.start 写 started 并 create_task；保留创建执行 Task 时的日志 context。send 失败不执行。
发送成功只说明发送调用返回，不说明 server 已提交 Started。send 返回与 journal 写入间崩溃、
started 写入与 Task 创建间崩溃，继续使用现有恢复决策，不自动重发 Started 或重新编码。[S1][S5]

**自然完成。** 执行 Task 的 done callback 调用幂等同步收集，不依赖 Client 是否存在。
`state.complete` 继续负责脱敏、构造 Succeeded/Failed、原子持久化和 digest。
在 `recovery`、`wait_terminal`、自然完成后的 stop/shutdown 边界再次收集，覆盖 callback 尚未运行
及写入失败窗口。只有成功落盘才清理结果引用；持久化异常不被转换成另一个业务 Failed。

**注册后完成。** Register 可能报告 started/active，执行随后完成，server 才返回 ResumeAssignment。
owner 先验证 job/token，再检查 journal。若已有同一内存 assignment 和 durable terminal，
`ExecutionState.resume` 只重绑定 epoch，随后重放结果；不要求仍有 Task，不伪报 executor_active，
不调用 executor。此分支要求 assignment/record 的 job、flow、token 一致，terminal 的 job/token
也一致，且没有 stopped、尚未 acknowledged；消息自身只含 job/token/epoch，不增加 flow 字段。
没有内存 assignment 的进程重启继续走原 ReplayResult 或 archive 恢复规则，不能用这条分支重建
普通编码任务。正在运行、已完成、archive 恢复三条分支明确区分，不用额外内存标志复制 journal 事实。
此分支是即时离线收集必需的配套调整；server 的恢复后接收循环本已接受终态。[S2][S5][S10]

**工具等待。** owner 的 deque 在发送期间仍持有原 pending 对象。Client 对队头创建的等待 Task
被取消时，无需 get/归还；故“请求就绪与断线同轮发生”不会丢请求。重发只替换请求副本 epoch。
同一连接仍串行发送工具请求；收到错误 call_id 仍报协议错误，不凭“Future 已完成”吞回复。
MCP 退出取消在途 Future 时，原连接仍用其持有的 pending 身份消费匹配迟到回执，再发送终态。
owner 结束执行/停止时清理整个 backlog；连接局部指针不赋予它重新入队的权力。[S1][S7]

**显式停止。** Client 负责验证当前连接 epoch；owner 总是核对 journal 的 job/flow/token 和
完整停止身份，再决定 join 进行中的 stop Task。不可只按相同 request_id 复用。
owner 的停止 Task 在取消执行前标记取消来源，沿用 15 秒等待预算；超时或清理异常继续 blocked，
保留 Task/证据，不把清理异常或取消后的返回值当成普通 Failed/Succeeded。
顺序为：执行退出确认 → record_stopped 成功 → 清执行引用 → discard tools → reclaim_workspace
成功 → 返回 receipt → Client 发当前 epoch 副本 → 校验 StopAccepted → acknowledge_stop。
通信方只 shield 等待；断线不取消这项清理操作。receipt 写入失败保留执行引用；reclaim 失败保留
receipt、不给 server 发送成功回执。新连接可 join 未完成操作；完成/失败后的重试按持久证据继续。
StopAccepted 丢失时仍走原回收/重放规则，不引入新的落盘 cleanup-complete 标记。[S1][S6]

**完成与停止竞争。** 自然完成已成功落盘时允许 terminal 与 stopped 并存；owner 请求取消后结束的
执行不再收集终态。先有 stop intent/receipt 的注册恢复优先走 server 现有停止分支；未改其状态机。
server 原本也接受停止期间到达的终态，但本阶段不增加“每次 stop 必须先发送 terminal”的新顺序。
保持原停止握手与 ACK 重放，未发送的终态不改变 server 的停止确认决策。实施验证必须覆盖这些交错，
不能用 callback 的调度顺序代替业务判定。[S6][S11][S12]

**RecoveryRejected。** 保留 clear_rejected 对 received provenance 和 archive 证据的处理。
clear 成功后，在任何 await 前阻止结果收集，包含 Task 已 done 但 callback 未运行的窗口；archive
拒绝导致 clear 抛错时不擅自取消执行。取消并等待属于 owner，连接观察者退出不造成第二次 cancel；
完成回调清理已被拒绝的内存执行，避免取消等待被打断后永久 busy。即使 journal 已清除，只要原执行
还没退出，owner 就必须拒绝新 assignment；取消观察者不能取消该 join 所保护的执行清理。[S1][S2]

**进程 shutdown。** Runtime 停止并 join 通信及查询任务，然后 await owner.shutdown，最后释放
健康任务/信号和 root lock。shutdown 是幂等受保护操作；反复取消外层等待也必须先 join 内部清理，
并消费其 result/exception。若执行早已自然完成，优先落盘；落盘最终失败应报告退出错误，但仍先完成
进程清理。普通 shutdown 不 reclaim workspace，不合成停止/结果 ACK；已获授权的在途 stop 则继续
完成其清理并保留回执。保持现有进程关闭等待策略，不新增 timeout 配置。
复用 VCS/Codex/Amp 原有清理路径，不扩大对脱离进程组或恶意后台进程的保证。[S3][S8]

### 兼容性和明确变化

- `app/contracts/protocol.py` 不改：wire v15、terminal_digest、消息字段和 ACK 含义均保留。
- `assignment-journal.json` 路径和 v4 实际 JSON 不改；保留权限、原子替换、文件及目录 fsync、
  root identity、secret 脱敏、archive/revision/workspace 证据。不增加迁移、双写或兼容包装。[S2][S4]
- `ResultAccepted` 仍表示 server 已完成原有结果应用；`StopAccepted` 仍是停止确认的唯一退休凭据。
  socket 关闭、普通 reply、Task 消失都不代替这些确认，不改变 Flow binding 的存活期。
- 有意改变的是所有权与时机：Client 退出不取消执行；离线完成主动落盘；工具队头在回执前不移走；
  已授权的 stop 清理不受连接取消影响；停止回执落盘失败不能先丢执行引用。
  同步收集与取消来源标记用于避免把这些时机改变转化成新的终态或恢复语义。
- 不批量改写历史计划。实施时仅更新当前架构说明中相关段落，包括其仍写 journal v3 的过时描述；
  实际代码的 v4 是本阶段依据。[S2]

### 方案比较、成本与风险

| 选择 | 采用理由和成本 |
| --- | --- |
| 一个 WorkerExecution，ExecutionState 保留持久状态 | Task/Future/cleanup Task 的生命期有明确所有者；只增加一个执行编排对象，不把业务循环放进 Runtime，也不把通信对象延长为进程级执行容器。 |
| done callback + 同步收集 | 离线也能落盘；保留原执行 Task 的执行结果和清理证据。避免 wrapper Task 混淆 journal 失败与 executor 失败；代价是明确处理 callback/recovery/stop 的交错。 |
| deque + Event 的队头借用 | 取消通信观察者不改变请求所有权，删除 Queue.get 后的归还分支；保留串行 FIFO，不增加通用消息队列或请求注册表。 |
| owner 的 shielded stop Task | 连接断开不能取消执行清理或 workspace 回收；Runtime 能 join 它。代价是保存一个完整停止身份和 Task，并处理重复请求/异常。 |
| 下一阶段才抽 Session/Gateway | 本阶段用完整 Client 替换证明边界，避免同时变协议和双端调度，减少故障归因范围。 |

只改名不能通过 Client 替换验收；把全部逻辑移入 Runtime 会混合业务与进程生命周期；保留旧 Client
当后台执行容器仍会把 tool_caller、socket 和执行关联；通用 actor/event bus 会增加本阶段不需要的机制。

主要风险：结果 callback 提早退休 Task 的恢复竞态、停止期间重复 cancel、持久化失败丢失内存证据、
工具迟到回复误投、内部接口改名漏掉脚本、shutdown 提前释放根锁。使用下面的行为矩阵验证。
队列仍为执行范围内内存队列，不新增容量策略；SIGKILL、持久磁盘故障和未知旧进程仍可能需要人工恢复。

### 获批后的实施顺序和交付

1. 确认 docs-only 发布权限和计划；为本阶段建立一个 bookmark/PR，后续实现沿用它。
   共享原工作目录的现有 jj change/bookmark 属于 Web 工作：不得 describe、split、rebase、移动其
   bookmark 或将其推入本 PR。使用同仓库独立 jj workspace 从重新核对的 `main@origin` 开始；
   若只获准继续使用原目录，先解决修改隔离再提交，不用整目录提交代替隔离。
2. 用既有用例固定基线，迁移 state 模块/类型和直接引用；不新增旧名字的转发层。
3. 引入 owner，并一起迁移 Task、完成收集、工具等待、stop、recovery 和 Client/Runtime 接线。
   同时适配既有行为测试，不以访问私有字段的断言代替 execute 次数、原工具等待者结果、wire/journal 和进程事实。
4. 补足 Client 完整替换、Started 失败与 Runtime shutdown 的合同缺口；重用/参数化已有场景，
   避免每个 helper 一个测试。同步当前架构说明与运维脚本的直接引用。
5. 运行相关与完整回归、静态检查、构建、简化对照；记录实际结果，再按 workflow 进入代码审查。
   不在此阶段合并、部署或执行真实 GitHub 副作用。

除四个主要模块外，直接引用范围包括：worker/client/session/runtime/executor 测试、
stop、archive-worker-recovery、workspace-archive、revision-delivery、remote/conversation E2E、
controller-update-boundary；`scripts/smoke/recovery.py`、`scripts/smoke/kubernetes.sh`；
`docs/architecture/components.md` 与 `docs/operations/cancel-drain.md` 的当前说明。
保留 `app/controler/web/` 和 `docs/plan/web-console-kanban.md` 的其他工作，禁止顺手格式化或混入提交。

没有存储迁移或 wire rollout。正常停止旧进程后部署新构建；回滚也遵循原恢复限制，
不得为了回滚重跑 started 编码任务。此计划不授权实际部署。

### 验证矩阵与简化对照

以下为获批后的验收，不代表已经通过。优先保留现有行为断言：[S7][S9]

| 场景 | 验收观察 |
| --- | --- |
| 完整 Client 替换 | 使用真实 `Client.run()` 退出路径而不只换 run_connection；旧通信全部 join，新实例注入同一 owner。原 executor 只启动一次，执行不被取消，epoch-2 心跳正确。 |
| 排队/已发工具重连 | 同轮就绪与断线、发送后回执丢失；保留原 call_id，原调用协程收到结果，下一 assignment 不带旧请求。 |
| MCP 取消后的迟到回复 | 原 socket 消费匹配回复，不写已完成 Future；正常继续发送终态；不吞错误 call_id。 |
| 无 Client 时完成 | 成功/异常都在创建新 Client 或调用 recovery 前写 journal，secret 已脱敏；首次写入失败后 recovery 重试，不二次 execute。 |
| Register 后、ResumeAssignment 前完成 | 新连接收到 ResumeAssignment 仍只重放已持久终态，digest 不变，未重新创建执行。 |
| Started 发送失败/取消 | journal 为 received，executor 未运行；server 后续恢复行为与旧规则一致。started 落盘失败也不得启动。 |
| 显式 stop 与 Client 替换 | 停止等待或 reclaim 阶段断线，owner 清理继续；同一完整身份 join；不同 job/token/request_id 不复用。真实父子进程退出，回收成功才发 receipt。 |
| 停止失败和完成竞争 | 15 秒超时、清理异常、record_stopped 写入失败、自然完成先于 stop；不误写取消产出的终态、不丢引用、不提前发成功回执。 |
| stop ACK 丢失/进程重启 | 原 nonce/stop_request_id 回执可重放；只有 started 且无 Task 时保持 ownership unknown；终态与 stop 回执并存时沿用 server 停止优先分支。 |
| Runtime shutdown | 复用真实无害子进程检查；清理结束前第二 root owner 仍拿不到锁，结束后可拿；普通 shutdown 无 stopped/伪造终态，重复取消不截断清理。 |
| archive/recovery/Flow binding | 原归档断点、lost ACK、server 重启、后继 dispatch 对账、revision fallback 证据保持；通信退出不释放 Flow binding。 |
| 历史和装配 | 原查询并发/游标/连接清理、Codex/Amp 配置及 worker/control 导入边界不变。 |

永久测试只补用户依赖的生命周期合同；持久 JSON/wire 比较、错误窗口和消融优先使用临时验证，
已有测试可覆盖时不保留另一套重复测试。遵循前序 #62 删除新增“旧版本兼容测试”的决定，
不会以改名为由恢复那项测试。v4 格式仍是硬约束：用相同输入、固定 nonce/root 的临时比较核对
所有 JSON 字段/默认值/嵌套终态、digest、脱敏和 ACK 保留规则，不只比较 version 数字。

检查顺序：

- 定向：`test_worker_client.py`、`test_stop_protocol.py`、`test_worker_session.py`、
  `test_worker_runtime.py` 及直接受影响的 executor/archive/revision/history/进程和边界用例。
- 双端：原 gateway、worker_connection、result/recovery/concurrency、remote E2E、
  controller_update_boundary 和 archive_worker_recovery。使用隔离的本地测试 PostgreSQL，
  不连接生产库；没有安全测试库时明确标记 PostgreSQL 验收未完成。
- 全量：`uv run pytest`、`uv run ruff check app tests`、`uv run mypy app tests`、
  `uv build --no-sources`。构建与全量检查在隔离 workspace 进行，不修改共享目录的 Web 产物。
  格式检查限本次变更，使用仓库既有规范，不全仓重排。全仓搜索旧运行引用并检查构建包包含新模块。

实现后、代码审查前做少量可证伪的简化对照，每次只改变一个机制，用同一确定性输入：

1. 把执行取消临时放回 Client.run finally：完整 Client 替换应失败；取消观察者的控制组应保持执行。
2. 取消 done callback，延迟到 recovery 才写 journal：无 Client 的离线落盘断言应失败；
   正常方案还须通过 Register/Resume 交错。
3. 将工具 peek 临时变成取走队头：同轮断线及发送后重连应暴露原 waiter 丢失；正常方案不用归还机制。
4. 将 stop 清理改为连接直接 await：替换 Client 的清理断点应失败；取消来源/receipt 写入失败用故障注入
   验证不能退化为 Failed 或丢失所有权证据。

若某机制删除后全部行为仍成立，先简化实现；若某对照不能稳定触发，修正场景再给结论。
不以代码行数、字段名或委托调用次数作为重构成功指标。

### 审批事项、实施细节与延后项

**需要用户批准：**

1. 采用 `WorkerExecution + ExecutionState` 边界；Runtime 只装配/关闭，Client 不再拥有执行。
2. 接受上述三个必要机制：主动完成收集及注册后完成分支、执行范围工具队头借用、owner 拥有停止清理 Task；
   严格保留 wire/journal/server 合同。
3. 是否发布本阶段 docs-only draft PR。发布计划不等于批准实现；只有明确批准实施后才写代码和测试。

**实施时解决，不需要另立设计：** 测试辅助装配、日志上下文位置、机械 import/mock 更新、
确定性故障注入方式。若实际需要改协议、journal 字段、server、执行隔离或新增更多管理层，停止并追加计划修订审批。

**延后：** 公共 Session/Gateway；历史双端试点及历史读取入口进一步归位；envelope/通用关联；
outbox/跨进程工具恢复；新的队列容量、shutdown 预算或进程隔离策略。

## 2. PR change timeline

<!-- 初始方案共享后，仅追加真实的需求变更、设计决策和实施结果；不记录单纯审批或阶段切换。 -->

### 2026-10-28 — 确认事件入口命名

用户指出 `acknowledge_result` 的动作方向不清楚，已确认采用 `on_..._accepted` 命名：
通信层收到 server 的确认后，通知执行所有者处理已发生的事件；这些方法不发送 ACK。
本次确认仅限命名，整体计划及实现仍待批准。

本修订替代初始方案接口表中 `WorkerExecution` 的两个名称，签名与职责更新为：

| 精确签名 | 责任 |
| --- | --- |
| `def on_result_accepted(self, accepted: ResultAccepted) -> None` | 处理 server 已接受结果的通知；核对结果身份后调用原 acknowledge 规则，不无条件清除活跃 Task。 |
| `def on_stop_accepted(self, accepted: StopAccepted) -> None` | 处理 server 已接受停止回执的通知；原 acknowledge_stop 校验成功后退休内存停止操作。 |

初始方案“显式停止”时序的最后一步相应替换为：校验 StopAccepted → owner.on_stop_accepted。
`ExecutionState.acknowledge` 和 `ExecutionState.acknowledge_stop` 的既有状态 API 保持不变；
消息类型、wire v15、journal v4、身份校验、ACK 和恢复语义均不改变。

### 2026-10-28 — PR review：恢复入口与工具调用合同

回应 [`resume` 的作用](https://github.com/b1indsight/agentflow/pull/120#discussion_r4388984720)
及[工具调用签名和定义](https://github.com/b1indsight/agentflow/pull/120#discussion_r4398950724)。
以下补充初始方案的接口定义，不增加公开方法、协议字段或持久字段；实现仍待批准。

#### `resume` 与从中断状态初始化的区别

这里把评审中的“用打断的状态初始化”理解为：进程启动时组装对象并读取 journal。
构造 `ExecutionState` 只建立路径及内存依赖，`load()` 读取持久证据；构造 `WorkerExecution`
不启动执行。journal 不包含可恢复的 Python Task/Future，也不包含普通编码任务的完整 Assignment。
`recovery()` 将持久状态及实际执行存活事实交给 Client，用于发送 Register。[S13][S1]

`def resume(self, message: ResumeAssignment) -> None` 则由 Client 在收到 server 的恢复决定后调用。
它将同一个 assignment 绑定到获准使用的新 epoch。断线期间原执行可能一直在跑；通常不需要重新
初始化 owner，也不需要重启执行。消息只提供 job/token/epoch 和心跳间隔，不能凭它重建编码任务。[S15]

| 入口 | 调用时机与依据 | 执行效果 |
| --- | --- | --- |
| 构造对象、`load()` | 进程启动时建立对象；按需读取并校验本地 journal | 不创建执行 Task，不因读取而改写 journal，不认定旧进程已停止。 |
| `recovery()` | 每次 Register 前；结合 journal 和 owner 实际持有的 Task | 先收集已完成结果，再报告恢复描述；本方法不授予继续通信或重跑任务的权限。 |
| `resume(message)` | server 返回 ResumeAssignment 后 | 校验恢复身份，再按下面三种情况接续；同进程原 Task 和工具 Future 保留。 |

`resume` 先校验 job/token；如果 Task 已完成，先尝试收集并落盘，失败就报错并保留结果。
不能为了绕过恢复检查，把已完成但尚未落盘的 Task 当作活跃执行。收集成功后按实际状态处理：

| 当前事实 | `resume` 的处理 |
| --- | --- |
| 同一 assignment 的原 Task 仍在运行 | 只重绑定 epoch，不新建 Task，不重新调用 executor。 |
| Register 报告 active，回包前原执行已完成并落盘 | 原内存 assignment、journal、终态身份必须一致，且未 stopped、未 acknowledged；重绑定后由终态发送路径重放，不重新执行。 |
| 原 Task 不存在，journal 为 started 且保存了 archive_task | 沿用既有 archive 恢复条件，允许重新启动冻结的归档清理；不把该例外扩大为普通编码任务重跑。 |

其他状态不从磁盘猜测执行已恢复或已停止。普通 started 编码任务失去 Task 后，保持 server 的
拒绝/失败规则；即使错误收到 ResumeAssignment，本地也拒绝启动。[S5][S13]
已有 terminal 的进程重启仍走 ReplayResult/ResultAccepted，不靠构造器或 `resume` 重建编码执行。
全新 assignment 保持 `receive → 发送 Started → start`；`resume` 不重复这段首次接收流程。
因此，初始化提供本地证据，`resume` 处理 server 已作出的恢复决定，两个入口保留各自职责。

#### 工具调用的签名、调用方和完成条件

以下三个方法属于 `WorkerExecution`。`call_tool` 保持既有 `FlowToolCaller` 签名；后两个方法仅供
当前 Client 的串行连接驱动使用，不是给 MCP 或 server 调用的公共 RPC。[S14]

```python
async def call_tool(
    self, name: str, arguments: dict[str, object], *,
    call_id: str | None = None, vcs_outcome: VcsOutcome,
) -> DeliveryReceipt: ...

async def next_tool_call(self) -> PendingToolCall: ...

def finish_tool_call(
    self, call: PendingToolCall, response: FlowToolResponse,
) -> None: ...
```

| 方法 | 调用方 | 定义与完成条件 |
| --- | --- | --- |
| `call_tool` | 执行器中的 WorkerMCP，经 FlowToolCaller 调用 | 用当前 assignment 补齐 job/flow/attempt/token，按既有规则固定 call_id，复制 VCS 交付证据，创建一个 PendingToolCall 和原 Future，入队并唤醒观察者；等待 Future 返回 DeliveryReceipt 或抛出原分类错误。自身不收发网络消息。 |
| `next_tool_call` | 当前 Client 的连接驱动 | 队列为空时等待；有请求时借出队头原对象，不出队。返回只表示请求可发送，不表示已发送或已完成；没有请求也不合成 None/终态，由连接驱动同时观察执行完成或停止。 |
| `finish_tool_call` | 当前 Client，收到对应 FlowToolResponse 后 | 回传借出的原 call；同步核对 call_id，只移除同一个队头，并完成原 Future。返回 None；结果交给等待 call_tool 的调用者，连接驱动不直接操作 Future。 |

`name` 沿用当前 `flow.open_pr`，`arguments` 为该工具的参数；本阶段不扩展工具目录。
当前 MCP 在调用前验证 VCS 结果，并传入固定的 call_id；未提供时沿用原 `call_id or uuid4().hex`
规则。返回的 `DeliveryReceipt` 仍为 `PreparedDelivery | OpenedDelivery`，不保证每次都已开出 PR。[S14][S16]
`PendingToolCall` 仍只有 `request: FlowToolRequest` 和 `future: asyncio.Future[DeliveryReceipt]`。
Client 发送 request 的 epoch 副本，不改原请求身份、参数、VCS 证据或 Future，不把这些等待者写入 journal。

```diagram
┌──────────────┐  call_tool   ┌────────────────────────┐
│ Executor/MCP │─────────────→│ WorkerExecution        │
└──────┬───────┘              │ deque + 原 Future      │
       │ 等待原 Future        └──────────┬─────────────┘
       │                                 │ next_tool_call：借出队头
       │                                 ↓
       │                      ┌────────────────────────┐
       │                      │ Client：发送 / 等待回复 │
       │                      └──────────┬─────────────┘
       │                                 │ finish_tool_call：回传原 call 与回复
       └────────── Future 完成 ←───────────┘
```

取消、重连和迟到回复的精确规则：

- 断线只取消连接局部的 `next_tool_call` 观察者；原 call 仍在队头，原调用者继续等待。
  新 Client 使用同一 call_id 和新 epoch 重发，不再调用 `call_tool` 创建另一个 Future。
- `call_tool` 的调用者自行取消时，原 Future 可以取消。借出时跳过已取消的队头，包括旧连接曾尝试
  发送但调用者后来取消的请求。初始表中的“未发送”明确为“未被当前连接借出”，不新增 sent 标志。
- 同一连接借出一个请求后，先消费其回复或处理 StopExecution，不再借出下一个请求；原 Future 已取消
  也不能略过当前在途回复。连接取消后，旧 Client 必须完全 join，新 Client 才能借用 owner。[S7]
- `finish_tool_call` 先核对 call_id，错误时抛 WorkerProtocolError 且不消费队头。
  正常待完成的 call 必须仍是同一个队头；收到成功结果则 set_result，分类失败则
  set_exception(FlowToolCallError)，保留 kind/retryable/retry_after。分类错误不会自动触发传输重发。[S1][S15]
- 执行结束/停止可能已清理队列并结束原 Future。对应迟到回复只消费、不再次写 Future；不能弹出后继
  请求，也不能把旧 call 重新入队。即使 Future 已结束，错误 call_id 仍报协议错误。
- 无活跃 assignment、执行已结束或正在停止/shutdown 时，owner 不接受新工具调用；停止及结束后的
  等待者清理由 owner 负责。Client 不直接 cancel/set_result/set_exception 原 Future。
  执行停止仍沿用原取消和错误分类规则，不将通信观察者取消变成工具调用取消。

#### 同步后的基线与验收补充

处理本次 review 前已 fetch，并把本 PR 的独立 workspace 同步到远端 main
[`59a99d3`](https://github.com/b1indsight/agentflow/commit/59a99d368d85d099b108bcad9a94a9736016208b)。
初始方案的固定基线记载保留；当前实现依据包括随后合入的
[#115](https://github.com/b1indsight/agentflow/pull/115)：`_parse_journal` 对 v4 journal 内的合法 v14
回执只做内存规范化，读取不写文件，后续正常 ACK/状态写入保存 v15，摘要和身份校验继续保留。[S13][S17]
初稿“不恢复旧版本兼容测试”的说明不用于删除这一新基线已有的兼容逻辑或用例；本阶段迁移时一并保留，
不另增兼容版本范围。共享原目录的 HEAD、未提交 Web 工作和本 PR 的独立 workspace 不混用。

实施验收继续覆盖原矩阵，并明确检查：构造/加载不启动执行；Resume 前完成且首次落盘失败时不伪报
active、不新建 Task；取消观察者不取消原工具等待者；取消的旧调用不重放；迟到回复不完成后继调用。
此次仅复核源码并运行既有测试建立新基线，尚未验证新 owner，也未编写实现或测试。

### 2026-10-28 — 实现执行所有权边界

按获批方案与上述命名修订，在独立 jj workspace 实现；实施前再次 fetch，远端 main 仍为
[`59a99d3`](https://github.com/b1indsight/agentflow/commit/59a99d368d85d099b108bcad9a94a9736016208b)，
本 PR 已基于该提交。共享原目录保持原 detached HEAD 和 Web 未提交修改，未移动其工作 change。

- 新增 `execution/lifecycle.py` 的 `WorkerExecution`，统一拥有执行 Task、完成落盘、工具等待者及
  stop/shutdown；`WorkerSession` 迁移为 `state/execution.py` 的 `ExecutionState`，不保留旧入口。
- Client 保留连接、消息分流、epoch、心跳与 ACK 检查；移除执行取消、结果收集和工具 Future 操作。
  Runtime 只装配依赖、join 通信及 owner、最后释放根锁；`WorkerExecutor` 继续编排编码与 workspace。
- 完成回调在无 Client 时落盘；写入失败保留原 Task 结果，恢复时重试。补齐 Register 与 Resume 之间
  完成的同进程分支，普通重启无 Task 时不重新执行。取消来源标记防止 stop cleanup 的返回值成为终态。
- `wait_terminal` 用 `asyncio.wait` 观察执行，取消观察者不取消执行；执行异常统一由完成收集路径处理，
  不额外捕获并忽略异常。工具队头借用至匹配回复，删除断线后归还队列的分支。
- stop Task 按完整停止身份复用并 shield；回执持久化先于退休 Task 和回收 workspace。
  Runtime 与 owner 都保护 join，反复取消外层仍等清理完成。错误保留证据并报告，未增加超时配置。

沿用既有测试的 wire、journal、调用次数、真实进程和 Flow binding 断言；将断线存活测试改为真实
`Client.run()` 退出并替换实例，在旧实现上先观察到执行被取消，再在新 owner 上通过。
补充 Started 发送/持久化失败不启动、Register/Resume 完成与落盘故障、停止清理两阶段替换 Client、
Runtime 关闭时真实父子进程及根锁的回归。当前架构/停机说明、运维脚本与 changelog 已同步。
wire、数据库及 server 实现未改；公共 Session/Gateway、outbox 和历史 facade 仍延后。

### 2026-10-28 — 验证与简化对照

验证在隔离 workspace 及本机临时 PostgreSQL 上进行。首次测试库使用 ASCII 编码，后改为 UTF8；
全量中的 recovery bundle 校验要求 TCP loopback，Unix socket 不满足，随后改用独立端口的
`127.0.0.1` 连接，完整重跑通过。未修改产品代码绕过环境问题，未访问生产数据库或真实 GitHub 副作用。

- `PYTHONPATH="$PWD" FLOWTOOL_TEST_DATABASE_URL=<隔离 UTF8 PostgreSQL TCP DSN> python -m pytest -q` 全量通过，
  PostgreSQL 用例实际执行；既有 Starlette TestClient 弃用警告保留。
- `ruff check app tests scripts/smoke/recovery.py`、`mypy app tests`、`bash -n scripts/smoke/kubernetes.sh`
  和 `uv build --no-sources` 通过；wheel 包含新 lifecycle/state 模块，不再含旧 session 模块。
- 临时对比脚本读取计划提交的原 `WorkerSession`，同 root、固定 nonce 执行 received/started、workspace、
  revision/fallback、成功/失败、stop ACK、archive cleanup/ACK；实际 journal/wire JSON 逐字一致，
  包含 digest、null/default、脱敏及文件权限，归档 ACK 后同样移除 journal。
- 临时故障注入验证 receipt 写入失败、workspace 清理失败、停止超时后重试、拒绝恢复时观察者消失、
  自然完成与停止并存、结果 ACK 不退休 stop，以及重启无 Task 拒绝普通 resume；均通过。
  临时验证脚本不加入仓库测试套件。

简化对照使用临时 pytest plugin 逐进程替换一个方法，不修改生产文件；同一组输入运行
`pytest -p lifecycle_ablation -q tests/test_worker_client.py tests/test_stop_protocol.py
-k 'replacing_client or completed_disconnected'`。由 `LIFE_VARIANT` 选择以下单一变体：

| 变体 | 实际观察 | 决定 |
| --- | --- | --- |
| `control`：当前实现 | 所有选中场景通过 | 保留为控制组。 |
| `client-cancels`：Client.run finally 再 shutdown owner | 完整替换 Client 时原 executor 被取消 | 执行关闭只归进程 owner。 |
| `no-callback`：移除完成回调 | 离线成功/失败均不能在 recovery 前落盘 | 保留主动收集与注册后完成分支。 |
| `take-head`：next_tool_call 返回时出队 | 未发送及已发送重连场景都不能完成原 waiter | 保留借出队头，不恢复归还机制。 |
| `unshield-stop`：通信方直接 await stop Task | 执行退出等待和 workspace 回收两个断点都被通信取消中断 | 保留 owner-held shielded stop。 |

对照初跑发现工具丢失变体会一直等待发送，给该连接步骤加测试超时后重跑，以明确失败代替悬挂。
报告和复现材料见[实现线程](https://ampcode.com/threads/T-01a17c72-87bc-7645-b1c9-44ba1c5b61b7)
中的 `worker-execution-lifecycle` 验证产物。此处记录的是本次实现验证，不引用旧计划的测试基线作为结果。
不包含真实模型/GitHub 闭环、Kubernetes 部署或任意脱离进程组进程的清理承诺。

### 2026-10-28 — 审查修复：借出后、发送前的工具取消

独立代码审查指出：`next_tool_call()` 借出队头后，Client 恢复发送前，MCP 调用者可能取消原 Future。
此前只在借出时检查，已取消请求仍可能发送并等待回复，阻塞终态或保存废弃的 delivery 参数。
用户明确授权修复后，在发送入口补充 `call.future.done()` 检查：结束的请求不发送，重新创建队头
观察者并继续同时等待执行完成。检查到进入 send 之间没有新增 await；send 已开始的调用仍沿用
匹配迟到回复的消费规则，不声称可以撤回已发送请求。

Client 只读取 pending 调用是否仍有效，不创建、完成或取消原 Future；队列跳过和等待者清理继续
归 owner。采用局部检查，不新增 owner 转发方法、请求状态字段或消息类型。
新增参数化行为回归：在借出后确定性取消调用，分别让执行直接完成、继续下一工具调用；断言
废弃请求未到 socket、后续调用正常完成且终态获得 ACK。两个场景在未修复实现上均复现失败，
补检查后通过；该前后对照验证了新增分支的必要性，无额外机制需要消融。

完整 pytest（含隔离 PostgreSQL）、Ruff、Mypy 与包构建通过；既有已发送调用的迟到回复测试保持通过。
证据保存在实现线程的 `cancel-race-before.log`、`cancel-race-targeted.log`、`cancel-race-full.log`
及 `cancel-race-build.log`；初始定向运行跳过的 PostgreSQL 用例已在后续全量中实际执行。
本次修改不变更 wire、journal、server 或执行所有权边界，沿用原 PR 复审和交付。

## 源码依据（规划时的固定基线）

[S1]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/worker/transport/client.py#L89-L543
[S2]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/worker/state/session.py#L38-L294
[S3]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/worker/runtime.py#L92-L156
[S4]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/worker/state/files.py#L8-L28
[S5]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/controler/workers/gateway.py#L437-L546
[S6]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/controler/workers/gateway.py#L127-L173
[S7]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/tests/test_worker_client.py#L249-L562
[S8]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/worker/execution/amp_process.py#L178-L254
[S9]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/tests/test_stop_protocol.py#L51-L304
[S10]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/controler/workers/gateway.py#L221-L290
[S11]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/controler/workers/gateway.py#L338-L364
[S12]: https://github.com/b1indsight/agentflow/blob/fd5c10fbb1eb7b7f988e18ea95e156d3a08fed60/app/persistence/stops.py#L87-L145
[S13]: https://github.com/b1indsight/agentflow/blob/59a99d368d85d099b108bcad9a94a9736016208b/app/worker/state/session.py#L40-L182
[S14]: https://github.com/b1indsight/agentflow/blob/59a99d368d85d099b108bcad9a94a9736016208b/app/worker/transport/mcp.py#L25-L150
[S15]: https://github.com/b1indsight/agentflow/blob/59a99d368d85d099b108bcad9a94a9736016208b/app/contracts/protocol.py#L243-L394
[S16]: https://github.com/b1indsight/agentflow/blob/59a99d368d85d099b108bcad9a94a9736016208b/app/domain/delivery_models.py#L1-L24
[S17]: https://github.com/b1indsight/agentflow/blob/59a99d368d85d099b108bcad9a94a9736016208b/tests/test_worker_session.py#L84-L184
