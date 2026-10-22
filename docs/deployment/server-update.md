# Webhook 镜像更新

Server 更新器在部署主机独立监听端口，接收仓库的 `workflow_run` Webhook；
事件写入本机文件队列后，由后台消费者执行部署。业务 Server 停止时仍可接收请求。
三个脚本从同一受信任版本复制到同一目录：

- [`image_event.py`](../../scripts/server_update/image_event.py)：验签 HTTP 入口、发布核验、文件队列和消费者。
- [`update-agentflow-server.py`](../../scripts/server_update/update-agentflow-server.py)：
  镜像检查、单实例停启和幂等续作。事件消费者直接调用同一个 `Update.apply()`，
  不需要外部 launcher；独立 CLI 也可检查或重试同一个固定镜像。
- [`column-check.py`](../../scripts/server_update/column-check.py)：只读取得现有列元数据和旧版
  建表语句，在临时 PostgreSQL 中执行候选镜像建表逻辑，识别移除的列，不复制业务数据。

## 更新流程

1. main push 触发 [Images](../../.github/workflows/images.yml) 构建、smoke 和 SHA 标签晋升。
   仓库 Webhook 将工作流完成事件推送到更新器 `POST /webhook`，不修改工作流增加 curl。
2. 用独立的非空 `FLOWTOOL_UPDATE_WEBHOOK_SECRET` 验证原始请求的 HMAC-SHA256。
   只接收 `b1indsight/agentflow`、`.github/workflows/images.yml`、main/push、completed/success。
   请求体上限 64 KiB，读取限时 10 秒；合法 ping 返回 200，无关事件返回 204。
3. 按 `(run_id, attempt)` 去重并持久化到本地 JSON，成功后返回 202；同身份 SHA 冲突返回 409，
   存储或锁失败返回 503。202 只代表落盘，不代表部署成功；HTTP 不等待网络核验和部署。
4. 单消费者先续作固定镜像的未完成操作，再按 run ID/attempt 从新到旧消费 queued。
   在队列锁外核验 GitHub 运行、发布步骤、当前 main 和 GHCR candidate/SHA manifest，固定
   digest 和授权。旧 run/attempt 不能替换更新的成功部署；已固定的进行中操作不因 main 前移换版本。
5. 拉取不可变 digest，并在无应用凭据/生产数据卷的临时 Pod 中验证导入、
   镜像身份和数据库删列检查。临时数据库仅使用 emptyDir 与 Unix socket，使用固定 digest
   的 PostgreSQL 17 镜像；不连接业务数据库。只读列元数据由旧实例单独提供。
   同时核对目标身份、Linux amd64 节点、单副本 StatefulSet、无 HPA、缩容保留 PVC、
   `/readyz` 探针及至少 45 秒正常退出宽限。检查模式也会创建及清理镜像检查 Pod。
6. 持久化完整预检结果和停旧意图，将原副本缩到 0，等待旧 Pod 正常退出。
7. 设置已验证的固定 digest，将副本恢复为 1；确认新 Pod `/readyz` 和运行时 imageID，
   清理本操作检查资源，先记录 operation complete，再记录事件 deployed。

Server 更新不设置全局 pause/resume，不等待通信 activity、Worker 更新、任务完成或通信队列清空。
新实例启动后业务路径直接按既有规则恢复；断线 Worker 和绑定继续适用普通 recovery grace，
没有维护豁免或额外 deadline 延长。Worker 正式安装包通过 `flowtool worker --update`
原地自更新并退出，由操作者停机和重启；不发送暂停接单协议，见 [Worker 更新](worker-update.md)。

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

以下由操作者在目标主机执行，本轮没有配置真实服务。准备既有 Python 3.12+ 环境中的
FastAPI、Pydantic、uvicorn，以及有目标集群权限的 kubectl；wheel 只打包 app，须另行复制脚本。
更新器不导入业务 Server、不读取 `FLOWTOOL_DATABASE_URL`、不直连 PostgreSQL。
首次删列快照通过 `kubectl exec` 在旧 Pod 内只读取得，候选 Pod 的 PostgreSQL 仅供隔离试跑。

消费者停止且没有进行中的部署时，复制同版三个文件；不能在部署中逐个覆盖。
本仓库不生成安装器、版本目录、launcher 或工具自升级。

```bash
PYTHON=/path/to/existing-env/bin/python
TOOL_DIR=/path/to/current-updater-directory
STATE_DIR="$HOME/.local/state/agentflow-deploy"
cp scripts/server_update/image_event.py scripts/server_update/update-agentflow-server.py \
   scripts/server_update/column-check.py "$TOOL_DIR/"
TARGET=(--context CONTEXT --namespace NAMESPACE --workload statefulset/NAME \
        --container CONTAINER --node NODE --state-dir "$STATE_DIR")

# 仅首次初始化：目录丢失或损坏时不能用 init 覆盖恢复。
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
"$PYTHON" "$TOOL_DIR/image_event.py" init "${TARGET[@]}"
"$PYTHON" "$TOOL_DIR/image_event.py" verify
"$PYTHON" "$TOOL_DIR/update-agentflow-server.py" "${TARGET[@]}" \
  --image ghcr.io/b1indsight/agentflow-serve@sha256:DIGEST

# 从私密主机配置注入非空 FLOWTOOL_UPDATE_WEBHOOK_SECRET，不在命令或日志中展示。
"$PYTHON" "$TOOL_DIR/image_event.py" listen "${TARGET[@]}" --host 127.0.0.1 --port 9001
```

`9001` 仅为示例端口；在独立进程管理器中托管上述命令，配置合理的重启退避。
将 TLS 反向代理/隧道的独立域名精确转发到该端口 `/webhook`；不能指向被更新的 Server。
仓库 Settings → Webhooks 中配置此 URL、JSON、同一个 secret 和 **Workflow runs** 事件。
业务 GitHub App Webhook 保持原样。先验证签名 ping，再验证真实 Images 发布入队及部署。

部署目标完全来自主机参数；请求不能提供镜像、URL、命令或 Kubernetes 目标。
不同目标不能共用或并行修改同一 workload。GitHub 不自动重投失败的 Webhook，
未接收事件由操作者在 Recent deliveries 重投；不增加发布轮询补偿链路，见
[失败投递说明](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)。

## 本地保存与人工诊断

- 每个 context/namespace/workload 使用独占目录
  `STATE_DIR/<目标标识>/events/<run_id>-<attempt>.json`；同目录保存 `operation.json`。
  目录为 0700、文件为 0600；写临时文件、fsync、原子替换，再 fsync 目录后才应答。
- 队列短锁仅覆盖读写；消费者锁覆盖消费生命周期，部署工具另有目标锁。
  不支持 NFS、多主机共享目录或磁盘灾难恢复。终态历史用于去重和防回退，首版不自动清理。
- 损坏事件、丢失目录、冲突 SHA 不静默重建。队列整体损坏或消费者致命退出须修复后重启；
  消费者已退出时部署请求返回 503。日志和事件不保存 secret、原始请求体或数据库连接串。

复用上面的 Python、工具目录和 TARGET，以同一部署用户执行：

```bash
"$PYTHON" "$TOOL_DIR/image_event.py" status "${TARGET[@]}"
"$PYTHON" "$TOOL_DIR/image_event.py" retry "${TARGET[@]}" RUN_ID ATTEMPT
# 仅监听服务已停时使用独立消费者；失败退出，不与 listen 并发运行。
"$PYTHON" "$TOOL_DIR/image_event.py" run "${TARGET[@]}" --apply --once
# 手工补录也必须经过相同发布核验；不接受任意镜像。
"$PYTHON" "$TOOL_DIR/image_event.py" enqueue "${TARGET[@]}" RUN_ID ATTEMPT SHA
```

`retry` 只将 failed/running 重新入队，或重投同一个失败 Webhook；deployed 重复不再部署，
checked/skipped 不自动转为部署。`run` 默认只检查并终结为 checked，**不要用真实待部署事件演练**；
预检应使用 verify 加直接部署 CLI（不带 `--apply`）。

## 等待、超时与故障

- 更新器 `--timeout` 默认每阶段等待 300 秒。预检、缩容、旧 Pod 退出、启动或身份核验失败时，
  不强制停止 Worker 或 Pod；failed 通过重投或 retry 再次尝试，不做忙重试。
  进程重启优先恢复 running，以及已有停旧意图对应的失败操作；停旧前 failed 不自动重排。
- 一旦尝试缩容或启动新副本，失败或中断保留 incomplete 记录，不回滚镜像、不恢复数据库。
  **同一目标和固定镜像直接重试**，按集群实际状态续作：副本 0 时等旧 Pod 正常删除并补启动；
  目标已启动则只等 Ready/imageID；已经 Ready 则补结果，不再次停启，不依赖旧 Pod 仍存在。
  独立 CLI 重试为上述更新器命令加 `--apply`；无需删 operation.json 或清锁文件。
- Kubernetes 写入响应丢失后先读实际资源；用 workload UID/resourceVersion 条件 patch，
  不覆盖无关配置。操作记录中的配置摘要仅用于检测 Kubernetes 并发配置变化，不是版本兼容门槛。
  检查 Pod 通过固定名称、归属、UID 和输入核验复用，按 UID 条件清理，已删除视为成功。
- 停旧意图持久化前的失败允许后续新镜像；意图落盘后须先完成原固定镜像。
  异镜像未完成操作、workload UID 变化、冲突配置或损坏记录明确失败，不自动回滚或强删。
- 状态默认位于 `~/.local/state/agentflow-deploy/<目标标识>/operation.json`，记录权限
  0600，保存授权、必要列快照、镜像身份及停旧意图，不保存 token 或环境变量。
  镜像检查失败的临时 Pod 保留诊断；同镜像重试只重建本操作拥有的失败检查资源。
- 文件复制或手工更新失败时，先核对脚本是否完整及 operation.json，再决定是否恢复消费者。
  不为重试而直接删除操作记录、队列或 Worker journal。

历史 PostgreSQL 更新事件表和 Server 维护表不自动 DROP；不提供旧草稿记录的自动迁移。
切换前先停旧消费者，由操作者通过集群内路径核对未完成事件和部署历史，明确处置后首次接入；
不要为重试删除权威状态。停旧后故障可能暂时没有可用 Server，坏镜像不能靠幂等重试变健康。

本地验证覆盖签名 HTTP、文件队列、真实列检查和部署 CLI 的中断续作；Kubernetes、GitHub
及 GHCR 边界仍使用模拟，尚未配置真实 Webhook、托管进程或执行真实 Pod 停启验收。
