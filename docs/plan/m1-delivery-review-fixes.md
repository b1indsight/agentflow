# M1 delivery review 修复计划

> 状态：已批准并实施，PostgreSQL CI 待验证 · 计划/实施日期：2026-09-06

## 目标

修复 M1 Issue→PR 合并 review 中直接影响交付闭环的三个问题：

1. Flow 失败并释放 execution 后，显式 retry 因 `flow_executions.flow_id` 主键冲突而无法重新分配；
2. `flow.open_pr` 在 INITIAL 仍运行时创建 GitHub PR，后续 Codex/VCS/终态校验失败会遗留已打开 PR；
3. GitHub lookup 返回 closed PR 时，当前校验会把它错误记录为 `DeliveryState.OPEN`。

本计划不处理 Revision capability、历史迁移修复或 push token broker；这些问题不属于本次三个 M1
delivery 修复，其中 token broker 已由 Issue #15 跟踪。

## 技术方案

### 1. 复用已释放的 execution binding

保持“一 Flow 一行 execution”的现有模型。`Store.assign_job()` 为无 active binding 的 Flow 分配 worker
时，用带条件的 upsert 代替无条件 insert：不存在时创建；同一 `flow_id` 已有且 `released_at` 非空时，
重置 worker、repo identity、绑定时间和 recovery 字段并重新激活。active 行不得被覆盖，worker/repo 的
partial unique constraints 继续承担并发防线。

### 2. 将 `flow.open_pr` 拆成意图和最终副作用

worker 运行期间的 `flow.open_pr` 只完成以下工作：

- 校验 assignment/attempt/execution fencing、参数和远端 branch SHA；
- 持久化 title、body、head SHA，并把 delivery 置为 `opening`；
- 完成 tool-call ledger，向 worker 返回可重放的 prepared acknowledgement。

它不调用 GitHub create PR、label 或 reviewer API。Coordinator 收到 exit code、session、最终 VCS outcome
和 tool result 均有效的 INITIAL terminal result 后，再调用 delivery owner 完成 GitHub 副作用：查找或创建
PR、验证远端快照、将 delivery 置为 `open`，最后设置 label/reviewer。若终态校验失败，Flow 失败但不会
产生 PR；若 control 在最终副作用中断，持久化 result 可重放，而 GitHub head lookup 和 tool ledger 保持
最终化过程幂等。

### 3. 只接受 open PR

PR snapshot 校验除 branch/base/head SHA 外，明确要求 `state == "open"`。closed PR 作为 conflict 拒绝，
不得更新 delivery 为 open，也不得发送 label/reviewer 请求。该规则同时覆盖已存在 PR lookup 和 create
冲突后的 lookup。

## 修改范围

- `app/store.py`：released execution 的条件 upsert；
- `app/control/tools.py`：prepared intent 与 terminal finalize 两阶段 API、open-state 校验；
- `app/control/coordinator.py`：INITIAL 终态校验通过后触发 finalize，并调整 delivery/tool-result invariant；
- `tests/test_store.py`：真实 PostgreSQL fail/release/retry/reassign 回归测试；
- `tests/test_flow_tools.py`：prepared/replay/finalize、closed PR 和副作用顺序测试；
- `tests/test_coordinator.py`：失败结果不创建 PR、成功结果才创建 PR 的协调器测试；
- M1 状态文档：记录修复结果和仍待执行的真实 App smoke/E2E。

## 实施顺序

1. 先增加三个最小回归场景，使其在当前实现上失败；
2. 实现 released execution 条件 upsert并通过 PostgreSQL retry 测试；
3. 重构 Flow tool delivery 为 prepare/finalize 两阶段，保持 call replay 和 result replay 幂等；
4. 加入 PR open-state invariant；
5. 运行定向测试，再运行 Ruff、Mypy、完整 pytest 和 build；
6. 通过代码 review gate 后更新同一 bookmark 和 PR，并记录尚需人工执行的 App smoke/E2E。

## 验收标准

- 同一失败 Flow retry 后能被任意可用 worker 重新绑定并执行，不发生主键冲突；
- Codex 在 `flow.open_pr` 后退出失败、遗漏必要结果或改变 VCS outcome 时，GitHub 不存在新 PR 副作用；
- 有效 terminal result 只创建或复用一张 open PR，重放不重复 create/label/reviewer；
- closed PR 不会被记录成 open delivery；
- 新增回归测试通过，Ruff、Mypy、完整 pytest 和 build 通过。

## 非目标与风险

- 不改变一 Flow 一 PR、一 worker 一 active Flow 和同 worker recovery affinity；
- 不扩展 M2 Revision/review loop 能力；
- 不在本次引入新的 durable outbox 表。finalize 依赖现有持久化 terminal result、GitHub head lookup 和
  幂等 delivery/tool-call 账本；若实现证明三者不足，再回到本计划更新设计而不是静默扩大范围。

## 实施结果

- released `flow_executions` 通过条件 upsert 重新激活，active binding 仍受原 partial unique indexes 保护；
- `flow.open_pr` 只持久化 prepared acknowledgement，Coordinator 在 INITIAL terminal result 校验后执行
  GitHub PR/label/reviewer 副作用；已 open delivery 的 terminal replay 直接复用持久化结果；
- PR snapshot 明确要求 state 为 `open`；closed lookup 保持 delivery=`opening` 并失败；
- 新增 fail/retry/reassign、失败 terminal 无 PR 副作用、prepared/finalize replay 和 closed PR 回归测试；
- 本地结果：Ruff、Mypy、uv lock、build 通过；pytest 97 passed、23 skipped。本机没有 PostgreSQL，相关
  场景（包括新增 retry 回归）由 PR CI 的 PostgreSQL service 验证。
