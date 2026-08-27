# 项目文档完整同步计划

> 状态：已实施 · 计划/实施日期：2026-08-27 · 基线：`main@origin` (`9cca7f67`)
>
> 本计划整理项目文档及其入站引用，不改变运行逻辑、配置、依赖或对外行为。实施前需在 draft PR
> 上获得明确批准。

## 1. 目标与边界

M0 已完成并验收，但当前文档仍同时承担产品入口、当前实现说明、目标架构和历史决策记录，部分表述
没有明确区分“已经实现并验证”与“M1+ 设计目标”。此外，可视化中的组件统计少计一个已完成节点、
图例存在重复项，两个 M0 计划也尚未完整记录最后一轮 worker heartbeat 修复。

本次目标是按当前文档规则完成一次全量同步：

- 以代码、测试、配置示例和已执行的运行验证为当前事实来源；
- 让 README 成为清晰的项目入口，直接说明 M0 能做什么、不能做什么以及从哪里继续阅读；
- 让架构正文明确区分当前实现、已验证边界和 M1+ 目标设计；
- 将架构文档和计划文档分别组织到 `docs/architecture/`、`docs/plan/`，各自提供唯一目录入口；
- 保留计划文档的原始背景和决策，只追加实际结果与实质偏差，不把历史改写成当前说明；
- 让 HTML 可视化与架构正文、测试数量和组件状态一致。

明确不在本次范围内：

- 修改任何 Python 运行逻辑、测试行为、依赖、环境变量或运行行为；文档迁移所需的源码注释和
  docstring 路径引用会机械同步；
- 实现或验证 GitHub、bub、Codex push、reconcile、插件等 M1+ 集成；
- 新建 changelog、独立文档站、文档生成器或 CI；仓库目前没有这些既有体系；
- 把目标架构删减成只描述 M0，或重写历史计划的计划阶段内容。

## 2. 文档角色与更新方案

### 2.1 README：项目入口与当前状态

- 将开头的闭环描述标为产品目标，紧接着给出 M0 当前能力和 M1+ 未完成边界，避免把目标当成可用功能。
- 增加精简的文档导航，分别指向 `docs/architecture/README.md` 和 `docs/plan/README.md`，
  不在项目入口重复维护子文档清单。
- 把端口与 adapter 表改成带实现状态的当前清单；桩、部分实现和已验证能力使用与架构文档一致的术语。
- 保持快速开始和验证命令可复制，补足 `.env`、外部凭据及当前只能做 M0 smoke 的限制说明。
- 继续集中维护“已验证/待验证”，不在其他入口文档重复一套不一致的事实。

### 2.2 architecture 目录：当前事实与目标设计

将当前单文件拆成以下结构：

```text
docs/architecture/
  README.md          # 架构入口、当前状态、阅读导航
  overview.md        # 定位、运行形态、平面边界与组件总览
  components.md      # 组件职责与实现状态
  workflows.md       # 端到端时序和 Flow 状态机
  decisions.md       # 关键决策、风险、安全与已知短板
  roadmap.md         # 里程碑和未来扩展
  github-app.md      # GitHub App 接入
  review-loop.md     # review / triage / revise 循环语义
  diagram.html       # 同步的交互式架构可视化
```

`README.md` 是唯一架构入口；各主题文件只维护各自权威内容，通过链接关联而不复制正文。
迁移后删除顶层 `docs/ARCHITECTURE.md` 和 `docs/architecture.html`，并一次性更新 README、计划文档、
源码注释和 docstring 中的旧路径/章节引用，不保留两套并行入口。

- 在文首增加“如何阅读本文件”和实现状态图例，说明正文是目标架构，状态标注才代表当前落地程度。
- 对照 `app/` 和 `tests/` 校正关键事实：入口/路由/worker 的 M0 覆盖，reconcile 与外部 adapter 的桩边界，
  Codex/Bub 的实际调用方式，以及尚未落地的串行化、外部副作用与运维能力。
- 保留既有架构决策、路线图和附录；对未来态使用明确措辞，不把设计能力写成已经验证的行为。
- 删除重复条目，统一术语、状态日期、测试数量和交叉引用。

### 2.3 plan 目录：保留历史，补记结果

计划文档统一放在：

```text
docs/plan/
  README.md                  # 计划索引、状态和文档角色说明
  m0.md                      # 原 M0 落地计划
  m0-closeout.md             # M0 收尾与验收计划
  documentation-refresh.md   # 本计划
```

`README.md` 是唯一计划入口，按“待批准 / 已批准或实施中 / 已完成”标明状态。迁移后删除顶层
`docs/M0-PLAN.md` 与 `docs/M0-CLOSEOUT-PLAN.md`，同步全部相对链接。

- `m0.md` 保留原范围、构建顺序和 `TODO(verify)` 表，只在实施结果中补记最终 19 项测试、
  heartbeat 异常隔离与瞬时失败重试，并记录与原计划之间值得保留的实现差异。
- `m0-closeout.md` 将计划制定时的“当前”语境标清，补记审查后修复与最终验证结果；不回写计划阶段方案。
- 两份计划互相链接并指向当前事实文档，避免读者把历史计划当作最新操作指南。

### 2.4 diagram.html：手工维护的同步可视化

- 明确它是 `docs/architecture/README.md` 的当前状态可视化，不是生成产物或独立事实来源。
- 修正组件统计、重复图例、测试数量和节点状态；统一“写实 / 部分 / 桩 / 将来”的判定。
- 同步 M0 验收、M1+ 边界、页脚链接与状态日期，并保持现有交互、主题和响应式布局不变。

`env.example` 已与 `app/config.py` 的变量、默认值和说明一致，本次预计不修改；实施时仍会重新核对。

## 3. 计划中的 change stack

获批后在本计划变更之上创建两个有序的文档子变更：

1. **`docs(structure): organize architecture and plans`**
   - 建立两个目录入口，按主题迁移架构正文、HTML 可视化和历史计划；
   - 更新 README、Markdown、Python 注释/docstring 内的所有入站引用并删除旧顶层文件；
   - 这是纯结构迁移，保持文档语义和程序行为不变，可独立审阅与回滚。
2. **`docs: align project documentation with M0 reality`**
   - 同步 README、架构正文、HTML 可视化和两份 M0 决策记录；
   - 共同完成“当前事实与目标设计分层”这一项语义更新，不按文件类型拆分；
   - 依赖前一项的新目录结构，但可独立复审与回滚。

若实施核对发现新的、与文档同步无关的产品问题，只记录为后续事项，不扩大本 PR。

## 4. 验证策略

实施完成后执行以下检查：

- 运行 `uv run pytest`，以实际收集/通过数量校验文档中的测试统计；
- 运行 `uv run ruff check app tests` 与 `uv run mypy app tests`，确认文档仍引用真实的验收基线；
- 运行 `uv run flowtool --help`，核对 README 中的 CLI 入口和命令名；
- 检查所有本地 Markdown 链接、HTML 文档链接和标题引用均可解析；
- 用 HTML 解析检查确认 `diagram.html` 结构有效，并核对状态节点计数与页面统计一致；
- 搜索 `docs/ARCHITECTURE.md`、`docs/architecture.html`、`docs/M0-PLAN.md`、
  `docs/M0-CLOSEOUT-PLAN.md` 等旧路径，确认没有残留入站引用；
- 全仓搜索旧测试数量、重复段落、退役术语和相互矛盾的“已实现 / 待验证”表述；
- 最后同时对照代码、测试、配置示例和完整文档 diff 复读。

本次不重复真实 GitHub/Codex/bub 联网验证，也不因此提升相应组件的状态。

## 5. 文档影响

预计新增目录入口并迁移：

- [`../../README.md`](../../README.md)：当前状态、快速开始、能力清单和两个文档目录入口；
- `docs/architecture/`：架构索引、主题文档与 `diagram.html`；
- `docs/plan/`：计划索引、两份 M0 决策记录与本计划；
- `app/` 中引用旧文档路径/章节的注释和 docstring：仅机械更新链接，不改变运行代码。

删除迁移后的四个顶层旧文件：`docs/ARCHITECTURE.md`、`docs/architecture.html`、
`docs/M0-PLAN.md`、`docs/M0-CLOSEOUT-PLAN.md`。不会修改 `env.example`，除非实施核对发现它与
当前配置 schema 确有差异；不会新增 changelog 或独立文档站。

## 6. 实施结果

计划范围已完成：

- 建立 `docs/architecture/README.md` 与 `docs/plan/README.md` 两个唯一目录入口；
- 将原架构正文拆为 overview、components、workflows、decisions、roadmap、github-app 和
  review-loop，并把交互图迁为 `diagram.html`；
- 将两份 M0 计划迁为 `m0.md`、`m0-closeout.md`，保留原计划正文并追加最终审查结果；
- 删除四个顶层旧文件，同步 README、Markdown、`env.example`、Python 注释和 docstring 的入站引用；
- 以代码和测试为事实源，明确 M0 当前能力、Flow Engine/外部 adapter 的真实边界和 M1+ 目标设计；
- 依据 GitHub 官方文档核对 App JWT、installation token、最小权限和 Webhook HMAC 说明。

实施没有修改 Python 运行逻辑、测试、依赖或配置 schema。`env.example` 仅机械更新了架构文档路径
注释；这是目录迁移的入站引用同步，不是配置变更。

计划阶段关于可视化“少计节点、重复图例”的观察在最新基线上未能复现：原图的 13/3/2 数量与节点
一致，图例也只有一项 missing。实施没有制造无依据修复，而是按新的状态定义重新核对每个组件，最终
分类为 9 个已实现、6 个部分、3 个桩，并在图中明确“实现状态不等于端到端验收”。

最终验证：

- `uv run pytest`：19 项通过；
- `uv run ruff check app tests`：通过；
- `uv run mypy app tests`：27 个 source files 无问题；
- `uv run flowtool --help` 与 `uv run flowtool flow --help`：通过；
- 所有本地 Markdown 链接目标存在；HTML tag 嵌套平衡；可视化节点计数与 9/6/3 页面统计一致；
- `git diff --check` 通过，除本计划中的历史迁移描述外没有旧文档路径或旧测试数量残留。

仓库仍没有 changelog、文档生成器或 CI，本次没有新建这些体系。真实 GitHub/Codex/bub 联网集成
按计划未执行，相应组件状态没有提升。
