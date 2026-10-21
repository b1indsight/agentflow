# FlowTool（代号，未定名）

FlowTool 的目标是把带 `flow` 标签的 GitHub issue 编排成
`code agent 编码 → PR → 人工审核 → 自动回喂修订 → 人工合并 → 归档` 的 headless 闭环。
工作流交互面是 GitHub issue 和 PR；另提供 [Web 控制台](app/controler/web/README.md)，通过同源 API
展示真实 Flow、执行记录和持久时间线，支持 cancelled Flow 的手动展示归档，
由 Cloudflare Access 限制单 owner 访问；归档保留取消状态，不触发交付清理或 GitHub 写入。

代码按职责分为 `domain`、`contracts`、`persistence`、`github`、`controler`、`worker`、
`telemetry` 与 `plugins`。`controler` 是控制服务端，内部按事件、Worker 管理、工作流、审核
和远端对账归组；目录和依赖边界见[组件职责](docs/architecture/components.md#包依赖边界)。

> 当前状态（2026-10-15，核对最新 main）：**M0 已验收；M1–M4 功能及 M5 部署、接收、运维代码已落地，真实 E2E 与部署验收仍待完成**。
> Control 运行 Judge 并管理 GitHub 操作，Worker 执行编码、修订和归档清理。Runtime 管理共享
> Database，各服务自行创建所需仓储；Webhook 在 receipt 事务内创建服务与路由。
> 实现与验收边界见[架构状态表](docs/architecture/README.md#当前实现状态)，最近装配调整见
> [持久层简化记录](docs/plan/persistence-simplification.md)。

## 当前能力

- **准备期读重试**：Assignment 发出前的临时 GitHub 读取故障按原 worker binding 自动退避；
  `FLOWTOOL_PREPARE_MAX_FAILURES` 默认 5 次，`FLOWTOOL_PREPARE_RETRY_WINDOW_SECONDS`
  默认 900 秒，遵守 Retry-After，重启保留次数与时间。`flow show` 展示准备失败、下次时间和原因。
  超出预算或永久 API 错误走现有失败/显式 retry 路径；凭据签发和 worker 执行不在此重试范围。

- **关闭与重开**：未合并 PR 关闭后保留原 worker/仓库 slot，停止新派发与 GitHub 写操作；
  已发操作按原账本收尾。重开同一 PR 后重新核验 head/base 与已完成的 worker 交付，再重新
  triage 未消费反馈。人工漂移、未知执行或未决 effects 保持暂挂；`flow show` 显示恢复条件，
  `flow retry` 不能绕过此状态。关闭期间收到的真实反馈编辑仍保留。

- **反馈对账**：原 Issue、PR 普通评论和 Review 正文分别保存分页进度，默认每 300 秒复扫；
  每个来源每次至多读取 50 条完整正文，未完成的页 5 秒后继续。重启保留 cursor/退避，
  `flow show` 展示各来源诊断。旧命令不追溯执行，列表缺席不推断删除。
  行内线程同样持续对账，包括人工 resolved/unresolved/outdated 事实；每次读取一个线程的
  至多 20 条评论，嵌套分页可恢复。跨页片段不作为完整 resolve 依据，poll 不执行远端写操作。

- **PR 对账**：对已追踪 PR 定时查询并补偿漏失合并事件，检查进度与退避持久化；
  `FLOWTOOL_RECONCILE_INTERVAL_SECONDS` 默认 300 秒，`FLOWTOOL_RECONCILE_BATCH_SIZE`
  默认 64，`FLOWTOOL_RECONCILE_CONCURRENCY` 默认 4。`flow show` 展示最后成功和下次检查时间。

- **恢复并发**：lease、settle 和网络结果恢复独立运行；结果恢复默认 4 并发、每页 64 项，
  可用 `FLOWTOOL_RESULT_RECOVERY_CONCURRENCY`（1–64）和
  `FLOWTOOL_RESULT_RECOVERY_BATCH_SIZE`（1–1000）调整。有限扫描周期保证旧待确认结果可重试。

- **接收与持久化**：校验 GitHub Webhook 的 `X-Hub-Signature-256`，处理 delivery 去重、allowlist、
  触发、取消和 review 入库；PostgreSQL 是唯一运行时数据库，启动时初始化 schema。
- **远程执行**：`flowtool serve` 管理状态、数据库和 GitHub 操作；`flowtool worker` 主动通过认证
  WebSocket 连接，执行 INITIAL、REVISION、ARCHIVE 任务；Judge 在 control 本地运行。
  支持多 worker 公平调度，每个 worker 同时执行一个 job。
- **执行环境与恢复**：Flow 生命周期独占 worker/repo，Attempt 保存本次运行和 session；
  assignment 使用 worker/epoch/job/token 校验。未授权派发的任务可在租约到期后重排，已授权
  派发但缺少 Started 的未知执行不按未发送重排；Started 任务在
  recovery grace 内等待原 worker；worker journal 支持忙碌重连、serve 重启恢复与终态结果重放。
- **Issue→PR 交付**：读取真实 Issue 和仓库默认分支，为 Codex 提供单仓库、短时 Contents write
  push capability，由 agent 自主 commit/push。`flow.open_pr` 先核验远端 SHA 并持久化 PR 意图，
  **INITIAL 终态结果校验成功后**才创建或复用唯一的 open PR，再设置标签和 reviewer。
  已修复失败 Flow 重试后的执行绑定复用和提前开 PR；创建后已关闭的 PR 保留身份并暂挂，
  INITIAL 结果只收尾执行事实，不回开 PR。
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
- **语义决策与直接答复**：control 冻结 Issue/PR、有限 patch 和讨论，control 用 bub 0.4.1 无工具入口
  判定；超时、非法输出和不完整快照保守 wait。决定保存在反馈批次，只有 revise 创建 worker job；答案引用来源反馈并以批次 marker 核对回执。
  `FLOWTOOL_JUDGE_MODEL` / `FLOWTOOL_JUDGE_API_KEY` 只配置在 control；可选 API 地址和 60 秒超时见
  [env.example](env.example)。未配置时 Judge 等待，不在健康检查中调用模型。
- **运维入口**：CLI 可查看 Flow、交付分支/PR、worker、任务恢复状态和未完成的 GitHub 操作，
  并提供 cancel/retry 命令。已核验修订中被明确拒绝的通知可补发；结果不确定时拒绝直接 retry。
- **取消与 drain**：cancel 持久化停止意图，worker 取消当前执行并回收其持有的 Codex/VCS
  进程组；停止凭据与已发送 effects 未确认前不释放绑定。drain 停止分配新 Flow，已有绑定
  继续收尾。未知旧执行保持阻塞，无强制释放入口，见[取消与停机](docs/operations/cancel-drain.md)。
- **Projects 与可观测性**：可选组织 Projects v2 同步独立运行；结构化日志、受 token 保护的
  `/metrics`、数据库就绪探针 `/readyz` 与 worker 状态文件已接入。Docker/Compose、Kubernetes
  和常驻运维说明见[部署指南](docs/deployment/README.md)，不代表真实环境已验收。
- **可靠修订**：每轮更新单仓库 push 凭据，复用原 repo/branch/PR；worker 核验 Git/jj 工作区及远端，
  control 再核验 PR/head/base/ref 与提交关系。缺 session 或明确未执行的 session 拒绝仅兜底一次，
  journal 防止中断后盲目重跑。未决反馈保留；no-op 或部分完成不重新请求 review。

- **原线程反馈闭环**：control 冻结有界分页的线程/comment 身份，按线程合并 addressed/deferred 说明；
  处理说明确认发送后，检查完整人类反馈覆盖、当前 PR/head 和较新的本地版本，再 resolve 并复读。
  回复/resolve 响应丢失由持久账本恢复；新评论进入下一批，权限或映射问题保留诊断并暂停 re-review。

## 尚未完成

- M3 F1/F2/F3 已合并，完整真实闭环仍待验收，见
  [#26 计划与实施记录](docs/plan/m3-archive-status.md)。
- 真实 GitHub App smoke，以及授权的一次性仓库中的完整 Issue→PR E2E 仍待执行；
  自动化测试使用外部服务替身，不代表真实 GitHub、Codex、bub 集成已验收。
- bub adapter 已通过真实库加本机假模型端点的契约测试；真实 provider 语义 smoke 仍待执行。
  review thread 映射、回复和 resolve 已通过自动化；冲突处理和真实多轮闭环仍待验收。
- 已追踪 PR 的 merge/close/reopen 及评论/线程漏投补偿已实现；漏失的新 Flow trigger 不由对账补建。
- 真实取消/恢复演练、镜像发布与目标集群验收仍待完成。跨 worker session 迁移、
  多 serve 高可用、冲突自动处理、GitHub-only 网络限制和每 Flow 动态强隔离仍属后续工作。

详细边界见[架构状态表](docs/architecture/README.md#当前实现状态)；M2 合并记录与剩余验收见
[M2 状态与待办](docs/architecture/README.md#m2-状态与待办)和 [F4 实施记录](docs/plan/m2-review-resolution.md)。

## 文档

- [Flow 会话查询 API](docs/architecture/conversation-api.md)：control 代理 worker 读取 Codex 历史，供 WebUI 分页展示；
- [已取消 Flow 手动归档](docs/plan/cancelled-flow-archive.md)：归档入口、持久化与取消/恢复的安全边界；
- [`docs/architecture/`](docs/architecture/README.md)：当前实现状态、目标架构、组件、流程、决策和接入说明；
- [`docs/architecture/diagram.html`](docs/architecture/diagram.html)：手工维护的交互式架构可视化；
- [`docs/plan/`](docs/plan/README.md)：计划、批准状态和实施结果记录。

## 平面边界

- **Control plane**：Webhook、状态机、持久化和 Judge，独占数据库操作与 Flow 的 PR 创建、标签和 review 编排；
- **Worker execution plane**：prompt、Codex 和 Flow 独占 repo，通过出站 WebSocket 接收结构化任务，
  Git/gh 使用当前 assignment 的单仓库短时 installation token；不持有数据库、GitHub App 或 Judge 凭据；
- **本地执行**：Flow repo 负责目录所有权；只有运行在无法读取 control-plane secret 的专用 OS account、
  container 或 VM 中的 worker 才能显式开启 full access。Agent 自选 Git/jj，worker 只做结果核验。

VCS 不是端口；详细设计与当前限制见 [`components.md`](docs/architecture/components.md) §4.6。

## 快速开始

完整步骤见 [部署与 Cloudflare 接入指南](docs/deployment/README.md)。

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

实际编码需安装 `git`、`gh` 和 Codex CLI（使用 jj 时另装 jj）。INITIAL 会在
`<worker_root>/repos/<owner>/<repo>` 缺失或为空时，用当前任务的短期 installation token
原子 clone；已有 seed 保持原状，不自动 fetch/reset 或安装依赖。seed 必须有任务基线分支，
不自动处理空远端、submodule 或 LFS。

每个 worker 按仓库维护执行位置池：优先复用原 Flow 绑定和空闲完整副本，全部占用时增加临时
Git worktree。`FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT=2` 设置每仓库总上限（正整数、最小 1，
重启生效），seed 不计入。归档后完整副本归还池并保留 ignored 缓存；临时 worktree 注销并删除。
达到上限、目录异常或所有权不明时拒绝执行，不自动清掉未知文件；旧未登记目录不迁移或接管。
位置数量上限不限制单个缓存的字节数，也不改变 Flow 生命周期独占 worker 的调度。

每次 INITIAL、REVISION resume/fallback 都提供本轮 bot `gh` 包装、独立配置和 Git helper，
不使用个人登录态；token 过期/权限不足即失败，不自动续期或扩权。worker 保持单仓库
Contents write，PR 通过 MCP `open_pr` 请求 controler 校验后创建。包装不构成 OS 沙箱。
新版本使用 protocol v14 / journal v4，serve 与 worker 应同时升级；旧 journal 不自动迁移。

`WorkerSettings` 不读取当前目录的 `.env`，worker 配置通过进程环境提供；默认要求 `wss://`，示例中的
`FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true` 仅用于开发。要运行 `danger-full-access` CodingAgent，必须在专用 OS account、
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
serve 启动后在后台发现 GitHub App 已授权的仓库，自动补齐 `flow`、`flow:in-review`、
`flow:done`；自定义名称使用对应配置，三个名称必须非空且互不相同（忽略大小写）。
未建齐或检查失败的仓库每 **300 秒**检查；确认三个标签齐全后每 **24 小时**复查并补回缺失项，
人工预建的也计入，已有名称、颜色和描述不变。检查时间只保存在内存，重启后重新扫描确认。
新安装与新增授权仓库每 300 秒发现；暂停的 installation 和归档仓库跳过。
App 需有 Issues write 权限；初始化失败不阻塞服务 readiness，日志记录待重试原因，限流可延长等待。
这只创建仓库标签定义，不给 Issue/PR 自动添加触发标签。缺少标签或权限仍可能保留待处理结果。
`flow show` 区分清理与 GitHub 回写进度；清理已确认但回写被明确拒绝时，修正远端原因后
执行 `flow retry <id>`，只重置原 job 的失败 effects，保留已完成回执、attempt 和 worker journal。
评论发送前的本地认证失败也可在修复凭据后按此方式重试；后续查询失败不会清除已有不确定发送记录。
状态写入由持久恢复逻辑处理，避免 SDK 内部重试掩盖此前已发送的请求；读取（含只读 GraphQL）
与 push token 签发保留有限重试。确认查询失败仍等待恢复，并遵守 Retry-After 或限流重置时间。
429/5xx 由恢复循环继续处理；评论结果不明时先查 marker，不盲目补发。等待期间 Flow 保持 ACTIVE
并占用原 worker。配置变更不改写已经冻结的通知；历史 SUCCEEDED Flow 和升级前冻结的空 effects
归档计划不补通知。`on_archived` 是提交后的尽力插件回调，异常不破坏完成，不承诺跨崩溃仅调用一次。

## Web 控制台

控制台在 serve 的 `/console/` 提供，看板、详情、执行记录和时间线通过同源 `/api/` 读取。
页面、静态资源及 API 都要求现有 Cloudflare Access owner 身份；缺少配置时拒绝访问。
Tunnel/Access 必须将 `/console*` 与 `/api/*` 交给同一服务并应用相同 owner 策略，
不要为静态资源增加公开旁路。Webhook 与 Worker 端点继续使用各自原有鉴权。

时间线永久保存已观察版本和删除标记；本次阅读绑定本地快照，末尾分界线显式刷新。
活跃 Flow 打开详情或刷新会排队后台同步，未完成的来源明确显示 pending/partial/unavailable，
不将尚未同步或权限错误当成“没有更新”。Worker 会话复用主动分页查询，心跳不触发全量抓取。
首次采集前的历史编辑/删除、远端已过期内容无法还原；个人阅读状态另见 #88。

从源码制作带控制台的 wheel/sdist 时，先在 Node 24 环境构建静态文件：

```bash
python scripts/build_console.py
uv build --no-sources
```

生成的静态文件随 Python 包安装，serve 运行时不需要 Node；Docker 使用独立 Node 构建阶段。
仅运行 Python 源码而未构建控制台时，`/console/` 返回明确的资源不可用错误，不退回示例数据。
前端开发可在 `app/controler/web` 运行 `npm ci && npm run dev`，API 代理至本地 serve 的 8000 端口。

## 验证基线

本地检查：

```bash
uv run pytest -q -p no:cacheprovider
uv run ruff check app tests
uv run mypy app tests
python scripts/build_console.py
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

M3 F3 及审查修复本地验证（2026-09-08）：真实 PostgreSQL 下 **502 passed / 0 skipped**；包含归档状态回写、
响应丢失与重启恢复。Ruff、Mypy 与 sdist/wheel 构建通过；真实 GitHub App/worker 验收仍待执行，
详见 [F3 审查修复记录](docs/plan/m3-archive-status.md#7-审查修复2026-09-08)。

当前仅支持 wire protocol **v13**、worker journal **v3** 与当前完整 PostgreSQL schema。
仓库尚未部署，不提供旧版本数据库、任务、结果或 journal 的迁移；首次运行使用新数据库/schema
和新 worker root。serve/worker 使用同一 commit/镜像版本，不混跑；协议不匹配直接拒绝。
同版本重启仍保留冻结任务、结果摘要、effect ledger 与 journal 恢复能力。

新 journal 在任务 ACK 后保留上一任务的 Flow 和 root 归属，成功归档 ACK 后才移除。不要更换
`FLOWTOOL_WORKER_ROOT`、复制 journal 到其他 root 或复用 worker ID 接管工作区。清理仅删除
`repos/<owner>/<repo>--flow-<id>` 与其 `.partial`，拒绝 symlink 祖先、顶层 symlink、路径逃逸
和含保留标记 `--flow-` 的 seed 仓库名；保留 seed、其他 Flow、journal 和共享会话。

合并后使用 `flow show <id>` 查看等待、清理失败、结果待核验/提交或完成及错误。
清理失败会保留 ACTIVE Flow 和原 worker binding；修复权限等原因后，原 worker 在线且执行已结束时
使用 `flow retry <id>`，只重试 ARCHIVE，保留当前 attempt。已有清理凭据时仅按上述方式恢复 GitHub
回写，不要重复删除。合并后的 cancel 被拒绝；旧 executor 状态未知、journal 丢失、原 worker 长期离线、
历史 FAILED/CANCELLED Flow 或已释放的绑定均不会自动接管、清理或释放，需人工核验。

线程快照、处理依据和远端回执均按当前契约持久化；本地 resolved 标记不替代 GitHub 确认。
线程权限失败可完成已验证代码交付，但相关反馈/诊断仍待处理。

修订失败保留原 worker binding；处理脏工作区或远端偏移后可显式 retry，不自动 reset/rebase/force push。
修订准备阶段的 GitHub 错误会持久记录为失败；恢复后显式 retry，未 Started 的准备失败不消耗修订轮数。
CodingAgent 总时限覆盖 resume、fallback 和 VCS 核验，超时回收并等待对应进程组退出；
用户 cancel 通过持久停止握手取消执行，未确认停止或远端结果不明时继续保留绑定。

可选组织 Projects v2 的配置及只读 `flowtool project check <repo>` 见 [GitHub App 接入](docs/architecture/github-app.md#a4-可选的组织-projects-v2)。

Projects 自动同步已接入；`flowtool project status <repo>` 查看状态，`flowtool project retry <repo>` 在修复远端条件后重新核对。
