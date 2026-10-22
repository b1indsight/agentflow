# macOS 原生 Worker 按执行空闲更新

此流程只接入**已在 macOS 登录用户下运行的原生 Worker**。它保留现有 Worker ID、token、
`HOME`、worker root、journal 和 Codex 认证；Worker 不会取得数据库、GitHub App、Webhook
或 Server 管理凭据。Linux 的 systemd、容器、Kubernetes/Compose Worker 自动更新仍未提供。

Server 更新和 Worker 更新是独立流程。Server 可在 Worker 尚未升级时部署；Worker 发布、离线、
未启用更新或更新失败都不改变 Server 镜像事件的结果。Server 的部署边界见
[Server 更新](server-update.md)。Server 更新只等待既有短业务交换，不等待 Worker 更新。

## 边界和所需文件

保留原生安装器、`agentflow-worker-update` 本地 check/apply CLI 和稳定 launcher。安装器
默认只渲染 plist，显式 `--apply` 只准备首个 immutable release、私有环境和 launcher，不执行
`launchctl` 或停止旧 Worker。CLI 只向拥有当前 runtime、执行器和 journal 的 Worker 提交请求，
不能建立第二条同身份 WebSocket，也不能绕过空闲确认。

更新根目录（以下记为 `$UPDATE_ROOT`）和 Worker root 是不同概念。前者含 launcher、
`environment.json`、`current.json` 和 immutable releases（各自带 `complete.json`）；后者仍是
`FLOWTOOL_WORKER_ROOT`，含执行 journal 和仓库。launcher 保留 PID/token/SHA 的运行归属及 root
锁；旧子进程正常退出且 `current.json` 已改变后，才启动新 release。非零退出不自动循环；明确的
人工重启仍可尝试当前指针。两者必须由同一登录用户拥有，且不能为了更新复制、移动、清空或恢复
journal/root。

不再存在 `operation.json`、启动授权目录或 Worker 升级专用状态机。发布元数据仍以
`ReleaseEvent` 用于核验下载；不会经 WSS 交换发布身份。

## 首次接入 LaunchAgent

先在**当前 Worker 的同一登录用户**会话中创建环境 JSON。不要 sudo，不要切换到 root，也不要
用另一个 HOME。环境只允许显式 Worker 配置和 Codex 所需认证变量；至少包含 Worker ID、token、
WSS URL 和 Worker root。生产连接必须使用 `wss://`；`ws://` 仅限已有的开发例外。

```sh
umask 077
mkdir -p "$HOME/.config/flowtool"
cat >"$HOME/.config/flowtool/worker-update-environment.json" <<'EOF'
{
  "FLOWTOOL_WORKER_ID": "worker-1",
  "FLOWTOOL_WORKER_TOKEN": "替换为现有 token",
  "FLOWTOOL_SERVE_WS_URL": "wss://serve.example/internal/workers/connect",
  "FLOWTOOL_WORKER_ROOT": "/Users/you/.flowtool/workers/worker-1",
  "FLOWTOOL_TRUSTED_HOST_EXECUTION": "true",
  "FLOWTOOL_EXECUTION_ISOLATION": "dedicated-account",
  "FLOWTOOL_WORKER_UPDATE_APPLY": "true",
  "CODEX_HOME": "/Users/you/.codex"
}
EOF
chmod 600 "$HOME/.config/flowtool/worker-update-environment.json"

export UPDATE_ROOT="$HOME/.local/share/agentflow/worker-update"
python3 scripts/worker_update/install.py \
  --sha <已验证的40位小写提交SHA> --root "$UPDATE_ROOT" \
  --environment "$HOME/.config/flowtool/worker-update-environment.json"
```

默认命令只验证输入并把 plist 写到 stdout；它不 fetch release、不写 `$UPDATE_ROOT`、不启动或停止
服务。JSON 必须是 0600，所有值必须是字符串；安装器会拒绝额外键、缺失 identity/root/token/WSS、
宽松权限或与当前登录用户不一致的 `HOME`。它会把实际 HOME 写入私有环境，并设置
`FLOWTOOL_WORKER_UPDATE_ROOT`。Codex 登录状态与 `CODEX_HOME` 保持在这个用户身份下，不能改用
control 用户的模型认证或 secret。

审核输出的 plist 后，才由操作者显式执行接入准备：

```sh
python3 scripts/worker_update/install.py \
  --sha <已验证的40位小写提交SHA> --root "$UPDATE_ROOT" \
  --environment "$HOME/.config/flowtool/worker-update-environment.json" --apply
plutil -lint "$UPDATE_ROOT/worker.plist"
```

`--apply` 会在旧 Worker root 锁空闲时准备固定 SHA 的 release、写入 0600 的
`environment.json`/`current.json`、复制 launcher 并写出 plist；它仍**不会**运行 `launchctl`，不会
停止、卸载或加载旧服务。确认旧进程已按既有安全流程退出、plist 内容和登录用户正确后，操作者
自行完成一次 `launchctl bootstrap`/`bootout` 交接。该人工交接不属于自动更新，也不应在未确认旧
进程退出时与其并行启动同一 ID/root。

LaunchAgent 依赖该用户登录。需要无人登录启动时，先设计并审查 LaunchDaemon 方案；不要把此
LaunchAgent 改成 root 服务。首次接入和真实 launchd 操作尚未在目标主机验收。

## 本地 check/apply 与通用暂停

`agentflow-worker-update` 始终要求完整小写 SHA：

```sh
# 只校验公开发布元数据；不改请求、暂停或启动指针。
agentflow-worker-update --sha <40位SHA>

# 写入原子本地请求；必须由已 launcher 托管的同一 Worker runtime 接收。
agentflow-worker-update --sha <40位SHA> --apply --root "$UPDATE_ROOT"
```

check 仅输出 `checked` 和 release 元数据。apply 需要已有 `current.json`，以原子创建
`request.json` 的方式提交给 runtime；它不直接 fetch、切换 `current.json`、暂停 Worker 或启动
新进程。实际 apply 还要求环境中的 `FLOWTOOL_WORKER_UPDATE_APPLY=true`、macOS、由 launcher
启动的 immutable release，以及当前 launcher claim、父 PID、SHA、venv 与 root 一致。

收到本地 apply 或周期检查发现的发布后，runtime 先在最终目录准备并验证固定 SHA 的 non-editable
release。准备失败时不会暂停接单。准备成功后，Worker 在现有认证 WSS 连接发送通用
`WorkerAvailability(paused, request_id, connection_epoch)`，并等待对应的 `AvailabilityAccepted` ACK。
Server 只在当前连接的内存中保存 `dispatch_paused`：它只阻止该 Worker 的**新**任务，不写数据库，
不修改既有任务的 busy 占用；其他 Worker 继续调度。心跳、工具调用、结果 ACK 和停止请求照常处理。

收到暂停 ACK 后，Worker 等本地执行、结果 ACK 和停止 ACK 收齐。连接变更时，它在新连接重新取得
暂停。最终会核验当前连接 epoch、本地空闲和 launcher 的运行归属，再原子写入 `current.json` 并
正常退出；旧进程释放 root lock 后 launcher 启动新 release。新进程仅使用原有 `Register` 和普通
断线重连恢复，不发送升级完成通知，也不注册 SHA。

失败时，Worker 请求解除暂停并确认 ACK；若解除确认也失败，则关闭连接并走普通重连。新连接默认
可接新任务。不存在 cutover/complete/aborted/resume/ready 阶段协议，也不存在升级专用失败恢复或
旧协议兼容路径。

## 自动发布检查

每个由 launcher 托管且启用自动 apply 的 Worker 自行每 **300 秒**查询并核验公开 GitHub Images
发布；它只采用成功的 `main` 发布和已验证的固定 SHA。检查、准备和切换都属于目标 Worker，
不需要也不会调用 host Worker notifier、`kubectl` Worker publisher 或 Server 端发布队列。

Server 镜像事件仍是独立流程：`scripts/image_event.py` 使用项目既有的 `psycopg` 环境和
`FLOWTOOL_DATABASE_URL` 将 Server 事件写入 PostgreSQL `server_update_events`，即使 Server 停止时
也可运行。它不使用 SQLite，不创建 `worker_releases` 或 `worker_updates`，也不持久化 Worker 的
检查、准备、暂停或完成状态。

因此 Server 全局维护只等待已有的短通信回执，不等待任何 Worker 更新完成；Worker 更新的离线、
失败或未启用不回写、阻塞或重试 Server 事件。本文没有执行真实 GitHub 发布检查、launchd 或真实
Worker 更新验收。
