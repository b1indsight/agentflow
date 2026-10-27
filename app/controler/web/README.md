# Web 控制台

当前由 `flowtool serve` 在 `/console/` 提供，使用真实同源 `/api/`，**没有生产示例数据或 mock 回退**。
提供 cancelled Flow 的手动展示归档，以及受阻 Flow 的安全重试；其余工作流管理操作不在页面提供。
页面、静态资源和 API 均要求 Cloudflare Access 的单 owner 身份；数据库、GitHub 与 Worker
读取由 control 负责。真实 Access 登录/MFA、GitHub App 全来源回填和目标部署仍待验收。
架构与持久化边界见[控制台功能记录](../../../docs/plan/web-console-kanban.md)。
技术栈为 **React + TypeScript + Vite + Tailwind CSS + shadcn/ui**。
shadcn 的 Button / Input 源码放在 `components/ui/`，按需引入；不使用 CDN、全局状态库或请求缓存库。

## 运行与构建

在仓库根目录构建资源后启动 serve（需先准备 PostgreSQL 和 control 配置）：

```sh
python scripts/build_console.py
uv run flowtool serve
```

脚本执行锁定 npm 安装与生产构建，并将 `dist/` 复制到 `app/controler/console_static/`。
随后 `uv build --no-sources` 制作的 wheel/sdist 会包含资源；Docker 使用独立 Node 构建阶段，
运行 serve 不需要 Node。未构建资源时 `/console/` 返回明确的资源不可用错误，不回退到样例。
配置 `FLOWTOOL_ACCESS_ISSUER`、`FLOWTOOL_ACCESS_AUDIENCE` 和 `FLOWTOOL_ACCESS_OWNER_EMAIL`；
Tunnel/Access 将 `/console*` 与 `/api/*` 路由至同一服务并应用相同 owner 策略。
配置缺失会拒绝访问，详见 [Access 部署](../../../docs/deployment/access.md)。

### 前端开发

需要 Node 20.19+（20.x）或 22.12+，建议 Node 24。以下命令在本目录运行：

```sh
npm ci
npm run dev
```

开发服务器默认在 `http://127.0.0.1:5173/console/`，支持 React 快速刷新，
`/api` 代理至本地 serve 的 8000 端口。Vite 不生成 Access JWT，API 仍要求有效的 owner assertion；
单独启动 Vite 无法获得真实数据。开发模式为支持 HMR 移除了页面 CSP，只绑定 loopback；
Vite 页面不受 serve 的生产认证保护，不应公开部署，也不应在前端写入 token 或绕过后端验签。
源码 HTML 不能直接双击运行，也不能直接用普通静态服务器提供，须先经过 Vite。

检查生产构建：

```sh
npm run build
npm run preview -- --port 8767
```

构建结果位于 `dist/`，资源检查地址为 `http://127.0.0.1:8767/console/`；
`vite preview` 沿用本地 `/api` 代理，但不提供生产页面认证或签发 Access JWT，不能替代 serve 的端到端预览。
生产 CSP 允许同源 API，禁止内联脚本。源码目录的 `node_modules/`、`dist/` 和
`test-results/` 不进入 Python 分发包或版本控制；正式静态产物通过构建脚本单独复制打包。

## 页面与读取语义

- `/console/#board`：五列看板、组合查询、服务端分页和各阶段全集计数，PR 优先编号。
- `/console/#flow/<id>/conversation`：GitHub + Agent 时间线、工具摘要、Review 线程、下滑加载和末尾刷新。
- `/console/#flow/<id>/overview`、`/jobs`：概览、来源同步状态、当前动作/下一条件与执行记录。
- `/console/?q=repo%3Aagentflow+is%3Aactive+pr%3A95#board`：可分享、刷新后恢复的组合查询。

Flow 编号必须来自当前数据库；没有固定可用的样例 ID。GitHub 链接指向真实记录。
API 错误显式显示，已有内容不会被示例数据替换；Worker 离线仍可读已归档内容，未采集历史无法凭空恢复。

顶栏只有一个查询框，支持普通关键词、双引号短语、`repo:`、`is:active|done`、
`status:queued|running|review|blocked|done`、`issue:`、`pr:`；词和条件之间为 AND。
`done` 的展示名称为“已归档”，包含成功归档、PR 关闭未合并与取消后的手动归档；`is:done` / `status:done`
查询该分组，`is:active` 排除已归档卡片，不改变 Flow 的真实生命周期。
Enter 提交；聚焦或输入字段显示统一建议，方向键 / 点击选择，Enter 补入后再次 Enter 提交。
中文组合输入不提交；Escape 关闭建议，清空恢复全部结果，浏览器前进/后退恢复已提交查询。
无效、重复或未知条件提示错误，保留上一有效结果；不实现 GitHub 全套搜索语法。

cancelled 且仍在「受阻」列的 Flow 可在「概览」点击「归档」，持久移入「已归档」并注明
「已取消 · 已归档」及归档时间；生命周期仍为 cancelled，任务和会话历史不变。
此操作不创建清理任务、不修改 GitHub 或 Worker、不确认停止，也不绕过未决结果对账；
恢复执行仍须经过现有安全重试，恢复后自动清除展示归档。失败可重试，不提前移动卡片。
归档接口 `POST /api/flows/{id}/archive` 仅接受 owner 的同源 JSON 请求（空对象），
不存在返回 404，非 cancelled 返回 409，重复请求不重复记录；未授权和明确跨站请求拒绝。

受阻 Flow（包括尚未展示归档的 cancelled Flow）可在「概览 → 当前动作」点击「重试」。
`POST /api/flows/{id}/retry` 同样只接受 owner 的同源空 JSON，复用 `ControlService.retry`；
仍需满足停止确认、结果对账、PR 状态、活跃 Flow 冲突及原 Worker 归档绑定等检查。
没有可重试工作或安全检查未通过时返回 409，并在页面解释原因；不存在返回 404。
提交期间重试与归档互斥，受理后按已提交详情刷新分组，不提前移动卡片，也不承诺任务已运行。
网络异常可能发生在提交之后，页面会提示先刷新核对，不自动重发；切换 Flow 不受旧响应覆盖。
已展示归档的 Flow 不额外提供恢复按钮。范围与验证见[重试功能记录](../../../docs/plan/console-flow-retry.md)。

本次阅读固定本地已提交快照，编辑和补录在末尾显式刷新后才进入新快照；线程回复使用同一快照。
刷新活跃 Flow 会排队后台同步，响应不等待 GitHub/Worker 全部抓取；来源可能仍是
pending/syncing/partial/unavailable，“没有新增”只描述当前已归档快照。
已观察版本和删除标记永久保存，首次采集前的历史编辑/删除及远端已过期内容无法还原。
来源明细保留 GitHub 提交列表 250 条上限、线程缺少远端变更时间等固定限制；这些限制
不会因重试消失，不单独触发同步告警。真实的同步异常仍提示并提供重试。
PR 关闭且未合并后，经对账进入“已归档”列，卡片与详情注明“已关闭 · 未合并”；
Flow 保留活跃状态及交付绑定，重新打开并成功对账后回到相应列，不将关闭当作任务成功。
启动时更新阶段规则已变化的既有投影，不需要等待下一次 GitHub 事件；重复启动不重复发布。
“已编辑”按需展示相对前一正文版本的红删绿增 diff；元信息变化和删除不误报正文编辑。
完整 patch 由后端限定预算后一次返回，前端每窗口最多挂载 200 行，保持文本安全。
GitHub 末尾 effect 标记在展示读取时移除，原始归档不变；预览、全文分段和 diff 使用同一
展示正文。无正文事件显示紧凑动作摘要，标签名称直接可见。连续同来源、同作者的消息共用
头像、作者栏与来源定位，包含 developer/system/user；Worker 还必须有相同的执行、会话与
会话代次归属，已有 assistant/tool 输出分组保持不变。连续同作者的检查与无正文事件可跨来源
汇总为默认收起的「操作记录」，展开后保留每条动作、状态、时间、正文与来源信息；单条操作
保持直接可见。普通消息或不同作者会截断操作分组，评审线程、删除记录与不同历史归属
不合并；每条正文、来源链接及编辑记录独立保留。普通消息不显示 `message`
标题；连续工具调用和结果汇总成一条默认收起的
工具活动短栏，展开后按调用顺序查看详情，同 call_id 的结果合并到对应调用下，
工具名和命令/参数同行，返回内容在下方用引用块展示，不显示 call_id 或“结果”标题，
不重复显示独立结果条目；assistant 正文直接接续显示。切换到用户消息、其他来源
或不同执行归属会截断工具分组；缺少归属信息的 Worker 记录不自动合并。长序列仍按原始行窗口化，
仅挂载当前片段，短栏标明片段统计；调用不在当前片段内、call_id 为空或找不到对应调用的
结果保留独立入口，不为配对加载全部正文。
正文仅保留一套上一段/下一段导航，继续读取跨过预览边界；失败保留当前内容并重试原请求。
消息与线程回复在当前有界正文段内渲染 Markdown/GFM，支持标题、列表、链接、引用、代码与
表格；跨段语法按各段独立解析，不额外读取整篇正文。工具输出和 diff 保持原文。原始 HTML
仅显示转义文本，危险 URL 被过滤；图片显示为链接，不自动请求外部资源。代码块在可用宽度内
自动换行，保留原始缩进、换行和完整文本，连续长字符也能换行；宽表格在自身区域横向滚动，
均不撑宽手机页面。
普通相对链接（含片段）与图片地址按消息原始来源 URL 解析；来源缺失或无效时保留文字，
不将相对目标跳到控制台域名。GFM 脚注使用每条正文独立的引用、标题及回链 ID；点击或键盘
激活时仅在当前正文段内定位并转移焦点，不覆盖会话 hash 路由，也不打开新窗口。
脚注标签先映射为内部数字标识，避免 `a` 的重复引用与 `a-2` 标签生成相同引用 ID；
显示编号、脚注正文及每次引用的回链不变。仅脚注定义添加程序化焦点入口，回链保留原引用的
Tab / Shift+Tab 顺序。
个人阅读状态未落库，另见 [#88](https://github.com/b1indsight/agentflow/issues/88)。

## 字号与渲染上限

正文和卡片标题以 16px 为基准，侧栏 15px，控件/代码 14px，辅助信息 12–13px，
详情标题桌面 28px、手机 24px；手机不缩小正文。
手机详情滚动阅读后，固定详情头收成一行，保留返回、PR/Issue 编号和短标题；可手动展开，
回到顶部自动恢复。切换高度时补偿正文阅读位置，完整导航隐藏时不再接收焦点；桌面保持展开。

- 看板按当前窗口剩余高度布局，扣除顶栏、内边距、阶段选择和页脚后计算列高度，
  再扣除列标题和内边距得到卡片区高度；无固定 540px / 400px 最小高度。
  桌面五列独立滚动、标题固定；手机仅显示当前阶段，其余列保留测量以避免虚拟列表复显错位。
  打开详情再返回会恢复各列滚动位置和卡片焦点，手机切换阶段保留阅读位置。
- 看板各列使用独立滚动根，侧栏分组、执行记录、关键进展、会话、正文块、Review 回复使用各自页面的共享滚动根。
  每个列表最多挂载 60 条，包含焦点所在条目；小集合保持普通文档流。
- 正文每段最多 2048 字符，代码每段最多 8192 字符且不超过 200 行；提供前后段按钮，
  保留全部内容可达性，不把长单行静默截断。标题与元信息使用有界预览，详情正文可分段阅读。
- 仓库建议每组最多 8 条，可翻组。折叠状态与文本阅读段独立于 DOM 保存，卸载后返回仍保留。
- 每个 Flow 会话最多缓存 240 条活动，最多缓存 4 个 Flow；历史逻辑长度不因淘汰缩短，
  保留不透明页游标，在同一快照内重读被淘汰的正文。切换快照时重建分页并恢复临时阅读锚点，
  不混合新旧快照；缓存的分页错误返回后仍可重试。
- 虚拟列表关闭 React 适配器的强制同步更新，避免尺寸测量期间立即提交布局而触发
  ResizeObserver 循环告警；保留原有测量、窗口上限与阅读恢复，不屏蔽浏览器错误。

这些限制约束 DOM 和会话正文缓存；列表身份、页游标、虚拟测量和阅读状态元数据仍在内存，
不代表整个应用总内存固定。

## 文件职责

| 文件                                 | 职责                                               |
| ------------------------------------ | -------------------------------------------------- |
| `main.tsx` / `App.tsx`               | React 入口、URL、服务端筛选/分页、看板、侧栏与详情 |
| `api.ts` / `types.ts`                | 同源可取消请求、错误与控制台 DTO；不回退到 fixture |
| `activity.tsx`                       | 作者分组、结构化活动、安全 Markdown 与原文工具输出 |
| `DiffWindow.tsx`                     | 编辑 patch 的有界窗口、行号与行内高亮              |
| `useConversation.ts`                 | 快照分页缓存、请求取消/隔离、回读、显式刷新与重试  |
| `search.ts` / `SearchBox.tsx`        | 查询语法、匹配、输入法安全提交和有界筛选建议       |
| `windowing.tsx`                      | 有界变高列表、焦点遍历、文本分段和阅读状态         |
| `focus.ts` / `phases.ts`             | 可见可聚焦控件规则、共享状态名称与颜色目录         |
| `components/ui/` / `lib/utils.ts`    | shadcn/ui 组件源码与类名合并工具                   |
| `theme.css` / `styles.css`           | Tailwind 主题、已确认的布局与 GitHub 风格代码块    |
| `components.json` / `vite.config.ts` | shadcn 配置、Vite 开发/构建和本地别名              |
| `e2e/` / `playwright.config.ts`      | 浏览器回归；仅测试通过路由拦截提供 API DTO         |

后端合同见 [`app/contracts/console.py`](../../contracts/console.py)，
接口、同步与快照仓储见[控制台功能记录](../../../docs/plan/web-console-kanban.md)。

## 检查

在本目录运行，使用 lockfile 安装的开发工具：

```sh
npm run typecheck
npm run format:check
npm test
npm run build
```

首次运行浏览器回归需安装 Chromium，然后执行：

```sh
npx playwright install chromium
npm run test:e2e
```

Playwright 自动启动并关闭端口 5174 的隔离测试服务器，无需手工运行 Vite。
当前 20 条浏览器回归覆盖真实 DTO 展示、编辑 diff/大 patch 窗口、API 故障、
大时间线缓存淘汰/回读/刷新、固定快照的线程/Unicode 正文续读、刷新后的回复游标重置，
以及切换页签后缓存 502 分页错误的重试、统一 Unicode 正文导航、跨页工具短栏与轮次隔离、
长工具序列展开后的虚拟窗口上限、归档分组与关闭未合并的结果区分、
同作者跨页分组、Markdown 与手机长代码自动换行、消息内脚注往返和同名隔离、回链后键盘
Tab 顺序及脚注标签与重复引用后缀的冲突、
手机长标题收缩与阅读锚点保持，以及桌面/手机取消归档的失败重试、提交状态、分组更新和历史保留。
测试拦截 `/api/` 网络响应，组件、分页 hook 与窗口化均用正式实现；
回归还检查 window error 与 pageerror，包含 ResizeObserver 调度错误。浏览器回归不证明真实后端或 Access 可用。

CI 的 Frontend job 执行锁定安装、格式检查、正文安全和查询合同回归、类型检查/构建，
再安装 Chromium（Linux 使用 `--with-deps`）并运行浏览器回归。

浏览器验收重点：桌面/手机无横向溢出，侧栏折叠和分组、深链接/前进后退、中文输入、
筛选不丢失当前详情与工具展开状态、下滑分页、分界线有新增/无新增、阅读位置保留，
切换 Flow/页签时取消旧读取，手机导航键盘焦点及 Escape 返回。

本前端不代表真实 API、GitHub 覆盖或 Access 登录已经验收。完整设计变化见
[功能记录](../../../docs/plan/web-console-kanban.md)。
