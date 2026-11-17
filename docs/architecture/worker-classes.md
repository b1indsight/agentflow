# Worker 全量类关系图

> 核对基线：`ce89bab`（2026-11-17）。范围为 `app/worker/**/*.py` 中定义的全部 **33 个类**，
> 包括私有类、Protocol、Pydantic 模型、dataclass 和异常；不含测试替身及 control 端同名概念。
> 本页记录当前结构，供后续简化评估，本次不改变运行逻辑。

[打开交互图](worker-classes.html)：下载 HTML 后可直接用浏览器打开，无需构建、联网或安装依赖。
支持缩放、下拉定位、点击高亮直接关系、键盘选择，以及逐条查看关系和源码位置。
GitHub 文件页不执行 HTML；下方静态图可直接预览，点击图片可放大。

![Worker 全部 33 个类的关系图，按进程根、传输、执行、会话、工作区和更新分组](worker-classes.svg)

## 关系含义

- **实线箭头**：实例持有引用或模型字段。共享引用不表示独占所有权；`Client.session` 与
  `Executor.session` 指向 Runtime 创建的同一个对象。
- **虚线箭头**：创建、临时参数、函数调用、模型读写、异常抛出/捕获；具体含义在交互图侧栏中列出。
- **实线空心三角**：显式 Python 继承，指向基类；Worker 内只有
  `FlowToolCallError → WorkerProtocolError`。第三方/标准库基类直接写在节点中，未展开独立节点。
- **虚线空心三角**：结构化满足 Protocol，代码中没有显式继承。
  `WorkerExecutor` 满足 `TaskExecutor`，`WorkerClient` 满足 `FlowToolCaller`，
  `CodexAgent`、`AmpCodingAgent` 满足包外的 `CodingAgent`。
- 灰色 `CodingAgent` 是包外接口，`run_amp()`、`update_worker()` 是函数锚点，均不计入 33 个类。
  箭头是静态关系，不能作为执行时序或“每轮都创建”的说明。

依据：[Runtime 装配](../../app/worker/runtime.py#L92-L130)、
[Client 注入和临时回调](../../app/worker/transport/client.py#L89-L149)、
[Executor 注入与分派](../../app/worker/execution/executor.py#L52-L103)、
[CodingAgent 端口](../../app/domain/ports.py#L15-L42)。

## 全量类清单

同一行的类都定义在所链接模块中；交互图的每个节点另有固定到基线的源码行链接。

| 模块 | 全部类 | 数量 | 当前职责 |
| --- | --- | ---: | --- |
| [config.py](../../app/worker/config.py) | `WorkerSettings` | 1 | worker 配置、隔离和 WS 安全约束 |
| [runtime.py](../../app/worker/runtime.py) | `WorkerRuntime`、`WorkerRootLock` | 2 | 装配与进程生命周期；root 锁和 worker 身份 |
| [health.py](../../app/worker/health.py) | `HealthFile` | 1 | 写入本地连接状态文件，供探针函数读取 |
| [transport/client.py](../../app/worker/transport/client.py) | `Connection`、`TaskExecutor`、`WorkerClient`、`_PendingTool`、`WorkerProtocolError`、`FlowToolCallError` | 6 | WS、心跳、执行 Task、恢复/停止握手、历史查询和工具复用 |
| [transport/mcp.py](../../app/worker/transport/mcp.py) | `FlowToolCaller`、`WorkerMCP`、`_EmbeddedServer` | 3 | 单次 INITIAL 的 MCP、VCS 前置核验及回调接口；嵌入服务不接管信号 |
| [execution/executor.py](../../app/worker/execution/executor.py) | `WorkerExecutor` | 1 | 三类任务编排、Schema/VCS 核验、修订 fallback、归档与取消后的目录回收 |
| [execution/codex.py](../../app/worker/execution/codex.py) | `CodexAgent` | 1 | Codex CLI start/resume、当前轮环境和进程回收 |
| [execution/amp_agent.py](../../app/worker/execution/amp_agent.py) | `AmpCodingAgent`、`_Init`、`_Result` | 3 | Amp 编码适配、流式 init/result 校验 |
| [execution/amp_process.py](../../app/worker/execution/amp_process.py) | `AmpProcessResult`、`_OutputLimit` | 2 | 共用进程监督函数的输出与内部超限异常 |
| [execution/vcs.py](../../app/worker/execution/vcs.py) | `VcsVerificationError` | 1 | VCS/目录核验函数的分阶段错误 |
| [execution/workspaces.py](../../app/worker/execution/workspaces.py) | `RepositoryWorkspaces`、`WorkspaceManifest`、`WorkspaceSlot`、`WorkspaceBinding` | 4 | 有界目录池、manifest、槽位状态与不可变分配身份 |
| [state/session.py](../../app/worker/state/session.py) | `WorkerSession`、`SessionState`、`SessionError` | 3 | assignment journal、恢复/停止/清理证据、历史读取入口 |
| [state/history.py](../../app/worker/state/history.py) | `CodexHistory` | 1 | 按需读取原生 Codex JSONL、分页、限流与脱敏 |
| [state/amp_history.py](../../app/worker/state/amp_history.py) | `AmpHistory`、`_Snapshot` | 2 | 原生 Amp export、公开块投影、短时缓存与游标身份 |
| [self_update.py](../../app/worker/self_update.py) | `InstalledPackage`、`UpdateError` | 2 | CLI 独立更新分支的安装事实与错误 |
| **合计** | | **33** | |

## 类图之外的实际依赖

### 无类模块与函数所有权

为避免把函数误画成管理器类，图中仅为两个跨类关系增加函数锚点，其余函数模块在这里列出。

| 模块 / 函数 | 调用方及职责 |
| --- | --- |
| [cli.py](../../app/worker/cli.py) | 普通启动在 `installation_lock(updating=False)` 内运行 Runtime；`--update` 调用 `update_worker()` 后退出，不启动 Runtime |
| [execution/prompts.py](../../app/worker/execution/prompts.py) | Executor 调用 `build_initial_prompt()` / `build_revision_prompt()`，组织任务输入与输出约束 |
| [execution/credentials.py](../../app/worker/execution/credentials.py) | Codex、Amp 环境函数和 VCS 命令复用临时 Git credential 环境 |
| [execution/gh.py](../../app/worker/execution/gh.py) | Executor 调用 `require_gh()`；两个 agent 使用 `github_environment()` 创建当前轮 bot gh 包装，复用 workspace 模块的仓库标识校验 |
| [state/files.py](../../app/worker/state/files.py) | Session 与 RepositoryWorkspaces 共用 `write_private_record()` 原子写入私有 journal / manifest |
| [execution/vcs.py](../../app/worker/execution/vcs.py) | Executor 负责执行前后核验，MCP 在工具转发前调用 `verify_pushed_outcome()`；目录池复用 seed、clean、align 和 worktree 核验函数 |
| [execution/amp_process.py](../../app/worker/execution/amp_process.py) | `run_amp()` 被 AmpCodingAgent 和 AmpHistory 共用；环境、临时 settings、脱敏也是函数 |
| [self_update.py](../../app/worker/self_update.py) | `installation_lock()` 保护安装目录；`installed_package()` 返回 InstalledPackage；`update_worker()` 编排更新与新进程核验 |

各级 `__init__.py` 不定义类。`health.py` 另有独立的 `healthy()` / `main()` 探针入口。

### 包外类型边界

“全部”限定于 worker 自定义类。以下是 worker 消费的主要包外数据类型，不把整个共享领域层和协议层
重复展开成 worker 自有类；定义、字段和模型继承以所链接源码为准。

| 来源 | worker 使用的边界 |
| --- | --- |
| [domain/ports.py](../../app/domain/ports.py) | `CodingAgent` 的 start/resume 结构化接口（已在图中单列）；Worker 不使用 Judge |
| [domain/models.py](../../app/domain/models.py#L227-L283) | `CodingExecutionContext → GitHubCapability`；agent 返回 `CodingExecutionResult`；VCS 函数使用 `VcsExecutionContext` / `VcsOutcome` / `VcsKind`；Codex 可抛出 `SessionUnavailable` |
| [domain/models.py 的输出契约](../../app/domain/models.py#L333-L350) | Executor 解析 `InitialCodingOutput` / `RevisionCodingOutput`，adapter 不替代业务结果核验 |
| [contracts/protocol.py](../../app/contracts/protocol.py) | `Assignment → InitialTask / RevisionTask / ArchiveTask` 与临时 `AssignmentSecret → PushSecret`；任务结果、注册/心跳、恢复、停止、工具请求/回执和会话查询消息 |
| [contracts/conversation.py](../../app/contracts/conversation.py) | `HistoryQuery`、`HistoryPage → HistoryItem → ConversationContent` 和 `ConversationFailure`；Session 按 session ID 选择两个历史 reader |
| [domain/delivery_models.py](../../app/domain/delivery_models.py) | `DeliveryReceipt` 是工具返回、待处理 Future 与 INITIAL 结果核验共同使用的交付回执 |

## 后续简化的评估点

下面是从当前依赖推导出的检查方向，尚未选择重构方案：

1. **先看 WorkerClient 的职责集中程度**：它同时管理连接、执行 Task、工具队列、历史并发和停止握手。
   拆分前先保留“断线不取消编码执行、一个 socket reader、停止凭据确认后才释放”的契约。
   依据：[client.py](../../app/worker/transport/client.py#L151-L479)。
2. **再看 WorkerSession 的边界**：journal 与历史读取拥有不同数据源和生命周期；目前由一个对象提供入口。
   可评估是否需要拆分，但终态脱敏、归档清理证据和结果 ACK 的顺序需要保持。
   依据：[session.py](../../app/worker/state/session.py#L60-L132)、
   [终态及 ACK](../../app/worker/state/session.py#L243-L272)。
3. **追踪执行→工具→连接的临时回调**：`Client → Executor → WorkerMCP → FlowToolCaller(Client)`。
   这是当前运行时调用路径，不是 Executor 长期持有 Client 的对象引用环。
   依据：[Client 传入 self](../../app/worker/transport/client.py#L319-L341)、
   [MCP 创建](../../app/worker/execution/executor.py#L147-L162)、
   [MCP caller](../../app/worker/transport/mcp.py#L131-L150)。
4. **不要直接合并 journal 与 workspace manifest**：它们共享 WorkspaceBinding，但分别证明执行状态
   与目录槽位所有权；同一 binding 的 generation 阻止旧任务释放复用后的目录。
   依据：[SessionState](../../app/worker/state/session.py#L38-L57)、
   [绑定与清理](../../app/worker/state/session.py#L190-L208)、
   [目录池所有权校验](../../app/worker/execution/workspaces.py#L134-L154)。
5. **按真实变化区分共用与差异**：两个 agent 共用 CodingAgent、临时凭据与 Executor 业务核验；
   进程结果格式、历史来源和 session 拒绝证明不同。Amp 已有 run_amp 共用点，自更新仍是独立 CLI 分支。
   依据：[Codex resume 拒绝判定](../../app/worker/execution/codex.py#L226-L241)、
   [Amp 不自动 fallback](../../app/worker/execution/amp_agent.py#L163-L173)、
   [CLI 分支](../../app/worker/cli.py#L23-L40)。

## 维护与核对

- `worker-classes.html` 的 `nodes` / `edges` 是手工维护源；类数量应与 Python AST 的 `ClassDef` 集合一致，
  不只核对总数，还要核对 `(模块, 类名, 行号)`，避免漏一个又多一个。
- 修改关系时追踪构造、字段、类型注解与调用，不把 import 当作持有关系；显式继承与结构化 Protocol 分开。
- `worker-classes.svg` 是 HTML 全部显示、未选中状态下的 SVG 导出，供 Markdown 静态预览；
  更新 HTML 后同步导出，并在浏览器检查文字边界及箭头。
- 同步更新两份文档的源码快照；图中源码链接固定到核对基线，避免后续行号漂移。
- 验证入口优先复用 `test_worker_client`、`test_worker_connection`、`test_worker_executor`、
  `test_worker_session`、`test_worker_workspaces`、`test_worker_tools`、`test_conversation_worker`
  及 agent/history 对应测试。类图本身不证明真实 GitHub、Codex 或 Amp 运行链路已验收。
