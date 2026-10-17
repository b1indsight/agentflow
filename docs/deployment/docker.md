# Docker / Compose 部署

首版支持 Linux amd64。`serve`、`worker` 是长期服务镜像；每个 worker 固定一个身份和两份
持久卷，不创建每 Flow 容器。现有根目录 `compose.yaml` 仍只用于开发数据库。

## 准备配置

在仓库根目录执行，Docker Engine 和 Compose v2 需已安装：

```sh
export FLOWTOOL_DEPLOY_DIR="$HOME/flowtool-deploy"
mkdir -p "$FLOWTOOL_DEPLOY_DIR/secrets"
chmod 700 "$FLOWTOOL_DEPLOY_DIR" "$FLOWTOOL_DEPLOY_DIR/secrets"
cp deploy/docker/serve.env.example "$FLOWTOOL_DEPLOY_DIR/serve.env"
cp deploy/docker/worker.env.example "$FLOWTOOL_DEPLOY_DIR/worker.env"
chmod 600 "$FLOWTOOL_DEPLOY_DIR/"*.env
```

编辑两个文件的占位值。serve 配置 GitHub App、Webhook secret、数据库、允许用户和 worker
身份/token 映射；worker 只配置自身 token 和模型凭据。两侧的 worker token 必须相同。
GitHub App 私钥放 `secrets/app.pem`。不要挂载个人 HOME 或 Docker socket。
`FLOWTOOL_TRUSTED_HOST_EXECUTION=true` 允许模型在这一 worker 容器内执行命令；仅接受可信仓库。
模型配置和权限参见[原生部署](README.md)。

使用内置开发 PostgreSQL 时，将随机数据库密码写入 `secrets/db-password`，并同步到 serve.env
的数据库 URL（特殊字符须 URL 编码）。生产可用外部 PostgreSQL，省略 database profile。

内部 worker 连接必须使用 WSS。以下证书只服务 Compose 网络名，不是公网证书：

```sh
openssl req -x509 -newkey rsa:3072 -nodes -days 90 \
  -subj /CN=worker-gateway -addext subjectAltName=DNS:worker-gateway \
  -keyout "$FLOWTOOL_DEPLOY_DIR/secrets/tls.key" \
  -out "$FLOWTOOL_DEPLOY_DIR/secrets/tls.crt"
docker compose -f deploy/docker/compose.yaml build --pull
# 保留公共根证书，否则 GitHub/模型 API TLS 验证会失败。
docker run --rm --entrypoint cat flowtool-worker:local /etc/ssl/certs/ca-certificates.crt \
  > "$FLOWTOOL_DEPLOY_DIR/secrets/ca-bundle.pem"
cat "$FLOWTOOL_DEPLOY_DIR/secrets/tls.crt" >> "$FLOWTOOL_DEPLOY_DIR/secrets/ca-bundle.pem"
chmod 444 "$FLOWTOOL_DEPLOY_DIR/secrets/"*
```

Compose 文件挂载的 secret 必须可被容器 UID 10001 读取，私有父目录限制宿主用户访问。
生产可改为受控组权限或 secret manager；不要把该目录放入 Git。证书到期前轮换 TLS key/cert
及 CA bundle，再重建 gateway/worker。serve 不接收模型凭据，worker 不接收 App/数据库凭据。

## 启动与检查

```sh
docker compose -f deploy/docker/compose.yaml --profile database config --quiet
docker compose -f deploy/docker/compose.yaml --profile database up -d
# 仅在容器内部访问控制面；默认不向宿主发布任何端口。
docker compose -f deploy/docker/compose.yaml exec serve flowtool workers
docker compose -f deploy/docker/compose.yaml exec serve python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status)"
```

等待数据库就绪及 worker 显示 online。`/healthz` 是进程探针，不保证完整闭环可用。
没有模型凭据时，连接测试不能证明任务可执行。真实验收使用 [M5 闭环流程](../verification/m5-live-e2e.md)。
复制 worker 服务时，为其分配独立 ID、token、root/home 卷；同时扩充 serve 的 token 映射。
不要 `--scale worker=2` 共享同一个身份和卷。

## 可选 Cloudflare 入口

按[原生部署文档](README.md)在运维机器创建 named tunnel 和 DNS。
将对应凭据 JSON 放 `secrets/tunnel.json`，复制 `deploy/docker/cloudflare.yml.example` 到部署目录
的 `cloudflare.yml`，填写 tunnel UUID 和 Webhook 主机名。只放行精确 `/webhook` 路径：

```sh
docker compose -f deploy/docker/compose.yaml --profile database --profile cloudflare up -d
```

先用 cloudflared 的 ingress validate/rule 检查配置，再执行真实 GitHub ping/redelivery。
Cloudflare 仅在部署层，不能访问模型凭据；worker WS、数据库和后续指标端点不暴露公网。

## 镜像发布与版本

PR 的 Images 工作流只 build/test，不登录 GHCR。合并到 main（以及直接 push main）后自动
构建并发布镜像；tag `v*` 或手工选择 publish 也会执行发布。仓库需自行保护 main、release
tag 和 workflow 权限。默认包名为
`ghcr.io/b1indsight/agentflow-serve` 和 `ghcr.io/b1indsight/agentflow-worker`。
发布先上传 candidate（尚未验收，禁止用于部署），拉取精确 digest 执行 Compose smoke，成功后
将同一 manifest 标记为 `sha-<完整提交 SHA>` / release tag。失败留下 candidate 供诊断，
不会更新 release tag；两个角色的 tag 更新不是跨镜像事务，以同一成功 job 输出的 digest 对为准。
SBOM/provenance 附在候选 manifest 上，晋升不重新 build。

main 发布使用 `sha-<完整提交 SHA>`，不更新 `latest`，也不会自动更新已部署的 server/worker。
例如 main 更新后的 serve 镜像为 `ghcr.io/b1indsight/agentflow-serve:sha-<main 提交的完整 SHA>`；
实际部署仍应使用该成功发布 job 的镜像 digest。

使用 job summary 里的真实 digest 设置 `FLOWTOOL_SERVE_IMAGE`、`FLOWTOOL_WORKER_IMAGE`，
然后 `docker compose ... pull` 和 `up -d --no-build`。当前文档不宣称包已发布。
Python 基础镜像固定 digest，依赖锁在 uv.lock；Codex CLI 0.153.4、jj 0.45.1 固定版本，jj 校验
SHA256。Debian 安全包仍从当前仓库安装，因此源代码相同不等于镜像逐字节相同，部署以 digest 为准。
worker 仅提供 Python、Node/npm、Git、jj、Codex。项目依赖需在派生 worker 镜像构建时安装并测试。
参见 [Codex CLI 安装](https://developers.openai.com/codex/cli)。

## 数据与升级限制

`worker_root` 保存 checkout/journal；`worker_home` 保存 Codex session；`postgres_data` 保存控制面。
备份需同时协调数据库和 worker 文件。不要用 `down --volumes` 操作正式部署。
当前升级先停止新触发，等待现有 Flow 收尾，再停止 worker，保留卷并换镜像。持久 drain 由 F6 补齐。

journal 的 root_identity 包含路径、设备号和 inode。容器重建或卷迁移可能改变身份：已有 journal
必须通过校验才能恢复；失败时保留原数据和 binding，人工确认，不删除 journal 或重写身份绕过检查。
同一卷中的普通文件还在，不代表活动执行可恢复。当前 smoke 只验证无任务 worker 的卷、TLS、
注册、serve 重启后的重连及角色边界；不宣称已完成 terminal journal 重放、多 worker、真实模型、
Cloudflare 公网或跨挂载恢复验收。这些需要实际环境逐项补充证据。

Codex API key 认证使用 worker 的 `CODEX_API_KEY`，不要仅设置 `OPENAI_API_KEY`。
Compose 内部网关通过 Docker DNS 每 5 秒重新解析 serve；重建 serve 换 IP 后无需重启网关，
已有 WSS 会断开并由 worker 重连。镜像 CI 会强制换 IP、保持同一个网关容器验证此路径。
