# Kubernetes 部署

模板部署单 serve 和显式单副本 worker-1，依赖外部 PostgreSQL、内部 TLS 和支持
ReadWriteOncePod 的 CSI。它不提供多 control HA、自动扩容或每 Flow 容器。
当前没有在目标 Kubernetes/CSI 上完成真实安装与重挂载验收；不要把 YAML 渲染当作验收通过。

## 前提与配置

先完成 [Docker 镜像](docker.md) 的发布，取同一成功 job 的 serve/worker digest；不要部署
candidate、占位 digest 或混用协议版本。需要 Linux amd64 节点、支持 NetworkPolicy 的 CNI、
IPv4 Pod/Service 网络和支持 RWOP 的 CSI。Kubernetes 1.29+ 已将 RWOP 设为稳定能力，
但仍须验证驱动支持；RWO 不能替代 RWOP。

复制 base 到部署者自己的受控配置目录，修改 kustomization.yaml 的两个镜像 digest、worker
volumeClaimTemplates 的 storageClassName，以及 NetworkPolicy 中外部 PostgreSQL 的 /32 地址。
TLS 证书 SAN 应包含 `flowtool-serve.flowtool.svc`；若修改 namespace/集群 DNS，同步调整
worker 的 WSS URL、证书、Secret 引用及可选 Cloudflare origin。默认 namespace 为 flowtool。
CA bundle 应包含公共根证书和内部 CA，准备方法见 Docker 文档。

模板只引用 Secret，不包含真实值。创建 namespace 后，在其下分别创建：

- flowtool-serve-env：serve 专属 env 文件，含数据库 URL、App ID、Webhook secret、worker token
  映射、allowlist/reviewers、Judge model/API key，以及可选独立 metrics token；
  用户 API 启用时还需 Access issuer/audience/owner email，见 [单账号登录](access.md)。
- flowtool-app-key：`app.pem`。
- flowtool-worker-1-env：worker token、`CODEX_API_KEY`；禁止放 DB/App key 或 Judge API key。
- flowtool-worker-ca：`ca-bundle.pem`。
- flowtool-worker-tls：标准 TLS Secret 的 tls.crt/tls.key。

使用 `kubectl -n flowtool create secret generic NAME --from-env-file=PRIVATE_FILE` 或
`--from-file=KEY=PRIVATE_FILE`，避免把密钥正文写入 shell 历史；文件存私有目录并限制权限。
worker ID 固定为 worker-1，需与 serve token 映射对应。模板不挂载 Kubernetes API token，
worker 也没有 App/DB/Cloudflare Secret。基础 NetworkPolicy 禁止 worker 访问控制面 HTTP
端口，只允许 WSS、DNS 和公网 HTTPS/SSH；使用私有模型代理/仓库时显式补充目的地规则。
它不是域名级模型执行隔离，实际策略行为须在目标 CNI 验证。

## 初次启动

base 的两个 StatefulSet 初始 replicas=0。先渲染检查、创建 Secret，再执行服务端 dry-run：

```sh
kubectl kustomize deploy/k8s/base > rendered.yaml
kubectl apply --dry-run=server -f rendered.yaml
kubectl apply -f rendered.yaml
```

`rendered.yaml` 不含 Secret 正文，但仍应放受控部署目录。模板不自动创建数据库。
使用匹配 serve digest 替换 `deploy/k8s/schema-job.yaml` 中的镜像，单独 apply schema Job；
它只调用 Store.init_db，不启动 router 或 dispatcher。确认没有旧 serve 正在运行：

```sh
kubectl apply -f deploy/k8s/schema-job.yaml
kubectl -n flowtool wait --for=condition=complete job/flowtool-schema --timeout=180s
# 只有上一步成功才继续；失败时查 Job 状态并保持 replicas=0。
kubectl -n flowtool scale statefulset/flowtool-serve --replicas=1
kubectl -n flowtool rollout status statefulset/flowtool-serve --timeout=180s
kubectl -n flowtool scale statefulset/flowtool-worker-1 --replicas=1
kubectl -n flowtool rollout status statefulset/flowtool-worker-1 --timeout=300s
kubectl -n flowtool exec flowtool-serve-0 -c serve -- flowtool workers
```

serve 的 `/readyz` 检查初始化完成和有界只读 DB 探测；`/healthz` 表示 HTTP 可以响应。
serve 不配置 startup/liveness 探针：现有同步 SQL 可能阻塞 HTTP，不能把超时当作进程死亡。
DB 故障或初始化未完成只使 readiness 失败；容器进程退出仍由 Kubernetes 自动重启。
取舍是应用死锁不由 HTTP 探针自动重启，需告警后人工诊断；全面异步化 SQL 不在本次修复内。
worker readiness 检查本进程注册状态及
本地心跳文件新鲜度；busy 不表示故障，也不是绑定可释放的证明。外部模型和 Cloudflare 不参与
存活探针。Service 仅 ClusterIP，内部 TLS 由 serve 同 Pod 的代理提供。

PVC Pending 时检查指定 StorageClass 和 CSI 的 RWOP 支持，不要降级 accessMode 或改成
emptyDir。每个 worker 一个独立 StatefulSet、token 和 PVC；新增 worker 需复制并重命名实例，
同步 serve 的 token 映射和 NetworkPolicy 的 worker selector，不将单实例 replicas 改成 2。
若使用 NodeLocal DNS，需按实际 DNS 地址补充 egress，默认规则匹配 kube-system 的 kube-dns Pod。

## 可选 Cloudflare

overlay 位于 `deploy/k8s/overlays/cloudflare`，填写 tunnel UUID、hostname，另建
flowtool-tunnel-credentials Secret（credentials.json）。按已准备的 base 镜像/存储配置渲染，
确认只转发 `/webhook` 到 serve:8000，catch-all 返回 404。凭据仅挂到 cloudflared 容器。
公网 health/metrics/worker endpoint 不应可访问；对照原生部署文档验证 ingress rule 和 GitHub ping。

## 更新、恢复与限制

当前仅支持完整 schema、协议 v10 / journal v3，不提供历史数据迁移。首次部署使用新数据库和 PVC；
下述流程适用于当前版本恢复与部署操作，不承诺跨版本升级或降级数据兼容。

更新前执行 [drain/停止确认](../operations/cancel-drain.md)，等所有 binding 收敛；保留数据库和
worker root/HOME 的一致备份。两个 StatefulSet 使用 OnDelete，避免修改镜像就自动替换执行者。
先把 worker、serve replicas 降到 0，等待正常删除完成，再运行同版本 schema Job。上一轮 Job
完成后才删除其对象以创建下一轮；schema 失败不启动新版本，不假定回滚镜像能降级 schema。
更新 digest 并依次启动 serve 和各 worker，检查 journal/身份、结果恢复，再 undrain。

terminationGracePeriodSeconds=60 覆盖应用停止窗口。节点断连时，不用 force-delete 或
`--grace-period=0` 绕过 StatefulSet/PVC 所有权；先隔离旧节点并核对旧执行。RWOP 和 StatefulSet
都不替代应用停止确认。不要清 journal 或改身份让探针变绿。

journal 校验包含路径、st_dev、st_ino，CSI 重挂载可能改变这些值。原卷匹配时才恢复；不匹配
保持 blocked，跨节点/复制卷会话迁移尚未支持。必须在选定 Kubernetes、CSI、CNI 版本上分别
记录唯一执行者、挂载身份、DB 故障、正常更新及节点失联证据，然后才宣称该组合可用。

依据：[RWOP 访问模式](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)、
[StatefulSet 受控更新](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#update-strategies)。

## 测试集群 smoke

`scripts/smoke/kubernetes.sh` 要求显式 KUBE_CONTEXT、以 flowtool-smoke- 开头的 KUBE_NAMESPACE、
FLOWTOOL_K8S_STORAGE_CLASS 和 FLOWTOOL_K8S_RENDERED。先把已渲染清单的 namespace、DNS/
证书、digest、CSI 配置与测试环境对应并按上述流程安装；脚本不会替你创建生产部署。
通过 `uv run --with pyyaml==6.0.2 bash scripts/smoke/kubernetes.sh` 运行，默认只检查已有资源。
显式设置 FLOWTOOL_K8S_RECREATE_IDLE_WORKER=yes 才执行 drain、确认无 Flow/JOB、正常删除
测试 worker Pod 并验证普通文件持久化；失败保留资源和 drain 状态，不自动删 namespace/PVC。
输出集群版本和 CSI provisioner 供记录；活动 journal 恢复、节点失联和真实闭环仍需单独验收。
