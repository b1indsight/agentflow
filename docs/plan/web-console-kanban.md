# Web 控制台与 Flow Kanban — Feature record

**Stage:** Backend planning / 前端已通过 #86 合并；本轮后端方案待批准，尚未实现
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
