# WorkerClient 历史消息分流

**Stage:** Implemented

## 1. 方案与授权范围

用户已确认同一 WebSocket 的消息分流属于 client，并明确要求新开 PR 实现，保持消息格式不变。
本次为既有通信逻辑的职责搬移，直接执行已确认范围；基于已合并的 PR #62。

删除 `app/worker/history_connection.py`，Connection 协议及查询分流、回复、并发控制、清理逻辑
迁入 WorkerClient。统一 `_receive_message()` 返回解析后的 ServerMessage，消除包装层及二次解析。
保留单一 socket 接收入口，查询回复捕获原连接；查询任务在连接 finally 中取消、等待并清空。
保留最多 4 个并发查询及 query_busy 错误语义。
`history.py` 仍负责 Codex 历史解析、分页、读取限流，经 WorkerSession.read 提供查询能力。
不改变协议版本、JSON 字段、control 实现、持久化数据或任务执行语义。

验收：执行中及空闲时历史查询可用，不阻塞任务消息；连接清理保持有效；无旧包装层引用。
风险：若某一接收路径漏用统一入口，可能误把查询当成任务消息；逐一迁移所有接收点并复用回归。
不引入新状态机制，复用现有查询任务集合和生命周期，无需新建分发框架或消息总线。

## 2. 变更记录

### 1. 2026-10-08 — 按已确认职责搬移

先将原包装层测试改为通过真实 WorkerClient.run_connection 入口执行，搬移前 5 项查询测试通过。
随后迁移实现，保留原历史组件和 session。未增加测试数量；验证搬移前后相同输入的行为。
无新增机制，消融不适用；删除包装层、发送转发方法、HistoryReader 接口及重复消息解析。

验证：搬移前 5 项历史查询回归通过，搬移后含 client 用例共 13 项通过；完整 PostgreSQL 回归 826 项通过（54.52s，1 条既有 Starlette 弃用警告）。Ruff、Mypy（156 文件）、sdist/wheel 构建通过。
完整记录：`.cache/2026-10-08/client-history-full.log`。app/tests/scripts 无旧包装层引用，所有接收路径使用统一解析入口，协议文件与历史组件没有修改。
