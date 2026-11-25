# 组件职责

## 包依赖边界

`agentflow serve` 和 `agentflow worker` 是两种进程。前者管理数据库和 GitHub App；后者主动连接 serve，在自己的机器上运行编码工具。[ServerRuntime](../../app/controler/runtime.py) 与 [WorkerRuntime](../../app/worker/runtime.py) 分别装配两端；本地 bub 是仅能接推理任务的可选 worker，不是编码执行器。

| 模块 | 负责 | 边界上交换什么 |
|---|---|---|
| [domain](../../app/domain) | Flow、Attempt、Job、交付和反馈的领域类型；状态转换、恢复与推理选择规则 | 输入当前事实，返回决定；领域策略不读数据库或执行网络请求 |
| [contracts](../../app/contracts) | 在线协议、冻结的任务和结果、会话与文件数据结构 | 注册和 assignment、工具调用与回复、终态与接纳；在线协议和磁盘格式分别版本化 |
| [communication](../../app/communication) | 两端共用的连接生命周期、消息队列、按方法分发及回复关联 | 路由到该连接登记的回调；传输 ACK 不表示 Job 结果已应用，也不授权释放位置 |
| [controler](../../app/controler) | 组合仓储、GitHub 资源、工作流与推理；唯一能提交业务状态的一侧 | 接受事件和用户命令，下发任务，核验结果，触发 GitHub 操作 |
| [worker](../../app/worker) | 管理本机位置、进程、编码会话和恢复证据 | 上报库存、执行及停止回执、历史或文件内容；不连接业务数据库、不持有 App 私钥 |
| [github](../../app/github) / [persistence](../../app/persistence) | 前者拥有 installation 绑定的远端资源及操作；后者拥有 PostgreSQL 事务、表和查询 | 向控制服务提供远端观察/回执与本地持久事实，两者没有共同提交事务 |

依赖方向由[边界测试](../../tests/test_boundaries.py)检查。产品功能的触发条件和可见结果见[端到端走查](workflows.md)；以下按模块说明谁负责兑现这些结果。

## 入口：GitHub 与控制台怎样进入同一工作流？

- **Webhook。** [HTTP 入口](../../app/controler/api.py)校验签名并在返回 202 前持久化收件；[消费者](../../app/controler/events/webhooks.py)随后调用 [EventRouter](../../app/controler/events/router.py)。触发标签经过 allowlist 检查；评审进入版本化反馈缓冲，PR 合并交给生命周期处理。重复投递用 delivery ID 去重。PR/反馈对账只补已追踪对象。
- **控制台。** [Web 客户端](../../app/controler/web/README.md)通过[单 owner API](../../app/controler/console/api.py)选仓库、建任务、加图片或指令、重试、归档和重新开工。服务验证仓库授权、同源写请求和当前 Attempt；浏览器展示的按钮不能替代服务端检查。任务来自 Issue 或持久化正文，不要求两者同时存在。
- **读取。** 看板/详情读数据库投影；活动时间线读本地归档。会话读取经 [conversation broker](../../app/controler/workers/conversation.py) 访问原 worker；已登记文件经[文件服务](../../app/controler/workers/files.py)按归属读取。控制台不能把任意本地路径当下载地址。接口和不可用状态见[会话查询](conversation-api.md)。

## 工作流：命令、任务准备和交付各自何时生效？

| 所有者 | 输入 → 输出 | 一项容易混淆的约束 |
|---|---|---|
| [ControlService](../../app/controler/workflow/service.py) | 创建、追加、重试、收尾或 resume → 本地事务与待派发 Job | 追加的指令可能只排队：待输入的首轮用 INITIAL 续跑，已有 PR 的工作走 REVISION。`retry` 不新建 Attempt；`resume` 核对预期 Attempt、Issue 冲突、归档证据和旧绑定后才重新开工。 |
| [JobCoordinator](../../app/controler/workflow/coordinator.py) | 已领取的 Job → 冻结任务、所选模型参数和本次执行所需凭据 | 首轮用命名结果确定交付分支；复用当前 Attempt 已有交付。准备完成还需检查归属并授权下发，不能因为读到了 GitHub 数据就允许执行。 |
| [review](../../app/controler/review) / [推理调度](../../app/controler/workflow/inference.py) | 冻结反馈、PR 身份、相关线程 → Judge 决定 | `answer` 和 `revise` 都在原工作区执行；前者只读，后者验证实际远端基线。`wait` 保留问题，`ignore` 精确消费版本。Judge 不代写最终答复。见[审核的并发与重试语义](review-loop.md)。 |
| [Flow 工具服务](../../app/controler/workflow/tools.py) / [结果应用](../../app/controler/workflow/results.py) | 工具意图、worker 终态及 VCS 事实 → 冻结结果计划与交付回执 | `flow.open_pr` 的 prepared 回执不等于 GitHub 已创建 PR。另一种有效首轮结果是等待人工输入，不产生 PR。 |
| [ResultEffects](../../app/controler/workflow/effects.py) / [生命周期处理](../../app/controler/workflow/lifecycle.py) | 结果计划 → GitHub 副作用回执与本地状态 | 结果持久接收与结果应用分开；已发送但未确认的请求先核对远端证据，不盲目重发。当前 PR 合并先归档，原 worker 停止/释放和 GitHub 通知可稍后完成。 |

## 通信与调度：断线后哪些东西还在？

[ServerCallbacks](../../app/controler/workers/callbacks.py) 管理注册、派发、lease、结果接纳和停止握手。[Gateway](../../app/communication/gateway.py) 按连接登记 Session，[Dispatcher](../../app/communication/dispatch.py) 匹配方法和回复。新连接有新的 epoch；同一 worker 身份不意味着旧回复可完成新任务。临时历史查询随连接结束；已授权的执行不能因 socket 关闭就判为未运行。

[工作区库存](../../app/worker/execution/workspaces.py)按仓库和容量提供具体位置。[领取事务](../../app/persistence/assignments.py)把编码任务绑定到 worker、workspace 和 generation，不同位置可并行。默认无仓库位置的推理不占用 Flow 编码绑定；满足条件时优先选本地 bub，否则可选支持推理的普通 worker。推理失败按[领域策略](../../app/domain/inference_policy.py)结算，不另起一套调度器。

[执行生命周期](../../app/worker/execution/lifecycle.py)管理任务和停止；[ExecutionState](../../app/worker/state/execution.py)按 Flow 保存 assignment、执行阶段、终态及停止证据。control 可重放已接收的结果，worker 可重传已落盘的终态。缺少充分证据时保留绑定，不能通过 lease 到期或删除 journal 强行腾出工作区。具体恢复操作见[取消、drain 与停机](../operations/cancel-drain.md)。

## 编码后端、模型与 skill

[WorkerExecutor](../../app/worker/execution/executor.py)组装任务输入、启动或恢复[编码后端](../../app/worker/coding_agent.py)，并核验结构化结果。后端可选择 [Codex](../../app/worker/execution/codex.py) 或 [Amp](../../app/worker/execution/amp_agent.py)；CLI 事件是执行历史，终态 JSON 是交付契约。两者由不同接口读取。新会话使用本次 assignment 捕获的模型和推理强度；恢复会话不套用后来修改的全局默认值。

首轮的[本地 MCP](../../app/worker/transport/mcp.py)提供当前仓库内的搜索，以及开 PR、等待输入两个互斥的完成动作。修订不会复用首轮 MCP 凭据。[VCS 模块](../../app/worker/execution/vcs.py)从实际 checkout 和远端验证 Git/jj 交付；模型自报成功不够。编码依赖可信 worker 环境，路径和 TCP 权限模型的限制见[安全决策](decisions.md)。

owner 可通过设置指定 skill 仓库。worker 使用独立只读授权获取快照；[工作区 skill 安装](../../app/worker/execution/skills.py)先链接常驻项，再按任务选择可选项。更新配置仓库的默认分支会刷新快照，不更换已有 Flow 链接。模型目录来自就绪 worker 的[原生探测](../../app/controler/workers/catalog.py)，可列出不代表该账号有权调用。Amp 一次性推理结束时归档其 thread；编码 thread 保留到所属 Attempt 结束，归档不删除历史。

## 4.7 Store（PostgreSQL）

[Database](../../app/persistence/database.py)拥有连接池、时钟、事务及共用锁。业务服务在同一事务上下文中组合具体仓储；聚合 `Store` 用于 CLI、live smoke 和测试。表的定义与 v3 增量在 [schema](../../app/persistence/schema.py)，升级与回退条件见[运维说明](../operations/upgrade.md)。

| 需要解释的事实 | 查询谁 | 不能从中推断什么 |
|---|---|---|
| Flow 当前阶段、归档结论 | `flows` 与[Flow 事实账本](flow-ledger.md) | 归档不代表资源已回收；重放不重建其他业务表 |
| 一次开工交付了哪张 PR | `attempts.delivery_*`、`flow_tool_calls`、结果计划与副作用回执 | 旧 PR 的观察不能修改新 Attempt 的交付 |
| 谁能继续执行 | `flow_executions`、`workers`、`jobs`、`execution_stops` 与 worker journal | 在线、空闲或超时都不能单独证明旧执行已停止 |
| 哪条反馈已经处理 | `review_versions`、`review_batches` 与已确认的交付结果 | 反馈已转达、版本已消费和 GitHub 线程已 resolve 是不同事实 |
| 控制台看到了什么 | `console_flows` 与 `console_activity`；[追加指令记录](../../app/persistence/agent_inputs.py)另有待交付标记 | 本地活动快照不等于实时 GitHub 状态或完整原生会话 |

## 对账、运维和扩展

[对账循环](../../app/controler/reconciliation)分别处理已追踪 PR 的关闭/合并/重开、反馈线程和可选 Projects 投影。它们持久保留各自进度和退避。Projects 根据当前可写 Issue owner 的控制台阶段更新受管字段；写看板失败不改变 Flow。[GitHub App 接入](github-app.md)说明权限和人工合并边界。

[生命周期插件](../../app/plugins/hookspecs.py)观察工作流事件；插件回调不是可靠消息队列，不能作为数据库提交或外部交付回执。服务探针、结构化日志、指标、备份与恢复入口见[运维手册](../operations/runbook.md)。
