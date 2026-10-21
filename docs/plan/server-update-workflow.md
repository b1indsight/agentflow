# 单节点 Server 自动构建与部署 — Feature record

**Stage:** Review — 本次仅交付 main 更新后的自动镜像构建与发布。
**PR:** [#93](https://github.com/b1indsight/agentflow/pull/93)

**Current scope:** 本 PR 包含 Images 工作流、镜像发布文档及本记录；不包含本地试用的部署
脚本及其测试，不执行自动部署。下方 Initial plan 保留最初完整设想，实际交付范围以本节和
后续变更记录为准。

**当前后续（Planning）：** 上述 Stage/PR/Current scope 记录历史 #93 的交付。本次草稿
[#104](https://github.com/b1indsight/agentflow/pull/104) 先评审本文末尾的「Server 与 Worker
按执行空闲更新」方案；Worker 实现及此前本地 Server 更新代码待方案批准后进入同一个
PR。当前不含实现，也不执行线上部署。

## 1. Initial plan

### Summary and motivation

当前更新需要手工拉取源码、Docker build、save、导入 containerd、选择 Pod、drain、停启
server、检查 readiness 和恢复任务分配。部署过程中容易复用旧镜像标签、选错集群、漏查
绑定或遗忘 drain 状态。用户要求先实现自动 build 和更新部署，减少重复操作。

首版沿用当前单节点 Linux amd64 homeserver 的本地镜像部署方式。工具在该节点运行，
使用 Docker 构建和 containerd 的 k8s.io namespace 导入；macOS Worker 仍在另一台机器，
只验证它重连，不安装、重启或迁移其 root/HOME。

### Goals, scope, and success criteria

- 一次执行完成固定源码版本的 serve 构建、镜像导入、安全停启、健康检查和 drain 恢复。
- 支持已经安装的单副本 Deployment 或 StatefulSet（包括 OnDelete），明确选择 context、
  namespace、工作负载和应用容器，不依赖 Pod 名称或 flowtool/agentflow 的默认命名。
- 构建或导入失败发生在服务变更前；存在任务、绑定、停止请求或待对账结果时不停止旧服务。
- 普通补丁更新不初始化数据库，不修改 Secret、TLS、Service、NetworkPolicy、PVC 或 Worker。
- 故障保留诊断和安全状态，不强制删除 Pod、不清 journal、不自动开启任务分配。
- 不自动 git pull、切换用户工作区或覆盖本地修改；默认 fetch origin/main 后固定提交，
  也允许显式选择本地 revision。构建固定提交的干净导出，不把未提交内容偷偷打进镜像。
- 不承诺无停机、HA、跨节点分发、数据库迁移、协议升级或自动回滚。

### Proposed behavior

拟提供以下入口；尚未实现：

```sh
python3 scripts/deploy/update_server.py --config /path/to/server-update.json --check
python3 scripts/deploy/update_server.py --config /path/to/server-update.json
# 需要指定已经取得的本地提交时：
python3 scripts/deploy/update_server.py --config /path/to/server-update.json --revision HEAD
```

配置使用 JSON 和 Python 标准库，不依赖 jq、PyYAML 或在宿主安装应用 Python 依赖。
示例配置记录 kube context、namespace、Deployment/StatefulSet 名称、serve 容器名、
预期节点、worker ID 集合、镜像仓库名、containerd socket 和有界等待时间；不存凭据。
kubectl 使用现有 kubeconfig，containerd 权限通过预先配置的用户权限或 sudo -n 获得，
不在中途交互索取密码，不执行任意配置 shell 字符串。

`--check` 仅检查并报告当前本地信息、部署目标和阻塞条件，不 fetch/build/import，不 drain，
不改变集群。正常更新默认先 fetch origin 的 main，仅更新远端跟踪信息，不修改用户工作区；
显式指定 --revision 时使用已经存在的本地提交，不隐含更新该引用。
正常执行先输出选中的提交和部署目标。工具取得本机操作锁，构建并导入镜像，然后进入
维护步骤。任务未空闲时有界等待，超时保留旧服务和已设置的 drain，并输出恢复说明。
只成功完成全部验证后，恢复本次操作改变的 drain 位；原先已 draining 的 Worker 保持原状。

输出保留每一步结果、旧/新镜像、固定源码提交、工作负载/Pod UID 和失败阶段；报告写入
权限受限的本地状态目录，不保存完整 Secret、环境变量、数据库 URL 或 kubeconfig。
中断后的重复执行必须识别未完成操作，先报告现场和恢复指引，不覆盖原始 drain 快照或
盲目再次部署。首次版本以安全拒绝未完成记录为主，不增加自动恢复状态机。

### Technical design and affected modules

- `scripts/deploy/update_server.py`：单个内聚的 Python 运维入口，使用 subprocess 参数列表
  调用 git、docker、ctr、kubectl；所有外部命令有超时，错误终止后续危险步骤。
- `deploy/k8s/server-update.example.json`：不含 Secret 的配置样例，部署者保存在自己的配置目录。
- `docs/deployment/kubernetes.md`：区分日常兼容补丁更新与首次安装/不兼容升级，补充命令、
  权限、备份前提、短暂停机、失败阶段和人工恢复说明。
- `tests/test_server_update.py`：模拟进程边界的顺序/安全约束回归，不依赖真实生产集群。
- 不为脚本扩充服务端公网管理 API；尽量复用现有持久化和运维语义。

执行顺序：

1. 解析和校验配置，按上述规则取得并固定 git commit。检查 context、Node 架构、实际调度节点、工作负载 UID、
   单副本、应用容器、旧 Pod 身份及 readiness；确认本机导入的 containerd 确为目标节点 CRI。
   不满足单节点假设、节点身份无法确认、HPA 等并发副本管理或配置歧义时拒绝运行。
2. 通过 kubectl exec 在旧 serve 容器执行小型结构化诊断，读取真实 worker/binding/job/stop/
   result 状态及当前 schema/protocol 指纹。不解析 `flowtool workers` 的人类可读表格，
   也不要求旧镜像先具备一个新的 CLI 选项。敏感查询值不进入宿主日志。
3. 从固定提交导出临时构建上下文，使用现有 Dockerfile 的 `serve` target 和 linux/amd64。
   镜像标签必须唯一；重建相同提交也不覆盖旧运行版本。将镜像构建结果与导入后的实际
   containerd 镜像身份核对，后续部署不依赖易变化的 latest。
4. 不注入生产凭据地读取候选镜像 schema/protocol 指纹，与旧运行服务比较；不一致时在
   drain/停机前拒绝自动更新，转人工升级。指纹只作为保守阻断，不宣称可证明所有兼容性。
5. 持久记录旧镜像、Pod/工作负载 UID 和每个受管 Worker 原有 drain 状态，再为已确认的
   Worker 设置 drain。按现有持久状态检查绑定、job、stop intent 和待对账结果，不用
   Worker 显示 online 或无 current_job 一项来代替安全停止条件。
6. 再核对 worker 集合、部署身份和安全条件；只将目标 server 缩到 0，等待旧 Pod 正常
   删除并确认旧实例已退出。保留 grace period，不 force-delete，不同时运行两个控制器。
7. 更新目标容器镜像及适用于本地导入的 imagePullPolicy，恢复原单副本。只修改该应用
   更新所需字段，不重新 apply 整套 base 或 schema Job；支持 Deployment 与 OnDelete
   StatefulSet 的显式停启。部署前后检测意外资源变更，避免覆盖其他操作人的更新。
8. 等新 Pod readiness、`/readyz`、实际 imageID 和候选镜像身份匹配；确认原本应在线的
   Worker 完成新连接且无新增恢复阻塞，再恢复本次改变的 drain 位。任一步失败则不 undrain。

声明式配置边界：本次记录实际部署的新旧镜像，但不擅自重写用户的任意 YAML/Kustomize
源文件。日常镜像更新由此入口负责；文档明确以后 apply 旧清单可能回退镜像，部署者应
将新镜像同步到自己的受控配置。首版不引入通用 YAML 重写器或 GitOps 控制器。

### Alternatives and rationale

- **GHCR 发布后拉取**：仓库已有经过 smoke 的发布工作流，适合后续联网发布；当前先减少
  本地构建部署步骤，不把集群拉取仓库的连通性和凭据作为本 PR 前提。
- **一段 shell 顺序拼接命令**：代码少，但难以可靠处理 JSON、原 drain 状态、失败阶段、
  身份校验和单元测试。采用 Python 标准库，仍只保留一个执行入口，避免部署框架化。
- **直接 set image + rollout restart**：可能产生新旧控制器重叠；OnDelete StatefulSet
  也不能假设修改镜像会重建 Pod。保留显式 stop-before-start，接受短暂停机。
- **Argo CD / Helm / 自动远端 SSH**：暂不引入；现阶段单节点与 Mac 原生 Worker 不需要
  为减少命令数量增加新的常驻控制器、发布权限或跨主机传输协议。

### Costs, risks, and mitigations

- 更新工具持有本机容器及目标 K8s 管理权限；显式限定 context/namespace/workload/节点，
  拒绝任意 shell 配置，避免输出凭据。
- 暂停期间可能收到新 webhook；其持久队列保留，不删除或当作执行完成。固定 Worker
  清单和维护期间不并行更改注册配置是首版运维前提，不宣称实现全局分布式维护锁。
- Schema/protocol 相同不代表任意版本都兼容；首版仅用于经过验证的兼容补丁，操作前
  需要一致备份，不能用自动镜像回退替代数据库或 journal 恢复。
- 构建消耗节点 CPU/磁盘且可能遇到镜像网络问题；安排在 drain 前完成，并让失败早退出。
- 新实例失败时可能暂时没有可用 serve；保留旧镜像与现场，输出具体恢复指令，不自动
  重开旧 server、undrain 或重跑业务。人工恢复也需确认候选进程已停止及版本兼容。
- 初版仅支持本机单节点 Linux amd64；远端 Mac 构建、跨节点、registry 模式、Worker
  升级和 root/HOME 迁移不在本次范围内。

### Implementation order

1. 实现配置、只读 check、命令运行边界、操作锁和权限受限报告。
2. 实现固定提交构建、导入、候选镜像身份与兼容性检查。
3. 实现 drain 快照、持久执行安全检查、正常停旧/启新和验证后恢复。
4. 增加故障注入回归、隔离 smoke 与操作文档；在同一 PR 做独立代码审查。

### Validation and ablation strategy

- **Tests:** 使用可记录副作用的假 docker/ctr/kubectl/git 边界，覆盖成功路径、Deployment /
  OnDelete StatefulSet、原已 drain、无 worker/缺 worker/额外 worker、busy binding、待停止/
  待对账、错误 context/节点、架构或指纹不符、构建/导入失败、旧 Pod 删除超时、readiness/
  重连失败、重复执行和并发操作。验证危险步骤绝不提前，失败不 undrain、不删除数据。
- **Real stack:** 本机工具允许时，在独立 Linux VM/一次性 K8s namespace 验证 build/import/
  单实例停启和 imageID，不用 mock 代替该结论；若当前 Mac 无相应环境，报告该项未验证，
  提供可复现 smoke 供目标 homeserver 人工执行，不触碰现有 agentflow namespace。
- **Ablation:** 用同一假命令序列移除“等待旧 Pod 消失”或“保留原 drain 位”步骤，要求对应
  安全用例失败；若完整用例不能捕获该破坏，完善测试后再进入独立审查。
- **Timing:** 实现后、代码 review gate 前执行对照；不为重构细节添加一函数一测试。
- **Checks:** Ruff / format / Mypy（按项目类型边界）、相关 pytest、命令 --help / --check。

### Open questions and deferred work

- **待用户确认的范围假设：**首版在 homeservermain 本机运行 Docker build + containerd import，
  不要求 GHCR，不从 Mac 远程部署；默认只更新 server，维护现有 Mac Worker 身份与数据。
- **实施时核实：**目标 namespace、工作负载种类/名称、serve 容器、节点/CRI socket、worker
  清单放进部署者配置，不硬编码用户现场；权限问题在预检中明确失败。
- **Deferred:** 自动发行、GHCR/离线 tar 传输模式、macOS launchd/Worker doctor、跨版本
  schema 迁移、零停机、无人值守回滚和全局维护锁。

## 2. PR change timeline

### 2026-10-19 — 先交付本地试用脚本，暂不更新 PR

- **Context / requirement:** 用户要求“先不做 pr，先做一个流程脚本我试试”，并希望仓库中的
  实现足够泛用。已有草稿 PR 保持不动，本轮仅保留本地文件，不提交、推送或部署。
- **Decision:** 将首轮范围收敛为参数化的单节点 build/import/stop/start/verify 脚本。
  入口为 `scripts/update_server.py`，默认只读预检；不引入配置框架或服务端管理 API。
  context、namespace、工作负载、应用容器、节点、镜像、Git revision、CRI socket 均可指定。
- **Supersedes for this trial:** 暂不实现初始计划的自动 drain/恢复、Worker 集合/业务持久状态
  检查、schema/protocol 指纹检查和 JSON 配置文件。操作者先完成维护检查，再明确传入
  `--apply --maintenance-confirmed`；脚本永不自动 undrain。该确认不是自动业务安全检查。
  完整维护编排延后，避免把应用内部模型写进通用构建部署入口。
- **Implementation:** 导出固定 Git 提交构建唯一 tag；核对 CRI socket/镜像；构建导入成功后才
  缩至零，正常等待旧 Pod 删除和控制器确认，再更新镜像并恢复一个副本。支持 Deployment /
  OnDelete StatefulSet；拒绝 HPA、多副本及缩容删 PVC。使用本地锁、带 UID/resourceVersion
  的条件 patch 和持久操作记录；未完成记录阻止盲目重试，无自动回退。
- **Validation:** CLI 模拟命令边界覆盖两种工作负载、只读默认值、维护确认、构建/导入失败、
  镜像身份、并发变更、旧 Pod 删除超时和 readiness 失败。真实 Docker/containerd/K8s 验收
  留给目标节点试跑，不把模拟结果当作部署通过。试用步骤和恢复边界写在本地的
  `docs/deployment/server-update.md`，该脚本、说明和测试暂不纳入本 PR。

### 2026-10-19 — 先启用 main 合并后的自动镜像发布

- **Requirement / decision:** 用户要求先做到合并后自动制作镜像；本轮只启用 main push 的
  Images 工作流，复用现有 serve/worker 构建、候选 digest smoke 和 GHCR 晋升步骤。
- **Behavior:** PR 保持只构建测试；main 更新后发布 `sha-<完整提交 SHA>`，保留 `v*` 标签与
  手工发布入口。不增加 latest 标签，不自动更新集群或 Worker。
- **Delivery:** 仅保留本地工作流和文档修改，暂不提交、推送或更新既有草稿 PR。

### 2026-10-20 — 后续方案：Server 与 Worker 按执行空闲更新

**状态：方案草稿，Worker 未实现，等待用户批准。** 此后续工作使用新的 bookmark 和草稿
PR；历史 [#93](https://github.com/b1indsight/agentflow/pull/93) 已合并，不更新或复用已关闭 PR。
批准后在本次同一个 PR 交付 Worker 更新和此前仅保存在本地的 Server 更新代码，不混入
其他控制台功能。不在此任务中安装、停止、重启或部署真实服务。

#### 需求与现有事实

- 用户追加“再加一个 worker 更新部署功能，然后提 pr，要求同上”。此前要求继续生效：
  等当前执行收敛即可更新，不等待 Flow 完成或 binding 释放；新增/修改 Worker 不阻止
  Controller 部署；不备份/还原，不增加 schema/源码/协议指纹、固定名单或全员重连门槛。
  已在本地完成的 Server 定向删列检查保留，后续一并交付。
- 本记录初始计划描述另一台机器上的 macOS 原生 Worker；[运行说明](../operations/runbook.md)
  也仅提供手动 macOS 进程，没有 launchd。实际主机未检查，**本方案暂按 macOS 原生
  Worker 实施，需用户确认**。现有 Images 仅发布 Linux/amd64，不能直接用于 macOS。
- [drain](../operations/cancel-drain.md) 只阻止新 Flow，已绑定的 revision/archive 仍会
  派发；`SIGTERM` 会取消当前执行，不能作为“等空闲”的实现。journal 在已确认结果后
  可继续留作原 Flow 的 provenance，文件存在不代表忙碌。
- [现有网关](../../app/controler/workers/gateway.py) 通过 token 验证 Worker 身份并核对
  Register，协议严格拒绝未知字段。更新通知应走这个已认证的连接；不建立第二个同身份
  WebSocket 抢占当前 epoch，不给 Worker 注入 DB/App/GitHub 凭据。

#### 提议的使用方式和边界

1. **首版覆盖 macOS 原生 Worker。** 提供 launchd LaunchAgent 模板及稳定 launcher，按
   已有运行账号显式配置路径；默认仅生成/检查，不执行 `launchctl`。首次接入由用户安排
   人工维护，确认旧进程已安全退出后再托管。LaunchAgent 依赖用户登录；如需开机无人
   登录运行，应先把方案改为 LaunchDaemon，而不在实现中偷偷改系统服务。
2. **后续事件沿用 GitHub Webhook 入口。** 将本地既有事件消费者整理为受版本管理的
   源码，保留原 Server enqueue/SQLite 队列和调用合同。验证成功的 Images/main/push
   run、attempt、完整 SHA 后，为 Worker 发布独立的原生 release 通知；Worker 更新的
   失败、离线或未启用更新，不改变 Server 事件结果，也不阻止 Controller 部署。
   主机端接入仅提供本地代码、安装包及说明，本任务不替主机安装或改消费者。
3. **通知只包含固定仓库的完整 SHA 与发布事件身份。** Controller 经现有认证 WSS
   发送给声明支持更新的 Worker；无任意命令/脚本/下载 URL 字段，不新增公网管理接口、
   远程 shell 或 SSH 部署能力。旧 Worker 仍可工作，只跳过其自动更新；扩展 Register
   的可选能力字段，不能把支持更新变成所有 Worker 的注册前提。
4. **本地显式启用自动 apply。** 未启用或未由受支持 launcher 托管时只检查/报告。
   暂定运维入口为 `agentflow-worker-update --sha <40hex>` 和带 `--apply` 的版本；
   check 不暂停或改运行指针，apply 请求由拥有执行对象的 Worker runtime 协调完成。
   同一 Worker 一次只允许一个 operation，不用外部 PID 猜测当前执行或发送更新 SIGTERM。
5. **先准备，再暂停。** 对公开仓库做只读 fetch，核实候选为所通知的成功 main 发布且
   没有被更新的 main/已采用版本淘汰。在单独的、唯一的最终 release 目录导出固定 SHA，
   创建锁定依赖的非 editable venv、自检后写入完整标记；不 `git pull` 或 `uv sync`
   正在运行的 checkout。venv 含绝对路径，不在构建后搬动目录；只原子切换启动指针。

#### 执行空闲、停启和恢复

- **只暂停目标 Worker。** 在数据库增加 operation-owned 的 per-worker 维护记录，
  不修改 drain；任务领取与暂停都先取得现有全局维护锁，再按相同顺序锁定目标 Worker，
  领取后复查暂停。公平选择也跳过暂停者；INITIAL、绑定 revision、archive 全部受控。
  暂停提交前已领取的任务继续完成，不归还或伪装为未派发；其他 Worker 正常接任务。
- **两端确认同一次空闲。** Controller 检查目标的 running job、已授权未 Started 派发、
  未确认 stop、未应用 result、未知/未完成 effect 和占用执行槽。Worker 检查当前持有的
  executor/task、assignment 及结果/停止 ACK 持久化。Flow/binding 数、journal 文件存在、
  health 文件、离线或单独 `_execution is None` 都不能代替这两个确认。
- **先记录切换意图，再切换和退出。** 确认绑定 operation、原 Worker 进程实例和当前
  epoch；将 cutover-intent 持久化后才原子切换启动指针。旧 Worker 自行正常退出，释放
  原 root lock，launcher 解析新 release 的真实路径后启动同一身份的新进程。旧进程的
  延迟 import 不经过可变指针。部署状态与 assignment journal 分开保存。
- **新进程就绪后才解除本次暂停。** 需要新进程实例/epoch、实际运行 release SHA、
  Worker ID、root 路径/设备/inode、HOME 和 journal 校验一致；不以“进程出现”或旧健康
  文件代替。保留 root、HOME、repo、session 和凭据，不改 journal 版本、不清历史。
  运行时无法读取 journal 或完成注册是启动失败；不因协议/源码摘要不同提前拦截更新。
- **保留等待 review 的绑定。** 维护期间不因该 Worker 重连宽限过期而回收其空闲绑定；
  恢复时重新给予正常宽限。未知执行、stop/result/effect fence 仍生效。Controller 重启
  保留 Worker 暂停，Server 全局 resume 不能清除它，Worker 维护也不能解除全局暂停。
- **故障保留现场。** 准备失败不暂停；进入切换意图前，只有原实例仍健康且暂停归属
  匹配时才能中止并恢复接单。切换尝试后故障/超时/断电保留暂停和 incomplete 记录，
  不自动回退代码、不自动恢复派发、不盲目重放。launcher 对一个更新 operation 只消费
  一次启动授权，不能用 KeepAlive 无限重试掩盖歧义失败；人工恢复另行明确操作。

#### 最小改动范围与实施顺序

1. 在 `app/persistence/maintenance.py`、`workers.py`、schema 和恢复策略中实现目标暂停、
   同序事务 fence、空闲判定及 binding 宽限，复用已有 Server 暂停的安全边界。
2. 在 `app/contracts/protocol.py`、Controller Worker gateway/connection 和 Worker client
   接入版本明确的更新通知、维护请求/确认；身份来自已有会话，只允许维护自己。维护
   消息必须由统一接收路径分流，不能破坏运行中任务的 tool/result/stop 消息及 ACK。
3. 在 `app/worker/` 增加原生 release 准备及更新协调模块，接入 runtime 的计划退出和
   新实例确认；CLI 只提交受限的本地更新请求，不独立抢占 Worker 连接或执行对象。
4. 在 `scripts/` 纳入事件消费者及 Worker 安装/检查入口，提供 `deploy/launchd/` 模板；
   对 Server 与 Worker 分别记录结果，不将 Worker 的完成条件加入 Server cutover。
5. 整理本线程已有 Server 更新/删列代码，隔离工作区的无关变更，在同一个 PR 中验证、
   审查和交付；同步部署说明、停机 runbook 与 changelog。Linux Worker 的 K8s/Compose/
   systemd 自动更新、跨节点/卷迁移、无人值守回滚和数据库恢复暂不纳入本次范围。

#### 验证与消融（尚未执行）

- PostgreSQL 回归：空闲 active Flow/绑定允许更新；目标暂停阻止新 Flow 和绑定后续
  任务，其他 Worker 继续；用确定性屏障测试领取与暂停两种提交顺序及全局暂停交错。
- 本地进程/协议回归：运行中任务、未 Started 派发、ResultAccepted/StopAccepted 丢失、
  未应用结果与未知 effect 均不能提前退出；ACK 收敛后以原身份和 journal 完成更新。
  串联真实无害子进程和 release/venv，验证旧进程未受准备影响、无双实例、指针变化及
  新实例确认；使用 launchd 命令替身不代表已完成真实服务验收。
- 失败矩阵：准备失败、重复/过期事件、并发 operation、连接断开、切换后启动失败、
  错误 SHA/root/实例、Controller 重启、超出 binding 宽限以及未启用更新的旧 Worker。
  Server 队列历史/去重、空闲更新、Worker 增减不阻断和定向删列回归必须继续通过。
- 消融分别移除目标领取 fence、本地 ACK 确认、实际运行版本确认与维护宽限保护；相同
  场景必须捕获提前分配/停机、旧实例冒充就绪或错误回收。若简化后仍满足合同，就删除
  没有证据支持的机制。实现后、代码审查前执行并记录，不把方案当作验证结果。
- 执行相关 pytest、Ruff/format、Mypy 和现有镜像 smoke；macOS 用 `plutil` 检查模板，
  真实 launchd、发布获取及进程替换在获准的隔离环境另行验收。不得触碰当前实际 Worker。

#### 需要批准的选择

请确认：**首版按当前记录的 macOS 原生 Worker，使用登录用户 LaunchAgent 托管，经过
既有认证 WSS 接收发布通知，再按目标 Worker 执行空闲更新；Linux 容器 Worker 自动部署
后续再做。** 如果实际 Worker 平台或需要无人登录开机运行与此不同，应先修订本方案。

本轮只阅读本地代码和 GitHub 只读元数据，未检查真实 Worker 主机。方案草稿使用本地
Claude Opus 5.5，初始化及响应模型一致、工具/MCP 为空、成功退出；已按实际认证/恢复
实现核对，修正 venv 搬动、切换意图时点及盲目 KeepAlive 等建议。咨询不替代运行验证。

### 2026-10-20 — 实施：独立发布队列、执行空闲切换与本地验证

**实施状态：本地代码、部署说明与回归验证已完成，尚未安装或部署真实服务。** 本条承接上面的
后续方案；前面的“方案草稿 / 未实现 / 等待确认”保留为当时记录。本次实现仍使用
[#104](https://github.com/b1indsight/agentflow/pull/104) 和 `feat/execution-idle-updates`，
没有混入原工作区中的 label、GitHub client 或控制台会话展示改动。

#### 实际实现和决策

- 纳入本地已有的 [Server 更新器](../../scripts/server_update/update-agentflow-server.py)、
  [全局维护暂停](../../app/persistence/maintenance.py) 和
  [定向删列检查](../../scripts/server_update/column-check.py)。暂停关闭任务领取、triage、
  Webhook 消费和对账/Console 同步的新批次，等已接纳执行收敛；活动 Flow、空闲 binding、
  queued job 和 Worker 名单均不作为部署门槛。删列在临时 PostgreSQL 做 schema-only
  对比与重放，覆盖声明移除、动态 DROP 和同名重建；新增列/默认值变化放行。
  无数据库备份/还原、源码/协议指纹或 Worker 全员重连条件。
- [Worker 维护记录](../../app/persistence/worker_updates.py) 与 Server 暂停相互独立。
  `assign_job` 公平选择和加锁后的复查均排除目标暂停；绑定宽限保留。对正在 busy 的
  同一连接允许取得 pause，但不能 commit，直至现有任务和 effect 收敛；其他 Worker
  正常接单。领取先提交、暂停先提交两种顺序均由真实 PostgreSQL 事务验证。
- [协议扩展](../../app/contracts/updates.py) 经原认证 WSS 的单接收器分流，只有
  `Register.update_version=1` 才获得通知。旧 Worker 不需要声明该能力。消息只含固定
  公开仓库的 SHA/run/attempt、operation 和身份，无可执行命令或下载 URL。
- [本地协调器](../../app/worker/updates/coordinator.py) 先验证发布并准备最终路径的非 editable
  release，再申请 pause。Worker 的 executor、assignment、结果/stop ACK 与 Controller
  执行事实都空闲后，先持久化 cutover intent，再请求 commit、切换指针并正常退出。
  实际导入的安装包 SHA、真实父 launcher、venv 路径、root/HOME/journal 和新 instance/epoch
  共同确认完成。丢失 resume 回应只允许同一新实例重连后确认，不能用另一个进程重放。
- [稳定 launcher](../../scripts/worker_update/launcher.py) 使用最终路径解释器和 `-I`，
  旧子进程退出后才启动新实例；每次 cutover 只消费一次持久化启动授权。选择
  `RunAtLoad=true / KeepAlive=false`，启动失败保留暂停和现场，不自动 rollback/retry/resume。
  首次安装默认仅渲染；显式 apply 仍不执行 launchctl。Linux Worker 自动更新继续延期。
- [事件消费者](../../scripts/image_event.py) 保留 Server 队列历史，新增独立 Worker 队列、
  锁、状态和重试入口。Worker notifier 失败后 Server 同一事件仍能部署成功。发布器核对
  Ready Pod 的 StatefulSet 归属和 UID；adapter 再次核验公开发布，只有 apply 才写 DB，
  不初始化 DB。相同 run/SHA 的更高 attempt 可替换发布，旧 attempt 和身份冲突被拒绝。
- 新增 [Worker 部署指南](../deployment/worker-update.md)，同步
  [Server 指南](../deployment/server-update.md)、README、runbook 与 changelog。
  首次接入和主机端 notifier/消费者安装由操作者另外安排，本任务不访问部署主机。

#### 验证结果和边界

在隔离 jj workspace 使用独立 PostgreSQL 17（`127.0.0.1:50528`，每个测试独立 schema）：

```sh
PYTHONPATH="$PWD" FLOWTOOL_TEST_DATABASE_URL="$SCRATCH_TEST_DATABASE_URL" \
  /path/to/test-venv/bin/python -m pytest -q
/path/to/test-venv/bin/python -m ruff check app scripts tests
/path/to/test-venv/bin/python -m mypy app tests
python scripts/build_console.py
uv build --no-sources --out-dir .amp/in/artifacts/dist
```

- 全量 **927 passed、0 skipped**，仅既有 Starlette 弃用告警；Ruff 通过，新增文件及更新器
  29 个文件 format check 通过；Mypy 213 个源文件无错误。`uv lock --check` 通过，无新依赖。
- [Worker 切换测试](../../tests/test_worker_cutover.py) 验证真实 journal ACK、准备失败、
  commit 回应丢失、指针写失败、check-only、新实例确认及新旧 epoch。实际生产两端
  parser/reader 和 PostgreSQL 在内存双向 transport 上交换通知/维护回复，保留执行帧顺序；
  没有为维护另外建立同身份连接。
- [launcher 测试](../../tests/test_worker_launcher.py) 使用真实最终路径 venv 和无害子进程：
  旧版延迟 import 仍读旧代码，同 root 锁无重叠，journal 字节不变，新版失败不重放启动。
  单独复制候选包并写 build stamp，执行 `uv sync --locked --no-dev --no-editable`，
  以 `python -I` 实际导入安装包并读到预期 SHA，CLI entrypoint 的 `--help` 通过。
- Console 生产构建、sdist/wheel 成功；wheel 包含构建后的 console。macOS `plutil -lint`
  检查模板和真实 render 输出通过，默认安装未创建目标目录。构建仅有现存的 bundle-size 告警。
- Docker 不可用，本地镜像 smoke 未执行。Kubernetes、GHCR/GitHub 发布和主机服务边界用替身；
  未做真实 launchd、完整原生 Worker 进程与真实 WSS/发布链路的联合验收，也未安装/停止/重启
  实际服务。这些结果不能解释为已部署。PR CI 以随后推送的实现 head 为准。

#### 已执行消融

临时脚本 `.amp/in/ablate_updates.py` 只在内存中重新编译被比较的方法，不改源码文件。
对 `baseline / admission / acks / actual-sha / new-instance / grace` 各自单独运行：

```sh
PYTHONPATH="$PWD" FLOWTOOL_TEST_DATABASE_URL="$SCRATCH_TEST_DATABASE_URL" \
  /path/to/test-venv/bin/python .amp/in/ablate_updates.py VARIANT
```

共同输入为 `test_target_pause_keeps_binding_and_other_workers_running`、
`test_cutover_requires_idle_and_new_version_identity_before_resume` 和
`test_local_idle_requires_result_and_stop_ack_even_without_executor`。基线 **3 passed**；
每个简化变体均 **1 failed、2 passed**，失败位置对应下表，未发生其他用例故障。

| 简化变体 | 单独移除/替代 | 实测违反的要求 |
| --- | --- | --- |
| admission | `assign_job` 的两个目标暂停排除谓词 | pause 后绑定 revision 仍被派发 |
| acks | `idle_for_update` 仅检查 executor 不存在 | 未确认 terminal result 被误判为空闲 |
| actual-sha | resume 的实际运行 SHA 检查 | 错误 SHA 的新实例能解除暂停 |
| new-instance | resume 的原 instance/epoch 排除 | 旧 epoch 可完成切换 |
| grace | sweep 的目标维护宽限保护 | 超时后等待 review 的 binding 被错误释放 |

另测联合移除 SHA/新实例确认，同样捕获旧实例提前 resume。保留这些机制；没有增加全员版本
检查或全员等待。原始结果位于本地 `.amp/in/artifacts/ablation-*.log`，全量/构建/安装验证
分别为 `pytest-full.log`、`build.log`、`native-package.log`；以上均为未跟踪的本地证据。

### 2026-10-21 — 修复 CI 列检查的测试认证适配

[实现提交的 CI](https://github.com/b1indsight/agentflow/actions/runs/39482720558) 中，
`test_image_update.py` 的 21 个失败均发生在临时列检查角色登录时。独立 PostgreSQL 17
对照复现：TCP + SCRAM 为 21 failed / 4 passed，trust socket 为 25 passed。
原因是测试仅替换用户名，却继续使用原数据库用户的密码；临时角色没有配置密码。

测试适配现在为临时角色配置独立随机密码，并在子进程失败时显示 stderr；候选 DDL 仍以
非超级用户执行，不放宽 CI 认证，不修改实际部署探针或删列规则。修复后 TCP 密码认证下
全量 927 passed，trust socket 下相关 25 passed；角色权限检查及尝试自提权被拒绝的验证
通过，Ruff lint/format 通过。证据为本地 `.amp/in/artifacts/ci-auth-fix-*.log`。

### 2026-10-21 — Controller 改按可恢复业务交换边界切换

**需求与决定。** 用户进一步确认：Controller/前端更新可与 Worker 本地 Codex 执行并行，
只等待两端正在处理的业务交换结束；替换 Worker 进程本身仍等待本地执行和 ACK 收敛。
此决定取代上文「执行空闲、停启和恢复」及 2026-10-20 实施记录中 Server 要求 running
job/执行槽归零的部分，不改变 Worker 更新条件、独立发布队列或定向删列检查。

**实现与取舍。**

- 复用 `server_maintenance` 的 admission 锁和 `server_activities`，不新增 Worker 协议。
  Gateway 覆盖注册/恢复、准备派发至 Started、工具请求至响应、结果接收/应用至 ACK、
  停止请求至 ACK；独立接收器中的 Worker 维护请求也覆盖其数据库写入至响应。
  后者不能仅以“同步事务”视为空闲，回复发送仍可能在 await 中等待。
- 等待普通消息、心跳和 Worker 本地 executor 不持有 activity。暂停后不接纳新的业务处理，
  返回可重试断连；Worker 保留执行及原 token，工具/终态通过既有恢复路径继续或重放。
  Worker 维护操作仍受原有故障处理约束，没有承诺其任意断连后自动完成。
- 后台结果恢复逐批纳入 activity。更新器只以 `controls == 0` 判断业务切换空闲；原有
  job、dispatch、stop、result、effect、slot 数量保留为诊断。活动 Flow、持久化待办、Worker
  名单/版本/离线均不新增门槛。仍先停旧 Pod，再启新 Pod，并验证 Ready/imageID/实例。
- 暂停期间 sweep 不对可能已执行的任务应用恢复超时失败/阻塞，绑定继续保留；resume 为
  已存在的 job 恢复 deadline 重新给予正常宽限，不修改执行 token 或扩大 lease。
  真正丢失的 Worker 在恢复后宽限结束仍按原规则失败，未执行任务仍可正常重排。
- 修复本次切换场景暴露的 Worker 工具队列竞争：断连与 `queue.get()` 同时完成时，已取出
  但尚未处理的调用重新入队；已消费的调用立即移交所有权，避免 finally 重复入队。
- 当前运行镜像及候选镜像均要求明确的 `COMMUNICATION_IDLE` 能力标记。仅有旧版 pause
  能力的运行时没有上述 receipts，不能安全放宽条件；明确拒绝，不隐式降级，首次接入仍需
  人工维护部署支持版本。这是本次安全边界检查，不是 schema/wire 指纹或 Worker 同版本检查。

先使用本地 Claude Code 的 `claude-opus-5-5` 从编号源码材料生成方案；初始化 tools/MCP
均为空且最终结果成功。实际实现额外覆盖独立 WorkerMaintenance 响应，未采纳依赖未经验证
重试幂等性的“不跟踪该响应”建议；没有引入新的协议或部署执行权限。

**验证。** 原代码运行新增/改写场景为 **10 failed、1 passed**：复现等待 job/backlog、
暂停期间租约失败、丢失待发工具及缺少旧运行时能力检查。实现后本地 TCP + SCRAM 的
PostgreSQL 17 全量 **941 passed**，仅有既存 Starlette 弃用警告；Ruff lint、Mypy 通过。
新增协议测试使用真实 Gateway、WorkerClient、journal、数据库和双向模拟传输，外部 GitHub
工具/结果业务以替身隔离，覆盖：

- 原 executor 仅启动一次，暂停期间发出的工具或终态在新 Controller 上恢复，结果只应用一次；
- 派发/工具/结果/停止/Worker 维护 ACK 发送受阻时 activity 保持，发送完成后归零；
- 本地执行及维护期间心跳不占 activity，暂停注册不替换原 epoch，结果恢复不接纳新批次；
- 超过一天的暂停仍保留执行，resume 后能以原 token 恢复；不重连则在正常宽限后失败；
- 实际 exec adapter 与列探针配合模拟 K8s 更新，保留运行中的 job 切换并恢复，拒绝缺少
  业务交换能力的旧/候选镜像，原删列检查及非破坏性变化放行仍通过。

可复现命令：

```sh
PYTHONPATH="$PWD" FLOWTOOL_TEST_DATABASE_URL="$SCRATCH_TEST_DATABASE_URL" \
  /path/to/test-venv/bin/python -m pytest -q
```

本地证据：`.amp/in/artifacts/communication-before.log`、`communication-full-final.log`、
`communication-mypy.log`、`communication-ruff.log`。未执行真实 Codex、网络 WebSocket、
Kubernetes/GHCR、主机安装、launchd 或线上更新；上述协议验证不能替代真实部署验收。

**简化对照。** Scratch 脚本仅在独立 Python 进程中替换内存方法，不修改实际源文件。
共同输入为协议 ACK 边界测试的五个分支、长暂停恢复的两个分支及待发工具恢复测试：

```sh
PYTHONPATH="$PWD" FLOWTOOL_TEST_DATABASE_URL="$SCRATCH_TEST_DATABASE_URL" \
  /path/to/test-venv/bin/python .amp/in/communication_ablation.py VARIANT
```

| 变体 | 单独简化 | 实测 |
| --- | --- | --- |
| baseline | 保留实现 | 8 passed |
| receipts | 业务交换不记录 activity | 5 failed、3 passed，全部 ACK 边界失去保护 |
| sweep | 暂停期间继续应用恢复失败 | 2 failed、6 passed，执行在长暂停中失败 |
| grace | resume 不更新 job 恢复宽限 | 2 failed、6 passed，恢复后立即误判超时 |
| tool-ownership | 取消 waiter 后不归还已取出的工具 | 1 failed、7 passed，重连后的工具调用丢失 |

因此保留以上机制；未增加等待整个 job 或全员升级的路径。能力检查是两个直接判断，其
缺失/拒绝由旧运行时与旧候选镜像的验收测试直接验证，没有另建版本兼容框架。
证据为未跟踪的 `.amp/in/artifacts/communication-ablation-*.log`。
