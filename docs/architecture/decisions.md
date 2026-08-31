# 关键决策、风险与已知短板

## 6. 关键决策

| 决策 | 结论 | 当前边界 |
|---|---|---|
| 认证 | GitHub App | 目标已定，adapter 尚未在真实 App 上校准；见 [GitHub App 接入](github-app.md) |
| 合并 | 全程人工 | 工具只监听合并事件，不提供自动合并路径 |
| CI 门禁 | 使用目标仓库自身 CI | FlowTool 不自建门禁，人工合并者依据 PR checks 判断 |
| 会话连续 | Codex `resume` 同一会话 | 2026-08-01 独立实测成立；真实多轮修订仍待验证 |
| PR 所有权 | Orchestrator 创建 PR | Code Agent 只负责本地改码/提交/push；真实路径未贯通 |
| 持久化 | PostgreSQL + 持久化 jobs | Store 只在 serve；assignment token 对远程消息 fencing，接口仍同步 |
| 执行拓扑 | serve + 单个出站长连接 worker | Feature 1 已实现；多 worker/单机多实例与恢复 lease 尚未实现 |
| 本地版本控制 | VCS 指引交给 Code Agent | VCS 不是端口；jj commit 已独立实测，git/push 未贯通 |

“决策已定”只表示目标方向固定，不表示对应 adapter 或端到端行为已经完成。

## 7. 主要风险

1. **外部闭环未验证**：githubkit、bub、Codex push 和凭据注入任一处都可能使 M1 路径失败；
2. **本机代码执行**：issue 内容是不可信输入，真实启用前必须落实沙盒、最小权限与受控网络；
3. **事件可靠性**：当前 delivery 在 handler 执行业务逻辑前记为已处理，handler 失败后的补偿依赖尚未实现的
   reconcile；
4. **并发与幂等**：同一 flow 串行化、重复外部副作用防护、人工 push 检测和 base 冲突处理仍是目标设计；
5. **worker 健康**：started 后 heartbeat 超时会 fail closed；短暂断网也可能使任务失败，恢复 lease 尚未实现；
6. **上下文与成本**：多轮 resume 的 token 增长、re-seed/压缩和预算限制尚未实现。

## 10. 待强化 / 已知短板

| # | 主题 | 状态 | 完成条件 |
|---|---|---|---|
| 1 | 持久化 job 与崩溃恢复 | M0 已验证 | 外部副作用再增加幂等键/查询，避免恢复后重复开 PR 或重复评论 |
| 2 | Webhook 漏投与 reconcile | 桩 | `_sweep()` 真正拉取 PR/review 状态并用游标幂等补偿 |
| 3 | Review 修订循环 | 部分 | 校准 Judge、PR context、thread 映射、Codex 结构化产出并完成真实多轮验证 |
| 4 | 触发授权与安全 | 部分 | allowlist 已有；仍需 scoped token 注入、受控 egress、git 写入路径和凭据清理 |
| 5 | Context Builder | 部分 | 当前 prompt/hunk/VCS 指引已写；仍需真实 issue、当前 diff 和未解决线程快照 |
| 6 | 状态机边界 | 部分 | 实现未合并 close、人工 push、base 前进/冲突、cancel kill 与可靠 cleanup |
| 7 | 成本与失控闸 | 部分 | run timeout、最大修订轮数已有；仍需预算、heartbeat 失败上限、kill switch、re-seed |
| 8 | 可运维性 | 部分 | flows/show/cancel/retry 已有；仍需结构化日志、worker health、dry-run/mock 和安全重试语义 |

优先顺序：先贯通并校准 GitHub/Codex/bub 与安全边界，再开放真实仓库；reconcile、幂等和取消语义必须
在无人值守运行前完成。
