# macOS 原生 Worker 按执行空闲更新

此流程只接入**已在 macOS 登录用户下运行的原生 Worker**。它保留现有 Worker ID、token、
`HOME`、worker root、journal 和 Codex 认证；Worker 不会取得数据库、GitHub App、Webhook
或 Server 管理凭据。Linux 的 systemd、容器、Kubernetes/Compose Worker 自动更新仍未提供。

Server 更新和 Worker 更新是独立流程。Server 可在 Worker 尚未升级时部署；Worker 发布、离线、
未启用更新或更新失败都不改变 Server 镜像事件的结果。Server 的部署边界见
[Server 更新](server-update.md)，运行时协议和现有 WSS/token 鉴权仍必须满足。
Controller/前端只等待当前业务交换结束，允许 Worker 保持本地执行并在重连后继续；下面的
严格执行空闲条件只约束替换目标 Worker 进程，不能用来阻止 Controller 部署。

## 边界和所需文件

更新工具位于源码树：

- `scripts/worker_update/install.py`：首次接入时渲染 plist，或在显式 `--apply` 下准备首个
  immutable release、私有环境文件和 launcher；它从不执行 `launchctl`、不停止旧 Worker。
- `scripts/worker_update/launcher.py`：复制到更新根目录的稳定父进程。它只在 `RunAtLoad` 或
  一次经过授权的正常交接后启动子进程；`KeepAlive` 为 false，更新不会发送 SIGTERM。
- `agentflow-worker-update`：本地 check/apply CLI。它只向**拥有当前 runtime、执行器和 journal**
  的 Worker 提交请求，不能建立第二条同身份 WebSocket，也不能绕过空闲确认。

更新根目录（以下记为 `$UPDATE_ROOT`）和 Worker root 是不同概念。前者含 launcher、
`environment.json`、`current.json`、`operation.json`、启动授权及 immutable releases；后者仍是
`FLOWTOOL_WORKER_ROOT`，含执行 journal 和仓库。两者必须由同一登录用户拥有，且不能为了更新
复制、移动、清空或恢复 journal/root。

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
宽松权限、已有启动指针/未完成操作，或与当前登录用户不一致的 `HOME`。它会把实际 HOME 写入
私有环境，并设置 `FLOWTOOL_WORKER_UPDATE_ROOT`。Codex 登录状态与 `CODEX_HOME` 保持在这个
用户身份下，不能改用 control 用户的模型认证或 secret。

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

## 本地 check/apply

`agentflow-worker-update` 始终要求完整小写 SHA：

```sh
# 只校验公开发布元数据；不改请求、暂停或启动指针。
agentflow-worker-update --sha <40位SHA>

# 写入原子本地请求；必须由已 launcher 托管的同一 Worker runtime 接收。
agentflow-worker-update --sha <40位SHA> --apply --root "$UPDATE_ROOT"
```

check 仅输出 `checked` 和 release 元数据。apply 需要已有 `current.json`，以原子创建
`request.json` 的方式提交给 runtime；它不直接 fetch、切换 `current.json`、暂停 Worker 或
启动新进程。实际 apply 还要求环境中的 `FLOWTOOL_WORKER_UPDATE_APPLY=true`、macOS、由 launcher
启动的 immutable release，以及当前 launcher claim、父 PID、SHA、venv 与 root 一致。

收到本地 apply 或周期检查发现的发布后，runtime 先在最终目录准备并验证固定 SHA 的非 editable venv。
准备失败时不会取得连接 scoped 的暂停。准备成功后，Server 和 Worker 必须分别确认同一次空闲：Server 检查
running job、已授权但未 Started 的派发、未确认 stop、未应用 result、未知/未完成 effect 和执行槽；
Worker 检查 executor/task、assignment 及 result/stop ACK 的持久化。Flow/binding 数、journal 文件、
health 文件、离线状态或单独 `_execution is None` 都不能替代这些确认。

目标 Worker 的连接 scoped pause 归本次 operation 所有，只阻止它领取新任务；其他 Worker 继续运行。
暂停和领取在 Server 的同一事件循环中串行生效，调度时排除暂停的连接；已领取的任务继续收尾。
确认后先持久化 cutover intent，再原子切换
`current.json`，由旧 Worker 正常退出、释放 root lock，launcher 解析固定 release 后启动一次新实例。
新实例必须以相同 Worker ID、root/HOME、journal 身份、新 epoch 和实际 SHA 注册。resume 确认
通过后，Worker 先持久化本地 complete，再发送 ready 恢复派发，避免 resume 回复丢失时提前接单。
若连接在完成前断开，认证的 `Register.update_pending` 会携带本地 operation 的
pause/commit 意图，在新连接上恢复 fence；它不是第二条连接，也不改变普通身份或 epoch 校验。
等待 review 的绑定仍适用普通 recovery grace；Worker 更新不保存或授予独立的 Server 侧维护豁免。

准备前失败不会暂停；切换意图前失败只会在原实例健康且 pause 归属匹配时 abort。写入 cutover 后的
超时、断连、启动失败或断电保留本地 incomplete `operation.json`；连接仍在时保持暂停，重连时恢复
该连接的 fence，离线绑定仍按普通恢复规则处理。不自动回滚代码、清除暂停或删除记录，也不重放
启动授权。launcher 对该 operation 只消费一次启动授权；人工先
核查私有 operation/startup 记录、进程和 Server 状态，再决定恢复步骤。

## 自动发布检查

每个由 launcher 托管且启用自动 apply 的 Worker 自行每 **300 秒**查询并核验公开 GitHub Images
发布；它只采用成功的 `main` 发布和已验证的固定 SHA。检查、准备和切换都属于目标 Worker，
不需要也不会调用 host Worker notifier、`kubectl` Worker publisher 或 Server 端发布队列。

Server 镜像事件仍是独立流程：`scripts/image_event.py` 使用项目既有的 `psycopg` 环境和
`FLOWTOOL_DATABASE_URL` 将 Server 事件写入 PostgreSQL `server_update_events`，即使 Server 停止时
也可运行。它不使用 SQLite，不创建 `worker_releases` 或 `worker_updates`，也不持久化 Worker 的
检查、准备、暂停或完成状态。Worker 的持久恢复依据仅为其本地 `$UPDATE_ROOT/operation.json`。

因此 Server 全局维护只等待已有的短通信回执，不等待任何 Worker 更新完成；Worker 更新的离线、
失败或未启用不回写、阻塞或重试 Server 事件。本文没有执行真实 GitHub 发布检查、launchd 或真实
Worker 更新验收。
