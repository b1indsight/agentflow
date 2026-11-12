# 标准 Tabs 组件 — Feature record

已复用现有 Radix Tabs，提供标准组合式组件。新增标签只需登记标识与标题并提供对应内容，不再维护固定数量的键盘切换逻辑。初始方案保留在下方；实际实现和验证限制见时间线。

**Stage:** 实现、完整本地复验和独立代码审查完成；远端 CI 待确认，保留基线滚动测试偶发失败的记录
**PR:** [#142](https://github.com/b1indsight/agentflow/pull/142)，分支 `feat/console-tabs`；未合并或部署

## 1. Initial plan

### Summary and motivation

用户希望将“概览 / 会话 / 执行记录”抽象为标准标签组件，为后续增加标签做准备。

当前 [App.tsx](../../app/controler/web/App.tsx) 分别在路由正则、按钮列表和键盘处理器中枚举三个标签，键盘切换还写死 `% 3`；内容区使用三元表达式，最后一支隐式对应执行记录。增加标签容易漏改其中一处。[types.ts](../../app/controler/web/types.ts) 另行维护 `Tab` 联合类型。

[package.json](../../app/controler/web/package.json) 已依赖 `radix-ui`，现有 [Button](../../app/controler/web/components/ui/button.tsx) 使用该包的原语，可以沿用现有组件目录和 props / className 约定，不新增依赖。

### Goals, scope, and success criteria

- 提供可复用的 `Tabs`、`TabsList`、`TabsTrigger`、`TabsContent`，使用 Radix 的原生 props，不另造业务专用 Tabs 协议。
- 通用组件管理标签交互、选中样式、焦点和标签与面板的 ARIA 关联，不读取 Flow、hash 或业务数据。
- 保留绿色文字和下划线、灰色非选中项、桌面与手机布局；标签增多时允许标签栏横向滚动，不撑宽整页。
- Flow 页继续通过 URL 控制选中项，保留直接访问、刷新、前进和后退行为。
- 不新增实际业务标签，不加入关闭、拖拽排序、持久化标签、动态权限或插件注册机制；不修改后端。

### Proposed behavior

- 原有三个标签、默认会话页和 `#flow/{id}/{tab}` URL 保持兼容；无效标签维持现有路由不匹配行为。
- 鼠标和触摸选择标签；左右方向键移动焦点并激活标签，支持首尾循环及 Home / End。禁用项遵循 Radix 的标准交互。
- 内容区与所选标签通过 `aria-controls` / `aria-labelledby` 关联，支持键盘进入内容区。
- 切离会话仍卸载会话内容并取消旧请求；进入执行记录时才加载对应数据。现有错误展示和重试入口不变。

### Technical design and affected modules

**`app/controler/web/components/ui/tabs.tsx`（新增）：** 组合式样式组件，沿用 `React.ComponentProps`、`data-slot` 和 `cn` 约定。导出接口对应现有依赖的 `Tabs.Root`、`Tabs.List`、`Tabs.Trigger`、`Tabs.Content`：

- `Tabs(props: React.ComponentProps<typeof TabsPrimitive.Root>)`：共享选中状态，支持受控 `value` / `onValueChange` 及原语已有的非受控用法。
- `TabsList(props: React.ComponentProps<typeof TabsPrimitive.List>)`：横向标签栏和键盘导航容器。
- `TabsTrigger(props: React.ComponentProps<typeof TabsPrimitive.Trigger>)`：通过必填 `value: string` 标识标签，封装现有选中外观。
- `TabsContent(props: React.ComponentProps<typeof TabsPrimitive.Content>)`：通过必填 `value: string` 关联内容，保持原语语义。

这些组件透传原语 props，不复制 Radix 的键盘、ID 或焦点实现，不新增自定义类型和配置选项。通用样式集中在组件所属选择器；详情页独有的顶部间距留在页面样式中。

**`app/controler/web/App.tsx`：** 在当前业务模块定义只读标签列表，字段为 `value`（稳定字符串标识）和 `label`（显示标题），从列表推导本地 `Tab` 类型。按钮渲染和路由合法性检查复用此列表，删除固定三项的键盘处理器及路由枚举。内容使用 `Record<Tab, ReactNode>` 显式按标识对应，避免新增标签被隐式当作执行记录；新增标识而未提供内容时应由类型检查报错。它只是当前页面的内容映射，不扩展为通用配置引擎。

`Detail` 使用受控 `Tabs`，`onValueChange` 更新现有 hash，继续由 App 的 `hashchange` 监听同步路由。通过 `asChild` 复用现有 `main.detail`，避免增加影响 flex 布局的 DOM 层。

保留 `.detail-body` 滚动容器及其 ref。页面只渲染当前内容，并以单个始终选中的 `TabsContent value={tab}` 关联现有 `section.content`。不使用 `forceMount` 缓存后台面板，也不让新容器改变会话卸载、任务请求取消和滚动恢复时序。未来其他调用处仍可正常组合多个 `TabsContent`。

**`app/controler/web/types.ts`：** 删除只由当前页面使用的独立 `Tab` 联合类型，避免与本地标签列表重复维护；其余 DTO 不变。

**`app/controler/web/styles.css`：** 将现有标签外观归到通用组件，保留页面间距和手机差异；为超出可用宽度的标签栏提供横向滚动，检查焦点轮廓不被裁切。

**`app/controler/web/e2e/console.spec.ts`：** 在现有真实组件与受控 API 场景中补充标签、焦点、ARIA 和历史导航检查，复用已有会话及执行记录回归，不创建另一套业务 fixture 框架。

### Alternatives and rationale

- 只抽出按钮列表：改动较少，但仍需自行维护焦点、键盘及面板关联，不能完成标准组件抽象。
- 手写通用 Tabs：会重复项目已有依赖提供的无障碍交互，维护成本更高。
- 全量标签配置引擎：容易将路由、加载和阅读状态耦合进共享 UI；本次采用组合式组件和页面自己的简单内容映射。
- 每个业务标签始终挂载一个内容组件：可能改变现有取消请求和阅读状态语义；本次继续只挂载当前业务内容。

### Costs, risks, and mitigations

- Radix 默认的鼠标按下和焦点激活时机与现有 click 处理不同；使用浏览器验证实际 URL、选中项和焦点保持一致。
- 内容面板可聚焦会改变 Tab 键的路径；检查从标签栏进入内容及继续访问内部控件的行为。
- 通用组件的 DOM 和样式迁移可能影响手机紧凑标题、滚动恢复及虚拟列表；保留布局节点和业务生命周期，并运行已有回归。
- 多标签横向滚动可能裁切焦点或下划线；桌面、手机和超宽标签列表都需渲染检查。

### Implementation order

1. 添加基于 Radix 的标准 Tabs 组件及现有视觉样式。
2. 整合标签列表和类型，改造路由与内容映射，接入受控 Tabs，删除原有键盘处理器。
3. 补充必要的浏览器交互回归，验证现有会话、任务分页和手机紧凑标题。
4. 运行前端类型检查、组件测试、生产构建、格式检查及 Playwright；检查桌面与手机截图。

不需要数据迁移或后端 rollout。回滚时恢复本次组件、页面、样式和测试改动即可，不改变原有 URL 或持久数据。

### Validation and ablation strategy

以下均为实施后的验收计划，尚未执行：

- 依次选择三个标签，核对可见内容、唯一选中项、hash 和浏览器前进 / 后退；检查缺省和无效标签路径。
- 使用真实键盘检查方向键、Home / End、首尾循环、焦点与选中项同步及 Tab 进入内容区；检查选中标签和面板的双向 ARIA 关联。
- 复用现有会话切换、阅读位置恢复、过期请求取消、执行记录加载更多和手机标题折叠回归。
- 临时组件场景使用超过三个标签，并包含禁用项，验证循环和跳过禁用项不依赖固定数量；窄屏检查横向滚动、末项可达和整页不溢出，不在产品中新增示例标签。
- 在 1440px 和 390px 检查三个选中状态的外观并保存截图，核对绿色下划线、间距及内容区布局。
- 在 `app/controler/web` 执行 `npm run typecheck`、`npm test`、`npm run build`、`npm run format:check` 和 `npm run test:e2e`。
- 本次复用已有原语，不新增缓存、调度或状态协调机制，不需要性能消融实验。扩展性负向检查是在临时副本中增加标签声明但不提供内容，确认类型检查拒绝不完整映射；随后清除临时文件。

### Open questions and deferred work

方案不依赖新的外部信息。实施前需要用户确认；提交、推送和创建 PR 仍需单独获得授权。多标签仅指组件能承载更多条目，具体新增什么业务标签留待后续需求。

## 2. PR change timeline

### [001] — 2026-11-12 — 接入标准组件和单一标签声明

- **Context / decision:** 用户希望标准化标签栏，为增加标签做准备，并已确认初始方案。沿用已安装的 Radix 和现有 UI 包装约定，不新增依赖或配置引擎。
- **Implemented changes:**
  - 新增 [tabs.tsx](../../app/controler/web/components/ui/tabs.tsx)，导出 `Tabs`、`TabsList`、`TabsTrigger`、`TabsContent`，透传原语 props。
  - [App.tsx](../../app/controler/web/App.tsx) 通过只读 `flowTabs` 同时驱动标签渲染、路由合法性检查和本地类型；使用 `satisfies Record<Tab, ReactNode>` 检查内容映射完整性。删除固定三项的键盘处理器和 [types.ts](../../app/controler/web/types.ts) 中重复维护的类型。
  - 用 `asChild` 保留现有 `main.detail` 和 `section.content`；单个始终选中的内容面板只渲染当前业务内容，保留 `.detail-body`、ref 和原有加载 / 卸载行为。
  - [styles.css](../../app/controler/web/styles.css) 迁移选中外观，增加横向溢出和焦点样式；页面顶部间距仍属于详情页。内边距与负外边距为末项焦点框留出空间，保持文字对齐。
- **Options / consequences:** 不使用 `forceMount`，也不让所有业务面板常驻；标准原语负责交互和 ARIA，业务模块继续拥有路由与请求生命周期。当前业务仍只有三个标签。
- **Evidence / validation:** 在现有 [浏览器测试](../../app/controler/web/e2e/console.spec.ts) 中先加入面板命名和焦点回归，旧实现因缺少面板可访问名称而失败，替换后桌面和手机场景通过。检查覆盖点击、方向键、首尾循环、Home / End、Tab 进入面板、历史导航、刷新和无效标签路径。

### [002] — 2026-11-12 — 扩展验证及基线对照

- **Evidence / validation:**
  - 在 `app/controler/web` 执行 `npm run typecheck`、`npm test`、`npm run build`、`npm run format:check` 及 `npm run test:e2e -- --grep 'standard tabs' --workers=2`，均通过。构建仍提示主包超过默认大小阈值；本次未调整打包策略。
  - 临时 Vite 内存变换在真实页面加入额外标签及禁用项，使用 Playwright 检查超过三项时的路由与内容、跳过禁用项、首尾循环、Home / End、窄屏末项可达及整页不溢出，全部通过。未将示例标签加入产品；临时配置与脚本验证后删除。
  - 通过 TypeScript 编译器的临时读取覆盖增加标签声明但不提供内容，确认类型检查拒绝不完整映射，未改动实际源文件。
  - 在桌面和手机渲染三个选中状态并检查截图；另外检查多标签横向滚动后的键盘焦点。截图收录于[实现记录](https://ampcode.com/artifacts/tabs-5ab14d0f-88b6-4309-b2a7-849fbb1e96d2)。
  - 全量 `npm run test:e2e -- --workers=4` 曾通过，但后续执行中 `large activity stays windowed, re-reads evicted pages, and refreshes explicitly` 再次出现间歇性失败：快照刷新后采样偏移为 282.890625px，预期小于 2px。
  - 通过临时 Vite load 钩子从 `git show HEAD:app/controler/web/App.tsx` 和对应 `styles.css` 读取改动前代码，其他业务模块与测试保持相同；从前端目录执行完整基线回归，排除新增的 `standard tabs` 场景，使用四个 worker。同一滚动测试出现完全相同的偏移，其余基线用例通过。失败上下文和 Playwright trace 保存在[实现记录](https://ampcode.com/artifacts/tabs-5ab14d0f-88b6-4309-b2a7-849fbb1e96d2)中。
- **Ablation:** 本次没有新增缓存或状态协调机制，直接复用标准原语，不需要性能消融。扩展性通过额外标签和不完整映射负向检查验证。
- **Outcome / pending:** Tabs 本地实现和专项检查完成。全量浏览器回归并非稳定全绿；基线对照证明该滚动失败在替换前也存在，具体原因尚未定位，本次未扩大范围修改滚动逻辑。未提交、推送、创建 PR 或部署。

### [003] — 2026-11-12 — 独立 PR 范围和完整复验

- **Implemented changes:** 从最新 `main@origin` 建立独立 jj 工作区，仅带入 Tabs 实现、浏览器回归、feature record 和 Changelog；原工作区的其他后端、测试及文档修改未纳入本次 PR。补充控制台 Tabs 的变更说明，不新增示例标签或文件读取功能。
- **Evidence / validation:** 在隔离工作区执行 `npm ci` 后，从前端目录运行 `npm run typecheck`、`npm test`、`npm run build`、`npm run format:check` 及 `npm run test:e2e -- --workers=4`，本轮全部通过。
- **Outcome / pending:** 本轮完整回归通过不代表 [002] 中的基线偶发失败已修复；该问题未改动，仍作为已知验证限制保留。待完成独立代码审查并发布同一功能分支的 PR，未部署。
