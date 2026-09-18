# M5 F4：Docker 镜像、发布与 Compose 部署

> 状态：已批准并实现镜像、Compose、CI 和部署说明；镜像构建与环境验收待完成。2026-09-18。
> 文档 PR 基线 main@origin / 55313e1f；交付依赖 F1 部署文档、F2 持久接收和 F3 真实闭环。
> M4 后续代码尚未全部进入 main；实施前核对集成版本，原 PR rebase 后继续实现。
> 阶段范围：[PR #38](https://github.com/b1indsight/agentflow/pull/38)。

## 1. 交付与范围

发布 serve/worker 两种角色镜像，提供 Compose 完整示例及可选 cloudflared profile。
使用现有 `flowtool serve` / `flowtool worker`；不加部署 CLI、不在应用封装 Cloudflare。
worker 容器为长期固定 identity/root 的部署单元，不创建每 Flow 容器，不挂载 Docker socket。

首版支持 Linux amd64；arm64 为追加平台，仅在工具链可取得并有实际 smoke 后宣称支持。
默认 registry 为 `ghcr.io/b1indsight/agentflow-serve` 和 `ghcr.io/b1indsight/agentflow-worker`，
实际可发布权限/包名在实施时验证。发布 sha-<commit> 标签和 digest；release tag 指向
经过验证的同一产物，不用 mutable latest 作为部署基线。

## 2. 镜像与配置契约

一个多阶段 Dockerfile，共享 Python 3.12/uv 锁定依赖的构建阶段，两个命名 runtime target：
serve 只带控制面依赖，worker 带 Codex CLI、Git、jj 及明确列出的基础工具链。
基础镜像和工具版本/digest 在实现时固定，记录来源；不假设所有用户项目依赖都已预装。
项目额外依赖通过继承 worker 镜像提供，文档说明，不在启动脚本里任意 curl 安装 latest。

- 非 root 固定 UID/GID，正确处理 entrypoint 参数与 SIGTERM；使用 init 回收子进程。
- worker 持久挂载 root 与专属 HOME/session，工作区内路径跨重建保持稳定；认证在运行时注入。
  serve 不挂 worker 数据，worker 不挂 DB/App/Cloudflare 私钥及宿主个人 HOME。
- 不使用 build ARG/ENV 传 secret；`.dockerignore` 排除 `.env`、认证、`.cache`、`.git/.jj`、
  测试结果和 worker 数据。构建从明确文件集复制，镜像历史/层检查证明无凭据。
- 单 serve，多个显式 worker 服务各自唯一 token/identity/volume；禁止 scale 复制同一 identity。
- runtime settings 沿用当前规则，选择 container 隔离声明不能替代实际挂载/secret 边界。

## 3. Compose 与持久性

`deploy/docker/compose.yaml` 分离 serve、worker、可选本机 PostgreSQL、TLS 内部代理及
可选 cloudflared。默认本地端口只绑定 loopback；通过 cloudflared 只转发 webhook，
不公开 DB、worker endpoint 或后续 metrics。外部 DB 配置可替换开发数据库服务。
跨容器 worker 使用验证证书的 WSS 内部入口，部署文档用官方工具准备本地 CA/证书，
不因 Docker bridge 为私网就默认开启 insecure WS。serve 也可经同一受控内部代理接入。

credentials/私钥分别只读挂载到对应服务；支持的 env/file 注入方式必须与当前 settings
一致，不能写不存在的 `_FILE` 配置。配置示例只含占位符。

当前 journal root_identity 包含绝对路径、st_dev、st_ino；不能假定卷在新容器中保持三者。
实施必须验证重建时的身份检查：相同可验证 root 正常恢复，身份变化时拒绝并保留数据。
本 feature 不自动重写 journal 绕过校验；需要跨挂载身份迁移时先拆前置 feature，经计划确认
后再宣称支持该恢复方式。F7 也必须沿用这项限制，不能只靠 PVC 就声称会话可迁移。

## 4. 构建与发布

- PR CI 只 build/test，不登录 registry 或发布；来自 fork 的代码不能接触发布 secret。
- 受保护 tag 或显式 workflow_dispatch 发布；GITHUB_TOKEN 权限限于 contents read/packages write。
  workflow 固定 action SHA，登录只在发布 job；平台/版本/源 SHA 标签完整。
- 为两个镜像保存 digest、SBOM/provenance 和 smoke 摘要；发布失败不更改已验证 tag。
- 用户可先本地 build/run，实际发布需要验证仓库包权限；本计划 PR 不执行发布。

## 5. 文件与实施顺序

`Dockerfile`、`.dockerignore`、`deploy/docker/compose.yaml` 和示例 env/证书说明；
`.github/workflows/images.yml`；`docs/deployment/docker.md`；镜像/Compose smoke 脚本与
必要的角色/secret 边界测试。不得更改现有开发 compose 的默认行为而不说明迁移方式。

先做干净 build 和镜像层/权限检查 → 两角色启动 → TLS worker 注册 → 持久卷重启 →
可选 Cloudflare 路由检查 → 真任务和重建恢复 → CI build → 受控发布与拉取复验。
复用 F3 测试仓库做真实任务；没有模型凭据的 CI 使用 fake executor 仅验证进程/协议。

## 6. 验收与预算

验证 `docker compose config`、amd64 镜像 build、非 root 权限、信号退出、secret 不入层、
双 worker 身份/卷隔离、DB/serve 重启、terminal journal 重放和重建身份变化时的保守阻塞。
从发布 digest 拉取后复验，记录实际镜像地址；仅 Dockerfile 能 build 不算完整交付。
README 区分 fake smoke 与真实 F3 复验；产品代码若变更执行标准 lint/type/PG 回归。

预算约 1000 行手写增删，镜像生成文件单列；超过 1200 行优先拆镜像发布与 Compose，
身份迁移不能隐藏在 Dockerfile 重构里。K8s、资源级强隔离、任意平台镜像和热升级不在范围。
依据：[Docker 构建 secret](https://docs.docker.com/build/building/secrets/)、
[官方 GitHub Actions 构建工具](https://github.com/docker/build-push-action)。

## 实施证据

见 [部署说明](../deployment/docker.md) 与 `scripts/smoke/docker.sh`。本机没有 Docker，
不能执行 Compose smoke；Images PR CI 负责实际 build/TLS/空闲 worker 重建重连验证。
尚未运行真实任务、双 worker、journal 跨挂载恢复、Cloudflare 公网和 GHCR 发布拉取，
保留 draft，不将静态检查当作本节完整验收。发布使用候选 digest 验证后晋升同一 manifest。
