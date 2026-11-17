# Worker 全量类关系图

> 核对范围：2026-11-17，`4849e88` 加当前工作树的四个 worker 专用类归属迁移。
> `app/worker/**/*.py` 的全部 **48 个 AST `ClassDef`**，包括私有类、Protocol、模型、dataclass 和异常。
> 递归核对也包含枚举及嵌套类；当前 worker 内未定义枚举或嵌套类。不含测试替身和包外同名概念。
> 源码链接使用仓库相对路径；文档与源码应一起检出到同一版本。

[打开交互图](worker-classes.html)：直接用浏览器打开，无需构建、联网或安装依赖。
保留下拉定位、点击/键盘选择、一跳关系高亮、侧栏跳转、缩放和适应宽度；可用“导出 SVG”同步静态图。
画布高度最多占视口的 80%，放大后可在画布内横纵滚动；窄屏同步缩放图的最小宽度，页面本身不被撑宽。
图本身可单独离线使用；源码链接使用相对路径，需保留仓库目录结构，行号同时显示在侧栏。
GitHub 文件页不执行 HTML；下方静态 SVG 可直接预览、放大。

![Worker 全部 48 个类，按进程根、连接回调、执行所有权、业务与 VCS、持久证据、目录池、Agent、文件及共享 start 边界分组](worker-classes.svg)

## 范围与关系含义

图中共 **64 个节点、131 条关系**：48 个 worker 类、14 个灰色包外边界类、2 个灰色函数锚点。
关系分为 40 条持有/字段、73 条使用、12 条显式继承、6 条结构化 Protocol 满足。
`nodes` / `edges` 的手工定义和布局位于 [HTML](worker-classes.html)。

- **实线箭头**：对象持有引用或模型字段。共享引用不表示独占所有权：每个 Flow 的
  `WorkerExecution.state` 和 `WorkerExecutor.state` 是同一个 `ExecutionState`；所有 Flow 共用目录池和配置后端的 Agent。
  同后端编码、推理、历史与文件操作共用一个 Agent；推理最终答案预算按调用传入，不修改共享实例的默认预算。
- **虚线箭头**：创建、临时参数、函数/静态方法调用、持久模型读写或异常抛出/捕获。
  侧栏逐条注明；间接调用会标出中间函数或 Runtime，避免把 import 画成持有关系。
- **实线空心三角**：显式 Python 继承，指向基类。worker 内有
  `FlowToolCallError → WorkerProtocolError`、`RepositoryProvisionError → VcsVerificationError`、
  `ExecutionRecord / _LegacyRecord → _RecordFields`、`CodexAgent / AmpCodingAgent → CodingAgent`；
  另有 `LegacyStopped → ExecutionStopped / _LegacyHeader` 两条有序基类关系，
  `_RecordFields / JournalSelection / _LegacyHeader → ProtocolModel` 三条，以及包外 `BubAgent → CodingAgent`。
  后端显式继承以复用执行生命周期与本地文件默认实现，不画成结构化满足。
  其余标准库/第三方基类写在节点内，不再展开。
- **虚线空心三角**：结构化满足 Protocol，未显式继承。包括
  `WorkerExecution → FlowToolCaller`、`WebSocketTransport → Transport`、
  四组连接回调 → `SessionCallbacks`。
- 灰色类来自 `app/communication`、`app/inference`、`app/domain` 或 `app/contracts`，不计入 48 类。
  `BubAgent` 虽继承 worker 的 `CodingAgent`，仍按定义路径排除。
  `link_history()`、`update_worker()` 是保留的函数锚点，**不是类**；
  `CodingAgent.start()` / `resume()` / `_execute()`、`AmpCodingAgent.run_process()` 是方法，不另造节点。
- 两个私有 `_OutputLimit` 各占一个节点，ID 为 `codex._OutputLimit` / `amp._OutputLimit`，
  展示名均保留真实类名；文件路径区分其身份。继承字段只在定义类上连线，不为每个子类重复画边。

依据：[Runtime 装配](../../app/worker/runtime.py#L147-L197)、
[执行所有权与工具 Future](../../app/worker/execution/lifecycle.py#L56-L88)、
[Executor 注入](../../app/worker/execution/executor.py#L68-L104)、
[worker-local CodingAgent](../../app/worker/coding_agent.py#L50-L187)。

## 全量类清单

链接逐个定位 `class` 所在行，按模块列出完整集合；数量以 AST 集合相等为准。

| 模块 | 全部类（定义行） | 数量 | 当前职责 |
| --- | --- | ---: | --- |
| `coding_agent.py` | [LocalFile:22](../../app/worker/coding_agent.py#L22)、[CodingAgent:50](../../app/worker/coding_agent.py#L50) | 2 | start / resume 共用 _execute；总超时、最终字节、严格 JSON、schema 生命周期；历史 / 文件契约与本地默认 IO |
| `config.py` | [WorkerSettings:12](../../app/worker/config.py#L12) | 1 | 配置、trusted-host 隔离与 WS 安全约束 |
| `runtime.py` | [Connection:67](../../app/worker/runtime.py#L67)、[WebSocketTransport:73](../../app/worker/runtime.py#L73)、[WorkerRootLock:106](../../app/worker/runtime.py#L106)、[WorkerRuntime:147](../../app/worker/runtime.py#L147) | 4 | IO 适配、root 锁、多 Flow 装配；给推理回调绑定共享 agent.start，路由历史与文件 |
| `health.py` | [HealthFile:12](../../app/worker/health.py#L12) | 1 | 本地连接状态文件；不证明执行所有权 |
| `transport/state.py` | [WorkerStateCallbacks:89](../../app/worker/transport/state.py#L89) | 1 | 注册后端标识、库存、恢复、结果/停止握手、心跳与协议授权；无 capability 探测 |
| `transport/business.py` | [WorkerBusinessCallbacks:29](../../app/worker/transport/business.py#L29) | 1 | 独立历史查询；借用执行拥有的工具调用并关联回复 |
| `transport/files.py` | [WorkerFileCallbacks:25](../../app/worker/transport/files.py#L25) | 1 | resolve_file 取得既有 Agent + source，直接 read_file；有界并发和串行 driver 内回复 |
| `transport/mcp.py` | [FlowToolCaller:24](../../app/worker/transport/mcp.py#L24)、[_EmbeddedServer:31](../../app/worker/transport/mcp.py#L31)、[WorkerMCP:38](../../app/worker/transport/mcp.py#L38) | 3 | INITIAL 与修正共享的 MCP、持有 VCS 前置事实和最近核验结果、执行工具回调 |
| `execution/lifecycle.py` | [WorkerProtocolError:35](../../app/worker/execution/lifecycle.py#L35)、[FlowToolCallError:39](../../app/worker/execution/lifecycle.py#L39)、[PendingToolCall:56](../../app/worker/execution/lifecycle.py#L56)、[WorkerExecution:63](../../app/worker/execution/lifecycle.py#L63) | 4 | 直接持有 WorkerExecutor；每 Flow 的 Task、工具 Future、取消/join、停止凭据和结果重放 |
| `execution/executor.py` | [InitialCodingOutput:59](../../app/worker/execution/executor.py#L59)、[WorkerExecutor:68](../../app/worker/execution/executor.py#L68) | 2 | INITIAL 严格输出模型；三类任务编排、Schema/VCS 核验、修正、修订 fallback 与归档清理 |
| `execution/codex.py` | [_OutputLimit:43](../../app/worker/execution/codex.py#L43)、[CodexAgent:120](../../app/worker/execution/codex.py#L120)、[CodexHistory:287](../../app/worker/execution/codex.py#L287) | 3 | 继承公共 start / resume；_run 原生执行与 IO / 取消监督、自有 JSONL 历史、本地文件 IO |
| `execution/amp_agent.py` | [AmpProcessResult:113](../../app/worker/execution/amp_agent.py#L113)、[_OutputLimit:121](../../app/worker/execution/amp_agent.py#L121)、[AmpCodingAgent:240](../../app/worker/execution/amp_agent.py#L240)、[AmpAttachment:436](../../app/worker/execution/amp_agent.py#L436)、[_Snapshot:493](../../app/worker/execution/amp_agent.py#L493)、[AmpHistory:499](../../app/worker/execution/amp_agent.py#L499) | 6 | 继承公共 start / resume；_run / export 原生监督、历史缓存分页及有 URL / name / read 行为的附件对象 |
| `execution/vcs.py` | [VcsVerificationError:27](../../app/worker/execution/vcs.py#L27)、[VcsExecutionContext:36](../../app/worker/execution/vcs.py#L36)、[VcsOutcome:51](../../app/worker/execution/vcs.py#L51) | 3 | VCS 前置事实与独立核验结果的 frozen dataclass；核验函数及分阶段错误 |
| `execution/workspaces.py` | [RepositoryProvisionError:37](../../app/worker/execution/workspaces.py#L37)、[WorkspaceBinding:45](../../app/worker/execution/workspaces.py#L45)、[WorkspaceSlot:55](../../app/worker/execution/workspaces.py#L55)、[InferenceWorkspace:81](../../app/worker/execution/workspaces.py#L81)、[WorkspaceManifest:87](../../app/worker/execution/workspaces.py#L87)、[RepositoryWorkspaces:107](../../app/worker/execution/workspaces.py#L107) | 6 | 仅 manifest v3、目录库存与 generation fence、默认推理位置；不再迁移 v1 |
| `state/execution.py` | [ExecutionStateError:34](../../app/worker/state/execution.py#L34)、[ExecutionState:38](../../app/worker/state/execution.py#L38) | 2 | 每 Flow journal、迁移、恢复/停止/清理证据与 ACK；不读历史 |
| `state/record.py` | [JournalSelection:29](../../app/worker/state/record.py#L29)、[_RecordFields:35](../../app/worker/state/record.py#L35)、[ExecutionRecord:99](../../app/worker/state/record.py#L99)、[LegacyStopped:105](../../app/worker/state/record.py#L105)、[_LegacyRecord:118](../../app/worker/state/record.py#L118) | 5 | journal v5 schema、只读 v4 转换、旧停止凭据投影及旧选择记录校验；不拥有 IO |
| `state/file_links.py` | [FileLinks:15](../../app/worker/state/file_links.py#L15) | 1 | 不透明文件 ID、通用 backend / source 登记、跨后端同源去重和 lookup；不导入后端、不读取字节 |
| `self_update.py` | [UpdateError:34](../../app/worker/self_update.py#L34)、[InstalledPackage:39](../../app/worker/self_update.py#L39) | 2 | 独立更新分支的已安装事实与安全失败 |
| **合计** | | **48** | |

## 当前的主要关系路径

### 执行所有权跨重连，回调按连接替换

`WorkerRuntime.executions[flow_id] → WorkerExecution → WorkerExecutor`。
`TaskExecutor` 已删除；执行所有者直接依赖唯一的生产执行器，不再通过单独的 Protocol。
执行对象持有工具队列和原调用者 Future；MCP 经 `FlowToolCaller(WorkerExecution)` 提交工具调用。
`WorkerBusinessCallbacks.pending` 只借用同一调用，注册回复关联，不接管执行 Task/Future。
Runtime 创建一个共享 `RepositoryWorkspaces`，为每个 Flow 创建一份 `ExecutionState` 并同时注入执行对象和 Executor。
Executor 的 `code_agent` 来自 Runtime 的共享后端实例；会话、cwd、临时凭据和子进程保持为每次调用的局部状态。
见 [Runtime](../../app/worker/runtime.py#L187-L197)、[工具队列](../../app/worker/execution/lifecycle.py#L271-L357)、
[借用与回复](../../app/worker/transport/business.py#L90-L172)。

每条连接通过 `WebSocketTransport → Transport` 进入共享 `Session`，`Gateway.serve()` 创建 `Dispatcher`，
挂载 state / business / inference / file 四组回调并串行处理事件。state 和 business 回调持有 Runtime 的反向引用，
所以静态图中存在真实的连接期引用环；Runtime 只在 `_serve_connection()` 内临时使用这些回调，按连接关闭它们。
`WorkerInferenceCallbacks` 属于包外共享实现，持有当前连接的推理 Task，断线时需要取消并等待其清理。
见 [连接装配与关闭](../../app/worker/runtime.py#L348-L395)、
[共享 driver](../../app/communication/gateway.py#L39-L105)、[推理回调](../../app/inference/transport.py#L36)。

### 四个专用类型随其生产消费者迁入 worker

本轮仅改变定义模块与导入位置：

| 类型 | 原模块 | 当前归属与消费者 |
| --- | --- | --- |
| `VcsExecutionContext` | `app/domain/models.py` | [execution/vcs.py:36](../../app/worker/execution/vcs.py#L36-L47)；Executor 准备 / replace，MCP 持有，VCS 核验与提示词使用仓库、分支、远端与前置 SHA；可空 push token 仅驻内存 |
| `VcsOutcome` | `app/domain/models.py` | [execution/vcs.py:51](../../app/worker/execution/vcs.py#L51-L57)；VCS 函数独立核验后返回，MCP 保存并传入 FlowToolCaller / WorkerExecution，Executor 对照提交与交付事实 |
| `InitialCodingOutput` | `app/domain/models.py` | [execution/executor.py:59](../../app/worker/execution/executor.py#L59-L65)；Executor 自己生成 INITIAL schema 并校验 `summary / vcs_backend_used`，保留 `extra="forbid"` |
| `LegacyStopped` | `app/contracts/receipts.py` | [state/record.py:105](../../app/worker/state/record.py#L105-L115)；仅 journal v4 的 `_LegacyRecord.stopped` 需要，`receipt()` 投影为当前 `ExecutionStopped` |

四个类的 AST（忽略源码坐标）与 `4849e88` 中原定义逐项相等；`app/` 全部类仍为 **371 → 371**，
worker 的 **44 → 48** 来自归属迁移，没有增加系统类、字段或包装层。
`LegacyStopped(ExecutionStopped, _LegacyHeader)` 的基类顺序与必填历史 `type` 均保留；
图显式区分双基类继承、`_LegacyRecord.stopped` 字段和 `receipt()` 创建当前凭据的使用关系。
`ExecutionStopped` 仍继承共享 `StopFence`；`_LegacyHeader` 仍供旧成功 / 失败凭据共用，不能随停止凭据一起迁入。
见 [MCP 字段和参数](../../app/worker/transport/mcp.py#L24-L49)、
[执行工具参数](../../app/worker/execution/lifecycle.py#L271-L280)、
[当前与历史 journal](../../app/worker/state/record.py#L99-L140)、
[共享停止契约](../../app/contracts/protocol.py#L230-L240)、
[共享旧凭据头及其他子类](../../app/contracts/receipts.py#L36-L56)。

### 编码、推理、历史、文件共用 Agent，历史仍跟随请求 session

`WorkerBusinessCallbacks → WorkerRuntime.read_history() → _agents[prefix] → CodingAgent.read_history()`。
Runtime 的 `_agents` 为 Codex、Amp 各持有一个实例；所有 Executor 按配置后端复用其中同一对象，不为每个 Flow 再创建 Agent。
历史查询复用这个集合，`T-` 前缀选择 Amp，其余选择 Codex，不依赖当前配置的后端或活动 Flow。
每个 Agent 自己创建 History，历史读取不要求 trusted-host 编码授权；同步 reader 被卸载到线程，保留各自限流和缓存。
`ExecutionState` 只负责执行持久证据，无 History 引用或历史入口。
四类操作共用固定两个后端实例，不随 Flow 数量增长，也不为推理另建 Agent。
`CodexAgent(CodingAgent)` 与 `AmpCodingAgent(CodingAgent)` 是显式继承；`backend="codex" / "amp"`
标识文件登记的归属。`CodingAgent` 除编码和历史契约外，显式提供
`register_file(Path | str, FileLinks)` 与异步 `read_file(dict[str, JsonValue], *, root=...)` 的本地默认实现；
Codex 直接继承，Amp 只扩展原生附件 URL，普通本地文件仍走同一默认实现。
见 [共享后端装配](../../app/worker/runtime.py#L164-L169)、[Executor 注入](../../app/worker/runtime.py#L187-L197)、
[历史读取与超时](../../app/worker/runtime.py#L242-L252)、
[文件契约](../../app/worker/coding_agent.py#L165-L187)、
[Codex Agent](../../app/worker/execution/codex.py#L120-L134)、[Amp Agent](../../app/worker/execution/amp_agent.py#L240-L274)。

Amp 的编码、历史导出和附件下载共用静态 `AmpCodingAgent.run_process()`；`AmpHistory → AmpCodingAgent`
是方法调用边，`AmpCodingAgent → AmpHistory` 是实例持有边，二者不表示循环构造。
流式 init/final 校验在 `_outcome()` 内进行，已无 `_Init` / `_Result` 模型。
见 [进程监督](../../app/worker/execution/amp_agent.py#L277-L379)、
[流式结果解析](../../app/worker/execution/amp_agent.py#L170-L237)、
[历史 export](../../app/worker/execution/amp_agent.py#L525-L566)、
[附件对象与下载](../../app/worker/execution/amp_agent.py#L436-L489)。

### 后端负责文件行为，FileLinks 只登记通用身份

原生 History 返回 `HistoryPage` 后，Runtime 调用 `link_history(page, self.register_file, backend=backend, deadline=deadline)`，
深拷贝缓存页、识别实际 Markdown 目标并登记文件，只改写出站副本。
`link_history.register_one()` 逐个目标选择登记来源：本地目标传 `Path`，owner 沿用历史后端；
Amp 附件对象用 `name` 作显示名、传 `url` 字符串登记，owner **始终为 `amp`**。
因此，复制到 Codex 历史中的 Amp 附件 URI 仍交给既有 Amp Agent，不能把整页历史后端固定到所有文件登记上。
`LocalFile` 现定义于 `coding_agent.py`；`AmpAttachment` 现定义于 `execution/amp_agent.py`，
仍是带 URL 校验、`name` 和异步 `read()` 下载行为的对象，不是无行为字典。

登记路径是 `link_history → register(source, owner)`，回调绑定 `Runtime.register_file`，再由
`_agents[owner].register_file(source, files) → FileLinks.register(agent.backend, source)` 持久化实际后端归属。
`FileLinks` 不导入 `CodingAgent`、`LocalFile` 或 Amp 后端，只持久化通用 `backend / source`、跨后端同源去重并提供 `lookup()`。
读取路径是 `WorkerFileCallbacks → Runtime.resolve_file(file_id) → (既有 agent, source) → agent.read_file(source, root=worker_root)`；
Runtime 只解析归属，不增加 reader / wrapper，回调直接调用 Agent。
本地默认实现重新核验 inode / device 和有界字节；Amp 按需认证下载并删除临时副本。
无 ownership 字段的旧文件记录仍可读取：含 `url` 选择 Amp，否则选择当前配置的既有 Agent；这与已删除的 manifest v1 迁移无关。
见 [链接投影及逐目标来源选择](../../app/worker/state/history_links.py#L171-L222)、
[通用登记与 lookup](../../app/worker/state/file_links.py#L15-L61)、
[Runtime 文件路由](../../app/worker/runtime.py#L254-L299)、[文件回调](../../app/worker/transport/files.py#L25-L76)。

### 推理回调直接绑定共享 start，公共生命周期归 CodingAgent

`WorkerInferenceCallbacks(start=runtime._agents[配置后端].start) → CodingAgent.start() → _execute() → 具体 Agent._run()`。
Runtime 只装配回调，已无 `infer()` / `inference_capability()` 方法；`execution/inference.py` 已删除。
`InferenceTask`、`InferenceOutput`、`domain.ports.Inference` 和 capability 探测也已删除，不保留节点或函数锚点。
注册时仅按 trusted-host 授权报告后端标识，执行失败由原生 Agent 路径报告。

共享回调接收 `worker.start` 的 `AgentStart`，在 `call.task` 前加只读要求，再直接调用绑定的 `start()`；
仅新建原生会话，不续接编码 session。`cwd=workspaces.inference_path` 使用目录池已初始化的位置，
**不创建每次调用的 workspace 子目录**；`context=None` 不携带 Flow 的 GitHub / MCP 临时能力。
`model`、`effort`、`max_output_bytes`、`timeout_seconds` 均来自本次调用，结果直接以
`CodingExecutionResult` 装入 `AgentResult`，无需独立推理输出包装。
回调拥有连接期推理 Task、busy 拒绝、心跳与取消；断线需等待提供方清理结束。
见 [连接注入](../../app/worker/runtime.py#L348-L370)、
[注册后端标识](../../app/worker/transport/state.py#L184-L204)、
[共享回调与调用](../../app/inference/transport.py#L36-L104)、
[取消与 join](../../app/inference/transport.py#L133-L155)。

`CodingAgent.start()` 和 `resume()` 都进入 `_execute()`，后者负责总超时、最终答案字节上限、
backend / 新会话 model 元数据；成功结果在有 schema 时须通过严格 JSON 解码（拒绝重复 key、非有限常量和尾随输出）。
schema 字典临时物化到调用者给定的 `cwd`，待原生 `_run()` 的清理 join 后删除；schema 路径仍由调用者拥有。
`max_output_bytes` 只覆盖本次调用的最终答案预算，不修改共享实例默认值，stdout / stderr 管道另有上限。
具体 Codex / Amp `_run()` 保留原生执行、空闲超时、有界 IO、凭据和取消后进程回收；
业务 schema 的语义与 VCS / 交付结果仍由上层调用方验证。
见 [start / resume](../../app/worker/coding_agent.py#L63-L96)、
[公共生命周期与原生 hook](../../app/worker/coding_agent.py#L98-L163)、
[严格 JSON](../../app/domain/json.py#L6-L19)、
[Codex 原生执行](../../app/worker/execution/codex.py#L136-L284)、
[Amp 原生执行](../../app/worker/execution/amp_agent.py#L381-L433)。

只读要求目前是 **prompt-only**。`context=None` 和无仓库 cwd 不会禁用原生后端的所有工具；
仍需 trusted-host 授权、专用账号 / 容器 / VM 等宿主隔离，不能把这条路径当作安全沙箱。
包外 `BubAgent(CodingAgent)` 也复用公共生命周期，自己的 `_run()` 启动无工具 SDK 子进程；
控制器经本地端点把 `BubAgent.start` 交给同一 `WorkerInferenceCallbacks`，未加入 worker 的 `_agents`。
Bub 不支持恢复原生 session 或查询历史；其定义不在 worker 统计范围内。
见 [隔离要求](../../app/worker/config.py#L40-L48)、[只读提示词](../../app/inference/transport.py#L25-L29)、
[Bub Agent](../../app/inference/bub.py#L23-L112)、[本地端点](../../app/inference/local.py#L54-L65)。

## 类图之外的函数与共享模型

### 函数所有权（含无类模块）

| 模块 / 函数 | 调用方及职责 |
| --- | --- |
| [cli.py](../../app/worker/cli.py) | 普通启动持有安装共享锁后运行 Runtime；`--update` 调用更新函数后退出 |
| [execution/prompts.py](../../app/worker/execution/prompts.py) | INITIAL、修正和 REVISION 的提示词构造 |
| [execution/credentials.py](../../app/worker/execution/credentials.py) | Agent / VCS 复用可信宿主环境过滤和临时 Git credential 环境 |
| [execution/gh.py](../../app/worker/execution/gh.py) | gh 可用性核验与本轮 bot gh 包装；不引入长期 GitHub client |
| [execution/git_identity.py](../../app/worker/execution/git_identity.py) | 目录池配置当前能力所对应的 Git 作者身份 |
| [execution/idle.py](../../app/worker/execution/idle.py) | 两个原生 Agent 的无新输出超时等待；进程回收由 Agent 负责 |
| [execution/vcs.py](../../app/worker/execution/vcs.py) | 除三个类外，执行前后及 MCP 转发前核验；目录池复用 seed、clean、align、worktree 核验函数 |
| [state/files.py](../../app/worker/state/files.py) | journal、manifest、文件身份记录共用的私有原子写入与目录 fsync |
| [state/history_links.py](../../app/worker/state/history_links.py#L171) | `link_history()` 函数锚点；Markdown 解析及文件注册回调，未造额外服务类 |
| [self_update.py](../../app/worker/self_update.py#L233) | `update_worker()` 函数锚点；安装独占锁、来源核验、更新与新进程验证 |

各级 `__init__.py` 不定义类。`health.py` 另有 `healthy()` / `main()` 探针入口。
`execution/amp_process.py`、`state/history.py`、`state/amp_history.py`、`state/amp_attachments.py` 已删除；
它们原有职责由具体 Agent 模块承接，`AmpAttachment` 类及其下载行为保留。
前轮已删除 `execution/inference.py`；原独立函数的生命周期校验并入 `CodingAgent`，推理直接复用 `start()`。

### 包外边界

14 个灰色类展示关键接口和数据流，不重复展开整个领域层或消息继承树。
以下逐项列出图节点的真实定义行，数量独立于 48 个 worker 类：

| 文件 | 图中全部包外类（定义行） |
| --- | --- |
| `app/communication/gateway.py` | [Gateway:30](../../app/communication/gateway.py#L30) |
| `app/communication/session.py` | [Transport:28](../../app/communication/session.py#L28)、[Session:44](../../app/communication/session.py#L44) |
| `app/communication/dispatch.py` | [SessionCallbacks:14](../../app/communication/dispatch.py#L14)、[Dispatcher:18](../../app/communication/dispatch.py#L18) |
| `app/inference/transport.py` | [WorkerInferenceCallbacks:36](../../app/inference/transport.py#L36) |
| `app/inference/bub.py` | [BubAgent:23](../../app/inference/bub.py#L23) |
| `app/domain/models.py` | [CodingExecutionResult:333](../../app/domain/models.py#L333)、[CodingExecutionContext:362](../../app/domain/models.py#L362) |
| `app/contracts/protocol.py` | [ProtocolModel:52](../../app/contracts/protocol.py#L52)、[ExecutionStopped:237](../../app/contracts/protocol.py#L237) |
| `app/contracts/receipts.py` | [_LegacyHeader:36](../../app/contracts/receipts.py#L36) |
| `app/contracts/conversation.py` | [HistoryQuery:19](../../app/contracts/conversation.py#L19)、[HistoryPage:81](../../app/contracts/conversation.py#L81) |

未单列节点的相关模型与边界：

| 来源 | 已画节点 / 补充边界 |
| --- | --- |
| [communication/](../../app/communication/) | 已画 `Gateway`、`Session`、`Dispatcher`、`Transport`、`SessionCallbacks`；Gateway 经 `Registration` 持有 Session 和路由器，省略中间类节点 |
| [inference/transport.py](../../app/inference/transport.py#L36) | 共享回调消费 `AgentStart`、发送 `AgentResult`；`InferenceProtocolError` 由 Runtime 转成 WorkerProtocolError |
| [inference/bub.py](../../app/inference/bub.py#L23) | 包外 `BubAgent` 继承 worker-local CodingAgent，作为本地端点后端；未展开其 OutputLimitError 或子进程辅助函数 |
| [domain/ports.py](../../app/domain/ports.py) | 仅剩 Judge；Inference 接口已删除，CodingAgent 定义在 worker 包 |
| [domain/inference_models.py](../../app/domain/inference_models.py) | 保留持久调度事实 `InferenceJob` / `InferenceSummary`、失败与错误；不再定义执行 task / output / capability |
| [domain/models.py](../../app/domain/models.py#L333-L367) | 已画 `CodingExecutionContext`、`CodingExecutionResult`；前者携带可空 `GitHubCapability` 和 MCP 能力；保留共享 `VcsKind`，Codex 可抛出 `SessionUnavailable`；VCS 的两个 frozen 类型已迁入 worker |
| [domain/models.py 的输出契约](../../app/domain/models.py#L423-L432) | 共享 `RevisionCodingOutput` 被协议 `RevisionTaskResult` 字段使用；INITIAL 的原生输出模型已迁入 Executor 模块 |
| [contracts/protocol.py](../../app/contracts/protocol.py) | 已画 `ProtocolModel` / `ExecutionStopped`；`Assignment.arguments` 中包含 `AssignmentArguments → InitialTask / RevisionTask / ArchiveTask`，临时 secret 不进入 journal；另有恢复/停止/工具/文件/推理与注册库存消息 |
| [contracts/conversation.py](../../app/contracts/conversation.py) | 已画 `HistoryQuery`、`HistoryPage`；页内含 `HistoryItem → ConversationContent`，失败使用 `ConversationFailure` |
| [contracts/files.py](../../app/contracts/files.py) | `FileFailure`、文件 ID 与字节上限；文件路径和原生远端认证留在 worker |
| [contracts/receipts.py](../../app/contracts/receipts.py#L36-L59) | 已画共享 `_LegacyHeader`；`LegacySucceeded / LegacyFailed` 和 `LegacyTerminal` 别名仍共享；`LegacyStopped` 已随 journal 迁入 state/record.py |
| [domain/delivery_models.py](../../app/domain/delivery_models.py) | `DeliveryReceipt` 贯穿 MCP 返回、`PendingToolCall.future` 与 INITIAL 交付核验 |

共享类型的保留依据包括包外生产消费者和定义模块内的字段 / 继承引用，不能仅按直接 import 判断：

- `CodingExecutionContext` 用于 [BubAgent._run](../../app/inference/bub.py#L51-L61)；
  `CodingExecutionResult` 还用于 [Bub runner](../../app/inference/runner.py#L68-L77)、
  [控制器推理](../../app/controler/workflow/inference.py#L47)、
  [judge](../../app/controler/review/judge.py#L43) 及 [AgentResult.result](../../app/contracts/protocol.py#L527-L529)。
- `RevisionCodingOutput` 是 [RevisionTaskResult.output](../../app/contracts/protocol.py#L169-L175) 的嵌套模型；
  `VcsKind` 同时用于 [InitialTaskResult](../../app/contracts/protocol.py#L143-L150)、
  [RevisionTaskResult](../../app/contracts/protocol.py#L169-L175) 和 [FlowToolArguments](../../app/contracts/protocol.py#L298-L306)。
- `ExecutionStopped` 仍供 [控制器停止持久化](../../app/persistence/stops.py#L118) 与
  [共享停止凭据摘要](../../app/contracts/receipts.py#L145-L165) 使用；
  `_LegacyHeader` 仍被 [LegacySucceeded / LegacyFailed](../../app/contracts/receipts.py#L36-L56) 继承。
- [控制器连接](../../app/controler/workers/transport.py#L84-L102) 也使用 Session，
  [本地 Bub 端点](../../app/inference/local.py#L54-L65) 也装配 Dispatcher / WorkerInferenceCallbacks；
  [控制器历史服务](../../app/controler/workers/conversation.py#L45-L51) 使用 HistoryQuery / HistoryPage。
  `BubAgent` 是控制器本地端点使用的后端，定义和消费者均在 worker 包外。

## 历史沿革与保留边界

同一口径递归统计 `app/worker/**/*.py` 的 AST `ClassDef`：

| 版本 | Worker 类数量 | 变化 |
| --- | ---: | --- |
| [最初类图](https://github.com/b1indsight/agentflow/commit/8fa8fa3193c865a31cecb38c7dcaa38994d39d37) | 33 | 旧代码基线 |
| [合入 main](https://github.com/b1indsight/agentflow/commit/f6eb85c369cc054e4c41a75166514bb03dfe0dac) | 49 | 多 Flow 执行、共享通信、文件、推理和兼容模型等变更 |
| [Agent 职责收拢](https://github.com/b1indsight/agentflow/commit/6a1fa5cb188ca75b8bd5cd9adcbec8d620764726) | 48 | 删除 `_Init` / `_Result`，原包外 `CodingAgent` 移入统计范围 |
| [共享编码 / 历史实例基线 c0da88d](https://github.com/b1indsight/agentflow/commit/c0da88d39aba3550c30f59e250cbfd1cac40cc4c) | 48 | 只改变对象装配，不新增或删除类 |
| [无状态推理 / 文件边界基线 b8aa1fb](https://github.com/b1indsight/agentflow/commit/b8aa1fbb6a5189fee4c11f386ce6652a014b0f59) | 45 | 删除 `NativeAgentInference`、`_LegacyManifest`、`_LegacySlot`；`LocalFile`、`AmpAttachment` 仅移位，无新增包装类 |
| 普通 Agent 调用重构 | 45 | 删除独立推理函数、InferenceTask / InferenceOutput / Inference 等包外执行接口；start / resume 生命周期并入 CodingAgent，worker ClassDef 集合未变 |
| 直接执行器基线 `4849e88` | 44 | 删除仅有一个生产实现的 TaskExecutor；WorkerExecution 直接依赖 WorkerExecutor |
| 当前工作树 | 48 | 四个原有专用类型迁入现有 worker 模块；全 app 类数量不变，未新增包装类 |

旧图 64 个节点中的 **57 个仍有效并保留**：删除四个 worker 类、三个包外执行接口节点；
4849e88 图另展示真实包外 `BubAgent`，共 58 节点。本轮补入四个迁入类及两个共享基类边界，
当前为 **64 节点 = 48 类 + 14 包外类 + 2 函数**，新增 13 条真实字段 / 调用 / 继承关系，共 131 条。
曾绘制的两个 inference 函数锚点、三个包外执行接口和 TaskExecutor 均保持删除。
关系以当前实现为准，未为维持旧数量而保留无效边。
执行生命周期拆分见 [`bbf8504`](https://github.com/b1indsight/agentflow/commit/bbf85048c432bf3d0e2960d36adde758eaeba9ea)，
共享 Session / callbacks 与 `transport/client.py` 移除见
[`4d9f37e`](https://github.com/b1indsight/agentflow/commit/4d9f37edf74726c89bda073d3a8c60674fbc5bec)，
并发目录库存见 [`fadf3c1`](https://github.com/b1indsight/agentflow/commit/fadf3c1fe1d77d1c96b22cf259e385710fc50bbd)。
本轮图在保留公共 start / resume / _execute、共享实例直接注入回调、原生 _run、后端文件 IO 和静态 Amp 进程监督的基础上，
补全四个迁入类的职责、字段与继承边界；业务编排、持久证据和 Agent 分组随之扩展布局。
目录池 `_load()` 直接校验 manifest v3，旧 v1 库存不再迁移；journal 的 `_LegacyRecord` / `JournalSelection` 兼容边界仍保留。

简化后仍需按实际生命周期读图：执行对象跨重连存活；连接回调只借用编码执行；历史 reader 不依赖 journal；
journal 和目录 manifest 分别证明执行状态和目录身份；文件 ID 独立于当前 assignment。
其中 generation 与目录身份核验见 [WorkspaceBinding](../../app/worker/execution/workspaces.py#L45-L78)、
[claim / release](../../app/worker/execution/workspaces.py#L336-L436)，
终态/清理 ACK 见 [ExecutionState](../../app/worker/state/execution.py#L402-L451)。

## 维护、精确核对与渲染

1. 手工维护 HTML 的 `nodes` / `edges` / `groups`，以源码构造、字段、类型注解和调用为依据。
   `id` 必须全局唯一；真实类名是 `node.name || node.id`。嵌套类新增时采用限定 ID，仍保留其真实类名和定义行。
   关系路由避让节点和分组标题栏；另用实际 `getBBox()` 核对文字边界及标题压线，不能只核对坐标常量。
   Agent 分组使用短标题；`coding_agent.py`、`execution/codex.py`、`execution/amp_agent.py` 保留在图下说明及类清单中。
2. 递归解析所有 worker `.py` 的 AST，用 `ast.walk()` 收集每一个 `ClassDef`，
   与排除 `kind=external/function` 后的图节点逐项比较 `(文件, 类名, lineno)` 的有序多重集。
   同时核对限定身份、唯一 ID 和清单表，不允许只比较总数；当前核对结果为 **48 对 48，缺失/多余/行号差异均为 0**。
   14 个灰色类与 2 个函数锚点的路径、真实名称、定义行也须核对，所有边端点须存在。
3. 本文和 HTML 使用同目录源码相对链接与实际行号，应与源码一起检出和更新。
   若改用 GitHub 固定链接，须等包含对应源码的提交产生后，再一起更新核对基线和静态图。
4. 用浏览器打开 `docs/architecture/worker-classes.html`，点击 **“导出 SVG”**，用下载结果覆盖同目录
   `worker-classes.svg`。导出直接复用 `serializeGraph()`：嵌入图形 CSS，移除聚焦/淡化和缩放状态，
   保留全部节点、关系、标题和图例；输出 **2140 × 2260**，无需 Graphviz、构建或网络。
5. 也可由现有 Playwright 环境打开本地 HTML，用 `page.evaluate(() => serializeGraph())` 获取 SVG。
   核对静态与交互图的节点/边一致，检查文本边界、箭头、下拉定位、Enter/Space、关系侧栏、reset、zoom/fit 和导出。

本次图形检查覆盖：全部 64 个节点的选择与源码链接、131 条边的有效端点/唯一性、所有节点和标题的文字边界、
键盘与鼠标/侧栏跳转、重置与缩放、聚焦前后导出一致，以及独立 SVG 的 XML 解析和节点/边数量。
桌面默认、迁入类 / LegacyStopped / CodingAgent 聚焦及 390 / 768px 窄屏默认/放大截图均已检查：关系线不遮挡标题，
图在画布内横纵滚动，页面无横向溢出；窄屏放大确实增加图宽，打印时解除缩放限制。
类图检查不证明真实 GitHub、Codex 或 Amp 链路已验收。
