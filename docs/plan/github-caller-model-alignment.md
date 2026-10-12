# GitHub 调用层与资源模型对齐 — Feature record

**Stage:** Implementation — 审查上下文复用现有反馈模型
**Bookmark:** `refactor/github-caller-model-alignment`
**PR:** [#79](https://github.com/b1indsight/agentflow/pull/79)。

## 1. Initial plan

### 背景与目标

基线为已合并的 [#77](https://github.com/b1indsight/agentflow/pull/77) 和
[#78](https://github.com/b1indsight/agentflow/pull/78)。逐项检查 control、domain、
persistence、CLI 和测试调用后，认证和资源操作已采用新的不可变对象；剩余值得
处理的边界集中在审查上下文读取和 revision effect 的授权身份传递。

目标是让业务层直接使用 GitHub 包提供的事实和资源关系，减少 SDK 数据解读和
重复的身份拼装。保持冻结上下文、反馈版本、恢复和保守授权规则。

### 拟议行为

- 审查上下文的 conversation/review_body 使用 `fetch_comments` / `fetch_reviews`
  和 `Batch` 游标读取，保留每种来源最多 20 条的预算、超限诊断、批次时间截止和
  冻结反馈排除。review 使用实际更新时间，与现有反馈扫描的时间语义一致。
- inline 评论仍需要 path、original_commit_id、diff_hunk；保留其 REST 读取，
  在 GitHub 包内转成明确的不可变事实，业务层不再读取 SDK user/id 等字段。
- 文件读取也在 GitHub 包内转成明确事实，保持缺失 patch、重命名路径和截断行为。
- revision 写入前校验直接接收 `GitHubApp`，按第一条冻结 effect 的 installation/repo
  绑定目标资源，避免从调用者仓库回溯 App。继续读取最新 PR，保留 SHA、分支、
  仓库、open/merged 全部授权条件。

### 技术设计与范围

- `app/github/pull_requests.py` 及必要的内聚事实模块：文件和 inline 评论提供具体
  返回类型，按现有约定从 `app.github` 导出。conversation/review_body 复用原生
  cursor API；迁移所有调用者后移除已无调用的旧 discussion 多来源入口。
- `app/control/review_context.py`：直接消费统一反馈字段，显式续读，预算仍由上下文
  编排负责。发现超过预算的条目才标记截断；不为凑满预算无限读取。
- `app/control/thread_effects.py`、`app/control/effects.py`：调整 revision 校验的
  输入身份边界；其他读取根据是否已有绑定对象使用 `refetch()`，仅在确实简化且
  不改变冻结身份时修改。
- `tests/github.py` 和受影响测试：更新替身返回契约。

不新增通用 paginator、资源基类或授权策略框架，不修改数据库、消息协议和扫描
状态。重开、创建恢复、triage 的不同 PR 校验规则不强行合并。

### 方案比较与理由

只给旧 SDK 返回值补类型，能缩小 diff，却会继续保留 review 的 submitted_at
时间解释，以及 conversation/review_body 两套查询。优先复用新反馈查询，让
读取时间和身份语义统一。inline 和文件缺少对应的完整事实模型，增加两个小型
事实类型能移除业务层 SDK 依赖，无需扩展现有反馈类型承载不属于它的字段。

直接复用传入仓库做 revision 校验更短，但恢复时必须采用冻结 effect 的身份，
不能用当前 flow 的仓库替代。显式传 App 可以保留这条约束并减少导航链。

### 成本、风险与处理

GraphQL 反馈目前会忽略作者已删除的记录，而上下文旧读取保留 `[deleted]`。
实施前明确此边界，保持已有上下文证据，不直接丢弃记录；若需要调整反馈解码，
只修改作者事实的表达并验证反馈扫描行为仍一致。

review 的实际更新时间可能使较晚编辑的正文被批次截止排除，这是与冻结输入
一致的预期。验证分页边界、缺失时间、deleted author、pending review，以及
查询失败的重试边界，避免把不完整证据当作完整输入。

### 实施顺序

1. 完成文件/inline 事实类型和相关替身，检查全部旧读取入口的调用者。
2. 迁移上下文 conversation/review_body 到原生 cursor 查询，删除无调用入口。
3. 简化 revision 校验的身份传递，保留恢复语义。
4. 验证、审查最终 diff，在同一 bookmark 和 PR 交付。

### 验证与消融

- 运行 GitHub 查询、审查上下文、triage、反馈恢复、revision delivery/recovery、
  thread resolution 的相关测试；执行 `ruff check app tests` 和 `mypy app tests`。
- 添加最小回归场景：review 编辑晚于批次截止；预算跨页；deleted author；
  当前调用者身份与冻结 effect 身份不同的恢复校验。复用已有测试，不逐字段镜像。
- 本次消除旧入口和隐式身份导航，不添加策略机制；无需人为构造消融实验。
  以相同场景比较迁移前后内容、诊断和请求预算，并说明时间语义的预期变化。

### 未决问题与延后事项

无阻塞设计问题。deleted author 的具体字段契约和测试覆盖缺口在实施时解决，
必须满足前述保留证据要求。统一所有分页或交付校验策略不在本次范围。

## 2. PR change timeline

### 2026-10-12 — 二次检查：收紧读取策略并明确空作者契约

**状态：** 拟议修订，实施待确认。以下内容替代初始计划对应部分，其他范围保留。

**背景与证据：**

- [`review_context.py`](../../app/control/review_context.py) 在每种来源读到第 21 条时
  停止，并在过滤批次截止、冻结反馈前消耗预算。
  [`test_review_context.py`](../../tests/test_review_context.py) 已覆盖 sentinel、
  过滤后不补满预算、后续页错误和缺失时间。上下文只要有界前缀，不需要完整扫描。
- [`feedback.py`](../../app/github/feedback.py) 当前在解码时跳过 null author；
  [`review_context.py`](../../app/control/review_context.py) 的 REST 读取保留它们，
  显示 `[deleted]`。直接切换查询会改变证据条数和截断边界。
- [`repositories.py`](../../app/github/repositories.py) 的 `fetch_pull_request` 本来
  就是绑定 PR 后调用 `refetch`。单纯更换调用写法不减少业务分支或身份来源。
- Projects 已使用绑定的 `ProjectItem.set_status()`；持久化反序列化得到无认证的
  事实，再按执行身份绑定资源是必要边界，不把这些地方列为冗余。

**方案与拟议决策：**

1. conversation/review_body 各调用一次 `fetch_comments(limit=21)` /
   `fetch_reviews(limit=21)`，保留前 20 条；第 21 条或未结束的 cursor 表示截断。
   不继续扫描后续批量。代替初始计划中的显式游标续读，减少请求与状态。
   inline 保留现有 REST 预算。筛选后的条目不补满来源预算。
2. `RemoteFeedback.author` 明确为 `str | None`，解码保留空作者条目及其正文、
   身份和更新时间，空作者的 `is_bot=False`。上下文映射为 `[deleted]`；
   `FeedbackReconciler._discussion_updates` 显式跳过空作者，保留其原有扫描语义。
   不把占位用户名写入持久反馈，不为复用查询添加 mode 参数。
3. 上下文与反馈扫描分别保留自身过滤规则，不复用 `_discussion_updates`：
   上下文不会因为 pending、bot、`/flow` 或 approved 状态自动丢弃/清空证据；
   冻结反馈和截止时间继续由上下文判断。缺失时间不得伪造为当前时间。
4. 移除“其他读取改用 refetch”的开放范围。仅修改 revision_current 的 App 输入，
   保留第一条冻结 effect 的身份和所有现有写入授权条件。

**后果与验证：**

新增空作者契约涉及反馈模型、解码和扫描调用者，属于上下文迁移的必要配套。
补充验证：空作者计入 20 条原始预算但不进入持久反馈；恰好 20 条且已结束
不标记截断；21 条、未结束批量标记截断；过滤后不继续读；pending/bot 正文
不会因扫描策略被上下文删除。保持 GraphQL 错误向现有重试边界传播。

复查运行现有基线测试：
`uv run --no-sync pytest tests/test_review_context.py tests/test_github_feedback.py tests/test_boundaries.py -q --tb=short`，
31 passed。未修改产品或测试代码；此结果用于确认现有行为，不代表拟议迁移已验证。

### 2026-10-12 — 范围澄清：以新模型的调用适配为检查目标

**状态：** 用户澄清检查目标；此前查询方式优化和 revision_current 签名调整暂缓，
不作为已确认的实施范围。用户关注整个项目是否有遗漏适配新数据模型的地方。

**检查结果：**

- control 的线程覆盖、回复规划、待处理反馈检查和 persistence 的线程观察均使用
  `thread.comments.items`；未发现把新 `Batch` 当旧评论数组遍历的生产调用。
- 持久扫描器分别保留外层 cursor 和评论 cursor；评论后续片段维持 `complete=False`。
  不把到达末页等同于完整覆盖。批量返回值已按 `items` / `next_cursor` 消费。
- ReviewContext、EffectReceipt、ResolveEffect 已直接使用新的资源模型；恢复时
  保留未观察字段和已知 null 的区别，无客户端事实不会直接执行远端操作。
- Projects 同步已使用绑定后的 Project / ProjectItem；独立 live smoke 使用 gh CLI
  及其自己的 REST 响应模型，不属于 app.github 调用者。
- app、tests、scripts 未检出旧 GitHubClient、旧分页方法或已删除 GitHub 模块导入。

**候选与边界：** `review_context.py` 仍消费 list_files/list_discussions 返回的 SDK
对象，这是当前 GitHub 包显式保留的 API 契约。可以进一步统一成包内事实模型，
但不是此次新模型迁移的明确遗漏。是否整理该边界需要另行明确具体收益；本次
不据此强制改查询、时间语义或授权接口。架构 diagram.html 的旧 GitHubClient
端口说明是文档残留，不能用它推断生产调用仍未迁移。

**验证：** 运行
`uv run --no-sync pytest tests/test_resource_persistence.py tests/test_github_client.py tests/test_review_resolution.py tests/test_github_thread_pages.py -q --tb=short`，
62 passed。尚未运行本轮数据库集成测试，因此结论限于已检查代码和上述验证范围。

### 2026-10-12 — 实施：收回 SDK 解读，调用层消费明确事实

**范围与决策：** 以用户澄清后的模型适配为范围，落实上一条的审查上下文候选。
实施仅整理事实边界；初始计划和二次检查中的 GraphQL 查询迁移、空作者反馈契约
变更、revision_current 签名调整均继续暂缓。这避免把适配模型扩成读取和授权策略修改。

**已实现：**

- PullRequest 的两种异步读取返回不可变 `PullRequestFile` 和
  `PullRequestDiscussion`，从公开 `app.github` 入口导出；不增加泛型读取框架。
- GitHub 包负责 SDK 字段转换，统一 path/previous_path、github_id、author、body
  和可选 inline 字段。timestamp 明确表示来源提供的时间：评论编辑时间或 review
  提交时间，缺失保持 None。不能把 REST review 提交时间命名成已验证的编辑时间。
- review_context 直接消费这些事实，保留现有内容截断、来源预算、超额 sentinel、
  冻结反馈排除和批次截止规则。测试替身返回类型同步适配。
- 更新架构说明及 changelog。删除作者和空正文的归一化移到数据拥有者，不修改
  RemoteFeedback 的反馈扫描契约。

**验证策略与消融：** 新模型替代隐式 Any 返回和 SDK 对象访问，没有增加策略或
状态机制，无需另造消融。原有上下文测试直接验证迁移前后分页请求、截断、截止、
缺失时间和失败传播不变；补充三种来源的已删除作者、空正文和来源时间回归。
定向测试 69 passed。完整数据库测试命令：
`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run --no-sync pytest -q --tb=short`，
888 passed，1 条既有 Starlette/httpx 弃用警告。`uv run --no-sync ruff check app tests`、
`uv run --no-sync mypy app tests`（165 个源码文件）和 `uv build` 均通过。
未执行真实 GitHub App 在线调用；公开返回类型变更的所有项目调用者和测试替身已迁移。

### 2026-10-12 — 修正：移除新增平行模型，复用已有数据契约

**触发与决策：** 用户指出新增的 PullRequestFile / PullRequestDiscussion 超出了
“适配已有新模型”的原意，并要求修改。此条替代上一条的两个新类型与多来源 REST
读取方案；不再创建文件或讨论的平行事实模型。

**已实现：**

- 删除两个新增类型及公开导出。文件直接使用 SDK 已有 DiffEntry 类型。
- 普通评论与 review 正文直接复用 `fetch_comments` / `fetch_reviews` 返回的
  `Batch[RemoteFeedback]`，每类请求 21 条、保留前 20 条，不增加续读循环。
  截断根据超额 sentinel 或 next_cursor 判断，过滤后的条目不补满预算。
- RemoteFeedback.author 使用实际可空身份，并保留该条目的完整正文和时间。
  上下文把 None 显示为 `[deleted]`；持久反馈扫描继续显式跳过空作者。
  不将占位用户名当成已确认的 reviewer 身份。
- review 上下文采用已有模型的实际更新时间，晚于批次截止的编辑正文不会进入
  冻结上下文；取消旧 REST review 的提交时间解释。
- inline 的 path/diff_hunk 片段仍读取 SDK 已有 PullRequestReviewComment，移除
  多来源 `list_discussions(source)` 分支，留下具体的 `list_review_comments()`。
  ReviewThread / ReviewComment 的线程归属、完整性和回复/resolve 权限模型保持现状。
- 具体 SDK 文件类型暴露出可选 patch/previous_filename 的 Unset：control 明确
  映射为 None，避免把未返回字段当文本处理。新增真实 SDK DiffEntry 回归。
- 适配记录资源与调用测试；反馈扫描后的 triage 使用同一个已有 GitHub 替身，
  避免测试 fixture 对另一实例进行错误的状态读取。

**取舍：** 直接用 ReviewThread 替代 inline 片段，需要改变全局评论顺序、补充
diff_hunk 字段并增加嵌套遍历，超出当前适配需要。保留 SDK 的具体片段类型能
保留现有证据字段和读取预算；不把 snippet 伪装成完整线程证据。SDK 类型只在
TYPE_CHECKING 中导入，不改变 worker 离线导入边界。

**验证：** 定向反馈、SDK 与上下文测试通过；已补充实际更新时间截止、空作者
保留/扫描跳过、20/21 条截断、过滤后不续读、GraphQL 失败传播及 SDK Unset 回归。
完整测试曾暴露两处测试替身身份不一致，已修正并重新验证。
最终命令：`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run --no-sync pytest -q --tb=short`，
891 passed，1 条既有 Starlette/httpx 弃用警告。Ruff、Mypy（165 个源码文件）、
`uv build` 和 `git diff --check` 均通过。本次没有引入新状态或通用机制，不另造
消融实验；预算 sentinel 与现有截断/不补满测试直接验证保守读取规则。
未执行真实 GitHub App 在线调用。
