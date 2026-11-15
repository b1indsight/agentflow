# Control 旧数据兼容路径清理

仅删除旧冻结答复、旧 failure 评论抑制和纯文本标题回写三条执行路径；当前 worker 答复、结构化命名与通用推理保持不变。按用户确认的“目前没有未结清的 Flow”前提，不安排中间状态迁移。

**状态：** 业务删减、真实版本前后对比、完整本地回归与静态检查均已完成；独立审查与 PR 交付结果见本线程。未部署。
**PR：** 用户已要求实现后推送 PR；在下述分支交付，链接见本线程交付结果。
**Bookmark：** `refactor/control-legacy-cleanup`。
**实施基线：** 已 fetch，并将本项隔离到最新 `main@origin`（[847f03cb](https://github.com/b1indsight/agentflow/commit/847f03cbcae8bca97f366e872ced9b43e1c43644)）之上；原工作区的其他功能保留在原 change，不纳入本项 PR。
**需求来源：** [原分析的第 1 项](https://ampcode.com/threads/T-01a1dea0-398e-75c0-9965-a5266f99770a)；不包含第 2～5 项。

## 当前方案

用户已说明当前没有未结清的 Flow，本项取消中间状态查询、等待旧版恢复或排空、迁移与相应验证要求。下方 Initial plan 保留原文；其中相关条款由 [001 修订](#001--2026-11-15--无未结清-flow不迁移中间状态) 取代。

已删除三条执行路径及 `answer_dispatched` 的模型字段、专用授权方法和新建表列，保留已完成历史的读取和正常任务回归。旧库物理列暂留，不新增启动迁移，生产删列仍须另行授权。DEMO 已改为在不同进程中运行实际基线和当前实现，不再临时改写业务函数。

## 最小执行前后对比

以下为[可运行 DEMO](../../scripts/smoke/control_legacy_demo.py)实测的删减前后行为。GitHub 使用记录替身，数据库为隔离 PostgreSQL，不涉及实际 Flow 迁移。

| 场景 | 执行前（基线） | 执行后（本次实现） |
| --- | --- | --- |
| 答复 | 新答复走 worker，旧冻结答复另走控制器直接发布 | 删除旧发布路径及 `answer_dispatched`；新答复仍走 worker |
| 旧 `failure` 评论 | 恢复时专门跳过未发送的旧评论 | 删除特殊分支；按用户确认的前提不迁移旧 plan |
| 标题 | 结构化命名与普通文本回写两条路径 | 只允许结构化命名更新标题；通用文本推理仍可用 |

当前实现保留正常任务、历史记录读取和通用结果对账：[答复入队](../../app/controler/workflow/coordinator.py#L88-L143)、[通用副作用](../../app/controler/workflow/effects.py#L89-L155)、[结构化标题应用](../../app/controler/workflow/results.py#L131-L175)。下方初始计划中的源码行号对应规划时的版本。

将 `FLOWTOOL_TEST_DATABASE_URL` 指向可丢弃的本地测试库后，运行：

```sh
uv run python scripts/smoke/control_legacy_demo.py --before 847f03cbcae8bca97f366e872ced9b43e1c43644
```

## 1. 初始计划

### 动机、范围与行为

当前答复与修订都入队 worker REVISION，命名任务固定使用 `flow_naming`。旧控制器直接发答复、按 `failure` key 抑制评论和将普通推理结果写作标题的分支，不再服务新任务。[答复入队](../../app/controler/workflow/coordinator.py#L119-L149)、[命名生产者](../../app/controler/workflow/coordinator.py#L296-L336)、[review 推理装配](../../app/controler/runtime.py#L265-L267)。

删除后，控制器只把 answer/revise 判定交给 worker；只有结构化命名任务可以更新 Flow 命名事实。通用 text 推理仍可正常返回、重试与恢复，但不再回写 Flow 标题。正常 GitHub 评论、发送前持久标记、不确定写入对账、I/O 后状态复查及逐 job 锁全部保留。

成功标准是三条旧执行路径及其专用授权状态消失，现有答复、命名和恢复行为通过回归。不新增配置、运行时兼容开关、迁移框架或持久状态；不改协议和前端。

### 模块改动与保留边界

1. [`coordinator.py`](../../app/controler/workflow/coordinator.py#L119-L197)：删除旧 ANSWER 特判、`_answer()` 及仅供它使用的导入。保留当前入队前的 PR 生命周期与 head/base 检查。
2. [`reviews.py`](../../app/persistence/reviews.py#L618-L658)：删除 `authorize_answer()`、`reject_answer()`，移除 `invalidate_context()` 中的 `NOT answer_dispatched` 条件；保留 context 匹配、workflow 锁和未派发 job 的原子退休。
3. [`review_models.py`](../../app/domain/review_models.py#L47-L64) 与 [`schema.py`](../../app/persistence/schema.py#L326-L342)：删除模型字段和新建表定义中的 `answer_dispatched`。已部署数据库的物理删列单独处理，不在启动时自动执行。
4. [`effects.py`](../../app/controler/workflow/effects.py#L145-L158)：只删除对未发送的 `CommentEffect(key="failure")` 的特殊跳过。通用评论执行与不确定写入恢复不变。
5. [`results.py`](../../app/controler/workflow/results.py#L123-L165)：只删除非结构化推理结果回写标题的分支；保留结构化命名的 job、round、attempt 校验及事务提交。沿用共享工作区已有的命名与分支改动。

保留 [`TriageVerdict.answer` 的历史解码](../../app/domain/models.py#L410-L439)，以便读取已完成批次保存的 JSON；不把旧答复重新当作 worker instruction。现有 Judge 输出仍拒绝最终 answer，不改写历史 JSON，也不扩展本次兼容清理范围。[Judge 输出约束](../../app/controler/review/judge.py#L43-L78)。

### 旧数据检查是部署前置条件

当前环境未设置生产数据库连接，本计划不声称生产库已满足删除条件。本地实现和隔离数据库验证可以先做；上线前必须停止会创建或恢复相关任务的消费者，再以只读查询确认以下三类记录已处理完，并保存涉及的 ID 和检查结果。

- **旧冻结答复：** 非 completed/cancelled 批次中，`decision->'verdict'->>'answer'` 含实际内容，尤其是尚无派生 job 或 `answer_dispatched` 已置位的记录。不能仅检查待运行 job，因为旧答复恢复本身没有 job。否则删除特判后可能错误入队空 instruction。
- **旧 failure plan：** 所有尚未应用的 job，其 `result_applications.plan.effects` 或 `result_effects.payload` 中含 `kind="comment"`、`key="failure"`。检查必须包含失败、重试和不确定发送记录，不能只查 queued job。未发送的旧 effect 在删掉抑制后可能真正发出；已发送但结果未知的写入须先用旧版本完成 marker 对账。[冻结 plan 与 effects](../../app/persistence/results.py#L40-L104)。
- **旧标题推理：** Flow 的 `title_job_id` 指向非 `flow_naming` 推理，且结果仍可能被应用或被当前轮次/attempt 的 INITIAL 重用。不能仅以 `jobs.state` 已终结或 `flows.title` 已有值判定安全；要连同应用状态、当前轮次、attempt 和 delivery 核对。[命名重用规则](../../app/controler/workflow/coordinator.py#L296-L309)。

存在遗留时暂停部署，优先由旧版本完成恢复，再重新检查。不得把 `answer_dispatched=TRUE` 直接重置以重发，也不删除缺少远端回执的记录。需要人工改数据或补发时另行提出具体操作并获批；本项不自动执行生产写入。

### Schema、发布与回滚

新库不再创建 `answer_dispatched`；旧库暂留物理列，新增应用代码不再读写它。旧列有 `DEFAULT FALSE`，不会要求新代码显式提供值；需在隔离数据库验证“旧表保留列、新代码重启并处理批次”的场景。[旧列定义](../../app/persistence/schema.py#L339)、[初始化方式](../../app/persistence/database.py#L187-L188)。

物理删列放在独立获批的维护操作中：先确认旧数据检查通过、旧消费者全部停止，并备份列值，再执行 `ALTER TABLE review_batches DROP COLUMN answer_dispatched`。不新增启动时的 `DROP COLUMN`。删列前可直接回滚代码；删列后必须先恢复列及必要的发送状态，再启动旧代码，不能仅补一个全 FALSE 的列后恢复旧答复处理。

推送、创建 PR、部署及生产删列不由本地实现审批一并授权；后续需要交付时，仅整理本项改动到同一 bookmark/PR，不夹带共享工作区的其他任务。

### 方案比较与成本

继续保留三条执行路径可以省去旧数据检查，但会长期维护两套答复方式和不再产出的结果。选择删除执行路径，并保留历史 JSON 的读取能力，避免额外改写持久数据。主要成本是部署前检查和后续显式删列；主要风险是旧记录漏查，尤其是删掉 failure 抑制后重新派发评论。

### 实施顺序

1. 获批后重新读取目标文件和共享差异，记录本项修改范围；准备可在只读事务执行的旧数据查询。
2. 删除旧答复执行链及专用字段，再删除 failure 特判与纯文本标题副作用，不改变现有恢复机制。
3. 清理只覆盖旧执行路径的测试与 helper；保留混合测试中的有效行为断言，运行定向验证。
4. 完成隔离 PostgreSQL 全量回归、静态检查、构建和差异审查，补充本记录的实施结果与交付状态。部署仍受上述旧数据检查约束。

### 验证与消融

- 复用 `tests/test_control_triage.py` 的 answer/revise 只入队一次、不直接评论的测试；保留当前 worker 答复、PR 变化、取消、重放与不确定评论恢复覆盖。
- 删除 `freeze_legacy_answer` 及只验证旧直接发答复的测试；删除 `test_legacy_failure_comment_is_never_sent_on_recovery` 的旧 key 特判覆盖，不删除相邻通用评论对账测试。[旧 failure 测试](../../tests/test_result_recovery.py#L293-L325)。
- 在旧 frozen-context 测试中只删除授权断言，保留缺少 `task_content` 时 digest、决策保存及重启读取的检查。[混合测试](../../tests/test_review_batches.py#L84-L116)。
- 复用 `tests/test_coordinator.py` 的结构化命名、无效结果、取消和事务回滚场景。扩展现有通用推理测试，验证 text 返回值与完成状态不变，并用有 `title_job_id` 的输入确认普通文本不再成为标题；预期值独立于被测实现。
- 运行定向 pytest，覆盖 triage、review batches、coordinator、worker inference、result recovery、revision delivery/recovery；随后在测试 fixture 管理的隔离 schema 中执行完整 `uv run pytest`。验证新 schema 和保留旧列的 schema 均能初始化、重启与处理当前批次。
- 执行 `uv run ruff check app tests`、`uv run mypy app tests`、`uv build --no-sources` 与本项 `git diff --check`；记录共享工作区已有失败与本项引入失败的区别。

本项只删机制，没有新增需要比较的设计，不额外安排消融实验。生产库检查尚未执行，产品与测试验证也尚未执行；当前已完成调用点、字段引用、现有测试和最新远端基线的核对。计划草稿通过本地 Claude Code 的 `claude-opus-5-5` 咨询生成，已按实际源码校正边界；咨询结果不作为测试通过的证据。

### 未决事项

本地实施只待计划审批，没有必须先获得生产访问的阻碍。生产查询目标、检查执行窗口与物理删列时间在部署前确定；检查结果不为空时，遗留处理方案必须单独审阅。其他架构简化、历史 JSON 格式迁移和共享工作区冲突均不在本项范围内。

## 2. PR 变更时间线

### 001 — 2026-11-15 — 无未结清 Flow，不迁移中间状态

- **前提与来源：** 用户明确说明：“目前没有未结清的flow，不需要迁移中间状态”。按此调整范围；该前提来自[本轮用户说明](https://ampcode.com/threads/T-01a1dea5-6668-759c-9c16-15ac45fcca45)，未由助手独立查询生产库验证。
- **取代的条款：** Initial plan 中“旧数据检查是部署前置条件”的中间态生产查询、等待旧版恢复或排空、人工迁移安排不再执行；“Schema、发布与回滚”中针对未结清记录备份及恢复发送状态的要求、“实施顺序”中的旧数据查询，以及“验证与消融”中的中间态迁移场景不再是本项必需内容。“未决事项”中的生产查询目标、执行窗口和遗留处理方案相应取消。
- **方案与决定：** 用户已排除未结清状态迁移需求，采用直接删除三条旧执行路径的范围，不新增迁移脚本、兼容开关或排空流程。保留初始计划文本用于追溯，以本修订为当前要求。
- **保持不变：** 保留已完成历史 JSON 的读取、通用推理、正常结果对账与现有任务回归；不清理历史数据。删除代码和新 schema 中的 `answer_dispatched`，既有物理列暂留，物理删列另行授权，不新增启动时自动迁移。若未来删列后需要回滚，仍需先恢复旧版本要求的 schema。
- **验证范围：** 复用当前答复、结构化命名、普通文本推理无标题副作用、取消与结果恢复回归，以及静态检查和构建。不为已经排除的中间状态增加迁移或恢复验证；历史记录读取验证仍保留。
- **实际状态：** 本轮只更新计划及索引说明，未修改产品代码或测试，未运行业务验证；本修订不代表完整计划已获实施、推送、部署或生产删列授权。

### 002 — 2026-11-15 — 可运行的隔离前后对比 DEMO

- **需求与边界：** 用户要求可运行的最小执行前后对比 DEMO。本轮新增独立演示，不将该请求扩大为完整业务删减、生产操作或中间状态迁移授权。
- **实际改动：** 新增 [`scripts/smoke/control_legacy_demo.py`](../../scripts/smoke/control_legacy_demo.py)，复用现有数据库 fixture、结果接收及应用入口。before 直接运行当前代码；after 仅在测试进程内移除 triage 旧答复分支、failure 评论抑制和纯文本标题回写，并移除旧答复方法；每个场景使用独立 schema，未改应用源文件。
- **观察结果：** 新答复在两侧均只入队一个 REVISION 且不直接评论，after 不再具有旧直接答复入口；人工构造的旧 failure effect 从 skipped 变为 succeeded，并只向 GitHub 替身记录评论；普通文本结果仍成功保存，但 after 不再写标题；结构化命名两侧都正常写入标题。演示旧 key 的差异不表示迁移任何实际记录。
- **执行证据：** 在临时本地 PostgreSQL 上执行 `uv run pytest -q -s scripts/smoke/control_legacy_demo.py` 全部通过。故意额外清掉结构化标题时，同一 DEMO 正确断言失败；移除该故障后重跑通过。Ruff 和差异空白检查通过；`git diff --exit-code -- app tests` 确认产品与正式回归文件未改。测试日志保存在本地 `.amp/in/artifacts/control-legacy-demo.log` 与 `control-legacy-demo-negative-control.log`，不加入版本控制。
- **环境与限制：** 本机 PostgreSQL 最初因 locale 初始化失败，设置 `LC_ALL=C` 后运行成功。GitHub 使用既有记录替身，未连接真实 worker 或模型，因此这是组件集成演示，不能作为完整功能或生产端到端验收。临时数据库及进程已清理；业务源代码、schema 和完整回归仍待后续实施验证。
- **复现：** 将 `FLOWTOOL_TEST_DATABASE_URL` 指向可丢弃的本地测试 PostgreSQL 后运行上述 pytest 命令；fixture 自动创建和删除各场景的随机 schema。DEMO 依赖当前源码片段，源码结构变化后会明确失败并要求更新，不静默略过。

### 003 — 2026-11-15 — 正式删除旧执行链并保留真实前后对比

- **实际实现：** 删除 `JobCoordinator.triage()` 的旧冻结答复特判、`_answer()` 和专用导入；删除 `ReviewStore.authorize_answer()` / `reject_answer()` 及 `invalidate_context()` 的旧发送标记条件；移除 `ReviewBatch` 字段与新建表列。删除旧 failure 评论抑制和普通文本标题回写，不改变结构化命名的 job、round、attempt 检查、通用评论 marker 对账或逐 job 结果锁。
- **历史与 schema 边界：** `TriageVerdict.answer` 的历史解码保持原样。原混合测试改为验证已完成批次的旧 JSON、digest 和决策重启读取，并覆盖新表和保留旧物理列的表。没有迁移中间状态、改写历史 JSON、生产数据库写入或启动删列。
- **测试取舍：** 删除仅验证旧直接答复恢复及 failure key 抑制的测试和 helper；将生命周期复查、WAIT 问题传递场景调整为当前 worker 任务合同，保留有意义的断言。扩展现有本地/远端推理集成测试，覆盖无 Flow 与绑定 Flow 两种调用；特意设置 `title_job_id` 后验证普通结果和重启读取正常、标题不变。
- **回归证据：** 新断言先在未改业务代码时失败，实际标题为 `Local answer`，无 Flow 的对照通过；正式实现后同一场景通过。定向与完整 `uv run pytest -q` PostgreSQL 回归、`uv run ruff check app tests scripts/smoke/control_legacy_demo.py`、`uv run mypy app tests`、`uv build --no-sources` 和 `git diff --check` 均通过；环境跳过项与既有 Starlette 弃用警告随本线程结果说明。日志保存在本地 `.amp/in/artifacts/control-legacy-*.log`，不纳入版本控制。
- **DEMO 取舍：** 取代 002 的进程内删源码方案：`--before` 指定 Git 基线，脚本导出临时源码副本，将同一组场景分别交给独立 pytest 进程执行；after 直接运行当前源码。临时副本自动清理，不切换调用者的分支、不改写业务函数。实测新答复保持入队、合成 failure 评论取消特殊跳过、普通文本不改标题、结构化命名保持正常。真实 worker、模型和 GitHub 服务未接入该 DEMO。
- **复杂度与交付：** 产品代码仅删除机制，没有新增需要做消融对照的设计。使用同一 bookmark/PR 交付，保留原工作区其他 change；用户授权包括推送与创建 PR，不包含部署、合并或生产物理删列。
