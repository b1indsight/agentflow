# Control 依赖与操作归属简化

**状态：** 已实施，本地验证完成；独立审查与 CI 见 PR。
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
同一个 Store 的停止操作单独注入接口。依据：[API](../../app/control/api.py)、
[gateway](../../app/control/gateway.py)、[coordinator](../../app/control/coordinator.py)。

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
