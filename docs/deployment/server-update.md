# Webhook 镜像更新

已有镜像事件消费者保持原调用方式：
`~/.local/bin/agentflow-server-update --image ghcr.io/…@sha256:… --apply`。
更新器的受版本管理源码为：

- [`update-agentflow-server.py`](../../scripts/server_update/update-agentflow-server.py)：
  GHCR 镜像准备、业务交换空闲等待、单实例停启和操作记录。
- [`agentflow-maintenance.py`](../../scripts/server_update/agentflow-maintenance.py)：
  通过已有 `kubectl exec` 通道设置暂停、读取一致业务快照、恢复接单，不新增公网管理 API，
  不在旧实例内初始化或升级数据库。
- [`column-check.py`](../../scripts/server_update/column-check.py)：只读取得现有列元数据和旧版
  建表语句，在临时 PostgreSQL 中执行候选镜像建表逻辑，识别移除的列，不复制业务数据。
- [`build_installer.py`](../../scripts/server_update/build_installer.py) 和
  [`install.py.in`](../../scripts/server_update/install.py.in)：构建并安装以上工具，复用
  已有 launcher 的 interpreter/context/namespace/workload/container/node 参数。

## Controller 业务交换空闲规则

1. 在暂停业务前拉取不可变 digest，并在无应用凭据/生产数据卷的临时 Pod 中验证导入、
   镜像身份和数据库删列检查。临时数据库仅使用 emptyDir 与 Unix socket，使用固定 digest
   的 PostgreSQL 17 镜像；不连接业务数据库。只读列元数据由旧实例单独提供。
   同时核对目标身份、Linux amd64 节点、单副本 StatefulSet、无 HPA、缩容保留 PVC、
   `/readyz` 探针及至少 45 秒正常退出宽限。检查模式也会创建及清理镜像检查 Pod。
2. 持久化本次 operation 的全局暂停。暂停与任务领取共用数据库事务锁，覆盖所有 Worker，
   包括已绑定 Flow 的 revision/archive 和维护期间新注册的 Worker；不改变 Worker drain。
   不再接纳新的 triage、普通 Webhook 消费和对账/Console 同步批次。
3. 等当前已接纳的业务交换和 Controller 批次结束：注册/恢复、准备派发到 Started 确认、
   工具调用到响应发出、结果持久化/应用到 ACK、停止交换到 ACK，以及 Worker 更新维护的
   单次请求/响应。后台结果恢复也停止接纳新批次，已开始的一批先收尾。等待普通消息和
   Worker 本地 Codex 执行不占用 activity；心跳继续续租，不阻止切换。
   暂停后到达的新工具/结果/维护请求不开始处理，连接以可重试状态关闭；Worker 保留原执行、
   待响应工具和终态 journal，工具/结果在重连后继续或重放；Worker 更新维护操作仍遵守其
   原有断连/故障处理规则。新 Webhook 保留在持久收件队列。
   **running job、持久化派发/stop/result/effect 待办和执行槽不要求归零；
   不等待 Flow 完成、binding 释放或 queued job 清空，不检查固定 Worker 名单或在线状态。**
4. 先将原副本缩到 0，等旧 Pod 正常退出，再切换镜像并恢复 1 个副本。新实例启动保留
   暂停；确认新 Pod Ready、实际 imageID 与已验证镜像一致、实例 ID 已更换后，仅解除
   本次 operation 拥有的暂停。Worker 可陆续重连；暂停期间不因恢复超时使已执行任务失败，
   也不释放绑定。恢复时为已断连的执行及绑定重新给予正常重连宽限；宽限结束仍未恢复的
   执行继续按原恢复规则处理，队列及原 Flow 继续运行。

这条规则只用于 Controller/前端部署。**更新 Worker 进程本身仍必须等待其本地执行结束，
并确认 assignment、result/stop ACK 全部收敛**，见 [Worker 更新](worker-update.md)。

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
  动态 SQL 以 PostgreSQL 实际执行结果为准。拒绝时输出表名和列名，尚未暂停或停止旧服务。
- 新增列、修改默认值和两版都未声明但未被删除的历史残留列放行；不以类型、默认值、
  约束或协议的指纹差异阻止更新。元数据缺失或试跑失败时不继续停机，保留检查 Pod 诊断。
- 此检查不复制业务行，不是备份或完整迁移兼容性证明。依赖业务数据才触发的条件 DDL、
  历史类型漂移、破坏性数据转换及移除仍被使用的 Worker 消息等不在保证范围，仍需作者
  单独安排升级；没有自动数据库回滚或无人值守恢复。

## 本地构建安装包与首次接入

构建只写本地文件，不连接部署主机或执行安装：

```sh
python3 scripts/server_update/build_installer.py --output /tmp/install-agentflow-updater.py
```

**首次接入需要人工维护安装同时支持全局暂停和业务交换 activity 的应用版本。**
安装包检查当前运行镜像及候选镜像的 `COMMUNICATION_IDLE` 能力标记，拒绝仅有旧版全局暂停、
却没有覆盖业务交换的实例，不会把未跟踪的执行误判为空闲，也不退回仅设置 drain。
这是切换安全能力检查，不要求 Worker 同版本。该首次应用部署与后续
主机工具安装都需要单独安排；修改源码或生成安装包不会自动让现有主机采用新逻辑。

在运行维护能力已具备、现有 Webhook/队列/launcher 均已安装的主机上，由操作者转交包
并选择经过发布验证的完整 digest，先检查，再决定是否安装及更新：

```sh
python3 /path/to/install-agentflow-updater.py --image ghcr.io/OWNER/IMAGE@sha256:DIGEST
python3 /path/to/install-agentflow-updater.py --image ghcr.io/OWNER/IMAGE@sha256:DIGEST --apply
```

以现有安装用户执行，不要 sudo 运行整个 Python。`--apply` 才暂停原消费者，取得队列和
consumer 锁，原子切换 launcher 到校验和固定的新版本目录，然后调用更新器；成功后
仅恢复原本 active 的消费者。保留队列历史、Webhook/tunnel 配置和旧更新工具，不重放
旧失败事件，不重置未完成操作。不要同时部署、修改 workload 或更换 launcher。

## 等待、超时与故障

- 更新器 `--timeout` 默认每阶段等待 300 秒。当前工作未收敛时不强制停止 Worker 或 Pod。
  切换尝试前失败/超时，仅在旧 Pod/实例健康、目标未变且暂停归属匹配时恢复接单，操作
  记为 aborted。事件消费者仍将本次事件记为 failed，需要核查后显式重试；不自动排期。
- 一旦尝试停旧/启新，失败或中断保留暂停及 incomplete 记录，不回滚镜像、不恢复数据库、
  不自动恢复接单。未完成记录会阻止盲目重复执行，应先人工核对 Pod、工作负载及日志。
- 状态默认位于 `~/.local/state/agentflow-deploy/<目标标识>/operation.json`，记录权限
  0600，不保存数据库 URL、token 或完整环境变量。镜像检查失败的临时 Pod 保留供诊断。
- 安装失败时消费者可能保持暂停；先检查安装阶段及 operation.json，再决定恢复方法。
  不为重试而直接删除操作记录、队列或 Worker journal。

本轮已在本地真实 PostgreSQL 上验证应用/更新器和 SQLite 事件链路；Kubernetes、GitHub
及 GHCR 边界使用模拟，尚未在真实主机安装或完成真实 Pod 停启验收。
