# M5 F8：常驻运维、备份恢复与故障验收

> 状态：已获“按顺序实现 PR”授权；工具与文档已实施，完整部署恢复验收待办。2026-09-22。
> 文档 PR 基线 main@origin / 55313e1f；依赖 F5 观测、F6 stop/drain、F7 K8s 及 F4 镜像。
> M4 后续恢复能力实施前核对真实集成版本，不从已关闭/合并的分支 PR 推断 main 已包含。
> 阶段范围：[PR #38](https://github.com/b1indsight/agentflow/pull/38)。

## 1. 交付与支持边界

提供直接进程、Docker、K8s 三种方式的常驻运行/维护文档、告警规则、冷备份恢复流程、
故障演练入口和实际证据。使用现有命令及系统/数据库工具，不新增一套部署管理 CLI。
首版单 serve、固定 worker identity/root，支持计划停机冷备份；不承诺跨 worker 迁移、
在线一致快照、多 serve HA、任意时点恢复或零停机 schema 升级。

首版运行参数建议：每日一次冷备份，保留最近 7 份日备/4 份周备；恢复目标暂定 RPO≤24h、
RTO≤2h，并记录实际演练值。不能为达到频率强行取消等待 review 的 Flow；未能安全停机则
该次备份标为失败并告警。上述为待验收目标，不是当前保证。

## 2. 常驻运行与维护

- 直接进程：提供 Linux systemd 示例，角色独立账号/EnvironmentFile/数据目录，配置停止超时
  与 F6 一致；macOS 首版沿用手动启动说明，不承诺额外 launchd 服务实现。
- Docker/K8s：复用已有交付，不复制另一套镜像/清单；记录版本/digest/配置/schema 与数据卷映射。
- 升级先 drain，完成/明确停止旧执行，保留旧配置/镜像和备份，单 serve 切换；schema 不可逆
  时恢复备份或前向修复，不能盲目降级二进制。中途失败保留已完成步骤及阻塞原因。
- 凭据轮换覆盖 App 私钥、webhook secret、worker token、模型认证和 tunnel 凭据；按实际
  支持能力给出停机窗口/重新投递步骤，不假定程序支持双密钥热轮换或环境变量即时生效。
- 不自动执行 GitHub merge、删除用户 PR/repo、轮换真实账号凭据或修改 Cloudflare DNS。

## 3. 一致备份集合

数据库快照与文件系统不是同一个事务。冷备步骤明确顺序：停止公网接入并记录最后确认
inbox → drain 并收敛 worker 执行 → 停止 serve/worker → 确认没有旧进程/写入 → DB dump
和 worker 数据归档 → 校验 manifest → 恢复原服务。仅停 cloudflared 不等于停止内部写入。

备份集合包含 PostgreSQL（inbox、jobs、bindings、result/effects 等）、每 worker repo/root/
journal、HOME 下会话数据、运行版本和配置引用；文件归档保留权限和身份映射。
secret 不进入公开备份摘要，按部署侧加密/受限存储策略另存并记录恢复所需版本。
manifest 包含 backup_id、UTC 时间、commit/digest、协议/schema、worker 身份和 root 路径、
数据文件 digest、各步骤完成状态。缺任一必需文件或备份尚有写入不能标为 complete。

DB 使用 PostgreSQL 官方 pg_dump/pg_restore，支持版本在文档固定；不用直接复制运行中的
PG 数据目录。pg_dump 自身的一致性不代表与 worker 文件同时一致，停写步骤不可省略。
保留策略仅删除已校验、超期且不被恢复引用的完整备份；不删除运行中的 worker 数据或 pending journal。

## 4. 恢复与远端事实

先保持旧集群/主机不可执行任务，在隔离测试环境恢复相同版本和配置，核验 DB/文件
manifest 及 role secret，再启动唯一 serve 和原身份 worker。旧 worker 存活未知时不得恢复
可派发的新副本。可连数据库不等于恢复成功。

当前 journal 使用路径/st_dev/st_ino，文件复制通常不能保留全部身份值。优先验证原 volume
回挂；如果备份恢复改变 identity，遵循 F4/F7 的可验证恢复机制或明确 blocked。不得修改
journal 字段绕过检查。如果完整活动 Flow 恢复需要新的身份迁移/管理员确认机制，拆出前置
feature；本计划保留未完成验收，不能拿“空库恢复成功”代替它。

GitHub 不是 DB 备份的一部分。备份后可能发生 push/merge/评论，恢复后先由现有 M4 对账和
result/effects marker 查实际远端事实，未知副作用保持阻塞；不能通过恢复旧 DB 撤销 merge
或重新发送已存在评论。密钥到期重新注入，不从 journal 恢复旧短期 capability。

## 5. 告警与演练

复用 F5 指标提供可配置的 Prometheus 规则及排查链接：inbox oldest>5m、blocked>0、
有待执行任务但无在线 worker>2m、恢复/归档积压>10m、采集快照陈旧>60s、备份失败或
距最后成功>26h。阈值为首版示例，按真实任务时长调整；并测试触发、恢复和无数据情况。
告警 channel 的接入由部署者配置，本 PR 不替用户发送通知。

演练矩阵至少包括：serve SIGTERM/SIGKILL、worker 短断连、执行进程状态未知、DB 暂停、
webhook 重投、终态 ACK 丢失、停止请求/确认间重启、容器/Pod 重建、备份损坏/缺文件、
恢复后的远端 head/merge 比备份更新。只在独立 fixture repo/DB/namespace 中执行。
每次报告版本、故障点、唯一执行证据、恢复耗时、重复副作用检查、通过/未通过/未覆盖。

## 6. 文件、步骤与完成标准

`docs/operations/runbook.md`、`backup-restore.md`、`upgrade.md`、`credentials.md`；
`deploy/monitoring/alerts.yml` 和 promtool 用例；`deploy/systemd/` 示例；
`scripts/smoke/recovery.*` 为测试环境显式操作入口，生产冷备首先交付可审计命令文档。
`scripts` 不内置生产连接串，不默认为 destructive 模式；保留测试资源供检查，不自动删生产卷。

先核对 F4–F7 实际接口 → 冷备恢复 runbook → manifest/边界测试 → 故障矩阵 → 告警规则 →
按三种部署方式执行可适用场景，汇总 M1–M5 真实证据。工具交付与恢复验收分开记录。
恢复验收必须证明唯一执行、无重复 PR/评论、终态一致、数据完整和不确定状态保守阻塞。
运行文档链接/配置检查、promtool 和必要脚本测试；产品变更另按标准 PG 回归验证。

预算目标约 1000 行手写增删；超过 1200 行拆常驻/告警与备份/故障验收。缺少实际运行环境
或 root identity 前置时明确剩余工作，不用模拟测试替代完整恢复证据。
依据：[PostgreSQL SQL dump](https://www.postgresql.org/docs/17/backup-dump.html)。

## 7. 实施与验证记录

2026-09-22：同一 PR #45 接续 #44 的 c72cd4d02707，实现 systemd 双角色示例、三种部署的
维护/冷备/升级/凭据轮换说明。没有部署 CLI、自动生产备份或本地每 Flow 容器。

先增加 manifest 拒绝缺件/损坏/未停写/越界/符号链接和 archive 最新任务回归，确认失败，
再实现脚本及新 gauge。脚本仅操作自己创建的 loopback fixture schema，保留证据与资源；
并未启动 serve/worker/模型或调用 GitHub。manifest hash 仅检测完整性，信任来自私有备份存储。
新增 archive_pending_age_seconds 供告警使用，排除被成功重试取代的历史失败；不更改旧指标口径。

10 条告警使用 Prometheus 3.5.1 的 promtool，通过官方资产 SHA256 后执行；12 组场景覆盖
触发/持续期/恢复、没有 worker 行、全部指标无数据。告警通知接收方与生产备份 gauge 需部署者接入。
CI 新增固定 PostgreSQL 17 镜像与同镜像客户端的实际恢复、systemd 语法和告警测试。

本地演练使用 PostgreSQL/pg_dump/pg_restore 16.2、协议 9，隔离 schema
`recovery_5d41ef6a337c4465bcd949c94c20d25b`，恢复耗时 0.053 秒（极小 fixture，不代表生产 RTO）。
活动 assignment/binding、inbox、HOME 数据恢复通过；原 journal 未变，复制 root 被身份校验阻塞。
本地 report 位于忽略目录 `.cache/2026-09-18/m5-f8/recovery-2/report.json`，只含合成数据。

| 故障 | 本轮证据与限制 |
| --- | --- |
| serve 正常停止、worker 短断连 | #42 真实双 worker 进程 smoke 通过；模型为替身，非生产故障验收 |
| serve SIGKILL / DB 暂停 | 未执行整套部署注入；readiness 单测不能替代 DB 故障的 runtime 验收 |
| webhook 重投、处理崩溃边界 | F2 持久 inbox PG 测试通过；真实 GitHub 重投未执行 |
| 终态/停止 ACK 丢失 | journal 与 F6 双向协议测试通过，含真实 Python 父子进程回收 |
| 执行进程未知/重启间隙 | F6 保守阻塞测试通过；完整主机断电未执行 |
| 容器/Pod 重建 | Docker CI idle worker 文件持久 smoke 通过；活动 journal/CSI/节点失联未执行 |
| 备份缺件/损坏 | manifest 单测通过；实际独立 PG dump/restore 与 root 阻塞演练通过 |
| 恢复后 GitHub head/merge 更新 | 仅前序 M4 模拟/PG 回归；真实远端故障矩阵未执行 |

RPO≤24h/RTO≤2h、完整活动 Flow 恢复、无重复 PR/评论、真实 systemd 主机及 K8s 目标 CSI
均未验收。保持 Draft；不会把 fixture 恢复耗时或未知旧执行视为可上线证明。

最终本地验证：789 项 PostgreSQL 测试通过（0 skipped）；Ruff、Mypy 130 文件、构建及
promtool 规则/单元测试通过。约 1,040 行增删，未超过计划的 1,200 行拆分阈值。
