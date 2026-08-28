# 计划文档

本目录保存功能与非小改动的计划、审批状态和实施结果。计划是决策记录，不代替 README 或架构文档
中的当前事实。

## 当前计划

- [`pydantic-store-models.md`](pydantic-store-models.md) — 保留 Psycopg 原生 SQL，以 Pydantic 模型和 `class_row()` 取代手工行转换，实施中；

## 已完成计划

- [`postgresql-store.md`](postgresql-store.md) — 以 PostgreSQL 替换 SQLite，保持现有单进程编排结构不变，已实施并验证；
- [`github-actions-ci.md`](github-actions-ci.md) — Ruff、Mypy、pytest 与 uv build 的首版 GitHub Actions CI，已实施并验证；
- [`documentation-refresh.md`](documentation-refresh.md) — 项目文档目录化与完整同步，已实施；
- [`../UV-BUILD-MIGRATION-PLAN.md`](../UV-BUILD-MIGRATION-PLAN.md) — 构建后端迁移到 `uv_build`，已实施；
- [`m0.md`](m0.md) — M0 落地计划，已验收；
- [`m0-closeout.md`](m0-closeout.md) — M0 收尾与验收计划，已实施。
