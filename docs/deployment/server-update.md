# Webhook 镜像更新

已有镜像事件消费者保持原调用方式：
`~/.local/bin/agentflow-server-update --image ghcr.io/…@sha256:… --apply`。
更新器的受版本管理源码为：

- [`update-agentflow-server.py`](../../scripts/server_update/update-agentflow-server.py)：
  GHCR 镜像准备、单实例停启和操作记录。
- [`column-check.py`](../../scripts/server_update/column-check.py)：只读取得现有列元数据和旧版
  建表语句，在临时 PostgreSQL 中执行候选镜像建表逻辑，识别移除的列，不复制业务数据。

## 更新流程

1. 拉取不可变 digest，并在无应用凭据/生产数据卷的临时 Pod 中验证导入、
   镜像身份和数据库删列检查。临时数据库仅使用 emptyDir 与 Unix socket，使用固定 digest
   的 PostgreSQL 17 镜像；不连接业务数据库。只读列元数据由旧实例单独提供。
   同时核对目标身份、Linux amd64 节点、单副本 StatefulSet、无 HPA、缩容保留 PVC、
   `/readyz` 探针及至少 45 秒正常退出宽限。检查模式也会创建及清理镜像检查 Pod。
2. 将原副本缩到 0，并等待旧 Pod 在正常退出宽限内退出。
3. 设置已验证的固定镜像 digest，将副本恢复为 1；确认新 Pod 的 `/readyz` 和实际 imageID
   与已验证镜像一致。

Server 更新不设置全局 pause/resume，不等待通信 activity、Worker 更新、任务完成或通信队列清空。
新实例启动后业务路径直接按既有规则恢复；断线 Worker 和绑定继续适用普通 recovery grace，
没有维护豁免或额外 deadline 延长。Worker 本地更新仍只暂停该 Worker 的新任务，并等待它自己的
执行和 ACK 收敛，见 [Worker 更新](worker-update.md)。

前端随 serve 镜像一起更新，不增加独立前端部署。前端、Controller、Console DTO、
非破坏性 schema 或 Worker 代码/配置的变化，不再因源码摘要、schema/wire 指纹、兼容
报告、迁移登记或 Worker 名单校验而阻断。**本流程暂不执行数据库备份、还原或恢复演练，
也没有免备份确认开关。** 运行时协议解析、鉴权和数据库约束仍生效；不兼容 Worker 的
连接错误不会变成要求所有 Worker 升级后才能部署的门槛。

## 自动删列检查

- 仅针对当前 schema 内的普通表和分区表。用旧镜像的 `SCHEMAS` 重建旧结构，补齐业务库
  中残留的历史列/表，再在另一空库创建候选结构，并在旧结构上试跑候选 `SCHEMAS`。
- 旧声明仍在业务库存在、候选声明已移除的列，以及试跑中被删除、删后同名重建或随表
  重建的列都会被拒绝。列的表 OID 和 attnum 一起比较，避免仅比较列名漏掉删后重建；
  动态 SQL 以 PostgreSQL 实际执行结果为准。拒绝时输出表名和列名，尚未停止旧服务。
- 新增列、修改默认值和两版都未声明但未被删除的历史残留列放行；不以类型、默认值、
  约束或协议的指纹差异阻止更新。元数据缺失或试跑失败时不继续停机，保留检查 Pod 诊断。
- 此检查不复制业务行，不是备份或完整迁移兼容性证明。依赖业务数据才触发的条件 DDL、
  历史类型漂移、破坏性数据转换及移除仍被使用的 Worker 消息等不在保证范围，仍需作者
  单独安排升级；没有自动数据库回滚或无人值守恢复。

## 复制部署脚本与首次接入

直接将本文开头的两个脚本从同一受信任版本复制到部署主机的同一目录。先检查现有
`~/.local/bin/agentflow-server-update`，确认其 Python 路径、脚本目录和固定的
context/namespace/workload/container/node 参数；下面的 `TOOL_DIR` 必须是该入口实际使用的目录。
本仓库不生成安装器、维护服务、哈希版本目录或自动替换 launcher。修改仓库源码不会自动更新
部署主机的工具。

仅在事件消费者已安全停止、没有 running 事件或未完成 Server 更新操作的维护窗口替换文件；
不要在部署进行中逐个覆盖脚本。由操作者传送源码并在部署主机执行复制，例如：

```sh
TOOL_DIR=/path/to/current-updater-directory
cp scripts/server_update/update-agentflow-server.py \
   scripts/server_update/column-check.py "$TOOL_DIR/"
```

选择经过发布验证的完整 digest，用现有入口先检查，再决定是否更新 Server：

```sh
~/.local/bin/agentflow-server-update --image ghcr.io/OWNER/IMAGE@sha256:DIGEST
~/.local/bin/agentflow-server-update --image ghcr.io/OWNER/IMAGE@sha256:DIGEST --apply
```

以现有部署用户执行。复制工具与更新 Server 是两个独立的人工操作；确认工具及服务状态后，
由操作者恢复此前运行的消费者。保留事件历史和 Webhook/tunnel 配置，不重放旧失败事件，
不重置未完成操作。不要同时部署、修改 workload 或更换 launcher。

事件消费者使用项目既有的 Python/`psycopg` 环境，并直接读取
`FLOWTOOL_DATABASE_URL`；即使 Server 已停止，该 URL 仍须能连接同一个 PostgreSQL 数据库。
消费者须在安装了本次项目版本的 Python 环境中运行。
`scripts/image_event.py` 的 `enqueue`、`run`、`status` 和 `retry` 都只读写既有
`server_update_events`，不创建独立数据库、不使用 SQLite，也不在这些事件中持久化 Worker
发布或更新状态。

## 等待、超时与故障

- 更新器 `--timeout` 默认每阶段等待 300 秒。预检、缩容、旧 Pod 退出、启动或身份核验失败时，
  不强制停止 Worker 或 Pod；事件消费者将本次事件记为 failed，需要核查后显式重试，不自动排期。
- 一旦尝试缩容或启动新副本，失败或中断保留 incomplete 记录，不回滚镜像、不恢复数据库。
  未完成记录会阻止盲目重复执行，应先人工核对 Pod、工作负载及日志。
- 状态默认位于 `~/.local/state/agentflow-deploy/<目标标识>/operation.json`，记录权限
  0600，仅保存 incomplete 或 complete 的部署报告，不保存数据库 URL、token 或完整环境变量。
  主机文件锁保留；它不是 Server 维护表。镜像检查失败的临时 Pod 保留供诊断。
- 文件复制或手工更新失败时，先核对脚本是否完整及 operation.json，再决定是否恢复消费者。
  不为重试而直接删除操作记录、队列或 Worker journal。

历史已安装的 Server 维护表不由此流程自动 `DROP`；本更新不兼容旧 PR 的部署。

Kubernetes、GitHub 及 GHCR 边界使用模拟，尚未在真实主机安装或完成真实 Pod 停启验收。
