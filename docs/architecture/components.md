# 组件职责

本页同时写目标职责和当前边界。组件的汇总状态以[架构入口](README.md#当前实现状态)为准。

## 包依赖边界

`domain/` 保存领域事实、端口与纯策略；`contracts/` 在这些类型上定义跨进程消息与冻结结果。
`persistence/` 拥有 SQL 和事务，`github/` 拥有资源、远端查询数据、绑定配置及其 GitHub SDK/GraphQL 操作，两者不反向依赖编排层。
领域容器、合同和仓储直接引用同一个资源模型；公开事实可以离线恢复，SDK/认证只在实际操作时加载。
`controler/` 是控制服务端，组合仓储、GitHub、协议与插件，负责工作流、Judge 决策和外部副作用；`worker/` 组合协议、CodingAgent
执行器及 VCS，不加载 control、数据库或 GitHub SDK 实现。轻量或通常会使用的依赖在模块级导入；
局部导入仅用于实际循环或依赖隔离。CLI 的 uvicorn、领域类型和离线 GitHub 资源直接导入，
控制端运行时、数据库与配置实例化仍归对应命令；加载 CLI 不提前加载 SQL、SDK 或控制端。

`telemetry/logging.py` 只提供通用日志配置；`telemetry/metrics.py` 由控制端使用，并调用
`persistence/metrics.py` 读取数据库指标。两者不在包入口集中导出，避免 worker 经日志导入 SQL。
GitHub 错误分类和 Retry-After 计时由 `github/errors.py` 提供，仓储通过 `app.github` 复用纯计算，
不会初始化 SDK。`domain/project_models.py` 只保留业务目标状态，GitHub Project 绑定配置及远端目标数据归 `github/projects.py`。
职责边界由 `tests/test_boundaries.py` 检查，另以独立解释器验证 worker 启动、实际 CLI 入口和资源 JSON 恢复的传递导入。

`controler/` 根目录的 `api.py`、`config.py`、`runtime.py` 分别拥有 HTTP/WS 入口、
`ServerSettings` 与 `ServerRuntime` 装配。子包按职责归组：`events/` 消费并路由持久 webhook，
`workers/` 管理 Worker 连接、派发通信与历史查询，`workflow/` 拥有 Flow/Job 准备、交付转换、
结果提交及副作用恢复，`review/` 加工审核上下文和反馈并运行 Judge，`reconciliation/` 对账
PR、反馈与 Projects。事件、Worker 管理和对账调用工作流，工作流调用审核模块；
各子包直接引用已有领域类型、协议、具体仓储/GitHub 及配置，不反向依赖 API 或运行时。
Runtime 统一初始化工具、effects、结果应用和 conversation broker；各服务仅接收实际
使用的数据库与服务，自行创建需要的具体仓储。Runtime 管理共享 Database 的初始化和关闭，
向服务提供同一 Database。Gateway 直接使用工具和结果应用，结果应用的锁由 Runtime、coordinator
与 Gateway 共享。Webhook 消费器接收配置、插件、通知回调和 Database，在每次 receipt
事务内直接创建 ControlService 与 EventRouter，构造函数以事务上下文创建仓储；通知和 hook
排到外层提交后执行。API 和对账使用独立的常驻控制服务。
各包入口只说明职责，调用者直接导入具体模块。

目录列表见[当前目录](roadmap.md#8-当前目录与里程碑)。ASGI Python 入口为
`app.controler.api`，Judge 内部子进程入口为 `app.controler.review.runner`；
CLI 命令为 `flowtool serve` / `flowtool worker`，真实验收入口为 `app.live_smoke`。

## 4.1 Webhook Ingress（FastAPI + uvicorn）

职责：读取原始请求体，用 Webhook secret 校验 `X-Hub-Signature-256`，把有效事件交给 router，避免
Codex 等长任务占用 GitHub 的 Webhook 请求。

当前实现：

- `GET /healthz` 返回 `{"status": "ok"}`；它是存活探针，不检查数据库或外部依赖；
- `GET /readyz` 检查运行时启动状态与数据库；`GET /metrics` 由独立 token 保护，未配置时关闭；
- `GET /api/flows/{flow_id}/conversation` 校验 Cloudflare Access JWT 并限制单 owner 邮箱，经 WS 查询原 worker 历史，
  不在数据库复制完整会话正文，详见[会话查询 API](conversation-api.md)；
- `POST /webhook` 缺少或带错误签名时返回 401；签名正确时持久化 webhook inbox 后返回 202；
- `WS /internal/workers/connect` 按 token map 鉴权，并为每个 worker identity 保持一条连接；
- FastAPI lifespan 初始化 PostgreSQL schema，启动 reconciler、lease sweeper 和 result inbox recovery，不启动进程内 worker；
- 自动化测试覆盖 health、错误签名、正确签名、202 持久化确认和一条 signed label→job 路径。

边界：202 表示事件已持久化到 webhook inbox；后台消费者在事务内调用 Event Router，失败后可重试。
202 不表示业务处理已经完成。

### 4.1.1 Web 控制台展示归档

控制台的 `POST /api/flows/{id}/archive` 接受空 JSON 对象，复用单 owner Access 验签，
拒绝明确跨站请求；工作流业务命令仍由 GitHub 与 CLI 发起。
它在 workflow/publication 锁内重新校验 cancelled，持久保存 `console_flows.archived_at`，
将卡片移到 done 并保留取消状态、停止确认与结果对账要求；重复请求幂等，任务和会话历史仍可阅读。
投影刷新和重建保留该标记；重试或合并对账恢复生命周期后清除。
此入口不创建 ARCHIVE job，不修改 Worker、交付或 GitHub，不将取消当作成功。
见[功能记录](../plan/cancelled-flow-archive.md)与[控制台说明](../../app/controler/web/README.md)。

## 4.2 Event Router

当前实现：

- 触发/反馈忽略配置 bot login 及 GitHub `type=Bot` 的事件；合法关联 PR 的合并事实不受此过滤；
- 以 `X-GitHub-Delivery` 原子占有并持久化去重，只有成功插入的 handler 才继续处理，同时记录原始 payload；
- 仅允许 allowlist 用户通过 `issues.labeled(flow)` 创建 active flow 与 initial job；
- `issues.unlabeled(flow)` 或严格等于 `/flow stop` 的评论取消 active flow；
- review body、inline comment 和 conversation 按 Flow/kind/GitHub ID 保存不可变版本，保留来源 attempt；
- 反馈 delivery、版本和 deadline 共用事务，持久 settle 后冻结批次；仅 allowlist 作者提交的 Request Changes 经 grace 直达 REVISION；
- `pull_request.closed (merged=true)` 原子保存事件去重、合并时间/SHA、delivery version，停止
  queued 编码/review 和活动批次并入队唯一 ARCHIVE；校验 repo/PR/installation，approve 不归档。
- 合并后的任务派发、flow.open_pr、结果应用和 GitHub 写入均检查 delivery；迟到结果只保存
  执行事实并收尾 ACK，已发 GitHub 操作只查询回执，ARCHIVE 等待旧执行/结果收尾。

M2 F1 自动化覆盖反馈工作流锁、同 Flow 批次串行、重启恢复和精确版本消费。
WebhookConsumer 已持久化失败退避和重放；PR 对账处理已追踪 PR 的 merge/close/reopen，
反馈对账补充 Issue/PR/review/线程漏投。未合并 close 暂挂而不取消 Flow，保留原绑定，
重开后核验基线再恢复；漏失的新 Flow trigger 不由对账补建。真实 GitHub payload 兼容性
仍需服务验收。依据：[消费者](../../app/controler/events/webhooks.py)、
[PR 对账](../../app/controler/reconciliation/pull_requests.py)、
[反馈对账](../../app/controler/reconciliation/feedback.py)。

## 4.3 Control Service / Job Coordinator（状态机核心）

当前持久状态分轴保存；编码、审核和修订阶段由 job、反馈批次与 delivery 推导：

```text
Flow:     ACTIVE → SUCCEEDED / CANCELLED / FAILED
Delivery: NONE → OPENING → OPEN → CLOSED / MERGED
Review:   settle → READY → PROCESSING → COMPLETED / WAITING
          新反馈进入后续批次；终态取消活动批次
```

`ControlService` 在显式事务中负责触发、取消和 retry，并由 `lifecycle` 提交合并事实；
`lifecycle` 也拥有创建 PR 的身份核验、绑定及 close/reopen。`JobCoordinator` 把 INITIAL、REVISION、ARCHIVE
持久 job 构造成版本化 task，并在创建 revision 前直接处理需要 Judge 判断的反馈批次。
Runtime 创建并共享的 `ResultApplication` 按 job ID 读取并校验已持久接收的 result，
冻结一次结果的领域变更和 GitHub 操作，
由 `ResultEffects` 查询/执行远端操作并保存回执，最后一起提交本地状态和结果确认。
PR 依赖操作与 INITIAL 提交复用 `contracts.result_models.unique_pull_request_receipt`，
只消费已读取的唯一创建回执；退休恢复缺少唯一回执时继续等待，不提交本地完成状态。
`ResultApplication` 同时拥有逐 job 锁、有界分页和固定并发恢复；Runtime 调度其恢复方法，
Gateway 持久接收后直接调用 `self.results.apply`，核验提交完成后才发送 ACK；
工具请求直接调用 `gateway.tools.execute`。内部配置、数据库、仓储与服务依赖使用具体类型。
网络请求不占用数据库事务；plugin hook 在提交后调用，不具备持久化投递保证。worker 不能直接更新这些状态。
`JobCoordinator.prepare_for_assignment` 完成任务构造、push capability 签发和 I/O 后业务复查，
直接返回 wire `Assignment` 或 QUEUED/DONE/FAILED。读取故障使用持久重试预算，签发故障直接失败。
`WorkerGateway` 在准备期间维持租约心跳，连接结束时取消并等待准备协程；发送前仍独立调用
`self.stops.authorize_dispatch`，再发送 coordinator 构造的消息。
非零 Codex exit 会转 FAILED。revision 成功后仅消费明确 addressed 的 item/version，deferred/遗漏项/
clarification 保持 waiting；不把 addressed 当作远端 resolved。有未决问题时不重新请求审核。

M2 已接入有界 Issue/PR/patch/讨论快照、线程/comment 映射与原线程回复/保守 resolve。
当前 coordinator 仍有以下边界：

- 上述路径有自动化覆盖，真实 GitHub App/Codex/bub 闭环待验收；
- 同秒已出现的编辑转换保留待核对，不重复回放；新鲜观察可推进，见 [M4 F1](../plan/m4-feedback-replay.md)；
- 冻结上下文尚不包含 CI 状态；
- cancel 持久化停止意图，worker 取消执行并回收持有的进程组；未知旧执行和未决 effects
  保留绑定，无强制释放。cancel 不撤销已发生的 push/PR/评论，也不执行归档清理；
- 未合并 close 暂挂，reopen 核验 head/base 和旧执行后恢复；修订拒绝远端 head 漂移，
  人工 push 的自动接纳、rebase/冲突处理尚未实现。

因此状态转换代码存在不等于目标闭环可用。

### 4.3.1 远程 Worker Gateway / Client / Executor

serve 的 `WorkerGateway` 校验每个 identity 的 bearer token，维护多会话 registry，并用随机 connection epoch 与
assignment token 对 Started、Heartbeat 和结果做四元组 fencing。每个 worker 是单槽，PostgreSQL 按 FIFO、
worker `last_assigned_at` 和 Flow execution binding 事务化领取任务。worker 的 `WorkerClient` 主动连接并指数退避；
`WorkerExecutor` 只依赖 CodingAgent 和 Flow repo/VCS helpers，不导入 Store、FastAPI 或 GitHub。

不同 identity 可并发在线；同 identity 新连接取得新 epoch 并关闭旧 socket，数据库条件保证旧连接最终失效。
未授权派发的任务断线后可在 lease 到期 requeue；已授权派发但缺少 Started 的未知执行不当作
未发送重排。Started job 进入 RECOVERING，只允许原 identity 在 grace 内恢复，
到期才把 job/活跃 flow 置 FAILED，不覆盖 CANCELLED。取消通过持久 StopExecution /
ExecutionStopped / StopAccepted 握手收敛；停止凭据与已发送 effects 未确认前不释放绑定，
离线和 lease 到期不能替代停止证明。drain 只阻止分配新 Flow，已有绑定继续 revision/archive。
详见[取消、drain 与停机](../operations/cancel-drain.md)。
已持久接收的终态结果由 control 接管，不因 worker 的 recovery deadline 到期重新执行或失败；
后台循环持续恢复未应用结果。必要的 GitHub 操作未完成时不发 ResultAccepted，worker 保留 journal 重连。
Flow 非终态时，即使暂时没有 running job，binding 也继续独占该 worker/repo。
worker socket 与 executor 解耦，root 内原子 journal 在 ack 前保存进度和终态结果；
进程重启可重放终态，但不会猜测恢复已经消失的 executor。同一 attempt 的后续任务仍只回到首次 Started 的 worker，
不支持跨 worker session 迁移。同机默认 root 按 identity 派生，并用持久 identity 标记与独占锁拒绝并发或顺序复用。

## 4.4 CodingAgent Drivers

`WorkerRuntime` 默认装配 Codex；`FLOWTOOL_CODING_AGENT=amp` 装配本地 Amp CLI，采用各自的
单轮超时，并将所选超时用于 revision deadline。INITIAL/REVISION 的业务 Schema、VCS 核验、
交付回执和 ARCHIVE 的 workspace 释放由共用 executor 负责。

### Amp

- `amp_agent.AmpCodingAgent` 适配 worker 的编码请求，使用私有 local execute thread；
  prompt 经 stdin，恢复显式指定同一 `T-UUID`，
  固定 `--mode low`，不运行 orb、runner 或 SDK。原生登录或 `AMP_API_KEY` 归专用 worker 账户。
- 临时 settings、Git helper 和 `gh` 环境在每轮重建，复用本轮 GitHub capability；INITIAL 配置
  当前 WorkerMCP，REVISION 禁用旧 `flow` broker。进程环境不继承 control/worker 或个人 GH token。
- 成功必须具有完整、同 session 的 init/result、进程退出 0、`is_error=false` 和 `subtype=success`。
  最终文本原样交给 executor，Schema 只作提示，不修复 JSON 或自动重跑未知执行。
- `amp_process.run_amp` 供编码与历史导出共用，并行有界读取两条管道；取消/超时先 SIGTERM，升级时包含事前捕获的独立
  shell 进程组。该快照无法保证回收任意脱离进程树的进程；仍依赖外部隔离。
- `ExecutionState` 按 ID 格式选择历史来源。Amp 原生 export 经公开块映射与脱敏后短时缓存，
  支持外部追加、块内分页、前缀编辑失效；不保存第二套捕获历史，不显示 thinking。
  记录 ID 保留 session 前缀，满足 control 的来源校验；两端使用 protocol v15。
- workspace Amp 设置可能覆盖临时设置，因此执行前发现设置即拒绝。managed policy/账户插件仍由
  运维核查；有效工具列表检查只影响结果接收，不能替代权限或 OS 隔离。

配置、历史预算和取消限制见[部署说明](../deployment/README.md)；实现与实测记录见
[Amp feature record](../plan/worker-amp-cli.md)。

### Codex

`CodexAgent` 已实现 `CodingAgent` 端口的 `start` / `resume`：

- start 仅在显式 `FLOWTOOL_TRUSTED_HOST_EXECUTION=true` 时使用
  `codex exec <prompt> --json -C <flow-repo> -s danger-full-access`；
- start/resume 都要求 trusted-host 开关；worker 还必须声明专用 account/container/VM 隔离，且不得继承
  control-plane 环境变量；
- 从 JSONL 的 `thread.started.thread_id` 读取 session id；
- start/resume 都显式接收当前 Flow 的 cwd，CLI 参数与子进程工作目录使用同一个值；
  resume 通过配置覆盖保存的 cwd 与 sandbox；
- `CodingExecutionContext` 只携带临时 push token 与 MCP endpoint/token；MCP 凭据必须成对提供，
  未提供 endpoint 时 start/resume 均显式禁用 flow MCP，修订 fallback 也遵循这个规则；
- VCS 远端验证只接收 push token，在 agent 仓库之外查询固定 URL；repository、过期时间与
  worker binding 仍由 executor 校验；
- `-o` 读取原始最终消息，`--output-schema` 约束 CLI 输出；`WorkerExecutor` 按 INITIAL/revision
  严格解析各自结果模型，adapter 不解释业务 schema；
- 子进程只继承 Codex 认证、代理/CA 和基础运行环境；INITIAL 额外获得单次短时 push credential helper 与
  指向 worker 进程内 loopback HTTP 服务的 `flow.open_pr` MCP 配置及单次 Bearer 凭据，不获得 control/worker credential；
- 单次运行使用配置的 timeout，超时 kill 子进程并返回 exit 124。

2026-08-01 的独立 CLI 实测证明 `exec` / `resume` 上下文连续、workspace-write 可修改工作区、jj
`.jj` 可写且能 commit。它没有证明当前 driver 能在真实 flow 中完成 issue→commit→push→PR。

当前 worker 返回 exit/result、worker 验证的 VCS facts 和 `flow.open_pr` handle；control 将完整
terminal payload 与 digest 持久化到 jobs，并与持久 tool outcome 核对。INITIAL 成功/失败和
REVISION 的有序 invocations（包括 resume 失败后 fallback）均以该结果为编码审计来源。
`result_received_at` 和 `result_applied_at` 分别表示接收与应用时间，不表示进程执行开始/结束。
跨协议不回传完整 prompt 或原始 JSONL。GitHub-only egress 和多轮 context 增长仍待验证。

## 4.5 GitHub 资源

目标职责：以 GitHub App installation 身份创建 PR、评论、设置 labels、请求 reviewers、列出 review
threads、resolve thread，并签发 push 所需的短时 installation token。自动合并明确不在职责内。

`GitHubApp` 已对齐锁定的 githubkit 0.16，负责 App 认证及安装绑定；`Repository` 读取仓库事实、
获取 Issue/PR，并通过父 App 签发只含单仓库 Contents write 的短时 capability。
Issue/PR、线程和 Project/Item 拥有各自操作；查询返回新的不可变观察，错误转换不含响应 body。
M2 已加入类型化 GraphQL 线程/comment 分页、共享预算、原线程回复和 resolve/复读，
并通过隔离自动化。Projects v2 资源 API 与独立同步已接入；真实 GitHub App/看板 smoke、
关闭 issue 等未纳入当前流程的动作仍待验收或实现。接入边界见 [GitHub App 文档](github-app.md)。

## 4.6 本地平面：Flow repo / execution policy / VCS

| 轴 | 是否端口 | 当前载体 | 当前边界 |
|---|---|---|---|
| Flow repo | 否 | `prepare_flow_repo/cleanup_flow_repo` | stable flow id 派生独占完整 repo；同 Flow 复用、不同 Flow 分离 |
| execution policy | 否 | worker config / `CodexAgent` | full access 需专用 account/container/VM；目录只表达所有权 |
| VCS | 否 | `agent_guidance/verify_pushed_outcome` | Agent 自探测；worker 在 tool 前用固定 remote 和净化、限时命令验证 ancestry/clean/remote SHA |

分支名由 control 定为 `flow/<flow-id>`。worker 内的 Code Agent 在 Flow repo 完成 branch/bookmark、commit 和
push；worker 在转发 `flow.open_pr` 前核验，control 再读取 GitHub ref 核对 SHA 后才创建/复用 PR，最后以
terminal result 三方核对。当前缺口包括：

- 每 Flow 容器/微 VM 和互不信任租户的强隔离仍属 M5；M1 至少要求 worker 与 control secret 分离；
- 仅允许 GitHub egress 的策略未实现；
- installation token 尚未在真实 GitHub 上验证，且 GitHub-only egress 未强制；
- 大 repo 的完整复制成本和 terminal cleanup 故障恢复仍待验证。

## 4.7 Store（PostgreSQL）

当前持久层由 Database 与具体仓储组成；Store 只为运维、live smoke 和测试聚合仓储。

反馈批次的前置判断/直接修订选择、指令和未决问题归并由 `review_policy` 的纯函数决定；
`recovery_policy` 根据固定时间及读取的事实决定重排、失败、保留诊断和绑定释放。
功能仓储负责候选查询、信号/回执事实和原子写入，Database 统一维护连接、事务、时钟和共用锁。
WorkerStore 的恢复扫描在原 worker 锁事务内先提交 job 动作，再查询事务内更新后的绑定事实；
策略函数不执行 SQL、读取时钟或调用外部服务。

服务接收共享的 `Database`，自行创建并持有以下具体仓储，使用 Psycopg 3 同步连接池访问
PostgreSQL，例如在构造函数内执行 `self.flows = FlowStore(database)`，业务调用使用
`self.flows.get_flow_by_pr(...)`。服务与功能仓储均不依赖聚合 Store：

| 仓储与模块 | 操作归属 |
| --- | --- |
| `FlowStore` / `flows.py` | Flow 与 Attempt 的事实读写 |
| `DeliveryStore` / `deliveries.py` | 交付状态与 tool-call 去重账本 |
| `JobStore` / `jobs.py` | Job 生命周期、未应用结果分页及完成提交 |
| `WorkerStore` / `workers.py` | Worker、Flow binding、fenced assignment、准备与租约恢复、drain |
| `StopStore` / `stops.py` | 停止请求、发送授权与停止回执 |
| `ResultStore` / `results.py` | 冻结结果计划和 effects/receipt 恢复 |
| `ReviewStore` / `reviews.py` | 反馈版本、批次、审核上下文及线程事实 |
| `WebhookStore` / `webhooks.py` | Webhook inbox 消费和事件去重审计 |
| `ReconcileStore` / `reconciliation.py` | PR 与反馈对账进度 |
| `ProjectStore` / `projects.py` | Projects 投影与有类型的局部更新 |

`schema.py` 集中完整建表 SQL，初始化沿用既有依赖顺序；`records.py` 仅提供原生行类型及
必需记录回读校验。`database.py` 提供连接、显式事务、注入时钟、共用锁和 schema 初始化；
Runtime 管理该数据库的生命周期。`repository.py` 的 `using(tx)` 保留具体仓储类型，
返回绑定事务连接的视图；原仓储保留自己的连接范围。仓储需要其他仓储的 SQL 操作时，
显式使用具体类型并传入同一上下文。Store 仅为 CLI 运维、live smoke 和测试提供整组仓储，
其事务视图用于这些入口的数据库操作；Runtime 不使用 Store 装配服务。
领域数据类型继续拥有自己的校验和纯规则，持久化操作归上述仓储；时间统一使用注入时钟。

`Flow`、`FlowExecution`、`FlowDelivery`、`FlowToolCall`、`Attempt`、`Job` 是 Pydantic
持久化模型。返回这些领域对象的查询在
cursor 上使用 Psycopg `class_row(Model)`，由模型统一校验枚举、时间和其他字段；其余轻量查询继续
使用 pool 的 `dict_row` 默认值。SQL、JSONB 适配和原子操作由对应仓储显式维护，
事务边界与共用锁由 Database 提供，没有引入 ORM。

- `flows`：issue 级状态，`(repo, issue)` 仅允许一个非终态 flow；
- `flow_executions`：Flow 到单槽 worker/repo identity 的生命周期 binding 与 recovery deadline；
- `flow_deliveries`：每 Flow 唯一 branch/base/PR、交付状态和版本；
- `flow_tool_calls`：fenced command 的参数与成功交付回执 response，支持同 call id 重放；
- `attempts`：一次运行/恢复尝试的 session 与 revision cycles，不拥有 worker、branch 或 PR；
- `jobs`：queued/running/done/failed、worker/token/start、lease/recovery deadline、result inbox/digest 与尝试次数；
- `result_applications`：按源 job 唯一保存冻结的结果计划；
- `result_effects`：有序 GitHub 操作、claim/fencing、远端回执、退避时间和最后错误；
- `workers`：identity、协议版本、epoch、online/busy/recovering/offline/failed、当前 job、连接/心跳与断线原因；
- `events`：delivery 去重和 payload 审计；
- `webhook_inbox`：202 前持久接收的原始事件、消费 claim、重试与保留状态；
- `execution_stops`：绑定到具体执行的停止意图、凭据 digest 与确认状态；
- `review_items` / `review_versions`：Flow 内稳定反馈身份、不可变版本、独立 relay/consume 时间和已核验 addressed 依据；
- `review_pending_edits`：同秒乱序编辑的前序正文与原始来源；只有未在历史表示且后继无歧义时原子回放，其余保留待核对；
- `review_buffers` / `review_batches`：持久 deadline/generation、冻结反馈/上下文/线程快照、批次所有者 job 和未决问题；
- `review_thread_observations`：远端线程快照与真实 resolved 状态，不以本地 addressed 代替远端确认。

新库不创建 `codex_runs` 或其唯一索引；应用停止写入和依赖该表。已有库的旧表及数据在
初始化时保留，不 DROP、不回填。外部 SQL 消费者需迁移到 jobs 的持久结果；旧表人工清理
另行处理。重复应用、control 重启及已合并 PR 的迟到结果保留原始 payload、digest 和接收时间；
应用时间与业务变更在同一事务提交，退休结果不改变 delivery 或当前 session。

自动化覆盖 active 唯一约束、flow/attempt/job 往返、review upsert、assignment start/heartbeat/fencing、
lease 到期 requeue、started grace/deadline、epoch fencing、result inbox 去重、跨 Store 可见性、delivery 去重和并发 assign。
`WorkerStore.assign_job()` 使用事务内的 `FOR UPDATE SKIP LOCKED`，gateway 接受多个独立配置 worker。

仓储继续使用同步接口。assignment claim 无锁读取公平候选，只有请求方就是候选时才锁定自身 worker，
重新检查 epoch、在线状态、空闲槽位及 drain/binding；随后锁定最早 eligible Flow job，并原子创建
或复用 lifecycle binding。`with database.transaction() as tx` 提供绑定连接的数据库上下文；
服务用 `self.flows.using(tx)`、`self.jobs.using(tx)` 绑定自己持有的仓储。
所有步骤共用该连接，异常一起回滚，事务退出后这些视图拒绝继续使用。
结果的最终事务同时写入 flow/delivery/attempt、带源 job 唯一键的派生 job、`result_applied_at` 和 worker slot；
编码审计保存在源 job 的终态结果，不再写 `codex_runs`。
`flow_tool_calls` 的 prepared delivery 与 response 也一起提交。

评论使用稳定隐藏 marker 并分页核对本 bot 作者，PR 使用固定 head 查询，labels/reviewers 则核对目标状态。
不能确认是否成功的非幂等请求保持 uncertain，不盲目重复 POST；`flow show` 显示待处理操作，retry 不能绕过它们。
因此不宣称跨 PostgreSQL/GitHub 的通用 exactly-once。跨 worker session 迁移和多 serve HA 仍未实现。
当前只初始化完整 schema，不执行历史迁移、数据回填或跨版本结果转换。serve/worker 使用同一
版本，wire protocol v15 / journal v4；持久结果的重放保障适用于当前版本的重启恢复。
journal v4 内的旧 v14 回执沿用既有内存规范化；读取不写文件，正常 ACK/状态保存时写为 v15。

## 4.8 GitHub 交互面

目标用户体验完全位于 GitHub：标签触发、PR review 反馈、网页人工合并、状态 labels 与评论。

当前 router 已识别触发、取消、review/comment 和 merged 事件；PR 创建、评论、reviewer 和 label 已有
经隔离测试的具体资源操作，且 reviewer 配置不复用 trigger allowlist。真实 Issue/default branch、
head PR 查询和 capability 已接入 INITIAL；M2 原线程回复/resolve 已接入并有自动化覆盖，
Project 状态更新由独立 ProjectSync 投影循环负责，远端失败不改变 Flow；真实 App/看板校准仍待完成。
工具没有自动合并实现。

`app/github` 以 `GitHubApp → Installation → Repository → Issue/PullRequest → ReviewThread`
组织仓库资源，`Installation → Project → ProjectItem` 组织看板资源。App 持有认证配置，安装
持有 installation ID；control 从 Flow、effect 或 Project 配置的持久身份绑定具体资源，方法只传操作数据。
`fetch_*` 和 `refetch()` 返回当次查询的新实例，成功状态更新返回仅确认字段改变的副本。
已有字段保留之前的观察，嵌套线程与项目配置不可变；仅绑定身份时，未知事实不能作为属性读取。
标准序列化排除私有认证及父资源引用，持久化直接序列化同一个资源模型，离线恢复不创建认证对象。

## 4.9 Context Builder

当前 `ContextBuilder` 是纯字符串组件：

- initial prompt 包含 issue 标题/正文、首个 `AGENTS.md` 或 `CLAUDE.md`、VCS 指引和硬约束；
- revision prompt 包含 Judge instruction、冻结批次的 item/version、inline path/diff hunk/original commit id、
  原 Issue、交付基线、此前 summary/open_points、VCS 指引和 `RevisionCodingOutput` 输出约定；有冻结 PR context 时一并传入。

INITIAL 已使用 GitHub Issue 的真实标题与正文；冻结 PR context 包含有界 patch 和讨论，不含 CI 状态。
F4 的完整线程映射由 control 持有，遵循当前 worker v13 契约。issue/PR 文本是不可信输入，必须保留明确的数据/指令边界。

## 4.10 Judge / Triage

语义见 [Review 修订循环](review-loop.md)。`review/context.py` 在 control 读取 Issue、PR 与分页文件/
讨论，冻结有界 JSON 快照；先后读取 PR 防止 head/base 在取数期间变化，未完成批次复用入库快照。

triage 是 worker 请求之前的批次判断，由 control 的 review sweep 顺序处理 ready 且无 job 的批次。
REVISE 在工作流事务中创建 revision job 并关联批次；ANSWER/IGNORE/WAIT 直接处理批次。
没有独立 job、assignment token 或本地任务恢复；中断后重新读取未完成批次。有效性检查与原子
关联避免迟到决定或重复下发，过程不更改 worker 状态或 Flow binding。答案已发送但回执不明时，
只查询批次 marker，不重复 POST。`ReviewStore` 拥有上下文失效、答案发送授权和确定拒绝后的复位；
coordinator 不直接更新业务表。批次答案与结果评论保留各自的事务授权和 marker 恢复，
共享 `GitHubError.definitely_rejected` 的拒绝判定；线程回复与普通结果评论共用一个执行分支。

control 的 `BubJudge` 启动独立 `app.controler.review.runner` 子进程，显式配置 bub 0.4.1 `ModelRunner.completion_response`，
不创建 framework、加载插件、读写 tape 或执行工具。每批次独立请求与会话 ID；凭据仅通过允许的
子进程环境传入。单次调用无 fallback/SDK retry，父进程限制时长和 stdout/stderr，取消时回收子进程。

严格校验四类 verdict、有限 confidence、动作字段和 envelope 身份。结果持久保存模型/adapter 版本、
context digest、reason/open_points；answer 使用同一个答案，由反馈批次持久化发送授权并通过
批次 marker 核对回执，不再调用模型。
adapter/控制流已通过自动化，真实 provider 的语义准确性仍待 smoke 验收。


### Worker 执行生命周期

`WorkerRuntime` 装配一个进程级 `WorkerExecution`，拥有根锁、信号、健康任务与关闭顺序。
`execution/lifecycle.py` 中的 owner 持有唯一执行 Task、工具请求和原 Future、停止清理 Task。
`WorkerExecutor` 继续编排 initial/revision/archive、agent、MCP、VCS 和 workspace，不管理连接。
Client 只借用 owner；替换前必须关闭并 join 旧 Client，同一 owner 不支持并行通信消费者。

`state/execution.py` 的 `ExecutionState` 保存当前 assignment、持久化阶段、修订调用记录、
终态脱敏/归档、恢复描述及停止/清理凭据。`ExecutionRecord` 保留 `assignment-journal.json`
的原 v4 JSON 结构与 digest。执行自然结束时 owner 主动落盘，无需等待重连；落盘失败保留
完成 Task 的结果，恢复时重试，不能重新执行。Register 后完成的任务可接收原 ResumeAssignment，
然后重放终态；普通 started 任务在进程重启后没有 Task 时仍不能推断已停止或重新运行。

首次派发保持 `record_received → 发送 Started → record_started → 创建 Task` 的顺序。
工具调用由 MCP 交给 owner，Client 借用队头并发送当前 epoch 的副本，收到响应后交回 owner
完成原 Future。断线不出队、不换 call_id；原 socket 仍消费已取消调用的匹配迟到回复。
`on_result_accepted` / `on_stop_accepted` 只处理收到的确认，不发送 ACK。

WorkerClient 在 `_receive_message()` 统一解析 WebSocket 消息，历史查询并发执行并在原连接回复；
其他任务消息继续进入执行/恢复流程。每条连接最多 4 个历史查询，连接结束时取消并回收查询任务。
历史读取经注入的 `read_history` 调用 `ExecutionState.read()`，再选择 Codex/Amp 读取器，
允许查询 control 指定的既往会话；无额外历史 facade。公共 Session/Gateway 留待下一阶段提取。

WorkerMCP 随首次编码启动与关闭，复用现有 HTTP 依赖，仅暴露 open_pr；无 STDIO 桥接进程。
修订恢复覆盖为禁用的 flow MCP 配置，避免重新连接旧任务端口。MCP 服务不接管 worker 信号。


Worker 目录分为 `execution/`（lifecycle、executor、Codex/Amp、prompts、VCS、credentials）、
`transport/`（control client、MCP）和 `state/`（execution、history）。
根目录保留 runtime/config/health，分别负责组装、配置和探针入口；各子包不提供转发导出。
