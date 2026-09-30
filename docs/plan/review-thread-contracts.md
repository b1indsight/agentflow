# Review 线程读取契约统一

**阶段：** 已实现并验证，待独立代码审查。
**PR：** [#54](https://github.com/b1indsight/agentflow/pull/54)。
**基线：** `main` / `717f97bb`。
**Bookmark：** `refactor/review-thread-contracts`。

## 1. 初始计划

### 动机、目标与范围

线程完整快照与持久分页都读取同一种 GitHub 评论，却分别维护 GraphQL 字段、身份检查、
评论转换和游标校验。前者仍读 `databaseId`，后者读 `fullDatabaseId`，已经发生漂移。
本次统一同职责的表示与解析，减少重复规则和隐式契约。范围为线程读取及其直接共享的
GraphQL connection 校验；不扩展到 webhook 生命周期或整个工作流状态机。

已有 M2 线程回复及 M4 线程对账计划记录原始功能；本计划记录独立的跨路径契约收敛。

### 概念审计与目标行为

| 概念 | 当前对象/入口 | 决策 |
| --- | --- | --- |
| GitHub 线程评论 | `ReviewComment`，两套 raw decoder | 保留一个领域对象、一个 decoder；评论及 reply-to 均读 `fullDatabaseId`，领域字段统一为整数 `github_id` / `reply_to_id` |
| 线程身份和内容 | `ReviewThread`，两套身份检查与构造 | 共享 repo/PR/thread 身份校验和线程页解析；完整性由遍历层决定 |
| 有界远端证据 | `ReviewThreads` | 继续作为唯一快照对象，保持持久化字段兼容 |
| 一次请求与续页 | `ReviewThreadPage` / `ThreadCursor` | 保留快照加 continuation 的明确接口；续页键和序列化格式保持兼容 |
| 完整遍历 | `ThreadReader.all/one` | 保留总请求/字节预算、跨页去重及游标循环检查 |
| 单页持久轮询 | `read_thread_page` | 保留每次最多一个请求；嵌套最后一页仍是片段，不能证明完整覆盖 |
| connection 的 pageInfo | threads、feedback、projects 各自检查 | 一个无业务策略的严格校验函数；调用者保留自身错误分类和遍历历史 |
| 反馈版本与 Judge 输入 | `RemoteFeedback`、`FeedbackUpdate`、`ReviewFeedback`、`ContextDiscussion` | 分别负责远端观察、授权/版本和裁剪后的上下文；不因字段相似而合并 |

同一 GitHub 评论经完整读取、单线程刷新和分页轮询，应得到相同的身份、正文、作者、
时间与回复关系。大整数 ID、删除的作者及机器人身份按同一规则解释。
分页失败、循环 cursor、重复或缺失评论不能产生可用于 resolve 的完整证据。

### 技术设计和模块归属

- `github_threads.py` 作为线程语义的所有者，集中共享 GraphQL 选择字段、原始评论解析、
  线程身份检查及线程页内容解析；`github_thread_pages.py` 复用这些逻辑，只拥有持久续页编排。
- 为已在至少三个模块重复的 `pageInfo` 校验提供小型 `github_pagination.py`，统一检查
  布尔完整性标记、非空 cursor 和不前进 cursor；不创建通用 paginator、策略类或模式开关。
  全遍历额外维护 seen 集合，确保检测多步循环；项目 adapter 保留其错误类型。
- `github_client.py` / `ports.py` 保留三种有实际调用者的操作：全 PR 快照、指定线程快照、
  单页读取。统一它们使用的解析契约，避免一个充满可选参数的万能入口。
- 沿用 `ReviewComment`、`ReviewThread`、`ReviewThreads`，只有出现真实复用需求才新增
  内部类型；不得再建一套字段相同的 DTO、兼容别名或旧 `databaseId` fallback。
- `github_models.py` 的字段及数据库中的 snapshot/continuation 结构保持可读；无需迁移。

### 备选方案与取舍

只替换 `databaseId` 能修补当前漂移，但保留两份负载较重的解析逻辑，无法防止再次漂移。
让完整读取循环调用单页接口能减少编排代码，却把每请求 100 个线程降为 1 个线程，
在同样 50 次预算下缩小可见证据，故保留不同的查询批量与预算所有者。
合并所有评论/反馈模型会混淆远端事实、授权、版本和裁剪上下文，不采用。

### 成本、风险与缓解

主要风险是重构时把部分线程标记完整、丢失重复检测，或改变已有 error/retry 分类。
用行为回归约束这些边界；保持序列化兼容，沿用已有领域对象。共享 helper 必须减少重复
规则；若仅搬移分支或需要多种模式，则退回直接编排。三个业务读取入口保留清晰职责。

### 实施顺序

1. 用同一代表性线程证明三条路径的大整数 ID、reply-to 和元数据一致，先运行并记录失败。
2. 统一字段、decoder 和身份校验，删除两处独立构造逻辑；复用现有模型。
3. 收敛 pageInfo 校验并保留各调用者的错误策略、预算和跨页历史。
4. 更新必要的测试 fixture/接口说明，执行回归、消融和代码审查，在同一个 PR 推送实现。

### 验证与消融

- 重点测试：64 位评论及 reply-to ID、独立内外 cursor、缺失/重复节点、错误 PR 身份、
  部分 GraphQL 响应、跨页循环、预算耗尽、消失的嵌套线程，以及末页片段不能自动 resolve。
- 保留完整读取的 101 个线程批量回归，验证请求数量不退化为逐线程遍历。
- 运行 thread/page、feedback/projects、reconcile、resolution/recovery 相关测试；
  最终运行 PostgreSQL pytest、Ruff、Mypy、`uv build --no-sources`。
- 消融在实现后、代码审查前进行：临时恢复旧 ID 字段应使身份回归失败；临时去掉片段
  完整性或 cursor 前进保护，应分别触发覆盖/分页回归。恢复实现后重跑受影响测试。
- 不为简单字段 helper 增加镜像测试；以 adapter 的代表性行为约束共享契约。

### 待决事项

没有需要额外产品决策的阻塞项。内部 helper 的精确签名由实现时的实际调用需求决定，
不新增未使用的扩展点。真实 GitHub smoke 仅在可用凭据与明确测试目标具备时另行执行，
自动化不依赖联网。

## 2. PR 变更时间线

### 2026-09-30：执行用户修复请求中的线程项

- **触发：** 用户在全仓审查后要求“修复这些问题2”。已询问尾部的 2 是仅指第 2 项还是
  全部问题；两种范围都包含本计划，先执行线程字段与解析契约修复。其余审查项不纳入本 PR。
- **实现：** `github_threads.py` 统一线程字段、单线程查询、身份检查和 `ReviewThread` decoder；
  三种读取均使用 `fullDatabaseId`，reply-to 同样转换为整数。缺失/重复评论在快照中保留
  incomplete，在持久分页中拒绝推进。跨页去重在完整读取中继续保留。
- **接口：** 沿用既有模型、三种读取入口和持久 cursor 格式；共享内部 GraphQL 参数使用
  `comments`，外层批量仍为 100 个线程，持久轮询仍为一个线程、一次请求。
- **收敛：** 新增一个小型 `github_pagination.next_cursor`，供 threads、feedback、projects
  共用；历史 cursor 集合、预算、错误分类仍由各自调用者负责，没有新增 paginator 或模式旗标。
- **测试先行：** 新增跨三种读取路径的大整数评论/reply-to 身份回归，旧实现先以
  `GitHub list_review_threads failed; kind=unexpected_response` 失败；实现后通过。
  原有 101 线程批量、预算、缺失节点及错误分类测试保持通过，追加混合整数/字符串重复 ID
  与缺失评论不能形成完整证据的两种情形。

### 2026-09-30：验证和隔离消融

- 修复前 adapter 基线：64 项通过；最终 adapter 回归：67 项通过。
- `FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run --no-sync pytest -q --tb=short`：
  **816 项通过**（58.85 秒），一项既有 Starlette 测试客户端弃用警告。
- 最后合并同一单线程 query 常量后，重跑全部 67 项相关 adapter 测试、Ruff、Mypy 与构建。
- `uv run --no-sync ruff check app tests`、`uv run --no-sync mypy app tests`、
  `uv build --no-sources`、`git diff --check` 均通过。
- 消融命令：`uv run --no-sync python .cache/2026-09-30/thread-contract-ablation.py`。
  实验在独立子进程中替换函数，未改写源码；脚本及输出为本地忽略的实验产物。

| 移除的机制 | 同条件回归 | 结果 |
| --- | --- | --- |
| decoder 改回 `databaseId` | 三种读取的大整数 ID/reply-to 一致性 | 1 failed，完整读取返回 unexpected_response |
| 去掉嵌套续页的片段标记 | 内外 cursor 连续读取 | 1 failed，最后一页错误地变为 complete |
| 去掉 cursor 不前进检查 | feedback 重复 cursor | 1 failed，未拒绝不前进的分页 |

保留上述机制；恢复条件下相关 67 项测试全部通过。未调用真实 GitHub。
