# Issue 可选的任务输入 — Feature record

**Stage:** 本地实现与验证完成；未提交、未推送、未部署。
**PR:** 未创建；尚未获得推送和创建 PR 的授权。
**拟用 Bookmark:** `feat/flow-task-input`，尚未创建，不改动当前其他 feature 的 bookmark。
**规划基线:** 2026-11-12 已 fetch 的 `origin/main`，包含 PR #142；实现时须同时考虑共享工作区已有的 workspace 并发等改动，不替换或夹带这些改动。

Issue 已成为可选输入来源，可直接注入任务内容创建 Flow。看板顶栏“新建 Flow”选择 Project（代码仓库）、输入内容后启动，无需填写 Issue 或 installation ID。保留 GitHub 仓库、PR、审核及归档机制，并提供创建 API 和 CLI。

下方 Initial plan 保留已分享的原始方案；[2026-11-12 的范围修订](#2026-11-12看板顶栏创建入口待批准未实施) 取代其中“网页创建表单后置”的决定。用户明确 project 就是代码仓库，实际交付与验证见末尾实施记录。

## 1. Initial plan

### 背景、目标与范围

用户要求：“任务输入模型，先把 issue 改成不必选，可以直接注入内容创建”。当前唯一的新 Flow 业务入口是 Issue 标签事件；首轮和修订准备、审核上下文都直接读取 Issue，持久模型与数据库也要求 Issue 编号非空。[触发入口](../../app/controler/events/router.py#L58-L70)、[创建服务](../../app/controler/workflow/service.py#L92-L104)、[执行准备](../../app/controler/workflow/coordinator.py#L292-L343)、[持久模型](../../app/domain/models.py#L98-L112)、[数据库](../../app/persistence/schema.py#L6-L30)。

本次的成功标准是：提供仓库、installation、任务标题和正文，不创建或关联 Issue，也不发送 webhook，就能创建 queued Flow，并沿用 INITIAL、PR、反馈修订、合并归档以及显式 retry/resume。注入内容持久保存，控制器重启或修订 fallback 不会丢失原始任务。

范围包括输入契约、创建用例、存储、执行/审核输入、Issue 专属行为的适用条件，以及控制台对无 Issue Flow 的展示。首版不做网页创建表单、任务正文编辑、后续补绑 Issue、自动创建 Issue、非 GitHub 仓库、输入 provider 框架或新的工作流状态。

### 提议行为与示例

创建支持两种输入，首版二选一：

| 输入 | `issue_number` | `task_content` | 后续任务内容来源 |
| --- | --- | --- | --- |
| GitHub Issue | 正整数 | NULL | 按现有行为重新读取 Issue |
| 直接注入内容 | NULL | 标题与正文 | 保存于 Flow 的原始内容 |

两项都缺失或同时提供均拒绝。这个限制是首版的方案选择：避免把“任务内容”和“附加关联 Issue”同时引入；本次先交付无需 Issue 的创建能力。不使用 `0`、负数或伪造的 Issue 对象表示直接任务。

拟议 API 示例，不是当前已可用接口：

```http
POST /api/flows
Content-Type: application/json

{
  "repo": "b1indsight/agentflow",
  "installation_id": 42,
  "task_content": {
    "title": "为任务列表添加分页",
    "body": "列表接口支持 cursor 和 limit，并保留现有筛选行为。"
  }
}
```

这里的 installation 值仅为示例。成功返回 201 和 `FlowDetail`，其中 `issue_number`、`issue_url` 为 null。同仓库可创建多个互不相关的直接任务；每次成功 POST 都创建新 Flow，不按相同正文去重，也不自动重试结果不明的创建请求。

拟议 CLI 用法为 `flowtool flow create REPO --installation-id N --title TITLE --body-file PATH`；`--body-file -` 从 stdin 读取。Issue 输入使用 `--issue-number N`，不得同时提供内容选项。只有标题时正文允许为空；标题必须包含非空白字符。

错误处理：输入形状/范围不合法返回 422；已有同 Issue 的活跃 Flow 返回 409；无 owner 身份、跨源或非 JSON 请求沿用现有拒绝方式。GitHub 目标校验失败时返回经过现有错误分类处理的安全诊断，不创建 Flow 或 INITIAL 任务。

### 任务模型与唯一创建入口

新增一个中性的、不可变的 `TaskContent` 值类型，放在 `app/domain/models.py`，职责仅为表达编码任务的标题和正文：

- `title: str`：非空白标题；实际标题文本。
- `body: str = ""`：任务正文，可为空。
- 配置 `extra="forbid"`、`frozen=True`；不携带 repo、installation、Issue 身份、来源枚举或能力凭据。

`Flow.issue_number` 改为 `int | None`，新增 `task_content: TaskContent | None = None`。模型与数据库共同要求恰好一种输入存在。旧 Issue Flow 的 `task_content` 保持 NULL，不在 webhook 到达时静默快照 Issue 内容。

直接创建边界限制标题至多 256 个字符、正文 UTF-8 至多 32 KiB，并拒绝 PostgreSQL text/JSONB 不支持的 NUL。TaskContent 共用于读取到的 Issue 内容，因此这些注入专用上限不追溯限制现有 Issue。正文上限控制新输入成本；它不保证连同所有 patch/讨论后一定低于审核总预算。

拟议公共签名：

```python
# app/controler/workflow/service.py：用通用名称替换 on_trigger，保留一个创建用例。
def create_flow(
    self, repo: str, issue_number: int | None, installation_id: int | None,
    *, task_content: TaskContent | None = None,
) -> Flow | None: ...

# app/persistence/flows.py：扩展现有写入口，不再并列添加 create_task_flow。
def create_flow(
    self, repo: str, issue_number: int | None, installation_id: int | None,
    *, task_content: TaskContent | None = None,
) -> Flow: ...

# app/controler/console/api.py：挂在已有 owner-only router 上。
async def create_flow(request: Request, body: CreateFlowRequest) -> FlowDetail: ...

# app/github/console.py：来源枚举根据两项实际关联决定。
def sources(issue_number: int | None, pr_number: int | None) -> list[str]: ...
```

`CreateFlowRequest` 放在现有 `app/contracts/console.py`：`repo: str`、`installation_id: int`（正整数）、`issue_number: int | None = None`、`task_content: TaskContent | None = None`。严格校验仓库名、输入二选一及直接内容上限；未知字段拒绝。CLI 使用同一请求模型，避免两套输入规则。

服务仍是 DB-only：事务内创建 Flow 和 INITIAL，提交后通知已有调度入口。Issue 输入重复返回 None；直接输入创建成功返回 Flow。EventRouter 改为调用通用创建用例，不改变 allowlist、delivery 去重和 receipt 事务。无需新增 intake 服务、provider 接口或仅转发参数的包装层。

### 创建 API 与 CLI 的权限和 GitHub 校验

API 复用 Cloudflare Access owner 认证、同源 JSON 检查、无缓存头，以及 `runtime.console.db` 的阻塞数据库隔离。[现有认证](../../app/controler/api.py#L58-L98)、[现有写接口](../../app/controler/console/api.py#L67-L111)。

手动入口显式接收 installation_id。通过 `installation(id).repository(repo).refetch()` 做只读访问校验，并以返回的规范仓库名入库；Issue 输入还校验对应 Issue 可读取。网络读取必须在数据库事务之外完成；不在 webhook receipt 事务中加入 GitHub 请求。[GitHub 资源与认证](../../app/github/apps.py#L50-L104)、[现有仓库读取](../../app/controler/workflow/coordinator.py#L317-L335)。

CLI 沿用管理员持有 ServerSettings 和数据库访问权的边界，通过已有异步客户端完成只读校验，再调用同一服务。API 按现有方式在提交后唤醒 gateway；CLI 使用现有后台扫描/worker poll 发现 queued job，不增加 CLI 到进程内 gateway 的伪通知。实施时用真实双进程验证发现任务的延迟和可达性，若已有扫描不能发现，再在原调度所有者中补齐必要接线。

创建不在 HTTP/CLI 请求中执行编码或创建 PR；后续执行仍由既有 dispatcher 领取。创建结果不明时，首版要求调用者先查看 Flow 列表确认，不引入未经要求的通用幂等键机制。

### 存储、锁与数据库升级

`flows` 增加可空的 `task_content JSONB`，并移除 `issue_number` 的 NOT NULL；增加有名字的 CHECK，要求 `(issue_number IS NULL) = (task_content IS NOT NULL)`。保留现有活跃 Issue 唯一索引：PostgreSQL 普通唯一索引允许多条 NULL Issue，仍限制非 NULL 的相同仓库/Issue。没有必要仅为增加显式非空谓词而重建等价索引。[现有表和索引](../../app/persistence/schema.py#L6-L30)。

新库定义和已有库升级都放入现有 schema 初始化顺序，使用幂等的加列、放宽非空约束与加约束步骤；所有旧 Flow 自然满足 Issue 非空、直接内容为空。不能只改 CREATE TABLE IF NOT EXISTS。先具备新列，再更新依赖它的 console view；新 view 列追加到末尾，避免 PostgreSQL 拒绝替换已有列位置。[初始化入口](../../app/persistence/database.py#L149-L186)、[console view](../../app/persistence/console_schema.py#L8-L42)。

Issue 创建、retry、resume 继续按原顺序取得 Issue advisory lock，再取得 workflow/worker 锁。无 Issue 时跳过 Issue advisory lock、Issue 活跃查找和 `issue_flow_owners` 写入，继续以 Flow ID 和已有 workflow/worker 锁保证生命周期串行；不把同仓库所有直接任务锁成同一个“空 Issue”。[现有锁](../../app/persistence/database.py#L83-L119)、[resume/retry](../../app/controler/workflow/service.py#L151-L179)、[Issue owner](../../app/persistence/flows.py#L181-L189)。

INITIAL 的 task 内容只从 Flow 获取，不复制到 Job payload；直接任务的 INITIAL payload 可为空对象。retry/resume 不改原始输入，不清掉执行、结果或停止确认门槛。

schema 放宽后，旧程序无法读取含 NULL Issue 的 Flow；即使已归档，该限制仍存在。不提供自动删除这类历史或恢复 NOT NULL 的 down-migration。首次创建直接任务后，应前向修复，或由操作者按备份方案整体恢复；本次不自动操作任何运行中的数据库。

### 编码与审核使用中性任务内容

`InitialTask` 用 `task_content: TaskContent` 替换 `issue_title/issue_body`，并把 `issue_number` 改为可空；`RevisionTask` 同样用 `task_content` 替换 Issue 命名字段。worker prompt 和 executor 从同一值读取标题、正文，去掉“来自 GitHub Issue”的无条件表述。[当前 wire](../../app/contracts/protocol.py#L38-L100)、[当前 prompt](../../app/worker/execution/prompts.py#L40-L51)。

Issue Flow 仍在 INITIAL/REVISION 准备时读取最新 Issue，并映射为 TaskContent；直接任务使用持久输入，不发 Issue 请求。仓库、默认分支、PR/head/base、push capability、绑定与 assignment 围栏校验均保留。[首轮与修订准备](../../app/controler/workflow/coordinator.py#L292-L343)。

`ReviewContext.issue_number` 改为可空，新增 `task_content: TaskContent | None = None`。新直接任务的审核上下文携带这份输入，`issue=None` 是正常状态，不写“缺失 Issue”限制；Issue 上下文仍保存实际 Issue 资源，不伪造中性内容为 Issue。原有 patch、PR、反馈及总字节预算和不完整上下文 WAIT 语义保持不变。[审核上下文](../../app/domain/context_models.py#L40-L71)、[冻结过程](../../app/controler/review/context.py#L19-L175)、[Judge 检查](../../app/controler/review/judge.py#L45-L59)。

兼容必须保护旧冻结上下文的 JSON 和 digest：旧上下文缺少 task_content 时，不因解析默认值而新增序列化字段、补写内容或重算决策。用显式“空值不序列化”的字段规则维持旧摘要；在现有冻结/授权/保存决策公共路径验证旧 JSON 能继续使用。[摘要](../../app/domain/context_models.py#L69-L71)、[摘要授权与决策](../../app/persistence/reviews.py#L604-L655)。

这是严格 wire 的不兼容变更，协议从当前 v20 递增；若实施前已有其他协议变更，使用下一未占用版本。server 和 worker 成组升级，不承诺混跑。当前 journal 保存结果、停止和归档证据，不保存 INITIAL/REVISION 请求；不为本次输入变化擅自升级 journal、重算终态 digest 或改写旧回执。旧 ReviewContext 单独按上述序列化边界处理。[journal 记录](../../app/worker/state/record.py#L35-L55)、[当前严格协议](../../app/contracts/protocol.py#L38-L48)。

### 无 Issue 时跳过专属行为，保留 PR 闭环

- PR 正文：只在有关联 Issue 时自动添加 `Closes #N`；直接任务不追加虚假的关闭指令。[现有逻辑](../../app/controler/workflow/tools.py#L133-L146)。
- 合并归档：只向存在的目标发布完成标签和工作区处置凭据。直接任务仍向 PR 发布，省略 Issue 行和 Issue 副作用，工作区清理与 ACK 规则不变。[归档副作用](../../app/controler/workflow/archive.py#L63-L89)。
- 反馈：直接任务不播种或消费 issue_comments；PR 普通评论、reviews 和 threads 继续工作。[来源调度](../../app/persistence/reconciliation.py#L43-L68)、[来源读取](../../app/controler/reconciliation/feedback.py#L124-L147)。
- Projects：无 Issue owner 的 Flow 不参与现有 Issue 型同步，不新增 draft item 或 PR 型投影。[现有 join](../../app/persistence/projects.py#L63-L81)。
- 关闭、合并、废弃、blocked、停止和恢复照常以 Flow/PR 身份工作，不能因 Issue 缺失而绕过原安全门槛。

### 控制台能读到原始任务，不显示假 Issue

`FlowSummary.issue_number/issue_url` 与前端类型改为可空。直接任务的标题取 `task_content.title`，不被远端显示缓存覆盖；Issue Flow 继续使用已有标题缓存和 Issue 编号回退。无 Issue/PR 时显示 Flow ID。[当前 DTO](../../app/contracts/console.py#L34-L55)、[当前链接](../../app/persistence/console.py#L107-L134)、[当前前端回退](../../app/controler/web/App.tsx#L95-L97)。

console_facts 只增加直接任务标题，不把正文复制进每条状态投影/状态活动。`FlowDetail` 增加 `task_content: TaskContent | None = None`，详情按需读取原始输入；现有详情页在概览显示标题和正文，使用已有安全文本/Markdown 组件，不执行输入中的 HTML。Issue 链接只在存在时显示，不出现 `Issue #null` 或 `/issues/None`。[详情契约](../../app/contracts/console.py#L66-L69)、[详情页链接](../../app/controler/web/App.tsx#L615-L631)。

ConsoleSync 的 SyncClaim、来源枚举和读取作用域支持可空 Issue。无 Issue 时不登记 issue.* 来源；未交付 PR 时 GitHub 来源可为空，worker 历史与本地活动仍独立工作。已有 Issue/PR 的 source cursor 含义保持不变，不因函数参数重排而无故失效。[当前来源](../../app/github/console.py#L51-L66)、[同步 claim](../../app/persistence/console_sync.py#L18-L31)、[来源调用](../../app/controler/console/sync.py#L83-L126)。

### 备选方案与取舍

只放宽 issue_number、继续使用 issue_title/issue_body，会让持久输入、审核和副作用继续假设存在 Issue，不能交付完整能力。TaskContent 是被 Flow、编码任务和审核输入复用的内容值，不另建任务实体/表，也不引入来源 provider 抽象。

把所有 Issue 内容统一快照到 Flow 更容易统一读取，但会改变现有 Issue 编辑在后续准备中生效的行为，本次不采用。允许“注入内容 + 关联 Issue”还需决定正文优先级、去重和关闭语义；首版二选一，组合输入留到明确需要时再设计。

只提供内部 Python 调用不足以让操作者直接创建，因此本次包含受保护 API 与管理 CLI；完整网页创建表单不影响输入模型落地，留作后续工作。

### 实施顺序、风险与交付边界

1. 扩展 TaskContent、Flow、CreateFlowRequest、持久 schema 与统一创建用例；验证已有库无损升级、原子创建和 Issue 去重。
2. 调整编码/审核输入与旧冻结上下文兼容，递增 wire 版本，保持结果/journal 证据不变。
3. 补齐 PR、归档、Issue owner、反馈和 console source 的适用条件，验证直接任务能完成 PR 闭环。
4. 接入创建 API 与 CLI，完善详情和卡片的无 Issue 展示；更新 README/相关架构与发布说明。
5. 运行有区分力的回归、简化对照、完整检查与浏览器检查，再按仓库门禁交付同一 feature bookmark/PR。

主要风险是遗漏 Issue 假设、NULL 身份误去重、旧 context digest 变化，以及跨版本 worker 不匹配。用下述场景分别覆盖，不通过默认 Issue、空任务或删除历史掩盖问题。共有工作区的已有修改不属于本 feature；实施前重读涉及文件，仅集成本次差异。

本次批准计划不等于批准生产 schema 升级或部署。当前仅形成计划；提交、推送、创建草案 PR 等外部交付仍需具体授权，且不携带当前其他 feature 的修改。

### 验证与消融

优先扩展已有测试文件，不为每个 helper 建重复测试：

- 事务与模型：两条同仓库、不同正文的直接任务可同时活跃且输入不串；Issue 同编号仍去重。创建 INITIAL 失败时 Flow 和任务一起回滚；缺输入/冲突输入/空白标题/超字节正文拒绝。
- 升级与恢复：从旧 schema 与真实旧 Issue 行升级，重复初始化保持幂等；直接内容经过重读、retry、resume 仍相同。没有假 Issue owner，不与其他直接 Flow 冲突。
- 编码与反馈：通过现有 coordinator/remote E2E 替身覆盖直接 INITIAL → PR → PR 反馈 → REVISION/fallback → 合并归档。测试的 Issue 读取入口拒绝任何无 Issue 请求；检查具体内容、PR 正文和有效目标，而非仅检查任务存在。
- 兼容：旧冻结上下文与已保存 decision 能继续通过 digest 授权和恢复；新直接任务上下文含实际任务，不因预期的 issue=None 进入 WAIT；原有不完整证据仍 WAIT。直接正文与等价 Issue 内容使用非对称文本验证来源选择。
- API/CLI：真实 ASGI + 隔离 PG 验证 owner/同源/JSON 边界、仓库不可达零写入、创建及列表/详情可读。CLI 双进程验证 queued job 被发现，不只断言模拟通知次数。
- 显示与同步：没有 Issue 时不创建 Issue 来源或 URL，不错误进入 Projects；PR 评论/线程和会话仍可读取。浏览器检查直接任务的排队态、已有 PR 态以及原 Issue 任务；桌面和手机检查标题、正文、链接，并截图后实际查看。

重点复用 `test_workflow_transactions.py`、`test_store.py`、`test_coordinator.py`、`test_remote_e2e.py`、`test_review_context.py`、`test_control_triage.py`、`test_console_api.py`、`test_console_sync.py`、`test_archive_status.py` 与 `test_project_sync.py`，不要求为每条修改各加一例。

实施后、自动代码审查前，用临时运行时替换分别比较：

1. 去掉持久任务读取：直接任务在 INITIAL/后续 REVISION 得不到原始正文，跨重启/修订公共场景应失败；若不失败，补强该场景或简化实现。
2. 恢复无条件 Issue 来源/关闭指令：直接任务出现非法 Issue 查询或虚假引用，对应端到端检查应失败。
3. 去掉新 context 空字段的序列化省略：旧冻结上下文的已保存 decision 授权失败，兼容回归应捕获；不改真实数据库中的证据。

对照后删除实验文件并恢复代码。检查命令沿用 CI：隔离 PostgreSQL 下 `uv run pytest`、`uv run ruff check app tests`、`uv run mypy app tests`；前端 `npm run format:check`、`npm test`、`npm run build`、`npm run test:e2e`；`python scripts/build_console.py` 与 `uv build --no-sources`。这些均为后续验证计划，当前没有运行产品测试。

### 开放问题与后续工作

无须补充业务信息即可评审此方案。需要用户批准的范围选择是：首版输入二选一，并包含 API/CLI，暂不做网页创建表单。

实施时核对 CLI 后台任务发现、数据库 JSONB 到 TaskContent 的原生映射、错误状态码与现有约定，以及实际协议版本占用；这些不要求新架构。网页创建表单、注入内容同时关联 Issue、编辑任务、创建幂等键及 installation 自动发现均暂不包含。

计划草案通过本机 Claude Code Oracle 生成后，结合实际源代码修订：已核验初始化与响应模型均为 `claude-opus-5-5`，工具/MCP 列表为空，正常退出。修订纠正了草案中的 journal 请求持久化假设、worker 历史来源、冗余创建入口/新模块、等价索引重建及“归档即可回退”的不成立说法；补充旧审核摘要兼容。这是设计咨询，不是执行验证。

## 2. PR change timeline

### 2026-11-12：看板顶栏创建入口（待批准，未实施）

**新增要求：** 用户要求“给看板顶栏加一个新建flow的按钮，需要选择project， 然后输入内容开启一个flow”。网页入口纳入本次 feature，不再后置。下述设计仍待批准，尚未修改产品或测试代码。

**project 的含义：** 暂按任务所属的 GitHub 代码仓库处理，选择项显示 `owner/name`。当前 `ServerSettings.projects` 是按仓库配置的 GitHub Projects v2 状态同步目标，不是通用的项目清单；现有任务、调度和工作区也以仓库为目标。本修订不增加本地 Project 实体，不要求配置 GitHub Projects v2。若用户指的是 GitHub Projects v2 看板，需要先修订这个目标契约。[配置含义](../../app/controler/config.py#L46-L46)、[仓库键约束](../../app/controler/config.py#L68-L76)、[Projects v2 配置](../../app/github/projects.py#L33-L43)。

#### 用户操作与输入

1. 在看板顶栏、刷新按钮附近增加“新建 Flow”，点击打开创建对话框。复用现有按钮、输入控件和样式，优先使用原生 `dialog`，不增加前端依赖。对话框有明确标题、字段标签与校验提示；支持键盘操作、提交前 Esc 关闭、关闭后回到触发按钮。桌面和手机都保留可见入口，不挤掉现有搜索与刷新。[顶栏](../../app/controler/web/App.tsx#L1279-L1316)、[现有键盘焦点处理](../../app/controler/web/App.tsx#L459-L482)。
2. 表单只有两个必填项：“Project（代码仓库）”选择器和“任务内容”多行文本框。不提供 Issue 或 installation 字段，也不额外要求用户填写标题。
3. 标题从首个非空白行取值，去除该行首尾空白，按 Unicode 码点取前 256 个字符，在表单中显示预览；完整文本原样保存为 `task_content.body`。首行过长只影响展示标题，不丢失正文。全空白、含 NUL、正文超过 32 KiB UTF-8 时拒绝提交并显示具体原因；前后端均校验，正文不静默截断。API/CLI 继续使用原方案的显式 `TaskContent(title, body)`。
4. 只有已选目标、内容合法且未提交时才能点击“开启 Flow”。提交前取消不发送创建 POST；创建等待期间禁用重复提交及关闭操作，避免把关闭表单误解为撤销创建。
5. 收到 201 后关闭并清空草稿、刷新看板数据，按返回 ID 打开 `#flow/ID/overview`，直接显示任务内容与排队状态。导航不依赖当前筛选是否包含新任务；没有空闲 worker 时正常排队。[现有详情 tab 和路由](../../app/controler/web/App.tsx#L78-L93)、[看板刷新与导航](../../app/controler/web/App.tsx#L1268-L1335)。

#### 创建目标来自授权清单，不限于历史任务

新增受现有 owner 认证保护、`no-store` 的 `GET /api/flow-targets?cursor=...`，在现有 console 契约和 API 所有者中提供分页响应：`items: [{repo, installation_id}]`、`next_cursor`、`has_more`。前端选中项目时保存整对身份，再提交既有拟议 `POST /api/flows`；installation ID 不暴露为需要填写的表单字段。

目标从已有 `GitHubApp.fetch_installations()` 和 `Installation.fetch_repositories()` 只读获取，排除暂停安装和归档仓库；不调用标签扫描器或执行标签写入。每次响应只推进有界的安装/仓库页读取，继续游标校验安装页、安装 ID 锚点和仓库页；不用易随过滤变化的数组下标充当身份，也不在一次请求中扫描全部仓库。锚点已失效时明确提示重新读取，不静默跳过目标。原始页过滤后为空仍保留后续游标。[现有安装与仓库分页](../../app/github/apps.py#L53-L104)、[清单和标签操作的边界](../../app/controler/reconciliation/labels.py#L50-L93)。

现有 `/api/repos` 保持看板历史筛选语义：它只从 `console_flows` 取已出现的仓库，不能单独用于创建选择，否则零历史仓库永远无法开启首个 Flow。也不借用 Projects v2 的可选同步配置充当准入清单。[历史仓库查询](../../app/persistence/console.py#L319-L340)、[现有 API](../../app/controler/console/api.py#L49-L56)。

选择器区分加载中、部分结果仍可加载更多、读失败可重试和全部读取后无可用项目；未耗尽游标时不能宣称没有项目。保留已选目标和任务草稿，切换或重新加载清单不清空正文。首版使用分页选择，不增加跨所有安装的搜索索引、持久清单表或后台刷新服务。

创建 POST 仍在数据库事务外重新校验所选仓库与 installation 的访问绑定及目标可用状态，不把曾经出现在下拉列表当作当前授权；发现授权撤销、安装暂停或仓库归档时不入队。现有仓库绑定本身不发网络请求，不能代替校验；需要补充可用性事实时，直接扩展既有 GitHub 资源的读取字段。[本地绑定与显式读取约定](../../app/github/repositories.py#L80-L106)、[安装资源](../../app/github/apps.py#L67-L104)。

#### 拒绝和结果未知分别处理

输入校验或明确的权限/目标拒绝保留草稿，显示可操作错误；目标失效时允许重新加载清单。清单 GET 可重试，创建 POST 不自动重试。网络中断、5xx 或成功响应丢失均按“结果未知”提示，提供关闭表单并清空筛选、刷新看板的核对入口；不显示创建成功，也不提供直接重发建议。仍沿用首版无通用幂等键的范围，人工核对后再次创建可能重复这一风险明确保留。[现有写入错误边界](../../app/controler/web/api.ts#L138-L161)。

#### 实施和验收增量

在原计划第 4 步接入创建 API/CLI 时，一并实现目标清单、顶栏入口、表单状态及成功导航，复用原子创建服务和既有 dispatcher。不在浏览器请求内运行编码，也不借此改动 Issue 触发规则、审核授权或部署状态。

扩展现有 `test_console_api.py` 与 `web/e2e/console.spec.ts` 的公共行为用例，除原计划回归外重点检查：

- 没有任何历史 Flow 的仓库仍可选择并创建；历史 `/api/repos` 的筛选结果不变。多个 installation 和多页、全被过滤的中间页仍能读取后续目标，游标非法或锚点失效有明确错误；读取清单不触发标签等写操作。
- 错配 repo/installation、列表读取后撤销授权或归档目标、非 owner 与跨源创建均零写入。受保护的 GET 和 POST 不缓存，不在数据库事务内做网络读取。
- 含前导空行、多字节中文、超过 256 码点且含 emoji 的首行生成可读标题，正文逐字保留；正文恰好 32 KiB 和超出 1 字节的结果不同，空白或 NUL 拒绝。
- 从顶栏选择仓库、填写内容到排队详情的浏览器路径正确；已有筛选不隐藏创建结果。等待期间连点只创建一次，提交前取消零创建；明确拒绝保留草稿，结果未知时不自动重发或报告成功。
- 用真实 ASGI 与隔离 PostgreSQL 验证创建后能由现有调度执行 INITIAL，不以浏览器 mock 的 201 代替后端验证。UI 用既有 Playwright 运行桌面和手机的默认、表单、加载/空/错误、提交等待与成功详情状态，检查焦点/键盘并截图后实际查看。

本次范围修订仍由本机 Claude Code Oracle 根据明确提供的材料草拟，初始化与响应均为 `claude-opus-5-5`，工具/MCP 为空，进程正常退出。人工核对并补足了实际已存在的 overview 路由、取消仅保证零创建 POST、清单与创建时状态校验的区别。这些是设计检查；产品测试和浏览器验证留待批准后实施。

### 2026-11-12：持久任务输入、统一创建与看板入口（已实施）

**要求与决定：** 用户确认 project 是代码仓库。采用中性 `TaskContent` 和可空 Issue，首版输入仍二选一；保留原 Issue 实时读取语义，不引入 Project 实体、provider 层、任务正文编辑或通用幂等键。

**实际实现：**

- `domain/models.py`、`persistence/schema.py/flows.py` 保存原始输入、校验 XOR；无 Issue 不参与 Issue 锁、去重与 owner。`ControlService.create_flow` 统一事件/API/CLI 创建，Flow 与 INITIAL 原子提交，retry/resume 不改任务输入。
- `contracts/protocol.py` 使用 wire21；coordinator、worker 初始及修订任务共用 TaskContent。直接任务不请求 Issue，Issue 任务仍读取最新内容；journal5 与 ResultInbox v1 不变。
- review 读取持久任务并沿用上下文预算，截断明确进入限制；None task_content 不进入旧 context JSON，旧摘要、已保存 decision 与答案授权保持有效。
- PR 正文不为直接任务追加关闭指令；反馈与控制台同步跳过 Issue 来源，归档只处理真实 PR。console 投影只复制标题，正文按需在概览安全展示，Issue URL/编号可空。
- owner-only `GET /api/flow-targets` 有界分页读取授权仓库，保留过滤后的空页 continuation；`POST /api/flows` 在事务外重新校验 repo/installation 与 archived 状态。CLI 使用同一请求校验，支持 UTF-8 文件/stdin；文件换行原样保存。
- 顶栏按钮、原生 dialog、项目分页选择与任务文本框已接入。标题按首个非空白行的 Unicode 码点生成，正文完整保存。提交防重复、取消零写入；明确拒绝保草稿，结果未知锁定重发并提供刷新核对入口；成功打开新 Flow 概览，不受旧筛选隐藏。

**集成时补齐：** 完整回归发现旧四态迁移重建当前 console view 前缺少 `task_content`。在该迁移已有的 Flow DDL 中只增加 nullable 列供视图读取，旧行保持 NULL，不运行会改变执行表的启动 schema；现有迁移、备份与事务回滚回归保持通过。仓库 refetch 的 SDK 替身补齐真实 `archived` 事实，没有用默认值掩盖缺失观察。

本地恢复 smoke 的合成 INITIAL 同步改用 TaskContent。实际运行隔离 `scripts.smoke.recovery`，数据库与 HOME 恢复通过、原 journal 不变、复制 root 被按预期拒绝；这只验证备份夹具，不代表活跃 Flow 恢复或 GitHub 副作用已验收。协议与 live smoke 回归、脚本静态检查通过，日志为 `.amp/in/flow-input-recovery.log`。

**升级和交付：** 当前六态库启动幂等增加输入列、放宽 NOT NULL 并添加 XOR；旧 workers/四态库仍遵守原显式迁移门槛。server/worker 需成组升级到 wire21；首次保存 NULL Issue 后不能直接降级旧程序，即使任务已归档。只改本地代码并构建包，未操作运行中的数据库或 GitHub 共享状态；工作区原有其他改动保留。

### 2026-11-12：组合验证与简化对照（已完成）

**测试先行：** 协议、事务/API、直接审核和前端创建回归先暴露不支持的输入/入口，再实施；端到端测试继续复用既有 fixture、真实 PostgreSQL 与 Git/jj 本地目录，不建立平行测试框架。

**执行验证：** 数据库均为本线程临时 PostgreSQL 55473，测试逐例使用独立 schema。完整 `FLOWTOOL_TEST_DATABASE_URL=… uv run pytest -q --tb=short`、`uv run ruff check app tests scripts/migrate_flow_states.py`、`uv run mypy app tests` 通过。随后补强 256 码点标题/恰好 32 KiB 多字节正文的 API 正边界并重跑 `tests/test_console_api.py` 通过；超限/NUL/空白负边界保留。前端 `npm run format:check`、`npm test`、`npm run build`、完整 `npm run test:e2e -- --workers=2`，以及 `uv run python scripts/build_console.py`、`uv build --no-sources` 均通过。保留现有 Starlette/httpx 弃用告警和 Vite bundle 大小提示。

**跨进程调度：** 一次性脚本启动真实 Uvicorn/ASGI、鉴权 HTTP 与 WebSocket；先注册空闲 peer，再由独立 CLI 进程和 owner POST 各创建一个直接任务。两个 INITIAL 均通过现有 worker poll 派发，非对称标题/正文逐字匹配，包括 CLI 文件的 CRLF；未调用 gateway 伪通知或真实 GitHub 写入。脚本与测试 schema 随后清理。日志：`.amp/in/flow-input-live-check.log`。

**消融，逐项只替换一个机制：**

| 比较 | 实际结果 | 决定 |
| --- | --- | --- |
| coordinator 去掉持久任务读取，全部改读 Issue | 原 Issue 对照通过；直接任务在构造不存在的 Issue 时失败 | 保留统一输入读取分支 |
| PR 正文恢复无条件 `Closes #N` | Issue 对照通过；直接任务正文多出 `Closes #None`，回归失败 | 保留按真实 Issue 生成关闭指令 |
| context 空字段不再省略 | 旧冻结 JSON 多出 `task_content:null`，兼容回归失败 | 保留省略规则，不重写旧证据 |
| 前端 Unicode 标题改为 JS 字符串切片 | emoji 标题的码点边界回归失败 | 保留码点切片 |
| 清单重读失败后直接沿用旧 continuation | 桌面/手机重读回归失败 | 保留 reset-aware 读取重试 |

后端临时进程运行时替换未写入生产文件；恢复原实现后相同回归全部通过。消融日志为 `.amp/in/flow-input-ablation-{persisted-input,issue-closing,context-null}.log`；前端对照由 UI 子任务运行并恢复。

**画面与边界：** 桌面 1440 与手机 390 的看板、表单、加载/空/失败、等待、未知结果、排队详情、直接 PR 和原 Issue 状态均由 Playwright 实际渲染并截图检查，证据保存在 `.amp/in/artifacts/flow-create-*.png` 与 `flow-input-*.png`。截图仅说明外观；请求正文、取消、重复提交、键盘和安全 Markdown 由执行断言验证。GitHub、编码 agent 和 provider 使用替身；真实 GitHub App、Access 登录及生产多轮闭环仍待部署验收。

可直接查看[本线程实施记录与审阅截图](https://ampcode.com/artifacts/flow-16b8216d-5e1a-4188-b9ba-baa5efc24800)。

### 2026-11-12：顶栏仅保留加号（已实施）

按用户要求，新建入口只显示加号，与右侧刷新按钮共用 `btn ghost` 样式，保留“新建 Flow”无障碍名称、表单打开及关闭后的焦点恢复。桌面和手机截图均已检查，创建流程定向回归、前端格式与构建通过。未改变输入和提交契约。

### 2026-11-12：隔离交付并对齐最新 main

从本功能开始前的工作区快照提取任务输入改动，在独立 jj workspace 中应用到最新 main，原共享工作区及其他功能保持不变。合并冲突保留 main 已有的正常 Runtime 构造、真实 Worker 测试和“PR 合并即业务归档、原 Worker 异步清理”行为；只接入可空 Issue、TaskContent 和统一创建入口。新增的直接任务通知回归同时核对“本轮已归档”，不再断言清理已完成。

版本说明按当前实现区分：wire21 / journal5，执行结果继续使用 ResultInbox v1，Control 本地归档通知保留 main 的 ResultInbox v2。本功能不调整两类 inbox 的格式或历史摘要，前文“inbox1 不变”指执行结果，不表示删除 main 新增的本地通知类型。部署与 README 的当前说明同步澄清这一边界。

### 2026-11-12：取消 TaskContent，分离任务原文与展示标题

**范围修订与决定：** 用户要求取消本 PR 新增的 `TaskContent` 类。任务输入统一为字符串；有关联 Issue 时，在 INITIAL/REVISION 准备阶段读取最新标题与正文，以空行拼接（正文为空只用标题）；直接输入则持久保存原文。展示标题异步推理，兜底顺序为 Issue 缓存标题、PR 标题、`Flow #ID`。这替代前文的标题/正文对象、前端首行标题和 JSONB 输入列，不增加 Issue 编辑触发会话、正文编辑或来源抽象。

**实际实现：** 删除 Python/TypeScript TaskContent；Flow、API、CLI、wire21、Worker prompt 和直接任务审核统一消费字符串。`flows.task_content` 使用 TEXT，保持 Issue 与直接输入的 XOR，Issue 行的该列仍为 NULL。表单只提交完整文本，CLI 使用 `--content` 或 `--content-file`（`-` 为 stdin），校验非空白、NUL 和 32 KiB UTF-8，不截断原文。审核仍受原有总预算和单来源预算约束，截断明确进入 WAIT；旧 Issue 冻结上下文不增加 null 字段，保留原 JSON/digest 和结果证据。

**标题生成的边界：** 新增 nullable `flows.title` 与 `title_job_id`；在 INITIAL 已创建 attempt、读到任务原文并提交 delivery 准备后，独立事务恰好入队一个已有 inference job，同时记录关联 ID。编码不等待模型调用；标题入队的可预期错误只保留兜底。推理沿用既有槽位、取消规则和最多三次派发，总预算 30 秒；永久失败不另排新逻辑调用。标题提示仅取任务前 12000 字符以满足推理预算，完整编码输入不变。排队且尚未准备的 Flow 使用兜底标题，不新增后台扫描器。

**并发与恢复取舍：** 标题结果与推理结算在同一显式事务提交；条件 UPDATE 只接受该 Flow 引用的 title job，并拒绝空白、多行、控制字符和超过 256 字符的标题。通用 `update_flow` 不写标题列，旧 Flow 对象不能抹掉异步结果。title_job_id 和工作流锁防重复入队；attempt 更换/归档按原规则取消推理，不绕过既有 currentness。只替换原 PR 尚未部署的输入格式，不新增 TaskContent 数据转换或双格式兼容层；真正 pre-feature 的 Issue 历史行与审核快照仍保持可读。

**咨询与核对：** 本机 Claude Code Oracle（实际初始化模型 `claude-opus-5-5`，工具/MCP 均为空，正常退出）建议隔离标题入队事务、条件更新避免陈旧对象覆盖，以及结果和标题原子提交。已核对 Database 的嵌套事务共用连接、推理的 worker→Flow→job 锁顺序、独立 inference 槽位与编码 claim 排除 inference 的条件；标题落库保持在业务结果应用所有者，不把展示逻辑移入通用 worker 仓储。

**交付状态：** 本修订在原 feature 的隔离 jj workspace 实施，尚未推送更新到 PR #144、未合并或部署；原已推提交的 CI 全部必需检查成功、镜像发布跳过。验证与简化对照结果在本轮完成后续记。

### 2026-11-12：纯文本任务的组合验证与简化对照

**执行验证：** 使用本线程临时 PostgreSQL 55475、逐例隔离 schema，完整 `FLOWTOOL_TEST_DATABASE_URL=… pytest -q` 通过；`ruff check app tests scripts/migrate_flow_states.py scripts/smoke/recovery.py`、`mypy app tests`、前端格式/类型/单元/生产构建、`python scripts/build_console.py` 与 `uv build --no-sources` 通过。完整回归补齐了新增标题 inference 的旧固定任务列表断言，并让通用推理替身明确处理标题调用，未禁用真实标题路径。日志位于本线程 `.amp/in/task-text-{all-tests-final,static-final,package-build}.log`。

**停止与资源释放：** 工作区并发测试曾直接将 Flow 改为 finalizing 并请求 stop，绕过业务入口的 `cancel_jobs`，使新增 queued inference 留存。改为调用已有 `ControlService.finish`，在没有推理 Worker 时仍能取消标题任务、确认原编码停止，并以更高 generation 将工作区交给下个 Flow。结果恢复、推理清理和并发派发的组合回归通过；没有因此放宽归档条件或改动生产停止实现。

**前端限制：** 桌面/手机的直接创建、精确字符串提交、异步标题刷新及 Issue/PR 身份用例通过，截图已实际检查。完整 Playwright 的会话滚动用例出现约 282.89px 偏移；只读导出旧提交 `6cd3280a79a68fc89c7dc479f2f9e30fea0f6199`，除隔离端口外使用原代码，执行 `CI=1 npm run test:e2e -- --workers=1 --grep 'large activity stays windowed' --repeat-each=5` 也在同一断言复现相同偏移。当前代码该用例独立重复通过，但不能据此宣称完整浏览器回归全绿。保留为旧版也存在的未定位不稳定性，不改会话实现或降低断言门槛；日志为 `.amp/in/task-text-ui-{final-tests,baseline-repeat,repeat}.log`。

**新增机制的简化对照：** 在独立 pytest 进程内替换方法，不编辑生产文件；每次只移除一个机制。

| 简化方案与复现入口 | 观察结果 | 决定 |
| --- | --- | --- |
| `_enqueue_title` 不检查 `title_job_id`；运行 `tests/test_coordinator.py::test_issue_edits_change_prepared_text_without_resubmitting_title` | 重复准备产生第二个标题任务并替换关联 ID，回归拒绝 | 保留持久关联和防重复条件 |
| `_apply_inference` 去掉包围结算与标题写入的显式事务；运行 `pytest -q tests/test_coordinator.py -k title_is_inferred_once` | 标题写入被注入中断后，结果却已标记应用完成，无法按原入口重放标题 | 保留共同事务 |

未加载替换的完整回归通过；比较日志为 `.amp/in/task-text-ablation-{once,atomic}.log`。临时替换插件和旧版导出目录在验证后删除。保留原 TaskContent PR 的两个已知审查问题边界，不夹带旧 wire 快照迁移或 Issue/PR 类型校验修复。该修订仍仅在本地隔离 workspace，未推送更新或部署；真实推理服务、GitHub 和 Access 生产集成未在本轮连接。

### 2026-11-12：拒绝手动 Issue 输入中的 PR 编号

用户要求只修复第二项审查问题，并更新第二个提交。共享 `Issue.refetch` 在丢弃 GitHub 原始字段之前检查 `pull_request`，发现 PR 即返回已有 validation 错误，不为 API/CLI 增加重复校验或新抽象。复用现有入口回归，先证明两处均误创建 Flow，再验证 HTTP 422、CLI 失败且无 Flow/INITIAL 写入；真实 SDK 的缺省标记及显式 null 仍作为 Issue 接受。旧修订快照兼容和标题持久化归属不在本次修复范围。
