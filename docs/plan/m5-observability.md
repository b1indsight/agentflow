# M5 F5：结构化日志与基础指标

> 状态：已批准，日志、指标及验证已实现，待独立审查。2026-09-19。
> 文档 PR 基线 main@origin / 55313e1f；接入依赖 F2，真实验证依赖 F3。
> M4 reconcile/Projects 指标在相应实现集成后接线；当前 main 不具备的路径不得伪造指标。
> 阶段范围：[PR #38](https://github.com/b1indsight/agentflow/pull/38)。

## 1. 结果与边界

现有 loguru 日志可看到局部错误，但缺少跨 ingress/job/worker/result 的统一关联和积压指标。
本 feature 提供结构化 JSON 日志与内部 Prometheus scrape，回答“任务在哪一步、等了多久、
恢复是否停滞”。不新增 Web 控制台、日志检索服务或通用 tracing 框架；告警部署留给 F8。

## 2. 日志契约

统一 timestamp、level、role、event、reason_code；按上下文可带 delivery_id、flow_id、job_id、
worker_id、epoch 和协议版本。assignment_token 是认证凭据，不当作关联 ID 写日志；需要时
使用不可还原的短 digest，默认通过 job/epoch 关联。result/context digest 只在必要诊断中输出。

用 loguru bind/contextualize 在任务边界绑定上下文，保证 async 并发不会串 Flow；所有模式
先经过字段白名单与异常净化。默认保持可读日志，可显式选择 JSON；stdout 由部署环境收集。
不输出 payload、prompt、模型完整输出、原始异常请求体、URL 查询凭据、token 或 DB 连接串。
以受控 reason_code + 异常类表示错误，需调试的私有原始日志另行操作，不打开万能 debug 泄露开关。

## 3. 指标与采集语义

| 指标族（拟定） | 语义与标签 |
|---|---|
| webhook_received_total / rejected_total | 本进程接收结果；有限 status/reason；同 ID 重投可计请求次数但不等于业务次数 |
| inbox_pending / blocked / oldest_age_seconds | PostgreSQL 持久事实，每次快照聚合，不把进程计数冒充队列状态 |
| jobs_by_state / oldest_queued_age_seconds | 有限 kind/state；completed 与失败的计数口径记录清楚 |
| job_execution_seconds | 从已确认 started 到首次持久 terminal 的时长；仅实际首次接收计一次，重放不再计 |
| workers_connected / bindings_active | 聚合在线与占用；不以短暂连接为 worker 空闲证明 |
| lease_expired_total / result_replay_total | 本进程观察次数；命名与文档明确不是“去重后的历史事件总数” |
| recovery_pending / oldest_age_seconds | result/effects 待恢复；包含 blocked 数量，空队列年龄为 0 |
| reconcile / archive / projects | 队列/延迟和有限 reason 的失败计数，按已集成能力分阶段启用 |
| metrics_snapshot_age_seconds / snapshot_errors_total | 采集健康度；失败不清零业务 gauge 假装恢复 |

指标不含 repo、Flow/job/delivery ID、URL 或异常文本；这类维度留在日志。
采用 `prometheus_client`（版本在实施时锁定），单 serve 进程，不启用 uvicorn 多 worker。
先只暴露 serve 聚合指标；不为 worker 增开公共 HTTP 服务或复制全部运行协议。

内部 `/metrics` 使用独立配置的 bearer secret，默认关闭；Cloudflare 模板继续只允许 webhook。
不复用 worker token；文档示例按监控侧注入 scrape 认证。DB 聚合通过后台有界快照，默认
15 秒刷新、单次最多 2 秒、只读查询；失败保留旧值并更新采集错误/陈旧度，scrape 不扫描全库。
计数器进程重启可重置，仪表盘使用 rate/increase；持久 backlog 不依赖计数器累积。

## 4. 文件与实施顺序

- `app/observability.py`：日志配置/字段净化、指标 registry，不导入业务 Store 全局单例。
- `app/metrics_store.py`：有索引/有界聚合快照；`config.py` 定义有限参数。
- `app/control/api.py`、`runtime.py`：内部 scrape、快照生命周期/超时。
- router/gateway/coordinator/results 与 worker 日志边界：统一事件和上下文，业务行为不变。
- `tests/test_observability.py`、PG 快照测试、并发上下文测试；`docs/operations/observability.md`。

先测试 secret/上下文泄露边界 → 日志契约 → 指标和 PG 聚合 → endpoint/权限 → 故障场景
验证可诊断性；用 F3 已知真实流程对照 ID 和延迟，随后交给 F8 编写告警规则。

## 5. 验收与预算

并发两个 Flow 的上下文不串；token/API key/DSN/嵌套异常脱敏；scrape 缺少认证拒绝；
事件重放不重复计业务耗时；DB 失败不输出假零值；快照超时不阻塞 lease/result；空队列和重启
语义正确。模拟积压、离线、归档失败能用日志和指标解释，不能只断言字段存在。
完成 Ruff/Mypy、必要 PG 回归和构建；指标标签集合/指标名写入文档作为稳定契约。

目标约 950 行手写增删；超过 1200 行可拆日志与指标，不通过删掉脱敏/采集故障测试压缩。
官方依据：[Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/)、
[标签命名与基数](https://prometheus.io/docs/practices/naming/)。

## 实施记录

见 [运行说明与稳定指标契约](../operations/observability.md)。执行耗时采用保留 job 的
持久 gauge 总数/总时长，避免进程计数在重放和重启时重复或丢失；不宣称纯模型延迟。
专用连接有 2 秒连接预算，加一条 2 秒聚合 SQL，scrape 只读缓存；这一轮预算相较计划
明确拆分为连接与 SQL 两段，系统 DNS 仍由 resolver 限制。真实 F3 对照待环境。

CI 后续验证：进程冒烟原本匹配旧日志全文，已改为 `event=worker_connected`。
本机隔离 PG schema 中实际双 worker 启动、serve 重启后双方重新注册和独立退出均通过。
此验证修正不包含独立审查报告中的对账候选过滤/第三方日志来源两项返修。
