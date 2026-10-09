# Assignment 准备的业务归属 — Feature record

**Stage:** Planning — 待审阅，尚未实施。
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
