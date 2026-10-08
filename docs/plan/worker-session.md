# Worker 持久化会话 — Feature record

**Stage:** Implemented
**PR:** https://github.com/b1indsight/agentflow/pull/62

## 1. Initial plan

### 动机和目标

将 worker 的任务状态持久化、编码执行记录和对话查询整理为统一的 WorkerSession 职责。
当前 WorkerJournal 独立保存 assignment 状态，WorkerClient 又自行创建 CodexHistory；
client/executor 分别理解这些对象之间的关系。新对象作为 worker 本地会话入口，负责读取、
记录和恢复执行状态，并提供该工作相关的历史查询。以已合并的 #61 为基线。

### 会话的含义与行为

WorkerSession 表示一个 worker root 下的本地持久化执行会话，承载当前绑定 Flow 的执行状态。
它不是单个 Codex thread：initial/revision 可继续同一 thread，fallback 可以产生新 thread。
一个 worker 仍只执行一个 assignment；不引入多会话调度或第二份 control 状态机。

接收任务后先保存身份和阶段，执行中保存 resume/fallback 记录；结束后保存结果，
待 control 确认。重连通过同一对象获取恢复描述和待补发结果。
对话查询也经同一入口，读取 control 请求明确列出的 Codex 会话，保持现有分页与访问范围。
会话历史可覆盖多个先后执行的任务；当前 assignment 记录不能充当历史访问授权目录。

### 设计与改动范围

- `app/worker/session.py`：新增 WorkerSession 和持久化状态模型 SessionState。
  迁入 journal 的真实读写与状态变更逻辑，删除 WorkerJournal/JournalRecord，避免只加转发包装。
  明确提供接收、开始、修订执行记录、终态、确认、停止、清理核对和历史读取入口。
  恢复描述与待补发结果的组装从 client 移入 session；网络收发和重连策略继续由 client 管理。
- `app/worker/history.py`：保留 Codex 文件格式解析、游标和安全边界，作为 session 使用的历史读取适配器。
  不把大段 JSONL 解析逻辑复制进 session；不再由 WorkerClient 创建或直接持有历史读取器。
- `runtime.py`：创建一个 WorkerSession，注入 client/executor；两者使用同一对象。
- `client.py`、`executor.py`、`history_connection.py`：改用 session 的状态和查询入口。
  HistoryConnection 继续只处理传输复用及查询并发，不拥有持久化状态。

持久化继续使用现有 `assignment-journal.json` 和 version=3 数据格式，包含任务身份、
执行阶段、完整终态和摘要、修订调用记录、停止回执、归档证据及 root 身份。
保留文件名是为了原地读取已存在的记录，不增加双写或兼容状态层。
Codex 会话 ID 已存在于终态/调用记录中，control 也有 SessionRef；不增加重复的本地会话索引。
对话正文仍由 Codex 的 JSONL 文件持久化；WorkerSession 统一访问两类数据，不保证跨文件事务。
不持久化 push token，不把健康探针、凭据注入或 Codex 进程管理移入该对象。

### 方案比较

仅把两个类放进同一文件不会统一生命周期；增加一个只转发两个对象的 facade 也不减少调用方职责。
选择让 WorkerSession 真正拥有原 journal 的状态变更和恢复视图，并组合专用历史读取适配器。
将所有历史复制进一个新数据库会带来同步、迁移和空间成本，本次不采用。

### 风险和兼容

保持协议字段、结果摘要、确认语义、停止/归档凭据及 JSON 结构不变。
保留原子替换、文件/目录 fsync、root 身份校验、历史读取限流和路径约束。
运行中断仍不等于可安全重跑编码；session 只记录证据，control 继续决定是否重试。
本次不修改此前 triage 的审查问题，也不扩展凭据隔离方案。

### 实施顺序

1. 以既有 journal、恢复、历史测试固定当前行为，核对持久化格式兼容。
2. 将持久化逻辑迁入 WorkerSession，接入历史适配器，迁移 client 中恢复视图的构造。
3. 更新 runtime/client/executor 注入和调用，删除旧对象与无用引用，同步架构说明。
4. 运行相关及完整回归、Ruff、Mypy、构建；独立审查后在本 PR 提交实现。

### 验证和简化比较

复用既有断线结果补发、重复确认、停止恢复、归档清理、resume/fallback、历史分页与隔离测试。
仅在现有测试未覆盖时增加“读取原 journal 文件”和“同一 session 支持恢复及历史查询”的行为测试。
不新增仅检查命名、属性或委托次数的测试。
与更简单的直接依赖方案比较：调用方应不再自行创建历史读取器或组装恢复描述，且全部行为一致；
若新对象只是转发，应移入实际所有权逻辑或撤掉冗余层。本次不增加持久化机制，
不为现有 fsync/token 等机制重做消融；实施时记录实际差异和验证结果。

### 待确认范围

本方案统一会话职责和访问入口，保留现有两种磁盘数据源；不创建存放全部聊天正文的新存储。
若要求独立于 Codex 文件保留完整历史，需要另行确认导入、保留期限和清理语义。

## 2. PR change timeline

### 1. 2026-10-08 — 统一会话状态所有权（已批准，已实现并验证）

用户确认进一步集成当前任务身份、终态脱敏/归档、恢复核对、修订调用记录及停止/归档凭据。
WorkerSession 接替 WorkerJournal，client 不再独立保存 assignment 或创建 CodexHistory；
迁入恢复描述、恢复身份校验、连接 epoch 重绑定、重放结果生成、终态构造及脱敏。
执行中的 asyncio Task 和连接状态保留在 client，Codex 进程仍由 adapter 管理。
SessionState 保留 journal version=3/字段/文件路径；历史适配器及查询传输层继续复用。
修订调用记录由 session 持久化后返回给 executor，删除两处独立追加记录。

先迁移既有测试到新接口，在旧实现上出现预期的 session 模块缺失；再迁移实现。
新增一项旧 version=3 文件读取、拒绝盲目重启编码、跨 assignment 历史查询的组合回归，
其余沿用状态/重连/停止/归档及对话查询测试。仅重命名原 journal 测试模块。

验证：827 项测试通过（55.03s，1 条既有 Starlette 弃用警告）；Ruff、Mypy（157 文件）、
sdist/wheel 构建通过。完整日志：`.cache/2026-10-08/worker-session-full.log`。
旧模块引用检查覆盖 app/tests/scripts，恢复脚本和 Kubernetes smoke 同步到 WorkerSession。

简化对比：与基线 421cc806 相比，worker 四个模块合计 +158/-146（净增 12 行）。
删除 client 的独立 assignment/history 所有权、恢复描述及终态构造；session 直接包含原状态实现，
没有保留 WorkerJournal 转发层，两个重复的修订记录追加点被去除。原有 826 项行为测试继续通过，
另有 1 项升级与历史访问回归通过。没有增加持久化机制、后台任务或新存储，故机制消融不适用。
本次验证未接真实 Codex/provider 或 Kubernetes 集群；既有外部集成边界保持不变。

### 2. 2026-10-08 — 删除旧记录兼容测试（用户要求）

删除 `test_legacy_session_recovery_and_history_share_one_owner` 及其独占的 json 导入。
不拆分或新增替代测试，保留既有状态恢复和历史查询覆盖；生产代码不变。
此条替代上一条新增兼容测试的决定，之前的 827 项验证记录保留为历史。

验证：保留的会话、client、历史读取相关 18 项测试通过（0.59s），Ruff 通过；本次仅删除测试，不重复全量回归或消融。
