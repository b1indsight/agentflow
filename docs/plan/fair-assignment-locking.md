# 公平领取只锁请求方 worker

**阶段：** 固定顺序回归、实现、消融与全套验证完成；独立审查无发现，待用户评审。
**PR：** [#56](https://github.com/b1indsight/agentflow/pull/56)。
**初始基线：** `main` / `c401b206`；之后更新至已合入 PR #55 的 `89555ae1`。
**Bookmark：** `fix/fair-assignment-locking`。

## 1. 已批准的修复方向

### 问题与目标

PR #55 的 CI 暴露既有调度问题：worker-2 请求任务时，公平候选查询锁住 worker-1；
同时 worker-1 的查询使用 SKIP LOCKED 跳过自己，选到 worker-2。
双方候选均非请求方，双双返回 None，队列中仍有可领取任务。

目标是在固定的相同交错中让 worker-1 当轮领取，worker-2 返回 None；仍保持
任务唯一领取、worker 单槽、connection epoch fencing、drain 和 lifecycle affinity。
不依赖重跑 CI、sleep、重试次数或放宽为“至多一人成功”的测试断言。

### 实现与取舍

- 公平候选按原有 last_assigned_at / worker_id 顺序无锁读取；非候选立即返回。
- 只有候选对应的请求方才锁定自身 worker 行，使用 SKIP LOCKED 避免等待。
  锁定时重新校验 epoch、online、空闲槽位，以及 draining worker 已有未释放 binding。
- 随后仍在同一事务内锁 Flow/job、创建或复用 binding，提交 job 与 worker 状态。
  不新增全局锁或通用调度器，不改变数据库 schema 和外部协议。

候选读取只是建议，不允许直接沿用旧状态执行写入。两步之间重连、离线、drain、
另一个同 worker 请求领取，都必须被锁定阶段的当前状态校验拒绝。
若真正的优先候选被其他业务事务锁住，仍允许本轮无结果并由正常轮询继续；
本次消除的是非候选请求替其他 worker 加锁造成的交叉空转，不承诺无锁等待或调度时限。

### 测试、风险与消融

先改原有并发测试，用 Event 在候选 SELECT 返回后暂停一个事务，再让另一请求完成。
超时仅作失败保护，不用 sleep 猜测线程执行顺序。增加同 worker 两请求交错和候选选出后
epoch/offline/draining 状态变化测试。保留实际 PostgreSQL 锁和提交行为。

按固定输入分别恢复旧候选锁、删除槽位复查、删除 drain 复查，要求对应回归失败；
随后恢复实现，执行全套 PostgreSQL 测试、Ruff、Mypy、打包和独立代码审查。

## 2. 实施时间线

### 2026-09-30：固定失败顺序

先仅修改 `test_concurrent_stores_do_not_claim_the_same_job`，在 worker-2 选出候选之后
暂停，worker-1 尝试领取完成后再释放暂停。旧实现稳定返回 None，断言 winner 非空失败，
单用例运行约 0.08 秒。没有修改断言来接受双空结果。

### 2026-09-30：修复与定向验证

将候选 SELECT 改为无锁，现有 epoch 查询改为仅锁请求方并复查当前资格。
固定两类请求交错；覆盖候选读取后重连、离线与 drain 提交。Store、cancel/drain、
recovery decisions 回归 **43 passed**；Ruff、Mypy（138 源文件）通过。

### 2026-09-30：消融实验

本地脚本 `.cache/2026-09-30/fair-claim-ablation.py` 临时修改单个机制，运行对应 pytest，
并在 finally 恢复 app/store.py；产生日志均在同日缓存目录。

| 变体 | 固定回归 | 结果 |
| --- | --- | --- |
| 恢复候选查询 FOR UPDATE SKIP LOCKED | concurrent_stores[worker-2] | winner 为 None，失败 |
| 删除 online/current_job_id 复查 | concurrent_stores[worker-1] | 同一 worker 再领取 job 2，失败 |
| 删除 drain/binding 复查 | rechecks_worker[draining] | draining worker 领取新 Flow，失败 |

三项均以 pytest exit 1 结束，证明对应保护不能删除。已恢复完整实现，进行全套回归。

### 2026-09-30：全套验证

`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run --no-sync pytest -q --tb=short`
完成 **807 passed，0 skipped**（约 57 秒）；唯一警告为 Starlette TestClient 的 httpx 弃用提示。
Ruff、Mypy（138 源文件）、`uv build` 与 `git diff --check` 通过。

### 2026-09-30：独立审查与 PR

实现提交 `51accf9f9619` 通过工作流独立 Codex 审查，返回空 findings。
已推送 `fix/fair-assignment-locking` 并创建独立 PR #56；本条仅记录结果，不修改已审查代码。

### 2026-09-30：同步已合并的 PR #55

PR 创建后确认 main 已合入 #55，changelog 与计划索引的并行新增条目产生冲突，导致
pull_request 工作流未启动。已将修复变基到 `89555ae1`，保留两边文档记录；调度代码和
并发测试原样重放，PR diff 仍只包含调度修复。重新验证合并后的完整版本。

合并后的真实 PostgreSQL 全套 **808 passed，0 skipped**；Ruff、Mypy（138 源文件）、
`uv build` 与 diff 检查再次通过。新增的一个测试来自已合并的 #55，调度修复范围未扩大。
