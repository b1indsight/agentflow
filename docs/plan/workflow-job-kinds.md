# Workflow 按任务类型归组 — 功能记录

建议把 INITIAL、REVISION、ARCHIVE 各自的任务准备和结果转计划逻辑放进对应模块，公共事务和恢复边界留在原入口；`retry` 保留共同的生命周期检查，不按类型拆成三套。

**阶段：** 计划已批准，开始实施；实现与验证进度见时间线。
**PR：** 正在创建计划草稿，发布后沿用同一 PR 实施。
**Bookmark：** `refactor/workflow-job-kinds`。
**需求：** [本线程](https://ampcode.com/threads/T-01a1e42f-8b95-748b-abf5-d8da1f7582c2)，承接[架构审查的第 3 项](https://ampcode.com/threads/T-01a1df4f-9c18-7322-8a3a-1b1153ba4a2d)。
**规划基线：** 2026-11-16 fetch 后的 `main@origin`，[c73f756c](https://github.com/b1indsight/agentflow/commit/c73f756cb1a0d8f1300e4217bfb523252a71406b)，已含 #171 和 #172。独立 jj workspace：`.amp/in/workflow-job-handlers/workspace`；不改 default workspace 的文件。

下列源码行号均指规划基线。规划咨询使用本机 Claude Code / Opus 5.5，只接收显式提供的源码材料；初始化确认 tools 和 mcp_servers 均为空，正常退出。主代理核对了其建议及补充依赖，咨询不算测试结果。

## 1. 初始计划

### 动机、范围和验收

- **现状：** [`JobCoordinator.prepare`](../../app/controler/workflow/coordinator.py#L324-L558) 在一个入口里交织三类任务；[`ResultApplication._prepare`](../../app/controler/workflow/results.py#L347-L564) 同时构造首轮交付、修订反馈和归档的计划。REVISION 准备被分成三段，跨段共享 batch、threads 和远端事实。
- **已有归属：** [`initial.py`](../../app/controler/workflow/initial.py)、[`revisions.py`](../../app/controler/workflow/revisions.py)、[`archive.py`](../../app/controler/workflow/archive.py) 已分别持有类型专属校验。扩展这些模块，避免再建一套 handler 目录、注册表或统一接口。
- **具体例子：** 调整“answer 只核对原 PR head、不同步分支”的行为时，维护者应主要阅读 `revisions.py` 的准备、远端核验和计划构造；coordinator/results 只展示公共检查与一次类型分派。
- **范围：** `workflow/{coordinator,results,preparation,initial,revisions,archive}.py`；新增共用计划构造函数模块 `workflow/plans.py`；必要的现有测试调用迁移和组件职责文档更新。
- **成功标准：** `prepare` 不再内嵌三类任务的具体算法；`_prepare` 只保留通用前置检查、修订批次身份绑定和类型分派，成功结果的具体转换各归对应模块。类型模块不反向依赖 coordinator/results/service，不接受这些服务对象作为上下文。
- **行为标准：** 公共入口、错误类型与消息、任务与持久计划内容、effect key/顺序、读取重试预算、锁与事务边界、plugin 时机均保持一致。以同场景基线对照和现有回归验证，不以函数行数作为唯一验收。

**不做：** 不改 schema、wire 协议、`ResultPlan` 格式、推理调度、领取或状态机；不改 `_apply` 的恢复流程、`_commit` 的原子提交或 `ResultEffects` 的写入协议；不添加基类、Protocol、Mixin、注册表、通用上下文 DTO、转发兼容外壳或新的配置项。命名推理的提示词和 JSON schema 原文搬移，不借重构修改指令。

### `retry` 保留共同编排

[`ControlService.retry`](../../app/controler/workflow/service.py#L290-L398) 当前依次处理 archived 通知、pending stop、closed/merged delivery、未应用结果、Issue 冲突、运行中执行及收尾。最终按失败 job 的 kind 入队，只有 REVISION 额外执行 `reviews.retry(previous, replacement)`。归档校验已经调用 `archive.py`。

因此建议本次不改 `service.py`。按三类 job 复制或分派整个 retry 会打散这些共同检查，INITIAL 和 ARCHIVE 的新接口也缺少独立职责。保留以下行为：

- 已归档 Flow 只能重试已有合并通知，不能重新执行编码；重新开轮仍要求显式 resume。
- 已核验 revision/archive 的 GitHub effect 失败，只重新启用失败 effect；不重新运行 worker。
- 未确认停止、未结清执行和不确定的远端写入继续拒绝重试；拒绝代码及事务回滚不变。

依据：[通知重试](../../tests/test_archive_status.py#L140-L191)、[修订通知恢复](../../tests/test_revision_recovery.py#L403-L489)、[事务回滚](../../tests/test_workflow_transactions.py#L196-L242)。这是相对原审查建议的一项范围判断，随本计划一起批准。

### 模块职责和接口

公共服务的构造函数与入口签名保持不变；内部私有方法搬走后直接迁移调用，不保留仅转发的旧方法。

#### `preparation.py`：共用读取与准备检查

保留现有异常和 `PreparationState`，迁入 coordinator 的 `_read`、`_task_content` 和准备退休判断。新增一个具体读取类，避免为泛型读取函数引入回调 Protocol：

```python
type FailJob = Callable[[Job, str], Awaitable[None]]

def preparation_retired(flow: Flow, job: Job, *, deliveries: DeliveryStore) -> bool: ...
def required_delivery(flow: Flow, *, deliveries: DeliveryStore) -> FlowDelivery: ...
def required_pr_number(flow: Flow, *, deliveries: DeliveryStore) -> int: ...

class PreparationReads:
    def __init__(self, database: Database, settings: ServerSettings) -> None: ...
    async def read[T](self, job: Job, operation: Callable[[], Awaitable[T]]) -> T: ...
    async def task_content(self, job: Job, flow: Flow, repository: Repository) -> str: ...
```

- `PreparationReads._database: Database` 提供现有时钟；`_settings: ServerSettings` 保存同一配置引用，每次 read 仍按原位置计算剩余窗口，不缓存秒数。
- `read` 的超时继续转换为 `PreparationReadFailure`，不吸收取消、不负责签发失败或持久重排。
- `required_delivery` / `required_pr_number` 原样迁入准备入口所用的两个检查，供 triage 和 revision 共用。结果阶段现有的不同报错不合并到这两个函数。
- `FailJob` 仅表示持久化控制失败这一个跨所有者操作，绑定现有 `JobCoordinator.fail`；不暴露整个 coordinator。

#### `initial.py`：首轮准备、命名和结果计划

```python
class InitialPreparation:
    def __init__(
        self, github: GitHubApp, *, database: Database,
        reads: PreparationReads, fail: FailJob,
        apply_result: Callable[[int], Awaitable[JobState]],
    ) -> None: ...
    async def prepare(self, job: Job, flow: Flow) -> InitialTask | JobState: ...
    def _ensure_naming(self, flow: Flow, job: Job, task_content: str) -> int: ...
    async def _await_naming(self, job: Job, flow: Flow, naming_job_id: int) -> str | JobState: ...

def initial_plan(
    base: ResultPlan, flow: Flow, job: Job, attempt: Attempt,
    result: InitialTaskResult, *, deliveries: DeliveryStore,
    in_review_label: str, reviewers: Sequence[str],
) -> ResultPlan: ...
```

- 类的字段为 `_github: GitHubApp`、`_database: Database`、`_reads: PreparationReads`、`_fail: FailJob`、`_apply_result: Callable[[int], Awaitable[JobState]]`，以及从同一 database 构造的 `_flows: FlowStore`、`_deliveries: DeliveryStore`、`_jobs: JobStore`、`_inference: InferenceStore`。沿用服务自行构造仓储的既有模式，不传入一份仓储装配清单。
- 在 `prepare` 的 INITIAL 分派处分配这个小对象；不持久缓存当前 job/flow，也不增加 Runtime 装配要求。三个方法共享命名、数据库和结果应用依赖，类只负责首轮准备。
- `_ensure_naming`、`_await_naming` 和 `BRANCH_SLUG_PATTERN` 原样迁入；结果应用的推理校验从此处导入同一个常量。命名结果应用只通过窄回调，不反向导入 `ResultApplication`。
- `initial_delivery_mismatch` 原签名、原逻辑保留，lifecycle 继续复用它。
- `initial_plan` 接收事务绑定的 DeliveryStore；只构造计划，不发 GitHub 请求、不执行 effect。

#### `revisions.py`：修订准备、远端预检和计划

```python
async def prepare_revision(
    job: Job, flow: Flow, attempt: Attempt, *, github: GitHubApp,
    reads: PreparationReads, flows: FlowStore, deliveries: DeliveryStore,
    reviews: ReviewStore, settings: ServerSettings, fail: FailJob,
) -> RevisionTask | JobState: ...

async def verify_revision_result(
    job: Job, *, github: GitHubApp, database: Database,
    flows: FlowStore, deliveries: DeliveryStore, result_store: ResultStore,
) -> str | None: ...

def revision_plan(
    base: ResultPlan, flow: Flow, job: Job, attempt: Attempt, batch: ReviewBatch,
    result: RevisionTaskResult, revision_error: str | None, *,
    deliveries: DeliveryStore, flows: FlowStore, reviews: ReviewStore,
    in_review_label: str, reviewers: Sequence[str],
) -> ResultPlan: ...
```

- `prepare_revision` 合并现有三个 REVISION 分支，保持其中检查和 I/O 顺序；`freeze_revision` 已确认返回 `RevisionTask`。保留 answer/revise 的现有差异。
- `verify_revision_result` 迁入 `_check_revision`，保留已有 plan 时跳过、持久验证 backoff 和 `ResultPending`，调用位置仍在 `_apply` 的事务之前。
- `revision_plan` 先检查 batch 和有序 feedback refs，再处理 exit、session、delivery、binding、消费反馈与 review effects。原 `frozen_revision`、`revision_mismatch` 和远端验证函数继续复用。
- 仅用一次的 summary `CommentEffect` 直接在 `revision_plan` 构造，不给它新增通用包装函数。

#### `archive.py`：清理任务及控制通知的计划

```python
async def prepare_archive(
    job: Job, flow: Flow, *, flows: FlowStore, deliveries: DeliveryStore,
    jobs: JobStore, fail: FailJob,
) -> ArchiveTask | JobState: ...

def merge_notice_plan(
    base: ResultPlan, flow: Flow, job: Job, notice: MergeArchiveNotice, *,
    activity: ActivityStore, done_label: str, in_review_label: str,
) -> ResultPlan: ...

def archive_plan(
    base: ResultPlan, flow: Flow, job: Job, result: ArchiveResult, *,
    deliveries: DeliveryStore, flows: FlowStore, jobs: JobStore,
    done_label: str, in_review_label: str,
) -> ResultPlan: ...
```

复用 `archive_task`、`verify_archive`、`verify_merge_notice`、`archive_effects`。清理回执与无 worker 的合并通知保持分开的校验合同；`prepare_archive` 在原事务退出后才把 ValueError 转为控制失败。

#### `plans.py`：仅放多处使用的计划构造

```python
def failed_plan(base: ResultPlan, reason: str) -> ResultPlan: ...
def installation_id(flow: Flow) -> int: ...
def review_effects(
    flow: Flow, number: int | Literal["created_pr"], *,
    in_review_label: str, reviewers: Sequence[str],
) -> list[Effect]: ...
def delivered_pr_number(flow: Flow, *, deliveries: DeliveryStore) -> int: ...
```

分别原样迁入 `_failure`、`_installation`、`_review_effects` 和结果阶段的 `_pr_number`。消费者是 results 和类型模块；这些函数不执行网络请求、不管理提交。不放到 contracts，以免把审核标签和 reviewer 的业务策略带入持久化模型层。新模块不增加类、数据字段或持久格式。

### 保留公共编排和依赖方向

- `JobCoordinator.prepare` 先读取 Flow、判断准备退休；INITIAL 直接分派；其余仍先验证 attempt，再分派 REVISION/ARCHIVE。缺少 Flow 返回 FAILED，退休返回 DONE；缺 attempt 的非 INITIAL 仍走控制失败。
- `prepare_for_assignment` 继续拥有配置选择、capability 签发、I/O 后的 token/epoch/lease 复查与 `AssignmentArguments` 构造；`_prepare_task` 继续拥有读取失败的持久预算。gateway 的发送前授权不迁移。
- `ResultApplication._prepare` 保留解码、base plan、退休及失败的顺序；在成功结果处分派到各类型 plan 函数。revision 批次绑定需要早于 Failed 处理，留在公共前置区并明确说明原因，不为了消除一次判断增加 preparation DTO。
- `ResultApplication` 仍拥有逐 job 锁、已冻结 plan 复用、effects 执行后的再次加锁、原子提交与完成标记；`_commit` 及 hooks 不按类型拆分。
- coordinator 可导入 results 和类型模块；results 导入类型模块与 plans；类型模块可导入 preparation/plans、review 的纯计划函数和仓储。preparation/plans 不反向导入任何类型模块或公共服务。

**现有边界的有意调整：** [`test_boundaries.py`](../../tests/test_boundaries.py#L90-L120) 当前禁止 `initial.py` 的所有 controller 运行时导入，因为它只做纯校验。扩展职责后只允许同包的 `preparation`、`plans` 两个叶子模块；配置类型只放 TYPE_CHECKING。把该条检查改为精确允许这两个模块，其余 controller 导入仍禁止，同时对新叶子模块检查不能反向导入公共服务。保留 lifecycle/review/worker 的现有边界，不通过延迟导入或忽略类型错误绕过循环。

### 必须保持的顺序与边界情况

1. **准备事务：** GitHub 读取、命名等待和 capability 签发都不持有数据库事务。每次冻结任务前的 Flow/attempt、delivery、binding、token、started/result/lease 检查不因搬移减少。
2. **修订准备：** batch 当前性检查、必要的 inline threads 读取、轮数检查、PR/ref/task content 读取、最终锁内复查保持原序。binding/assignment 变化返回 QUEUED，context 基线失效先失效批次再返回 DONE，不重新解释成失败。
3. **通知优先：** `MergeArchiveNotice` 在 terminal/旧 round 退休判断之前校验和构造计划。它没有 worker receipt，不能流入普通 archive result 校验；无 installation 时仍冻结空 effects。
4. **失败批次：** terminal/旧 round 的短路在读 batch 前；后续退休用重绑后的 batch attempt；batch 当前性检查在 Failed 前。批次过期的 FAILED plan 目前不带 BLOCKED 字段，不统一替换为 `failed_plan`。
5. **冻结与恢复：** plan/digest、effect key 和列表顺序不变。已有 plan 不重读远端、不按新配置重算；不确定写入只对账，确定拒绝才可重试。
6. **可观察时机：** 保留 `_pr_number` 在远端读取 lambda 中的读取位置；不顺手用先前 delivery 快照替换。通知和 plugin hook 仍在原提交之后调用，原提前返回的分支不补发通知。

依据：[准备实现](../../app/controler/workflow/coordinator.py#L147-L558)、[结果前置与原子提交](../../app/controler/workflow/results.py#L207-L564)、[归档校验](../../app/controler/workflow/archive.py#L16-L115)、[已收敛的退休边界](job-retirement-simplification.md#L110)。

### 备选方案、成本与风险

- **仅在原类中提取三个私有方法：** 改动更少，但同类任务仍跨 coordinator/results 两处拥有具体算法，未达到按类型归组的目标。
- **三类 Handler 加统一接口：** 三种准备依赖不同，ARCHIVE 不需要 GitHub 读取，REVISION 需要 batch，INITIAL 会创建 attempt。统一它们需要额外通用参数或空实现；不采用。
- **传整个 coordinator/results：** 容易搬移，却保留原私有依赖和反向耦合；不采用。
- **全部改为函数：** 适用于 revision/archive；INITIAL 的准备、命名入队与等待反复共享一组依赖，使用一个具体类。共享读取类负责实际的有界 I/O，不是配置数据袋。
- **只拆结果转换：** 可作为可验证的中间步骤，但完整交付还必须拆准备逻辑。
- **接受的成本：** 新增两个无继承的具体类、一个共用计划模块和两项窄回调能力；类型模块会变大，lifecycle 导入初始校验时会加载更多叶子依赖。用单向依赖、真实导入检查和分阶段回归控制成本，不承诺总行数减少或性能改善。
- **主要风险：** 改变错误优先级、提前/延后读取配置、把远端 I/O 移入事务、改变旧 plan 的恢复语义。差分比较和现有并发/回滚测试分别保护这些边界；若需要改协议或锁定粒度，先追加计划修订，不借重构实施。

### 实施顺序

1. 在最新 main 上再次核对差异；用隔离 PostgreSQL 跑相关既有场景并采集临时基线。批准前不写实现或测试。
2. 先迁共享计划构造与命名常量，再逐类迁结果转换及 revision 远端预检。验证后再开始准备路径，避免同时改恢复编排和任务构造。
3. 扩展 preparation，迁 ARCHIVE、REVISION，最后迁 INITIAL 与命名；删除废弃私有方法和 imports。公共服务构造函数不变，Runtime 无需增加新依赖参数。
4. 调整现有边界检查和一个私有核验调用点，保持其行为断言；更新 `docs/architecture/components.md` 的职责说明及本记录时间线。无需改数据库、数据迁移或协调协议升级。
5. 完成同场景对照、简化比较和全量检查后，按既有 code-bearing push 审查门禁，沿用同一 bookmark/PR 发布。回滚应用代码即可，不回写持久计划或结果历史。

### 验证和简化比较

**保留的测试：** 本次是纯重构，优先不增加永久行为测试，也不因新函数名逐个新增测试。现有 [`test_revision_recovery`](../../tests/test_revision_recovery.py#L637-L675) 直接调用 `_check_revision`，迁为调用 `verify_revision_result`，不改损坏证据必须拒绝且不改写 inbox 的断言；边界测试按上述职责调整。发现真实公共合同覆盖缺口时再说明并补最小场景。

**相关回归：** `test_coordinator`、`test_preparation_retry`、`test_result_recovery`、`test_revision_recovery`、`test_revision_verification_retry`、`test_revision_delivery`、`test_archive_status`、`test_archive_recovery`、`test_archive_worker_recovery`、`test_workflow_transactions`、`test_console_api`、`test_pr_lifecycle`、`test_cancel_drain`、`test_boundaries`。

**临时基线对照：** 同一冻结时钟、固定 ID 和脚本化 GitHub 场景，分别执行基线与改动后代码，比较公开 prepare/apply/retry 的 task/state/拒绝码、持久 plan 与有序 effect、batch/attempt/delivery/binding 最终状态。代表场景为 INITIAL 命名成功/超时，REVISION answer/revise 与准备失败后 retry，远端读取期间 assignment/context 变化，通知在已归档或新轮次后的恢复，确定拒绝和不确定写入的区别，以及事务提交故障后重放。只归一化已识别的随机 ID 与时刻，保留 round、attempt、digest 及它们的关联；并发场景比较因果关系和最终结果，不把不确定调度顺序当合同。

**简化/消融：** 实现后、自动代码审查前，在临时副本比较“仅迁结果”和“完整按类型迁移”的入口职责；再比较 INITIAL 模块函数显式传依赖与具体类的实际调用复杂度。使用同一组代表场景，输出必须一致，若类或 helper 没有减少共享依赖传递就删除它。无需引入运行时开关或性能基准；退休、fence、冻结 plan、网络核验是既有合同，不能以消融结果为由随意删除。临时插件、脚本和变体运行后删除，只保留记录和必要日志。

**最终检查：** 使用真实隔离 PostgreSQL 跑 `uv run pytest -q --tb=short`，不能把数据库用例跳过当通过；运行 `uv run ruff check app tests`、`uv run mypy app tests`、项目打包构建和 diff 检查。构建沿用 CI 的 `python scripts/build_console.py`、`uv build --no-sources`；只验证打包，不改 UI。

**已执行与未执行：** 规划期已 fetch、确认基线及 #172 已合入、核对上述源码/调用方/测试约束并校验规划咨询执行信息；没有运行应用回归、实施对照或构建，不宣称重构已通过验证。

### 审批和延后工作

本计划整体待批准，重点请审阅：`retry` 不改；两个具体准备类和共用 `plans.py` 的边界；`initial.py` 扩展职责后仅允许两个叶子依赖。推荐按本文实施，无需另选框架。

具体私有字段命名、临时对照采集方式可在实现时决定，不影响公共合同。`_commit` 的按类型后果提交、Flow 状态机、连接回调拆分和推理表设计均延后；不承诺本次让新增 job 只改一个文件。

## 2. PR 变更时间线

<!-- 初始计划待审批；仅在需求或设计发生实质变化、或实现交付时追加条目。 -->
