# 部署 FlowTool 与 Cloudflare Tunnel

容器部署见 [Docker / Compose](docker.md) 和 [Kubernetes](kubernetes.md)。

本文使用现有 `flowtool serve`、`flowtool worker` 和官方 `cloudflared`，说明直接运行进程的
部署步骤。示例基于同一主机、不同 OS 账号的首次联调；Docker 镜像和 K8s 清单仍在 M5 计划中。
目前 `/healthz` 仅表示 HTTP 进程可响应；本文不假设已有 readiness 或部署管理命令。

## 1. 准备运行环境

准备 Python 3.12+、uv、PostgreSQL，以及 worker 账号可运行且已配置认证的 Codex CLI、Git
和所需项目工具链。若项目使用 jj，还需安装 jj。serve、worker 应使用同一个应用版本。

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

提前创建 `flow`、`flow:in-review`、`flow:done` 标签；如修改名称，同步修改对应配置。
Judge 配置只提供给 worker，不使用 serve 的 `.env` 传递它。

PostgreSQL 已存在时直接配置连接串。需要临时开发数据库时，可在仓库执行：

```bash
docker compose up -d postgres
```

此命令只运行现有开发数据库，不容器化应用；没有 Docker 时使用自行安装的 PostgreSQL。
仓库 Compose 的示例账号只适合本机开发。远程数据库使用独立凭据和 TLS。
serve 启动时初始化/更新 schema，数据库账号需拥有应用 schema。当前没有单独部署迁移命令；
从旧版本升级前先备份，并阅读 [README 中的升级说明](../../README.md)，不要并行运行新旧 serve。

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
FLOWTOOL_TRUSTED_HOST_EXECUTION=true
FLOWTOOL_EXECUTION_ISOLATION=dedicated-account
FLOWTOOL_JUDGE_MODEL='provider:实际模型名'
FLOWTOOL_JUDGE_API_KEY='替换为模型API-key'
```

本文的明文 WS 只用于同机 loopback 联调；跨主机连接需要 WSS，不通过公开的 webhook 域名连接 worker。
`dedicated-account` 必须对应已建立的真实账号边界。Codex 所需认证在该 worker 账号下独立准备，
不复制 control 环境变量。可选 Judge API 地址使用 `FLOWTOOL_JUDGE_API_BASE`。

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
| delivery 成功但没有 Flow | allowlist、触发事件/标签、App 安装仓库及 serve 日志 |
| CodeAgent/Judge 失败 | worker 的工具链、独立认证、模型配置及 Flow 错误记录 |

前台进程用各自终端的 Ctrl-C 停止。维护前确认正在执行的 Flow，并保留数据库与 worker
持久目录；当前 `flow cancel` 尚不保证终止正在运行的 agent，不能把取消当成 drain。
本文已核对当前代码和官方文档，尚未在用户域名上执行真实部署或公网验收。
