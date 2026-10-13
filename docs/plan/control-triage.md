# 将 triage 执行迁到 control — Feature record

**Stage:** Implemented，triage 已改为反馈批次前置判断，826 项回归通过。
**PR:** [#60](https://github.com/b1indsight/agentflow/pull/60)
**Baseline:** `main` / `b88a93b2`（PR #59）
**Bookmark:** `refactor/control-triage`

## 1. Initial plan

### 背景、目标与范围

当前 control 已负责冻结审核上下文及应用 Judge 决策，却通过 WebSocket 把
`TriageTask` 派给绑定 worker。worker 的 `BubJudge` 仅消费上下文调用模型，
不需要本地仓库或 Codex 会话。用户要求把 triage 迁到 controller，即本项目的 `app/control/`。

本次将 triage 的领取、执行、恢复和 Judge 配置归入 control；worker 仅执行
initial、revision、archive 及原有对话查询。保留四类判定、置信度门槛、冻结反馈版本、
答复副作用和授权 request-changes 直接修订的现有语义。

依据：[现有准备逻辑](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/coordinator.py)、[结果应用](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/results.py)、
[worker 执行器](../../app/worker/executor.py)、[Judge](../../app/worker/judge.py)。
这是对已完成 [M2 semantic triage](m2-semantic-triage.md) 中“worker 执行 Judge”决策的后续变更；
原计划作为历史保留。本 PR 不更改模型供应商或判定 prompt，不拆分全部 Python 安装依赖，
不新增多 control 实例高可用或跨 worker 会话迁移。

### 预期行为与验收标准

- 普通审核反馈 settle 后产生持久 triage job，由 control 调用 Judge。
- 对仍有效的 ACTIVE Flow，即使绑定 worker 暂时离线或不接受新任务，triage 仍可处理；
  既有 Flow 终态及恢复期限规则仍生效，不让已失败/取消/合并的 Flow 被重新推进。
- revise 只派生一个 revision job，等待原 worker 可用；answer/wait/ignore 不向 worker 派任务。
- triage 不设置 worker busy/current_job，不解除 Flow 已有的仓库绑定，也不使用虚构 worker 身份。
- control 重启后恢复未完成 triage；已经持久化的决定只恢复结果应用，不重新调用模型。
- worker 启动、执行和配置不再依赖 Judge 或 bub；Judge API key 仅配置在 control。

### 技术方案

1. **适配器与配置归属。** 将 `worker/judge.py`、`worker/judge_runner.py` 移到 control，
   保留 domain 的 Judge 接口及决策模型。由 `ControlRuntime` 构造适配器，
   `ServeSettings` 接收现有 `FLOWTOOL_JUDGE_*` 配置，删除 WorkerSettings 中对应字段。
   保留隔离子进程、最小环境、无工具调用、超时与失败转 wait 的契约；不得把 control 的
   DB/GitHub 凭据传给 Judge 子进程。worker 的 Codex 环境继续排除 Judge 密钥。
2. **本地 triage 调度。** 在 control 中增加专用 triage 运行循环，由 runtime 管理启动和退出。
   首版每个 control 进程串行处理 triage，独立于 WebSocket、webhook 和其他恢复循环；
   不新增通用执行框架或可配置并发池。持久 job 继续是待办事实，内存通知仅加速唤醒。
3. **领取与恢复。** 为 triage 增加受事务保护的领取/完成操作，worker 的全部候选查询排除 TRIAGE。
   保留既有锁顺序；需要 workflow 锁时继续遵守 worker→Flow→job 顺序，但不占用远程执行槽。
   每次 control 领取使用可校验的执行 token；冻结上下文和写入结果时重新检查 token、job、
   attempt、批次和 Flow 是否仍有效。网络调用在事务外执行。
   单 control 的启动恢复只重排没有持久结果的本地 triage，轮换 token 拒绝迟到写入；
   worker lease sweeper 不处理 control triage。快照读取沿用有界准备重试，正常 shutdown
   取消并回收 Judge 子进程。持久结果交给共享恢复路径。
4. **上下文与结果。** 从远程 assignment prepare 中提取 triage 准备入口，复用上下文构建、
   冻结及批次校验。复用结果计划、GitHub effect ledger 和原子派生逻辑，避免复制四类判定实现。
   将 control 本地 triage 结果与 WebSocket 消息契约分开；结果 inbox 可按本地/远程终态类型
   解析，但不为了复用而伪造 worker token 或 connection epoch。完成本地结果不改写 worker 槽。
5. **协议及交付。** 从远程 ExecutionTask/ExecutionResult 移除 triage，提升 worker 协议版本。
   调整相关 fixtures、fake executor、边界检查、配置测试及运维状态展示；更新架构文档、
   直接部署、Docker/Compose、Kubernetes 环境示例和 CHANGELOG。

### 方案比较与取舍

- 仅移动 Judge 文件、仍占用远程 assignment：改动较小，但 triage 继续依赖 worker 在线，
  配置与恢复责任仍不一致，不采用。
- 在 webhook/settle 请求内直接调用 Judge：缺少独立恢复入口，慢模型会阻塞接入，不采用。
- 保留持久 job、增加 control 专用执行入口：复用已有批次和副作用处理，同时明确执行归属，采用。
- 独立 Judge 服务或通用多执行器调度框架：当前无具体需求，后置。

### 风险、兼容性与部署

- 协议/持久结果发生不兼容变化：升级前排空旧 triage assignment、结果 inbox 和 worker journal，
  协调升级 serve/worker；不提供旧协议双栈或历史结果转换。若部署存在旧未完成数据，先按旧版
  完成或显式处置，再升级；不自动删除未知执行记录。回滚同样要求排空新版本在途任务。
- 模型返回后、结果持久化前崩溃可能再次调用模型；允许重复计算，不承诺 provider exactly-once。
  一旦结果持久化，重启只恢复应用；派生 revision 和 GitHub effect 沿用幂等保护。
- 合并、关闭、取消、重开和新批次可能与模型调用并发；应用前重新检查有效性，禁止迟到决定
  恢复终态 Flow、重复消费反馈或创建过期 revision。
- 迁移配置与运行依赖，不把“worker 不再调用 bub”表述为“worker 镜像不含 bub”。当前项目和
  镜像共用依赖安装；拆分包/镜像依赖后置，项目仍需 bub 为 control 提供 Judge。

### 实施顺序

1. 先补最小行为回归：worker 离线的 triage、远程调度排除、重启恢复和迟到结果。
2. 实现 control 领取、准备、执行、结果持久化和恢复，复用现有决策应用。
3. 迁移适配器/配置，移除 worker triage 路径并升级协议，调整现有测试。
4. 更新部署和架构文档，运行验证、消融及代码评审，在本 bookmark/PR 完成实现。

### 验证与消融

- 代表性四类判定保持原语义，Judge 配置缺失、超时、非法输出仍安全等待。
- PostgreSQL 回归覆盖：离线 worker 仍可 triage、唯一领取、准备重试、重启恢复、
  已持久结果不重复调用模型、ANSWER effect 恢复、唯一 revision 派生、取消/合并竞争。
- 边界检查验证 worker 不导入 Judge/bub，control 不导入 worker；适配器子进程不继承 control 密钥。
- 运行 `uv run ruff check app tests`、`uv run mypy app tests`、PostgreSQL 全量 pytest、
  `uv build --no-sources`，以及受影响的部署静态检查。
- 实现后、代码评审前分别移除远程候选排除、迟到结果 token 校验、持久结果恢复优先三个机制，
  确认对应最小回归能失败；恢复代码并复验，将结果追加到时间线。不会用重复实现型测试充数。

### 待定与后置

批准前无额外业务问题。具体存储方法和本地终态模型按现有契约选择最小改动，实施时记录决定。
多实例 control、Judge 并发扩容、依赖打包拆分和真实 provider smoke 不作为本次验收前置。

## 2. PR change timeline

### 2026-10-08 · 1. 优先复用 control 现有能力

**需求状态：** 用户明确要求“能利用 controller 现有功能的尽量复用”。本条记录已确认的
设计约束及据此提出的方案修订；整体计划尚待批准，产品代码和测试未开始修改。

**背景：** control 已有 review settle、上下文冻结、结果 inbox、分页恢复、结果计划及
GitHub effect ledger。新增另一套 triage 队列、结果存储或恢复器会重复这些职责。

**方案修订：** 本条细化初始技术方案第 2–4 项；其中“专用运行循环”和“领取/完成操作”
不再表示预先承诺新建独立调度器或存储 API。先接入已有生命周期和调度入口，只有模型调用
与快速 settle 必须隔离的部分才保留最小异步执行任务，不另建调度框架、通知系统或恢复轮询。

| 能力 | 优先复用的位置 | 需要的最小适配 |
| --- | --- | --- |
| 反馈 settle 与持久任务 | `ControlRuntime._settle_reviews`、`ReviewStore.claim_due` | 沿用已有 batch/job 创建；把 TRIAGE 引向本地执行，不增加第二份待办队列 |
| 上下文准备与重试 | `JobCoordinator`、review context builder、批次冻结逻辑 | 从远程 assignment 中分离 worker 无关部分，保留同一套快照、有效性校验和准备重试规则 |
| 本地结果持久化 | `Store.record_control_result`、现有 jobs result inbox | 复用存储结构与写入能力，补足执行 token 校验；不新建 triage 结果表 |
| 四类判定与副作用 | `ResultApplication`、`ResultStore`、effect ledger | 共享决定应用、反馈消费、revision 派生和幂等 GitHub 操作，不复制分支实现 |
| 结果恢复 | `ControlRuntime._sweep_results`、gateway 现有分页及逐 job 恢复 | 将 worker 无关恢复职责提取到 control 的共享组件或扩展原入口，保留同一轮询、分页和互斥机制 |
| 任务完成 | `Store.complete_persisted_result` | 复用 job 完成逻辑；仅远程结果更新 worker 槽和 binding deadline，本地结果不得改变这些状态 |
| 生命周期与配置 | `ControlRuntime.startup/shutdown`、`ServeSettings` | 纳入现有启动、取消和配置机制，迁移 Judge 配置 |

**边界与取舍：** 复用数据库操作不等于跳过身份检查；现有 `record_control_result` 仍需
调用侧锁和领取身份保护。现有 `complete_persisted_result` 会更新 Flow binding 的恢复期限，
不能直接用于本地 triage 而不做分支隔离。禁止为了复用远程协议而伪造 worker 身份。
只有独立的本地领取与失效 token 语义确实无法由现有方法表达时，才增加最小专用方法。

**验收补充：** 代码评审逐项核对上述复用关系；每个新增组件须说明现有能力不足在哪里。
增加代表性回归，证明本地 triage 完成及恢复不会改写原 worker 的槽状态或 Flow binding
恢复期限，并证明本地与远程结果均通过同一套结果应用和副作用恢复路径。

依据：[runtime](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/runtime.py)、[coordinator](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/coordinator.py)、
[现有恢复](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/gateway.py)、[结果存储与完成](../../app/persistence/store.py)、
[结果应用](https://github.com/b1indsight/agentflow/blob/a50ac2c0615bea1f8c8e42b23b3307deaa1e4ba8/app/control/results.py)。


### 2026-10-08 · 2. 方案批准与实现

用户在复用约束更新后明确要求“实现吧”（原消息“实现把”），批准初始计划及第 1 条修订。
已开始测试优先实施：先运行新增 control triage 回归，得到缺少新入口的预期失败。

实现选择：复用 review sweep 启动单个异步 triage 任务，不增加轮询；复用 JobCoordinator
准备重试、Store 的 control result inbox、ResultApplication 和现有分页结果恢复。
本地终态契约放入 `contracts/control.py`，远程协议移除 triage 并升至 v12。
Store 仅新增本地领取/启动恢复及 token 校验；现有完成函数按是否存在远程 owner 隔离
worker 槽和 binding deadline 更新。Judge 子进程与配置迁到 control，共用安装依赖保持现状。
旧远程恢复测试以真实编码任务代替 triage 占位，保留原 journal/停止/归档恢复覆盖。
本地模型任务只保存决定，结果应用完全交给既有 result sweep，从而复用其逐 job 互斥和恢复。
在无本地执行器运行时复用同一恢复操作重排未保存结果的本地任务，覆盖启动恢复，以及数据库
短时故障导致写回失败但 control 进程仍存活的情形；已保存结果保持 RUNNING 等待共享结果恢复。
慢 Judge 不阻塞 settle，且同一 control 同时只运行一个本地 Judge。

### 2026-10-08 · 3. 消融验证

使用 `.cache/2026-10-08/triage-ablation.py`，每项仅临时移除一个机制，运行固定回归后
在 finally 中恢复原文件。三项均得到预期失败，恢复后 `tests/test_control_triage.py` 8 项通过。

| 移除机制 | 对应回归 | 结果与决定 |
| --- | --- | --- |
| 远程 assignment 候选排除 triage | `test_worker_cannot_claim_triage` | 1 failed，worker 错误领取本地任务；保留 |
| 本地领取 token 比对 | `test_reclaimed_triage_rejects_late_decision` | 1 failed，旧模型调用成功写回；保留 |
| 未保存结果才允许重排 | `test_triage_without_worker_and_persisted_recovery` | 1 failed，已保存决定被重新领取；保留 |

另补无活跃本地执行器时的恢复回归，覆盖数据库故障后的同进程恢复。

### 2026-10-08 · 4. 最终验证

- `FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run pytest -q --tb=short`：
  **845 passed**，1 条既有 Starlette/httpx 弃用警告。覆盖 PostgreSQL 事务、远程恢复、PR 生命周期、
  本地 triage 恢复、协议拒绝和配置/导入边界。
- `uv run ruff check app tests`、`uv run mypy app tests`：通过（159 个源文件）。
- `uv build --no-sources`：sdist、wheel 构建通过。
- Docker env 示例静态检查：Judge 变量只位于 serve 示例，且与 ServeSettings/WorkerSettings 字段归属一致。
- 真实模型 provider、Docker/Kubernetes 集群运行未执行，本次不宣称这些外部环境已验收。
- 代码原则核对：未引入新队列、结果表或恢复轮询；复用冻结上下文、准备重试、结果计划与 effect ledger。
  保留三项经消融证明有效的保护；新增回归针对实际状态隔离和恢复风险。

同一 bookmark/PR 接下来经独立只读代码审查门禁推送；审查报告保存在仓库 `.git/jj-reviews/`。



### 2026-10-08 · 5. 按用户要求精简实现

用户明确要求“先简化当前实现”，批准在本 PR 上调整结构，优先复用已有能力。
删除独立 ControlTriage 执行器，将本地编排并入 JobCoordinator；删除内部 TriageTask，
直接返回已冻结上下文的 ReviewBatch。批次进入 PROCESSING 复用准备事务，删除本地
started_at 镜像及单独启动方法；远程 worker 的 started/lease 协议保持不变。
本地/远程结果仍分别校验，持久 inbox 改用单个判别联合解析器，避免手写多层解析。
合并离线恢复、worker 状态隔离及结果重放的重复准备，并共用 runtime fixture；保留迟到结果
和慢 Judge 并发场景。测试先调整到 coordinator 入口，旧实现出现 4 项预期失败。
本条替代第 2 条中的独立执行器与内部任务封装选择；安全边界和四类决定语义继续保留。
验证：`uv run ruff check app tests`、`uv run mypy app tests`（158 个源文件）、
`uv build --no-sources` 均通过；完整 PostgreSQL 回归 **841 passed**，保留原 Starlette 弃用警告。
相对上一版 `a859ecb8`：业务代码净减少 **67 行**，测试净减少 **73 行**；合并后用例数减少 4 项。
三项保护用相同的单项移除方式复验：远程候选排除、领取 token、已保存结果保护均触发预期失败；
恢复后精简的 5 个 control triage 场景全部通过。移除独立执行器、内部任务模型和重复启动状态
未造成全量行为回归，采用当前更小结构；不再为这些已删除结构保留适配层。


### 6. 2026-10-08 — 取消独立 triage job（已批准，实施中）

用户明确指出 triage 是向 worker 下发请求前的流程，确认删除独立 job 领取、token 校验与中断恢复。
此决定取代第 4、5 项中的本地 job/inbox 设计；此前针对这些机制的消融只证明旧模型内部约束，
不构成继续保留该模型的理由。

ReviewBatch 的 ready 状态且 job_id 为空，直接表示待判断反馈；control 读取批次并调用 Judge。
REVISE 在同一个工作流事务中创建 revision job 并关联批次，ANSWER/IGNORE/WAIT 直接处理批次。
删除 JobKind.TRIAGE、ControlSucceeded/ControlFailed/TriageResult、领取/恢复方法、本地准备分支、
结果派生模型和 triage job 的结果处理。保留原 worker assignment 路径与 JobCoordinator。
不再发送独立的“按此修改中”评论，实际 revision 仍保留交付通知。

只保留现有批次/attempt/PR 有效性检查与原子创建 revision；尚未完成的批次由下一轮重新读取。
冻结输入和已保存答案仍放在批次中，回复通过 batch marker 查找已有评论后完成精确版本消费；批次只保存一个 answer_dispatched 标记，
用于保留既有“不重发结果未知的评论”保证，明确拒绝时清除标记。此标记不参与任务领取或调度。
Provider/上下文读取中断不改变 Flow/worker 绑定，因此不再触发上一轮审查的归档绑定释放路径。

测试优先：`test_only_revision_enters_job_queue` 在旧实现上预期失败（jobs 中已有 triage）。
调整既有用例到批次接口，删除本地 job 租约/领取/retry 用例；结果账本与远程恢复测试使用实际 worker 任务。
验证：完整 PostgreSQL 回归 **826 passed**（56.03s，1 条既有 Starlette 弃用警告），
`uv run ruff check app tests`、`uv run mypy app tests`（157 个源文件）、`uv build --no-sources` 通过。
完整记录：`.cache/2026-10-08/triage-batch-final-tests.log`。

消融脚本 `.cache/2026-10-08/triage-batch-ablation.py` 对同一实现逐项移除保护并恢复源码：

| 移除机制 | 代表用例 | 结果 |
|---|---|---|
| 批次有效性检查 | 重复处理、取消/合并后的迟到决定 | 预期失败，不能删除 |
| 答案已发送标记 | POST 结果未知且未查到 marker | 预期失败，会重复 POST |
| PR 基线核对 | Judge 期间 head 改变 | 预期失败，会发送过期答案 |

恢复后批次/Judge/回复相关 **50 项通过**；再运行上述全量回归。
相对前一版 `74a4fac5`，业务代码 +193/-358（净减 165 行），测试 +275/-681（净减 406 行）。
代码原则核对：删除任务概念和特殊分支，保留批次状态、现有工作流事务及一个评论发送标记；
未新增队列、轮询或本地终态协议。归档绑定回归验证 Judge 中断→重试 IGNORE→合并仍由原 worker 领取 archive。

