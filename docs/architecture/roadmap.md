# 路线图

## 8. 当前目录与里程碑

```
flowtool/
  app/
    domain/           # 领域事实、类型、端口与纯策略（无运行时 I/O）
    contracts/        # 版本化 task/result/message 与冻结结果应用契约
    persistence/      # PostgreSQL schema、事务仓储、指标查询与就绪探针
      store.py        # 创建仓储并负责数据库初始化与关闭；服务接收具体仓储
      database.py     # 独立连接/事务上下文、注入时钟和共用锁
      repository.py   # 保留具体类型的显式事务绑定
      schema.py       # 按依赖顺序初始化的完整建表 SQL
      records.py      # 原生行类型与必需记录回读校验
      flows.py        # Flow/Attempt
      deliveries.py   # 交付与 tool-call 账本
      jobs.py         # Job 生命周期与原始结果 inbox
      workers.py      # Worker/binding/assignment/准备/租约/drain
      stops.py        # 停止意图、授权与回执
      results.py      # 冻结结果计划与副作用回执
      reviews.py      # 反馈版本、批次、上下文与线程事实
      webhooks.py     # 持久 Webhook 消费与事件审计
      reconciliation.py # PR/反馈对账进度
      projects.py     # Projects 投影与同步检查点
      metrics.py      # 数据库指标读取
      readiness.py    # 只读就绪探针
    github/           # githubkit App 认证、GraphQL 分页与远端数据解码
    controler/        # 控制服务端，根目录为 API、配置与运行时装配
      events/         # 持久 webhook 消费与事件路由
      workers/        # Worker 连接、派发通信与历史查询
      workflow/       # Flow/Job 准备、交付转换、结果与副作用恢复
      review/         # 审核上下文、反馈解释与 Judge
      reconciliation/ # PR、反馈与 Projects 对账
    worker/           # WS client、journal、CodingAgent、prompt、工作目录与 VCS
    telemetry/        # 通用 logging 与控制端 metrics，分开依赖
    cli.py            # admin CLI（§10-8）
    live_smoke.py      # 真实闭环验收命令入口
    plugins/          # 生命周期 hook 插件（§9-7）
  tests/
  pyproject.toml      # uv 管理
  README.md
```

包之间的依赖规则见[组件职责](components.md#包依赖边界)。包迁移不改变 CLI、数据库 schema
或网络消息字段；内部 Python 导入使用新路径，不提供旧路径转发模块。

- **M0（已验收）** 骨架 + webhook 验签 + SQLite（含 `jobs`）+ 配置 + worker / 崩溃恢复骨架；
  19 个自动化测试、Ruff、Mypy、CLI 与 `/healthz` runtime smoke 全部通过（2026-08-26）；
- **持久层替换（2026-08-27）** PostgreSQL 成为当前唯一运行时数据库；M0 的 SQLite 描述保留为历史验收事实；
- **远程执行 Features 1–3（2026-09-02）** serve/worker 分离、多 worker 公平调度与 affinity、持久 lease、
  同 identity 忙碌重连、serve 重启恢复和结果重放均已实现；跨 worker session 迁移与多 serve HA 不在范围内；
- **M1（已实现并合并，真实 E2E 待验收）** 触发（含 allowlist）→ Flow 独占 worker/repo → codex 首轮 → 开 PR
- **M2（已实现并合并，真实 E2E 待验收）** 语义 Triage（bub）门 + 回喂循环
  （settle → revise/answer/wait/ignore + resume/兜底 + 保守 resolve）；F1–F4 对应 PR #19–#22。
  同秒重复 webhook 重放已在 M4 F1 修复；合并证据与剩余验收见 [M2 状态与待办](README.md#m2-状态与待办)。
- **M3（F1/F2/F3 已合并，真实闭环待验收）** 人工合并检测（`pull_request.closed`）→ 归档 + 状态标签 / 评论；
  拆为 F1 合并检测与任务收敛、F2 工作区清理与 worker 释放、F3 状态回写与闭环验收三个 PR，
  [F3 实施与验收](../plan/m3-archive-status.md)记录增量回写、恢复操作及真实闭环待办。
- **M4（已批准按序实施）** 在现有多 worker 能力上补恢复并发、已追踪 Flow 的 PR/反馈对账、
  close/reopen、准备期有界重试与 Projects v2 看板；拆为九个 feature，每 PR 约 1000 行，
  依赖、预算和验收见 [M4 阶段计划](../plan/m4-stage.md)。
- **M5** 安全隔离（容器化 codex）+ 公网 / 隧道部署 + 完整运维与可观测性

M1/M2 已合并不等于真实闭环已验收；M3 尚未完成，M4–M5 仍为目标里程碑。实际状态见[架构入口](README.md#当前实现状态)。

## 9. 未来方向（Roadmap）

> 以下是 MVP 之后的方向，接口与类型在实际需要时扩展。GitHub 使用具体资源；
> CodingAgent、Judge 和插件沿用各自已有职责。

1. **Web 控制台（Kanban）** —— 一个 webapp 打开项目看板，每张卡 = 一条被追踪的对话，可点进去实时参与对话。
   - 预留：core 状态经 store + 一层 API 暴露；headless（GitHub）与 webapp 是同一 core 的两个入口适配器。
     Driver 除"批量回喂 review"外，要留一个**实时注入消息**的入口，webapp 才能"插话"。

2. **可派生 Flow 编排** —— Flow 像持久化的分布式 subagent 一样 spawn/send/wait/inspect/accept 其他 Flow；
   每个子 Flow 拥有自己的隔离执行环境、repo、干净上下文、skills、结果和最多一张 PR。
   - **目标边界已定（2026-09-03）**：调度和协作的一等单位是 Flow，不是人格化 Agent；一 Flow = 一
     隔离执行环境 = 一独占 repo = 一 branch = 最多一 PR，Attempt 只表示同一 Flow 的执行尝试。目标模型、
     状态分轴、tool RPC、context/skill portability 和父级验收见[可派生 Flow 编排](derivable-flows.md)。
   - M1 先让 Flow 全生命周期固定占用单槽 worker/repo，并以 `flow.open_pr` 验证通用 tool transport；
     FlowLink、派生、durable join、result acceptance 和可释放的跨 worker checkpoint 后续分阶段实现。

3. **完善 GitHub 协作** —— 覆盖常见协作动作（reviewer 指派、draft PR、labels、Projects 联动、review 线程 resolve 等）。
   - GitHub 使用具体资源对象，能力按真实需求添加到相应资源；control 同步使用实际接口。

4. **Code Agent 标准接口** —— 把"与 code agent 交互"抽象成统一端口，方便接入新的底层 agent（不只 codex）。
   - **当前接缝已就位**：`CodingAgent` port 已定义 `start / resume`，Codex 是第一个 adapter；实时
     `inject`、统一日志/产物收集和其他 agent adapter 属于后续扩展。

5. **更强隔离** —— 每个 Flow 放进容器 / 微VM，隔离完整 repo、文件系统与网络。
   - M1 full-access worker 必须先通过专用 OS account、容器或 VM 与 control secret 隔离；不同 Flow repo
     目录只表达所有权。容器 / 微VM adapter 在 M5 接管 per-Flow FS、egress 和生命周期，支持互不信任租户。

6. **多 Worker 调度与恢复** —— 多 identity、session affinity、公平调度、单机多 root、忙碌重连、
   持久 lease 与 result replay 已完成。
   - M1 先增加 Flow 生命周期固定 binding：一个 worker 同时服务一个 Flow，空闲 job 期间也不复用。
   - 后续方向：可释放 checkpoint、跨 worker 重建 session/repo、多 serve leader election、外部副作用幂等键
     和通用可观测性。

7. **端口插件扩展** —— 在生命周期端口挂插件。
   例：合并后总结插件——归纳上一段对话里的代码风格偏好、更新仓库的 skill 文件。
   - **已接线（2026-08-02）**：五个生命周期 hook（`on_flow_start / on_pr_opened / on_review / on_merged / on_archived`）已在 engine 的对应状态转移处触发，插件可订阅；插件异常被隔离（不掀翻 core）。
     待实现：示例插件 `post-merge-summary` 的插件体（读会话 + diff → 提炼风格偏好 → 提 PR 更新 repo 的 skill），目前只打日志。

control 直接依赖具体 GitHub 资源，由 App/Installation 绑定身份并调用 Repository、Issue、PR、Thread 和 Project/Item。
GitHub 不预留 adapter 或替代协议。`CodingAgent` / `Judge` 保留已有接口，插件使用生命周期 hooks；
worker 执行器直接管理执行与工作区约束。
**VCS 不是端口**：它由 Flow repo 准备、自探测 guidance 和只读事实核验函数组成（见 §4.6）。
当前使用 codex、bub、WebSocket worker、githubkit 和插件集；外部契约校准情况由各组件的验收记录说明。
