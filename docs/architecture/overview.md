# 架构总览

## 1. 定位

FlowTool 的**目标形态**是本地优先、headless 的编排器：把带 `flow` 标签的 GitHub issue 运行成
`Codex 编码 → PR → 人工审核 → 回喂修订 → 人工合并 → 归档` 的闭环。产品交互面是 GitHub
issue / PR；Web 控制台只属于未来路线图。

M0 已验收基础设施切片；M1 Issue→PR 与 M2 回喂循环已实现、合并并有自动化覆盖，真实服务闭环
仍待验收。当前状态和 M2 已知问题以[架构入口](README.md#m2-状态与待办)为准。

已定基调：**Python** · **Webhook 驱动的 control plane** · **出站长连接 worker** · **headless** ·
**worker 本地 checkout 跑 Codex** · **PostgreSQL 持久化**。

## 2. 运行形态

目标部署要求 Webhook 能到达 serve；持有本地 checkout 的 worker 只需主动访问 serve：

- 本地开发可通过可信隧道把 GitHub Webhook 转发到 `localhost`；
- 长期运行时，serve 部署在有受控公网入口的控制节点，worker 可部署在独立执行节点；
- 给 issue 打标签会触发本机执行模型生成的命令，因此 allowlist、沙盒、短时凭据和网络出口都是
  前置安全边界，而不是后期增强。

当前验证了 serve/双 worker 多进程启动与重启重连、认证 WebSocket 注册、租约恢复和 fake executor 的远程 job；公网入口、
真实 Webhook 投递和生产部署尚未验证。

## 3. 组件总览

```text
              GitHub (issues / PRs / reviews / projects)
                     │  ▲
           webhooks  │  │  REST + GraphQL
                     ▼  │
 ┌──────────────────── serve / control ────────────────────┐
 │ [Webhook] → [Event Router] → [Control Service]          │
 │                   [Job Coordinator] → Store / GitHub    │
 │                           │                              │
 │                    [Worker Gateway]                      │
 └───────────────────────────┼──────────────────────────────┘
               authenticated WebSockets
                       │       │
 ┌──────────────── remote workers (1..N) ──────────────────┐
 │ [Worker Client + Journal] → [Executor] → Judge / Codex  │
 │                                      → Workspace / VCS   │
 └──────────────────────────────────────────────────────────┘
```

平面边界：

- **Control plane**负责 Webhook、持久化、状态机和 GitHub API，是唯一数据库权威；
- **Worker execution plane**负责 Judge、Context Builder、Codex 与 Flow repo 内的改码/提交/push；
- control 只确定分支名并在 worker 完成 push 后创建 PR，不直接执行本地版本控制；
- 本地能力按 [Flow repo / execution policy / VCS](components.md) 分离（§4.6）：Agent 选择并操作
  Git/jj，worker 只验证事实。

这些是架构边界。当前 GitHub、Judge、GitHub-authenticated push 和完整 coordinator 副作用仍未完成，
不能从图中推断它们已经端到端可用。
