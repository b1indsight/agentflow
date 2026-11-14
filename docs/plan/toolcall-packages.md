# ToolCall 工具包收敛 — Feature record

**Stage:** Implementation，按时间线 002 的直接实现方案，本地未提交；初稿已退回，见时间线 001。
**PR:** 未创建，未提交或推送。

## 1. Initial plan

### Summary and motivation

将固定协议收敛为按接收方划分的两个特殊 ToolCall 工具包：`server_calls` 和 `worker_calls`。每个命令的请求模型、响应模型和通信策略在工具包中声明一次；解析器和连接分发器读取同一份声明。新增命令只增加本地定义、实际 handler 和调用代码，不再追加全局集合或修改 `on_rejected` 中的命令分类。

这里的“工具包”指两个协议命名空间，分别落在 `app/contracts/server_calls.py`、`app/contracts/worker_calls.py` 两个模块中。按接收方划分是本方案的选择，尚未获得用户批准。

当前固定命令已经继承 `ToolCall`。重复维护的内容包括 `_FIXED_METHODS`、四组方向联合、注册时的方法字符串、发送侧响应模型选择，以及心跳、进度、迟到响应、拒绝处理中的方法或类型名单。实际请求按 `method` 分发，响应按 `reply_to`、原方法和响应模型返回原调用方，这个关联方式保留。[模型与方向联合](../../app/contracts/protocol.py#L309-L631)、[Dispatcher](../../app/communication/dispatch.py#L48-L144)、[server 注册与发送](../../app/controler/workers/callbacks.py#L152-L194)、[发送侧分类](../../app/controler/workers/callbacks.py#L793-L828)。

### Goals, scope, and success criteria

- 删除手工维护的 `_FIXED_METHODS` 和方向请求/响应联合；目录从命令声明派生。
- 删除因新增命令而必须扩充的心跳身份、通信进度、响应配对、迟到响应和拒绝处理名单。真实业务处理仍需编写和挂载，不使用反射调用远端指定的 Python 名字。
- 新增一个临时 `worker.echo` 查询时，仅定义模型与命令、挂载 handler、编写调用；方向解析、非法参数、非法响应、超时清理无需修改通用层。
- 保留原调用方的 Task/Future、持久 journal、租约、取消和重放所有权；普通命令不强制携带 Flow、job 或 Git 上下文。
- 纳入当前基线已有的 `worker.file_read` 和 `server.file_register`，不从旧工作副本覆盖它们。
- 不引入插件扫描、每个命令一个模块、通用重试框架、数据库迁移或 UI 改动。Flow 工具继续保留业务授权和类型化错误。

基线是已 fetch 的 `main@origin`，提交 [53e7cafe](https://github.com/b1indsight/agentflow/commit/53e7cafe469572f14d5e32a857909857173bc403)，wire version 25。工作位置是独立 jj workspace `.amp/in/workspaces/toolcall-packages`；根工作副本中的其他改动不纳入本功能。

### Proposed behavior

#### 开发体验

当前需要在模型之外更新多处通信分类。本方案将这些分类拆成“命令定义的静态事实”和“本连接绑定的业务回调”。以下是拟议 API 示例，并非已实现的代码：

```python
# app/contracts/worker_calls.py
class EchoCall(ToolCall):
    method: Literal["worker.echo"]
    text: str = Field(max_length=1024)

class EchoResult(ToolResult):
    method: Literal["worker.echo"]
    text: str

ECHO = WORKER.define(EchoCall, EchoResult, traffic="query")

# worker 的现有 callbacks 所有者
routes.handle(ECHO, self.on_echo)

# server 调用方：调用的参数仍由 EchoCall 严格校验
reply, incoming = await request_reply(
    registration, ECHO, call, message, timeout=timeout,
)
```

`on_echo` 的签名是 `async def on_echo(self, call: EchoCall, message: ServerMessageUnion) -> None`，通过现有 Session 发送 `EchoResult`。示例不成为生产命令，批准后用临时扩展验证实际开发体验。

#### 错误与兼容性

- 已知方法只能使用其声明的模型；非法参数或错误方向不能退回 Flow 通用模型。未知工具名继续走现有 Flow `INVALID_REQUEST` 路径。
- 整条消息的模型和头部授权通过之后，才能通知 Future、发送错误、写 journal 或产生控制副作用。畸形错误对象不触发另一个错误响应。[输入边界](../../app/contracts/validation.py#L1-L89)。
- 将“对端拒绝我发送的项”和“对端发回畸形响应”分开绑定。Assignment 被拒绝可能意味着从未启动；畸形 ExecuteResult 需要恢复确认。Flow 畸形响应仍保留远端副作用结果未知的语义，不能一律转换为安全重试。
- 临时查询超时后撤销关联；断连只结束选择了断连回调的调用方。持久协议在业务接纳后撤销关联，跨连接恢复仍由原业务所有者确认。
- 推理专用端点只接纳已挂载的 infer 请求；未挂载请求和游离响应继续失败，不能因全局目录存在某种查询就自动放行。[专用端点](../../app/inference/local.py#L55-L150)。
- JSON 字段、方法名、校验约束、稳定 E/T/A/C/R/B ID 与 journal 内容保持不变；计划保持 `PROTOCOL_VERSION = 25`。若实作发现必须改变 wire 合同，先补充方案，不顺带升级。

**唯一拟议行为对齐：** server 收到畸形 `worker.file_read` 结果时，按照其他临时查询处理。有原调用时结束文件读取并报告 `invalid_response`；迟到时只拒绝该 payload，不授予业务接纳。当前 server 的 `on_rejected` 查询名单缺少该方法，静态阅读显示可能落入连接级错误；批准后先复现并记录实际差异，不能把此推断当作已验证的 bug。[现有拒绝路径](../../app/controler/workers/callbacks.py#L703-L755)、[文件读取调用方](../../app/controler/workers/files.py#L40-L80)。其他端点的未知/迟到畸形响应限制保持原样。

### Technical design and affected modules

#### 模块与所有权

- 新增 `app/contracts/toolcall.py`：严格基础模型、ToolCall/ToolResult、公共 validation error、message ID、call/result header，以及下述命令描述与 schema 构建机制。它不导入执行端或控制端。
- 新增 `app/contracts/execution.py`：从 `protocol.py` 原样迁移两个工具包共享的任务、执行结果、执行身份、停止身份和终态摘要合同，解决工具包反向导入消息层造成的循环依赖。密钥序列化、可选字段排除规则和摘要输入保持原样。
- 新增 `app/contracts/worker_calls.py`：`worker.execute`、`stop`、`history`、`file_read`、`infer`、`provision`、`reject_recovery` 的 call/result 模型、专用参数和定义对象。
- 新增 `app/contracts/server_calls.py`：`server.execution_started`、`accept_result`、`accept_stop`、`file_register` 的 call/result 模型、专用参数和定义对象。
- `app/contracts/protocol.py`：保留消息 envelope、control、方向解析、稳定 ID；Flow 模型和通用 Flow 定义也保留在这里。导入两个固定包后冻结目录，再构造消息 schema。不保留需要随新命令扩充的旧模型导出清单；同仓库消费者直接迁移到模型的新归属。
- `app/communication/dispatch.py`、`requests.py`：统一 typed 绑定、逐次调用关联、发送项拒绝关联、拒绝预检和临时查询封装，不持有业务 Future 或 journal。
- `app/controler/workers/{callbacks,flow,files}.py`、`app/worker/transport/{state,business,files}.py`、`app/inference/{transport,local}.py`、`app/worker/runtime.py`：在原所有者处绑定命令、重放和失败回调。数据库、文件权限、执行恢复、Started 和停止协调逻辑留在原所有者。
- 调整受模型移动影响的其余导入和既有测试；不顺带整理其他模块。

#### 核心数据结构

以下类型均为本地 Python 合同，不新增 wire 字段。

`Method[C: ToolCall, R: ToolResult]` 是不可变、带类型参数的描述对象：

| 字段 | 类型 | 意义 |
| --- | --- | --- |
| `name` | `str \| None` | 固定命令从 call 的单值 Literal 推导；Flow 族未绑定具体名字时为 None |
| `receiver` | `Literal["server", "worker"]` | 固定包赋值，用于派生方向 schema；Flow 接收方为 server |
| `call` | `type[C]` | 严格请求模型 |
| `result` | `type[R] \| None` | 严格响应模型；None 表示单向通知 |
| `traffic` | `Literal["execution", "query", "capacity"]` | 保留现有进度与迟到处理差异；不代表通用超时、重试或持久化策略 |

`MethodSet` 持有 `receiver: Literal["server", "worker"]`、`_methods: dict[str, Method[ToolCall, ToolResult]]`、`_frozen: bool`。`define()` 就是登记位置，不再添加到第二份集合。冻结后不可变，重复名字、模型 Literal 不匹配、前缀错误在启动时失败。使用不可变描述和泛型保持定义与 handler/result 的类型对应，不使用 Any 或强制 cast 掩盖类型问题。

`CallRef` 持有 `message_id: MessageId`、`method: str`。恢复时可以重建调用关联，无需伪造缺少 arguments 的完整请求模型。

`PayloadSchema` 持有 `sender: Literal["server", "worker"]`、`methods: tuple[Method[ToolCall, ToolResult], ...]`、`flow: Method[ToolCall, ToolResult]`。flow 字段由 protocol 层传入具体 Flow 定义，基础模块不反向导入 Flow 模型。通过 Pydantic 支持的 schema hook 生成完整具体模型的校验与序列化 schema；消息 payload 的静态基础类型仍为 ToolCall/ToolResult。不采用运行时宽松 dict 解析或仅在 TYPE_CHECKING 下伪装的模型联合。

`RejectionPlan` 是整批纯预检结果：`items: tuple[ToolCall | ToolResult, ...]` 为待拒绝的可信头部，`callbacks: tuple[Callable[[], Awaitable[None]], ...]` 为已授权的调用方通知，`recover: bool` 标记所有者需要恢复。它不包含未验证参数，不在构造时运行回调。某项无权拒绝时，在返回计划之前整批失败。

#### 拟议接口

下面的 `FailureHook` 表示 `Callable[[Incoming | RejectedMessage], Awaitable[None]]`，`Invalid` 表示 `Literal["reply", "recover", "fatal"]`，均用于本地通信层。原有 configure/freeze/prepare 的生命周期保留。

```python
# contracts/toolcall.py
Method.ref(self, message_id: MessageId) -> CallRef
Method.named(self, name: str) -> Method[C, R]  # 仅绑定动态 Flow 名字
MethodSet.__init__(self, receiver: Literal["server", "worker"]) -> None
MethodSet.define[C: ToolCall, R: ToolResult](
    self, call: type[C], result: type[R] | None = None, *,
    traffic: Literal["execution", "query", "capacity"] = "execution",
) -> Method[C, R]
MethodSet.freeze(self) -> tuple[Method[ToolCall, ToolResult], ...]
PayloadSchema.__get_pydantic_core_schema__(
    self, source_type: object, handler: GetCoreSchemaHandler,
) -> CoreSchema
ToolCall.execution_identity(self) -> tuple[int, str] | None
ToolResult.execution_identity(self) -> tuple[int, str] | None

# communication/dispatch.py，类型参数沿用外层 Dispatcher[Incoming]
Dispatcher.handle[C: ToolCall, R: ToolResult](
    self, method: Method[C, R], callback: Callable[[C, Incoming], Awaitable[None]], *,
    invalid: Invalid = "reply",
) -> None
Dispatcher.replay[C: ToolCall, R: ToolResult](
    self, method: Method[C, R], callback: Callable[[R, Incoming], Awaitable[None]], *,
    invalid: Invalid = "fatal",
) -> None
Dispatcher.expect[C: ToolCall, R: ToolResult](
    self, method: Method[C, R], call: C | CallRef,
    deliver: Callable[[R, Incoming], Awaitable[None]], *,
    refused: FailureHook, invalid: FailureHook,
    on_reply: Callable[[R, Incoming], Awaitable[None]] | None = None,
    on_closed: Callable[[], None] | None = None,
) -> None
Dispatcher.track(self, item: ToolCall | ToolResult, *, refused: FailureHook) -> None
Dispatcher.refuse(self, error: ValidationErrorResult, message: Incoming) -> Awaitable[bool]
Dispatcher.plan_rejection(self, message: RejectedMessage) -> RejectionPlan
Dispatcher.forget(self, message_id: MessageId) -> None
Dispatcher.clear(self) -> None
RejectionPlan.errors(self) -> list[ValidationErrorResult]

# communication/requests.py
async def request_reply[Incoming: Message, Outgoing: Message, C: ToolCall, R: ToolResult](
    registration: Registration[Incoming, Outgoing], method: Method[C, R], call: C,
    message: Outgoing, *, timeout: float,
) -> tuple[R, Incoming]
```

现有 `Dispatcher.configure()` 增加必填参数 `reject_unmatched: Callable[[ToolCall | ToolResult], Invalid]`，其余参数保留。它只对没有逐次关联或显式绑定策略的畸形项进行只读授权；所有者可以查目录的 traffic，但不增添方法名或模型名单。

`expect()` 拒绝无 result 的定义，并验证 CallRef 的方法。`track()` 也覆盖已发送结果和单向通知，解决 Started、终态、停止结果或接纳回执被对端拒绝时的关联；错误响应本身不进入此表。清理与现有 `_sent` 淘汰、协议接纳和连接关闭同步，不能无限保留。

schema 加载只依赖 contracts/domain；连接的 `bind()` 不参与 schema 发现。`WorkerPayload` 从 server 包的请求、worker 包的响应及现有 Flow 请求派生；`ServerPayload` 反向派生，并包含 Flow 响应。公共 validation error 单独判别。全局已知名也从这两个包派生，已知但方向不符的方法不会被当成未知 Flow。

`execution_identity()` 默认返回 None；拥有编码执行身份的具体模型在自己的定义处覆写，替换 Heartbeat 和 `_item_job` 的类型名单。推理、独立查询和 provisioning 维持现有身份语义。Started 的业务栅栏、ACK 内容相等检查等单一业务判断保留，不把业务状态机搬进通用目录。

#### 各类通信的绑定与边界

| 类别 | 静态定义 | 接收与调用绑定 |
| --- | --- | --- |
| execute | execution | 非法 Assignment 仅拒绝；原调用被拒绝走现有未启动栅栏，畸形 ExecuteResult 和跨连接重放走所有者恢复验证 |
| stop、reject_recovery、Started、接纳回执 | execution | 需要恢复的请求在 handle 处声明；发送时绑定拒绝回调；持久响应通过 replay 挂载，重复接纳仍验证已有记录 |
| history、file_read、file_register、infer | query | 不证明编码进度；调用方分别保留 Future、推理槽位或文件操作的失败收尾；临时查询保持 timeout/finally 清理 |
| provision | capacity | 保留当前会证明接收进度、可触发停止协调、但迟到结果丢弃的行为，不合并为普通查询 |
| Flow | execution | 相同描述和绑定 API，继续校验 active assignment、flow_id、call_id、停止状态与业务权限 |

有效迟到响应先查逐次关联，再查显式 replay，最后交给端点所有者。原生端点根据 traffic 统一丢弃允许的查询结果；infer-only 端点继续严格失败。不能在 Dispatcher 内无条件丢弃全局目录中的查询响应。

`on_rejected` 只保留端点的 ready/epoch/control 授权、纯预检、执行计划、发送整批 validation error，以及按计划恢复。命令分类从 handle/replay/expect 的绑定和目录读取。未匹配项的授权沿用端点边界：server 的临时查询规则、worker 必须具备原响应关联的规则、infer-only 必须挂载请求的规则各自保留，用端点级默认策略表达，不按命令名追加集合。预检需要的端点规则在 configure 时绑定，保持同步、只读，不调用业务处理器。

`on_error` 在 epoch 授权后按 method 与被拒绝项 message_id 查找绑定，调用原所有者回调。Assignment、inference、provision、Flow 的失败收尾分别放回它们的发送/关联位置。`_send` 不再通过 isinstance 选择响应模型；各调用点先用命令定义绑定，再调用普通发送。发送失败撤销新建关联，保留已有重放项的恢复语义。

### Alternatives and rationale

- 只包装固定名集合：diff 更小，但方向联合、错误处理和响应配对仍会漂移，不能满足新增命令无需改列表的目标。
- 把固定协议全部变成普通 Flow 工具：会让心跳、恢复、独立推理或文件查询依赖 Flow/job/Git 授权，且无法统一临时与持久调用的生命周期，未采用。
- 每命令一个文件并自动发现：需要维护导入列表或引入包扫描。当前采用两个固定模块，定义与登记同处，启动路径确定。
- 所有响应按 method 分发：会丢失原调用的结果模型、超时和权限关联，未采用。
- 所有查询在通用层直接丢弃迟到结果：会扩大 infer-only 的接收范围，未采用。

### Costs, risks, and mitigations

- 动态 schema 与静态类型的配合是主要技术风险。先实现目录到 Pydantic schema 的最小垂直切片，同时跑序列化、JSON schema 和 mypy；不通过时调整本层设计，不引入 Any 或放宽远端校验。
- 模型迁移有导入冲突风险。基础 ToolCall 与执行数据先分离，固定包只依赖它们，最后消息层统一 bootstrap；运行现有新进程导入边界测试。
- 失败回调绑定有遗漏风险。覆盖发送的 request、result 和单向通知，以及恢复时重建的关联；逐条检查释放位置和重复响应。
- 保留三类 traffic 和两种拒绝来源增加少量描述数据，它们对应现有可观察差异。验证阶段通过消融确认这些差异确实需要，删除不能证明价值的机制。
- 本方案假设两个工具包按接收方划分；若用户期望其他边界，需在实施前修改方案。它不扩展远端可调用的 Python 范围。

### Implementation order

1. 批准后先对 unchanged 基线补充临时表征：方向解析、混合拒绝批次、provision 进度、文件畸形响应、infer-only 限制和跨连接重放。确认文件路径差异之后再改行为。
2. 迁移基础/执行合同和两个固定包，实现命令定义、冻结与派生 schema；保持 wire、终态摘要和严格校验语料一致。
3. 修改 Dispatcher 与 request_reply，保留逐次响应关联，加入 typed handler/replay 和发送项拒绝关联；按完整批次预检错误。
4. 迁移 worker、inference、文件与 Flow callbacks，再迁移 server callbacks 和各发送点；每个协议所有者同时迁移绑定与清理，不创建第二套兼容路径。
5. 删除旧集合、联合和分类分支，执行新增命令演练、消融、边界/集成检查；更新本记录的时间线与证据。

这是同一功能的行为保持迁移及一项明确的文件查询对齐。无数据迁移；回退为整体恢复该功能的代码，数据库/journal 无需回退。提交、推送、PR 和部署均待相应授权，不在当前规划阶段执行。

### Validation and ablation strategy

**已执行的基线检查：** 在独立 workspace 上运行现有 `tests/test_protocol.py`、`test_session_events.py`、`test_communication_session.py`、`test_communication_gateway.py`、`test_boundaries.py`，结果通过。尚未运行本功能实现、PostgreSQL 集成或全量测试。

**批准后验证：**

- 对现有合法/非法 JSON 语料逐方向比较 parse、dump、JSON schema 的实际字段与判别行为；测试已知方法畸形、已知方法错误方向、未知 Flow 名、畸形 error 对象。
- 在协议与通信现有测试中保留少量合同回归：整批非法时无 ACK/lease/journal/tool 副作用；后项未授权时前项 Future 也不能先完成；错误 method/reply_to 不得命中原调用。
- 在原生 worker 与 infer-only 两种端点验证缺失挂载、有效/畸形迟到响应、超时、EOF、发送失败、关闭与回调清理。回调失败不可被吞掉。
- 验证拒绝 Assignment 与畸形执行结果的不同收尾，Started-before-launch、停止恢复、重复 ACK、稳定 ID、终态摘要和跨连接 replay。
- 临时加入真实 `worker.echo` 定义、handler 和调用，经过生产解析器及内存连接验证成功、非法参数、非法响应和超时。确认没有改动任何全局列表或拒绝处理代码；演练结束删去生产示例，必要合同测试保留在既有测试文件。
- 运行 `uv run ruff check app tests`、`uv run mypy app tests`、现有 backend pytest。数据库相关测试使用本地隔离的测试库；若环境不可用，明确报告缺失覆盖，不以跳过伪装通过。UI 未变，不要求 UI 截图。
- 用检索和 diff 确认 `_FIXED_METHODS` 与手写方向联合已删除，通信分类中没有替代性的命令名/模型名单。保留的具体业务分支需能说明它处理的业务约束。

**消融在实现后、自动代码审查前进行：**

- 将 capacity 简化为 query，同一组 provision 进度/停止协调测试应能发现行为差异；若不能，继续调查或删除无依据的分类。
- 去掉整批预检后再跑“前项可拒绝、后项未授权”的混合批次，测试必须发现 Future 提前结束或副作用。保留预检机制。
- 合并 refused 与 invalid、移除 replay、移除执行身份覆写，分别用未启动 Assignment/畸形 ExecuteResult、跨连接停止/结果、非对称身份心跳检查区分正确行为。测试无法区分时改进输入或简化设计。
- 用新增 echo 演练检验最重要的结构目标；如果仍需追加集合，此方案尚未完成，不能以减少名单数量作为验收。

### Open questions and deferred work

- **审批事项：** 是否同意按接收方划分两个固定工具包、采用上述统一描述/绑定机制，以及将 server 的畸形 file_read 响应与临时查询策略对齐？批准前不编写产品代码或测试。
- **实施中解决：** Pydantic schema hook 的最简构造和回调记录的内部存储细节，通过垂直切片、mypy 和现有合同测试确定；不改变上述公开合同和端点授权边界。若需要改变本方案的核心类型或行为，先记录补充方案。
- **不纳入本功能：** 持久调用的统一重试调度、流式 tool result、外部插件自动发现、更多特殊包、改变 provision 的进度语义及其他顺手修复。

## 2. PR change timeline

### [001] — 2026-11-14 — 实际复杂度简化不足，退回设计

- **Context / requirement update:** 用户在查看 `worker.history` 的迁移示例后指出“没有实际复杂度简化”。原目标仍是两个特殊 ToolCall 包，并移除新增命令时的集合追加与硬编码分类；用户没有批准初稿。
- **Decision / status:** 撤回初稿作为推荐实施方案的地位。它收敛了名单维护，却新增 Method、MethodSet、CallRef、PayloadSchema、RejectionPlan 和策略配置，尚未证明对现有通信路径有净简化。上面的 Initial plan 保留为原始提案，不再请求照此实施。
- **Options considered:** 重新比较直接复用现有 Dispatcher 的最小收敛，以及统一 ToolCall/ToolResult 外层格式后删除每命令通信包装。保持 wire v25 是初稿自行选择的限制，用户没有提出此约束；改变协议的兼容性成本仍需明确。
- **Proposed direction:** 倾向通过统一工具调用的外层格式，删除每命令的 Call/Result 通信包装类型；history 只保留严格业务参数、HistoryPage 和读取函数。公共通信层负责请求/响应封装、发送和按原调用关联，handler 返回业务结果，调用方不再构造 ServerMessage 或重复指定响应模型。保留现有 wire 也可减少手工发包代码，但不能把这些局部简化夸大成已删除所有协议机制。这一方向尚非获批实施方案。
- **Consequences:** 统一格式需要协议升级，处理旧 worker、在途调用和已持久化结果；不能默认增加长期兼容层或自动排空执行。参数/响应严格校验、整批无副作用拒绝、执行归属、错误来源区别与持久恢复安全性继续保留。未采用“其余迟到响应一律丢弃”的简化，它会放宽 infer-only 和 Flow 等端点的现有边界。
- **Evidence / validation:** 已对照协议模型、history 两端、Dispatcher、输入验证和 Session 入队路径。另作一次受限 Claude Code 设计咨询，初始化与响应模型均为 `claude-opus-5-5`，工具和 MCP 列表为空，正常结束；其中把统一 wire 当作删除类型名单的必要条件、或建议无条件丢弃非重放响应的结论未采纳。咨询与静态阅读不构成运行时简化或正确性的证明。
- **Implemented changes:** 仅更新方案状态与本条记录，没有修改产品代码或测试。
- **Outcome / pending:** 后续提案必须列出能实际删除的类型、函数和处理路径，以及仍需保留的业务状态机；新增抽象数量和注册点减少不能单独作为简化证据。新方案与协议兼容性选择尚未批准。

### [002] — 2026-11-14 — 不新增注册框架，直接删除名单

- **Context / requirement update:** 用户重新给出原需求，并约束“不要动 routes.register 的api”。001 退回的 Method/MethodSet、策略配置和 wire 升级不再作为基础。
- **Decision / status:** 已实现，尚未获批。两个特殊包就是 `worker.*` 与 `server.*`：模型的 `method` 为该前缀的单值 Literal 即属于该包。方向联合与 `_FIXED_METHODS` 在 `protocol.py` 中由模型派生。`routes.register`、`request_reply` 签名和 wire v25 不变。
- **Implemented changes:**
  - `protocol.py`：删除手写 `_FIXED_METHODS` 和四个方向联合，改由 `_package()` 派生；静态类型为 `ToolCall | ToolResult`，调用方仍用 isinstance 收窄。`ToolCall/ToolResult.execution()` 默认返回 None，只有携带编码执行身份的八个模型覆写。Heartbeat 改用它判断身份。
  - server `callbacks.py`：`on_message` 活性、`before_item`、`on_unmatched_reply` 改用 `execution()`，删除 `_item_job` 和四处类型元组。`_send` 不再按类型选择响应模型，由发送处 `expect_reply`。infer 的失败收尾移入其 `on_rejected`，删除 `on_error` 与 `on_rejected` 中的 infer 特判。
  - 两端 `on_rejected`：只保留连接授权，把畸形响应交给原调用方的 `reject_reply`，再整批回 validation error。删除方法名集合和接收方 critical 判断；持久项的恢复由发送方 `on_error` 负责，现有逻辑已覆盖 Started、终态、停止、接纳回执、stop 和 reject_recovery。
  - `Dispatcher`：`on_rejected` 回调与 `reject_reply` 改为 async，删除已无调用方的 `reply_model`。
- **Behavior changes:**
  - 接收方收到畸形持久项时，只回 validation error，不再主动断开；由发送方要求恢复后断开。
  - 迟到或无关联的畸形响应只回 validation error，不再断开。server 端的 `worker.file_read` 因此与其他查询一致。
  - provision 结果不再刷新 coding 接收期限，也不在 `before_item` 触发停止，停止仍由 `poll` 推进。
  - 已知包中的未知方法仍走 Flow 判别路径，与原行为一致。
- **Evidence / validation:**
  - `uv run ruff check app tests`、`uv run mypy app tests` 通过。
  - 本地 PostgreSQL 上运行全量 pytest。
  - 新增 infer 畸形响应和被拒请求的回归测试，移除 infer `on_rejected` 后两例失败。
  - 临时加入 `worker.echo` 模型，未改任何名单即通过两个方向的解析、Heartbeat 和错误方向拒绝，演练后已删除。
- **Outcome / pending:** 待用户审阅行为变化与实现；提交、推送和 PR 未执行。

