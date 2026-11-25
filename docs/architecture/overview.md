# 架构总览

## 1. 定位

AgentFlow 在 GitHub 与单 owner Web 控制台接收任务。给 Issue 加 `flow` 标签，或在控制台选择仓库并提交任务，都会创建 Flow 及首次 Attempt。worker 运行 Codex 或 Amp；首轮可以交付 PR，也可以暂停等待下一条指令。交付后的审核、修订和人工合并见[端到端流程](workflows.md)。

控制台可传入图片、追加任务、重试失败任务、归档及重新开工。追加指令持久排队，在任务边界交给 Agent；这不保证正在执行的 CLI 立即收到输入。可用操作取决于当前 Attempt 和交付状态，见[控制台 API](../../app/controler/console/api.py)。

M0 已验收基础设施切片；M1–M4 功能及 M5 部署、接收、运维实现已落地，有自动化覆盖，
真实服务闭环与部署仍待验收。当前状态以[架构入口](README.md#当前实现状态)为准。

已定基调：**Python** · **Webhook 驱动的 control plane** · **出站长连接 worker** · **headless** ·
**worker 本地 checkout 跑 Codex 或 Amp** · **PostgreSQL 持久化**。

## 2. 运行形态

目标部署要求 Webhook 能到达 serve；持有本地 checkout 的 worker 只需主动访问 serve：

- 本地开发可通过可信隧道把 GitHub Webhook 转发到 `localhost`；
- 长期运行时，serve 部署在有受控公网入口的控制节点，worker 可部署在独立执行节点；
- 给 issue 打标签会触发本机执行模型生成的命令，因此 allowlist、沙盒、短时凭据和网络出口都是
  前置安全边界，而不是后期增强。

当前验证了 serve/双 worker 多进程启动与重启重连、认证 WebSocket 注册、租约恢复和 fake executor 的远程 job；公网入口、
真实 Webhook 投递和生产部署尚未验证。

## 3. 组件总览

```diagram
┌──────────────────────────────────────────────────────┐
│ GitHub：Issue / PR / Review / Projects                │
└──────────────┬───────────────────▲───────────────────┘
               │ Webhook           │ REST / GraphQL
               ▼                   │
┌────────────────── serve / control ───────────────────┐
│ API → 持久 Inbox → Consumer → EventRouter            │
│                            → ControlService          │
│ Coordinator → 本地 Judge / Worker 任务准备            │
│ ResultApplication → ResultEffects → GitHub           │
│ 独立循环：租约 / 结果恢复 / 反馈合批 / PR与反馈对账    │
│           Projects 同步 / 指标采集                    │
│ 各服务自建仓储，共享 Database                        │
│ ServerCallbacks / Broker + Gateway / Session × worker│
└──────────────┬───────────────────┬───────────────────┘
               │                   │ 认证 WebSocket
               ▼                   ▼
┌────────────────────┐  ┌──────────────────────────────┐
│ PostgreSQL         │  │ Runtime + Gateway + Session  │
│ 状态、结果、回执   │  │ journal / 历史查询 / 停止握手 │
│ 批次、对账进度     │  │ Executor → Codex 或 Amp       │
└────────────────────┘  └──────────────────────────────┘
```

平面边界：

- **Control plane**负责 Webhook、持久化、状态机、Judge 决策和 GitHub API，是唯一数据库权威；
- **Worker execution plane**负责编码 prompt、调用编码后端在分配的 workspace 改码/提交/push，以及执行停止、
  journal 重放和历史查询；不持有数据库、GitHub App 或 Judge 凭据；
- 首轮编码由 control 指定交付分支，worker 回传执行结果。申请开 PR 时，worker 核验提交、工作区和远端分支，control 核对 GitHub ref 并负责创建或复用 PR。请求人工输入时，control 保存待办消息，保留会话和工作区绑定，不创建 PR。两条路径的可见结果见[端到端流程](workflows.md)；
- 本地能力按 [Flow repo / execution policy / VCS](components.md) 分离（§4.6）：Agent 选择并操作
  Git/jj，worker 只验证事实。

Runtime 创建并关闭共享 Database，装配共享的结果应用、effects、工具和 conversation broker。
两端 Runtime 持有相同的 Gateway / Session：server 管多 worker，worker 至多登记一个 server。
Gateway 驱动统一的 Session 事件入口；Dispatcher 按 `package.method` 精确路由请求，普通回复按
`reply_to` 找原调用，经过可选 `on_reply` 再交付。所有 payload 与拒收均走 FIFO，无 history 旁路。
server 每连接挂载 ServerCallbacks；worker 平级挂载 WorkerStateCallbacks / WorkerBusinessCallbacks。
状态类管理连接和执行状态协议；业务类注册 worker.history，通过 before_wait 管理工具观察，
通过 Dispatcher 接收回复与已授权的拒收。两类互不持有或调用，在同一 driver 串行处理。
Broker 持有查询 Future 与限额，从 Registration 取得配对的 Session/Dispatcher；慢业务回调与 history 互相等待。
server 接入函数只负责认证与首条 message，WorkerRuntime 直接负责主动拨号、重连与连接任务回收；
在线版本由[协议契约](../../app/contracts/protocol.py)定义，serve 与 worker 必须匹配。
执行 Task、durable 工具 Future 和停止清理由 WorkerExecution 长期持有，journal 由 ExecutionState 保存。
服务自行创建所需仓储；Webhook 每条 receipt 在事务内创建 ControlService/EventRouter，
事务外执行提交后通知。聚合 Store 只用于 CLI 运维、live smoke 和测试。
依据：[运行时装配](../../app/controler/runtime.py)、[事件消费](../../app/controler/events/webhooks.py)、
[数据库上下文](../../app/persistence/database.py)、[Store 用途](../../app/persistence/store.py)。

GitHub、Judge、push capability 和结果回执恢复已有实现与自动化；真实 GitHub/Codex/bub
闭环、生产部署和恢复演练仍需单独验收，不能从图中推断它们已经端到端可用。

## 4. 数据架构

PostgreSQL 是唯一运行时数据库，由 control 独占。表按职责分为五类：

```diagram
┌──────────────────── 业务事务（control） ────────────────────┐
│                                                             │
│  ② 追加 Flow 事实            ① 同事务更新状态表              │
│  ┌───────────────────┐       ┌──────────────┐               │
│  │ console_activity  │ ────▶ │ flows        │ ◀── 调度、对账 │
│  │ source='flow' 的  │ 投影  │ 六态、标题、 │     API、CLI  │
│  │ 事实行（只追加）  │       │ 归档结论     │     读取      │
│  └─────────┬─────────┘       └──────────────┘               │
│            │ 不复制其他表                                    │
│            ▼                                                │
│  ┌──────────────────────────────────────────────┐           │
│  │ 业务状态与专用证据：jobs、flow_tool_calls、   │           │
│  │ attempts、review_*、result_*、events 等       │           │
│  └──────────────────────────────────────────────┘           │
│  ┌──────────────────────────────────────────────┐           │
│  │ 协调数据：workers、租约与 claim、对账游标、   │           │
│  │ 退避时间（不是业务事实，不进入账本）          │           │
│  └──────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────┘
                 │ 触发器刷新
                 ▼
  展示投影与采集归档：console_flows、console_activity 的展示记录
```

- **Flow 事实账本**是 Flow 状态的唯一可信来源：每次改变 `flows` 的业务列，都同事务追加一条以事件
  命名的事实。重放一个 Flow 的事实可以得到与 `flows` 当前行一致的结果。
- **`flows` 状态表**是账本的同事务投影。业务判断、行锁和唯一约束继续使用它，读路径不变。
- **其他表**保持各自的权威，不由账本重建。
- **协调数据**记录谁占用什么、处理到哪里，不进入账本。

`agentflow flow verify` 重放比较账本列；迁移后只读的旧 Flow 跳过，启动不补写历史证据。
完整分类、事实目录和写入规则见 [Flow 事实账本与状态表](flow-ledger.md)。
