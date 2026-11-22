# Flow 状态转换收敛

将当前六态 Flow 的运行期状态决策收敛为领域事件函数，既有行的状态写入收敛到 `FlowStore.transition()`。公共上下文负责事务、按既有顺序取锁、读取最新 Flow 和比较轮次/attempt；各阶段保留自己的退休、权限和错误处理规则。

**状态：** 已按审批实现单写入口和公共锁定上下文，完成真实 PostgreSQL 全量回归、静态检查、打包及简化对照。发布审批、PR、审查及 CI 的最新状态见[本线程](https://ampcode.com/threads/T-01a1df73-b16a-7222-9765-8009b73f83d5)和验证附件。

**PR / Bookmark：** `refactor/flow-state-transitions`；同一功能记录、同一 bookmark、同一个 PR。PR 链接及发布结果在[本线程](https://ampcode.com/threads/T-01a1df73-b16a-7222-9765-8009b73f83d5)更新。

**需求与审批：** [本线程](https://ampcode.com/threads/T-01a1df73-b16a-7222-9765-8009b73f83d5)。按要求由本线程直接编写计划，不另开线程，审批后再实现。

**规划基线：** `main@origin` 的 [45b190c0](https://github.com/b1indsight/agentflow/commit/45b190c072a67e7ec4aeac123f8083caedef0590)，已含 #169；规划期间执行 `jj git fetch --remote origin`，远端未变化。独立 jj workspace 为 `.amp/in/flow-state-writer/workspace`，不编辑 default workspace 的工作副本。

## 1. 初始计划

### 目标与范围

目前，控制层通过修改 `Flow.state` 再调用通用 updater 落库，job/worker 仓储还直接执行状态 SQL；`update_flow()` 同时承担元数据和状态写入，单纯关联 attempt 也会回写旧状态。转换权限、条件性 no-op 和归档后果因而分散在多层。[现有 updater 与恢复新轮](../../app/persistence/flows.py#L79-L130)、[入队投影](../../app/persistence/jobs.py#L118-L162)、[worker 回收](../../app/persistence/workers.py#L1171-L1257)。

本项完成后：

- 调用方提交“job 已领取”“结果失败”“清理结果已提交”等内部事件，不提交任意目标状态；领域层完整定义六态的合法结果、no-op 和拒绝条件。
- 当前六态运行期的 `UPDATE flows ... state` 全部由 `FlowStore.transition()` 执行；创建 Flow 的 INSERT 从同一领域函数取得初态，不能硬编码另一套规则。
- 普通单 Flow 路径共用锁定上下文；领取与批量回收复用已经取得的锁和事实，保持 worker → Flow → job 顺序、批量 Flow 按 id 取锁和 `SKIP LOCKED`。
- 外部行为、六态名称、数据库结构、Activity 证据、持久化 ResultPlan、wire 协议以及远端 GitHub 操作保持不变。

不改 Job/Delivery/Worker 各自的状态机，不重做调度器，不增加状态历史表、schema migration、版本号或通用命令总线。只迁移本项状态写入及直接支撑它们的上下文和元数据调用，不追求清除全仓库每一次 `lock_workflow()`。

运行期约束涵盖 `app/` 和使用当前仓储的 smoke 调用。[四态离线迁移脚本](https://github.com/b1indsight/agentflow/blob/6600d34dbf14e22f76d835405dd8742ac3f902f5/scripts/migrate_flow_states.py#L1-L27) 处理旧 schema、保留原始证据并要求全停机，保持冻结，不执行它，也不让旧状态通过六态运行期策略。

### 纯领域函数定义完整事件语义

新增 `app/domain/flow_policy.py`，沿用 [recovery_policy.py](../../app/domain/recovery_policy.py#L1-L76) 的纯决策边界。只新增一个内部 `FlowEvent(StrEnum)` 和一个函数，不新增事实 DTO、事件 payload 模型或目标状态参数：

```python
def decide_flow_state(
    state: FlowState | None,
    event: FlowEvent,
    *,
    archive_reason: ArchiveReason | None,
) -> FlowState | None:
    ...
```

`state=None` 只表示尚未 INSERT 的初态，不表示找不到已有 Flow。`archive_reason` 是锁内 Flow 携带的结论；请求结束前仍由现有业务代码验证并准备 reason/summary，合并时仍可将已有结论改为 succeeded，resume 则由事件明确要求清空。函数不读取时间、job、delivery、plan 或数据库，不执行归档资格判断。

返回 `FlowState` 表示执行写入，包含有意的同态写入；返回 `None` 表示既有条件性 no-op，不能刷新 `updated_at` 或触发归档副作用。未列为合法结果的事件/来源组合抛 `ValueError`；非法调用不得部分落库。

下表定义 `FlowEvent` 的全部成员及结果。`—` 是条件性 no-op，`×` 是非法调用；目标与来源相同仍执行原有写入。Q/R/V/B/F/A 分别代表 queued/running/review/blocked/finalizing/archived。

| FlowEvent | Q | R | V | B | F | A | 附加条件或含义 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `CREATED` | × | × | × | × | × | × | 仅 `state=None` 且无结论时得到 Q。 |
| `JOB_ENQUEUED` | — | — | Q | — | — | — | 非 ARCHIVE 入队；有结论则 no-op。ARCHIVE 入队不发此事件。 |
| `JOB_CLAIMED` | R | — | R | — | — | — | 有结论则 no-op；不能把 ARCHIVE 的 F 改成 R。 |
| `ASSIGNMENT_REQUEUED` | — | Q | — | — | — | — | 有结论则 no-op；显式退回和 sweep 的轮次条件留在各自资格判断中。 |
| `EXECUTION_UNCERTAIN` | B | B | B | B | B | — | 保留已有结论；回收不能重新打开 A。 |
| `RESULT_FAILED` | B | B | B | B | B | × | 普通失败、ARCHIVE 失败及 effects 失败恢复；各阶段已有资格检查继续生效。 |
| `DELIVERY_BASELINE_REJECTED` | B | B | B | B | × | × | PR 源分支校验失败，须无归档结论。 |
| `DELIVERY_APPLIED` | V | V | V | V | × | × | 已接受的当前结果提交 delivery，须无归档结论；保留 B 中的结果恢复。 |
| `FINALIZATION_REQUESTED` | F | F | F | F | F | × | 必须已有本次验证过的归档结论；用于结束、未合并关闭及合并观察。 |
| `CLEANUP_APPLIED` | × | × | × | F | F | × | 已验证 ARCHIVE 清理结果，必须有归档结论；先回到 F，随后才检查最终归档。 |
| `RETRY_REQUESTED` | × | × | × | Q / F | × | × | B 无结论到 Q，有结论到 F；其他状态仍由 retry 现有分支返回或拒绝，不发送此事件。 |
| `ROUND_ARCHIVED` | × | × | × | × | A | × | 必须有结论；store 还须验证本轮不可变 Activity 证据。 |
| `ROUND_RESUMED` | × | × | × | × | × | Q | 同一条 UPDATE 递增轮次并清空 attempt、结论、summary、archived_at。 |

转换合法不等于允许执行 job。`Flow.accepts()` 继续决定派发资格，`Flow.retires()` 继续只判断现有的轮次/结论/delivery 淘汰事实；它们不被状态表取代。[现有领域方法](../../app/domain/models.py#L147-L159)。

### FlowStore 成为既有行状态的唯一写入者

在 `app/persistence/flows.py` 提供：

```python
def transition(
    self, flow: Flow, event: FlowEvent, *, now: datetime | None = None,
) -> Flow:
    ...

def update_flow_metadata(self, flow: Flow) -> None:
    ...
```

`transition()` 消费同一事务内已锁定的最新 Flow，先调用领域策略，再执行参数化 SQL。它不隐式获取 worker/Flow/job 锁；普通路径由下节上下文取得锁，领取和 sweep 直接传入预锁事实。未绑定事务的调用应明确失败，不能悄悄开一个独立事务。`now` 供 claim/sweep 等复用操作开始时已捕获的时刻，默认继续使用数据库时钟。

状态 UPDATE 保留当前轮次的比较，并用来源状态约束写入；读取或传递旧快照不能覆盖锁内新状态。这里使用刚重读的 Flow 作来源，不额外把旧 job/plan 的轮次、attempt 或退休条件加进通用写入器。no-op 不写库；同态写入保留当前的时间戳更新。写入成功后保留现有调用对内存 Flow 的可见更新；`start_round()` 仍返回新轮对象并保留旧轮快照。

- 普通转换在同一 UPDATE 中持久化目标状态及该分支原有的结论/时间字段；不替元数据调用顺手回写状态。
- F/A 的 review cancel、A 的 Activity 证据要求迁入此入口，仍处于原事务中。普通状态变化不新增 Activity 记录。
- `start_round()` 保留业务 API，状态 SQL 移到 `transition(..., ROUND_RESUMED)` 的专属分支，不能拆成“先变 queued，再清 archived_at”。Issue reservation、active owner 检查、binding release、delivery/cursor reset 和 owner 选择仍留在当前事务与原顺序。
- `archive_round()` 仍先追加本轮不可变归档 Activity，再提交 `ROUND_ARCHIVED`；证据与 A 同事务成功或回滚。
- 删除 `update_flow()` 这个通用状态写入口，改用 `update_flow_metadata()` 只更新 installation/current_attempt 和 updated_at，保留轮次比较。归档元数据随状态事件落库；[历史 merge 纠正](../../app/persistence/flows.py#L302-L325) 的已验证结论更新保持原样，不借它改变 state。
- [coordinator 关联 attempt](../../app/controler/workflow/coordinator.py#L350-L363) 和 [smoke setup](../../scripts/smoke/recovery.py#L145-L150) 改用元数据接口。测试 setup 通过合法事件建立场景，不保留 test-only `set_state()` 或任意状态 bypass。

创建 INSERT 仍归 `create_flow()` 所有，通过 `decide_flow_state(None, CREATED, archive_reason=None)` 取得 Q；已有行状态 SQL 只能出现在 `transition()` 内。这样，INSERT 的初始合法性和 UPDATE 的转换合法性共用一个定义。

### 公共上下文合并取锁和当前性比较

仍放在 `app/persistence/flows.py`，避免为小上下文新增模块、仓储集合或服务层：

```python
@dataclass(frozen=True)
class LockedFlow:
    database: Database
    flow: Flow | None

    def matches(self, *, round_no: int, attempt_id: int | None) -> bool:
        ...

@contextmanager
def locked(self, flow_id: int) -> Iterator[LockedFlow]:
    ...  # FlowStore 的方法
```

`database` 是本次事务绑定的 Database；`flow` 是 `lock_workflow()` 在等待、重新发现 owner 后读到的最新事实，缺失时保留 `None` 供原调用方决定返回还是报错。`matches()` 只比较存在性、轮次和 attempt，`attempt_id=None` 是精确比较，不是通配符。只需比较轮次或 attempt 的位置继续显式检查，不能为使用公共方法而增加另一项前置条件。

`locked()` 包含现有 `transaction()` 和 `lock_workflow()`；复用 Database 的嵌套事务、回滚、过期仓储检查和 owner 重新发现逻辑，不实现另一套锁算法。调用方从 `context.database` 绑定自己实际需要的仓储，随后才锁 job；网络 await 仍在事务之外。[原有事务与锁实现](../../app/persistence/database.py#L55-L132)。

优先迁移 workflow 的 service/coordinator/results/effects/lifecycle 和 PR reconciliation 中与本项写入、异步前后复查直接相关的重复块。退休判断与阶段处理留在原调用点：

- `Flow.retires()`、`Flow.accepts()`、effects `_plan_stale()` 各保留自己的用途，不合成 `allow_terminal/allow_archive/skip_attempt` 等开关。
- result prepare 的前后短路、effects 的远程 I/O 前后复查、attempt 不匹配时的 FAILED、已退休结果仅保存执行事实、既有异常类别及先后顺序不变。
- [effects 失败后的 BLOCKED 恢复分支](../../app/controler/workflow/results.py#L234-L258) 有意没有完整的 round/retirement 判断，不能强制套 `matches()` 或 `retires()`。
- retry/resume 先取得的 Issue advisory reservation 仍在 workflow 锁之前；上下文不接管 Issue 身份发现。

### 领取、入队和批量回收保留原有取锁方式

领取继续先锁 worker、按既有公平策略用 `FOR NO KEY UPDATE SKIP LOCKED` 选择 Flow，再锁 job。将锁住的 Flow 传到状态入口，不在 job 锁之后发现或获取新 worker/Flow 锁。[领取路径](../../app/persistence/workers.py#L430-L477)。

入队保留 job INSERT 与 review→queued 的原子性、source-key 幂等性。为 `JobStore.enqueue_job()` 增加 keyword-only `locked_flow: Flow | None = None`：有锁调用方传入同一事务的 Flow；独立调用缺省时在 INSERT 前使用公共上下文。这个参数只传递已经持锁的事实，不豁免校验；不能把“已绑定事务”直接当作“已经锁住 Flow”。需要传递事实的现有 service/coordinator/reviews 和 merge-notice 调用一并调整，避免入队在 job INSERT/冲突行锁之后重新取 workflow 锁。

sweep 仍先锁全体 worker，再按 Flow id 锁相关 Flow，最后读取/锁定 job；在既有 Flow 锁查询时取得可复用的 Flow 快照，按唯一 Flow 聚合事件。保留批量 job UPDATE，Flow 事件按现有“先 requeue、再 blocked”的顺序应用，同一 Flow 不为每个 job 重复写入，不增加逐 job 查询或逐 job 事务。

显式 `requeue_assignment()` 原有 job round 与 Flow round 检查继续保留；sweep 的 Flow requeue 原 SQL 没有该条件，本项不补上。`EXECUTION_UNCERTAIN` 保留 F→B、B→B 和 A no-op，不能将回收写入统一改成“仅 running 可 blocked”。[显式退回](../../app/persistence/workers.py#L1115-L1150)、[sweep 原 SQL](../../app/persistence/workers.py#L1223-L1246)。

### 结果与归档各自的提交边界保持不变

[持久化 ResultPlan](../../app/contracts/result_models.py#L95-L118) 的 `flow_state` 字段与恢复格式保持原样。当前生产者生成 None/BLOCKED/ARCHIVED；在结果应用边界将已有结果解释为 `RESULT_FAILED`、`CLEANUP_APPLIED` 或无状态事件，不能把该字段直接作为目标状态传给 store，也不能提供任意目标的兜底事件。

`flow_state=ARCHIVED` 的清理结果仍先进入 F，完成 result marker 后再经 `try_archive()` 检查并归档；交付成功再发 `DELIVERY_APPLIED`。B 仍可恢复到 V，不能因为派发拒绝 B 而拒绝已接收结果的恢复。[现有结果提交顺序](../../app/controler/workflow/results.py#L623-L669)。

合并观察继续在证明 delivery 身份后写 succeeded/F、请求 stop、取消排队任务、记录通知意图并立即写归档 Activity/A；原 worker 的清理独立进行。不能把 [record_merge()](../../app/controler/workflow/lifecycle.py#L258-L297) 改为等待普通 `try_archive()` 的所有 executor/effects 结清条件。已归档 merge 的证据纠正仍走原分支，不能重新打开本轮。

### 替代方案与成本

| 方案 | 取舍 |
| --- | --- |
| 只保留 `transition(flow, target_state)` 并检查边集合 | 接口仍要求各调用方决定目标，retry 与清理恢复的业务含义继续分散；不采用。 |
| 在 `Flow` 模型上加 setter，保留各处 SQL | 无法约束 job/worker 仓储的裸 SQL，也不能集中归档证据与事务副作用；不采用。 |
| 所有调用统一进入“取锁、过滤、退休、写状态”的大函数 | 会改变 result 各阶段短路和错误语义，领取/sweep 也不能后置重新取锁；不采用。 |
| 数据库 trigger 或新状态历史表 | 引入 schema/运维成本，且跨入纯领域策略与应用副作用的边界；本项不采用。 |
| 事件纯函数 + FlowStore 写入 + 小型锁定上下文 | 增加一个 enum 和一个双字段上下文，删除散落的目标选择和事务样板；采用。 |

主要成本是迁移跨模块内部调用和测试 setup。事件表将保留当前有意的同态写入及 no-op，合法状态较普通线性流程更多；不能为得到漂亮的线性状态图而删除恢复路径。批量状态写可能由两条 UPDATE 变为每个受影响 Flow 的写入，验收时比较 SQL/事务数量，禁止退化为每个 job 一次完整 workflow 加锁。

### 实施顺序

审批通过后，在同一 workspace、同一功能记录和同一 bookmark 内依次完成：

1. 记录未改动基线的相关数据库回归结果及可复用行为快照，覆盖状态、轮次、attempt、结论、归档证据和 job/result 标记。
2. 实现领域事件表与 FlowStore 状态入口，收拢初态、resume 和 archive；同步拆除通用状态 updater，迁移直接相关的测试 setup。保持这一阶段行为等价。
3. 迁移控制层状态写入和持久化结果解释，再迁移 enqueue、claim、requeue、sweep 的 SQL；按原锁顺序传递预锁事实与捕获时间。
4. 引入公共锁定上下文，替换本项相关的重复事务/重读/相同当前性比较，保留阶段策略及不等价的检查。
5. 执行下述针对性回归、全量验证和简化对照；检查没有遗留运行期状态旁路，补齐同一记录中的实测证据。通过审批后的实现不自动授权 push、PR 发布、合并或部署。

无数据迁移、wire 升级或新旧格式双写。最终按一个完整变更交付，不能发布新旧状态写入方式混用的中间版本；回退应用版本无需改库。共享机器上只启动本项测试资源，不重启现有服务、不写生产数据。

### 验证与简化对照

以下均为审批后的拟执行项，本计划不声称已经通过。

- **纯决策合同：** 用独立写出的表覆盖六态/事件、结论有无、初态；期望不能从被测实现的映射生成。重点区分 B 无结论 retry→Q 与 B 有结论 retry→F、B 结果恢复→V、F 失联→B、A 回收 no-op、非法非 A resume 和 ARCHIVE 领取不退出 F。
- **状态写入合同：** 扩展现有 `test_flow_states.py` / `test_store.py`，验证非法事件完整回滚、resume 原子递增与字段清空、Activity 与 A 原子性、review cancel、元数据更新不能改变状态，以及 no-op 不刷新时间而既有同态写入仍刷新。
- **结果与生命周期：** 重用 `test_result_recovery.py`、`test_revision_recovery.py`、`test_archive_status.py`、`test_flow_archive_activity.py`、`test_pr_lifecycle.py`、`test_preparation_retry.py`。保留冻结 plan 的恢复、attempt 不符失败、旧轮/退休结果只留执行事实、B 中恢复已接受结果、merge 立即业务归档且旧 binding 仍待清理。
- **真实 PostgreSQL 并发：** 重用 `test_recovery_decisions.py`、`test_recovery_concurrency.py`、`test_workflow_transactions.py` 和现有领取并发用例，以 barrier/event 固定交错，验证等待后读到新 receipt/owner、领取与结果/sweep 不死锁、跳过已锁候选、失败时 job 与 Flow 一起回滚。GitHub I/O 保持 recording fake，不做远端写入。
- **单写所有者与批量边界：** 在现有 `test_boundaries.py` 补充当前运行期状态 SQL 的所有者约束，临时审计残留 `.state =` 与旧 updater；不靠变量名或固定 helper 调用顺序断言业务正确。对相同批量输入比较最终事实和 SQL/事务数量，不将逐 job 查询带入 sweep。
- **测试 setup：** 优先扩展现有文件；只保留保护上述合同且能发现真实错误的测试。现有断言原则上不变，因移除通用状态 updater 而必要的准备数据路径调整单独核查，不能通过改变预期掩盖行为漂移。
- **全量检查：** 在隔离测试库设置 `AGENTFLOW_TEST_DATABASE_URL` 后执行 `uv run pytest --no-testmon -q --tb=short`，并运行 `uv run ruff check app tests`、`uv run mypy app tests`、`python scripts/build_console.py` 和 `uv build --no-sources`，核查 wheel 内的 console 产物以及 diff 空白错误。不能以未配置数据库导致的 skip 代替通过。[测试库隔离机制](../../tests/conftest.py#L66-L89)、[CI 检查与构建](../../.github/workflows/ci.yml)。本项无 UI 外观变更，不增加截图或 UI 重设计。

在全量回归后、代码审查前执行临时对照并删除实验文件：

1. **省掉事件合法性检查：** 对非 archived 的 resume 和 archived 上的普通结果写入使用放宽版本；合同用例应失败，证明测试能发现“只是集中 SQL、没有约束转换”的错误。
2. **省掉锁后重读或精确当前性比较：** 在原有并发用例中让 receipt/attempt 在等待期间变化；错误版本应产生被用例发现的旧事实提交。若某个新增上下文方法没有独立维护价值，就简化或删除它。
3. **将 merge 统一走普通结清归档：** 用仍有原 executor/binding 的已证明合并场景比较；错误版本不得满足立即业务归档合同。保留不同归档资格判断的理由由此验证。

### 审批点与后续边界

本计划没有需要补充产品信息才能决定的阻塞问题。请审批上述事件语义、单写入口和保留的锁定/恢复边界；批准后才修改产品代码与测试。

实现时可以调整私有函数拆分和上下文覆盖范围，以更少代码实现相同合同；若发现必须改变状态表、结果资格、归档时机、wire/schema 或锁顺序，先记录差异并重新请求审批。新事件审计表、CAS 版本列、全仓库锁 API 改造、离线历史迁移改写及部署均不在本项范围。

## 2. PR 变更时间线

### 2026-11-15：领域事件与状态写入收拢

- **触发与背景：** 原通用 updater 混合状态和元数据，jobs/workers 的 SQL 另行选择目标状态；关联 attempt 的旧快照也可能回写状态。
- **实际实现：** [flow_policy.py](../../app/domain/flow_policy.py) 定义完整事件决策；[FlowStore](../../app/persistence/flows.py) 的 `transition()` 成为既有行状态的唯一运行期写入者。创建共用初态策略，resume 在一条 UPDATE 中推进轮次并清空旧字段，archive 保持 Activity 与状态原子提交；删除通用 updater，元数据接口不再写 state。
- **决策与后果：** 采用初始计划的事件边界，不接收任意目标状态；状态写入要求事务，并比较源状态与轮次。保留条件性 no-op、同态更新时间及 F/A 的 review cancellation；不添加新表、协议或事件日志。ResultPlan 仍保留旧格式，在提交边界解释 None/BLOCKED/ARCHIVED，其他结果明确拒绝。
- **验证：** 新状态合同先在原代码上失败，再由事件实现通过；现有状态、归档证据、结果恢复及生命周期回归继续检查原行为。所有测试准备改走合法事件或纯元数据接口，未引入任意 `set_state()` 后门。

### 2026-11-15：复用锁内事实，保留阶段和批量边界

- **实际实现：** `FlowStore.locked()` 复用既有 `transaction()` 与 `lock_workflow()`；`LockedFlow.matches()` 精确比较轮次和 attempt，包括 None。workflow 和 PR 对账共用上下文；只比较一项的阶段、effects 失败恢复、retry/resume 的 Issue reservation 保留原先判断与顺序。
- **已保留边界：** 领取先锁 worker、以 `SKIP LOCKED` 取得 Flow，再锁 job；入队接收已锁 Flow，独立入队在 INSERT 前取得上下文；sweep 在原批量 Flow 锁查询中取得事实，按 Flow 去重并依旧先 requeue、再 block。没有逐 job 新事务或后置 workflow 重锁。
- **决策与后果：** 不把派发资格、退休判断、effects 当前性及 merge 的业务归档合并成一个总闸。已接受的 B 状态结果仍能恢复到 V；合并仍立即写 succeeded/F 和 Activity/A，不等原执行器清理。
- **并发证据：** 新 PostgreSQL 回归固定一次等待 Flow 锁的交错，读者必须看到等待期间提交的 blocked 与新 attempt；PR 远程读取期间发生 attempt 变化时，旧查询不能提交归档。现有 receipt、owner 重新发现和跳过已锁候选回归继续覆盖原锁算法。

### 2026-11-15：简化对照与批量代价

- **设置：** 仅在本项私有 PostgreSQL 的临时 schema 中实验。临时 pytest plugin 每次替换一个机制，产品文件不变；实验脚本和命令保存在本线程验证附件，临时可执行文件在结束时删除。
- **事件合法性：** 放宽非 A resume 与 A 普通失败后，事件矩阵及非法转换事务回滚用例失败，出现应拒绝而未拒绝的写入；保留事件校验。
- **锁后事实与当前性：** 改用锁前快照后，并发回归读到 queued 而非新提交的 blocked；仅比较 round 而忽略 attempt 后，PR 旧查询错误地产生归档通知。保留公共上下文的锁后重读与精确比较。
- **归档阶段差异：** 将 merge 改经普通 `try_archive()` 后，有活跃执行器的既有合并回归停在 finalizing，未满足立即 archived 的合同；保留已证明合并的直接归档路径。
- **批量对照：** 原 `45b190c0` 与当前实现对相同事实执行 sweep：覆盖未开始、已执行、带结论 finalizing、archived、同 Flow 混合 requeue/block 及已收到结果。比较完整 Flow/job 行、归档证据及返回值，序列化摘要一致。相同 6 个 Flow 的 7/175 个 job 批次中，原实现均为 9 条客户端 SQL、2 条 Flow UPDATE；当前均为 12 条 SQL、5 条 Flow UPDATE。两版均只有 1 个事务和 1 次批量 Flow 锁查询，无单 Flow `lock_workflow()` 调用。
- **决定：** 接受按受影响 Flow/事件增加写入的代价，以维持唯一决策与状态写入入口；job 数增加没有带来逐 job 查询或事务。不为本项增加另一套批量状态 API。此项比较 SQL 数量与最终事实，不声称吞吐或时延改善。

### 2026-11-15：最终本地验证与交付边界

- **验证完成：** 在隔离 PostgreSQL 上执行 `AGENTFLOW_TEST_DATABASE_URL=postgresql://127.0.0.1:15479/flow_state_test uv run pytest --no-testmon -q --tb=short -rs`，全量通过。唯一 skip 是 Git 后端组合不适用的 jj metadata 保留场景，并非缺少数据库。现有 Starlette/httpx 弃用警告保持原样。
- **其余检查：** `uv run ruff check app tests scripts/smoke/recovery.py`、`uv run mypy app tests`、`uv run python scripts/build_console.py`、`uv build --no-sources` 及 diff 空白检查通过；wheel 内的 console HTML/CSS/JS 已核验。保留现有 Vite 大 chunk 提示，本项没有 UI 改动。
- **写入审计：** AST 合同及额外函数级审计确认既有行的两条状态 UPDATE 均位于 `FlowStore.transition()`；旧 updater 和 jobs/workers 状态 SQL 不再存在，四态离线迁移保持冻结。
- **测试准备修正：** 首次全量发现 PR backlog fixture 在锁内重读后仍比较旧的 queued 对象；现改为 sweep 前保存实际 blocked 快照，保持“不完整 merge 不变”的业务断言。最终完整重跑通过。
- **证据与限制：** 原基线、先失败合同、最终全量/构建日志、简化实验源码与命令、批量事实摘要均见[线程验证附件](https://ampcode.com/artifacts/flow-42a79e9c-57a4-40b5-a113-123c02376cdf)。未进行 GitHub 写入、真实远端 smoke 或部署；未为本项启动其他线程或代理。
- **交付状态：** 产品、测试、changelog 和本记录留在独立 jj workspace 的当前工作变更。default workspace 仍为原来的空变更；本项私有 PostgreSQL 已停止，临时实验代码及基线副本已清理。尚未执行发布前 review gate、创建 bookmark/PR、推送或合并。

### 2026-11-16：发布基线重放

- **触发与实际变更：** 用户请求推送并创建 PR 时，远端 main 已合入仅涉及 console 前端的 #170。将本项完整工作变更无冲突重放到 [aaf1c272](https://github.com/b1indsight/agentflow/commit/aaf1c2729a487558cf29955c58aec226697830c7)，状态机、锁及结果恢复实现未另作修改，default workspace 的工作副本保持不动。
- **验证与发布边界：** 发布前复验及仓库要求的功能/安全、复杂度双项只读审查结果保存在[线程验证附件](https://ampcode.com/artifacts/flow-42a79e9c-57a4-40b5-a113-123c02376cdf)；只将门禁返回的精确提交设为同名 bookmark 并推送。CI 由 PR 正常触发，不手动触发部署或合并。

### 2026-11-16：入队改用单一 Flow 入参

- **触发与决策：** 用户要求去掉 `flow_id` 与可选 `locked_flow` 的双重入参，并更新同一 PR。已采用 `enqueue_job(flow: Flow, kind, payload, ...)`，取代初始计划“领取、入队和批量回收保留原有取锁方式”中的可选参数及独立入队自动加锁方案；其他事件、锁和恢复边界保持不变。
- **实际实现：** 删除入队内部自动加锁与递归调用。service 的 create/resume/retry、coordinator 的 triage、review claim 和 merge notice 均传入同一事务中的最新 Flow；新建 Flow 可直接使用同事务创建的对象。入队只负责 job INSERT 和状态事件，保留原 round 子查询、source-key 幂等性、时间戳及 ARCHIVE 不发送入队事件的行为。
- **合同与代价：** 调用方负责先取得 workflow 锁并使用锁后事实；事务 guard 只拒绝未绑定事务的调用，不声称能证明 Flow 对象的锁归属或新鲜度。不新增锁凭证、缓存或布尔开关。同一事务重取已持有的行锁本身不构成死锁；保持既有 worker → Flow → job 顺序，是为了避免不同事务相互等待尚未持有的锁。
- **调用迁移：** 测试准备统一复用 `tests/flow_fixtures.py` 中的真实 `flows.locked()` 上下文，smoke 与测试服务也显式绑定该事务；未改变原有业务断言或引入运行期兼容包装器。
- **验证与简化：** 未绑定事务的 INITIAL/ARCHIVE 合同先失败后通过；既有入队回滚、创建/resume/retry、review、合并和并发领取回归通过。此次移除双模式及递归，没有新增实质机制，因此无需新增 ablation；最终全量回归、静态检查、构建与本轮审查结果见[线程验证附件](https://ampcode.com/artifacts/flow-42a79e9c-57a4-40b5-a113-123c02376cdf)。
