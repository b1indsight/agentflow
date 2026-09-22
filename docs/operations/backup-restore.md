# 冷备份与恢复边界

目标为每日一次、保留 7 份日备/4 份周备，RPO≤24h、RTO≤2h；这是待验收目标。
完整活动 Flow 从复制卷恢复目前 **blocked**：journal 校验 root 路径、设备与 inode。
原 volume 回挂需保持这些身份并验证唯一执行；文件复制不能假定可恢复运行，禁止手改 journal。
计划停机优先让 Flow 完成归档并释放 binding；无法安全停机时标记该次备份失败并告警。

## 冷备前提与采集

PostgreSQL 生产基线 17.x（Compose 为固定 digest），用同 major 的 pg_dump/pg_restore；
隔离演练也支持现有 16.x 测试库，但不能以此代替生产 17.x 验收。密码放 0600 PGPASSFILE，
连接参数通过 PGHOST/PGPORT/PGDATABASE/PGUSER 指向已核对的实例，不放命令行或日志。
[pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html) 的一致快照仅覆盖数据库，不覆盖 worker 文件。

1. 停 Cloudflare/公网 webhook 入口，记录最后成功 delivery 与 inbox；GitHub 后续可能需要人工重投。
2. 每 worker `flowtool worker-drain ID`，允许现有 Flow 收敛；检查 jobs、results/effects、stop 和绑定。
   drain 不会结束正在等待 review 的 Flow，不能为赶备份强制 cancel。
3. 停 serve，再停 worker，禁用自动重启/重建；验证旧进程和子进程已退出，所有写入源已停止。
   systemd 用 `systemctl stop`；Compose 用 `stop`（不 down -v）；K8s 两类 StatefulSet scale 0，
   等待 Pod 真正退出和 CSI detach，禁止 force delete。worker/DB 写入未知时停止后续步骤。
4. 在受限目录保存 DB、每 worker root/repo/journal、HOME 会话、配置引用和版本。HOME 可能含认证，
   整个集合按 secret 等级加密离机存储；公开摘要不包含连接串、私钥、token、HOME 文件正文。

以下在**确认完全停写、变量已设定**后逐条执行。`BACKUP_DIR` 必须为本次新目录；
`WORKER_DATA_PARENT` 必须是包含 root/home 的已停止 worker 数据目录，不是活跃挂载的替代副本。
多 worker 每个单独 tar/manifest 项；容器卷通过受控只读挂载导出，不启动 worker 来拷贝。

```sh
umask 077
mkdir "$BACKUP_DIR"
pg_dump --format=custom --no-owner --no-acl --file "$BACKUP_DIR/database.dump"
tar -C "$WORKER_DATA_PARENT" -czpf "$BACKUP_DIR/worker-1.tar.gz" root home
pg_restore --list "$BACKUP_DIR/database.dump" > "$BACKUP_DIR/database.list"
sha256sum "$BACKUP_DIR/database.dump" "$BACKUP_DIR/worker-1.tar.gz" > "$BACKUP_DIR/SHA256SUMS"
sha256sum --check "$BACKUP_DIR/SHA256SUMS"
```

命令失败就停止，不生成 complete。dump 不含数据库角色；单独记录角色/扩展/权限配置，
恢复前显式创建对应最小权限账号。不要复制运行中的 PostgreSQL 数据目录。
每份 manifest 记录 backup_id、UTC、commit/镜像 digest、协议、schema 对应版本、DB major、
worker ID、原 root 路径与 stat(dev,inode)、UID/GID、文件 digest、配置/secret 版本引用，
并逐项签署 stopped/dumped/archived/verified；完整性检查通过后才设 complete。
当前无独立 schema migration version，记录应用 commit 和 dump 自身 schema，不虚构版本号。
校验清单需可信保存；hash 不能证明被攻击者同时替换的 manifest/归档可信。
最后按原部署版本启动唯一 serve/worker、验证恢复与指标后再开入口、处理 GitHub 重投。
保留策略仅删除已验证、过期且无恢复引用的完整备份；不自动删除生产卷或 pending journal。

## 恢复顺序

隔离旧主机/集群，确保不能恢复派发；新环境先禁公网入口、worker 和 serve 自动启动。
核验 manifest、解密权限、所有文件 hash、版本/secret 引用；缺文件/损坏/未知写入立即 blocked。
用独立空测试数据库，预建正确 owner，再执行：

```sh
pg_restore --exit-on-error --single-transaction --no-owner --no-acl --dbname "$RESTORE_DATABASE" "$BACKUP_DIR/database.dump"
```

PGHOST 等必须指向隔离恢复环境。失败不启动应用；不向生产现有库执行 --clean 或覆盖恢复。
归档先展开到私有空 staging 目录，保留权限，核验 UID/GID/文件 hash；不覆盖运行卷。
检查 inbox/job/result/effect/binding 与对应 worker journal 是否来自同一停写点；确认唯一执行证据。
恢复 secret 的有效版本，不从 journal 恢复旧短期 push capability。

在启用可能触发写操作的 serve 前，**只读**核对 GitHub 当前 head/merge、PR/评论 effect marker。
备份后的 GitHub 写入不会随 DB 回滚。若本地落后、副作用未知或旧执行无法排除，保持 blocked；
当前没有通用“只读恢复模式”，不得以启动 serve 来代替这一步。先在 fixture 验证对应 M4
对账路径能保守收敛，否则需要独立恢复 feature。启动后验证无重复 PR/评论、终态一致再开入口。
root identity 变化也需独立迁移机制；该前置未完成前不承诺活动 Flow 恢复成功。

## 可重复隔离演练

```sh
FLOWTOOL_TEST_DATABASE_URL='postgresql://test_user@127.0.0.1:5432/test_db' \
uv run python -m scripts.smoke.recovery --run --work-dir .cache/2026-09-18/my-recovery \
  --postgres-bin /path/to/postgresql/bin --revision REVIEWED_COMMIT
```

父目录须存在、work-dir 须全新；仅接受显式 loopback 测试连接，不传生产 URL。
首次连接后冻结 libpq 实际解析的配置，Python 与 dump/restore 共用端口、密码文件、TLS 路径
等设置，并固定已验证的 loopback 地址。显式 URL 参数优先于环境默认值；没有安全环境变量
映射的非空参数（如 `keepalives`）会在创建资源前拒绝，不会静默忽略。凭据不写入命令行或报告。
脚本只创建/删除/恢复自己随机命名的 schema；保留 resources.json、manifest、归档及 report，
失败也保留已创建资源。检查完仅删除该 resources.json 标识的 fixture schema/目录。
脚本不启动 serve/worker/模型，不连 GitHub、不修改生产 backup gauge。
真实测试活动 assignment/binding、inbox、HOME 和 journal，不是空库还原；复制 root 应阻塞。
损坏/缺件/越界/链接/未停写 manifest 拒绝由单元测试覆盖。实际证据与尚未覆盖场景见
[验收记录](../plan/m5-operations-recovery.md)。这个脚本是测试入口，不是生产备份工具。
