# 当前版本部署与重启

当前格式为 wire protocol v16、worker journal v5、ResultInbox v1、workspace manifest v1。
首次部署选择新数据库/schema 与新 worker root，serve/worker 固定到同一 commit/镜像 digest；
schema 初始化只创建当前结构。本次协议迁移不增加 DDL、批量数据改写或部署自动化。

## v15 → v16 协调升级

网络只接受 v16，不自动降级或兼容 v15。安排受控维护窗口，停止新分配，等待活跃执行结束与
已派发外部 effects 收敛；drain 仍允许已有 Flow 收尾，不能将 drain 本身当作全部执行已停止。
普通执行 Task 不能靠 started journal 跨进程恢复。升级前备份并只读盘点实际格式；未知记录
先保留并制定处理方案，不能清空 journal/inbox 或伪造确认。

serve 与 worker 协调升级并考虑现有 recovery grace。新版本严格读取 v4 journal 中已知
v14/v15 回执及已知历史 worker/control inbox，保留原摘要、停止证据和 tool ledger。
读取只在内存转换；下一次正常状态/确认写入才写 journal v5，新结果写 inbox v1。
终态和停止凭据可重放；未知、混合或损坏记录拒绝并保留，不据此假定真实部署已有哪些格式。

**不能直接降级：** 写入 v5/v1 后，旧二进制与旧来源 SQL 不理解新格式。默认向前修复；
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
