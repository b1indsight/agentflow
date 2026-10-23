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
| `FLOWTOOL_BOT_LOGIN` | 实际 bot login |
| `FLOWTOOL_ALLOWLIST` / `FLOWTOOL_REVIEWERS` | 可触发用户 / 请求 review 的用户 |
| `FLOWTOOL_WORKER_TOKENS` | 例如 `{"worker-1":"替换为随机token"}`，与 worker 一致 |
| `FLOWTOOL_HOST` / `FLOWTOOL_PORT` | 本文使用 `127.0.0.1` / `8000` |

无需提前创建 `flow`、`flow:in-review`、`flow:done`：serve 首轮后台扫描会在 App 已授权的
非归档仓库补齐缺失定义。修改名称时同步修改对应配置；已有标签的名称、颜色和描述保留。
未建齐或检查失败每 300 秒重试，确认齐全后每 24 小时复查；检查时间只保存在内存，重启后重新确认。
新安装与新增授权仓库每 300 秒发现，扫描耗时和限流会延后处理；退避不跨重启保存。
App 需有 Issues write 权限，程序不会自动申请权限或扩大仓库授权；有权限仍需等首轮完成后再添加
触发标签。`/readyz` 只验证本地启动与数据库，不代表仓库标签已建好。
Judge 配置只提供给 control，在 serve 的 `.env` 配置 `FLOWTOOL_JUDGE_MODEL`、
`FLOWTOOL_JUDGE_API_KEY`，可选 `FLOWTOOL_JUDGE_API_BASE` 和 `FLOWTOOL_JUDGE_TIMEOUT_SECONDS`。

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
```

本文的明文 WS 只用于同机 loopback 联调；跨主机连接需要 WSS，不通过公开的 webhook 域名连接 worker。
`dedicated-account` 必须对应已建立的真实账号边界。Codex 所需认证在该 worker 账号下独立准备，
不复制 control 环境变量或 Judge 密钥。

Amp 同样只在该专用 worker 身份下认证：使用其专用 HOME 执行 `amp login`，或提供可选
`AMP_API_KEY`（Amp CLI 自行处理其优先级）。不要复制或挂载个人 HOME、Docker socket、control
secret 或个人 Git/gh 配置。每轮 Git/gh 继续使用 GitHub App 下发的 assignment 凭据和临时配置，
不改变个人全局配置；提交作者仍由 worker 的 Git 配置决定。切换为 `amp` 前先 drain 并等待该
worker 的 Flow 收尾；未知续跑不自动重跑。Amp 历史是原生完整 thread 快照，可能滞后于刚发生的
外部续跑，且不承诺离线可用。adapter 固定使用 Amp `--mode low`，不从 worker 环境提供模式覆写。

Amp 的最终业务 JSON 通过 Schema 提示与现有 Pydantic/VCS 核验接收，没有 Codex 的
`--output-schema` 生成约束；无效 JSON、错误终态或不完整输出均失败，不自动整理或重跑。
`threads continue` 的错误（包括尚未充分分类的 thread 不存在）不会触发自动新建；只有任务原本
没有 session 时，沿用 executor 的一次性 start fallback。ARCHIVE 继续使用既有 workspace
释放流程，不会远程删除或归档 Amp thread。

临时 `--settings-file` 限制工具与本轮 MCP，但 workspace 设置优先级更高，因此 adapter 在执行前
拒绝发现的 `.amp/settings.json` / `.amp/settings.jsonc`，不修改这些文件。机器或 workspace 的
[managed settings](https://ampcode.com/docs/cli/settings#managed-settings) 仍可能覆盖/合并本地策略；
运维必须核查专用 Amp 账户的插件、服务端 MCP 与 managed policy，不将 CLI 工具列表当作隔离边界。
有效工具列表检查用于拒收不符合策略的执行结果，不保证在额外工具运行前阻止其副作用。
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
  `workspace-pool.json` 与 `assignment-journal.json` 一起保存所有权、分配代次和恢复证据。
  不要编辑、删除清单后让程序重新接管现有目录。准备中断、未知内容和损坏清单需要人工核查；
  没有实际 checkout 的失败 clone 可安全重试，已执行或未知状态不会自动重放。
- `FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT` 默认 2、最小 1，启动时读取；完整副本、临时
  worktree、准备/清理中和隔离位置共同计数，seed 不计入，各仓库独立计数。降低上限不会删除
  既有位置；原 Flow 可继续，数量回落前不扩容。完整副本归档返回 `released` 并保留目录和
  ignored 依赖/构建缓存；worktree 返回 `removed`，经 Git 注销后删除。`flow show` 会注明目录保留。
- Git worktree 共享对象库，不共享可写依赖目录。tracked 修改、未知非 ignored 文件、进行中的
  VCS 操作或基线会覆盖缓存时拒绝复用，不使用 reset/clean 丢弃内容。jj 完整副本可复用；
  溢出要求独立 Git seed，只准备 Git worktree，不复制 `.jj`。本策略不提供总字节配额或后台 GC。
- token 不自动续期，运行超出有效期可能失败。gh 包装每次调用检查有效期；普通绝对路径 gh
  也继承 bot 环境，但不经包装的本地期限检查。认证最终由 GitHub 判断，任何权限失败都不
  回退个人凭据或自动重放写操作。仍须专用账户/容器/VM 隔离。

取消时先持久保存停止证据，再清理受管 workspace 的未提交修改和未跟踪文件；原 copy/worktree、
已推送 commit 和 ignored 缓存保留，位置归还空闲池。成功后才发送 `ExecutionStopped`，
清理或 manifest 提交失败时保留控制面绑定并重连重试，不增加 ACK 字段或清理完成回执。
旧停止重放仍核对下一任务；已 dispatch 却缺 journal 的普通任务明确失败，不自动重跑。
停止 ACK 保留原位置证明，供取消期间已知合并后的 ARCHIVE 使用；详见[取消说明](../operations/cancel-drain.md)。
当前 #109 仍有取消 REVISION 后显式重试无法重新占用 idle 位置的 medium，尚不建议合并部署。

取消回收基线恢复为原 protocol v14，Amp 会话查询扩展在其上递增为 v15，沿用 journal v4 /
workspace manifest v1，数据库 schema 不变。涉及协议或 journal 格式变更时，由操作者协调
serve/worker 使用同一 commit 或对应镜像升级；更新器不检查或等待 Worker 升级。
不混跑仅含取消回收的 v14、历史含 `release_workspace` 的 v15 或旧 Amp v16 快照。
旧 journal 不自动迁移；在没有活跃/未知执行且归档与停止 ACK 完成后升级两端。
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
