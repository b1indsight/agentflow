# FlowTool（代号，未定名）

FlowTool 的目标是把带 `flow` 标签的 GitHub issue 编排成
`code agent 编码 → PR → 人工审核 → 自动回喂修订 → 人工合并 → 归档` 的 headless 闭环。
项目自身不提供业务 UI，交互面是 GitHub issue 和 PR。

> 当前状态（2026-09-07）：**M0 已验收；M1 Issue→PR 已实现并有自动化覆盖，真实 GitHub E2E 待验收**。
> 当前实现还包含 M1 交付修复、原子结果提交与恢复，以及 M2 F1 持久反馈批次与 settle。
> 本机最新测试为 **180 passed / 0 skipped**（实际 PostgreSQL）；完整产品闭环尚未验收。

## 当前能力

- **接收与持久化**：校验 GitHub Webhook 的 `X-Hub-Signature-256`，处理 delivery 去重、allowlist、
  触发、取消和 review 入库；PostgreSQL 是唯一运行时数据库，启动时初始化 schema。
- **远程执行**：`flowtool serve` 管理状态、数据库和 GitHub 操作；`flowtool worker` 主动通过认证
  WebSocket 连接，执行 Codex/Judge 任务。支持多 worker 公平调度，每个 worker 同时执行一个 job。
- **执行环境与恢复**：Flow 生命周期独占 worker/repo，Attempt 保存本次运行和 session；
  assignment 使用 worker/epoch/job/token 校验。未 Started 的任务可在租约到期后重排，Started 任务在
  recovery grace 内等待原 worker；worker journal 支持忙碌重连、serve 重启恢复与终态结果重放。
- **Issue→PR 交付**：读取真实 Issue 和仓库默认分支，为 Codex 提供单仓库、短时 Contents write
  push capability，由 agent 自主 commit/push。`flow.open_pr` 先核验远端 SHA 并持久化 PR 意图，
  **INITIAL 终态结果校验成功后**才创建或复用唯一的 open PR，再设置标签和 reviewer。
  已修复失败 Flow 重试后的执行绑定复用、提前开 PR 和 closed PR 被误判为成功的问题。
- **原子提交与操作恢复**：trigger/retry/merge/cancel 的状态和任务变更在共享事务内提交；
  终态结果先持久接收，再执行带回执的 GitHub 操作，最后原子提交本地结果并发送 `ResultAccepted`。
  恢复时核对远端状态，避免重复派生 job 或重复发送评论；无法确认的远端结果保留待核对状态。
- **GitHub 请求超时**：App 和 installation 客户端的连接、读写及连接池等待默认各为 30 秒，
  可通过 `FLOWTOOL_GITHUB_REQUEST_TIMEOUT_SECONDS` 调整；这不是整批结果恢复的总时限。
- **持久反馈批次**：反馈版本与 settle deadline 入库，重启后恢复；同 Flow 串行处理冻结批次，
  运行中新评论/编辑进入下一批。allowlist 强信号经 grace 合批直达修订；wait 等待新信息，
  answer/ignore/明确 addressed 只消费本批次精确版本，未决问题保留。
- **运维入口**：CLI 可查看 Flow、交付分支/PR、worker、任务恢复状态和未完成的 GitHub 操作，
  并提供 cancel/retry 命令。存在待核对结果时会拒绝直接 retry。

## 尚未完成

- 真实 GitHub App smoke，以及授权的一次性仓库中的完整 Issue→PR E2E 仍待执行；
  自动化测试使用外部服务替身，不代表真实 GitHub、Codex、bub 集成已验收。
- Review 的 TRIAGE/REVISION/ANSWER 和合并后 ARCHIVE 已有控制流，但 bub 命令与输出契约、
  review thread 映射、冲突处理和真实多轮修订闭环尚未完成或验证。
- Reconciler 目前只定时记录活跃 Flow，尚不拉取 GitHub 状态补偿漏失事件。
- cancel 会取消本地流程和任务，尚不终止正在执行的 agent 进程；跨 worker session 迁移、
  多 serve 高可用、GitHub-only 网络限制和由项目管理的强隔离运行环境仍属后续工作。

详细边界见[架构状态表](docs/architecture/README.md#当前实现状态)；近期变更与验收范围见
[M1 交付修复](docs/plan/m1-delivery-review-fixes.md)和[原子结果恢复](docs/plan/atomic-workflow-results.md)。

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

要求 Python 3.12+、uv，以及可访问的 PostgreSQL；以下用 Docker Compose 启动开发数据库。
实际编码还需要在隔离的 worker 环境准备 Codex CLI 和认证，GitHub App 配置见
[接入说明](docs/architecture/github-app.md)。

先准备 control plane：

```bash
uv sync --locked --dev
docker compose up -d postgres
cp env.example .env
# 编辑 .env：配置 GitHub App、allowlist、reviewers 和 worker 身份/token
FLOWTOOL_HOST=127.0.0.1 uv run flowtool serve
```

另开终端启动本地连接验证用的 worker；下面的 token 必须与 serve 的 `FLOWTOOL_WORKER_TOKENS` 一致。
该示例未授权 Codex 编码执行：

```bash
FLOWTOOL_WORKER_ID=worker-1 FLOWTOOL_WORKER_TOKEN=change-me-worker-token \
FLOWTOOL_SERVE_WS_URL=ws://127.0.0.1:8000/internal/workers/connect \
FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true uv run flowtool worker
```

`WorkerSettings` 不读取当前目录的 `.env`，worker 配置通过进程环境提供；默认要求 `wss://`，示例中的
`FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true` 仅用于开发。要运行 `danger-full-access` CodeAgent，必须在专用 OS account、
container 或 VM 中只注入 worker/Codex 所需变量，并设置
`FLOWTOOL_TRUSTED_HOST_EXECUTION=true` 与对应的
`FLOWTOOL_EXECUTION_ISOLATION=dedicated-account|container|vm`。开启该模式时，worker 进程环境若包含数据库、
GitHub App、webhook 或 control worker-registry secret，会拒绝启动。仅使用不同 repo 目录不构成这个 secret 边界；
同一 VM 可以运行多个隔离 worker，但 control plane 的凭据和 `.env` 必须对其 OS 身份不可读。

Compose 中的 PostgreSQL 账号只用于本地开发。远程部署必须使用独立密码和 TLS；serve 会初始化
schema，因此数据库账号需要拥有应用 schema，但不应具有数据库外权限。项目不自动导入旧 SQLite 文件。
也不要用示例 Webhook secret、worker token 或 allowlist 启动可公开访问的服务。启动 serve
和 `/healthz` 不等于 GitHub/Codex/bub 集成已经可用；接入外部服务前请先阅读
[GitHub App 接入说明](docs/architecture/github-app.md)和[已知短板](docs/architecture/decisions.md)。多个 worker
必须各自配置唯一 identity/token；未显式设置 root 时会按 worker id 派生，显式 root 由 identity 标记和进程锁防止复用。
serve 的 heartbeat、lease、recovery grace 配置见 [`env.example`](env.example)；worker 的
`FLOWTOOL_RECONNECT_MIN_BACKOFF`、`FLOWTOOL_RECONNECT_MAX_BACKOFF` 和 `FLOWTOOL_RECONNECT_JITTER`
通过进程环境调整。非法时间关系会在启动时拒绝。

常用运维入口：

```bash
uv run flowtool --help
uv run flowtool flows --active
uv run flowtool workers
uv run flowtool flow show <FLOW_ID>
uv run flowtool flow cancel <FLOW_ID>
uv run flowtool flow retry <FLOW_ID>
```

运维命令在可访问数据库的 control 环境运行。`flow show` 会列出未完成 GitHub 操作的状态、
下一次尝试时间和错误原因；`flow retry` 重排最近失败的任务，无法代替待核对结果的人工处理。

## 验证基线

本地检查：

```bash
uv run pytest -q -p no:cacheprovider
uv run ruff check app tests
uv run mypy app tests
uv build --no-sources
uv run flowtool --help
```

未设置 `FLOWTOOL_TEST_DATABASE_URL` 时，PostgreSQL 集成用例会跳过。启用开发数据库后运行完整测试：

```bash
docker compose up -d postgres
FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool:flowtool@127.0.0.1:5432/flowtool uv run pytest
```

[GitHub Actions CI](.github/workflows/ci.yml) 会在 pull request、推送到 `main` 和手动触发时，使用
Python 3.12 分别运行 Ruff、Mypy、pytest 与 sdist/wheel 构建。pytest job 配置 PostgreSQL 17，
并启动 serve 和两个 worker，重启 serve 后验证 worker 自动重新注册。

自动化覆盖配置与协议、Webhook、任务调度和恢复、Flow 工具与 PR 交付、GitHub 超时及错误映射、
结果重放与远端操作核对。数据库用例还覆盖事务回滚、跨连接并发、任务去重、取消交错和恢复期限；
GitHub、Codex、bub 使用替身，不访问真实外部服务。

2026-09-07 M2 F1 本机复核：设置临时 PostgreSQL 数据库后 `uv run pytest -q` 为 **180 passed / 0 skipped**；
覆盖反馈交接/领取回滚、重启、并发、版本隔离、等待接续与结果恢复。保留一个
Starlette TestClient/httpx 弃用告警。数据库 CI 的历史验证见
[M1 交付修复记录](docs/plan/m1-delivery-review-fixes.md#实施结果)，原子结果改动的验证边界见
[实施记录](docs/plan/atomic-workflow-results.md#实施记录2026-09-06)。

## 升级注意

M2 F1 将任务协议升级到 v5，serve 和 worker 必须一起升级。先用旧版本排空 queued/running review
job 与所有未确认旧结果，再停止旧进程；新 serve 遇到缺 batch 的旧 review job 或旧协议未确认结果
会拒绝启动并列出 job ID。不要手工清除结果标记来绕过检查。schema 会一次性导入旧反馈，保留历史表；
旧 resolved 标记不代表 GitHub 已确认。详见[Review 循环](docs/architecture/review-loop.md#b5-取消重试与恢复)。

从原子结果恢复改动之前的版本升级时，先停止旧 serve，再启动新版本；不支持新旧 serve 混跑。
旧版本留下的未知结果可能无法自动核实，需要人工处理；回退前也要处理完或隔离新增待办，避免旧版本
忽略操作记录后重复执行。详见[兼容与交付边界](docs/plan/atomic-workflow-results.md#兼容与交付边界)。
