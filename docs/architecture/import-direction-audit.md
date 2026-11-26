# 哪些模块还不能按单向依赖排列？

在 [审计基线](https://github.com/b1indsight/agentflow/commit/0cdf36620564ccee971e0e7f7f89f0ba2ef278b3) 上，Python 模块顶层的显式导入没有环；计入函数内的 `import` 后，仓储和 GitHub 资源各有一组环。**仓储环已解除，剩余的显式导入环在 GitHub 资源组。** 此外，worker 自更新会动态导入 CLI 入口；按包看也还有双向依赖。

下文 `A → B` 表示 A 导入 B。判断高低层以职责为准：[控制端](../../app/controler/runtime.py#L12-L46)装配业务服务、仓储和 GitHub 适配器；[工作端](../../app/worker/runtime.py#L48-L64)装配传输、执行器和本地状态。两端共用的方向是 [通信 → 契约](../../app/communication/session.py#L17-L18)、[契约 → 领域模型](../../app/contracts/protocol.py#L26-L45)。入口调用下层、下层返回结果，本身不构成反向导入。

已解除：[`reviews → jobs`](../../app/persistence/reviews.py#L34-L35) 和 [`jobs → flows`](../../app/persistence/jobs.py#L19) 保留，`FlowStore.transition()` 不再回引 `ReviewStore`。正常收尾时由 [`retire_queued_work()`](../../app/persistence/assignments.py#L689-L697) 在原事务内取消审核；收尾重试若仍有遗留批次，由[审核扫描](../../app/persistence/reviews.py#L265-L287)清理。

## 具体文件已经成环

| 范围 | 闭合路径和反向导入的位置 |
| --- | --- |
| GitHub 资源（6 个模块） | 父资源导入子资源，构造子资源时又局部导入父类型做校验。互引对：[`apps → repositories`](../../app/github/apps.py#L13-L14) ↔ [回引 `Installation`](../../app/github/repositories.py#L91-L100)；[`repositories → issues / pull_requests`](../../app/github/repositories.py#L13-L14) ↔ [Issue](../../app/github/issues.py#L151-L159) 和 [PR](../../app/github/pull_requests.py#L110-L118) 回引 `Repository`；[`pull_requests → review_threads`](../../app/github/pull_requests.py#L24-L25) ↔ [回引 `PullRequest`](../../app/github/review_threads.py#L58-L67)。另外，[`threads → review_threads`](../../app/github/threads.py#L13) ↔ `review_threads → threads` 用于[重读线程](../../app/github/review_threads.py#L114-L118)和[读取评论](../../app/github/review_threads.py#L131-L136)。这些互引连成一组。 |
| Worker 自更新（动态导入，另计） | [`app.cli → worker.cli`](../../app/cli.py#L23) → [`worker.self_update`](../../app/worker/cli.py#L19-L23)。安装校验用 [`importlib.import_module`](../../app/worker/self_update.py#L158-L163) 反过来加载 `app.cli` 和 `app.worker.cli`，核对实际文件与 wheel 的 `RECORD`。这是更新路径的回引，不是启动时的递归调用。 |

## 按包分层仍会遇到的反向边

以下区别对拆分很重要：`domain → github → domain` 表示两个**包**不能整体排出先后，不表示同一个领域文件在加载时绕回了自己。

| 希望保持的边界 | 目前哪些模块跨过了它 |
| --- | --- |
| GitHub 适配器 → 领域；领域不认适配器 | [`github.projects → domain.project_models`](../../app/github/projects.py#L21) 取 `ProjectStatus`。反方向有三处：[`domain.context_models`](../../app/domain/context_models.py#L8) 取 Issue/PR，[`domain.review_models`](../../app/domain/review_models.py#L12) 取线程集合，[`domain.recovery_policy`](../../app/domain/recovery_policy.py#L9) 取错误类型和退避函数。 |
| 契约和仓储不认具体 GitHub 实现 | [`contracts.result_models`](../../app/contracts/result_models.py#L14) 直接取 PR 和审核线程。仓储中，[flows](../../app/persistence/flows.py#L31)、[reviews](../../app/persistence/reviews.py#L32)、[reconciliation](../../app/persistence/reconciliation.py#L11) 取资源事实，[projects](../../app/persistence/projects.py#L17)、[results](../../app/persistence/results.py#L12) 取配置或错误与重试规则；[activity](../../app/persistence/activity.py#L12)、[agent_inputs](../../app/persistence/agent_inputs.py#L9)、[inference_notices](../../app/persistence/inference_notices.py#L10) 取 `Observation`，[console_sync](../../app/persistence/console_sync.py#L12) 还取 webhook 解析函数。[assignments](../../app/persistence/assignments.py#L39-L40) 仅为类型导入 `GitHubError`。这些边在以整个 `github` 包为适配器时越层；[现有设计](../plan/github-capability-boundaries.md#L396-L398)允许复用离线资源模型，不能据此断言领域或 worker 发起了 GitHub 请求。 |
| `worker.execution → worker.state` | [原有约定](../plan/worker-layout.md#L38-L44)写明 state 不导入 execution。回引共 5 条：[state.execution](../../app/worker/state/execution.py#L27) 和 [state.record](../../app/worker/state/record.py#L23) 从 `execution.workspaces` 取 `WorkspaceBinding`；[state.history_links](../../app/worker/state/history_links.py#L16-L18) 从 `amp_agent`、`codex`、`prompts` 取附件类型、分页上限和修订文本处理函数。正向边可见 [executor](../../app/worker/execution/executor.py#L56) 和 [workspaces](../../app/worker/execution/workspaces.py#L33)。 |
| Worker 传输与执行各自整体单向 | [`execution.executor → transport.mcp`](../../app/worker/execution/executor.py#L57)，但 [`transport.mcp → execution.vcs`](../../app/worker/transport/mcp.py#L22) 做推送校验，[transport.business](../../app/worker/transport/business.py#L25) 和 [transport.state](../../app/worker/transport/state.py#L65-L68) 也导入执行模块。按文件可以排序；若按目录分层，须先区分 MCP 工具服务与连接回调的职责。 |
| 控制端装配层 → 控制台服务 | [`runtime → console.sync`](../../app/controler/runtime.py#L17)，而 [`console.api → runtime`](../../app/controler/console/api.py#L32) 为请求中的 `ServerRuntime` 标注类型。它是普通顶层导入，但 HTTP 路由本身也是入口；只有把整个 `console` 视作 `runtime` 下层才冲突。 |

## 仅在类型检查时闭环

- **GitHub → 控制端配置。** [`github.apps`](../../app/github/apps.py#L16-L31) 导入 `ServerSettings` 作为构造参数类型；[`controler.config`](../../app/controler/config.py#L13) 又从 [GitHub 包入口](../../app/github/__init__.py#L6-L15) 导入 `ProjectConfig`。GitHub 内部还有仅靠类型回引的关系：[projects → apps / repositories](../../app/github/projects.py#L26-L28)、[issues → pull_requests](../../app/github/issues.py#L14-L16)、[threads → pull_requests](../../app/github/threads.py#L15-L16)。
- **Worker 传输 → 装配入口。** [`runtime`](../../app/worker/runtime.py#L62-L64) 导入回调；[transport.business](../../app/worker/transport/business.py#L28-L42) 和 [transport.state](../../app/worker/transport/state.py#L70-L71) 都为参数导入 `WorkerRuntime`。反向 *import* 不执行，但回调运行时确实使用传入的对象，例如[读取历史](../../app/worker/transport/business.py#L89)。

[边界测试](../../tests/test_boundaries.py#L59-L76)有意忽略 `TYPE_CHECKING`。例如叶模块 [`workflow.preparation → controler.config`](../../app/controler/workflow/preparation.py#L15-L16) 也是源代码依赖，在[禁止该叶模块依赖 controler 的运行时检查](../../tests/test_boundaries.py#L106-L111)中被排除；它本身没有形成文件环。

## 检查范围

扫描 `app/` 的 163 个 Python 文件，用 AST 解析 `import` / `from … import …`（含包入口和函数体），按顶层、函数内、`TYPE_CHECKING` 区分直接边，再查互相可达的模块。另核对了动态导入；启动另一个进程不算导入，例如 [bub 以 `python -m` 启动 runner](../../app/inference/bub.py#L78)，[runner 导入 bub 的版本常量](../../app/inference/runner.py#L22)是单向的 Python 导入，但与进程启动方向相反。

控制台另用 TypeScript 解析生产 `.ts` / `.tsx` 的本地导入（含类型、`@/` 别名和 `import()`）：29 个模块没有文件环，未见公共 UI 或数据模块反向导入页面入口。测试、构建配置、运维脚本、打包产物及第三方库不参与排序。以上是导入关系，不是每个请求的执行时序。
