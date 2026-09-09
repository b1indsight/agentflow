# M4 Feature 6/9：未合并 PR 关闭与重新打开

> 状态：计划待审批；本 PR 只含文档，尚未实施。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-pr-lifecycle`；实现前置：F3。
> 一 feature = 一 bookmark = 一 PR；前置合并后更新到最新 main，在本 PR 实施，不先合并纯计划。

## 1. 目标与依赖

依赖 F3 的按编号 PR 事实读取和共享入口。用户关闭未合并 PR 后停止新的编码/反馈交付；
重新打开同一 PR 后，以新鲜上下文继续。webhook 和 poll 得到相同结果。

## 2. 状态与竞态

- 使用已有 `DeliveryState.CLOSED/OPEN/MERGED`；ACTIVE+CLOSED 表示暂挂，保留原 binding、
  repo 和 attempt。普通关闭不等于 M3 归档，不写 SUCCEEDED，不自动释放 worker。
- 把 closed(merged=false)、reopened、synchronize 作为提前触发新鲜查询的提示；
  webhook 缺少可靠版本排序，不直接用迟到 close payload 覆盖已 reopen 的事实。
  MERGED 永不回退；所有提交复核 Flow/attempt/delivery version。
- CLOSED 时阻止新的 INITIAL/TRIAGE/REVISION 派发和新的 GitHub 写；排队旧任务及批次按
  已有收敛机制退出，但保留未消费反馈。仍在执行的 job 只保存执行事实并按原 ACK 协议收尾，
  不能用其旧结果回开 PR、推进已过期批次或改变 CLOSED。
- 已发送请求仍由原 ledger 核对结果；不会声称能撤回。关闭状态的暂挂不会授权 worker kill，
  也不会把未知 executor 当成已经退出；旧执行/未决 effects 未收敛前不恢复派发。
- reopen 必须是同一 repo/PR/base 和可接受的当前 head；重新查询且通过交付核验后恢复 OPEN，
  用未消费反馈重新冻结上下文，不重用关闭前的 Judge 决策或旧任务。
- head 已被人工 push 或 base 改动时，只显示需要人工处理并继续阻止旧任务；不改写已核验
  delivery SHA。正常 agent 的已冻结在途交付先走原结果核验，不能仅凭 SHA 暂时不同判成人工漂移。
- 如果关闭发生在 INITIAL 最终提交之前，旧 INITIAL 仍需收敛；无法证明完整交付时显示明确阻塞，
  不自动新建 PR 或启动第二轮 INITIAL。已终态 Flow 不因 reopen 自动重激活。
- `flow retry` 拒绝绕过 CLOSED、未决结果或 head 漂移；`flow show` 说明暂挂原因和恢复条件。
  长期关闭会继续占用 slot；本项不解决取消后的进程回收和跨 worker 清理。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `control/router.py`、`reconcile.py`、`service.py` | 查询提示及共享生命周期应用 |
| `models.py`、`store.py`、`review_store.py` | 既有 delivery 分轴和排队批次收敛 |
| `control/coordinator.py`、`results.py`、`effects.py` | CLOSED 和旧版本结果边界 |
| `cli.py`，新增 `tests/test_pr_lifecycle.py` | 暂挂诊断和生命周期矩阵 |

先用 PostgreSQL 测试 close 与 prepare/result/effect 的交错，再接入关闭状态；
之后实现新鲜查询驱动的 reopen，最后检查合并与归档回归。只抽取生命周期必需的共享谓词。

## 4. 验收

- 普通 close、重复 close、漏失 webhook 均暂挂，PR 不被重开且 worker 不被提前释放。
- close 与 INITIAL 最终结果、REVISION push、已发评论交错，不产生错误新交付。
- reopen 后等旧 executor/effects 收敛，符合基线才恢复；反馈不丢、不复用旧 decision。
- 延迟 close 查询不能盖过已提交 reopen/MERGED；head/base 漂移保持可诊断阻塞。
- 历史终态 reopen、CLOSED 下显式 retry、权限错误均不绕过约束。
- 自动化通过后仍需一次真实 close/reopen 与合并归档 smoke。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 520、测试 450、文档 110，合计约
**1080 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：人工 push 接纳、自动 rebase、自动清理关闭 PR 的 repo 和 executor kill。未完成必需正确性验收时不能仅以行数达标合并。

本次规划仅做文档内容、链接和 diff 检查。实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

