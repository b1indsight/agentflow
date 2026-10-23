# Server / Worker 更新计划

**Stage:** Implementation — 实现与本地自动化验证完成；真实接入待验收，交付及审查状态见 PR。
**PR:** [#104](https://github.com/b1indsight/agentflow/pull/104)
**Bookmark:** `feat/execution-idle-updates`

第 1–8 节保留实施前的方案基线，其中的「当前」「待批准」「尚未实现」描述当时状态；
最新实现、取舍和验证见第 9 节的 2026-10-23 实施记录。当前操作说明以
[Server 更新](../deployment/server-update.md)与 [Worker 更新](../deployment/worker-update.md)为准。

## 1. 已确认的需求

1. **Server 更新器监听独立端口。** 合并 main 后自动制作镜像；仓库 Webhook 在镜像发布后
   推送给更新器，更新器收到请求后加入部署队列，由消费者执行部署。
2. **减少机制和无关改动。** 删除本地源码构建部署试用流程；不混入 Console、Gateway
   等无关重构或纯格式修改。保留与 Server 重启及正常恢复直接相关的修复。
3. **Server 与 Worker 更新解耦。** Worker 是否升级、名单或版本变化、是否全员重连，
   均不作为 Server 部署条件，不维护固定 Worker 名单。
4. **删除 Server 维护控制。** 不设置全局 pause/resume，不等待任务、activity、通信回执
   或队列清空，不检查 `COMMUNICATION_IDLE`，不延长维护专用 recovery deadline。
   流程为普通预检 → 副本 0 → 旧 Pod 正常退出 → 固定镜像、副本 1 → 就绪与镜像身份核验。
5. **保留定向删列检查。** 不做数据库备份/还原，不以源码、schema 或 wire 指纹差异阻断更新。
6. **Server 更新事件保存在更新器主机。** PostgreSQL 位于 Kubernetes 集群内，事件队列改用
   本地持久文件，不要求更新器直连 PostgreSQL。保留去重、消费者互斥和同镜像续作能力；
   不引入 SQLite、Worker 发布队列或独立版本/维护表，不写 Worker 更新状态。
7. **直接复制 Server 工具。** 最终部署工具为更新器和列检查两个文件；不做安装器、解包、
   哈希版本目录、launcher 自动替换或工具自升级。
8. **Worker 提供更新命令行参数。** 正式安装的包自动查询是否为最新版本，有更新时更新自身本体。
   源码运行不属于更新命令的使用场景，不增加源码/editable 检测或跳过分支。
   不要求用户手填 SHA，不恢复独立 release 环境、新虚拟环境、安装器或后台更新服务。
9. **Server 整个部署流程幂等。** 同一部署目标、同一不可变镜像可直接重试，按集群实际状态
   继续未完成步骤，最终收敛到单副本、指定镜像且 Ready；已完成则成功返回，不重复停启。
   普通中断不再因 running/incomplete 要求人工删记录或解锁。

最新要求取代上一版的 Server 发布轮询建议，以及 Worker 命令只读、实际安装全由人工完成的要求。
具体参数拼写、更新后是否启动 Worker、实际安装方式及主机托管属于下面的**方案建议或待确认项**。

## 2. 当前代码与缺口

| 环节 | 当前事实 |
| --- | --- |
| 镜像发布 | [Images 工作流](../../.github/workflows/images.yml) 已在 main push 后构建 serve/worker 候选镜像，按实际 digest 做 smoke，再晋升同一 manifest 的 SHA 标签；没有入队或部署调用 |
| Server 入口 | [image_event.py](../../scripts/server_update/image_event.py) 已有 `verify/enqueue/run/status/retry`，但没有 HTTP 监听器；`enqueue` 按 `(run_id, attempt)` 去重，`run` 只消费已有队列 |
| 持久化 | `app/persistence/server_updates.py` 当时使用 PostgreSQL 事件表、独立连接与 advisory lock；将替换为主机本地文件队列，并删除该模块及 [schema.py](../../app/persistence/schema.py) 中的事件表注册，不改业务表 |
| 停启工具 | [更新器](../../scripts/server_update/update-agentflow-server.py) 已有预检、候选 Pod、停旧启新和操作记录，但当前拒绝重试 incomplete，且要求健康旧 Pod，须改为幂等续作；[列检查](../../scripts/server_update/column-check.py) 已有只读快照及隔离试跑 |
| 业务 Webhook | [api.py](../../app/controler/api.py) 的入口属于被更新的 Server；本方案不通过它接收部署事件，也不改业务 Router/inbox |
| Worker | [CLI](../../app/worker/cli.py) 当前只有 `update --sha SHA` 发布校验；[releases.py](../../app/releases.py) 已有 `latest_release()`，但没有本地版本比较和本体更新 |
| 版本标识 | [pyproject.toml](../../pyproject.toml) 与 [app/__init__.py](../../app/__init__.py) 的版本号均为 `0.0.1`，不能据此判断每个提交的新旧 |
| 安装与主机 | [当前部署文档](../deployment/README.md) 使用独立检出、`uv sync` 与 `uv run`；实际目标 Worker 安装方式、更新器端口和主机配置尚未核实 |

缺口是独立 Webhook 监听服务、本地文件队列、Server 幂等部署，以及 Worker 的真实自更新；
不能把已有消费逻辑或发布校验当作已完成这些功能。

## 3. Server：Webhook 接收、入队、部署

### 目标流程

1. **发布。** [images.yml](../../.github/workflows/images.yml) 保持构建、smoke 与 SHA 标签晋升。
   仓库级 Webhook 订阅 `workflow_run`，镜像工作流结束后由 GitHub 原生投递，不额外加 curl。
2. **监听。** 建议在 [image_event.py](../../scripts/server_update/image_event.py) 增加 `listen` 子命令，
   使用已有 FastAPI/uvicorn，在可配置 host/port 提供独立的 `POST /webhook`。
   该服务和后台消费者在部署主机运行，不导入或启动 ServerRuntime，业务 Server 停止后仍可接收。
3. **验签与筛选。** 限量、限时读取原始请求体，使用独立的非空 secret 核对 HMAC-SHA256。
   只接受固定仓库、Images 工作流、main/push、completed/success 的运行；严格解析正整数
   run ID/attempt 和完整小写 SHA。合法 ping 正常应答，无关事件忽略，不以 Bot 身份过滤发布。
4. **持久入队。** 每个 `(run_id, attempt)` 保存为一个本地 JSON 事件文件，包含 SHA、状态、
   核验后的 run number、镜像及原因。持短期队列文件锁完成去重和原子落盘，落盘成功后返回 202。
   同身份同 SHA 只保留一个文件；失败请求可重投续作，成功重复请求不重复部署。
   SHA 冲突返回 409，持久化失败返回 503。
   请求不等待 GitHub/GHCR 核验或部署，202 只表示事件已持久接收。
5. **后台消费。** 单消费者持进程级文件锁，先续作该目标未完成的固定镜像操作，再按数字
   run ID/attempt 从新到旧领取 queued，持久记录 running 后释放短期队列锁。
   新操作在队列锁外通过 `resolve()` 核验仓库/工作流、run/attempt/SHA、当前 main、发布步骤
   与 candidate/SHA manifest，取得唯一 Linux/amd64 digest 并固定授权信息；保留新事件防回退。
   续作使用已持久化的目标和镜像，不因 main 前移重新选版本或丢弃进行中的操作。
   请求体不能指定任意镜像、URL 或部署命令。
6. **检查并部署。** 首次操作核验目标、节点、PVC、HPA、探针和退出宽限，在候选 Pod/临时
   PostgreSQL 中做删列试跑。停旧前持久保存目标、预检结果及后续恢复所需信息；
   按实际状态执行必要的副本 0、等待旧 Pod 正常删除、固定 digest 副本 1，并核验 Ready/imageID。
   已完成的动作不重做，已是目标镜像且健康时不再次停旧。详细续作规则见下一节。
7. **恢复。** Server 和 Worker 使用普通恢复逻辑；后台消费者继续消费。HTTP 与部署并行，
   慢部署不阻塞后续请求；启动时继续处理既有 queued，不轮询 GitHub 发现发布。

### 同镜像重试与状态收敛

- 幂等键为部署目标（context/namespace/workload/container/node）与固定镜像 digest。
  复用 `operation.json` 按阶段保存 workload UID、原 Pod 身份、已核验发布信息、列元数据快照/
  预检结果、候选运行时 imageID 及受管字段目标值，不保存凭据。检查 Pod 创建前固定授权与资源身份，
  缩为 0 前持久化完整预检结果及停旧意图；先落盘，再执行相应 Kubernetes 写入。
  拆分首次预检和恢复校验，恢复不要求旧 Pod 仍存在；阶段记录仅作提示，每次以实际资源为准。

| 读取到的实际状态 | 相同镜像重试的动作 |
| --- | --- |
| 尚未通过预检，旧 Server 仍在 | 补齐发布与列检查，未通过不停止旧 Server |
| 副本 0，旧 Pod 尚在退出 | 继续正常等待删除，不强删、不提前启动新 Pod |
| 副本 0，旧 Pod 已消失 | 使用已落盘的预检结果与固定镜像，补齐副本 1，不依赖旧 Pod 重新取快照 |
| 目标镜像已启动但未 Ready | 继续等待和核验，不再缩为 0 或重复重建 |
| 单副本目标镜像已 Ready，运行时身份一致 | 成功返回；必要时补记 operation complete 和事件 deployed，不重复停启 |

- Kubernetes 请求超时或响应丢失后重新读取资源，用 UID/resourceVersion 条件提交尚未完成的
  局部 patch，不把请求失败当作操作未执行，不覆盖无关配置。节点、PVC、HPA 等安全约束仍校验。
- 候选检查 Pod 也须可重入：创建前记录稳定身份，重试读取并核对归属和检查输入，复用已完成结果；
  重建只处理本操作拥有的资源，清理时资源已不存在视为成功，不在每次重试遗留随机命名 Pod。
- 同一命令、显式 retry 或同一失败事件的 Webhook 重投均可触发续作，无需删记录。
  重启后先恢复已有 running 操作，成功后再取新事件；不增加无界忙重试或另一个重试调度器。
  running 事件若尚未固定授权/操作意图，仍须先执行新事件核验，不能跳过发布检查。
  完成判定必须核对实际 Ready/imageID，补齐本操作检查资源清理，先记操作 complete 再记事件 deployed；
  已就绪但终态落盘失败时，重试只补记结果，不能仅凭 complete 标记判成功。
- 停旧意图尚未持久化时，明确失败的预检结束该次尝试，不长期阻塞后续新镜像；
  停旧意图一经持久化，同镜像继续收敛，未完成前不切换另一镜像。
- 不同镜像不能覆盖进行中的操作；workload UID 改变、冲突配置或本地权威记录损坏/丢失仍须明确失败。
  幂等保证以有效持久记录、目标归属不变和临时故障最终解除为前提，不承诺坏镜像自动变健康，
  不回滚、不强删、不把旧事件作为新部署回退版本。

### 本地保存、接口与故障合同

- 监听 secret 缺失时拒绝启动。签名针对原始字节并用恒定时间比较；校验头、JSON 和字段类型，
  超限/超时/非法输入拒绝，不记录 secret 或完整请求体。部署目标和状态目录来自主机固定配置。
- 队列使用每个部署目标独占的本地持久目录，例如
  `~/.local/state/agentflow-deploy/<target-id>/events/<run_id>-<attempt>.json`；
  与该目标既有 `operation.json` 分开，不保存完整 Webhook 或凭据。目录权限 0700、事件 0600。
  同目录临时文件写完后 flush/fsync，原子替换正式文件，再 fsync 目录；重复请求也须确认
  事件和目录已持久化，不能因前一次替换可见但同步失败就返回 202。遗留临时文件不视为事件。
- 短期队列锁覆盖读取、校验、去重和状态写入；消费者锁覆盖消费进程生命周期，均使用主机文件锁。
  网络核验和部署不持有队列锁，慢部署不阻塞接收。状态落盘失败停止本次尝试，保留意图供重试；
  权威事件损坏仍拒绝消费，不静默忽略或覆盖；同身份重投也不能重建损坏记录。
  校验文件名与内容中的 run ID/attempt 一致，终态写入前确认事件仍为 running。
- `workflow_run` 的 completed 同时包含成功和失败，必须筛选 conclusion。
  仓库 Webhook 支持此事件，无需把 GitHub App 配置作为前提；见
  [GitHub 官方事件说明](https://docs.github.com/en/webhooks/webhook-events-and-payloads#workflow_run)。
  实施前用官方 schema 或脱敏样本固定工作流身份、run/attempt/SHA 的实际字段路径。
- 限制请求读取和队列锁等待时间；锁超时、磁盘满、写入或 fsync 失败均不能返回 202。
  持久化失败不伪报接收成功，不新增数据库或 delivery 台账。
- 用文件锁替换 PostgreSQL advisory lock，保留部署工具主机锁；running/incomplete 是续作状态，
  不再是永久拒绝条件。queued/running 重复投递复用事件，failed 可重投或 retry；
  deployed 重复不重新停启，checked/skipped 不自动转为部署，新事件仍执行防回退核验。
- 终态事件文件保留用于去重及已部署版本比较，首版不自动清理，不新增独立版本指针。
  只支持单主机本地持久磁盘，不使用 NFS 或多主机共享队列；磁盘丢失会丢失队列和去重历史，
  必须人工核对实际部署与操作记录后重建，不能承诺跨主机或磁盘灾难恢复。
- 建议首版采用人工重投处理未接收事件，而非增加另一条发布发现链路。GitHub **不会自动重投**
  失败投递；已入队事件可从本地文件继续，未收到的不能靠队列恢复。自动补偿如需加入须明确范围，
  不把它写成用户禁止事项；见 [官方失败投递说明](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)。
- 事件接收与消费不读取 `FLOWTOOL_DATABASE_URL`、不直连 PostgreSQL。业务数据库仍在集群内；
  首次删列预检沿用 `kubectl exec` 在旧 Server Pod 中取得列快照，再由候选 Pod/临时 PostgreSQL
  试跑并保存结果；已停旧的同镜像续作使用该结果，不要求把数据库暴露给更新器主机。
- 监听模式后台执行 apply；预检使用 `verify` 加直接调用更新器且不带 `--apply`。
  不用真实事件做非 apply 消费演练，避免终结为 checked 后无法部署。保留无自动回滚的边界：
  停旧后失败可能无可用 Server；删列检查也不证明全部数据或协议变更兼容。

## 4. Worker：更新参数检查最新版并更新本体

### 目标流程（参数及退出语义为建议）

```sh
flowtool worker --update
```

1. **识别已安装版本。** 从正式安装包的可靠来源元数据取得 revision；不比较固定的 `0.0.1`，
   不以独立缓存标记冒充实际版本。来源不明明确失败，不报告 already-current。
2. **查最新版。** 调用 [releases.latest_release()](../../app/releases.py)，自动取得经过
   Images/main/push 发布步骤和当前 main 核验的固定 SHA，无需用户指定。
3. **执行更新。** 已是目标版本且安装健康时输出 already-current；落后时自动更新本机 Worker
   使用的包和必要依赖，安装位置、解释器与启动入口不变，不触碰集群 Server。
   需强制覆盖或降级等不安全情形明确拒绝，不覆盖用户修改或假报最新。
4. **核验。** 用新的解释器进程验证实际导入路径、CLI 和目标 revision，确认依赖同步成功，
   全部通过才输出 updated。目标 SHA 相同也须检查安装健康，上次同步失败不能被当成已完成。
5. **退出。** 建议作为一次性更新命令，完成后退出；普通 `flowtool worker` 仍直接运行。
   移除旧 `worker update --sha` 只读入口，避免保留两套更新语义。是否更新后继续启动待确认。

### 安装与运行安全

- 更新**现有安装**，不恢复独立环境、release 目录、launcher 指针、自启动模板或后台更新器。
  不自动更新 Git/jj/Codex 等外部工具，不读取 Server 数据库或连接 Controller 执行更新。
- 该命令仅面向正式安装包。源码运行不调用更新命令，不为此增加工作目录、源码路径或
  editable 检测及跳过分支；不实现源码拉取、检出切换或 `uv sync` 的自更新路径。
- 实施前核实正式包的固定来源、可靠 commit 元数据、当前解释器和原地包更新命令；
  不引入通用安装器适配框架。未核实的容器/只读或其他管理方式明确诊断，不宣称已支持。
- 正式包更新前，建议由操作者先正常停止共享该安装的 Worker，暂停服务自动拉起。
  增加安装级互斥并复用必要的 root 占用检查；[WorkerRootLock](../../app/worker/runtime.py)
  只覆盖单个 root，不能据此声称其他共享安装进程也已停止。不新增全局暂停或等待活动协议。
- 保持原 HOME、Worker ID、root、journal 和认证；不删 journal、不换 root，不强杀进程。
  fetch、安装/同步或核验失败均非零退出，报告阶段与已知 revision；原地更新可能部分改变依赖，
  不承诺原子更新或自动回滚，恢复步骤按实际安装方式给出。

## 5. 实施范围与顺序（批准后执行）

1. **固定接入合同。** 核实 Webhook 样本、更新器端口/代理及 Worker 正式包安装方式，确定
   原地包更新命令、revision 来源、单次更新退出语义和故障恢复；不预设主机系统。
2. **Server 接线与保存替换。** 修改 [image_event.py](../../scripts/server_update/image_event.py)，
   将 enqueue/run/status/retry 的 SQL 存储改为本地事件文件和文件锁，增加独立监听、验签筛选
   和并行 apply 消费；对齐同镜像续作、失败重投与部署结果补记，不抽象通用存储框架。
   删除 `app/persistence/server_updates.py` 及
   [schema.py](../../app/persistence/schema.py) 中仅更新事件使用的导入与注册，不保留 PostgreSQL
   回退路径，不自动连接真实数据库迁移或删表。不改业务 Webhook/inbox，也不增加版本表。
   修改 [部署脚本](../../scripts/server_update/update-agentflow-server.py) 的首次/恢复预检、操作记录、
   候选检查资源及停启步骤，以实际状态收敛取代 incomplete 一律拒绝，保留现有文件锁和条件 patch。
3. **Worker 自更新。** 修改 [app/worker/cli.py](../../app/worker/cli.py)，复用
   [app/releases.py](../../app/releases.py) 的最新发布核验，实现正式包自更新，必要逻辑放在单一
   `app/worker/self_update.py` 模块。
   不恢复旧 `updates/` 环境安装包或源码更新分支；清除仅旧入口使用的代码。
4. **验证公共行为。** 扩展 [test_image_event.py](../../tests/test_image_event.py)、
   [test_worker_cli.py](../../tests/test_worker_cli.py)，覆盖下一节合同；新增模块不要求逐函数测试。
   复跑 [镜像停启](../../tests/test_image_update.py)、
   [Server 恢复边界](../../tests/test_controller_update_boundary.py) 和
   [发布核验](../../tests/test_update_publication.py) 回归。
5. **更新当前指南。** 同步 [Server](../deployment/server-update.md)、
   [Worker](../deployment/worker-update.md)、[部署入口](../deployment/README.md)、
   [运维](../operations/runbook.md) 及必要 README/变更说明，不把计划提前写成已上线事实。
6. **运行检查并记录。** 相关及全量 pytest、Ruff、修改文件格式检查、
   `mypy app scripts/server_update`、`uv lock --check --offline`；更新同一个计划、bookmark 和 PR。

默认不改 Images 发布合同、业务数据库表及列检查主体，不新增协议或 Console/Gateway 重构。
功能实现仍待批准；目录整理仅移动 Server 事件入口并同步测试与文档路径，未接入上述新功能。

## 6. 首次接入（由操作者另行执行）

1. 核对更新器主机、既有 Python/HTTP 依赖、源码和独立托管方式；
   [wheel 只打包 app](../../pyproject.toml)，须提供 `scripts/server_update/image_event.py`。
   为每个固定目标配置并初始化独占的本地持久状态目录，核对磁盘、文件锁、原子替换与 fsync 支持。
   后续启动不静默重建缺失目录；无需配置宿主机数据库连接串或初始化更新事件表。
   若旧数据库队列已投入使用，先停旧消费者，由操作者通过集群内路径核对未完成事件和部署历史，
   明确处置后再切换；不自动丢弃旧事件或导入本地目录。
2. 消费者停下、部署目标锁已释放时，复制同版更新器与列检查，保留事件文件及 operation.json，核对
   `~/.local/bin/agentflow-server-update` 的脚本路径和固定 context/namespace/workload/container/node。
   首次先 verify 并直接预检；已有有效操作直接按固定镜像续作，不清除 running/incomplete。
   不自动替换入口或工具自升级。
3. 给更新器配置独立 secret 和监听地址/端口。建议私有监听配合 TLS 反向代理/隧道；公网精确
   路由到更新器的部署 Webhook，不能指向业务 Server Pod，也不转发其他管理路径。
   主机须能访问 Kubernetes API 并有现有预检、exec 和部署权限，不要求直接访问 PostgreSQL。
4. 启动独立监听服务和后台消费者，由操作者配置仓库级 Workflow runs Webhook 的 URL、JSON
   和同一 secret；业务 GitHub App Webhook 不变。验证 ping、签名、入队及实际发布部署。
   进程托管配置合理重启退避；重启后继续固定操作，不切换另一镜像或增加无界忙重试。
5. Worker 首次安装仍按既有方式。正式包更新时正常停旧，运行 `--update`，检查实际 revision
   和安装健康后按原方式启动；源码运行继续使用既有启动方式，不调用该更新命令。
   所有真实配置和进程操作本轮均不执行。

## 7. 验收与简化验证

- **Server 端到端：** 从实际 HMAC 签名 HTTP 请求出发，在临时本地目录验证持久化后 202、
  queued/running、固定 digest + apply 调用及 deployed；独立进程重启仍能读取状态。
  未设置数据库 URL、禁止宿主机数据库连接时仍可接收入队和消费；阻塞部署替身时仍能接收下一请求。
- **安全与去重：** 篡改/无签名、超限/超时、非法头/字段、错误仓库或工作流、非成功 main/push
  不部署；ping/无关事件不入队。重复 delivery/不同 delivery 同 run-attempt 只有一个事件文件，
  SHA 冲突不覆盖，持久化失败不回 202，不访问业务 Server 或在 HTTP 中请求 GitHub/GHCR。
- **队列恢复：** 重启先续有效 running/operation，再从新到旧处理 queued；第二消费者拒绝启动，
  部署不持队列锁。failed 重投或 retry 可续作，成功重复不再停启，旧新事件防回退，监听不产生 checked。
  覆盖 main 在中断后前移仍完成原固定镜像；跨进程验证文件锁，不只在线程内验证互斥。
- **部署幂等：** 通过公开 CLI，在候选检查 Pod 创建/清理、缩为 0、旧 Pod 删除、恢复副本 1、
  Ready、操作/事件终态落盘前后注入进程中断、请求已执行但响应丢失和磁盘失败；故障解除后反复
  执行同一目标/镜像命令，最终均为单副本、指定镜像及匹配 imageID、Ready、complete/deployed。
  目标镜像已启动时不再次缩为 0，已完成时无重复停启，无遗留重复检查 Pod；验证异镜像、
  UID 变化及记录损坏不会被覆盖。更新原有 incomplete 重试必须失败的旧测试，不保留错误合同。
- **落盘故障：** 覆盖写入/替换/文件与目录 fsync 失败、磁盘满、锁等待超时及崩溃窗口；
  失败不回 202，未持久记录 running 不执行部署。损坏事件阻断消费且不能被重投覆盖；
  遗留临时文件不入队，终态落盘失败不报成功；重试核对实际状态后补记。已接受事件在普通重启后可恢复。
- **正式包更新：** 通过公开参数验证自动查询、已是最新不重复安装、落后实际更新到固定 SHA；
  新进程加载实际目标版本，不只断言安装命令被调用。相同 SHA 但依赖同步失败不得假报健康。
- **Worker 保护：** 临时正式安装中覆盖未知来源、错误环境、占用/并发及各失败阶段，
  均不得假报成功；不新建环境/版本目录、不改 journal/身份，普通启动不回归。隔离临时安装用于
  实现阶段验收，不修改开发机当前环境，也不作为产品的新环境安装机制。
- **回归与消融：** 保持删列、镜像身份、先停后启和普通恢复。实现后、代码审查前临时去掉签名、
  去重或 Worker 最终版本核验，对应公共场景应失败；撤销所有临时改动，不保留额外消融框架。

远程替身不证明真实 GitHub 投递、镜像拉取、Pod 停启或目标主机自更新；这些需单独真实验收。
上述功能尚未实施或验收；目录整理的入口和路径检查不能证明监听、文件队列、幂等部署或自更新已完成。

## 8. 待确认项

- 更新器监听端口、公网 URL/TLS 代理、secret 存放与服务托管；集群目标及 kubectl 权限，
  本地持久状态目录、文件系统支持与磁盘容量。无需为事件队列提供集群 PostgreSQL 访问路径。
- Worker 正式包的安装方式、解释器路径、revision 来源、安装所有权及是否由多个进程共享。
- `--update` 是否采用建议的一次性更新后退出，或更新后继续启动；具体参数拼写待方案批准。
- 首版人工重投的漏投处理是否足够；如需要无人值守补偿，先确定范围，不默加回发布轮询。

## 9. 修订记录

### 2026-10-22 — 清理旧文档

按用户要求重建本计划并删除旧计划，不保留旧方案全文或历史版本链接。

### 2026-10-22 — 明确独立 Webhook 与 Worker 自更新

用户明确 Server 更新器监听端口、仓库 Webhook 入队部署，以及 Worker 参数自动查询并更新本体。
本条取代上一版的 Server 轮询建议和 Worker 仅校验要求；保留其余删减、普通停启与解耦约束。
技术方案按上述流程待批准，实际安装方式和退出语义尚需确认；本轮未改实现或操作真实服务。

### 2026-10-22 — 源码运行跳过自更新

用户补充「源码目录执行就不更新」。本条取代此前源码检出切换与同步依赖的自更新分支；
源码目录或源码/editable 运行先在本地跳过，不查询、不安装、不改源码/环境或 Worker 状态。
仅正式安装包执行自更新，Server Webhook 部署方案不变；本轮只修订计划，未修改实现。

### 2026-10-22 — 取消源码检测与跳过分支

用户明确源码运行时不会执行 update，不需要增加跳过逻辑。本条取代上一条的源码检测与跳过方案：
更新命令仅面向正式安装包，不增加 cwd、源码路径或 editable 检测，不定义跳过状态或对应验收场景。
源码检出更新仍不在范围内；Server 方案不变。本轮仅修订计划与索引，未修改实现。

### 2026-10-22 — Server 更新事件改为主机本地文件

用户说明 PostgreSQL 位于 Kubernetes 集群内，要求更换更新事件的保存方式。本条取代此前
复用 PostgreSQL 事件表与 advisory lock 的方案：更新器采用主机本地 JSON 文件队列与文件锁，
持久化后应答、单消费者、中断保护和先停后启不变，不增加 SQLite 或数据库暴露路径。
业务 PostgreSQL 与通过 kubectl exec 完成的删列检查不变，Worker 方案不变。
代价是只支持单主机，依赖本地持久磁盘，磁盘丢失需人工核对恢复。本轮仅修订计划与索引，
未替换实现、配置真实服务或迁移/删除数据库记录。

### 2026-10-22 — Server 整个部署流程支持同镜像幂等重试

用户要求指定同一个镜像可直接重试并得到相同最终结果。本条取代此前 running/incomplete
一律人工恢复及 failed 核查后才能重试的限制：固定目标和 digest，复用操作记录，按 Kubernetes
实际状态补齐剩余动作；已就绪不重启，成功但未落盘时补记，候选检查与清理同样可重入。
已授权操作先于新事件续作，main 前移不改其镜像；不同镜像、目标身份冲突及记录损坏仍明确失败。
保持本地文件队列、删列检查、正常停旧启新和无自动回滚，Worker 方案不变。本轮仅修订计划与索引，
尚未改实现或验证幂等行为。

### 2026-10-23 — 功能实施前整理 Server 工具目录

用户要求执行功能前先调整当前文件目录。将事件入口归入现有 `scripts/server_update/`，
与部署脚本及列检查脚本放在一起；同步测试加载路径、部署指南和本计划，不保留旧路径包装。
只移动文件，不拆分模块或更改命令与部署行为。Worker 入口与共享发布核验仍在原位；
现有 PostgreSQL 事件模块留待文件队列实施时删除，不为即将删除的模块另做迁移。
目录整理已执行，监听、文件队列、Server 幂等部署和 Worker 自更新仍待实施。
验证：脚本内容逐字节不变，新路径入口及五个子命令帮助可执行，测试加载路径与文档链接有效，
Ruff、格式及三份 Server 脚本的类型检查通过。相关回归 6 项通过，26 项因未配置测试 PostgreSQL
跳过；未安装环境、连接真实集群或执行部署，不据此宣称数据库相关部署行为已经验收。

### 2026-10-23 — 独立监听、本地文件队列与同镜像部署续作

**触发与范围：** 按已批准的最新流程实施。修改
[事件入口](../../scripts/server_update/image_event.py)、
[部署工具](../../scripts/server_update/update-agentflow-server.py)和
[schema 注册](../../app/persistence/schema.py)，删除 PostgreSQL 更新事件模块；
业务数据库、Webhook/inbox、Images 发布步骤及列检查主体保持不变。

**实现与取舍：**

- `listen --host --port` 提供独立 `POST /webhook` 和并行 apply 消费。使用非空独立 secret、
  HMAC-SHA256、64 KiB/10 秒上限，严格筛选固定仓库、工作流和成功 main/push。
  字段路径已核对 Octokit 的
  [completed 事件 schema](https://raw.githubusercontent.com/octokit/webhooks/main/payload-schemas/api.github.com/workflow_run/completed.schema.json)、
  [workflow schema](https://raw.githubusercontent.com/octokit/webhooks/main/payload-schemas/api.github.com/common/workflow.schema.json)与
  [run schema](https://raw.githubusercontent.com/octokit/webhooks/main/payload-schemas/api.github.com/common/workflow-run.schema.json)。
- 每个目标显式 `init` 一次；JSON 事件按 run/attempt 原子替换并同步文件和目录，持久化后才返回 202。
  短期队列锁与生命周期消费者锁分开，网络和部署不持队列锁。保留终态历史，SHA 冲突 409、
  存储故障 503；目录或事件损坏拒绝静默重建。只支持单主机本地磁盘，不增加存储适配层。
- 继续复用 `resolve()`，先固定授权再部署。运行中操作优先于新事件；新旧 run number/attempt
  比较也覆盖已固定的失败事件，防止重投导致降级。checked/skipped 不自动转部署。
  部署失败不忙重试；重投/retry 恢复 failed，进程重启继续 running 及停旧意图对应操作。
- 事件入口直接加载同目录的部署模块并调用 `Update.apply()`；相较外部 launcher，减少一层
  不透明参数和状态目录接线，同时让两种入口共享目标锁与 journal。三份脚本须复制同版，
  不增加安装器或工具自动升级。主机端口、TLS 与进程托管仍由操作者配置。
- 首次预检和恢复校验分开。操作记录持久保存目标/UID、列快照、候选身份、授权与停旧意图；
  缩零之前落盘。恢复按实际 old/0、old/1、target/1 收敛，不依赖旧 Pod 存活；
  target 已启动只等 Ready/imageID，已 Ready 只补记结果，不再次停启。
- 候选 Pod 创建前固定名称/归属，重入核对 UID 和检查输入，按 UID 条件正常删除；
  create/delete/patch 响应丢失后读取资源核对。停旧前原 Pod 正常重建可重新取基线，
  停旧后不替换原身份。Kubernetes 非受管配置摘要只用于并发修改保护，未恢复源码/schema 指纹门槛。
- 停旧前失败不阻塞后续新镜像；停旧意图落盘后必须完成原镜像，拒绝异镜像覆盖、UID/配置冲突
  和损坏记录。保留单副本、PVC/HPA/节点保护、删列检查及无备份/回滚/强删边界。

**验证：** [HTTP/文件队列测试](../../tests/test_image_event.py)和
[部署 CLI 测试](../../tests/test_image_update.py)覆盖签名接收、并行消费、跨进程互斥、去重、
文件/替换/目录同步失败、固定版本续作、旧事件防回退、资源写入响应丢失和各持久阶段中断。
签名 HTTP → 本地队列 → 真实事件 CLI → `Update.apply()` 的组合用例在 Ready 后注入事件终态
写盘失败，再次运行得到 deployed，缩零/启动各仅一次。删列用例使用真实隔离 PostgreSQL，
Kubernetes/GitHub/GHCR 仍用边界替身，不声称已完成真实部署。

### 2026-10-23 — Worker 一次性原地自更新

**实现与决定：** [CLI](../../app/worker/cli.py)采用建议参数 `flowtool worker --update`，
核验和更新完成即退出；删除 `worker update --sha`，不在更新后启动 Worker。
首版限定同用户所有的 POSIX 现有虚拟环境、固定仓库的 uv Git 安装，具体合同见
[Worker 指南](../deployment/worker-update.md)。尚未核实目标主机是否满足此合同，不能据此宣称支持
任意 pip/wheel、容器或系统包安装。

[self_update.py](../../app/worker/self_update.py)读取实际安装包的 PEP 610 commit_id 和 RECORD，
核对所有权、未修改文件、导入路径和原入口；调用既有 `releases.latest_release()`，用公开 compare
确认前进关系后，以固定 SHA 调用 `uv --no-config pip install --python 当前解释器` 原地重装包及必要依赖。
无需新的环境、release 目录、版本标记或安装器框架；依赖按包需求解析，不宣称精确复现 uv.lock。
新隔离解释器再核对实际 revision/路径，新进程执行原 Worker CLI 帮助，`uv pip check` 验证依赖。
同 SHA 也完整核验；全部成功才输出 updated/already-current。

安装目录 flock 覆盖普通 Worker 的共享使用与更新排他操作，更新子进程继承锁；
原 root 锁和身份/journal 保持不变。旧版本进程、直接 Runtime 与手工包管理器仍须人工停止，
CLI 不操作系统服务，不检查源码/editable 并跳过，不增加后台更新器。原地更新失败可能改变
部分本体/依赖，不保证原子回滚；保持停止并修复原安装。

**验证：** [公开 CLI 测试](../../tests/test_worker_cli.py)使用临时安装、真实新解释器和只读 uv 检查，
验证包本体加载、同 SHA 健康检查、来源/占用/并发/中断和各失败阶段，不连接真实发布或执行真实
Git 包安装；测试环境未修改当前开发安装。普通 Worker 相关及发布核验回归一并通过。

### 2026-10-23 — 组合验证与简化对照

在隔离工作区复用机器已有 Python 3.13 环境和 PostgreSQL 17 程序。测试数据库使用独立临时
数据目录及本机 55404 端口，未安装软件、连接集群数据库或运行真实部署/自更新。

- `PYTHONPATH="$PWD" FLOWTOOL_TEST_DATABASE_URL=隔离测试库 python -m pytest -q`：
  **951 passed**，无跳过；有一条既有 Starlette TestClient 弃用警告。
- `python -m mypy app scripts/server_update`：117 个源文件通过。
- Ruff、修改文件格式检查及 `uv lock --check --offline` 通过。
- 当前指南、README、运维说明和变更日志已同步；本计划保留实施前基线并追加实际记录。

对以下五个公共场景先执行正常实现，5 项均通过；再用临时 pytest 插件逐项替换一个机制，
每种简化各产生 1 项预期失败，退出码均为 1，未发生收集/插件错误：

| 简化方式 | 复现选择器（pytest） | 实际结果与保留决定 |
| --- | --- | --- |
| 恒真代替签名比较 | `test_image_event.py::test_untrusted_inputs_and_storage_failure_never_acknowledge` | 无效签名返回 202 而非 401；保留验签 |
| 重投总写 queued，删除去重状态保留 | `test_image_event.py::test_consumer_pins_before_deploy_releases_queue_lock_and_reconciles_result` | 已部署事件被重新排队；保留去重 |
| 去掉目录 fsync | `test_image_event.py::test_failed_persistence_is_not_acknowledged_and_redelivery_recovers[directory]` | 同步失败场景错误应答 202；保留目录同步 |
| 恢复旧版 incomplete 一律拒绝 | `test_image_update.py::test_same_image_cli_resumes_every_durable_stage_without_restarting_ready_server[starting-new-pod]` | 缩零后不能重试完成；保留状态收敛 |
| 去掉 Worker 新进程 revision 比较 | `test_worker_cli.py::test_update_failure_never_reports_success[revision-fresh-verification]` | 安装仍是旧 SHA 却输出 updated；保留最终核验 |

各选择器在 `tests/` 下；对照仅做内存替换，不修改产品文件，实验前后源文件 SHA-256 完全相同。
临时插件已删除，未留下消融框架。恢复正常实现后重新执行上述全量回归通过。
真实 Webhook 投递、镜像拉取、Pod 停启和目标主机原地安装仍待单独验收；
目前只完成本地修改和验证，未提交、推送、合并或部署。

### 2026-10-23 — 删除发布成功状态的重复查询

成功以已验签 Webhook 的 completed/success 为准；`resolve()` 不再检查运行的
status/conclusion 或查询 job/step，覆盖方案基线中的重复成功核验。运行身份、run number、
当前 main、manifest 一致性及 digest 固定仍保留；Webhook 字段不能证明消费时 main 未前移。
手工 enqueue 由操作者确认成功。签名请求到 resolve 的两种临时验证通过，用例不永久保留。

### 2026-10-23 — 精简整个 PR 的测试

检查全部 6 个测试文件，删减其中 5 个；相较 PR 已发布版净删 290 行测试，另删除本地新增的
49 行一次性验证。移除重复队列验证、额外积压/长期离线场景、逐字段枚举和框架自身行为测试。
保留签名、持久化、删列、部署幂等/互斥、Worker 原地更新及断线恢复的关键覆盖；未新增测试。
验证：全量 922 passed；最终相关用例 80 passed，无跳过；Ruff、格式及 mypy（205 文件）通过。
复用已有隔离 PostgreSQL，GitHub/GHCR/Kubernetes/包安装使用替身；未修改业务实现或执行真实部署。
本批修改仅在本地，未提交或推送。

### 2026-10-23 — 删除三项重复参数用例

逐项核对后，删除文件同步失败的 `file`、动态删列的 `dynamic-drop` 和部署完成后的 `ready`
中断参数，并清除对应专用模拟分支。保留替换前/后的持久化失败、删列/删后重建，以及完成后
重复部署不再停启的覆盖，不修改业务实现。最终相关用例 77 passed；两个修改文件的 Ruff、
格式及 mypy 检查通过。未重跑全量测试，未执行真实部署或 Worker 包安装。
