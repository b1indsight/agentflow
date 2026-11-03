# Worker / server 统一消息协议（第四阶段）— Feature record

**Stage:** Implementation — 第一步协议/数据结构已交付；第二步实现迁移待明确授权
**PR:** [Draft #125](https://github.com/b1indsight/agentflow/pull/125)，当前不可独立运行、合并或部署
**Bookmark:** `refactor/unified-message-protocol`，两步沿用同一文档 / bookmark / PR
**Workspace:** `.amp/in/workspaces/unified-message-protocol`（独立 jj workspace）
**结构实施基线:** [`b99c8e39`](https://github.com/b1indsight/agentflow/commit/b99c8e39f6d391b5685a91a8d6c428c51810bb11)
**已推送结构 head:** [`90bc9167`](https://github.com/b1indsight/agentflow/commit/90bc9167ad57016e22c09482ca5665a40f893ba1)，不含本次文档重写
**现行依据:** 下文是完整的当前方案；历史初案/R1–R6 通过文末固定提交保留，不作为候选方案

## 当前方案（2026-11-03）

### A. 目标、状态与范围

将 worker/server 的全部通信统一为：**顶层连接控制 + payload 条目列表**。业务与功能处理
由 `tool_call`、`tool_result` 及其组合表达，通过 `(type, method)` 路由到已注册的处理回调。
连接控制与业务可以同帧，各自保留接收、授权和完成条件，为后续事件驱动架构打基础。

| 状态 | 内容 |
| --- | --- |
| 用户已确定的方向 | 控制字段与 payload 同级且可同帧；ack 是控制接收确认的 bool，ack_id 指向控制帧；payload 为列表；业务统一 call/result；reply_to 表示回复目标；按 type/method 注册回调；工作只分协议/数据结构、实现迁移两步 |
| 授权边界 | 用户已授权当前线程第一步及提 PR；第二步仍需明确授权；不授权合并或部署 |
| 实际实施状态 | C–E/J 结构已实现并完成定向验证；运行时未接入，完整 pytest/Mypy 及部分 CI 失败；实际证据和遗留项见“第一步交付与验证” |

**第一步由当前线程修改协议结构和数据结构**：C–E 的帧/条目、方法、身份，以及 J 的持久模型、
纯编解码与摘要合同。**第二步由接续线程迁移实现和调用方**：F–I 的收发/分派/业务行为，
J 的实际存储读写、SQL 与恢复接入。整体覆盖全部 19 种原消息、双端收发、持久化与回归。
这是同一 feature 内的两步，不拆成独立发布；第一步是未集成中间态，第二步完成前不能作为
可运行或可部署版本。不为第一步单独运行增加旧 wire 兼容层、别名或过渡包装。

结构实施使用上列远端基线。本次文档重写未重新 fetch，不声称它仍是远端最新 main。
不以根 checkout 或本地 main 为基线；嵌套 workspace 无独立 `.git`，git 可能误读根仓库。
继续在独立 jj workspace 开展，保留根工作区的 Web 变更及其他线程工作。

Runtime 装配完全对称后置；可靠领域事件/outbox、中间件、event sourcing、多 server HA、
跨进程 Future 恢复不在本阶段。MCP JSON-RPC、HTTP console、GitHub webhook 外部协议不改。
不新增 Endpoint/Run/Context、通用 RPC 平台、依赖、消息去重表或自动传输重试。

前序合同：[执行所有权](worker-execution-lifecycle.md)、[Session](shared-communication-session.md)、
[Gateway 与最终关闭修复](shared-communication-gateway.md)。

### B. 所有权与不可改变的合同

双方复用同一 Gateway/Session 实现，worker 的 Gateway 只连接一个 server；Runtime 装配对称后置。

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
第一步已验证列表序列化、JSON schema 与 Mypy 类型窄化。空控制判别使用 `Literal[None]`；
改成普通 `None` 会使 schema 构造失败。不以 Any/cast 绕过类型约束。

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

**F–I 是第二步的迁移合同，尚未接入。** 第一步只提供协议与数据类型，不实现回调分派、
ACK sender、队列调度、业务消费循环或接纳/恢复流程；这些行为由接续线程实现和验证。

每个方向显式登记 `(type, method) → 一个 callback`，可直接使用现有实例绑定方法。
重复 key、key 与模型不符均拒绝；没有广播、每方法一个 Handler 类或第二张 peer 表。
第二步拟新增 `app/communication/dispatch.py`，只放注册/分派与薄类型适配：给定严格模型，
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

**处理顺序（单帧行为，不是额外实施阶段）：**

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

第一步已定义持久模型、纯 decoder、摘要与一致性校验；第二步接入实际读取、正常写入时的
惰性升级、ResultApplication/prove_reopen 与 source SQL。第一步未启用运行时新格式读写。
第二步必须删除 `state/execution.py` 内联的旧 ExecutionRecord / `_parse_journal`，接入
`app/worker/state/record.py` 的唯一纯模型，不保留双轨运行路径。

**结构版本：wire16 / journal5 / ResultInbox v1。** wire 严格只收 v16，旧持久数据在专用
durable 边界读取，不把网络 parser 放宽。未投入运行的新格式不因文档整理递增版本。

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
`app/contracts/receipts.py` 定义 ResultInbox、严格历史 decoder 与固定停止摘要；
`app/worker/state/record.py` 定义 ExecutionRecord v5 和只读 v4/v5 decoder。两者不拥有存储、
连接或 Task，不加泛化版本注册表。[S1][S8][S12]

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
  移除日期。生产状态尚未盘点，不能推断实际已存格式或数量。[S12][S13][S14]

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

实施只有以下两步，继续同一 feature record/bookmark/PR；准备和验证分别属于这两步。

| 步骤 | 负责线程与修改范围 | 完成边界与当前状态 |
| --- | --- | --- |
| 1. 协议结构和数据结构 | 当前线程：protocol.py / receipts.py 的帧、条目、方法、方向 parser、E/T/A/C/R/B、持久模型与纯 decoder/digest；record.py 的 ExecutionRecord v5；Job.result_payload 类型 | 结构已实现并定向验证、已发布 Draft PR；不切换运行时收发、存储读写或业务流程，未迁移调用方和全仓断点已记录 |
| 2. 对应实现迁移 | 接续线程：Session/dispatch/ACK/query、worker/server 收发与生命周期、Gateway 必要类型调整、持久化/SQL/结果恢复/reopen，以及受影响脚本、fixtures 和当前文档 | 待明确授权；全部调用方接入新结构，旧 wire 路径和旧内联记录移除；L 节整体验收与精确 code-bearing head review gate 通过后，按授权更新同一 PR |

第二步全链路验证属于迁移完成条件，不另列准备、测试或发布阶段。不用 Any/cast、跳过
测试或兼容别名掩盖断点；第一步从未改动基线保留的 terminal/stop/旧记录黄金样本继续使用。

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

本节列出两步验收标准；实际完成项见“第一步交付与验证”。不引用前三阶段或早期规划基线
测试作为本阶段通过证据，也不把咨询或文档检查算作产品验收。

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

### M. 升级、回退与交接授权

第一步的中间 head 不独立合并或部署；部署另需授权。wire16 与 v15 不在线互通，认证后首帧
版本不符拒绝，不自动降级。受控窗口先停止新分配、等待活跃执行结束与外部 effects 收敛；
普通执行 Task 无法靠 journal 跨进程升级。
server/worker 协调升级且考虑 recovery grace，不新增 timeout 配置。升级前备份并只读盘点
实际持久版本，未知数据先制定处理方案，不清空；已持久终态/停止回执由新版本恢复。
写入 v5 journal/v1 inbox 后不能直接运行旧二进制，旧 lifecycle SQL 也会误判新 control 来源。
默认向前修复；反向转换/effects 对账另行设计，不能用旧备份覆盖后续外部事实、删 journal
或伪造 ACK。不承诺无损滚动升级、一键降级。

第一步及提 PR 已按用户授权执行。**第二步仍需明确迁移授权**，覆盖 F–I、J 读写接入与
K–L 整体验收；获批后不另加逐模块审批。已发布 PR、咨询通过或文档核验均不构成第二步授权。
若迁移需要改变字段/方法/身份或业务语义、放宽历史格式、增加存储列或框架，先修订方案再审批。
私有函数位置、测试屏障与类型签名在第二步落实；合并和部署未获授权。

[接续线程](https://ampcode.com/threads/T-01a1a199-8e0a-7690-9c0e-3b4a4bf22852)负责第二步，
目前只读记录，未启动实现。它取得授权及唯一写入权后，核对精确 head、基线、黄金样本、
验证与未迁移调用方，在同一 workspace/bookmark/PR 接续；不与当前线程并发写入，不另建
feature record。生产数据盘点、维护窗口属于部署前待核实项，不在本次文档重写中执行。

## 第一步交付与验证

以下是已推送结构 head 的执行记录，本次文档重写没有重跑产品测试或改变这些结果。
详细输出见[交付审阅记录](https://ampcode.com/artifacts/0a34e8c0-f8b8-49ec-8853-0d40d6fcf15d)及
[当前线程](https://ampcode.com/threads/T-01a19069-3d51-72de-a60f-ea62cb7a4728)。

**已实现：** protocol.py 的 wire16 控制/条目联合、严格 parser、稳定 ID；receipts.py 的
inbox v1、受限历史 decoder、固定停止摘要；record.py 的纯 ExecutionRecord v5；Job JSON
类型及对应合同测试、changelog。`Message[Payload]` 仅复用共同头与校验，不是运行时 RPC 框架。
未改 Gateway/Session/WorkerExecution 运行时，未启用新格式 IO 或生产数据转换。

| 验证范围 | 已观察结果 |
| --- | --- |
| 纯合同 | Python 3.12/3.13 的定向测试通过；typed 构造、序列化、Mypy 窄化、双向 JSON schema 探针通过 |
| 静态检查与构建 | 改动范围 Mypy、全仓 Ruff、变更格式、console 与包构建、wheel 内容检查通过；不代表服务能启动 |
| 黄金样本 | 未改动基线的 Failed（Unicode/转义）、Initial、Revision、Archive 经 v14/v15 解码与 v5/inbox1 往返，terminal/stop 摘要不变 |
| 完整 pytest / Mypy | 失败；前者在 collection 仍导入旧 ReplayResult/ResumeAssignment，后者仍有旧字段、构造器与 wire 联合 |
| 真实 PostgreSQL / 连接恢复 / 运行时故障消融 | 本步未执行；属于第二步验收 |
| 精确 head review gate | critical-only gate 通过；仍有下述 LOW，不能据此宣称整体 ready |

可复现的定向检查：
- `uv run pytest --noconftest tests/test_protocol.py tests/test_receipts.py -q`
- `uv run mypy app/contracts/protocol.py app/contracts/receipts.py app/worker/state/record.py app/domain/models.py tests/test_protocol.py tests/test_receipts.py`
- `uv run ruff check app tests`、变更文件 `ruff format --check`
- `uv run python scripts/build_console.py`、`uv build --no-sources`

`--noconftest` 只隔离纯合同测试与导入旧运行时的全局 fixture；完整 pytest 已单独执行并报告
失败。没有删除、skip 集成测试、放宽 schema 或修改配置来掩盖断点。

**结构故障消融：** 移除 ACK 配对、重复 ID 拒绝或已知方法优先判别，随机化稳定 ID，把 wire
身份写入终态摘要，放宽历史版本，改变 stop 摘要版本，移除 v4 decoder，最终均被捕获。
首次放宽版本的变体存活，原因是 journal mixed-version 检查掩盖了单独 inbox 的覆盖缺口；
补充 inbox 未知版本输入后捕获，未放宽实现。消融与类型探针留 scratch。

**遗留 LOW：** 永久 decoder 正向测试目前只覆盖 Failed；成功 Initial/Revision/Archive
已做 scratch 黄金验证，尚未成为永久回归。审查中拒绝全部成功旧回执的变体仍可通过定向
测试。该项已报告，未自行修复；本次改文档不作为测试返工授权。

**未迁移调用方：** `controler/workers/{transport,handler,conversation}.py`、
`controler/workflow/{coordinator,tools}.py`、`worker/{transport/client,execution/lifecycle,state/execution}.py`、
`persistence/{workers,stops}.py` 及对应测试；还必须接入 Session 分派、ResultApplication、
JobStore 来源 SQL、tool ledger、后台恢复、prove_reopen 和相关脚本，不能只修 Mypy 报错。
精确清单、黄金样本与失败日志保存在独立 workspace 的 `.amp/in/artifacts/`，不入库。

**PR-head 最终 CI：** [CI](https://github.com/b1indsight/agentflow/actions/runs/41657515308) 中
Frontend、Ruff、Python Build 通过，pytest/Mypy 失败；[Kubernetes render](https://github.com/b1indsight/agentflow/actions/runs/41657516752)
通过；[recovery](https://github.com/b1indsight/agentflow/actions/runs/41657515383) 失败；
[Images](https://github.com/b1indsight/agentflow/actions/runs/41657516005) 的 build 失败、publish 跳过。
Images 日志只展示 serve 健康检查失败，没有容器内部 traceback；本地同 head 能复现旧
ReplayResult 导入错误，不能断言 CI 容器内就是同一异常。当前 PR 保持 Draft，未合并或部署。

## 与来源线程对照

依据[来源线程](https://ampcode.com/threads/T-01a16fd3-4a30-7198-9401-c27c98620c27)的关键决策与
最新交付记录核对。下列消息编号为读取工具返回的位置；跨线程报告与直接人类要求分开列明。
本次核查不声称穷尽来源线程的每条人类消息；本地直接用户决策也不能因来源只收到转述而失效。

| 对照项 | 来源与本地决定 | 当前方案结论 |
| --- | --- | --- |
| 总目标 | 来源 #1 直接要求统一全部 worker/server 消息，为 event-based 打基础 | A/D 覆盖 19 种原消息；可靠领域事件/outbox 后置，符合 |
| 通信所有权 | 来源 #56 直接提出双方 Gateway/Session、worker 单 server；#193/#199/#205 报告删去 Endpoint/Run 等包装并后置 Runtime 完全对称 | B 保留共用通信与各自业务 owner；F 不增加执行/上下文容器，符合 |
| 关闭与安全 | 来源 #223/#249 记录第三阶段窄化 close 修复及合并；第四阶段交接要求保留 Started、恢复、原连接回复与提交/停止条件 | B/F/H/I/L 全部保留；不把 WebSocketDisconnect 以外错误吞掉，符合 |
| 最新协议方向 | 本地用户直接选择控制与 payload 同帧、bool ack + ack_id、列表 call/result、reply_to、type/method 路由；来源 #274–#321 收到相应演进报告 | C–I 采用这些决定；四分类、互斥帧和旧关联名不再作为候选，符合 |
| 两步与授权 | 本地用户要求“当前plan修改协议结构和数据结构，新开thread做对应的实现迁移”，随后要求“做当前plan，提pr”；来源 #325/#327/#333 记录范围及交付 | K 只有两步；第一步已交付，第二步待明确授权，符合 |
| 实际完成边界 | 来源 #331/#333/#335 记录结构交付，#341–#344 记录最终 CI 与未迁移状态 | 本文保留 CI 失败、LOW 和不可运行/合并/部署边界，不写成第四阶段整体完成 |

对照未发现现行设计与上述关键要求冲突。修正的是旧文档中的过期状态与叠加呈现，
不因此改变协议结构、扩张本阶段范围，或把未来迁移合同当作已完成行为。

## 决策记录与历史

初案与 R1–R6 的完整原文、理由和取舍保存在
[固定提交的历史区](https://github.com/b1indsight/agentflow/blob/90bc9167ad57016e22c09482ca5665a40f893ba1/docs/plan/unified-message-protocol.md#L572-L2066)。
本次按用户要求将正文重写为唯一现行版本；历史只供追溯，不要求读者再逐段合并方案。

### 2026-11-03：两步范围

用户将原七步顺序收敛为协议/数据结构与实现迁移两步。F–I 归接续线程，J 分为纯结构和
实际读写接入；保留全部恢复、接纳、旧状态合同。不保留七个子阶段，也不把第一步做成独立
可部署版本。早期四分类、控制/业务互斥及关联字段备选均由当前 C–I 取代。

### 2026-11-03：结构实施

C–E/J 结构落地；纯 record 模块与 IO 分离，使结构可验证而不提前改变 Task/锁/ACK 所有权。
运行时留在第二步，故完整回归与部分 CI 明确失败。实际交付、故障消融和遗留 LOW 见上节，
不将构建通过或 review gate 通过等同于可运行。

### 2026-11-03：现行方案重写与来源核对

移除正文内嵌的历史草案，改为固定提交引用；更新结构已交付、迁移待授权、PR/CI 与遗留项。
保留现行字段、方法、身份、时序、限额、摘要、旧数据和验收合同，新增来源对照。
使用本机 Opus 5.5 只读咨询，核验初始化/响应模型、空 tools/MCP、success 与 exit 0。
未采用草稿把多轮候选混称为“初案决定”的历史归纳；以原文链接保留决策来由。
本次仅重写文档，不修改实现/测试，不启动迁移；咨询和文档检查不计为产品验证。

## 源码依据（固定实施前基线）

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
