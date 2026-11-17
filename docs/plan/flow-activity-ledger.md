# Flow 事实账本 — Feature record

让 `console_activity` 中的 Flow 事实行成为 Flow 状态的唯一可信账本，`flows` 作为它的同事务状态表；
其他状态库保持不变。

**Stage:** Implementation — 已按 [001] 的修订实现并通过真实 PostgreSQL 全量回归；未部署。
**PR / Bookmark:** `docs/flow-activity-ledger`，独立 jj workspace `.amp/in/flow-activity-ledger`；[PR（按 head 查询）](https://github.com/b1indsight/agentflow/pulls?q=is%3Apr+head%3Adocs%2Fflow-activity-ledger)。
**讨论：** [架构讨论](https://ampcode.com/threads/T-01a1dea7-36fc-73a9-954d-184ef4e2dd53)。
**基线：** `main` / [847f03cbcae8](https://github.com/b1indsight/agentflow/commit/847f03cbcae8)（PR #165），2026-11-15。
**目标架构：** [Flow 事实账本与状态表](../architecture/flow-ledger.md)，本记录不重复其中的数据分类和事实目录。

## 1. Initial plan

### 摘要与动机

- **问题与现状：** Flow 的状态由多处直接写入 `flows`。经
  [`FlowStore.update_flow`](../../app/persistence/flows.py) 的模型写入分布在
  [`lifecycle.py`](../../app/controler/workflow/lifecycle.py)、
  [`results.py`](../../app/controler/workflow/results.py)、
  [`service.py`](../../app/controler/workflow/service.py) 和
  [PR 对账](../../app/controler/reconciliation/pull_requests.py)；另有直接的 `UPDATE flows` 语句
  位于 [`jobs.py`](../../app/persistence/jobs.py)、[`workers.py`](../../app/persistence/workers.py)、
  `results.py` 和 [`coordinator.py`](../../app/controler/workflow/coordinator.py)。
  状态变化的记录由[数据库触发器](../../app/persistence/console_schema.py)事后生成：它保存视图的前后快照，
  不知道是哪条业务命令、因为什么引起变化。只有归档、合并证据和归档更正由业务事务写成不可变事实。
- **具体例子：** 一个 Flow 从 `running` 变成 `blocked`。今天要判断原因，需要对比触发器快照并去
  `jobs` 里猜是结果失败、租约到期还是 PR 分支变化。没有办法验证 `flows` 当前行是否与历史一致，
  也无法发现某处绕过业务规则的直接写入。
- **受影响的人：** 运维 Flow 的 owner，以及修改状态机的开发者。
- **方案概述：** 每次改变 `flows` 的业务列，都在同一事务内先向 `console_activity` 追加一条带类型和
  起因的 Flow 事实，再更新 `flows`；全部写入收敛到一个入口。提供重放与校验命令，证明账本与状态表一致。

### 目标、范围与成功标准

- **目标与成功标准：**
  1. `flows` 的业务列只经一个写入口改变；`app/` 其他位置没有 `UPDATE flows` 或 `INSERT INTO flows`。
  2. 每次状态变化恰好对应一条事实，带 `cause` 和相关的 `job_id` / `pr_number`；条件不成立的转换不写事实。
  3. 对任意 Flow 重放事实，结果与 `flows` 行在[比较列](../architecture/flow-ledger.md#8-重放与校验)上一致。
  4. 现有 PostgreSQL 回归在不修改业务断言的前提下通过；控制台、API、Worker 协议和 GitHub 行为不变。
  5. 含存量 Flow 的数据库可以启动，并通过校验。
- **范围：** 只覆盖 `flows` 表；包括事实种类、写入口、纯投影函数、重放校验命令、存量快照和相应文档。
- **非目标：** 重建 Job、交付、反馈、Worker 等表；把协调数据写入账本；新建事实表或改表名；
  自动修复不一致；改变控制台时间线展示；停止生成触发器 `state` 行。
- **约束：** schema 只有幂等建表脚本，没有迁移框架；Server 更新器带删列检查，本方案不删列、不改列类型。
  仓储是同步接口，事务与锁由 `Database` 提供。真实 E2E 尚未验收，状态机行为不能有可见变化。

### 拟议行为

- **前后对比：** 以“租约到期后重排”为例。
  - 现在：`sweep_expired_assignments` 用一条 `UPDATE flows SET state='queued' WHERE state='running' AND …`
    修改多个 Flow；触发器为每个 Flow 追加一条 `state` 快照。
  - 之后：清扫按 `flow_id` 升序逐个调用写入口。每个确实处于 `running` 的 Flow 得到一条
    `kind=queued`、`cause=assignment_requeued`、带 `job_id` 的事实，并在同一事务更新为 `queued`；
    已经不在 `running` 的 Flow 不写任何行。`flowtool flow history` 可以直接读到这条事实。
- **错误情形：**
  - 事实追加失败或投影违反表约束：整个业务事务回滚，状态和事实都不留下。
  - 事实字段不合法（缺少必需的 `metadata` 字段、轮次不符）：写入口报错，由调用方按现有方式处理。
  - 试图更新或删除事实行：数据库触发器拒绝。
  - 校验发现差异：命令列出 Flow、列名、重放值与当前值，以非零状态退出，不修改数据。
- **兼容性：** 不新增列和表。新增事实种类、扩大不可变触发器范围、新增两个部分唯一索引，均为幂等的
  追加式变更。旧版本 Server 读不到新种类也不受影响，因为时间线和业务检查不读取它们；
  回滚到旧版本后新事实行保留，旧代码继续直接写 `flows`，此后这些 Flow 的校验会失败，
  需要重新补 `snapshot`（见风险）。

### 技术设计与涉及模块

- **涉及模块与职责：**
  - `app/domain/flow_facts.py`（新增）：事实类型与纯投影函数，不执行 I/O。
    - `FlowFactKind(StrEnum)`：事实种类，取值为 `created`、`snapshot`、`round_started`、`queued`、
      `running`、`review`、`blocked`、`finalizing`、`archive`、`merge`、`archive_correction`、`titled`。
      职责是让写入、重放和数据库约束使用同一组名字。
    - `FlowFact(BaseModel)`（frozen）：一条事实，与账本行一一对应。字段：
      - `kind: FlowFactKind` — 事实种类；
      - `round_no: int` — 所属轮次，大于零；
      - `occurred_at: AwareDatetime` — 业务时间，取事务时钟；
      - `metadata: dict[str, str | int | bool | None]` — 该种类的字段，必含 `cause`，
        各种类的字段见[事实目录](../architecture/flow-ledger.md#6-事实目录)；
      - `body: str = ""` — 仅 `created` 与 `snapshot` 使用，保存 `task_content` 原文。
    - 不为每个种类单独建类型：账本行的 `metadata` 本身是扁平 JSON，现有三种事实已经这样读写；
      各种类的必需字段由 `apply_fact` 校验。
  - [`app/persistence/activity.py`](../../app/persistence/activity.py)：账本读写。现有 `append_flow`
    由接收 `FlowFact` 的 `append_fact` 取代；对“每轮至多一条”的种类，保留重复写入相同内容返回
    未新增、内容冲突报错的行为。
  - [`app/persistence/flows.py`](../../app/persistence/flows.py)：`flows` 的唯一写入口和重放入口；
    删除 `update_flow`。
  - [`app/persistence/console_schema.py`](../../app/persistence/console_schema.py)：扩大不可变触发器
    范围；为 `created`、`snapshot`（每 Flow 一条）和 `round_started`（每轮一条）增加部分唯一索引；
    增加存量 `snapshot` 的幂等补录语句。
  - `app/persistence/jobs.py`、`workers.py`，`app/controler/workflow/` 下的 `lifecycle.py`、
    `results.py`、`service.py`、`coordinator.py`，以及 `app/controler/reconciliation/pull_requests.py`：
    把现有写入点改为调用写入口，业务判断不变。
  - [`app/cli.py`](../../app/cli.py)：新增 `flowtool flow verify`。
  - `tests/test_boundaries.py`：新增“只有 `flows.py` 写 `flows`”的检查。
- **接口与流程：**
  - `apply_fact(flow: Flow | None, fact: FlowFact) -> Flow | None`
    纯函数。`flow` 为空只接受 `created` 和 `snapshot`。返回应用事实后的新 Flow；前置条件不成立
    返回 `None`；事实缺少必需字段或轮次不符时抛出 `ValueError`。在线写入和重放共用。
  - `replay(flow_id: int, facts: Iterable[FlowFact]) -> Flow`
    纯函数。从最后一条 `created` 或 `snapshot` 开始依次 `apply_fact`；任一步返回 `None` 即报错，
    因为账本里不应有未生效的事实。
  - `ActivityStore.append_fact(self, flow_id: int, fact: FlowFact) -> bool`
    在当前事务内锁定 `console_flows` 行、分配 `version` 并插入事实行；返回是否新增。
  - `ActivityStore.facts(self, flow_id: int) -> list[FlowFact]`
    按 `version` 升序读取该 Flow 的全部事实行，排除 `state`、`baseline`、`inference_exhausted`。
  - `FlowStore.record(self, flow_id: int, kind: FlowFactKind, metadata: dict[str, str | int | bool | None], *, expected_round_no: int | None = None) -> Flow | None`
    唯一写入口，步骤见[写入路径](../architecture/flow-ledger.md#7-写入路径)。
    `expected_round_no` 用于只对指定轮次生效的转换（现有 `AND current_round_no=%s` 条件）；
    轮次不符或前置条件不成立时返回 `None`。进入 `finalizing` 或 `archived` 时沿用现有行为退休活动反馈批次。
  - `FlowStore.create_flow(...)`、`start_round(flow)`、`archive_round(flow, delivery)`、
    `observe_archived_merge(...)`：签名不变，内部改为经 `record` 或 `append_fact` 写入。
  - `FlowStore.set_current_attempt(self, flow_id: int, attempt_id: int) -> None`、
    `FlowStore.set_title_job(self, flow_id: int, job_id: int) -> None`
    更新两个指针列，不产生事实。
  - `FlowStore.replay(self, flow_id: int) -> Flow`
    读取事实并调用领域层 `replay`，供校验命令和测试使用。
  - `flowtool flow verify [FLOW_ID]`
    比较重放结果与当前行；不带参数检查全部 Flow。
- **边界情形：**
  - 合并：一个事务内先后写 `finalizing` 和 `archive` 两条事实。
  - 已归档后收到合并证据：只写 `merge` 和可能的 `archive_correction`，不改变状态和 `updated_at`。
  - `blocked` 且已有 `archive_reason`（归档 Job 失败）：重试写 `finalizing`，结论保持原值。
  - 命名结果晚到：`titled` 的前置条件仍由调用方按 `title_job_id` 判断，不成立时不调用写入口。
  - 同一事务内对同一 Flow 多次调用写入口：行锁已持有，`version` 依次递增。

### 备选方案与理由

- **方案：**
  1. 不改：继续用触发器的 `state` 快照当历史。
  2. 用触发器从 `flows` 的 UPDATE 自动生成带类型的事实，不改写入点。
  3. 本方案：业务事务显式写事实，单一写入口。
  4. 新建独立的 `flow_events` 表承载事实。
  5. 完整事件溯源：所有表都由账本重建。
  6. 数据库强制：`flows` 上加触发器，没有本事务事实就拒绝更新。
- **取舍与选择：**
  - 方案 1、2 的方向都是“状态表在先、记录在后”。方案 2 改动最小，也能重放出状态序列，
    但拿不到起因，而且任何绕过业务规则的直接写入都会被记成合法事实，账本不能用来检验状态表。
  - 方案 4 的边界更干净，但需要另一套版本号分配和不可变保护，还要迁移现有三种事实；
    只管 Flow 时收益不足以抵消这些工作，留作后续。
  - 方案 5 要把租约、心跳、游标等协调数据也写入账本或排除在外。写入则噪声淹没业务事实，
    排除则“全部可重建”不成立；正确性所依赖的唯一约束和行锁仍然在状态表上。
  - 方案 6 约束最强，但要靠事务级会话变量在应用和触发器之间传递信息，调试和测试成本高。
    首期用边界测试加校验命令达到同样目的，保留以后加数据库强制的可能。
  - 选择方案 3：它是让账本成为可信来源所需的最小改动，并且归档路径已经按这个方式工作。
- **既有工作：** [Job 结果审计](job-result-audit.md)当时排除了“通用事件表”，理由是 Job 的审计事实
  已经保存在结果里。本方案与它不冲突：不新建事件表，Job 审计仍以 Job 结果为准，账本只引用 `job_id`。

### 代价、风险与缓解

- **代价：** 约二十处写入点需要改为调用写入口；每次状态转换多写一行；账本表名和外键归属不理想
  （见[架构文档](../architecture/flow-ledger.md#12-代价与边界)）；事实字段一旦写入就要长期兼容。
- **风险与限制：**
  1. 改写写入点时改变了状态机行为。真实 E2E 尚未验收，回归只能依靠自动化。
  2. 遗漏某个写入点，账本与状态表从此不一致。
  3. 批量清扫改为逐个 Flow 写入后，单次清扫变慢。
  4. 回滚到旧版本后，旧代码直接写 `flows`，新版本再次上线时这些 Flow 校验失败。
  5. 账本只覆盖 `flows`，不能回答 Job 和交付层面的“为什么”。
- **缓解：**
  1. 写入点逐类迁移，每一类迁移后运行全量 PostgreSQL 回归；前置条件直接取自现有 SQL 条件。
  2. 边界测试禁止其他模块写 `flows`；测试夹具在每个用例结束时对全部 Flow 运行校验。
  3. 清扫的候选数量受现有批次限制；实施时测量一次清扫的耗时并记录。
  4. 实施时评估能否让启动补录识别“最后一条事实之后 `flows` 行又被改过”的 Flow，并为它们再补一条
     `snapshot`；做不到时，在升级说明中写明回滚后的人工处理步骤。
  5. 接受该限制；交付和 Job 事实属于后续计划。

### 实施顺序

1. 领域层：`FlowFactKind`、`FlowFact`、`apply_fact`、`replay`，以纯函数测试覆盖每种事实和前置条件。
2. 持久层：`append_fact`、`facts`、`record`、`replay`；schema 的触发器、索引和 `snapshot` 补录。
   现有三种事实改走 `append_fact`，行为不变。
3. 测试夹具加入全量校验。此时尚未迁移的写入点会使校验失败，用它列出待迁移位置。
4. 按类迁移写入点，顺序为：创建与新轮次 → 结果应用 → 生命周期与服务 → 派发、入队和清扫 → PR 对账
   → 标题。每类之后全量回归。
5. 删除 `update_flow`，加入边界测试。
6. CLI `flow verify`、`flow history` 的说明、架构文档中的“目标”改为“当前”、CHANGELOG。
- **迁移与回滚：** 上线即幂等补录 `snapshot`，无需停机迁移。回滚步骤见风险 4。

### 验证与消融策略

- **测试：**
  - 每种事实：写入一条、状态按目录改变、`metadata` 含 `cause` 与引用；前置条件不成立时不写。
  - 事务：业务事务回滚后事实和状态都不存在；同一事务多条事实 `version` 连续。
  - 不可变：更新、删除任一事实行被数据库拒绝。
  - 批量清扫：多个 Flow 各得一条事实，已离开 `running` 的不写。
  - 重放：现有端到端场景（首轮、修订、关闭、合并、取消、重试、恢复新轮次、归档更正）结束后校验通过。
  - 存量：只有 `baseline` 行的旧 Flow 启动后获得 `snapshot` 并通过校验；补录重复执行不新增行。
  - 边界：在其他模块加入一条直接的 `UPDATE flows`，边界测试失败。
- **消融：**
  1. 去掉测试夹具的全量校验，人为保留一处未迁移写入点，确认回归是否仍能发现；用于判断夹具是否必要。
  2. 把 `apply_fact` 的前置条件改回各调用点的 SQL 条件，比较代码量和重放是否仍然成立；
     用于判断前置条件是否值得放在领域层。
  3. 把 `queued`、`running`、`review`、`blocked` 合并为一种带目标状态字段的事实，比较目录、
     约束和查询的复杂度；用于判断按状态分种类是否多余。
- **时机：** 消融在实现完成后、自动代码审查前进行；简化后重跑受影响的检查。

### 开放问题与后置工作

- **审批前需确认：**
  1. 是否接受复用 `console_activity` 作为账本，而不是现在就新建独立的表。
  2. 是否接受 `current_attempt_id`、`title_job_id` 不参与重建，以及
     [`updated_at` 的规则](../architecture/flow-ledger.md#5-状态表-flows)。
  3. 是否接受首期校验只报告、不自动修复。
- **实施中解决：** 回滚后的 `snapshot` 补录判断方式；清扫耗时测量；`flow history` 是否默认隐藏
  触发器的 `state` 行。
- **后置工作：** 见[架构文档的后续方向](../architecture/flow-ledger.md#13-后续方向)。

## 2. PR 变更时间线

### [001] — 2026-11-17 — 基于 PR #171 的写入口实现账本

- **背景 / 需求更新：**
  - 用户要求同步 `main` 后实现。分支 rebase 到 `main` / [eb99903e](https://github.com/b1indsight/agentflow/commit/eb99903e)（PR #179），无冲突。
  - 基线之后合并的 PR #171 已把全部状态写入收拢到 `FlowStore.transition(flow, FlowEvent)`，
    合法转换由领域函数 `decide_flow_state` 决定；`update_flow` 已不存在。初始计划中“约二十处写入点迁移到
    新的 `record` 入口”和“按目标状态划分事实种类”的前提因此不再成立。
  - 实现中发现两条既有约束：控制台测试要求任务原文不出现在任何 `console_activity` 正文里；
    旧版迁移脚本会单独执行 `CONSOLE_SCHEMA`，随后才补写 `archive` 证据。
- **备选方案：**
  - 事实种类：按初始计划保留 `queued`/`running`/… 加 `cause`；或直接以 `FlowEvent` 为种类。
    前者要维护第二套“事件 → 种类”映射和一份与 `decide_flow_state` 重复的前置条件。
  - 写入口：新增 `record` 并让 `transition` 调用它；或在 `transition` 内追加事实。
  - 任务原文：按计划存入 `body`；或只存摘要。
- **决定 / 状态：**（已实施；用户以“实现”批准计划，本条修订未单独审批）
  1. 状态转换事实的 `kind` 即 `FlowEvent` 的值，`metadata` 只记录写入后的 `state`、`archive_reason`、
     `archive_summary`。事件名本身就是起因，不再设 `cause`。`ROUND_ARCHIVED` 由已有 `archive` 证据行记录。
     取代初始计划“技术设计”中的 `FlowFactKind` 目录和 `round_started`（改名为 `round_resumed`）。
  2. 不新增 `record`/`apply_fact`：`transition` 在条件更新成功后调用 `ActivityStore.append_flow` 追加事实；
     重放函数 `replay_flow` 复用 `decide_flow_state` 校验每条事实在前一状态下生效，并与记录的 `state` 比对。
     事实排在状态更新之后，是为了让调用方捕获“状态已变化”错误时不留下孤立事实；同事务提交，顺序不影响一致性。
  3. 首期不记录 `job_id`、`pr_number`。事件名已经回答“为什么变化”；按调用点传 ID 要改约十处签名，
     收益留给后续“事实引用”计划。仅 `titled` 记录命名 `job_id`。
  4. 起点事实只记录 `task_digest`（SHA-256），不存原文；重放由调用方传入当前 `task_content` 并核对摘要。
     保留“原始需求只在详情中读取”的既有约束。
  5. 存量快照放在 `schema.py` 的最后一个 schema（`FLOW_LEDGER_BACKFILL`），不放在 `CONSOLE_SCHEMA`，
     保证它排在迁移脚本补写的 `archive` 证据之后。
  6. `update_flow_metadata` 改为只写 `current_attempt_id` 的 `set_current_attempt`，不再推进 `updated_at`、
     不再回写从不变化的 `installation_id`；标题与命名 Job 指针写入移入 `FlowStore.set_title`、`set_title_job`。
- **后果：**
  - 账本与状态机共用一套规则，事件新增时不需要同步第二份目录；`FlowEvent` 的值成为持久化名称，不能改名。
  - `flow history` 能直接读到事件名，但仍需查 `jobs` 才知道是哪个 Job。
  - 用 SQL 直接改 `flows` 的测试夹具必须标记 `ledger_unverified`；旧版迁移测试整体如此标记。
- **相关记录：** 修订本记录第 1 节“技术设计与涉及模块”“实施顺序”和“验证与消融策略”的对应部分；
  目标、范围、非目标和“只报告不修复”的决定不变。
- **已实施的改动：**
  - 领域：[`flow_policy.py`](../../app/domain/flow_policy.py) 新增 `FlowFact`、`fact_kind`、`task_digest`、
    `replay_flow` 和 `LEDGER_COLUMNS`。
  - 持久层：[`activity.py`](../../app/persistence/activity.py) 的 `append_flow` 接受全部账本种类，
    可重复的转换以 `flow:<id>:fact:<version>` 为键，新增 `flow_facts`；
    [`flows.py`](../../app/persistence/flows.py) 的 `create_flow`、`transition`、`set_title` 追加事实，新增 `verify`；
    [`console_schema.py`](../../app/persistence/console_schema.py) 扩大不可变触发器、新增两个部分唯一索引和存量快照。
  - 调用方：[`initial.py`](../../app/controler/workflow/initial.py)、[`results.py`](../../app/controler/workflow/results.py)
    改用 `FlowStore` 的标题与指针方法。CLI 新增 `flowtool flow verify`。
  - 测试：边界测试改为“只有 `flows.py` 写 `flows`”；conftest 在每个 PostgreSQL 用例结束时校验全部 Flow；
    新增存量快照与差异报告用例，并把不可变检查扩展到 `created` 事实。
- **证据 / 验证：**
  - 同步后、改动前的全量回归：`FLOWTOOL_TEST_DATABASE_URL=… uv run pytest`，1957 passed / 1 skipped。
  - 改动后（含每用例账本校验）：全量回归 1958 passed / 1 skipped，用时 364 秒（改动前 355 秒）；
    `ruff check app tests`、`mypy app` 通过。
  - CLI 冒烟：在临时库创建并取消一个直接任务 Flow，`flowtool flow verify` 退出 0；`flow history` 依次列出
    `created`、`finalization_requested`、`archive` 与触发器 `state` 行；用 SQL 改写 `archive_summary` 后
    `flow verify 1` 报告该列差异并退出 1。
  - 消融 1（每用例账本校验是否必要）：在副本中让 `set_title` 不追加事实，模拟遗漏的写入点，运行
    `tests/test_flow_states.py tests/test_coordinator.py tests/test_workspace_dispatch.py tests/test_worker_inference.py`。
    开启校验：112 passed / 37 errors，全部为账本不一致；关闭校验：112 passed。结论：保留校验夹具。
  - 消融 2、3（前置条件放在领域层、按状态分种类）：不再适用。前置条件直接复用 `decide_flow_state`，
    没有第二份实现；种类即事件名，没有可合并的按状态种类。
  - 记录的 `state` 与重算结果比对只有一行，用于在状态机规则改变后发现旧事实不再按原意重放，未单独消融。
  - 一次全量运行中，旧版迁移用例的显式重放检查暴露了快照排在迁移补写的 `archive` 之前的问题，由决定 5 修复。
- **结果 / 待办：**
  - 已实现首期范围；未部署，真实 E2E 未验收。
  - 回滚后的处理：旧版本期间的写入没有事实，再次升级时这些 Flow 已有起点，不会重新补快照，
    `flow verify` 会报告差异。首期只能人工判断；按需补“在最后一条事实后再补快照”的修复命令。
  - 清扫耗时：租约清扫仍按 `flow_id` 升序逐个 `transition`（PR #171 已如此），每个 Flow 多一次插入，未单独测量。
  - `flow history` 继续列出触发器 `state` 行，未默认隐藏。
