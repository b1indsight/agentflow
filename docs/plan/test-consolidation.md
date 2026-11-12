# 整体测试整理

**状态：** 实施及最新主线上的全量验收已完成；本轮使用独立分支交付，发布状态见本线程。
**需求来源：** [本线程](https://ampcode.com/threads/T-01a1ced6-76ec-73e7-bdd4-bf05dcbb0875)。

## 1. 初始方案

### 目标与范围

优先保留尽量完整的真实流程，把同一行为的独有断言并入现有场景后删除重复测试。复用现有数据库、Git/jj、MCP 和双端通信流程，不为每个回归重新搭建一套状态机。删除仅用于已移除或未实现概念的测试及无调用方的测试辅助。

范围是 `tests/` 和必要的测试辅助，前端测试也纳入核对；不改变产品行为、生产依赖、数据库 schema 或 CI 跳过规则。保留工作区已有改动，以当前本地实现为准，不通过重置到远端覆盖它们。不设测试数量或删行目标，每次删除都要有当前实现依据或明确的承接测试。

### 优先复用现有真实流程

- INITIAL：复用 [`completed_open_initial`](../../tests/test_result_recovery.py) 已有的 trigger、assignment、prepare、tool、结果入库、apply 和 inventory 流程。将其准备至工具回执的前半段抽成 `prepared_initial` fixture，让故障测试停在真实提交边界，完成态 fixture 在同一基础上继续；不复制整个流程。计划签名为 `async def prepared_initial(store: Store, recording_resources: None) -> tuple[Flow, JobCoordinator, RecordingGitHub, FlowToolRequest, InitialTaskResult]`，沿用现有返回内容，不新增场景类型。
- review/revision：复用 [`review_flow`、`ingest`、`claim`、`run_triage`](../../tests/test_review_batches.py) 及 INITIAL 的真实交付，生成持久批次和 REVISION assignment；将 [`revision_control`](../../tests/test_revision_delivery.py) 改为真实 Store 装配，保留外部线程快照。不能只把 `consumed` 替换成 batch 状态，应查询精确 item/version 是否仍待处理。
- archive：继续复用 [`ready_archive`](../../tests/test_archive_recovery.py)、[`pending_archive`](../../tests/test_archive_status.py) 和 [`原 Worker 清理恢复`](../../tests/test_archive_worker_recovery.py)，不再新建平行归档流程。
- Worker：复用 [`test_initial_bootstrap_push_mcp_and_final_verification_share_current_capability`](../../tests/test_worker_executor.py) 的本地 bare remote、真实 clone/commit/push、HTTP MCP 和最终核验；复用 [`client 重连流程`](../../tests/test_worker_client.py)、[`workspace 并发派发`](../../tests/test_workspace_dispatch.py) 和 [`stop 双端流程`](../../tests/test_stop_protocol.py)。

这些 fixture 的阶段和返回值只服务实际调用方，不添加模式开关、通用场景引擎或测试专用产品注入接口。保留外部服务、模型/编码 agent、时间与故障边界的替身；逐步删除重新实现内部事务、仓储和状态转换的模拟。

### 已确认的合并位置

| 整理对象 | 承接位置与必须保留的断言 |
| --- | --- |
| `test_coordinator.test_initial_job_crosses_worker_boundary_and_opens_pr` 及 INITIAL 结果恢复的内存流程 | 迁入真实 INITIAL fixture 的既有场景；保留任务内容、基线分支、工具阶段不开 PR、失败后不开 PR、session、REVIEW、OPEN 和重放不重复 PR/审核/标签。用数据库事实替换 `try_archive` 的调用次数断言。 |
| `test_coordinator.MemoryStore`、`test_flow_tools.ToolStore` 与 `revision_control` / `scenario` / `initial_scenario` | 逐项改用真实 Store 与上述流程；保留提交回滚、epoch/token、call_id 冲突、迟到结果、未决反馈、未知远端写入和结果 ACK 门槛。`test_flow_tool_ref_retry` 继续通过真实 SDK 加假 HTTP 传输核验重试。 |
| `test_worker_executor.test_executor_runs_agent_without_store`、`test_vcs.test_git_outcome_requires_new_clean_commit_on_target_remote_branch` | 并入已有 bootstrap + push + MCP 测试；迁入 agent 启动、实际 workspace/cwd、结果类型、backend/branch、commit/remote/receipt SHA 一致，以及提交不同于初始基线的断言，再删重复成功流程。保留原 session 纠错和祖先关系测试。 |
| `test_worker_session.test_owner_collects_late_completion_without_restarting` | 并入 `test_worker_client.test_completion_between_register_and_resume_is_persisted_without_reexecution`；保留 journal 不可用分支、错误 token 拒绝、assignment/lease、ACK 前后注册结果与 recovery 的区别。完全断线时 done callback 自行落盘是另一故障阶段，继续保留。 |
| `test_protocol.test_registration_rejects_business_but_heartbeat_can_carry_independent_queries` | 将注册 payload 拒绝和 `ack=True` 并入现有 register 控制字段测试；heartbeat 独立 history 结果并入 mixed control/business 测试；非法 history session_id 并入方法契约矩阵。承接完整后删除原函数。 |
| `test_workspace_inventory.test_scan_preserves_all_evidence_on_invalid_inventory[filename]` | 已由 `test_worker_session.test_recovery_preserves_conflicting_journal_files[filename]` 覆盖同一非规范文件名分支，可删除重复参数。其他 corrupt/pointer/symlink/root/record 分支不随它删除。 |
| `test_merge.test_archived_flow_corrects_prearchive_merge_without_reactivation` | 合入 `test_flow_archive_activity.test_delayed_merge_automatically_corrects_without_resuming`；补齐 EventRouter 入口、delivery 不变、merge SHA、无新 job，保留归档历史与重复通知幂等。 |
| `test_review_batches.test_revise_transfers_batch_to_one_derived_job` | 合入 `test_control_triage.test_only_revision_enters_job_queue`；保留 batch/job 双向归属、无 GitHub 评论、后续反馈不能并行领取，以及重复 runtime 扫描只派生一次。事务回滚和置信度两侧边界单独保留。 |

合并按行为和故障阶段判断，不按文件名相似或 Mock 数量判断。同一流程不同故障窗口可以复用 fixture，但各自的触发条件和可观察断言仍须清楚独立。

### 删除过期概念与死辅助

- [`test_schema`](../../tests/test_schema.py) 中枚举 `review_threads`、`review_migrations`、`codex_runs` 以及已删除列的缺席断言删除；当前 schema 初始化和数据保留的幂等断言并入 [`test_store`](../../tests/test_store.py) 已有数据库生命周期场景。当前重要约束通过行为测试保护，不维护历史名称清单。
- [`test_review_batches`](../../tests/test_review_batches.py) 中 `kind.value != "answer"` 的历史否定，改为当前预期队列内容；当前 [`JobKind`](../../app/domain/models.py) 已不含该任务类型。
- 测试 executor 中残留的 `reclaim_workspace` 方法没有产品调用方，也不属于当前 [`TaskExecutor`](../../app/worker/execution/lifecycle.py) 契约；删除死方法及只为它设置的 Mock，不删除所在测试的其他有效行为。
- 每批迁移后检查调用方，再删除不再需要的 MemoryStore、ToolStore、MemoryResults、MemoryReviews、RepositoryViews 和构造补丁。仍被其他有效测试使用时保留到其最后一个调用方迁移完成；`review_context`、`judge_decision` 等仍被使用的纯数据辅助不随类一起删除。
- [`tests/wire.py`](../../tests/wire.py) 已有的报文构造与 worker handler 局部包装重复，可直接复用既有函数，不增加新层。

目前没有发现足够证据支持以“导入了不存在的类”为理由删除整组测试。未实现概念只有经现有代码与调用方核实后才删除，不能把旧格式的拒绝测试等同于为旧功能背书。

### 明确保留与条件候选

- 保留当前仍支持的 [journal v4/v5 读取](../../app/worker/state/record.py)、[旧 receipt/inbox 读取](../../app/contracts/receipts.py)、[旧布局读取](../../app/worker/state/execution.py)、[manifest 兼容与拒绝规则](../../app/worker/execution/workspaces.py)及[离线 Flow 状态迁移](../../scripts/migrate_flow_states.py)。数据库对旧 workspace 状态 schema 的明确拒绝仍是[当前启动行为](../../app/persistence/database.py)，相应测试保留。
- 保留事务回滚、并发领取、跨 Flow 隔离、ACK/stop 未结清、各重启窗口和 VCS 子进程取消/回收。这些行为不能只靠一条正常完整流程替代。
- `test_remote_e2e` 有真实双端运行时但模拟仓储/执行器，`test_remote_asgi` 有真实 ASGI/数据库但手工发送 worker 报文；目前互补，不能直接互删。后续若合并，必须在现有 ASGI 场景接入已有 WorkerRuntime 路径并承接工具、落盘及业务 ACK 后，才删除 RemoteStore 流程；不另建第三套远程 E2E。
- `test_stop_preserves_edits_until_next_initial_prepares_new_generation` 暂不直接删除。只有现有 stop 或 failed-initial/resume 流程承接 terminal 已完成后 stop、同 Flow 下一 generation、真实 WorkerExecutor、manifest 字节及旧绑定不变后，才可合并。
- 前端已核对 activity/search 单测及 Playwright 的现有入口与代表场景，未确认可直接删除的重复用例。HTTP route 替身位于后端边界，保留真实组件交互、移动端、阅读位置和不可信内容安全测试；不为了减少数量合并不同风险。

### 实施顺序与验证

1. 保存当前工作区状态并跑整理前基线。本轮已在独立 UTF-8 PostgreSQL 上运行 Python 全量与前端 `npm test`，均无失败或跳过；这只说明整理前状态，尚未验证任何合并后的测试。
2. 先完成 Worker 成功交付、重连、协议参数、merge 和 REVISE 的明确合并项。逐项列出“删除场景 → 承接测试 → 独有断言”，先验证承接，再删除旧测试。
3. 整理 INITIAL/revision/flow tool 的共享真实 fixture，迁移内存场景及错误注入；按最后调用方顺序移除专用替身与 autouse 构造补丁。移除过期断言和死方法。
4. 运行受影响的真实数据库、Git/jj、通信和恢复测试，再运行 PostgreSQL 全量 `pytest`、`ruff check app tests`、`mypy app tests` 与差异检查。前端测试如有变动，运行 `npm test`、typecheck 和受影响 Playwright 场景。数据库和 jj 不可用时不能把跳过当成验收通过。
5. 对提前开 PR、重复派生任务、迟到 merge 重新激活、结果未提交就 ACK 等关键门槛，按本批实际删除的覆盖选择小范围临时故障对照，确认承接测试会失败。对照仅在独立测试进程或临时副本运行，不提交故障代码，不新建消融框架。本次不新增产品运行机制。

### 代价与风险

真实数据库测试比内存替身需要更多资源，采用已有独立 schema fixture 隔离，并复用已有场景减少重复准备。迁移可能暴露原模拟与产品的差异；如发现产品问题，先记录和隔离，不能通过删断言、扩大 Mock 或修改产品语义来让整理变绿。

实施前需批准本方案。其余 fixture 细节和条件合并是否成立，可在实施中通过调用链、差异与测试核实，不作为开始整理的额外问题。当前未授权远端发布；后续需交付时保持该整理为一个独立 bookmark/PR，不混入已有其他工作。

## 2. PR 变更时间线

### 2026-11-12：明确外部服务模拟边界

- **需求补充：** GitHub 等无法控制的外界对象，可按真实返回结果模拟；其余流程按当前实现测试。
- **实施决定：** INITIAL、revision、flow tool 和结果恢复共享真实 PostgreSQL 准备流程；删除重新实现事务、仓储和结果状态转换的内存辅助。外部 GitHub 响应、模型和编码 agent、时钟与明确的故障注入继续保持可控。
- **进度：** 整理前全量基线已运行。Worker 与 Controller 的独立合并项和真实仓储迁移正在实施，尚未完成整理后验收。

### 2026-11-12：共享真实准备流程并合并明确重复项

- **实际实现：** 原 `completed_open_initial` 拆成 `conftest.prepared_initial` 与继续同一 assignment 的完成 fixture。Coordinator、Flow tool、revision 和结果恢复直接使用真实 Store、事务和结果入库接口，删除 `MemoryStore`、`ToolStore`、`MemoryResults`、`MemoryStops` 与 `MemoryReviews`。review context 改用领域数据，保留纯数据辅助。
- **故障语义：** 不再改写任务 kind 或伪造内存回滚；通过实际反馈入库产生 revision，查询精确待处理 item/version。取消与 merge 测试调用当前 ControlService/EventRouter，验证 stop 未结清、binding 不释放及唯一后续 archive。恢复断言按 effect 类型取评论回执，避免把已成功的线程观察误当成评论成功。
- **重复承接：** Worker 成功核验合入 bootstrap/Git/MCP 流程；owner 迟到完成合入 client 重连；注册/history 合入协议矩阵；非规范 filename 删除重复参数；迟到 merge 合入归档活动；REVISE 合入 runtime 扫描。原独有断言均保留，stop 完成后下一 generation 的候选仍独立保留。
- **过期测试：** schema 历史名称缺席清单已删除，幂等初始化及 flow/attempt/job/delivery 数据保留并入生命周期测试；历史 answer kind 否定改为当前空队列结果；删除无调用方的 reclaim 方法。当前兼容读取、旧 schema 拒绝和离线迁移测试保留。
- **验证：** 已运行真实 PostgreSQL 的受影响跨模块测试，全部通过；[测试整理审查](https://ampcode.com/artifacts/91fae6c8-e2aa-4b62-b02a-c04dbd22dc8c)保存针对性日志。远程 handler 及其消费者迁移尚在继续，最终全量结果另行记录。
- **临时故障对照：** 在独立 pytest 进程内分别注入“工具阶段提前开 PR”“重复派生 revision”“迟到 merge 重激活归档 Flow”；三个承接测试均在对应业务断言失败，未注入的相同测试全部通过。[审查中的对照证据](https://ampcode.com/artifacts/91fae6c8-e2aa-4b62-b02a-c04dbd22dc8c)保留失败位置与正常结果。此次删除内部模拟，没有新增产品机制，不另做复杂度消融；临时注入文件运行后删除。

### 2026-11-12：远程协议和剩余仓储替身改用当前实现

- **实际实现：** `remote_e2e` 与 `conversation_e2e` 共用既有双端 transport、真实 PostgreSQL、coordinator、WorkerRuntime、WorkerExecutor 和本地 Git，保留工具回执、terminal journal、业务 ACK 与 history 分页/阻塞阶段。没有新建第三套远程流程；ASGI 场景继续验证真实 WebSocket 边界，并接入同一 coordinator、工具和结果处理装配。
- **内部状态：** handler、payload validation、workspace provisioning、stop 和恢复并发改用真实仓储；删除 `RecordingStore`、`RecordingCoordinator`、`RemoteStore`、`HistoryStore`、`Inbox`、`InventoryStore`、`NoPendingStops`、`AsgiCoordinator`、`ControlledCoordinator`、`AppliedResults`、`persistence_fakes.py` 及全局仓储构造补丁。`result_application` 的最后一个 ASGI 消费者迁移后，辅助也一并删除。单 session history 复用完成态 INITIAL，session metadata 与 generation 失效检查落到真实数据库。
- **最后调用方：** `test_worker_inference` 同步采用实际 coordinator、工具和结果处理；编码对端明确返回失败结果，再验证它能在 inference 清理未完成时独立提交，避免原替身无条件将不含交付回执的成功声明标记 DONE。成功编码交付仍由已有真实 Git/工具/ACK 完整流程覆盖。
- **测试数据校正：** workspace 并发场景通过当前 `ControlService.on_trigger` 产生含 installation 的任务，使用合法的正数 issue 编号；先前内存准备绕过了这些实际前置条件。history 跨 Flow 游标场景使用真实存在的另一条 Flow，generation 变化经持久化更新。未为测试修改产品行为或扩大超时。
- **断言承接：** `worker_handler.test_recovery_continues_after_one_broken_result` 并入 `recovery_concurrency` 同名场景，查询实际提交状态；原 live/recovery 锁场景扩展为 live apply 与重复 recovery sweep 共用锁，并保留其他 job 可继续提交。stop 的未结副作用通过 GitHub POST 成功但响应丢失重现，保留实际回执阻止 ACK 的条件。
- **保留边界：** 外部 GitHub 响应、编码 agent/Amp export、协议对端、可控并发暂停和明确的连接/时钟/文件系统故障继续注入；不删除不同的 stop/ACK、重启、跨 Flow 隔离和当前兼容读取场景。没有新增产品机制，因此无需另做复杂度消融。
- **验证状态：** 真实 PostgreSQL 上不排除文件的 Python 全量通过，无跳过；前端单测、TypeScript、Ruff、Mypy 和差异检查通过。最后调用方与迁移后的调度、history、inference、ASGI 场景也已通过针对性检查。执行命令、合并对应关系和原始日志保存在[同一审查记录](https://ampcode.com/artifacts/91fae6c8-e2aa-4b62-b02a-c04dbd22dc8c)，不以迁移中的失败或排除文件的阶段检查代替验收。临时 PostgreSQL、测试前快照和故障注入文件已清理，验证日志保留。

### 2026-11-12：保留最新归档行为并隔离交付差异

- **上下文与决定：** 原工作区混有其他任务，发布时从整理前的 jj 快照提取本轮测试增量，再三方移植到最新 `main`。保留主线的产品代码、前端及其他任务测试，不复制旧文件覆盖后来实现。
- **主线兼容：** 合并 PR 后立即归档的业务结果、未结执行绑定和 stop 回执仍各自验证；恢复场景使用真实 merge 入口并检查 ARCHIVED，取消场景保持 FINALIZING。保留主线新增的迟到结果、丢失 stop ACK 和新 writer 保护断言，以及 ResultEffects 的 plugin 装配。共享 fixture 的旧导入调用方同步移除冗余导入。
- **验收：** 独立 UTF-8 PostgreSQL 上完整 Python 测试通过，无跳过或排除；Ruff、Mypy 与差异检查通过。此次移植没有修改产品、前端、schema 或 CI；[审查记录](https://ampcode.com/artifacts/91fae6c8-e2aa-4b62-b02a-c04dbd22dc8c)保存单独交付工作区的日志。
