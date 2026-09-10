# M4 Feature 6/9：未合并 PR 关闭与重新打开

> 状态：用户已批准顺序独立实施并推送；实现完成，完整验证通过。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-pr-lifecycle`；实现前置：F3。
> 本 PR 以 F5 bookmark 为 base；按用户指示无需等待合并，独立 review 后由用户统一合并。

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

实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

## 实现记录

复用 `DeliveryState.CLOSED` 和 `supersedes()` 贯穿领取、准备、最后派发、tool/effect dispatch 与结果收尾。
关闭/漂移取消旧队列与 batch，保留真实编辑；重开通过持久 `force_triage` 重新冻结，避免复用强信号
决策或伪造反馈已 relayed。新鲜查询检查原 PR/base/ref；只允许原 head 或经既有 revision/INITIAL
核验的冻结 worker 结果。状态、attempt、delivery version 和最终 coding job 再次核对后恢复。
PR 创建响应丢失后若发现已 closed，绑定经编号查询核验的身份，退休未发 effects 并完成原 ACK。
仍在执行或没有可靠结果的 executor 不被当成退出，不自动释放、kill 或采纳人工 push。
自动化已覆盖关闭/重开、真实 PostgreSQL、派发凭据竞态、in-flight revision、旧评论响应丢失、
INITIAL 最终提交前关闭和事务回滚；真实 GitHub close/reopen/归档 smoke 尚未验收。

验证：先失败测试后实施，完整 PostgreSQL **607 passed / 0 skipped**；Ruff、Mypy、
`uv build --no-sources` 均通过。17 个新增数据库生命周期用例，已有合并与结果恢复回归通过。

## 阻塞与恢复返修

- CLOSED 保留原执行绑定；失联和租约超时不会释放现场，原 Started 任务可以恢复连接。
- 重开时先退休关闭后重新入队的旧任务，再冻结当前反馈，避免陈旧任务重新执行。
- 对账提示持久化版本；远端查询期间新到的提示保留立即再检查的期限。
- 新增关闭后断联、三种重新入队和查询竞态的 6 项 PostgreSQL 回归；针对性 73 passed。

返修验证：PostgreSQL 完整套件 624 passed；Ruff、Mypy、build 通过。
