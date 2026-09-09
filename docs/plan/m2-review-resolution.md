# M2 Feature 4/4：review 线程回复、自动 resolve 与闭环验收

> 状态：已实现并合并 [PR #22](https://github.com/b1indsight/agentflow/pull/22)，自动化通过；真实 M2 矩阵待验收，另有一项未修复的 Medium。
> 实施日期：2026-09-07 · 合并日期：2026-09-08（Asia/Singapore）· 文档核对：2026-09-08。
> 实施基线：`main` / `f305acd5`（PR #21，前置 #19–#21 均已合并）
> Bookmark：`feat/m2-review-resolution`；计划、实现与审查修复均在同一 PR。
> 实现前置：F1 `feat/m2-feedback-batches`、F2 `feat/m2-semantic-triage`、
> F3 `feat/m2-revision-delivery`。本 feature 是 M2 集成验收入口。

第 1–6 节保留规划基线、方案与验收要求；第 7 节记录实施结果，第 8 节记录合并与剩余工作。

## 1. 目标与规划时缺口

reviewer 应在自己提出问题的线程看到处理说明；系统仅在当前线程的问题已处理且对应代码交付
通过核验后自动 resolve。新评论、延后事项或待澄清问题必须继续保留。

规划时 `GitHubClient.list_review_threads()` 只读取前 100 个线程及每线程前 20 条评论，没有 cursor
分页；返回无类型字典，也未接入 router/结果映射。`ReviewItem.thread_id` 有字段但入口未填充。
`ResultApplication` 把 addressed 的数字 comment_id 直接设为本地 resolved，既不发布 note，
也不生成 ResolveEffect。已有副作用执行器支持 resolve 状态查询和恢复，可直接延伸使用。

本 feature 补齐 GitHub thread/comment 映射、原线程回复、保守的整线程完成判定和持久恢复，
并用真实服务验收 [M2 路线图](../architecture/roadmap.md) 的完整回喂循环。
不实现自动 merge、M3 归档、M4 通用 reconcile、自动 unresolve 或把过时线程一律关闭。

## 2. GitHub 类型、分页与身份

新增类型明确的 ReviewThread/ReviewComment 快照，包含 thread node ID、comment database/node ID、
根评论/回复关系、作者、正文版本/更新时间、path、原始 commit、isResolved、isOutdated，以及
完整性/取数时间。GraphQL 错误继续走当前 adapter 的分类模型，不将 errors 当作成功的空集合。

- 按 cursor 遍历所有 reviewThreads，并独立分页每个线程的 comments；测试跨 100/20 边界。
- 设置有界请求/字节上限。超限、节点消失或部分 GraphQL 错误标记快照不完整，不自动 resolve。
- 映射使用 `(repo, pr, kind=inline, github_comment_id)` 到 thread ID；本地消费与回写使用
  F1 的 item/version。review body、issue/PR conversation 没有可 resolve 的 review thread。
- GitHub comment 的 node ID 不是 thread ID；不能拼接 ID 或把同数字的 review ID 当 inline ID。
- 映射在 control 侧上下文构建时补全，必要时结果处理前刷新；不让 webhook 等待大量 GitHub 分页，
  worker 也不自行调用 GitHub。冻结本轮输入同时保存映射来源和版本。
- 旧本地 resolved=true 数据作为未核验历史：首次读取以 GitHub 当前状态校正，不能跳过验证。

## 3. 处理结果与自动 resolve 条件

F3 提供 batch/item version、已验证的 commit SHA 和 addressed/deferred/clarification。
本 feature 先校验结果属于本轮快照，再按 thread 分组；一次 mutation 影响整个线程，所以不能
看到一个 addressed comment 就认定线程内所有问题均已解决。

| 反馈类别/状态 | 回复位置 | resolve 行为 |
|---|---|---|
| inline addressed | 回复原 review thread，含说明与已核验 commit 链接；明确 no-op 时说明现有代码依据 | 满足全部条件后 resolve |
| inline deferred | 回复原线程，保存原因 | 保持开放；等待后续反馈，不自动重跑 |
| review body / conversation | PR conversation，引用原 review/comment 链接，按 batch 合并说明 | 不做 thread mutation |
| needs_clarification | 能明确关联时回复原线程，否则 PR conversation 提问 | 相关线程保持开放，暂停本轮 re-review |
| 非本批次/不存在/版本不符的 addressed ID | 记录结果校验问题 | 不猜测映射，不关闭线程 |
| 映射缺失或线程数据不完整 | 持久记录未完成处理，可发布一次引用原评论的诊断/说明 | 保留开放，禁止仅设置本地 resolved |

自动 resolve 必须同时满足：

1. F3 已验证本轮交付，PR 仍 open，当前 head SHA 与本轮核验 SHA 相同。
2. thread 属于目标 repo/PR，映射和完整评论快照有效；isOutdated 本身不是解决依据。
3. 该线程所有未决的非 bot 反馈均被已核验批次覆盖，明确 addressed，无 deferred/clarification
   或未报告项；历史批次的 addressed 只有在仍有效、未被新版本覆盖时才可作为处理依据。
4. resolve 前刷新线程状态；本轮快照之后新增/编辑的人类反馈使线程保留开放，并归入 F1 下一批。
   刷新发现但尚未收到 webhook 的评论按来源版本幂等补入 buffer，不能丢弃或重复建批次。
5. 本轮处理说明已成功发送或由 effect ledger 找回相同的 GitHub reply receipt。

已经远端 resolved 的线程可记录当前事实，不再次 mutation；本 feature 不自动重新打开。
若已有未决新回复，即使线程被人工 resolve，也继续保留该反馈供下一批判断，不能当作反馈已消费。
无稳定映射/远端确认时，本地最多记录 addressed，只有查询或 mutation 的确认才能写 remote resolved。

## 4. 副作用顺序与恢复

扩展当前类型化 effect ledger，新增 review-thread reply effect 和按精确标记查询回复的能力。
使用 REST reply-to-review-comment 或校准后的 GraphQL reply mutation，选定一种写路径并固定测试；
不把 PR issue comment API 当成线程回复 API。标记包含 source job/batch、thread 和用途，不含凭据。

本轮处理顺序：冻结处理计划 → 原线程 note/deferred/question → 刷新并判定 resolve 资格 →
resolve 并保存远端 receipt → 符合条件时 summary/labels/re-review → 原子提交本地状态与 result ACK。
per-thread effect 只执行一次；同线程多个 addressed note 合并，避免逐 comment 重复 resolve。
新评论导致不具备 resolve 资格属于正常“仍待处理”结果，不应卡住已成功交付代码的 result ACK。

- 请求前检查 active Flow、current attempt/batch 与 delivery；取消后禁止尚未发出的外部操作。
- 回复成功但响应丢失时，分页查找本 App 作者及精确标记，复用 reply ID；找不到且结果仍不确定时
  沿用账本的 unknown 状态，不盲目再次发送。
- resolve 成功但响应丢失时查询 isResolved，确认后完成本地记录；rate limit/明确未执行的临时错误
  复用现有退避与 claim 恢复，重启不重新运行 Codex。
- 403、删除节点、缺权限、未知 outcome 等使用可诊断状态；不得将整个批次报告为全部解决。
  可恢复错误保持 effect pending；确定不可执行的线程操作记录未完成项，保留反馈供人工处理。
- re-review 仅在有已核验新提交、无未决反馈、必要回复/resolve 完成后执行；失效/缺失映射也作为
  未决事项，不能借由跳过线程步骤提前宣告完成。reviewer effect 复用当前恢复策略。

GitHub 不提供“线程评论版本未变才 resolve”的跨请求事务；刷新与 mutation 之间仍有竞态。
尽量缩短间隔并在 mutation 后复读；检测新回复时保留下一批反馈与审计记录，不声称严格原子关闭。
本期不自动 unresolve，以免覆盖人工操作；更广泛的远端持续对账属于 M4。

## 5. 文件布局与实施顺序

| 文件 | 改动 |
|---|---|
| `app/github_models.py`、`app/github_client.py`、`app/ports.py` | 类型化分页、线程映射、原线程回复/恢复查询 |
| F1 `app/review_store.py`、`app/models.py` | 映射版本、addressed 与远端 resolved 分离、刷新发现反馈入库 |
| `app/control/coordinator.py`、`app/context_builder.py` | 为当前批次补充真实线程身份与上下文 |
| `app/control/results.py`、`app/result_models.py`、`app/control/effects.py` | 按线程判定、reply/resolve 顺序和状态收尾 |
| `app/result_store.py` | 新 effect 类型兼容与恢复持久化 |
| `tests/test_github_client.py`、`tests/test_review_resolution.py`（新增） | 分页、ID、线程完整性、资格判定 |
| `tests/test_result_recovery.py`、`tests/test_remote_e2e.py` | 故障恢复与完整反馈循环 |

1. 先写 GitHub HTTP/GraphQL fixtures：多页 threads/comments、回复 ID、错误/缺权限和部分数据。
2. 落地类型化映射与回复/查找原语；为纯判定规则编写多评论、混合结果和版本变化测试。
3. 接入 F3 交付事实，生成有稳定 key 的 reply/resolve effects，去除 addressed 直接标 resolved 的旧路径。
4. 故障注入验证回复/resolve 响应丢失、cancel、重启、DB 提交失败与新评论竞态。
5. 完成下面的集成验收，更新 README、架构状态、review-loop、GitHub App 权限与各计划实施结果。

## 6. 自动化与真实 M2 验收

自动化必须证明：跨页评论正确映射；跨 kind 数字 ID 不误关；一线程多个问题全部处理才 resolve；
deferred/clarification/遗漏/新版本/远端 SHA 变化时保留；reply 在 resolve 前完成；多次重放只有
一个回复和一次必要 mutation；所有路径不新开 PR、不重新跑 CodeAgent、不错误消费下一批反馈。

执行 Ruff、Mypy、完整 pytest（含 `FLOWTOOL_TEST_DATABASE_URL` PostgreSQL 场景）和
`uv build --no-sources`。fake GitHub/Codex/bub E2E 验证控制流，不代替真实集成验收。
升级前处理旧 pending results/effects；不把旧本地 resolved 标记迁移成已核验远端 receipt。

在配置好的 disposable GitHub repo、App、worker 和真实模型上按以下矩阵记录证据。
截至 2026-09-08，以下场景均为**待验收**，表中描述验收要求：

| 场景 | 验收要求（均待执行） |
|---|---|
| M1 Issue→PR 前置 | 保存原 PR/branch、worker/repo binding 与首轮 SHA，确认可真实 push |
| 普通讨论尚未定论 → 新回复形成结论 | 先 wait，无改码；新反馈后只派生一次 revision |
| 代码问题、闲聊 | answer 只发一次且不改 SHA；ignore 不发无意义回复 |
| Request Changes + 多条 inline、明确 bot 指令 | 仅授权 Request Changes 经 grace 合批直达 revision；bot 指令进入 Judge，均受交付/轮数限制 |
| 两轮正常修订 | 同 session/repo/branch/PR，实际新 SHA；处理说明返回正确线程 |
| 修订中追加或编辑评论 | 不被旧结果消费，不误 resolve，下一轮继续处理 |
| session 丢失 | 干净原 repo 一次 start 兜底；保留同一 PR 和批次，更新 session 记录 |
| deferred / clarification → 人工补充 | 保持线程开放且不 re-review；补充后恢复并正确处理 |
| settle 中 serve 重启、结果/ACK 恢复 | 反馈与任务不丢失；无重复修订、回复和 resolve |
| 有效 addressed 与分页线程 | 确认远端回复/resolve；记录 thread/comment ID 与核验 SHA |

真实 PR 评论与 resolve 属于配置授权测试环境后的验收操作，目前尚未执行。
记录运行日期、CLI/adapter/model 版本、GitHub 对象链接、数据库批次/结果摘要和失败项，禁止记录密钥。
任何未运行场景单列“待验收”；只有自动化与真实矩阵都达到预期，才把 M2 标为已验收。

本 PR 已包含实现；M3 的人工 merge/归档和 M4 的通用恢复能力不因本次实现而视为完成。


## 7. 实施记录（2026-09-07）

- 新增 `github_threads.py` 承担 adapter 内分页与预算，类型化 snapshot 存入 control 的 batch.threads；
  保持 worker protocol v7，不改变已冻结 Judge context digest。通过原线程根评论 REST endpoint 回复。
- 新增 `control/resolution.py` 规划每线程说明与覆盖依据；`control/thread_effects.py` 刷新、入库、resolve
  和复读；result ledger 持久化回复及实际远端状态。历史 addressed 只接受当前 attempt 的最新版本。
- 线程权限/删除/映射错误写未完成诊断；新版本或未决问题保持开放并抑制 re-review。
  查询落后于 webhook 的同秒版本冲突保留本地新反馈；未知写结果继续 uncertain，禁止盲目重发。
- PostgreSQL 用例覆盖丢响应重放、取消、提交失败、迟到 webhook、两轮修订、历史覆盖、同数字不同 kind、
  多评论合并、deferred、no-op、映射不完整，以及人工 resolve / mutation 期间的新评论。
- 全套 Ruff、Mypy 通过；pytest **377 passed / 0 skipped**（真实 PostgreSQL，本机 Git/jj），
  `uv build --no-sources` 成功生成 sdist/wheel；保留原有 Starlette/httpx 弃用告警；外部 GitHub/Codex 使用替身，bub 契约使用假 provider。
- 真实矩阵所有场景均为**待验收**：尚无授权的一次性仓库、GitHub App 和真实模型的完整验收记录。
  没有执行该矩阵要求的真实 GitHub 评论/resolve 或模型调用，缺少对应对象链接、版本和数据库运行证据。
  测试环境配置并授权后逐项执行第 6 节矩阵；M2 保持“已实现并合并、真实验收未完成”。

### 审查修复（2026-09-07）

用户明确授权修复首轮审查的 3 medium / 1 low；同一 bookmark/PR 继续提交：

- 同秒 observation 使用冻结 item/version，防止 A→B→A 后的旧 B 查询回退当前版本。
- 复用持久 edit-chain reconciliation，远端补齐前驱或重读已有前驱时继续处理排队编辑，再调度下一批。
- label/reviewer 在 dispatch 事务内重查反馈，新输入使通知跳过并写待处理诊断，不把 result 卡成失败。
- 按 comment ID 建立 inline 依据索引，每个 resolve effect 仅保存所属线程内容；20 个双评论线程
  的回归场景从重复保存 1,200 份反馈缩减为 40 份，保留当前及历史版本的覆盖判定。
- 新增 6 项回归用例，均先复现原问题；线程与修订测试共 71 项通过；
  完整 pytest **377 passed / 0 skipped**（真实 PostgreSQL），Ruff、Mypy、`uv build --no-sources` 通过。

## 8. 合并状态与剩余工作（2026-09-08）

[PR #22](https://github.com/b1indsight/agentflow/pull/22) 于 2026-09-08 00:02:45（Asia/Singapore；
UTC 2026-09-07 16:02:45）合并为 `5fd27f23584e`，M2 F1–F4 全部进入 main。
修复 head `0294c003128b` 的 [CI](https://github.com/b1indsight/agentflow/actions/runs/34140178991)
全部通过。审查修复后的独立审查无 critical，新发现以下 Medium，尚未修复。
第 6 节真实验收矩阵仍待执行；合并和自动化通过不表示 M2 已验收。

### 同秒重复 webhook 重放（Medium，未修复）

> 本节标题保留历史链接；以下记录 M2 合并时的缺陷，现由 [M4 F1](m4-feedback-replay.md) 修复。

来源：[PR #22 的修复后审查记录](https://github.com/b1indsight/agentflow/pull/22)。
相关路径为 `app/review_store.py` 的远端线程观察与 `_reconcile_edits()`。

1. GraphQL 已观察到评论从 A 编辑为 B，并将 B 保存为当前版本。
2. 同一条 A→B 的 webhook 迟到；其前序 A 与当前 B 不符，编辑进入持久队列。
3. 后续批次冻结 B，远端又在同一时间戳内将正文从 B 改回 A。
4. 观察写入 A 后，reconciliation 将已发生过的旧 A→B 再次重放，错误生成新版本 B。

影响：已过时的要求重新成为待处理反馈，可能阻塞 resolve/re-review，并进入下一轮 worker。
第 7 节的 377 项历史测试与 CI 不包含该交错顺序；新增修复与验证证据见 M4 F1 实施记录。

修复验收条件：识别版本历史中已表示的编辑转换，避免将迟到重复 webhook 作为新编辑；无法确定的
编辑保留诊断。新增“远端观察 → 迟到重复 webhook → 同秒正文回退”回归，确认最终正文保持 A，
不派生旧 B 的待处理版本，并保持真实新编辑和乱序编辑的处理能力。
