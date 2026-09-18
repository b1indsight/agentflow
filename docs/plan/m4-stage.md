# M4 阶段计划：并发收敛、GitHub 对账与 Projects 看板

> 状态：F1/F2 已推送；F3 已实现并验证；F4–F9 待依次推进。日期：2026-09-09。
> 基线：`main@origin` / `be65878f`；M3 #24–#26 及归档恢复修复 #27 已合并。
> 阶段总表随 F1 PR 维护；每个 feature 有自己的 bookmark 和 draft PR，批准后在原 PR 实施。
> 用户统一 review 和合并；本轮不执行合并。M1–M3 的真实 E2E 待办继续保留。

## 1. 检查结果与 M4 实际范围

| 原路线图 feature | 当前代码事实 | 本阶段增量 |
|---|---|---|
| 多 Flow 并发 | 多 worker、公平领取、Flow 独占 binding/repo、持久 lease 和同 Flow 串行已实现 | F2 消除恢复队列与本地 sweep 的等待耦合，补多 Flow 故障隔离 |
| reconcile 对账 | `control/reconcile.py` 只记录日志；M2 已有临时远端观察能力 | F3–F5 持久扫描已追踪 PR/反馈，F6 补 close/reopen |
| 错误恢复 | result inbox、effects、worker journal、归档重试已有；准备期读失败仍需人工 retry | F1 修已知版本重放，F7 仅重试可证明尚未派发的只读准备 |
| Project 看板 | 尚无 Projects port、配置或同步 | F8 最小 App/API 能力，F9 可恢复状态投影 |

检查依据：`app/control/gateway.py:recover_pending_results`、
`runtime.py:_sweep_assignments`、`reconcile.py:_sweep`、
`review_store.py:_reconcile_edits/observe_threads` 及
[M2 已知缺陷](m2-review-resolution.md#同秒重复-webhook-重放medium未修复)。
M4 的“对账”限定为已经被本系统追踪的 Flow，不能宣称补偿了所有 GitHub 事件。

## 2. 九个 feature 与行数预算

按最终 feature PR 相对其最新合并基线的 **新增行 + 删除行** 控制规模，包含产品、测试、
schema、共享替身和文档；锁文件、生成文件单独报告，不用其净行数抵消手写改动。
目标 **800–1100 行/PR**，约 1000 行；下列数字为设计估算，合计约 **8790 行**。

| Feature / 计划入口 | 产品 | 测试 | 文档 | 合计 |
|---|---:|---:|---:|---:|
| F1 [同秒反馈版本去重 · #28](https://github.com/b1indsight/agentflow/pull/28) | 300 | 370 | 180 | **850** |
| F2 [多 Flow 恢复并发与调度隔离 · #29](https://github.com/b1indsight/agentflow/pull/29) | 360 | 480 | 100 | **940** |
| F3 [持久 PR 对账与漏失合并补偿 · #30](https://github.com/b1indsight/agentflow/pull/30) | 480 | 430 | 100 | **1010** |
| F4 [Issue/PR 讨论与 Review 正文对账 · #31](https://github.com/b1indsight/agentflow/pull/31) | 440 | 450 | 100 | **990** |
| F5 [Review 线程持续对账 · #32](https://github.com/b1indsight/agentflow/pull/32) | 390 | 480 | 100 | **970** |
| F6 [未合并 PR 关闭与重新打开 · #33](https://github.com/b1indsight/agentflow/pull/33) | 520 | 450 | 110 | **1080** |
| F7 [准备期只读 GitHub 故障重试 · #34](https://github.com/b1indsight/agentflow/pull/34) | 420 | 460 | 100 | **980** |
| F8 [GitHub Projects v2 最小接口与配置 · #35](https://github.com/b1indsight/agentflow/pull/35) | 410 | 440 | 100 | **950** |
| F9 [Projects 状态投影与恢复 · #36](https://github.com/b1indsight/agentflow/pull/36) | 460 | 450 | 110 | **1020** |

预计超过 **1200 行** 时，在实施前或增量检查时拆出可独立验收的 feature，修订依赖和计划；
不能删减竞态/恢复测试、压行、批量移动文件来满足预算。行数小不等于完成，正确性验收优先。
各计划最后一节列出优先另拆范围；必需正确性工作仍超预算时继续拆主闭环，不能直接省略。

## 3. 依赖与交付顺序

建议按 F1→F2→F3→F4→F5→F6→F7→F8→F9 的顺序逐个实施，减少共享 Store/runtime 的冲突。
依赖表表达技术前置。按用户后续指示，无需等待合并：F1 以 main 为 base，F2–F9 依次以
上一项已推送的实现分支为 base；各 PR 只展示自身增量，建议按 #28→#36 顺序统一 review/合并。

| Feature | 必需前置 | 独立验收结果 |
|---|---|---|
| F1 | 现有 main | 同秒迟到编辑不复活旧反馈 |
| F2 | 现有 main | 慢 Flow 不拖延其他 Flow 的结果/lease/settle |
| F3 | 现有 main，建议先完成 F2 | 漏失合并 webhook 仍进入唯一归档 |
| F4 | F1、F3 | Issue/PR 评论与 review 正文漏投可补收 |
| F5 | F1、F3 | 行内反馈及人工线程状态可持续观察 |
| F6 | F3 | 普通 close 暂挂，符合基线的 reopen 安全恢复 |
| F7 | F2 | 准备期暂时读失败可有界自动恢复 |
| F8 | 现有 main | 可配置、只读校验及经过契约测试的 Projects adapter |
| F9 | F3、F6、F8 | 一 Issue 一卡，终态及失败后状态最终同步 |

F4/F5、F8 在设计上可分别推进，本轮仍逐项实现。每项把原计划 bookmark 更新到前项实现，
在同一 PR 中测试、实现与审查。阶段总表保持在 F1，不再另开总实现 PR。
后续 feature 合并时将本表入口更新为仓库内计划链接，并记录实际 diff 行数及验收证据。

## 4. 共用技术约束和明确取舍

- 保留单 serve、多 worker；一 Flow 全生命周期固定 worker/repo，复用 PostgreSQL 事务、
  assignment token/epoch、result inbox、effects 和 journal，不建立第二套调度/结果框架。
- GitHub 读取在事务外，提交时核对 Flow/attempt/delivery/item 版本；页进度与观察结果
  同事务保存。incomplete、未知结果和 404 都不能冒充“没有反馈/已删除/已完成”。
- 每轮请求、任务数量和分页量有界；退避持久化，不让一个失败 Flow 卡住整轮。
  遵守服务端 Retry-After，做有限重试；依据 [GitHub API 最佳实践](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)。
- Projects 默认按组织 Project v2 + 现有 installation 认证规划，默认关闭；尚未提供具体目标。
  原 Issue 是卡片内容，一 repo 最多一 Project，只同步受管 Status；权限依据见
  [GitHub 官方说明](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)。
  个人 Project 如需新增认证，先另拆，不把凭据支持隐含进 F8 的 950 行预算。
- 看板失败不能阻塞 M3 归档、释放和 ACK。Project 展示表独立于绑定 job 的 ResultPlan，
  能在 Flow 已终态后继续恢复；不是新的业务生命周期状态。
- 自动 merge/rebase、人工 push 自动接纳、跨 worker session 迁移、多 serve HA、
  cancel kill、关闭 PR 自动清理、强隔离和完整指标告警不在本轮九项内。
- 未创建 Flow 的 trigger 漏投、旧 stop/unlabel 控制命令补执行、删除反馈自动推断、
  全历史导入和 Project 双向拖卡控制另列后续；现有对应缺口不宣称已修复。

## 5. 验证与 M4 完成条件

每个 PR 先写证明具体缺陷/边界的失败测试，再实现；数据库竞态和恢复使用实际 PostgreSQL。
优先跑变更相关文件，通过后跑一次完整检查：`uv run ruff check app tests`、
`uv run mypy app tests`、配置 `FLOWTOOL_TEST_DATABASE_URL` 的 `uv run pytest -q --tb=short`、
`uv build --no-sources`。数据库用例跳过不能记作恢复验收通过。

| 阶段真实验收 | 通过证据 |
|---|---|
| 两条 Flow、两个 worker，同仓库不同 Issue | 独占 repo/branch/PR；A 请求暂挂期间 B 可交付 |
| 丢弃合并及三类反馈 webhook | poll 补齐当前事实、唯一批次/归档，无旧版本回退 |
| 429/503、页中断、serve/worker 重启 | 有界退避、续页、token/epoch 与 ACK 恢复正确 |
| close/reopen 和人工 head 漂移 | 无旧任务越界交付；正常 reopen 恢复，漂移可诊断阻塞 |
| merge→cleanup→状态回写→Project Done | 归档释放正确；Project 离线后补齐，不影响归档成功 |

真实 smoke 在已有授权的测试仓库、GitHub App、worker 和 Project 上执行，记录时间、PR、
日志与结果；当前没有这些验收证据。本次计划只做文档/链接/diff 检查，不运行产品测试。
M4 完成需九项各自实现并验证，以及上述真实矩阵通过；M1–M3 的未验项需补证或明确仍未通过，
不能用自动化替身测试替代外部闭环。
