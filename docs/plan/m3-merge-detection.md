# M3 Feature 1/3：合并检测与后续任务收敛

> 状态：计划草案待审批，未实施。需求核对：2026-09-08。
> 基线：`main` / `81417c5b`；沿用 bookmark `feat/m3-merge-archive` 与 [PR #24](https://github.com/b1indsight/agentflow/pull/24)。

## 1. M3 拆分与复杂度边界

[路线图](../architecture/roadmap.md) 的 M3 按下面三个 feature 顺序实现、独立验收。
各 PR 都承载自己的计划及后续实现，不合并纯计划 PR 后另开实现 PR。
三份计划 PR 均以 main 为 base；F2/F3 实施前更新到已合并前置 feature 的 main。

| Feature | 可验收结果 | 依赖 | PR |
|---|---|---|---|
| F1 合并检测与任务收敛 | 合并事实不丢失，停止新修订，旧结果不覆盖合并状态 | 已合并 M2 | [#24](https://github.com/b1indsight/agentflow/pull/24) |
| F2 工作区清理与 worker 释放 | 原 worker 提供清理凭据，失败可恢复，完成后释放 | F1 | [#25](https://github.com/b1indsight/agentflow/pull/25) |
| F3 归档状态回写与闭环验收 | Issue/PR 标签和归档说明可恢复回写，完成真实 M3 验收 | F2 | [#26](https://github.com/b1indsight/agentflow/pull/26) |

三个 feature 共用的简化决定：

- 保留 Flow ACTIVE/SUCCEEDED 与 Delivery MERGED 的现有分轴，不新增归档生命周期状态机。
- 使用现有 jobs、FlowExecution、result inbox、ResultPlan/effect ledger、worker journal；
  不新增 `flow_archives` 表、归档专用存储层或通用恢复框架。
- 归档进度由 ARCHIVE job、清理 result 和 effects 推导；错误复用 job.last_error。
- 只完成正常 ACTIVE Flow 合并后的闭环。历史 FAILED/CANCELLED Flow 自动重激活、重新抢占
  已释放资源、跨 worker 清理、通用 reconcile、Project、未合并 close/reopen 留待后续。
- 不扩展自动 merge、进程 kill、共享 Codex 会话清理、远端 branch 删除或 LLM 合并总结。
- M2 的同秒旧 webhook 重放缺陷与真实验收继续单列，不借拆分宣称解决。

## 2. F1 目标与现有问题

用户合并关联 PR 后，合并事实与 ARCHIVE 入队应原子完成。重复 webhook 不新增工作；合并
时仍在执行的 INITIAL/TRIAGE/REVISION 可以交回结果，但不得再派生修订或覆盖 MERGED。

当前 router 先 `record_event()` 再调用 `on_merged()`，业务事务失败后重投可能被去重挡住；
service 只更新 delivery.state 和 enqueue ARCHIVE。其他任务/已冻结 ResultPlan 缺少统一的
合并状态检查。F1 只补这条 control 路径；worker 清理凭据与异常恢复由 F2 增强。

## 3. 方案

1. merge 事件走专用的事务入口：校验 `closed + merged=true`、repo/PR/installation 关联，
   在同一事务锁 Flow/delivery，去重、保存合并事实、递增 delivery.version、停掉 queued
   编码/review 工作及活动批次，并入队唯一有效 ARCHIVE。失败回滚后允许同 delivery 重投。
   不把 running job 的数据库取消当成进程退出；保留反馈历史，不把未处理内容设为 resolved。
2. FlowDelivery 只增加可空 `merged_at`、`merge_commit_sha` 两个事实字段。真实 merged payload
   缺少必需事实时拒绝推进并记录错误，允许修正后重投；不新增后台 API 补全队列。旧记录不
   回填虚假合并信息。支持 squash/rebase 的 merge SHA 与 PR head 不同。
3. 合并事实不受新 Flow 的 allowlist 或反馈 bot 过滤限制；未知 PR、身份不符、普通 close
   和 approve 不入队。已终态 Flow 只记录可匹配的合并事实，不重激活、不自动清理资源。
4. 对 scheduler/prepare、结果计划、effect claim 和最终提交增加 delivery 已合并检查。
   未派发的旧操作停止；旧结果保存执行事实并按既有 digest/ACK 完成收尾，不重开 PR、不
   派生修订、不写回 OPEN 或把正常归档 Flow 改成 FAILED。覆盖已开 PR 但 INITIAL 尚未完成。
5. 已发出的 GitHub 请求可能完成，沿用当前 receipt 查询/恢复后再收尾；不承诺远端原子取消。
   在旧 executor 退出、旧 result inbox 与已发 effects 收敛前不派 ARCHIVE。遇到无法确认的
   情况保留待处理和错误，不绕开既有单 worker 单槽约束。
6. `on_merged` 只在首次事务提交后调用，保持现有尽力调用/异常隔离；不新增持久插件投递。

F1 保留当前健康 worker 上的 ARCHIVE 执行路径，不升级 wire protocol、不增加标签/评论。
单独合并 F1 后可以验收合并入口和任务收敛；清理 crash recovery 仍明确等待 F2。

## 4. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/control/router.py`、`service.py`、`app/store.py` | 原子事件入口、唯一有效 ARCHIVE、领取前检查 |
| `app/models.py`、`app/store.py` | 合并事实字段及兼容旧数据的 schema 更新 |
| `app/review_store.py`、`control/coordinator.py` | 合并后停止新批次/任务，等待原执行收尾 |
| `app/control/results.py`、`effects.py` | 新旧 ResultPlan 的合并检查、迟到结果收尾与 ACK |

审批后先写 PostgreSQL 原子性与重放失败测试，再实现事件入口；然后补执行/结果交错测试并
接入合并检查，最后做完整回归。只提取复用所需的小函数，不新增 archival service 框架。

## 5. 验收

- 同 delivery 重投、不同 delivery 的重复 merge、并发 merge、事务中途异常：事实与 job
  同成同败，只有一个有效归档任务，重投可继续。
- 普通 close/approve、未知 PR、installation 不符不归档；合法关联合并不误用 bot/allowlist
  过滤；终态 Flow 不复活。merge/squash/rebase 信息准确保存。
- queued/running TRIAGE/REVISION、已开 PR 的 INITIAL、已有 frozen effects 时合并：
  无新修订、无状态倒退；旧执行/结果收尾之前不派发清理；合并后新反馈不创建批次。
- 合并前/后的 result 重放与 ACK：保留执行事实，停止未发副作用；已发操作恢复不重复。
- 健康 worker 的既有 archive 路径可继续；不把这项回归冒充 F2 的清理可靠性验收。

复用 router/service/coordinator/result recovery 测试，数据库原子性使用真实 PostgreSQL。
实现完成运行 Ruff、Mypy、完整 pytest（设置 `FLOWTOOL_TEST_DATABASE_URL`，数据库用例不跳过）
和 `uv build --no-sources`。本计划阶段仅检查文档差异与链接，未运行产品测试。
