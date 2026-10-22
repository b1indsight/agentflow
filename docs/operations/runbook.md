# 常驻运行与排障

首版为单 serve、固定 worker ID/root/HOME。部署入口见 [部署文档](../deployment/README.md)，
不新增部署 CLI；Cloudflare 只属于接入部署。生产环境完整恢复与 RPO/RTO 尚未验收。

## Linux systemd（手动更新）

先按直接进程文档安装固定 commit 的 `/opt/flowtool/.venv`，worker 另外安装同镜像版本的
Git、jj、Codex 并确保 service PATH 可见；模型认证使用 `CODEX_API_KEY` 或预置受限 HOME。
两个非登录系统账号 `flowtool-control` / `flowtool-worker` 分别拥有 control 与 worker-1 数据目录。
`/etc/flowtool/{serve,worker-1}.env` 由 root 拥有、0600，systemd 读取后传给对应进程。
App 私钥仅 control 可读；worker env 不得放 DB URL、App key 或 tunnel 凭据。

```sh
sudo install -d -o flowtool-control -g flowtool-control -m 0700 /var/lib/flowtool/control
sudo install -d -o flowtool-worker -g flowtool-worker -m 0700 /var/lib/flowtool/worker-1/root /var/lib/flowtool/worker-1/home
sudo install -m 0644 deploy/systemd/flowtool-serve.service deploy/systemd/flowtool-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flowtool-serve flowtool-worker
sudo journalctl -u flowtool-serve -u flowtool-worker --since '10 minutes ago'
```

账号、依赖和私密 env 应先创建，命令不生成凭据。工作目录必须存在；只启动一个 serve unit。
worker token/ID/WSS/CA 与现有 [worker 配置](../deployment/docker.md) 同义；serve 设置私有监听地址。
跨主机经内部 TLS 网关连接，公网仍只放行 webhook；勿为方便而公开 worker/metrics 路由。
unit 的 cgroup 在超时后会清理子进程，但强杀不是应用层 stop receipt，仍需遵守恢复 fence。
Worker 更新命令不操作 systemd；服务停止和启动由操作者分别执行，原地更新不变更执行路径。
systemd 实际主机验收尚未执行。

## Worker 显式自更新

正式 uv Git 安装在原入口执行 `flowtool worker --update`，自动查询并更新当前包及依赖，
通过新进程版本、CLI 和依赖核验后退出。人工完成既有任务和确认、正常停止共享安装的全部 Worker，
更新成功后使用原启动方式重启；源码检出不执行此更新命令。
保持原登录用户、HOME、ID、root、journal 和认证；若使用系统服务，先人工停止并禁用自动重启。
CLI 的安装目录锁拒绝与本版 Worker 并发更新，但旧版本进程和手工包管理器仍须人工停止。
不操作服务、不创建版本目录或独立环境，失败可能部分改变现有依赖；保持停止并修复原环境。

没有后台发布检查、自动空闲切换或暂停接单协议。操作步骤见
[Worker 更新部署](../deployment/worker-update.md)。Server 更新仍按
[Server 更新](../deployment/server-update.md)的普通预检、停旧和启新流程执行，不等待 Worker 更新。

## Server Webhook 更新

独立更新器监听仓库 Images 完成事件，验签后写主机本地文件队列，由单消费者部署。
它不使用业务 inbox 或宿主机数据库连接。先查 `image_event.py status` 和 operation.json；
failed 可重投或 retry，running/停旧后的 incomplete 直接按原镜像续作，不删文件解锁。
已 Ready 不重复停启，队列损坏、目标 UID/配置冲突须明确处置；不回滚或强删。
首次端口、TLS、secret、状态目录和命令见 [Server 更新](../deployment/server-update.md)。

## 日常与故障操作

先保存时间、部署 commit/digest、delivery/Flow/job/worker ID、停止/恢复边界，避免记录 token/正文。
用 `flowtool workers`、`flowtool inbox status` 及 [日志指标](observability.md) 确认状态；
用 `flowtool --help` 查看现有 flow 查询命令。不要直接改数据库状态或清 journal 解锁。

| 场景 | 操作与继续条件 |
| --- | --- |
| worker 短断连 | 保留原进程/root，等待相同身份恢复与结果 ACK；不能并起相同 ID |
| worker 旧进程未知 | drain 并隔离旧机器/Pod；持有 binding，直到有可验证停止证明 |
| DB 暂停/连接失败 | 停新入口并恢复 DB；确认快照新鲜、inbox 和结果恢复后再接入 |
| 重复 webhook | 查相同 delivery；仅已确认可重试的 blocked 使用 `flowtool inbox retry DELIVERY` |
| ACK 丢失 | 保留 terminal journal，让相同身份重连重放；不重新执行模型来补 ACK |
| SIGTERM/SIGKILL | 检查进程/cgroup、journal、未应用结果和 stop 状态；重启不等于释放成功 |
| 归档失败 | 查 merge、cleanup 证明及最新 archive job；保留绑定和 root，勿删除卷 |
| GitHub 比备份更新 | 保持派发关闭，按 [恢复边界](backup-restore.md) 做只读远端核对 |

停止/排空详见 [cancel/drain](cancel-drain.md)。K8s 不强制删 Pod；Docker 不删数据卷。
升级、凭据变更分别见 [升级](upgrade.md)、[轮换](credentials.md)。

<a id="alerts"></a>
## 告警

Prometheus 加载 `deploy/monitoring/alerts.yml`，job 名必须为 `flowtool` 和 `flowtool-backup`。
服务 scrape 使用独立 bearer 文件、私有网络，参见观测文档；Alertmanager 接收方由部署者配置。
告警没有自动发通知，也没有自动执行 retry/cancel/释放 binding。

规则：inbox 最老 >5m、blocked>0；queued 且无 online/busy worker 持续 2m；结果恢复/归档 >10m；
采集快照 >60s 或从未成功；服务不可 scrape；备份失败、成功距今 >26h、监控缺失。
除无 worker 为 2m，其余持续 1m 触发。阈值是示例，不是 SLA；长模型任务应调整。
`archive_pending_age_seconds` 只包含活动且仍绑定 Flow 的最新非成功 archive job。
旧的 archive/reconcile 历史年龄指标不用于这些告警。online/busy 是登记事实，不能证明能接新 Flow；
draining worker、容量不足和长期 busy 需结合 queued 年龄人工检查。

备份管理方通过内部 textfile exporter 提供两个 gauge（成功时间为 UTC Unix 秒）：
`flowtool_backup_last_success_timestamp_seconds`、`flowtool_backup_last_run_success`（0/1）。
仅完整生产备份验证成功后原子更新成功时间；失败只设 last_run_success=0，保留旧成功时间。
fixture 脚本不会发布这些值。exporter job 设为 `flowtool-backup`；未接入会产生 Missing 告警。
缺数据不能解释为零积压；先排查 Unavailable/SnapshotStale/BackupMissing，再看业务数值。
当前示例按一个部署编写；多部署需加入 deployment 标签并改按部署检测缺失。

本地或 CI 用固定 Prometheus 3.5.1：`promtool check rules deploy/monitoring/alerts.yml`，
`promtool test rules deploy/monitoring/alerts.test.yml`。覆盖触发、持续时间、恢复和无数据。
依据：[官方规则测试](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)。
