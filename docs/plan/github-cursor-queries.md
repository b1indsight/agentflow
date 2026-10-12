# GitHub 独立游标查询与扫描职责 — Feature record

**Stage:** Implemented — 实现与本地验证完成，待评审。
**PR:** [#78](https://github.com/b1indsight/agentflow/pull/78)。
**Bookmark:** `refactor/github-cursor-queries`
**基线:** `main` / `eca1f5ee`（PR #77）。

## 1. 初始计划

### 动机与目标

当前 `PullRequest.list_review_thread_page(cursor)` 将 thread 分页和某个 thread 的
comment 分页编排进一个查询。返回 `ReviewThreadPage` 可能只有同一 thread 的评论片段，
cursor 是包含外层位置、thread ID、评论位置和外层后续位置的 JSON。
讨论评论使用 REST 页码，review 正文使用 GraphQL 游标。

把查询单位明确为 thread、comment、review；查询对象只读取对应列表的一批数据。
反馈扫描器负责持久进度、分步调度和完整性判断。沿用 #77 的不可变资源对象，
不新增客户端抽象或通用 paginator。不考虑旧页码和旧混合 cursor 的兼容。

### 范围与验收标准

- 反馈讨论评论、review 正文、review thread 和 thread 评论读取统一使用 GraphQL。
- 每种批量查询接收 `cursor` 和 `limit`，分别映射到 `after` 和 `first`。
  `limit=1` 与其他批量使用相同接口；数量参数不命名为表示跳过数量的 `offset`。
- 返回不可变容器，包含当前批次的对象和 `next_cursor`；不携带扫描器的嵌套进度。
- thread 列表的返回对象不隐式承诺评论已完整加载。评论查询具有独立容器和游标。
- 保留完整快照/单 thread 刷新、请求和字节预算、错误分类、机器人过滤、settle、
  版本观察边界和 resolve 完整性要求；扫描片段不能成为自动 resolve 的完整证据。
- 不把所有 GitHub REST 操作改写为 GraphQL；写入、权限、文件等接口不属本次范围。

### 目标行为与接口

沿用资源归属，拟定以下查询接口；在实现时核对已有名称避免同名异义：

```python
await pr.list_thread_batch(cursor=None, limit=10)       # ThreadBatch
await thread.list_comment_batch(cursor=None, limit=20) # CommentBatch
await issue.list_comment_batch(cursor=None, limit=50)  # FeedbackBatch
await pr.list_comment_batch(cursor=None, limit=50)     # FeedbackBatch
await pr.list_review_batch(cursor=None, limit=50)      # FeedbackBatch
```

`ThreadBatch.threads` 是绑定所属 PR 的 thread 资源及本次查询的元数据，
`CommentBatch.comments` 是 `ReviewComment`，`FeedbackBatch.items` 是 `RemoteFeedback`。
三个容器各自提供 `next_cursor: str | None`，没有通用容器继承体系。
thread 列表不查询评论，其资源上的评论事实保持未观察状态，不能被误认作空列表。
查询指定 thread 的评论由该资源的 node ID 定位，并校验返回身份与所属 PR。
GraphQL 的游标对查询者是不透明字符串；任何查询均不解析扫描状态 JSON。
限制 `1 <= limit <= 100`，以 `hasNextPage/endCursor` 判定续读，不依赖本页数量。

例如 A 有 45 条评论，扫描器先查询一个 thread 得到 A；随后三次查询 A 的评论，
分别观察 20、20、5 条，最后推进外层 thread 游标。查询方法只看到各自的原生游标。
完整读取可以一次取 100 个 thread，再分别遍历其评论；不降为逐 thread 查询列表。

### 技术设计与模块

- `app/github/pull_requests.py`：提供 thread/review 批量查询，删除混合分页入口。
- `app/github/review_threads.py`：提供所属 thread 的评论批量查询；保留绑定与不可变事实。
- `app/github/issues.py`：讨论评论反馈读取改为 GraphQL，PR 讨论评论共享所属语义。
- `app/github/threads.py`、`feedback.py`：明确批量容器及共享解码，完整读取编排复用
  批量接口。拆分选择字段，避免 thread 列表隐式携带 comment 分页。
- `app/github/pagination.py`：复用原生 connection 校验，保留部分响应、游标不前进检查。
- `app/control/reconcile_feedback.py`：拥有类型化扫描状态及其序列化。持久扫描默认
  thread limit=1、comment limit=20；完整遍历的 thread limit=100，反馈 limit=50。
  每次调度最多一次远端请求，解析和推进仅在扫描层完成。
- `app/persistence/reconcile_store.py`：继续原子保存观察结果和扫描状态，现有 TEXT 列
  可承载扫描器 JSON，无需为查询接口增加数据库字段。
- 相应调用者、测试替身、导出和当前架构说明同步迁移；已完成的历史计划保持原文。

扫描器状态区分读取 thread 列表与读取当前 thread 评论，包含所属位置和续读所需事实。
读取评论时保存已知 thread 身份及元数据，保证重启无需重查上一批 thread。
请求失败不推进状态；当前 thread 消失时记录不完整证据并继续外层扫描。
完整读取的预算耗尽、缺失节点、重复、游标循环仍产生不完整证据。
旧状态不增加兼容解析或静默回退；需要运行现存开发数据库时，显式重置反馈扫描状态，
保留反馈版本历史，具体操作在实现中写入文档。

### 备选与取舍

保留混合 cursor 改名为 Batch 能减少修改，但查询仍承载扫描策略。
查询自动补齐所有评论可提供完整 thread，却使一次查询的请求数量不可预测，
无法直接维持单次持久调度的请求上限。
选择独立查询，由完整读取器和持久扫描器分别编排，两者共享读取和解码规则。
不采用泛型分页框架，实际只需要各自领域容器和原生游标校验。

### 成本与风险

独立读取 thread 元数据再读取评论，会比原先首个请求同时取得评论多一次请求。
这是查询边界明确带来的已知成本；完整读取仍采用批量 thread 查询，避免退化为
每个 thread 都发一次列表请求。保证扫描状态可重启以及片段不会被误标完整，
比维持当前单请求返回形状更重要。GraphQL 节点删除、作者删除、部分响应和
大整数 ID 必须继续严格处理；未观察评论与实际零评论必须可区分。

### 实施顺序

1. 引入具体批量容器，拆分 GraphQL 查询，统一评论/review 游标读取与边界验证。
2. 将完整 thread 读取和刷新接到独立查询，保留预算、身份和覆盖检查。
3. 将嵌套续读编排移到持久扫描器，同事务更新观察结果与扫描状态。
4. 删除旧 Page/混合查询入口、迁移调用者与测试；更新架构和开发状态重置说明。

### 验证与消融

- 查询：limit=1 与批量、独立游标、评论超过一批、终页、作者删除、大整数 ID、
  缺失节点、部分响应、游标不前进和无效 limit。
- 扫描：A 有 45 条评论后继续 B；评论第二批失败和重启；事务回滚；当前 thread
  消失；同秒 webhook 竞争；扫描片段与完整快照的 resolve 边界。
- 全遍历：101 个 thread 保持批量查询，独立评论分页不影响总请求/字节预算。
- 运行关联 pytest，最终实际 PostgreSQL 下全量 pytest、`uv run ruff check app tests`、
  `uv run mypy app tests`、`uv build --no-sources`。
- 实现后、自动代码审查前比较相同 fixture 下的分步扫描和完整读取的输出与请求量；
  尝试移除多余容器/包装，保留有实际调用者且能表达不同完整性要求的结构。
  记录新增元数据请求成本；不以把评论循环重新藏进查询来降低请求数。

### 未决问题与延后事项

批准前没有额外阻塞问题。具体方法命名、模块拆分与本地扫描状态重置命令在实现时确定，
遵守资源归属及独立游标约定。外部配置批量大小、通用列表框架、旧状态兼容和其他
REST 操作迁移均延后。

## 2. PR change timeline

### [001] — 2026-10-12 — 查询命名与嵌套评论结果修订

- **背景与要求：** 用户指出 `list_*_batch` 方法名不合适，并询问为何 thread
  查询不直接返回带已反序列化评论的 thread。初始计划将独立分页错误地扩大为
  拆开首批数据的获取，导致没有必要的额外请求。
- **备选与取舍：** 仅取 thread 元数据再查询评论需要额外请求；thread 查询内
  自动读完所有评论会使请求数不受单次查询限制。采用 GitHub 原生嵌套 connection，
  首批评论与 thread 同请求取得，后续评论仍独立查询。
- **方案 / 状态：** 本项为待批准的修订，取代初始计划中“thread 列表不查询评论”、
  相应额外元数据请求成本和 `list_*_batch` 命名。实现尚未开始。
- **方法命名：** `pr.list_threads(cursor, limit)`、`thread.list_comments(cursor, limit)`、
  `issue.list_comments(cursor, limit)`、`pr.list_comments(cursor, limit)`、
  `pr.list_reviews(cursor, limit)`。分页批量是参数和返回值契约，不放进方法名。
  当前全量 `list_review_threads()` 及单 thread `refetch()` 仍保留其明确的完整读取语义，
  不与批量接口混用。默认参数与 keyword-only 形式在实现中统一。
- **返回结果：** 外层 `ThreadBatch` 包含 `threads` 和 thread 的 `next_cursor`；每个
  thread 的已观察评论采用 `CommentBatch(comments, next_cursor)` 容器，包含本次
  响应已反序列化的 `ReviewComment` 和该 thread 自己的评论续读位置。
  `thread.list_comments()` 返回相同评论容器。对应使用 `.comments.comments` 的地方
  在实现时迁移并检查可读性，不并存两份评论列表或另外建立游标映射表。
  Issue/PR 讨论评论、review 正文仍使用包含 items/next_cursor 的反馈批量容器。
- **请求语义：** `list_threads(limit=N)` 同一次 GraphQL 请求选取最多 N 个 thread，
  每个附带首批最多 20 条评论及独立 pageInfo；outer limit 只控制 thread 数量。
  20 是查询层已知固定首批上限，不增设尚无实际调用者需求的额外参数。
  评论少于或等于该批容量且没有下一页时，正常校验通过后可标记完整；有下一页时
  明确为部分评论。续读最后一批本身也不能被误认为覆盖整个 thread。
- **扫描责任：** 扫描器观察首批评论后，只对有后续评论的 thread 保存独立续读状态。
  如 A 有 45 条评论，首个 thread 请求带回 20 条，随后两次评论请求返回 20 和 5 条，
  再推进外层位置。查询接口不解析扫描 JSON，扫描器仍每次调度最多一个请求。
  完整遍历从首批评论接着读，不重新获取首批；保留批量 thread 查询及覆盖预算。
- **成本与迁移：** 取消初始计划中的额外元数据请求。实际请求量随各 thread 的
  评论续页数增长；响应体大小及共享预算仍有上限。更改评论容器形状时同步迁移
  快照导出、持久证据读取、调用者、测试替身和 resolve 覆盖校验，不增加旧形状兼容。
- **验证：** 增加首批评论无需独立请求、多个 thread 各自评论游标互不混用、
  不重复读取首批评论的行为检查；其余初始计划的恢复和完整性验证继续适用。
  尚未运行实现测试。本次只追加计划修订，等待用户批准。

### [002] — 2026-10-12 — 远端查询命名与共享批量容器

- **背景与要求：** 用户确认 [001] 的 thread 首批评论嵌套返回方案；要求方法名
  表明存在远端延迟和代价，并询问是否可以将 Batch 抽象为一个容器。
- **方法命名 / 拟议决策：** 批量远端读取使用 `fetch_threads(cursor, limit)`、
  `fetch_comments(cursor, limit)`、`fetch_reviews(cursor, limit)`，分别归属于 PR、
  thread、Issue/PR 和 PR；本地已取得数据使用属性读取。延续 #77 中 `fetch_*`
  读取远端事实的表达，本次批量接口不沿用 [001] 的 `list_*` 命名。
  完整扫描入口继续与一次批量查询区分；不因这项要求扩大到其他资源的方法更名。
- **共享容器 / 拟议决策：** 使用一个不可变 `Batch[T]`，仅包含
  `items: tuple[T, ...]` 与 `next_cursor: str | None`。thread 查询返回
  `Batch[ReviewThread]`，每个 thread 的 comments 为 `Batch[ReviewComment]`；
  独立评论查询返回相同评论类型，讨论评论/review 正文返回 `Batch[RemoteFeedback]`。
  替代初始计划和 [001] 的三种专用批量容器，不保留兼容别名。
- **抽象边界：** Batch 只表示一次查询所得数据和该列表的原生后续游标；容器访问
  不请求网络，不提供自动翻页、客户端引用、扫描策略或完整读取循环。
  `next_cursor=None` 只表示从本次位置已到列表末尾，不能证明包含整个 thread 的
  评论；完整证据仍由遍历者基于起点、续读历史、预算和异常作判断。
  本次是相同数据契约在三类实际结果上的复用，无需通用 paginator 或基类层级。
- **结果示例：** `batch.items[0].comments.items` 是该 thread 已反序列化的评论；
  `batch.next_cursor` 续读 thread，`batch.items[0].comments.next_cursor` 续读该 thread
  的评论。读一个 thread 仍使用 `fetch_threads(limit=1)`。
- **状态与后续：** [001] 的同请求返回首批评论方向已获用户确认；上述命名和共享
  容器为待评审修订。只更新计划，完整实现计划尚待批准。

### [003] — 2026-10-12 — 独立查询、泛型容器和持久扫描实现

- **实际变更：** 按 [001]、[002] 实现 `fetch_threads`、`fetch_comments`、
  `fetch_reviews`，参数为 keyword-only 的 `cursor`、`limit`；limit 范围 1–100。
  `Batch[T]` 收归 `github.pagination`，只持有不可变 items 和 next_cursor。
  thread 查询同请求返回首批最多 20 条评论，独立评论查询可指定 1–100 条。
  Issue/PR 讨论评论及 review 正文都使用 GraphQL；删除 FeedbackPage、ReviewThreadPage、
  原混合查询和 github.thread_pages 模块，不保留旧类型或游标兼容。
- **职责与实现取舍：** `ThreadScanState` 位于 control.reconcile_feedback，保存下一批
  thread 的原生游标和未读完 thread 的元数据、评论游标；已观察评论正文不重复保存。
  查询层看不到扫描 JSON。扫描继续每轮一个请求，结果和进度同事务提交。
  完整读取器保留独立预算编排，直接复用相同查询与 decoder，而不循环调用持久扫描器。
  严格批量查询拒绝缺失/重复评论，完整读取器可保留有效片段但标记不完整；共享 decoder
  返回同一评论容器和完整性事实，两种实际使用者各自决定失败策略。
- **调用者迁移：** thread 评论事实、冻结上下文、观察入库、回复恢复和 resolve 覆盖
  检查均使用嵌套 Batch；测试替身同步迁移。架构、changelog 和开发数据库说明已更新。
  新接口不自动完成多批查询；独立评论结果的末尾不证明整个 thread 的覆盖。
- **验证：** 新 API 测试先因缺少 Batch 导出失败；实现后接口、分页和完整覆盖测试通过。
  全量 PostgreSQL 测试 `892 passed / 0 skipped`（保留既有 Starlette 弃用 warning）；
  Ruff、Mypy、`uv build --no-sources` 均通过。真实扫描 fixture 的 45 条评论通过
  首批 20、续批 20、末批 5，再继续下个 thread，重启后请求游标为
  `None / comments-20 / comments-40 / threads-next`，未重复首批。
- **消融证据：** 相同 101 个 thread、各一条评论的模拟 GraphQL fixture，正常批量读取
  2 次请求得到 101 个完整 thread；仅把外层查询改为 first:1，50 次请求预算下只读取
  50 个 thread，并产生 threads:budget-exhausted。保留批量外层读取。
  独立 pytest 进程临时将 ThreadScanState.continuation 改为只保存外层 cursor，
  原恢复测试从通过变为失败：未完成 thread 状态丢失，不能恢复评论读取。
  保留扫描器的嵌套状态，未引入查询层混合 cursor。比较脚本位于本地
  `.cache/2026-10-12/cursor-ablation.py`，不修改生产源码。
- **局限：** 验证覆盖真实 SDK MockTransport、资源解码和实际 PostgreSQL，
  未进行真实 GitHub App 在线调用。未重置已有开发数据库；旧评论数组和旧扫描状态
  需要按 `docs/operations/github-cursor-development.md` 使用新的开发数据库。

### [004] — 2026-10-12 — 删除重复测试并合并恢复场景

- **要求与实际变更：** 用户要求先删除多余测试和步骤，再合并适合的用例。
  删除 test_github_batches.py，其中多 thread 的独立评论游标和单次请求断言并入
  原 thread 分页用例；重复的评论续读用例与简单 limit 条件枚举删除。
  SDK 节点删除用例只保留 NOT_FOUND 分类，后续 thread 查询由扫描器用例覆盖。
- **恢复场景合并：** 原模拟重启用例与真实资源查询的扫描用例合成一个，涵盖
  第二批请求失败、断点不推进、重启后重试同一评论游标、末批保持片段标记，
  最后继续下个 thread。将 45 条评论缩到最小跨批场景的 21 条，减少一次续读及
  多余循环；请求游标为 None / comments-20 / comments-20 / threads-next。
- **边界与验证：** 未修改生产实现或请求参数校验。相关 51 项测试、Ruff 和 Mypy
  通过；实际 PostgreSQL 全量回归 `885 passed / 0 skipped`，保留既有 Starlette warning。
  测试文件净减少 81 行，总计删除 7 个执行用例，保留独立游标、完整性、
  删除节点、事务回滚和新 webhook 竞争的行为覆盖。没有新增机制，不另做消融实验。
