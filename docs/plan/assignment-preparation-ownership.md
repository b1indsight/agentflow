# Assignment 准备的业务归属 — Feature record

**Stage:** Implemented — 实现、消融与本地验证完成。
**PR:** [#71](https://github.com/b1indsight/agentflow/pull/71)。
**Bookmark:** `refactor/assignment-preparation-ownership`
**基线:** `main` / `a7c91202`（PR #68），2026-10-10。

## 1. Initial plan

### 背景、目标与范围

coordinator 准备任务并决定读取失败的重试；gateway 随后获取 capability，又处理 capability
失败落库、delivery 在等待期间关闭/合并的业务决策。gateway 多次区分 ExecutionTask 与
JobState。职责分散使传输代码必须理解准备阶段的业务失败规则。
依据：[coordinator](../../app/control/coordinator.py)、[gateway](../../app/control/gateway.py)、
[准备重试测试](../../tests/test_preparation_retry.py)。

目标是 coordinator 完整拥有准备业务，gateway 拥有连接、租约心跳、取消和消息发送。
范围限于未开始 assignment 的准备路径；不调整领取、恢复、stop 协议、SQL 锁顺序、
准备重试配置、wire 版本或已开始执行的状态机。

### 拟议行为

gateway 启动一个 coordinator 准备协程，在等待期间维持当前 token/epoch 的心跳；
准备成功后执行既有 authorize_dispatch，再发 Assignment。取消或断线时取消并等待
准备任务结束；绝不延续旧连接的准备任务。

coordinator 在同一准备入口中依次完成任务构造、push capability 获取和 I/O 后业务复查。
返回已构造的 Assignment，或只能为 QUEUED/DONE/FAILED 的准备结果。成功直接复用
既有 wire Assignment；不增加 PreparedTask、PreparedAssignment 等同内容中间类型。
gateway 在单一分支处理完成结果：QUEUED 重排，其余结束租约并通知。

### 技术设计与关键边界

- `Coordinator.prepare_for_assignment(job, worker, token, epoch)` 收拢 capability 构造、
  capability 失败处理及最终 delivery 判定，复用当前 prepare 和 fail/apply_result 能力。
  对外不再分两次调用 prepare_for_assignment / assignment_secret。
- `AssignmentCoordinator` Protocol 同步收窄；用 Literal 限定允许的准备状态，避免把
  RUNNING 等整个 JobState 空间当成合法返回。取消异常不能被业务失败 handler 吞掉。
- `WorkerGateway._prepare_assignment()` 保留心跳等待循环、租约失效检测、取消并 join；
  不读取 delivery 或决定“PR merged before dispatch”的业务结果。
- coordinator 在所有外部 I/O 后复查当前 token/epoch、未开始状态和 delivery；gateway
  authorize_dispatch 仍是发送前的独立授权屏障，不能用早期成功结果取代它。
- 初始 Github 读取失败仍走持久读取重试预算。capability 签发失败仍落为失败，不能进入
  该预算。CLOSED/MERGED 期间的 DONE/FAILED 判定与现有路径逐项对照，不借搬移统一语义。

### 备选方案与取舍

仅提取 gateway 的布尔 helper 无法集中业务归属。把心跳循环搬到 coordinator 会让业务
层拥有连接调度。新增通用 PreparationService 会扩大职责和调用链。本方案使用既有
coordinator 和 Assignment，保留 transport 的生命周期所有权。
返回 task/status 两种实际结果仍有必要，不为了减少一个分支引入异常控制正常重排。

### 风险与控制

最主要风险是长时间 capability I/O 后使用过期授权、关闭/合并期间错误派发，以及
断线后准备协程继续写入。持久 fence、发送前授权、心跳和 cancellation 必须同时保留。
业务处理必须保持已有事务粒度，不能在数据库锁内等待 Github 请求。
成功对象包含 SecretStr capability，禁止整体日志输出。

### 实施顺序

1. 对照当前准备路径建立分支清单，复用或补齐“capability 等待期间 close/merge、取消、
   lease/epoch 变化”的可控并发测试；实施前验证现状结果。
2. coordinator 收拢业务并直接构造 Assignment，逐步迁移 gateway 的调用和替身。
3. 删除旧分离入口，确认恢复路径和 stop 分支没有额外迁移，更新职责说明。

### 验证与消融

复用 `test_preparation_retry`、`test_gateway`、`test_recovery_concurrency`、
`test_cancel_drain`、`test_pr_lifecycle`、`test_coordinator` 和事务回归。
新增并发用例使用可控事件交错，不使用实际睡眠；只覆盖缺失的边界。
实现后分别移除 capability I/O 后的有效性复查、准备任务取消并等待，确认上述并发用例
能捕获旧授权派发或取消后的写入；保留 authorize_dispatch 的独立回归。
消融之后恢复实现，运行真实 PostgreSQL 全套、Ruff、Mypy、`uv build` 和 diff 检查。

### 开放问题与后续工作

无阻塞设计问题；实施时核对现有 CLOSED/MERGED 各时序结果，并在时间线记录必要调整。
不合并读取重试与签发失败政策，不创建通用调度器。与其他三个计划独立，建议最后实施。

## 2. PR change timeline


### 2026-10-10 — 收拢准备业务与并发边界

- **触发与实现：** 原 gateway 分别取得 task/secret 并判定签发失败及 delivery；现在
  `JobCoordinator.prepare_for_assignment` 直接构造既有 `Assignment`，内部保留读取预算与
  capability 签发的独立边界。`PreparationState` 使用 Literal 限定 QUEUED/DONE/FAILED，
  不增加消息中间类型。旧公开 `assignment_secret` 已移除，签发 helper 仅供 coordinator 内部使用。
- **边界选择：** 复用 `clear_preparation` 的事务复查，在签发成功和异常后检查原 token、
  worker epoch、有效租约、未 started、未接收 result，再使用已有 fail/result 路径。
  相比 heartbeat，该检查也拒绝已启动任务；不改变 SQL 锁顺序，不在数据库事务中等待签发。
  gateway 继续拥有心跳、取消并 join、发送前 `authorize_dispatch`，成功后仅序列化 Assignment。
- **政策对照：** 准备入口已发现退休任务返回 DONE；读取失败仍由 `defer_preparation` 决定
  DONE/QUEUED/FAILED。签发成功后遇 CLOSED 返回 DONE，遇 MERGED 失败；签发异常在原授权
  仍有效时失败，即使 delivery 已 CLOSED/MERGED，也不进入读取预算。原授权失效则抛
  PreparationStale，不写失败结果。未统一这几个时序的语义。
- **测试迁移：** gateway/ASGI/remote/stop 替身改为直接返回 Assignment；真实 PostgreSQL
  参数化测试覆盖签发成功和失败时的 CLOSED、MERGED、epoch、token、租约过期和 started。
  取消测试以事件控制异步清理，验证准备入口等待 signer 退出；发送前授权保留独立回归。
  先写的 coordinator 测试在原实现下 CLOSED/MERGED 两例失败，证明业务尚未收拢。

### 2026-10-10 — 消融结果与保留决定

- **设置与复现：** 本地 PostgreSQL `127.0.0.1:55419`，同一组回归逐个移除机制，
  每次结束在 finally 恢复文件。命令 `uv run python .cache/2026-10-10/pr71-ablation.py`；
  脚本及各项日志保存于本地日期 cache，不进入版本控制。
- **结果：** 移除签发成功后的持久 fence：epoch/token/expired/started 四例失败；移除
  delivery 复查：CLOSED/MERGED 两例失败；移除取消后的 join：异步清理尚未退出时准备入口
  已结束，一例失败；移除发送前授权：过期 token 被发送，一例失败。各变体均以 pytest
  exit 1 捕获预期行为差异，恢复后再运行完整套件。
- **测试修正与决定：** 初版取消测试的清理立即结束，移除 join 后仍通过；增加可控清理
  release 事件后可靠捕获差异。保留四个独立机制，未增加通用 service、状态空间或协议版本。


### 2026-10-10 — 最终验证

- 恢复消融变体后，`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres
  uv run pytest -q --tb=short`：**851 passed**，无跳过；覆盖准备重试、gateway、恢复并发、
  cancel/drain、PR lifecycle、coordinator 与事务回归。仅有既有 Starlette/httpx 弃用警告。
- `uv run ruff check app tests`、`uv run mypy app tests`（160 files）、`uv build`、
  `git diff --check` 均通过。已检查最终 diff 与 code-principles，新增测试限于真实并发边界，
  不引入通用调度抽象、数据库 schema 或 wire 变更。


### 2026-10-10 — 精简重复测试

- **请求与变更：** 按用户要求删除低价值测试，签发并发矩阵由 12 个实例减为 5 个，
  删除 7 个重复排列：CLOSED 成功、MERGED 失败、token/expired 的成功与失败，以及 started 失败。
  CLOSED 成功仍由 `test_close_during_capability_preparation_never_dispatches` 覆盖；签发失败
  的终态与读取预算政策仍有独立测试。token、lease 与 started/epoch 共用已有
  `_lock_preparation`，保留 epoch 的成功/失败两例验证准备入口的两个复查分支，另保留
  started 成功证明复查不能被允许已启动任务的 heartbeat 取代。未删除仓储边界测试。
- **保留：** MERGED 成功后的业务判定、CLOSED 时签发失败的时序政策、取消后等待异步
  清理、发送前独立授权。消除矩阵笛卡尔积和不再使用的 token/lease 场景构造。
- **证据：** 精简后重跑原 `pr71-ablation.py`，移除签发后 fence 为 2 failed，移除
  delivery 复查为 1 failed，移除 join 与发送前授权各 1 failed；均为预期行为断言失败，
  文件已恢复。三个受影响测试文件的真实 PostgreSQL 回归为 **60 passed**；Ruff 与 diff 检查通过。
  本次仅删除测试排列与简化断言，不修改生产代码；之前的 851 项全套结果保留为历史记录。
