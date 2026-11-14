# 部署 FlowTool 与 Cloudflare Tunnel

容器部署见 [Docker / Compose](docker.md) 和 [Kubernetes](kubernetes.md)。
常驻服务、告警与维护见 [运维手册](../operations/runbook.md)。
用户 API 的 GitHub 登录、单账号限制和 MFA 见 [Cloudflare Access](access.md)；
本文默认仍仅公开 Webhook，不将整个 serve 暴露到公网。

本文使用现有 `flowtool serve`、`flowtool worker` 和官方 `cloudflared`，说明直接运行进程的
部署步骤。示例基于同一主机、不同 OS 账号的首次联调；容器方式使用上面的专门文档。
`/healthz` 表示 HTTP 进程可响应，`/readyz` 还检查启动状态与数据库；不提供部署管理命令。

## 1. 准备运行环境

准备 Python 3.12+、uv、PostgreSQL，以及 worker 账号可运行且已配置认证的 Codex CLI 或 Amp
CLI、Git、gh 和所需项目工具链。若项目使用 jj，还需安装 jj。Worker 通过独立的显式命令更新；
Server 部署不等待所有 Worker 升级，但连接中的 Worker 仍须通过现有协议和身份鉴权。

在 control 账号的应用检出目录执行：

```bash
uv sync --locked
cp env.example .env
chmod 600 .env
```

已有 `.env` 时直接编辑，不执行覆盖复制。按照 [GitHub App 接入说明](../architecture/github-app.md)
创建 App、安装到测试仓库、配置权限和事件。编辑 `.env` 中的以下配置：

| 配置 | 内容 |
|---|---|
| `FLOWTOOL_DATABASE_URL` | 实际 PostgreSQL 连接串 |
| `FLOWTOOL_GITHUB_APP_ID` | App ID |
| `FLOWTOOL_GITHUB_PRIVATE_KEY_PATH` | control 账号可读的私钥路径 |
| `FLOWTOOL_GITHUB_WEBHOOK_SECRET` | 与 GitHub App 设置相同的随机 secret |
| `FLOWTOOL_ALLOWLIST` / `FLOWTOOL_REVIEWERS` | 可触发用户 / 请求 review 的用户 |
| `FLOWTOOL_WORKER_TOKENS` | 例如 `{"worker-1":"替换为随机token"}`，与 worker 一致 |
| `FLOWTOOL_HOST` / `FLOWTOOL_PORT` | 本文使用 `127.0.0.1` / `8000` |

Bot login 自动取自当前 App 的 `slug[bot]`，无需手工配置；旧 `.env` 中的
`FLOWTOOL_BOT_LOGIN` 已不再读取，可以删除。解析成功后在当前进程共享，重启后重新查询。

无需提前创建 `flow`、`flow:in-review`、`flow:done`：serve 首轮后台扫描会在 App 已授权的
非归档仓库补齐缺失定义。修改名称时同步修改对应配置；已有标签的名称、颜色和描述保留。
未建齐或检查失败每 300 秒重试，确认齐全后每 24 小时复查；检查时间只保存在内存，重启后重新确认。
新安装与新增授权仓库每 300 秒发现，扫描耗时和限流会延后处理；退避不跨重启保存。
App 需有 Issues write 权限，程序不会自动申请权限或扩大仓库授权；有权限仍需等首轮完成后再添加
触发标签。`/readyz` 只验证本地启动与数据库，不代表仓库标签已建好。
Judge 配置只提供给 control，在 serve 的 `.env` 配置 `FLOWTOOL_JUDGE_MODEL`、
`FLOWTOOL_JUDGE_API_KEY`，可选 `FLOWTOOL_JUDGE_API_BASE` 和 `FLOWTOOL_JUDGE_TIMEOUT_SECONDS`。

完整配置且依赖预检通过时，control 自动管理一个仅提供 infer 的 bub mini worker，无需额外部署。
其就绪、推理槽空闲且能力匹配时优先领取；未配置、半配、忙或其他条件不满足时选普通 worker。
若只希望使用 Codex/Amp，清空 control 的 model/key/base 三项。预检失败只记录安全分类，不注册 bub。
总预算沿用 `FLOWTOOL_JUDGE_TIMEOUT_SECONDS`（默认 60 秒），包含排队、最多 3 次授权派发、通信与退避，
截止和次数持久化；清理可有有界余量，但超过预算的答案不再接纳。worker 无需 bub key，使用 `FLOWTOOL_CODING_AGENT=codex|amp`
及对应专用账号的原生认证、trusted-host 和隔离配置。能力登记只检查本机可执行文件和目录，
不证明 provider 登录、额度或外部服务可用。

将 worker root 放在任何 Git 仓库/worktree 之外。推理在其 `inference/<workspace>/answer-*` 临时目录启动独立
会话，结束、超时和取消时清理目录；不向 agent 注入 Flow/GitHub capability，也不记录推理 journal。
空目录与“只生成答案”的指令不能禁止原生工具或自定义凭据产生副作用；仍须遵守本页隔离要求。
每台 worker 一个独立推理槽，编码忙仍可推理，draining/recovering/failed 的 worker 不选用。
普通 worker 同时提供 execute/infer，无需已有 Flow/repo 绑定；推理取消不会停止编码任务。
每个 worker 初始化一个无仓库默认 workspace，不占仓库配额。本地 bub 的默认目录在 controller
进程内创建，跨连接重试保留，请求使用独立临时子目录；退出清除，不创建编码 workspace 或 journal。
断线重试可能与旧计算重叠并重复计费，不保证模型调用恰好一次。

serve 和 worker 需协调升级到 wire24，journal5 与执行结果 inbox1 不变；Control 本地归档通知保留 inbox2，无旧 wire fallback。
wire24 分开记录历史交付 head、当前 PR 观察 head 和 Worker 实际 fetch 基线，允许同来源分支外部 push/rebase/force-push。升级前 drain 并收敛旧执行和未决回执，再更新两端；回滚前先收敛新版任务。历史任务/结果的 JSON 与 digest 保持不变，无数据库列迁移；不自动重跑未知执行或恢复旧 BLOCKED Flow。详见[实施记录](../plan/external-pr-updates.md)。
wire23 的 assignment 必须包含已核对的 App bot login 和用户 ID；control 在签发时读取 bot 用户，失败不回退个人账号。升级前 drain，待在途执行收敛后协调更新两端；恢复任务领取新凭据，journal 不保存身份或 token，无数据库迁移。
wire22 的 RevisionTask 冻结 answer/revise 用途；旧 worker 不能接收 answer 并按改码任务运行。升级前收敛在途任务和未确认的旧直接答复；旧 answer 继续按原 marker 核对，不转成 worker 任务重发。历史 context/decision/result digest 不重算，不自动重跑被旧上下文阻塞的批次。
任务输入使用纯文本；展示标题复用该推理队列，首轮准备后有界尝试一次逻辑调用（总预算 30 秒），不阻塞编码。
生成前或失败时依次显示 Issue 缓存标题、PR 标题、Flow 编号；不会因 Issue 编辑自动开启会话或重新推理标题。
workers 现为 `(worker_id, workspace)` 复合主键；旧 schema 须显式迁移，不能直接启动新版本。
耗尽通知只写应用内执行记录；READY 后续扫描可创建新一轮 Job，WAITING 仍需新的有效反馈等原有触发。
页面进入执行记录或显式刷新时读取通知，不轮询、不增加推理重试按钮。

PostgreSQL 已存在时直接配置连接串。需要临时开发数据库时，可在仓库执行：

```bash
docker compose up -d postgres
```

此命令只运行现有开发数据库，不容器化应用；没有 Docker 时使用自行安装的 PostgreSQL。
仓库 Compose 的示例账号只适合本机开发。远程数据库使用独立凭据和 TLS。
serve 启动时初始化/更新 schema，数据库账号需拥有应用 schema。当前没有单独部署迁移命令；
不要并行运行新旧 serve。Webhook 驱动的 Server 镜像更新只保留定向删列检查，不执行备份、
恢复或广泛的 schema/协议兼容性检查，详见 [Server 更新](server-update.md)。

## 2. 启动 serve 和 worker

在 control 账号的应用目录启动 serve，保留该终端：

```bash
FLOWTOOL_HOST=127.0.0.1 uv run flowtool serve
```

另一个 control 终端验证：

```bash
curl --fail http://127.0.0.1:8000/healthz
uv run flowtool workers
```

预期 health 返回 `{"status":"ok"}`；此时 worker 列表可能为空。

为 worker 使用专用 OS 账号及独立应用检出目录，执行 `uv sync --locked`。确保该账号不能读取
control 的 `.env`、App 私钥和隧道凭据。仅换终端或更换 repo 目录不等同于账号隔离。
在 worker 账号下，将下列配置填入自己创建的私有文件（例如 `~/.config/flowtool/worker.env`），
文件仅包含 shell 变量赋值，权限设置为 `600`；替换 token、模型和 API key 占位符：

```bash
FLOWTOOL_WORKER_ID=worker-1
FLOWTOOL_WORKER_TOKEN='替换为serve登记的token'
FLOWTOOL_SERVE_WS_URL=ws://127.0.0.1:8000/internal/workers/connect
FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true
FLOWTOOL_WORKER_ROOT=~/.flowtool/workers/worker-1
FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT=2
FLOWTOOL_TRUSTED_HOST_EXECUTION=true
FLOWTOOL_EXECUTION_ISOLATION=dedicated-account
# 默认 codex；仅在该空闲 worker 明确采用 Amp 时改为 amp。
FLOWTOOL_CODING_AGENT=codex
FLOWTOOL_AMP_RUN_TIMEOUT_SECONDS=1200
FLOWTOOL_CODEX_RUN_TIMEOUT_SECONDS=1200
```

两个 `*_RUN_TIMEOUT_SECONDS` 均为编码的**空闲**秒数，默认 1200，必须为正有限值。
直接 CLI stdout/stderr 读到非空新数据即续期（包括半行、工具调用/结果和进度）；连续
20 分钟无新输出才终止，持续输出没有编码总时限。worker 心跳、历史查询、EOF 和进程存活
不续期；静默工具仍可能超时，持续诊断输出也算活动。历史导出 20 秒、VCS 单命令 30 秒
的总时限不变。活跃任务可能超过 GitHub capability 有效期；本功能不自动续签凭据。
更新 worker 并在空闲时重启后生效；controler 也需更新才能显示准确的执行阶段和失败原因。
推荐先更新 controler，或同步更新两端；无需数据迁移，不自动重试已失败的 Flow。

本文的明文 WS 只用于同机 loopback 联调；跨主机连接需要 WSS，不通过公开的 webhook 域名连接 worker。
`dedicated-account` 必须对应已建立的真实账号边界。Codex 所需认证在该 worker 账号下独立准备，
不复制 control 环境变量或 Judge 密钥。

Amp 同样只在该专用 worker 身份下认证：使用其专用 HOME 执行 `amp login`，或提供可选
`AMP_API_KEY`（Amp CLI 自行处理其优先级）。不要复制或挂载个人 HOME、Docker socket、control
secret 或个人 Git/gh 配置。每轮 Git/gh 继续使用 GitHub App 下发的 assignment 凭据和临时配置，
不改变个人全局配置。control 用当前 App 的 JWT 查询 `GET /app`，核对 App ID 和非空 slug，
自动使用 `slug[bot]`，再查询并核对真实 Bot 用户及正数用户 ID；身份查询失败时准备失败，
不接受其他 App 的 bot。评论恢复、Webhook 及反馈轮询共用这个身份，忽略大小写匹配；
Webhook 的身份查询遇到 GitHub 错误时按现有退避等待，并遵守 `Retry-After` 秒数或 HTTP 日期；
新事件不会提前触发查询，等待可随 controller 停止取消。receipt 保持待处理，不在数据库事务中访问 GitHub。
首次编码准备时，在当前 workspace 的 Git
`config.worktree` 中设置 bot 的 `user.*`、`author.*`、`committer.*`，保存原有效作者并安装
hooks；完成后记录 bot 身份。后续准备只核对当前 App 身份，不重写配置或 hooks。
本地 `commit-msg` hook 在追加原账号的 `Co-authored-by` 前核对实际 Author/Committer，
身份不符则拒绝提交，不自动修复配置。它覆盖 `git commit -m` 和交互编辑，保留已有 hooks
和其他 coauthor，不重复署名。显式 `--no-verify` 跳过此 hook。
linked worktree 只在共享配置启用 `extensions.worktreeConfig`（必要时迁移 main worktree 的
`core.worktree/core.bare`）；seed 与兄弟 worktree 的身份、hooks 设置不被替换。需要 Git 2.31+。
继承的 `GIT_AUTHOR_*`/`GIT_COMMITTER_*` 不传给 agent；不通过环境变量注入 bot 身份。
只读 answer 不准备署名；没有原账号时不虚构 coauthor。此机制不配置 jj 或改写已有提交，
原生 jj/`commit-tree` 等不运行 Git hooks。`--amend` 保留非 bot 作者时会被 hook 拒绝；
显式 `--reset-author` 可使用当前 bot 作者，但不能绕过既有的提交祖先与交付基线校验。
切换为 `amp` 前先 drain 并等待该
worker 的 Flow 收尾；未知续跑不自动重跑。Amp 历史是原生完整 thread 快照，可能滞后于刚发生的
外部续跑，且不承诺离线可用。adapter 固定使用 Amp `--mode low`，不从 worker 环境提供模式覆写。

Amp 的最终业务 JSON 通过 Schema 提示与现有 Pydantic/VCS 核验接收，没有 Codex 的
`--output-schema` 生成约束；无效 JSON、错误终态或不完整输出均失败，不自动整理或重跑。
`threads continue` 的错误（包括尚未充分分类的 thread 不存在）不会触发自动新建；只有任务原本
没有 session 时，沿用 executor 的一次性 start fallback。ARCHIVE 继续使用既有 workspace
释放流程，不会远程删除或归档 Amp thread。

执行期间不要从其他客户端并发继续同一 worker thread；手工续跑的公开历史在后续快照刷新后可见。

历史导出单次最多 20 秒、2 MiB，最多两个并发；只缓存已脱敏的公开块，缓存至多 16 个 thread，
两秒后重新导出。页面最多 500 项、256 KiB；导出超限报告 `history_limit`，不展示不完整快照。
控制侧单次 worker 查询允许 30 秒，覆盖导出与清理预算，不在导出仍正常进行时提前按旧 10 秒超时。
公开历史与执行诊断共用脱敏规则，过滤 `sgamp_` 等 token；初始化前失败保留有界、脱敏 stderr，
仍不据此自动重跑任务。
取消/超时先 SIGTERM，等待最多三秒后升级清理捕获到的子进程组。`ps` 必须可用（Linux worker
镜像包含 procps）；进程树快照有竞态，任意双重 fork/脱离进程树的守护进程不在可靠回收保证内。
worker 容器是长期运行的，不会在每次取消后销毁；需要更强保证时必须使用外部每次执行隔离。

在 worker 应用目录加载自己编写的文件并启动：

```bash
set -a
. "$HOME/.config/flowtool/worker.env"
set +a
uv run flowtool worker
```

worker 不自动读取 `.env`，上述加载仅针对 worker 私有文件。回到 control 终端运行
`uv run flowtool workers`，确认 `worker-1` 在线。保留 worker root、journal 和会话数据，
不要为重启临时更换 identity/root。启动注册成功尚不代表真实编码闭环已通过。

正式 uv Git 安装的 Worker 使用原安装入口 `flowtool worker --update`，自动查询最新版、
原地更新并核验后退出。操作者先停共享安装的全部 Worker，成功后按原方式重启；
源码检出不执行此命令。安装合同与恢复边界见 [Worker 更新](worker-update.md)。
Compose 和 Kubernetes Worker 继续使用各自部署方式换镜像，没有后台更新或专用服务模板。

Kubernetes Server 的自动部署入口是部署主机独立端口上的仓库 Workflow runs Webhook，
事件保存本地文件，不要求主机访问集群 PostgreSQL，详见 [Server 更新](server-update.md)。
它与下文业务 GitHub App Webhook 的端口、secret 和持久化路径分开配置。

### Skills 与 MCP 继承

Amp/Codex 的 INITIAL、REVISION resume 和无 session 的 start fallback 都沿用 worker 专用
账号、HOME、原生配置位置和任务 cwd。skills/MCP 由各 CLI 按原生规则发现；不复制 controler、
当前对话或其他主机的配置，不分发 skills，也不挂载个人 HOME。所需配置和文件须事先放在
worker 身份可访问的位置。Amp 不再提供替代用户配置的 `--settings-file`，保留
`AMP_SETTINGS_FILE` 和 `.amp/settings.json[c]`；Codex 保留原有 HOME/CODEX_HOME 配置入口。
恢复原生配置也会恢复其中的工具和插件策略，不能再依赖旧 worker 的固定工具白名单。

两个后端继承自定义环境变量（如 `EXAMPLE_MCP_TOKEN`），但先清除 `FLOWTOOL_*`、`GH_*`、
`GITHUB_*`、`GIT_*`、`SSH_*`，仅保留 Git 作者/提交者的 NAME、EMAIL；清除
`AMP_CURRENT_THREAD_ID`、`AMP_THREAD_ID`、`CODEX_THREAD_ID`、`CLAUDECODE` 等父会话状态。
随后注入本轮 assignment 的 Git/gh/MCP 能力；这些保留名称不能用作自定义 MCP 凭据。
需要 GitHub 认证的自定义 MCP 应使用专用凭据配置，不能依赖原样继承个人 `GH_TOKEN`。
环境过滤不改变独立 VCS 核验的最小环境，也不能阻止 full-access agent 读取磁盘或启动其他程序。

`flow` 是 worker 保留的 MCP 名称。Amp 通过 `--mcp-config` 整项覆盖它，其他 server 不变：
INITIAL 使用当前 endpoint 和 token 引用；无当前 broker 的 REVISION/fallback 使用
`http://127.0.0.1:0/mcp` 替换旧 endpoint，并移除旧 token。固定 Amp 版本不支持 MCP 的
`disabled` 字段，因此这不是原生“禁用”状态：CLI 会重试并显示 `flow` 连接错误，其他 MCP
仍可连接。Codex 沿用 `enabled=false`。不要把已有自定义 server 命名为 `flow`。

用户的工具禁用、MCP 权限与 workspace 审批继续交给原生 CLI；worker 不自动批准或覆写
这些规则。配置损坏、缺凭据或必要工具被禁止时，不扩大权限或重放未知执行。
运维仍须核查 [managed settings](https://ampcode.com/docs/cli/settings#managed-settings)、
账户插件及服务端 MCP。CLI 列表不是运行期权限证明，旧 Amp thread 是否保留创建时工具限制
尚未完成模型执行验收；升级不会主动重写旧 thread。
自定义变量、配置文件和第三方工具输出中的秘密不保证被现有脱敏识别；只注入所需凭据，
不要记录整份环境或 MCP headers，仍须专用账号、容器或 VM 隔离。
更新并重启空闲 worker 后生效，无数据迁移；回退仅恢复原启动策略，不改写原生配置。

### 仓库准备与执行位置

- 不需要提前登录个人 gh 或复制 controler token。controler 为每次编码任务下发目标仓库的
  installation token，worker 为 Git 与 gh 注入它；每次执行销毁临时包装和空配置目录。
  App 需授权该仓库且具备 Contents write；worker 不持有 App 私钥，不新增 PR/Issue 权限。
  PR 仍经本地 MCP `open_pr` 提交交付意图，由 controler 在结果通过校验后创建或复用。
- INITIAL 自动 clone 缺失/空的 `<worker_root>/repos/<owner>/<repo>`，使用任务的基线分支及
  remote。已有 seed 不自动 fetch、checkout、reset 或改写 remote；保持所需的已提交基线可用。
  空远端、缺分支、非空非仓库目录、symlink、缺 gh 或过期能力均在编码前失败。SSH remote
  不在当前核验范围内，使用不含凭据的 HTTPS URL。项目依赖、LFS/submodule 另行准备。
- 执行目录位于 `<worker_root>/workspaces/<owner>/<repo>/<slot_id>`；
  `workspace-pool.json` 与 `journals/flow-<flow_id>.json` 一起保存所有权、分配代次和恢复证据，
  runtime 管理多个 Flow，扫描全部 journal，不再用 `current.json` 选择唯一执行。
  旧 `assignment-journal.json` 和选择指针在启动持有 worker root 锁时核对并迁移；
  root 身份、结果和停止证据不因目录层级改变。迁移后不能直接降级到只识别旧路径的程序。
  不要编辑、删除清单后让程序重新接管现有目录。准备中断、未知内容和损坏清单需要人工核查；
  没有实际 checkout 的失败 clone 可安全重试，已执行或未知状态不会自动重放。
- `FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT` 默认 2、最小 1，启动时读取；完整副本、临时
  worktree、准备/清理中和隔离位置共同计数，seed 不计入，各仓库独立计数。降低上限不会删除
  既有位置；原 Flow 可继续，数量回落前不扩容。完整副本归档返回 `released` 并保留目录和
  ignored 依赖/构建缓存；worktree 返回 `removed`，经 Git 注销后删除。`flow show` 会注明目录保留。
- controller 按等待任务发送仓库库存需求，worker 先创建并登记受管目录；没有库存时 job 排队。
  派发直接选择 `(worker_id, workspace)`，同一 worker 可在不同位置同时执行多个 job。
  配置为 3 时，同仓库最多三个 Flow 绑定；review 保留位置，同 Flow 的后续 job 串行。
- Git worktree 共享对象库，不共享可写依赖目录。tracked 修改、未知非 ignored 文件、进行中的
  VCS 操作或基线会覆盖缓存时拒绝复用，不使用 reset/clean 丢弃内容。jj 完整副本可复用；
  溢出要求独立 Git seed，只准备 Git worktree，不复制 `.jj`。本策略不提供总字节配额或后台 GC。
- token 不自动续期，运行超出有效期可能失败。gh 包装每次调用检查有效期；普通绝对路径 gh
  也继承 bot 环境，但不经包装的本地期限检查。认证最终由 GitHub 判断，任何权限失败都不
  回退个人凭据或自动重放写操作。仍须专用账户/容器/VM 隔离。

取消时先持久保存停止证据，再清理受管 workspace 的未提交修改和未跟踪文件；原 copy/worktree、
已推送 commit 和 ignored 缓存保留。成功后才发送 `ExecutionStopped`，
清理或 manifest 提交失败时保留控制面绑定并重连重试，不增加 ACK 字段或清理完成回执。
收到业务 ACK 后将 job/token 结清凭据写入 pool，并上报下一代库存，位置才允许交给其他 Flow。
旧停止重放仍核对下一任务；已 dispatch 却缺 journal 的普通任务明确失败，不自动重跑。
取消期间确认合并时，数据库将原 Flow 的独占预留交接到同位置的新 generation；ARCHIVE 核对
原 journal 和清理凭据后使用该目录。不能改用其他目录，也不能让新 Flow 抢占此交接。

当前协议为 v25，使用 journal v5 / 执行结果 ResultInbox v1 / workspace manifest v3；Control 本地归档通知使用 ResultInbox v2。
文件链接扩展新增 `worker_file_links` 表；本地路径只保存在 worker root 的私有注册表中，接口见[Worker 文件链接](../plan/worker-file-links.md)。
本版允许从持久任务内容创建无 Issue Flow；六态库启动幂等增加输入列及二选一约束。
出现 NULL Issue 记录后旧程序不可直接读取，即使已归档也不能直接降级；须前向修复或按备份整体恢复。
升级前停写并备份数据库和 worker root，核对真实 workspace 身份；旧 workers 单列主键会被启动
检查拒绝，不猜造位置、不自动迁移。Flow 四态迁移工具只处理 Flow 状态，不能替代 workspace 迁移。
涉及协议或 journal 格式变更时，由操作者协调
serve/worker 使用同一 commit 或对应镜像升级；更新器不检查或等待 Worker 升级。
不混跑旧协议。受限 journal v4 / 内嵌回执 v14–v15 只读转换，正常保存才写 v5；
在没有活跃/未知执行且归档与停止 ACK 完成后升级两端。
保留旧数据作人工核查，不将旧 per-Flow 目录迁入池，详见[升级说明](../operations/upgrade.md)。

## 3. 配置 Cloudflare 入口

需要一个由 Cloudflare 管理的域名。按 [官方建隧道说明](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)
安装 cloudflared；macOS 可用 `brew install cloudflared`。在能访问本机 serve 的部署账号下执行：

```bash
cloudflared --version
cloudflared tunnel login
cloudflared tunnel create flowtool
```

记录输出的隧道 UUID 和 credentials JSON 路径。创建独立文件 `~/.cloudflared/flowtool.yml`，
替换以下 UUID、绝对路径和域名；凭据由 cloudflared 使用，不交给 worker：

```yaml
tunnel: YOUR-TUNNEL-UUID
credentials-file: /absolute/path/to/YOUR-TUNNEL-UUID.json
ingress:
  - hostname: hooks.example.com
    path: ^/webhook$
    service: http://127.0.0.1:8000
  - service: http_status:404
```

直接使用官方命令创建 DNS、校验配置并运行，域名同样替换为自己的：

```bash
cloudflared tunnel route dns flowtool hooks.example.com
cloudflared tunnel --config "$HOME/.cloudflared/flowtool.yml" ingress validate
cloudflared tunnel --config "$HOME/.cloudflared/flowtool.yml" ingress rule https://hooks.example.com/webhook
cloudflared tunnel --config "$HOME/.cloudflared/flowtool.yml" ingress rule https://hooks.example.com/internal/workers/connect
cloudflared tunnel --config "$HOME/.cloudflared/flowtool.yml" run flowtool
```

两个路由检查应分别匹配 webhook 规则和 404 兜底。官方支持 hostname/path 匹配和末尾兜底，
详见 [配置说明](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/)。
不要把整个应用的路径都转发到公网。首次联调保持三个进程的终端运行；常驻部署时再用宿主
进程管理器托管，cloudflared 的方式见 [官方服务运行说明](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/run-as-service/)。

## 4. 验证 GitHub 接入

在 GitHub App 设置中填写 `https://hooks.example.com/webhook`，secret 与 serve 配置一致。
通过 App 的 Recent deliveries 检查测试 delivery；同时检查 serve 日志。公网检查：

```bash
curl -i -X POST https://hooks.example.com/webhook -H 'Content-Type: application/json' --data '{}'
curl -i https://hooks.example.com/healthz
curl -i https://hooks.example.com/internal/workers/connect
curl -i https://hooks.example.com/docs
```

无签名 POST 应返回 401，其余路径应返回 404。合法 GitHub delivery 预期返回 202；
202 表示事件已在 PostgreSQL inbox 持久接收，业务处理在后台完成；诊断和重试见
[Webhook inbox 运维](../operations/webhook-inbox.md)。隧道不存储离线事件，未到达系统的
事件仍需在 GitHub 核查并按需重新投递。

用 allowlist 账号在测试仓库触发 Flow，通过 `uv run flowtool flows --active` 和
`uv run flowtool flow show <FLOW_ID>` 查看进度，再验证 PR、review 和归档。
记录实际应用/cloudflared 版本、delivery ID、PR 和验收时间；不要将凭据写入验收记录。

## 5. 排障与停止

| 现象 | 核对项 |
|---|---|
| 公网入口不可达 | cloudflared 是否运行、DNS 指向、隧道连接日志 |
| 隧道返回 origin 错误 | 本机 `/healthz`、serve 监听地址与端口 |
| webhook 返回 401 | GitHub App 与 serve 的 webhook secret 是否一致 |
| webhook 返回 404 | hostname、精确 `/webhook` 路径和配置匹配顺序 |
| worker 不在线 | identity/token、连接地址，以及是否错误继承了 control secret |
| 仓库缺少 FlowTool 标签 | App 授权范围、Issues write 权限、是否暂停/归档；日志 `repository labels pending` / `inventory deferred` 的 kind、status、request ID；修复后等待重试 |
| delivery 成功但没有 Flow | allowlist、触发事件/标签、App 安装仓库及 serve 日志 |
| CodingAgent/Judge 失败 | worker 的工具链、独立认证、模型配置及 Flow 错误记录 |

前台进程用各自终端的 Ctrl-C 停止。维护前确认正在执行的 Flow，并保留数据库与 worker
持久目录；当前 `flow cancel` 尚不保证终止正在运行的 agent，不能把取消当成 drain。
本文已核对当前代码和官方文档，尚未在用户域名上执行真实部署或公网验收。
