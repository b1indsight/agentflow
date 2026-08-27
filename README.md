# FlowTool（代号，未定名）

FlowTool 的目标是把带 `flow` 标签的 GitHub issue 编排成
`code agent 编码 → PR → 人工审核 → 自动回喂修订 → 人工合并 → 归档` 的 headless 闭环。
项目自身不提供业务 UI，交互面是 GitHub issue 和 PR。

> 当前状态：**M0 骨架已验收（2026-08-26）**。仓库已验证配置、持久化、Webhook 入口、
> 事件路由与 worker 恢复基础；真实 GitHub/Codex/bub 闭环属于 M1+，目前不能按生产能力使用。

## 当前能力

M0 已经具备并验证：

- 加载 `FLOWTOOL_` 配置，初始化 SQLite 的 flow、attempt、job、event、run 和 review 数据；
- 校验 `X-Hub-Signature-256`，异步交接 Webhook，并处理 delivery 去重、allowlist、取消和 review 入库；
- 用进程内 worker claim/dispatch 持久化 job，维护 heartbeat，启动时恢复过期的 running job；
- 通过 CLI 启动 daemon、查看 flow，并执行基础 cancel/retry 运维命令；
- 提供 Flow Engine、Codex、GitHub、Judge、Sandbox 和插件接缝，供后续里程碑补全。

尚未形成可用闭环：GitHub App API 尚未校准，issue 正文和 review thread 映射仍是桩，bub 调用、
Codex commit/push、网络与 scoped token、reconcile 对账、取消运行中进程和真实多轮修订均未端到端验证。
完整边界见[架构状态表](docs/architecture/README.md#当前实现状态)。

## 文档

- [`docs/architecture/`](docs/architecture/README.md)：当前实现状态、目标架构、组件、流程、决策和接入说明；
- [`docs/architecture/diagram.html`](docs/architecture/diagram.html)：手工维护的交互式架构可视化；
- [`docs/plan/`](docs/plan/README.md)：计划、批准状态和实施结果记录。

## 平面边界

- **Orchestrator / GitHub API 平面**：Webhook、状态机、持久化、GitHub API、Judge 与 prompt 组织；
- **Code Agent / 本地平面**：在隔离工作区内改码、commit、branch/bookmark 和 push；
- **本地三轴**：workspace 负责目录隔离，Sandbox 负责可写根与网络策略，VCS 只通过 git/jj 指引交给 agent。

VCS 不是端口；详细设计与当前限制见 [`components.md`](docs/architecture/components.md) §4.6。

## 快速开始

要求 Python 3.12+ 与 uv：

```bash
uv sync --locked
cp env.example .env
# 先编辑 .env：替换 change-me、your-username 和其他占位值
FLOWTOOL_HOST=127.0.0.1 uv run flowtool serve
```

不要用示例 Webhook secret 或 allowlist 启动可公开访问的服务；对外暴露端点前必须生成独立 secret，
填入真实 allowlist，并配置 HTTPS/反向代理。启动 M0 daemon 和 `/healthz` 不等于 GitHub/Codex/bub
集成已经可用；接入外部服务前请先阅读 [GitHub App 接入说明](docs/architecture/github-app.md)和
[已知短板](docs/architecture/decisions.md)。

常用运维入口：

```bash
uv run flowtool --help
uv run flowtool flows --active
uv run flowtool flow show <FLOW_ID>
uv run flowtool flow cancel <FLOW_ID>
uv run flowtool flow retry <FLOW_ID>
```

## 验证基线

```bash
uv run pytest
uv run ruff check app tests
uv run mypy app tests
uv run flowtool --help
```

截至 2026-08-26，19 项自动化测试覆盖配置/契约、SQLite、HMAC、health、异步交接、delivery 去重、
allowlist、取消与 review 路由，以及 worker dispatch、失败、heartbeat 清理/重试和启动恢复；测试隔离
GitHub、Codex、bub、真实凭据与轮询等待。`uv sync --locked`、Ruff、Mypy、CLI help 和真实
`flowtool serve` + `/healthz` smoke 均已通过。

另有 2026-08-01 的独立实测证明 Codex `exec` / `resume` 上下文连续，`workspace-write` 下 jj `.jj`
可写并能 commit；git `.git` 写入被拦截，push 与凭据注入未验证。这些结果不能替代 M1+ 端到端验收。
