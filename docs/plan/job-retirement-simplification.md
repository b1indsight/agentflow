# Job 作废判定简化

将真正相同的作废规则收敛为一个领域方法和一个 effects 内部函数，保留派发授权、结果恢复、最终提交之间的行为差异。不把所有检查塞进带豁免开关的通用函数。

**状态：** 实现完成；本地回归、等价对照和构建证据见时间线，独立审查与 CI 状态见本线程及 PR。未部署。
**PR / Bookmark：** `refactor/job-retirement-simplification`；计划与实施沿用本记录和同一独立 PR。[按分支查找 PR](https://github.com/b1indsight/agentflow/pulls?q=is%3Apr+head%3Arefactor%2Fjob-retirement-simplification)。
**需求来源：** [Controller 简化分析的第 2 项](https://ampcode.com/threads/T-01a1dea0-398e-75c0-9965-a5266f99770a)，由[本线程](https://ampcode.com/threads/T-01a1df30-93b4-7420-b475-34bb23a5ceac)选定。
**验证入口：** [线程内实施与验证记录](https://ampcode.com/artifacts/job-620071c7-d5bd-4116-9028-213587323db6)；下文 `.amp/in/` 原始日志仅供本地核验，属于忽略文件，不随 PR 上传到 GitHub。
**实施基线：** 首次实施基于 `main@origin`：[2692f59f](https://github.com/b1indsight/agentflow/commit/2692f59f)；当前发布基于 [f77b4136](https://github.com/b1indsight/agentflow/commit/f77b41368ac709a0269cb46e9226439b27940310)，其中 [PR #168](https://github.com/b1indsight/agentflow/pull/168) 已合并。本项相对 main 的差异不重复包含 legacy 清理。

## 1. 初始计划

### 目标与范围

当前几处代码都检查轮次、取消、PR 生命周期，但检查用途不同。目标是让必须同步维护的条件只有一个定义，同时保持 job 终态、冻结 plan、effect 对账、Flow/Delivery/session 更新及 GitHub 写入行为不变。

只调整 `app/domain/models.py`、`app/controler/workflow/coordinator.py`、`results.py` 和 `effects.py` 中本项拥有的条件表达式。不修改数据库、wire、持久化 plan 格式、推理命名、worker 状态机或第 1、3～5 项简化。不扩展到 persistence 中的领取、停止和租约授权条件。

### 原分析七处判断的实际差异

下面链接指向规划时共享工作区的源码；后续删改可能使行号变化。最终提交与失败恢复的检查也列出，避免漏合并或误合并。

| 位置 | 当前规则与结果 | 本项处理 |
| --- | --- | --- |
| [`coordinator._retired`](../../app/controler/workflow/coordinator.py#L79-L86) | 不接受新任务、轮次不同、delivery 淘汰该类 job 时返回 DONE。`accepts()` 还拒绝 BLOCKED 中的编码任务。 | 复用领域规则，保留 `not flow.accepts(job.kind)`。 |
| [`results._apply` 最终提交](../../app/controler/workflow/results.py#L259-L300) | 非 ARCHIVE 遇到归档原因、轮次不同或 delivery 淘汰时，保留执行事实、放弃业务后果；attempt 不同则另判 FAILED。 | 作为领域共用规则的基准；attempt 和终态分支保留。 |
| [`results._check_revision`](../../app/controler/workflow/results.py#L303-L318) | 缺少 Flow、终态、取消、旧轮次或 delivery 淘汰时跳过远程校验。只处理 REVISION。 | 终态及缺失检查留在本阶段；其余复用领域规则。 |
| [`results._prepare`](../../app/controler/workflow/results.py#L378-L400) | 合法 merge notice 先单独处理；终态/旧轮次在读取 batch 前返回，其他作废在读取 batch 后返回。两次返回的 `attempt_id` 来源可能不同。 | 保留前置短路、batch 读取和 plan 构造顺序，只替换后面的共用条件。 |
| [`results._commit`](../../app/controler/workflow/results.py#L605-L637) | 仅复查 delivery 是否淘汰 job，控制本地 session 等后果。 | 保留窄检查，不将它扩大为另一次全条件判断。 |
| [`effects.run`](../../app/controler/workflow/effects.py#L89-L138) | 按 plan 的 archive/notice 取消豁免、轮次、attempt、终态和 delivery 判断；未发的跳过，已发但结果不确定的继续只读对账。 | 四项 plan 归属条件与 `_dispatch` 共用内部函数；delivery 单独保留。 |
| [`effects._dispatch`](../../app/controler/workflow/effects.py#L218-L276) | I/O 后重查同样的 plan 归属条件，另核对 job、digest、applied；归属变化抛 ResultPending，delivery 变化另抛 DeliveryConflict。 | 共用归属条件，保留复查时点、完整性检查、异常类别和先后顺序。 |

另有两处相关条件有意不合并：[`prepare_for_assignment`](../../app/controler/workflow/coordinator.py#L173-L181) 对 CLOSED 返回 DONE、对 MERGED 记录失败；[`_apply` 的副作用失败恢复](../../app/controler/workflow/results.py#L234-L258) 决定是否 BLOCKED 并等待显式重试，现有条件不包含 round 和 archive_reason，不能替换成更宽的作废规则。

这些差异有具体用途：BLOCKED 必须允许恢复已接收的结果；effects 中 attempt 不匹配只停止旧 plan 的新写入，最终提交中 attempt 不匹配仍须失败；当前轮已归档的通知可发送，旧轮通知只对账。[结果提交边界](../../app/controler/workflow/results.py#L272-L300)、[已归档通知设计](m3-workspace-archive.md#三个独立提交边界)、[旧轮通知回归](../../tests/test_archive_status.py#L194-L220)。

### 两个共用定义，不新增配置或类型

**领域共用规则：** 在已有 `Flow` 模型、`accepts()` 邻近位置增加：

```python
def retires(self, job: Job, delivery: FlowDelivery | None) -> bool:
    ...
```

只判断三件事：job 轮次不同；Flow 已有 `archive_reason` 且 job 不是 ARCHIVE；delivery 的现有 `supersedes(job.kind)` 为真。不判断终态、attempt 或派发资格，不访问数据库。`False` 只表示未被这三项淘汰，不授予执行权限。

选择现有模型作为规则所有者，与 [`Flow.accepts()` 和 `FlowDelivery.supersedes()`](../../app/domain/models.py#L147-L201) 的放置方式一致，不新建 policy 文件、事实 DTO 或包装类。文件已有延迟注解，可引用后定义的 Job / FlowDelivery。接收现有 Job，避免调用方反复拆传 kind 和 round。

在 coordinator 的既有 `_retired()` 中保留 delivery 读取和 `accepts()`；在 results 最终提交、revision 预检查以及 `_prepare` 的后半段复用 `retires()`。`_prepare` 的前置终态/轮次短路仍保留：不能为了消除一行重复改变冻结 plan 的 attempt 归属或开始读取旧轮 batch。

**effects 内部共用规则：** 在 `effects.py` 增加模块私有纯函数：

```python
def _plan_stale(flow: Flow, job: Job, plan: ResultPlan) -> bool:
    ...
```

只判断四件事：终态且没有 archive_notice；Flow 的 attempt 与 plan 不同；job 轮次不同；Flow 已有 `archive_reason` 且 plan 的 archive 与 archive_notice 都为空。`run()` 和 `_dispatch()` 必须共用，防止远程读取前后采用不同的计划归属规则。

函数不包含 delivery：`run()` 将其作为只读对账的触发条件，`_dispatch()` 仍在原位置抛 DeliveryConflict。也不包含缺少 Flow/job、digest 冲突、已应用等完整性检查，这些留给调用方处理。

这两组取消规则不能继续合并：领域规则按 job.kind 豁免 ARCHIVE，effects 按冻结 plan 是否有 archive/notice 豁免；失败 ARCHIVE 的 plan 可能两者都没有。完整归档权限继续由 [`verify_archive` / `verify_merge_notice`](../../app/controler/workflow/archive.py#L43-L86) 校验，不能凭函数返回 False 绕过。

### 替代方案、成本与保留边界

- **全阶段一个函数加 allow_terminal/allow_archive 等开关：** 每个阶段都需理解参数组合，掩盖 attempt 的不同失败语义；不采用。
- **返回原因枚举或字符串：** 当前调用方不消费统一原因，错误及恢复方式也不同；只返回 bool，不新增原因类型。
- **保留所有内联判断：** 改动为零，但结果侧共用规则继续分散，effects 前后检查仍需手工同步；不采用。

代价是两个有明确用途的新方法/函数，以及调用时少量参数传递。没有新层、新文件、配置、数据库迁移或外部兼容成本。主要风险是为了“统一”误改短路顺序、plan.attempt_id、异常类型或归档豁免；按上表逐处等价替换，不顺带修正或强化现有行为。

保留逐 job 锁、workflow/job 锁、事务边界、I/O 后复查、发送前持久标记、marker、原 plan/digest、`retire_for_merge()` 的不确定写入对账，以及所有 archive/notice 身份校验。两次检查可以共用函数，不能删掉任一检查时点。[退役 effect 的持久处理](../../app/persistence/results.py#L188-L201)。

### 实施顺序

1. 获批后重新读取共享差异，在最新基线隔离本项改动，保留第 1 项及其他任务。先运行现有定向回归建立基线。
2. 增加 `Flow.retires()` 并逐处替换真正等价的结果侧条件；保持派发资格、前置短路及事务归属。
3. 增加 `_plan_stale()`，替换 effects 的两份归属判断；不调整 delivery 检查或异常顺序。
4. 完成等价对照、定向及全量回归、静态检查和打包；删除临时校验文件，更新本记录的实施证据。只交付本项改动。

纯内部重构，不需要数据迁移、排空 Flow 或协调 wire 升级；回滚只回滚本项代码，不改持久数据。推送、PR、合并和部署没有获得本轮授权。

### 验证与简化对照（待实施后执行）

**现有公开行为回归优先，不为 helper 单独新增永久测试。** 先运行 `test_coordinator.py`、`test_result_recovery.py`、`test_revision_recovery.py`、`test_archive_status.py`、`test_archive_recovery.py` 和 `test_merge.py`。重点保留以下可观察结果：

- merge/cancel 发生在远程查询与 dispatch 之间，不产生新评论、review 或错误的 session/delivery 更新。[I/O 后合并](../../tests/test_result_recovery.py#L322-L346)、[校验或提交前合并](../../tests/test_revision_recovery.py#L490-L524)。
- 已发评论结果未知时不重发；有远端回执才结清，没有回执继续 pending。[merge/cancel 对账矩阵](../../tests/test_result_recovery.py#L349-L398)。
- BLOCKED 中的已核验结果仍能在显式 retry 后完成；merge/cancel 后不再派发旧结果的副作用。[失败通知恢复](../../tests/test_revision_recovery.py#L401-L487)。
- 当前轮归档通知正常完成，旧轮次只观察已发送写入，新 Issue owner 的标签不被覆盖，伪造通知仍被拒绝。[通知恢复与身份边界](../../tests/test_archive_status.py#L140-L254)。

**临时等价性与简化对照：** 遍历 FlowState、ArchiveReason、JobKind、当前/旧轮次、delivery 缺失及各状态，将原内联表达式与拟议方法比较；对 plan 另覆盖 attempt 相同/不同（含 None）以及无 archive/ARCHIVE 结果/merge notice。分别检查各调用阶段，不能拿一个 helper 的结果作为另一个 helper 的预期。包括 ARCHIVE kind 相同、plan.archive 却为空的反例，证明两组规则不能合并；也包括 BLOCKED 下拒绝新派发但允许旧结果恢复的对照。保留 `_prepare` 两个返回路径的 plan.attempt_id，对照 `_dispatch` 两种异常归属。临时脚本运行后删除，结论记入本记录。

本项不新增锁、账本或运行机制，不重复消融既有安全机制。上述反例用于判断能否进一步合并，若合并改变行为就保持两个职责；在实施后、代码审查前执行。

随后在隔离 PostgreSQL 上运行完整 `uv run pytest -q`，以及 `uv run ruff check app tests`、`uv run mypy app tests`、`uv build --no-sources` 与 `git diff --check`。复用现有测试即可覆盖的情形不新增测试；若发现可达场景缺少保护，在原文件扩展最小公开入口用例。不要以修改既有预期来适配重构。

### 未决事项与当前证据

没有需要用户补充的产品选择，待确认上述范围后实施。已读取调用点、模型、相关持久规则、现有回归、归档设计和历史变更；没有运行产品测试，尚不能声称重构已保持运行行为。

计划草稿通过本地 Claude Code 的 `claude-opus-5-5` 生成，再按实际源码补充调用方、模型与边界；该咨询无工具或 MCP 权限，属于设计输入，不是执行验证。当前只保存本地文档并提供线程内审阅副本。

## 2. PR 变更时间线

### [001] — 2026-11-15 — 收敛共用规则，保留阶段与冻结计划的边界

- **需求与实施：** 按本线程批准的第 2 项范围，在 `Flow` 增加纯判定 `retires(job, delivery)`，由 coordinator 和 results 的四个调用点复用轮次、非 ARCHIVE 的归档原因及 delivery 淘汰规则。在 `effects.py` 增加私有 `_plan_stale(flow, job, plan)`，由 `run()` 和远程读取后的 `_dispatch()` 共用归属判断。没有新增配置、类型、生产文件、数据库或 wire 字段。
- **保留边界：** 派发资格继续由 `accepts()` 决定；BLOCKED 中的结果恢复不套用派发限制。`_prepare()` 的终态/旧轮次短路仍在读取 batch 前，后续作废仍保留 batch 的 attempt。最终提交的 attempt 不匹配仍判 FAILED。`_commit()`、副作用失败恢复、逐 job 锁、事务、冻结 plan/digest、写入前标记及不确定写入的只读对账未改。
- **成本与取舍：** 非终态但已取消或旧轮次的 revision 预检查现在会先读取一次 delivery，再由领域方法返回；接受这次只读查询以统一规则，不改变 GitHub 校验或写入行为。没有把 plan 的取消豁免改成按 job.kind 判断；`verify_archive()` / `verify_merge_notice()` 和 delivery 的独立异常仍在原阶段执行。
- **新旧实现对照：** 从上述 main 基线用 `git show <base>:<path>` 读取原代码，直接提取其表达式，与实际新方法逐项比较。覆盖全部 FlowState、无/两种归档原因、INITIAL/REVISION/ARCHIVE、前/当前/后轮次、无 delivery 及全部 DeliveryState；plan 另覆盖双方 attempt 为 None/两个不同 ID，以及无归档结果/ArchiveResult/MergeArchiveNotice。领域规则、effects 归属及缺失 flow/job、digest 变化、已应用等 dispatch 保护均等价。矩阵包含合成状态组合，单独辅以真实 PostgreSQL 公开行为回归，不将矩阵视为所有业务场景可达的证明。[对照日志](../../.amp/in/artifacts/job-retirement-equivalence.log)
- **不能继续合并的实测反例：** BLOCKED 可以拒绝新派发而不淘汰当前结果；FINALIZING 的失败 ARCHIVE 没有 archive/notice，领域规则不淘汰该 kind，但 effects 必须停止新写入；当前轮 merge notice 在 ARCHIVED 下仍可继续，旧轮或不同 attempt 仍须停止新写入。更窄的两组定义保留，未增加豁免开关。
- **冻结计划与异常：** 在现有 `pending_revision` PostgreSQL fixture 上，将同一已接收结果和 Flow 快照传给新旧 `_prepare()`，旧轮、取消、关闭 PR 三个分支的完整序列化 plan 相同；旧轮使用当前 attempt，后两者保留 batch attempt。对实际持久 plan 分别改变 attempt 和 delivery，新旧 `_dispatch()` 同样抛 ResultPending / DeliveryConflict，均未写入 dispatch 标记。旧轮和取消的 `_prepare()` 对照使用合成 Flow 快照，不宣称新增公共 API 场景。[同一对照日志](../../.amp/in/artifacts/job-retirement-equivalence.log)
- **执行方式：** 临时对照命令为 `FLOWTOOL_TEST_DATABASE_URL=<isolated-UTF8-DSN> uv run pytest -q -s .amp/in/test_job_retirement_checks.py`；临时脚本只作本轮验证，运行后已删除，不纳入产品或正式测试。未修改既有测试或其预期。
- **回归与构建：** 隔离 PostgreSQL 使用 UTF8 数据库；最初 SQL_ASCII 数据库在 fixture 建表时失败，改用 template0 创建 UTF8 数据库后重跑，未以产品改动规避环境问题。实施前后相同定向命令均通过；完整 `uv run pytest -q --tb=short` 通过，保留其 skip 和 Starlette 弃用警告；`uv run ruff check app tests`、`uv run mypy app tests`、`uv build --no-sources` 与 `git diff --check` 通过。[基线](../../.amp/in/artifacts/job-retirement-baseline-utf8.log)、[定向回归](../../.amp/in/artifacts/job-retirement-targeted.log)、[全量回归](../../.amp/in/artifacts/job-retirement-full.log)、[静态检查与构建](../../.amp/in/artifacts/job-retirement-static.log)
- **当前结果与剩余事项：** 本地实现与验证完成；本任务的临时脚本、字节码及独立 PostgreSQL 实例已清理，验证日志保留。代码只完成本地自查，尚未运行发布前独立审查门禁；未创建 PR、未推送、未部署。

### [002] — 2026-11-15 — 对齐已合并的 legacy 清理，独立发布第 2 项

- **上下文与集成：** 发布前 fetch 确认 PR #168 已合入 main，将本项工作副本直接对齐 `f77b4136`。产品改动自动合并；CHANGELOG 和计划索引的同位置新增冲突手工保留双方条目，本项差异仍只拥有第 2 项，不恢复已删除的兼容代码或临时 DEMO。
- **对照适用性：** 第 [001] 项对照使用首次实施基线 `2692f59f`；本轮逐一比较新旧基线的 `_retired`、`_apply`、`_check_revision`、`_prepare`、`_commit`、`_dispatch` 以及 effects 的 cancelled/retired 表达式 AST，均未改变，因此原判定、冻结 plan 和异常对照仍适用于发布基线。[基线对照](../../.amp/in/artifacts/job-retirement-pr-baseline-comparison.log)
- **发布基线验证：** 在新的隔离 UTF8 PostgreSQL 上执行第 [001] 项相同定向命令和完整 `uv run pytest -q --tb=short`，结果分别保存在[定向回归日志](../../.amp/in/artifacts/job-retirement-pr-targeted.log)与[全量回归日志](../../.amp/in/artifacts/job-retirement-pr-full.log)。Ruff、mypy、`uv build --no-sources` 与差异空白检查重跑通过。[静态检查与构建](../../.amp/in/artifacts/job-retirement-pr-static.log)
- **环境处置：** macOS 上 PostgreSQL 首次启动因宿主 locale 触发 multithreaded startup 保护；以 `LC_ALL=C LANG=C` 启动同一隔离实例后恢复，未修改产品或测试。
- **交付边界：** 使用本项 bookmark 创建独立 PR；发布前门禁固定审查 commit，只推送通过门禁返回的精确提交。实际独立审查结果和 GitHub CI 状态见本线程与 PR，不包含合并或部署。
