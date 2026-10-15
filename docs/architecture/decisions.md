# 关键决策、风险与已知短板

## 6. 关键决策

| 决策 | 结论 | 当前边界 |
|---|---|---|
| 认证 | GitHub App | 目标已定，adapter 尚未在真实 App 上校准；见 [GitHub App 接入](github-app.md) |
| 合并 | 全程人工 | 工具只监听合并事件，不提供自动合并路径 |
| CI 门禁 | 使用目标仓库自身 CI | FlowTool 不自建门禁，人工合并者依据 PR checks 判断 |
| 会话连续 | Codex `resume` 同一会话 | 2026-08-01 独立实测成立；真实多轮修订仍待验证 |
| PR 所有权 | Orchestrator 创建 PR | Code Agent 改码/提交/push 后调用 `flow.open_pr`；control 独占 GitHub API，真实 E2E 待验收 |
| 持久化 | PostgreSQL + 持久化 jobs/result inbox | Runtime 管理 Database，服务自建具体仓储；Store 用于 CLI/live smoke/测试；epoch + assignment token 对远程消息 fencing，仓储接口仍同步 |
| 执行拓扑 | 单 serve + 多个出站单槽 worker | 多 worker、affinity、单机多实例和同 identity lease 恢复已实现；多 serve HA 未实现 |
| 本地版本控制 | VCS 指引交给 Code Agent | VCS 不是端口；Git/jj 由 Agent 自选，worker 在开 PR 前验证 commit/clean/remote SHA |

“决策已定”只表示目标方向固定，不表示对应 adapter 或端到端行为已经完成。

## 7. 主要风险

1. **外部闭环未验证**：githubkit、凭据注入和 Issue→PR 已通过隔离自动化，但真实 GitHub App/Codex 与 bub 仍可能使路径失败；
2. **本机代码执行**：issue 内容是不可信输入，真实启用前必须落实沙盒、最小权限与受控网络；
3. **事件可靠性**：Ingress 在 202 前持久接收，后台处理与 receipt 完成共享事务并支持重试；
   PR/反馈对账补偿已追踪 Flow 的漏投，但不补建漏失的新 Flow trigger。同秒历史转换重放
   已在 [M4 F1](../plan/m4-feedback-replay.md) 修复；
4. **并发与幂等**：一 Flow 一 binding/branch/PR 与 open PR 重放已约束；修订检测并拒绝远端 head 漂移，
   人工 push 合并和 base 冲突处理仍待实现；
5. **worker 健康**：短断线可在 recovery grace 内恢复，但 worker 进程丢失 executor、grace 到期或 journal 损坏仍会 fail closed；
6. **上下文与成本**：多轮 resume 的 token 增长、re-seed/压缩和预算限制尚未实现。

## 10. 待强化 / 已知短板

| # | 主题 | 状态 | 完成条件 |
|---|---|---|---|
| 1 | 持久化 job 与崩溃恢复 | 内部已验证 | result inbox 去重内部应用；外部副作用仍需幂等键/查询，避免崩溃窗口重复开 PR 或评论 |
| 2 | Webhook 漏投与 reconcile | 已实现、有自动化覆盖 | 持久 inbox 重试、PR merge/close/reopen 与评论/线程对账已接线；真实漏投演练和新 trigger 补偿仍待完成 |
| 3 | Review 修订循环 | 已实现并合并，真实验收待完成 | M2 F1–F4 和 M4 同秒重放修复有自动化覆盖；仍需真实 GitHub App/Codex/bub 矩阵，见 [M2 待办](README.md#m2-状态与待办) |
| 4 | 触发授权与安全 | 部分 | allowlist 与 reviewers 已分离，单仓库 scoped token 已实现；仍需真实 smoke、assignment 注入、受控 egress 和凭据清理 |
| 5 | Context Builder | 部分 | Issue/PR、有界 patch/讨论已冻结，control 持有线程快照；仍需真实输入校准与 CI 上下文 |
| 6 | 状态机边界 | 部分 | close/reopen、持久 stop/进程组取消及原 worker 归档清理已实现；未知执行保守阻塞；人工 push 接纳、base 前进/冲突与真实取消恢复演练仍待完成 |
| 7 | 成本与失控闸 | 部分 | run timeout、最大修订轮数已有；仍需预算、heartbeat 失败上限、kill switch、re-seed |
| 8 | 可运维性 | 部分 | epoch/lease/recovery/reason、drain、受保护指标、结构化日志、安全重试及部署/告警配置已提供；真实镜像发布、集群和备份恢复演练待验收 |

优先顺序：先在授权的一次性仓库校准 GitHub/Codex/bub 与安全边界，再开放真实业务仓库；
对账、幂等、取消与部署恢复虽已有实现，仍需在无人值守运行前完成真实环境验收。
当前实现依据见[架构状态表](README.md#当前实现状态)，取消限制见
[停止确认](../operations/cancel-drain.md#停止确认)，真实验收见 [M5 E2E 清单](../verification/m5-live-e2e.md)。
