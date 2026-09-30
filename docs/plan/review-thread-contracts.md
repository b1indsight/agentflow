# 仓库概念与接口统一（起始于 Review 线程读取）

**阶段：** 当前版本专用契约已实现并验证，等待本轮独立审查。
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

### 2026-09-30：独立代码审查完成

- 仓库 review gate 审查实现提交 `b4a80fa33c9a`，返回空 findings，审查通过。
- 实现已推送至同一 bookmark / PR #54；本次追加仅更新文档状态。

### 2026-09-30：用户批准修复全部审查问题

**要求与批准：** 用户明确要求“修复刚刚找到的所有问题”。本次覆盖全仓审查的 8 组问题及
4 项低成本清理，沿用同一文档、bookmark 和 PR；第 2 项线程解析已完成。此要求取代前一条
“其余审查项不纳入本 PR”的范围约束；PR 暂时恢复 draft，完成后统一审查。

**已批准方向与实施顺序：**

1. 新 review 标签 effect 改用增量标签操作，保留旧账本读取/回放兼容；先证明已有用户标签
   不被覆盖，并沿用回复、re-review、归档的 fence 与恢复测试。
2. 远端 PR 事实统一读取入口与 decoder；上下文仅持有其文本裁剪投影，保持旧冻结上下文
   字段和 digest 不变。保留已合并 PR 的严格事实校验。
3. 明确交付准备/已交付回执的类型，串联 control、持久工具账本、协议和 worker；沿用 v9
   已有 JSON 字段及合法回执形状，避免将持久化 digest 或重放契约变更伪装成内部重构。
4. journal、worker 身份、tool caller、stop store 成为实际运行所需的依赖，移除生产不使用
   的无依赖模式；测试注入明确替身或临时 journal，保留客户端/执行器组装的职责边界。
5. 收紧 API Context/Gateway 类型；删掉闲置 webhook handler 生命周期，runtime 直接拥有
   settle 调度；更新仍描述非持久 webhook 交接的当前架构文档。
6. 移除旧 ReviewItem CRUD 运行时 API，保留旧表及单次迁移；迁移回归直接写旧 schema fixture。
   移除无生产调用的 fail_assignment、Sandbox/JobDispatch 扩展桩、重复 VCS backend 字段、
   broker 转包层，以及未使用的 delivery helper 参数。

**风险与验证：** 首先为行为问题添加失败回归；改动现有测试以覆盖真实必需契约，避免
测试替身维持不再支持的执行模式。持久回执往返 JSON/digest、旧迁移、标签重放与停止/重连
为重点回归；完成后跑 PostgreSQL 全量、Ruff、Mypy 和 build。消融临时恢复标签全量覆盖，
验证真实回归失败；类型收紧与死代码删除不新增运行时机制，无需为删除本身另造消融。
新的实现提交需经过独立代码审查后推送，未经用户请求不合并 PR。


### 2026-09-30：全部审查项实现与验证

- 标签：新计划仅写 `LabelEffect`，旧 `LabelsEffect` 保留为持久兼容格式；执行与退休观察路径
  共用标签增量语义。新交付及旧计划重放均保留人工标签，沿用原有 dispatch/revision fences。
- PR：删除两个用途各自维护的 REST 方法，统一为 `get_pull_request` / `PullRequestStatus`；
  `PullRequestDetails` 是远端模型的基类及冻结上下文投影，字段顺序/JSON 不变。显式投影避免
  内存子类与数据库恢复对象不一致；保留 PR 身份快照用于创建结果，不合并不同责任的持久状态。
- 回执：`PreparedDelivery | OpenedDelivery` 贯穿 tool ledger、control、协议、worker 和 MCP；
  仅在边界序列化。保留 v9 `pull_request` wire 字段以及两种已有 JSON 形状；新增摘要往返回归。
  `FlowToolCall.error` 保留旧记录读取，删除无调用的写错误参数。
- worker：journal/worker_id/stops 必需；`execute(..., tool_caller=...)` 从当前 assignment 传入，
  删除运行时回填的 client/executor 循环引用以及可跳过日志/停止检查的分支。
  Gateway 返回真实 `WorkerRecord`，API 明确 metrics/host/port/token 依赖；测试提供实际 journal
  或明确内存替身。无生产用途的 webhook handlers、router 转发与 broker 转包层已删除。
- 旧接口：删除 ReviewItem 运行时对象及 CRUD、fail_assignment、SandboxPolicy/Sandbox/
  WorkspaceWriteSandbox/JobDispatch；保留旧表迁移，迁移测试直接写旧表。VCS 验证结果只保留
  一个 backend，删除未使用的 coordinator 参数。runtime 直接拥有 settle；当前架构文档已同步。
- 验证：真实 PostgreSQL 全仓 **822 passed**；最后移除转发接口后，受影响路径再次
  **120 passed**。Ruff、Mypy（137 个源文件）、`uv build` 和 diff 空白检查均通过。
  未调用真实 GitHub/模型，未修改数据库 schema 或协议版本。

### 2026-09-30：标签行为消融

复现命令：`FLOWTOOL_TEST_DATABASE_URL=<test-db> uv run --no-sync python
.cache/2026-09-30/label-contract-ablation.py`。脚本仅在独立 Python 进程中将增量调用替换为
旧式全量 set_labels，源码不变；对新/旧两种 effect 使用同一条标签保留与重放测试。

- 实现前：两例失败，人工 `bug` / `user-owned` 标签丢失。
- 当前实现：两例通过（包含在 822 / 120 项回归中）。
- 消融恢复覆盖语义：两例重新失败，实际标签仅剩 `flow:in-review`；因此保留增量操作。
- 其余改动移除分支、重复规则或加强边界类型；未新增调度/重试机制，不为删除另造消融。

本轮独立审查完成后记录结果并更新同一 PR；不自动合并。


### 2026-09-30：全仓契约收敛独立审查完成

- review gate 审查实现提交 `765026a3b89e`，返回空 findings，审查通过。
- 全部已批准问题完成，沿用 bookmark / PR #54；本次提交仅记录结果并更新阶段。
- 本地验证与消融见上文；推送后由 CI 再次验证。不自动合并。


### 2026-09-30：用户批准精简重复测试

用户要求“删除简化重复的测试”，批准此前列出的清理范围：删除内存 retry 回滚测试（保留
真实 PostgreSQL 同故障回归）、纯默认值/枚举重复测试；参数化两个 retry 正常场景；executor
替身直接使用执行参数 tool_caller。回执兼容测试使用独立固定 v9 数据与摘要，避免当前模型
同时生成预期和实际结果。生产代码不变；保留恢复竞争、分页完整性、停止和旧记录重放测试。
验证覆盖被修改的测试及真实事务/Judge 替代覆盖，运行 Ruff/Mypy。仅删除重复机制，不新增
运行时复杂度，无需另造消融；对固定回执基准用临时序列化字段漂移验证其能捕获兼容性变化。


### 2026-09-30：测试精简完成与验证

- 删除 3 个重复/默认值测试，两个 retry 正常路径合为参数化用例并保留两个场景；删除
  RetryStore 的人工回滚实现及两个 executor 替身的 client 回填，生产代码不变。
- 回执测试保留原来的两种形状，以固定 v9 字段与 SHA-256 基准替代当前模型生成的预期。
- 真实 PostgreSQL 相关回归 **104 passed**，涵盖修改项、Judge/Store 替代覆盖、事务回滚及
  archive 对共享替身的调用。全仓 Ruff、Mypy（137 源文件）和 diff 检查通过。
- 隔离实验 `.cache/2026-09-30/test-cleanup-ablation.py` 临时让 InitialResult 序列化漏掉 pushed：
  原有两例错误通过，固定基准两例均正确失败。实验仅修改子进程对象，无源码变动。

### 2026-09-30：测试精简独立审查完成

实现提交 `f9ddc38d7032` 的独立审查返回空 findings，通过；同一 PR #54 更新测试精简与验证记录。


### 2026-09-30：用户明确取消历史兼容要求

用户说明“当前仓库没有部署”，要求“直接去掉兼容之前版本的代码，全部固定到最新版本”。
此要求取代此前保留旧表迁移、旧 effect、旧回执字段/摘要及冻结上下文投影的决定，批准直接清理：
完整当前 schema 替换增量迁移/回填；删除旧任务枚举、迁移启动检查和旧结果版本分支；仅保留
当前单标签 effect。回执字段命名统一为 delivery_receipt、状态显式区分 prepared/opened，上下文
直接使用统一 PR 模型；更新 wire 为 v10、journal 为 v3，拒绝其他版本，不提供转换。
删除旧版本专用测试，保留当前版本重放、重启、摘要完整性与并发保护。新增干净 schema 的
初始化/重复初始化回归，执行真实 PostgreSQL 全仓、Ruff/Mypy/build；同步当前运维文档。
不清空本地已有数据库；旧开发数据库应单独重建，历史计划保留为历史记录。


### 2026-09-30：当前版本专用实现完成

- schema：将增量 ADD COLUMN 合并到各表完整定义，删除状态/交付回填、旧 review_threads /
  review_migrations、旧 attempts branch/PR/worker 字段、jobs pid/recovery version 和工具 error。
  webhook 顺序使用当前 identity 列，保留事务内分配顺序。循环外键的幂等建立属于当前表关系，
  并非旧版迁移，继续保留。
- 运行时：删除旧 ANSWER、command signal、LabelsEffect、result/resolution version、pre-inbox
  以及 check_upgrade/migrate_legacy 分支。ResolveEffect 的 PR 编号和快照为必填；当前 batch
  缺失、摘要不符、凭据/绑定错误、未知副作用等运行期保护不变。
- 契约：仅 wire v10 / journal v3；INITIAL 使用 delivery_receipt，prepared/opened 为显式判别字段。
  删除仅为冻结旧字段保留的 PullRequestDetails；远端与上下文共用 PullRequestStatus。
- 测试：删除历史升级/迁移专用场景，将 fixture 里的旧 worker 版本替换为 PROTOCOL_VERSION。
  新增当前 schema 重复初始化、现有 Flow 保留、无旧表字段回归；改动前因 review_threads 存在
  失败，当前实现通过。修正生命周期测试遗漏的旧 router.handle 调用为 dispatch。
- 验证：真实 PostgreSQL 全仓 **803 passed**；Ruff、Mypy（138 源文件）、uv build 与 diff 检查通过。
  删除的兼容路径不引入额外机制；无需额外消融。保留现有当前版本恢复、标签和线程回归。
- README、架构及运维文档以当前版本为准；历史计划和 changelog 保留原事实，由本条明确取代
  兼容决定。没有删除或迁移本地现有数据库，没有调用真实 GitHub/模型。
