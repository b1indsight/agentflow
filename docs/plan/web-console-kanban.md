# Web 控制台与 Flow Kanban — Feature record

**Stage:** Backend implementation / 本地实现与自动化检查已完成；审查、远端 CI 及真实部署验收分别记录
**PR branch:** `feat/web-console-backend`；同一草稿 PR 承载方案及后续实现
**Frontend PR:** [#86](https://github.com/b1indsight/agentflow/pull/86)
**Backend issue:** [#85](https://github.com/b1indsight/agentflow/issues/85)

## 1. Initial plan

### Summary and motivation

为现有 headless control 增加一个单账号、只读 Web 控制台：用 Kanban 查看 Flow 的进度，
点击卡片查看任务、交付、worker 和 Codex 会话。GitHub 继续承载 Issue、review 和人工合并。

当前已有 Cloudflare Access owner 认证、会话查询 API、Flow/Job/交付/worker 持久数据和
Projects v2 状态投影。控制台复用这些事实，不创建第二套工作流状态或独立身份系统。

依据：
- [当前实现状态](../architecture/README.md#当前实现状态)
- [未来控制台方向](../architecture/roadmap.md#9-未来方向roadmap)
- [会话查询契约](../architecture/conversation-api.md)
- [单账号认证与部署](../deployment/access.md)

### Goals, scope, and success criteria

首版范围：

1. 五列看板：排队中、执行中、待审核、受阻、已完成。
2. 仓库筛选、生命周期筛选、按 Flow/Issue/PR 编号及已知 PR 标题搜索；终态历史按需查看。
3. 卡片展示 Flow/Issue 身份、已知 PR 标题或 Issue 编号回退、阶段、当前任务/worker、
   PR 链接和更新时间。未获取 Issue 标题时不伪造标题，也不逐卡实时请求 GitHub。
4. 点击卡片打开详情，展示生命周期、任务、branch/PR、修订次数和安全的恢复诊断。
5. 会话按需读取，区分 user/assistant/tool/system/developer，工具调用/结果可折叠，
   显示截断、不支持记录及不可用原因；提供继续加载和增量刷新。
6. 桌面看板配右侧详情；窄屏切换为选中列的卡片列表和全屏详情，支持键盘、焦点管理和深链接。
7. 空库、无搜索结果、认证失败、数据库故障、worker 离线、无 session、游标失效均有明确状态。

本 PR 不包含：拖卡改变状态、创建 Flow/Issue、发消息或实时 inject、cancel/retry/drain 写入口、
自动合并、多人权限、GitHub 完整活动时间线、独立看板配置或新的实时消息基础设施。
这些是首版提议边界，用户可在批准前调整。

完成条件：启动现有 serve 即可访问控制台；真实 PostgreSQL 数据能驱动看板和详情，
浏览器通过现有会话 API 读取历史；未经授权不能读取页面或数据；不影响 webhook/worker 路由。
真实 Access 登录/MFA 与目标域名上线仍需独立环境验收，不能由本地测试替代。

### Proposed behavior

- `/console` 打开看板；URL 保留筛选与所选 Flow，刷新和浏览器前进/后退可恢复位置。
- 默认聚焦 active Flow；生命周期筛选允许显示成功、失败、取消的历史。
- 五列是派生展示状态。failed/cancelled 在受阻列显示明确终态标签，不能被误读成仍在运行。
  closed PR 暂挂、准备期退避、worker 恢复和未完成副作用分别显示具体原因。
- 卡片展示本地已知事实，不因浏览器轮询触发 GitHub 写操作或调度。
- 看板建议每 5 秒刷新；隐藏页面暂停轮询，失败退避，显示最后成功刷新时间和旧数据提示。
- 会话仅在详情打开且可见时轮询；按现有从旧到新的游标契约继续读取，按钮使用“继续加载”，
  不承诺倒序读取。has_more 为 true 的空页仍能继续；409 清空对应会话分页并重新读取。
- 切换 Flow 或关闭详情时取消旧请求，旧响应不能覆盖当前 Flow；加载错误不抹掉已展示历史。
- worker 离线只影响历史查询提示，不直接改写 Flow 的业务状态。
- 所有文本按不可信内容展示，不将 Issue/PR/会话内容直接作为 HTML 渲染。

### Technical design and affected modules

**前端：** 首版采用随 Python 包分发的 HTML/CSS/JavaScript 原生模块，由 FastAPI 同源提供。
不增加 Node 构建、前端框架或 CDN 运行时依赖；使用 URL、fetch、语义化 HTML 和明确的视图状态。
界面职责集中到 `app/controler/web/`，不将 UI 拼装逻辑放入 domain 或 worker。
如果实现中出现显著状态管理复杂度，再记录是否改用框架；不预先扩展成通用组件平台。

**读取 API（拟定）：**

- `GET /api/flows`：有界分页、仓库/生命周期/搜索筛选、各阶段计数和卡片摘要。
  列表每页默认 50、上限 100；服务端筛选在分页前执行；计数覆盖当前筛选全集，
  已加载卡片数与总数分开显示。稳定按 Flow ID 倒序翻页，允许刷新时重新取首段。
- `GET /api/flows/{flow_id}`：选中 Flow 的交付、当前执行、attempt、任务与安全诊断。
  任务历史有界分页；禁止直接序列化数据库模型、job payload、原始结果或副作用 payload。
- `GET /api/flows/{flow_id}/conversation`：复用现有契约及 worker 通信，不更改协议。
- 仓库筛选数据来自有界列表查询的数据库聚合，无需逐 Flow 查询或新增 GitHub 读取。

**阶段规则：** 对照 `ProjectStore.refresh()` 的现有 todo/in_progress/in_review/blocked/done
逻辑建立唯一的可复用本地查询投影。控制台不读取可能未启用或尚未同步的 Projects 远端投影。
终态优先、closed PR/失败任务及失败 effects 受阻、merged 待归档仍执行中、pending job/result
优先于 open PR 的待审核状态；具体 SQL 与现有规则对照验证，避免新增第二套不一致分类。
Projects 保留自身 Issue 归属选择与远端投影逻辑；只抽取真正共用的 Flow 阶段判定。

**数据层：** 新增专属只读查询仓储，批量读取摘要并有界分页，避免对每张卡依次查询仓储。
同步数据库调用按既有线程边界执行，不能阻塞异步 HTTP 事件循环。首版不需要 schema 迁移；
若真实查询计划显示必须增加索引，记录证据与迁移方式后再加。

**认证与部署：** 页面与所有新增数据接口均复用 owner Access 验签，配置缺失时 fail closed。
浏览器不保存或输入 App/worker/API token；不新增共享密码或生产认证绕过开关。
静态资源仅包含公共应用代码，不嵌入配置秘密；使用同源外部脚本与样式，设置 CSP/no-store
等适用响应头。Tunnel 仅增加 `/console`、必要静态资源和明确用户 API 路径，
不得因此开放 `/docs`、worker、metrics、探针或其他内部路径。

预计涉及：

- `app/controler/api.py`：挂接控制台路由，复用现有 owner dependency。
- `app/controler/web/`：只读 HTTP 契约/路由与静态界面。
- `app/persistence/`：有界控制台读取与共用阶段投影。
- `app/persistence/projects.py`：如需共用阶段规则，最小调整引用；不改变同步语义。
- `app/controler/runtime.py`：仅在确有生命周期需求时装配读取服务。
- `tests/`：API、阶段分类、权限与分页回归；浏览器验证脚本优先放临时目录。
- `README.md`、架构/Access/部署文档及相关 Tunnel 模板：入口、能力与安全边界。
- 打包配置：只在需要时明确包含静态文件，验证 wheel 和 serve 镜像内容。

### Alternatives and rationale

- **选用轻量同源静态界面**：现有项目只有 Python 构建；首版读取与分页交互不需要独立
  SPA 服务或 Node 工具链。代价是需要手写少量视图状态与 DOM 更新。
- **React/Vue 独立 SPA**：适合后续复杂写入与交互，但当前增加工具链、部署及鉴权集成成本。
- **仅链接 GitHub Projects**：已有同步能力，但无法直接查看本地 worker、恢复状态和 Codex 历史。
- **状态拖拽**：列来自多个持久事实，拖卡不能安全地等价为一次状态更新，因此首版不提供。
- **视觉 A：看板 + 抽屉（建议）**：快速切换并保留看板上下文；手机使用全屏详情。
  **视觉 B：看板 + 独立详情页**：适合长时间阅读，页面切换较多；URL 深链接两者均保留。
  草图仅供布局选择；示例标题、计数和时间不代表真实数据，倒序加载文案不沿用。

### Costs, risks, and mitigations

- 状态漂移：共用阶段判定并对照已有 Projects 分类；展示 lifecycle 与原因，不只展示颜色。
- 会话可能含敏感源码：沿用 owner 认证和现有脱敏边界，不承诺识别所有秘密；不持久化浏览器历史副本。
- 存储数据泄漏：API 显式白名单输出，诊断使用安全代码/摘要，不输出凭据、内部路径或完整异常。
- 大量 Flow/历史：数据库与 DOM 均有界，按需分页，避免无界自动拉取或只筛选当前页。
- 轮询成本与竞态：详情按需加载、请求取消、串行轮询和退避，不引入 SSE/WebSocket 新协议。
- 静态资源漏打包：检查 wheel，并用非源码目录安装运行，避免只在开发工作目录可用。

### Implementation order

1. 用户批准范围与视觉方向；沿用一个 feature bookmark 和 PR。
2. 定义读契约和唯一阶段投影，实现数据库分页及权限保护的摘要/详情接口。
3. 实现看板、筛选、详情和会话加载，覆盖窄屏与错误/离线状态。
4. 更新打包、部署文档和管理域名路由；执行自动化、浏览器和安装后验证。
5. 更新本记录并按仓库审查流程交付；不自动发布、配置 Cloudflare 或触发业务仓库操作。

### Validation and ablation strategy

- **测试：** PostgreSQL-backed API 覆盖身份拒绝、筛选后分页、真实五列状态、终态及 closed/merged/
  pending-result 优先级；与已有 Projects 判定对照。检查响应无原始敏感字段。
- **会话：** 多页和空页续读、409 重置、worker 离线、无 session、查询超时、切 Flow 取消旧请求。
- **浏览器：** 用确定性数据在桌面与手机宽度实际渲染并截图检查；运行 DOM/交互检查，
  覆盖过滤、深链接、键盘/焦点、刷新、错误提示及会话折叠。测试替身不得成为生产后门。
- **构建：** Ruff、Mypy、pytest、uv build；安装构建产物后验证静态资源和路由，
  具备 Docker 环境时补 serve 镜像 smoke，否则明确未验证。
- **消融：** 在临时验证中移除 owner dependency，确认权限测试失败；破坏阶段优先级，
  确认 closed/merged/pending-result 场景失败；移除旧请求隔离，确认延迟响应切 Flow 场景失败。
- **执行时机：** 实现后、代码审查前执行；消融后恢复原代码并重跑受影响检查。
- **真实环境：** Access/GitHub 登录、MFA 和目标部署在实际环境单独验收，不以本地签名测试替代。

### Open questions and deferred work

批准前需要确认：是否接受本方案的只读首版及推荐的 A 抽屉布局。
如希望首版就能发消息、创建任务或 cancel/retry，需要先补对应写入契约与安全设计。

实现时确定：具体空态文案、响应 DTO 命名、可共用阶段投影的最小形态和静态资源打包方式。
实时消息、写操作、拖拽编排和多人协作不在本 PR 范围内。

## 2. PR change timeline

### [001] — 2026-10-16 — B 独立详情页与左侧竖向 Flow 标签栏

- **Context / requirement update:** 用户选择 B，并要求点击卡片进入独立详情页时，
  看板转换为左侧竖标签栏。原始方案保留作为比较基线，本条更新决定后续布局。
- **Options considered:** A 的右侧抽屉保留较多看板空间，但限制长会话阅读宽度；
  B 的独立详情可提供宽阅读区，左侧竖标签栏继续承担跨 Flow 导航。
- **Decision / status:** 用户已选定 B 和左侧竖标签栏方向，替代初始方案第 6 项及视觉 A 推荐。
  将竖栏细化为按五个阶段分组的 Flow 标签是本次设计提议，尚未单独获用户确认；
  这次布局选择不视为对全部技术方案或开始实现的批准。
- **Consequences:** 默认页保持五列 Kanban；点击卡片后导航至独立 Flow URL，
  页面左侧显示精简任务导航，右侧作为完整详情主区，不再并排保留五列看板或使用右侧抽屉。
  提议竖栏保留当前仓库/搜索条件、五阶段分组与计数，并在分组下列出 Flow 短标签；
  当前 Flow 高亮，点击其他标签切换详情，无需返回看板。
  返回看板时恢复原筛选和滚动位置，浏览器前进/后退与深链接保持可用。
  竖栏分组沿用看板分页数据，计数与已加载数量区分，允许继续加载；
  当前 Flow 因过滤或自动更新移出列表时，详情保留并明确提示，不自动跳到其他任务。
  手机将竖栏收起为可打开的 Flow 导航，关闭导航后详情使用全屏。
- **Implemented changes:** 仅更新计划和设计草图，未修改产品代码或测试。
- **Evidence / validation:** 新草图展示“完整看板 → 左侧分组 Flow 标签栏 + 独立详情”的两个页面状态；
  草图是视觉参考，不是浏览器运行证据，示例数据也不代表实际任务状态。
- **Outcome / pending:** 布局方向已确定；竖栏分组细节与完整实施方案仍待确认，未提交、推送或创建 PR。

### [002] — 2026-10-16 — 先制作可交互 HTML demo 调整细节

- **Context / requirement update:** 用户接受 B 调整方向，要求先制作 HTML 交互 demo，
  一起调整细节。本轮范围为独立设计原型，不接数据库、GitHub、Worker 或生产认证。
- **Decision / status:** 采用单文件 HTML/CSS/JavaScript 和内置示例数据；可直接在浏览器打开，
  不依赖服务、CDN、构建产物或真实凭据。此次请求只授权原型，不扩大到正式后端实施。
- **Implemented changes:** `.amp/in/artifacts/agentflow-demo.html` 提供五列看板、搜索/仓库/状态筛选、
  hash 独立详情、左侧阶段分组 Flow 切换、概览/会话/任务页签、工具折叠、示例记录续读、
  离线/无 session/空搜索状态、手机单列看板与可收起导航。刷新和 Issue/PR 链接仅提示示例行为。
  手机五个状态标签同屏显示，中文输入法合成期间不替换输入框。
- **Evidence / validation:** 用 `.venv/bin/python .amp/in/check-kanban-demo.py` 通过真实 Chrome CDP
  执行 29 项交互/布局检查，未捕获 JavaScript 异常；报告为 `.amp/in/artifacts/demo-checks.json`。
  已检查桌面和 390px 手机截图；Prettier 格式检查通过。
  Demo 会话里的测试命令和“18 passed”均为示例文本，不能视为本项目后端验证。
- **Outcome / pending:** 可交互 demo 已就绪，正式 API、认证接线和生产部署均未实施。
  原型、临时验证脚本及截图保存在本地排除的 `.amp/in/`，未提交、推送或创建 PR。

### [003] — 2026-10-16 — 去掉全局顶栏

- **Context / requirement update:** 用户反馈顶栏意义不大。
- **Decision / status:** 移除全局品牌、工作空间和示例头像顶栏，不保留隐藏开关；
  看板直接展示标题和筛选，详情的左右两区使用完整视口高度。
- **Implemented changes:** 删除顶栏 DOM 生成函数、调用和相关样式；移除桌面 64px/手机 56px
  的顶部预留，手机导航与遮罩覆盖整个视口。详情的“示例数据”提示并入现有只读标签，
  看板保留原有页脚示例说明。
- **Evidence / validation:** Chrome 交互检查扩为 32 项并通过，包含无顶栏、详情全高和手机遮罩
  顶部覆盖；桌面看板、详情及手机详情/导航截图已检查，无残留顶栏空槽。Prettier 检查通过。
- **Outcome / pending:** 仅更新本地 HTML demo 与方案记录；未接后端，未提交或推送。

### [004] — 2026-10-16 — 用功能顶栏集中筛选与导航信息

- **Context / requirement update:** 用户要求筛选和搜索放到顶栏，左边放路径，右边放刷新和状态，
  并要求说明 Kanban 的具体内容。
- **Decision / status:** 本条取代 [003] 的完全无顶栏布局。保留一条有实际操作的功能栏，
  不恢复品牌、头像或独立大标题/统计区。左侧路径、中部仓库/状态筛选及搜索、右侧刷新与快照状态。
- **Implemented changes:** 看板和详情共用功能栏；详情侧栏移除重复的仓库选择与搜索，
  只承担 Flow 分组导航。手机将筛选搜索折行，导航及遮罩从功能栏下方展开。
  状态为“示例快照”，不代表后端或 Worker 实际在线。
- **Evidence / validation:** 33 项 Chrome 交互/布局检查通过；Prettier 检查通过；已检查桌面
  看板/详情及手机看板/详情/导航截图，无重叠或横向溢出。
- **Outcome / pending:** 本轮仍只更新本地示例原型和方案记录，未接真实服务，未提交或推送。

### [005] — 2026-10-16 — 卡片合并交付编号并展示当前动作

- **Context / requirement update:** 用户要求 Issue/PR 编号合并，有 PR 时显示 PR 编号，
  隐藏 Flow 编号，并加入“正在做什么”。
- **Decision / status:** 卡片和详情左侧导航统一采用 PR 优先、Issue 回退的主编号，
  不展示 Flow 内部编号；详情路径与正文保留完整身份，便于诊断和追溯。
- **Implemented changes:** 删除卡片重复的底部 PR 编号和 Flow 编号，主编号只出现一次。
  卡片动作行展示等待分配、首轮编码、第 N 轮修订、等待审核、等待执行恢复或归档完成；
  Worker 降为次要信息，已归档任务不再重复显示 worker/归档标签。左侧任务标签同步简化编号。
- **Evidence / validation:** 38 项 Chrome 检查通过，新增覆盖 PR 优先、Issue 回退、隐藏 Flow、
  动作展示和详情仍保留两个关联编号；Prettier 检查通过。已检查桌面看板、手机看板和详情截图。
- **Outcome / pending:** 已更新本地交互 demo；正式动作需从执行事实推导，当前仍为示例数据。
  未接后端，未提交或推送。

### [006] — 2026-10-16 — 加长桌面搜索栏

- **Context / requirement update:** 用户认可看板整体布局，要求中间搜索栏更长，并进一步讨论详情。
- **Implemented changes:** 桌面搜索宽度由 230px 调整为随窗口变化的 260–560px，
  1440px 窗口下约 461px；解除原有 310px 上限。较窄窗口和手机继续使用各自已有响应式规则。
- **Evidence / validation:** 39 项 Chrome 检查通过，新增搜索加长且不产生横向溢出检查；
  Prettier 检查通过。已检查桌面看板和详情截图，路径、筛选与刷新均完整可见。
- **Outcome / pending:** 本轮只调整原型搜索栏，详情尚未按新要求改版；未接后端，未提交或推送。

### [007] — 2026-10-16 — 可折叠详情侧栏与详情信息优化

- **Context / requirement update:** 用户要求左侧栏可折叠，其余详情按已讨论建议优化；
  本轮仍限独立 HTML 交互 demo。
- **Decision / status:** 桌面侧栏收为 48px 窄栏，保留展开按钮；切换标签和筛选时保持收起状态，
  不重新渲染会话来完成折叠。手机继续使用任务导航浮层，不套用桌面收起宽度；刷新页面后恢复默认展开。
- **Implemented changes:** 删除详情内重复路径及阶段/分支元数据行，突出标题和当前动作，
  仓库、关联编号、Worker、更新时间作为次要信息。概览先展示当前动作、原因与下一步条件，
  再展示任务信息和关键进展；排队、执行、审核、受阻、完成分别提供示例说明。
  “任务”改为“执行记录”，中文记录名优先，技术类型与 attempt 标识次要显示，并注明并非完整历史。
- **Evidence / validation:** 58 项 Chrome 检查通过，覆盖侧栏收起/展开、焦点与可访问名称、
  工具展开状态保留、标签/筛选切换、五阶段概览、执行记录和 1440/1024/390px 视口；
  无 JavaScript 异常，Prettier 与 JavaScript 语法检查通过。已检查桌面展开/收起、窄桌面、
  概览各阶段、执行记录及手机详情/概览/执行记录/导航截图。
- **Outcome / pending:** 本地 demo 与方案记录已更新，数据和刷新仍为模拟，未接真实服务；
  未提交、推送或部署。正式控制台实现仍待方案批准。

### [008] — 2026-10-16 — 紧凑执行信息与混合会话交互原型

- **Context / requirement update:** 用户认可将任务信息精简为分支、Worker、修订三项，
  要求会话包含当前 Flow 关联 Issue/PR 的全部 GitHub 交互；移除会话加载/刷新按钮，
  改为下滑加载、末尾可点击分界线刷新。讨论后用户要求制作新版 demo 看效果。
- **Decision / status:** 本轮只制作本地模拟交互，不连接 GitHub 或 Worker。
  顶栏全局刷新保留；会话按旧到新排列，先下滑读取当前样例，再用末尾分界线获取样例新增。
  正式实现需补充 GitHub 记录读取与跨来源合并，不能把现有 Codex 历史视为完整 GitHub 时间线；
  可获取记录之外的删除/丢失历史不承诺还原。
- **Implemented changes:** 八格任务信息改为三行执行信息，提供分支复制，移除头部重复 Worker，
  排队不虚构分支，终态标记最后执行 Worker。会话样例加入 Issue 正文/评论、PR 描述/评论、
  Review、行内讨论与回复、提交/检查/审核通过/合并事件，以及 Agent 消息/工具输出。
  审核输入默认折叠以减少重复；来源、作者和时间明确。下滑按段追加，分界线可演示检查中、
  有新增、无新增；保持阅读位置与讨论展开状态。Worker 离线时仍可独立追加 GitHub 样例。
  手机工具名称允许换行，结果标签保持完整，修复截图检查发现的裁切。
- **Evidence / validation:** 75 项真实 Chrome 检查通过，无 JavaScript 异常；覆盖分页、
  分界线刷新/去重/阅读位置、跨 Flow 异步隔离、离线来源、无 Agent 会话、精简字段、
  复制成功/拒绝（剪贴板接口模拟）及桌面/手机布局。已检查混合会话、Review 讨论、
  分界线及新增、精简概览和手机截图；未将样例内的测试输出作为实际后端测试结果。
- **Outcome / pending:** demo 与方案记录更新在本地，未提交、推送或部署；真实 GitHub
  读取、完整交互类别覆盖、来源失败重试与正式合并分页仍待生产实现阶段处理。

### [009] — 2026-10-16 — 统一代码块与状态分隔线

- **Context / requirement update:** 用户要求代码块使用 GitHub 标准样式，“本次新增交互”
  使用与末尾刷新相同的分割线；侧栏状态分组参考所附截图的名称—横线—数量布局。
- **Implemented changes:** 代码上下文与工具输出统一采用浅灰底、等宽字体、保留缩进及块内横向滚动，
  使用 `pre/code` 结构并支持键盘聚焦。新增标记与末尾刷新共用分割线样式，但新增标记不作为刷新按钮。
  侧栏移除状态标题圆点、箭头与数量胶囊，横线延伸到右侧裸数量；保留分组点击折叠与整体侧栏折叠。
  只借用参考图的分隔结构，维持原型现有浅色主题。
- **Evidence / validation:** 81 项 Chrome 检查通过，无 JavaScript 异常，格式与语法检查通过；
  新增覆盖状态分隔结构、分组折叠、代码块样式、手机块内横向滚动和新增/刷新分隔线一致性。
  已检查桌面默认会话、Review、刷新新增及手机导航/分隔线截图。
- **Outcome / pending:** 仅本地交互 demo 与方案记录更新，未接真实服务，未提交、推送或部署。

### [010] — 2026-10-16 — 从交互原型进入正式控制台实施方案

- **Context / requirement update:** 用户暂时接受当前视觉效果，要求继续下一步。
  将 [001]–[009] 的界面要求作为正式实施依据；本条补充真实数据接入方案，尚未实施。
  以下范围替代初始方案的 A 抽屉、排除 GitHub 活动时间线和会话加载/刷新按钮等提议。
  原型确认不等同于批准新增生产接口、数据迁移或部署。

#### 正式交付范围

1. 保留当前五列看板、宽搜索功能栏、PR 优先编号和当前动作；独立详情页带按状态分组的
   可折叠左侧导航。正式 URL 保留筛选、详情和页签；直接打开及浏览器前进/后退均可恢复。
2. 概览使用当前动作/原因/下一步和分支、Worker、修订次数三项执行信息；执行记录有界分页。
   标题缺失、没有分支或无执行记录时显示事实性空态，不能把 demo 文案当作真实执行事实。
3. 会话包含当前 Flow 关联 Issue/PR 在现有授权下可读取的 GitHub 交互和 Agent 历史。
   下滑继续加载，末尾分界线点击刷新；保留顶栏全局刷新，不恢复会话加载/刷新按钮。
4. 首版只读：不包含拖卡、创建任务、发表评论、发消息、cancel/retry/drain 或合并操作。
   所有 GitHub 请求仅用于读取，不因浏览或刷新触发工作流或修改共享状态。

#### 已核实的复用点与缺口

- `app/controler/api.py` 已提供 owner 保护、no-store 的 Agent 会话读取；
  `app/controler/workers/conversation.py` 已封装 worker 请求、超时、并发上限及跨 session 游标。
  保留该接口与 worker 协议，增加聚合层，不能把 Agent 查询改名后当作混合时间线。
- `app/persistence/projects.py::ProjectStore.refresh()` 已派生五类阶段；最小提取共享判定，
  让控制台和 Projects 对同一个 Flow 得到相同结果，不依赖 Projects 是否启用。
- `app/github/issues.py`、`pull_requests.py`、`threads.py` 已有评论、Review 和讨论分页。
  可复用认证、错误分类和分页基础，但现有反馈 DTO 主要为审核处理服务，
  缺少展示所需的部分作者/发生时间/链接/代码位置字段，不能直接序列化给页面。
- 尚缺完整的活动、提交和检查读取。Webhook inbox 会清理历史 payload，不能充当永久历史库。
  不为只读页面扩大 webhook 保留期限，也不改变现有审核判断语义。

#### 接口与模块建议

- 沿用 Python 包内原生 HTML/CSS/JavaScript、FastAPI 同源分发方案，不新增前端框架或 CDN。
- 新增控制台专属只读查询和显式 DTO：列表/阶段计数、详情、执行记录；
  过滤先于分页，聚合计数覆盖过滤全集，列表默认 50/上限 100，避免逐卡数据库或 GitHub 查询。
  当前动作由 job、交付和恢复事实派生；无法判断时显示“状态待确认”，不编造进度。
- 拟新增 `GET /api/flows/{flow_id}/activity` 承载混合会话读取，
  内部复用现有 conversation service 和 GitHub 读取器。来源分别报告读取状态与续读信息，
  GitHub 失败不抹掉 Agent 结果，Worker 离线不阻止 GitHub 历史。
- 活动条目使用来源命名空间下的稳定 ID，区分发生时间与更新时间，
  包含类型、作者、关联对象和安全正文；讨论保留父子关系、文件位置及 resolved/outdated 状态。
  同一条 GitHub 评论经 timeline、Review 和讨论读取时只显示一次；编辑更新原条目。
- 生产静态资源、页面和 API 均复用 owner 验签，缺失配置时拒绝访问。
  不提供浏览器 token 输入框、认证绕过或生产 demo 开关。正文以不可信文本处理，
  代码块保留 `pre/code` 和块内横向滚动；链接只允许安全协议，不执行正文内 HTML。

#### GitHub 覆盖与完整性

- 逐类核验并覆盖 Issue/PR 描述与评论、Review 及行内讨论/回复、提交、检查结果，
  以及 API 可读取的开启/关闭/重新打开、合并、标签、指派和审核请求等关联活动。
  reactions 等附属交互也需核验可读性和展示方式，不能仅实现 demo 中列举的几条样例。
- 实现前对照 GitHub 当前 API/SDK 验证每类数据的字段、权限和独立分页，
  形成覆盖表：已支持、权限不足、GitHub 不提供或历史已不可恢复。
  不将“全部”解释为可以还原已删除正文、过期检查日志或授权范围外的数据。
- 外层列表和讨论内部回复都必须继续分页；单次请求限制条数、字节、时间与远端请求数。
  预算耗尽或来源失败时返回续读位置/限制原因，页面不能显示“已全部加载”。
- 旧评论被编辑、讨论解决/重新打开、PR 新增提交或检查改变，都不能只靠尾部新增游标发现；
  刷新需要针对可变对象重新核对，更新已有条目，明确显示本次核对范围。

#### 合并分页的实施约束与待验证点

- Worker 为跨 session 的正向读取，GitHub 各连接各自分页；单独一个 `has_more=false`
  既不代表所有来源已读完，也不代表任务结束。统一游标必须绑定 Flow、来源进度和读取版本，
  不能把某来源的空页或暂时不可用当成整个会话结束。
- 初次浏览按旧到新组织，来源内保留原始顺序；不能仅把两个任意页按时间排序后就宣称
  全部历史已按时间正确合并。先验证来源时间和顺序保证，再确定有界合并策略。
  若需要新增投影或持久化才能满足完整排序，应先补充数据保留、迁移和成本决策，
  不默认复制完整 Worker 日志，也不暗中缩小 GitHub 交互范围。
- 刷新新增保留阅读锚点与折叠状态；编辑原位更新。补录的早期记录需明确标记，
  不能伪装成刚发生的新事件，也不能无提示地移动正在阅读的记录。
  跨 Flow、筛选或页签切换要隔离异步结果；游标失效只重置受影响来源并明确提示。
- 具体来源游标编码、缓存和排序策略在第一步契约验证中确定；
  如果验证表明无法满足这些交互约束，先回报差异，不以近似行为宣告完成。

#### 实施顺序与验收

1. 核验 GitHub 覆盖和混合分页契约，确定安全 DTO、错误状态、排序与刷新规则；
   若需 schema 变化，先追加具体方案再实施。
2. 接入真实数据库列表、阶段计数、概览与执行记录，再将已确认 demo 拆成正式静态资源。
3. 完成 GitHub 读取与 Agent 聚合，接上下滑分页、末尾刷新、去重、编辑及部分失败状态。
   这些仍属同一功能交付，不能以仅接通 Agent 会话作为最终完成。
4. 检查 owner 权限、敏感字段白名单、筛选后分页、五阶段一致性、双层讨论分页、
   旧记录编辑、重复事件、空页续读、限流、Worker 离线及跨 Flow 延迟响应。
   用临时消融确认鉴权、阶段优先级和响应隔离检查能捕获破坏。
5. 实际渲染桌面/手机的默认、收起、离线、分页和刷新状态，截图检查；
   运行 Ruff、Mypy、pytest、`uv build --no-sources`，在源码目录外安装产物验证页面和静态资源。
   本地替身/浏览器测试和真实 GitHub/Access 环境验收分别报告，不相互替代。
6. 更新部署模板与文档只准备代码，不自动修改 Tunnel、发布部署或触发业务操作。
   提交、推送和 PR 仍须遵循用户授权，当前仅保存本地方案。

- **Implemented changes / validation:** 本轮仅追加实施方案；只读 fetch 显示远端 main 无变化，
  并直接检查上述 API、持久化、GitHub 读取器及 CI 配置。未修改产品代码或测试，
  未执行或宣称通过真实后端、GitHub、Access 和部署验收。
- **Decision / pending:** 布局延用已确认原型；只读正式版及本条接入方案待用户批准。

### [011] — 2026-10-16 — 先交付前端，后端能力记录为需求 issue

- **Context / requirement update:** 用户要求“先只做前端，需要的后端能力做成需求 issue”。
  本条将 [010] 的本轮实施范围缩小为前端；数据库查询、HTTP 路由、GitHub 读取聚合、
  Worker 协议、认证托管与部署均不在本轮改动范围。
- **Decision / rationale:** 将已确认 demo 整理为 `app/controler/web/` 原生静态模块，
  仍无框架、CDN 或运行时构建步骤。示例事实集中到 `fixtures.js`，
  不用直接嵌入 HTML 的活动数据，也不添加对未实现接口的请求或隐式 mock 回退。
  本地可用普通静态 HTTP 服务预览；`flowtool serve` 尚未挂载控制台。
- **Implemented changes:** 保留看板、独立详情、侧栏分隔/折叠、精简概览、执行记录及混合会话；
  新增 URL 筛选恢复，详情筛选只更新导航和提示，保留会话 DOM、工具展开与阅读状态。
  会话通过可取消的异步 fixture 分页读取，末尾刷新追加/无新增；切 Flow/页签取消旧读取。
  手机导航具备模态语义、背景 inert、键盘焦点循环和 Escape 返回。
  错误沿用末尾分界线重试；修复重试后仍有分页时寻找不存在的刷新按钮导致的焦点异常。
  所有活动正文、作者/时间、属性、工具输出及线程回复统一转义，保留 GitHub 风格代码块。
  静态入口 CSP 禁止 API 连接及内联脚本；界面持续标记“前端预览 / 示例数据”。
- **Backend requirements:** 按用户要求已在自己的仓库创建
  [#85：只读控制台后端 API](https://github.com/b1indsight/agentflow/issues/85)，无标签。
  其中列出看板/仓库/全集计数、详情/执行记录、GitHub + Agent 聚合、独立来源分页与编辑刷新、
  有界读取/完整性、权限与安装产物托管及验收条件，引用现有 #49 / #47，避免重复性能需求。
  当前 fixture offset 和展示 blocks 不冻结为正式 HTTP 契约，真实交互覆盖仍待该 issue 实现。
- **Validation:** `.venv/bin/python .amp/in/check-web-console.py` 在新建 Chrome 页面中通过
  92 项交互/布局检查，未捕获 JavaScript 异常；覆盖 URL 恢复、DOM 保留、手机焦点、
  分页/刷新、离线/无 session/归档、失败重试及跨 Flow 异步隔离。已渲染并检查桌面/手机截图。
  `node --test tests/web-console.test.mjs` 的正文/元数据/代码上下文转义回归通过；
  在生成的临时模块中移除转义会使同一测试失败，原源码保持不变。
  严格 JSDoc 类型检查、Prettier、Ruff、Mypy 和 `uv build --no-sources` 通过。
  wheel 包含全部前端静态文件；另生成仅供评审的单文件 HTML 快照，内联资源使用 CSP hash，
  在真实 `file://` URL 下通过 8 项检查（含手机已加载详情与导航），没有外部资源请求或 JS 异常。
  浏览器报告、截图和转义消融记录在本地排除的 `.amp/in/artifacts/`。
- **Outcome / pending:** 已交付本地前端代码、运行说明和后端需求 issue；
  未修改 Python/数据库/Worker 后端，未运行完整后端 pytest 或真实 GitHub/Access 集成验收，
  未提交、推送、创建 PR 或部署。前端预览不代表真实控制台已上线。

### [012] — 2026-10-16 — 改用 React，为后续扩充建立前端基础

- **Context / requirement update:** 用户倾向直接采用 React，为后续扩充做基础。
  这项技术选择取代初始方案与 [011] 中继续使用原生 DOM 模块的决定；
  已确认的布局、交互和“仅前端、使用示例数据”范围保持不变。
- **Proposed implementation:** 采用 React + TypeScript + Vite，使用 npm lockfile 固定依赖。
  将看板、详情、侧栏、任务概览、执行记录和会话拆成实际业务组件，
  用 React 状态与 effect 管理交互，移除字符串 HTML 拼接和手工 DOM 更新。
  保留现有 CSS 与 URL 深链接行为，不同时重做视觉设计。
  示例数据和可取消的分页读取保持独立，接入真实 API 留给 #85；
  不为尚未确定的后端契约创建通用 SDK 或隐式 mock 回退。
- **Dependencies / boundaries:** 首轮不增加全局状态库、请求缓存库或 UI 组件库。
  使用组件状态、必要的自定义 hook 和原有 URL 约定，正文由 React 文本节点安全渲染，
  不使用 `dangerouslySetInnerHTML`。Vite 负责开发与静态构建；
  生产产物不需要 Node 服务。本轮不修改 Python 路由、认证、数据库或部署。
- **Validation plan:** TypeScript 检查与生产构建通过；通过实际浏览器验证桌面/手机看板、
  独立详情、筛选与前进后退、侧栏折叠/焦点、会话分页/末尾刷新、错误重试、
  切换任务时的取消隔离与正文转义。检查构建产物的预览，截图确认视觉不回退。
  更新前端运行说明和受迁移影响的测试，不保留并行的原生实现。
- **Status:** React 技术方向已由用户提出；本条迁移方案待确认，尚未开始迁移代码。

### [013] — 2026-10-16 — React / Tailwind / shadcn/ui 前端迁移

- **Context / decision:** 用户了解 shadcn/ui 与 Vite 后确认“就这样迁移”。
  本条按 React + TypeScript + Vite + Tailwind CSS + shadcn/ui 实施，
  取代 [012] 中暂不引入 UI 组件库的提议；其余前端范围与已确认交互不变。
- **Implemented changes:** 增加锁定 npm 依赖、类型检查、开发/构建命令和独立 Frontend CI job。
  `main.tsx` 挂载 React StrictMode，`App.tsx` 管理 URL、筛选与页面交互，
  `views.tsx` 承载业务视图组件，`activity.tsx` 用文本节点渲染结构化正文，
  `useConversation.ts` 管理可取消分页与完成读取的缓存。删除原生 JS 渲染实现及旧转义器，
  将原有正文安全回归迁至前端的 TSX 组件测试。通过 shadcn CLI 添加 Button / Input 源码，
  使用 Tailwind 主题与本地类名工具，保留已有布局 CSS，无 CDN、全局状态库或请求缓存库。
- **Behavior / trade-offs:** Conversation 按 Flow ID 设置 React key，切任务/页签卸载时取消请求，
  避免会话状态串用；活动条目也使用稳定 key，使筛选/刷新不重建已展开记录。
  中文输入分开维护输入法草稿和已提交查询。保留侧栏分组折叠、URL 深链接、浏览器导航、
  手机焦点循环/Escape/inert、下滑分页与末尾分界线刷新。
  Vite 开发模式仅在 loopback 上运行，为 HMR 移除页面 CSP；生产构建继续禁止内联脚本和 API 连接。
  Node 仅用于开发/构建，静态产物无需 Node 服务。示例缓存和 offset 不作为正式后端契约。
- **Packaging boundary:** 实际构建发现 uv 默认把本机 `node_modules` 带入 wheel，
  含 7,116 个依赖目录/文件条目，产物约 50.7 MB。添加 uv source-exclude，
  同时在 Docker context 排除前端依赖与预览 dist，避免改变后端运行产物。
  改后 wheel 约 0.28 MB，wheel/sdist 均无 `node_modules` 或预览 dist；
  Python 路由、数据库、Worker、认证与部署流程均未接入前端，正式托管仍属于 #85。
- **Validation:** `npm ci`、Prettier、组件正文安全回归、严格 TypeScript / Vite 生产构建通过。
  生产产物在 Chrome 通过 92 项原有交互/布局检查；补充 17 项检查覆盖开发 StrictMode、
  真实 CDP 输入法草稿/提交、全部 URL 筛选恢复、取消后重入、分组保留、Escape/resize 与未知路由。
  从实际构建生成带 CSP hash 的单文件评审快照，通过 8 项 file URL 检查；
  已检查桌面看板/详情、已加载的手机详情与导航截图，无捕获的 JavaScript 异常。
  Ruff、Mypy（180 源文件）和 `uv build --no-sources` 通过。
  记录位于 `.amp/in/artifacts/react-checks.json`、`react-extra-checks.json`、
  `react-snapshot-checks.json` 和 `react-package-checks.json`。
- **Ablation:** 仅在临时副本移除 Conversation 的 Flow key，并对照相同的 42 → 37 切换：
  正式版正确显示 Issue #88 的两条记录；无 key 的副本显示旧 Issue #82，共八条记录。
  因此保留明确的组件身份边界，不用更多状态重置 effect 补偿。正式源码未参与破坏性实验。
  uv 排除规则的前后构建也验证了新增打包约束的必要性。
- **Outcome / pending:** 本地迁移完成；未提交、推送、创建 PR 或部署。
  CI 配置已准备但未在 GitHub 运行；完整后端 pytest、Docker 构建及真实 GitHub/Access 集成未验证。
  后端仍使用 #85 跟踪，本轮不实现真实接口或生产托管。

### [014] — 2026-10-16 — 16px 正文基准与有界渲染窗口方案

- **Context / requirement update:** 用户要求以 16px 正文为标准试用，其余字号相对正文调整；
  所有可能无限增长的区域只渲染固定最大窗口内的内容。
- **Typography proposal:** 桌面/手机会话及说明正文均为 1rem（16px），
  卡片标题 16px、侧栏名称 15px、控件/代码 14px、辅助信息 12–13px，
  详情标题桌面 28px、手机 24px。集中定义相对字号 token，调整行高、控件高度及换行，
  不以整页缩放代替，也不继续在手机端把正文缩小。
- **Rendering proposal:** 用虚拟滚动保留连续历史和完整滚动范围，实际挂载可见区域及少量缓冲，
  每个列表窗口硬上限先定 60 条，代码/工具输出先定每窗口 200 行。
  覆盖看板各列、左侧分组任务、执行记录、关键进展、会话活动、活动内 blocks、Review 回复；
  仓库候选也采用有界候选列表。固定的五阶段与三个页签不虚拟化。
  超长正文和超长单行按有界文本分块浏览，明确当前位置并可继续读取，不静默截断。
  滚动容器只限制高度不能满足此要求，不将 CSS overflow 或 content-visibility 当作挂载上限。
- **State and constraints:** 列表窗口使用稳定身份，窗口外保留必要的展开/折叠状态及阅读锚点，
  焦点所在条目进入窗口预算，避免卸载正在操作的控件；窗口切换不被当成历史读取完成。
  保留下滑续读、末尾分界线刷新和新增标记，字体/宽度或折叠高度变化后重测布局。
  渲染窗口与数据缓存分开处理：预览分页缓存设有界淘汰，较早内容可重新读取，
  不宣称固定 DOM 窗口同时解决了所有内存增长。真实服务端反向续读/有界页面缓存契约仍归 #85。
- **Implementation choice / trade-offs:** 拟按需增加 `@tanstack/react-virtual` 管理变高列表测量，
  在业务层显式限制窗口数量和文本分块。相比手写通用虚拟列表，多一个依赖，
  可减少滚动偏移、展开重测和滚动容器管理的自制逻辑；不得只依赖默认 overscan 而没有硬上限。
- **Validation plan:** 用临时大数据样例覆盖万条任务/会话/回复、超长工具输出与单行文本，
  实测挂载条目/文本大小不超限，向前向后均可访问，新增/刷新不跳阅读位置。
  验证焦点、折叠、筛选、切 Flow 请求隔离及桌面/手机/200% 缩放布局；
  对照无窗口版本证明 DOM 上限检查能发现退化，再运行类型、格式、组件测试与生产构建。
- **Status:** 需求已记录；以上窗口语义、限额和新增依赖的具体方案待用户确认，尚未修改实现或测试。

### [015] — 2026-10-16 — 侧栏切换按钮只保留图标

- **Requirement / implementation:** 用户要求去掉侧栏收回按钮的圆角矩形背景框。
  移除展开/收起两态按钮的背景、边框、圆角和阴影，悬停时也保持透明；
  保留原点击区域、辅助标签和键盘焦点轮廓。不改动 [014] 中待确认的字号与渲染窗口。
- **Validation:** 生产构建、格式检查、正文组件测试通过；Chrome 通过 7 项按钮检查，
  包含两态外观、悬停、点击切换及真实 Tab 焦点。已检查两态与键盘焦点截图；
  更新下载快照并通过 8 项 file URL 检查。初次焦点检查因 CDP 前台/目标页面设置失败，
  在独立前台页面设置设备尺寸后验证通过，无产品代码补丁。
- **Delivery:** 仅本地修改，未提交、推送或部署。

### [016] — 2026-10-16 — 圆形角色头像与固定详情头部

- **Requirement / implementation:** 会话角色头像统一使用圆形。详情标题、状态、关联信息和页签
  保持固定，只有页签下方的内容独立滚动；桌面和手机采用同一布局。
  通过新增内容滚动容器实现，保留正文原有宽度和间距，会话分页观察器、切任务滚动重置
  和焦点恢复使用同一个新容器。未更改 [014] 中待确认的字号与渲染窗口方案。
- **Validation:** 格式检查、正文组件测试、类型检查与生产构建通过。Chrome 通过 18 项专项
  检查，覆盖桌面/手机圆形头像、头部固定与正文移动、下滑分页、末尾刷新和阅读位置/焦点
  保留、页签操作、切任务重置与无横向溢出；已检查两种尺寸滚动前后的截图。
  原浏览器回归脚本将滚动目标从 `.detail` 调整为 `.detail-body` 后通过 92 项；
  更新单文件 HTML 快照，以同样的滚动目标调整通过 8 项 file URL 检查。
- **Delivery:** 仅本地修改，未提交、推送或部署；后端未改动。

### [017] — 2026-10-16 — 统一查询框与筛选建议（待确认方案）

- **Requirement:** 将顶栏仓库、关键词、生命周期筛选合并为单个搜索框；筛选方式参考 GitHub，
  加长至正文宽度，下拉面板统一风格。保留左侧路径、右侧刷新与示例状态。
- **Proposed interaction:** 单框支持普通关键词与 `repo:agentflow`、`is:active` / `is:done`、
  `status:queued|running|review|blocked|done`、`issue:82`、`pr:95`。
  字段间使用 AND，同字段重复时显示错误；关键词按词 AND，支持引号短语。
  不实现 GitHub 全套查询语法；不支持的字段、无效值或未闭合引号显示提示，保留上一次有效结果。
  草稿在 Enter 时提交，避免输入一半条件就清空列表；中文组合输入期间 Enter 不提交。
  清空按钮清除条件并恢复全部结果。URL 保存已提交查询，刷新与前进/后退恢复查询与筛选。
- **Proposed suggestions:** 聚焦显示字段和简短语法示例，输入 `repo:` / `is:` / `status:`
  时显示匹配值；点选或方向键 + Enter 补入条件，后续 Enter 提交完整查询，Escape 关闭。
  建议面板与搜索框同宽，统一边框、圆角、阴影、选中/悬停和键盘焦点，取消两个原生 select；
  最多展示 8 条建议并提供下一组入口，不产生无限增长列表。沿用现有依赖，不引入搜索服务。
- **Layout:** 桌面框宽上限沿用正文容器的 890px（窄屏按可用空间收缩）；手机在路径/状态
  下方独占一行，建议面板不超出视口。保留已固定的详情头部、独立内容滚动和侧栏交互。
- **Implementation / validation plan:** 提取查询解析与建议组件，接入现有 fixture 过滤，
  用公开查询行为测试覆盖组合条件、无效输入和关键词；浏览器检查候选选择、真实 IME、
  Enter/Escape/方向键、清空、URL 恢复、筛选不丢详情阅读状态，并检查桌面/手机打开面板截图。
  运行格式、类型、组件测试与生产构建；仍只做前端，不增加后端能力。
- **Status:** 已记录用户要求与上述具体方案，等待确认；尚未修改产品代码、依赖或测试。

### [018] — 2026-10-16 — 落实剩余前端设计：字号、有界窗口与统一查询

- **Requirement / scope:** 落实 [014] 与 [017] 中此前未实施的前端设计。沿用 React 前端，
  保留 [015] 的图标按钮、[016] 的圆形头像和固定详情头部；仍只使用示例数据。
  真实接口、GitHub 全量聚合、鉴权和生产托管继续归 [#85](https://github.com/b1indsight/agentflow/issues/85)。
- **Typography / implementation:** `styles.css` 集中定义相对字号，正文和卡片标题 16px、侧栏 15px、
  控件/代码 14px、辅助信息 12–13px、详情标题桌面 28px / 手机 24px。
  调整控件、换行与移动布局；修正下一步条件的嵌套文本误继承辅助字号的问题。
- **Search / implementation:** `search.ts` 与 `SearchBox.tsx` 合并原仓库、关键词与生命周期控件，
  支持 [017] 中全部条件、AND 关键词及引号短语。Enter 提交草稿，无效条件显示红框与错误信息，
  保留上一有效结果；初始无效 URL 不放宽查询。提供同宽统一建议、最多 8 项/组、方向键、
  点选、Escape、清空及真实 IME 安全提交；URL 与前进/后退恢复已提交查询。
  `App.tsx` 保留正在看的详情、滚动位置和展开工具，不因筛选重建会话。
- **Windowing / decision:** 增加 `@tanstack/react-virtual`，由 `windowing.tsx` 管理共享滚动根、
  动态高度测量和业务硬上限。看板每列、导航每组、执行记录、关键进展、会话、正文块和 Review 回复
  每个列表最多挂载 60 条，焦点及待聚焦项计入预算；键盘 Tab 可进入尚未挂载的下一逻辑项。
  小列表保持普通文档流，同一组件跨越 60 条门槛；缓存此前测量，避免切换时展开状态丢失或阅读跳动。
  小列表禁用虚拟器，避免嵌套列表在会话追加时重新挂载并重置共享根的滚动位置。
- **Text / state:** 正文每段最多 2048 字符；代码每段最多 8192 字符且不超过 200 行，
  标题和元信息使用有界展示。提供前后段与字符位置，不静默丢弃正文，不拆开 UTF-16 代理对。
  折叠及当前阅读段独立于 DOM 保存，离开窗口后返回仍保留；长代码只在自身区域水平滚动。
- **Conversation cache / boundary:** `useConversation.ts` 使用稀疏索引保存已加载逻辑长度，
  每 Flow 活动缓存最多 240 条、最多保存 4 个 Flow。淘汰后向回滚动按示例 offset 重读，
  反向补读不改变末尾游标或新增分界线；错误重试原请求，保留请求取消和 Flow 隔离。
  DOM、活动缓存上限与总内存上限是不同约束：完整 fixture、测量及阅读状态元数据仍在内存，
  不宣称总内存固定，也不将示例 offset 定为真实后端契约。
- **Validation:** `npm test`（2 个公开行为/安全合同测试）、`npm run build`（含严格类型检查）、
  `npm run format:check` 通过。Chrome 通过 39 项查询/字号/交互检查，含真实 IME、
  URL 历史、固定头部、分页、刷新/焦点、StrictMode 快速切 Flow 与 200% 等效视口。
  大数据检查通过 39 项：七类万条列表可往返首尾，每列表挂载不超过 60，初始实际为 8–27 条；
  1000 条真实 hook 历史缓存最高 240，淘汰后可回读并继续尾部刷新。
  手机万条样例与代码分段通过 20 项；下载单文件 HTML 通过 8 项 file URL 检查，
  无外部资源请求和捕获的 JavaScript 异常。已逐项检查最终桌面/手机/建议/错误/长代码截图。
  证据位于 `.amp/in/artifacts/unified-console-checks.json`、`window-stress-checks.json`、
  `window-mobile-checks.json`、`unified-snapshot-checks.json`。
- **Ablation:** 用同一组件和同一输入，仅在临时 Vite transform 分别移除机制，14 项检查通过：
  移除 DOM 窗口，正文块挂载由 27 变为 10000；移除文本窗口，代码字符由 8192 变为 50003；
  移除独立阅读状态，返回首条后折叠和阅读段丢失；移除活动缓存上限，1000 条历史全部驻留，
  对照正式实现为 240。保留这四项机制。另验证 60 → 64 条时展开行与阅读锚点保持不变。
  可复现命令：`.venv/bin/python .amp/in/check-window-ablation.py`，
  记录见 `.amp/in/artifacts/window-ablation-checks.json`；正式源码未参与破坏性实验。
- **Delivery / limits:** 本地实现和文档已更新，下载快照已重新生成；未提交、推送、创建 PR 或部署。
  前端 CI 仅本地执行其对应命令，未在 GitHub 运行。此次未修改后端，未运行完整后端 pytest、
  Docker 或真实 GitHub/Access 集成验收。

### [019] — 2026-10-16 — 详情状态标签透明背景

- **Requirement / implementation:** 用户要求标题旁“第 2 轮修订”状态标签背景透明。
  仅将 `.detail-activity` 底色改为透明，保留所有状态的圆点、文字、间距及手机布局。
- **Validation:** 格式检查、2 项测试、类型检查及生产构建通过；Chrome 桌面/手机的执行、
  排队、受阻、完成四类状态共 10 项检查通过，并检查两种尺寸截图。
  更新下载 HTML 后通过 8 项 file URL 检查；证据见 `.amp/in/artifacts/transparent-status-checks.json`。
- **Backend scope:** 只读核对 #85 和当前 API；现有 `/api/flows/{flow_id}/conversation`
  可复用，但仍缺控制台列表、仓库候选、详情、执行记录及 GitHub/Agent 聚合查询。
  后续需补充统一查询语法、历史回读及嵌套内容有界续读的具体契约，本次未修改 issue 或后端。
- **Delivery:** 仅本地修改，未提交、推送或部署。

### [020] — 2026-10-16 — 看板按窗口定高、各列独立滚动

- **Requirement / implementation:** 用户接受桌面各列独立滚动、列标题固定，并要求按当前窗口计算
  看板纵向长度和最大列高度。`styles.css` 用现有 `100dvh` 容器与 Flex/Grid 剩余空间分配，
  看板区域扣除顶栏，列区域再扣除上下边距、手机阶段选择及页脚，卡片区再扣除列标题与列内边距。
  移除旧 540px / 400px 最小高度，不新增 JS 测高状态或固定窗口高度常量；窗口调整时自动重排。
- **Scroll ownership:** `views.tsx` 在每列标题下增加独立、带辅助标签和键盘入口的滚动根，
  标题不进入滚动区；虚拟列表就近使用该根，保留每列表 60 条硬上限。手机只显示当前阶段，
  未选列保留尺寸但通过 visibility 隐藏，避免虚拟列表在隐藏/复显时测量失效。
- **Reading state:** `App.tsx` 从单一看板偏移改为分阶段保存位置，返回时待虚拟列表测量后
  恢复五列位置，再恢复原卡片焦点。检查发现原生 popstate/hashchange 的重复通知会取消延迟恢复，
  因此布局 effect 仅依赖真实 Flow 身份和页签，不依赖重复生成的路由对象。
- **Validation:** 格式、2 项组件/查询测试、严格类型及生产构建通过。Chrome 通过 25 项专项检查：
  真实滚轮仅滚动所在列、标题/顶栏固定、10000 条样例分五列均可到尾部、DOM 上限、
  详情返回位置/焦点、手机阶段往返、空结果，以及 7 种尺寸（含 720×500 的缩放等效视口）。
  例如 1440×900 窗口中看板区域 832px、列高 747px、卡片滚动区 684px；
  1440×650 中对应 582px、497px、434px，随可用高度变化，不由任务数量撑开。
  已检查桌面/手机默认和长列表截图；原交互 39 项、万条压力 39 项、手机压力 20 项、
  更新的下载 HTML 8 项回归通过，无捕获的 JavaScript 异常。
- **Ablation / decision:** 对同一手机尾部阅读场景临时用 display:none 替换隐藏列方案，
  隐藏后返回虽保留 scrollTop，却不再挂载末尾卡片；正式版末尾卡片仍存在，6 项对照检查通过。
  因此保留可测量的隐藏列，未再增加逐阶段测量缓存或补偿 effect。
  证据为 `.amp/in/artifacts/board-columns-checks.json`、`board-column-ablation-checks.json`、
  `board-columns-build.log`；复现命令为 `.venv/bin/python .amp/in/check-board-columns.py`
  和 `.venv/bin/python .amp/in/check-board-column-ablation.py`。
- **Delivery:** 仅本地前端与文档修改，更新单文件预览；未提交、推送、创建 PR 或部署，未改后端。

### [021] — 2026-10-16 — 按结构审查顺序修复焦点、类型与回归覆盖

- **Trigger / implementation:** 用户要求依次处理本轮审查的四项问题。先修复手机导航折叠最后分组后
  Tab / Shift+Tab 无法围绕可见控件回绕的问题；`focus.ts` 统一侧栏与虚拟列表的可聚焦控件选择，
  使用 `checkVisibility` 排除仍有布局矩形的隐藏 details 后代，并排除禁用控件。
  其次将关键进展从 `string[][]` 改为必需 `title` / `description` 的对象数组，同步 fixture 与概览，
  不保留旧数组兼容或缺字段的运行时空字符串回退。最后将状态顺序和名称移至只读的 `phases.ts`，
  搜索、建议、看板与导航不再因状态目录依赖示例数据模块。
- **Durable regression / boundary:** 增加开发依赖 `@playwright/test` 与四条正式浏览器行为回归，
  覆盖折叠分组后的手机焦点循环、千条列表卸载/复挂后的展开与文本段及跨窗口 Tab 顺序、
  返回看板恢复五列位置与卡片焦点、300 条会话超过 240 条缓存后回读并保留展开及尾部刷新。
  大数据入口与快速活动 I/O 仅在 `e2e/` 及专用 Vite 配置中使用，复用正式组件与 hook，
  不在产品代码增加测试参数、mock 回退或数据源框架；CI 安装 Chromium 并执行 `npm run test:e2e`。
  测试输出从版本控制、格式检查、Python 分发和 Docker context 排除。
- **Validation / comparison:** 焦点回归先在未修复代码上失败，修复后通过；类型探针确认原有缺字段记录
  可通过编译并在渲染时崩溃，新类型拒绝缺少说明或使用旧数组形状的记录，完整对象通过编译。
  `npm ci`、格式检查、2 条单元测试、严格类型与生产构建通过；4 条 Playwright 各重复 3 次，
  双 worker 共 12/12 通过。原有看板 25、交互 39、万条列表 39、手机压力 20、离线预览 8 项
  共 131 项检查通过，检查概览与手机看板截图确认展示内容和布局未变化。
  Python sdist / wheel 构建通过，核查归档无 node_modules、dist、test-results，生产 JS 无测试样例标记。
  本次生产逻辑只统一已有焦点规则、收紧类型并移动常量，未新增需独立消融的运行时机制。
  证据为 `.amp/in/artifacts/frontend-review-fixes-checks.log`、`frontend-review-type-fixed.log`、
  `frontend-review-fixes-browser.log`、`frontend-review-packaging.log`。
- **Remaining observation:** 新 Chromium 的压力测试会打印 `ResizeObserver loop completed with undelivered notifications`
  调度告警；四条回归的行为断言均通过。本轮未忽略错误或改变测量调度来隐藏告警，未断言此问题已修复。
  GitHub CI 尚未实际运行，未进行 Docker 或真实后端 / GitHub / Access 集成验收。
- **Delivery:** 更新前端说明、changelog 与单文件预览；仅本地修改，未提交、推送、创建 PR 或部署。

### [022] — 2026-10-17 — 修复虚拟列表 ResizeObserver 循环告警

- **Trigger / diagnosis:** 用户要求修复 [021] 保留的告警。对同一千条变高列表执行展开、
  文本翻段、滚到尾部再返回，观察到虚拟列表在尺寸回调期间强制提交 React 更新，
  改变被观察祖先的布局并触发浏览器未投递通知错误。仅监听 Playwright pageerror 会遗漏该错误，
  因此浏览器回归同时收集 window error 事件；补强断言后，原实现的阅读状态回归失败。
- **Decision / implementation:** `WindowList` 使用 React 虚拟列表适配器提供的 `useFlushSync: false`，
  让 React 正常批处理范围更新，不在尺寸投递期间强制同步提交。未屏蔽错误，未更换观察器，
  未统一延迟尺寸测量至下一动画帧；每窗口 60 条、阅读恢复与键盘行为保持原约束。
  四条正式回归均检查两类浏览器错误，结束前等待两次动画帧以覆盖最终布局的通知，
  阅读状态场景增加桌面到手机的视口变化。
- **Comparison / validation:** 同一操作序列中，原行为、只观察父级、稳定测量 ref 各出现 2 次告警，
  正式修复为 0；再次恢复原行为仍为 2，再启用修复为 0。格式、2 条单元测试、严格类型及
  生产构建通过；四条 Playwright 各重复 5 次，双 worker 共 20/20 通过且两类浏览器错误为空。
  原有看板 25、交互 39、万条列表 39、手机压力 20、重新生成的离线预览 8 项，共 131 项通过。
  证据为 `.amp/in/artifacts/resize-observer-before.log`、`resize-observer-ablation.log`、
  `resize-observer-after.log`、`resize-observer-browser-checks.log`。
- **Delivery / limits:** 更新前端说明、changelog 和单文件预览，仅本地修改，未提交、推送、创建 PR 或部署。
  未改后端，GitHub CI 与真实后端 / GitHub / Access 集成仍未验收。

### [023] — 2026-10-17 — #85 后端接入方案（提案，待批准）

#### 背景、目标与边界

用户要求在新分支和新 PR 实现 [#85](https://github.com/b1indsight/agentflow/issues/85)。
前端已经通过 [#86](https://github.com/b1indsight/agentflow/pull/86) 合并，
本轮以包含该 PR 的 `main` 为基础；继续维护本功能记录，后端使用新的
`feat/web-console-backend` 分支及一个草稿 PR，批准后在同一 PR 实现。

本提案取代“本轮只做前端”的交付边界，保留既有 React 布局、只读定位、owner 认证及 Worker 协议。
不增加创建 Flow、评论、审核、拖卡、注入消息、cancel/retry/drain 或合并入口。
读取不触发 GitHub mutation，不改调度、结果应用、审核或 Projects 同步业务语义。
所有下述接口、限额和完整性声明均为拟实施契约，尚未实现或通过验收。

#### 已核实的复用点与缺口

- `app/controler/api.py` 已有 owner dependency、API no-store、会话请求断连清理；
  页面及其他控制台 API 尚未挂接。认证复用 `app/controler/access.py`，缺配置拒绝服务。
- `app/persistence/projects.py::ProjectStore.refresh()` 内联了阶段 SQL，包含终态、
  closed/失败 effects、archive 恢复、merged、无 PR/无执行者及 pending job/result 的优先级。
  控制台不能另写一份近似规则，也不能依赖 Projects 是否配置。
- `flows.py::list_flows()` 全量返回；控制台需要独立批量查询，不改变内部调用方的全量语义。
  `flow_deliveries` 已有可空 title/PR；Issue 标题未作为独立事实持久化，搜索先限定本地已知标题。
- `ConversationService.read()` 每页只读一个 session，按 attempt ID 向前推进，游标绑定 session 集合及版本；
  EOF 保留最后 session 游标。它不提供跨 session 的事件时间排序，也不提供旧 session 的刷新索引。
  复用现有 Worker `HistoryQuery`、文件身份校验和单页预算，不能将 EOF 解释为整个 Flow 结束。
- 现有 GitHub 评论、Review 和线程读取主要服务审核，部分展示字段缺失；
  timeline、PR 提交及 checks 等需要补充专用只读查询。不能直接对外序列化审核对象或内部 payload。
- Docker 目前只有 Python build stage，wheel 构建排除前端 `dist`；
  Tunnel 管理路径目前仅允许 conversation。需要落实静态构建产物分发及精确路径白名单。
- [#49](https://github.com/b1indsight/agentflow/issues/49) 的 CLI 分页和
  [#47](https://github.com/b1indsight/agentflow/issues/47) 的全局异步数据库治理继续独立跟踪；
  本轮仅保证新增查询无逐行往返且不阻塞事件循环，不宣称这两个 issue 已解决。

#### 提议的接口与本地查询合同

| 接口 | 内容与分页 |
| --- | --- |
| `GET /api/flows` | `q`、可选列阶段、cursor、limit；默认 50、上限 100，返回卡片摘要、过滤全集阶段计数和计数观察时间。五列可独立续读。 |
| `GET /api/repos` | 从全部本地 Flow 聚合完整 `owner/name`，支持搜索及字典序游标，不依赖当前任务页，不访问 GitHub。 |
| `GET /api/flows/{id}` | 详情及有界关键进展、当前动作/原因/下一步、交付、当前或最后 Worker、安全诊断；不受列表筛选影响。 |
| `GET /api/flows/{id}/jobs` | 默认 50、上限 100，按 job ID 稳定分页，保留 job/attempt 身份、类型、状态及时间，不只取最近几项。 |
| `GET /api/flows/{id}/activity` | 下述多来源活动、覆盖信息、各源读取与刷新续读位置。保留原 conversation 接口不变。 |
| `GET /api/flows/{id}/activity/threads/{thread_id}` | 线程内回复独立分页，验证 thread 归属本 Flow 的 PR，限制单次条数和字节。 |

- 服务端拥有查询语法和阶段真值；普通词/引号短语、`repo:`、`is:`、`status:`、`issue:`、`pr:`
  与现有 UI 交互一致，增加明确的 Flow 编号搜索。未知、重复或无效条件返回安全的 422，不能放宽查询。
  repo 必须使用完整身份，不再移除特定 owner 前缀；不因候选页尚未加载而拒绝有效的完整仓库名。
- SQL 先筛选再分页；每页固定数量的批量语句，不逐卡查询或调用 GitHub。
  阶段判定在 persistence 内最小提取共用 SQL，由控制台和 Projects 同时引用并对照测试。
  当前动作只依据 job、交付、结果应用及恢复事实，无法推导时明确 unknown；终态显示历史 Worker。
- 列表采用 Flow ID 倒序 keyset，游标绑定规范化过滤、列、版本和首段最高 ID；
  新 Flow 由首段刷新发现。它不是跨请求 MVCC 快照：并发状态变化可改变过滤成员和计数，
  返回观察时间，前端按 ID 合并；首段刷新后清空该查询旧分页，不混用旧游标。
  jobs 使用同类有界 keyset。错误/跨 Flow/跨查询游标返回可恢复的 409，不静默重置为首段。
- DTO 显式列白名单；排队无分支/Worker 时返回 null，终态与待恢复分开。
  URL 从已验证的 GitHub 仓库和编号构造或校验，不透传任意协议；错误只暴露代码。
  不输出 assignment token、App/Worker/push token、内部路径、job/result/effect 原始 payload。
- 同步查询以完整数据库工作单元移入受控线程，使用数据库/连接池超时限制实际占用；
  取消 await 不等于已停止线程，不能提前释放并发许可后让后台 SQL 无限累积。
  不在数据库事务中 await GitHub 或 Worker。仅在查询计划证明需要时增加局部索引。

#### GitHub 覆盖、聚合与刷新合同

1. **先核对覆盖表再编写读取器。** 对 Issue/PR 描述、普通评论、Review、线程和回复、
   PR 提交、checks/statuses、生命周期、标签、指派、审核请求、reactions 等逐类核验当前 API、
   App 权限、来源身份和分页；区分“支持”“无权限”“远端不可用”“不可恢复历史”，
   不能将尚未实现的类型列成已支持。删除正文、过期日志和 API 不提供的历史无法还原。
2. **复用认证和错误分类，新增展示读取。** 扩展 `app/github/` 对应概念的只读接口，
   补足作者、发生/更新时间、原始链接、文件/行号、代码片段、resolved/outdated 等字段。
   以来源命名空间和远端稳定 ID 标识；timeline 中已有对应实体的事件只引用该身份，
   不重复产生第二条评论。Review、thread、reply 保留父子关系。
3. **明确排序限制，不承诺全局时间有序。** 首版提议保证每个来源、session 及线程的原生向前顺序，
   跨来源通过确定性的有界轮转取得记录，携带发生时间和来源位置，标明 `source_ordered`。
   页面沿已读窗口稳定展示，较早时间的补录明确标识；不把两页局部排序说成全局时间排序。
   现有 GitHub 读取与 Worker 契约没有建立可验证的跨来源单调时间下界，若要求严格跨来源时间序，
   需要预读完整历史或新增索引/投影，不能在有界首屏中无条件保证；这是本方案待批准的产品取舍。
4. **双向阅读不复用 mock offset。** 每个来源/session 保留明确游标，聚合游标绑定 Flow、
   契约版本及读取方向；页面按已读窗口记录续读锚点，淘汰后从对应源锚点回读。
   旧 Worker session 复查使用现有单 session query，不修改协议或复用仅能前进的最后 session 游标。
   单页不内嵌无限增长的 session/线程游标数组，线程回复独立读取。
5. **刷新有可恢复核对过程。** 新增、编辑/状态更新、早期补录和无变化分别表达。
   有更新过滤的来源利用其原生边界；缺少可靠变更索引的线程、reactions 等需要从已知起点分段复核，
   输出覆盖范围和 refresh continuation。预算用完或某源失败时显示“核对未完成”，
   不能显示“无新增/全部读完”；完成所声明范围的检查后才显示对应范围无变化。
   对已被缓存淘汰的旧对象也可回读并按稳定 ID 更新，不只查询最后一页。
6. **来源独立失败。** GitHub 和 Worker 以及 GitHub 子类型均返回 available/partial/unavailable、
   安全原因、最后成功观察时间/位置和续读信息。无 session 不等于无 GitHub 历史；
   一个来源失败不丢弃其他来源的结果或前端旧数据。空页有 continuation 时继续，
   `has_more=false` 仅表示当前读取位置，不代表流程结束或未来没有新增。
7. **资源预算。** 初拟活动默认 50、上限 100 条、响应最多 512 KiB；每个 HTTP 请求最多
   8 次远端读取、总 deadline 12 秒，GitHub 并发最多 2、Worker 保留既有并发/10 秒超时约束。
   实施时按完整响应（含嵌套回复和元数据）计费，GraphQL 外层/内层都限制，超额保留未消费位置；
   解析前约束远端响应大小，不能先下载无限数据再裁剪。尊重 Retry-After，浏览器请求内不无限重试。
   大正文/工具输出有明确分段或来源截断信息；原 Worker 已截断内容不能假装可恢复全文。
   这些是总预算，不能对每个来源分别重置预算后宣称请求整体有界。
8. **保留策略。** 首版不新增永久活动库，不把 webhook inbox 当历史库，不持久化完整 Worker 日志。
   优先使用可校验的来源游标和客户端有界阅读窗口；如实际游标/变更检查需要服务端状态，
   在实现前补充容量、TTL、失效恢复和退出清理方案并说明取舍，不隐式加入无限缓存。
   若最终需要 schema 或持久投影，应先在本记录提案，明确迁移、保留和淘汰后再实施。

#### 模块、鉴权、打包与前端接入

- `app/contracts/console.py`：对外只读 DTO、分页/错误和覆盖状态，不暴露持久模型。
  `app/persistence/console.py`：批量读取；共用阶段 SQL 归 persistence。
  `app/controler/console/`：路由、读取编排和来源聚合，按实际职责拆分，避免继续扩大 `api.py`。
  不在现有 React 源码目录里混入后端实现，不为单一实现新建 Protocol/通用数据源框架。
- `ServerRuntime` 显式装配依赖并负责相关读取资源关闭；复用 GitHubApp、数据库和 conversation broker。
  断开 HTTP、切页或超时取消等待中的 GitHub/Worker 请求并回收控制端 pending；
  无法中断的线程/Worker 文件读仍受自身预算约束，不能宣称取消 HTTP 即中止远端文件 I/O。
- `/console`、固定静态资源前缀及全部新增 API 均先执行现有 owner 验签，
  StaticFiles mount 不能因未执行 dependency 绕过保护。无配置 503，无效身份 401，其他 owner 403。
  鉴权必须先于文件内容或 304 返回；API no-store，静态缓存不允许跨身份共享敏感响应。
- 使用同源生产构建，CSP 将 `connect-src 'none'` 审查调整为 `'self'`，不开放 CDN、任意域或脚本执行。
  浏览器依靠 Access 传递身份，不接触后端凭据。后端只提供结构化文本，不输出可执行 HTML。
- Node 仅在构建期使用。增加明确静态产物目录（如 `app/controler/console_static/`），
  由锁定 npm 构建生成，wheel/sdist 含该产物，运行时不需要 Node；保留排除 node_modules/test-results。
  发布构建必须先生成资源，再由 sdist 重建 wheel 验证资源仍在；未构建源码环境明确提示资源缺失，
  不悄悄转为 mock。Docker 增加锁定 Node 构建阶段，将同一产物接入 Python 安装过程。
- 原生/Docker/K8s Tunnel 模板仅增加控制台、静态资源和明确的用户 API 路径，
  仍拒绝 `/docs`、openapi、Worker、metrics 和探针；只修改模板，不代为配置线上 Access/Tunnel。
- React 改用实际接口，删除生产入口的 fixture/隐式回退；测试样例仅留在测试入口。
  保留现有布局和 DOM/缓存上限，适配完整仓库身份、列级续读/总数、真实 DTO、来源完整性、编辑合并、
  反向回读和可恢复错误。取消旧请求并检查响应归属；正式验证延迟乱序、失败重试和 Worker 离线。
  PR #86 的三项 low finding 不自动视为已修复；若接入修改了相同导航/读取路径，记录实际覆盖和变化。

#### 备选方案、成本与风险

- **继续只提供前端样例**成本最低，但不能满足 #85；本轮选择真实读取接入。
- **不持久化、按来源向前读取（提议）**保留现有协议与运维边界，首屏有界；
  代价是跨源不保证严格事件时间序，旧状态完整复核可能需要多次续读。
- **永久聚合事件库/索引**可支持更强排序与增量刷新，但引入 schema、同步、修正、保留和删除隐私成本；
  首版不默认采用。不能为了更好看的时间线把全量抓取藏在一次 HTTP 请求里。
- 查询 keyset 只保证稳定键顺序，不冻结可变事实；明确弱快照语义和首段刷新避免给出错误完整性承诺。
  GitHub 权限/限流、Worker 离线及源历史截断属于预期状态，必须展示范围而非吞掉错误。

#### 实施顺序与验收

1. 批准方案后先确定 DTO、GitHub 覆盖表、查询语法、游标/刷新/排序契约和预算；更新对应架构文档。
2. 实现控制台批量查询、共用阶段判定和详情/jobs；通过真实 PostgreSQL 的过滤分页、
   全集计数、优先级、查询次数与敏感字段排除测试，再接 HTTP owner 保护。
3. 逐来源补足 GitHub 读取，接入 Worker 历史；实现有界聚合、去重、编辑/状态复核、
   多级续读、部分失败、限流和取消。不把不可恢复或未核对范围标成完整。
4. 接入 React 真实请求并验证阅读/焦点、列分页、下滑、末尾刷新及各错误状态；
   加入可控延迟/失败/乱序的公开合同回归，测试样例不进入生产包。
5. 增加受保护静态托管、Node 构建、wheel/sdist/serve 镜像和 Tunnel 白名单；
   检查安装后的产物和直接深链接，不只在 Vite 开发模式验证。
6. 执行格式/前端类型/单元和浏览器检查、Ruff、Mypy、完整 pytest、分发构建、部署模板检查；
   做完下述对照后经过代码审查门禁，在同一分支/PR 推送实现，不自动合并。

**对照/故障验证（实施后执行，当前未运行）：**

- 对同一批真实 PostgreSQL 数据移除过滤前置/共用阶段，验证跨页结果、全集计数和
  merged + pending-result 等组合的断言会失败；扩大页长时应用 SQL 往返数量保持固定。
- 对受锁 SQL 比较移出完整工作单元前后事件循环响应性，确认健康读取不被新增查询阻塞；
  取消后实际数据库工作仍受语句超时/并发边界约束，不以取消 await 作为证明。
- 对多页、嵌套回复、同 ID 多路径、旧评论编辑、线程状态、checks/reactions 变化及
  有 continuation 的空页移除预算/规范身份/复核步骤，验证对应完整性或更新断言失效。
  Worker 跨 session、EOF 后追加和旧 session 核对单独验证，不用 mock offset 推导协议能力。
- 安装 wheel 与 serve 镜像分别请求页面、资源、API、非法路径和未认证路径；
  移除资源鉴权后拒绝断言应失败。浏览器走真实 HTTP DTO，GitHub/Worker I/O 仅在受控验证中替换。
- 真实 GitHub 私有读取和 Cloudflare Access 登录/MFA 验收单独报告，需要环境时明确阻塞；
  不执行会影响他人仓库的写入，不用替身或单元测试替代真实登录成功的结论。

#### 待批准事项与实现中核验项

- **待批准的主要取舍：** 首版采用显式 `source_ordered`、可标识补录的混合阅读，
  保证来源内顺序并披露跨源限制，不默认新增永久聚合库；刷新可能需要多段核对。
  若要求所有来源严格按事件时间全局排序，应先调整该设计及索引/保留成本，再开始实现。
- **实现中核验：** GitHub 各类 API 的准确字段、权限、排序和更新过滤，SDK 的流式响应字节上限，
  统计查询执行计划、静态产物发布入口、预算具体值。核验发现改变上述合同的限制时追加提案，
  不静默缩减 #85 覆盖或宣称完成。
- 本轮仅交付方案文档；未写实现和测试，未运行后端接入验收，#85 保持未完成。

### [024] — 2026-10-17 — 选择 C：本地活动索引/投影，继续讨论合同

- **Requirement / decision:** 用户明确选择方案 C，并要求继续讨论细节。采用本地持久活动索引/投影，
  由受控同步读取 GitHub/Worker，再向控制台提供统一查询；不再采用 [023] 的不持久化、
  跨来源轮转 `source_ordered` 作为最终混合会话展示合同。
- **Superseded scope:** [023] 中“不新增永久活动库”的保留策略、跨来源读取顺序展示方案及相关
  前端续读设计需要据此修订；只读、owner 验签、资源预算、来源完整性、Worker 原协议、
  有界分页及真实环境验收等约束继续适用。本地投影不能充当工作流状态真值或触发 GitHub 写操作。
- **Guarantee boundary:** 本地可按已同步版本的事件时间及稳定身份建立统一顺序和分页；
  这不证明远端历史已经全部同步，也不能保证未来不会发现更早事件。
  首次回填、增量同步、旧对象核对和来源不可用仍需独立呈现范围及进度。
- **Pending details:** 保存规范正文还是只存索引/摘要、正文与工具输出的容量及保留策略、
  编辑历史和删除语义、同步触发与公平调度、快照/变更游标与补录、迁移/恢复及清理成本仍待讨论。
  “选 C”不等于批准永久保存所有 Worker 原始日志，也不自动确定上述默认值。
- **Implementation / validation:** 仅记录已选架构方向；未写 schema、实现或测试，
  未执行后端接入验收，整体方案仍处讨论阶段。

### [025] — 2026-10-17 — 确定按 Flow 保存时间线及分层内容

- **Confirmed requirements:** 用户要求以 `flow_id` 为关联键保存 Flow 时间线。
  GitHub 交互和 Flow 变更保存完整内容；Worker 输出保存展示正文；其他过长的工具输出、
  代码 diff 等保存必要信息、摘要、跳转链接和查询方式。
  用户另提出 Flow 查看状态及最新状态可独立保存或从时间线计算，具体方案继续讨论。
- **Content contract:** 时间线条目保留来源、稳定身份、发生/观察/更新时间及对象关系。
  完整 GitHub/Flow 内容指已观察的交互正文和业务变更内容，不直接持久化或对外暴露
  内部凭据及原始 job/result/effect payload。无法获取的源历史和既有源截断继续明确标记，
  不把摘要伪装为原文。完整内容可分段存取，不因单个 HTTP 响应预算静默截断。
- **Large artifact proposal:** 长工具输出和 diff 保留类型、必要上下文、可得的大小/行数/文件统计、
  摘要及定位信息；查询定位绑定 Flow/来源身份和适用的 session/commit/记录位置，
  不把任意内部路径、命令或凭据作为浏览器可执行入口。
  查询时重新检查 owner 与 Flow 归属；区分定位内容的准确版本、原文已变化/清理和来源不可用。
  元数据与摘要的具体格式、阈值及保留期限仍待定，不默认增加 LLM 摘要调用。
- **View/latest-state proposal (not yet approved):** 将“查看状态”暂理解为 owner 的已读状态及阅读位置，
  与业务状态分开存储；如用户指的是用于看板展示的派生状态，应归当前状态投影，不混用概念。
  阅读进度应参考本地变更版本/续读范围与稳定条目锚点，不能仅用事件发生时间或最大自增 ID
  代表已读，避免补录、旧条目编辑及并发提交漏报。读取页面本身不等于整条时间线已读。
  个人阅读状态写入只修改 UI 元信息，不新增工作流/GitHub 业务写入口。
- **Latest-state boundary proposal:** 当前业务状态继续以已有 Flow/job/交付等持久事实为准，
  实时通过共用阶段查询读取，确有需要再维护可重建的看板摘要；不每次打开页面回放整条时间线，
  也不把异步 GitHub 补录或最后一条活动当作最新 Flow 状态。
  新增本地业务变更记录应和对应事实在同一事务落地；历史不能还原的变更只能标为迁移基线，
  不伪造完整旧历史。具体事务边界与摘要维护策略在实现方案中核实。
- **Status:** 已确认内容分类和 Flow 归属；查看状态含义、状态存储方案及其他细节仍在讨论。
  本轮仅更新文档，未写实现、迁移或测试，未执行后端接入验收。

### [026] — 2026-10-17 — 个人阅读状态独立跟踪，本轮聚焦 #85

- **Clarification / scope:** 用户确认“查看状态”指个人阅读状态，随后要求先记录 issue、后续再实现，
  当前专注补全控制台后端能力。已创建 [#88](https://github.com/b1indsight/agentflow/issues/88)，
  跟踪个人已读/未读、阅读位置和跨设备同步；前述具体已读判定建议不视为全部获批。
- **Deferred:** 本 PR 不新增阅读状态表、个人阅读状态写 API、未读提醒或跨设备阅读进度合并。
  个人阅读状态不作为 #85 后端交付的前置条件。
  现有页面交互所需的临时滚动/焦点/折叠恢复继续保留，不扩展为个人已读持久化。
- **Still in scope:** 按 `flow_id` 保存分层时间线、GitHub 与 Worker 同步、完整性和来源状态、
  当前业务状态与阶段查询、列表/详情/jobs/activity API、真实前端接入及受保护静态托管。
  内容保存范围继续按 [025]，持久投影方向继续按 [024]；不把业务状态或同步状态误删出范围。
- **Status / validation:** 仅更新功能记录和 PR 范围，保留方案讨论状态，未写实现或测试。
  #88 为后续需求，未实现、未关闭；#85 和本 PR 的后端能力仍待实现及验收。

### [027] — 2026-10-17 — 确定永久版本、响应式同步、阅读快照与当前状态表

- **Decision / status:** 用户明确决定下列六项产品策略，取代 [023]–[026] 中相应候选方案；
  决策已确定，数据库结构、同步与迁移实现仍未完成，不能将文档更新当作能力交付。

#### 1. 永久保存

按 [025] 确定的分层内容永久保存，Flow 终态不触发自动过期或正文清理。
这包括完整 GitHub/Flow 业务内容、Worker 展示正文，以及长工具输出/diff 的必要信息、
摘要和定位；不扩大为永久保存全部 Worker 原始日志或本轮原本不归档的大型原文。
请求/响应/渲染仍有界，长期保存意味着数据库总量可以增长，不宣称总存储固定。

#### 2. 保存每个采集版本，删除只追加标记

保存实际观察到的各个内容/状态版本，编辑追加版本，历史版本不覆盖；
远端删除只追加删除标记，不清除已采集正文或旧版本。
相同对象的相同版本重复投递/重复读取需幂等，不因一次重试复制同一版本。
删除必须有足够证据，权限变化、远端错误或孤立 404 不能当作确认删除。
首次采集前未观察到的编辑不能还原；远端删除也不会从本地永久档案中擦除正文，
归档内容继续受 owner 鉴权保护。此项取代此前“删除后清除正文”的候选建议。

#### 3. Flow → Issue 单向关联及此前交互

由 `flow_id` 指向明确的仓库/Issue 身份，不把 Issue 号当作唯一 Flow 身份，
不新增独立的反向映射真值；事件路由所需的索引/查询不改变该关联方向。
时间线仍按 `flow_id` 归属；关联 Issue/PR 中早于本轮 Flow 创建的交互标为“此前交互”，
不能将采集时间冒充发生时间，也不能伪装为本轮执行事件。
Worker session 与本地业务变更保持真实 Flow 归属，不因共享 Issue 混入其他轮次执行记录。

#### 4. 响应式触发，活跃 Flow 打开详情/刷新时后台同步

不新增对所有 Flow 高频扫描的控制台同步循环。优先响应 webhook、已提交的本地业务变更和
Worker 相关连接/执行事件；活跃 Flow 打开详情或点击刷新时，另外触发该 Flow 的后台同步。
合并重复触发，按来源保存持久检查点、失败重试和限流退避；事件消费与网络读取解耦，
重试/启动恢复只处理待完成工作，不借此引入全量定时抓取。
页面先读取已有本地档案，明确展示同步中、最后成功位置和来源不完整，不等待全量回填结束。

**已核实的现状边界：** `ServerRuntime.receive_webhook()` 持久接收后唤醒 consumer，
Worker 连接处理心跳、工具请求和执行结果；但现有 `ConversationQuery/Result` 是主动查询响应，
Worker 不会自动推送每条会话正文。正文同步需要在触发后复用该有界历史查询。
心跳本身不作为每次全量抓取正文的信号，不修改 Worker 协议以虚构实时输出流。
当前服务还包含租约、结果、审核及远端 reconciliation 的周期补偿；保留这些既有正确性机制，
不将“响应式优先”误写为当前没有任何周期任务。
Webhook 未覆盖的交互/字段需在明确触发的核对中补齐，不能宣称 webhook 单独保证全部历史。

#### 5. 本次阅读快照 + 显式刷新

时间线读取绑定本地已发布版本/快照，并按发生时间及稳定身份排序；后续分页保持同一视图，
后来的新增、编辑、补录或删除标记不悄悄改变已打开的分页内容。
显式刷新先触发适用的后台同步，再在可读取的新版本上检查并切换/合并变化，保留稳定阅读锚点；
同步尚未完成时显示对应范围，不以旧快照未变化冒充远端无变化。
此前交互及新发现的早期记录分别标识，刷新不隐式标为个人已读；个人阅读状态仍由 #88 后续实现。
具体快照发布、提交可见性和变更游标由实现设计验证，不能直接把未提交的最大自增 ID 当作连续快照。

#### 6. 维护当前状态表

新增按 `flow_id` 维护的当前状态投影表，供看板/详情读取最新生命周期、派生阶段、当前动作和
执行摘要等白名单字段；不采用“每次实时联表查询即可、暂不维护状态表”的旧候选方案。
现有工作流事实继续拥有业务写入语义，状态表复用共用阶段规则，不能成为另一套调度状态机。
本地业务变更的事实、历史记录和当前投影需在一致事务边界更新；远端观察经身份/版本校验后应用，
不能让旧 webhook、补录活动或晚返回响应覆盖较新状态。
支持从当前业务事实重建/核对投影；历史不完整时标记迁移基线，不从缺失事件伪造过去。
时间线阅读快照与当前状态版本分别表明，不能让“最新状态”被历史阅读快照无意冻结。

- **Validation planned:** 增加重复事件幂等、编辑版本永久可读、删除后旧正文保留、此前交互归属、
  事件触发合并与恢复、无全量扫描、快照隔离/刷新、事务回滚及状态投影重建/防回退验证。
  真实 API 能力、索引和预算等继续由实现核验；不再把上述六项列为待用户选择的问题。
- **Implementation:** 本轮仅记录决策并更新同一草稿 PR；未新增状态表、同步服务、API 或测试。

### [028] 2026-10-17 — 永久归档、当前投影和主动 Worker 会话采集

- **Requirement / context:** 用户要求按 [027] 六项策略实现，明确 Worker 会话正文采用主动查询。
  本项实现取代 [024]—[027] 中尚未实现的候选机制；个人阅读状态继续留在 #88。
- **Implementation:**
  - `app/persistence/console_schema.py` 新增 `console_flows` 当前投影、`console_activity` 不可变
    版本、`console_sync` 持久来源任务及 installation 限流退避。现有事实的数据库触发器在原事务内
    更新投影、追加业务历史、排队同步；迁移只为缺失 Flow 建立明确的基线，不伪造旧业务历史。
  - `console_facts` 共用看板和 Projects 的阶段优先级。投影可从事实重建；看板状态版本与
    时间线阅读版本独立。远端标题以实际更新时间防回退，包括较新但正文未变化的观察。
  - `app/persistence/activity.py` 按 Flow 行锁串行分配发布版本，版本与正文一起提交；读取已提交
    的发布版本作为快照。重复观察幂等，编辑和确认删除追加版本，已保存正文永久可读。
    查询按发生时间与稳定身份排序，关联 GitHub 早期内容标为“此前交互”。
  - `app/persistence/console_sync.py` 将页面内容和检查点原子提交；持久 claim、失效 claim 栅栏、
    dirty 合并、重启恢复和来源独立失败避免中途崩溃或重复触发丢失工作。GitHub 子来源独立续读，
    installation 级限流不阻断 Worker。事务锁顺序统一为 Flow 发布锁后来源记录。
  - `app/controler/console/sync.py` 每轮最多处理四个来源任务，网络限时 30 秒，数据库工作在线程内
    受限执行并在取消时等待完成。每秒只检查到期任务，不周期扫描所有 Flow。
    webhook、本地事实、session 注册/变更和连接 epoch 变更触发同步，普通心跳不触发全量读取。
    活跃 Flow 详情/显式刷新另外排队同步，页面立即返回已有本地档案。
  - Worker 复用 `ConversationService.read(flow_id, cursor, 50)`，保存 EOF 游标继续增量查询；
    generation/cursor 失效后重新读取且幂等归档，不修改现有 Worker 协议。
    message 保留来源展示正文，tool_call/tool_result 保存最多 2000 字符摘要、截断信息及
    attempt/session/generation/record/call 查询定位；仅 arguments 的工具调用也有可见摘要。
- **Decision / consequences:** 选择数据库事务触发器覆盖所有规范写入口，避免恢复路径漏发 outbox；
  代价是新增 PostgreSQL 投影/触发器维护成本。保留永久数据库增长，有界的是请求、页面及 DOM，
  不声称总存储有界。继续复用现有会话有界查询，不能恢复来源已经截断或丢失的原始日志。
- **Validation:** 真实 PostgreSQL 回归覆盖幂等版本、删除后旧内容、此前交互、事务回滚、重建、
  旧标题防回退、未提交版本不可见、过期 claim 不可发布、限流隔离、Worker 主动查询及 EOF 恢复。

### [029] 2026-10-17 — GitHub 多来源、受保护 API 与真实数据前端

- **Implementation:**
  - `app/github/console.py` 对 Issue/PR 描述、评论、timeline、reviews、threads/replies、reactions、
    commits、check runs/statuses 分来源分页采集；旧对象编辑和线程状态可通过再次核对形成新版本。
    使用规范对象身份；webhook 保存实际观察版本及明确删除标记，不凭 404 推断删除。
    lifecycle webhook 元信息保存在对象版本上，规范 timeline 负责事件身份，避免 delivery 重复事件。
    GraphQL review 使用真实 updatedAt，回复使用 fullDatabaseId；checks/status 游标绑定 commit SHA。
  - 单次 GitHub 请求最多两对象、响应预算 1 MiB，控制页最多处理 20 对象；子来源独立续读。
    超预算/权限/限流均保留档案并明确报告来源状态，不静默丢弃后声称完整。
  - `app/contracts/console.py` 定义白名单 DTO；新增 `/api/repos`、`/api/flows`、详情、jobs、
    activity、activity/versions、activity/content。筛选先于分页、计数不受当前列分页影响；
    流列表为可变事实下的弱快照，时间线分页绑定本地已发布快照，正文按 Unicode 字符分页。
  - owner Access 同时保护 `/console/`、所有静态资源和 API，鉴权先于文件访问；安全错误、
    无缓存及 CSP 头保留边界。保留原 conversation、webhook 与 Worker 路由。
  - React 删除生产 fixtures 和旧展示入口，改用同源请求；看板、仓库、jobs、thread 和版本均分页。
    请求取消及旧响应检查防止切换后覆盖；失败保留原内容并支持重试。
    每列表最多挂载 60 行，每 Flow 最多缓存 240 条活动正文、最多四个 Flow；保留轻量页游标以便回读。
    正文/回复/版本窗口替换而非无限拼接；临时折叠、滚动、焦点及正文分段恢复保留。
    显式刷新按新快照重建游标到阅读锚点，不混用新旧版本。界面显示当前同步范围，不宣称远端完整。
  - `scripts/build_console.py` 与 Node 24 Docker 构建阶段生成静态资源，随 wheel/sdist 安装；
    serve 运行时不需要 Node。CI 构建包含资源断言；Tunnel 仅白名单控制台与精确 API 路由。
- **Limitations:** 首次采集前的编辑/删除、缺少时间戳的 reaction 移除或 thread 状态变化无法重建；
  PR commits REST 上限 250、已过期远端日志及 Worker 来源截断明确披露。
  未知 Worker 时间以 Flow 时间作排序回退并标记 `timestamp_unknown`，不伪造精确发生时间。
  GitHub/Worker 文本按不可信文本展示，不执行 HTML。未增加个人已读/未读或跨设备状态。
- **Validation:** GitHub 来源契约用受控响应覆盖分页、编辑、线程及错误；对自有仓库执行只读
  GraphQL schema 核验，但未替代真实 installation 全来源回填。浏览器使用 API DTO 替身，
  真实 HTTP/鉴权及数据库边界另由集成测试验证，不声称已经完成真实线上全链路。

### [030] 2026-10-17 — 自动化验证与机制消融

- **Validation results:**
  - `FLOWTOOL_TEST_DATABASE_URL=postgresql://127.0.0.1/flowtool_test uv run pytest -q`：
    781 passed；有一条 Starlette TestClient/httpx 依赖弃用提示，未修改或抑制上游依赖告警。
  - `uv run ruff check .`、新增 Python 文件 `ruff format --check`、`uv run mypy app tests`：
    通过，mypy 检查 193 个文件。`git diff --check` 通过。
  - 前端 `npm run format:check`、`npm run test`、`npm run build` 通过；
    `npm run test:e2e`：4 passed，覆盖 API DTO、错误态、320 条活动窗口/淘汰回读、固定头部、
    显式快照刷新、Unicode 正文续读、卸载重挂后的分段恢复及手机导航。
    已查看看板、详情/版本、概览、手机会话和错误态截图，无可见布局遮挡；截图使用替身数据。
  - `python3 scripts/build_console.py && uv build` 成功；wheel 与 sdist 均含 HTML/JS/CSS，
    不含 node_modules。隔离安装 wheel 后以其模块运行 HTTP smoke，验证 HTML/JS/CSS
    授权成功、未授权全部拒绝，运行检查不依赖 Node。
  - 两份可选 Tunnel 表达式各用 11 个允许路径和 8 个拒绝路径通过 Python regex smoke。
    本机无 Docker/cloudflared/kubectl，实际镜像、RE2 ingress 和集群验收不能据此宣称通过。
- **Ablation setup:** 使用临时 pytest 插件在进程内替换单一方法，不改产品文件。
  环境为同一 PostgreSQL 测试库；完整实现上述测试通过。对照分别执行：
  1. `ActivityStore.append` 的 `if duplicate:` 改成 `if False:`，归档版本测试失败，重复观察被追加。
  2. `ConsoleSyncStore.finish` 的 `next_cursor` 无条件设为 `None`，Worker EOF/刷新测试失败，
     持久续读位置丢失。
  3. `ActivityStore.page` 的 `if snapshot is None:` 改为无条件取最新版本，归档快照测试失败，
     旧阅读视图被删除版本替换。
  命令为 `CONSOLE_ABLATION=<mode> PYTHONPATH=.amp/in FLOWTOOL_TEST_DATABASE_URL=...`
  `uv run pytest -q -p console_ablation <test>`，mode 分别 dedupe/checkpoint/snapshot；
  test 为 `tests/test_console_archive.py::test_archive_versions_snapshots_and_prior_interactions`，
  checkpoint 使用 `tests/test_console_sync.py::test_worker_is_actively_queried_and_eof_checkpoint_survives_refresh`。
  三组均按预期 1 failed，保留幂等、检查点、快照三项机制。
  临时插件 `.amp/in/console_ablation.py` 和 `.amp/in/artifacts/ablation-*.log` 仅本地保存，不发布。
- **Remaining acceptance:** 真实 GitHub App 权限/全来源采集、Access 登录/MFA、目标 Tunnel/镜像部署，
  以及此次推送后的远端 CI。未合并、未部署，#85 不提前关闭。

### [031] 2026-10-17 — “已编辑”链接与上一版差异

- **Requirement:** 用户要求移除历史版本按钮/列表，只保留可点击的“已编辑”提示，点击展开上一版 diff。
- **Implementation:** Activity DTO 返回该归档版本的上一内容版本；只有已观察到标题/正文变化才显示
  无背景边框的文字按钮，首次采集、仅元信息变化及删除标记不展示“已编辑”。点击按需请求
  `/activity/content?compare=true`，在原活动下展开红删绿增 unified diff，再次点击收起。
  不再重复列出各版完整正文，也不将 Flow 归档序号展示为编辑次数；归档及版本查询能力仍保留。
- **Decision / bounds:** 比较绑定不可变版本，后续采集不改变当前比较对象；跳过元信息重复观察。
  复用 Python 标准库，无新增依赖。每版最多比较 65536 字符正文、4096 字符标题和 2000 行，
  超预算明确提示，仍可阅读正文/使用已有来源链接，绝不把截断预览冒充完整差异。
  diff 响应每部分最多 16384 字符，DOM 继续使用现有代码窗口，展开结果只保留一个响应部分。
- **Validation:** Python 完整测试 782 passed；Ruff、Mypy、前端格式/单测/类型检查/构建通过；
  Playwright 4 passed，更新回归验证默认收起、键盘展开、红删绿增、点击收起和手机无横向溢出。
  HTTP 回归使用真实 PostgreSQL，验证无旧版不显示、元信息变化不误报、超过预览长度的 Unicode
  修改仍可比较、授权、分页、旧比较对稳定和超预算明确失败。wheel/sdist 已重新构建。
  已检查桌面收起、桌面展开及手机展开截图，使用 API DTO 测试数据。
- **Ablation:** 临时进程内把比较输入改为旧的 2000 字符展示预览，同一 HTTP 回归因遗漏尾部修改
  而失败（1 failed）；完整实现通过。命令：`PYTHONPATH=.amp/in FLOWTOOL_TEST_DATABASE_URL=...`
  `uv run pytest -q -p edited_diff_ablation tests/test_console_api.py::test_edited_hint_and_diff_compare_full_immutable_content_not_preview`。
  插件和日志仅保留在 `.amp/in/`；本轮改动仍在本地，未推送，不绕过此前超时的发布审查门禁。

### [032] 2026-10-17 — 时间拆行、简化计数和对齐修正

- **Requirement / implementation:** 按用户连续反馈，概览“创建 / 更新”拆成两个独立标签/时间行；
  看板及侧栏移除“已加载”数量，只保留任务总数和分页入口，不改变分页/窗口化。
  会话头像与作者组共享响应式高度、移除顶部补偿，作者与来源标签用基线对齐；
  侧栏任务圆点取消固定顶部偏移，用 flex 垂直居中，标题换行时仍随标题块居中。
- **Validation:** 前端格式、2 项单测、类型检查/构建和 4 项浏览器回归通过；检查桌面看板、
  桌面/手机概览及会话截图，确认无已加载数量、时间两行、头像/文字及侧栏圆点对齐。
  仅修改展示布局，不引入新机制，无需另做消融；本地改动仍未推送。

### [033] 2026-10-17 — 完整 patch 与 react-diff-view

- **Requirement / decision:** 用户选择 react-diff-view，并要求后端返回完整 diff。
  替代 [031] 的 diff 字符分页及自制逐行着色，正文分页不变；不引入双栏切换或代码编辑器。
- **Implementation:** `compare=true` 一次返回完整 unified patch，`next_offset=null`，
  `offset` 必须为零，正文的 `limit` 不切割 patch。继续绑定不可变比较版本，保留每版
  65536 正文字符、4096 标题字符和 2000 行的计算预算；超限明确失败，不返回截断结果。
  新增 `DiffWindow`，用 react-diff-view 解析完整 patch 并生成行内高亮，再按已解析变更行
  窗口化：每次最多 200 行，保持原始行号和完整文本，视口最高 480px，切换无需网络请求。
  “已编辑”仍默认收起，移除旧 diff 专用 TextWindow 分支及 CSS；普通正文/工具摘要不变。
- **Validation:** HTTP 回归先在原实现失败（超过 16K 的上下文截断了尾部修改），修改后通过；
  完整 Python 782 passed，Ruff/Mypy 通过，仍有一条上游 Starlette/httpx 弃用提示。
  前端类型/构建、格式、2 项单测、5 项浏览器测试通过；新增长 patch 浏览器回归覆盖
  多窗口、跨差异块、旧新行号、恶意 HTML 按文本展示、只请求一次及收起后恢复窗口。
  已检查桌面/手机差异截图及固定高度长 diff 截图，均使用 API DTO 测试数据。
- **Ablation:** 同一份 24120 字符、900 行 patch，直接使用库渲染会挂载 900 行，
  `DiffWindow` 挂载 200 行；长 diff 浏览器测试确认切换后末尾仍可达，不丢文本。
  对照用 `npx tsx --input-type=module` 调用 React SSR，日志保存于
  `.amp/in/artifacts/console-diff-window-ablation.log`；保留窗口机制以满足固定最大 DOM 要求。
  本轮仅本地实现与验证，未推送，不绕过此前超时的发布审查门禁。

### [034] 2026-10-17 — macOS 风格状态色表

- **Requirement / implementation:** 用户要求参考苹果 App 左上角窗口按钮配色调整状态图标。
  用共享 CSS 色表统一排队灰、执行蓝、待审黄、受阻红、完成绿；红黄绿参考窗口按钮，
  补充灰蓝以区分五个业务阶段。保持圆点形状、尺寸和文字，覆盖看板、卡片、侧栏与详情。
  来源状态带上既有 status 类，pending/syncing/partial/unavailable/available 对应同一色表，
  修正原先来源圆点未区分状态的问题；不改变数据或状态含义。
- **Validation:** 前端格式、类型/构建、2 项单测与5项浏览器回归通过；本地临时浏览器脚本
  `.amp/in/verify-status-palette.mjs` 校验五阶段各处实际计算色值、五种来源状态色值、
  手机无横向溢出和无页面异常，检查桌面看板/详情及手机截图。纯配色调整无新增复杂机制，
  不新增永久样式测试或消融；仅本地改动，未推送。

### [035] 2026-10-17 — 同步并发、归档字符与阅读恢复修复

- **Requirement:** 用户要求修复上一轮独立审查报告中的六项问题，继续使用同一功能分支和 PR #87。
- **Implementation:** `console_enqueue` 先锁 Flow 发布行再操作来源队列，Worker 重连按 flow_id 顺序
  排队，与 `ConsoleSyncStore.finish` 保持一致；同步批次显式持有任务，失败或取消时取消并等待
  全部兄弟任务，避免超出并发上限或在数据库关闭后继续写入。已处于取消状态的任务不二次取消，
  保证 `db()` 完成底层线程回收。
  `ActivityStore.append` 在 PostgreSQL 边界将 NUL 显示为 `\u0000`，覆盖文本和来源元信息，
  标记 `unsupported_characters_escaped`；仍以原始观察内容计算摘要，保持重复采集幂等，
  不把原始 NUL 与字面转义串混为一版。非法 Unicode 代理码点已被输入模型拒绝，无需重复处理。
  来源限制去重累计，后续干净页/增量 EOF 不再覆盖历史缺口。
  线程回复读取器以 Flow/线程/快照作为组件身份，快照更新会中止旧请求并清空旧分页状态；
  会话错误连同失败请求的 cursor/refresh 参数一起缓存，切换标签再回来仍可显式重试。
- **Decision / trade-offs:** 保留已有发布锁、任务并发限制及快照协议，不引入新队列或缓存层。
  当前没有可确认历史覆盖已修复的回填操作，因此保守保留来源限制，不因一次成功采集自动清除；
  未来确实修复对应缺口时再增加显式清除能力。前端只缓存失败参数，不缓存跨组件生命周期的闭包；
  快照更新后线程回复需要重新打开，避免混合两个快照的游标/正文。界面样式保持不变。
- **Validation / ablation:** 修复前用同一组针对性测试复现 PostgreSQL 死锁、NUL 入库错误、
  缺口误报 available、失败批次遗留兄弟任务，以及线程旧游标/缓存重试无效的两项浏览器故障。
  修复后 `FLOWTOOL_TEST_DATABASE_URL=postgresql://127.0.0.1/flowtool_test uv run pytest -q`
  全量 786 passed，仍有一条上游 Starlette/httpx 弃用提示；Ruff、Mypy（193 个文件）通过。
  前端格式检查、2 项单测、类型/构建与 7 项 Playwright 测试通过；本轮是交互修复，以 DOM 和
  实际请求的 cursor/snapshot 断言验收。`python3 scripts/build_console.py && uv build` 通过。
  对照日志见 `.amp/in/artifacts/console-fixes-before-python.log`、
  `console-fixes-before-nul.log`、`console-fixes-before-browser.log`，修复后全量日志见
  `console-fixes-python.log`。新增机制均由对应失败场景验证必要性，取消场景同时作为正常回收对照。
  本轮修改及验证在本地完成，尚未推送或部署。

### [036] 2026-10-17 — 临时不可用恢复与取消期间的线程回收

- **Requirement / context:** 用户要求修复 [035] 复审剩余的两项中等问题：启动阶段的
  `no_worker` / `no_session` 被永久累计为覆盖缺口；失败批次正在回收线程时，停服再次取消
  会中断等待，使数据库线程在同步器退出后仍持有连接。
- **Implementation / decision:** 补充 [035] 的限制分类：会话页的 `unavailable_reason` 单独写入
  既有 `reason`，显示为 unavailable，不再进入永久累计的 `limitations`。随后成功读取清除
  临时原因，真实截断/历史缺口仍保留。无会话的读取不更新 `last_success_at`，也不算失败重试；
  保留已有 dirty/repeat 处理，读取期间新到的 Worker/session 事件仍能排队下一轮同步。
  新增同步器内部 `_drain`，数据库线程等待和批次清理共用：屏蔽取消向下传播，记录取消请求，
  等所拥有的任务真正结束后再向上抛出取消。重复停服取消、来源超时追加取消均不提前释放连接槽。
- **Trade-offs:** 复用现有 reason/limitations 字段，不新增状态表、数据库列或兼容层。
  停服可能需要等数据库操作完成；不能安全强制终止 Python 线程，因此不以提前退出冒充清理完成。
  只保护已拥有的工作，开始工作前等待信号量仍可直接取消；不把整个同步过程改为不可取消。
- **Validation / ablation:** 先运行新增的两个可用性恢复参数场景及真实线程回归，修复前
  3 failed；修复后同步测试 10 passed。线程回归让三条真实 PostgreSQL 连接停在事件屏障上，
  第四个来源失败后连续取消父任务，再追加子任务取消，确认三条线程退出后同步器才结束。
  只保护父任务清理、保留旧 `db()` 等待方式的简化实现仍失败（3 个子任务均提前返回），
  因而在两个等待点共用取消安全的回收逻辑。日志保留于
  `.amp/in/artifacts/console-recovery-before.log`、`console-recovery-drain-ablation.log`。
  全量 Python 789 passed（仍有一条上游 Starlette/httpx 弃用提示），Ruff、Mypy 193 个文件通过；
  前端格式、类型检查、2 项单测和 7 项浏览器回归通过。本轮没有前端代码或视觉变化。
  全量日志：`.amp/in/artifacts/console-recovery-python.log`；本轮尚未推送或部署。

### [037] 2026-10-17 — 按会话恢复覆盖状态并清理旧临时限制

- **Requirement / context:** 用户要求补齐 [036] 复审剩余的问题：`not_found` 也可能恢复，
  旧版写进 limitations 的临时状态还会阻止已有来源恢复。
- **Implementation:** Worker 会话页不再把 session status 写进永久限制；`finish` 在同一事务内
  将每个会话的当前状态写入 `console_sync` 的 `worker:<attempt>:<generation>:<session>` 子行。
  子行始终不排队，复用现有来源汇总；missing/unsupported 显示 partial 和具体原因，只有同一
  会话再次被观察为 available 时才能清除。后面的干净会话页不会覆盖前面的未恢复状态。
  扫到 EOF 仍有未恢复会话时，将主来源游标设为 NULL，下一次事件/显式刷新从第一会话重查；
  全部会话可读后保留 EOF 增量检查点，不引入周期全量查询或自动无限重扫。
  对旧 `no_worker` / `no_session` / `not_found` / `unsupported` 限制，claim 从第一会话重新读取，
  成功提交时移除旧的无身份临时标记，用这轮实际采集的会话状态替代。读取失败不提前清除，
  多页未完成时仍为 pending；`worker_source_truncated` 等真实内容缺口继续永久累计。
- **Decision / trade-offs:** 按会话保存状态，避免“最后一页成功就清除所有缺口”和“所有缺口永不恢复”
  两种错误。复用子来源状态和已有联合主键，不新增表/列；缺失会话存在时，显式同步成本比单纯 EOF
  增量读取高，换取此前未读到的会话确实能被补采。归档版本和业务状态不被此恢复清理删除或改写。
- **Validation:** 修复前，旧限制清理、跨会话恢复和保留真实截断的 7 个目标场景失败；修复后
  同步测试 16 passed，全量 Python 795 passed（仍有一条上游 Starlette/httpx 弃用提示）。
  Ruff、Mypy 193 个文件、前端格式/类型检查、2 项单测、7 项浏览器回归、`uv build` 通过。
  日志：`.amp/in/artifacts/console-session-recovery-before.log`、`console-session-recovery-python.log`。
- **Ablation:** 同一多会话/重启场景，去掉会话子状态后两项参数测试失败（误报 available）；
  只去掉 EOF 重置后同样两项失败（下次仍从最后会话开始，无法重读早期会话）。保留两个机制。
  临时 pytest 插件已删除，文本证据留在 `console-session-ablation-plugin.txt`；复现时恢复为
  `.amp/in/console_session_ablation.py`，以 `PYTHONPATH=.amp/in CONSOLE_ABLATION=without_sessions`
  或 `without_rewind` 加上述测试数据库变量，运行 `uv run pytest -q -p console_session_ablation`
  `tests/test_console_sync.py -k missing_session`。日志：`console-session-ablation-state.log`、
  `console-session-ablation-rewind.log`，均位于 `.amp/in/artifacts/`。本轮尚未推送或部署。

### [038] 2026-10-17 — 同一会话跨页累计覆盖，完整重读后恢复

- **Requirement / context:** 用户要求修复 [037] 复审的中等问题：Worker 的 `unsupported`
  只描述当前页，同一会话后续正常页会覆盖此前 partial，错误地保留 EOF 检查点。
- **Implementation / decision:** 补充 [037] 的恢复条件：只有从同一会话开头完整读到末尾且全程
  available，才清除其可恢复缺口。`SessionHistory` 显式携带 `starts_at_beginning` 和
  `reached_end`，由 Control 使用原始 inner cursor 和 Worker `HistoryPage.has_more` 赋值，
  不把 Flow 跨会话续页误当成本会话尚未读完。既有 `console_sync` 新增布尔列
  `worker_scan_clean`，在会话子行记录这轮是否从头开始且截至当前页均正常，与归档和主游标
  同事务提交；完成或出现不支持页后清零。重启可继续已有扫描，干净尾页和未完成的干净重扫
  均不能清除旧原因。仍有缺口时沿用 [037] 的 EOF 回退和事件/显式刷新触发策略。
- **Trade-offs:** 新增一个持久布尔字段，避免依赖易丢失的进程内累计状态，也避免存储逐页副本。
  旧子行默认 FALSE，已有缺口需完整重读才能恢复；不改 Worker 传输协议、不加周期全量扫描。
  本次仅修复覆盖判断，不改变内容永久保存、版本或个人阅读状态范围。
- **Validation:** 新增真实 PostgreSQL 回归，修复前 1 failed：unsupported 首页面后接正常
  末页会误变 available；修复后同步/会话测试 26 passed。回归逐页重建同步器，覆盖旧增量
  EOF、未完成重扫和完整正常重扫；已有 HTTP/Worker 端到端测试同时验证会话/Flow 边界。
  全量 Python 796 passed，保留一条上游 Starlette/httpx 弃用提示；Ruff、Mypy 193 个文件、
  前端格式/类型检查、2 项单测、7 项浏览器测试及 `uv build` 通过，无新增视觉变化。
  证据：`.amp/in/artifacts/console-page-coverage-before.log`、`console-page-coverage-python.log`。
- **Ablation:** 同一回归分别移除跨页持久状态、起点边界和终点边界，三组均 1 failed：
  无持久状态不能在重启后完成恢复；无起点判断会让正常尾页误清除；无终点判断会让正常重扫
  首页面提前清除。保留三个机制。插件文本保存在 `console-page-ablation-plugin.txt`；复现时
  恢复为 `.amp/in/console_page_ablation.py`，以 `PYTHONPATH=.amp/in`、上述测试数据库变量及
  `CONSOLE_ABLATION=without_persistence` / `without_start_boundary` / `without_end_boundary`
  运行 `uv run pytest -q -p console_page_ablation tests/test_console_sync.py -k session_gap`。
  日志为 `.amp/in/artifacts/console-page-ablation-<模式>.log`。临时插件删除，本轮尚未推送或部署。

### [039] 2026-10-17 — 跨页回归改为验证公开来源状态和后续读取位置

- **Requirement / scope:** 用户仅要求修复代码准则审查的第 3 项：跨页回归对内部子行结构和
  位置元组依赖较重。本轮不处理“上次成功”时间和不可用原因类型两项问题，不改生产代码。
- **Implementation:** `test_session_gap_survives_clean_pages_until_complete_clean_rescan`
  使用具名的 unsupported 首段、正常尾段、正常重扫首段，按场景顺序直接调用；删除六列元组、
  基于循环索引的分支，以及读取子行 status/reason 和主行 cursor 的断言 SQL。通过
  `ConsoleStore.sources()` 检查对外状态/原因，通过后续 `claim().cursor` 和会话读取参数验证
  检查点回退与保留。保留模拟旧 EOF 的 SQL，调度仍直接推进 next_at 以免依赖实际等待。
  一个局部有类型的读取辅助函数复用真实同步和重启步骤，不引入通用场景框架或新永久测试。
- **Validation / trade-offs:** 同步/会话相关测试 26 passed，Ruff 和目标测试文件 Mypy 通过。
  沿用 [038] 的三种故障注入，新测试在每种情况下均按预期 1 failed，保留原回归保护能力。
  本轮只调整既有测试，没有新增生产机制；无需新增机制消融。证据：
  `.amp/in/artifacts/console-test-contract-validation.log`、
  `console-test-contract-without_persistence.log`、`console-test-contract-without_start_boundary.log`、
  `console-test-contract-without_end_boundary.log`；注入方法同 [038]，临时插件已删除。
  本轮未重跑全量 Python、前端和打包，生产代码未变；修改仅在本地，尚未推送或部署。

### [040] 2026-10-17 — 分清 Worker 当前同步健康与历史会话覆盖的汇总规则

- **Requirement / scope:** 用户要求先修结构审查第 1 项：共用 `console_sync` 的队列记录与
  历史会话覆盖记录，不应无差别合并所有字段。此次不迁表、不改 Observation 归属或原因类型。
- **Implementation / decision:** `ConsoleStore.sources()` 继续让全部 Worker 行参与状态优先级
  和缺口汇总，但 Worker 最近成功时间只取主来源行；当前主来源为 unavailable 时，其错误
  优先于历史会话原因。主来源没有当前不可用错误时，保留原有覆盖原因汇总。GitHub 来源的
  时间、原因和状态规则保持不变，归档与同步任务状态不修改；会话缺口不会因显示连接错误而清除。
- **Trade-offs:** 在已有聚合查询中增加明确的字段归属规则，无新增表、类、API 字段或查询轮次。
  单个 reason 优先解释当前连接故障，历史覆盖原因仍持久保存；恢复连接后可继续显示未恢复的
  历史缺口。该约定避免用旧会话时间表达当前新鲜度，也避免按字典顺序决定当前连接错误。
- **Validation:** 新增一个双场景真实 PostgreSQL 回归，通过公开 `finish` / `fail` / `sources`
  接口验证：较早正常会话不能固定较新读取的成功时间，历史 unsupported 不能遮住当前
  worker_unavailable。修复前两场景均失败，修复后控制台同步/API 21 passed，全量 Python
  798 passed（保留上游 Starlette/httpx 弃用提示）；Ruff、Mypy 193 个文件、测试文件格式、
  前端类型检查、2 项单测、7 项浏览器测试及 `uv build` 通过。一次目标测试命令引用了不存在的
  `test_console_persistence.py`，未执行测试；修正目标文件后通过，本轮全量已覆盖实际 archive 测试。
  日志：`.amp/in/artifacts/console-source-boundary-before.log`、
  `console-source-boundary-targeted.log`、`console-source-boundary-python.log`。
- **Ablation:** 分别恢复旧时间汇总和旧原因汇总，同一回归分别得到 2 failed 和
  1 failed / 1 passed，证明两个规则各自必要。临时插件已删除，文本证据保存在
  `console-source-ablation-plugin.txt`；复现时恢复为 `.amp/in/console_source_ablation.py`，设置
  `PYTHONPATH=.amp/in`、上述测试数据库变量及
  `CONSOLE_ABLATION=without_freshness_boundary` / `without_error_priority`，运行
  `uv run pytest -q -p console_source_ablation tests/test_console_sync.py -k freshness`。
  日志为 `.amp/in/artifacts/console-source-<模式>.log`。修改仅在本地，尚未推送或部署。

### [041] 2026-10-18 — 详情根据执行与交付事实解释原因和下一步

- **Requirement / scope:** 用户要求实现 #85 收尾核对的第 1 项：详情的 `reason` 与
  `next_condition` 不再仅按五阶段返回通用文案。此次是既有详情读取的局部修复，沿用原 API
  字段及页面，不新增状态表、领域类、迁移、控制入口或个人阅读状态。
- **Implementation / decision:** `ConsoleStore.detail()` 在同一 SQL 语句快照内读取当前投影及
  一条相关任务的白名单诊断事实。未完成 GitHub 写入、未应用结果、执行中和排队任务优先于
  普通历史任务，避免较新的排队记录遮住旧任务的未完成写入。展示函数区分准备重试、等待开始
  确认、首轮编码/修订/归档执行、恢复待确认、校验重试、结果应用、GitHub 写入失败，以及
  关闭 PR、等待审核、归档与终态；有任务时带安全任务 ID。终态与关闭 PR 优先解释，保留重开
  与 head/base 对账要求；原执行状态未知时明确不能强制释放绑定或重复执行。
- **Trade-offs:** 直接从规范任务读取几个布尔事实，无需扩展持久投影或复制恢复策略。
  诊断只解释已保存事实，不承诺重试一定被允许，不返回异常正文、路径、凭据或 effect payload。
  详情固定增加 SQL 内的任务选择与失败 effect 检查，不增加逐任务查询或远端请求；阶段和
  `action` 仍由原有事实规则决定，具体诊断不反过来驱动调度。
- **Validation:** 新增一项参数化真实 PostgreSQL HTTP 回归，20 个场景验证任务类型、阻塞
  优先级、结果优先于恢复状态、关闭 PR 优先于写入错误及敏感字段排除。原实现的 16 个初始
  场景均因通用诊断失败；修复后 20 场景通过。全量 Python 818 passed，保留上游
  Starlette/httpx 弃用提示；Ruff、格式检查、Mypy 193 文件、前端类型检查、2 项单测、7 项
  Playwright 和 `uv build` 通过。证据：`.amp/in/artifacts/console-diagnostics-before.log`、
  `console-diagnostics-contract.log`、`console-diagnostics-python.log`。
- **Ablation:** 临时插件分别将诊断任务选择改为只取最新 ID、去掉关闭 PR 优先解释。在相同的
  older_result / closed 两场景上分别得到 1 failed / 1 passed：前者漏报旧任务的 GitHub 写入
  阻塞，后者用写入错误遮住必须重开/对账的交付限制，故保留两项规则。复现插件文本为
  `.amp/in/artifacts/console-diagnostics-probe.txt`；临时恢复为 `.amp/in/console_diagnostics_probe.py`，
  以 `PYTHONPATH=.amp/in FLOWTOOL_TEST_DATABASE_URL=postgresql://127.0.0.1/flowtool_test`
  和 `DIAGNOSTICS_ABLATION=without_blocker_priority` 或 `without_closed_guard` 执行
  `uv run pytest -q -p console_diagnostics_probe tests/test_console_api.py -k 'older_result or closed'`。
  日志为 `console-diagnostics-<模式>.log`；临时插件已删除。
- **Rendered check / delivery:** 将真实 PostgreSQL HTTP 测试返回的 DTO 注入浏览器路由，验证
  桌面执行中、关闭 PR 和 390px 手机归档恢复的完整原因/条件可见、无横向溢出或页面异常，
  并检查截图。证据位于 `.amp/in/artifacts/console-diagnostics-{running,closed,archive_recovery}.png`
  及 `console-diagnostics-browser.log`。这不替代真实 Access/GitHub 线上全链路验收；此次修改
  仅在本地，尚未提交、推送或部署，#85 仍保持开放。

### [042] 2026-10-18 — 会话事件、内部标记和长正文阅读修复提案

- **Requirement / scope:** 用户连续提供三张截图：无正文的 `labeled` 事件展示不明确且占用
  完整消息布局；GitHub 评论将 `<!-- flowtool:effect:… -->` 显示为正文；长正文同时出现
  “下一段”和“下一部分”，用户报告点击后无法继续阅读，并要求按钮更贴合页面背景。
  本记录仅保存需求与修复提案，尚未批准或实现。不改变截图中的实际仓库执行错误，也不
  将 developer/skills 指令隐藏或删除；用户未要求这些行为。
- **Observed code / context:** `app/github/console.py` 已采集标签名称到 metadata，当前活动
  组件直接使用原始事件标题。`app/controler/workflow/effects.py` 在评论末尾附加 effect
  标记，用于副作用去重和结果恢复，不能从 GitHub 原文移除。`ActivityBody` 从归档读取
  正文部分，内部 `TextWindow` 再进行本地分段，形成两套独立导航。分页点击失败的具体
  触发条件仍需浏览器复现，不能仅凭截图确定。
- **Proposed behavior:** 无正文的标签事件用紧凑行展示“添加标签：标签名”或“移除标签：
  标签名”，保留作者、时间和可展开来源信息；缺失标签名称时只陈述已知动作，不伪造名称。
  其他无正文事件使用明确的动作摘要，未知事件保留原始名称供查询。GitHub 评论的正文、
  续读和编辑差异均不显示末尾内部 effect 标记；原始归档及远端评论保持不变。
  长正文只显示一套上一段/下一段控件和基于全文的阅读范围，到已读取部分末尾时继续读取
  后续部分；加载失败保留当前正文并允许重试。按钮使用与背景协调的低强调样式，同时
  保留 hover、键盘焦点、禁用和加载状态。
- **Technical direction / alternatives:** 在展示读取边界处理内部标记，并保证分页偏移和
  长度使用同一份展示文本，避免前端逐片删除导致跨边界标记泄漏或字符统计错误。只识别
  实际生成格式的末尾 effect 标记，不引入任意 HTML 渲染或通用 Markdown 清理。
  统一活动正文导航，保留现有有界读取、工具行数预算、Unicode 安全和虚拟列表阅读恢复；
  不采用一次加载整篇正文或只隐藏其中一排按钮的方案。预计涉及 `activity.tsx`、
  `windowing.tsx`、`styles.css`、归档读取及对应回归；不新增依赖、表或写接口。
- **Implementation order / validation:** 获批后先用浏览器复现截图所示两层分页，再统一
  阅读状态和跨部分导航；随后处理事件摘要和内部标记。验证短文、超过预览长度、跨多个
  后端部分、emoji、前后往返、读取失败重试、卸载后恢复；验证标记位于预览/分段边界、
  正常正文和非内部 HTML 注释不被误删、编辑差异与正文一致。运行相关 PostgreSQL/API
  回归、前端单测、类型/格式检查、构建和 Playwright；检查桌面与手机的默认、翻页、
  加载/失败状态截图。此轮定位尚未执行上述测试或浏览器复现。消融优先对比修复前后
  同一失败场景，不为样式或文案添加额外机制。
- **Outcome / pending:** 等待用户批准本提案；实现、验证、提交、推送及部署均未进行。

### [043] 2026-10-18 — 补充同轮 Agent 输出分组与技术标题去除

- **Requirement update:** 用户补充第四张截图，要求相关输出合并展示，`message` 不应作为
  消息标题。截图同时包含 assistant 说明、`exec` 调用和独立的 `tool_result`。
- **Context:** `ConsoleSynchronizer._worker()` 以工具名称或 `item.kind` 生成标题，因此
  普通消息得到 `message`，工具结果得到 `tool_result`；前端又同时展示工具标题和折叠摘要。
  归档 metadata 已包含 attempt、session、generation 和 call_id，可作为关联依据。
- **Proposed decision / status:** 在 [042] 的待批准范围内增加输出分组：普通消息直接展示
  正文，去除 `message` / `tool_result` 等内部类型标题；同一会话内有关联的工具调用和结果
  合并为一个可折叠工具块，工具名称只出现一次；相关 assistant 说明和连续输出组合展示，
  减少重复头像与来源信息。保留每条原始记录的定位和顺序，不修改归档身份或丢弃正文。
  缺少明确关联信息时不凭相同时间戳强行配对，不跨 session / generation / attempt 合并，
  用户消息或其他来源插入时保留边界。此处为提案，尚未实现。
- **Validation additions:** 覆盖调用和结果跨活动页、并行调用结果交错、缺失/未返回结果、
  会话切换、刷新及虚拟列表卸载重挂；确认完整正文与各自来源仍可达，分组不突破 DOM 与
  正文读取预算。桌面和手机截图检查分组展开/收起以及普通消息不再出现技术标题。
- **Outcome / pending:** 与 [042] 一起等待方案批准；本轮仅更新记录，未修改实现代码。

### [044] 2026-10-18 — 补充手动关闭 PR 后的 502 与状态恢复问题

- **Requirement update:** 用户报告手动关闭 PR 后，页面出现“读取失败：请求失败 (502)
  （重试）”，状态永久卡住。截图没有请求 URL、响应 detail、Flow 编号或服务端日志，
  目前只能确认用户报告的操作顺序，不能认定关闭 PR 与 502 已有明确因果关系。
- **Observed code / boundary:** PR 对账在发现未合并关闭时调用 `pause_delivery()`，将
  delivery 设为 CLOSED 并取消排队任务，按现有约定保留 Flow 和运行中的执行绑定，等待
  验证后重开。这个受阻状态本身不能作为死锁证据。会话活动来自本地归档读取，前端发生
  错误后停止自动加载，并允许手动重试原请求；另一个实时 conversation API 会将部分
  `ConversationFailure` 映射为 502。截图尚不能区分应用响应和代理错误。
- **Proposed investigation / decision pending:** 将本项纳入同批修复并优先定位。获取受影响
  Flow/PR、失败请求路径与响应及对应时间日志；分别复现关闭时排队中、执行中、等待审核
  三种情形，检查 webhook、对账、绑定、任务、同步任务与页面读取是否能继续推进。根据
  证据修复实际失败边界，保证已归档会话仍可读、失败可重试、状态和下一步说明与事实一致。
  不以捕获所有异常、无限重试、清空归档或强制释放未确认执行的绑定掩盖错误。
  未经用户确认，不将“关闭但可重开”的现有语义改为自动取消/归档。
- **Validation additions:** 同一失败场景修复前后对比；关闭/重开及漏 webhook 对账回归，
  页面 502 后重试成功、切换页面后重试、归档继续读取；确认关闭 PR 不造成服务进程退出
  或后台同步失去后续执行机会。真实线上 502 的根因与具体修复仍待请求和日志证据。
- **Outcome / pending:** [042]–[044] 共记录五项用户问题，均待批准实施；本轮只更新本地
  需求记录，未改产品代码、未运行行为验证、未提交、推送或部署。

### [045] — 关闭 PR 故障的目标与关闭时间核实

- **Requirement evidence:** 用户提供受影响 PR：
  [b1indsight/neural_network#2](https://github.com/b1indsight/neural_network/pull/2)，
  标题“测试 agentflow 正常运行”，head 为 `flow/1`，base 为 `main`。
- **Read-only verification:** `gh pr view 2 --repo b1indsight/neural_network` 返回 CLOSED、
  mergedAt 为 null；GitHub timeline 显示 b1indsight 于 2026-10-18 21:20:16 UTC
  （新加坡时间 2026-10-19 05:20:16）关闭 PR。读取时未发现后续重开事件。
  这确认了手动关闭且未合并，但不能据此确定 502 的服务端原因或 Flow 执行状态。
- **Outcome / pending:** 已补齐 [044] 的 PR 定位与日志时间窗口；失败请求及服务端日志
  仍待核查。用户此次提供定位信息，未明确批准 [042]–[044] 的实施方案；产品代码未修改。

### [046] — 补充手机详情标题随阅读收缩

- **Requirement update:** 用户反馈手机详情中不随正文滚动的标题区域占用过多高度，要求
  向上滑动、继续阅读正文时收缩为一行，只保留最简信息，为会话内容留出更多空间。
- **Observed code / context:** `App.tsx` 的 `detail-head` 包含任务切换、完整标题、状态、
  仓库与 Issue/PR 链接和页签，位于独立滚动的 `detail-body` 之外；手机 CSS 将标题与
  状态纵向排列，因此这些内容始终占据正文上方的固定空间。
- **Proposed behavior / status:** 仅手机窄屏：初次进入且位于正文顶部时保留完整详情头；
  向上滑动使正文继续向下阅读后，整个固定详情头收缩为一行，保留返回入口、PR 编号
  （无 PR 时使用 Issue 编号）和省略的短标题。完整标题、仓库链接、状态及页签不继续
  占用固定高度；回到顶部时恢复完整头部，并提供明确的展开入口，确保阅读中仍能切换
  页签和任务。桌面布局不变。此处为待批准提案，尚未实现。
- **Technical direction / risks:** 以现有 `detail-body` 为滚动观察对象，不监听错误的
  window 滚动；使用稳定阈值，避免头部变高/变矮导致反复展开收缩、正文跳动或虚拟列表
  阅读位置丢失。遵循减少动画偏好，隐藏区域不可继续接收键盘焦点；切换 Flow、页签、
  屏幕方向和恢复已有阅读位置时，折叠状态应与实际滚动位置一致。不增加依赖或新的
  持久化阅读状态。
- **Validation additions:** 手机普通/长标题的顶部展开、连续阅读单行收缩、展开入口、
  返回顶部恢复、横竖屏和页签切换；检查头部实际高度、正文可用高度、无横向溢出和
  折叠阈值附近无抖动，确认正文分页、列表加载及阅读锚点不受影响。浏览器执行检查，
  并分别检查展开与收缩状态截图；桌面作不变对照。
- **Outcome / pending:** 与前五项一起等待批准实施；本轮仅追加本地需求记录，未修改
  产品代码、未执行渲染或行为验证、未提交、推送或部署。

### [047] — 会话展示、正文续读与手机详情的本地修复

- **Implemented:** 空标签等事件使用紧凑动作摘要；展示读取统一去除 GitHub 评论末尾
  的 effect 标记，预览、长度、续读和编辑差异使用同一正文，原始归档保持不变。
  正文改用单套低强调分段导航，支持 Unicode 偏移、读取失败保留当前内容并重试，
  页签切换后恢复阅读位置。连续 Agent 输出减少重复身份、来源和内部类型标题。
  同会话、同代且 call_id 匹配的相邻工具调用/结果共用折叠框，交错结果保持原始顺序并
  联动展开；没有匹配调用或调用已离开虚拟窗口时保留独立入口，不改变原始记录顺序。
  手机阅读时详情头收为一行，可手动展开或回到顶部恢复；高度变化补偿正文位置，
  桌面保持完整头部，隐藏导航不能继续接收焦点。
- **Verification:** 全量 Python 825 项通过；恢复临时验证改动后，后端相关 122 项、
  Ruff、格式及 Mypy（112 个源文件）通过。前端格式、TypeScript/Vite 构建、2 项单元测试
  和 12 项 Playwright 回归通过。浏览器使用受控 API 响应，验证真实组件、分页及窗口化；
  不代表线上服务、Access 或关闭 PR 的生产故障已修复。已检查桌面折叠/展开、手机完整/
  收缩头部、正文导航/失败重试截图。日志及最终截图位于 `.amp/in/artifacts/console-fixes-*`。
- **Regression evidence:** 临时移除展示标记过滤后，公开读取测试出现标记泄漏；移除手机
  高度补偿后，正文位置跳动约 180px；移除工具展开关联后，展开调用不再显示对应结果。
  以上临时改动均已恢复，最终验证通过。
- **Unresolved:** 用户提供的 neural_network PR #2 已手动关闭且未合并。关闭交付的现有
  生命周期会暂停并保留重开对账要求，但本地没有关联的线上失败请求或服务日志，无法
  确定 502 与卡住状态的根因。已覆盖空响应 502 后跨页签重试保留游标和快照的浏览器
  场景；该回归不能证明生产故障已解决。仍需关闭时刻附近的部署端请求、响应和日志。
- **Delivery:** 用户批准后完成上述五项本地实现，保留工作区已有改动；未提交、推送、
  创建 PR 或部署。关闭 PR 后线上 502 的修复仍受日志缺失阻塞。

### [048] — 工具活动改为 assistant 对话中的共享短栏

- **Requirement / supersedes:** 用户提供紧凑工具摘要截图，明确要求多次工具响应与
  assistant 正文放在同一段对话展示，并汇总到一个短栏。本项替代 [047] 的逐调用配对
  折叠方案；该方案虽减少了重复结果，仍留下多个工具框，未达到用户要求。
- **Implemented / decision:** `App.tsx` 按连续工具记录及执行归属划分短栏，
  `activity.tsx` 使用组内共享展开状态，默认只显示调用/结果数量；展开后按归档顺序
  显示各条详情，assistant 前后正文始终可读。用户消息、其他来源和不同 session /
  attempt / generation 截断分组，缺少明确归属的记录不自动合并；不跨 assistant 正文
  搬移工具记录，不从任意工具正文猜测文件或命令统计。移除旧的逐调用配对组件和样式。
- **Virtualization trade-off:** 保留原始分页、记录身份及最多 60 行挂载限制，不把整个
  工具序列塞进一个不受限组件。长序列只展示当前挂载片段，短栏明确标注“当前片段”；
  已缓存连续记录共享展开状态。原始归档及后端接口不变。
- **Validation / comparison:** 同一回归在上一版失败：找不到共享的“2 次调用 · 2 条结果”
  短栏；改动后通过。覆盖跨页交错结果、切换页签后恢复展开、键盘展开、手机宽度、
  不同会话/用户轮次之间的状态隔离。另用 140 条连续工具记录加末尾正文验证跨虚拟
  窗口展开和末尾可读，挂载行数不超过 60。前端格式、TypeScript/Vite 构建、2 项单元
  测试及全部 13 项 Playwright 回归通过；已检查桌面与手机的收起/展开截图。
  对比日志为 `.amp/in/artifacts/tool-bar-before.log`，最终日志为
  `.amp/in/artifacts/tool-bar-verification.log`，截图为 `tool-bar-*.png`。
  本轮只修改前端和文档，未重跑 Python；[047] 的线上 502 日志阻塞不变。
- **Delivery:** 本地实现与验证完成，未提交、推送或部署。

### [049] — 短栏内部按 call_id 合并调用与结果

- **Requirement / supersedes:** 用户进一步要求同 call_id 的调用和结果合并。本项替代
  [048] 在短栏内部逐条保留交错结果顺序的显示方式，保留其外层共享短栏和分组边界。
- **Implemented / trade-off:** 在同一执行归属、连续工具区间的当前挂载片段内，按非空
  call_id 将结果归到前面的对应调用下；交错返回时仍按调用顺序显示，每个调用包含参数
  和返回内容。匹配结果的原始行隐藏，不重复挂载正文；原始归档、分页及统计不改变。
  多个同 ID 结果按到达顺序保留；同 ID 再次出现调用时，后续结果归给最近一次调用。
  调用不在当前片段内、没有匹配调用或 call_id 为空时保留独立结果，避免丢失内容或
  跨用户消息/会话误配。结果的正文分页及编辑差异在合并区域内仍可使用。
- **Verification / comparison:** 新增的公开界面断言在上一版失败：call-a 区域内没有
  “工作目录干净”；实施后，call-a 与 call-b 各自包含正确的命令及结果，原结果行
  不再单独显示。覆盖跨页交错返回、不同会话重复 ID、用户新轮次重复 ID、空 ID 与
  孤立结果保留，以及长序列的 60 行挂载上限。格式、TypeScript/Vite 构建、2 项单元
  测试和全部 13 项 Playwright 回归通过；已检查桌面及手机配对后的展开截图。
  对比日志为 `.amp/in/artifacts/tool-pair-before.log`，最终日志为
  `.amp/in/artifacts/tool-pair-verification.log`；最终截图为 `tool-bar-*-expanded.png`。
- **Delivery / remaining:** 本轮只改前端和文档，未重跑 Python，未提交、推送或部署。
  [047] 中关闭 PR 后线上 502 的日志阻塞不变。

### [050] — 工具详情简化为调用行与结果引用

- **Requirement / supersedes:** 用户指定 `exec {command}` 下接 `> {result}` 的格式，
  替代 [049] 展开区域中的工具名称/ID 标题和单独“结果”标题，保留其 call_id 配对逻辑。
- **Implemented:** `ActivityBody` 在同一代码行显示工具名与命令/参数；返回内容使用
  原生 blockquote 和低强调左侧竖线，去除 call_id、重复标题及上下分隔线。原始输入文本
  不做猜测解析或删减；内部 ID 仍用于关联及来源查询。无法配对的结果也使用引用样式，
  保留独立入口、正文分页与差异查看。外层短栏、虚拟窗口及数据协议不变。
- **Validation:** 更新后的界面回归在上一版失败（调用行不含工具名），改动后通过；
  断言同行命令、结果引用块、无可见 ID/重复标题。格式、TypeScript/Vite 构建、2 项
  单元测试及全部 13 项 Playwright 回归通过，桌面与手机展开截图均已检查。仅为展示
  标记与样式调整，没有新增状态机制，不需要额外消融实验。
  对比日志为 `.amp/in/artifacts/tool-inline-before.log`，最终日志为
  `.amp/in/artifacts/tool-inline-verification.log`；最终截图为 `tool-bar-*-expanded.png`。
- **Delivery / remaining:** 仅本地前端与文档修改，未重跑 Python，未提交、推送或部署；
  [047] 的线上 502 日志阻塞不变。

### [051] — 关闭 PR 的权威对账、无 ID 时间线与固定来源限制

- **Evidence / supersedes:** 用户提供 `event=closed` 的归档元信息，但控制台仍显示
  awaiting_review。只读核查 neural_network PR #2 确实关闭且未合并，Issue #1 仍打开。
  真实 REST 返回中存在 merge_commit_sha，githubkit 生成的 PullRequest 模型却没有该字段，
  原读取路径触发 unexpected_response，对账保留旧的 OPEN 交付；issue.timeline 还包含
  没有 id/node_id 的 cross-referenced 事件，旧的键生成会令整个来源同步失败。
  这两项原因已复现，替代 [047]–[050] 对“卡住”原因尚未确定的结论；最初的线上 502
  仍没有对应失败接口、响应和日志，不能据此断言同因。
- **Implemented / decision:** PR GET 保留 SDK 的传输、认证及错误处理，从原始响应使用
  小型类型投影校验当前 PR 事实，保留 merge_commit_sha；字段缺失仍拒绝，不补造 null。
  不修改第三方 SDK、不添加依赖，也不从历史 closed 事件推断完整当前状态。
  关闭且未合并沿用已有生命周期，交付转 CLOSED、控制台 blocked / delivery_closed，
  Flow 仍 ACTIVE 并保留原绑定供重新打开后对账；不误报成功，不强制释放绑定。
  cross-referenced 缺少事件 ID 时使用引用来源身份与发生时间建立稳定键，编辑来源
  不改变键；其他缺少身份的异常仍拒绝。
- **Source UI:** 提交列表 250 条上限和线程没有远端变更时间加入固定历史限制，仍可在
  折叠来源明细查看，不再单独触发重试告警；真实同步失败仍告警并保留重试按钮。
  浏览器断言覆盖固定限制可查、无告警，以及叠加真实 issue.timeline 异常时只显示该错误。
  已检查 `closed-pr-sources-normal.png`、`closed-pr-sources-desktop.png` 和
  `closed-pr-sources-mobile.png`，默认入口紧凑，桌面/手机告警无重叠或横向溢出。
- **Before / after / ablation:** 捕获的真实响应在旧代码中重放，PR 读取与 issue.timeline
  均失败；修复后读到 closed / merged=False，Issue 与 PR 时间线分别读取 2 / 3 条。
  新的 4 个读取/时间线用例在旧实现失败，修复后相关 39 项通过。
  新增真实 HTTP 解码 + PostgreSQL 对账 + 控制台查询回归，确认 awaiting_review 转为
  delivery_closed；临时恢复 SDK parsed_data 后，该回归复现持久化对账失败，随后恢复修复。
  证据为 `.amp/in/artifacts/closed-pr-github-replay*.log`、
  `closed-pr-regression-before.log`、`closed-pr-regression-after.log` 和
  `closed-pr-without-response-decoder.log`；临时重放脚本删除，保留证据日志。
- **Validation:** 更新既有 PR GET 测试桩以提供原始 HTTP JSON，保留 review context、
  仓库重命名与合并证据的原断言。全量 Python 845 项通过，Ruff、Mypy（111 个源文件）
  和 diff 检查通过；Python 日志为 `closed-pr-python-final.log`。
  前端 `npm run format:check`、`npm run build`、2 项单元测试及 13 项浏览器回归通过。
  长工具回归此前会在倒数工具短暂可见后停止滚动，动态测量再改变列表高度时漏掉末尾正文；
  改为同时确认末条工具结果及最终正文可见后停止，保留 60 行挂载上限断言，额外并发重复
  3 次通过，日志为 `closed-pr-long-tool-repeat.log`。浏览器来源测试使用受控 API 响应，
  真实 GitHub 捕获只读；以上不代表线上已部署或原 502 已验收。
- **Delivery:** 仅本地实现与验证，保留工作区其他改动；未提交、推送、创建 PR、部署或
  修改线上 Flow。上线新版本并完成一次成功 PR 对账后，线上状态才能反映关闭事实。

### [052] — 已完成列改为已归档，并收纳关闭未合并的 PR

- **Requirement / supersedes:** 用户要求关闭的 PR 从受阻列移入结束事项分组，并将
  “已完成”改名为“已归档”。本项替代 [051] 中关闭 PR 显示 blocked 的归类决定，
  保留其当前 PR 对账、Flow 生命周期及原交付绑定要求。
- **Implemented / decision:** 保留内部 done 阶段和查询 token，统一看板、手机阶段切换、
  侧栏、详情与搜索提示的“已归档”文案。成功归档和关闭未合并共用该列，后者明确显示
  “已关闭 · 未合并”，不修改 Flow 为 succeeded、不派发归档执行，也不释放绑定。
  关闭事实优先于旧任务的失败或待应用结果显示为 delivery_closed，执行记录仍保留。
  is:done / status:done 查询归档分组，is:active 排除已归档卡片；重新打开后依据当前事实
  回到相应列。初始化仅刷新阶段与新规则不一致的既有投影，重复启动不重复发布。
- **Shared-contract boundary:** 全量测试发现 GitHub Projects 同步复用了 console_facts.phase，
  直接改归类会把关闭未合并错误同步为 Done。ProjectStore 明确保留其原有 blocked 语义，
  原终态优先级用例不修改预期并通过；控制台“已归档”仅为界面分组，不扩大成功语义。
  本次未运行真实 Projects 写入，测试使用隔离的远端替身。
- **Validation:** PostgreSQL 回归覆盖关闭后的归档计数、两种归档查询、活跃查询排除、
  原生命周期保留、旧投影刷新及幂等、重新打开移出归档。既有 PR HTTP 对账测试与 API
  关闭场景同步验证 done / delivery_closed，含旧任务失败及待应用结果。
  全量 Python 846 项通过，Ruff、Mypy（111 个源文件）及 diff 检查通过；日志为
  `.amp/in/artifacts/archived-python-verified.log`。
  前端格式、TypeScript/Vite 构建、2 项单元测试及全部 14 项浏览器回归通过；浏览器验证
  两种归档结果、搜索提示、桌面/手机归档列及关闭详情。已有长工具测试在异步测量完成前
  读到短暂的末尾节点，补充等待 ResizeObserver 后的绘制帧再断言，保留最终正文与 60 行
  上限断言，额外重复 10 次通过；日志为 `archived-frontend-verified.log`。
- **Visual evidence:** 已检查 `archived-board-desktop.png`、`archived-board-mobile.png`、
  `archived-detail-desktop.png` 和 `archived-detail-mobile.png`；列名、计数与两种结果
  正确，关闭详情保留 active / 已归档 和重新打开要求，未见重叠或裁切。
  浏览器使用受控 API 响应，生命周期、查询及投影另由真实 PostgreSQL 测试覆盖。
- **Delivery / remaining:** 仅本地实现与验证，未提交、推送、部署或修改线上状态。
  [051] 中原始线上 502 尚未关联请求及日志的限制不变。

### [053] — 在最新 main 上隔离本轮修复并准备后续 PR

- **Publication scope:** 用户要求更新 PR。只读核查已有控制台 PR 均已合并、没有开放 PR，
  因而以最新远端 main 建立独立发布 workspace，准备本轮后续修复 PR，不改已合并记录。
  只纳入 [042]–[052] 的控制台、归档展示、PR 读取及相关测试文档；原工作区保持不变，
  服务器更新脚本、部署与架构文档等其他未发布改动不纳入。
- **Upstream preservation:** 保留 main 上已合并的 GitHub push capability metadata 修复与
  测试，仅向当前测试文件追加 PR 读取回归，避免旧工作区覆盖新上游行为。
  控制台 README 的说明链接指向本功能记录，不依赖本次排除的架构文档更新。
- **Validation:** 发布候选在独立 workspace 上重跑全量 Python 836 项、Ruff、Mypy
  （111 个源文件）、前端格式/构建、2 项单元测试与 14 项浏览器回归，全部通过。
  此计数针对候选 PR 的源码，不包含原工作区未发布的服务器更新测试。
  日志为 `.amp/in/artifacts/pr-console-python.log` 和 `pr-console-frontend.log`。
  临时依赖链接在验证后移除，不进入发布 diff。
- **Delivery boundary:** 本记录写入时准备发布前独立审查；审查通过后才推送并创建后续 PR。
  不合并、不部署；原始线上 502 仍不具备已解决的证据，限制沿用 [051]。

### [055] — 2026-10-20 — CI 活动显示工作流、任务和状态（方案待批准）

- **Requirement:** 用户要求检查活动使用 `{workflow}: {job}: {status}`，例如
  `CI: check (3.10): success`。保留 `app.name` 作者、紧凑行、每个检查的独立身份、
  摘要正文与来源入口；不改变同作者分组及 Markdown 渲染。
- **Evidence:** 已只读核对真实 GitHub 响应：check run 提供任务名、status、conclusion
  和 check_suite.id，但没有 workflow 名。按该 suite ID 查询 Actions workflow runs，
  实际返回 `name=CI`；安装的 githubkit 原生接口支持 check_suite_id 过滤。
  [GitHub Actions API](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository)
  及 [GitHub App 权限表](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps#repository-permissions-for-actions)
  是接口和 Actions read 权限依据；生产 installation 是否已经授权尚未核实。
- **Proposed data flow:** 仅在既有检查分页中，为 GitHub Actions 的 check_suite.id
  查询真实 workflow 名，同页按 suite 去重；当前每页最多两个检查，因此最多增加两次
  有界只读请求。使用 suite 身份关联，不解析链接、不假设 job/check ID 相同、不硬编码
  `CI`。将名称补入检查元数据，保留归档 title 中的原始 job 名；界面组合显示三部分。
  状态优先使用非空 conclusion，否则使用当前 status，缺失时明确显示 unknown。
- **Incomplete data / errors:** workflow 名尚未取得时明确显示“未知工作流”，不从作者
  或任务名称猜测；可选补全遇到权限不足、远端缺失或暂时故障时保留已读检查及状态，
  在来源元数据注明原因，后续正常刷新可再次查询，不把暂时失败永久累计成历史缺口。
  补全请求须有短超时并遵守现有同步总预算。同步 webhook 映射保持无 I/O，收到原始
  检查时允许名称暂缺，后续既有 API 重扫补全；既有归档不会被宣称已自动补齐。
- **Implementation / validation after approval:** 依次补读取与元数据、前端格式、权限说明
  和公开接口回归。覆盖成功/失败、queued/in_progress、同 suite 去重、多 workflow、
  缺失名称与权限不足、webhook 先到和重扫补全；桌面及手机检查同行格式、摘要和来源
  入口，并运行相关 Python、前端单测、格式、类型/构建及浏览器回归。
- **Scope / risk / delivery:** 不新增依赖、表结构、同步来源、Webhook 订阅或长期缓存。
  额外请求会增加延迟与 API 配额消耗；App 的 Actions read 如需增加，由用户手动配置。
  本项目前仅记录本地方案，等待明确批准；尚未为本项修改产品代码或测试，未提交、
  推送、创建 PR、修改线上权限或部署。

### [056] — 2026-10-20 — CI 活动格式实现及独立发布候选

- **Requirement / implemented:** 用户批准 [055] 后实现 `{workflow}: {job}: {status}`，
  随后要求提 PR。检查作者使用 `app.name`，标题进入作者右侧的紧凑活动栏，保留原始 job
  title、摘要及来源入口；状态优先非空 conclusion，否则当前 status，缺失为 unknown。
  后端在既有分页中按 Actions suite 查询真实 workflow name，同页去重、共用 2 秒预算，
  外层取消继续传播。权限、限流和暂时故障阻止同页后续可选请求，原因进入元数据，
  不永久累计为历史限制。Webhook 不联网，后续 API 重扫补全同一检查身份。
- **Publication isolation:** 从最新远端 main 建立独立 jj workspace，仅迁入本线程 CI
  作者、紧凑标题、工作流补全和相关测试文档。保留 main 的 [053]；编号 [054] 对应
  [独立 Markdown PR #98](https://github.com/b1indsight/agentflow/pull/98) 的功能记录，
  此处不复制其作者分组、Markdown、代码换行、依赖或浏览器回归。原共享工作区不修改。
  不新增生产依赖、表结构、同步来源、Webhook 订阅或长期缓存；补充 Changelog 与 Actions
  read 权限说明。生产 installation 授权尚未核实，用户如需增加权限须手动配置。
- **Validation:** 发布候选运行全量 Python：438 passed、411 skipped，跳过项需要当前
  未配置的 PostgreSQL 测试数据库；Ruff 和 Mypy（194 个源文件）通过。前端锁定安装、
  Prettier、3 项单测、TypeScript/Vite 构建及全部 17 项浏览器回归通过。
  桌面和 390px 手机另检查 6 种 CI 状态、独立作者栏、无重复标题、摘要、来源入口与原因
  展开、无横向溢出，截图已检查。证据位于原工作区 `.amp/in/artifacts/` 的
  `ci-pr-python.log`、`ci-pr-frontend.log`、`ci-pr-visual.log`、`ci-pr-desktop.png` 和
  `ci-pr-mobile.png`。浏览器拦截 API，未宣称真实 App、数据库或线上部署验收。
- **Ablation / trade-off:** 与发布候选后端相同的实现已使用真实 GitHub JSON 经原生
  SDK/MockTransport 回放，同页 2 个同 suite job 只查询 1 次，移除去重后 2 次；
  两个 suite 遇到 403 时仅查询 1 次，移除同页故障阻止后查询 2 次。两次查询分别延迟
  1.2 秒，共用预算在 2.005 秒保留首个名称并中止第二次，移除预算后等待 2.422 秒。
  保留机制以限制重复请求与可选补全延迟，慢查询可能需要后续刷新才能补齐。
  原实验命令为 `PYTHONPATH=. uv run python .amp/in/verify-ci-workflow.py`，临时脚本
  已清理，输入样本留在原工作区 `.amp/in/`；结果为
  `.amp/in/artifacts/ci-workflow-sdk-validation.log`，另覆盖缺失名称、403/404/429/503、
  不匹配 suite、webhook 先到及外层取消传播。
- **Delivery boundary:** 本记录写入时发布候选已验证、等待独立审查；通过后按用户请求
  推送并创建 PR，不合并、不部署、不修改 GitHub App 权限。已有归档不保证立即补齐名称。

### [057] — 2026-10-20 — 修复工作流名补全恢复被历史去重拒绝

- 修复 #100 问题 1：检查补全 A→B→A 时，远端时间不变，恢复版本被全历史去重拒绝。
  检查改为仅对连续相同结果去重，保留旧版本和远端时间；不新增缓存或名称合并规则。
- 保留一条真实 PostgreSQL 回归，验证名称恢复及重复读取幂等；详细验证记录见
  [PR #101](https://github.com/b1indsight/agentflow/pull/101)。#100 问题 2 不在本次范围。
