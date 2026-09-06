# FlowTool（代号，未定名）

FlowTool 的目标是把带 `flow` 标签的 GitHub issue 编排成
`code agent 编码 → PR → 人工审核 → 自动回喂修订 → 人工合并 → 归档` 的 headless 闭环。
项目自身不提供业务 UI，交互面是 GitHub issue 和 PR。

> 当前状态：**M0 已验收，M1 三个 feature 均已实现并通过自动化（2026-09-05），真实 M1 E2E 待验收**。
> 仓库已贯通 Issue/default branch、Flow 生命周期 binding、Codex commit/push、`flow.open_pr` 与一 Flow 一 PR；
> 真实 GitHub App smoke、一次授权 disposable repo 的 Issue→PR E2E 和 bub 仍未完成，目前不能按生产能力使用。

## 当前能力

M0 已经具备并验证：

- 加载 `FLOWTOOL_` 配置，通过 Psycopg 连接池初始化 PostgreSQL 的 flow、attempt、job、event、run 和 review 数据；
- 校验 `X-Hub-Signature-256`，异步交接 Webhook，并处理 delivery 去重、allowlist、取消和 review 入库；
- `flowtool serve` 独占 Store、Webhook、状态机和 GitHub 权限，通过独立认证 WebSocket 向多个 worker 公平派发 job；
- `flowtool worker` 主动保持长连接，只装配 workspace、Codex 和 Judge，不读取数据库或 GitHub App 私钥；
- INITIAL/TRIAGE/REVISION/ANSWER/ARCHIVE 使用 v4 task/result/tool/error 契约，显式携带 stable Flow identity，并用
  worker/epoch/job/token 四元组拒绝旧连接写入；
- 未 Started 的 assignment 在 lease 到期后重排；Started job 在 recovery grace 内只等原 worker，serve 重启不会立即失败；
- worker journal 在 ResultAccepted 前保留 assignment/终态结果；control 持续恢复已接收结果，
  以持久化 GitHub 操作回执和最终本地事务避免重放重复派生任务/评论，未知远端结果保留待核对；
- 同一 attempt 固定到首次 Started 的 worker；每个 worker 单槽运行，同机实例使用独立 root、identity 标记和进程锁；
- Flow 生命周期独占一个 worker/repo，`FlowDelivery` 独立保存 branch/base/PR；Attempt 只保存本次运行与 session；
- INITIAL 获取真实 Issue/default branch，临发短时 push capability；Codex 通过 worker-local MCP 调用
  `flow.open_pr`，control 在远端 SHA 核验后幂等创建或复用唯一 PR；
- 通过 CLI 分别启动两个角色、查看 flow/worker，并执行基础 cancel/retry 运维命令。

尚未形成可用闭环：GitHub App adapter 已对齐锁定的 githubkit 0.16 并通过隔离测试，但真实 App smoke、
真实 Issue→PR E2E、review thread 映射、bub 调用、受控网络、reconcile 对账、取消运行中进程和真实多轮修订
仍未端到端验证。
完整边界见[架构状态表](docs/architecture/README.md#当前实现状态)。

## 文档

- [`docs/architecture/`](docs/architecture/README.md)：当前实现状态、目标架构、组件、流程、决策和接入说明；
- [`docs/architecture/diagram.html`](docs/architecture/diagram.html)：手工维护的交互式架构可视化；
- [`docs/plan/`](docs/plan/README.md)：计划、批准状态和实施结果记录。

## 平面边界

- **Control plane**：Webhook、状态机、持久化和 GitHub API，是唯一数据库与外部副作用权威；
- **Worker execution plane**：Judge、prompt、Codex 和 Flow 独占 repo，通过出站 WebSocket 接收结构化任务；
- **本地执行**：Flow repo 负责目录所有权；只有运行在无法读取 control-plane secret 的专用 OS account、
  container 或 VM 中的 worker 才能显式开启 full access。Agent 自选 Git/jj，worker 只做结果核验。

VCS 不是端口；详细设计与当前限制见 [`components.md`](docs/architecture/components.md) §4.6。

## 快速开始

要求 Python 3.12+ 与 uv：

```bash
uv sync --locked
docker compose up -d postgres
cp env.example .env
# 先编辑 .env：替换 change-me、your-username 和其他占位值
FLOWTOOL_HOST=127.0.0.1 uv run flowtool serve
# 未启用 CodeAgent 的本地 worker 可以另开终端，以进程环境传入 worker-only 配置：
FLOWTOOL_WORKER_ID=worker-1 FLOWTOOL_WORKER_TOKEN=change-me-worker-token \
FLOWTOOL_SERVE_WS_URL=ws://127.0.0.1:8000/internal/workers/connect \
FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true uv run flowtool worker
```

`WorkerSettings` 不读取当前目录的 `.env`。要运行 `danger-full-access` CodeAgent，必须在专用 OS account、
container 或 VM 中只注入 worker/Codex 所需变量，并设置
`FLOWTOOL_TRUSTED_HOST_EXECUTION=true` 与对应的
`FLOWTOOL_EXECUTION_ISOLATION=dedicated-account|container|vm`。worker 进程环境若包含数据库、GitHub App、
webhook 或 control worker-registry secret，会拒绝启动。仅使用不同 repo 目录不构成这个 secret 边界；
同一 VM 可以运行多个隔离 worker，但 control plane 的凭据和 `.env` 必须对其 OS 身份不可读。

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

M1 Feature 1 已用一次性本地 repo 验证 Codex 自主完成 Git commit/push；Feature 2 已实现按单仓库和
Contents write 收窄的短时 capability，以及 Issue/repository/ref/PR primitives；Feature 3 已通过 95 项
自动化测试贯通 capability 下发、worker-local MCP、双端 VCS/ref 核验和幂等 PR delivery。真实 GitHub App
smoke、GitHub-only egress 和授权 disposable repo 的完整 E2E 仍待完成。

原子结果应用改动的本机验证为 118 passed / 30 skipped；按用户要求先跳过本机 PostgreSQL 安装和真实数据库
测试，数据库用例保留在现有 PostgreSQL CI 中。Ruff、Mypy、CLI help 和 sdist/wheel 构建通过。
升级必须先停止旧 serve；旧版本已接收但未确认的结果默认要求人工核对，详见
[结果应用计划与验证范围](docs/plan/atomic-workflow-results.md)。
