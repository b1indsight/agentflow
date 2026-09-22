# 日志与内部指标

serve / worker 设置 `FLOWTOOL_LOG_FORMAT=json` 启用 JSON stdout，默认 text 也执行同样的字段过滤。
日志仅保留时间、级别、角色、受控 event/reason_code、代码位置、异常类型和有效的关联 ID。
不会渲染原始异常值/栈局部变量、消息正文、额外嵌套对象、prompt、HTTP URL、模型输出或 token。
旧日志调用未迁移的事件显示 `application_log / legacy_log`，通过 source 定位；不要依赖旧 message
全文告警。第三方 standard logging 和 Uvicorn 日志使用同一出口。CLI 自身命令结果仍是命令输出。

`delivery_id` 关联接收与应用；`flow_id/job_id/worker_id/epoch` 关联执行和结果处理。
executor 的 async task 在创建时继承独立上下文；assignment_token 不允许出现在日志字段中。
标识符也应限制日志访问权限，保留期由部署的日志系统配置。

## 启用 scrape

仅 serve 设置独立 `FLOWTOOL_METRICS_TOKEN`（不要复用 worker token），可选
`FLOWTOOL_METRICS_INTERVAL_SECONDS=15`，范围 5–300 秒。缺少或空 token 时 `/metrics` 返回 404，
缺少/错误的 `Authorization: Bearer <metrics token>` 返回 401。后台采集只在配置 token 后启动。
不在 URL query 放凭据；监控通过私有网络直连 serve:8000，公网 tunnel 仍只放行 webhook。

Prometheus scrape 使用 secret 文件（在监控进程中挂载）：

```yaml
scrape_configs:
  - job_name: flowtool
    scrape_interval: 15s
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /run/secrets/flowtool_metrics_token
    static_configs:
      - targets: ['serve:8000']
```

该例用于受控 Compose 网络；跨主机需部署内部 TLS，配置 https/CA 和实际主机名。
监控服务不需要 App key、worker token 或数据库密码。不得扩展 Cloudflare catch-all 来公开接口。

## 指标契约

全部以 `flowtool_` 为前缀；以下省略此前缀。ID、repo、URL、异常文本不进入 label。

| 指标 | 类型 / 标签 | 口径 |
| --- | --- | --- |
| webhook_requests_total | counter / status | 本进程 HTTP 结果；含重复投递，202 不等于业务处理完成 |
| inbox | gauge / state | pending、processing、done、blocked 的持久记录数 |
| jobs | gauge / kind,state | 全部保留 job 的排队、运行及终态记录数 |
| workers | gauge / state | 数据库登记状态，online/busy 不等于当前已确认空闲 |
| bindings_active | gauge | 未释放的 Flow-worker binding |
| archive_pending_age_seconds | gauge | 仍绑定的活动 Flow 最新未成功 archive job 的最老年龄；用于运维告警 |
| recovery_pending | gauge | 已收到结果但尚未应用的 job，包括被阻塞的恢复 |
| effects | gauge / state | pending/running/retry/uncertain/succeeded/failed/skipped 的外部 effect |
| projects | gauge / state | desired_version > synced_version，区分 pending/blocked |
| reconcile_due | gauge | 已到 next_at 的 PR 对账行，包含失败退避已到期 |
| oldest_age_seconds | gauge / queue | inbox/jobs/recovery/archive/reconcile/projects 的最老等待秒数；空队列为 0 |
| execution_receipts | gauge / kind | 保留数据中具有 started_at 和首次 result_received_at 的 job 数 |
| execution_elapsed_seconds | gauge / kind | 上述 job 从 started 到首次结果收到的非负秒数之和 |
| result_replay_observations_total | counter | 本进程验证通过且已应用结果的恢复/重复确认观察次数 |
| lease_expiry_transitions_total | counter / outcome | 本进程 sweep 实际转为 requeued/failed 的过期分配数 |
| metrics_snapshot_ready | gauge | 至少成功采集过一次为 1；首次失败时没有业务 gauge |
| metrics_snapshot_age_seconds | gauge | 距上次成功采集的秒数；从未成功则从进程启动计算 |
| metrics_snapshot_errors_total | counter | 本进程后台采集失败次数 |

kind 只允许 initial/triage/revision/answer/archive，历史未知值归入 other；状态 label 亦为有限集合。
执行统计使用持久 gauge 而非进程 Histogram：重复恢复不会增加记录，进程重启不丢历史；清理
job 数据会降低 gauge，不对其直接使用 counter 的 rate/increase。它衡量 serve 收到结果的时刻，
包含网络延迟，不能当作纯模型耗时。counter 在进程重启时归零，可用 rate/increase。
没有伪造未接线的 reconcile reason counter 或模型 token 指标。

## 故障诊断与采集隔离

scrape 只读内存快照，不访问数据库。后台每轮专用只读连接，不占 dispatcher 连接池，连接超时
2 秒，整个聚合 SQL statement 超时 2 秒；包括连接的一轮通常最多约 4 秒，DNS 解析仍受系统
resolver 约束。单次一个采集，不重叠。聚合是同一个数据库 statement 的一致视图。

失败保留上次成功值、增加 errors 并继续老化 age；先判断 ready/age，再判断 backlog 为零。
大数据库可能持续超过 SQL 预算：此时应评估索引/数据保留或采集架构，不通过取消查询预算绕过。
这不是单 serve 的容量扩展方案，不要启动多个 Uvicorn worker。

典型排查：inbox blocked 用 `flowtool inbox status`；recovery_pending/effects uncertain 查对应
job 和 effect 的恢复状态；workers offline 加 active binding 表示仍有执行现场，不表示可以释放；
archive failed 或 projects blocked 分别检查收尾和投影，不能用重建 worker 替代事实核对。
具体告警、保留和备份部署由 F8 提供。

验证包含并发上下文、standard logging/嵌套异常净化、默认关闭/独立鉴权、PG 积压与空队列、
重复快照不重复记执行、真实 pg_sleep 超时及旧快照保留。F3 外部真实闭环对照尚未运行。
实现依据：[Prometheus 自定义采集器](https://prometheus.github.io/client_python/collector/custom/)。

reconcile_due 与 reconcile 年龄仅包含调度器当前可选的持久行：Flow 未 succeeded、delivery
有 PR 且未 merged。cancelled/failed 仍可能需要对账，并非一律排除。标准 logging 保留原始
级别与代码来源；消息、参数和异常正文仍被过滤，不记录 pathname。
