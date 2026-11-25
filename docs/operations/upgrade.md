# 当前版本部署与重启

当前格式为 [wire protocol v33](../../app/contracts/protocol.py)、worker journal v5、执行 ResultInbox v1、Control 合并通知 inbox v3、workspace manifest v3。serve 和 worker 拒绝不匹配的在线协议；在维护窗口停止新分配，等待在途准备、推理、执行和已发远端操作收敛，再备份并协调更新两端。
首次部署选择新数据库/schema 与新 worker root，serve/worker 固定到同一 commit/镜像 digest；
空 schema 初始化为 v3；已有 v3 库启动时校验结构、应用[声明的增量](../../app/persistence/schema.py)，并在更新控制台视图时刷新旧的 `unknown` 操作投影，不补写历史账本。应用账号需具备相应 DDL 权限。未标记旧库须离线迁移；不要手工补版本标记。前置的[多工作区并发版本](../plan/worker-workspace-concurrency.md)仍须独立核验 workers 结构及物理位置迁移。

## 将旧 Flow 保留为只读历史（数据库 schema v3）

**只迁移全部 Flow 均已归档的旧库。** 预检和 `--apply` 都检查这个条件。发现未归档记录时报告 Flow ID 和状态并停止，不跳过记录，也不替用户归档。迁移保留原链接、归档结论和任务正文，释放 Issue 所有权。旧详情只供查阅；切换后创建的 Flow 可正常运行，归档后仍可重新开工。

只支持从已有六态账本、合表和多工作区的未标记轮号库迁移。空库直接初始化为 v3。旧库不能通过启动服务来升级；未知或混合结构需人工核对。先在旧版本核对账本、按[冷备与恢复](backup-restore.md)恢复独立 PostgreSQL 副本。以下从**新版本源码根目录**运行，service 名指向副本，不要让副本连接生产 Worker 或 GitHub：

```sh
uv run python -m scripts.migrate_flow_attempts --database-url 'service=restored_copy' > preflight.json
uv run python -m scripts.migrate_flow_attempts --database-url 'service=restored_copy' --apply > applied.json
uv run python -m scripts.migrate_flow_attempts --database-url 'service=restored_copy' --apply
```

第一条只读。检查 `status`（待迁移库应为 `legacy`）和全部 `conflicts`。预检拒绝未完成的 Job（含独立推理）、未应用的收件、未释放的绑定/Worker 占位、未确认停止、未结清的工具调用或副作用、未处理 webhook、未定的 Projects 远端新增、仍在处理的评审批次。不要删队列、伪造停止回执或覆写结果让检查通过。回到旧版本按报出的身份结清；无法证明真实进程已退出或远端写入已对账时，停止升级。

`--apply` 在独占锁内重做检查，同一事务内完成身份回填、历史标记、队列退役和版本提交。按同一 Flow 内的旧轮号关联已有 Attempt，保留其 ID、时间戳和会话。归档卡片、作业或活动引用的轮号缺少 Attempt 时，只用 Flow 创建时间（首轮）或唯一的重开记录补建；归属冲突或缺少所需时间则停止。迁移不重放旧状态机。历史收件、计划和 effects 原样保留，不再派发或重试。

在恢复入口前，二次执行应返回 `status=current, applied=false`。对比备份：Flow ID、选择的 Attempt、归档结论、活动版本/key/父引用/元数据、作业收件和 digest 应对应；无归属的共享活动仍无归属。核实 `SELECT count(*) FROM flows WHERE NOT history_only OR is_issue_owner` 为零，旧 Console 同步不再 pending、无 claim，旧 Projects/对账不再排队，来源限制、覆盖缺口和已采集正文仍可查看。**不要用重新采集旧内容验证切换**。在隔离副本上打开原链接，确认归档记录可读且不可重新开工。

正式切换需另行授权。停止**全部**入口、控制端、Worker、定时写入方；记录/排空在途任务、外部操作和未确认停止，保存并验证数据库及原 Worker root/HOME 备份可恢复，再在停写库执行上述核对。API、Console、CLI、Worker 同步发布。恢复写入前若失败，事务会回滚；若已提交可恢复停写备份并运行旧版本。恢复写入后**不可**直接降级或覆盖库：先停写、保存增量与外部事实，再前向修复或对账恢复。

## 升级前核对数据库与工作区

- 仍有 `issue_flow_owners`、`flow_deliveries` 或 `webhook_inbox` 的六态库须先停写、备份并按[合表记录](../plan/flow-event-table-merge.md)手工迁移，再启动新控制端。直接输入的任务没有 Issue；首次写入后，即使任务已归档，也不能直接降级到只支持 Issue 的版本。
- 更早的四态库须在[固定旧版本](https://github.com/b1indsight/agentflow/tree/6600d34dbf14e22f76d835405dd8742ac3f902f5)单独检出 `scripts/migrate_flow_states.py`。先预检，核实未知执行与归属冲突；应用时须提供 `--apply --expect-report <预检指纹>`。数据、schema 或迁移规则变化后重做预检。仅校验通过且已应用的 v13 非推理结果可列入 `inbox_conversions`，备份原行并转换到 v14；不转换在线协议或 Worker journal。
- 四态迁移不会迁移旧的单行 Worker 主键。须分别核对实际 Worker 身份、root 与 workspace，再迁移到 `(worker_id, workspace)`。保留原 ID、root、HOME 和逐 Flow journal；不要靠复制目录或替换 ID 接管未结执行。更新方式见 [Worker 安装与更新](../deployment/worker-update.md)。

## 合并归档与资源回收分离

对可运行的 Flow，Control 核验 PR 合并后先提交业务归档，原 Worker 的停止、精确回执与 GitHub 通知独立收尾。新合并不再派发 Worker ARCHIVE；当前 Control 合并通知写 inbox v3，Worker 执行写 v1。只读历史不参加这套恢复流程。

在维护窗口暂停入口和新分配，备份数据库及原 worker root/HOME，盘点冻结任务、dispatch authorization、终态回执、effect ledger、停止请求和各位置 generation。当前环境已由用户确认不存在遗留 Worker ARCHIVE 任务，本次不新增该类任务的恢复或交接逻辑；既有历史格式读取、原回执与 digest 保持不变。若盘点发现不符合此前提的任务，停止升级并单独核验。

新版本的有界积压扫描会归档当前轮次已有完整合并时间/SHA 的记录，包括原绑定缺失或已释放的记录；不补造合并证据、不复活绑定、不改变仍 open 的执行失败。启动该恢复属于生产状态写入，必须单独取得部署与生产恢复授权，计划或本地实现审批不包含该授权。

业务归档不证明目录清理完成。原 Worker 确认执行及其子进程已退出、远端结果与已发操作结清后，Control 原子释放绑定并推进 generation，立即允许位置复用，不等待固定宽限期或 journal ACK。下一 INITIAL 在获得独占后清理旧工作副本；stop 不做 Git 清理。库存只刷新目录身份与在线信息，不能清空 claim、释放绑定或推进 generation；原 root/journal 或 generation 证据丢失时继续拒绝不安全接单并人工核验。相邻工作区可独立运行，同一 Flow 的 resume 仍等待旧绑定释放。

通知权限失败只保留冻结的失败 effects，可通过 `flow retry` 补发；发送结果不确定时先查询原 marker，不重发，也不恢复编码。轮次已 resume 或 Issue 已换 owner 时不让旧通知覆盖新的标签。

**写入 inbox v2 后不能直接换回旧二进制。** 旧 reader 不认识 Control 通知，旧业务规则也不能安全解释“已归档但仍持有资源”。先核对并收敛 v2 通知、未决 effects 和真实工作区，另行制定回退方案；不得删除收件、清空 journal、伪造 ACK，或仅凭备份覆盖升级后的外部事实。本次没有执行部署、生产积压修复或生产目录清理。

## v17 → v18 协调升级

v18 增加通用 `worker.infer` 和注册中的必填可空推理能力，网络不兼容 v17；持久格式不变。
在受控维护窗口暂停新入口与分配，等待编码执行及已派发 effects 收敛，再协调更新 serve/worker。
推理无 journal，连接关闭会取消本连接的推理；未保存决定的 READY 批次可在后续扫描重新计算。
升级或分区可能重复计算和计费，不能假定旧原生进程已经退出。保留并核查 worker root/HOME。

首次启动前核对 controller 的 bub model/key/base：三项均空会启用 worker 路由，半配不会转派。
worker root 须在 Git 仓库/worktree 外，原生登录态和隔离要求见[部署说明](../deployment/README.md)。
回退 v17 同样需要两端协调；先核验当前业务状态并准备本地 bub 配置，旧版没有此 worker 推理路由。
不清空 journal/inbox，不伪造确认；持久格式相同不代表可以无损滚动回退。

## 历史：v16 → v17 协调升级

v17 引入仅含固定 code/message 的 `tool_result.error` 验证错误，持久格式与 v16 相同。
网络只接受 v17，不兼容 v16 或更早版本，也不自动降级。安排受控维护窗口，停止新分配，等待活跃执行结束与
已派发外部 effects 收敛；drain 仍允许已有 Flow 收尾，不能将 drain 本身当作全部执行已停止。
普通执行 Task 不能靠 started journal 跨进程恢复。升级前备份并只读盘点实际格式；未知记录
先保留并制定处理方案，不能清空 journal/inbox 或伪造确认。

serve 与 worker 协调升级并考虑现有 recovery grace。回退 v16 也须双端协调并另行核验运行中
业务状态，不能只换一端；持久格式相同不代表可以无损滚动回退。本次不提供自动回滚或部署。

## v15 及更早版本的持久格式边界

新版本严格读取 v4 journal 中已知
v14/v15 回执及已知历史 worker/control inbox，保留原摘要、停止证据和 tool ledger。
读取只在内存转换；下一次正常状态/确认写入才写 journal v5，新结果写 inbox v1。
终态和停止凭据可重放；未知、混合或损坏记录拒绝并保留，不据此假定真实部署已有哪些格式。

**不能直接降级到旧持久格式：** 写入 v5/v1 后，早于 v16 的二进制与旧来源 SQL 不理解新格式。默认向前修复；
反向转换和 effects 对账需要单独设计。不能用旧备份覆盖升级后的外部事实，也不能删除
journal 或伪造 ACK 来恢复运行。不承诺无损滚动升级或一键回退。

## 同版本重启

同版本重启保留现有恢复契约：

1. 暂停入口并 drain worker，记录未完成任务与无法收敛的原因。
2. 按[冷备](backup-restore.md)保存数据库、worker root/HOME 及身份映射。
3. 停止所有写入进程；使用相同版本初始化 schema，初始化可重复执行。
4. 按原身份启动 serve/worker，检查 readiness、journal、inbox 和结果恢复状态。
5. 验证后恢复入口并 undrain。Docker/K8s 两种角色使用同一版本镜像。

受限 decoder 之外的旧版数据需独立保留，应用不自动删除或批量迁移。备份恢复使用生成备份的
同一应用版本；不能仅修改 journal/manifest 的版本号来加载不同契约的数据。部署仍需单独授权。
