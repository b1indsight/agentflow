# 内部契约与输出解析精简

**阶段：** 已实现并通过全套验证，待独立代码审查。
**PR：** 待创建。
**基线：** `main` / `c401b206`（PR #54 已合并，开工时工作区与 main 内容一致）。
**Bookmark：** `refactor/internal-contract-simplification`。

## 1. 已批准的方案

用户在整体架构审查后明确要求“实现这些优化”。本记录承接该轮已批准的七项建议，
不重复请求计划批准。PR #54 已关闭，因此本次后续优化使用新的功能记录与 PR。

### 动机、范围与验收

减少没有表达真实变化的内部接口、重复字段与业务解析层，同时保留 control/worker 分离、
持久化结果恢复和不同安全语义的模型。验收要求如下：

1. `apply_result(job_id)` 只应用持久 inbox 中的结果；类型与 job 不匹配仍被拒绝。
2. `CodexAgent` 返回原始最终消息与进程信息，不再根据 schema 字段猜测业务类型。
   INITIAL/REVISION executor 分别校验各自模型；非法输出仍在 result 阶段失败，
   非零 exit、session fallback、VCS 核验及网络结果格式保持原有语义。
3. 删除 `AgentRun.output`、`SettleScheduler`、重复结果类型校验和未使用的 runtime-checkable
   装饰器。settle 的启动执行、独立循环、异常恢复和 job 通知保留。
4. `CoordinatorSettings` 复用 `ResultSettings`，移除未使用的 allowlist 声明；
   `TaskExecutor.secret` 明确为 `AssignmentSecret | None`。
5. 插件模块集中调用与异常隔离；业务流程仍决定触发时机，提交后回调不变。

### 取舍、风险与实施顺序

不合并 `PushCapability/PushSecret`、`PreparedDelivery/OpenedDelivery`、
`WorkerRecord/WorkerView` 或 `ResultApplication/ResultEffects`，它们具有真实的安全、
状态或职责边界。不引入统一调度器、事件总线、通用结果模型或历史接口兼容层。

先用行为测试固定原始输出解析和持久结果读取，再删除重复实现并同步调用者。
主要风险是成功输出不再被解析、错误类型被接受、恢复或通知时机变化；分别由 executor、
coordinator、gateway、事务、归档插件和并发恢复回归覆盖。无需数据库或 wire 迁移。

验证包括针对性测试、真实 PostgreSQL 全套测试、Ruff、Mypy、打包及 diff 检查。
本次只删除与收敛既有机制，不新增需要消融证明的调度、缓存或决策机制；
使用相同回归输入比较修改前后行为，不另造性能实验。

## 2. 实施时间线

### 2026-09-30：批准范围与基线确认

- 用户批准上一轮五项主要建议和两项小清理。
- GitHub 确认 PR #54 已合并；fetch 后 `jj diff --from main@origin --to @ --summary`
  为空，从最新 main 创建独立变更，保留此前已完成的执行控制接口命名。

### 2026-09-30：测试先行与实现

- 改动前运行三个代表性测试，均按预期失败：adapter 对不含 properties 的调用方 schema
  抛 KeyError，INITIAL 无法消费原始 JSON，coordinator 仍要求第二个结果参数。
- 移除 schema 猜测和 `AgentRun.output`；executor 使用各自 Pydantic 模型解析 last_message。
  解析异常不输出包含输入值的 ValidationError；保留现有失败分类。
- 结果应用只接收 job ID，删除 coordinator 重复类型校验，持久化结果由 ResultApplication
  验证。同步 live/recovery 调用和测试替身；远程闭环替身现在实际保存接收载荷。
- settle 操作归入 runtime；配置 Protocol 继承已有字段；插件异常隔离归入 emit_hook。
- 针对性回归 **92 passed / 1 skipped**；跳过项需要 PostgreSQL，另行完整验证。
  Ruff、Mypy（138 源文件）与 diff 检查通过。全套数据库回归进行中。

### 2026-09-30：完整验证与复杂度检查

- `FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run --no-sync pytest -q --tb=short`
  完成：**804 passed，0 skipped**。保留的一条警告来自 Starlette TestClient 的 httpx 弃用提示。
- `uv run --no-sync ruff check app tests`、`uv run --no-sync mypy app tests`（138 源文件）、
  `uv build`、`git diff --check` 均通过。
- 原有结果重放/事务原子性、并发恢复、worker Git/jj 交付、归档插件异常回归继续通过。
  唯一新增场景验证 adapter 对调用方 schema 不做业务推断；其余沿用现有行为测试并调整契约。
- 改动减少既有机制，无新增可消融的决策或调度结构；保留的薄入口 apply_result(job_id)
  是 gateway 的窄接口，避免让传输层依赖 ResultApplication 的具体实现。
