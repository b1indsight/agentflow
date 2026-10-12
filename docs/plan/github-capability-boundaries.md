# GitHub 不可变资源对象与目录重构 — Feature record

**Stage:** Implemented — 代码与自动化验证已完成，待用户评审。
**PR:** [#77](https://github.com/b1indsight/agentflow/pull/77)。
**Bookmark:** `refactor/github-capability-boundaries`
**基线:** `main` / `323e1ea9`（PR #76），2026-10-11。

**当前方案：** 资源对象持有身份、认证上下文和自己的操作，业务接口为
`await repo.fetch_pull_request(number) -> PullRequest`。资源实例及其事实不可变；
`fetch_*` 取得远端数据，`refetch()` 重新查询并返回新实例，状态更新成功后返回修改过的
副本。构造函数按已有属性本地绑定；数据导出使用标准序列化方法。下文列出全部实施要求。

## 当前完整方案

### 目标与范围

把 `app/github` 按实际 GitHub 资源组织。身份和认证在资源绑定时确定；调用方法时只传
本次操作所需的数据。查找和创建子资源放在所属对象上，针对已有资源的操作放在该资源上。
不预留 adapter、Protocol、抽象基类、跨领域模型、transport、Provider 或客户端工厂。
命名与模块位置表达资源归属，后续发生真实需求时再扩展。

基线中的 `GitHubApp` 混合认证、仓库、Issue、PR、线程、凭证和上下文读取；Projects 还调用
入口类的私有认证及执行方法。依据：[基线入口](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/client.py#L64)、
[基线 Projects](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/projects.py#L60)。
本次同时迁移 `app/github`、实际 control/CLI 调用者、相关类型与测试替身，并更新当前
架构说明和 changelog。返回类型按资源接口迁移；远端请求、错误、逐操作重试、
持久恢复和最终审核上下文保持等价，不为统一返回实例而增加查询。
数据库 schema、配置、消息协议、持久游标和 githubkit 版本保持现状。

### 资源关系与日常调用

```text
GitHubApp                         App 身份、原生 SDK 认证、私钥与 timeout
└── Installation                 安装 ID、App 引用、原生安装客户端
    ├── Repository               owner/name、安装上下文
    │   ├── Issue                issue number、仓库上下文
    │   └── PullRequest          PR number、仓库上下文
    │       └── ReviewThread     thread node ID、PR 上下文、已知根评论 ID
    └── Project                  ProjectConfig、project ID、安装上下文
        └── ProjectItem          item node ID、Project 上下文
```

```python
from app.github import GitHubApp, Issue, ProjectItem, ReviewThread

app = GitHubApp(settings)
installation = app.installation(flow.installation_id)
repo = installation.repository(flow.repo)
before = await repo.fetch_pull_request(number)  # 此次查询的不可变 PullRequest
after = await before.refetch()  # 再次查询，返回新实例；before 保持原值
facts = before.model_dump(mode="json", exclude_unset=True)  # 标准本地序列化
await after.request_reviewers(reviewers)

# 已有身份时直接绑定；构造和绑定均不发请求。
issue = Issue(repo, flow.issue_number)
await issue.comment(body)
thread = ReviewThread(after, thread_id, root_comment_id=root_comment_id)
await thread.reply(body)

# Project 属于安装下的组织目标，配置与仓库分别提供各自的身份。
project = installation.project(config)
await project.validate(repo)
item = ProjectItem(project, item_id)
updated = await item.set_status(status)  # 成功后返回副本，仅状态取得新的确认
# 确实需要重新观察其他字段时，调用方显式查询。
observed = await updated.refetch()  # ProjectItem | None；item、updated 均不变化
```

`GitHubApp` 的属性是 App ID、认证配置和 bot 身份。同一个 App 可以有多个安装，
`installation_id` 属于 `Installation`，不固定在全局 App 或仓库方法参数中。
`app.installation(id)`、`installation.repository(name)` 和 `installation.project(config)`
只绑定实际身份，不查询远端、不注册缓存、不提供可替换实现。Project 绑定检查配置中的
安装 ID 与所在 Installation 一致；真实安装、组织和目标权限仍在既有 `validate` 请求中核对。

从父资源取得资源或事实的读取使用 `fetch_*`，已有资源重新查询自身使用 `refetch()`。
扫描和分页使用 `find_*` / `list_*`，比较和校验使用 `compare_*` / `validate`，
这些异步方法会请求远端。名词绑定方法、构造函数以及标准序列化只进行本地操作。
写入方法继续使用
`comment`、`request`、`add`、`remove`、`set`、`reply`、`resolve` 等实际动作名称。
不以 `get_*` 同时表达资源绑定与远端读取。

`GitHubApp` 保留这个准确的身份名称，但原先所有仓库、Issue、PR、线程及 Projects
业务方法移走。不会保留一个转发全部业务方法的旧入口。每个资源绑定自己的父资源，
从父资源取得原生 SDK 和 bot 身份，不在执行时切换安装或仓库。

绑定身份沿用原操作的来源：新准备和普通对账使用当前 Flow 的持久身份；执行或恢复
已持久 effect 时，使用 effect/plan 保存的安装、仓库与资源 ID，包含 retired 恢复和
revision 核验。当前 Flow 仍参与原 fence 检查，不能以它的最新身份替换已记录的目标。
依据：[effect 执行与恢复](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/control/effects.py#L196)、
[revision 身份来源](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/control/thread_effects.py#L32)。

### 文件与类型归属

```text
app/github/
├── __init__.py          # 资源、查询数据、绑定配置、错误和重试计时的公开入口
├── _fields.py           # 包内 Pydantic 未观察字段声明
├── apps.py              # GitHubApp、Installation；实际认证与身份绑定
├── repositories.py      # Repository、RemoteRef、PushCapability；仓库操作与凭证
├── issues.py            # Issue；读取、共享评论分页/评论/标签、Issue node ID
├── pull_requests.py     # PullRequest、PullRequestMerge；PR 操作与合并事件
├── review_threads.py    # ReviewComment、ReviewThread；评论数据与线程操作
├── projects.py          # Project/Item、ProjectConfig/Target；绑定、校验与条目操作
├── errors.py            # GitHubError/Kind、retry_delay；请求执行与错误转换
├── feedback.py          # RemoteFeedback、FeedbackPage、FeedbackSource 查询结果类型
├── threads.py           # 现有 ThreadReader、线程解码与完整性预算
├── thread_pages.py      # 现有 ThreadCursor 与单页查询常量
└── pagination.py        # 现有 pageInfo 校验
```

`client.py` 在迁移完成后删除。资源类直接实现其操作，可以调用包内真实复用的解码、
评论/标签、请求执行和分页函数；不再提供同一操作的公开模块函数与对象方法两套入口。
`ThreadReader` 的遍历预算和 `ThreadCursor` 的续页状态仍有实际用途，继续保留。
评论分页与评论、标签操作一样，以同一具体方法直接绑定 Issue/PR；review 反馈和线程单页
查询由 PR 方法直接请求及解析。查询结果模块不再承接转发式资源操作或来源分派参数。
父资源创建子资源时直接使用模块导入；构造器的反向身份类型校验保留必要的循环导入断点，
SDK 仍按实际认证或请求需要加载。

包外统一从 `app.github` 导入资源与查询结果：`GitHubApp`、`Installation`、`Repository`、
`Issue`、`PullRequest`、`ReviewComment`、`ReviewThread`、`ReviewThreads`、`ReviewThreadPage`、`Project`、
`ProjectItem`、`ProjectItemsPage`、`RemoteRef`、`PushCapability`、`PullRequestMerge`、
`RemoteFeedback`、`FeedbackPage` 和 `FeedbackSource`；绑定与校验数据为 `ProjectConfig`、`ProjectTarget`，
操作错误为 `GitHubError`、`GitHubErrorKind`，退避计算使用 `retry_delay`。
内部实现按具体模块引用，不通过公开入口互相取类型；字段声明、解码、扫描器、游标与请求执行函数不导出。
入口直接重导出现有类型。App 与客户端构造时才导入 SDK，单纯导入公开类型和标准 JSON 恢复不初始化认证。

具体资源使用现有 Pydantic 模型方式表达不可变事实与标准序列化。父资源、SDK 和
认证引用属于私有运行时属性，不混入公开事实字段；不建立统一资源基类或序列化框架。
标准模型解析不加载 SDK 或 App 认证；请求执行和 SDK 异常类型在实际操作内加载。
worker 可以通过合同使用同一个资源模型，继续隔离控制编排、数据库和 GitHub SDK。

`Issue` 和 `PullRequest` 分别绑定自己的身份，两者均提供普通评论及标签操作；共用的
GitHub Issue API 实现使用少量包内函数复用。不建立抽象 Issue 基类或泛型资源框架。
当前只需要读取评论、review 和 ref 的事实，以及返回写入回执，因此不新增 Comment、
Review、Ref、Branch、Label 等资源类或预留方法。

### 完整操作归属

下表覆盖现有 GitHub/Projects 接口；括号中的值是实际操作数据，不再重复传认证或父身份。

| 对象 | 方法 | 当前操作与返回含义 |
| --- | --- | --- |
| `GitHubApp` | `installation(id)` | 绑定实际安装 |
| `Installation` | `repository(full_name)`、`project(config)` | 绑定仓库或配置目标 |
| `Repository` | `refetch() -> Repository` | 原 `get_repository`；返回承载此次仓库事实的新实例 |
| `Repository` | `fetch_issue(number)`、`fetch_pull_request(number)` | 远端读取并返回 `Issue` / `PullRequest` 资源 |
| `Repository` | `fetch_ref(branch, retry_transient=False)`、`compare_commits(base, head)` | 返回 `RemoteRef`、原有提交关系布尔结果 |
| `Repository` | `find_pull_request_by_head(branch) -> PullRequest \| None`、`open_pull_request(branch, base, title, body) -> PullRequest` | 使用真实查找/创建响应构造资源；只承载响应已取得的事实，不补 GET；回执直接保存同一资源的已观察字段 |
| `Repository` | `create_push_capability()` | 由父 Installation 和 App 签发当前仓库凭证 |
| `Issue` | `refetch() -> Issue`、`fetch_content_id() -> str` | 返回此次 Issue 事实的新实例；读取经过身份核对的 Issue node ID，原 `projects.issue_id` |
| `Issue` / `PullRequest` | `comment(body)`、`find_comment(marker, …)` | 原普通评论写入与 marker 恢复查询；回执仍为数据库 ID |
| `Issue` / `PullRequest` | `fetch_labels()`、`add_label(label)`、`remove_label(label)` | 原标签读取和增删；标签不扩入现有 Issue/PR 状态事实，保留既有集合结果及写入返回含义 |
| `Issue` / `PullRequest` | `list_comments_page(cursor)` | 完整正文的持久反馈页；资源决定 issue/pr 评论来源 |
| `PullRequest` | `refetch() -> PullRequest` | 原 `get_pull_request`；返回承载此次完整 PR 状态的新实例 |
| `PullRequest` | `fetch_reviewers(since)`、`request_reviewers(reviewers)` | 原 reviewer 查询与请求；不为返回 PR 副本扩充 reviewer 状态字段 |
| `PullRequest` | `list_reviews_page(cursor)` | review 的完整反馈页，保留真实编辑版本 |
| `PullRequest` | `list_files()` | 按项异步遍历已解析文件；资源内部管理分页，调用者按审核配额停止 |
| `PullRequest` | `list_discussions(source)` | 按项异步遍历指定 conversation/review/inline 来源；资源内部管理分页 |
| `PullRequest` | `list_review_thread_page(cursor)`、`list_review_threads()` | 原持久线程片段页和有预算的完整遍历证据 |
| `ReviewThread` | `refetch() -> ReviewThread`、`find_reply(marker)` | 读取器直接生成最终资源，保留已知根评论身份并核对所有评论页，统一转换远端身份错误；无资源导出后重建；新实例承载事实、完整性与 bot marker 查找 |
| `ReviewThread` | `fetch_resolution()`、`reply(body) -> int`、`resolve() -> ReviewThread` | 原 resolution 查询；REST 根评论回复仍返回 ID；resolve 成功后返回仅 resolved 更新的副本 |
| `Project` | `validate(repo)`、`list_items_page(cursor)` | 原目标验证及含完整性标记的条目页 |
| `Project` | `fetch_item(item_id)`、`add_issue(content_id)` | 读取返回 `ProjectItem \| None`，添加返回带已有回执的 `ProjectItem` |
| `ProjectItem` | `refetch() -> ProjectItem \| None`、`set_status(status) -> ProjectItem` | 查询返回此次事实的新实例或原有缺失结果；设置成功后返回仅 option_id 更新的副本 |
| 已有事实的资源 | `model_dump(...)`、`model_dump_json(...)` | 标准本地序列化，按已知字段及现有持久化契约导出数据，排除私有运行时引用 |

这些方法只迁移现有功能。`fetch_content_id` 的请求和校验移到实际 Issue，仍沿用原
`projects` 错误标签；`Project.add_issue(content_id)` 使用已有持久 content ID，
不为了接受 Issue 对象而再读一次 node ID。Project 状态字段和五个 option ID 由 Project
持有，`ProjectItem.set_status` 从父对象取得映射。

线程绑定保留 GraphQL thread ID 与 REST root comment 数据库 ID 的区别。
`reply(body)` 使用对象上已知的 `root_comment_id`，缺失时明确报本地前置条件错误，
不隐式发起读取。指定线程重新查询继续验证 repo/PR 归属；返回的 `ReviewThread`
实例直接承载评论、resolved、complete、limitations、fetched_at 和缺失结果；
`ReviewThreads` 只表示扫描结果，其中每个条目都是同一个 `ReviewThread` 模型。
不能缩为一个忽略完整性与存在性的线程值。缺失证据不等于线程当前存在。

### 不可变实例、查询与状态更新

资源实例保存已知身份与事实。`repo.fetch_pull_request(number)` 执行原 PR 请求，
用真实返回字段构造新的 `PullRequest`；`fetch_issue` 和 `Project.fetch_item` 同理。
已有资源的 `refetch()` 每次执行对应的远端读取并返回新实例，实例身份仍来自原绑定。
不替换原实例的数据，不维护资源上的“最近读取”状态，不提供平行的读取/刷新入口。
两个并发读取各自返回结果，由调用者持有；后返回的请求不会覆盖另一个实例。

身份、目标配置及状态映射固定在绑定时；外部修改配置不能改变已绑定资源的目标。
公开事实及嵌套集合均不可变；构造时处理外部可变输入，避免保留可被修改的事实引用。
仅配置 `frozen=True` 或浅复制可变 list/dict 不能满足这个要求。资源之间可以共享
不可变事实和原有父资源/SDK 引用，不深复制整条认证与客户端对象链。

改变已承载状态的更新方法返回同类型新实例。可以先复制旧实例并修改目标字段，
远端确认成功后再将副本作为成功结果返回；旧实例保持原值。`ProjectItem.set_status`
核对 mutation 条目 ID 后更新副本的 `option_id`，`ReviewThread.resolve` 成功后更新
副本中的 resolved 事实。其他已知字段沿用旧实例，未知字段保持未取得。
这些副本表示成功写入后已知的状态；其他字段并未被本次操作重新查询。
原 fetched_at、complete、limitations 和旧评论仍属于原读取，不改成新的全量确认。

复制使用标准模型复制方式，并显式核验写入确认的字段；不能依赖绕过字段校验的
`model_copy(update=...)` 自动执行校验。只复制/冻结需要处理的公开事实，不使用
`model_copy(deep=True)` 深复制私有父资源、SDK、锁或认证上下文。

不为返回新实例额外增加 GET。调用者确需重新观察其他字段时显式 `refetch()`；
原有 resolve 后核验、Project 添加后读取及其他恢复确认继续保留。
标签和 reviewer 操作没有对应的现有 Issue/PR 状态字段，保留实际集合结果和既有写入
返回含义，不为返回自己扩充状态字段。评论及回复创建子资源，当前仍返回真实评论 ID，
不新增 Comment 类型、不把新评论猜测性补入旧线程证据。

失败或写入结果未知时抛出原错误，不返回假定成功的副本；旧实例仍保持原值。
读取失败不能用旧事实代替此次结果，也不能解释为不存在。成功读取 ProjectItem
为缺失时返回 `None`，旧实例仍是历史数据，不清空或改写。Issue/PR 原有 404 仍抛错；
线程缺失仍通过原有完整证据表达，返回承载该证据的 `ReviewThread` 实例。
如果原有业务流程已写入成功、后续确认读取失败，保留“写入已发生、确认待完成”，
不能用后一次读取的 definitely_unsent 覆盖此前写入事实或重做写入。

资源不做 TTL 缓存，已有事实或写后副本不能替代原有授权、fence 和恢复确认。
必要的远端核验仍由 control 在既有流程中显式调用。

### 属性构造与标准序列化

构造函数只进行本地绑定。恢复可以直接使用持久记录的属性构造同一种资源：

```python
installation = Installation(app=app, installation_id=effect.installation_id)
repo = Repository(installation=installation, full_name=effect.repo)
pr = PullRequest(repository=repo, number=effect.number)
comment_id = await pr.find_comment(marker)
```

Issue、ReviewThread、ProjectItem 同样使用已有 number、thread_id、root_comment_id、
item_id 等实际属性。已有事实通过具体资源的实际字段传入，不另外包装可变 `data`
容器、引用对象或加载协议；身份与传入事实须一致。只绑定身份时未取得的事实保持
缺席，读取这些事实报本地错误，不能填入默认 open、False、空正文或空评论。
不为构造或执行只需要身份的操作先请求远端；需要完整事实时显式 `refetch()`。

取消单独的快照导出方法，使用标准 `model_dump(...)` / `model_dump_json(...)`。
父资源、App、安装客户端、认证信息和私钥是私有运行时引用，不参与事实序列化。
已有事实中的 repo 字符串、number、node ID 等继续按原数据契约保存；不能把父资源
对象图、新安装字段或运行时元信息加入冻结 JSON。标准序列化不请求 GitHub，导出
结果不与资源共享可变事实引用。

```python
pr = await repo.fetch_pull_request(number)
known = pr.model_dump(mode="json", exclude_unset=True)
known_json = pr.model_dump_json(exclude_unset=True)
```

未知字段使用 Pydantic 原生字段排除规则，标准序列化自动省略未观察字段；
明确观察到的 `None` 保留，不能用 `exclude_none=True` 将它与未查询混淆。
最低 Pydantic 版本为 2.12，锁定版本保持不变。完整审核入口检查同一个模型中
所需事实是否已观察；部分资源不能直接作为完整审核证据。构造、导出和已捕获事实
均不替代原有远端身份及当前性核验。

查找/创建 PR 返回真实响应生成的 `PullRequest`。head 查找只承载实际查找字段；
创建响应可使用确实返回并通过校验的字段，不补 GET 或给缺少的 rich 状态填默认值。
持久 effect 回执直接保存 `PullRequest` 的已观察事实。短响应和完整响应使用同一个
模型；已有短 JSON 缺少的字段仍为未知。需要完整状态的调用者使用
`fetch_pull_request` / `refetch()`。
Project 添加使用已有 mutation 回执构造 `ProjectItem`，不为返回资源补读状态。
未选择状态字段时，`option_id` 尚未观察；模型默认 `None` 不能证明远端未设置状态。
后续原有完整字段读取确认缺失时，才将 `option_id=None` 作为已知事实。

每种资源只有一个模型，定义在 `app/github`，直接包含不可变事实与资源操作。
删除 `IssueSnapshot`、`RepositorySnapshot`、`PullRequestSnapshot`、`PullRequestStatus`、
`ReviewThreadSnapshot` 和 `ProjectItemSnapshot`，不留兼容别名。查询、列表、上下文、
回执及持久化全部使用同一个资源类型，不经过第二套事实模型。

标准 `model_validate` / `model_validate_json` 可以恢复只有公开事实的资源实例。
父资源是可缺省的私有运行时引用；未绑定时调用需要父资源的操作明确失败，
执行恢复按已持久的安装、仓库及对象身份构造带父绑定的同一模型，不补 GET。
旧 JSON 不含的新观察字段保持未知，私有认证对象不进入持久化。

`ReviewContext`、`EffectReceipt`、配置和分页/扫描结果保留各自的业务含义。
`ReviewThreads` / `ReviewThreadPage` 放在 `github/threads.py`，扫描元素为 `ReviewThread`；
`ProjectItemsPage` 放在 `github/projects.py`，列表元素为 `ProjectItem`。
线程、评论和项目条目集合使用 tuple，JSON 继续为数组。
反馈、合并事件、错误与凭证没有平行的资源类型，保留实际用途。
它们与评论、Project 绑定配置一起按 GitHub 概念归入对应模块，删除旧 `domain/github_models.py`
和 `domain/github_retry.py`，不保留兼容重导出或共用事实基类。`NodeID` 是 Projects 内部校验类型；
`ProjectStatus` 表达 Flow 的五个业务目标状态，继续保留在 `domain/project_models.py`。
旧 `GitHubAdapterError` 改名为 `GitHubError`，保留错误事实和脱敏行为。

冻结上下文使用两次独立查询实例：

```python
before = await repo.fetch_pull_request(pr_number)
# 按既有顺序读 Issue、文件和三类讨论，逐页处理。
after = await before.refetch()
# 比较 before / after 的 head、base、state、merged；before 始终不变化。
# 同一个 PullRequest 直接进入 ReviewContext；完整证据入口检查已观察字段。
```

依据：[当前 PR 事实](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/domain/github_models.py#L106)、
[Project 回执与读取](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/projects.py#L108)、
[resolve 后确认](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/control/thread_effects.py#L85)。

### 认证、重试与错误

App 启动验证正数 App ID、可读且非空的私钥，并只读取一次私钥；保留有限 timeout。
App/Installation 使用原生 githubkit `AppAuthStrategy` 和安装策略。Installation 绑定
原生安装客户端；推送凭证和 Projects 的安装验证需要 App 认证，具体方法从父 App
取得该认证，不能直接改用仓库的 installation-auth 客户端。
绑定不提前签 JWT 或获取 token；认证失败仍发生在原请求的错误转换范围内，保持
目标请求确实未发送与此前写入结果未知的区别。

每次操作按基线选择 SDK 重试，不修改其他资源正在使用的 SDK 认证或配置。
通过原生 `with_auth()` 复制客户端及其配置，保留认证、timeout、测试 transport 和已有重试策略；
写入只关闭副本的 retry，后续读取和其他资源仍使用原有策略。

| SDK 重试选择 | 必须保持的操作 |
| --- | --- |
| 启用既有 SDK 重试 | App 推送凭证签发；head 查找 PR；普通评论 marker 扫描；标签读取；reviewer 查询；指定线程读取及回复 marker 扫描；resolution 查询；添加标签前的 label 定义读取 |
| 显式参数决定 | `fetch_ref` 默认禁用，实际执行工具使用 `retry_transient=True` |
| 禁用 SDK 重试 | 仓库/Issue/PR 状态读取；反馈、文件、讨论和持久线程页；完整线程遍历；commit compare；PR 创建；评论、标签写入、reviewer 请求、线程回复及 resolve；全部 Projects GraphQL 和 App repo-installation 验证 |

删除标签的 404 分支仍以启用既有重试的 `fetch_labels` 确认目标可读和标签确实缺失；
不能无条件吞掉仓库不可见的 404。依据：[基线逐操作选择](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/client.py#L369)、
[指定线程与写入](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/client.py#L549)。

`errors.py` 使用同步 `translate_errors(endpoint)` 上下文管理器，资源方法在 `with` 块内直接
等待 SDK 请求并解析响应，无需为错误转换构造回调或 lambda；各块保留原请求和解析的错误范围，
本地身份与参数检查仍直接报错，任务取消原样传播。已有 resolve 失败用例同时覆盖未知结果和取消，原实例保持不变；
已有 timeout 用例同时检查 App/Installation 的认证、transport 和读写重试配置隔离。
REST/GraphQL 分类、旧 endpoint 标签、
status、Retry-After、request ID、definitely_unsent、request_rejected、
脱敏与写入结果未知的区别不变。control 继续在原事务、ledger 和恢复边界决定重试，
资源方法不自动重放未知结果的写入。单仓库推送凭证仍限制到当前 repository 与
`contents:write`，核对返回仓库、权限及到期时间，token 仍用 `SecretStr`。

### 持久反馈、线程与 Projects 行为

- `FeedbackReconciler` 保留原持久 source 字符串；按 source 选择 Issue/PR 的评论、
  review 或线程页。评论和 review 正文完整保留，64 位数据库 ID、bot 身份和真实
  updatedAt 不变；上下文用 REST review 的 submitted_at 语义不混入持久反馈版本。
- 普通评论及线程回复的 marker 查询保留配置 bot_login、Bot 事实与逐行完全匹配，
  未完成原本要求的扫描时不能报告 marker 缺席。
- `ThreadCursor` 的 `after/thread_id/comments/following` JSON 和一次最多一个请求保持。
  嵌套评论最后一页也继续标记为片段；已移除线程仅接受当前明确 NOT_FOUND 的恢复路径。
  全遍历仍保留 50 次请求、2 MB 解析内容预算、去重、游标循环、身份和 complete 判定。
  control 根据原有授权及完整性规则选择回复或 resolve。
- Project 验证真实安装、Organization、仓库 owner、project/field ID、closed、
  viewerCanUpdate 和五个选项。条目分页中的不可见 content、archived、缺失及权限错误
  保留原含义；只有完整扫描可证明未添加。add 与 set_status 分开记录回执；未知写入
  先查询恢复，不重复添加，不把已知条目缺失解释为允许新建。
  条目字段读取仍最多遍历 10 页，并校验 project/content/item 身份与 mutation 回执。
  添加回执中未读取的状态字段保持未知；原有添加后 `fetch_item` 取得真实状态后才
  决定是否 `set_status`。设置成功返回新实例，业务不能将其他沿用字段当作再次读取。

依据：[反馈读取](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/feedback.py#L20)、
[持久线程页](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/thread_pages.py#L27)、
[Project 恢复](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/control/projects.py#L67)。

### 审核上下文加工

`PullRequest.list_files()` 与 `list_discussions(source)` 返回按项异步迭代器，
资源内部管理页码、20/10 页长、请求与 SDK 响应解析；每页的错误转换块在产出条目前结束。
control 通过 `async for` 消费条目，仅负责审核数量配额、哨兵判定、映射、裁剪、过滤和冻结，
不再管理页码、页长或短页终止。条目保持已解析 SDK 数据，不新增事实模型或 DTO。
删除只服务旧聚合入口且已无调用者的 `FilePage` / `DiscussionPage`。
不新增 raw page DTO、通用 paginator 或 HTTP 响应字节拒绝机制。

以下是等价迁移的验收条件，不能仅以新接口能调用为验收：

1. 保持 PR 前读、Issue、文件、三类讨论、PR 后读的请求顺序。文件每页 20，最多
   40 条；conversation/review/inline 每类每页 10，最多 20 条；均保留第三页哨兵。
2. 每页立即处理，不累计巨大 SDK 条目。先按原来源配额取条目，再按 cutoff 和
   frozen IDs 过滤；被过滤的条目占用原配额，不补取。哨兵只证明截断，不加入内容。
3. 保留原 limitations 文本、顺序和重复项，包括缺失 patch 的两处诊断、
   `file:{path}:patch:truncated`、来源的 `body:truncated` / `diff_hunk:truncated`
   与后续 control 裁剪标记。被过滤评论的诊断不得进入最终上下文。
4. 保持每来源 4,000 字符、最终 96 KB UTF-8 上限、裁剪顺序与 fallback；相同输入
   输出相同 JSON、limitations 和 digest。当前预算在 SDK 解析后应用，不改为限制
   网络响应字节或提前拒绝合法回复。
5. 后续页和哨兵页失败仍抛出原错误事实和 endpoint，不冻结为部分成功。
   第一次 PR 实例始终保留原值，前后 head/base/state/merged 比对使用独立查询结果。

依据：[当前分页/来源预算](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/github/client.py#L158)、
[当前过滤与冻结](https://github.com/b1indsight/agentflow/blob/323e1ea97825a8abfb2451dbe82681f2a3f1d5dd/app/control/review_context.py#L18)。

### 调用者迁移

| 调用者 | 重构后的依赖和使用 |
| --- | --- |
| `ControlRuntime` | 构造一次真实 `GitHubApp`；多 Flow 组件按其原操作的持久身份来源绑定资源 |
| `JobCoordinator` | 每次处理 Flow 绑定 `Repository`，使用 Issue/PR/Thread 的实际方法；只查询线程时按持久编号直接构造 PR，再调用线程查询，无额外 PR GET；事务和授权不移到资源 |
| `ResultApplication` / `ResultEffects` | 多 Flow 入口持有真实 App；按 effect/plan 的安装、仓库与 ID 绑定执行或恢复资源，保留 retired 恢复和原 Flow fence |
| revision/lifecycle/thread 辅助函数 | 接收按原身份来源绑定的具体仓库或资源；revision 核验使用 plan 记录的身份，保留 fence/ledger/marker 恢复 |
| PR `Reconciler` | 每个待查 Flow 绑定仓库并调用 `fetch_pull_request`，直接使用此次不可变事实做判断 |
| `FeedbackReconciler` | 按持久 source 绑定 Issue/PR 并读取页，原 source/cursor 继续存储 |
| `build_review_context` | 接收具体仓库，按原顺序查询并逐页加工；直接接收 Issue/PR 实例，分别保存 PR 前读与 `refetch` 后读实例，检查所需事实后直接序列化同一资源模型 |
| `FlowToolService` | 按已授权 Flow 绑定仓库；`repo.fetch_ref(branch, retry_transient=True)` |
| `ProjectSync` | 接收真实 App 与配置，按目标配置的安装绑定 Project/Repository/Issue/Item；添加后显式取得条目事实，设置成功取得新实例；删除 `Callable[[ProjectConfig], ProjectsClient]` |
| CLI `project check` | 相同绑定与 `project.validate(repo)`；配置禁用时仍不创建 App/认证 |

删除 `domain/ports.py` 的 `GitHubClient` 和 `ProjectsClient`，清理对应引用及未使用导入，
不新增替代 Protocol 或保留兼容入口。其他真实业务协议维持各自职责。control 使用
同一个 GitHub 资源模型和必要的已解析 SDK 数据；domain 容器、持久化和
worker 合同可以引用同一个资源模型，但不创建 SDK 或认证。测试优先在原生 SDK 请求边界模拟真实响应，不建立生产用接口来容纳替身。

### 实施顺序与验证

1. 列出所有旧操作的请求顺序、认证、retry、endpoint、返回/错误与持久恢复基线，
   用既有测试确定行为覆盖；补充现有测试未覆盖的关键边界。
2. 迁移真实 App/Installation 认证和错误函数，实现具体不可变 Repository/Issue/PR；
   属性构造与标准序列化无网络，`fetch_*` / `refetch` 显式请求且返回新实例；事实的
   缺席/已知 None、私有运行时绑定以及现有持久化字段分别明确。
3. 迁移 Thread、Project/Item 方法和事实同名类型，保持持久分页及恢复；更新全部
   control/CLI 装配与调用，落实成功复制更新、失败不返回副本、旧实例不变；去掉
   GitHub 协议、业务工厂和旧 `client.py`。
4. 将上下文加工完整收归 control，完成逐页有界处理与冻结等价对比；更新架构、
   changelog 和 feature record。本 bookmark/PR 继续承载实施。

| 行为 | 主要既有验证 |
| --- | --- |
| 认证、跨安装身份、timeout、推送凭证、错误与逐操作重试 | `test_github_client.py`、`test_flow_tool_ref_retry.py`、`test_archive_status.py` |
| 线程证据、分页、回复/resolve 和恢复 | `test_review_threads.py`、`test_github_thread_pages.py`、`test_reconcile_threads.py`、`test_thread_recovery.py` |
| 完整反馈及持久续扫/重放 | `test_github_feedback.py`、`test_reconcile_feedback.py`、`test_feedback_replay.py` |
| Project 权限、不可见内容、回执与未知写入恢复 | `test_github_projects.py`、`test_project_sync.py` |
| 冻结 JSON/digest、来源配额、过滤、哨兵及 UTF-8 预算 | `test_review_context.py` |
| 边界、delivery 和副作用恢复 | `test_boundaries.py`、`test_delivery_receipts.py`、`test_revision_delivery.py`、`test_reconcile.py` |

新增验证只覆盖真实风险：不同安装不会串用身份、同一资源混合 retry 不修改共享配置、
已有 ID 的属性构造及写入/恢复不新增 GET、嵌套事实不会被外部输入或导出数据修改、
并发 `refetch` 与成功复制更新不修改旧实例、失败不返回假定成功状态；未知字段和
已知 None 正确导出，标准序列化不泄露运行时引用且保持原持久字段；来源配额/过滤/哨兵组合的
旧新 JSON/digest 对比、后续页失败及 UTF-8 边界。复用已有覆盖，不为改名、目录、
简单绑定构造或只复刻实现的断言增加测试。

实施完成后运行 `uv run ruff check app tests`、`uv run mypy app tests`、具备项目
PostgreSQL 环境的全量 `uv run pytest` 和 `uv build --no-sources`。实际执行结果如下；本次未进行真实 GitHub App 联网读写验收。

### 当前实施与验证结果

资源模块、control/CLI 装配及消费者使用同一个资源模型；旧 client、GitHub/Projects
Protocol、资源 Snapshot/Status 模型和上下文聚合页模型删除。线程与项目页内的元素
和单项查询是同一不可变资源类型，JSON 集合仍为数组。
评论、反馈页、ref、推送凭证、合并事件、Project 绑定配置及目标信息归属 GitHub 模块；
错误和重试计时使用同一公开入口。删除旧 domain GitHub 模块和共用事实基类，业务 Project 状态仍留 domain。
PR 创建与复用回执直接保存已观察事实，冻结上下文检查所需事实后直接序列化；
认证、私有父绑定和运行时对象不进入持久数据。控制测试显式启用资源方法替身，
共享录制 App 仅保存测试状态与调用身份；替身直接使用真实资源方法的签名。
原生 SDK transport 测试继续覆盖真实重试、认证与错误转换；同一用例混合使用时，
明确调用保存的原生方法。

未知字段声明共用一个纯 Pydantic 字段助手，访问保护直接依据本模型字段，避免维护第二份字段名单。
PR 完整读取和部分响应共用 `PullRequest._from_response()`，直接构造同一资源，
分别保留完整字段要求与已观察字段判断；不向其他模块暴露事实字典助手。
文件和讨论分页归 `PullRequest` 的异步迭代方法；`build_review_context()` 直接消费，
仅保留审核上下文加工，删除独立读取函数和旧单页接口。提前停止只访问配额与哨兵所需页面；
SDK 字段在生成器内解析，产出期间不持有错误转换上下文，消费者错误不转为远端错误。
评论反馈页直接绑定共享资源方法；PR 的 review 反馈和线程单页方法直接请求与解析，
删除 `read_feedback_page`、`read_thread_page` 及仅用于转发的来源参数。
6 处子资源纯模型导入收归模块顶部，保留反向身份校验和 SDK 的必要延迟导入。
上述改动只收拢已有实现并删除转发层，不增加状态或遍历机制，复用原生分页和离线恢复测试。
副作用执行复用同一绑定资源；测试替身删除全局自动安装、方法注册表、签名转译、
App 上的重复操作接口与原生 fallback 分派，保留完整目标身份录制。
测试合并重复恢复路径与分页夹具，
保留未知事实、写入响应、不可变性、身份绑定和恢复的行为覆盖。
线程单项读取直接返回最终资源，根评论身份在所有评论页收集后校验；远端冲突通过统一错误边界返回。
control 直接使用该实例进行 resolve 判断和确认，集合包装仅供观察持久化，不再包装后重新查找。
Issue 读取使用非空契约，测试只为 PR 查找保留空结果，创建必须返回资源；测试命名使用实际查询与资源含义。
这些清理复用既有校验、完整性和恢复机制，没有引入新的状态机制，无需新增机制消融。
合并或删除 9 个重复用例；恢复用例数量不变。包外类型导入统一使用公开入口，
现有新进程验证同时覆盖 worker 启动与公开入口的 JSON 恢复。
字段助手仅复用原有 Pydantic 配置；7 个模型的 JSON Schema 与精简前逐字相同，无新增恢复机制。
类型归属整理前后对照 16 个模型，JSON Schema 全部相同；既有独立进程用例同时恢复评论、ref 和 Project 配置，
确认公开入口不加载 SDK、认证、control 或 persistence。此整理只移动定义与导入并删除旧基类，没有增加运行时机制。

验证使用本地 PostgreSQL，运行命令为
`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres .cache/2026-10-11/github-resource-venv/bin/python -m pytest -q --tb=short`。
最终全量结果为 **887 passed**；独立环境的 `ruff check app tests`、
`mypy app tests`（166 个文件）及 `uv build --no-sources` 均通过。
独立环境直接安装本工作区，保证 Judge 的隔离子进程也加载此次实现。
27 处未改变调用形态的 SDK 错误范围和 endpoint 与回调包装的 AST 对照一致，异常处理分支未变；
直接绑定评论页和移入 PR 的 review、线程单页查询由既有原生分页用例验证错误与游标契约。
文件与讨论的两处查询改为资源内部逐页迭代，其请求顺序、错误和冻结结果由下述等价对照验证。
对照仅归一化 PR 私有工厂替代原事实解码和构造的表达式。复现：
`python3 .cache/2026-10-11/check-github-error-scopes.py`。
最终改动文件检查不包含行尾空白或缺少末尾换行。
原有 Starlette 测试客户端弃用提示不影响测试结果。

按项消费与先收集全部条目再裁剪使用同样的远端输入对照：前三页各 20 个文件，第四页权限失败。
审核保留 40 条并读取哨兵时只请求第 1–3 页；先收集全部结果的版本请求到第 4 页并失败。
因此保留配额驱动的异步迭代，分页由资源负责、停止位置由审核业务决定；不新增通用分页器或配额参数。
复现：`.cache/2026-10-11/github-resource-venv/bin/python .cache/2026-10-11/github-pagination-ablation.py`；
结果保存在 `.cache/2026-10-11/github-pagination-ablation.json`。

仓库重查核对响应与绑定的身份，允许同一名称的大小写差异；线程构造在已经观察到根评论
时核对显式根 ID；单项读取在最终观察上执行同一校验，包括根评论出现在后续页的情形。
身份构造不补 GET，也不派生未经观察的默认值。

冻结上下文以同一 SDK 输入运行基线与现实现：来源配额及长文本、UTF-8 总预算、
过滤后不补充配额、部分页、空哨兵、超长诊断回退、查询期间 head 改变，共 7 场景的 JSON
逐字节、digest 和请求顺序相同；文件与讨论在第 2 / 3 页失败的 4 场景也保留相同错误事实
及请求顺序，没有返回不完整的成功上下文。复现命令：
`uv run --no-sync python .cache/2026-10-11/github-review-compat/compare_context.py`，
当前工作区的输出为同目录 `comparison.json`。

消融在独立进程使用相同 4 个契约测试，每次只移除一项机制，真实写入和回执校验保持：
当前实现 4 通过；移除原生未知字段序列化排除的变体为 2 通过 / 2 失败；
取消未知字段访问检查、改为写后原地更新或共享可变 options 的三个变体均为 3 通过 / 1 失败，分别暴露未知值无法标准导出、
未读取字段可访问、旧状态被覆盖、外部修改改变实际写入目标。
因此保留原生字段排除、字段访问检查、复制更新和 options 复制冻结。复现命令：
`.cache/2026-10-11/github-resource-venv/bin/python .cache/2026-10-11/github_resource_ablation.py`；
当前工作区汇总为 `.cache/2026-10-11/github-resource-ablation-results.json`。
临时对照脚本和原始输出仅保存在当前工作区缓存。

### 取舍与后续范围

真实 App/Installation 与资源层级让业务方法直接表达当前对象，认证实参在绑定后收起。
代价是 control 明确依赖具体 GitHub 实现；实例表示查询或成功写入后已知的事实，
写后副本中的其他字段保留原观察，需要重新确认时显式查询。资源的公开事实可以离线恢复，操作所需的父绑定在执行时提供。
同一个模型服务查询、持久恢复、消息和审核；只为这些实际用途保存数据，不预备其他供应商或多个领域的共享模型。

`fetch_*` 获取资源或事实，`refetch()` 重新查询并返回新实例；更新成功后返回修改过的
副本。标准序列化导出既有数据，不再增加专有快照接口。同一种资源可由上游读取生成，
也可由已有属性构造；二者使用相同的实际操作与授权规则。只有身份或短回执时不补造
完整事实，持久化继续使用当前字段契约，不为统一返回形式增加网络读取。方法和目录
使用当前资源名称即可，将来实际需要 Comment 等资源时再引入。
