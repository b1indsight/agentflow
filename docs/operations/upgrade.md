# 当前版本部署与重启

仓库尚未部署，仅支持当前完整 PostgreSQL schema、wire protocol v10 和 worker journal v3。
不提供旧数据库、旧任务或旧 journal 的升级/回滚转换。首次部署选择新数据库/schema 与新
worker root，serve/worker 固定到同一 commit/镜像 digest；schema 初始化只创建当前结构。

同版本重启保留现有恢复契约：

1. 暂停入口并 drain worker，记录未完成任务与无法收敛的原因。
2. 按[冷备](backup-restore.md)保存数据库、worker root/HOME 及身份映射。
3. 停止所有写入进程；使用相同版本初始化 schema，初始化可重复执行。
4. 按原身份启动 serve/worker，检查 readiness、journal、inbox 和结果恢复状态。
5. 验证后恢复入口并 undrain。Docker/K8s 两种角色使用同一版本镜像。

已有旧版开发数据需独立保留或显式重建，应用不自动删除、改写或迁移。备份恢复使用生成备份的
同一应用版本；不能仅修改 journal/manifest 的版本号来加载不同契约的数据。
