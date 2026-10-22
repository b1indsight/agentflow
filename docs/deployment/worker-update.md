# Worker 手动更新

Worker 只提供本体 CLI 的手动更新入口：`flowtool worker update`。普通 `flowtool worker` 仍直接
启动执行进程。没有后台发布轮询、自动空闲切换、暂停接单协议或更新专用安装器/launcher；
仓库不再提供 Worker 更新用的 LaunchAgent 模板，也不自动操作系统服务。

Server 与 Worker 的更新互不等待。Server 镜像事件仍使用 PostgreSQL `server_update_events`，
不保存 Worker 更新状态；Server 的预检、停旧和启新流程见 [Server 更新](server-update.md)。

## 只检查发布

```sh
flowtool worker update --sha <40位小写提交SHA>
```

此命令只查询并核验公开 GitHub Images/main/push 成功发布，要求指定 SHA 同时为当前 main head。
成功时输出含 `state: checked` 的 JSON；不下载源码、不准备环境、不读取 Worker 凭据、不连接
Controller，也不修改本地启动方式。无效 SHA 在查询前拒绝。

## 停止旧进程后准备新版本

1. 使用原 Worker 的同一登录用户、HOME 和私有环境配置。保留 Worker ID、token、WSS URL、
   `FLOWTOOL_WORKER_ROOT`、journal、仓库和模型认证；不要通过新建 root 或删除 journal 绕过检查。
2. 按 [取消与停机](../operations/cancel-drain.md) 完成既有任务和结果/停止确认，再人工正常停止
   Worker。若由系统服务管理，操作者应先停止服务并禁用其自动重启，避免它在准备完成后抢先启动旧版。
3. 加载原 Worker 环境后，执行手动准备：

   ```sh
   set -a
   . "$HOME/.config/flowtool/worker.env"
   set +a
   flowtool worker update --sha <40位小写提交SHA> --apply \
     --root "$HOME/.local/share/agentflow/worker-releases"
   ```

`--apply` 和 `--root` 必须同时提供。`--root` 是新版本的存放目录，与保存 journal 和仓库的
`FLOWTOOL_WORKER_ROOT` 不同。更新要求原 Worker root 已有匹配的 `.worker-id`；CLI 在该 root
持有原有进程锁，并在锁内检查 journal。Worker 尚在运行、身份不符、journal 损坏，或仍有任何
未确认记录时拒绝准备。待确认记录必须先用原版本走普通恢复流程，不能清空后重试。

通过检查后，在 `--root/releases/<SHA>-<随机标识>/` 下载固定 SHA 的源码，按 lockfile 建立
non-editable `.venv`，核验实际导入的版本后输出准备结果；不另存更新状态或完成标记。
准备期间始终持有原 Worker root 锁；不覆盖旧环境，不复制 journal 或认证，也不创建新的私密环境文件。

## 人工启动新版本

成功输出的 JSON 包含 `state: prepared`、`release`、`path` 和 `python`。操作者核对后，使用其中
的 `python` 绝对路径启动新版本；以下路径需替换为命令实际输出：

```sh
"/实际release目录/.venv/bin/python" -I -m app.cli worker
```

此进程继续使用已加载的同一组 Worker 环境变量；注册和断线恢复仍走普通协议。CLI 不修改系统
服务的执行路径，不创建启动指针，不停止、启动或重启任何 Worker。若需要常驻运行，由操作者
自行修改现有服务的解释器路径，再人工启动；不要并起同一 ID/root 的两个 Worker。

准备失败时保留不完整目录用于检查；旧版和 journal 不变。修复原因后可重新执行命令，新尝试使用
另一个独立目录，不复用失败环境。清理旧目录前应确认没有进程或服务仍引用它。

此前 PR 草稿的独立命令和 `FLOWTOOL_WORKER_UPDATE_ROOT` / `FLOWTOOL_WORKER_UPDATE_APPLY`
配置已移除；不提供旧 launcher 的自动迁移。若曾安装旧草稿的自动更新服务，需由操作者人工退出
该服务后改用此流程。容器和 Kubernetes Worker 仍通过各自既有部署方式手动更新。

本文不代表真实 GitHub 发布下载、目标主机服务切换或真实编码任务验收已完成。
