# M5 阶段计划草案：部署、可靠接收与运维

> 2026-09-18：用户要求直接编写部署文档，并保留 Docker 镜像和 Kubernetes 部署；Cloudflare Tunnel 归入部署环节，不作为产品功能。
> 每 Flow 本地执行容器隔离仍后置。本文为阶段范围；F1 部署文档见 [部署指南](../deployment/README.md)。
> 计划 PR 基线：main@origin / 55313e1f；现状分析亦参考本地 M4 收尾工作。
> 后续 feature 实施时重新核对已合并基线，不把本地 M4 代码视为当前 main 已具备的能力。

## 1. 目标与部署边界

本轮为现有单 serve + PostgreSQL + trusted-host worker 提供直接运行进程的部署文档，以及 Docker、Kubernetes
部署方式。Cloudflare 是部署侧的 webhook 接入选项，直接使用官方 cloudflared。
这三种方式共享角色配置、持久数据目录约定和数据库初始化规则。现有 health 只表示存活；
K8s 需要的就绪探针在 F7 具体计划中单独定义，不作为部署文档的附带代码改动。

- 直接部署文档：使用已有 serve/worker 和官方 cloudflared，说明安装、配置和启动；不新增部署 CLI。
- Docker：提供可构建、可发布的 serve/worker 镜像及 Compose 示例，cloudflared 独立运行。
- Kubernetes：复用上述镜像，提供 Kustomize 清单、Secret 引用、持久卷及内部服务。
- Docker/K8s 容器是服务部署单元，不新增每 Flow 动态容器、Docker-in-Docker 或宿主引擎 socket。
- 保留单 serve、多 worker；首个真实闭环仅需一个 worker。worker 保持固定 identity/root。
- PostgreSQL 可外置；开发 Compose 可继续使用。数据库运维不因支持 K8s 就自动变为托管能力。

现有 trusted-host 的外部隔离和凭据分离要求继续有效。部署容器化不等于互不信任租户支持。

## 2. Feature 与验收

每个 feature 一个 bookmark/PR，目标约 800–1100 行手写增删，包含测试和文档；
小 feature 不凑行数，预计超过 1200 行时继续拆分。以下为交付顺序，不是规模承诺。

| Feature | 范围 | 必需前置 | 验收结果 |
|---|---|---|---|
| F1 [部署文档 · #38](https://github.com/b1indsight/agentflow/pull/38) | 现有安装、角色配置、数据库初始化与启动步骤；文档内 Cloudflare 配置及官方操作命令 | 当前 main | 文档与现有命令一致；明确 health 和事件接收边界，列出公网联调方法；不修改应用代码 |
| F2 [Webhook 持久接收与重放 · #39](https://github.com/b1indsight/agentflow/pull/39) | 验签后、202 前写 inbox；delivery 去重、重试、崩溃重放、请求体限额和保留策略 | 当前 main | 202 后杀进程仍可处理；重复事件不重复建 Flow；写入失败不返回接收成功 |
| F3 [真实最小闭环 · #40](https://github.com/b1indsight/agentflow/pull/40) | GitHub/Codex/Judge smoke 入口、测试仓库操作及证据；承接前序里程碑真实待办 | F1、F2；相应 M1–M4 实现已集成 | Issue→PR→两轮 review→人工 merge→archive 跑通；失败另列修复，不记作通过 |
| F4 [Docker 镜像与 Compose · #41](https://github.com/b1indsight/agentflow/pull/41) | serve/worker 镜像、版本固定、构建/发布流程、Compose、持久卷；可选 cloudflared 服务与 secret 挂载 | F1–F3 | 镜像启动真实角色；重建保留必要数据；镜像层无凭据；角色认证分离；发布实际 registry/tag/digest |
| F5 [结构化日志与指标 · #42](https://github.com/b1indsight/agentflow/pull/42) | 关联日志、队列年龄、worker 在线、lease/replay/reconcile、归档/Projects 错误指标及脱敏 | F2、F3 | 可定位失败 Flow；指标无 Flow ID 等高基数标签；日志无密钥 |
| F6 [运行中取消、drain 与停机 · #43](https://github.com/b1indsight/agentflow/pull/43) | 持久停止请求/确认、进程组回收、迟到结果 fencing；drain 与终止宽限期 | F3 | 未确认停止不复用槽位；离线/进程状态未知保守阻塞；取消/完成/合并竞态收敛；重启不误杀其它进程 |
| F7 [Kubernetes 部署 · #44](https://github.com/b1indsight/agentflow/pull/44) | Kustomize 清单、单 serve、稳定 worker identity/PVC、Secret 引用、迁移 Job、探针和内部服务；可选 cloudflared overlay | F4、F6 | 集群安装/升级/重启可用；serve 不重叠运行；同 worker identity 不出现两个执行者；公网只公开 webhook |
| F8 [常驻运维与恢复验收 · #45](https://github.com/b1indsight/agentflow/pull/45) | 三种方式的托管/升级说明、告警、凭据轮换、DB/repo/session/journal 一致备份和恢复演练 | F5–F7 | 断连/重启/备份恢复有真实证据；不双跑、不重复副作用；记录恢复时间和缺失数据时的阻塞行为 |

F1 仅交付部署文档，不新增命令、安装脚本、readiness 或隧道模块；文档已编写，真实部署待验证。
F1 先验证部署和连通性，不等待 F2；F2 完成前仍有当前 202 后进程退出丢事件的窗口。
F3 尽早跑真实闭环，不等待 Docker/K8s。每个 feature 同时完成自身必要验证。
F4 若超预算，拆镜像与发布、Compose；F6 可拆取消与 drain；F7 可拆清单与升级恢复。

## 3. 共同部署约定

- 同一版本的应用配置和 schema，serve/worker 独立认证与数据目录；运行凭据不烘焙进镜像。
- Cloudflare 固定域名和仅 `/webhook` 路由在三种方式中一致，仅 origin 地址变化。
- 不新增 `flowtool tunnel`、应用内 Cloudflare adapter 或 Cloudflare 配置依赖；安装、认证、
  DNS、配置校验与运行直接使用官方工具，当前配置示例和说明归属 `docs/deployment/`，Docker/K8s 阶段再增加部署清单。
- 应用可在未安装 cloudflared 时正常启动；Cloudflare 凭据只交给隧道进程。
- 同机 worker 可使用显式配置的 loopback WS；跨主机/Pod 使用经验证的内部连接方案，
  不把 webhook 公网隧道兼作 worker 入口，不因在集群内部就默认关闭传输安全要求。
- 首版 K8s 固定副本/身份和持久数据，不提供 HPA、不宣称跨 worker session 迁移或 control-plane HA。
- K8s 更新必须处理旧进程存活和存储挂载边界；不能把 Pod 删除视作执行已停止的充分证据。
- F4 计划中确定镜像 registry、CPU 架构及 worker 所需 Codex/Git/jj 工具链；F7 定义支持的集群和存储基线。

## 4. 后置范围

每 Flow 动态容器/微 VM、强 HOME/session/凭据隔离、默认拒绝的 egress、容器级孤儿清理
后置为 M5 隔离子阶段。多 serve HA、跨 worker session 迁移、Flow 派生和 Web 控制台不在本轮。
原 M5 的强隔离目标仍未完成，本轮只报告接入、部署与运维子阶段验收。

## 5. 计划 PR 状态与实施依赖

2026-09-18：F2–F8 均已创建独立 draft plan PR（#39–#45），尚未批准实施；F1 文档沿用 #38。
每个计划 PR 只增加自己的计划文件，以 main@origin / 55313e1f 为基线，不包含其它 feature 的
代码或计划。阶段总表与集中入口在 #38 维护，避免各分支同时修改目录产生冲突。

依赖表是实施依赖，不是当前 plan PR 的叠加关系。批准后先核对 main 的 M4 集成状态；
需要前置 feature 时将同一 bookmark/PR rebase 到其已合并基线，或明确调整 PR base 再按依赖
实现，不能因为计划 PR 已打开就当作前置功能已经存在。每个 feature 保留自己的实现 PR。

本次远端检查：#29–#37 已在 M4 分支链合并，而 main 尚只含 M4 F1；不执行额外合并。
每 Flow 动态容器/微 VM 等已明确后置的隔离范围，不在这七个待实施 feature 中重新引入。

2026-09-18 实施授权：用户批准按 #39→#45 顺序实施；以 M4 收尾分支 88de4d5e 为实现基线，
#38 及后续 PR 按序叠加，每项只展示自身增量；用户统一 review/合并，本轮不自动合并。
