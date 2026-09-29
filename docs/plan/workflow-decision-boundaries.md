# 工作流决策与持久层分离 — Feature record

**Stage:** Implemented — 实现与本地验证完成，待评审。
**PR:** https://github.com/b1indsight/agentflow/pull/53

## 1. Initial plan

### 背景与目标

架构检查发现两个持久化入口同时承担业务决策：

- `ReviewStore._claim()` 冻结反馈、选择 TRIAGE/REVISION、构造修订指令和未决问题，再写入批次与任务。
- `Store.sweep_expired_assignments()` 在 SQL 中决定重排、失败、保留未知执行和释放绑定，并更新 job、Flow、worker。

这使规则变更必须同时理解 SQL、事务和业务状态。目标是让上述决策可以用明确输入和输出独立理解、独立测试；Store 保留原子持久化职责。

本 PR 保持用户可见行为、数据库 schema、协议、超时配置和既有公开 Store 入口不变。不扩展到整个持久层，不引入通用状态机、Repository 基类或依赖注入框架。

### 拟议结构

新增两个有明确概念归属的纯规则模块：

- `app/review_policy.py`：反馈批次决策。输入为已选择且排序的冻结反馈、是否存在本批可用强信号、force-triage 事实及历史未决问题；输出为不创建批次，或具名、不可变的批次决策，包含 trigger、job kind、可选 revision instruction 和去重后的 open points。
- `app/recovery_policy.py`：恢复动作决策。使用显式、只读的输入快照和调用方传入的同一个 `now`，分别决定 job 的重排/失败/保留及绑定的保留/释放。输出采用有限的具名类型，避免字符串动作加任意 kwargs 的命令框架。

模块不导入 Store、psycopg、control 编排或网络 adapter；不读取时钟，不执行 I/O。输入复用现有枚举和模型，仅为数据库关系查询产生的事实添加必要字段。查询结果不得被重新包装成没有业务含义的通用对象。

### 反馈批次路径

`ReviewStore.claim_due()` 继续负责候选选择及 worker→Flow→job 的既有锁协议。`_claim()` 在原事务内读取 buffer、active batch、反馈版本、强信号有效性和历史 WAITING 问题，调用纯决策，然后原子写入 buffer、batch、job。

必须保留：

- 无当前 attempt、delivery 非 OPEN、未到期或已有 active batch 时，不创建新批次。
- 同一 Flow 只能有一个 READY/PROCESSING 批次；空正文不能触发批次，也不能饿死有正文的反馈。
- 强信号只来自当前选择范围内、未消费/未转发/未退休的授权 request-changes；force-triage 优先。
- 无有效正文时仍按现有规则清除到期 buffer，但不完成旧 WAITING 批次。
- 反馈和指令顺序、open points 去重顺序、冻结版本及派生任务关联不变。

SQL 保留版本、授权信号和并发事实查询；TRIAGE/REVISION 选择、指令构造与问题归并由纯函数持有。

### 租约与恢复路径

保留 sweeper 单次事务和已有 worker 锁的序列化作用；不把读、决策、写拆成多次提交。Store 查询候选及有关 Flow、delivery、stop、result 和 binding 的事实，在必要的行锁和写入条件保护下调用纯规则，并分组执行现有原子更新。

先应用 job 动作，再基于事务内更新后的状态决定 binding 动作，保留原先执行顺序。候选 SQL 只做粗筛选和事实获取；业务判断集中在纯规则。写入时的 token、状态和结果条件用于并发 fencing，不另建一份相互独立的业务决策树。

必须保留：

- 未开始且不可能已执行的任务，只在 lease 到期且无结果/未决 stop 时重排。
- Started 任务只在 recovery deadline 到期且无结果/未决 stop 时走原失败路径。
- 已授权但未收到 Started、冻结归档及已有未应用结果的任务，保留原有未知执行保护，不能因 refactor 重新执行或提前释放绑定。
- ACTIVE Flow 的 CLOSED/MERGED delivery 保留原执行现场和诊断；停止确认仍先于允许释放。
- job 失败与空闲 binding 到期导致的 Flow/worker 更新、终态保护和返回计数保持现状，不顺带调整统计定义。
- 同一轮使用固定 `now`，保留到期边界的 `<=` 语义。

### 方案比较

1. **仅拆 Store 文件**：能缩短文件，但决策仍与 SQL 交织，无法达到目标。
2. **只提取几条布尔 helper**：若 SQL 仍独立决定全部动作，会出现两份规则；不采用。
3. **把整个事务搬到 control service**：会向编排层暴露底层行锁和 SQL，也可能破坏原子性；不采用。
4. **纯决策 + 事务内事实读取/动作提交**：选择此方案。引入的类型仅对应上述实际变化点，事务所有权继续留在 Store。

### 风险与约束

最大风险是将原子条件 UPDATE 改为读后写时扩大竞争窗口，以及逐行处理增加 SQL 往返。实施时先固定行为测试，保持锁顺序及条件写入；候选事实批量读取，动作按类别批量提交，避免每项额外查询。

已有 SQL 中无法安全移出的并发约束继续保留在 SQL，并解释其保护对象。若提取必须实质改变锁协议或运行复杂度，先在本记录追加方案变更，不能以“代码移动”名义扩大范围。

本次不涉及数据库迁移或配置变化。回滚为代码回滚；不改变持久数据格式。

### 实施顺序

1. 用已有 PostgreSQL 测试建立两条路径的基线，补充风险所需的决策矩阵和事务回归场景。
2. 先提取 review policy 并接入 `_claim()`，保留冻结和写入事务。
3. 提取 recovery policy 并接入 sweeper，验证 job 动作与后续 binding 动作的顺序。
4. 更新职责文档、changelog 和本记录的实施时间线；在同一个 bookmark/PR 完成评审。

### 验证与消融

- 纯函数测试：TRIAGE/REVISION/空反馈、force-triage、open points 顺序；lease/deadline 边界、stop、closed/merged、已接收结果及未知执行保护。
- PostgreSQL 回归：`test_review_batches`、`test_feedback_replay`、`test_reopen_triage`、`test_store`、`test_gateway`、`test_recovery_concurrency`、`test_cancel_drain`、`test_pr_lifecycle`、`test_archive_worker_recovery` 及相关事务测试。
- 行为对比：同一组固定时钟与冻结输入，比较提取前后派生任务、状态、诊断及返回计数；并发用例验证结果接收/停止确认不会被 sweep 覆盖。
- 消融：实现后、代码评审前，分别移除 force-triage 优先规则、未决结果/stop 的释放保护，确认代表性用例捕获错误，并恢复最终代码。每次只移除一个机制，证据写入时间线；不保留无效果的包装层。
- 检查：Ruff、Mypy、`git diff --check` 和项目构建；完整 PostgreSQL 测试用于最终回归。

### 待定与后置

待批准的是上述范围和事务边界。具体输入类型字段按已确认的查询事实落实，不要求新增序列化模型。通用 Store 拆分、异步数据库迁移及其他性能 issue 不在本 PR 中。

## 2. PR change timeline

### 2026-09-29 · 1. 方案批准

用户在计划 PR #53 创建后明确要求“实现”，批准上述两个决策边界的重构范围。
实施继续使用同一 bookmark/PR，先验证当前 PostgreSQL 行为，再提取纯规则与补充回归测试。

### 2026-09-29 · 2. 决策提取与事务验证

- 已实现 `review_policy.decide_review_batch()`：统一反馈有效性、强信号/force-triage 优先级、指令与 open points；持久层继续提供本批信号有效性并原子冻结数据。
- 已实现 `recovery_policy`：具名动作与不可变事实模型分别负责 job 和 binding 决策，数据库侧用 `class_row()` 校验关系查询结果；输入不读取时间、数据库或网络。
- Sweeper 保留原 worker 锁事务，为候选 job/binding 加行锁，在事务内批量读取事实并按动作批量写入。新增两个批量事实查询，不按候选数量增加 SQL 往返。先处理 job 再读取 binding，保留原有计数与状态顺序。
- 保留 dispatch/reconnect 路径的 `_MAY_HAVE_EXECUTED` SQL，并在纯策略注释中关联其共同的执行证据；该表达式在本次 sweeper 中不再决定动作。
- 新增 34 个纯策略情形、11 个 PostgreSQL 状态/计数/并发/回滚情形及依赖边界测试。并发测试在 worker 锁被占用时启动 sweep，先提交 result/stop 回执，再验证 sweep 读取新事实；不依赖 sleep。
- 基线：原实现的 180 项相关 PostgreSQL 测试通过；接入策略后同组与 34 项纯测试合计 214 项通过。额外 11 项事务测试全部通过。
- 对照：从 PR 基线 `e0e97dfe` 加载原 `sweep_expired_assignments()`，运行 `tests/test_recovery_decisions.py -k 'not binding_decision_failure'` 的 10 个状态/回执场景，全部通过；新实现相同场景也通过。回滚注入测试依赖新决策边界，单独验证。

### 2026-09-29 · 3. 消融结果

在相同冻结输入和隔离 PostgreSQL 下，每次只移除一条规则，测试结束后恢复源文件：

| 移除的规则 | 命令中的测试选择 | 观察结果与结论 |
| --- | --- | --- |
| `strong = has_unused_signal and not force_triage` 中的 `and not force_triage` | `pytest -q tests/test_review_policy.py` | 1 failed / 5 passed；强制分诊被错误变为直接修订，保留规则 |
| `should_release_binding()` 的 pending-stop 保护 | `pytest -q tests/test_recovery_decisions.py -k sweep_preserves_states_and_counts` | 1 failed / 7 passed；未确认停止错误释放绑定，保留规则 |
| `should_release_binding()` 的 unapplied-result 保护 | 同上 | 1 failed / 7 passed；未应用结果错误释放绑定，保留规则 |

各消融只有对应场景失败，未引入异常收集失败。最终文件已恢复，随后运行全量验证。
本地脚本与日志保存在 `.cache/2026-09-29/policy-ablation.py`、`policy-ablation.json` 和对应 `ablation-*.log`；不纳入版本控制。

### 2026-09-29 · 4. 最终验证

- `FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55439/postgres uv run --no-sync pytest -q --tb=short`：855 passed，0 skipped，58.67 秒。数据库为本次独立实例，测试按 schema 隔离。
- Ruff、Mypy（136 个源文件）、`uv build --no-sources` 和 `git diff --check` 全部通过。
- 首轮全量使用 Unix socket，现有 `freeze_connection` 的 3 个 TCP 回环校验用例失败；切换 TCP 后通过。原 55429 TCP 端口已被其他实例占用，最终在本次实例的 55439 端口复核。此过程未修改备份恢复实现或测试。
- 唯一 warning 来自现有 FastAPI/Starlette TestClient 的依赖弃用提示，不影响本次结果。
- 自查范围：策略只持有业务选择，Store 保留事实查询与锁；新增不可变输入明确反映所需并发事实，没有引入通用命令/状态机层。
