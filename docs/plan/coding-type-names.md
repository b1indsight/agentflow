# 编码类型命名统一

2026-10-08：用户批准类型重命名，并明确要求在已合并的 PR #60 后单独提交新 PR。
以合并提交 39fcd0da 为基础，仅包含此次命名调整。


用户确认按讨论中的名称重命名，继续保留首次和修订任务两个结果类型。
- `CodeAgent` → `CodingAgent`
- `CodeAgentContext` → `CodingExecutionContext`
- `AgentRun` → `CodingExecutionResult`
- `InitialCodexResult` → `InitialCodingOutput`
- `CodexResult` → `RevisionCodingOutput`
- `InitialResult` → `InitialTaskResult`
- `RevisionResult` → `RevisionTaskResult`
- `RevisionInvocation` → `RevisionExecutionRecord`

保留 CodexAgent 作为具体适配器名称；同步代码、既有测试和当前架构文档引用。
仅修改类型名称，不更改字段、判别值、执行行为或序列化结构，不增加兼容别名和测试。
历史计划与已发布 changelog 保留原名；本次未增加机制，消融不适用。

验证：826 项既有测试通过（57.21s，1 条既有 Starlette 弃用警告），Ruff、Mypy（157 文件）、构建通过。
测试日志：`.cache/2026-10-08/coding-rename-tests.log`；app/tests 无旧类型名残留。
