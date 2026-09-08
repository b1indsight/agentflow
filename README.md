# FlowTool（代号，未定名）

FlowTool 的目标是把带 `flow` 标签的 GitHub issue 编排成
`code agent 编码 → PR → 人工审核 → 自动回喂修订 → 人工合并 → 归档` 的 headless 闭环。
项目自身不提供业务 UI，交互面是 GitHub issue 和 PR。

> 当前状态（2026-09-08）：**M0 已验收；M1 Issue→PR、M2 F1–F4 已实现并合并，真实 E2E 待验收**。
> M2 包含持久反馈批次、语义决策、修订交付核验与 session 兜底、原线程回复与保守 resolve。
> 自动化使用实际 PostgreSQL 与临时 Git/jj 仓库；仍有一项同秒 webhook 重放问题，见 [M2 状态与待办](docs/architecture/README.md#m2-状态与待办)。

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
- **合并与归档**：人工合并后由原 worker 核验并清理工作区；对关联 Issue/PR 增加完成标签、
  移除 in-review 标签并各发布一条固定归档说明。全部回写完成后，原子提交成功、释放 worker 并 ACK。
  部分回写或响应丢失只恢复未完成操作，保留清理凭据和用户标签。
- **GitHub 请求超时**：App 和 installation 客户端的连接、读写及连接池等待默认各为 30 秒，
  可通过 `FLOWTOOL_GITHUB_REQUEST_TIMEOUT_SECONDS` 调整；这不是整批结果恢复的总时限。
- **持久反馈批次**：反馈版本与 settle deadline 入库，重启后恢复；同 Flow 串行处理冻结批次，
  运行中新评论/编辑进入下一批。仅 allowlist 作者提交的 Request Changes 经 grace 合批直达修订；
  包含 `@bot` 的评论也进入 Judge。wait 等待新信息，
  answer/ignore/明确 addressed 只消费本批次精确版本，未决问题保留。
- **语义决策与直接答复**：control 冻结 Issue/PR、有限 patch 和讨论，worker 用 bub 0.4.1 无工具入口
  判定；超时、非法输出和不完整快照保守 wait。答案只生成一次，通过持久账本发布并引用来源反馈。
  `FLOWTOOL_JUDGE_MODEL` / `FLOWTOOL_JUDGE_API_KEY` 只配置在 worker；可选 API 地址和 60 秒超时见
  [env.example](env.example)。未配置时 Judge 等待，不在健康检查中调用模型。
- **运维入口**：CLI 可查看 Flow、交付分支/PR、worker、任务恢复状态和未完成的 GitHub 操作，
  并提供 cancel/retry 命令。已核验修订中被明确拒绝的通知可补发；结果不确定时拒绝直接 retry。
- **可靠修订**：每轮更新单仓库 push 凭据，复用原 repo/branch/PR；worker 核验 Git/jj 工作区及远端，
  control 再核验 PR/head/base/ref 与提交关系。缺 session 或明确未执行的 session 拒绝仅兜底一次，
  journal 防止中断后盲目重跑。未决反馈保留；no-op 或部分完成不重新请求 review。

- **原线程反馈闭环**：control 冻结有界分页的线程/comment 身份，按线程合并 addressed/deferred 说明；
  处理说明确认发送后，检查完整人类反馈覆盖、当前 PR/head 和较新的本地版本，再 resolve 并复读。
  回复/resolve 响应丢失由持久账本恢复；新评论进入下一批，权限或映射问题保留诊断并暂停 re-review。

## 尚未完成

- M3 F1/F2 已合并；F3 归档状态回写已实现，完整真实闭环仍待验收，见
  [#26 计划与实施记录](docs/plan/m3-archive-status.md)。
- 真实 GitHub App smoke，以及授权的一次性仓库中的完整 Issue→PR E2E 仍待执行；
  自动化测试使用外部服务替身，不代表真实 GitHub、Codex、bub 集成已验收。
- bub adapter 已通过真实库加本机假模型端点的契约测试；真实 provider 语义 smoke 仍待执行。
  review thread 映射、回复和 resolve 已通过自动化；冲突处理和真实多轮闭环仍待验收。
- 已知 Medium：远端观察编辑后，迟到的重复 webhook 可能在同秒正文回退时被再次重放，
  使旧内容成为新版本并进入待处理反馈。复现顺序与修复验收条件见 [F4 已知问题](docs/plan/m2-review-resolution.md#同秒重复-webhook-重放medium未修复)。
- Reconciler 目前只定时记录活跃 Flow，尚不拉取 GitHub 状态补偿漏失事件。
- cancel 会取消本地流程和任务，尚不终止正在执行的 agent 进程；跨 worker session 迁移、
  多 serve 高可用、GitHub-only 网络限制和由项目管理的强隔离运行环境仍属后续工作。

详细边界见[架构状态表](docs/architecture/README.md#当前实现状态)；M2 合并记录与剩余验收见
[M2 状态与待办](docs/architecture/README.md#m2-状态与待办)和 [F4 实施记录](docs/plan/m2-review-resolution.md)。

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
下一次尝试时间和错误原因；`flow retry` 重排最近失败的任务。已核验并 push 的修订若遇到 GitHub
明确拒绝通知（例如 reviewer 请求 422），结果和回执保持待完成；修正 GitHub 端原因后使用同一命令
只重试失败通知，成功后原子提交交付/session/反馈，不重新运行修订。状态不明的操作仍须先核对。

归档使用 `FLOWTOOL_DONE_LABEL`（默认 `flow:done`）与 `FLOWTOOL_IN_REVIEW_LABEL`。
部署前在仓库创建这两个标签；它们与 trigger label 必须非空且互不相同（忽略大小写）。
缺少标签或 GitHub App 权限会保留待处理结果，服务不会创建仓库标签。
`flow show` 区分清理与 GitHub 回写进度；清理已确认但回写被明确拒绝时，修正远端原因后
执行 `flow retry <id>`，只重置原 job 的失败 effects，保留已完成回执、attempt 和 worker journal。
429/5xx 由恢复循环继续处理；评论结果不明时先查 marker，不盲目补发。等待期间 Flow 保持 ACTIVE
并占用原 worker。配置变更不改写已经冻结的通知；历史 SUCCEEDED Flow 和升级前冻结的空 effects
归档计划不补通知。`on_archived` 是提交后的尽力插件回调，异常不破坏完成，不承诺跨崩溃仅调用一次。

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
GitHub、Codex 与模型服务使用替身；Judge 契约测试运行真实 bub 库连接本机假模型端点，不访问真实 provider。

2026-09-07 M2 F4 本机复核：完整 pytest **377 passed / 0 skipped**（真实 PostgreSQL），Ruff、Mypy 和发行包构建通过；
覆盖反馈批次、Judge/答案恢复、Git/jj 修订核验、session 兜底，以及线程分页、原位回复、保守 resolve、
两轮反馈处理和 PostgreSQL 故障恢复。保留一个
Starlette TestClient/httpx 弃用告警。数据库 CI 的历史验证见
[M1 交付修复记录](docs/plan/m1-delivery-review-fixes.md#实施结果)，原子结果改动的验证边界见
[实施记录](docs/plan/atomic-workflow-results.md#实施记录2026-09-06)。

## 升级注意

M3 F2 本地验证（2026-09-08）：真实 PostgreSQL 下 **461 passed / 0 skipped**，含真实 Git/jj
目录及 worker/control 恢复测试；Ruff、Mypy、sdist/wheel 构建通过。仅保留既有 Starlette 弃用告警。

M3 F3 本地验证（2026-09-08）：真实 PostgreSQL 下 **483 passed / 0 skipped**；包含归档状态回写、
响应丢失与重启恢复。Ruff、Mypy 与 sdist/wheel 构建通过；真实 GitHub App/worker 验收仍待执行，
详见 [F3 实施与验收记录](docs/plan/m3-archive-status.md#6-实施与验收记录2026-09-08)。

M3 F2 使用 wire protocol **v8** 和 worker journal **v2**，serve/worker 必须一起升级。
先用旧版本排空在途任务、未应用结果及其 ACK，并处理仍占用 worker 的旧 Flow；无法排空的工作区、
旧 journal 或旧空归档结果需要人工核验。已 Started 却缺少冻结任务的 ARCHIVE、旧协议结果会阻止 serve 启动；
仅领取但未冻结的任务可正常恢复。冻结后的归档在 Started 丢失或 grace 到期时保留原 token，
按原 journal 与冻结身份恢复；断线期间已完成的执行结果在重连注册前先持久保存。
不转换空结果为清理凭据，不删除 journal 绕过检查；旧版本已清除 journal 的绑定也需先人工处理。

新 journal 在任务 ACK 后保留上一任务的 Flow 和 root 归属，成功归档 ACK 后才移除。不要更换
`FLOWTOOL_WORKER_ROOT`、复制 journal 到其他 root 或复用 worker ID 接管工作区。清理仅删除
`repos/<owner>/<repo>--flow-<id>` 与其 `.partial`，拒绝 symlink 祖先、顶层 symlink、路径逃逸
和含保留标记 `--flow-` 的 seed 仓库名；保留 seed、其他 Flow、journal 和共享会话。

合并后使用 `flow show <id>` 查看等待、清理失败、结果待核验/提交或完成及错误。
清理失败会保留 ACTIVE Flow 和原 worker binding；修复权限等原因后，原 worker 在线且执行已结束时
使用 `flow retry <id>`，只重试 ARCHIVE，保留当前 attempt。已有清理凭据时仅按上述方式恢复 GitHub
回写，不要重复删除。合并后的 cancel 被拒绝；旧 executor 状态未知、journal 丢失、原 worker 长期离线、
历史 FAILED/CANCELLED Flow 或已释放的绑定均不会自动接管、清理或释放，需人工核验。

M2 F4 保持 wire protocol v7，在 control 的批次和结果账本中增加线程快照、处理依据和远端回执。
升级前须完成或人工核对旧 pending revision result/effect；缺少 F4 线程处理意图的旧计划会列出 job ID 并阻止启动。
旧本地 resolved 不会迁移为远端确认。线程权限失败可完成已验证代码交付，但相关反馈/诊断仍待人工处理。

M2 F3 将任务协议升级到 v7，修订契约必须包含冻结基线与交付证据，serve 和 worker 必须一起升级。先用旧版本排空 queued/running review
job、重试并完成仍可重试的 failed review job，核对所有未确认旧结果，再停止旧进程；
历史失败的 ANSWER 也须核对并处理；已被替换的其他历史失败不阻止升级。新 serve 遇到旧 ANSWER、
缺 batch 的旧在途任务、已开始执行却缺冻结上下文的 TRIAGE、缺冻结 assignment 的 REVISION 或旧协议未确认结果
会拒绝启动并列出 job ID；尚未开始执行的上下文准备任务可正常恢复。不要手工清除结果标记来绕过检查。
schema 会一次性导入旧反馈，保留历史表；
旧 resolved 标记不代表 GitHub 已确认。详见[Review 循环](docs/architecture/review-loop.md#b5-取消重试与恢复)。

session generation 和前轮摘要自动迁移入 attempts。旧 worker journal 必须先确认或人工核对，不能混跑 v6/v7。
修订失败保留原 worker binding；处理脏工作区或远端偏移后可显式 retry，不自动 reset/rebase/force push。
修订准备阶段的 GitHub 错误会持久记录为失败；恢复后显式 retry，未 Started 的准备失败不消耗修订轮数。
CodeAgent 总时限覆盖 resume、fallback 和 VCS 核验，超时回收并等待对应进程组退出；用户 cancel 仍不终止运行中的进程。

从原子结果恢复改动之前的版本升级时，先停止旧 serve，再启动新版本；不支持新旧 serve 混跑。
旧版本留下的未知结果可能无法自动核实，需要人工处理；回退前也要处理完或隔离新增待办，避免旧版本
忽略操作记录后重复执行。详见[兼容与交付边界](docs/plan/atomic-workflow-results.md#兼容与交付边界)。
