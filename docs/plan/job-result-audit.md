# Job 结果作为编码审计来源 — Feature record

**Stage:** Implemented — 实施与本地验证完成。
**PR:** [#69](https://github.com/b1indsight/agentflow/pull/69)。
**Bookmark:** `refactor/job-result-audit`
**基线:** `main` / `a7c91202`（PR #68），2026-10-10。

## 1. Initial plan

### 背景、目标与范围

`ResultApplication._commit()` 是 `record_codex_run()` 的唯一生产调用，传入空的
prompt/log_path；Store 将开始、结束时间同时设为应用结果时的时间。
这行数据表示一次 job 结果应用，不能表示真实 Codex 调用：一次 revision 可先 resume
失败再 fallback 成功，调用事实已经保存在结果的 invocations 中。
依据：[结果应用](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/results.py)、[存储与建表](https://github.com/b1indsight/agentflow/blob/f601472a6133966e0647aa7c666bdd5c71952804/app/persistence/store.py)、
[结果协议](../../app/contracts/protocol.py)。

目标是消除第二套含义不准确的运行记录，以 jobs 中的持久化结果、结果接收/应用时间及
RevisionExecutionRecord 为审计事实来源。范围限于记录写入、当前 schema、相关测试和架构说明。
不新增审计 API、真实进程时间采集、通用事件表或 JSONL 传输。

### 拟议行为

INITIAL 成功/失败及 REVISION 的 resume/fallback 证据仍保存在 job 结果中；重复应用、
control 重启及迟到结果不会覆盖原证据。已合并 PR 的迟到结果仍被保留，不改变 delivery
和当前 session。审计时间明确区分结果接收与应用，不能冒充执行开始/结束时间。

删除新的 codex_runs 写入，新的数据库不再创建该表及其唯一索引。已有数据库中的旧表
保持原数据，不在启动时 DROP 或回填；应用停止依赖它。旧表的人工清理和外部 SQL
消费者迁移不属于本 PR，架构文档说明这一兼容边界。

### 技术方案与模块

- `app/control/results.py`：删除 codex_runs 写入；保留结果计划对 attempt session、generation、
  summary 的更新，以及退休结果的处理。`RunUpdate` 目前也服务于 session 更新，不能因删表直接删掉。
- `app/persistence/store.py`、`result_store.py`：删除写入方法、当前建表和索引声明；jobs 的
  result_payload、digest、received/applied 标记继续支撑持久恢复，不增加重复摘要列。
- tests：调整 RecordingStore 等替身，替换“codex_runs 只有一行”的断言，验证 job 原始
  证据被保留且业务结果仅应用一次。更新 schema 初始化回归。
- `docs/architecture/components.md`：纠正 prompt、执行时间及审计来源说明；历史计划保持原样。

### 备选方案与取舍

保留表并改成 job 审计投影仍需第二份数据和写入维护，目前没有生产读取入口。
扩展为真实 invocation 表需要 worker 采集和协议演进，超过本次概念简化范围。
选择已有持久结果作为来源，代价是审计查询需要理解结果类型，暂不为未知消费者增加 API。

### 风险与兼容

外部消费者可能仍查询旧表，仓库内没有生产读取并不能证明外部没有使用；发布说明必须
指出停止写入。启动不能破坏旧表，也不能留下引用该表的新索引声明。
恢复事务的 ACK、session 更新和应用标记必须保持原子性。

### 实施顺序

1. 固定结果保留、重复应用、merge 期间迟到结果的行为测试；先证明新的来源能表达原审计事实。
2. 删除重复写入与当前 schema 声明，更新替身和架构说明。
3. 检查新库幂等初始化、含旧表的库可启动及恢复事务，再完成代码审查。

### 验证与消融

复用 `test_revision_recovery`、`test_result_recovery`、`test_workflow_transactions`、
`test_schema` 和 `test_coordinator`；重点保留 resume/fallback 的顺序和 job digest，验证
重放不会再次执行 agent、改变 session 或重复远端效果。
新增测试只用于现有覆盖缺口，不机械增加字段断言。
实现后运行真实 PostgreSQL 全套回归、Ruff、Mypy、`uv build` 和 diff 检查。
本次删除冗余投影，不新增决策机制；用相同恢复场景比较前后结果，不设计额外消融结构。

### 开放问题与后续工作

拟定方向已经明确，可按本计划审阅。若需要继续维护独立 SQL 审计投影，应在批准前修改
本方案；实现时核对实际部署文档。进程级时间采集、审计查询接口及旧表物理删除另行设计。

## 2. PR change timeline

### 2026-10-10 — 持久 job 结果替代重复运行投影

- 已删除 `ResultApplication._commit()` 的 codex_runs 写入、Store 的写入方法以及当前
  schema 的表和唯一索引声明。保留 RunUpdate 与 attempt session、generation、summary
  更新；结果 ACK、应用标记和退休结果路径沿用现有事务。
- 测试替身移除 runs 投影。INITIAL 成功/失败的 merge 后重放、INITIAL 远端操作延迟后的
  重放，以及 REVISION resume/fallback 提交中断和 merge 竞态，改为验证持久 payload、
  digest、接收时间及应用标记。新库检查表/索引不存在，旧表初始化保留已有审计行。
- 架构说明和 CHANGELOG 记录停止写入、旧数据保留、外部 SQL 消费者迁移及时间语义。
  不增加审计接口、重复摘要或进程时间采集。

验证与消融：本次删除冗余投影，没有新增决策机制，因此不需要额外机制消融。删除前先运行
更新后的 schema/revision 回归；恢复场景 16 个通过，2 个 schema 测试因旧表仍被创建而失败。
删除后相同场景全部通过，扩展到计划指定的五个测试文件共 51 个通过，业务/session 恢复行为
保持一致。命令均在独立 PostgreSQL（127.0.0.1:55469）执行：

```sh
export FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55469/postgres
uv run pytest tests/test_schema.py tests/test_revision_recovery.py -q --tb=short
uv run pytest tests/test_schema.py tests/test_revision_recovery.py tests/test_result_recovery.py tests/test_workflow_transactions.py tests/test_coordinator.py -q --tb=short
uv run pytest -q --tb=short
uv run ruff check .
uv run mypy app tests
uv build
git diff --check
```

全套真实 PostgreSQL 回归 836 passed（62.17s），无跳过；有一个既有 Starlette/httpx
弃用警告。Ruff 通过，Mypy 检查 160 个源文件无问题，sdist/wheel 构建成功，diff 检查通过。
局限：未验证仓库外 SQL 消费者，旧表人工清理仍在范围外。
