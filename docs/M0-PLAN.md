# M0 落地计划

> 状态：已验收 · 计划日期：2026-08-02 · 验收日期：2026-08-26 · 配套 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
> 本文件是**实现计划**（写给动手前对齐用），不是代码。未验证的集成统一标 `TODO(verify)`。
> 2026-08-02 修订：本地平面拆成 workspace / sandbox / VCS 三轴——VCS 不再是端口（普通函数），Sandbox 提为端口（§5 / ARCHITECTURE §4.6）。

## 0. M0 范围

**目标**：骨架能起来、端口都立好、接口定死。
`webhook 验签入库 → SQLite → 配置 → 持久化 job + worker/崩溃恢复骨架`。

**不在 M0**：真实 codex/bub/GitHub 的完整闭环（M1+）。M0 里这些都是**桩 + `TODO(verify)`**，但签名和文件先定死，后续只填实现、不动结构。

已建的通用文件：`pyproject.toml`、`README.md`、`.gitignore`（repo 根 = 项目根，包名 `app/`）。

## 1. 文件布局（文件级职责）

```
app/
  __init__.py         # 版本
  config.py           # pydantic-settings：env schema（§3）              [M0 写实]
  models.py           # 枚举 + dataclass + pydantic 契约（§4）           [M0 写实]
  store.py            # SQLite：DDL + 仓储函数（§4 DDL）                  [M0 写实]
  ports.py            # 5 个端口 Protocol（§5）                          [M0 写实]
  ingress.py          # FastAPI /webhook：HMAC 验签 + 入队               [M0 写实]
  router.py           # 去重 + 触发授权 + 忽略bot + 路由 + 反馈缓冲        [M0 部分]
  worker.py           # job 队列消费 + 启动 reconcile 恢复                [M0 部分]
  engine.py           # Flow 状态机 + 转移动作                           [M0 桩/部分]
  codex_driver.py     # CodeAgent adapter（codex CLI）                  [M0 写实命令构造]
  vcs.py              # 普通函数：workspace 隔离(VCS-无关) + git/jj 指引    [M0 部分]
  sandbox.py          # Sandbox 端口 adapter：codex workspace-write        [M0 写实]
  judge.py            # Judge adapter（bub）：语义判定                    [M0 桩]
  context_builder.py  # 首轮/修订 prompt 构建                            [M0 写实]
  github_client.py    # githubkit 封装：App 认证/PR/评论/label/resolve   [M0 桩]
  reconcile.py        # 对账 poller                                     [M0 桩]
  cli.py              # typer：serve / flows / retry / cancel           [M0 部分]
  plugins/
    __init__.py
    hookspecs.py      # pluggy 生命周期 hook 定义                        [M0 写实]
    builtin.py        # 示例插件 post-merge-summary（桩）                [M0 桩]
tests/
  test_hmac.py        # 验签
  test_store.py       # 建表 + active flow 唯一约束
  test_models.py      # 枚举/契约
migrations/           # M0 用 store.init_db() 直接建表，暂不引 alembic
```

## 2. 依赖（已写进 pyproject）

`fastapi` · `uvicorn[standard]` · `githubkit`(App+webhook) · `bub`(Judge) ·
`pydantic` / `pydantic-settings` · `pluggy`(插件) · `typer`(CLI) · `httpx` · `loguru`。
dev：`pytest` / `pytest-asyncio` / `ruff` / `mypy`。
`TODO(verify)`：`githubkit` API 面、`bub` 版本 pin（pre-0.4）。

## 3. 配置 schema（`config.py`，env 前缀 `FLOWTOOL_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GITHUB_APP_ID` | 0 | App ID（附录 A）|
| `GITHUB_PRIVATE_KEY_PATH` | `./*.pem` | App 私钥，签 JWT |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC 验签 |
| `BOT_LOGIN` | `flowtool[bot]` | 忽略自身事件用 |
| `TRIGGER_LABEL` | `flow` | 触发标签 |
| `ALLOWLIST` | `[]` | 允许触发的用户（逗号分隔）|
| `REPOS_ROOT` | `~/.flowtool/repos` | 本地 checkout 根 |
| `DB_PATH` | `~/.flowtool/flowtool.sqlite3` | SQLite |
| `SETTLE_SECONDS` | 75 | 附录 B.7 |
| `SUBMIT_GRACE_SECONDS` | 15 | 附录 B.7 |
| `CONFIDENCE_THRESHOLD` | 0.6 | Judge 阈值 |
| `CODEX_RUN_TIMEOUT_SECONDS` | 1200 | 单 run 超时 |
| `MAX_REVISION_CYCLES` | 20 | 失控闸 |
| `HOST` / `PORT` | `0.0.0.0` / 8000 | webhook 服务 |
| （bub）`BUB_MODEL` / `BUB_API_KEY` | — | Judge 模型，bub 自读 |

## 4. 数据模型 + DDL（`models.py` / `store.py`）

枚举：`FlowState`(pending/coding/pr_open/revising/merged/archived/cancelled/failed)、
`JobKind`(initial/revision/answer)、`JobState`(queued/running/done/failed)、
`Verdict`(revise/wait/answer/ignore)、`ReviewItemKind`(review_body/inline/conversation)、`VcsKind`(git/jj)。

pydantic 契约（跨模型 JSON）：`TriageVerdict`(附录 B.2)、`CodexResult`(附录 B.4：summary/addressed/deferred/needs_clarification)。
内部 dataclass：`Flow`(issue 级 + 状态机)、`Attempt`(候选解法，§9-2)、`SandboxPolicy`(§4.6)。

```sql
CREATE TABLE IF NOT EXISTS flows (            -- issue 级追踪 + 状态机
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
  installation_id INTEGER,
  state TEXT NOT NULL,
  current_attempt_id INTEGER REFERENCES attempts(id),   -- 当前驱动 flow 的 attempt
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- 一个 (repo, issue) 只能有一条 active flow（§4.2）
CREATE UNIQUE INDEX IF NOT EXISTS ux_flows_active ON flows(repo, issue_number)
  WHERE state NOT IN ('archived','cancelled','failed');

-- 一条 flow 挂多个候选 attempt（§9-2 多方案分支）；MVP 恰好一个。per-attempt 产物在此。
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_id INTEGER NOT NULL REFERENCES flows(id),
  branch TEXT, codex_session_id TEXT, pr_number INTEGER,
  revision_cycles INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_flow ON attempts(flow_id);
CREATE INDEX IF NOT EXISTS ix_attempts_pr ON attempts(pr_number);

CREATE TABLE IF NOT EXISTS jobs (            -- 队列真相（§10-1）
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  flow_id INTEGER NOT NULL REFERENCES flows(id),
  kind TEXT NOT NULL, state TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0, pid INTEGER, heartbeat_at TEXT,
  payload TEXT,                              -- JSON：本轮反馈快照等
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS events (          -- webhook 去重 + 审计（§4.2）
  delivery_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
  flow_id INTEGER REFERENCES flows(id), payload TEXT, processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codex_runs (      -- flows→runs 一对多，归属到 attempt（§9-2）
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  kind TEXT NOT NULL, prompt TEXT, log_path TEXT, exit_code INTEGER,
  started_at TEXT, ended_at TEXT
);

CREATE TABLE IF NOT EXISTS review_threads (  -- 反馈项 + 回喂/解决状态（附录 B），归属到 attempt
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  kind TEXT NOT NULL, github_id INTEGER NOT NULL, thread_id TEXT,
  path TEXT, line INTEGER, original_commit_id TEXT, diff_hunk TEXT,
  author TEXT, body TEXT,
  relayed INTEGER NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_gid ON review_threads(attempt_id, kind, github_id);
```

`store.py` 提供：`init_db()`、flows 的 create/get/get_active/get_by_pr(经 attempts join)/update、
attempts 的 create/get/get_current/list/update、jobs 的 enqueue/claim/heartbeat/finish/recover_stale、
events 的 seen?/record、review_threads 的 upsert/unrelayed/mark_relayed/mark_resolved（均按 attempt_id）。
`TODO(verify)`：M0 用 stdlib `sqlite3`（同步）；FastAPI 是 async，落地时看要不要 `aiosqlite` 或 executor。

## 5. 端口接口（`ports.py`，签名先定死）

```python
class CodeAgent(Protocol):        # adapter: codex（codex_driver.py）
    async def start(self, task: str, cwd: Path, *, output_schema: Path | None) -> "AgentRun": ...
    async def resume(self, session_id: str, prompt: str, *, output_schema: Path | None) -> "AgentRun": ...

class Judge(Protocol):            # adapter: bub（judge.py）
    async def triage(self, discussion: str, pr_context: str) -> TriageVerdict: ...
    async def answer(self, question: str, pr_context: str) -> str: ...

class Sandbox(Protocol):          # adapter: codex workspace-write（sandbox.py）；容器/微VM 后续
    def policy(self, ws: Path, *, network: bool) -> SandboxPolicy: ...
    # 可写根 + 网络出口，只认路径、不认 git/jj（§4.6 三轴之一）。

class GitHubClient(Protocol):     # adapter: githubkit（github_client.py）
    async def installation_token(self, installation_id: int) -> str: ...
    async def open_pr(self, repo, head, base, title, body) -> int: ...
    async def comment(self, repo, number, body) -> None: ...
    async def set_labels(self, repo, number, labels: list[str]) -> None: ...
    async def request_reviewers(self, repo, number, reviewers: list[str]) -> None: ...
    async def list_review_threads(self, repo, pr) -> list[dict]: ...      # GraphQL
    async def resolve_review_thread(self, thread_id: str) -> None: ...    # GraphQL

class JobDispatch(Protocol):      # M0 = 进程内（worker.py 直接读 jobs 表）
    async def submit(self, job_id: int) -> None: ...
```

> **VCS 不是端口**：workspace 隔离（VCS-无关拷贝）+ git/jj 指引文本，都是 `vcs.py` 里的普通函数
> （`make_workspace` / `cleanup_workspace` / `detect_vcs_kind` / `agent_guidance`）。§4.6 三轴。

## 6. 关键流程（伪码级）

**A. webhook 入口（ingress + router）**
```
POST /webhook:
  verify HMAC(X-Hub-Signature-256, secret, raw_body)   # 不过 → 401
  if sender == BOT_LOGIN: return 204                     # 忽略自身（附录 B.1）
  if events.seen(delivery_id): return 200                # 幂等
  route(event_type, payload); events.record(...)
  return 202                                             # 秒回，长活交 worker
route:
  issues.labeled(flow) & actor in ALLOWLIST → flow.create(PENDING) + enqueue(initial)
  issues.unlabeled(flow) / issue_comment '/flow stop'   → flow.cancel
  pull_request_review.submitted / review_comment / issue_comment
        → 落 review_threads(unrelayed) + 触碰 settle 计时（附录 B）
  pull_request.closed(merged=true)                       → flow → MERGED + enqueue(archive)
```

**B. worker（消费 + 恢复）**
```
startup: recover_stale()   # jobs.state=running 但进程已死 → 重排队或 failed（§10-1）
loop: job = jobs.claim(queued); dispatch(job); heartbeat 中途续期
```

**C. initial job**
```
ws = vcs.make_workspace(repo_path, branch)                    # VCS-无关拷贝（workspace 轴）
prompt = context_builder.initial(issue, repo_conventions, vcs.agent_guidance(repo_path, branch))
run = codex.start(prompt, ws)                    # 在 Sandbox 内跑；codex 改码+commit+push（本地平面）
pr = github.open_pr(repo, branch, base, title, "Closes #N")   # GitHub 平面
flow → PR_OPEN; github.request_reviewers([you]); github.set_labels(['flow:in-review'])
```

**D. 修订循环（附录 B）**
```
settle 到点 → snapshot = review_threads.unrelayed(flow)
verdict = judge.triage(snapshot, pr_context)
  revise  → enqueue(revision, payload=snapshot)
  answer  → github.comment(judge.answer(...)); 不改码
  wait    → 留着，等更多
revision job:
  prompt = context_builder.revision(instruction, snapshot_with_hunks, vcs.agent_guidance(repo_path, branch))
  run = codex.resume(session_id, prompt, output_schema=CodexResult)
  result = parse(run.output)
  addressed → github.resolve_review_thread + comment(note)
  deferred  → comment(reason)（留 open）
  needs_clarification → comment；flow 回 IDLE 等你
  mark_relayed; github.request_reviewers([you]); label 'flow:in-review'
```

## 7. 构建顺序（M0 步骤，每步可单独验收）

1. **models + config**：枚举、契约、settings 能 load。
2. **store**：`init_db()` 建表；active-flow 唯一约束；jobs enqueue/claim/recover 往返。
3. **ports**：5 个 Protocol + 桩 adapter（返回 `NotImplementedError` / 假值）；`vcs.py` / `sandbox.py` 写实。
4. **ingress + HMAC**：`/webhook` 验签、忽略 bot、dedup、`/healthz`。
5. **router**：labeled→create+enqueue、merged→archive、feedback→review_threads。
6. **worker**：claim/dispatch/heartbeat + startup recover。
7. **codex_driver**：把已验证的 `exec`/`resume` 命令构造 + JSONL 解析 `thread_id` 写实（改码本身 M0 可先跑 dry）。
8. **cli serve**：`flowtool serve` 起 uvicorn；`flows list` 读库。
9. **smoke tests**：HMAC、建表、契约。

## 8. `TODO(verify)` 汇总（集中跟踪）

| # | 待验证 | 出现处 |
|---|---|---|
| V1 | githubkit 具体 API（App token / PR / GraphQL resolve） | `github_client.py` |
| V2 | bub 判定调用方式（库 turn / 结构化产出解析） | `judge.py` |
| V3 | push 联网 egress（Sandbox network 策略）+ 仓库级 scoped token 注入 | `sandbox.py` / `github_client.py` |
| V4 | git repo 提交路径 = **sandbox 轴**问题（`.git` 只读）：放宽 workspace-write / 换容器 adapter | `sandbox.py` |
| V5 | codex 改码+commit+push 在一次 run 内跑通（jj 已验 commit，push 未验） | `codex_driver.py` |
| V6 | 多轮 resume 的 context/token 增长 → 是否需 re-seed/压缩 | `codex_driver.py` |
| V7 | sqlite 同步 vs async（FastAPI） | `store.py` |
| V8 | codex 对 jj 不熟 → 注入 jj 指引/skill 的效果 | `context_builder.py` / `vcs.py` |
| V9 | settle/grace/阈值 参数实调 | `config.py` |

## 9. M0 验收

- [x] `uv sync --locked` 通过；`flowtool serve` 可启动，`GET /healthz` 返回 200。
- [x] 正确签名的 `issues.labeled` webhook 建出 PENDING flow + queued job；错误签名返回
  401；重复 delivery-id 不重复处理。
- [x] worker 能 claim/dispatch job，成功与失败状态可回写；启动恢复和过期 heartbeat
  通过确定性自动化测试模拟验证。
- [x] `pytest` 18 项全绿，Ruff 与 Mypy 全绿。

## 10. 实施结果（2026-08-26）

M0 按原范围完成，没有把真实 GitHub/Codex/bub 闭环提前纳入。测试实际按行为边界拆为：

- `test_config.py` / `test_models.py` / `test_store.py`：配置、契约与持久层；
- `test_ingress.py` / `test_router.py`：health、HMAC、异步交接、去重与路由；
- `test_worker.py`：dispatch、失败状态、启动恢复与 heartbeat 清理。

实施中修正了三个由验收暴露的 M0 缺陷：逗号 allowlist 在 settings source 层被误当成
JSON、已持久化实体 ID 仍向核心流程传播可空类型、heartbeat task 取消后未等待清理。
为隔离 HTTP 测试，组合根新增 `create_app(context_factory=...)`；生产模块级 `app` 的启动方式不变。

V1–V9 仍按上表跟踪，其中 V1–V6/V8/V9 属于后续真实集成验证；V7 在 M0 继续采用同步
SQLite，是否迁移异步实现留待并发数据证明需要时再决定。
