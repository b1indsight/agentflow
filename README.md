# FlowTool（代号，未定名）

FlowTool 的目标是把带 `flow` 标签的 GitHub issue 编排成
`code agent 编码 → PR → 人工审核 → 自动回喂修订 → 人工合并 → 归档` 的 headless 闭环。
项目自身不提供业务 UI，交互面是 GitHub issue 和 PR。

> 当前状态：**M0 骨架已验收，多 worker 调度、租约恢复与结果重放已实现（2026-09-02）**。仓库已验证
> PostgreSQL、Webhook、版本化 WebSocket 协议、worker affinity、重连 fencing 和远程 job 闭环；真实 GitHub/Codex/bub 闭环属于
> M1+，目前不能按生产能力使用。

## 当前能力

M0 已经具备并验证：

- 加载 `FLOWTOOL_` 配置，通过 Psycopg 连接池初始化 PostgreSQL 的 flow、attempt、job、event、run 和 review 数据；
- 校验 `X-Hub-Signature-256`，异步交接 Webhook，并处理 delivery 去重、allowlist、取消和 review 入库；
- `flowtool serve` 独占 Store、Webhook、状态机和 GitHub 权限，通过独立认证 WebSocket 向多个 worker 公平派发 job；
- `flowtool worker` 主动保持长连接，只装配 workspace、Codex 和 Judge，不读取数据库或 GitHub App 私钥；
- INITIAL/TRIAGE/REVISION/ANSWER/ARCHIVE 使用 v2 task/result 契约，并用 worker/epoch/job/token 四元组拒绝旧连接写入；
- 未 Started 的 assignment 在 lease 到期后重排；Started job 在 recovery grace 内只等原 worker，serve 重启不会立即失败；
- worker journal 在 ResultAccepted 前保留 assignment/终态结果，control result inbox 去重重放并恢复未应用结果；
- 同一 attempt 固定到首次 Started 的 worker；每个 worker 单槽运行，同机实例使用独立 root、identity 标记和进程锁；
- 通过 CLI 分别启动两个角色、查看 flow/worker，并执行基础 cancel/retry 运维命令。

尚未形成可用闭环：GitHub App API 尚未校准，issue 正文和 review thread 映射仍是桩，bub 调用、
Codex commit/push、网络与 scoped token、reconcile 对账、取消运行中进程和真实多轮修订均未端到端验证。
完整边界见[架构状态表](docs/architecture/README.md#当前实现状态)。

## 文档

- [`docs/architecture/`](docs/architecture/README.md)：当前实现状态、目标架构、组件、流程、决策和接入说明；
- [`docs/architecture/diagram.html`](docs/architecture/diagram.html)：手工维护的交互式架构可视化；
- [`docs/plan/`](docs/plan/README.md)：计划、批准状态和实施结果记录。

## 平面边界

- **Control plane**：Webhook、状态机、持久化和 GitHub API，是唯一数据库与外部副作用权威；
- **Worker execution plane**：Judge、prompt、Codex 和隔离工作区，通过出站 WebSocket 接收结构化任务；
- **本地三轴**：workspace 负责目录隔离，Sandbox 负责可写根与网络策略，VCS 只通过 git/jj 指引交给 agent。

VCS 不是端口；详细设计与当前限制见 [`components.md`](docs/architecture/components.md) §4.6。

## 快速开始

要求 Python 3.12+ 与 uv：

```bash
uv sync --locked
docker compose up -d postgres
cp env.example .env
# 先编辑 .env：替换 change-me、your-username 和其他占位值
FLOWTOOL_HOST=127.0.0.1 uv run flowtool serve
# 另开终端，使用同一份 .env：
uv run flowtool worker
```

Compose 中的 PostgreSQL 账号只用于本地开发。远程部署必须使用独立密码和 TLS；serve 会初始化
schema，因此数据库账号需要拥有应用 schema，但不应具有数据库外权限。项目不自动导入旧 SQLite 文件。
也不要用示例 Webhook secret、worker token 或 allowlist 启动可公开访问的服务。启动 M0 daemon
和 `/healthz` 不等于 GitHub/Codex/bub 集成已经可用；接入外部服务前请先阅读
[GitHub App 接入说明](docs/architecture/github-app.md)和[已知短板](docs/architecture/decisions.md)。多个 worker
必须各自配置唯一 identity/token；未显式设置 root 时会按 worker id 派生，显式 root 由 identity 标记和进程锁防止复用。
heartbeat、lease、recovery grace 与重连退避可通过 `env.example` 中的参数调整，非法时间关系会在启动时拒绝。

常用运维入口：

```bash
uv run flowtool --help
uv run flowtool flows --active
uv run flowtool workers
uv run flowtool flow show <FLOW_ID>
uv run flowtool flow cancel <FLOW_ID>
uv run flowtool flow retry <FLOW_ID>
```

## 验证基线

```bash
FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool:flowtool@127.0.0.1:5432/flowtool uv run pytest
uv run ruff check app tests
uv run mypy app tests
uv build --no-sources
uv run flowtool --help
```

[GitHub Actions CI](.github/workflows/ci.yml) 会在 pull request、推送到 `main` 和手动触发时，使用
Python 3.12 分别运行 Ruff、Mypy、pytest 与 sdist/wheel 构建；四项检查并行执行且独立报告结果。

自动化测试覆盖角色配置/协议、PostgreSQL、HMAC、路由、coordinator/executor、WebSocket 鉴权、
assignment/epoch fencing、lease sweeper、忙碌重连、result replay、worker journal/root lock、多 identity registry，以及 control gateway 与 worker client 的
远程 job 闭环；测试隔离 GitHub、Codex、bub 和真实凭据。PostgreSQL 场景还覆盖跨 Store 可见性、
原子 delivery 去重、并发 assignment、单槽、attempt affinity 和 result inbox。CI 还会在同一 runner 启动两个 worker root，并重启 serve 验证自动重连。
`uv sync --locked`、Ruff、Mypy、CLI help 和构建检查均纳入基线。

另有 2026-08-01 的独立实测证明 Codex `exec` / `resume` 上下文连续，`workspace-write` 下 jj `.jj`
可写并能 commit；git `.git` 写入被拦截，push 与凭据注入未验证。这些结果不能替代 M1+ 端到端验收。
