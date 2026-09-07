# M2 Feature 1/4：持久反馈批次与 settle

> 状态：待审批，plan only · 日期：2026-09-07 · 基线：`main` / `9fd98266`（PR #18）
> Bookmark：`feat/m2-feedback-batches`；本 PR 获批后继续承载实现，不另开实现 PR。

## 1. M2 需求分析与 feature 拆分

[路线图](../architecture/roadmap.md)定义 M2 为语义 Triage（bub）门与回喂循环：
`settle → revise / answer / wait`，包含 resume、兜底与自动 resolve。
用户应能在同一 PR 上讨论、提出修改、得到答复并继续审核；系统应保留尚未决议或未处理的反馈。
本次只提交四份计划，各自创建独立 draft PR；下面是实现依赖，不是四个 PR 的堆叠关系。

| Feature | 交付结果 | 实现前置 | 计划文件 |
|---|---|---|---|
| F1：持久反馈批次与 settle | 重启不丢反馈，运行中的新反馈进入下一批 | 最新 main | `m2-feedback-batches.md` |
| F2：bub 语义决策与答复 | 普通讨论经语义门决定 revise/answer/wait/ignore | F1 | `m2-semantic-triage.md` |
| F3：可靠修订与会话兜底 | 同 repo/session 修订，验证 commit/push，安全恢复失效会话 | F1、F2 | `m2-revision-delivery.md` |
| F4：review 线程回复与自动 resolve | 把处理说明发回正确线程，确认后 resolve，并验收完整 M2 | F1、F2、F3 | `m2-review-resolution.md` |

建议实现顺序 F1 → F2 → F3 → F4。每个 PR 从相同 main 单独分出，仅含本 feature 的计划；
实现前在同一 bookmark 更新到已合并前置。审批计划不等于提前合并计划 PR。

### 当前代码事实与缺口

| 当前事实 | 对 M2 的影响 | 负责 feature |
|---|---|---|
| `SettleScheduler` 用 `asyncio.Task` 保存计时，事件去重与反馈写入分别提交 | 重启丢到期调度；事件已去重但反馈未写入时重投也无效 | F1 |
| task prepare 与 result apply 都重新查询 `unrelayed_items()` | 执行期间新来的反馈可能被本轮结果误标为 relayed | F1 |
| Flow 生命周期固定绑定单槽 worker，结果已使用工作流事务与副作用账本 | 复用现有串行执行与恢复机制，补齐批次串行生命周期 | F1–F4 |
| `uv.lock` 和已安装包为 bub 0.4.1，Judge 注释仍写 pre-0.4 | 需要校准真实契约；现有 JSON 提取和错误处理不足 | F2 |
| `pr_context` 只有 repo/PR/Issue 编号；ANSWER 会另调模型且未消费反馈 | 缺少判断证据，可能重复答复或答非所问 | F2 |
| REVISION 无新 push capability、无 VCS/远端 SHA 核验，缺 session 直接失败 | 首轮能力尚未延伸到修订；exit 0 不能证明交付 | F3 |
| addressed 直接标本地 resolved；thread 查询只取前 100 个线程、前 20 条评论 | 无法证明处理的是正确且完整的 GitHub 线程 | F4 |

架构中部分 review-loop 描述仍基于旧状态机；实现基线以代码为准：Flow 使用
`ACTIVE/SUCCEEDED/CANCELLED/FAILED`，delivery 另有 `OPEN/CLOSED/MERGED`，不恢复旧
`PR_OPEN/REVISING` Flow 枚举。settling/running/waiting 从批次、job 和 delivery 推导。

### M2 的共同边界与验收

- 保持一个 Flow、一个独占 worker/repo、一个稳定 branch、最多一个 PR；同 Flow 执行串行。
- Webhook 只完成轻量持久交接；Judge/CodeAgent 在 worker 执行；GitHub API、数据库在 control。
- 普通反馈进入 Judge；allowlist 用户明确提及配置的 bot 并附带指令、或提交 Request Changes，
  可绕过 Judge，但仍持久化、合批并遵守轮数、状态与交付校验。
- wait 保留问题并等待新信息；answer/ignore 不产生 Codex 修改；低置信度或模型失败不能触发改码。
- 真实 M2 验收包含两轮修订、答复、等待后继续、serve 重启、失效 session 兜底和真实线程 resolve。
  M1 真实 GitHub App/Codex E2E 尚未验收，作为联调前置记录，不因这些计划而宣称完成。
- M3 merge/archive 收尾、M4 完整 reconcile/自动 rebase/人工 push 合并、多 serve HA、跨 worker
  迁移、Flow 派生、预算系统、运行中 kill、`/flow restart` 和 M5 隔离部署不纳入 M2。
  本期遇到远端漂移保守暂停，不覆盖他人提交；沿用现有 stop/cancel 的调度和结果 fencing。

## 2. 本 feature 的目标与范围

把反馈入口到 TRIAGE/强指令 REVISION 的交接变成可恢复、可去重、可审计的批次。
关键例子：A/B 评论触发一轮处理，执行期间到达 C；结果只确认 A/B，C 在后续批次处理。
同一评论被编辑也必须视为新版本，不能因为旧版本已 relayed 而丢失更新。

本 feature 包含持久 settle、反馈版本、批次快照、同 Flow 批次串行、强信号路由，以及批次 ID
贯穿现有 TRIAGE/REVISION/ANSWER 的兼容接线；不在这里校准 bub 或完成真实修订与线程处理。

## 3. 数据与事务设计

新增专用 `review_store.py` 封装反馈事务；复用 `Store.transaction()` 和 `lock_workflow()`，
schema 初始化/升级仍使用仓库现有方式，不引入新的迁移框架。

- 反馈身份使用 `(flow_id, kind, github_id)`，保留 attempt 来源；不同 GitHub 对象的数字 ID
  不视为同一命名空间。每次有效变更保存版本、来源 delivery、GitHub 更新时间、正文和定位信息。
- 以不可变反馈版本保存批次输入；当前 item 可保留查询投影。删除是 tombstone，不能复活旧正文。
  较旧 edited 事件不覆盖新版，完全相同的重复 delivery/版本不推进 settle。
- 持久缓冲保存 `flow_id`、generation、`due_at`、强信号原因；更新、合批和领取均在 Flow 锁内。
- 批次保存 `batch_id`、Flow/attempt、冻结的 item/version 列表、触发类型、状态、来源/派生 job；
  状态至少表达 ready、processing、waiting、completed、cancelled。冻结 PR 内容由 F2 补齐。
- 数据库唯一约束保证同 Flow 最多一个 ready/processing 批次；buffer 可以在其运行时继续收集。
  waiting 批次在新 generation 到来时由新批次显式接续，旧批次不因定时 sweep 自动再次运行。
- item 的“已交给模型”“已消费当前版本”“远端线程 resolved”分开表达；`relayed` 不再兼任三者。

对本期反馈事件，delivery 去重、反馈版本写入、buffer 更新属于一个事务：失败全部回滚，使重投可重试。
终态 Flow、无关联 PR、非目标 action 的事件有明确忽略结果；不改写其他 M1 事件的处理语义。
反馈写入成功后再通知 scheduler，通知丢失也由持久到期扫描补偿。

claim 到期 buffer 时冻结输入，创建唯一来源键的 job，并更新批次状态，三者原子提交。
任务 prepare 读取批次快照；result apply 只确认 task 对应的 item/version，不能再查询全部
`unrelayed_items()` 作为本次完成集合。旧 attempt/result、取消或已被新版覆盖的标记都经锁内校验。
所有网络调用在事务之外；结果仍走现有 `ResultApplication`、result inbox 和 effect ledger。

## 4. 调度与路由行为

| 事件/结果 | 行为 |
|---|---|
| 普通 conversation、inline、带正文的 review | 追加版本，重置普通静默窗口；review submitted 可使用较短 grace |
| approve、其他 bot/self、重复或无有效变化事件 | 不触发修改或重复批次；不将普通文本中的引用误识别为命令 |
| allowlist 用户的明确 bot 指令、submitted Request Changes | 持久化强信号，以 submit grace 收集关联 inline，绕过 Judge 后创建一个 REVISION |
| 非 allowlist 用户的同类文本 | 作为普通讨论进入 Judge，不能绕过语义门；普通讨论入口仍保持开放 |
| 空正文 Request Changes | 保存 review 状态本身；无可操作正文/inline 时等待补充，不创建空指令修订 |
| 强信号之后继续收到关联 inline | 保留强信号及 grace 语义，最后一次相关事件后静默到期，不降级回普通批次 |
| 当前有处理中的批次 | 继续缓冲新版本，到期也等当前结果和必要副作用完成后才领取下一批 |
| wait / deferred / clarification 留下未决问题 | 无新 generation 时不自动重跑；新反馈接续问题与未消费反馈 |
| answer / ignore / revision 成功 | 根据结果精确消费本批次版本；下一批不能再次消费相同版本 |
| cancel / terminal Flow | 原子取消 buffer 和未执行批次，禁止继续派生 job；旧结果按现有 fencing 收尾 |

bot 判断覆盖 payload 的 actor 类型与配置的 bot login；edited/deleted/dismissed 等 action 显式分流，
不能把撤销或编辑的 review 当作新的强信号。强信号识别仅取用户明确提交的正文，排除引用/代码块。
执行开始后新增/编辑/删除反馈留给下一轮，不在 webhook 内打断 Codex；F4 在 resolve 前再校验线程。

启动时与周期 sweep 查询持久 due_at；数据库时间/可注入 clock 决定到期，内存 timer 只作为唤醒优化。
复用现有 Flow 单槽 binding，不新增一套 worker 调度器；核对现有 worker/Flow/job 锁顺序。

## 5. 模块与实施顺序

| 文件 | 改动 |
|---|---|
| `app/models.py`、`app/review_store.py`（新增）、`app/store.py` | 版本、buffer、batch、约束与事务 API |
| `app/control/router.py`、`app/control/runtime.py` | 原子反馈交接、持久 settle、启动/周期恢复 |
| `app/control/coordinator.py`、`app/control/results.py`、`app/result_models.py` | 冻结输入、批次状态与精确消费、来源键 |
| `app/protocol.py`、`app/worker/executor.py` | batch/item version 身份跨 task/result 传递与校验 |
| `tests/test_router.py`、`tests/test_store.py`、`tests/test_review_batches.py`（新增） | 路由、并发与重启场景 |
| `tests/test_coordinator.py`、`tests/test_result_recovery.py` | 新反馈不被旧结果消费，旧 result 重放 |
| README、架构 review-loop/components、计划索引 | 完成后同步已验证事实 |

1. 先写 PostgreSQL 回归：去重之后崩溃、settle 重启、处理 A/B 时到达 C、同评论版本更新。
2. 落地 schema 与事务 API，再用可注入 clock 实现 scheduler；先证明重复唤醒只派生一个 job。
3. 接入普通/强信号入口与明确 action 规则，补 allowlist、bot、空 review 和乱序测试。
4. 批次身份贯穿现有任务和原子结果路径；加入精确消费和 waiting 接续。
5. 执行与既有 lease/result recovery 的组合测试，更新协议和操作文档。

## 6. 验证、升级与完成条件

- PostgreSQL：两个 delivery 同时进入、两个 scheduler 同时 claim、事务各阶段故障、serve 重启后到期；
  一批至多一个有效任务，反馈版本均可追溯，没有已去重却丢反馈的提交间隙。
- 行为：窗口内合批、窗口后新批次、强信号合并 inline、执行中追加/编辑/删除、跨 kind 的相同数字 ID。
- 恢复：result replay 不重复派生或消费；cancel 后不新调度；waiting 没有自发循环；新反馈恢复判断。
- 兼容：保持多 worker 公平性和 Flow binding；协议变更按顺序升级版本，旧版本拒绝而非静默误读。
- 升级：维护窗口停止 serve/worker，先完成或明确处理旧 pending results，再升级 schema/protocol；
  旧 relayed=false 数据作为待处理版本导入，旧 resolved 标记不声称已获 GitHub 确认。
  无 batch 的在途旧任务不得猜测消费集合；显式阻止启动并给出待处理 job 清单。
- 执行 `uv run ruff check app tests`、`uv run mypy app tests`、`uv run pytest`、
  `uv build --no-sources`；PostgreSQL 用例需设置 `FLOWTOOL_TEST_DATABASE_URL`，不能以 skipped 验收。

完成标准是确定性自动化证明上述事务与批次行为。真实 bub、Codex 和 GitHub 线程验收分别属于后续 feature。
本 PR 当前只含计划，没有执行产品测试或声明上述功能已经实现。
