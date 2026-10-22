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

### 2026-10-21 — 返修：待应用结果的恢复宽限与旧任务工具清理

**触发与范围。** 用户要求修复上一轮独立审查报告的两个 medium 问题。仅调整
`MaintenanceStore.resume()` 和 Worker 工具调用生命周期；保留上文按可恢复业务交换边界
更新 Controller 的决定，Worker 自身更新仍等待执行与 ACK 收敛。未操作主机服务或部署。

**决定与实现（已完成）。**

- 已收到但尚未应用的结果仍可能是 running，并携带旧的恢复截止时间。维护恢复时不再
  排除这类 job，与其他带恢复截止时间的 running job 一同延长宽限；不放宽 token/epoch
  校验，也不允许任意过期执行无限重放。保留待应用结果的 journal，使后续归档仍有原始证据。
- Worker 在发出工具请求前跳过已取消或已完成的调用者；执行已结束时优先记录终态。
  终态成功写入 journal 后使用现有清理函数清空遗留工具，避免旧 job/token 进入下一任务。
  不增加第二套队列、调用状态或 ACK 规则；断连期间仍保存活跃任务的有效工具调用。

**验证（已完成）。** 新增公共协议回归场景：真实 PostgreSQL 中已持久但未应用的结果经历
超过恢复宽限的维护后，由原 journal 重放、应用一次并收到 ACK，执行器不重跑；工具调用在
断连期间取消后分别恢复原执行，或重放终态再接收下一任务，均不重发旧调用且可重新空闲。
修改前这三个场景全部失败；修改后相关测试 28 passed，全量 944 passed、零跳过，仅有既存
Starlette 弃用警告。Ruff lint、修改段格式检查及 Mypy（214 files）通过。

**简化对照。** 未跟踪脚本 `.amp/in/communication_repair_ablation.py` 只在独立进程的内存中
替换一个方法，不修改源文件。相同三个公共回归输入，在 `baseline` 下 3 passed；
`pending-grace` 恢复旧排除条件、`cancelled-tools` 删除发送前取消检查、`retirement` 删除
终态时工具清理，分别 1 failed、2 passed，故保留三处修复。可复现命令：

```sh
PYTHONPATH="$PWD" FLOWTOOL_TEST_DATABASE_URL="$SCRATCH_TEST_DATABASE_URL" \
  /path/to/test-venv/bin/python .amp/in/communication_repair_ablation.py VARIANT
```

日志：`.amp/in/artifacts/communication-repair-{before,focused,full,ruff,mypy}.log` 与
`communication-repair-ablation-*.log`。协议传输使用内存替身，未执行真实 Codex、网络
WebSocket、Kubernetes、GHCR 或 launchd 更新；本地测试不代表 GitHub CI 或真实部署通过。

### 2026-10-22 — 清理额外部署流程与冗余快照

用户要求减少新增内容。删除早期本机源码构建试用入口 `scripts/update_server.py`、其专用
测试和过时操作说明，只保留 Webhook 镜像更新；所需 Kubernetes 替身移入现有镜像更新测试。
Server 维护快照仅保留能力、暂停归属和 `control_work`，移除不参与切换的七类计数及
Worker/binding 列表；删列、身份、ACK、恢复与 Worker 更新逻辑不变。本次只做删除和直接
简化，未新增机制，不需要新增消融或永久测试。原试用流程的历史记录保留，以本条为准。
验证：相关 40 passed，全量 929 passed、零跳过（移除试用入口的 15 项测试）；Ruff、格式、
Mypy、安装包生成/CLI help 通过，非法 activity 快照均拒绝。证据：`.amp/in/artifacts/cleanup-*.log`。
外部 Kubernetes/GHCR 仍使用替身；未部署、安装或重启真实服务。

### 2026-10-22 — PR #104 修订：仅用 PostgreSQL 保存 Server 更新事件

**触发与决定。** 用户要求删除 SQLite、Worker 发布队列、版本表和维护状态表，并明确 Server
更新不应考虑 Worker 更新状态。本条取代 2026-10-20 实现记录中的 SQLite 双队列、独立 Worker
消费者、主机 notifier、`kubectl` Worker 发布器及 `worker_releases`/`worker_updates` 表方案；
上文保留为历史，不再按其安装这些组件。没有把 Worker 更新状态转存进 Server 事件。

**实现与边界（本地已完成）。**

- `scripts/image_event.py` 只消费现有 PostgreSQL 的 `server_update_events`；同一行的 state
  表示 queued/running/deployed/checked/failed/skipped，无独立版本表或更新维护表。重复投递
  不覆盖终态，失败显式重试，残留 running 事件要求人工检查，避免中断后重复切换镜像。
- 消费者与安装器使用同一 PostgreSQL 会话锁，部署期间不持有数据库事务。安装器只在停止
  消费者的短窗口持有事件表锁，随后保留会话锁；启动消费者前先释放。安装包仍可独立转交，
  只需既有 `psycopg` 环境，无需主机安装 `app` 包；消费者使用本次项目版本和相同 DB URL。
- Worker 保留本地更新能力，每 300 秒独立核验 GitHub Images 发布。其 `operation.json`
  持久化本机操作，现有认证连接中的 pause/commit/resume/ready 只暂停该连接的派发。
  `Register.update_pending` 在重连时先恢复暂停；resume 确认后先落盘 complete，再以 ready
  恢复派发，避免回复丢失时 journal 被新任务改变。公平调度同时排除暂停连接。
- Server 更新继续使用已有的短业务交换回执，不等待 Worker 更新完成。没有 Server 侧持久化
  Worker 维护豁免；离线 Worker 和等待 review 的绑定适用普通恢复宽限。没有对真实数据库
  执行 DROP、删除旧文件、部署、安装或重启服务，历史实例的数据清理不在此次本地修改内。

**验证。** 隔离 PostgreSQL 17 上全量 927 passed、零跳过，仅有既存 Starlette 弃用警告。
相关场景覆盖 Worker 停在 cutover 时 Server 仍可消费、事件去重/失败/过期/中断、安装器和消费者
互斥、真实 journal 交接、resume 回复丢失、重连暂停与其他 Worker 的公平派发。Ruff 通过，
Mypy 214 files 通过，修改段格式检查和安装包构建/隔离模式 CLI help 通过。
证据为未跟踪的 `.amp/in/artifacts/{pytest,focused-tests,ruff,mypy,installer-help}.log`。
网络、Kubernetes、GHCR 和系统服务使用替身，未执行真实发布或目标主机更新验收。

**简化对照。** 同一组三个回归输入，在独立进程中仅修改内存行为：baseline 为 3 passed；
将 resume 直接当成解除暂停、取消公平选择中的暂停排除、取消认证注册携带本地意图，分别
1 failed、2 passed。因此保留本地完成后的 ready 确认、调度排除和重连意图；未恢复任何
Worker 持久表或发布队列。证据为 `.amp/in/artifacts/ablation-*.log`，临时比较脚本已删除。

### 2026-10-22 — 删除 Server 更新工具的打包和自安装层

**触发与决定。** 用户指出三个部署脚本可直接复制，要求删除相关打包安装机制。本条取代此前
Server 单文件安装包、哈希版本目录和自动替换 launcher 的方案；此前的实现和验证记录仅作历史。

**实现。** 删除 `scripts/server_update/build_installer.py`、`install.py.in` 及专用安装器测试，
包括 tar/gzip/Base64 嵌入、解包校验、旧入口解析、版本目录、自动停启消费者和工具自升级。
清除消费者代码和测试中的安装器引用；保留消费者互斥与异常中断保护。三个实际部署脚本及
Server 业务交换暂停、删列检查和镜像切换不变。文档改为维护窗口中直接复制同版三个文件到
现有入口实际使用的目录，检查或更新 Server、恢复消费者由操作者分别决定，不增加替代安装器。

本次只做删除和引用清理，无新增机制或永久测试，不需要新增消融实验。真实部署主机未操作，
未停止服务、复制文件到主机或推送 PR。

**验证。** 隔离 PostgreSQL 17 上全量 926 passed、零跳过，仅有既存 Starlette 弃用警告；
移除的专用安装器测试不再计入。仅复制三个部署脚本到临时目录，在隔离 Python 模式中运行
CLI help，并让现有镜像更新测试使用复制后的文件，26 passed。Ruff、修改文件格式检查、
Mypy（214 files）及 diff 空白检查通过。证据为未跟踪的
`.amp/in/artifacts/remove-installer-{pytest,copied-tests,copied-help,ruff,format,mypy}.log`。
Kubernetes、GitHub、GHCR 和系统服务边界仍使用替身，未进行真实部署验收。

### 2026-10-22 — 用户修订：通用暂停接单取代 Worker 升级维护协议

**决定。** 删除 Worker 升级专用通信状态：`WorkerMaintenance`、`MaintenanceReply`、
`UpdateIdentity`、`Register.update_version`、`Register.update_pending`、`WorkerUpdateSession`、
`UpdateOperation`、Worker `operation.json`、启动授权目录，以及 cutover/complete/aborted/resume/ready
阶段协议。`ReleaseEvent` 仅保留发布元数据以核验下载，不通过 WSS 交换发布身份。

以通用 `WorkerAvailability(paused, request_id, connection_epoch)` 及 `AvailabilityAccepted` 替代。
Server 只在当前连接内存保存 `dispatch_paused`，不写数据库、不改变原有任务 busy；暂停只拦截该
Worker 的新任务，心跳、工具调用、结果 ACK 和停止请求照常处理，其他 Worker 正常调度。Worker
先准备并验证 immutable release，再暂停并等待 ACK，等本地执行、结果/停止 ACK 收齐；连接改变时在
新连接重新取得暂停。最终核验连接 epoch、本地空闲与 launcher 的运行归属后写入 `current.json` 并
正常退出。失败时确认解除暂停；解除确认失败则关闭连接，走普通重连，新连接默认可接单。

新进程只复用既有 `Register` 和断线恢复，不发送升级完成通知或 SHA 注册。launcher 保留
PID/token/SHA 运行归属与锁，以及 environment/current/complete 元数据；旧子进程正常退出且 current
指针变化后启动新 release。非零退出不自动循环，明确人工重启仍可尝试当前指针。原生安装器保留。
删除 operation 专用失败恢复说明，不保留旧协议兼容路径。

Server 更新事件表与三个脚本的直接复制方式不变，Server 部署不等待 Worker 升级。本文之前的
Worker 专用维护状态、重连携带更新身份、发布身份 WSS 交换和阶段化恢复方案均由本条取代；历史
feature record 保留其当时上下文。

**实现（本地完成）。** `protocol.py` 定义通用消息，`WorkerConnection` 保存连接内存标记，
`WorkerClient` 的原有单 reader 分发 ACK；删除 Server 侧的升级空闲查询和 Worker 维护会话。
准备任务期间收到暂停时，在授权派发前重新检查并退回未授权任务；已经开始发送的 Assignment
使用既有 FIFO send lock 排在暂停 ACK 前面，不再增加一把派发锁。停止请求先于新任务暂停检查，
因此暂停接单不阻断 stop 收尾。launcher 只依赖经过校验的 current 指针及子进程正常退出；
不持久化操作阶段，也不在新进程启动时恢复升级握手。发布顺序检查复用 current 中的元数据。

**验证。** 隔离 PostgreSQL 17 上全量 **921 passed、零跳过**，仅既有 Starlette 弃用警告；相关
测试 **35 passed**。覆盖准备期间暂停、已授权派发与 ACK 顺序、其他 Worker 公平调度、暂停期间
的工具/停止交换、结果/停止 ACK 等待、丢失暂停 ACK 的解除或断连、切换前重连重新暂停、仅检查，
以及正常注册和保留 journal。真实无害子进程测试确认 root lock、不可变环境的延迟导入、正常交接
和崩溃不循环；没有操作真实 launchd、Codex、Kubernetes/GHCR 或部署主机。传输使用内存替身，
不把本地验证当作真实网络或部署验收。

Ruff、改动文件/改动段格式检查、`mypy app scripts/worker_update`（123 files）与 diff 空白检查通过。
扩大到 `mypy app scripts` 时，未修改的 `scripts/smoke/recovery.py:120–121` 有两个既有字典类型
错误；用父提交该脚本的副本复现相同错误，未扩大范围修复。证据：本地未跟踪的
`.amp/in/artifacts/generic-pause-{full,focused,ruff,format,scoped-mypy,mypy,baseline-mypy}.log`。

**简化对照。** 在独立进程中仅替换一个方法的内存实现，以相同六个公共场景比较，不修改源码：
baseline 为 6 passed；删除准备后的暂停复查、删除调度中的暂停排除、删除最终 epoch 复查、
不等暂停 ACK，分别为 1 failed / 5 passed、2 failed / 4 passed、1 failed / 5 passed、
1 failed / 5 passed。失败分别对应暂停后仍派发、暂停 Worker 抢占调度、未在新连接暂停即切换，
以及确认丢失时仍切换。保留这四个直接检查，不恢复任何升级状态机或持久表。临时比较脚本和
父提交检查副本已删除，日志保留于 `.amp/in/artifacts/generic-pause-ablation-*.log`。

### 2026-10-22 — 发送暂停消息时断线，继续使用普通重连

**触发与实现。** 自查发送失败路径时，复现 WebSocket 已关闭、reader 尚未清理旧 epoch 的
窗口：`ConnectionClosedError` 未进入协调器的连接重试分支。通用 availability channel 现在将
`ConnectionClosed` 转成 `ConnectionError`，仅清除仍属于该请求的旧连接；先取消本请求的 future，
避免断连通知产生未消费异常。协调器继续等待普通重连，并在新 epoch 再次确认暂停，不增加升级状态。

**验证与对照。** 给已有重连测试增加 closed-send 输入，修改前 1 failed，修复后同组三个重连输入
3 passed。最终隔离 PostgreSQL 17 上全量 **922 passed、零跳过**，相关 **36 passed**；Ruff、
修改文件格式、`mypy app scripts/worker_update`（123 files）和 diff 空白检查通过。测试仍使用内存
传输与实际 WebSocket 异常类型，不宣称执行了真实网络或部署验收。证据：本地未跟踪的
`.amp/in/artifacts/generic-pause-closed-send-{before,after}.log` 和
`.amp/in/artifacts/generic-pause-final-{full,focused,ruff,format,mypy}.log`。

### 2026-10-22 — 用户修订：删除 Server 维护控制层

**决定。** 删除 `app/persistence/maintenance.py`、`MaintenanceStore`、`server_maintenance` 和
`server_activities`。Server 更新不再设置全局 pause/resume、不等待活动或通信回执、不检查
`COMMUNICATION_IDLE`，也不在维护期间延长 recovery deadline。业务路径在新 Server 就绪后直接按
既有规则恢复；断线 Worker 和绑定继续使用普通 recovery grace。

Server 更新改为普通预检，随后将副本缩至 0、等待旧 Pod 正常退出、以固定镜像 digest 恢复 1 个副本，
并核验 `/readyz` 与实际镜像身份。不等待 Worker 更新、任务完成或通信活动清空。保留 PostgreSQL
`server_update_events`、消费者互斥、候选 Pod 预检和删列检查。`agentflow-maintenance.py` 删除，
部署主机直接复制 `update-agentflow-server.py` 与 `column-check.py` 两个文件；不提供替代安装器或
维护服务。主机文件锁和 `operation.json` 保留，后者只保存 incomplete/complete 部署报告，
不是 Server 维护表。

Worker 的 `WorkerAvailability` 单 Worker 暂停新任务、本地升级 ACK 收尾与普通断线重连保持不变。
历史已安装的 Server 维护表不自动 `DROP`，且本修订不兼容旧 PR 的部署。

**取代的旧决定。** 本条取代 2026-10-21 的全局业务暂停、`server_activities` 通信等待、
`COMMUNICATION_IDLE` 能力校验和维护恢复宽限，也取代 2026-10-22 的三个 Server 脚本复制说明。
此前记录保留为历史上下文。

**实现与验证。** 删除业务路径中的 admission/activity/exchange 包装和专用测试；只读 schema
快照并入现有 `column-check.py --snapshot`，使用只读一致事务，不初始化业务库。未增加替代维护层、
依赖或兼容分支；本次为删除和职责合并，没有新增机制需要消融实验。

隔离 PostgreSQL 17 上全量 **903 passed、零跳过**，仅既有 Starlette 弃用警告；相关场景
**54 passed**。覆盖普通断线后执行继续、离线完成后重连记录结果、超过重连宽限后的 Server
持久结果恢复、Worker 通用暂停及公平派发、候选删列拦截、存量待办不阻止替换、停旧后启新和
中断记录拒绝盲目重试。两个 Server 脚本单独复制到临时目录后，隔离模式 CLI help 和镜像更新
测试 **21 passed**。Ruff、两个脚本/重写测试等七个文件的格式检查、Webhook 修改段格式检查、
`mypy app scripts/worker_update scripts/server_update`（124 files）及 diff 空白检查通过；
未格式化其他文件的既有无关代码。此前已记录的 `scripts/smoke/recovery.py` 类型错误不在本次范围。

证据：本地未跟踪的 `.amp/in/artifacts/remove-server-maintenance-{full,focused-final,copied-tests,
copied-help,ruff,format-final,mypy}.log`。PostgreSQL、Worker journal 与脚本实际执行；网络传输和
Kubernetes/GHCR 边界仍使用替身，未执行真实数据库迁移、主机安装、部署或服务重启。

### 2026-10-22 — 用户修订：Worker 仅保留本体 CLI 手动更新

**背景、选项与决定。** 用户要求移除 deploy 侧 Worker 更新，并选择只保留
`flowtool worker update` 手动入口。删除原生安装器、launcher、LaunchAgent 更新模板和独立
`agentflow-worker-update` 命令；不将自动轮询和进程切换搬进 CLI。保留固定 SHA 发布核验及独立
non-editable 环境准备。此条取代前述 Worker 按执行空闲更新、通用暂停接单与后台发布检查的决定；
历史记录保留，Server 更新脚本及镜像事件消费流程不变。

**实现与取舍。** `app/worker/cli.py` 挂载到现有 `flowtool worker`，无子命令仍启动普通 Worker。
`update --sha SHA` 仅检查发布，不加载 Worker 凭据；`--apply --root RELEASE_ROOT` 必须同时提供，
要求操作者先正常停止原 Worker。CLI 复用原 Worker root 进程锁和身份校验，在锁内拒绝损坏或
尚未确认的 journal，再准备新环境并输出解释器路径。操作者保持相同身份、root、HOME 和认证，
显式启动该解释器；命令不操作系统服务、不写启动指针、不发信号，也不自动重启。

删除 `app/worker/updates/coordinator.py`、旧 CLI、`scripts/worker_update/`、部署模板及专用测试；
删除更新配置、runtime 协调和 availability transport/protocol，以及 Controller 调度中的暂停排除
与复查。保留普通派发、历史查询、结果/停止确认和断线恢复。代价是准备期间原 Worker 离线，
服务执行路径与重启由操作者管理；不提供旧 PR 自动更新服务的兼容层或自动迁移。
本次为删除机制和入口归并，身份、锁、journal 和发布准备复用既有实现，没有新增实质机制需要
消融实验；新增 CLI 测试覆盖这些可观察约束。

**验证。** 以 `FLOWTOOL_TEST_DATABASE_URL=postgresql://127.0.0.1/postgres` 运行完整 pytest，
本地 PostgreSQL 17 上 **904 passed、零跳过**，仅既有 Starlette 弃用警告。覆盖普通启动、无凭据
检查、非法参数、锁内准备、journal 字节保留，以及运行中、待确认、损坏和身份不符时拒绝更新。
Ruff 全量检查、修改文件格式检查、`mypy app scripts/server_update`（120 files）及
`uv lock --check --offline` 通过；未修改依赖或 lockfile。

实际执行离线缓存的 `uv sync --locked --no-dev --no-editable`，使用安装后的隔离解释器确认
Worker CLI help 和 build stamp，同时检查没有启动指针、launcher 或请求文件，journal 不变且锁
已释放。该安装实验替换了发布元数据和远程 Git 导出，未执行真实 GitHub 发布下载或服务切换。
首次全量检查使用 Unix socket 时有 3 个既有 recovery-bundle 测试因缺少 IP peer 失败；改用 TCP
连接后完整通过，未修改这些测试或恢复代码。

证据：本地未跟踪的 `.amp/in/artifacts/worker-manual-cli-full-tcp.log`、
`worker-manual-cli-installed.log`；首次 Unix socket 结果为 `worker-manual-cli-full.log`。
本次修改保存在基于 #104 head 的本地隔离 workspace，未提交、推送、部署或重启真实服务。
