# M5 F7：Kubernetes 部署

> 状态：已批准，清单/探针/文档已实现，真实集群验收待环境。2026-09-22。
> 文档 PR 基线 main@origin / 55313e1f；实现依赖 F4 镜像、F6 stop/drain；F5 指标可选接入。
> 每 Flow 执行隔离后置；K8s 仅部署固定 serve/worker 服务。沿用同一 PR 实现。
> 阶段范围：[PR #38](https://github.com/b1indsight/agentflow/pull/38)。

## 1. 首版支持范围

交付 Kustomize base、Cloudflare 可选 overlay、测试集群 smoke 和部署/更新文档。
使用 F4 已发布 digest 镜像、一个 serve 和显式定义的 worker 实例、外部 PostgreSQL。
首版测试一个实施时固定版本的 Linux amd64 Kubernetes + CSI 组合；不宣称支持任意 StorageClass。
不提供 Helm/operator/HPA、多 serve HA、任意 worker 身份复制或跨 worker session 迁移。

每个 worker 一个独立单副本 StatefulSet（名称即 identity），每实例独立 token Secret 与 PVC。
增加 worker 通过新增配置实例，不用 replicas 扩容同一 identity；serviceAccount 不自动挂载 API token。
serve 使用单副本 StatefulSet 和受控更新，不依赖 Deployment Recreate 来证明永无并发实例。
同数据库只允许部署一套 control 实例；需要多 control 的全局 fencing/leader election 后置。

## 2. 网络与认证

- serve ClusterIP 内部服务；worker WSS 经受控内部 TLS 代理，挂载可信 CA 和域名证书，
  不默认打开 allow_insecure_worker_ws。模板列清 DNS SAN 和证书更新步骤。
- 可选 cloudflared 独立 Deployment + 配置/credentials Secret，仅转发 webhook；
  health/metrics/internal workers 不公开。默认 base 不要求 Cloudflare 账号。
- Secret 按角色拆分；worker 无 App/DB/Cloudflare Secret，serve 不持有 worker 模型认证。
  不在 ConfigMap、镜像、示例 env 或日志保存真实凭据；secretName 由部署者指定。
- NetworkPolicy 限制控制面/数据库内部入口，文档列出 CNI 前提；不把普通 NetworkPolicy
  宣称为已实现的域名级 agent egress 隔离。

## 3. 数据卷与运行所有权

worker root 与 HOME/session 固定容器内路径、稳定 UID/GID、专属 PVC，使用支持
ReadWriteOncePod 的 CSI；仅 ReadWriteOnce 不等于只有一个 Pod 可访问，不能静默降级。
Pod 驱逐/重建后先验证 root identity、journal 和旧 assignment；网络失联的旧 Pod 未确认停止
时不可 force-delete 后拉起替代执行者。未知执行仍按 F6 阻塞，不能清 journal 让探针变绿。

现有 journal identity 使用路径/st_dev/st_ino，F4 已要求验证容器重建兼容性。实施必须在
目标 CSI 实测重挂载：匹配则恢复，不匹配则明确拒绝。若实现安全的持久 UUID + 绑定版本
迁移成为必要前置，单独拆计划，验证旧根仍被隔离且迁移记录可追踪；本 PR 不直接删校验。
持久 PVC 和 StatefulSet 身份本身不证明会话可跨节点恢复，交付说明写清已验证存储范围。

## 4. 探针、schema 与升级

此阶段新增应用 `/readyz`：启动/schema 初始化完成且 DB 有界只读探测成功才 200，
失败/未完成/draining（若对应 serve 维护状态）503；存活 `/healthz` 不随 DB 故障失败，
避免数据库故障引发重启风暴。worker readiness 表示进程/注册状态；忙碌执行不视为不健康。
不将外部模型或 Cloudflare 可达性放入 liveness。

现有 Store.init_db 启动初始化。提供独立 schema Job（复用 Python Store 入口，不新增部署 CLI），
在旧 serve 已停止后手动执行并等待成功，再启动新 serve。Job 无 router/worker 分派副作用。
首版不支持零停机跨 schema 升级；失败时保持旧实例停止，按版本兼容记录恢复，禁止简单回滚
镜像就假定 DB 可降级。若初始化必须改造，变更限于可复用的 schema-only 入口。

更新步骤：F6 drain worker → 等待已绑定 Flow 收敛或显式 cancel 并确认停止 → 备份 →
停止旧 serve → schema Job → 更新固定 digest → 启动 serve/worker → 验证身份和结果恢复。
worker 采用 OnDelete/人工受控更新；terminationGracePeriod 覆盖 F6 窗口；未知存活旧实例
需要先完成节点隔离/核验，禁止以 `--force --grace-period=0` 作为正常恢复方法。

## 5. 文件与验证

`deploy/k8s/base/`：namespace/config、serve/worker StatefulSets、服务、PVC、Secret 引用、
TLS 代理、schema Job 模板与 NetworkPolicy；`overlays/cloudflare/` 提供可选入口。
`docs/deployment/kubernetes.md`：必要集群/CSI/证书、kubectl apply/wait、停机更新及回退边界。
`app/control/api.py`、runtime/Store：仅必要 readiness/schema-only 接缝；对应测试。
`scripts/smoke/kubernetes.*`：显式测试 namespace、digest、存储参数，默认不作用生产集群。

先渲染/静态校验 → readiness 故障测试 → 测试集群 DB/serve/worker → 持久卷与停止竞态 →
schema 失败/升级 → Cloudflare overlay 配置验证 → F3 真实任务复验并记录集群/CSI 版本。

验收：kubectl kustomize 和 server-side dry-run；真实安装/重启后唯一执行者；DB 故障只使
readiness 失败；Secret 跨角色不可见；缺 CSI/证书时给出明确错误；节点断连时保守阻塞；
内部路径不公开；schema Job 失败不启动新版本；PVC 身份不匹配不自动重置。
仅 YAML 验证或 kind 下 emptyDir 测试不算持久部署验收；真实模型证据与 fake 协议 smoke 分开。

预算约 1100 行，超 1200 行拆“基础部署/探针”和“受控升级/恢复”；root identity 迁移另拆。
官方依据：[StatefulSet 强制删除风险](https://kubernetes.io/docs/tasks/run-application/force-delete-stateful-set-pod/)、
[持久卷访问模式](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)。

## 实施证据

本机使用经官方 SHA256 校验的 kubectl v1.35.4 成功渲染 base/Cloudflare overlay，并验证
角色 Secret、OnDelete、RWOP、digest 和入口边界。探针及 PG 回归另附 PR 验证记录。
没有目标集群/CSI 的安装、server-side dry-run、活动 journal 重挂载和节点失联证据，
不宣称任意 CSI 或跨节点恢复可用。保留 draft，运行步骤见 [Kubernetes 部署](../deployment/kubernetes.md)。
