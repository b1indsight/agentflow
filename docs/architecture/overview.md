# 架构总览

## 1. 定位

FlowTool 的**目标形态**是本地优先、headless 的编排器：把带 `flow` 标签的 GitHub issue 运行成
`Codex 编码 → PR → 人工审核 → 回喂修订 → 人工合并 → 归档` 的闭环。产品交互面是 GitHub
issue / PR；Web 控制台已提供单 owner 的看板、详情、执行记录和混合时间线，
支持 cancelled Flow 的本地展示归档，保留取消状态且不修改交付。
实时插话、个人阅读状态和工作流写操作仍待实现，见[路线图](roadmap.md#9-未来方向roadmap)。

M0 已验收基础设施切片；M1–M4 功能及 M5 部署、接收、运维实现已落地，有自动化覆盖，
真实服务闭环与部署仍待验收。当前状态以[架构入口](README.md#当前实现状态)为准。

已定基调：**Python** · **Webhook 驱动的 control plane** · **出站长连接 worker** · **headless** ·
**worker 本地 checkout 跑 Codex** · **PostgreSQL 持久化**。

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
│ 批次、对账进度     │  │ Executor → Codex / repo / VCS │
└────────────────────┘  └──────────────────────────────┘
```

平面边界：

- **Control plane**负责 Webhook、持久化、状态机、Judge 决策和 GitHub API，是唯一数据库权威；
- **Worker execution plane**负责编码 prompt、Codex 与 Flow repo 内的改码/提交/push，以及执行停止、
  journal 重放和历史查询；不持有数据库、GitHub App 或 Judge 凭据；
- control 只确定分支名并在 worker 完成 push 后创建 PR，不直接执行本地版本控制；
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
wire v17 双端协调升级。
执行 Task、durable 工具 Future 和停止清理由 WorkerExecution 长期持有，journal 由 ExecutionState 保存。
服务自行创建所需仓储；Webhook 每条 receipt 在事务内创建 ControlService/EventRouter，
事务外执行提交后通知。聚合 Store 只用于 CLI 运维、live smoke 和测试。
依据：[运行时装配](../../app/controler/runtime.py)、[事件消费](../../app/controler/events/webhooks.py)、
[数据库上下文](../../app/persistence/database.py)、[Store 用途](../../app/persistence/store.py)。

GitHub、Judge、push capability 和结果回执恢复已有实现与自动化；真实 GitHub/Codex/bub
闭环、生产部署和恢复演练仍需单独验收，不能从图中推断它们已经端到端可用。
