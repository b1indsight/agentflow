# 架构总览

## 1. 定位

一个 **本地优先、headless** 的编排器：把打了 `flow` 标签的 GitHub issue，自动跑成
`codex 编码 → PR → 你审核 → 回喂修订 → 合并 → 归档` 的闭环。
**所有人机交互都发生在 GitHub 的 issue / PR 上**，工具自身没有界面（MVP 阶段；Web 控制台是后续扩展，见 §9）。

已定基调：**Python** · **webhook 驱动的常驻进程** · **headless** · **本地 checkout 跑 codex** · **SQLite 持久化**。

## 2. 运行形态的前提

webhook 需要公网端点，但 codex 要在本地 repo 目录跑——两者靠"**常驻进程部署在持有本地 checkout 的机器上**"统一：

- 本地开发：用 **smee.io / cloudflared 隧道** 把 GitHub webhook 转发到 `localhost`。
- 长期：把 daemon 放到一台有公网入口、且持有代码 checkout 的机器上。

## 3. 组件总览

```
              GitHub (issues / PRs / reviews / projects)
                     │  ▲
           webhooks  │  │  REST + GraphQL
                     ▼  │
 ┌───────────────────────────────────────────────────┐
 │              Orchestrator (Python daemon)           │
 │                                                     │
 │  [Webhook Ingress] → [Event Router] → [Flow Engine] │
 │   FastAPI/验签        去重/定位flow     状态机(每issue)│
 │                                    │      │         │
 │                     ┌──────────────┘      └───────┐ │
 │                     ▼                             ▼ │
 │           [Codex Driver] (在 Sandbox 内)   [GitHub  │
 │           子进程, cwd=每flow工作区          Client] │
 │                     │                             │ │
 │        [Workspace 隔离] + [Sandbox 端口]   [Store (SQLite)]
 │        VCS-无关拷贝     可写根/网络策略     flows/events/runs │
 └───────────────────────────────────────────────────┘
                     │
                     ▼
     本地 repo 主 clone + 每 issue 独立工作区(拷贝) → codex 进程(Sandbox 内)
```

**平面边界（关键）**：
**Orchestrator = GitHub API 平面**——webhook、开 / 评论 / 合并检测 PR、labels、reviewer、Project、签发 scoped token，
外加编排大脑（状态机 / Judge / ContextBuilder）。另加一步**薄的、VCS-无关的工作区隔离**（定分支名——它来开 PR）。
**Code Agent（在 Sandbox 内）= 本地平面**——改码、commit、branch / bookmark、push。
工具本身**不做本地版本控制、不出现裸 `git`**；本地 VCS 全归 agent（经 §4.6 的 **VCS 指引文本**，git / jj 通用）。
本地平面拆成三条**正交轴**（§4.6）：**workspace**（隔离目录，VCS-无关）·**sandbox**（可写根 + 网络，端口，只认路径）·**VCS**（commit/push 由 agent 依指引做）。

