# AgentFlow

AgentFlow 把 GitHub Issue 或一段任务描述交给 Codex 或 Amp。Worker 编码并推送分支，服务端核验后创建 PR。你在 GitHub 审核，Worker 根据反馈修订；人工合并后，Flow 自动归档。看板、执行记录和时间线在 Web 控制台查看。

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
   AGENTFLOW_HOST=127.0.0.1 uv run agentflow serve
   ```

3. **连接编码 Worker。** 按[部署指南](docs/deployment/README.md)在专用账号、容器或 VM 中配置 Git、gh、Codex 或 Amp 及其认证，通过进程环境设置 Worker ID、token 和服务端 WebSocket 地址，再运行 `agentflow worker`。回到服务端执行 `uv run agentflow workers` 检查注册状态。**已连接只说明通信正常**；开启编码权限前，确保 Worker 读不到服务端 `.env`、数据库凭据或 GitHub App 私钥。

### 在 Python 程序中运行 Worker

`agentflow worker` 作为独立进程运行，收到 `SIGTERM` 后会清理并退出。也可以在隔离的 Worker 宿主进程中导入同一个 `WorkerRuntime`，在已有的 asyncio 事件循环中运行。宿主负责处理信号和配置日志脱敏；若要沿用 CLI 的日志策略，可在宿主启动时调用 [`configure_logging("worker", "text")`](app/telemetry/logging.py)，它会替换进程的日志处理器。

```python
import asyncio

from app.worker.config import WorkerSettings
from app.worker.runtime import WorkerRuntime


async def serve_worker(shutdown: asyncio.Event) -> None:
    worker = WorkerRuntime(WorkerSettings())  # 读取与 CLI 相同的环境变量
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(worker.run())
        try:
            await shutdown.wait()  # 宿主在需要停机时设置此事件
        finally:
            worker.stop()
```

退出 `TaskGroup` 时已等待 Worker 清理完成。两种方式可以并行连接同一服务端，但每个实例须有独立的 Worker ID、token 和工作目录。不要把编码 Worker 嵌入持有控制面凭据的进程。直接调用 Runtime 不持有 CLI 的[安装更新锁](docs/deployment/worker-update.md)，更新其 Python 环境前先停止宿主。

## 创建第一个 Flow

- **从控制台开始：** 用 owner 身份打开部署地址的 `/console/`，点「新建 Flow」，选择已授权的仓库并输入任务（最多 32 KiB UTF-8）。无需先建 Issue。若提交后连接中断，先刷新看板核对，以免重复创建。
- **从 GitHub 开始：** 配好 [Webhook](docs/architecture/github-app.md) 后，由 allowlist 中的用户给目标仓库的 Issue 添加 `flow` 标签（名称可配置）。

没有空闲 Worker 时任务会排队。打开 Flow 详情可以看执行记录、时间线和 PR；在 PR 上审核、留言或请求修改。归档后可在原 Flow 重新开工；重开 Issue 或 PR 不会自动启动编码。

首次编码推送分支后，agent 可用 `request_user_input(message)` 结束当前 Job，不创建 PR。Flow 在审核栏保留待处理说明；没有新输入就不再派发。可直接在控制台成功或废弃归档，也可通过现有输入入口提交新指令或受委托工作的结果：新 Job 会在原会话和工作区继续。该功能不授予其他 agent 跨 Flow 发消息的权限。升级时请一同更新 Control 和 Worker（通信协议 33）。

**每个 Job 都会加载 `AGENTS.md` 吗？** Worker 不会把仓库指令文件的内容加入 Job 提示。文件由 [Codex](https://developers.openai.com/codex/guides/agents-md) 或 [Amp](https://ampcode.com/docs/customize/agents-md) 按各自的规则加载。如果仓库只用 `CLAUDE.md`，Codex 需要将它配置为备用文件名。

本次升级前已归档的 Flow 保留原链接，详情标有「历史记录 · 只读」。升级后新建的 Flow 不受此限制，归档后仍可重新开工。

在能访问数据库的服务端，也可用 CLI 查看进度（将 `42` 换成实际 Flow ID）：

```bash
uv run agentflow flows --active
uv run agentflow flow show 42
uv run agentflow flow history 42
```

## 文档

| 想了解什么 | 入口 |
|---|---|
| 安装、隔离和更新 Worker | [部署与配置](docs/deployment/README.md) · [Worker 更新](docs/deployment/worker-update.md) |
| Flow 受阻、取消、重试或升级 | [运维手册](docs/operations/runbook.md) · [取消与停机](docs/operations/cancel-drain.md) · [升级与恢复](docs/operations/upgrade.md) |
| 想了解项目如何运作 | [架构导读](docs/architecture/README.md) · [模块地图](docs/architecture/components.md) |
| 想跟着一次任务看结果 | [功能走查：从提出需求到归档](docs/feature-walkthroughs/README.md) |
| 开发控制台、运行测试或打包 | [控制台开发](app/controler/web/README.md) · [本地验证](docs/verification/README.md) |

真实 GitHub App、编码服务与目标部署的完整闭环仍待[环境验收](docs/verification/m5-live-e2e.md)。
