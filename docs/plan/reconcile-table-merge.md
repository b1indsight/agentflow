# 对账调度表合并 — 功能记录

**阶段：** 计划，待审批
**PR：** [#183](https://github.com/b1indsight/agentflow/pull/183)（draft）
**Bookmark：** `refactor/reconcile-table-merge`
**基线：** `main` / `0eaf15b9`（[PR #182](https://github.com/b1indsight/agentflow/pull/182)），2026-11-18。

## 1. 初始计划

### 动机与现状

对账有两张调度表，结构和生命周期几乎相同：

| 表 | 主键 | 独有列 | 使用者 |
|---|---|---|---|
| `flow_reconcile` | `flow_id` | `hint_version` | `PullRequestReconciler`：轮询 PR 状态，补漏合并、关闭、重开与源分支变化 |
| `feedback_reconcile` | `(flow_id, source)` | `cursor`、`scan_incomplete` | `FeedbackReconciler`：按 `issue_comments`、`pr_comments`、`reviews`、`threads` 分页补抓反馈 |

两表共有 `next_at`、`last_success_at`、`failures`、`last_error`，都只由 `ReconcileStore` 写入。
`reset_round` 在开新一轮时把两张表的行一起删掉。PR 检查本质上就是“PR 状态”这一个来源的检查，
单独占一张表没有带来额外约束。

- **受影响者：** 维护持久化层的开发者。Worker、GitHub、控制台 API 与 Prometheus 指标都看不到变化。
- **提议：** 删除 `flow_reconcile`，PR 检查改存为 `feedback_reconcile` 中 `source='pull_request'` 的一行；
  `feedback_reconcile` 增加 `hint_version` 列。

### 目标、范围与验收

- **目标：**
  - 删除 `flow_reconcile` 表及其索引 `ix_reconcile_due`。
  - PR 对账与反馈对账的行为不变：资格条件、调度时间、退避、webhook 唤醒（`hint`）、
    检查期间收到 webhook 时立即重查（`hint_version`）、开新一轮时清空，全部保持现状。
  - 升级时把现有 `flow_reconcile` 行原样迁入，不丢失退避截止时间和诊断信息。
- **范围：** `app/persistence/schema.py`、`app/persistence/reconciliation.py`、
  `app/persistence/metrics.py`，受影响的测试，`docs/architecture/flow-ledger.md`、
  `docs/operations/github-cursor-development.md` 中的表说明，`CHANGELOG.md`，`docs/plan/README.md`。
- **非目标：**
  - 不改表名。按要求合并到 `feedback_reconcile`，即使它此后也保存 PR 检查。
  - 不改两个 Reconciler 的轮询逻辑，也不合并成一个 Reconciler。
  - 不动 `project_projections` / `project_destinations`。
- **约束：** 仓库没有迁移框架，结构变更由 `init_db` 依次执行 `SCHEMAS`，每段 SQL 必须可重复执行。

### 行为变化

无对外行为变化。`reconcile_due` 指标和 `oldest_age_seconds{queue="reconcile"}` 仍只统计 PR 检查。

### 技术设计

**表结构（合并后的 `feedback_reconcile`，`←` 为新增）：**

| 列 | 类型 | 含义 |
|---|---|---|
| `flow_id` | `BIGINT` FK `flows` | 主键之一 |
| `source` | `TEXT` | 主键之一；`issue_comments` / `pr_comments` / `reviews` / `threads` / `pull_request` ← |
| `cursor` | `TEXT` | 反馈分页游标；`pull_request` 行恒为 NULL |
| `next_at` | `TIMESTAMPTZ` | 下次检查时间 |
| `last_success_at` | `TIMESTAMPTZ` | 上次成功时间 |
| `failures` | `INTEGER` | 连续失败次数 |
| `last_error` | `TEXT` | 最近错误或诊断 |
| `scan_incomplete` | `BOOLEAN` | 本轮有缺页；`pull_request` 行恒为 FALSE |
| `hint_version` ← | `BIGINT NOT NULL DEFAULT 0` | PR webhook 唤醒计数；反馈行恒为 0 |

`ix_feedback_reconcile_due (next_at, flow_id)` 保留，两类查询都按 `next_at` 取到期行。

**升级 SQL（`RECONCILE_SCHEMA`，可重复执行）：**

```sql
CREATE TABLE IF NOT EXISTS feedback_reconcile (... 同现有定义 ...,
  hint_version BIGINT NOT NULL DEFAULT 0, PRIMARY KEY(flow_id,source));
ALTER TABLE feedback_reconcile ADD COLUMN IF NOT EXISTS hint_version BIGINT NOT NULL DEFAULT 0;
DO $$ BEGIN
  IF to_regclass('flow_reconcile') IS NOT NULL THEN
    INSERT INTO feedback_reconcile(flow_id,source,next_at,last_success_at,failures,last_error,hint_version)
      SELECT flow_id,'pull_request',next_at,last_success_at,failures,last_error,hint_version
      FROM flow_reconcile ON CONFLICT DO NOTHING;
    DROP TABLE flow_reconcile;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_feedback_reconcile_due ON feedback_reconcile(next_at,flow_id);
```

迁移与删除在同一个 DO 块里，失败时整体回滚；第二次执行时 `flow_reconcile` 已不存在，直接跳过。

**`ReconcileStore` 的改动（公开方法名与签名不变）：**

| 方法 | 改动 |
|---|---|
| `reset_round(flow_id)` | 两条 DELETE 合成一条 `DELETE FROM feedback_reconcile WHERE flow_id=%s` |
| `seed(limit)` | 插入 `(flow_id,'pull_request',next_at)`；`NOT EXISTS` 改查 `source='pull_request'` 的行 |
| `hint(flow_id)` | `INSERT ... VALUES(%s,'pull_request',%s,1) ON CONFLICT(flow_id,source) DO UPDATE ...`，更新表达式不变 |
| `due(limit)` / `get(flow_id)` | 从 `feedback_reconcile` 读，加 `source='pull_request'` |
| `succeeded` / `failed` / `defer` | `WHERE flow_id=%s` 改为 `WHERE flow_id=%s AND source='pull_request'` |
| `feedback_seed` | 不变（`NOT EXISTS` 已按 `source` 区分） |
| `feedback_due` | 增加 `AND r.source<>'pull_request'`，否则会把 PR 行交给反馈 Reconciler |
| `feedback_get` / `feedback_finish` / `feedback_failed` | 不变（都已按具体 `source` 过滤） |

PR 检查的 SQL 字面量 `'pull_request'` 集中为模块常量 `PR_SOURCE = "pull_request"`。

`ReconcileState`（PR 检查）与 `FeedbackCheck(ReconcileState)`（反馈检查）两个模型保留：
`FeedbackCheck.source` 仍是 `ReconcileSource`，不含 `pull_request`，反馈路径的类型约束不变。
`due` / `get` 显式列出 `ReconcileState` 的列，不用 `SELECT *`。

**`metrics.py`：** `due_reconcile` CTE 改读 `feedback_reconcile r ... AND r.source='pull_request'`。

### 备选方案与取舍

1. **不合并。** 两表同构，合并后少一张表、`reset_round` 少一条语句；代价只是 PR 查询多一个 `source` 条件。
2. **合并为新名字（如 `reconcile_checks`）。** 名字更准确，但要多一次改名迁移并改动反馈路径的全部 SQL。
   按要求保留 `feedback_reconcile`。
3. **把 `ReconcileState` / `FeedbackCheck` 合成一个模型，`failed` 与 `feedback_failed` 合成一个方法。**
   SQL 已几乎相同，但会把 `pull_request` 混进反馈路径的 `source` 类型，并改动两个 Reconciler 与大量测试调用。
   本项不做，可作为后续独立简化。

### 成本、风险与缓解

- **漏加过滤：** `feedback_due` 若漏掉 `source<>'pull_request'`，PR 行会以 `FeedbackCheck` 校验失败，
  反馈对账整轮报错。用回归测试覆盖：PR 行与反馈行同时到期时，两个 `due` 各取各的。
- **回滚：** 旧代码启动会重建空的 `flow_reconcile` 并由 `seed` 重新登记，但旧的 `feedback_due` 会读到
  `pull_request` 行并校验失败。回滚前须执行 `DELETE FROM feedback_reconcile WHERE source='pull_request'`；
  丢失的只是退避截止时间与诊断，下一轮会重新检查。
- **并发：** 部署沿用停止旧进程再启动新进程；迁移在 `init_db` 中执行，不与旧进程并发写同一表。
- **数据：** 迁移删除 `flow_reconcile`，部署前按惯例备份。

### 实施顺序

1. schema：新增列、迁移 DO 块、删除 `flow_reconcile` 定义。
2. `ReconcileStore` 与 `metrics.py` 改读写 `feedback_reconcile`。
3. 测试：更新直接操作 `flow_reconcile` 的测试（`test_observability.py`、`test_workflow_transactions.py`），
   新增升级测试与 `due` 隔离测试。
4. 文档与 CHANGELOG。

### 验证与简化对照

- **升级测试（新增）：** 在只有旧结构的库里写入一行 `flow_reconcile`（带非零 `failures`、`hint_version`、
  未来的 `next_at`）和一行反馈检查，执行 `init_db` 两次：`flow_reconcile` 消失，PR 行原样出现在
  `feedback_reconcile`，反馈行不变。
- **隔离测试（新增或并入现有用例）：** 同一 Flow 的 PR 行与反馈行都到期时，`due()` 只返回 PR 检查，
  `feedback_due()` 只返回反馈检查；`reset_round` 两类都删掉。
- **回归：** `test_pr_lifecycle.py`、`test_reconcile*.py`、`test_observability.py`、
  `test_workflow_transactions.py`、`test_flow_states.py` 以及真实 PostgreSQL 全量 pytest；`ruff`、`mypy`。
- **简化对照：** 本项只删除结构，不新增机制，不做消融实验；以“去掉 `feedback_due` 的 `source` 过滤后隔离测试失败”
  确认该测试有效。

### 待定问题与后续工作

- **审批前需确认：** 无。
- **实施中确认：** `test_pr_lifecycle.py` 中依赖 `store.reconcile.get` 的断言预计无需改动。
- **后续：** 备选方案 3（合并检查模型与失败方法）；表关系图在合并后更新。

## 2. PR 变更时间线

### [001] — 2026-11-18 — 合并后的表改名为 `reconcile_checks`

- **背景 / 需求更新：**
  - 用户要求把合并后的表改成更合适的名字。合并后表里既有反馈来源，也有 PR 状态检查，
    `feedback_reconcile` 不再准确。
- **考虑的选项：**
  - `reconcile_checks`：每行是一个 Flow 在一个来源上的定时检查，与两个 Reconciler 中已有的
    `check` / `FeedbackCheck` 命名一致。
  - `github_reconcile`：强调数据来自 GitHub，但不体现“一行一次调度检查”。
  - `reconcile_schedule`：强调调度，但表里还有游标与诊断，不只是时间表。
- **决定 / 状态：**
  - 采用 `reconcile_checks`，替代初始计划“非目标”中“不改表名”一条和“备选方案 2”的取舍。状态：提议，待审批。
- **影响：**
  - 反馈路径的 SQL 也要改表名：`feedback_seed`、`feedback_due`、`feedback_get`、`feedback_finish`、
    `feedback_failed`、`reset_round`。方法名、模型名与签名仍不变。
  - 索引改名为 `ix_reconcile_checks_due (next_at, flow_id)`。
  - 额外受影响：`tests/test_reconcile_feedback.py`（直接 INSERT 旧表）、
    `docs/operations/github-cursor-development.md`（重置扫描的 SQL 改为
    `UPDATE reconcile_checks ... WHERE source<>'pull_request'`）。
  - 回滚改为：旧代码会重建两张空表并由 `seed` / `feedback_seed` 重新登记，不会读到新表，
    因此不再需要回滚前删除 `pull_request` 行；丢失的只是退避截止时间、诊断和反馈游标，
    反馈会从头重扫，已有版本由 `_append_version` 去重。
- **相关记录：** 修改初始计划的“表结构”“升级 SQL”“回滚”三部分，其余不变。
- **修订后的升级 SQL（`RECONCILE_SCHEMA`，可重复执行）：**

  ```sql
  CREATE TABLE IF NOT EXISTS reconcile_checks (
    flow_id BIGINT NOT NULL REFERENCES flows(id),
    source TEXT NOT NULL,
    cursor TEXT,
    next_at TIMESTAMPTZ NOT NULL,
    last_success_at TIMESTAMPTZ,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    scan_incomplete BOOLEAN NOT NULL DEFAULT FALSE,
    hint_version BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(flow_id,source)
  );
  DO $$ BEGIN
    IF to_regclass('feedback_reconcile') IS NOT NULL THEN
      INSERT INTO reconcile_checks(flow_id,source,cursor,next_at,last_success_at,failures,last_error,scan_incomplete)
        SELECT flow_id,source,cursor,next_at,last_success_at,failures,last_error,scan_incomplete
        FROM feedback_reconcile ON CONFLICT DO NOTHING;
      DROP TABLE feedback_reconcile;
    END IF;
    IF to_regclass('flow_reconcile') IS NOT NULL THEN
      INSERT INTO reconcile_checks(flow_id,source,next_at,last_success_at,failures,last_error,hint_version)
        SELECT flow_id,'pull_request',next_at,last_success_at,failures,last_error,hint_version
        FROM flow_reconcile ON CONFLICT DO NOTHING;
      DROP TABLE flow_reconcile;
    END IF;
  END $$;
  CREATE INDEX IF NOT EXISTS ix_reconcile_checks_due ON reconcile_checks(next_at,flow_id);
  ```

  用复制而不是 `ALTER TABLE ... RENAME`，主键、外键约束名随新表生成，不残留 `feedback_reconcile_*` 名字。
- **验证计划调整：** 升级测试同时预置两张旧表各一行，执行两次 `init_db` 后两张旧表都消失，
  `reconcile_checks` 中反馈行（含 `cursor`、`scan_incomplete`）与 PR 行（含 `hint_version`）都原样存在。
- **已实现的改动：** 无代码改动，仅更新计划。
