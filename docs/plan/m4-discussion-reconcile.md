# M4 Feature 4/9：Issue/PR 讨论与 Review 正文对账

> 状态：用户已批准顺序独立实施并推送；实现完成，完整验证通过。
> 规划日期：2026-09-09；基线：`main@origin` / `be65878f`（归档恢复修复 #27 已合并）。
> bookmark：`feat/m4-discussion-reconcile`；实现前置：F1、F3。
> 本 PR 以 F3 bookmark 为 base；按用户指示无需等待合并，独立 review 后由用户统一合并。

## 1. 目标与依赖

依赖 F1 反馈判重、F3 持久 poll。补收已追踪 Flow 的原 Issue 评论、PR 普通评论和 review 正文，
让漏失 webhook 的有效反馈进入现有 settle/Judge 路径。行内线程属于 F5。

## 2. 方案

- 保留既有上下文构建 `list_pull_request_discussions()` 的截断语义；新增或复用明确返回
  cursor/complete 的分页读取契约，不能把供 prompt 使用的前 N 条当成完整对账结果。
- 在 F3 存储中加入三个明确来源的进度（Issue comments、PR comments、reviews）。
  每页 observations 和下一页进度同事务提交；中断可从未提交页重读。定期重新扫描以捕获编辑，
  不能只用最大 ID 或 submitted_at 判断 review 正文是否变化。
- 评论支持 updated 时间筛选时使用重叠窗口，并保留对象 ID/版本去重；不支持筛选的来源
  有界分页完整遍历。达到预算记录未完成和 continuation，不能跳过剩余页或标记本轮成功。
- 回写前检查 Flow/attempt/delivery 仍适用，以查询前冻结的 item/version 防止慢查询覆盖
  较新 webhook。远端观察走 ReviewStore 共享版本逻辑，不伪装成新 webhook。
- 过滤 bot，保留作者与 review 状态；远端状态不提供原 webhook sender，故不补造 allowlist
  强信号授权，默认走普通 discussion/Judge。approved/dismissed 只撤销原有效反馈。
- 对账内容中出现 `/flow stop` 或其他命令时只记录诊断，不追溯执行旧命令。
  原 Issue 与 PR 的匹配分别校验，不能把任意仓库评论挂到当前 Flow。
- 明确只补可读取的创建/编辑与 review 状态。页面缺席不证明删除，删除漏投暂不自动推断；
  缺失或不完整反馈不允许形成新的 resolve 依据，人工/现有 webhook 路径仍可处理删除。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/ports.py`、`github_models.py`、`github_client.py` | 分页观察契约 |
| 新增 `app/control/reconcile_feedback.py` | 三个来源的标准化与有界轮询 |
| `app/reconcile_store.py`、`review_store.py` | 来源进度、原子观察与 settle |
| 新增 `tests/test_reconcile_feedback.py`，既有 client/batch 测试 | 漏投、分页、授权和重放 |

先验证页中途故障后可恢复，再实现三种来源映射和事务写入；最后接入 F3 的扫描调度。
source 状态只表达分页进度，不引入第二套反馈模型或 Judge 队列。

## 4. 验收

- 分别漏掉 Issue 评论、PR 评论和 review submitted/edited，最终各形成一个正确的新版本。
- 超过单轮预算的评论仍能全部扫到；第二页失败/提交前重启不丢页、不重复唤醒。
- 与同秒 webhook 编辑、真实正文回退交错仍满足 F1；稳定内容重复扫描不产生新批次。
- bot、approved/dismissed、未授权作者和旧命令不会触发强信号直达修订或控制命令。
- 人工已删除但列表缺席的评论不被伪造 tombstone；不完整扫描保留诊断。
- 执行新对账测试与 M2 feedback/triage 回归，再完成阶段质量检查。

## 规模预算与拆分触发点

按相对合并基线的新增行 + 删除行估算：产品 440、测试 450、文档 100，合计约
**990 行**。数字是规划预算，不是已实现统计；共享测试替身、schema、格式调整也计入。
目标 800–1100 行，预计超过 1200 行先缩小/拆出独立可验收 feature，不压缩测试或代码行数凑数。
优先另拆：自动执行漏失的 stop/trigger 命令、全文历史导入和删除推断。未完成必需正确性验收时不能仅以行数达标合并。

实施按“先失败测试→最小实现→针对性验证→完整检查”
推进；完整检查为实际 PostgreSQL 下 pytest、Ruff、Mypy 和 `uv build --no-sources`。

## 实现记录

三个来源共享 `feedback_reconcile` 调度表，每页写入与 cursor 原子提交。每条不可变版本增加
全局递增 `observation_seq`；查询前在 Flow 锁内冻结边界，因此不必载入整个历史来保护新 webhook。
新增完整正文分页接口，REST 读评论，GraphQL 的 `updatedAt`/`fullDatabaseId` 读 review 编辑与 64 位 ID。
这些周期读取禁用 SDK 自动重试，由持久 deadline 处理错误。现有 prompt 截断接口保持原语义。
已覆盖三来源漏投、第二页失败/重启、事务回滚、同秒竞争/删除、强信号保留、历史命令过滤、
限流和池取消；自动化不代表真实 GitHub App/Codex/bub E2E 已验收。

验证：先失败测试确认缺少新模块；完整 PostgreSQL **570 passed / 0 skipped**，Ruff、Mypy、
`uv build --no-sources` 均通过。现存 Starlette 弃用 warning 与本变更无关。

## 2026-09-14 恢复问题返修

批次最多冻结 60 条并优先交付未 relay 的版本；完成后自动唤醒未包含的反馈，WAIT 不再把
整个积压锁在超限批次里。已观察 review 可由真实授权 submitted webhook 升级信号；
观察补齐前驱后同事务重放唯一合法后继。命令编辑以持久 inert 版本失效旧反馈，后续编辑
仍可恢复；inert 仅用于持久化，不增加 worker wire 字段。新增 5 个回归先失败后通过，
相关反馈/批次/版本回归共 82 passed。
