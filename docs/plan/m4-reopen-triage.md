# M4 收尾：重开反馈分诊与轮询恢复回归

> 状态：用户已批准并实施；自动化验证完成，待代码审查及用户合并。日期：2026-09-18。
> 基线：`feat/m4-projects-api@origin` / `4e1b9e71`，包含 M4 F1–F9。
> bookmark：`fix/m4-reopen-triage`；本计划与实现使用同一 PR。

## 1. 当前事实

- GitHub PR #28–#36 均已合并，但 #29–#36 合入的是前项 feature 分支。
  当前 `main@origin` 为 `55313e1f`，仅包含 F1；不能把 PR 的 MERGED 状态等同于主干集成完成。
- 本地旧 M3 工作区与已合并 #27 的文件内容一致，原 jj change 保留。
- F9 的记录为真实 PostgreSQL 719 passed，外部 GitHub App/worker/Projects 矩阵尚未验收。
- 原阶段计划明确保留了重开后多批反馈绕过分诊的 Medium 和 OPEN 轮询跨重启覆盖缺口。

## 2. 问题与目标行为

`ReviewStore.reopened()` 设置单个 `force_triage` 标记；`_claim()` 在领取第一批时清除它。
当重开时保留的反馈超过 `MAX_DISCUSSIONS`，后续批次可能凭旧 request_changes 信号直接
创建 REVISION，绕过重新分诊。空正文批次同样不能提前解除保护。

目标：重开时已有的待处理反馈，无论分页、批次、WAIT 或 serve 重启，都不能沿用旧强信号
直接驱动修订；Judge 仍可基于新冻结上下文决定 revise。之后真正新到的有效强信号继续遵守
现有授权规则，不把整个 Flow 永久降级成强制分诊。

## 3. 实现方向与边界

- 在现有 Flow 事务锁内持久退休重开时的旧强信号授权，或记录等价的版本边界；优先复用
  `review_versions`，必要时仅增加一个授权失效字段。不得伪造 consumed/relayed 时间，
  不得更改反馈正文、删除反馈或把旧版本标成已经处理。
- `_has_unused_signal()` 和 `_claim()` 共用授权判定；新内容版本与新提交按既有规则授权，
  重复 webhook/远端观察不能重新激活已退休授权。多个 close/reopen 周期保持幂等。
- 保留首次重开的强制 triage 语义，并明确后续批次如何受到版本级授权退休保护。
- 不改变批次上限、worker 协议、Flow 状态或并发模型；不做线程解析重构。
- 为普通 OPEN PR 增加 PostgreSQL 跨 Store 重启轮询回归，验证持久 next_at、到期后
  再查询、失败退避及单 Flow 隔离；只有测试证明存在缺陷时才做相应最小修复。

## 4. 文件与实施顺序

1. `tests/test_pr_lifecycle.py` 或独立 `tests/test_reopen_triage.py`：先复现超批次旧强信号
   绕过 Judge，包含关闭期间编辑、空正文、WAIT、重启与真正新强信号对照。
2. `app/review_store.py`：在授权所属持久层完成版本级退休和查询规则；仅在必要时调整
   `app/control/lifecycle.py` 接线，不建立另一套反馈队列。
3. `tests/test_reconcile.py`：补 OPEN 正常周期与失败退避的跨 Store 回归。
4. 更新本计划、阶段待办和 CHANGELOG，记录实际证据和保留的外部验收项。

## 5. 验证与交付

- 使用实际 PostgreSQL，固定时间与模拟远端；先观察新增回归失败，再实现并跑相关测试。
- 全量执行 Ruff、Mypy、pytest（数据库用例不可跳过）、`uv build --no-sources`。
- 通过仓库 code review gate 后推送同一 PR，不自动合并。
- 本 PR base 使用包含全部 M4 的 `feat/m4-projects-api`，只呈现本次修复增量；不重复提交
  已有 F2–F9。主干集成须另以完整实现栈为输入核对 diff/冲突/CI，不能仅合入 F1 分支
  就宣布 M4 已进入 main。用户仍统一 review/合并。
- 外部验收继续按 `m4-stage.md` 第 5 节执行；需可用且获授权的测试仓库、App、worker
  和组织 Project。缺少这些条件时记录未验收，不以替身测试代替。

## 6. 实施与验证

- `review_versions.signal_retired` 持久记录版本的强信号授权失效；在重开事务内退休该 Flow
  已有 request_changes 授权。统一查询排除已退休版本，保留首次强制 triage。
- 不改正文或 consumed/relayed 记录；同版本 webhook/轮询重复观察不会恢复授权。
  真正的新反馈、新内容版本继续使用既有授权规则，再次 close/reopen 会退休当时的授权。
- 新增六项分诊回归：超过 60 条积压完成/WAIT 后重启、重复通知与新 Review/编辑版本、
  空正文和事务回滚。最初五项在旧实现均失败，修复后通过。
- 新增 OPEN 正常轮询→重启→限流→再次重启→恢复的持久 deadline 回归；轮询产品代码无需修改。
- 实际 PostgreSQL 全量 **726 passed / 0 skipped**；Ruff、Mypy（115 个源文件）和
  `uv build --no-sources` 通过。保留既有 Starlette 弃用警告。
- 本次不追溯改写升级前已经产生的批次；新规则在修复版本执行重开事务时应用。
  真实外部验收、完整 M4 汇入 main 和线程解析重复的 low 仍待后续处理。
