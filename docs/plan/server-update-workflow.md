# 单节点 Server 自动构建与部署 — Feature record

**Stage:** Review — 本次仅交付 main 更新后的自动镜像构建与发布。
**PR:** [#93](https://github.com/b1indsight/agentflow/pull/93)

**Current scope:** 本 PR 包含 Images 工作流、镜像发布文档及本记录；不包含本地试用的部署
脚本及其测试，不执行自动部署。下方 Initial plan 保留最初完整设想，实际交付范围以本节和
后续变更记录为准。

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
