# FlowTool（代号，未定名）

把打了 `flow` 标签的 GitHub issue，自动跑成
`code agent 编码 → PR → 你审核 → 回喂修订 → 人工合并 → 归档` 的闭环。
**headless**：所有交互都在 GitHub 的 issue / PR 上。

- 架构文档见 [`docs/architecture/`](docs/architecture/README.md)。
- 计划与实施记录见 [`docs/plan/`](docs/plan/README.md)。
- **状态：M0 骨架已验收（2026-08-26）**。真实 GitHub/Codex/bub 闭环属于 M1+，
  尚未验证的集成在代码里以 `TODO(verify)` 标注。

## 平面边界

- **Orchestrator = GitHub API 平面**：webhook、PR、评论、labels、reviewer、合并检测、签发 token。
- **Code Agent + 本地能力 = 本地平面**：工作区隔离、改码、commit、branch / bookmark、push（git / jj）。

## 端口、本地能力 & 适配器（hexagonal）

| 端口 | MVP adapter | 文件 |
|---|---|---|
| CodeAgent | codex CLI | `app/codex_driver.py` |
| Judge | bub | `app/judge.py` |
| VCS（普通函数，非端口） | git / jj | `app/vcs.py` |
| GitHubClient | githubkit | `app/github_client.py` |
| Sandbox | codex `workspace-write` | `app/sandbox.py` |
| JobDispatch | 进程内 worker | `app/worker.py` |

## 快速开始

```bash
uv sync
cp env.example .env      # 填 GitHub App / bub 配置
uv run flowtool serve    # 启动 webhook 常驻进程
```

本地开发用 [smee.io](https://smee.io) 把 GitHub webhook 转发到 `http://localhost:8000/webhook`。

开发验证：

```bash
uv sync --locked
uv run pytest
uv run ruff check app tests
uv run mypy app tests
```

## 已验证 / 待验证

M0 已实测：

- 19 个自动化测试覆盖配置/契约、SQLite、HMAC、health、delivery 去重、allowlist、取消与
  review 路由、worker dispatch/失败/启动恢复；
- `uv sync --locked`、Ruff、Mypy 和 CLI help 通过；
- `flowtool serve` 可启动，worker/reconciler 正常初始化，`GET /healthz` 返回 200；
- codex `exec` + `resume` 上下文连续；`workspace-write` 下 jj `.jj` 可写、`jj commit` 成功；
  git `.git` 只读、`git commit` 被挡（见 [`components.md`](docs/architecture/components.md) §4.4 / §4.6）。

待验证（代码里 `TODO(verify)`）：githubkit 具体调用、bub 判定 API、push 联网 + scoped token、
git repo 的提交路径、多轮 context 增长。
