# FlowTool（代号，未定名）

FlowTool 把 GitHub Issue 或一段任务描述交给 Codex 或 Amp。Worker 编码并推送分支，服务端核验后创建 PR。你在 GitHub 审核，Worker 根据反馈修订；人工合并后，Flow 自动归档。看板、执行记录和时间线在 Web 控制台查看。

## 快速开始

已有部署可直接跳到[创建第一个 Flow](#创建第一个-flow)。首次从源码启动，需要 Python 3.12+、uv 和 PostgreSQL；从源码使用控制台还需 Node.js（建议 24）。容器部署见 [Docker / Compose](docs/deployment/docker.md) 或 [Kubernetes](docs/deployment/kubernetes.md)。已有实例升级前先看[升级说明](docs/operations/upgrade.md)。

1. **准备服务端。** 将 [GitHub App](docs/architecture/github-app.md) 安装到目标仓库。在源码根目录运行：

   ```bash
   uv sync --locked
   docker compose up -d postgres   # 仅用于本地开发；已有数据库可跳过
   cp -n env.example .env
   chmod 600 .env
   ```

   编辑 `.env`，填写数据库连接、GitHub App 私钥路径、Webhook secret、可触发用户、审核人以及每个 Worker 的独立 ID/token。不要对外使用示例凭据。

2. **启动服务和控制台。** 按 [Cloudflare Access 接入说明](docs/deployment/access.md)为 `/console/` 和同源 `/api/` 配置同一 owner 策略；没有 Access 身份不能打开本地看板。在服务端执行：

   ```bash
   uv run python scripts/build_console.py
   FLOWTOOL_HOST=127.0.0.1 uv run flowtool serve
   ```

3. **连接编码 Worker。** 按[部署指南](docs/deployment/README.md)在专用账号、容器或 VM 中配置 Git、gh、Codex 或 Amp 及其认证，通过进程环境设置 Worker ID、token 和服务端 WebSocket 地址，再运行 `flowtool worker`。回到服务端执行 `uv run flowtool workers` 检查注册状态。**已连接只说明通信正常**；开启编码权限前，确保 Worker 读不到服务端 `.env`、数据库凭据或 GitHub App 私钥。

## 创建第一个 Flow

- **从控制台开始：** 用 owner 身份打开部署地址的 `/console/`，点「新建 Flow」，选择已授权的仓库并输入任务（最多 32 KiB UTF-8）。无需先建 Issue。若提交后连接中断，先刷新看板核对，以免重复创建。
- **从 GitHub 开始：** 配好 [Webhook](docs/architecture/github-app.md) 后，由 allowlist 中的用户给目标仓库的 Issue 添加 `flow` 标签（名称可配置）。

没有空闲 Worker 时任务会排队。打开 Flow 详情可以看执行记录、时间线和 PR；在 PR 上审核、留言或请求修改。无 PR 的调查也可填写结论并结束执行。需要继续已归档的 Flow 时，显式重新开工，系统会在原 Flow 下创建新的 Attempt。修订、重试和切换编码会话沿用当前 Attempt；重开 Issue 或 PR 不会自动启动编码。

在能访问数据库的服务端，也可用 CLI 查看进度（将 `42` 换成实际 Flow ID）：

```bash
uv run flowtool flows --active
uv run flowtool flow show 42
uv run flowtool flow history 42 --attempt 71  # 使用 show 显示的 Attempt ID
```

## 文档

| 想了解什么 | 入口 |
|---|---|
| 安装、隔离和更新 Worker | [部署与配置](docs/deployment/README.md) · [Worker 更新](docs/deployment/worker-update.md) |
| Flow 受阻、取消、重试或升级 | [运维手册](docs/operations/runbook.md) · [取消与停机](docs/operations/cancel-drain.md) · [升级与恢复](docs/operations/upgrade.md) |
| Control 如何调度、交付和处理审核 | [端到端流程](docs/architecture/workflows.md) · [Review 循环](docs/architecture/review-loop.md) · [组件职责](docs/architecture/components.md) |
| 开发控制台、运行测试或打包 | [控制台开发](app/controler/web/README.md) · [本地验证](docs/verification/README.md) |

真实 GitHub App、编码服务与目标部署的完整闭环仍待[环境验收](docs/verification/m5-live-e2e.md)；各模块的实现与验证范围见[架构状态表](docs/architecture/README.md#当前实现状态)。
