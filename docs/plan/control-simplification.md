# Control 依赖与操作归属简化

**状态：** 依赖简化及 `app/controler` 模块归组已实施并通过本地验证；已报告的构造失败日志 LOW 见 PR。
**PR：** [#81](https://github.com/b1indsight/agentflow/pull/81)。
**Bookmark：** `refactor/control-simplification`
**基线：** `main` / `cbe01f90`（已合并 PR #80），2026-10-13。

## 1. 初始计划

### 动机与目标

沿用 [GitHub 简化](github-capability-boundaries.md#目标与范围) 的思路：直接依赖实际对象，
由对象拥有自己的操作，删除预留接口、工厂和纯转发。上次 [control 收敛](control-ownership-and-duplication.md#4-2026-10-10--按用户要求删减重构中的额外层次)
已删除独立结果处理器和评论回调层，本次继续减少现有层次。

当前 `app/control` 有 13 个 Protocol、156 行定义；其中内部配置、存储、服务协议重复描述
已有实现。`JobCoordinator.execute_tool` / `apply_result` 仅转发到已有对象；gateway 还为
同一个 Store 的停止操作单独注入接口。依据：[API](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/api.py)、
[gateway](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/gateway.py)、[coordinator](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/coordinator.py)。

### 范围、行为与验收

范围为 `app/control` 及必要的 CLI、测试和当前职责说明。生产代码净减少，分别报告生产、
测试、文档的增删；以实际删除的概念和调用层为验收依据。
沿用数据库 schema、配置字段、HTTP/WebSocket 消息、持久 JSON、错误分类及重试规则。
任务派发、取消、历史查询、结果 ACK 和后台恢复继续执行现有授权与提交条件。

### 技术设计与模块归属

1. **内部依赖使用具体类型。** `api`、`router`、`webhooks`、`gateway`、`coordinator`、
   `results`、`conversation` 直接引用 `ControlRuntime`、`ControlService`、`JobCoordinator`、
   `ServeSettings`、`Store` 等已有实现，删除对应的配置、上下文、存储和服务 Protocol。
   `ConversationBroker` 使用真实连接 `WorkerConnection`，类型引用处理现有循环依赖。
   socket 边界保留一个 `WorkerSocket`：gateway 的原始 WebSocket 和接收泵连接确实共用
   此边界。预计删除其余 12 个 Protocol，共 145 行定义。
2. **操作直接调用所属对象。** gateway 调用 `coordinator.tools.execute` 和
   `coordinator.results.apply`，删除 coordinator 两个纯转发入口。
   实时结果与后台恢复继续使用同一个 `ResultApplication` 及其逐 job 锁；
   coordinator 的任务准备、triage 和持久失败处理保留各自实际逻辑。
3. **停止操作归 Store。** gateway 使用既有 `store.stops`，删除 `execution_control`
   注入参数及 runtime 对 `StopStore` 的重复装配。发送授权、停止请求和回执继续由
   现有持久存储决定。runtime 的 reconciler 直接使用已创建的 GitHubApp。
4. **应用与配置显式构造。** `create_app` 接收可选 `ControlRuntime`；默认 runtime 在
   lifespan 内构造，删除 `ApplicationContext`、`_default_context` 和通用 context factory。
   配置在实际入口通过 `ServeSettings()` 创建，删除模块 `__getattr__` 的隐藏单例；
   CLI 复用该命令的配置实例，worker 导入隔离与默认 API 导入时机沿用现状。
5. **测试适配保持简短。** 调整真实入口和现有替身的调用，优先使用已有 Mock、方法 patch
   及真实对象；避免为替身新增生产接口、平行类型或大量类型忽略。

### 方案比较、成本与风险

仅移动模块或缩短函数不能减少这些接口与转发；合并 control 为一个入口大类会混合持久
状态、远端操作和连接生命周期。选择保留已有职责，直接连接实际对象。
具体类型会增加模块间导入，需要检查循环与 worker 隔离；删除 factory 会改变内部 Python
调用方式，迁移全部仓库调用者。主要风险是替身适配掩盖共享锁或停止授权，验证使用真实
业务入口。接收泵、持久发送标记、I/O 后状态复查及事务通知继续由现有模块拥有。

### 实施顺序

1. 迁移内部具体类型与测试调用，删除被替代的 Protocol。
2. 删除 coordinator 转发、停止接口注入及重复依赖导航。
3. 简化 API 和配置构造，迁移 CLI；更新当前组件说明和实施记录。
4. 执行行为验证，检查生产代码净变化，在同一 bookmark 和 PR 交付。

### 验证与消融

复用 ingress/router、gateway、真实 ASGI、历史查询、启动与导入边界、取消停止、结果恢复
及并发恢复测试；检查实时/后台同 job 互斥、未提交结果不 ACK、取消后禁止派发、
繁忙连接仍可读取历史、webhook 事务失败可重放。仅在实际行为缺口处补最小回归。
完成 PostgreSQL 全量 pytest、`uv run ruff check app tests`、`uv run mypy app tests`、
`uv build --no-sources` 及差异检查。本次删除接口与转发，没有新增运行机制需要消融；
现有风险机制由既有行为测试验证，不增加接口计数或实现镜像测试。

### 未决问题与后续

没有阻碍计划审批的未决问题。类型导入和替身适配在上述范围内按最少改动处理；新的调度、
恢复模型和功能扩展留待独立需求。

## 2. PR 变更时间线

### 2026-10-13 — 命名与类型边界要求

用户要求命名准确有效、类型边界清晰。实施沿用初始范围：应用入口使用 runtime 命名，
内部依赖指向具体实现，原始 socket 与接收泵连接明确区分；返回类型保留实际状态和失败
语义。类型适配优先修正对象契约，避免用宽泛类型或大量忽略掩盖不匹配。

### 2026-10-13 — 实施与验证

删除 12 个内部 Protocol 和 coordinator 两个转发入口；gateway 直接调用所属工具、结果
对象和 `store.stops`。配置由实际入口构造，API 使用 runtime 名称；注册前使用原始 socket，
注册后使用 `WorkerConnection`，同时删除连接上未使用的 headers/accept 转发。
内部配置、存储、服务和插件参数明确为具体类型；类型引用处理连接与历史查询的循环依赖。
测试替身对齐真实签名和对象归属，结果提交回调声明实际输入与异步返回类型；固定配置
使用完整的 ServeSettings 值，CLI 测试替换构造入口，保留原有行为覆盖和 728 个用例。

生产代码增加 125 行、删除 326 行，净减少 **201 行**；测试净增加 39 行，主要为类型与
依赖归属适配，没有新增测试矩阵。本次删除层次，没有新增运行机制需要消融。
PostgreSQL 全量 **728 passed、无跳过**；Ruff、Mypy（165 个源文件）、sdist/wheel 构建与
差异检查通过。保留既有 Starlette 弃用提示。
复现：`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run pytest -q --tb=short`；
静态检查使用 `uv run --no-sync ruff check app tests` 与 `uv run --no-sync mypy app tests`，构建为 `uv build --no-sources`。

### 2026-10-13 — 逐项复查与日志启动回归修复

用户要求确认简化是否落实并修复已报告的 bug。AST 与调用链复查确认 12 个内部协议、
coordinator 两个转发、停止控制注入、配置隐藏单例及 context factory 均已删除；唯一
保留的 Protocol 为传输边界 WorkerSocket，实时与后台继续共用 coordinator 创建的结果对象。

独立审查发现 CLI 提前创建 runtime 后，Uvicorn 默认配置覆盖脱敏 handler。CLI 现在传入
`log_config=None`，保留 runtime 的统一日志所有权，无需恢复工厂或再次初始化日志。
新增一个启动顺序回归，用真实 runtime 装配和 Uvicorn Config、替换数据库及 GitHub I/O；
检查访问日志的查询参数和异常正文不会输出，并验证 JSON 格式、级别和异常类型。
此用例在修复前因敏感正文泄露失败，修复后与既有日志回归共 3 项通过。
最终 PostgreSQL 全量 **729 passed、无跳过**；Ruff、Mypy（165 个源文件）、包构建及
差异检查通过。相对 PR 基线生产代码净减少 **200 行**，测试净增加 77 行。

### 2026-10-13 — 按模块组织代码层次（初版补充方案，包名见后续修订）

**要求与现状。** 用户希望在已完成的依赖简化上，按模块组织 `app/control` 的代码层次，
继续要求命名准确、类型边界清晰。当前工作流、连接、审核与对账的实现已有独立职责，
但同处根目录；运行时集中装配这些对象。依据：[runtime](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/runtime.py)、
[组件职责](../architecture/components.md#包依赖边界)、[既有模块边界检查](../../tests/test_boundaries.py)。

**拟议目录与完整迁移范围。** 根目录保留 `__init__.py`、`api.py`、`config.py`、`runtime.py`；
其余实现归入五个子包。每个子包的 `__init__.py` 仅说明职责，调用者直接导入所属模块。

| 子包 | 所有权 | 源文件 → 目标文件（相对 `app/control`） |
| --- | --- | --- |
| `ingress/` | GitHub webhook 持久消费、事件路由与授权 | `router.py` → `ingress/router.py`；`webhooks.py` → `ingress/webhooks.py` |
| `workers/` | Worker 鉴权、连接、派发通信与历史查询 | `gateway.py` → `workers/gateway.py`；`worker_connection.py` → `workers/connection.py`；`conversation.py` → `workers/conversation.py` |
| `workflow/` | Flow 操作、任务准备、交付生命周期、结果提交与副作用恢复 | `service.py`、`coordinator.py`、`preparation.py`、`lifecycle.py`、`tools.py`、`archive.py`、`initial.py`、`revisions.py`、`results.py`、`effects.py`、`thread_effects.py` → `workflow/` 下同名文件 |
| `review/` | 审核上下文、反馈解释、Judge 决策与隔离进程 | `review_context.py` → `review/context.py`；`resolution.py` → `review/resolution.py`；`judge.py` → `review/judge.py`；`judge_runner.py` → `review/runner.py` |
| `sync/` | 远端 PR、反馈与 Projects 状态对账 | `reconcile.py` → `sync/pull_requests.py`；`reconcile_feedback.py` → `sync/feedback.py`；`projects.py` → `sync/projects.py` |

**依赖方向与类型。** `runtime` 拥有装配和后台任务生命周期，`api` 拥有 HTTP/WS 入口。
`ingress`、`workers`、`sync` 调用 `workflow`；`workflow` 调用 `review`；各包直接使用已有
领域类型、跨进程契约及 Store/GitHub 对象。配置引用仍指向根目录 `config`。
子包不依赖 `api` 或 `runtime`，`workflow` 不导入接入、连接或对账实现，`review` 不导入
其他 control 子包。现有 `WorkerSocket` 传输协议、`Judge` 领域端口与具体内部类型各归
原有边界，继续由同一个 `ResultApplication` 拥有结果锁。通知通过既有回调完成。

**方案比较与决策状态。** 保留平铺结构的迁移成本最低，但无法从路径看出上述归属；
按任意技术层划分会把同一工作流的准备、结果与生命周期拆散。拟采用这五个职责包，
单层子目录足以表达当前归属；结果恢复与交付检查在 `workflow` 内直接互相调用。
本补充方案在初始计划的依赖简化基础上增加目录归组，既有依赖与操作归属继续成立。
方案和实现均待用户审批，在同一 bookmark 与 PR #81 中推进。

**实施、风险与验收。** 审批后移动实现，迁移全部 Python 导入及 monkeypatch 字符串，
同步 CLI、现有边界检查和当前架构说明。`app.control.api` 保持实际应用入口；Judge 的
私有进程入口改为 `app.control.review.runner`，同步 `-I -m` 启动路径与隔离测试。
包入口不增加集中导出或旧路径转发，类和函数继续使用已验证的类型与业务行为。
主要风险为遗漏动态路径、循环导入、worker 意外加载 control 依赖及 wheel 缺少新包。
扩展既有边界检查验证上述依赖方向，复用角色隔离、Judge、ingress、真实 ASGI、连接、
结果并发恢复与停止授权回归；随后运行 PostgreSQL 全量 pytest、Ruff、Mypy、sdist/wheel
构建和差异检查。逐项核对迁移清单、当前入口及遗留引用，报告新增目录与生产代码变化。
本次归组只改变职责的代码位置，没有新增运行机制需要消融。

### 2026-10-13 — 包名按实际职责修订（待审批）

用户要求重新考虑包名。复查实现后，建议采用 `events`、`workers`、`workflow`、`review`、
`reconciliation` 五个包；本条修订[上一条方案](#2026-10-13--按模块组织代码层次初版补充方案包名见后续修订)
中的两个名称，其余文件归属、依赖和验收范围沿用上一条。

| 包名 | 命名依据与取舍 |
| --- | --- |
| `events`（原 `ingress`） | WebhookConsumer 消费持久事件，EventRouter 路由与授权；HTTP 接入由根目录 API 拥有。`events` 指明处理对象，`ingress` 只能描述进入系统的位置。 |
| `workers`（保留） | WorkerGateway 管理控制端看到的 Worker 身份、连接、派发与恢复，ConversationService 读取其历史。`transport` 只能表达通信，`dispatch` 不能覆盖连接与历史查询；`app.control.workers` 与执行侧 `app.worker` 的完整路径明确各自归属。 |
| `workflow`（保留） | 共同拥有 Flow/Job 的准备、状态转换、交付和结果恢复；按同一业务过程归组，具体模块表达操作职责。 |
| `review`（保留） | 审核上下文、反馈解释和 Judge 决策共同服务审核过程；`judge` 无法覆盖上下文与反馈处理。 |
| `reconciliation`（原 `sync`） | PR/反馈观察补全本地事实，Projects 按目标状态写入远端投影，均通过定期核对使状态收敛。名称比 `sync` 长，但明确表达对账目的；`background` 只表达运行方式。 |

依据：[webhook 消费](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/webhooks.py)、[Worker 管理](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/gateway.py)、
[工作流用例](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/service.py)、[审核上下文](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/review_context.py)、
[PR 对账](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/reconcile.py)、[反馈补全](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/reconcile_feedback.py)、
[Projects 收敛](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/projects.py)。

迁移映射中的 `ingress/` 全部改为 `events/`，`sync/` 全部改为 `reconciliation/`；
依赖方向为 `events/workers/reconciliation → workflow → review`，仍由根目录 runtime 装配。
本条是命名方案修订，目录迁移尚未实施；审批后按修订名称统一迁移导入、入口和边界检查。

### 2026-10-13 — 顶层包明确 Server 角色（待审批）

用户指出目前命名看不出 Server 角色。建议将 `app/control` 迁移为 `app/server`，
与执行侧 `app/worker` 并列。现有 CLI 的 `serve` 装配 API、配置与运行时，`worker` 装配
WorkerRuntime；Docker 同样按 serve/worker 分别启动。依据：[CLI](../../app/cli.py)、
[镜像入口](../../Dockerfile)。顶层包表达运行角色，子包表达服务端拥有的职责。

```text
app/
  server/
    api.py
    config.py
    runtime.py
    events/
    workers/
    workflow/
    review/
    reconciliation/
  worker/
```

本条修订前两条方案的根路径；子包名称及文件归属沿用上一条。完整路径如
`app.server.events.router`、`app.server.workers.gateway`、`app.server.workflow.results`。
运行时与配置类型分别命名为 `ServerRuntime`、`ServerSettings`，对应服务端运行角色；
业务对象继续按各自职责命名，内部依赖指向相同的具体类型。

根包迁移包括全部导入与动态 monkeypatch 路径。ASGI Python 入口改为 `app.server.api`，
Judge 私有进程入口改为 `app.server.review.runner`；这两项替代初版补充方案中的旧路径。
命令仍为 `flowtool serve` / `flowtool worker`。同步 README、当前架构目录与边界检查，
特别是 worker 禁止加载服务端依赖的规则，以及 GitHub 对配置的类型引用。
沿用前两条的行为、隔离、静态与构建验证，并核对构建产物包含新包及实际启动入口。
该补充方案尚未实施，根包迁移和职责归组将在同一 bookmark 与 PR #81 中交付。

### 2026-10-13 — 采用 controler 根目录并实施模块归组

**要求与决策。** 用户指定根目录为 `controler`，其余按当前方案执行。本条替代前条
`app/server` 根目录提议，采用 `app/controler`；运行时和配置按已确认方案命名为
`ServerRuntime`、`ServerSettings`，子包为 `events/workers/workflow/review/reconciliation`。

**实施。** 已移动全部 27 个现有模块，新增五个仅含职责说明的子包入口。根目录保留
API、配置及运行时装配；文件归属沿用前两条清单。全部 Python 导入、测试替身和
monkeypatch 路径同步迁移。ASGI 入口为 `app.controler.api`，Judge 私有进程入口为
`app.controler.review.runner`；CLI 直接使用新路径，角色隔离与具体类型边界继续成立。
同步 README、当前架构说明、目录图与 CHANGELOG；保留前述方案的历史内容。

**验证与变化。** 先适配现有边界检查，并增加五组模块的依赖约束；迁移前因新根目录不存在
而失败，迁移后边界及 Judge 回归 **40 passed**。PostgreSQL 全量 **734 passed、无跳过**；
Ruff、Mypy（170 个源文件）、sdist/wheel 构建及差异检查通过，保留既有 Starlette 弃用提示。
wheel 包含全部 32 个 controler 模块，旧 control 包已移除；独立解释器以 `-I` 成功导入
API、Judge runner 及两个新类型。对迁移前 `a50ac2c0` 的 28 个实现文件进行比较，统一
已批准的模块路径与类型名称后，导入集合和业务 AST 均一致。历史记录的 26 个代码链接
固定到迁移前提交，当前架构说明指向新路径。依据：[新运行时](../../app/controler/runtime.py)、
[依赖边界检查](../../tests/test_boundaries.py)、[当前目录](../architecture/roadmap.md#8-当前目录与里程碑)。

本轮生产增加 106 行、删除 80 行，净增加 26 行，来自五个包说明和较长导入的格式调整；
测试增加 155 行、删除 143 行，净增加 12 行，用例数仅增加五组边界检查。
相对 PR 基线，生产增加 204 行、删除 378 行，净减少 **174 行**；测试增加 379 行、
删除 290 行，净增加 **89 行**；文档增加 293 行、删除 27 行，净增加 266 行。
本次增加的五个目录只表达代码归属，没有新增运行机制需要消融。验证命令沿用前文；
包入口核对使用 wheel 清单与 `.venv/bin/python -I`，新的 ASGI/Judge 导入路径见本条实施记录。
此前已报告的构造失败日志 LOW 继续保留在 PR 记录，目录归组沿用已验证的启动行为。
