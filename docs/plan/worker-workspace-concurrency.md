# Worker 按 workspace 并行执行

**状态：** 并发派发、按需求补充库存、独立 journal 与 ACK 恢复已在本地实现并通过全量回归。
**交付：** 按用户要求通过代码审查门禁后交付独立 PR，发布与 CI 状态见本线程；未部署，只操作临时测试数据库。
**需求来源：** [本线程](https://ampcode.com/threads/T-01a1bbca-99b5-70d6-a109-76d3a414c93e)。本文只保留当前方案。

## 1. 行为要求

- 同一时刻，一个 Flow 最多绑定一个 workspace，一个 workspace 最多绑定一个 Flow。
- 一个 worker 可以绑定多个 Flow，同时执行多个编码 job，但这些 job 必须位于不同 workspace。同一 Flow、同一 workspace 的 job 串行。
- 每个 worker、每个 repository 的库存上限取该 worker 的 `FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT`，默认 2，配置为 3 时可供三个 Flow 使用。不同仓库分别计数。
- workspace 的创建、路径、准备、清理、隔离和代际由 worker 管理。controller 只从已登记的具体 `(worker_id, workspace)` 领取任务。
- 每个远程 worker 启动时创建或核对一个真实、无仓库的默认推理 workspace。它不占仓库配额，也不绑定编码 Flow；推理请求继续使用独立临时子目录。
- 等待 review 保留原目录；revision、archive 使用原 worker 和 workspace，普通后续 job 不推进 generation。停止清理与合并同时发生时，原 Flow 的 archive 使用清理后的下一代，见第 4 节。容量不足的新 Flow 排队，已有绑定不自动迁移。
- 多执行集合直接放在 `WorkerRuntime`，不增加 Registry 类。不增加 workspace 表、不增加 workspace 外键、不增加总 job 并发上限。

## 2. workers 表和关联记录

`workers` 表扩展为一个 workspace 一行，主键为 `(worker_id, workspace)`。`workspace` 是 worker 产生的稳定 32 位十六进制 ID，独立于仓库名和 generation。

| 字段 | 含义 |
| --- | --- |
| `worker_id` | 物理 worker 身份，可出现在多行。 |
| `workspace` | 具体位置身份，非空。 |
| `repository` | `owner/repo`；默认推理行为空。 |
| `generation` | 当前分配代际；清理和 ACK 结清后推进，普通同 Flow 后续 job 不推进；停止后归档交接使用下一代。 |
| `current_job_id` | 此 workspace 尚未结清的编码 job；为空不代表 Flow 已解绑。 |
| `workspace_state` | idle、preparing、occupied、releasing、absent、quarantined。 |
| `inventory_epoch` | 哪次连接完成了此位置的库存确认。 |
| `retired_generation` | 已退休代际的边界，迟到 idle 报告不能使它重新可分配。 |

连接状态、connection epoch、drain、协议能力、仓库限额和单推理槽，以该 worker 的默认无仓库行作为权威。部分唯一索引保证一个 worker 至多一条默认行；注册保证该行存在。列表和指标按物理 worker 计数，CLI 另列各 workspace 的 Flow/job/repository。

`flow_executions` 保留 `flow_id` 主键，补充 workspace/generation，并以活跃 `(worker_id, workspace)` 唯一索引保证位置独占。jobs 在领取时保存同样的快照；execution_stops 保存原执行的位置和代际，其唯一键为 `(flow_id, workspace, generation)`。停止后的 archive 可能再次停止，不能因沿用原 `bound_at` 而复用上一代停止记录。

移除 `flow_executions`、`execution_stops` 到 `workers(worker_id)` 的旧外键，不增加复合外键。其他既有外键不在本次清理范围。完整性由事务锁、唯一索引以及逐次校验共同维护：位置缺失、代际不同、epoch 或 token 失效都不能通过更新。

主要实现：[schema](../../app/persistence/schema.py)、[models](../../app/domain/models.py)、[WorkerStore](../../app/persistence/workers.py)、[StopStore](../../app/persistence/stops.py)。

## 3. 按需求补充，再直接派发

1. controller 发现有未绑定的新 Flow 等待某仓库，且 worker 此仓库库存未达到上限时，发送 `worker.provision` 仓库需求。该请求不携带 job、Flow、push token，也不预占执行。
2. worker 在自身限额内建立稳定的受管空目录，写 manifest，再上报 inventory。重复需求幂等。没有已登记目录前，job 留在队列中。
3. controller 在同一领取事务中选择具体位置、建立或沿用 Flow 绑定、保存 job 的 workspace/generation/token，并设置该行 `current_job_id`。
4. 保留现有任务参数准备和持久派发授权。`worker.execute` 从首次发送就携带选定位置和代际；worker 核对后只使用该目录，不再自行选槽。
5. Git 内容准备使用 assignment 带来的授权。seed 和共享 Git 元数据操作按仓库串行；长时间 clone、copy、清理不持有 manifest 锁。完成后重新读取、核对目标，再写回该项。

非 absent 的 idle、preparing、occupied、releasing、quarantined 都计入仓库配额；seed 和默认推理目录不计入。降低限额不驱逐旧 Flow、不删除现有目录，阻止超额扩容，并按新额度限制新 Flow 绑定。drain 阻止新 Flow，允许原绑定收尾。

主要实现：[协议](../../app/contracts/protocol.py)、[控制端回调](../../app/controler/workers/callbacks.py)、[本地池](../../app/worker/execution/workspaces.py)、[执行器](../../app/worker/execution/executor.py)。

## 4. 执行、journal 和 ACK

`WorkerRuntime.executions` 按 Flow 持有多个 `WorkerExecution`。每个对象有独立 task、停止任务、工具队列和 `ExecutionState`。消息按 job/token 查到原 journal，再核对持久化目标；不同 Flow 不共享“当前执行”指针。

journal 为 `journals/flow-<flow_id>.json`，不再通过 `current.json` 路由。启动在进程根锁内扫描、校验全部文件。旧单文件和选择指针只参与一次性迁移；不按 mtime 选执行、不覆盖未确认证据。

ACK 按以下顺序持久化：

1. controller 已提交本次结果或停止事实，worker 收到对应业务 ACK。
2. journal 记录 acknowledged。
3. `reconcile_acknowledgement()` 向 pool 发布包含 job/token 的结清凭据。
4. journal 记录 pool_acknowledged；成功 archive 的 journal 最后才删除，并移除 runtime 中对应的执行对象。

启动和重连重复执行结清核对，覆盖 journal 写入后、manifest 写入前后、archive 删除前的崩溃窗口。即使目录准备失败且尚未 bind_workspace，也使用 journal 的 selected_repository/workspace/generation 定位，不跳过结清。

普通 job 结清保留 Flow 绑定。清理成功也不能提前转交目录，必须等原执行 ACK；下一代库存到达前 controller 保持该位置不可选。上一 job 的 ACK 不能清空同位置的新 job，旧 generation 不能清理新 Flow。

停止清理期间收到合并事实时，原 Flow 仍需要 archive 结果。精确的 confirmed-stop ACK 将目录推进一代后，数据库在同一库存事务中把原 Flow 的独占预留交接到该位置的新 generation，不先释放给其他 Flow。要求 Flow 为成功收尾、交付已合并，且没有未结清结果、停止或远端副作用。journal 和 pool 分别核对原 Flow、上一代清理回执与实际目录身份，archive 不重新 clone、不接管其他目录。此路径的原始失败与修复验证见[取消与合并回归](../../tests/test_cancel_drain.py)、[原目录归档恢复](../../tests/test_archive_worker_recovery.py)。

额外的“保留绑定”ACK 字段无法覆盖合并晚于 ACK 的情况，因此沿用现有 ACK 凭据和上述精确交接。旧 result 凭据不能清除随后创建的 stop 占用或回复缓存；已经确认的 stop 只允许按原 digest 重 ACK，不触碰后续分配。

主要实现：[runtime](../../app/worker/runtime.py)、[lifecycle](../../app/worker/execution/lifecycle.py)、[journal](../../app/worker/state/execution.py)、[worker 状态回调](../../app/worker/transport/state.py)。

## 5. 协议与恢复

wire 升级为 20，不支持新旧协议混用。Register/Inventory 上报库存页和 recoveries 集合；Registered 返回逐 job 决定及 ready 标记。每页有明确上限，接收方确认后才发送下一页。

库存必须先于恢复处理。首批恢复附在最后一页库存中，更多恢复再分页面发送；controller 先处理同页库存，再处理恢复。仅最终页触发整机 ready，漏报不能解释成释放。

assignment 携带 workspace/generation；started、工具、结果、停止和 ACK 保留 job/token 身份，由控制端查询不可变位置快照并核对具体 workspace 行，不从 worker 的“当前任务”推断归属。

心跳逐 job 续租，只发送已获恢复授权的执行。Flow 工具调用等整次注册完成后发送。未知、缺失或无法证明原进程退出的执行保留占用，不自动重跑，不占住其他已验证位置。

一个聚合事件观察所有执行，不为每个 job 注册 Session watch。控制端 preparing/current/starting/stopping 都按 job 管理；长准备和停止清理在独立任务中进行，不阻塞其他 workspace 的派发与结果。

Session 保持有界 FIFO。有效消息突发填满队列时暂停 reader，由底层 transport 施加背压；单条过大消息仍拒绝。这样消息批大小不会变成总并发上限。断线只销毁通信观察者，worker 退出才等待全部执行清理后释放根锁。

## 6. 推理和影响范围

默认推理 workspace 不参加编码候选，每 worker 仍只有一个独立推理槽；编码和推理可同时运行。本地 controller 托管的 bub 同样创建一个真实无仓库默认目录，在该服务生命周期内跨重连保留，退出前等待推理清理完成，再删除目录。每次请求使用其独立临时子目录；本地协议端点使用保留的零 workspace ID。保留本地优先策略，不创建编码目录或 journal。远程默认目录身份则持久保存并跨进程重启核对。

影响范围包括 schema/领域模型、领取与停止事务、双端协议、worker 池/runtime/journal、控制端调度回调、worker CLI/统计和升级说明。没有 UI 外观变更，不引入依赖或新表。不同目录只隔离工作文件，不构成 OS 安全沙箱；并发增加也会增加 CPU、内存和磁盘需求。

## 7. 验证与完成条件

使用临时 PostgreSQL、临时 Git/jj 仓库、真实 Session transport 和受控执行器；不操作真实 GitHub 或模型服务。

- 每 worker 限额 3/2、跨仓库独立额度、无库存不能领取、精确目标派发。
- 同 Flow 和同位置的竞争领取、旧 inventory/epoch/token/generation、目标行缺失等不能破坏独占。
- review 亲和、ACK 前后复用、单 job 停止/结果不影响其他执行。
- 多份 journal、迁移中断、ACK 崩溃窗口、分页重连、重复 execute 不重复启动。
- 超过 Session watch 数和单页大小的执行集合；消息仍有界，心跳逐个续租。
- 默认推理目录真实且稳定；推理保留单槽和请求目录隔离。
- 全量 pytest（包含 PostgreSQL）、Ruff、Mypy、console 构建、Python 包构建、diff 检查。

新增合同测试：[数据库独占与结清](../../tests/test_workspace_claims.py)、[本地库存与故障窗口](../../tests/test_workspace_inventory.py)、[真实并发派发与分页重连](../../tests/test_workspace_dispatch.py)。大集合联调曾复现有效心跳/Started 突发导致 FIFO overflow；改用有界背压后，同一场景通过。

最终验证在临时 UTF-8 PostgreSQL 17 上执行，完整 pytest 无失败、无跳过；Ruff、Mypy、console 构建、sdist/wheel 构建及包内 console 检查通过。保留已有 Starlette/httpx 弃用警告及前端 chunk 大小提示，不以屏蔽诊断换取通过。源码没有 UI 外观变更，未做真实 GitHub/模型写入或部署验收。

复现命令为设置 `FLOWTOOL_TEST_DATABASE_URL` 后执行 `uv run pytest -q --tb=short`；静态检查使用 `uv run ruff check app tests`、`uv run mypy app tests`，构建使用 `uv run python scripts/build_console.py` 与 `uv build --no-sources`。历史迁移脚本另以 `mypy --explicit-package-bases` 检查。详细结果保存在[本线程实施记录](https://ampcode.com/artifacts/worker-workspace-206bc2d8-54dd-48f9-934a-52242e13f040)。

### 简化对照与取舍

只在独立 Python 进程内替换机制，不修改产品源码；每次使用同一合同用例与临时数据。原实现全部通过，以下简化变体分别失败，因此保留这些约束：

| 简化变体 | 实际观察 | 保留决定 |
| --- | --- | --- |
| 移除当前 workspace/generation 核对 | 目标行缺失或 generation 已变，旧 assignment 仍被判定有效。 | job/token 与真实位置必须共同核对。 |
| 一个 job 心跳更新整个 worker | 没有发心跳的另一个 job 的 lease 被续期。 | 保留逐 job 心跳，避免掩盖失联执行。 |
| 整个 agent 执行持有共享池锁 | 两个独立目录的并行 barrier 超时。 | 只锁必要的仓库元数据操作，不锁 agent 生命周期。 |
| ACK 只核对 Flow/位置，不核对清理 job/token | 先前 INITIAL 的 ACK 能退休后续 ARCHIVE 的清理。 | 保留清理执行凭据，拒绝同代旧 ACK。 |
| FIFO 满时立即断开，不施加背压 | 有效的第 33 条消息触发 SessionOverflow。 | 保留有界 reader 背压，不增加无界缓存。 |

对照脚本与日志作为本地验证材料保留，不进入产品代码或永久测试。永久测试集中验证容量、位置独占、执行隔离和恢复合同，不按执行集合的内部容器逐函数增加测试。

## 8. 升级与交付限制

新建数据库直接创建复合主键 schema。旧 workers 单列主键数据库启动时明确报错，要求停写并核对真实 worker-root 的 workspace 身份后显式迁移；不能从 worker_id 猜造位置。当前不自动迁移旧数据库，也不将删除库、删除目录或伪造 ACK 当成迁移办法。

既有 Flow 四态迁移脚本只安装自身需要的 Flow 所有权、活跃索引和 console 投影结构，不执行最新完整 schema，不改动历史 worker、执行绑定和 stop 结构。其 fingerprint 已反映实际 DDL；完成 Flow 状态迁移不等于完成 workspace 身份迁移。相关隔离验证见[历史 Flow 迁移测试](../../tests/test_flow_state_migration.py)。

旧 journal/manifest 能证明身份的部分保留并转换；缺少目标或结清凭据的历史记录保留且拒绝不安全接单，需要核对实际部署数据。多绑定运行后不能直接回退到只理解单执行的版本。

本次已获提交、推送和创建 PR 的授权；真实数据迁移及部署仍需单独授权。PR 交付不代表已部署。
