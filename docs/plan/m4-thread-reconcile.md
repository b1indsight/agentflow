# M4 Feature 5/9：Review 线程持续对账

> 状态：用户已批准顺序独立实施并推送；实现完成，完整验证通过。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-thread-reconcile`；实现前置：F1、F3。
> 本 PR 以 F4 bookmark 为 base；按用户指示无需等待合并，独立 review 后由用户统一合并。

## 1. 目标与依赖

依赖 F1、F3。把已实现的 GraphQL 线程读取和 `observe_threads()` 接到持久扫描中，
补收漏失的行内评论/编辑，持续记录人工 resolved/unresolved/outdated 事实。
F4 处理普通讨论；两者可以独立验收，共用现有反馈版本和 settle 语义。

## 2. 方案

- 复用 `ReviewThread/ReviewThreads`、原 thread ID 映射与评论分页能力。
  审查现有读取上限；若不能续页，只增加必要 continuation，不能循环永远重读相同前 N 条。
- F3 的记录增加线程来源进度；每轮预算耗尽时保留 continuation 和 incomplete。
  只在完整观察后更新成功标记，嵌套 comments 未扫完的 thread 保持 incomplete。
- 查询前冻结本地 feedback item/version；事务内再次核对当前 Flow/attempt/PR/head，
  调用 F1 加固后的 observation 逻辑。并行 webhook 的较新版本保留，未消费新输入进入
  原有 settle，poll 本身不启动独立 Judge 或修订队列。
- 人工 resolved 的线程仍可包含新的人类评论，不能只查询 unresolved 线程。
  只更新远端观察事实；不把 resolved 当成“本地已处理”，也不把 unresolved 变更自动写回 GitHub。
- poll 不直接 reply、resolve 或请求 re-review；这些写操作继续经过 M2 的覆盖检查和 effect ledger。
  同一线程新版本使旧处理依据失效，不自动扩大已有 resolution receipt 的覆盖范围。
- 缺页、线程映射不全、权限错误、评论缺席都不能证明删除或完成处理。记录局限，
  等待后续观察或人工处理，保留原有保守 resolve 行为。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/github_threads.py`、`github_models.py`、`ports.py` | 必要的可续页查询契约 |
| `app/control/reconcile_feedback.py` 或小型线程扫描模块 | 扫描与观察接线 |
| `app/reconcile_store.py`、`review_store.py` | 进度和版本边界 |
| 新增 `tests/test_reconcile_threads.py`，既有 review/thread recovery 测试 | 漏投、人工操作与不完整快照 |

先写漏失行内评论和人工 resolved 后新增反馈测试，再补续页及观察接线，最后复用现有
回复/resolve 恢复测试验证没有重复远端写。若 F4 尚未合并，使用小型线程模块避免依赖其实现。

## 4. 验收

- 漏失评论/编辑最终入库一次，人工 resolved/unresolved/outdated 变更正确记录。
- resolved 线程内的新评论可进入下一批；旧线程处理依据不会覆盖新版本。
- 线程和嵌套评论跨页、页失败、重启后继续；不完整快照不得导致自动 resolve。
- poll 与 resolve 已发出、响应丢失、迟到 webhook 交错时不重复回复，也不自动 unresolve。
- 同秒回退满足 F1，已合并或旧 attempt 的观察不启动新的修订。
- PostgreSQL 线程对账和 M2 resolution/recovery 回归通过；真实线程矩阵单独留证。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 390、测试 480、文档 100，合计约
**970 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：自动 unresolve、删除评论推断和无限历史线程归档。未完成必需正确性验收时不能仅以行数达标合并。

实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

## 实现记录

在线程来源复用 F4 的有界调度/重试和版本序号，不新建 Judge 队列。每次一条 GraphQL 请求，
获取一个线程的至多 20 条评论；类型化 JSON continuation 分别保存外层和内层 cursor。
使用 [GitHub 64 位 comment ID 与 updatedAt](https://docs.github.com/en/graphql/reference/pulls#pullrequestreviewcomment)。
跨页 thread 片段始终 incomplete，即使最后一页读完也不伪装为完整覆盖；既有 M2 完整新鲜
读取与 effect ledger 仍拥有 resolve 权限。消失节点不会卡死后续线程，但整轮保持缺失诊断。
每页的反馈、人工状态与进度同事务提交；不推断删除或扩大原 resolution receipt。
真实 GitHub 线程操作矩阵尚未验收。

验证：先失败测试确认缺少分页契约；完整 PostgreSQL **590 passed / 0 skipped**，Ruff、Mypy、
`uv build --no-sources` 均通过。覆盖嵌套 cursor、重启/回滚、新版本竞争、缺页和人工状态。

## 2026-09-14 恢复问题返修

真实 SDK 对嵌套节点返回 null + NOT_FOUND 时，转换为不完整页并保留外层续页；
权限混合错误和非嵌套失败仍交回持久退避。真实 MockTransport 回归先失败后通过。
合并前序 F4 的 inert 命令版本逻辑到提取后的讨论转换函数，避免返修被重构覆盖。
原 low 的两套线程解析重构不在本次恢复修复范围。
