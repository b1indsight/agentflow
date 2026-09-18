# 计划文档

本目录保存功能与非小改动的计划、审批状态和实施结果。计划是决策记录，不代替 README 或架构文档
中的当前事实。

## 当前计划

- [`m4-pr-reconcile.md`](m4-pr-reconcile.md) — M4 F3：持久 PR 检查与漏失合并补偿已实施，548 项 PostgreSQL 回归通过；PR #30 以 F2 分支为基线；

- [`m4-recovery-concurrency.md`](m4-recovery-concurrency.md) — M4 F2：本地 sweep 隔离、有界结果恢复并发及公平续扫已实施，523 项 PostgreSQL 回归通过；PR #29 以 F1 分支为基线；

本节包含待审批及已实现、仍有验收待办的计划。M2 F1–F4 已全部合并；状态、证据和开放问题见 [M2 状态与待办](../architecture/README.md#m2-状态与待办)。

- [`m4-stage.md`](m4-stage.md) — M4 阶段计划：九个 feature，单 PR 预算 850–1080 行；已批准按序实现和推送，由用户统一 review/合并；
- [`m4-feedback-replay.md`](m4-feedback-replay.md) — M4 F1：同秒远端观察与迟到 webhook 的旧版本重放已修复，512 项 PostgreSQL 全量通过，待用户 review/合并；
- [`m3-merge-detection.md`](m3-merge-detection.md) — M3 F1：合并检测与后续任务收敛，已合并 [#24](https://github.com/b1indsight/agentflow/pull/24)；
- [`m3-workspace-archive.md`](m3-workspace-archive.md) — M3 F2：可核验工作区清理、原 worker 恢复与原子释放，已合并 [#25](https://github.com/b1indsight/agentflow/pull/25)；
- [`m3-archive-status.md`](m3-archive-status.md) — M3 F3：归档状态回写已合并 [#26](https://github.com/b1indsight/agentflow/pull/26)，真实闭环待验收；
- [`m2-feedback-batches.md`](m2-feedback-batches.md) — M2 F1：持久反馈批次与 settle，已实现并合并 [#19](https://github.com/b1indsight/agentflow/pull/19)；
- [`m2-semantic-triage.md`](m2-semantic-triage.md) — M2 F2：bub adapter、有界上下文与幂等答复，已实现并合并 [#20](https://github.com/b1indsight/agentflow/pull/20)，真实 provider smoke 待执行；
- [`m2-revision-delivery.md`](m2-revision-delivery.md) — M2 F3：修订交付核验与一次性 session 兜底，已实现并合并 [#21](https://github.com/b1indsight/agentflow/pull/21)，真实多轮 smoke 待验收；
- [`m2-review-resolution.md`](m2-review-resolution.md) — M2 F4：原线程回复、保守 resolve 与持久恢复，已实现并合并 [#22](https://github.com/b1indsight/agentflow/pull/22)；真实矩阵待验收，同秒重复 webhook 重放已在 M4 F1 修复；
- [`atomic-workflow-results.md`](atomic-workflow-results.md) — 已批准并实施：原子结果提交与 GitHub 操作恢复；本机 118 项通过，30 项 PostgreSQL 用例按要求跳过；
- [`m1-github-capabilities.md`](m1-github-capabilities.md) — M1 Feature 2/3：GitHub App、短时 push capability 与 PR primitives 已实现并通过自动化，真实 App smoke 待执行；
- [`m1-issue-to-pr.md`](m1-issue-to-pr.md) — M1 Feature 3/3：Flow 生命周期独占 worker/repo、Flow tool RPC、CodeAgent commit/push、远端验证与一 Flow 一 PR 已实现并通过自动化，真实 E2E 待执行；

## 已完成计划

- [`m1-delivery-review-fixes.md`](m1-delivery-review-fixes.md) — failed Flow retry、INITIAL 成功前提前开 PR 和 closed PR 误判已修复并通过 PostgreSQL CI；
- [`m1-code-agent-vcs.md`](m1-code-agent-vcs.md) — M1 Feature 1/3：Flow repo、CodeAgent 自主 Git/jj commit/push 与 worker 事实核验，已实施并验证；
- [`worker-connection-recovery.md`](worker-connection-recovery.md) — Feature 3/3：忙碌重连、持久租约、serve 重启恢复和结果重放，已实施并验证；
- [`multi-worker-scheduling.md`](multi-worker-scheduling.md) — Feature 2/3：多 worker 公平调度、attempt affinity 和单机多实例隔离，已实施并验证；
- [`control-worker-separation.md`](control-worker-separation.md) — Feature 1/3：`serve` 与一个远程 worker 的完整 WebSocket 执行闭环，已实施并验证；
- [`pydantic-store-models.md`](pydantic-store-models.md) — 保留 Psycopg 原生 SQL，以 Pydantic 模型和 `class_row()` 取代手工行转换，已实施并验证；
- [`postgresql-store.md`](postgresql-store.md) — 以 PostgreSQL 替换 SQLite，保持现有单进程编排结构不变，已实施并验证；
- [`github-actions-ci.md`](github-actions-ci.md) — Ruff、Mypy、pytest 与 uv build 的首版 GitHub Actions CI，已实施并验证；
- [`documentation-refresh.md`](documentation-refresh.md) — 项目文档目录化与完整同步，已实施；
- [`../UV-BUILD-MIGRATION-PLAN.md`](../UV-BUILD-MIGRATION-PLAN.md) — 构建后端迁移到 `uv_build`，已实施；
- [`m0.md`](m0.md) — M0 落地计划，已验收；
- [`m0-closeout.md`](m0-closeout.md) — M0 收尾与验收计划，已实施。
