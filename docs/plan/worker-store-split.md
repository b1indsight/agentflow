# WorkerStore 拆分与派发策略下沉 — 功能记录

**阶段：** 计划已批准并已实现；验证结果与交付状态见时间线。
**PR：** [#172](https://github.com/b1indsight/agentflow/pull/172)（草稿）。
**Bookmark：** `refactor/worker-store-split`；同一功能记录、同一 bookmark、同一个 PR。
**需求与审批：** [本线程](https://ampcode.com/threads/T-01a1e050-ae2c-7193-81c2-44c2a71f1548)，由[架构审查线程](https://ampcode.com/threads/T-01a1df4f-9c18-7322-8a3a-1b1153ba4a2d)提出。
**规划基线：** `main@origin` 的 [c1233e2a](https://github.com/b1indsight/agentflow/commit/c1233e2ad121ef5133ba8fe9f72720e05ec38855)，已含 #171；规划前执行 `jj git fetch --remote origin`，远端无变化。独立 jj workspace 为 `.amp/in/worker-store-split/workspace`，不编辑 default workspace 的工作副本。

下文的源码链接都固定在规划基线，行号不随后续提交漂移。

## 1. 初始计划

### 摘要与动机

- **现状：** [`WorkerStore`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L88) 一个类 33 个方法、约 1180 行，同时承担四件事：worker 会话与库存登记、Flow 执行绑定、编码与推理的领取和租约、推理调用的取消/结算/恢复。
- **策略写在仓储里：** 推理失败后的退避、三次上限、结束原因和 worker 避让规则在 [`_settle_inference`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L854-L918)；候选 worker 的排序在 [`assign_job`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L385-L408)；取消条件在 [`_inference_cancellation`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L773-L800)；准备阶段的重试预算在 [`defer_preparation`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L1080-L1089)。这些规则只能通过真实数据库场景测试。
- **仓储互相缠绕：** `flows`、`jobs`、`workers`、`reviews` 之间有 7 处函数内导入（[`workers.py` L775](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L775)，[`flows.py` L113](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/flows.py#L113)、[L255–L256](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/flows.py#L255-L256)，[`jobs.py` L36–L37](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/jobs.py#L36-L37)、[L202](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/jobs.py#L202)）。`WorkerStore` 还有 11 处调用 `Database` 的私有锁方法 [`_lock_worker` / `_lock_workers`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/database.py#L132-L149)。
- **受影响的人：** 维护调度与恢复逻辑的开发者。要改一条推理重试规则，目前必须读懂整个锁定事务；控制层的 9 个服务都构造 `WorkerStore`，其中多数只为读取一条 Flow 绑定。
- **方案概述：** 策略改为 `app/domain` 的纯函数；`WorkerStore` 按职责分到四处；`Database` 提供公开的 worker 锁方法；7 处函数内导入消除 6 处。外部行为、数据库结构、wire 协议和 worker → Flow → job 锁顺序不变。

### 目标、范围与验收标准

**目标与验收：**

1. 推理结算、取消条件、候选选择、单次尝试规划和准备重试预算都是 `app/domain` 的纯函数，不读时钟、不执行 SQL；仓储只加锁、读事实、调用决策并落库。验收：这些函数有不依赖数据库的表驱动测试，仓储内不再出现对应的分支判断。
2. `WorkerStore` 只保留会话与库存登记。验收：原 33 个方法各有明确归属（见“技术设计”的对照表），`workers.py` 不再包含领取、租约或推理逻辑。
3. `app/persistence` 内的函数内导入从 7 处降到 1 处；`_lock_worker(s)` 的私有调用为 0。验收：新增结构性测试只放行剩余的那一处。
4. 行为不变。验收：现有 PostgreSQL 全量回归的断言不改，只改调用的仓储对象；固定场景的 SQL 语句序列与基线一致，已知差异只有下文“边界情况”列出的两项。

**范围：** `app/persistence/{workers,flows,jobs,stops,store,database}.py`，新增 `app/persistence/{assignments,inference}.py` 和 `app/domain/inference_policy.py`，以及因方法归属变化而调整的控制层调用、smoke 脚本、测试调用和架构文档。

**不做：**

- 不改数据库结构、wire 协议、`ResultPlan` 格式和任何对外行为。
- 不把推理移出 `jobs` 表（判断见“两项判断”）。
- 不改 `FlowStore.transition()` 的入参方式（判断见“延后工作”）。
- 不改 Flow/Job/Delivery 的状态机，不重写调度算法，不调整各阶段各自的派发资格和退休判断。
- 不保留转发旧方法名的兼容外壳。

**约束：**

- [persistence-simplification](persistence-simplification.md) 已决定：跨表操作按完整的不变量归属，不按表拆散原子提交；保留现有业务方法名；不新增 Protocol、基类、Mixin 或转发层。本计划沿用。
- [worker-inference-dispatch 变更 009](worker-inference-dispatch.md) 记录了用户决定：推理走统一的 Job 分配流程，进入 `jobs` 表，由同一个领取入口按类型分配，“只把另一个 `assign_inference_job` 搬进同一文件”不满足要求。因此 `assign_job(slot=...)` 仍是唯一的领取入口。
- [#171](flow-state-transitions.md) 确立的单一 Flow 状态写入口、`FlowStore.locked()` 和测试不绕过合法事件的约定不变。

### 拟议行为

**改动前后（开发者视角）：** 以“连续被限流时第三次失败怎样结束”为例。

- 现在：规则在 `_settle_inference` 中，与工作区代次校验、两条 UPDATE、通知写入和归档检查交织。验证它需要启动 PostgreSQL、注册 worker、入队、领取三次并注入失败。
- 之后：规则是 `decide_inference_settlement(job, failure, cancellation=None, now=...)`，输入一个 `Job` 值和失败信息，返回目标状态与更新后的 `InferenceJob`。测试直接构造值并比较结果。仓储的 `settle_inference` 只剩：校验代次、读取取消事实、调用决策、写 `jobs` 和 `workers`、追加通知、检查归档。

**调用方式的变化：**

| 现在 | 之后 |
| --- | --- |
| `WorkerStore(db).assign_job(...)`、`start_assignment(...)`、`sweep_expired_assignments()` 等 | `AssignmentStore(db)` 的同名方法 |
| `WorkerStore(db).inference_needs_cancel(...)`、`complete_inference_result(...)` | `InferenceStore(db)` 的同名方法 |
| `JobStore(db).enqueue_inference(...)`、`consume_inference(...)` | `InferenceStore(db)` 的同名方法 |
| `JobStore(db).cancel_jobs(...)` | `AssignmentStore(db).cancel_jobs(...)` |
| `WorkerStore(db).get_flow_execution(...)`、`release_flow_execution(...)` 等 | `FlowStore(db)` 的同名方法 |
| `tx._lock_workers(connection)`、`db._lock_worker(connection, worker_id)` | `tx.lock_workers()`、`tx.lock_worker(worker_id)` |
| `Store.workers` 聚合全部方法 | `Store` 增加 `assignments`、`inference` 两个入口 |

**错误情况：** 全部保持原样。领取失去工作区代次、结算失去代次、准备无法退回队列仍抛原有 `ValueError`；过期、他人或冲突的结果仍返回 `"rejected"` / `"conflict"`。新的 `lock_workers()` / `lock_worker()` 在未绑定事务时抛 `RuntimeError`，与现有 [`lock_workflow` / `lock_job`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/database.py#L94-L130) 一致。

**兼容性：** 无数据迁移、无协议升级、无新旧格式并存。这些类都是进程内对象，没有外部调用方；回退应用版本不需要改库。

### 技术设计与受影响模块

#### 职责划分及理由

审查时的初步划分是“槽位与库存登记、派发与租约、推理调用”三块。读完代码后采用这三块，并补充一项：Flow 执行绑定的读取和释放归 `FlowStore`。

| 模块 / 类 | 职责 | 原 `WorkerStore` 方法 |
| --- | --- | --- |
| `workers.py` / `WorkerStore`（保留，约 260 行） | worker 会话与库存：连接、就绪、库存上报、断线和启动时的隔离、列表、drain | `worker_connected`、`report_workspace`、`worker_ready`、`worker_disconnected`、`recover_abandoned_assignments`、`list_workers`、`list_workspaces`、`drain`（8 个） |
| `assignments.py` / `AssignmentStore`（新增，约 620 行） | 带 fencing 的领取与租约：候选查询、领取、开始、心跳、恢复、结果入库、完成、准备重试、退回队列、租约过期回收、取消排队任务 | `workspace_demand`、`assign_job`、`_claim_job`、`start_assignment`、`heartbeat_assignment`、`resume_assignment`、`assignment_active`、`record_assignment_result`、`finish_assignment`、`_lock_preparation`、`reject_assignment`、`clear_preparation`、`defer_preparation`、`requeue_assignment`、`sweep_expired_assignments`（15 个），另从 `JobStore` 接收 `cancel_jobs` |
| `inference.py` / `InferenceStore`（新增，约 330 行） | 一次逻辑推理调用的生命周期：入队、消费、加锁、取消、结果校验、结算、失联恢复 | `_lock_inference`、`_inference_cancellation`、`inference_needs_cancel`、`complete_inference_result`、`_settle_inference`、`_recover_inference`（6 个），另从 `JobStore` 接收 `enqueue_inference`、`consume_inference` |
| `flows.py` / `FlowStore`（已有） | 增加 Flow 执行绑定的读取与释放 | `get_flow_execution`、`get_worker_executions`、`get_worker_execution`、`release_flow_execution`（4 个） |

8 + 15 + 6 + 4 = 33，没有遗漏或重复的方法。

理由：

- **绑定归 `FlowStore`。** `flow_executions` 以 `flow_id` 为主键，[`FlowExecution`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/domain/models.py#L176-L186) 的定义是“一个 Flow 生命周期内对 worker 槽位的稳定占有”，释放它的主要入口 [`try_archive`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/flows.py#L248-L315) 本来就在 `FlowStore`。`flows.py` 对 `workers.py` 的函数内导入只为这两个调用。绑定必须位于 `flows` 之下或之内，循环才会消失；放进 `FlowStore` 不需要新类。控制层的 `archive.py`、`lifecycle.py`、`effects.py`、`conversation.py`、`pull_requests.py` 使用 `WorkerStore` 只为读取或释放绑定；其中的服务和 `lifecycle.py` 的函数已经持有 `FlowStore`，改用它之后不再需要 `WorkerStore`。绑定的创建仍在 `assign_job` 的领取事务内，不拆开。
- **领取入口保持一个。** 按变更 009 的决定，`assign_job(slot="execute" | "infer")` 留在 `AssignmentStore`，`_claim_job` 继续为两类任务提交占有。推理分支的候选规则交给纯函数，取消和超时结算调用 `InferenceStore` 的公开方法。依赖方向是 `AssignmentStore → InferenceStore`，单向。
- **断线隔离归会话。** `worker_disconnected` 和 `recover_abandoned_assignments` 形状相同：将 worker 标为离线、恢复未知的推理尝试、给可能已执行的任务和绑定设置恢复期限。后者相当于“启动时所有 worker 都已断线”，两者放在一起。`WorkerStore` 因此依赖 `InferenceStore.recover_inference`。
- **推理的入队和取消归 `InferenceStore`。** `enqueue_inference` 依赖 `ReviewStore` 判断批次当前性，`cancel_jobs` 的推理部分依赖推理结算；它们留在 `JobStore` 正是 `jobs.py` 需要函数内导入的原因。
- **`cancel_jobs` 归 `AssignmentStore`。** 它退休排队中和未派发的任务，并清空 `workers.current_job_id`，属于领取与槽位的事务。推理部分拆成 `InferenceStore.cancel_flow_inference`，在同一事务内调用。

#### 拆分后的模块依赖

顶层导入构成单向依赖，箭头表示“导入”：

```text
stops ─> assignments ─┐
            workers ──┴─> inference ─> reviews ─> jobs ─> flows ─> deliveries / results / activity
```

`stops` 导入 `assignments` 是为了 `assignment_active`；图中靠左的模块也可以直接导入更靠右的模块（例如 `assignments`、`workers` 从 `jobs` 取得共用 SQL 片段），方向始终向右。

| 基线中的函数内导入 | 处理 |
| --- | --- |
| `workers.py` L775 → `reviews` | 随取消逻辑移入 `inference.py`，改为顶层导入 |
| `flows.py` L255 → `deliveries` | `deliveries.py` 不导入 `flows`，本就不成环，改为顶层导入 |
| `flows.py` L256 → `workers` | 绑定方法进入 `FlowStore`，导入删除 |
| `jobs.py` L36 → `reviews`、L37 → `workers` | `cancel_jobs` 移到 `AssignmentStore`，导入删除 |
| `jobs.py` L202 → `reviews` | `enqueue_inference` 移到 `InferenceStore`，改为顶层导入 |
| `flows.py` L113 → `reviews` | **保留。** #171 把 F/A 转换时的 review 取消放进 `transition()`，以保证与状态写入同事务；`reviews.py` 又在顶层导入 `FlowStore`。消除它需要让 `FlowStore` 持有 review 表的 SQL，或者为一个两条语句的函数新建模块，两者都与本次拆分无关，列入延后工作 |

#### 共用 SQL 片段的归属

| 片段 | 现状 | 之后 |
| --- | --- | --- |
| `_CURRENT_WORKSPACE`（任务记录的占有仍是槽位当前的精确占有） | `workers.py` 私有，领取、租约和推理结算共用 | `jobs.py` 导出 `CURRENT_WORKSPACE_SQL`，沿用 [`RESOURCE_JOB_SQL`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/results.py#L21-L32) 的导出方式；`assignments.py` 与 `inference.py` 都导入它 |
| `_MAY_HAVE_EXECUTED`（任务可能已经执行的证明） | `workers.py` 私有，断线和启动隔离共用 | `jobs.py` 导出 `MAY_HAVE_EXECUTED_SQL`；`workers.py` 导入 |
| `_ASSIGNMENT_FLOW`、`_ELIGIBLE_JOB` | `workers.py` 私有 | 移到 `assignments.py`，保持私有；文本不变 |

`release_flow_execution` 和 `try_archive` 中带不同表别名的内联副本原样移动，本次不合并。

#### `Database` 的公开锁方法

`app/persistence/database.py`：

```python
def lock_workers(self) -> None:
    """按 (worker_id, workspace) 顺序锁全部 worker 行；须在事务内，位于任何 Flow/job 锁之前。"""

def lock_worker(self, worker_id: str) -> None:
    """按 workspace 顺序锁一个 worker 的全部行；须在事务内。"""
```

两者替换原来接收 `connection` 的静态私有方法，SQL 不变，与 `lock_workflow` / `lock_job` 一样在未绑定事务时抛 `RuntimeError`。11 个调用点全部迁移。`heartbeat_assignment` 和 `finish_assignment` 原先在未显式开启事务的连接上取锁，改为包在 `self._db.transaction()` 内；`record_assignment_result` 原有的 `connection.transaction()` 改用同一写法。

#### 领域纯函数

新增 `app/domain/inference_policy.py`，沿用 [`recovery_policy.py`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/domain/recovery_policy.py) 的边界：函数不执行 SQL、不读时钟、不生成随机数。入参使用仓储已经在锁内读到的领域模型 `Job`、`Flow`、`ReviewBatch`、`WorkerRecord`，不为它们再造一套事实 DTO。

```python
MAX_INFERENCE_ATTEMPTS = 3
```

与持久化格式 [`InferenceSummary.max_attempts: Literal[3]`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/domain/inference_models.py#L121-L132) 一致；替换仓储里的两处字面量 3。

```python
class InferenceSettlement(BaseModel):  # frozen, extra="forbid"
    state: JobState
    call: InferenceJob
    failure: InferenceFailure | None
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `state` | `JobState` | 结算后的任务状态：`DONE`（无失败）、`QUEUED`（可恢复失败且还有预算）、`FAILED` |
| `call` | `InferenceJob` | 要写回 `jobs.inference` 的完整值，含更新后的 `failures`、`avoid_workers`、`excluded_workers`、`next_attempt_at`、`cancel_failure`，`FAILED` 时含 `summary` |
| `failure` | `InferenceFailure \| None` | 实际生效的失败（取消优先于上报）；其 `code` 写入 `jobs.last_error` |

```python
def decide_inference_cancellation(
    job: Job, *, flow: Flow | None, batch: ReviewBatch | None,
    batch_current: bool, now: datetime,
) -> InferenceFailure | None:
```

判断一次调用是否必须停止，返回 `cancelled`、`execution_timeout` 或 `None`。`flow` 是 `job.flow_id` 对应的行（任务有来源 Flow 而行不存在时为 `None`）；`batch` 是 `call.batch_id` 对应的批次；`batch_current` 是 `ReviewStore.current(batch)` 的结果，无批次时忽略。规则与现实现逐条相同：已记录的 `cancel_failure` 优先；来源 Flow 缺失、不再接受推理或 attempt 已变则取消；批次已指向其他任务、不再当前、已有决定或上下文摘要不符则取消；按时到达且尚未应用的结果以接收时刻比较截止时间，否则用 `now`。

```python
def decide_inference_settlement(
    job: Job, failure: InferenceFailure | None, *,
    cancellation: InferenceFailure | None, now: datetime,
) -> InferenceSettlement:
```

决定结算结果，使用 `job.state`、`job.attempt`、`job.assigned_worker_id`、`job.created_at` 和 `job.inference`。规则与现实现相同：

- 取消优先于上报的失败；两者都没有则 `DONE`，`call` 不变。
- 延迟取 `retry_after_seconds`；未提供时，`rate_limited` 与 `backend_unavailable` 为 `0.25 × attempt` 秒，其他为 0。延迟不小于剩余预算时，下次时刻取截止时间，不构造超大时间值。
- 可恢复失败：被取消或下次时刻到达截止时间则 `deadline`；`attempt` 达到 `MAX_INFERENCE_ATTEMPTS` 则 `max_attempts`；否则回到 `QUEUED`。不可恢复失败为 `non_retryable`。
- 只有运行中的任务才把失败追加到 `failures`。本次 worker 进入 `avoid_workers`；失败码为 `busy`、`connection_closed`、`rpc_timeout`（结果未知）时还进入 `excluded_workers`。
- `FAILED` 时生成 `InferenceSummary`，字段取值与现实现相同。

```python
def plan_inference_attempt(
    call: InferenceJob, *, request_id: str, now: datetime,
) -> InferenceTask | InferenceFailure | None:
```

规划一次派发：退避未到返回 `None`；原始截止时间已过返回 `InferenceFailure(code="execution_timeout")`；否则返回本次派发的任务，即冻结任务换上 `request_id`，`timeout_seconds` 为剩余预算。`request_id` 由仓储生成后传入。

```python
def choose_inference_worker(
    call: InferenceJob, task: InferenceTask, available: Sequence[WorkerRecord],
) -> WorkerRecord | None:
```

从空闲且就绪的 worker 中选出本次调用的首选者。先排除 `excluded_workers` 和能力不支持 `task` 的 worker；再按“未失败过的优先，同一层内本地 bub 优先”稳定排序。`available` 必须保持仓储查询给出的公平基础顺序（`last_inference_assigned_at NULLS FIRST, worker_id`），函数只在其上分层。调用方仅在首选者就是请求方时领取。

`app/domain/recovery_policy.py` 增加准备重试预算：

```python
class PreparationRetry(BaseModel):  # frozen, extra="forbid"
    failures: int
    first_failed_at: datetime
    next_at: datetime | None
    cause: str
    reason: str
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `failures` | `int` | 计入本次后的连续失败次数，写入 `prepare_failures` |
| `first_failed_at` | `datetime` | 重试窗口的起点，写入 `prepare_first_failed_at` |
| `next_at` | `datetime \| None` | 允许再次领取的时刻；`None` 表示预算或窗口耗尽，任务以控制端失败结束 |
| `cause` | `str` | 写入 `prepare_last_error` 的原因 |
| `reason` | `str` | 写入 `last_error` 和控制端 `Failed.error` 的说明 |

```python
def decide_preparation_retry(
    job: Job, error: GitHubError | None, *, now: datetime,
    max_failures: int, window_seconds: float,
) -> PreparationRetry:
```

规则与 `defer_preparation` 现有计算相同，继续使用 `app.github.retry_delay`。`app/domain` 已经从 `app.github` 导入值类型（`review_models.py`、`context_models.py`），不新增依赖方向；`assignments.py` 因此不再在运行时导入 `app.github`。

#### 仓储接口

移动的方法保持原名和原签名，下面只列新增、改名或改归属的部分。

`InferenceStore`（`app/persistence/inference.py`）：

```python
def enqueue_inference(self, task: InferenceTask, *, flow_id: int | None = None,
                      batch_id: int | None = None,
                      validation: Literal["text", "review", "flow_naming"] = "text") -> Job
def consume_inference(self, job_id: int) -> None
def inference_needs_cancel(self, job_id: int) -> bool
def complete_inference_result(self, job_id: int, *, failure: InferenceFailure | None = None) -> bool
def settle_inference(self, job: Job, failure: InferenceFailure | None) -> None
def recover_inference(self, worker_id: str | None = None) -> None
def sweep_inference(self) -> tuple[int, int]
def cancel_flow_inference(self, flow_id: int) -> None
```

- 前四个是原样移动。`settle_inference`、`recover_inference` 是原 `_settle_inference`、`_recover_inference`，因为跨仓储调用而去掉下划线；调用方必须已持有原来要求的锁。
- `sweep_inference` 是 [`sweep_expired_assignments` 中的推理循环](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L1186-L1204)，返回（重新排队数，失败数）；调用方已锁全部 worker 和相关 Flow。
- `cancel_flow_inference` 是 [`cancel_jobs` 的推理部分](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/jobs.py#L58-L64)；调用方已持有 workflow 锁。
- `_lock_inference` 保持私有，是推理唯一的加锁路径。

`AssignmentStore`（`app/persistence/assignments.py`）：15 个方法原样移动，另加

```python
def cancel_jobs(self, flow_id: int, *, reason: str = "Flow cancelled") -> None
```

从 `JobStore` 原样移入，推理部分改为调用 `InferenceStore(tx).cancel_flow_inference(flow_id)`，语句和顺序不变。`assign_job` 保持一个公开入口，内部分成 `_assign_inference` 与 `_assign_coding` 两个私有方法。

`FlowStore`：`get_flow_execution`、`get_worker_executions`、`get_worker_execution`、`release_flow_execution` 原样移入。

`JobStore`：移出 `cancel_jobs`、`enqueue_inference`、`consume_inference`；导出 `CURRENT_WORKSPACE_SQL`、`MAY_HAVE_EXECUTED_SQL`。

`Store`（`store.py`）：增加 `assignments`、`inference` 两个属性，随事务视图一起绑定。

#### 其他受影响的文件

- `app/persistence/stops.py`：绑定读取改用 `FlowStore`，`assignment_active` 改用 `AssignmentStore`。
- `app/controler/workers/callbacks.py`：同时持有 `WorkerStore`（会话）、`AssignmentStore`（领取与租约）、`InferenceStore`（取消检查）。
- `app/controler/runtime.py`：启动隔离用 `WorkerStore`，定期回收用 `AssignmentStore`。
- `app/controler/workflow/{coordinator,results,tools,service,inference}.py`：按上表改用对应仓储。
- `app/controler/workflow/{lifecycle,archive,effects}.py`、`app/controler/reconciliation/pull_requests.py`、`app/controler/workers/conversation.py`：绑定改经 `FlowStore`。已持有 `FlowStore` 的函数删除 `workers` 参数，`archive.py` 的函数把它换成 `flows`；`lifecycle.py` 中调用 `cancel_jobs` 的函数改收 `AssignmentStore`。
- `app/cli.py`、`app/live_smoke.py`、`scripts/smoke/recovery.py`、`tests/`：只改调用的仓储对象。
- `docs/architecture/components.md`、`docs/plan/README.md`、`CHANGELOG.md`：更新仓储职责表、计划索引和变更说明。

#### 流程与锁顺序

每个操作内的取锁语句和先后顺序不变，只是方法所在的类不同：

- **编码领取：** 锁全部 worker → 无锁读候选 → `FOR NO KEY UPDATE SKIP LOCKED` 锁 Flow → 锁后复查 → 锁 job → 创建或复用绑定 → 提交占有与 `JOB_CLAIMED`。全部在 `AssignmentStore.assign_job` 的一个事务内。
- **推理领取：** 锁全部 worker → 读空闲 worker 与排队调用 → 逐个调用：`InferenceStore.inference_needs_cancel`（嵌套事务，worker/Flow → job）→ `plan_inference_attempt` → `choose_inference_worker` → `_claim_job`。
- **回收：** 锁全部 worker → 按 id 锁相关 Flow → `InferenceStore.sweep_inference` → 锁过期的编码 job → `decide_job_recovery` → 批量更新。
- **推理加锁：** `_lock_inference` 仍是 worker 或 Flow → job，无 Flow 的通用调用只锁已分配的 worker。

#### 边界情况

- **公平顺序留在 SQL。** 候选的基础顺序包含 `ORDER BY worker_id`，使用数据库排序规则；改在 Python 中排序会按码点比较，大小写混合的 worker id 可能得到不同的并列次序。因此纯函数只做稳定分层，不重排基础顺序。
- **已知差异一：结算只读一次时钟。** 现实现在 [`_settle_inference`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L864-L865) 读一次时钟，随后 `_inference_cancellation` 再读一次；之后两个决策共用同一时刻。只有截止时间恰好落在两次读取之间的调用结果不同，间隔是同一事务内相邻的两条语句。
- **已知差异二：两处显式事务块。** `heartbeat_assignment` 和 `finish_assignment` 的语句与提交点不变，事务边界由隐式改为显式。
- **嵌套事务。** `inference_needs_cancel`、`release_flow_execution` 等在调用方事务内仍以 savepoint 运行，回滚范围不变。
- **事务绑定。** 新仓储沿用 `Repository.using()` 和 `XStore(tx)` 的现有写法；事务结束后使用绑定对象仍立即失败。

### 两项判断

#### `_ELIGIBLE_JOB` 与领域规则的重复：保留 SQL，补一个等价性测试

[`_ELIGIBLE_JOB`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L70-L85) 的状态条件重写了 [`Flow.accepts`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/domain/models.py#L147-L151)，交付条件对应 [`FlowDelivery.supersedes`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/domain/models.py#L207-L209)。

- 这段 SQL 是集合式的候选过滤：领取查询和 [`workspace_demand`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L259-L271) 在每个 worker 的每次轮询中执行，需要在数据库内连接 `flows`、`flow_deliveries`、`execution_stops` 和先前任务，并在锁住 Flow 后用同一条件复查。改成先取全部排队任务再用 Python 判断，会把整个队列及关联行拉到应用侧，锁后复查也要重写。
- 领域方法用于单行的事后检查（`authorize_dispatch`、`reject_assignment`、`defer_preparation`），各阶段的组合有意不同，[job-retirement-simplification](job-retirement-simplification.md) 已决定保留这些差异。
- 结论：两处是按用途并存的重复，不合并，也不需要改表。本次原样移动 SQL，在两处加互指注释，并新增一个 PostgreSQL 合同测试：枚举 Flow 状态 × 有无归档结论 × 交付状态 × 任务类型，断言 SQL 判定为可领取的集合等于由 `Flow.accepts` 和交付状态独立写出的期望。之后任何一侧单独变化都会使测试失败。

#### 推理留在 `jobs` 表：保留；改表方案单独提出，本次不做

`kind<>'inference'` 在 `app/` 的 8 个文件中出现 25 次，加上 `kind='inference'` 等写法共约 39 行（规划基线上用 `rg` 统计）。本次拆分不减少这个数量，只是让 `kind='inference'` 的语句和 `_lock_inference` 集中到 `inference.py`。

保留的理由：

- 这是变更 009 记录的用户决定：推理进入 `jobs` 表并共用入队、领取、身份校验、结果持久化和回收流程。
- 推理任务的 id 与编码任务共用一个序列，并出现在 wire 上：`InferenceResult`、`CancelJob`、心跳都以 `job_id` 加 `assignment_token` 标识。分表后要么共享序列，要么给协议增加类型区分，后者改变 wire。
- `workers.current_inference_job_id`、`review_batches.inference_job_id`、[`ux_jobs_inference_request`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/schema.py#L382-L397)、通知归属查询和控制台查询都引用 `jobs` 中的推理行。

**单独提出、不在本次实施的方案 P1：`inference_calls` 独立表。**

- 内容：新表保存逻辑调用（冻结任务、截止时间、尝试次数、失败记录、摘要、来源 Flow/批次），槽位指针和批次关联改指新表；`jobs` 恢复 `flow_id NOT NULL`，`JobKind.INFERENCE` 和 `Job.required_flow_id` 的收窄随之删除。
- 收益：约 39 处类型过滤和 `jobs` 上的类型 CHECK 约束消失；推理状态从一个 JSONB 字段变成有类型的列。
- 成本：建表并迁移已有推理行；结果收件箱、租约和心跳要么为新表另写一套，要么推翻“统一 Job 流程”的决定；id 空间与 wire 的关系需要重新设计；控制台和通知查询要改。
- 建议：现在不做。本次拆分完成后，推理语句集中在一个模块，届时改表的范围可以清楚估算；如果要做，应作为单独的计划重新审批，并明确是否修改变更 009 的决定。

### 方案比较

| 方案 | 取舍 |
| --- | --- |
| 只抽出纯函数，不拆类 | 满足目标 1，改动最小；但 `WorkerStore` 仍是 33 个方法的单一类，循环导入和私有锁调用不变。不采用。 |
| 拆类但保留 `WorkerStore` 作为转发外壳 | 调用方和测试不用改；但保留一个只做转发的层，方法的真实归属继续隐藏，与 persistence-simplification 拒绝转发层的决定冲突。不采用。 |
| 按表拆分（workers / flow_executions / jobs） | 领取在一个事务内同时写三张表，按表拆会拆散原子操作，persistence-simplification 已拒绝。不采用。 |
| 为绑定新建叶子仓储 `ExecutionStore` | 同样能消除 `flows`↔`workers` 循环；但多一个只有 4 个方法的类和一个 `Store` 入口，控制层仍要多持有一个对象。未采用；如果希望 `FlowStore` 不继续变大，可以改选此方案，其余设计不受影响。 |
| 推理拥有独立的领取入口 | `InferenceStore` 更自足；但与变更 009 的统一入口决定冲突，`_claim_job` 的占有提交也要复制一份。不采用。 |
| 纯函数 + 三个按职责的仓储 + 绑定归 `FlowStore` | 新增两个类、一个领域模块；消除 6 处函数内导入和全部私有锁调用。采用。 |

### 成本、风险与缓解

- **成本：** 约 25 个 `app/` 文件和约 50 个测试文件有机械性的调用改动；新增两个持久层模块和一个领域模块；`AssignmentStore` 仍有约 620 行，因为领取、租约和准备重试共用同一套 fencing 条件，再拆只会增加互相调用。
- **风险一：移动过程中改变语句或锁顺序。** 缓解：策略抽取和方法移动分成两个提交；在基线和改动后对同一组场景记录 SQL 语句序列并比较。
- **风险二：纯函数与原分支不等价。** 缓解：期望表依据本计划的规则独立写出，不从实现生成；现有数据库回归继续覆盖端到端结果；对规则做错误版本对照。
- **风险三：测试的机械改名掩盖断言变化。** 缓解：将测试 diff 中的仓储对象名归一后，应当没有剩余差异；新增的测试单独列出。
- **风险四：新仓储绑定到错误的连接。** 缓解：沿用 `XStore(tx)` 的现有写法，事务结束后的访问继续立即失败；并发回归覆盖嵌套事务路径。
- **接受的限制：** `flows.py` → `reviews` 的一处函数内导入、持久层对 wire 类型的导入以及约 39 处推理类型过滤继续存在，原因见上文和“延后工作”。

### 实施顺序

审批通过后，在同一 workspace、同一功能记录和同一 bookmark 内依次完成：

1. **记录基线。** 在隔离 PostgreSQL 上运行未改动代码的全量回归；用临时 pytest 插件记录选定场景的 SQL 语句序列（领取、推理重试与取消、准备重试、断线与启动恢复、回收、归档释放）。
2. **公开锁方法。** `Database.lock_workers()` / `lock_worker()`，迁移 11 个调用点。
3. **纯函数与测试。** 新增 `inference_policy.py` 和 `decide_preparation_retry`，先写表驱动测试，再让现有 `WorkerStore` 方法调用它们。此时类还没有拆，回归和语句序列应与基线一致（已知差异一除外）。
4. **绑定归 `FlowStore`。** 移动 4 个方法，删除 `flows.py` 的两处函数内导入，调整 `stops.py` 和控制层调用。
5. **拆出 `InferenceStore` 与 `AssignmentStore`。** 移动方法和共用 SQL 片段，`JobStore` 移出三个方法，更新 `Store`、控制层、smoke 和测试调用。
6. **结构性测试与文档。** 增加函数内导入的约束测试和 `_ELIGIBLE_JOB` 等价性测试；更新 `components.md`、计划索引、CHANGELOG 和本记录的时间线。
7. **验证。** 执行下节的全部检查与对照，随后进入代码审查门禁。

第 2–3 步与第 4–5 步分别成为一个提交，便于区分“改了规则的位置”和“搬了方法”。无数据迁移、无灰度发布；回退即回退应用版本。共享机器上只启动本项自己的测试数据库，不重启现有服务。

### 验证与简化对照

以下都是审批后才执行的计划，本记录不声称任何一项已经通过。

- **纯决策测试（新增）：** `tests/test_inference_policy.py`。结算：成功、不可恢复失败、可恢复失败的第 1/2/3 次、取消优先于成功结果、超大 `Retry-After` 截到截止时间、排队中结算不追加 `failures`、结果未知的失败码进入排除集而其他只进入避让集、摘要字段。取消：四类来源事实，以及按时到达的结果在截止后恢复应用仍然有效。候选：排除优先于避让、避让层级高于本地优先、能力不符、基础顺序保持。尝试规划：退避中、已过期、剩余预算。准备重试的窗口与次数边界并入 `tests/test_recovery_decisions.py`。
- **现有回归（不改断言）：** `test_worker_inference.py`、`test_workspace_claims.py`、`test_workspace_dispatch.py`、`test_preparation_retry.py`、`test_recovery_decisions.py`、`test_recovery_concurrency.py`、`test_workflow_transactions.py`、`test_stop_protocol.py`、`test_archive_recovery.py`、`test_store.py` 等真实 PostgreSQL 用例继续覆盖领取唯一性、fencing、锁等待后的新事实和回滚。
- **语句序列对照（临时，不提交）：** 第 1 步的记录与最终实现逐场景比较归一化后的 SQL 文本和事务边界。预期只有“边界情况”的两项差异；其他差异要么修掉，要么作为新的决定记入时间线并重新请求确认。
- **结构性测试（新增，并入 `tests/test_boundaries.py`）：** `app/persistence` 内对 `app.persistence` 的函数内导入只允许 `flows.py → reviews` 一处。
- **`_ELIGIBLE_JOB` 等价性测试（新增）：** 见“两项判断”。
- **全量检查：** 设置 `AGENTFLOW_TEST_DATABASE_URL` 后执行 `uv run pytest --no-testmon -q --tb=short -rs`，以及 `uv run ruff check app tests scripts/smoke/recovery.py`、`uv run mypy app tests`、`uv run python scripts/build_console.py`、`uv build --no-sources` 和 diff 空白检查。未配置数据库导致的 skip 不算通过。本项没有界面改动，不做截图。

**简化对照：** 在全量回归后、代码审查前执行，实验文件用完删除。本项主要是移动和抽取，新增的机制只有纯函数和模块边界，对照集中在这两点：

1. **规则的错误版本应被新测试发现：** 把排除集并入避让集；把本地优先放到避让层之上；把次数上限改为 4；去掉超大延迟的截断。每一项都应让至少一个纯决策测试失败，否则补强测试，或者重新评估这条规则是否需要独立表达。
2. **`plan_inference_attempt` 是否值得独立：** 把它内联回仓储的两处比较，对比代码量和测试覆盖。如果独立函数没有带来可验证的差别，就内联并删除。
3. **绑定留在 `WorkerStore` 的对照：** 恢复 `flows.py` 对 `workers` 的函数内导入，结构性测试应失败，证明该测试能拦住循环回归。
4. **`_ELIGIBLE_JOB` 测试的有效性：** 临时从 SQL 中去掉关闭交付的条件，等价性测试应失败。

### 待确认事项与延后工作

**审批时请确认的决定**（没有需要补充信息才能回答的阻塞问题）：

1. Flow 执行绑定的 4 个方法归 `FlowStore`，不新建 `ExecutionStore`。
2. `assign_job(slot=...)` 仍是唯一的领取入口，位于 `AssignmentStore`。
3. `JobStore` 移出 `cancel_jobs`（到 `AssignmentStore`）和 `enqueue_inference`、`consume_inference`（到 `InferenceStore`）。
4. `_ELIGIBLE_JOB` 保留为 SQL 并补等价性测试；推理留在 `jobs` 表，方案 P1 本次不做。
5. `Store` 增加 `assignments`、`inference` 入口，测试改用新入口，不保留转发外壳。

**实施中再定的问题：**

- `plan_inference_attempt` 是否保留，由简化对照 2 决定。
- `get_worker_execution` 在 `app/` 中没有调用，只有 2 处测试使用；实施时确认是否值得保留，倾向原样移动。
- 语句序列对照所选的场景清单，在记录基线时确定并写入时间线。

**延后工作（不在本 PR 范围）：**

- **让 `FlowStore.transition()` 自己读取最少事实。** 评估后认为它与本次拆分不重合，不一并做。领取、退回和回收三个调用点拆分后都在 `AssignmentStore` 内，形状不变；其中退回经过 `lock_workflow`，它本身就返回整行 Flow。实际的代价在回收：[每次扫描都加载所有活跃 Flow 的整行](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/workers.py#L1180-L1184)（含 `task_content`），而多数扫描没有任何转换。改进需要给 `transition()` 增加按 id 读取事实的第二种入参，或者新的事实类型；#171 的最后一次修订刚按要求去掉了入队接口的双重入参，这个接口变化值得单独决定。
- **`flows.py` → `reviews` 的函数内导入。** 两种做法：`FlowStore` 直接持有取消 review 的两条 SQL，或者把它放进一个被两边导入的小模块。
- **持久层对 wire 类型的导入。** 结果收件箱按原样保存有类型的 wire 载荷，`jobs.py`、`stops.py`、`reviews.py` 也是如此，并且 [边界测试](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/tests/test_boundaries.py#L97) 允许。本次只让这些导入随方法移动，不改变做法。
- **方案 P1（推理独立表）。** 见“两项判断”。

## 2. PR 变更时间线

### [001] — 2026-11-16 — `cancel_jobs` 的归属理由更正，并改名为 `retire_queued_work`

- **背景：** 审批时用户问 `JobStore` 为什么移出 `cancel_jobs`，随后提出改名并批准实现（[本线程](https://ampcode.com/threads/T-01a1e050-ae2c-7193-81c2-44c2a71f1548)）。
- **更正：** 初始计划写的理由之一“它清空 `workers.current_job_id`，属于领取与槽位的事务”不成立：留在 `JobStore` 的 [`retire_preparation`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/jobs.py#L293-L310) 和 [`complete_persisted_result`](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/jobs.py#L368-L415) 也清空同一列。移出的理由只有依赖方向：这个方法在一个事务里取消审核批次、退休编码任务、结算推理调用，后两个依赖位于 `jobs.py` 之上，留在 `JobStore` 就必须保留[两处函数内导入](https://github.com/b1indsight/agentflow/blob/c1233e2ad121ef5133ba8fe9f72720e05ec38855/app/persistence/jobs.py#L36-L37)。
- **考虑过的做法：** 整体移到 `AssignmentStore`（调用方仍是一次调用）；留在 `JobStore` 并保留两处函数内导入；`JobStore` 只留编码任务部分，由 5 个调用点各自按顺序调三个仓储（漏掉一步就是行为变化）。
- **决定（已接受，已实现）：** 移到 `AssignmentStore`，改名为 `retire_queued_work(flow_id, *, reason=...)`。它同时处理审核批次、编码任务和推理调用，并且只退休排队中和未派发的工作，原名两处都不准确。这取代初始计划“移动的方法保持原名”对这一个方法的适用；其他方法名不变。

### [002] — 2026-11-16 — 基线、语句序列的记录方法与重放

- **基线：** 未改动的 `c1233e2a` 在本项私有 PostgreSQL 上全量运行三次，结果都是 1882 passed、1 skipped（Git 后端不适用的 jj metadata 场景）。
- **语句序列：** 用临时 pytest 插件包住 psycopg 的 `Cursor.execute` 和连接级命令，按测试、按线程记录归一化后的 SQL 文本及 `BEGIN`/`COMMIT`/`SAVEPOINT`，测试 schema 名统一替换。记录范围是全量测试，取代“实施中再定的问题”里的选定场景清单。插件和对比脚本不进入仓库。
- **不稳定的序列：** 基线自身三次运行之间有 34 个测试的序列不同，都是多线程或异步轮询的端到端用例。对比时排除它们，只对它们检查是否出现基线没有的语句。
- **重放：** 实现期间 `main` 前进到 `12ab7300`（#173、#174，只改 console 前端），本项提交已重放到其上；Python 代码与基线相同。

### [003] — 2026-11-16 — 策略改为领域纯函数，公开 worker 锁方法（第一个代码提交）

- **实现：** 新增 [`app/domain/inference_policy.py`](../../app/domain/inference_policy.py)：`decide_inference_cancellation`、`decide_inference_settlement`、`plan_inference_attempt`、`choose_inference_worker` 和 `MAX_INFERENCE_ATTEMPTS`；[`recovery_policy.py`](../../app/domain/recovery_policy.py) 增加 `decide_preparation_retry`。仓储里对应的分支删除，改为读事实后调用。`Database` 提供 `lock_workers()` / `lock_worker()`，11 处私有调用全部迁移。
- **与计划一致的两项差异：** 结算只读一次时钟；心跳与完成使用显式事务块，线上语句顺序不变。
- **新增的第三项差异（已实现，请确认）：** 取消判断现在一次读全来源事实（Flow、批次、批次当前性），再交给纯函数。原实现按判断顺序短路：已记录取消时不读，Flow 不符时不读批次，批次已指向其他任务时不查当前性。因此在调用被取消的那一刻，锁内最多多出 5 条只读 `SELECT`，结果不变。保留短路需要仓储按策略的判断顺序读取，等于把策略留一半在仓储里，所以没有保留。全量对比中有 3 个测试出现这些多出的读取。
- **类型：** `InferenceSettlement` 和 `PreparationRetry` 按计划用冻结的 pydantic 模型。
- **未改动的既有行为：** `record_assignment_result` 在自己的事务里调用 `self.assignment_active(...)`；仓储未绑定事务时，这次读取走连接池里的另一条连接。结果不受影响（本事务没有改动它读的行），但每次结果入库会同时占用两条连接。这是基线行为，本项没有改，留给后续决定。
- **新测试：** [`tests/test_inference_policy.py`](../../tests/test_inference_policy.py)（不需要数据库）和 [`tests/test_recovery_decisions.py`](../../tests/test_recovery_decisions.py) 里的准备重试用例，期望值按本计划的规则独立写出。

### [004] — 2026-11-16 — 按职责拆开 `WorkerStore`（第二个代码提交）

- **实现：** 33 个方法按初始计划的对照表移动，方法体内只改所属的类、共用 SQL 片段的名字和 [001] 的改名。`assign_job` 仍是唯一入口，内部分成 `_assign_inference` 和 `_assign_coding`。新增 `InferenceStore.sweep_inference`、`cancel_flow_inference` 两个从原方法里拆出的片段。`Store` 增加 `assignments`、`inference` 入口。
- **结果：** `workers.py` 240 行、`assignments.py` 782 行、`inference.py` 297 行。`AssignmentStore` 比计划估计的约 620 行多，原因是领取、租约、准备重试的 SQL 原样保留。`app/persistence` 内的函数内导入从 7 处降到 1 处（`flows.py` → `reviews`），`_lock_worker(s)` 不再存在。
- **调用方：** 控制层按方法归属改用对应仓储。`archive.py` 的函数把 `workers` 参数换成 `flows`；`lifecycle.py` 的 `pause_delivery`、`close_delivery` 把 `jobs` 换成 `assignments`，`record_merge`、`adopt_created_pull_request` 把 `workers` 换成 `assignments`；`live_smoke` 的 `observe` 去掉只用于读绑定的 `workers` 参数；`conversation.py`、`effects.py`、`pull_requests.py` 不再构造 `WorkerStore`。
- **测试改动：** 现有测试只改调用的仓储对象、上述参数名和三处 monkeypatch 的目标类，断言不变。新增两个合同测试：`test_claim_eligibility_matches_the_domain_dispatch_rules`（枚举 7 种可存储的 Flow 阶段 × 5 种交付状态 × 3 种任务类型，比较实际领取的集合与 `Flow.accepts`、`FlowDelivery.supersedes` 的结论）和 `test_persistence_stores_import_each_other_at_module_level`。
- **文档：** 更新 [`components.md`](../architecture/components.md) 的策略说明和仓储表、[`roadmap.md`](../architecture/roadmap.md) 的目录说明、计划索引和 CHANGELOG。

### [005] — 2026-11-16 — 语句序列对比、简化对照与最终检查

- **语句序列：** 在拆分提交上全量运行带记录插件的测试，与两次按实际发送顺序（隐式 `BEGIN` 在语句之前）记录的基线比较。没有任何测试发出基线从未出现过的 SQL 文本。按测试比较时有 10 个测试的序列不同：3 个是 [003] 记录的取消事实多读；其余 7 个是端到端或轮询用例，差异只是整轮轮询语句多一次或少一次，与基线之间本来就存在的 33 个不稳定用例同类。锁语句的种类与先后关系没有变化。
- **简化对照：** 用临时脚本每次改一处机制，运行新增合同测试和改动前就有的数据库回归，跑完恢复文件。

  | 错误版本 | 新增测试 | 原有回归 |
  | --- | --- | --- |
  | 排除集当作避让集 | 失败 | 120 项通过 |
  | 所有失败都排除 worker | 5 项失败 | 1 项失败 |
  | 本地优先放在避让层之上 | 失败 | 120 项通过 |
  | 次数上限改为 4 | 失败 | 1 项失败 |
  | 去掉超大 `Retry-After` 的截断 | 失败 | 120 项通过 |
  | 尝试规划忽略退避 | 失败 | 120 项通过 |
  | 尝试规划给出完整超时 | 失败 | 120 项通过 |
  | `flows.py` 恢复对 `workers` 的函数内导入 | 结构性测试失败 | 不适用 |
  | 领取 SQL 去掉“交付已关闭”条件 | 失败 | 127 项通过 |
  | 领取 SQL 允许 finalizing 的编码任务 | 失败 | 127 项通过 |

  每个错误版本都至少让一个新增测试失败；10 项中有 8 项原有回归全部通过，说明这些规则以前没有便宜的测试。`plan_inference_attempt` 只有一个调用点，但它的两条规则只有新增的纯测试能发现，所以保留为独立函数，不内联（计划“实施中再定的问题”第一项的结论）。
- **合同测试改用合法事件：** `test_claim_eligibility_matches_the_domain_dispatch_rules` 原先用 SQL 直接改写 Flow 的状态和归档结论，与 #171 “测试准备通过合法事件，不绕过状态写入口”的约定不符。现改为在 `flows.locked()` 内依次提交事件（含 `archive_round`）建立 7 种阶段，并断言六种状态都实际出现。上表最后两项在改写后重新运行，结果相同。同时修正了它的三处类型错误。
- **与本项无关的不稳定用例：** 曾有两次全量运行与另一次全量运行同时在本机执行，`tests/test_project_sync.py` 的两三个用例失败（Projects 期望状态和截止时间）。这些用例不涉及本项改动的代码；单独运行该文件 3 次均全部通过，单独的全量运行也全部通过。
- **检查：** `uv run ruff check app tests scripts/smoke/recovery.py`、`uv run mypy app tests`、`uv run python scripts/build_console.py`、`uv build --no-sources` 通过，wheel 内含新模块和 console 资源；diff 无行尾空白。

### [006] — 2026-11-16 — 发布前复验与审查

- **全量回归：** 对将要发布的精确提交 [`ae1db41f`](https://github.com/b1indsight/agentflow/commit/ae1db41f84c0864c1b4e3c3e78c08399b06dc33f)，在本项私有 PostgreSQL 上执行 `uv run pytest --no-testmon -q --tb=short -rs`：全部通过，唯一的 skip 与基线相同（Git 后端不适用的 jj metadata 场景），唯一的 warning 是既有的 Starlette/httpx 弃用提示。基线 `main` 为 `12ab7300`，发布前 `jj git fetch` 确认未前进。
- **审查门禁：** 两个代码提交分别通过仓库的 review gate（功能/安全与复杂度两项只读审查）：[`c1988f6f`](https://github.com/b1indsight/agentflow/commit/c1988f6f9530ee3c455d0676a856e02480fa6c24)（策略下沉）和 `ae1db41f`（拆分），两者都没有 findings。bookmark 设到门禁返回的 `ae1db41f` 后推送；本条记录是其后的纯文档提交。
- **交付状态：** 已推送到 PR #172，CI 由 PR 正常触发。未合并、未部署；无数据库迁移。
- **遗留限制：** 见初始计划“延后工作”：`flows.py` → `reviews` 的一处函数内导入、持久层对 wire 类型的导入、约 39 处推理类型过滤（方案 P1）、`FlowStore.transition()` 读取最少事实，以及 [003] 记录的 `record_assignment_result` 在未绑定事务时多占一条连接的基线行为。

