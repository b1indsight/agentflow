# 路线图

## 8. 当前目录与里程碑

```
flowtool/
  app/
    control/          # FastAPI/router/service/coordinator/gateway/reconciler/runtime
    worker/           # WebSocket client/executor/runtime + durable journal/root lock
    protocol.py       # 版本化 task/result/message 契约
    codex_driver.py   # CodexSession 抽象（CodeAgent 端口首个 adapter）
    context_builder.py # prompt / 上下文构建（§4.9）
    judge.py          # Judge / Triage 适配器（bub，§4.10）
    github_client.py  # githubkit 封装（App 认证）
    vcs.py            # Flow repo 准备 + VCS 自探测指引/事实核验（§4.6）
    sandbox.py        # 旧 workspace-write 接缝；强隔离 adapter 留到 M5（§4.6 / §9-5）
    store.py          # PostgreSQL schema + 仓储（含 jobs 表）
    config.py         # serve/worker 角色配置（pydantic-settings）
    models.py         # 领域类型 / 枚举
    cli.py            # admin CLI（§10-8）
    plugins/          # 生命周期 hook 插件（§9-7）
  tests/
  pyproject.toml      # uv 管理
  README.md
```

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

## 9. 未来扩展与架构预留（Roadmap）

> 以下是 MVP 之后的方向。列在这里是为了**现在就把接缝（ports）留好**，避免日后大改——
> core 只依赖抽象端口，扩展一律做成"换 adapter"或"挂插件"，不动 core。

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
   - 预留：GitHub Client 保持薄封装，能力按需追加即可，不影响 core。

4. **Code Agent 标准接口** —— 把"与 code agent 交互"抽象成统一端口，方便接入新的底层 agent（不只 codex）。
   - **当前接缝已就位**：`CodeAgent` port 已定义 `start / resume`，Codex 是第一个 adapter；实时
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

**架构取向：hexagonal（ports & adapters）。** control core（ControlService + JobCoordinator + store）只依赖端口：
`CodeAgent` / `Judge` / `GitHubClient` / `Plugin hooks`；worker 执行器直接管理执行与工作区约束。
**VCS 不是端口**：它由 Flow repo 准备、自探测 guidance 和只读事实核验函数组成（见 §4.6）。
当前已给端口放置最简 adapter 或桩
（codex / bub / WebSocket worker / githubkit / 插件集）。
“存在 adapter”不代表外部契约已校准；未来扩展仍优先通过换 adapter 或加插件保持 core 边界稳定。
