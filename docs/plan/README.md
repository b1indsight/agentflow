# 计划文档

本目录保存功能与非小改动的计划、审批状态和实施结果。计划是决策记录，不代替 README 或架构文档
中的当前事实。

## 当前计划

- [`m1-github-capabilities.md`](m1-github-capabilities.md) — M1 Feature 2/3：GitHub App、短时 push capability 与 PR primitives 已实现并通过自动化，真实 App smoke 待执行；

## 已完成计划

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
