# Worker / server 统一消息协议（第四阶段）— Feature record

**Stage:** Implementation — 第一步协议/数据结构已实现并定向验证；第二步迁移待授权
**PR:** 第一步按用户“做当前plan，提pr”请求发布 draft；准确 PR/head 与 gate 结果见本线程交付记录
**Bookmark:** `refactor/unified-message-protocol`，计划和实现沿用一个文档 / bookmark / PR
**Workspace:** `.amp/in/workspaces/unified-message-protocol`（独立 jj workspace）
**当前方案:** 下方整合版是唯一实施依据；第一步获批，第二步仍为迁移合同；历史方案折叠保留

## 当前方案（2026-11-03）

### A. 目标、状态与范围

将 worker/server 的全部通信统一为：**顶层连接控制 + payload 条目列表**。业务与功能处理
由 `tool_call`、`tool_result` 及其组合表达，通过 `(type, method)` 路由到已注册的处理回调。
连接控制与业务可以同帧，各自保留接收、授权和完成条件。

| 状态 | 内容 |
| --- | --- |
| 用户已确定的方向 | 控制字段与 payload 同级且可同帧；ack 是控制接收确认的 bool，ack_id 指向控制帧；payload 为列表；业务统一 call/result；reply_to 表示回复目标；按 type/method 注册回调；工作只分协议/数据结构、实现迁移两步 |
| 授权边界 | 用户已授权当前线程第一步及提 PR；不自动启动接续线程的实现迁移，不授权合并或部署 |
| 实际实施状态 | C–E/J 结构已实现并完成第一步定向验证；运行时未接入，完整 pytest/Mypy 失败，属于不可运行、不可部署的中间态；详见下方实施记录 |

本次重新 `jj git fetch --remote origin`，最新 `main@origin` 与 workspace 父版本仍为
[`b99c8e39`](https://github.com/b1indsight/agentflow/commit/b99c8e39f6d391b5685a91a8d6c428c51810bb11)。
不以根 checkout 或本地 main 为基线；嵌套 workspace 无独立 `.git`，git 可能误读根仓库。
本阶段继续在上述独立 jj workspace 开展，保留根工作区的 Web 变更及其他线程工作。

**第一步由当前线程修改协议结构和数据结构**：C–E 的帧/条目、方法、身份，以及 J 的持久模型、
纯编解码与摘要合同。**第二步由接续线程迁移实现和调用方**：F–I 的收发/分派/业务行为，
J 的实际存储读写、SQL 与恢复接入。整体仍覆盖全部 19 种消息、双端收发、持久化与回归。
这是同一 feature 内的两步，不拆成独立发布；第一步是未集成中间态，第二步完成前不能作为
可运行或可部署版本。不为第一步单独运行增加旧 wire 兼容层、别名或过渡包装。

Runtime 装配完全对称后置；可靠领域事件/outbox、中间件、event sourcing、多 server HA、
跨进程 Future 恢复不在本阶段。MCP JSON-RPC、HTTP console、GitHub webhook 外部协议不改。
不新增 Endpoint/Run/Context、通用 RPC 平台、依赖、消息去重表或自动传输重试。

前序合同：[执行所有权](worker-execution-lifecycle.md)、[Session](shared-communication-session.md)、
[Gateway 与最终关闭修复](shared-communication-gateway.md)。

### B. 所有权与不可改变的合同

| 所有者 | 保留的职责 |
| --- | --- |
| Gateway | 进程级登记、连接路由与替换；A→B→C latest-wins、旧 finally 身份安全 |
| Session | 单连接 reader、FIFO、实际发送、临时查询关联、限额与通信任务 close/join |
| WorkerExecution | 跨重连执行 Task、原工具 call_id/Future、离线完成收集、shielded stop 与清理 |
| ExecutionState | journal、持久执行和停止证据；不接管 Task/Future |
| server | Flow binding、业务权限、结果应用与停止确认 |

回复固定原 Session，不经 Gateway 改投后继连接；临时查询断线即失败，不跨连接重放。
连接退出不取消执行、不释放 binding。server transport.close 仍**只**处理 WebSocketDisconnect，
其他错误与取消继续传播，公共通信层不引入框架异常。[S2][S5][S9][S10]

执行接入必须保持 `record_received → await Started 对应条目实际发送 → record_started → launch`。
发送失败、取消或 guard 拒绝不得启动；实际发送成功与 record_started/launch 之间不插入 await。
只返回“待发送列表”就启动 Task 不满足合同。[S3][S5]

Registered 前恢复/停止、Register 后 resume 前离线完成、普通 started 任务失去 Task 后不重跑
均保留。Gateway 登记、控制 ACK、历史查询均不授予业务 ready 或 assignment 权限。
结果接纳仍在业务应用提交后；停止接纳仍满足原完整证据与 effects 条件。[S4][S8][S12]

### C. 帧与条目的定义

**顶层共同字段全部必填；空值显式为 null。** 本地构造器可以填值，parser 不补缺失元数据。

| 字段 | 类型与含义 |
| --- | --- |
| protocol_version | `Literal[16]`，新在线格式 |
| message_id | `MessageId`：32 位小写 hex，`^[0-9a-f]{32}$`；帧身份，每次投递构造新 UUID4 |
| connection_epoch | 1–64 字符或 null；Register、纯 ACK 为 null，ProtocolError 可空，其他非空 |
| ack | 严格 bool，不接受 0/1 或字符串；仅表示携带控制接收确认 |
| ack_id | MessageId 或 null；当且仅当 ack=true 时非空，指向本 Session 收到的控制帧 |
| control | register / registered / heartbeat / error / null；操作字段也在顶层 |
| payload | 方向明确的 typed 列表，无业务时为 `[]` |

不再有四分类 kind、外层业务 reply_to/correlation_id、重复的 control/business 标签。
不为本次协议增加 timestamp、trace、重试次数或通用 expects_reply 标志。

| control / 方向 | 顶层操作字段 | payload 约束 |
| --- | --- | --- |
| register，W→S | `worker_id: str`、`recovery: RecoveryDescriptor \| None` | `[]` 或 H 节限定的单个结果接纳调用 A；ack=false |
| registered，S→W | `heartbeat_interval: float`、`recovery: RecoveryDecision \| None` | recovery=null 可带合法条目；resume/replay 分支为 `[]` |
| heartbeat，W→S | `job_id: int`、`assignment_token: str` | 可带业务；执行条目 job/token 与心跳一致 |
| error，S→W | `code: str`、`detail: str` | `[]`；致命通知，不等 ACK 才关闭 |

RecoveryDescriptor 不新增字段。RecoveryDecision 仅含 `action: resume|replay`、job_id、
assignment_token；heartbeat_interval 与 epoch 使用共同控制字段。Registered 自身是握手决定，
允许 ack=false；对 Register 的接收 ACK 可以已经独立发出。单连接仍只有一次初始 Register。
Heartbeat 仍是携带 job/token 的 lease 心跳，不能为了合帧伪造心跳或改成无条件 ping。[S1][S4]

| 条目 type | 共同字段与内容 |
| --- | --- |
| tool_call | message_id、`reply_to: null`、method、严格 typed arguments |
| tool_result | message_id、非空 reply_to、method，以及该方法自己的严格结果字段 |

条目共同键也必须出现；结果的 reply_to 指向具体调用条目 ID，不能等于自身 ID。
method 回显调用方法。call/result 不保证每次调用都产生结果：Started 和恢复拒绝采用静态约定
的单向方法，不新增返回或 ACK。每条结果仍核对实际调用和相应业务权限。

方向 parser 严格验证整个控制及全部条目，再允许任何分派：extra=forbid、ID/版本/方向、
method 与模型、ACK 配对、同帧重复条目 ID、限额和控制组合均检查。空帧
`control=null,payload=[],ack=false` 非法；纯 ACK 则 epoch=null，不进入业务处理。
具体控制模型直接共享消息头，payload 为方向联合；不造 all-optional 字段袋或每方法一层 envelope。
Pydantic 的 null control 判别、列表序列化与类型窄化须在获批后验证，不以 Any/cast 绕过。

### D. 方法、参数与全部旧消息映射

下表 call 的业务字段都位于 arguments；result 则按表列出的严格结果形状表达。
原 task/result/secret、默认值、Flow 一致性与 exactly-one outcome 校验保留。[S1]

| 方向 / type / method | typed 业务字段 |
| --- | --- |
| S→W call worker.execute | job_id、flow_id: int；assignment_token: str；task: ExecutionTask；secret: AssignmentSecret 或 None |
| S→W call worker.stop | StopFence：request_id、assignment_token: str；flow_id、job_id: int |
| S→W call worker.history | session_id: SessionId；cursor: str 或 None，默认 None、长度 1–1024；limit 默认 100，范围 1–500 |
| S→W call worker.reject_recovery | job_id: int、reason: str、register_message_id: MessageId；单向 |
| W→S call server.execution_started | job_id: int、assignment_token: str；单向 |
| W→S call server.accept_result | job_id、assignment_token、result_id=T |
| W→S call server.accept_stop | 完整 StopFence、receipt_id=R |
| W→S call 原 Flow 方法，如 flow.open_pr | job_id/flow_id/attempt_id: int；assignment_token/call_id: str；arguments: dict[str, object]；vcs_backend: VcsKind；branch/commit_sha/remote_sha: str |
| W→S result worker.execute | 平铺 job_id、assignment_token、result: ExecutionResult 或 None、error: str 或 None；result/error 恰好一个非空 |
| W→S result worker.stop | result：完整 StopFence 加 execution_nonce，nonce 原长度 1–128 |
| W→S result worker.history | page: HistoryPage 或 None、error: ConversationError 或 None；恰好一个非空 |
| S→W result server.accept_result | result：job_id、assignment_token、state，state 限 done/failed |
| S→W result server.accept_stop | result：完整 StopFence |
| S→W result 原 Flow 方法 | 原 call_id、result: DeliveryReceipt 或 None、error/error_kind、retryable 默认 false、retry_after；保留默认 None 和全部错误校验 |

Flow method 直接使用原 name，不套 flow.tool/name。已知方法先严格定型；其他 W→S 调用只有
符合原 FlowToolArguments 完整形状的未知工具请求才进入定型回退，返回原 INVALID_REQUEST。
未知 worker 生命周期/查询形状及其他非法路由拒绝；不按方法字符串反射调用 Python 方法。
不额外引入方法前缀拒绝规则改变原未知工具错误。Flow 参数语义仍由 FlowToolService 校验，
cursor 语义仍在历史查询内校验，均不升级为整连接解析错误。[S6][S7]

| 原 wire 类型（19 种） | 当前目标 | 业务 ID / reply_to |
| --- | --- | --- |
| Register | control=register | 帧 ID |
| Registered | control=registered，recovery=null | 帧 ID |
| ResumeAssignment | registered.recovery.action=resume | 帧 ID |
| ReplayResult | registered.recovery.action=replay | 帧 ID |
| Heartbeat | control=heartbeat | 帧 ID |
| ProtocolError | control=error | 帧 ID |
| Assignment | call worker.execute | E / null |
| Started | 单向 call server.execution_started | UUID4 / null |
| Succeeded | result worker.execute | T / E |
| Failed | result worker.execute | T / E |
| ResultAccepted | result server.accept_result | UUID4 / A |
| FlowToolRequest | call 原 Flow 方法 | PendingToolCall 中的 ID / null |
| FlowToolResponse | result 原 Flow 方法 | UUID4 / 原调用 ID |
| StopExecution | call worker.stop | C / null |
| ExecutionStopped | result worker.stop | R / C |
| StopAccepted | result server.accept_stop | UUID4 / B |
| ConversationQuery | call worker.history | UUID4 / null |
| ConversationResult | result worker.history | UUID4 / 查询 ID |
| RecoveryRejected | 单向 call worker.reject_recovery | UUID4 / null，参数关联本 Session 的 Register 帧 |

### E. 身份、幂等与重放

确定性 ID 使用固定 namespace `46ebd500-2f34-463f-b746-f5c03b53b0ac`，计算
`uuid5(namespace, json.dumps(parts, ensure_ascii=False, separators=(",", ":"))).hex`：

| ID | parts |
| --- | --- |
| E，执行调用 | `["worker.execute", job_id, assignment_token]` |
| T，执行终态 | `["terminal", job_id, assignment_token, result_digest]` |
| A，结果接纳调用 | `["server.accept_result", T]` |
| C，停止调用 | `["stop_execution", request_id, flow_id, job_id, assignment_token]` |
| R，停止回执 | `["execution_stopped", request_id, flow_id, job_id, assignment_token, execution_nonce]` |
| B，停止接纳调用 | `["server.accept_stop", R]` |

标签/namespace 不随 wire 版本变化；ID 不含 epoch、时间、投递次数或 secret。secret 脱敏先于
digest/ID。UUID5 仅作标识，不能替代权限、SHA256 内容校验或原业务幂等。

| 身份 | 边界 |
| --- | --- |
| Session + connection_epoch | 本次连接隔离；不等于 assignment 授权 |
| job_id + assignment_token | 派发与恢复授权 |
| 帧 message_id / ack_id | 帧与控制接收确认；恢复拒绝也显式引用 Register 帧 |
| 条目 message_id / reply_to | 具体调用与结果关联；不创建通用去重表 |
| 工具 call_id | 原 ledger 幂等，仍核对 flow/attempt/job/name/arguments |
| stop request_id + 完整 fence + nonce | 持久停止请求及真实执行证据 |
| result_digest | 终态冲突及 application/effect 绑定 |

重连只替换帧 ID/epoch，保留需重放的条目 ID。E/T/A/C/R/B 可从既有持久事实重建，不加存储列。
PendingToolCall 保留调用条目与原 Future，条目 ID 只存一份；call_id 仍在业务参数内。
新的 call_tool 创建新条目/Future；同进程重连借用原调用。业务重试可沿用 call_id，但要有新
条目 ID；server 仍按 ledger 判定。工具回复从 ledger 结果重新构造，不缓存旧连接帧。[S5][S7]

### F. 消息流水线与注册回调

**F–I 保留为第二步的迁移合同。** 第一步只提供所需协议与数据类型，不实现回调分派、ACK sender、
队列调度、业务消费循环或接纳/恢复流程；这些行为与安全条件由接续线程实现和验证。

每个方向显式登记 `(type, method) → 一个 callback`，可直接使用现有实例绑定方法。
重复 key、key 与模型不符均拒绝；没有广播、每方法一个 Handler 类或第二张 peer 表。
拟新增 `app/communication/dispatch.py`，只放注册/分派与薄类型适配：给定严格模型，
用 isinstance 窄化后调用 typed callback，不使用 Any、cast、getattr 或通用 ParamSpec 框架。

| 路由 | 处理实例 / 调度 |
| --- | --- |
| worker.execute / stop / reject_recovery 调用 | WorkerClient 业务入口，调用原 WorkerExecution |
| worker.history 调用/结果 | 原历史回调与 Session 临时 pending，有界旁路 |
| Flow 调用 | WorkerHandler 阶段/fence 检查后交 FlowToolService |
| Flow 结果 | WorkerClient 匹配原 PendingToolCall，调用 finish_tool_call |
| worker.execute 结果、server.accept_result 调用 | WorkerHandler 的 inbox/application 与接纳处理 |
| worker.stop 结果、server.accept_stop 调用 | WorkerHandler 原 stop 处理与 StopStore |
| server.execution_started 调用 | WorkerHandler 当前派发握手 |
| server.accept_result / accept_stop 结果 | WorkerClient 核对 A/B，再调用 WorkerExecution 接纳入口 |

查表决定“调用哪段代码”；reply_to 决定“哪个具体调用”；阶段、epoch、job/token、call_id、
完整 fence 与提交事实决定“能否处理”。同 method 的多个调用不能相互完成 Future。
原 Session、帧/条目直接传入回调或由连接局部函数捕获；server current 三元组仍是连接局部值，
不得放进共享 Handler 的全局可变字段。[S3][S4][S5]

**处理顺序：**

1. Session 是唯一 socket reader。先严格验证整帧、容量及静态跨字段约束，再登记控制接收 ACK。
   首个 Register 仍由 transport 在 reader 前认证/校验，并显式登记一次。
2. 纯 ACK 被 reader 消费；不进业务 FIFO/pending，不绑定 epoch、ready，不延长 lease/业务超时。
3. 无新控制时，逐条执行原 history pending/responder 旁路；每次只消费一个条目。
   迟到历史结果沿原规则忽略，其余业务进入 FIFO，不因 query 命中吞掉整帧。
4. 有新控制时，原 owner 先处理控制；通过后逐条调用同一分派入口，控制失败不执行 payload。
   reader 与控制 owner 二选一分派，每项恰好一次；Heartbeat 分支不能 continue 掉剩余业务。
5. 同一执行的业务按列表和帧接收顺序处理。查询仍可有界并发，不对全部回调 gather/create_task。
   控制已完成而后续条目失败时不回滚；不新增“捕获所有异常继续下一条”的中间件。

worker 原 incoming/terminal/next_tool 多路复用提升到**同一个业务消费循环**，回调只完成当前
状态转换后返回。不能把整个 `_execute_assignment` 挂成串行回调，又嵌套 receive 等待排在后面
的工具/接纳回复。execute 回调显式遵守 B 节实际发送顺序；stop 回调可等待原 shielded 清理，
发送 `[R,B]` 后返回，由后续 B 回复完成确认。通信观察任务取消/join 不取消执行 owner 的 Task。
原发送 guard、提前回复仍等实际写完成、取消/迟到回复不影响后继 Future 均保留。[S2][S3][S5]

### G. 控制 ACK 与批量资源限制

Register、Registered（包括 recovery）和 Heartbeat 产生控制接收 ACK；ProtocolError 不要求。
ACK 本身、纯业务条目不触发 ACK；同帧带 ACK 和新控制时，只确认新控制，不形成 ACK 循环。
ack=true 不表示 lease 延长、任务启动、结果接纳或停止完成。

Session 新增有界的控制 ID FIFO（上限 32）与一个 sender：下次写可顺带队首 ACK，没业务出站
时发送纯 ACK，不无限等合帧。reader 只入队，不等待网络写。容量检查与登记之间不 await，
溢出按 SessionOverflow 关闭，不丢旧项。没有 outstanding 表、逐条 Future、重试或定时器。

ACK 在持有既有发送锁、通过最后一刻 guard 后选取附加，不修改调用方帧；guard 与 transport
入口间不加 await。实际写成功才出队，guard 拒绝保留。纯 ACK 同样走 Session.send；close 清队列并 join
sender，旧 ACK 不跨连接重放。未知/重复 ack_id 无业务作用，也不能吞同帧 payload。
本阶段没有等待控制 ACK 的调用者，不承诺检测漏发 ACK 或增加可靠交付保证。[S2]

每帧最多 32 条业务；保留最多 32 个排队帧、16 MiB 原始字节，再限制最多 32 个排队业务条目。
整帧容量预检后再分派，不部分入队后静默丢弃。inbox 可用原帧和未消费下标的普通元组，
不引入 Context；原始字节只计一次，直到不再被排队项持有。旁路 pending/handler/busy 限额保留。
history 的 512 KiB 限额按所选结果条目加共享头计算，整帧另受总上限约束，避免无关终态误伤
合法历史页。Session.request 显式选择一个 history 调用，不给列表所有 call 创建 Future。[S2][S6]
不自动凑批、整批事务或整帧重放；重连只重放业务 owner 需要的条目，history 不随之重放。

### H. 结果接纳与注册恢复

worker 执行完发 `[T,A]`：T 是 worker.execute 的结果，A 是 server.accept_result 调用。
server 处理 T 时核对 E/T、worker/job/token/epoch/digest，持久 inbox 并调用 results.apply；
不能提前 break 吞掉 A，也不主动向 T 发接纳返回。[S4]

A 核对已持久结果及其导出的 T/A，**只有 result_applied_at 非空且 state=done/failed 才回复**。
在线 A 单独到达，可对当前已持久的同一结果走原 apply；ResultPending 仍走 1013 与原重连退避。
在线缺结果或 fence/ID/内容冲突按原协议错误拒绝，不伪造接纳成功。T 单独到达仍应用，
但当前 assignment 等待 A，不先派下一个 job；stop、独立查询和其他 worker 不因此停住。
旧 job 的重复 T/A 须经完整持久 fence 验证，不能清空新 job。仅当前 A 回复实际发送成功后
退出该 assignment；worker 核对 A/method/epoch/job/token/state 后才调用原 acknowledge。
普通 journal 保留 acknowledged，只有符合原条件的成功 archive 才删除。[S5][S12]

**Register 携带 A 的窄化规则：**

- worker 先收集离线终态。仅未 acknowledged 的 terminal、无 stopped 且无 stop/取消清理时，
  Register 带 `[A]`，不带 T；A 与 recovery 的 job/token/digest 一致。其他情况为 `[]`。
- 已确认且保留的 journal 仍发 recovery descriptor，不发 A；不省略归属核查。
- server 先认证/校验首帧，Gateway 替换/join/登记并 worker_connected，再按新 epoch 核验恢复。
  Register 的 null epoch 只允许这个恢复请求，不能绕过认证开放任意工具。
- stop 优先。DB 已提交且摘要匹配时回复实际 A，可先于 Registered；worker 可在原授权位置
  绑定 epoch，但不宣告 ready。无结果或未应用时走原授权检查后发 registered.recovery=replay，
  注册过程中不 apply；worker 再发送 `[T,A]`。矛盾/失效/摘要冲突沿原拒绝分支。
- 没有 A 但 descriptor 匹配已提交结果，只完成恢复核查与 Registered，不发送无人请求的结果。
- Register 收集状态后、resume 前才完成时，resume 再收集并发送 `[T,A]`，不重跑；包括首次
  终态落盘失败的原竞态。worker.reject_recovery 校验当前 Register 帧并完成原清理，才处理
  后续 Registered。恢复拒绝不单独授予 ready。[S3][S4][S5][S12]

下面是完整的失败终态例子；E/T/A 按 E 节导出，帧 ID/回复 ID 为示例值。

```json
{
  "protocol_version": 16,
  "message_id": "11111111111141118111111111111111",
  "connection_epoch": "epoch-2",
  "ack": false,
  "ack_id": null,
  "control": null,
  "payload": [
    {
      "type": "tool_result",
      "message_id": "7eecf358047357e3904fae12781b482c",
      "reply_to": "ee3eaa171e5652b8bff155694215e29b",
      "method": "worker.execute",
      "job_id": 5,
      "assignment_token": "assignment-1",
      "result": null,
      "error": "executor exited 1"
    },
    {
      "type": "tool_call",
      "message_id": "b7723aac9a2f534e822941852084c2f2",
      "reply_to": null,
      "method": "server.accept_result",
      "arguments": {
        "job_id": 5,
        "assignment_token": "assignment-1",
        "result_id": "7eecf358047357e3904fae12781b482c"
      }
    }
  ]
}
```

业务应用提交后，server 返回如下帧。执行失败仍可被成功接纳；这个返回不再要求下一层确认。

```json
{
  "protocol_version": 16,
  "message_id": "22222222222242228222222222222222",
  "connection_epoch": "epoch-2",
  "ack": false,
  "ack_id": null,
  "control": null,
  "payload": [
    {
      "type": "tool_result",
      "message_id": "33333333333343338333333333333333",
      "reply_to": "b7723aac9a2f534e822941852084c2f2",
      "method": "server.accept_result",
      "result": {"job_id": 5, "assignment_token": "assignment-1", "state": "failed"}
    }
  ]
}
```

两帧均可 ack=false。T 已持久而 A 未处理或回复丢失时不回滚；靠原 journal/inbox、digest、
应用锁与提交事实重放，不建 A ledger。新增 A 与 T 同发、返回替代原 ResultAccepted，
正常路径不增加一轮网络往返。

### I. 停止接纳与 stopping 阶段

server 发稳定 C；worker 完成原停止、子进程清理与 workspace 回收后发 `[R,B]`。
R 是 worker.stop 的结果，reply_to=C；B 请求 server.accept_stop，参数含完整 fence/receipt_id=R。
server 核验实际 R，并通过原 StopStore.confirm 的 worker/job/flow/token/epoch/nonce、binding、
pending result、已派发 effects 及提交条件，才向 B 返回完整 StopFence。[S8]

DB 只存 receipt_digest，没有完整 receipt/nonce，**B 单独到达不能凭 hash 接纳**。
必须有同批先处理的真实 R，或原停止处理路径在同一连接内已验证的 R；否则拒绝。
R 分支不能提前 return 丢掉 B。重连仍按持久 stop intent 发同一个 C，worker 重放真实 `[R,B]`，
C/R/B 不变；Register 不单独带 B，不加回执存储列或通用 registry。
worker 从 journal 导出 R/B，核对 reply_to、method、epoch 与完整 fence 后 acknowledge_stop。
确认或 cleanup 不确定继续 blocked；不把新帧/回复元数据纳入 stop Task 复用的相等性。

停止阶段保留原准入：合法在途 Heartbeat 被吸收，不改为普通续 lease；Flow 调用不执行新副作用；
终态 T 仍 record/apply，合法 A 校验后消费但不回复。迟到 A 回复不能退休 stop，只有匹配 B 的
结果满足原停止确认合同。普通路由表不能绕过这些阶段检查。[S4][S5]

### J. 持久化版本、摘要和旧数据

**职责边界：** 第一步定义持久模型、纯 decoder、摘要与一致性校验；第二步接入实际读取、
正常写入时的惰性升级、ResultApplication/prove_reopen 与 source SQL。第一步不启用运行时
新格式读写，两步共同遵守下面的数据保留规则。

**提案版本：wire16 / journal5 / ResultInbox v1。** wire 严格只收 v16，旧持久数据在专用
durable 边界读取，不把网络 parser 放宽。草案修订不递增尚未发布的版本。

| 边界 | 保存内容与处理 |
| --- | --- |
| journal v5 | terminal/stopped 只存 typed 业务数据，其余 v4 执行、archive、revision、root/workspace、nonce、ACK/cleanup 证据全保留 |
| ResultInbox v1 | 严格 `{version:1, source:worker\|control, terminal:TerminalPayload}`；Job.result_payload 为 `dict[str, JsonValue] \| None` |
| terminal_digest | 原 sorted compact JSON 的 `{type:succeeded,result:...}` 或 `{type:failed,error:...}`；ExecutionResult 字段/默认值/编码不变 |
| tool ledger | 原 call_id、参数、归属、DeliveryReceipt 与冲突检查不变 |
| stop receipt_digest | 固定历史 v15 投影与字段顺序；与当前 wire 版本脱钩 |

journal/inbox 不保存帧、条目包装、message_id/reply_to、method、ACK/control 或 epoch。
workers 的 connection_epoch/protocol_version 等连接记录仍保留，继续用于原 DB fence。
业务模型仍可在 protocol.py 共用，但旧 Succeeded/Failed/ExecutionStopped 名称只作 typed
业务数据，不能作为独立 wire payload。显式投影业务字段，不对整条 wire 用 exclude 列表碰运气。
新增 `app/contracts/receipts.py` 定义 ResultInbox、严格历史 decoder 与固定停止摘要，
不拥有存储/连接/Task，不加泛化版本注册表。[S1][S8][S12]

**旧状态读取：**

- journal v5 或受限 v4；v4 嵌套 receipt 只接受已知 v14/v15，先验证完整历史形状、必填版本/
  epoch/type，再投影。terminal/stopped 并存时旧版本须一致；核对 job/token/digest、
  stop 的 job/flow/token/nonce、root_identity。一致性检查同时用于当前格式。
- inbox v1 或严格已知 v14/v15 terminal；旧 epoch=control 映射为 source=control，其他为 worker，
  核对行 job/token/digest。source=control 只允许 Failed；token=control 的原特例仅允许
  source=control 且 DB assignment_token=None，不伪造网络 epoch。
- 未知、损坏、mixed 或矛盾数据保留并拒绝；不先删未知字段，不转换成失败结果，不盲目清空。
- 读只做内存转换；下一次原本需要的状态/ACK 写才写 v5，新 inbox 写 v1。无 DDL、批量 UPDATE、
  启动清理或为迁移冒充 ACK。原 root 锁、私有权限、原子替换、fsync、secret 脱敏保持。
- ResultApplication 三处读取及 prove_reopen 全部改 durable decoder；旧 reader 不虚设自动
  移除日期。本轮未盘点生产状态，不能推断实际已存格式或数量。[S12][S13][S14]

**stop 摘要：** 固定按 protocol_version=15、request_id、flow_id、job_id、assignment_token、
type=execution_stopped、execution_nonce 的顺序及原 Pydantic JSON 编码写摘要；15 是摘要算法
常量。固定样本的完整摘要为 `6ca59855bc756a9dafaa69f4c04b0a33a81be55d1a43b1aa3b01c78018aa204c`。
已有裸摘要只在收到真实完整 fenced receipt 后核对已知 v15/v14 投影；匹配保留原值，
新摘要写固定 v15；都不匹配继续 blocked，不离线反推 nonce，不跳过 confirm 条件。
terminal 的既有 digest、result application plan/effect 关联一律不重算。[S1][S8][S12][S13]

**source SQL：** JobStore.lifecycle_job 继续排除“未启动且 control 产生的结果”，保留 null inbox、
worker 结果及已 started 的 control。新旧来源判断须有版本约束和 COALESCE，防止 NULL 三值
逻辑误排除；不写裸 source=control OR epoch=control。[S15]

### K. 两步职责、影响范围与取舍

实施只分以下两步，获得相应明确批准后执行；继续同一 feature record/bookmark/PR。

| 步骤 | 负责线程与修改范围 | 完成边界 |
| --- | --- | --- |
| 1. 协议结构和数据结构 | 当前线程：protocol.py 与新 receipts.py 的帧/条目/方法模型、方向 parser、E/T/A/C/R/B、持久数据与纯 decoder/digest；ExecutionRecord v5 的记录模型、Job.result_payload 类型 | C–E/J 结构合同及定向验证完成；不切换运行时收发、存储读写或业务流程，记录尚未迁移的调用方 |
| 2. 对应实现迁移 | 接续线程：Session/dispatch/ACK/query、worker/server 收发与生命周期、Gateway 必要类型调整、持久化/SQL/结果恢复/reopen，以及受影响脚本、fixtures 和当前文档 | 全部调用方接入新结构，旧 wire 路径移除；L 节整体验收与精确 code-bearing head review gate 通过后，按授权发布同一 PR |

第一步开工时核对 main@origin 漂移，从未改动基线保留 terminal/stop/旧记录黄金样本；结构验证
属于本步。第二步的全链路验证属于迁移完成条件，均不另列准备、测试或发布阶段。
第一步交接时如实列出未迁移调用方及检查断点，不用 Any/cast、跳过测试或兼容别名掩盖它们。

第二步接入中，epoch 从业务数据移到 Session/帧后，Coordinator 准备与 FlowToolService 两次 DB fence 仍通过
显式原连接 epoch 校验；typed persistence 在边界构造 inbox/digest，避免调用者分开传不一致
的 JSON/摘要。不能只迁移 live 路径，遗漏后台 result recovery/reopen。[S7][S11][S13][S14]
脚本更新仍不扩大 recovery drill 的安全范围。

| 选择 | 理由与成本 |
| --- | --- |
| 控制 + call/result 列表 | 单帧承担两种作用；代价是逐项分派、容量与部分失败处理 |
| 显式接纳 A/B | 保留业务提交/停止确认，统一为结果；增加条目，无新增正常往返，不无限 ACK |
| 确定性 ID | 无新存储列且旧状态能重建关联；算法必须稳定，不能代替业务幂等 |
| 薄路由与单一业务循环 | 支持直接注册已有方法，避免嵌套等待；阶段检查仍显式，不扩张 Runtime |
| 固定摘要与有限旧读取 | 保护已有持久事实；需要保留受限旧 decoder，不能承诺随时直接降级 |

停止也使用 `[R,B]`，不另设单向 stop_accepted 备用形状。结果字段按领域严格定型，不引入
all-optional Result 大模型。消息形状统一不改变领域错误、重试权限、存储或 Future 的所有者。

### L. 验收与故障消融

本节列出两步的验收标准；实际完成项见下方实施记录，不能引用前三阶段或早期规划基线测试作为本阶段通过证据。

**第一步验收：** 定向验证严格帧/条目及 19 种映射、parser 静态约束、序列化/secret 脱敏、
Mypy 类型窄化、稳定 ID/摘要黄金向量、当前与受限旧格式 decoder 及不改写输入。
运行合同范围的测试、Ruff/Mypy/格式检查；未迁移调用方造成的全仓断点如实记录为未完成项，
不视为通过，也不为通过检查放宽合同。第一步不声称系统集成或可部署。

**第二步验收：** 复验第一步结构合同，并完成下表全部运行时、存储及恢复行为。
只有整合后的精确 head 通过完整验收和 review gate，第四阶段才达到整体交付条件。

| 合同 | 必须观察到的结果 |
| --- | --- |
| 严格 wire | 19 种旧行为映射完整；缺键/错方向/版本/类型/ID/ACK/重复项/非法组合拒绝；secret repr 脱敏但正常 wire 可交付 |
| 注册与逐项分派 | 每项一个回调，同方法多调用只完成各自 reply_to；未知 Flow 返回 INVALID_REQUEST、坏 cursor 为查询错误；不吞项/双分派 |
| ACK/限额/发送 | 纯 ACK 不设 ready/lease/超时、不循环；未知 ACK 不吞 payload；FIFO 有界、实际写后出队、guard 拒绝保留、双向写阻塞 reader 仍推进普通 query、close join |
| 混合消息 | Heartbeat+history、Registered+history、ACK+多条业务；控制先通过；整帧与条目配额不能绕过，history 按单项限额 |
| 执行/工具 | Started 发送或落盘失败不启动；执行中 query/stop/工具返回可推进；原 Task/call_id/Future 跨 Client 替换；取消/迟到/错 epoch 或 reply_to 不影响后继 |
| T/A | 成功/失败均可接纳；已持久未提交不回 A；部分处理断连、提交后丢回复、分帧/重复仍幂等；旧 job 不清新执行；T 无 A 不派下一个任务 |
| R/B | 完整停止清理/effects 后确认；B 无真实 R 被拒；部分处理/丢回复/新 epoch 重放摘要相同；stopping 不回 A、不产生新 Flow effects |
| 注册恢复 | Register+A 已提交快路径、缺结果 replay、stop 优先、已确认 journal 无 A；恢复拒绝清理先于 Registered；Register→resume 离线完成含首次落盘故障不重跑 |
| 持久兼容 | v4 内嵌 v14/v15、v5、terminal/stopped 并存；只读不写、正常写升级；新旧 worker/control inbox 可应用/reopen；未知/mixed/corrupt 保留 |
| 摘要/SQL | Unicode/转义/default-null 黄金向量不变；旧 stop 摘要由真实 receipt 核验；新旧 control/worker/null/started 来源筛选正确 |
| 前序安全 | 原连接回复、临时查询不重放、latest-wins/旧 finally；WebSocketDisconnect 可替换，RuntimeError/OSError/取消传播；archive/revision/reopen 与 shutdown root-lock 顺序不变 |

第二步完整检查包括双端真实 PostgreSQL、完整 `uv run pytest`（0 skipped）、
`uv run ruff check app tests`、`uv run mypy app tests`、变更范围格式检查、
`python scripts/build_console.py`、`uv build --no-sources`。真实 FastAPI/Uvicorn/websockets
复验恢复与替换，不触发真实 GitHub 写操作；无安全测试库就明确阻塞，不能用 skipped 算通过。
只在独立 workspace 构建；无 UI 变化，不增加视觉验收。

用相同输入逐个故障消融：提交前回 A；ACK 当业务成功/ACK 再 ACK/写前出队；T/R 后提前退出；
query/Heartbeat 整帧 continue；用帧 ID 关联或整帧重放；去掉 reply_to/epoch/阶段 guard；
阻塞 execute 回调；随机化稳定 ID；整条 wire 求摘要；删 durable decoder；Register+A 绕过
认证/stop；B 仅凭 DB hash；SQL 去掉 COALESCE；扩大 close 吞错。每个有效变体应被对应公开
合同捕获，未触发不算证明；若机制可安全删除，先简化或修正验证。
永久测试只保留用户依赖的收发/恢复/提交边界；类型探针、穷举、消融留 scratch，不一函数一测试。

### M. 升级、回退与审批门

第一步的中间 head 不独立合并或部署；部署另需授权。wire16 与 v15 不在线互通，认证后首帧
版本不符拒绝，不自动降级。受控窗口先停止新分配、等待活跃执行结束与外部 effects 收敛；
普通执行 Task 无法靠 journal 跨进程升级。
server/worker 协调升级且考虑 recovery grace，不新增 timeout 配置。升级前备份并只读盘点
实际持久版本，未知数据先制定处理方案，不清空；已持久终态/停止回执由新版本恢复。
写入 v5 journal/v1 inbox 后不能直接运行旧二进制，旧 lifecycle SQL 也会误判新 control 来源。
默认向前修复；反向转换/effects 对账另行设计，不能用旧备份覆盖后续外部事实、删 journal
或伪造 ACK。不承诺无损滚动升级、一键降级。

**审批按两步范围执行。** 可明确批准第一步（C–E/J 结构与 L 对应验证），也可一次批准包含
F–I 迁移合同、J 读写接入及 K–L 全部范围的整体方案。只授权第一步时，接续线程仍不开始迁移；
整体获批后依两步交接执行，不另加逐模块审批。docs-only draft PR 的发布单独确认；发布或
“可以审阅”不构成实现授权。若迁移需要改变字段/方法/身份或业务语义、放宽历史格式、增加存储列
或框架，应先修订方案再审批。私有函数位置、测试屏障与类型签名在获批后的验证中落实。

**线程交接：** 当前线程负责第一步；[已开的接续线程](https://ampcode.com/threads/T-01a1a199-8e0a-7690-9c0e-3b4a4bf22852)
改为负责第二步，不再按原七步接管全部实现。当前线程完成结构改造后，在同一 feature record
记录实际 jj head/基线、结构验证、黄金样本与未迁移调用方，再停止编辑并移交唯一写入权。
接续线程核对交接与授权后迁移，不并发修改同一 workspace，不另建 feature record/bookmark/PR。
生产数据盘点、维护窗口属于部署前待核实项，不在本次执行。

本次重写使用本机 Claude Opus 5.5 只读咨询，核验 init/响应模型一致、tools/MCP 为空、
success/exit 0。主线程按实际代码复核；未采用草稿新增的方法前缀拒绝规则、重复存储条目 ID、
“增加正常往返”及多项无依据待定问题。下面历史原文保留；当前方案不依赖读者逐段合并历史。
咨询与文档校验不等于产品行为验证；第一步的实际实现与验证另列下方，未做部署。

## 当前方案变更记录

### 2026-11-03：收敛为协议/数据结构、实现迁移两步

用户要求减少实施顺序，并将当前工作限定为协议结构与数据结构改造。当前方案已把原 K 节
七步清单替换为两步职责；F–I 作为接续线程的迁移合同，J 按结构定义与运行时接入分属两步。
两步沿用同一 feature record/bookmark/PR，保留全部所有权、提交/停止接纳、恢复与旧数据合同。
未采用保留七个子阶段或把第一步做成独立可部署版本的方案。

本次范围修订另经本机 Claude Opus 5.5 只读咨询，核验 init/响应模型、空 tools/MCP 与 success/exit 0。
采用结构与调用方边界、唯一写者交接及分层验收建议；未采用把全仓断点排除为“非失败”的措辞，
也未增加逐项交接阶段。这里只落实计划调整，代码和测试尚未获准开始。

### 2026-11-03：第一步协议和持久数据结构落地

**实施范围与取舍。** 当前线程落实 C–E/J，保留两步划分及不可部署的中间态边界：

- `app/contracts/protocol.py` 将在线协议替换为 wire16，方向明确的控制帧/条目联合，
  严格共同元数据、控制组合、批次上限、重复条目检查和已知方法判别；加入 E/T/A/C/R/B。
  `Message[Payload]` 只复用共同头和校验，未引入运行时 RPC 框架；Pydantic 的空控制判别
  使用 `Literal[None]`，改成普通 `None` 会使 schema 构造失败，不按 lint 建议破坏此合同。
- `Succeeded/Failed/ExecutionStopped` 仅表示持久业务事实；结果向业务数据显式投影。
  `app/contracts/receipts.py` 定义 inbox v1、受限旧格式读取和固定停止摘要；
  `app/domain/models.py` 收紧 Job 的 JSON 类型。未知 Flow 方法仍按完整原工具参数定型，
  已知坏方法不回退；领域错误、cursor 语义及工具幂等仍留原所有者。
- 新 `app/worker/state/record.py` 承载纯 `ExecutionRecord` v5 与只读 v4/v5 decoder。
  结构模块不打开文件或升级存储。`state/execution.py` 的旧内联记录和 `_parse_journal`
  尚未迁移；第二步必须删除它们并接入该唯一纯模型，不能把两套模型变成兼容或运行时分支。
  位置分离使 schema 可单独验证，不提前接入 IO 或更改原锁/Task/ACK 所有权。

**已执行验证。** 原基线保留了 Failed（Unicode/转义）、Initial、Revision、Archive 的
terminal/journal 与停止摘要黄金样本；新 decoder 对 v14/v15 的读取及 v5/inbox v1 往返
保持摘要一致。公开合同测试覆盖全部消息映射、方向/组合、secret wire 与 repr、注册恢复、
稳定身份和历史数据拒绝；Python 3.12 与 3.13 均通过。typed 构造/序列化、Mypy 窄化与双向
JSON schema 探针通过，无 Any/cast 或类型忽略。

可复现主检查：`uv run pytest --noconftest tests/test_protocol.py tests/test_receipts.py -q`；
`uv run mypy app/contracts/protocol.py app/contracts/receipts.py app/worker/state/record.py app/domain/models.py tests/test_protocol.py tests/test_receipts.py`；
`uv run ruff check app tests`；变更文件 `ruff format --check`；
`uv run python scripts/build_console.py`；`uv build --no-sources`，并检查 wheel 包含 console 和新模型。
这些检查通过。`--noconftest` 只用于纯合同隔离，避免全局 autouse fixture 导入尚未迁移的
运行时；完整 pytest 另行执行并明确失败，未删除、skip 或修改集成测试/配置。

**故障消融。** 在 scratch 副本用同一公开测试输入，逐个移除 ACK 配对、重复 ID 拒绝、
已知方法优先判别，随机化稳定 ID，把 wire 身份写入 terminal digest，放宽历史版本，
改变停止摘要版本，移除 v4 decoder，最终均被测试捕获。首次放宽历史版本的变体存活：
journal 的 mixed-version 拒绝掩盖了单独 inbox 的覆盖缺口；补充 inbox 未知版本输入后，
该变体失败，未改宽实现。保留这些约束，不引入通用版本注册表或兼容包装。

**未迁移调用方与失败边界。** `uv run pytest` 在 collection 因旧 `ReplayResult` /
`ResumeAssignment` 导入失败；`uv run mypy app tests` 失败，涉及旧平铺字段、旧构造器与 wire
联合。主要接入点是 `controler/workers/{transport,handler,conversation}.py`、
`controler/workflow/{coordinator,tools}.py`、`worker/{transport/client,execution/lifecycle,state/execution}.py`、
`persistence/{workers,stops}.py` 及对应测试。第二步还须覆盖静态检查未暴露的 Session 分派、
ResultApplication、JobStore 来源 SQL、tool ledger、后台恢复与 `prove_reopen`，不能只修类型错误。
未运行真实 PostgreSQL 全链路或连接恢复；构建成功不表示 CLI、server、worker 可运行。
全仓断点、失败的现有调用方和 CI 红灯不得作为通过或 ready；第二步按 L 完成后才可合并。

黄金样本、故障日志、完整检查输出与调用方清单位于独立 workspace 的 `.amp/in/artifacts/`，
不入库；[本线程交付记录](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)
附实际 head、验证结果、review gate 与 PR。接续线程按该精确 head 继续同一 bookmark/PR，
取得明确迁移授权后才接管唯一写入权。第一步未改 Gateway/Session/运行时，未触发生产数据转换。

## 历史记录（不作为当前实施方案）

<details>
<summary>展开初案与 R1–R6 原文；其中已被后续讨论替代的字段与选项仅作决策来由</summary>

## 1. Initial plan

### 1.1 目标、基线与范围

将现有全部 worker/server WebSocket 消息统一为一个严格的 envelope 和按方向区分的 typed
payload，明确逻辑消息身份、关联、分类、完成确认与重放边界。为后续事件驱动设计提供消息合同；
本阶段不引入领域事件投递系统。

2026-10-31（Asia/Hong_Kong）重新执行 `jj git fetch --remote origin`，并用 GitHub commits API
交叉核对，最新 `main@origin` 与本 workspace 父版本均为
[`b99c8e39`](https://github.com/b1indsight/agentflow/commit/b99c8e39f6d391b5685a91a8d6c428c51810bb11)。
已包含 [#120](https://github.com/b1indsight/agentflow/pull/120)、
[#122](https://github.com/b1indsight/agentflow/pull/122)、
[#124](https://github.com/b1indsight/agentflow/pull/124)。不采用根 checkout 或本地 main 作为基线；
不修改根目录的 Web 工作、其他 workspace 或前阶段的历史记录。jj workspace 没有独立 `.git`，
后续版本检查继续以本 workspace 的 jj revision 为准。

前序合同：[执行所有权](worker-execution-lifecycle.md)、[Session](shared-communication-session.md)、
[Gateway 的 R2 与最终关闭修复](shared-communication-gateway.md)。
来源：[架构讨论](https://ampcode.com/threads/T-01a16fd3-4a30-7198-9401-c27c98620c27)；
本阶段：[规划 / 实施 thread](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)。

**包含：** 19 种消息的共同结构、双端全部收发调用方、与消息耦合的 journal / result inbox /
digest / tool ledger / stop receipt 边界、必要的旧持久数据读取、相关回归和当前架构说明。

**不包含：** Runtime 装配完全对称、Endpoint/Run/上下文包装、通用 handler registry、自动传输
重试、可靠领域事件/outbox、中间件、event sourcing、多 server HA、跨进程 Future 恢复、
扩大消息方向、UI、真实 GitHub 业务副作用或部署。agent↔worker 的本地 MCP JSON-RPC、HTTP
console API、GitHub webhook 均不是 worker↔server wire，不改它们的外部协议。

### 1.2 当前全部消息及处理路径

现有 `Message` 仅提供 v15 版本字段，其他字段散在各类型；8 种 worker 消息、11 种 server
消息构成两个 discriminator union。[S1] 两端已经共用 Session，但历史请求单独使用 request_id，
工具使用 call_id，停止使用另一个持久 request_id，终态主要由 job/token/digest 关联。[S2][S3][S4]

下表描述当前代码。普通收发均经原 Session；只有首帧 Register 在 server 接入函数直接读取。
“持久化”列区分业务事实和原始消息，并不表示该消息需要通用 ACK。

| 消息 | 方向 / 实际发送方 → 接收方 | 持久化及现有重放 |
| --- | --- | --- |
| Register | W→S；Client._run_connection → serve_worker 首帧 | 每个新连接重新注册；携带 journal 恢复描述，不保存 Register |
| Registered | S→W；Handler.handle → Client 握手 FIFO | 连接信息，不持久化 |
| Assignment | S→W；Coordinator 准备、Handler 派发 → Client / Execution.receive | DB assignment 与 dispatch authorization；worker 写 received，不保存完整编码 Assignment 或 secret；不自动重发 |
| Started | W→S；Client._execute_assignment → Handler 首次执行握手 | server start_assignment / mark_started；worker 必须发送成功后才写 started / 启动；不自动重发 |
| Heartbeat | W→S；Client heartbeat → Handler 执行 / 停止循环 | 更新 DB 心跳；每次是新观察，不重放 |
| Succeeded / Failed | W→S；ExecutionState 完成、Client drive/replay → Handler 结果 / 停止循环 | journal 保存完整 wire；DB inbox 保存完整 wire；丢 ACK 可重放，应用结果不重复执行 |
| FlowToolRequest | W→S；WorkerExecution 创建、Client 发送 → Handler / FlowToolService | 原请求/Future 留在 owner；server ledger 保存 call_id、参数和归属；同进程重连重发原调用 |
| FlowToolResponse | S→W；FlowToolService 返回、Handler 发送 → Client / owner.finish_tool_call | ledger 仅存 DeliveryReceipt，不存 response envelope；重放从 ledger 构造回复 |
| StopExecution | S→W；StopIntent / Handler._stop_worker → Client / owner.stop | DB 持久 stop intent；恢复时可在 Registered 前重发 |
| ExecutionStopped | W→S；owner shielded stop、Client 发送 → Handler / StopStore.confirm | journal 保存完整回执；DB 仅存 receipt_digest，不存完整 receipt/nonce；可重放 |
| StopAccepted | S→W；Handler 在 confirm 后发送 → Client / owner.on_stop_accepted | worker 更新 acknowledged；丢 ACK 后再次确认 |
| ResumeAssignment | S→W；Handler._recover → Client 握手 / owner.resume | 同一执行重新获得通信授权；不重跑普通编码 |
| ReplayResult | S→W；Handler._recover → Client._replay_result | 指示重发持久终态，不重新执行 |
| RecoveryRejected | S→W；Handler._recover → Client / owner.reject_recovery | 按原 received / archive / active 规则清理，不持久化回复 |
| ResultAccepted | S→W；Handler 结果应用后或恢复快路径 → Client / owner.on_result_accepted | 只在业务应用提交后确认；worker ACK 清理仍依赖 journal 状态 |
| ProtocolError（type=error） | S→W；serve_worker 或 Handler → Client FIFO | 致命协议通知，关闭连接；无持久化/重放 |
| ConversationQuery | S→W；Broker.query → worker Session Responder / Client._answer_history | 临时只读查询，不持久化、不跨连接重放 |
| ConversationResult | W→S；history/busy handler、Session 发送 → server Session pending / Broker 校验 | 唯一进入临时 pending 的回复；超时/旧 epoch/旧连接隔离 |

来源：[首帧][S10]、[Client][S3]、[Handler][S4]、
[执行 owner][S5]、[Broker][S6]、[工具业务][S7]、[停止存储][S8]。

### 1.3 不变的所有权、安全与完成合同

1. Gateway 只管进程登记/路由/替换；Session 只管一条物理连接的 reader、FIFO、实际发送、
   临时关联及通信任务 join。无第二张路由表、通用 ACK 表、消息去重表或业务 Task 容器。[S2][S9]
2. WorkerExecution 保留跨重连 Task、原 call_id/Future、离线完成收集、shielded stop；
   ExecutionState 管 journal；server 管 Flow binding。连接退出不取消执行或释放 binding。[S5]
3. 保持 `record_received → await Started 实际发送 → record_started → 创建执行 Task`；
   发送失败/取消不启动。Started 表示该握手步骤完成，不宣称 server 已提交或执行已完成。
4. Registered 前恢复/停止、直接 Resume/Replay、Register 后 Resume 前离线完成都保留。
   Gateway 发布不代表业务 ready；查询不能确认 epoch；普通 started 任务失去 Task 后不重跑。
5. ResultAccepted 必须在 inbox 接收、result application 与提交核验之后；StopAccepted 必须
   保留原 job/flow/token/epoch/nonce、binding、结果及已 dispatch effects 的确认条件。[S4][S8]
6. 回复固定原 Session，不重新 `Gateway.get` 改投后继连接。只读查询断线即失败；工具/结果/停止
   的重放由各自业务 owner 决定，不由 Session 推断“发送失败等于远端未执行”。
7. 保留真实首帧验证、认证、方向白名单、A→B→C latest-wins、旧 finally 身份安全，以及完整
   close/join。server `_SocketTransport.close` 仅消化 WebSocketDisconnect；其他错误继续传播。
   握手前 raw socket 路径不扩大吞错；不把框架异常放进公共通信核心。[S9][S10]

### 1.4 推荐结构：一层 envelope、一层具体 payload

采用一个泛型 `Message[P]`；现有消息类改为 payload，保留原名与 `type` discriminator。
不再为每种 payload 创建 Envelope 子类；不加 Meta/Context/Endpoint/Run。

| envelope 字段 | 精确类型 / 含义 |
| --- | --- |
| `protocol_version` | `Literal[16]`；在线格式，wire 必填，无 parser 默认值 |
| `message_id` | `MessageId`：长度恰好 32、匹配 `^[0-9a-f]{32}$` 的 str；一个逻辑消息的身份 |
| `kind` | `Literal["command", "event", "query", "reply"]`；与 payload 固定分类一致 |
| `connection_epoch` | `Annotated[str, Field(min_length=1, max_length=64)] \| None`；本次投递的连接 fence，wire 键必填 |
| `correlation_id` | `MessageId \| None`；本消息回复的消息 ID，wire 键必填 |
| `payload` | `P`；具体严格 Pydantic payload，方向 union 按 `payload.type` 判别 |

`Payload(ProtocolModel)` 只有 `KIND: ClassVar[MessageKind]` 分类声明，无额外 wire 字段；
每种具体类指定 KIND。`Message` 为 frozen model，P 使用协变 TypeVar；不会声明嵌套字典深度不可变。
构造后不就地改消息身份或内容，重发由同一业务事实构造新的 epoch 投递副本。

`WorkerPayload` / `ServerPayload` 保留现有 8/11 白名单；`WorkerMessage = Message[WorkerPayload]`、
`ServerMessage = Message[ServerPayload]`。方向 parser 使用各自参数化 TypeAdapter。
业务先取 `payload = message.payload`，再用 `isinstance` 窄化 payload；元数据仍从 message 读。
不增加 TypeIs helper、cast 或新的依赖。构造器保留具体 payload 序列化（Pydantic 的
`SerializeAsAny[P]`）；它不放宽方向 parser 的联合校验。实施第一步用实际 Pydantic/Mypy 验证
参数化序列化与协变，若需要额外框架才能成立，应先修订计划，不默加 Codec/serializer 注入层。

解析器必须拒绝：缺失任何 envelope 键、旧 wire 版本、反方向类型、错误 KIND、多余字段、非法 ID、
correlation 等于自身 ID。reply 当且仅当 correlation 非 null；其他种类的 correlation 为 null。
Register 的 epoch 必须为 null；ProtocolError 可为 null；其他消息必须非 null。
这些是结构校验，不能替代协议阶段、授权、job/token/Flow 或业务提交核验。

示例（示例 ID 只展示格式，不是可执行的恢复消息）：

```json
{
  "protocol_version": 16,
  "message_id": "11111111111141118111111111111111",
  "kind": "query",
  "connection_epoch": "epoch-2",
  "correlation_id": null,
  "payload": {
    "type": "conversation_query",
    "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8c",
    "cursor": null,
    "limit": 100
  }
}
```

对应 ConversationResult 是新 message_id、kind=reply、同 epoch、correlation_id 指向上面的查询。
不加 timestamp、trace、causation、delivery attempt、通用 retry/ACK flags；现在没有调用方需要它们。

#### Payload 字段

所有 payload 去掉 protocol_version/connection_epoch；Conversation 的 request_id 被 envelope
ID/关联替代。stop 的持久 request_id、工具 call_id、assignment_token 均保留且不改语义。
现有 task/result/secret 及领域嵌套模型、默认值、exactly-one outcome 校验保持。[S1]

| Payload（另各含原 `type: Literal[...]`） | 字段和类型 |
| --- | --- |
| Register | `worker_id: str`, `recovery: RecoveryDescriptor \| None = None` |
| Registered | `heartbeat_interval: float` |
| Assignment | `job_id: int`, `flow_id: int`, `assignment_token: str`, `task: ExecutionTask`, `secret: AssignmentSecret \| None = None`；Flow 一致性校验保留 |
| Started / Heartbeat | `job_id: int`, `assignment_token: str` |
| Succeeded | `job_id: int`, `assignment_token: str`, `result: ExecutionResult` |
| Failed | `job_id: int`, `assignment_token: str`, `error: str` |
| ResumeAssignment / ReplayResult | `job_id: int`, `assignment_token: str`, `heartbeat_interval: float` |
| RecoveryRejected | `job_id: int`, `reason: str` |
| ResultAccepted | `job_id: int`, `state: JobState`, `assignment_token: str` |
| StopExecution / StopAccepted | `request_id: str`, `flow_id: int`, `job_id: int`, `assignment_token: str` |
| ExecutionStopped | 同上，加 `execution_nonce: str`，原 1–128 长度约束 |
| FlowToolRequest | `job_id/flow_id/attempt_id: int`, `assignment_token/call_id/name: str`, `arguments: dict[str, object]`, `vcs_backend: VcsKind`, `branch/commit_sha/remote_sha: str` |
| FlowToolResponse | `call_id: str`, `result: DeliveryReceipt \| None`, `error: str \| None`, `error_kind: FlowToolErrorKind \| None`, `retryable: bool = False`, `retry_after: str \| None`；原默认 None 与校验保留 |
| ConversationQuery | `session_id: SessionId`, `cursor: str \| None = None`（1–1024）, `limit: int = 100`（1–500） |
| ConversationResult | `page: HistoryPage \| None = None`, `error: ConversationError \| None = None`，恰好一个 outcome |
| ProtocolError | `code: str`, `detail: str` |

`TerminalPayload = Succeeded | Failed` 替代旧 TerminalMessage 类型名。RecoveryDescriptor 不新增字段。
工具 arguments 继续在 FlowToolService 内校验：未知 name/非法 arguments 仍返回原 INVALID_REQUEST，
不借本次模型改动把业务参数错误升级成连接解析失败。

### 1.5 分类、稳定身份与关联

| 消息 | kind | message_id 生命周期 | correlation_id |
| --- | --- | --- | --- |
| Register | command | 每次新连接 UUID4 | null |
| Registered / ResumeAssignment / ReplayResult / RecoveryRejected | reply | 每次回复 UUID4 | 当前 Register 的 ID |
| Assignment | command | 本次派发 UUID4，不自动重发 | null |
| Started | reply | 本次握手 UUID4，不自动重发 | 当前 Assignment 的 ID |
| Heartbeat | event | 每次心跳 UUID4 | null |
| Succeeded / Failed | event | 从不可变终态事实确定性导出 T | null |
| ResultAccepted | reply | 每次确认 UUID4 | T，正常和恢复快路径一致 |
| FlowToolRequest | command | 新 PendingToolCall 时 UUID4；跨连接借用原 ID | null |
| FlowToolResponse | reply | 每次回复 UUID4，包括 ledger 命中 | 当前请求的 ID |
| StopExecution | command | 从持久完整 stop fence 导出 C | null |
| ExecutionStopped | reply | 从完整 stop fence + execution_nonce 导出 R | C |
| StopAccepted | reply | 每次确认 UUID4 | R |
| ConversationQuery | query | 每次查询 UUID4 | null |
| ConversationResult | reply | 每次回复 UUID4，包括 busy / 业务错误 | 查询的 ID |
| ProtocolError | event | 每次错误通知 UUID4 | null |

这里的 reply 表示回应一条命令/查询/结果报告。Started 是派发握手回复，ExecutionStopped 是停止
完成回复，Resume/Replay 是注册恢复决定；不因其中带动作指示再发明第二套分类。
ProtocolError 是连接致命通知；解析失败时没有可信关联，统一保持无关联并关闭，不尝试从坏帧
捞取 ID。可恢复的工具/查询错误仍在其 typed reply 中保留关联，不能改成泛化 ProtocolError。

**确定性 ID 算法：** 固定命名空间 UUID `46ebd500-2f34-463f-b746-f5c03b53b0ac`，
以 `uuid5(namespace, json.dumps(parts, ensure_ascii=False, separators=(",", ":"))).hex` 导出：

- T 的 parts：`["terminal", job_id, assignment_token, result_digest]`。
- C 的 parts：`["stop_execution", request_id, flow_id, job_id, assignment_token]`。
- R 的 parts：`["execution_stopped", request_id, flow_id, job_id, assignment_token, execution_nonce]`。

标签/命名空间是身份算法合同，不随 wire 版本升级。结构化数组避免拼接歧义；nonce、token 和摘要
来自既有不可变事实。ID 不包含 epoch、投递次数、时间或 secret。使用 UUID5 仅生成标识，
不作为密码学认证或替代 SHA256 内容冲突判断。

这三类 ID 不另存 journal/DB、不加 RecoveryDescriptor.result_message_id：旧 journal 和旧 inbox
可分别转换，再从相同 job/token/digest 或 stop fence/nonce 得到相同 ID。接收方校验导出值；
但仍独立核验所有业务 fence、内容和提交事实。新 ID 不是执行许可，重用 ID 也不是去重成功凭据。

| 独立身份 | 实际责任 |
| --- | --- |
| Session 实例 + connection_epoch | 本次连接与旧连接隔离；epoch 不授予 assignment 权限 |
| job_id + assignment_token | server 分配/恢复授权；不能用 message_id 替代 |
| message_id / correlation_id | 逻辑消息与回复关联；不建通用持久去重表 |
| call_id | 原工具业务幂等；ledger 继续校验 flow/attempt/job/name/arguments |
| stop.request_id + 完整 fence + nonce | 停止请求、执行归属与原停止证据 |
| result_digest | 终态内容冲突与既有 result application/effect 绑定 |

每次新的 `call_tool` 创建新 PendingToolCall/message_id/Future；调用方选择沿用 call_id 时，
server 仍按原 ledger 判定业务重试。重连只重发原 PendingToolCall，不制造新 waiter。
回复自身可有新 ID，不能通过缓存旧 reply envelope 把旧 epoch/correlation 发到新连接。

### 1.6 接入、关联与错误边界

- Session.request 删除显式 key 参数，直接从 outgoing 的 message_id/epoch 生成 key，拒绝非 query。
  返回值仍是方向消息；保留总预算、提前回复仍等实际发送、失败/取消清理和所有限额。[S2]
- `conversation_result_key` 只从 ConversationResult 提取 `(correlation_id, epoch)`。
  **kind=reply 不决定是否进入 pending**：Started/工具/结果/停止回执继续进入业务 FIFO。
  pending 不匹配的历史回复继续消费忽略，不完成其他请求。
- server 正常 FIFO 接收统一检查 envelope epoch 等于原 Session epoch，再核对 payload job/token。
  首帧仍直接 parser 校验 Register 和认证 worker_id，之后才能启动 reader。当前 Assignment ID
  留在派发方法的局部变量中，Started.correlation_id 必须匹配；不新增 pending 表。
- worker 仍只在现有授权握手位置 bind_epoch；历史查询只在原连接回显 epoch，绝不置 connected。
  Registered/Resume/Replay/RecoveryRejected 比对当前 Register ID；ResultAccepted 比对 T；
  StopExecution 比对 C/完整 fence；绑定后 FIFO 的非空 epoch 必须匹配，包括新增带 epoch 的工具回复。
  RecoveryRejected 不单独授予 ready/执行权限；其 envelope epoch 与已绑定值不一致时拒绝。
- Client 心跳使用当前 Session.epoch，不从执行状态复制 epoch；ExecutionState.assignment 只保存
  Assignment payload。Coordinator 的 epoch 参数仍用于准备期间 DB fence，不能因为 payload
  去掉 epoch 就删除这些检查。[S11] 执行日志保留 worker/job/flow；连接日志使用当前 epoch。
- 工具完成由 owner 同时核对原 call_id 与 correlation_id；原连接已发送后取消的迟到回复仍消费，
  不再次写已结束 Future、不弹出后继调用。最后一刻发送 guard、旧消费者 join 屏障保持。[S3][S5]
- 结果 ACK 核对 payload job/token、当前 epoch、journal 终态导出的 T；不退休活跃执行或未确认 stop。
  恢复快路径依据 DB 已提交 state/digest 导出 T，不要求 terminal 先再次到达。
- stop ACK 核对原完整 fence、当前 epoch 和 journal 中的 R。stop Task 复用只比较业务 payload，
  不把 envelope 的 epoch/回复 ID 纳入停止操作相等性，也不能只比较 request_id。
- 保留错误类别：FlowToolErrorKind/retryable/retry_after、ConversationError、RecoveryRejected.reason、
  ProtocolError.code/detail。工具分类错误不会触发 Session 自动重试；query busy、timeout、
  unavailable 分别维持原错误，不合并成一种不可用错误。[S6][S7]

没有新增 receipt-of-bytes ACK。send 成功仍只说明实际 transport await 返回；只有已有业务提交
边界允许 ResultAccepted/StopAccepted。事件分类本身不附带持久化、投递保证或重试策略。

### 1.7 持久化依赖、版本与旧状态处理

已确认依赖如下；本轮未访问生产数据库/worker root，**不推断实际已存版本或数量**。

| 位置 | 当前耦合 | 提案 |
| --- | --- | --- |
| worker journal v4 | terminal/stopped 是完整 wire；已有合法 v14→v15 只读规范化 | journal v5，只存 typed payload；保留其余全部执行/归档/修订/位置/nonce/ACK 证据 |
| jobs.result_payload | 保存 wire JSON；4 个业务读取点调用在线 parser | 独立 ResultInbox v1，旧格式只在持久读取边界投影；不经在线 parser |
| jobs/result_applications/plan.digest | 使用原 terminal_digest；决定重复应用/冲突 | 字节算法与所有已有 digest 原值不变 |
| flow_tool_calls | call_id 主键，保存参数和 DeliveryReceipt | 表、数据、冲突检查不改；message_id 只留 PendingToolCall 内存 |
| execution_stops.receipt_digest | 除 epoch 外完整 wire JSON 的 SHA256，包含版本/字段顺序；无原 receipt/nonce | 显式固定摘要投影；旧摘要需有真实回执才能核验，不凭摘要反推证据 |
| control 生成的 Failed | 以 epoch="control" 区分来源；SQL 直接查询这个键 | source="control" 明确表示；SQL 同时识别新格式和限定旧格式 |

#### Journal v5

`ExecutionRecord.version: Literal[5] = 5`；`terminal: TerminalPayload | None`、
`stopped: ExecutionStopped | None`。其余字段与默认值沿用 v4：job_id、flow_id、assignment_token、
state、result_digest、revision_phase、fallback_consumed、invocations、archive_task、root_identity、
acknowledged、cleanup_verified、workspace、execution_nonce。无需新增消息 ID 字段。
根锁、私有权限、原子替换、文件/目录 fsync、secret 脱敏保持。[S12]

读取当前 v5 或受限 v4：v4 嵌套 receipt 仅支持已知 v14/v15，先严格校验历史形状、必填版本/epoch
与类型，再投影 payload；不是先删除未知字段让坏数据通过。terminal 与 stopped 同时存在时，
两者旧版本必须一致，保留现有拒绝 mixed 的规则。统一核对 terminal 的 job/token/digest、
stopped 的 job/flow/token/nonce、root_identity 与记录一致性。未知版本/损坏/矛盾不改写、不删除。

加载只做内存转换；下一次原本就需要的状态/ACK 写入才保存 v5。不会为了升级而 ACK、清 journal、
启动执行或重收 workspace。仍允许 terminal/stopped 并存；server 的停止优先恢复分支保持。

#### Result inbox v1

新增数据类型 `ResultInbox`，只有三个字段：`version: Literal[1]`、
`source: Literal["worker", "control"]`、`terminal: TerminalPayload`，extra=forbid。
存储 JSON 自身版本与 wire 版本独立，不保存 epoch/kind/correlation/随机投递 ID。
source=control 只允许 Failed，沿原应用流程，但不构造网络 envelope，也不伪造 connection_epoch。

新增 `app/contracts/receipts.py` 放 ResultInbox、历史 receipt 解析和固定 stop 摘要投影；
它只定义数据/编码，不拥有存储、Task 或连接。wire 与 durable decoder 分开，不提供泛化版本 registry。
现有 protocol.py 继续定义本次共用的业务 payload；其嵌套结果数据仍是受版本管理的存储合同。

新写入使用 v1。旧行只读接受严格的已知 v14/v15 terminal 形状（与历史 journal 的同类 receipt
一致），从旧 epoch="control" 识别 control 来源，其余为 worker；验证内容 digest 与 DB 行，
以及 job_id/assignment_token 与该行的关系。control 原来允许 token="control" 对应未分配 token，
只在 source=control 且 DB assignment_token 为 None 时允许。未知旧形状报错并保留，不能改为失败结果。

所有应用读取（ResultApplication 三处、prove_reopen）使用 durable decoder；不把旧格式放宽到
wire parser。[S13][S14] 采用惰性读取，历史行不因查询重写；无 DDL、无启动时批量 UPDATE。
旧行可能长期存在，legacy reader 的维护成本是保留已持久事实的代价；本阶段不虚设自动移除日期。

`JobStore.lifecycle_job` 必须继续排除“尚未启动且 control 产生的结果”，同时保留 null inbox / worker
结果行。使用有版本判定、COALESCE 的新旧来源表达式，不能写裸 `source='control' OR epoch='control'`
而让 SQL NULL 三值逻辑误排除正常任务。新旧 control、worker、null inbox、started control 都须验收。[S15]

`Job.result_payload` 收窄为 `dict[str, JsonValue] | None`；JSONB 存储边界仍处理 JSON，业务消费得到
ResultInbox/TerminalPayload。结果 payload 数据不搬到另一张表；result/effect 状态机与 application key 不改。

#### 摘要必须与网络 envelope 脱钩

**terminal_digest：** 保留现有 sorted compact JSON 的 `{"type":...,"result":...}` 或
`{"type":"failed","error":...}` 字节算法。保持 ExecutionResult 所有字段/默认值/序列化，
不加入 message_id、source、epoch 或 inbox version。原 DB digest、application plan 和 effect
关联均无需重算。secret 脱敏发生在 digest/稳定 ID 生成之前。[S1][S12][S13]

**stop digest：** 推荐固定显式投影，继续写与历史 v15 完全相同的摘要；其中数字 15 是存储摘要
算法常量，不再读取当前 wire 版本。字段顺序固定为 protocol_version、request_id、flow_id、job_id、
assignment_token、type、execution_nonce，沿 Pydantic JSON 编码，不加入 envelope。
本轮在未改源码上实际检查了该字段顺序；固定样本 SHA256 为
`6ca59855bc756a9dafaa69f4c04b0a33a81be55d1a43b1aa3b01c78018aa204c`：

```json
{"protocol_version":15,"request_id":"stop-1","flow_id":3,"job_id":5,"assignment_token":"assignment-1","type":"execution_stopped","execution_nonce":"execution-1"}
```

不能假设更早的 v14 server 摘要已消失。对于已有裸摘要，仅在收到真实、完整、fenced 的回执后，
允许核对已知 v15 投影或同形状 v14 投影；它们对应历史 receipt 版本的既有差异。[S12]
已有匹配摘要保持原值，新 receipt_digest 写固定 v15 投影；不离线批量转换、不增加前缀/版本列。
两种投影均不匹配就保留 blocked。匹配仅证明原证据一致，不能跳过原 StopStore.confirm 的
绑定/effects/提交条件；已经 confirmed 的重放仍按原规则处理。

选择固定投影而非新版语义 hash，避免多一套无法完成的摘要迁移；DB 只有 hash，缺少旧 nonce，
不可能在无回执时重算。旧 v14/v15 投影只在 durable 边界存在，网络严格只收 v16。

### 1.8 精确接口与受影响模块

下面是拟定的新/变更接口；其余公开方法和所有权保持。参数中的 str 依 wire 边界约束为 ID/epoch，
方法内部仍做业务校验。私有机械拆分不另设框架。

```python
# contracts/protocol.py
def new_message[P: Payload](payload: P, *, connection_epoch: str | None,
    correlation_id: str | None = None, message_id: str | None = None) -> Message[P]: ...
def terminal_message_id(job_id: int, assignment_token: str, result_digest: str) -> str: ...
def stop_command_id(request: StopExecution) -> str: ...
def stop_receipt_id(receipt: ExecutionStopped) -> str: ...
def terminal_digest(terminal: TerminalPayload) -> str: ...
def parse_worker_message(raw: str) -> WorkerMessage: ...
def parse_server_message(raw: str) -> ServerMessage: ...

# contracts/receipts.py；解析入口严格核对存储形状和行事实
def parse_result_inbox(payload: Mapping[str, object], *, job_id: int,
    assignment_token: str | None, digest: str) -> ResultInbox: ...
def parse_legacy_terminal(payload: Mapping[str, object]) -> TerminalPayload: ...
def parse_legacy_stopped(payload: Mapping[str, object]) -> ExecutionStopped: ...
def stop_receipt_digest(receipt: ExecutionStopped) -> str: ...
def matches_stop_receipt_digest(receipt: ExecutionStopped, digest: str) -> bool: ...

# communication/session.py；其余发送/关闭/Responder API 不变，只调整泛型上界
async def request(self, message: Outgoing, *, timeout: float) -> Incoming: ...

# worker/state/execution.py；assignment 为无 epoch 的业务 payload
def resume(self, *, executor_active: bool) -> Assignment: ...
def replay(self) -> TerminalPayload: ...
def complete(self, outcome: ExecutionResult | Exception) -> TerminalPayload: ...
def record_terminal(self, terminal: TerminalPayload) -> ExecutionRecord: ...
def acknowledge(self, job_id: int, state: JobState, *, result_message_id: str) -> None: ...
def acknowledge_stop(self, accepted: StopAccepted, *, receipt_message_id: str) -> None: ...

# worker/execution/lifecycle.py
async def wait_terminal(self) -> TerminalPayload: ...
def finish_tool_call(self, call: PendingToolCall, response: FlowToolResponse,
    *, correlation_id: str) -> None: ...
def on_result_accepted(self, accepted: ResultAccepted, *, correlation_id: str) -> None: ...
def on_stop_accepted(self, accepted: StopAccepted, *, correlation_id: str) -> None: ...
# resume(self, message: ResumeAssignment)、stop(self, request: StopExecution) 签名不变；参数成为 payload

# controler/workers/handler.py；首帧 envelope 传给 handler，入口显式窄化 register.payload
async def handle(self, worker_id: str, register: WorkerMessage,
    connection: Session[WorkerMessage, ServerMessage]) -> None: ...
async def _recover(self, connection: Session[WorkerMessage, ServerMessage], worker_id: str,
    connection_epoch: str, recovery: RecoveryDescriptor,
    *, register_message_id: str) -> tuple[Job, str, ExecutionTask | None] | None: ...
# controler/workers/conversation.py
def conversation_result_key(message: WorkerMessage) -> ReplyKey | None: ...
def _validated(self, request: ConversationQuery, response: WorkerMessage) -> HistoryPage: ...

# controler/workflow/tools.py；两次 DB fence 均使用传入的原连接 epoch
async def execute(self, request: FlowToolRequest, *, worker_id: str,
    connection_epoch: str) -> FlowToolResponse: ...
async def _open_pr(self, request: FlowToolRequest, worker_id: str,
    connection_epoch: str) -> FlowToolResponse: ...

# persistence/stops.py
def command(self) -> StopExecution: ...  # StopIntent，取代 message(epoch)
def confirm(self, worker_id: str, receipt: ExecutionStopped,
    *, connection_epoch: str) -> bool: ...
# persistence/workers.py；在存储边界构造 source=worker 的 inbox、计算原 digest
def record_assignment_result(self, job_id: int, worker_id: str, token: str,
    connection_epoch: str, terminal: TerminalPayload) -> str: ...
# persistence/jobs.py；同理构造 source=control，消除调用者独立传 JSON/digest 的不一致面
def record_control_result(self, job_id: int, terminal: Failed, reason: str) -> None: ...
```

`new_message` 只在本地构造时填 v16、KIND、UUID4 或明确传入的稳定 ID；parser 不调用它补值。
`PendingToolCall` 全部字段为 `request: FlowToolRequest`、`message_id: MessageId`、
`future: asyncio.Future[DeliveryReceipt]`；只增加需要跨连接保留的逻辑消息 ID。
ResultInbox 和 ExecutionRecord 的全部新增/变更字段见 §1.7；不新建执行上下文。

Broker._validated 在方法内窄化 response.payload 后做业务校验；大小上限继续计算完整 response
序列化字节，包含 envelope，避免扩展元数据后绕过原限额。不增加类型适配 helper。
WorkerRecord.protocol_version 从注册 envelope 读取；DB 的连接/在线版本记录保持原语义。

| 模块 | 调整责任 |
| --- | --- |
| protocol.py / receipts.py | wire envelope、方向联合、身份算法；持久数据 decoder 与固定摘要分开 |
| communication/session.py / gateway.py | 泛型上界和 request key 来源；不动路由、close、背压、发送完成机制 |
| worker/transport/client.py | 首帧/全部业务/查询构造 envelope、元数据检查；保留原消费者和执行顺序 |
| worker/execution/lifecycle.py / state/execution.py | 工具稳定 ID、payload journal、ACK 身份核验；epoch 从执行状态移回连接 |
| controler/workers/transport.py / handler.py / conversation.py | 首帧和各收发路径迁移；业务确认仍原连接、原提交位置 |
| controler/workflow/coordinator.py | prepare_for_assignment 保留 epoch fence 形参，只返回无 epoch Assignment；fail 生成 control payload |
| controler/workflow/tools.py / results.py / lifecycle.py | 显式传原连接 epoch；三处 result 读取和 reopen 改 durable decoder |
| persistence/workers.py / jobs.py / stops.py、domain/models.py | typed 入库边界、source SQL、停止摘要、JSON 字段类型；不改表结构/ledger/effect 状态机 |
| scripts/smoke/recovery.py、相关 fixtures、当前 architecture / operations、CHANGELOG | 更新直接构造和版本说明；不改历史计划，不把恢复 drill 的同版本 manifest 改成通用迁移工具 |

### 1.9 方案比较、成本和风险

| 选择 | 理由 / 代价 |
| --- | --- |
| 嵌套 envelope + 原名 payload | 元数据不再泄漏进业务持久记录；多一次 `.payload`，但不将类型数量翻倍。flat 继续依赖易漏的 exclude 列表，stop digest 已显示其风险 |
| 三种确定性 ID + 工具内存 ID | 从持久事实恢复同一身份，无 DB 新列、journal 新 ID 或恢复描述冗余；代价是固定且不可随意更改的身份算法 |
| 业务回执仍进 FIFO | 保留提交/停止顺序；统一 reply 元数据不改变生命周期归属 |
| durable payload / inbox 独立版本 | 以后 wire 演进不自动破坏恢复；本次升级影响文件与 DB JSON 读写调用方，必须全量核对 |
| 有限历史读取 + 固定 stop 摘要 | 保留已持久数据，在线只收 v16；保留受限旧格式代码，不能假称没有兼容维护成本 |
| 不统一所有领域错误 | 各领域已有稳定错误、不同重试权限；合并会增加模式和映射且易改变行为 |

不采用随机 durable ID + 新字段方案：旧 server/worker 分别升级需要共同恢复身份，仍要迁移/导出；
本场景已有足够不可变事实。也不把消息 ID 当 call_id/token，不通过“ID 已见过”跳过冲突检查。

主要风险：ID 算法漂移、元数据缺失被默认补齐、泛型序列化丢 payload 字段、reply 被错误吞掉、
epoch 从 payload 移除时漏掉 DB fence、旧 stop 摘要字节变化、SQL NULL 误筛、旧 inbox 无法应用。
以下以实际行为与故障变体验证。没有新增框架、依赖或通用 migration 系统。

### 1.10 获批后的实施顺序、升级与回退

1. 确认实质方案及 docs-only 发布；沿用本 workspace 的一个 bookmark / draft PR。实现开始前
   再 fetch 最新 main@origin，核对漂移；未经明确批准不写实现或测试。
2. 在未改动基线采集 wire / terminal / stop 摘要和持久记录黄金样本；在 scratch 验证泛型类型/
   序列化方案。通过后实现 envelope、payload、固定身份与 durable decoder，不引入退路框架。
3. 迁移 ExecutionState / WorkerExecution 与 Session 类型；迁移 Client 全部收发，保留 Started、
   离线完成、工具取消、stop / shutdown 时序。只更新因合同变化失效的既有测试。
4. 原子迁移 server 收发、typed persistence、结果消费及 source SQL；不可只让 live 路径通过而
   留下后台 recovery/reopen 读旧 wire parser。同步直接脚本/架构说明。
5. 定向、真实 PostgreSQL 双端与完整回归、静态/构建、简化对照、自查；按 workflow 对精确
   code-bearing head 运行 review gate 后发布同一 PR。计划发布本身不构成实现授权。

**部署提案，不授权执行：** wire v16 不支持与 v15 在线互通，版本不匹配应认证后首帧失败并关闭，
不能退回旧 wire 或自动降级。安排受控升级窗口，先停止新任务分配，等待活跃编码执行结束和外部
effects 收敛；普通 running Task 无法通过进程升级恢复，不能声称旧 journal reader 解决了这一问题。
持久 terminal/stop receipt 可在新版本中重放；未知旧进程/未完成清理仍保持 blocked。
server 与所有 worker 需协调升级，窗口不能无视既有 recovery grace；不新增 timeout 配置。

升级前备份原 DB/worker 状态、只读统计实际格式，确认仅在已支持范围内；未知版本先制定数据处理
方案，不清空现场。无需 DDL/批量改写；启动/恢复正常写入新格式。既有恢复 drill 保留原本安全范围。

**回退边界：** 新格式尚未写入时可协调回退二进制；一旦写出 v5 journal/v1 inbox，旧二进制
不能直接运行这些数据。尤其旧 lifecycle SQL 会误读新的 control source，不能笼统称为 fail-closed。
默认向前修复；若需要回退数据，须另行设计和验证反向转换/外部 effects 对账，不以恢复旧备份覆盖
后续外部事实、不删 journal、不冒充 ACK。本计划不承诺无损滚动升级或一键降级。

### 1.11 验证矩阵与简化对照

以下均是实施期验收，不能用前三阶段或本轮未改代码的测试结果代替。

| 行为 | 必须观察到的结果 |
| --- | --- |
| 严格协议与方向 | 19 类型 round-trip 完整；漏 ID/元数据、错 kind、反方向、未知 type/version、extra 拒绝；secret repr 脱敏，正常 wire secret 仍可交付 |
| 临时关联 | 新 ID/关联 key；同 ID 不同 Session/epoch 隔离；busy、timeout、替换各自原错误；业务 reply 进入 FIFO；配额可复用，无跨连接重放 |
| 首帧/恢复握手 | 真实首帧错误不能被 reply dispatcher 隐藏；query 早于 Registered 不授予 ready；stop/result ACK 早于 Registered；直接 Resume/Replay；错 Register correlation 拒绝 |
| Started 与离线完成 | 发送/落盘失败不启动；Register 后 Resume 前完成（含首次落盘故障）只重放，execute 次数不增加 |
| 工具重放/业务重试 | 完整 Client 替换保留原 ID/call_id/Future；ledger 命中只重包回复；新业务重试新 message_id；错误关联/epoch/call_id 拒绝；取消/迟到回复不影响后继 |
| 终态 ACK | 持久接收而应用未提交不 ACK；已提交但 ACK 丢失，恢复快路径无需重发 terminal；T 一致，digest/effects 不变；伪造 correlation 不清 journal |
| 停止 ACK | 真实执行/子进程清理与 workspace 回收后才发 receipt；stop 期间换连接仍 join；已有结果 ACK 不退休 stop；错误 fence/nonce/关联或未收敛 effects 不确认 |
| 旧状态 | v4 内嵌 v14/v15、v5、terminal/stopped 并存；读不写、正常 ACK 写 v5；旧 worker/control inbox 和新 v1 均可应用/reopen；未知/mixed/corrupt 保留拒绝 |
| 跨格式恢复 | 旧 worker journal 与旧 server inbox 分别升级；已应用终态快 ACK；已有 v14/v15 stop digest 重放；不需要旧消息 ID 列、不重复应用 |
| 摘要与 SQL | Unicode/转义/default-null 黄金样本；terminal digest 原字节不变；stop 固定投影匹配旧值；source SQL 的新旧 control/worker/null/started 组合正确 |
| 关闭与替换 | 第三阶段全部安全合同不退化；真实 WebSocketDisconnect close 可替换，RuntimeError/OSError/取消继续传播；旧 finally 不覆盖新 epoch |
| 整体边界 | 普通重启无 Task 不重跑；archive 原例外；Flow binding、reopen、取消/合并收敛、shutdown root lock 顺序不变 |

优先复用 `test_protocol`、`test_worker_session`、`test_worker_client`、`test_worker_handler`、
`test_stop_protocol`、`test_result_recovery`、`test_communication_session/gateway`、
conversation/remote E2E、archive/revision/reopen 和 runtime 既有合同。
永久测试只保留用户依赖的 wire/恢复/提交边界；黄金向量以 literal 旧数据保护持久合同，
不为每个 helper、字段代理或类型改名各加测试。类型探针、组合穷举与变体留 scratch。

检查：定向 → 双端真实 PostgreSQL → 完整 `uv run pytest`（0 skipped）→
`uv run ruff check app tests` → `uv run mypy app tests` → 变更范围格式检查 →
`python scripts/build_console.py` / `uv build --no-sources`。仅在独立 workspace 构建；
没有安全测试库时明确阻塞，不把 skipped 算通过。使用真实 FastAPI/Uvicorn/websockets 复验
恢复与断连替换，不发真实 GitHub 写操作。无 UI 修改，不增加视觉验收。

实施后、代码审查前用同一输入逐个去掉机制：

1. terminal 重放改随机 ID：跨格式/快 ACK 关联应失败；固定 T 的控制组通过。
2. 用整条 envelope 求 digest：旧 terminal/stop 重放应暴露冲突；不能只断言版本常量。
3. 仅按 kind=reply 分派 pending：Started/工具/停止流程应失败；证明分类与处理所有权分离。
4. 省掉显式元数据或错配 kind 校验：畸形帧应被错误接纳，原有严格边界用例捕获。
5. 去掉历史 durable decoder 或只保留旧 wire parser：旧恢复/新 inbox 应失败；读不写控制组通过。
6. 去掉 correlation/epoch 核验，或把 ACK 提到提交前：错误回执/原连接/未提交场景应失败。
7. SQL 去掉 COALESCE：null inbox / worker 行筛选场景应失败。

若去掉某机制后合同仍成立，先简化或修正测试；不把未触发的变体算成证明。不保留变体产品代码。

### 1.12 本轮实际核查、Oracle 取舍与审批事项

本轮只读取基线源码、前序计划、测试和 CI 配置；执行既有定向测试及原停止回执的序列化检查。
测试使用独立 UTF8 PostgreSQL/TCP，全部通过、无跳过；日志在本 thread 本地产物
`.amp/in/artifacts/planning-baseline.log`。首次 DB 启动遇到 macOS Unix socket 路径长度和 locale
问题，改为仅 TCP 并显式 LC_ALL=C 后正常执行，测试库已停止。未修改源码绕过环境问题；
未运行本阶段新实现、全量 suite、静态检查或构建。原 Starlette TestClient 弃用提示仍在。

按 consulting-claude-oracle 的受限脚本使用本机 CLI；确认 claude.ai 订阅登录，检查适用 managed
配置位置后，以 stdin 提供编号源码与边界。初始化和实际响应模型均为 `claude-opus-5-5`，
tools/mcp_servers 为空，result success、is_error=false、退出 0。原始输入/事件流不提交，
脱敏核验信息保存在 `.amp/in/artifacts/oracle-verification.json`。咨询意见不等于执行验证。

主线程核对后采用：单 envelope、payload 存储、确定性 durable ID、无 DDL、固定 stop 摘要。
对草案作了必要收窄/修正：

- 不增加 TypeIs/额外依赖或 serializer 注入备选层，直接窄化 payload。
- stop ID 纳入完整不可变 fence；保留独立业务校验。
- 不用“当前在线 parser 只收 v15”推断磁盘/DB 没有 v14；已知 v14/v15 历史 receipt 在 durable
  边界显式处理，未知版本仍阻塞。实际部署数据尚未调查，升级前必须盘点。
- 修正 source SQL 的 NULL 三值逻辑；保留原 query_busy/query_timeout/worker_unavailable 区分。
- 不声称活跃编码 Task 可以通过升级恢复，也不声称写入新格式后可以直接降级。

**需批准的实质选择：**

1. §1.4–1.6 的单 Message/payload 结构、完整 kind/correlation 表与三种稳定 ID 算法；
   仅历史回复走 pending，业务确认留原 owner。
2. wire v16、journal v5、inbox v1；有限旧状态只读转换、固定 stop 摘要及 v14/v15 核验边界；
   不清旧状态、不加 DDL，不承诺直接二进制降级。
3. 将 epoch 从业务 payload/journal 移到 envelope/Session，并通过显式参数维持原 DB fence；
   完整所有权、顺序、关闭和确认合同不变。
4. 是否授权发布本 docs-only draft PR；方案明确获批后才实施。仅“可以审阅”或发布草案不构成实现授权。

**实施期可定：** 测试屏障/fixture 更新、局部变量与私有函数位置、错误文案；泛型方案须先验证。
若需放宽旧数据范围、增加存储字段/框架、改变消息方向/重试或业务状态机，先追加实质修订再审批。
**部署前待核实：** 实际 worker 数量、持久版本分布、维护窗口与 drain 条件。它们阻塞实际部署，
不阻塞在已知格式上实现和验证此方案；本轮未获部署授权。
**延后：** Runtime 对称装配、可靠领域事件/outbox、跨进程工具恢复、中间件、HA。

## 2. PR change timeline

<!-- 初始方案分享后，仅追加真实需求修订、设计决定和实施结果；不记录单纯审批或阶段切换。 -->

### 2.1 R1 — 两类消息与共用 toolcall（2026-11-03，Asia/Singapore）

#### 要求、状态与替代范围

用户明确要求收敛为两种情况：连接控制字段在顶层，与 payload 同级；其他 toolcall 和业务消息
放进 payload，查询和调用 worker 都作为特殊 toolcall。删除初案 command/event/query/reply
四分类，不把它们改名后搬到另一层。此要求已经明确；以下具体字段、合并方式与 ID 方案仍待审批，
不构成实现授权。[本阶段讨论](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)

本修订替代 §1.4 的全 payload 结构、§1.5 的 kind 表和随机 Assignment ID、§1.6 的 kind/query
约束及相应接口。§1.3 的全部所有权/安全合同、§1.7 的持久化方案和升级限制继续适用；
尚未实施，不同时维护初案四分类作为备用协议。

#### 两种 wire 形状

两种形状共用四个顶层必填字段：`protocol_version: Literal[16]`、`message_id: MessageId`、
`connection_epoch: str | None`、`correlation_id: MessageId | None`。ID 与 epoch 的原约束保留。
不再有 `kind` 或 payload.KIND，也不加重复的 `type=control|business`。

- **控制消息：** 顶层有 `control`，控制操作字段也在顶层；没有 payload。
- **业务消息：** 顶层有严格 typed `payload`；没有 control 或其他控制操作字段。

使用互斥的严格模型联合，不用一个所有字段都可空的大模型。两键同时出现、都不出现或显式 null
占位均拒绝。对外只有这两种形状；内部按控制操作与具体业务参数窄化，避免 Any/未验证字典。

| 控制操作 | 方向 | 顶层操作字段（不包含上述共同元数据） | 关联与作用 |
| --- | --- | --- | --- |
| `control="register"` | W→S | `worker_id: str`, `recovery: RecoveryDescriptor \| None` | 新 ID、epoch/correlation 均 null；提交注册与恢复证据 |
| `control="registered"` | S→W | `heartbeat_interval: float`, `recovery: RecoveryDecision \| None` | 新 ID、非空 epoch、关联本连接 Register；完成握手 |
| `control="heartbeat"` | W→S | `job_id: int`, `assignment_token: str` | 新 ID、当前 epoch、correlation=null；保留执行 lease 心跳 |
| `control="error"` | S→W | `code: str`, `detail: str` | 新 ID、epoch 可空、correlation=null；致命协议通知并关闭 |

`RecoveryDecision` 只有 `action: Literal["resume", "replay"]`、`job_id: int`、
`assignment_token: str`。建议将 ResumeAssignment/ReplayResult 合并为 Registered.recovery：
它们目前都是结束握手、绑定 epoch 并提供 heartbeat_interval 的互斥响应。[S1][S3][S4]
这是注册响应中的具体恢复决定，不新增执行上下文或拥有 Task 的对象。

RecoveryRejected 保留在业务 payload：它拒绝某次执行恢复，可能取消执行，之后仍等待最终注册响应；
它不授予 ready/执行权限。[S3][S5] StopExecution、ResultAccepted 仍可先于最终 Registered 到达。
原“Register 后 Resume 前离线完成”对应新“Register 后 Registered.recovery=resume 前完成”：
同一时点收集终态并只重放，绝不重新执行。合并响应不能提前发布 connected 或新建一个 await 窗口。

Heartbeat 继续携带 job/token、只在原 lease 条件下发送，server 继续检查 assignment；没有改成
无条件连接 ping。连接 epoch 从 Session 取得；执行清理与恢复权限仍由原业务 owner 核验。

控制消息示例（固定 ID 仅展示结构）：

```json
{
  "protocol_version": 16,
  "message_id": "11111111111141118111111111111111",
  "connection_epoch": "epoch-2",
  "correlation_id": null,
  "control": "heartbeat",
  "job_id": 5,
  "assignment_token": "assignment-1"
}
```

#### 业务调用只用一种 toolcall 形状

所有调用的 payload 共用 `type="tool_call"`、`method`、`arguments` 三个字段。
调用的消息 ID 已在 envelope，payload 不再加 id/ref/另一个通用 call_id。
各 method 的 arguments 是具体模型，现有业务字段直接成为参数，不嵌套旧消息或追加 context。

| method / 模型 | 方向 | arguments 的精确内容 | 寿命 / 重放 |
| --- | --- | --- | --- |
| `worker.execute` / ExecuteCall | S→W | `Assignment`：§1.4 的 job_id、flow_id、assignment_token、task、secret，去掉 type/元数据 | 原 WorkerExecution Task；重连恢复不重新派发执行 |
| `worker.history` / HistoryCall | S→W | `HistoryArguments`：session_id、cursor、limit，原值域/默认值 | Session 临时 pending；不跨连接重放 |
| `flow.open_pr` / FlowToolCall | W→S | `FlowToolArguments`：§1.4 的 job/flow/attempt/token、call_id、arguments、VCS/branch/SHA 字段，去掉 type/name/元数据；原 name 移为 method | 原 PendingToolCall/Future；重连重发同一调用 |
| `worker.stop` / StopCall | S→W | `StopFence`：request_id、flow_id、job_id、assignment_token | 原 shielded stop Task；按持久 stop intent 重放 |

`ToolCall[A]` 继承无 KIND 的 Payload，字段为 `type: Literal["tool_call"]`、`method: str`、
`arguments: A`（A 为严格 ProtocolModel）。三个 worker 调用模型收窄 method 为各自字面量，
arguments 为上表具体类型。FlowToolCall 收窄 arguments，method 仍为 str，当前支持 flow.open_pr；
不新增 flow.tool 再套一个 name 的转发层。FlowToolService 将 method 作为原 ledger 的 name 使用。
原工具参数字典仍为 `dict[str, object]`；未知 Flow 工具 method/非法业务参数继续返回
INVALID_REQUEST。S→W 未知 worker method 属于无匹配的协议调用，按原未知消息类型拒绝；
W→S 的调用只能进入 FlowToolService，不能借 method 调用 server 的任意 Python 方法。[S7]
HistoryArguments 不使用会提前解码 cursor 的 HistoryQuery validator；cursor 的语义错误仍返回
原查询错误，不改成连接错误。

业务消息示例：

```json
{
  "protocol_version": 16,
  "message_id": "22222222222242228222222222222222",
  "connection_epoch": "epoch-2",
  "correlation_id": null,
  "payload": {
    "type": "tool_call",
    "method": "worker.history",
    "arguments": {
      "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8c",
      "cursor": null,
      "limit": 100
    }
  }
}
```

历史和 Flow 工具的直接结果共用 `payload.type="tool_result"`；字段仍各自定型：
W→S 的 ConversationResult 为 page/error，S→W 的 FlowToolResponse 为 call_id/result/error/
error_kind/retryable/retry_after。原 exactly-one、错误和业务 call_id 校验保留；correlation
指向调用消息。当前两个方向各只有一种这种直接结果，不加多余的 method 回显或泛化 Result 包装。

执行与停止有自己的开始、终态、提交确认过程；Started、Succeeded、Failed、ResultAccepted、
ExecutionStopped、StopAccepted 保留具体 typed payload，其他字段见初案 §1.4。
不再另外将它们标记为 command/event/reply，不添加通用 progress/status/notification 枚举。
Succeeded/Failed 是 worker.execute 的业务终态，不能用立即 tool_result 替代提交后的确认。

#### 原消息映射与身份

沿用初案 T（终态消息 ID）、C（停止调用 ID）、R（停止回执 ID）的算法。
新增 E，即执行调用自身的 message_id：使用同一 UUID namespace，结构化数组
`["worker.execute", job_id, assignment_token]` 导出。E 不包含 epoch、结果、secret，
不原样使用 assignment_token，也不增加 journal/DB 字段。

E 的必要性是让执行终态明确关联到 worker.execute 调用，且旧 journal/server inbox 分别升级后
都能重建该关联。T 仍独立区分具体终态内容；ResultAccepted 继续确认 T，不能仅确认 E。
不删除原 message_id/correlation_id，不把它们改成 token 或业务 call_id。

| 原消息 | 新位置/形状 | message_id / correlation_id |
| --- | --- | --- |
| Register | 顶层 control=register | 新 ID / null |
| Registered | 顶层 control=registered，recovery=null | 新 ID / Register ID |
| ResumeAssignment | registered.recovery.action=resume | 新 ID / Register ID |
| ReplayResult | registered.recovery.action=replay | 新 ID / Register ID |
| RecoveryRejected | payload.type=recovery_rejected | 新 ID / Register ID |
| Assignment | payload tool_call(worker.execute) | E / null |
| Started | payload.type=started | 新 ID / E |
| Heartbeat | 顶层 control=heartbeat | 新 ID / null |
| Succeeded / Failed | payload.type=succeeded / failed | T / E |
| ResultAccepted | payload.type=result_accepted | 新 ID / T |
| FlowToolRequest | payload tool_call(method=原 name) | 原 PendingToolCall 的 ID / null |
| FlowToolResponse | payload.type=tool_result | 新 ID / FlowToolCall ID |
| StopExecution | payload tool_call(worker.stop) | C / null |
| ExecutionStopped | payload.type=execution_stopped | R / C |
| StopAccepted | payload.type=stop_accepted | 新 ID / R |
| ConversationQuery | payload tool_call(worker.history) | 新 ID / null |
| ConversationResult | payload.type=tool_result | 新 ID / HistoryCall ID |
| ProtocolError | 顶层 control=error | 新 ID / null |

关联要求直接按上表的具体操作校验，删除旧“kind=reply 当且仅当 correlation 非空”的规则。
没有把每条消息变成有结果的 RPC，也不因控制/业务分类新增持久化、重试或确认。

#### 类型、接口与所有权

`Message` 回到共享元数据基类；Register/Registered/Heartbeat/ProtocolError 直接继承。
`BusinessMessage[P]` 只增加 typed payload，不为每个业务类型生成独立 envelope。
`WorkerMessage = Register | Heartbeat | BusinessMessage[WorkerPayload]`；
`ServerMessage = Registered | ProtocolError | BusinessMessage[ServerPayload]`。
两个方向的控制集合按 control 判别，业务 payload 按 type 判别；S→W 的 tool_call 按 method
判别三个 worker 调用，W→S 的 tool_call 只有 FlowToolCall，不把工具名收成枚举。
最外层使用严格联合，靠必填字段和 extra=forbid 保证互斥；不新增 callable discriminator/路由注册表。
BusinessMessage 的泛型序列化/协变按初案实施首步验证，尚未验证。

删除 ResumeAssignment/ReplayResult/StopExecution/ConversationQuery/FlowToolRequest 的独立 wire
模型，调用方改用上面的调用参数和恢复决定；不留旧构造器或四分类兼容层。
初案接口中只有下列部分需要修订，其余持久化边界和 ACK 核验接口沿用：

```python
# contracts/protocol.py；new_message 现在只构造业务帧，控制模型在少数收发点直接构造
def new_message[P: Payload](payload: P, *, connection_epoch: str,
    correlation_id: str | None = None, message_id: str | None = None) -> BusinessMessage[P]: ...
def execution_call_id(job_id: int, assignment_token: str) -> str: ...
def stop_call_id(fence: StopFence) -> str: ...  # 替代 stop_command_id，C 的字节算法不变
def parse_worker_message(raw: str) -> WorkerMessage: ...
def parse_server_message(raw: str) -> ServerMessage: ...

# communication/session.py；泛型上界仍为 Message，不加 request_key 注入
async def request(self, message: Outgoing, *, timeout: float) -> Incoming: ...
# 仅从 message_id/epoch 生成临时 key；删除 kind/query 检查，调用者选择临时调用

# worker/execution/lifecycle.py；全部仍在 WorkerExecution，不能移到 ExecutionState
def resume(self, decision: RecoveryDecision) -> None: ...
async def stop(self, request: StopFence) -> ExecutionStopped: ...
async def _stop_execution(self, request: StopFence) -> ExecutionStopped: ...
# PendingToolCall: request: FlowToolCall, message_id: MessageId, future: Future[DeliveryReceipt]
# finish_tool_call(..., response: FlowToolResponse, *, correlation_id: str) 沿用初案

# worker/transport/client.py
async def _serve_assignments(self, connection: Session[ServerMessage, WorkerMessage],
    first: Registered) -> None: ...
async def _execute_assignment(self, connection: Session[ServerMessage, WorkerMessage],
    assignment: Assignment, *, message_id: str) -> None: ...
async def _handle_stop(self, connection: Session[ServerMessage, WorkerMessage],
    request: StopFence) -> None: ...
async def _replay_result(self, connection: Session[ServerMessage, WorkerMessage],
    decision: RecoveryDecision) -> None: ...

# controler/workers/handler.py；Register 自身就是完整控制帧
async def handle(self, worker_id: str, register: Register,
    connection: Session[WorkerMessage, ServerMessage]) -> None: ...
# _recover 的初案签名保持，分支发送带 RecoveryDecision 的 Registered

# controler/workers/conversation.py
def _validated(self, request: HistoryArguments, response: WorkerMessage) -> HistoryPage: ...

# controler/workflow/tools.py
async def execute(self, call: FlowToolCall, *, worker_id: str,
    connection_epoch: str) -> FlowToolResponse: ...
async def _open_pr(self, call: FlowToolCall, worker_id: str,
    connection_epoch: str) -> FlowToolResponse: ...
# 私有错误构造器继续接收 call.arguments（FlowToolArguments），原错误内容与 call_id 保留

# persistence/stops.py：仅返回既有业务 fence，不在 store 构造网络帧
def fence(self) -> StopFence: ...  # StopIntent，替代初案 command()
```

Session 的 pending 与 Responder 仍只承接 worker.history。conversation_result_key 识别
业务帧中的 ConversationResult，返回 correlation/epoch；其他业务确认继续 FIFO。
WorkerExecution 拥有执行 Task、原工具 Future 和 stop Task；ExecutionState 继续只管 journal。
统一 toolcall 格式不改变调用寿命，也不引入一个管理所有调用的新对象。

#### 持久化、取舍与验证

初案 wire v16、journal v5、inbox v1 和有限历史转换方案继续作为提案。调用的 arguments 与
业务 receipt 不保存外层 metadata；终态/停止仍是同一份 typed payload，不在里面加 id/ref。
terminal_digest、T、R 和冻结 stop digest 的输入不变；E 直接由现有 job/token 重建。
Flow 工具 ledger、source SQL、旧数据只读加载、未知状态保留和回退限制均沿用 §1.7/§1.10。

主要取舍：取消四分类和重复的外层类别标签；合并三个互斥的握手完成响应；共用四种调用的形状。
保留有不同完成条件的具体业务 receipt，不强行统一为一个 result；保留稳定消息身份与内容关联，
避免仅为删字段而让 ResultAccepted 无法指明它确认的终态。RecoveryDecision 是新增的三个字段值，
E 是新增的一个纯函数；没有存储列、全局 pending、Context 或通用 RPC 框架。

实施验收沿用初案矩阵，并替换已失效的四分类用例：

1. 控制/业务两种形状互斥；控制字段不能藏在 payload；错误方向、未知 control/worker method 拒绝。
2. worker.execute/history/stop 的 method/arguments 对应严格；Flow 调用保留严格归属参数，
   未知工具名仍是业务 INVALID_REQUEST，非法 cursor 仍是查询错误。无新的自动重放/重试行为。
3. registered.recovery 的 idle/resume/replay 分支等价于原三个响应；stop、结果 ACK、rejected
   仍可先于最终注册响应；离线完成竞态不重跑；Started 实际发送顺序原样保持。
4. 只有历史 tool_result 走 pending；执行、Flow 工具 Future、停止的所有权与取消/替换行为不变。
5. E 在旧 journal/inbox 分别转换和重连后稳定；T/R/原摘要保持；错误 correlation、token、epoch
   不能释放 journal、完成后继 Future 或越过应用/停止确认条件。
6. 不为消融先实现已删掉的四分类/额外标签。对新增 E，比较省略关联与每次随机 ID 的变体，
   应被跨重连结果关联用例捕获。不得用“所有业务都进 pending”替代分工。

本轮重新 fetch，main@origin 仍为规划基线。按本机只读 Oracle 流程生成修订草稿，初始化与实际
响应均核验为 claude-opus-5-5，tools/MCP 为空，退出 0；核验记录在
`.amp/in/artifacts/control-payload-oracle-verification.json`。主线程核对源码后未采纳草稿中删除
稳定消息 ID/终态内容关联、移交 Task/Future 给 ExecutionState、删除 PendingToolCall.future、
额外注入 request_key 和扩大工具参数解析边界的建议；也去掉了 flow.tool/name 的重复转发表达。
咨询输出不代表执行验证。

**实际交付：** 仅本地文档 R1，初案原文未覆盖；没有实现、测试改动或本轮产品测试。未 push、未开 PR。
**待确认：** 两类形状的具体字段、Registered.recovery 合并、共用 toolcall 的四个 method 和
执行调用 E 的导出；docs-only 发布及实施仍等待明确授权。

### 2.2 R2 — 允许控制与 payload 同帧，增加 ack（需求补充）

用户进一步要求：“添加一个 ack 字段，表示是否回应和结果，而且 payload 可以和控制消息一同发送，
可以一个消息实现两种作用。”[本阶段讨论](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)

**已明确的要求：** 连接控制字段与 payload 同级，两者允许出现在同一条消息中。R1 的控制/业务
互斥约束及对应验收条目作废；初案与 R1 原文保留为历史。外层增加 ack 字段。

**待澄清：** ack 是否为布尔标记，false 表示发起、true 表示回应，具体成功/失败及结果仍在
payload；还是 ack 本身需要区分“已收到”与“已完成”等确认阶段。此差异会影响字段类型和
原有业务确认的表达，暂不将其中一种解释记为已确定设计。

这里只记录用户的新要求与待澄清点，尚未生成 R2 具体方案或进行本轮 Oracle 咨询。方案仍需覆盖
同帧两部分的关联对象、处理顺序与失败行为，不能让其中一部分被分发逻辑吞掉；原 ResultAccepted
和 StopAccepted 的完成条件继续保留。实现与测试均未修改，未 push、未开 PR。

### 2.3 R3 — 控制接收确认与同帧业务（2026-11-03）

#### 用户决定与提案范围

用户明确：“ack 仅表示对控制消息收到的确认，用 bool 表示，参考 tcp 实现。”因此 R2 中的
两种解释均不采用：ack 不标记业务回应，也不表达 received/completed 等业务阶段。
控制字段与 payload 同级、允许同帧的要求继续适用。
[本阶段讨论](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)

以下 `ack_id`、发送策略、类型和分发方法是待审批的具体方案。R1 的互斥模型/验收和控制响应
占用 correlation 的规则被本节替代；初案、R1、R2 均保留历史原文。其余 R1 的 toolcall、恢复
决定、稳定 ID 以及初案的持久化方案仍是提案；本次语义澄清不构成整体实施授权。

TCP 的 ACK 标记使确认号有效，确认可以与控制和数据同段，ACK 本身不需要再被 ACK。
本方案只参考这三点；已有 WebSocket/TCP，不再实现 TCP 字节序号、累计确认、窗口或重传。
这里的确认目标是一个控制消息 ID，不能把 TCP 的“下一个期望字节序号”照搬成 UUID 的大小关系。
[RFC 9293 §3.1、§3.5、§3.10.2](https://www.rfc-editor.org/rfc/rfc9293.html)

#### 最小平面 envelope

| 字段 | 含义与约束 |
| --- | --- |
| `protocol_version` | 仍提议 wire v16；R1/R3 均未发布，不为修订草案递增 wire 版本 |
| `message_id` | 本条逻辑消息的身份；有业务 payload 时沿用其 E/T/C/R/工具/查询身份，否则新 ID |
| `connection_epoch` | 保留原连接 fencing；初始 Register、纯 ACK 为 null，纯 ACK 不授予 epoch |
| `ack: bool` | true 表示携带对某条控制消息的接收确认；false 表示未携带此确认 |
| `ack_id: MessageId \| None` | 当且仅当 ack=true 时有值，指向本 Session 收到的那条控制消息的 message_id |
| `correlation_id: MessageId \| None` | 业务关联独立保留；无 payload 时为 null，不用它承载控制 ACK |
| `control` 与控制操作字段 | 原 register/registered/heartbeat/error 标签与各自字段仍在顶层；control=null 表示没有新控制操作 |
| `payload` | 方向明确的 typed 业务 payload，允许为 null；业务调用、回复与完成确认都在这里 |

wire 的共同字段必须出现，空值明确为 null。ack 按严格布尔值校验，不接受 0/1 或字符串。
`ack=false, ack_id≠null` 和 `ack=true, ack_id=null` 都是非法帧。没有控制、payload 或 ACK 的
空帧非法；纯 ACK 的 control/payload/epoch/correlation 均为 null。这些规则在整帧 parse 时检查。

单独一个 bool 无法说明确认了 H1 还是 H2；复用 correlation 又无法同时确认 H、回复 C。
因此建议只增加一个 `ack_id`，不增加通用 receipt 包装、业务 status 或序列号。
一个 ACK 精确确认一个控制 ID，不默认为“之前所有控制均已确认”。

**关键结果：** 即使是 tool_result、Succeeded、Failed、ResultAccepted、StopAccepted，
没有顺带确认控制消息时也全部是 `ack=false`。控制 ACK 不能解析成任意一种业务回执。

#### 同帧示例

以下是完整形状示例；固定 ID 只用于展示关系。实际 ID 生成沿用对应消息算法。

**控制 ACK + 业务回复：** server 确认心跳 H，同时回复另一条 Flow 调用 C。
两者的目标分别为 ack_id 与 correlation_id；即使业务回复是失败，控制接收确认仍可以为 true。

```json
{
  "protocol_version": 16,
  "message_id": "11111111111141118111111111111111",
  "connection_epoch": "epoch-2",
  "ack": true,
  "ack_id": "22222222222242228222222222222222",
  "correlation_id": "33333333333343338333333333333333",
  "control": null,
  "payload": {
    "type": "tool_result",
    "call_id": "call-7",
    "result": null,
    "error": "unsupported tool",
    "error_kind": "invalid_request",
    "retryable": false,
    "retry_after": null
  }
}
```

**新控制 + 业务回复：** worker 的 lease 心跳与一次历史查询结果同帧。ack=false 仍然是业务回复。

```json
{
  "protocol_version": 16,
  "message_id": "44444444444444448444444444444444",
  "connection_epoch": "epoch-2",
  "ack": false,
  "ack_id": null,
  "correlation_id": "55555555555545558555555555555555",
  "control": "heartbeat",
  "job_id": 5,
  "assignment_token": "assignment-1",
  "payload": {"type": "tool_result", "page": null, "error": "query_busy"}
}
```

**新控制 + 控制 ACK + 新业务调用：** Registered 确认收到 Register，同时带一个历史查询。
worker 必须先处理 Registered，之后才分派查询；ack=true 不会把这个新调用变成回复。

```json
{
  "protocol_version": 16,
  "message_id": "66666666666646668666666666666666",
  "connection_epoch": "epoch-2",
  "ack": true,
  "ack_id": "77777777777747778777777777777777",
  "correlation_id": null,
  "control": "registered",
  "heartbeat_interval": 10.0,
  "recovery": null,
  "payload": {
    "type": "tool_call",
    "method": "worker.history",
    "arguments": {
      "session_id": "019eac3b-a60c-7a93-a442-3718f660fc8c",
      "cursor": null,
      "limit": 100
    }
  }
}
```

#### 控制接收确认的边界

| 收到的内容 | 是否产生控制 ACK | 原有作用保持 |
| --- | --- | --- |
| Register | 身份匹配且合法首帧被接纳后确认，首帧检查仍在 Session 之前 | ACK 不等于 Gateway 发布、恢复成功或 Registered |
| Registered，包括 recovery=resume/replay | 合法帧被本连接接收入队后确认 | 业务 owner 仍负责 epoch/恢复验证及 connected；ACK 不替代这些判断 |
| Heartbeat | 合法帧被接收入队后确认 | 只是收到；是否续 lease 仍由 server 检查 job/token/epoch 和业务状态 |
| ProtocolError | 致命错误不要求 ACK，也不等待 ACK 才关闭 | 按原路径报错和关闭；不能夹带待执行 payload |
| ACK-only 或某帧的 ACK 字段 | 不再确认 | 防止 ACK 循环；同帧若有新的可确认控制，确认的是那个新控制 |
| 只有业务 payload | 不产生控制 ACK | 业务回复、完成确认、重试仍由原 owner 决定 |

初始 Register 无授权 epoch，因此 payload=null、ack=false；不能借同帧绕过首帧认证。
Registered 的 `recovery=null` 可夹带原本合法的注册后调用；恢复分支暂不夹带另一条业务操作，
避免把恢复/重放与新派发的先后关系改成隐含语义。先于 Registered 的 stop、ResultAccepted、
RecoveryRejected 仍是原顺序的 payload 帧，可以顺带确认 Register，但不会提前宣告 ready。

Heartbeat 仍按原 lease 条件发送，不能为了合帧伪造一个已经没有 lease 的心跳；同帧携带执行
消息时，双方的 job/token 必须一致，业务 payload 仍单独过 fence 检查。等待 Started、空闲和
停止阶段保留各自的心跳准入/处理规则。ACK 不延长 lease，不重置业务接收超时。

#### Session 的最小确认机制

提议 Session 持有有界的待发送控制 ID FIFO，以及一个发送任务；上限沿用现有入站消息条数
量级，不创建逐条 ACK Future、发送方 outstanding 表、定时器或重试状态机。每条被接纳的
可确认控制登记一次；容量检查与入队之间不 await。溢出按 SessionOverflow 关闭，不静默丢旧项。

- 下次出站写可以从队首顺带一个 ACK；没有业务出站时，发送任务发纯 ACK，不无限等待合帧。
- reader 只登记，不 await 网络写。纯 ACK 发送与 piggyback 都走现有 send lock、写失败封闭
  和实际写完成逻辑。只在成功写入后移除对应队首；is_current 拒绝写时保留它。
- ACK 在持有写锁、is_current 检查之后选取并附加，不修改调用方持有的消息对象。guard 与
  transport.send_text 之间仍不得新增 await。纯 ACK sender 也用同一条 send 路径，不另写 transport。
- 只确认当次 Session 实际收到的控制。关闭清空队列并 join sender；未发 ACK 不跨连接重放。
- 收到格式合法的 ACK 只作为连接层接收反馈，不驱动任何业务转换。本阶段没有等待控制回执的
  调用者，因此不增加 ACK 丢失超时；重复/未知 ack_id 不释放对象、不拒绝同帧的合法业务 payload。
  这也意味着本阶段不承诺检测“对端漏发 ACK”，不能据此宣称可靠交付。

需要一个方向定型的纯 ACK 构造回调供 Session 使用；不增加 Endpoint/Context/Run。已在 transport
认证的首个 Register 显式登记一次，不能为了统一接收入口而把真实首帧检查移到分派之后。
新增 ACK 写可能更早发现断连，写失败仍按现有 Session 规则处理；不因此停止跨重连的执行 Task。
没有延迟 ACK 定时器、自动拼接两个独立业务帧或改动 Runtime 装配。

#### 同帧分发与类型

保留直接的控制 wire 模型及两个方向的无新控制模型，共享消息头；每个具体模型声明该方向的
payload 联合，不增加 WorkerHeader/ServerHeader 中间类。control 在外层判别、payload 按 type
判别、worker toolcall 按 method 判别；没有 all-optional 控制字段袋、Any 路由或新 KIND 类别。
R1 的互斥 BusinessMessage 泛型方案由此替换。Pydantic 的 null discriminator、字段窄化和
完整序列化在获批后的首个实现步骤验证；本轮尚未做类型/运行时验证。

当前 Session._read 在 query reply 或 Responder 命中后直接 continue；handler 的 Heartbeat
分支也直接 continue，这两处都不能用于吞掉整条混合消息。[S2][S4] 采用以下具体分派顺序：

1. **整帧验证。** parse 必须验证控制、payload、方向及两者的静态约束；非法 payload 不会因为
   控制部分合法而先被 ACK。整帧大小用于入站限额；致命 error 不能藏一个可执行查询。
2. **纯 ACK。** reader 消费，不进入业务 FIFO、不进入 pending、不影响 ready/epoch 或接收计时。
3. **没有新控制的 payload（可带 ACK）。** 保留原 reply_key/Responder 快速路径、busy 限额
   及未匹配/迟到 query reply 的丢弃规则；其他业务照旧进入 FIFO。
4. **新控制 + 可选 payload。** 整帧作为一个 FIFO 项交给原控制 owner，不先旁路执行其中的查询。
   登记的是控制接收 ACK，不承诺控制业务效果已完成。先处理控制；失败则不执行同帧 payload。
5. **控制通过后。** owner 调用 Session 的 `dispatch_payload(message) -> bool`，复用现有
   临时 reply/Responder 分派函数：被临时机制消费则不再分派业务；否则由原业务分支处理 payload。
   reader 只对“没有新控制”的帧调用此函数，owner 只对“有新控制且已处理”的帧调用，恰好一次。

这样支持 Heartbeat+ConversationResult 和 Registered+ConversationQuery，不以实现方便为由
禁止这两种组合，也不制造“剥掉 payload 后的新消息”或额外上下文对象。查询收到的是原完整帧，
broker 的响应字节限额仍计算完整序列化结果。混合帧按原始字节数只占一个 FIFO 项。

**明确取舍：** 带新控制的查询要等待其控制 owner，不能承诺绕过这个前置步骤；普通查询与
ACK+查询仍保留原旁路能力。发送方不会为了 piggyback 新控制而延迟一个本可立即发送的查询。
这不是跨两部分的事务：控制已完成而 payload 随后失败时，控制效果不回滚。业务重试/持久化
仍由原 owner 处理；临时查询超时不跨连接重放。

具体改动位置：

- `communication/session.py`：提取现有临时分派逻辑供 reader/owner 二选一调用；新增有界 ACK
  发送队列/任务；扩展 close 的 join，保留 pending、busy、FIFO、actual-send 与取消合同。
- `controler/workers/transport.py`：实际首帧/身份检查不动；认证后登记 Register 接收。close 仍
  只吞 WebSocketDisconnect，其他错误继续传播。
- `worker/transport/client.py`：先处理 Registered 再处理其首个 payload；drive/stop/replay
  只检查业务 payload 与关联，不把 ack=true 当作成功。Started 的实际发送顺序不变。
- `controler/workers/handler.py`：正常/停止循环分别处理心跳，再按上述函数分派剩余 payload；
  不在心跳分支无条件 continue，也不把 stop 阶段的心跳改成普通续 lease。
- `controler/workers/conversation.py`：按 payload 类型及 correlation/epoch 识别回复，不要求
  它必须来自无控制的 frame 模型；早到回复仍不能替代实际 request 写完成。

#### 全部消息的 ACK 映射与身份

R1 的 payload 字段、toolcall method/arguments、E/T/C/R 算法不改；下表补齐 ACK 后的 19 类映射。
ACK 字段只由接收过控制的 Session 附带，与下面的业务 correlation 独立。

| 原消息 | 控制或业务位置 | message_id / correlation_id | 控制接收确认 |
| --- | --- | --- | --- |
| Register | control=register | 新 ID / null | 对端确认 Register ID |
| Registered | control=registered，recovery=null | 新 ID，或同帧 payload 的 ID / payload 的关联或 null | 本条控制可被确认；对 Register 的确认使用 ack_id |
| ResumeAssignment | registered.recovery=resume | 新 ID / null | 同 Registered；不以 ACK 授权 resume |
| ReplayResult | registered.recovery=replay | 新 ID / null | 同 Registered；不以 ACK 授权 replay |
| Heartbeat | control=heartbeat | 新 ID，或同帧 payload 的 ID / payload 的关联或 null | 对端确认收到，不表示 lease 续期成功 |
| ProtocolError | control=error | 新 ID / null | 不要求 ACK，不等待 ACK 才关闭 |
| RecoveryRejected | payload.type=recovery_rejected | 新 ID / Register ID | payload 自身不产生控制 ACK |
| Assignment | tool_call(worker.execute) | E / null | 同上 |
| Started | payload.type=started | 新 ID / E | 同上；控制 ACK 不能替代 Started |
| Succeeded | payload.type=succeeded | T / E | 同上 |
| Failed | payload.type=failed | T / E | 同上 |
| ResultAccepted | payload.type=result_accepted | 新 ID / T | 同上；仍须业务应用提交 |
| FlowToolRequest | tool_call(method=原 name) | 原 PendingToolCall 的 ID / null | 同上 |
| FlowToolResponse | payload.type=tool_result | 新 ID / FlowToolCall ID | 同上 |
| StopExecution | tool_call(worker.stop) | C / null | 同上 |
| ExecutionStopped | payload.type=execution_stopped | R / C | 同上 |
| StopAccepted | payload.type=stop_accepted | 新 ID / R | 同上；仍须原停止确认条件 |
| ConversationQuery | tool_call(worker.history) | 新 ID / null | 同上 |
| ConversationResult | payload.type=tool_result | 新 ID / HistoryCall ID | 同上 |

Registered 本身是握手决定；对 Register 的纯接收 ACK 可以早于它独立发出，所以 Registered
允许 ack=false，不再用 correlation 指向 Register。单连接只允许一次初始 Register，握手与
epoch 的授权仍由原 owner 检查。RecoveryRejected 的 correlation 保留其拒绝的恢复请求关联，
不借此确认连接控制。

message_id 仍表示逻辑消息，不是某次物理 write 的编号。跨重连重放相同业务时保留业务 ID，
重新建立 epoch/ACK 头，不重放旧连接的 ACK。同一连接内不把同一个 ID 改成不同业务或不同控制
操作；重复投递仍靠业务 fence、digest/ledger 判断，不能用一个“整帧已 ACK”缓存去去重业务。
epoch、assignment token、call_id、message_id 和 ack_id 的用途分别保留。

#### 持久化、验证与审批

仍提议 wire v16 / journal v5 / ResultInbox v1。ack、ack_id、控制字段不进入 journal、inbox、
terminal digest、stop digest 或 tool ledger；存储边界只接收 typed payload。控制字段变化不能
改变 T/R 或已有摘要。source=control 是原 server 生成业务失败的来源，不从 control/ack 推导。
原 terminal 字节算法、固定 v15 stop 摘要投影、真实回执核验 v14/v15 旧摘要、有限旧状态只读转换、
COALESCE 来源 SQL、未知数据保留和降级限制全部沿用 §1.7；不新增持久 ACK 表或 DDL。

获批后的验证补充如下；不是本轮已执行的测试：

1. 严格布尔、ack_id 配对、空帧、首帧认证、方向、混合 payload 全帧校验；覆盖全部 19 类映射。
2. ACK-only 不进入业务 FIFO，不延长业务 timeout，不绑定 epoch/ready；ACK 不触发 ACK 循环。
   ACK 与新控制同时存在时，仍仅对新控制产生一次确认。
3. ACK 队列有界、不丢旧项；actual send 成功才出队，guard 拒绝保留，写失败封闭，close join；
   双向写阻塞时 reader 仍能处理普通临时回复。未知 ACK 与有效 payload 同帧不吞 payload。
4. 运行三个示例代表的混合场景：控制和 payload 各处理一次；历史 pending 只完成一次，未命中
   pending 不吞控制；非法控制不启动查询；Registered 先于同帧调用；整帧限额不被拆分绕过。
5. `ack=false` 的 ResultAccepted/StopAccepted 正常生效；只有 ack=true 不能清 journal、结束
   stop 或完成工具 Future。原提交/停止条件、Started 实际发送顺序和离线完成竞态不变。
6. 跨连接变更 ACK/控制头而保留业务 payload 时，稳定 ID、terminal/stop 摘要和 ledger 内容
   不变；原持久旧数据、source SQL、替换关闭、真实 PostgreSQL 与全量验证继续执行。

消融覆盖：把 ACK 当业务成功、ACK 再触发 ACK、写前清队列、恢复原 continue 吞 payload、混合
查询绕过控制验证、忽略纯查询旁路、将控制头混进摘要、扩大 close 吞错范围；对应合同应能捕获。
只为公开行为保留必要回归测试，不把一次性消融脚本纳入永久测试。

本轮重新 fetch 后 main@origin 未变。已按本机只读 Oracle 流程咨询；初始化与实际响应均为
claude-opus-5-5，tools/MCP 为空，result success、is_error=false、退出 0。核验记录在
`.amp/in/artifacts/control-ack-oracle-verification.json`，原始输入/事件流不发布。
主线程未采用草稿中的“只保留最新 ACK”、禁止控制+临时查询、额外方向 Header 层和仅接收端
支持混合帧却不验证发送路径的限制；采用精确 ID FIFO、owner 先控制后分派、直接方向模型。
这些属于方案取舍，尚未运行类型验证或产品行为测试。

**实际交付：** 本地文档 R3，历史原文保留；无实现或测试改动，未 push、未开 PR、未部署。
**待审批：** ack_id 与 correlation 分离、有界控制接收 ACK 发送、混合查询的控制前置顺序，
以及仍未批准的整体协议/持久化方案。发布 docs-only 草案与实施仍需明确授权。

### 2.4 R4 — 采用 ack_id（2026-11-03）

用户在比较 TCP 的序号确认方式后明确：“那我们用 ack_id。”采用 R3 的离散控制消息 ID
确认方式，不新增 TCP 式序号空间；本决定替代 R3 中 ack_id 字段仍待选择的状态。
[本阶段讨论](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)

保留先前确定的 `ack: bool`：`ack=true` 时，ack_id 指向收到的控制消息的 message_id；
`ack=false` 时，ack_id 为 null。业务 correlation_id 继续独立表达业务关联；控制、ACK 和
payload 可以同帧，ACK 不表示业务完成。

本次只记录对既有提案字段的选择，不生成新的接入方案或重复 Oracle 咨询。Session 的确认发送、
混合分发、整体协议与持久化方案仍待审批。本地文档已更新，未修改实现或测试，未 push、未开 PR；
此字段决定不构成整体实施授权。

### 2.5 R5 — 批量 payload 与显式结果接纳调用（2026-11-03）

#### 要求、状态与替代范围

用户提出 payload 改成 JSON list，可以同发多个 tool_call/tool_result；worker 将执行结果和
要求 server 确认接纳的调用放在同一列表，ResultAccepted 改为后一个调用的 tool_result。
以下为据此形成的具体提案，方法名和接入细节仍待审批，不构成实现授权。
[本阶段讨论](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)

本节替代 R1/R3 的单 payload、外层业务 correlation_id、帧与业务共享 message_id，以及
Succeeded/Failed/ResultAccepted 独立 wire 类型；替代 R3 的 Register 一律不能带 payload。
初案/R1–R4 原文保留。控制仍在顶层，允许与 payload 同帧；ack/ack_id 的控制接收语义不变。
Started、停止回执/确认、恢复拒绝等其他既定业务条目本轮不顺带改造成新的 RPC；进入列表后
也独立携带业务 ID/关联。完整最终映射是 R3 的映射应用本节增量，不同时保留两套 wire。

#### 帧身份与条目身份

| 位置 | 字段及职责 |
| --- | --- |
| 外层 | protocol_version、message_id、connection_epoch、ack、ack_id、control 及控制字段 |
| 外层 message_id | 本次帧的 ID，也供对端 ack_id 确认其中的控制；每次重放重新建帧 |
| payload | 必填有界列表；无业务时为 `[]`，不再是对象或 null |
| 每个条目 | 独立 message_id、correlation_id；后者关联具体调用/业务条目，调用通常为 null |
| 业务参数 | assignment_token、原工具 call_id、stop.request_id 等仍各负其责 |

删除外层 correlation_id；多条回复不能共用一个目标。E/T/C/R 算法与原 PendingToolCall ID
不变，只下移到条目。重连替换帧 ID 和 epoch，保留需重放的条目 ID；列表下标不充当身份。
帧 ID、条目 ID、epoch、assignment token 和工具业务 call_id 都不互相替代。
纯 ACK 是 control=null、payload=[]、epoch=null，仍不能设置 ready 或续 lease。

#### 结果提交与接纳调用

| 条目 | 方向 / 类型 | 内容与关联 |
| --- | --- | --- |
| 执行终态 T | W→S，tool_result，method=worker.execute | correlation_id=E；job_id、assignment_token、result/error 二选一 |
| 接纳调用 A | W→S，tool_call，method=server.accept_result | correlation_id=null；arguments 为 job_id、assignment_token、result_id=T |
| 接纳返回 | S→W，tool_result，method=server.accept_result | 新条目 ID、correlation_id=A；result 为 job_id、assignment_token、state（done/failed） |

A 使用原 UUID5 命名空间和紧凑 JSON 数组 `["server.accept_result", T]` 导出，T 是规范的
32 位小写 hex ID。A 不再另设 correlation_id=T，避免和 arguments.result_id 重复表达。
接纳返回无需稳定新身份或另一层确认：丢失后重发原 A，server 可以生成新的返回条目。

tool_result 增加所回复的 `method`，便于无临时 pending 的执行恢复直接定型；解析后还必须
核对 method、correlation 和 owner 期望，不能凭 method 授权。结果对象保持直接字段，不加
`kind`，不把旧 `Succeeded`/`ResultAccepted` 整个 wire 对象嵌进 result，也不增加 result_kind。

- worker.execute 结果：job/token、ExecutionResult|null、error|null，严格 exactly-one。
- worker.history 结果：原 page/error，继续保留查询错误分类和 cursor 语义。
- Flow 工具结果：原 call_id/result/error/error_kind/retryable/retry_after；method 回显调用名。
- server.accept_result 结果：上述严格 result；执行失败仍可以被成功接纳，state=failed 不等于
  接纳请求失败。缺失、冲突、尚未提交继续走下面明确的错误/恢复路径，不伪造成功。

每个方向先按 type、已知 method 选严格模型；worker 结果只接受 worker.execute/history，
server 结果识别 server.accept_result 后，其余仍用原 Flow 结果形状严格验证。
W→S 调用精确识别 server.accept_result，其余按原 FlowToolCall 参数校验并交 FlowToolService；
未知 Flow 方法仍返回 INVALID_REQUEST。不得按字符串调用任意 Python 方法。使用局部明确分支/
严格 TypeAdapter，不引入注册表、Endpoint/Run/Context 或 all-optional 结果模型。

下面是执行失败的完整结构示例。E/T/A 按既定算法计算；帧 ID 和返回 ID 只为示例。
成功执行仅将终态的 result/error 换为具体 ExecutionResult/null，接纳仍独立发生。

```json
{
  "protocol_version": 16,
  "message_id": "11111111111141118111111111111111",
  "connection_epoch": "epoch-2",
  "ack": false,
  "ack_id": null,
  "control": null,
  "payload": [
    {
      "type": "tool_result",
      "message_id": "7eecf358047357e3904fae12781b482c",
      "correlation_id": "ee3eaa171e5652b8bff155694215e29b",
      "method": "worker.execute",
      "job_id": 5,
      "assignment_token": "assignment-1",
      "result": null,
      "error": "executor exited 1"
    },
    {
      "type": "tool_call",
      "message_id": "b7723aac9a2f534e822941852084c2f2",
      "correlation_id": null,
      "method": "server.accept_result",
      "arguments": {
        "job_id": 5,
        "assignment_token": "assignment-1",
        "result_id": "7eecf358047357e3904fae12781b482c"
      }
    }
  ]
}
```

业务应用提交后，server 才发送：

```json
{
  "protocol_version": 16,
  "message_id": "22222222222242228222222222222222",
  "connection_epoch": "epoch-2",
  "ack": false,
  "ack_id": null,
  "control": null,
  "payload": [
    {
      "type": "tool_result",
      "message_id": "33333333333343338333333333333333",
      "correlation_id": "b7723aac9a2f534e822941852084c2f2",
      "method": "server.accept_result",
      "result": {"job_id": 5, "assignment_token": "assignment-1", "state": "failed"}
    }
  ]
}
```

以上都可以 ack=false；若同帧顺带确认已收到的控制，才设置 ack=true/ack_id。
不增加独立 ResultAccepted wire 类型，也不再向这个接纳返回追加另一个接纳调用。

#### 处理顺序、部分失败与幂等

1. server 对 T 校验 E/T、认证 worker、job/token、当前 epoch 和摘要，沿原路径先持久 inbox，
   再调用 `results.apply`。收到 T 不能直接 break 而吞掉同帧 A；不向 T 主动发送接纳返回。
2. A 用 job/token 查已有 inbox，核对它的 result_id 和 A ID 与已存 digest 导出的值。只有
   `result_applied_at` 非空且 state 为 DONE/FAILED，才能返回成功；正常在线 A 单独到达时，
   可对同一个当前、已持久但未应用的结果走已有 `results.apply`，不要求再传 T。
3. 单独 T 未跟 A 时，原结果应用仍继续；这个 worker 的分配循环保留等待接纳调用的状态，
   不提前派发下一任务。stop、独立 history 和其他 worker 不因等待 A 而停住；仍有原接收超时。
4. 未提交的 ResultPending 不返回成功，保留 1013 关闭/原重连退避。正常在线 A 找不到结果或
   fence/ID/内容冲突按原协议错误拒绝；注册恢复中的“缺结果”按下一节要求请求重放。
5. 相同 T/A 重放按现有 digest、job 应用锁和提交事实处理，不新增通用去重表或 A ledger。
   已完成旧 job 的重复 T/A 经完整持久 fence 校验后可无副作用接受/重回结果；不能清空正在
   执行的新 job。只有 A 对应当前等待确认的终态，实际返回写成功后才退出该 assignment。
6. worker 核对当前 epoch、method、job/token、由自己的终态导出的 A 和终态 state，才调用
   原 acknowledge 路径。不能退休活跃 Task 或未确认 stop；普通 journal 保留 acknowledged，
   只有符合原条件的成功 archive 才删除 journal。[S5][S12]

列表不是事务：T 已持久、A 未处理或返回丢失时不回滚 T；靠原 journal/inbox 重放恢复。
一次 WebSocket 写成功不证明两个业务操作都已完成。仅有控制 ACK 更不能释放 journal。
当前代码的 inbox 持久化早于业务应用，最终发送确认还有独立提交检查；本提案保留该边界。
[result 持久化](https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/persistence/workers.py#L441-L474) · [S4]

#### 保留 Registered 前恢复：Register 可以带接纳调用

当前恢复快路径只凭 recovery descriptor 与 DB 已提交摘要，就能在 Registered 之前确认结果。
改为 tool_result 后，必须存在实际接纳调用，不能凭空制造一个请求再回复它。[S3][S4]

- worker 先收集离线完成结果。仅当 journal 有未 acknowledged 的 terminal、没有 stopped、
  没有进行中的 stop/取消清理时，Register 携带 `[A]`；不带 T。A 与 recovery 的 job/token/digest
  必须一致。普通首次注册、已确认的保留 journal 等发送 `[]`。已确认 journal 仍发送原
  recovery descriptor；journal 的 root/Flow 归属证据保持，不通过省略 descriptor 跳过恢复核查。
- 实际首帧先完成认证 worker_id、Register 以及其至多一个 A 的静态校验；Gateway 替换/join、
  登记和 worker_connected 之后，才按新 Session epoch 核验 DB。Register 的 epoch=null
  只允许这一个恢复确认调用；不开放注册前任意工具、查询或执行。没有业务效果先于认证/登记。
- stop 优先，仍按原 stop receipt/nonce/binding/effects 条件确认；同帧 A 不能绕过 stop。
  原 acknowledge_stop 会标记 journal 已确认，后续不把其中保留的 terminal 当成待接纳结果。
- A 与 recovery 静态矛盾直接拒绝；旧 assignment 已失效则沿原 RecoveryRejected。DB 已提交
  且 digest 相同，回复实际 A，worker 校验并处理接纳后再处理后续 Registered。
- DB 尚无结果或未应用：沿原授权检查发送 Registered.recovery=replay，不在注册过程中
  apply，也不等待 T 才发恢复决定。worker 再发 `[T,A]`，保留同一个 A。DB digest 冲突绝不
  返回成功，沿原冲突/恢复拒绝分支处理，不强行恢复已终止任务。
- 没有 A、但 descriptor 与已提交结果匹配时，只完成原恢复检查并 Registered，不发送无人
  请求的 tool_result；正常客户端的已确认保留 journal 走此路径。无需新增 descriptor.acknowledged。
- 若完成发生在 Register 收集状态之后，原 resume 路径再次收集终态并发送 `[T,A]`。
  不能根据旧 descriptor 重跑普通执行。接纳返回仍可先于 Registered 绑定 epoch，但绝不宣告 ready。

这取代 R3 的 Register 禁止 payload；致命 ProtocolError 仍 payload=[]，不等待控制 ACK 才关闭。
不新增一轮注册握手、不把恢复确认放进 Session 临时 pending，不更改 Runtime 装配。

#### 批量分派与资源边界

- 整帧严格验证后才有动作：控制、所有条目的类型/方向/ID、同帧重复 ID、静态跨字段约束及
  大小。建议每帧最多 32 个业务条目；保留 32 个排队帧和 16 MiB 原始字节上限，并新增
  最多 32 个排队业务条目的配额。旁路 query 的 pending/handler/busy 各自限额保持。
  入队前预检所需容量，不能只看帧数或为装不下的条目静默丢弃。
- 有新控制时由原 owner 先处理控制，然后逐条分派；无新控制时 Session 保留普通 query
  旁路。匹配 pending、丢弃迟到查询回复、启动 responder 都只消费当前条目，不能 continue
  整帧或再次交给业务 owner。没有新控制的独立查询不被同帧耗时业务阻塞。
- 剩余执行业务条目在原 owner 内按列表及帧接收顺序处理。完成一个 assignment 的分支不
  丢掉列表后续条目；后续条目按转换后的业务状态验证。不得仅因同帧而跳过 job/token fence。
- 可在现有 inbox 内保留原帧和未消费下标，用普通内部元组传递帧/所选条目；不增加业务
  Context、通用 Endpoint 或另一套队列 owner。原始帧字节仅计一次，直到它不再被排队项持有。
  query key 改从所选条目的 correlation_id 与原帧 epoch 提取，不用帧 ID。
- history 的 512 KiB 回复上限按所选结果条目及共享头计算，整帧另受总字节限额约束；不能
  因为同帧含一个无关终态就把合法历史页认作超限。响应校验仍有 session/cursor/条数检查。[S6]
- 列表不是所有条目的完成顺序承诺：查询可以先于独立业务完成，但 `[T,A]` 在同一业务
  owner 中按序处理。控制接收 ACK 不表示任何条目已完成。无自动凑批、批量事务或整帧重放。

`Session.request` 仍是临时查询：一次调用显式选一个请求条目，不自动为列表中的全部 toolcall
创建 Future；返回关联的结果条目及所需帧信息。提前回复仍需等待实际写完成。execution/tool
恢复由原 WorkerExecution 持有，重连只重发需要的条目；history 不随原混合列表重发。[S2][S5]

#### 持久化、实施顺序与验收

wire16、journal5、ResultInbox v1 仍是未发布提案，不因本轮文档修订再递增。journal/inbox
只存终态/停止业务数据，不存整帧、列表、控制 ACK、method 回显、帧 ID 或条目关联。
终态的在线 tool_result 显式投影为 durable TerminalPayload；摘要继续使用既定
`{"type":"succeeded","result":...}` / `{"type":"failed","error":...}`，不是哈希 tool_result。
因此旧 journal4/inbox v14/v15 可经严格只读转换重建相同 T/A，不新增持久 ID 或 DDL。
stop digest、tool ledger、source=control、未知状态保留、惰性写与降级限制均沿用 §1.7。

获批后的实施顺序：

1. `contracts/protocol.py`：列表、帧/条目身份、严格结果模型与 A 算法；先验证类型窄化和
   序列化，不照搬 Oracle 的 kind+旧消息嵌套结构。持久化 reader 继续与 wire parser 分开。
2. `communication/session.py` 与 `controler/workers/conversation.py`：逐条旁路/FIFO、容量、
   query 关联与字节边界；沿用 R3 的控制确认队列和 actual-send/取消/close/join 合同。
3. `controler/workers/handler.py`、`transport.py`：T/A 处理和原连接返回、首帧 A 的准入、
   已提交重放与恢复优先级。transport.close 仍只吞 WebSocketDisconnect。
4. `worker/transport/client.py`、`worker/execution/lifecycle.py`、`worker/state/execution.py`：
   `[T,A]` 的构造/重放、Register 中 A、接纳验证和列表状态转换；Started 实际发送之后才
   record_started/启动 Task。其他持久化接入仍按原阶段四计划完成，不能因本轮未新增 DDL 而漏做。

验收通过公开收发行为覆盖：成功和失败终态的接纳；inbox 已存但业务未提交不能回 A；
部分列表处理后断连/提交后丢回复，恢复仍返回同一 A 且不重复业务；T/A 分帧与重复；
错误 result_id/token/epoch 与旧 job 回复不能清空新执行；Register+[A] 的已提交快路径、
缺结果重放、stop 优先、已确认保留 journal 无 A，以及 Register 后 Resume 前完成竞态；
混合控制与多 query/业务不吞项、不双重分派、容量和超时可控；旧数据恢复 T/A 和摘要不变。

计划消融：去掉提交检查、T 后提前 break、用帧 ID 关联条目、查询分支吞掉整个列表、
整帧跨连接重放、Register 中 A 绕过 stop/认证，各自必须被上述合同测试捕获。
其余前三阶段的关闭异常、原 Future/Task 寿命、stop 安全和真实 PostgreSQL 回归继续验收。

**咨询与实际交付：** 本机 Opus 5.5 只读咨询完成，init/响应模型一致、tools/MCP 为空、
success/exit 0。采用批量身份、显式 A、恢复携带 A 和部分处理建议；未采用 kind 包装旧消息、
稳定接纳回复 ID、A 的重复 correlation，以及把全部 apply 推迟到 A 的建议。
咨询不是行为验证；本轮仅有文档/示例检查。只更新独立 workspace 的本地计划，无实现或测试
改动，未 push、未开 PR、未部署。待审批为本节具体方案及尚未批准的整体接入/持久化方案。

### 2.6 R6 — reply_to 与按方法注册处理回调（2026-11-03）

#### 用户决定与提案范围

用户已明确接受 `reply_to` 这个名字：从本修订起替代业务条目的 correlation_id，表示所回复
消息的 message_id；不新增身份、不改变 R5 的 ID 算法，也不保留旧字段别名。历史原文不改写。
用户进一步提出业务/功能处理都用 tool_call 的回应与新调用组合，并按 method/type 路由到
注册的处理实例。下面收敛为一个薄分派机制，尚非整体实施批准。
[本阶段讨论](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)

本节替代 R5 的“禁止任何注册表”和保留特殊生命周期 payload 的提案。目标业务条目只有
tool_call/tool_result；Register、Registered（含 recovery）、Heartbeat、ProtocolError 与控制
ACK 仍在 envelope，不能把“统一 payload”解释成取消控制部分。Runtime 对称装配仍后置。

#### 注册表只决定调用哪个处理入口

每个方向显式登记 `(type, method) -> callback`。callback 可以是已有实例的绑定方法，
不为每个 method 新造一个 Handler 类。一次匹配只执行一个入口，不向所有订阅者广播。

| 方向 / key | 回调借用的现有实例与职责 |
| --- | --- |
| S→W，tool_call / worker.execute | WorkerClient 的执行接入；借用 WorkerExecution 接收、启动 |
| S→W，tool_call / worker.stop | WorkerClient 的停止接入；借用原 WorkerExecution.stop |
| S→W，tool_call / worker.history | 原历史查询回调；仍由 Session 有界调度 |
| W→S，tool_result / worker.history | 原临时查询结果入口；Session 按 reply_to/epoch 匹配 pending |
| W→S，tool_call / flow.open_pr | 原 WorkerHandler 的 fence 检查后调用 FlowToolService |
| W→S，tool_result / worker.execute | 原结果接入：校验、持久 inbox，再 ResultApplication.apply |
| W→S，tool_call / server.accept_result | 原结果接纳检查，提交后通过原 Session 返回 |
| S→W，tool_result / server.accept_result | WorkerClient 核对具体 A，再调用 WorkerExecution 的接纳方法 |
| S→W，tool_result / Flow 工具方法 | 按原 PendingToolCall 匹配，再 finish_tool_call；不搬入 Session pending |

`type/method` 解决“执行哪段处理代码”；`reply_to` 解决“回应哪个具体调用”。同一个方法的
多个调用不能靠方法名完成同一个 Future。结果还要核对预期方法、结果模型、epoch、job/token、
原业务 call_id 等相应条件。匹配处理入口本身不授予业务权限。[S3][S5][S7]

实现只需要显式路由表与一个薄的类型收窄辅助函数：注册时给定 key、严格 payload 模型和
具体类型回调；存入统一签名的适配函数，先 isinstance 窄化再调用具体回调。重复 key 拒绝，
已知模型与 key 的不一致也拒绝；不用 Any、cast 或反射 getattr 执行业务。具体泛型签名在
获批后的类型验证步骤确定，不直接采用未经验证的 ParamSpec 通用框架。

原 Session、帧/条目和现有阶段局部值直接传给回调，或由连接内的绑定函数捕获；保留现有
server current 三元组，不把所有连接的 current 放到一个共享 Handler 实例字段。注册表保存
处理方法，不保存第二份 peer/connection 表、journal 或跨连接 Future。[S4][S9]

Flow 工具继续直接使用 flow.open_pr 等原工具名作为 method。保留一个明确、定型的未知
Flow 调用入口，继续返回 INVALID_REQUEST；其回复也只能匹配原 PendingToolCall 的方法/
call_id。它不是任意方法调用入口。不采用 flow.tool/name 的重复转发层，也不把未知查询/
worker 生命周期方法都交给它。其他非法路由仍拒绝。[S7]

#### 分派不接管任务寿命，也不取消顺序约束

- Session 仍是唯一 socket reader。整帧验证、控制处理和 R5 逐项分派继续；临时查询的注册
  回调走既有有界旁路，业务回调进入原 owner FIFO。每个 key 只有一个实际消费入口，不能
  既被 pending/responder 消费，又进入业务表。迟到历史回复仍按原规则丢弃，不一律升级为错误。
- 注册与调度分开：不对所有 callback 使用 gather/create_task；同一 execution 的状态变更
  按序处理，历史查询保持有界并发。method 的业务处理继续有阶段准入检查，不因查表命中就
  在 idle、执行、stopping 或建立连接时执行相同副作用。
- 不能直接把整个 `_execute_assignment` 挂进一个串行 dispatcher，然后让它在内部等待
  dispatcher 尚未处理的回复。把 `_drive_assignment` 的 incoming/terminal/next_tool 多路
  复用提升到同一个 WorkerClient 业务消费循环；回调处理一次状态转换后返回，不嵌套 receive
  等待下一条网络结果。原 next_tool 的发送 guard、取消和跨连接 Future 保留。[S3]
- execute 回调仍先 record_received，再显式 `await original_session.send(Started 对应条目)`；
  仅实际写成功后才 record_started/launch，中间不插入 await。只返回一个“待发送列表”后
  立即启动 Task 不满足此合同。[S3][S5]
- stop 回调可等待原 shielded stop 的清理结果并发送回执，但发送后返回消费循环，由后续
  接纳回复完成停止确认；执行 Task/stop Task 仍由 WorkerExecution 持有。连接关闭不取消
  它们，控制 ACK 不完成它们。
- 回复使用回调收到的原 Session，不重新从 Gateway 获取“最新连接”。所有控制/业务错误
  继续按各自合同处理，不增加“捕获所有异常继续下一条”的通用中间件；transport.close
  仍只吞 WebSocketDisconnect。[S2][S9][S10]

#### 全部现有消息的新位置与方法

表中所有新 tool_call 的 reply_to 均为 null；tool_result 指向对应调用条目。单向方法是
静态方法合同：接收后执行，不额外返回 tool_result，不新增通用 expects_reply/ACK 标志。

| 现有 wire 类型 | R6 目标 | 关联与说明 |
| --- | --- | --- |
| Register | control=register | 保留恢复描述；按 R5 可携带接纳调用 A |
| Registered | control=registered | 保留注册字段；普通 recovery=null |
| ResumeAssignment | registered.recovery.action=resume | 沿 R1，恢复竞态检查不变 |
| ReplayResult | registered.recovery.action=replay | 沿 R1，重放而不重跑 |
| Heartbeat | control=heartbeat | 原 job/token lease 心跳，不是无条件 ping |
| ProtocolError | control=error | 致命错误不执行同帧业务、不等待 ACK |
| Assignment | tool_call / worker.execute | 稳定 E，原 assignment 业务参数 |
| Started | 单向 tool_call / server.execution_started | 原 job/token；server 从当前 job/token 核验原 E 对应的派发阶段 |
| Succeeded / Failed | tool_result / worker.execute | 稳定 T，reply_to=E；typed result/error |
| ResultAccepted | tool_result / server.accept_result | reply_to=A；A 的 arguments.result_id=T |
| FlowToolRequest | tool_call / 原工具名 | 原 PendingToolCall ID 与独立业务 call_id |
| FlowToolResponse | tool_result / 原工具名 | reply_to=原工具调用条目 ID；原错误分类 |
| StopExecution | tool_call / worker.stop | 稳定 C，原完整 StopFence |
| ExecutionStopped | tool_result / worker.stop | 稳定 R，reply_to=C；result 为原 fence 与 execution_nonce |
| StopAccepted | tool_result / server.accept_stop | reply_to=B；下面定义新增接纳调用 B |
| ConversationQuery | tool_call / worker.history | 临时调用 ID；原查询参数 |
| ConversationResult | tool_result / worker.history | reply_to=查询 ID；原 page/error |
| RecoveryRejected | 单向 tool_call / worker.reject_recovery | arguments 为 job_id、reason、register_message_id |

新增 A 沿用 R5。Started 改成单向调用后，不再把它伪装成 E 的最终结果；job/token 与当前
派发状态足以验证归属，不新增重复 execution_id。RecoveryRejected 的 register_message_id
明确指向本 Session 发出的 Register 帧；保留 job 和原恢复证据检查，清理完成后才处理
后续 Registered。它不单独授予 ready；绑定 epoch 后仍拒绝不一致的帧。[S3][S5]

#### 停止接纳采用与结果接纳一致的调用组合

worker 对 C 完成原停止与回收后，发送 `[R,B]`：

- R 是 worker.stop 的 tool_result，携带真实完整停止证据，reply_to=C。
- B 是 server.accept_stop 的 tool_call，arguments 为 request_id、flow_id、job_id、
  assignment_token、receipt_id=R。B 按原命名空间、紧凑 JSON 数组 `["server.accept_stop", R]`
  生成稳定 ID。server 返回 tool_result，reply_to=B、method=server.accept_stop，result 为
  原 StopAccepted 的完整业务 fence。返回不再请求确认。
- server 必须先验证实际 R 并通过原 StopStore.confirm 条件，才可以回复 B；不得在 R
  分支提前 return 而丢掉同帧 B。worker 从 journal 中的 R 推导 B，并检查完整 fence/epoch
  后才执行原 acknowledge_stop。确认失败和 cleanup 不确定仍按原阻塞/关闭规则处理。
- **B 单独到达不能只凭 DB hash 被接纳。** 允许同批 R，或原停止处理路径在同一个连接内
  已验证并保留的 R；没有真实 receipt 就拒绝。DB 仅存 receipt_digest，不能反推出 nonce/R。
  重连仍由 server 按原 stop intent 发送 C，worker 从 journal 重放真实 `[R,B]`，不在 Register
  里单独塞 B。不为这个组合新增持久化列或通用 receipt registry。[S8]
- C/R/B 的业务 ID 在重连后不变；只有外层帧 ID/epoch 改变。R 永远 reply_to=C，不能误把
  每次 stop 帧的新 ID 当成新的业务调用 C。

这是具体提案，选择该组合是为了让两种完成接纳共用“调用/返回”结构。另一种可行选择是
server 单向调用 worker.stop_accepted，消息更少；本提案暂不采用该不对称表达，也不增加
多种可选 wire 形状。两者都不能放松停止证明条件。

**停止阶段的在途消息：** 当前 `_stop_worker` 会吸收合法在途 heartbeat/Flow 工具请求，
对终态 record/apply，但不发送 ResultAccepted。[S4] 新回调必须保留这些阶段语义：
停止中收到 `[T,A]`，处理 T 的已有事实、校验并消费 A，但不回 A；在途 Flow 调用不得因
命中 FlowToolService 路由而继续产生新副作用。worker 以匹配 B 的回复完成停止；A 的迟到
回复不能退休 stop，也不能被通用 reply handler 当成任意 Future 成功。

#### 持久化、实现影响与验收

不增加持久化或网络兼容别名；wire16/journal5/inbox1 仍未发布，不为本修订递增。journal
与 inbox 存 typed 业务数据，旧数据严格惰性读取、未知数据保留；终态/stop 摘要固定投影
与 tool ledger call_id 不变。停止参数比较、nonce 回执构造及摘要都显式取业务字段，不能
继续通过整个新 wire 对象的 model_dump(exclude=...) 顺带纳入 method/reply_to 等元数据。

获批后，R5 的首个协议步骤补全上表模型和 B 算法；新增一个共享的薄注册/分派辅助实现
（拟放 `communication/dispatch.py`），接入现有 WorkerClient、WorkerHandler 和查询回调。
worker 消费循环的调整属于本阶段必要的收发接入，不借此重排 Runtime 装配或重新分配状态
所有权。server 仍使用原连接局部阶段状态和持久化服务，Gateway peer 路由无需新增职责。

在 R5 验收基础上，补充：执行期间 query/stop/工具返回均能推进、没有嵌套分派等待死锁；
同方法多调用只匹配各自 reply_to；每个条目只执行一个回调；Stopped `[R,B]` 的部分处理/
丢回复/新 epoch 重放与原摘要一致；B 无真实 R 被拒；stopping 时 `[T,A]` 不确认终态且
在途 Flow 调用不执行；未知 Flow 方法仍返回原 INVALID_REQUEST。已有 Started 发送失败
不启动、原 Task/Future 跨重连、server close 异常传播和真实 PostgreSQL 回归继续保留。
错误 reply_to、去掉阶段 guard、R 后提前 return、把整个 execute 会话挂成阻塞回调、
按 wire 对象重算 stop digest，应由合同测试/故障消融捕获。类型窄化与并发行为尚未验证。

**咨询与交付：** 已完成本机 Opus 5.5 只读咨询，实际 init/响应模型一致、tools/MCP 为空、
success/exit 0。采用单一分派入口、worker 消费循环调整、停止阶段处理及真实回执验证建议；
不采用固定 flow.tool 包装、重连改变业务 C、将 Heartbeat 归属视为未决、或通用 ParamSpec
框架。当前仅更新独立 workspace 的本地计划与审阅摘要，未修改实现/测试、未 push、未开 PR。
reply_to 字段名已确定；具体路由、单向方法、停止接纳组合和整体方案仍需明确批准。

</details>

## 源码依据（固定规划基线）

[S1]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/contracts/protocol.py#L27-L425
[S2]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/communication/session.py#L49-L328
[S3]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/worker/transport/client.py#L150-L351
[S4]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/controler/workers/handler.py#L112-L503
[S5]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/worker/execution/lifecycle.py#L65-L374
[S6]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/controler/workers/conversation.py#L35-L91
[S7]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/controler/workflow/tools.py#L41-L250
[S8]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/persistence/stops.py#L20-L146
[S9]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/communication/gateway.py#L17-L136
[S10]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/controler/workers/transport.py#L33-L123
[S11]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/controler/workflow/coordinator.py#L194-L218
[S12]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/worker/state/execution.py#L40-L347
[S13]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/controler/workflow/results.py#L227-L488
[S14]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/controler/workflow/lifecycle.py#L137-L175
[S15]: https://github.com/b1indsight/agentflow/blob/b99c8e39f6d391b5685a91a8d6c428c51810bb11/app/persistence/jobs.py#L47-L55
