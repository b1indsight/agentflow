# 架构总览

## 1. 定位

FlowTool 的**目标形态**是本地优先、headless 的编排器：把带 `flow` 标签的 GitHub issue 运行成
`Codex 编码 → PR → 人工审核 → 回喂修订 → 人工合并 → 归档` 的闭环。产品交互面是 GitHub
issue / PR；Web 控制台只属于未来路线图。

当前 M0 只验收了这个目标所需的基础设施切片，不具备真实闭环。当前状态以[架构入口](README.md#当前实现状态)
为准。

已定基调：**Python** · **Webhook 驱动的常驻进程** · **headless** · **本地 checkout 跑 Codex** ·
**PostgreSQL 持久化**。

## 2. 运行形态

目标部署要求 Webhook 能到达 daemon，同时 daemon 所在机器持有本地 checkout：

- 本地开发可通过可信隧道把 GitHub Webhook 转发到 `localhost`；
- 长期运行时，daemon 部署在有受控公网入口且持有代码 checkout 的机器上；
- 给 issue 打标签会触发本机执行模型生成的命令，因此 allowlist、沙盒、短时凭据和网络出口都是
  前置安全边界，而不是后期增强。

M0 只验证了本机 daemon 启动和 `/healthz`；公网入口、真实 Webhook 投递和生产部署尚未验证。

## 3. 组件总览

```text
              GitHub (issues / PRs / reviews / projects)
                     │  ▲
           webhooks  │  │  REST + GraphQL
                     ▼  │
 ┌───────────────────────────────────────────────────┐
 │              Orchestrator (Python daemon)          │
 │                                                    │
 │  [Webhook Ingress] → [Event Router] → [Flow Engine]│
 │   FastAPI / HMAC      去重 / 定位 flow   状态机     │
 │                              │            │        │
 │                  [Worker + PostgreSQL]    ├── GitHub Client
 │                              │            └── Judge
 └──────────────────────────────┼─────────────────────┘
                                ▼
               [Codex Driver] in [Sandbox]
                                │
                [Workspace 隔离] + [VCS 指引]
```

平面边界：

- **Orchestrator / GitHub API 平面**负责 Webhook、持久化、状态机、Judge、Context Builder 与
  GitHub API；
- **Code Agent / 本地平面**负责工作区内的改码、commit、branch/bookmark 和 push；
- Orchestrator 只确定分支名并在 push 后创建 PR，不直接执行本地版本控制；
- 本地能力拆成 [workspace / sandbox / VCS 三条正交轴](components.md)（§4.6）。

这些是架构边界。当前 GitHub、Judge、push 和完整 Engine 副作用仍未完成，不能从图中推断它们已经
端到端可用。
