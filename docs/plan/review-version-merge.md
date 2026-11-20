# 评审反馈表合并 — 功能记录

**阶段：** 已实现并推送，本地验证通过；代码审查门禁经用户授权跳过；待 CI
**PR：** [#182](https://github.com/b1indsight/agentflow/pull/182)（draft）
**Bookmark：** `refactor/review-version-merge`
**基线：** `main` / `72707a21`（[PR #181](https://github.com/b1indsight/agentflow/pull/181)），2026-11-17。

## 1. 初始计划

### 动机与现状

一条 GitHub 反馈的持久化现在分散在四张表里：

| 表 | 内容 | 问题 |
|---|---|---|
| `review_items` | 身份 `(flow_id, kind, github_id)`、当前版本号、`observed_at` | `reviews.py` 中 8 处查询靠 `i.id=v.item_id AND i.version=v.version` 取当前版本 |
| `review_versions` | 每个内容版本及 relay/consume/addressed 进度 | 无 |
| `review_pending_edits` | 同一秒内顺序无法判定的编辑 webhook，等前一个版本到达后回放 | 触发条件很苛刻：同一条评论在一秒内被人工编辑两次，且 webhook 乱序到达。即使不处理，主动对账也会收敛到 GitHub 的实际内容 |
| `review_thread_observations` | PR 线程快照 | 只在 `ReviewStore.observe_threads` 写入，`app/` 内没有读取方 |

- **受影响者：** 维护评审持久化的开发者。Worker、Judge 和 GitHub 侧看不到变化，唯一例外见“行为变化”。
- **提议：**
  - 把 `review_items` 并入 `review_versions`。
  - GitHub 对象改为按全局的 `(kind, github_id)` 识别，不再按 Flow 区分。
  - 删除 `review_pending_edits` 和 `review_thread_observations`。
  - 删除已无写入方或读取方的字段：`review_buffers.generation`、`review_buffers.force_triage`、`review_batches.generation`、`review_versions.signal_retired`。
  - 评审相关的表只剩 `review_versions`、`review_buffers`、`review_batches`。

### 目标、范围与验收

- **目标：**
  - 删除三张表和四个空转字段。删除这些字段后行为不变：它们现在要么是常量，要么只写不读。
  - 由数据库约束保证：每个 GitHub 对象只有一段版本历史，且只有一个当前版本。
  - `(item_id, version)` 继续作为反馈版本的标识。Worker 协议、prompt、`FeedbackRef`、批次 JSONB、结果计划与 effects 载荷都不改，已冻结的 digest 保持有效。
- **范围：**
  - `app/persistence/schema.py`、`app/persistence/reviews.py`。
  - 受影响的测试。
  - `docs/architecture/components.md`、`docs/architecture/flow-ledger.md` 中的表说明，`CHANGELOG.md`。
- **非目标：** 不改 `review_buffers`、`review_batches` 的职责，也不改评审策略。
- **约束：**
  - 仓库没有迁移框架。结构变更由 `init_db` 执行 `SCHEMAS`，每段 SQL 都必须能重复执行。
  - 已冻结的 `(item_id, version)` 不能改写，原因见“备选方案 1”。

### 行为变化

两项都已在讨论中确认接受：

1. **同一 Issue 先后对应多条 Flow 时，旧 Issue 评论不再进入新 Flow。** Flow A 归档后，同一 Issue 新建 Flow B。A 时期留在 Issue 下、之后没有再编辑的 `conversation` 评论，不再作为 B 的待处理反馈；被编辑或新增的评论，B 照常处理。
   - 原因：对象不再按 Flow 区分；内容未变时只推进 `observed_at`，不追加版本。
   - 这与同一 Flow 后续轮次用 `issue_comment_cutoff` 排除旧 Issue 评论的做法一致。
   - 其余情况不变：`inline` 和 `review_body` 挂在各 Flow 自己的 PR 上；同一 Issue 同一时刻只有一条活跃 Flow；删除对所有 Flow 都是终态。
2. **同一秒内乱序到达的编辑会先被丢弃，由对账纠正。** 例：一条评论 10:00:05 创建为 A，同一秒内被编辑成 A→B、B→C，而 B→C 的 webhook 先到。
   - 现在：B→C 被暂存，A→B 到达后两条依次回放，版本序列为 A、B、C。
   - 之后：B→C 被丢弃，版本停在 B。下一次对账（`reconcile_interval_seconds`，默认 300 秒）读到 GitHub 上的 C：时间同一秒但正文不同，主动拉取不带 `delivery_id`，于是追加版本 C。
   - 最坏情况：`settle_seconds`（默认 75 秒）短于对账间隔，批次可能先用中间正文 B 跑一轮修订。C 到达后会再触发一批。结果是多做一次，不会卡住，也不会永久出错。
   - `_edit_seen` 保留。它只依赖版本历史，用来防止迟到的 webhook 把旧正文重放成新版本。

### 技术设计

合并后的 `review_versions` 结构，`←` 标出新增的列：

| 列 | 类型 | 含义 |
|---|---|---|
| `item_id` | `BIGINT NOT NULL` | 一个 GitHub 对象的稳定标识。旧对象保留原值；新对象由 `(kind, github_id)` 编码生成，见下文 |
| `version` | `INTEGER NOT NULL` | 从 1 开始递增 |
| `kind` ← | `TEXT NOT NULL CHECK (kind IN ('review_body','inline','conversation'))` | GitHub 对象类型 |
| `github_id` ← | `BIGINT NOT NULL CHECK (github_id>0)` | GitHub 数字 ID（`fullDatabaseId`） |
| `current` ← | `BOOLEAN NOT NULL` | 是否为该对象的当前版本 |
| `observed_at` ← | `TIMESTAMPTZ NOT NULL` | 最后观察到的 GitHub `updated_at`。只有当前行的值会被读取 |
| `attempt_id`、`source_delivery`、`content`、`relayed_at`、`consumed_at`、`addressed_sha`、`addressed_job_id`、`observation_seq` | 不变 | 不变（`signal_retired` 删除，见下文） |

约束与索引：

- 主键保持 `(item_id, version)`。`record_addressed`、`relayed`、`complete` 现有的 `WHERE item_id=%s AND version=%s` 不用改。
- `UNIQUE (kind, github_id, version)`：两段历史都会从 v1 开始，所以同一对象不可能出现第二段历史。这取代了原来 `review_items` 上的 `UNIQUE(flow_id, kind, github_id)`。
- `CREATE UNIQUE INDEX ux_review_current ON review_versions(kind, github_id) WHERE current`：每个对象恰好一个当前行，同时是按对象查当前版本时用的索引。
- 删除 `review_versions.item_id` 指向 `review_items` 的外键。

**新对象的 `item_id`：** `github_id * 4 + kind_code`，`review_body=1`、`inline=2`、`conversation=3`，在插入 SQL 中计算。

- 编码一一对应，可以反解，不需要序列。GitHub ID 现在约为 10⁹ 量级，编码值远小于 `BIGINT` 上限。
- 旧 `item_id` 是从 1 开始的自增值，远小于 4×10⁹，两者不会重叠。万一重叠，主键冲突会让写入失败，不会悄悄串号。
- 旧对象继续沿用原 `item_id`。只看 `item_id` 本身无法区分新旧对象，但所有读取都经由表，不依赖反解。

`ReviewStore` 的改动：

- **`_append_version`：**
  - 按 `kind=%s AND github_id=%s AND current` 查当前行。
  - 只推进 `observed_at` 时，更新当前行。
  - 追加新版本：先把旧行设为 `current=FALSE`，再插入 `version+1` 的当前行，沿用旧行的 `item_id`。顺序不能反过来，否则会撞上部分唯一索引。
  - 新对象：插入 `version=1` 的当前行，使用编码生成的 `item_id`。
  - 删除终态和乱序丢弃的判断不变。删除同秒暂存分支，同秒无法定序的编辑直接丢弃。
- **删除 `_reconcile_edits` 和 `_competing_edit`，** 并删除 `ingest`、`observe_feedback`、`observe_threads` 中对它们的调用。
- **`observe_feedback`、`_has_unused_signal`、`pending`、`addressed`、`observe_threads`、`complete`：**
  - `FROM review_items i JOIN review_versions v ON … WHERE i.flow_id=%s` 改为 `FROM review_versions v WHERE v.current`。
  - Flow 归属改由 `v.attempt_id` 关联 `attempts.flow_id` 判断。`pending` 与 `_has_unused_signal` 原本就限定当前轮次的 attempt，只需去掉 `i.flow_id`。
  - 按对象查询的地方去掉 `flow_id`。
- **删除 `observe_threads` 中写入 `review_thread_observations` 的语句。** 线程中的人工评论仍照常写成 `inline` 版本。
- 公开方法的签名不变。

空转字段清理：

- **`force_triage` 和 `signal_retired`：** 唯一会把它们设成 TRUE 的 `ReviewStore.reopened` 已在 [#134](https://github.com/b1indsight/agentflow/pull/134) 中删除。
  - 删除 `review_buffers.force_triage`、`review_versions.signal_retired` 两列。
  - 删除 `decide_review_batch` 的 `force_triage` 参数：`strong` 直接等于 `has_unused_signal`。
  - 删除 `_has_unused_signal` 中的 `NOT v.signal_retired`，以及 `_claim` 中读写 `force_triage` 的部分。
- **`generation`：** `review_buffers.generation` 只会递增，唯一的读取是在 `create_batch` 里被抄进 `review_batches.generation`，而后者没有读取方。
  - 删除两列和 `UNIQUE(flow_id, generation)`，并删除 `ReviewBatch.generation`。
  - `create_batch` 不再写 `review_buffers`；`_touch` 改为只 upsert `due_at`。
  - 每条 Flow 至多一个活跃批次，仍由 `ux_review_active_batch` 保证。
- 迁移用 `ALTER TABLE … DROP COLUMN IF EXISTS` 实现，可以重复执行。

迁移在 `REVIEW_SCHEMA` 中完成，每一步都可以重复执行：

1. `CREATE TABLE IF NOT EXISTS review_versions` 直接使用新结构，新库到此为止。
2. 用 `DO` 块，仅当 `to_regclass('review_items')` 存在时执行：
   1. 若有 `(kind, github_id)` 分属多个 item，`RAISE EXCEPTION` 中止，提示需人工处理。按讨论判断生产库应为 0。
   2. 给 `review_versions` 加 `kind`、`github_id`、`current`、`observed_at` 列，并从 `review_items` 回填：`current = (v.version = i.version)`；`observed_at` 在每行都填该 item 的值。
   3. 设置 `NOT NULL` 和 `CHECK`，建立唯一约束与部分唯一索引。
   4. 删除指向 `review_items` 的外键，然后删除 `review_items`。
3. `DROP TABLE IF EXISTS review_pending_edits, review_thread_observations`。迁移时尚未回放的暂存编辑由下一次对账收敛。

### 备选方案

1. **所有 `item_id` 都由 `(kind, github_id)` 编码生成，包括旧数据，迁移时改写冻结引用。**
   - 不采用。旧 `item_id` 出现在两类带 digest 的证据里：
     - `review_batches.context`：`ReviewContext.digest` 是整段 JSON 的 sha256，保存的 Judge 决策用 `context_digest` 绑定它，`record_decision` 和 `coordinator.triage` 都会比对。
     - `jobs.result_payload`：Worker 结果中的 `addressed`、`deferred` 带 `item_id`，`result_digest` 校验整段结果。
   - 改写这两处会让已保存的决策和未应用的结果校验失败。只改写其余位置，又会让同一批次内的引用互相不一致。
2. **不加 `current` 列，用视图或 `DISTINCT ON … ORDER BY version DESC` 取当前版本。**
   - 收益：少一个可变列。
   - 代价：按 Flow 过滤的查询无法利用索引，每次都要扫全部版本；数据库也无法约束“恰好一个当前版本”。
   - 不采用：可变的进度列本来就存在。
3. **保留 `flow_id`。** 可以保持旧 Issue 评论进入新 Flow 的现有行为，已在讨论中否决。
4. **保留 `review_pending_edits`。** 同秒乱序时能立即得到正确内容，代价是一张表、两个方法和一整套回放逻辑，只为覆盖一个罕见场景。最坏只是多做一次修订，因此不采用。
5. **删除 `review_buffers`，由 `review_versions` 推算到期时间。**
   - 需要给 `review_versions` 增加写入时间列。
   - `claim_due` 的到期条件要同时排除已转发的版本，否则 Judge 判定 wait 后会反复分诊同一批反馈；还要排除只有空正文的情况，否则每次扫描都会空锁 Flow。
   - 不采用：删掉的只是一张两列表，换来的是一条更难写对的查询。保留 `(flow_id, due_at)`，它是由写入事务维护的到期标记。
6. **保持现状。** 维护时要理解四张表，其中两张几乎不起作用，因此不采用。

### 代价与风险

- **迁移不可逆：** 会删除三张表。
  - 缓解：部署前用 `pg_dump` 备份这四张评审表。迁移在 `init_db` 的同一事务中执行，失败时由 PostgreSQL 回滚 DDL。
- **同秒乱序编辑最多延迟 300 秒纠正，** 期间可能多做一次修订（见行为变化 2）。接受。
- **`current` 的维护顺序** 固定为先清后插，在调用方的 workflow 锁和事务内执行。若出现无锁的并发写入，部分唯一索引会让后写者事务失败，不会悄悄分叉。
- **测试共享的旧 DDL：** `tests/test_flow_state_migration.py` 把 `REVIEW_SCHEMA` 当作“未变化的辅助 DDL”导入，要改用冻结的旧评审 DDL，以免旧库夹具意外得到新结构。
- **`item_id` 不再是连续小整数，** prompt 中显示的 `item_id` 会变成 10 位数字。对模型无影响，`FeedbackRef` 只要求大于 0。

### 实施顺序

1. 修改 `schema.py`：新结构、可重复迁移块，删除三张表的 DDL。
2. 修改 `reviews.py`：重写 `_append_version`，删除 `_reconcile_edits` 和 `_competing_edit`，改写 6 个按 Flow 查询的方法，删除线程快照写入；删除空转字段，同步修改 `review_policy.py` 和 `review_models.py`。
3. 调整测试：
   - `test_feedback_replay.py`、`test_review_batches.py`、`test_thread_recovery.py`、`test_reconcile_feedback.py`：依赖暂存回放的断言，改为“丢弃后由主动观察收敛”。
   - `test_reconcile_threads.py`：删除快照断言。
   - `test_flow_state_migration.py`：冻结旧评审 DDL。
   - 其他直接查询评审表的测试按新列核对。
   - `test_reconcile_feedback.py` 中对 `generation` 的断言删除；`test_console_api.py` 中设置 `force_triage` 的夹具删除。
4. 新增测试，见下节。
5. 更新 `components.md`、`flow-ledger.md` 中的表清单和说明，以及 `CHANGELOG.md`。

**发布：** 部署前备份，正常重启 server 即执行迁移。Worker 不需要更新。

### 验证与简化对照

- **全量回归：** 在真实 PostgreSQL 上跑全部测试，重点是 ingest、迟到 webhook 去重、删除终态、线程观察、批次以及 resume 后的 cutoff。
- **迁移测试（新增一个）：**
  - 夹具：旧结构的库，含两个对象的多版本历史、一条 `review_pending_edits` 和一条线程快照。
  - 执行两次 `init_db`，断言：
    - 三张表都已删除；
    - 原有 `(item_id, version)` 和内容不变；
    - 每个对象只有原当前版本那一行 `current`；
    - 之后对旧对象追加的版本仍使用原 `item_id`。
  - 另一个夹具让同一 `(kind, github_id)` 分属两个 item，断言 `init_db` 报错中止，且旧表保持原样。
- **行为测试（新增两个）：**
  1. Flow A 归档后，为同一 Issue 新建 Flow B。B 观察到 A 时期未编辑的 Issue 评论，不产生待处理反馈。同一评论被编辑后，B 产生新版本，`item_id` 不变，A 的旧版本不受影响。
  2. 同一秒内 B→C 的 webhook 先于 A→B 到达，版本停在 B；随后一次主动观察读到 C，版本收敛为 C，并只触发一次 settle。
- **简化对照：**
  - 去掉 `UNIQUE (kind, github_id, version)` 或 `ux_review_current` 中的任一个，应各有一条断言失败，以证明二者都是必要约束。
  - 以 `DISTINCT ON` 替换 `current` 列，比较查询写法和 `EXPLAIN` 结果，结论记入时间线。

### 未决问题

- **审批阻塞：** 是否同意旧对象保留原 `item_id`、只有新对象使用编码（备选方案 1 说明了为什么不能统一编码）。
- **实施中确认：**
  - 部署前在生产库执行 `SELECT kind, github_id FROM review_items GROUP BY 1,2 HAVING count(*)>1`，确认为 0。
  - 执行 `SELECT count(*) FROM review_versions`，判断回填是否需要分批。
- **延后：** 无。

## 2. PR 变更时间线

### [001] — 2026-11-18 — 测试影响清单与三组测试合并

- **Context / requirement update:**
  - 批准前，用户要求列出相关测试及其场景，并合并重复的测试。
- **Options considered:**
  - 只改写受影响的测试；或者顺带把场景重复的测试合并成参数化测试。
- **Decision / status:**
  - 已接受（用户在批准计划时一并确认）：在本 PR 合并以下三组测试，与本次改动无关的重复测试不动。
    1. `test_feedback_replay.py` 中的 4 个测试合为 1 个表驱动测试，覆盖“观察之后，迟到的同秒 webhook 不能重放”：`observed_edit_does_not_replay_after_same_second_revert`、`observation_covering_skipped_predecessor_cannot_replay`、`first_remote_observation_does_not_require_a_missing_predecessor`、`query_frozen_before_a_webhook_does_not_revert_it`。
    2. `ambiguous_repeat_waits_for_a_fresh_observation` 与 `repeated_transition_in_a_later_second_is_new_feedback` 合为 1 个同秒/跨秒的边界测试。
    3. 3 个依赖暂存回放的测试合为 1 个“乱序编辑先丢弃，观察后收敛”的测试，放在 `test_feedback_replay.py`，按 `observe_feedback` 和 `observe_threads` 参数化：`test_review_batches.py::same_second_out_of_order_edits_reconcile_after_restart`、`test_reconcile_feedback.py::polling_missing_predecessor_applies_the_queued_successor`、`test_thread_recovery.py::remote_predecessor_releases_the_queued_webhook_edit`。
  - 删除只验证暂存机制的测试：`competing_edit_successors_are_not_ordered_by_delivery_id`、`incoming_edit_cannot_bypass_pending_competing_successors`、`deleted_feedback_cannot_be_revived_by_pending_edits`。删除不可复活已由 `test_deletion_is_a_tombstone_and_cannot_be_resurrected` 覆盖。
  - 不在本次合并：`slow_same_second_page_cannot_overwrite_webhook` 与 `new_webhook_version_wins_over_slow_thread_page`。两者分别保护两条观察边界，夹具也不同。
- **Consequences:**
  - 场景覆盖不变，`test_feedback_replay.py` 的测试数从 11 个减少到 5 个。
- **Implemented changes:** 尚未实施。

### [002] — 2026-11-18 — 实施与验证

- **Context / requirement update:** 用户批准计划和 [001] 的测试合并后，开始实施。
- **Implemented changes:**
  - `app/persistence/schema.py`：`REVIEW_SCHEMA` 改为合并后的结构，在同一段 SQL 中完成可重复执行的迁移。删除 `review_items`、`review_pending_edits`、`review_thread_observations`，以及 `generation`、`force_triage`、`signal_retired` 列。
  - `app/persistence/reviews.py`：
    - 重写 `_append_version`：按 `(kind, github_id)` 查当前行；先清除旧当前行，再插入新版本；新对象使用编码 `item_id`。
    - 删除 `_reconcile_edits`、`_competing_edit`，以及线程快照的写入。
    - 7 处查询改为直接读 `review_versions WHERE is_current`，Flow 归属改由 attempt 判断。
  - `app/domain/review_policy.py` 删除 `force_triage` 参数；`ReviewBatch` 删除 `generation`。
  - 测试：按 [001] 合并、删除和改写。另外新增迁移测试、跨 Flow 遗留对象中止升级测试，以及“其他 Flow 已记录且未编辑的评论不算新反馈”测试。
  - 文档：`components.md`、`flow-ledger.md`、`review-loop.md`、`architecture/README.md`、`CHANGELOG.md`。
- **与计划的差异：**
  - 当前版本标记列命名为 `is_current`，而不是 `current`。`CURRENT` 是 SQL 关键字，改名可以避免歧义。
  - 新增部分索引 `ix_review_current_attempt (attempt_id) WHERE is_current`。按 Flow 查当前反馈时，过滤条件从 `review_items.flow_id` 改成了 attempt，需要这个索引。
  - `tests/test_flow_state_migration.py` 不需要冻结旧评审 DDL。它的夹具只要求 `review_batches` 存在，不写入评审数据；新的 `REVIEW_SCHEMA` 在其旧库上可以直接执行。
  - `test_reconcile_threads.py` 中还有三处读取线程快照表的断言（resolved、outdated、complete），计划里没有列出，已一并删除。行为断言保留：待处理反馈数量、扫描游标和成功时间。
  - `test_reconcile_feedback.py` 中“approval 不唤醒讨论”原本通过 `generation` 不增长来断言，现改为断言 `due_at` 不变，保留了原意。
- **Evidence / validation：** 本地 PostgreSQL 17，临时集群。
  - `uv run pytest tests`：2001 passed，1 skipped。
  - `uv run ruff check app tests`、`uv run mypy app tests` 均通过。
  - 第一次全量运行（测试改写前）有 11 个失败，全部是计划中预期要改的测试。没有出现因同一 `github_id` 跨 Flow 共享而导致的意外失败。
- **简化对照：**
  - **`is_current` 与 `DISTINCT ON` 对比。** 在独立数据库中生成 500 条 Flow × 40 个对象 × 5 个版本，共 10 万行，用 `pending` 的核心条件查询一条 Flow 的当前反馈，各执行 20 次取平均：`is_current` 方案 0.11 ms，`DISTINCT ON` 方案 34.39 ms，后者需要扫描并排序全部版本。保留 `is_current`。两条查询除取当前版本的方式外，其余条件相同；对比脚本是本地临时文件，未保留。
  - **去掉 `ux_review_object_version` 和 `ux_review_current`（后者已在 [003] 删除）。** 评审相关的 5 个测试文件（107 个测试）全部通过。这与计划“应各有一条断言失败”的预期不符：两个索引约束的是代码在 workflow 锁内已经维持的不变量，现有路径不会违反它们。决定保留：它们是计划目标中“由数据库约束保证”的落实，代价是两个索引。将来如果代码改错，比如忘记清除旧当前行，事务会直接失败，而不是让 `pending` 悄悄返回重复项。
- **Outcome / pending：** 实现完成，尚未推送。2026-11-18 第一次运行代码审查门禁时，Codex 额度用尽（恢复时间 2026-11-19 17:40），审查未完成，门禁按规则判定为失败，因此没有推送实现。


### [003] — 2026-11-18 — 去掉 `is_current`，当前版本取最大版本号

- **Context / requirement update:** 用户提出，当前版本就是同一 `(kind, github_id)` 下版本号最大的一行，不需要另存标记列。
- **Decision:** 删除 `is_current` 列、`ux_review_current` 和 `ix_review_current_attempt`。新增普通索引 `ix_review_versions_attempt (attempt_id)`。当前版本按 `(kind, github_id)` 取版本号最大的一行，不建视图、不存标记：
    - 单对象查询（`_append_version`、`observe_feedback`、`observe_threads`）写 `WHERE kind=%s AND github_id=%s ORDER BY version DESC LIMIT 1`。
    - 按 Flow 的查询（`_has_unused_signal`、`pending`、`addressed`、`complete`）拼接常量 `_CURRENT`：不存在同一 `(kind, github_id)` 下更大版本号的行。
    - 两者都走 `ux_review_object_version (kind, github_id, version)`。曾考虑视图 `review_current`，但它会让以后删表、删列或改列类型前必须先删视图，执行计划与内联条件相同，因此不用；也曾考虑按 `item_id` 判断，但统一用对象标识 `(kind, github_id)` 更一致，`item_id` 只负责冻结摘要。
- **Implemented changes:**
  - `schema.py`：建表和迁移回填都不再写入 `is_current`；新增上述视图和索引。
  - `reviews.py`：原来 7 处 `WHERE is_current` 按上述两种写法替换；`_append_version` 不再先清除旧当前行，直接插入下一版本。只更新 `observed_at` 的分支仍指向查到的最新行。
  - 迁移测试改为按最大版本号断言当前版本；`components.md`、`CHANGELOG.md` 同步修改。
- **Rationale:** 当前版本由数据推导，不会出现“标记和最大版本不一致”的状态；`ux_review_object_version` 仍保证同一对象的版本号唯一，所以最大版本只有一行。
- **Evidence / validation:** 同 [002] 的 10 万行数据（500 条 Flow × 40 个对象 × 5 个版本），按一条 Flow 查当前反馈，各执行 20 次取平均：

  | 取当前版本的方式 | 耗时 |
  |---|---|
  | `is_current` 部分索引 | 0.061 ms |
  | `NOT EXISTS` 更大版本（本次采用） | 0.220 ms |
  | `DISTINCT ON` | 22.9 ms |
  | 单对象 `ORDER BY version DESC LIMIT 1` | 0.050 ms |

  `NOT EXISTS` 依靠 `ux_review_object_version` 做反连接探测，比 `is_current` 慢约 0.16 ms，量级可以忽略；`DISTINCT ON` 要扫描并排序全部版本，不采用。[002] 中 `is_current` 与 `DISTINCT ON` 的结论由本条替代。
- **Outcome / pending：** 全量回归 2001 passed、1 skipped，ruff、mypy 通过。Codex 额度未恢复，代码审查门禁无法运行；2026-11-18 用户明确授权跳过门禁，直接推送到 PR #182。待 CI。
