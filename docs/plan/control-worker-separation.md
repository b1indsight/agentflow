# 远程单 Worker 执行闭环计划

> 状态：已实施并验证 · 计划日期：2026-08-31 · 实施/验证日期：2026-09-01 · Feature 1/3
>
> 本计划只建立 `serve` 与一个远程 worker 之间可工作的完整闭环。多 worker 调度与单机多实例属于
> Feature 2；忙碌重连、租约恢复和 `serve` 重启接管属于 Feature 3。

## 1. 背景与目标

当前 `flowtool serve` 同时装配 FastAPI/Webhook、`FlowEngine`、Codex/Judge/GitHub adapters 和进程内
`Worker`。worker 直接轮询 PostgreSQL，控制面与执行面没有网络协议或权限边界。

本 feature 完成以下最小纵向切片：

1. `flowtool serve` 成为唯一数据库权威，负责 Webhook、状态机、GitHub API、任务持久化和一个 worker
   连接；
2. 新增独立 `flowtool worker` 进程，通过主动发起的认证 WebSocket 长连接接收所有现有 job kind；
3. worker 只执行 workspace、Codex、Judge 和清理动作，不连接 PostgreSQL，也不持有 GitHub App
   私钥；
4. 把当前 `FlowEngine` 拆成控制侧 coordinator 与执行侧 executor，双方只交换版本化 Pydantic 消息；
5. 没有 worker 时 job 保持 queued；唯一 worker 在线时逐个执行，并把结构化结果交给控制面落库；
6. `serve` 与 worker 可独立启动、停止和部署，原 `serve` 不再暗中运行进程内 worker。

验收后系统仍只允许一个 worker identity 在线。这是刻意收窄的安全边界，不是多 worker 的临时宣传。

## 2. 范围与非目标

### 2.1 本次包含

- 单 worker WebSocket 鉴权、注册、任务派发、started/heartbeat/result/ack 消息序列；
- INITIAL、TRIAGE、REVISION、ANSWER、ARCHIVE 五类 job 的完整 task/result 契约；
- 控制面独占 Store/GitHub，worker 独占 Codex/Judge/workspace 的模块边界；
- 最小 assignment token，拒绝不匹配 job 或旧连接的 heartbeat/result；
- role-specific 配置和独立 worker root；
- job 未开始即断线时安全 requeue，已开始后断线时 fail closed 并要求显式 retry；
- 当前测试、CI smoke、运行文档与架构图迁移到双进程形态。

### 2.2 本次不包含

- 第二个 worker、调度公平性、跨 flow 并行或单机多 worker；
- attempt affinity 的多节点选择逻辑；单 worker 下只记录 identity，不做选择；
- 忙碌 worker 跨断线继续执行、终态结果重放、connection epoch、持久 lease expiry 或 `serve` 重启后
  恢复运行中 job；
- 多 `serve`、broker、控制面 HA 或 exactly-once 外部副作用；
- Kanban Web UI、新的无认证管理 API、完整 Reconciler、可靠 Codex kill 或真实 GitHub/Codex/bub
  集成校准；
- 拆成多个 Python distribution。两个角色仍由同一个 `flowtool` 包交付。

## 3. 架构与模块布局

```text
GitHub / admin CLI
        |
        v
+---------------- serve ----------------+
| FastAPI/Webhook -> ControlService      |
|                  -> JobCoordinator     |
|                  -> Store / GitHub     |
|                  -> WorkerGateway -----+---- authenticated WebSocket
+----------------------------------------+                  |
                                                   +--------v--------+
                                                   | single worker   |
                                                   | Codex / Judge   |
                                                   | workspace       |
                                                   +-----------------+
```

目标布局：

```text
app/
  control/
    service.py       # 触发/取消/合并/重试及查询
    coordinator.py   # job -> task；result -> 状态/GitHub 副作用
    gateway.py       # 单 worker WebSocket 会话
    router.py        # GitHub Webhook 路由与 settle
    reconcile.py     # 控制面对账
    api.py           # Webhook、health、internal worker endpoint
    runtime.py       # serve 组合根
  worker/
    client.py        # WebSocket 协议循环与空闲重连
    executor.py      # workspace + Codex/Judge，不访问 Store/GitHub
    runtime.py       # worker 组合根
  protocol.py        # 共享的 Pydantic 传输契约
  models.py          # 持久化领域模型
  store.py           # 仅控制面导入
  cli.py             # serve/worker 与现有 admin CLI
```

现有 `ingress.py`、`router.py`、`reconcile.py` 和 `worker.py` 移入对应包。`FlowEngine` 拆成
`JobCoordinator` 和 `WorkerExecutor`，不保留只转发调用的 shim。

静态依赖规则：

- `app.control.*` 不导入 worker runtime、Codex driver 或 Judge adapter；
- `app.worker.*` 不导入 Store、FastAPI、GitHub App、Event Router 或控制面模块；
- 只有控制面更新 flow/job/attempt 并调用 GitHub；
- worker 只返回协议声明的执行结果，不能自行推进领域状态。

## 4. 单 Worker 协议

### 4.1 连接与鉴权

FastAPI 新增 `/internal/workers/connect` WebSocket。worker 主动连接，因此远端执行节点不需要开放入站
端口。首版 serve 配置一个预期的 `worker_id` 和 secret：

- serve：`FLOWTOOL_WORKER_ID`、`FLOWTOOL_WORKER_TOKEN`；
- worker：相同 identity/token，以及 `FLOWTOOL_SERVE_WS_URL`；
- token 在握手阶段用常量时间比较，不写日志或数据库；
- 生产部署要求 `wss://`，只有显式开发配置允许 `ws://`；
- 非预期 identity、错误 token、协议版本不兼容或已有 worker 在线时，连接在派发前关闭。

worker 使用独立 `FLOWTOOL_WORKER_ROOT` 存放 repo/workspace、agent session state 和临时产物。worker
启动后对 root 持有进程生命周期独占锁，防止误启动第二个进程覆盖状态。多 root/多实例留给 Feature 2。

### 4.2 消息与 job kind

`protocol.py` 用 tagged unions 定义消息；每条包含 `protocol_version`、`type` 和关联 id，assignment 还
包含随机 `assignment_token`：

```text
Register -> Registered
Assignment -> Started -> Heartbeat* -> Succeeded|Failed -> ResultAccepted
```

所有 heartbeat/result 必须匹配当前 connection、worker、job 和 token。worker 收到
`ResultAccepted` 后才释放本地任务并接收下一项。

五类 task/result 分工：

- INITIAL：serve 提供 repo/issue/branch/约束；worker 创建 workspace 并运行 Codex，serve 记录 run、
  创建 PR 和更新状态；
- TRIAGE：serve 提供 discussion/context；worker 返回 `TriageVerdict`，serve 决定下一 job；
- REVISION：serve 提供 session id 和反馈；worker resume 并返回 `CodexResult`，serve 负责评论与状态；
- ANSWER：worker 返回 Judge answer，serve 发布 GitHub 评论；
- ARCHIVE：worker 清理 workspace，serve 最后置 `ARCHIVED`。

GitHub App 私钥只在 serve。当 CodeAgent 需要 push 时，serve 可在 task 中提供短时、最小权限的
installation credential；真实 credential/helper 流程仍保持当前“未校准”状态。

### 4.3 断线语义

- 空闲 worker 断线后指数退避重连；
- assignment 尚未 `Started` 就断线时，控制面解除 assignment 并把 job 退回 queued；
- `Started` 后断线或 heartbeat 超时，worker 尽力取消本地 executor，控制面把 job 和 flow 标为
  FAILED；不会自动重跑可能已经产生副作用的任务；
- serve 启动时发现旧版/远程 running job，按同样的 fail-closed 语义收敛并记录原因；
- 旧 connection 的迟到结果因 token 不匹配而被拒绝。

忙碌重连、serve restart 后继续相同 lease 和结果重放全部留给 Feature 3。

## 5. Coordinator、Store 与运行时

`ControlService` 承接当前分散在 Engine/CLI 的触发、取消、合并和重试用例。Event Router 和 admin CLI
不再直接借用执行 Engine。

`JobCoordinator`：

- 从持久状态构造对应 `ExecutionTask`；
- 校验 job kind 与 `ExecutionResult` 类型匹配；
- 只在 assignment token 仍有效时记录 run、更新状态、调用 GitHub/plugin 并入队下一 job；
- 不导入 Codex/Judge/workspace 实现。

Store 在现有手写 schema 上兼容增加：

- `attempts.worker_id`，记录 session/workspace 所在 identity；
- `jobs.assigned_worker_id`、`assignment_token`、`started_at` 和 `last_error`；
- 事务化 assign/start/heartbeat/complete/requeue/fail 方法，所有更新校验 token。

不引入 Alembic/ORM，也不删除历史数据。旧 queued job 视为未分配；旧 running job 启动时 fail closed。

`flowtool serve` 初始化 Store，只启动控制面 tasks 和 WorkerGateway；`flowtool worker` 不读取数据库、
Webhook secret 或 GitHub App 私钥，只装配 client/executor。两边分别处理 SIGINT/SIGTERM 并清理自己的
连接与后台 task。

## 6. 实施顺序

1. **协议与 schema，测试先行**
   - 先写消息版本/类型/token 校验和新增字段/事务 API 测试；
   - 实现 Pydantic protocol 与兼容 schema 变更。
2. **Coordinator / Executor 拆分，测试先行**
   - 用五类 job 的代表场景固定 task 构造和 result 应用；
   - 拆分 Engine，保持现有状态转换与 GitHub/plugin 调用位置。
3. **单连接 Gateway / Client，测试先行**
   - 覆盖鉴权、唯一连接、完整消息序列、heartbeat、未开始 requeue 和已开始 fail closed；
   - 实现 ASGI WebSocket gateway、worker client 和 root lock。
4. **独立运行时与 CLI**
   - `serve` 去掉进程内 worker，新增前台 `flowtool worker`；
   - 拆分 role settings，并迁移现有 Webhook/worker 测试。
5. **端到端与文档**
   - 真实 PostgreSQL + ASGI server + fake executor 验证 queued -> remote execution -> DONE；
   - 更新 CI、README、env example、架构文档/图和本计划实施结果。

## 7. 验收标准

- 只运行 serve 时可接收事件并持久化 queued job，但不会执行；
- 唯一合法 worker 连接后能依次完成五类 task/result 协议；
- worker 包的 import graph 不包含 Store/FastAPI/GitHub，控制包不包含 Codex/Judge；
- worker 不配置数据库或 GitHub App 私钥也能启动；第二个 worker 不能领取任务；
- 未 started 断线会 requeue，started 后断线会 fail closed，迟到结果不能覆盖状态；
- 现有 Webhook、路由、状态机和 admin CLI 行为保持测试覆盖；
- 测试不访问真实 GitHub/Codex/bub，也不依赖真实 heartbeat 等待。

验证命令：

```bash
uv run pytest
uv run ruff check app tests
uv run mypy app tests
uv lock --check
uv build --no-sources
uv run flowtool --help
```

CI 分别启动 serve 和一个 fake-executor worker，确认 `/healthz`、注册、一次远程 job 和优雅关闭，再运行
`git diff --check` 与代码 review gate。

## 8. 风险与回退

- **大边界迁移**：五类 job 同时改走协议；通过 task/result 契约测试和一次完整远程闭环防止只搬文件
  没有搬清职责。
- **断线失败**：Feature 1 对 started job 采取保守失败，短暂网络抖动会降低可用性；Feature 3 才改为
  可恢复 lease，首版不能提前宣称无缝重连。
- **外部副作用窗口**：Codex/GitHub 动作尚非 exactly-once；assignment token 防止过期写库，但不能
  撤销已经发生的外部动作。
- **凭据下发**：短时 token 不得进入日志/结果，真实 push 流程仍需单独校准。
- **后续兼容**：协议从 v1 开始；Feature 2/3 只能向后兼容扩展或显式提升版本。

回退本 PR 可恢复原单进程组合根。schema 只新增可空列，旧代码会忽略它们，不执行破坏性数据迁移。

## 9. 实施结果

- 按计划拆成 `app/control/`、`app/worker/` 和 `app/protocol.py`；旧的平铺 ingress/router/engine/
  worker/reconcile 模块已删除，没有保留转发 shim；
- `flowtool serve` 只装配 Store、Webhook、ControlService、JobCoordinator、GitHub、Gateway 和 Reconciler；
  `flowtool worker` 只装配 Client、Executor、Codex/Judge 和 worker root lock；
- 单 worker v1 WebSocket 协议覆盖五类 job、鉴权/唯一连接、Started/Heartbeat/result ack、assignment token
  fencing、未开始 requeue、已开始 fail closed 和 serve 启动收敛；
- 角色配置已分离，worker 不要求数据库/Webhook/GitHub App 配置；明文 `ws://` 必须显式开发开关；
- PostgreSQL 条件测试包含真实 Store 与 FastAPI WebSocket queued→DONE 联合切片；CI 提供 PostgreSQL，
  并另行启动 serve/worker 双进程验证注册；
- `ruff check app tests`、`mypy app tests`、`uv lock --check`、`uv build --no-sources` 和 CLI help 均通过。

Feature 2 的多 worker/单机多实例和 Feature 3 的忙碌重连/持久 lease 没有提前实现。
