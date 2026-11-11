# 当前版本部署与重启

当前格式为 wire protocol v20、worker journal v5、执行 ResultInbox v1、Control 合并通知 inbox v2、workspace manifest v2。
首次部署选择新数据库/schema 与新 worker root，serve/worker 固定到同一 commit/镜像 digest；
schema 初始化只创建当前结构。合并归档调整不增加 DDL；前置的[多工作区并发版本](../plan/worker-workspace-concurrency.md)仍须独立核验 workers 结构及物理位置迁移。

## 合并归档与资源回收分离

Control 核验 PR 合并后先提交业务归档，原 Worker 的停止/清理、ACK/inventory 与 GitHub 通知独立收尾。新合并不再派发 Worker ARCHIVE；仅新的 Control 通知写 inbox v2。Worker 执行仍写 v1，wire20、journal v5 和 manifest v2 不因这项调整再次升级。

在维护窗口暂停入口和新分配，备份数据库及原 worker root/HOME，盘点冻结任务、dispatch authorization、终态回执、effect ledger、停止请求和各位置 generation。当前环境已由用户确认不存在遗留 Worker ARCHIVE 任务，本次不新增该类任务的恢复或交接逻辑；既有历史格式读取、原回执与 digest 保持不变。若盘点发现不符合此前提的任务，停止升级并单独核验。

新版本的有界积压扫描会归档当前轮次已有完整合并时间/SHA 的记录，包括原绑定缺失或已释放的记录；不补造合并证据、不复活绑定、不改变仍 open 的执行失败。启动该恢复属于生产状态写入，必须单独取得部署与生产恢复授权，计划或本地实现审批不包含该授权。

业务归档不证明目录清理完成。原 Worker 的停止、精确 ACK 和库存结清后立即允许位置复用，不等待固定宽限期；原 root/journal 或 generation 证据丢失时继续隔离并人工核验。相邻工作区可独立运行，同一 Flow 的 resume 仍等待旧绑定释放。

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
