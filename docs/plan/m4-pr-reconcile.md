# M4 Feature 3/9：持久 PR 对账与漏失合并补偿

> 状态：用户已批准按序实现并推送；F3 已实施并通过 PostgreSQL 全量回归。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-pr-reconcile`；实现前置：最新 main；建议 F2 后实施，PR 合并事实路径可独立设计。
> 用户统一 review/合并；本 PR 以 F2 分支为 base，只展示 F3 增量，不等待前项合并。

## 1. 目标与边界

让目前只打印日志的 Reconciler 定时读取已追踪 PR，在合并 webhook 漏失时进入 M3 已有归档链。
本项只补合并事实；普通 close/reopen 由 F6 处理，反馈由 F4/F5 处理。不根据全仓库标签发现新 Flow。

## 2. 方案

- 新增最小 `flow_reconcile` 持久记录：Flow 标识、下次检查时间、最后成功时间、
  失败次数和脱敏错误；按到期时间/Flow ID 有界选取。记录可由已有 PR 的 Flow 幂等补建。
- 默认间隔 300 秒、小批次且有限并发；单 serve 内同一 Flow 不重叠扫描。
  周期成功后推进检查时间；崩溃在提交前则允许重复读取，不能跳过未应用事实。
- GitHub port 增加按 PR 编号查询完整生命周期事实的小方法：repo/number、state、merged、
  merged_at、merge_commit_sha、head/base 标识。复用 App 认证和错误类型，不用模糊 branch 搜索。
- 网络调用在事务外；冻结 Flow/current attempt/delivery version，提交时加原有 Flow 锁
  重新检查关联。合并事实不可回退；较旧查询不得覆盖已提交的合并或新 attempt。
- 提取 M3 `on_merged` 内部的合并事实用例，webhook 与 poll 共用；webhook delivery 去重
  留在入口，poll 不伪造 webhook payload/随机 delivery ID。事实、任务收敛和唯一 ARCHIVE
  同事务提交；保留原 worker、清理凭据和 GitHub effects 的恢复约束。
- ACTIVE 且有 PR 的 Flow 持续扫描；FAILED/CANCELLED 且 delivery 尚未 MERGED 的关联 PR
  也可低频确认合并事实，但不重激活或自动清理。SUCCEEDED/MERGED 不重复扫描。
- 429/临时故障持久退避，遵守 Retry-After；401/403/404 显示阻塞并低频探测，不能推断已关闭
  或已删除。一个 Flow 错误不终止整轮。`flow show` 显示最后成功、下次检查及错误。
- head/base 的外部漂移可记录诊断，但不得更新已验证交付 SHA、自动接受人工 push 或自动 rebase。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/control/reconcile.py`、`runtime.py` | 有界 poll 与运行接线 |
| `app/store.py`、新增 `app/reconcile_store.py` | 到期记录及事务应用 |
| `app/control/service.py` | 共享合并事实用例 |
| `app/ports.py`、`github_models.py`、`github_client.py` | 按编号查 PR 事实 |
| `app/config.py`、`cli.py` | 间隔、批次及诊断 |
| 新增 `tests/test_reconcile.py`，既有 merge/client 测试 | 漏投、重启与并发合并 |

先写漏投合并与提交前崩溃测试，再接 port/store，最后复用 M3 用例并接入 runtime。
只新增支撑这个闭环的存储模块，不构建可插拔 reconcile 框架。

## 4. 验收

- 不发送 closed webhook，poll 仍准确记录 squash/rebase merge SHA，并只入队一次 ARCHIVE。
- webhook 与 poll 同时到达、重复 poll、重启、事务故障不重复归档、不释放错误 worker。
- 旧结果、旧 PR/attempt 快照不能回退 MERGED；历史终态只更新可确认事实。
- 分批公平、失败隔离、Retry-After 跨重启有效；权限/查询错误不会被误判为合并。
- 使用真实 PostgreSQL 与 GitHub 替身；M3 真实归档验收仍单独记录。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 480、测试 430、文档 100，合计约
**1010 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：跨仓库发现新 Flow、完整历史回填和自适应 API 配额调度。未完成必需正确性验收时不能仅以行数达标合并。

本次规划仅做文档内容、链接和 diff 检查。实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

## 实施记录（2026-09-09）

- 新增 `flow_reconcile` 的到期时间、成功时间、失败次数和脱敏诊断；按有界批次补建和扫描
  已追踪 PR。失败/取消的历史 Flow 可确认合并事实，不能自动重激活或启动归档。
- PR 按编号读取并验证 repo/head repo 与合并事实，事务外 I/O，提交时复核 attempt、
  lifecycle、installation 与 delivery version。过期快照只延后复查。
- webhook 和 poll 共用 `ControlService.record_merge`；合并事实、任务收敛、ARCHIVE 与
  poll 成功时间同事务提交，提交后再通知插件与 worker。poll 不伪造 webhook delivery ID。
- `FLOWTOOL_RECONCILE_INTERVAL_SECONDS` 默认 300，批次默认 64、并发默认 4；
  永久读取错误低频探测，临时错误持久退避；共用 `github_retry.retry_delay` 保持 effects 的服务端等待语义。
- `flow show` 展示检查时间/错误；普通 close 和 head/base 漂移只记诊断，不修改已核验交付。
- 实际 PostgreSQL 完整回归 548 passed / 0 skipped；涵盖原 M3 合并与归档路径。
  Ruff、Mypy 和 sdist/wheel 构建通过，真实 App smoke 待执行。
