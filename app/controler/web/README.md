# Web 控制台前端

当前仅实现前端，**全部为示例数据**，不连接数据库、GitHub、Worker 或现有会话 API。
`flowtool serve` 尚未挂载此页面；后端需求在 [#85](https://github.com/b1indsight/agentflow/issues/85)。
技术栈为 **React + TypeScript + Vite + Tailwind CSS + shadcn/ui**。
shadcn 的 Button / Input 源码放在 `components/ui/`，按需引入；不使用 CDN、全局状态库或请求缓存库。

## 本地预览

需要 Node 20.19+（20.x）或 22.12+，建议 Node 24。以下命令在本目录运行：

```sh
npm ci
npm run dev
```

开发服务器默认在启动服务的机器的 `http://127.0.0.1:5173/`，支持 React 快速刷新。
开发模式为支持 HMR 移除了页面 CSP，只绑定 loopback；此预览没有生产认证，不应公开部署。
源码 HTML 不能直接双击运行，也不能直接用普通静态服务器提供，须先经过 Vite。

检查生产构建：

```sh
npm run build
npm run preview -- --port 8767
```

构建结果位于 `dist/`，在启动服务的机器打开 `http://127.0.0.1:8767/`。
`dist/` 可由普通静态服务器托管，运行产物无需 Node 服务。生产构建保留严格 CSP，禁止 API 连接及内联脚本。
当前 Python 分发包和 Docker context 排除 `node_modules/` 与 `dist/`，避免把本机依赖和示例构建带入后端；
浏览器测试输出 `test-results/` 同样不进入分发包、Docker context 或版本控制。
正式构建产物托管、认证和镜像接入属于 #85，尚未实现。

- `/#board`：五列看板，仓库/生命周期筛选、搜索、PR 优先编号、当前动作。
- `/#flow/42/conversation`：GitHub + Agent 混合会话、代码/工具/Review 讨论、下滑加载、末尾刷新。
- `/#flow/42/overview`：概览及三项执行信息；`/#flow/42/jobs`：执行记录。
- `/#flow/40/conversation`：Worker 离线；`/#flow/37/conversation`：排队、尚无 Agent session。
- `/#flow/39/conversation`：成功归档；未知 Flow/路径有返回看板入口。
- `?q=repo%3Aagentflow+is%3Aactive+pr%3A95#board`：可分享、刷新后恢复的组合查询。

顶栏只有一个查询框，支持普通关键词、双引号短语、`repo:`、`is:active|done`、
`status:queued|running|review|blocked|done`、`issue:`、`pr:`；词和条件之间为 AND。
Enter 提交；聚焦或输入字段显示统一建议，方向键 / 点击选择，Enter 补入后再次 Enter 提交。
中文组合输入不提交；Escape 关闭建议，清空恢复全部结果，浏览器前进/后退恢复已提交查询。
无效、重复或未知条件提示错误，保留上一有效结果；不实现 GitHub 全套搜索语法。

第一次末尾刷新追加示例交互，再次刷新显示无新增；归档 Flow 始终无新增。
页面中的 GitHub 编号、时间、命令和测试输出均为虚构，链接只提示，不打开真实仓库记录。
Worker 离线样例保留已有历史，但新增只来自 GitHub。

## 字号与渲染上限

正文和卡片标题以 16px 为基准，侧栏 15px，控件/代码 14px，辅助信息 12–13px，
详情标题桌面 28px、手机 24px；手机不缩小正文。

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
  返回旧位置时按示例 offset 重读。真实后端反向游标和缓存一致性仍需 #85 定义。
- 虚拟列表关闭 React 适配器的强制同步更新，避免尺寸测量期间立即提交布局而触发
  ResizeObserver 循环告警；保留原有测量、窗口上限与阅读恢复，不屏蔽浏览器错误。

这些限制约束 DOM 和示例会话缓存；完整任务 fixture、虚拟列表测量和阅读状态元数据仍在内存，
不代表整个应用总内存固定或真实服务端分页已经实现。

## 文件职责

| 文件                                 | 职责                                                 |
| ------------------------------------ | ---------------------------------------------------- |
| `main.tsx` / `App.tsx`               | React 入口、URL、筛选、键盘/焦点、侧栏与会话页面     |
| `views.tsx`                          | 看板、导航、概览、详情头部与执行记录组件             |
| `activity.tsx`                       | 结构化活动组件，正文仅通过 React 文本节点渲染        |
| `useConversation.ts`                 | 示例分页缓存、请求取消/隔离、下滑读取与刷新状态      |
| `search.ts` / `SearchBox.tsx`        | 查询语法、匹配、输入法安全提交和有界筛选建议         |
| `windowing.tsx`                      | 有界变高列表、焦点遍历、文本分段和阅读状态           |
| `focus.ts` / `phases.ts`             | 可见可聚焦控件规则、独立于 fixture 的状态展示目录    |
| `fixtures.ts` / `types.ts`           | 示例数据、可取消读取和展示类型；不冻结真实 HTTP 契约 |
| `components/ui/` / `lib/utils.ts`    | shadcn/ui 组件源码与类名合并工具                     |
| `theme.css` / `styles.css`           | Tailwind 主题、已确认的布局与 GitHub 风格代码块      |
| `components.json` / `vite.config.ts` | shadcn 配置、Vite 开发/构建和本地别名                |
| `e2e/` / `playwright.config.ts`      | 浏览器行为回归、隔离的大数据与快速活动读取样例       |

以后接入后端时，替换示例数据入口并接入列表/详情/记录/活动接口，不保留隐式 mock 回退。
当前 offset 游标、样例发生时间和 blocks 不是最终 HTTP 契约；跨来源排序、编辑、补录、
来源级错误/完整性必须先按 #85 定义，再调整读取与合并逻辑。
生产预览的 CSP 使用 `connect-src 'none'` 阻止网络 API，请求真实 API 前需按同源认证方案审查修改。
看板当前在内存中筛选完整的八条 fixture，生产版须改为服务端过滤/分页和全集计数。

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
四条浏览器测试覆盖折叠分组后的手机焦点循环、千条列表的阅读状态及键盘顺序、
五列看板返回位置和焦点，以及 300 条会话的缓存淘汰/历史回读/尾部刷新。
每条测试同时断言 window error 事件与页面异常为空，并等待最后一次布局的测量回调；
ResizeObserver 调度错误也会使回归失败，不能只依赖行为断言或 Playwright pageerror。
测试专用 Vite 配置只替换活动读取 I/O；组件、分页 hook、缓存与窗口化代码均使用正式实现。
测试入口和模拟读取不被生产入口导入，不进入 Vite 生产产物。

CI 的 Frontend job 执行锁定安装、格式检查、正文安全和查询合同回归、类型检查/构建，
再安装 Chromium（Linux 使用 `--with-deps`）并运行上述四条浏览器回归。

浏览器验收重点：桌面/手机无横向溢出，侧栏折叠和分组、深链接/前进后退、中文输入、
筛选不丢失当前详情与工具展开状态、下滑分页、分界线有新增/无新增、阅读位置保留，
切换 Flow/页签时取消旧读取，手机导航键盘焦点及 Escape 返回。

本前端不代表真实 API、GitHub 覆盖或 Access 登录已经验收。完整设计变化见
[功能记录](../../../docs/plan/web-console-kanban.md)。
