# FlowTool 架构方案

> 状态：M0 已验收、M1+ 仍为草案 · 初始日期：2026-08-02 · M0 验收：2026-08-26。
> 本文件是 living doc，细节随讨论迭代。
> 项目暂用代号 **FlowTool**（源自触发标签 `flow`），未定名。
> 2026-08-02 修订：本地平面拆成 **workspace / sandbox / VCS 三条正交轴**——VCS 降为普通函数（不再是端口），
> Sandbox 提为一等端口（§4.6 / §9-5）。

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

## 4. 组件职责

### 4.1 Webhook Ingress（FastAPI + uvicorn）

`/webhook` 端点：校验 `X-Hub-Signature-256` HMAC → 按 `X-GitHub-Event` 解析 → **立即入队并秒回 2xx**
（GitHub 10s 超时，codex 长任务绝不能卡在这里）。

关注事件：`issues.labeled/unlabeled`（`flow` 标）、`pull_request_review.submitted`、
`pull_request_review_comment.created`、`issue_comment.created`（斜杠命令）、`pull_request.closed`（merged）。

### 4.2 Event Router

- **幂等**：用 `X-GitHub-Delivery` 去重（GitHub 会重投）。
- **定位**：event →(repo, issue/pr)→ 找到或新建 Flow；`(repo, issue)` 唯一 active flow（防重复打标重复起）。
- **触发授权**：仅 allowlist 内的用户打 `flow` 标才启动——因为触发 = 在本机执行代码（见 §10-4）。
- **持久化入队**：webhook → 落 `events` 表 → 派生**持久化 job**（`jobs` 表）交 worker 消费；**内存队列不作为真相**，重启可恢复（见 §4.3、§10-1）。
- **对账兜底**：另有 reconcile poller 启动时 + 定期从 GitHub 重新同步 open flows，补 webhook 漏投（见 §10-2）。
- **串行化**：同一 flow 的事件串行处理（按 flow id 加锁/单队列），避免两个 codex 进程同时写一个工作区；不同 flow 之间并行。

### 4.3 Flow Engine（状态机，核心）

```
PENDING → CODING → PR_OPEN ⇄ REVISING
                      │
              (你在网页 Approve + Merge)
                      ▼
                    MERGED → ARCHIVED
任意态 → CANCELLED (去掉 flow 标 / /flow stop)
任意态 → FAILED    (codex 报错 / push 冲突)
```

| 转移 | 触发 | 动作 |
|---|---|---|
| PENDING→CODING | 打 `flow` 标 | 建工作区（VCS-无关拷贝），用 issue 正文启动 codex 会话 |
| CODING→PR_OPEN | codex 完成改码 + commit + push（本地平面，依 VCS 指引） | Orchestrator 据分支开 PR（`Closes #N`）、指定你为 reviewer、贴状态标签 |
| PR_OPEN→REVISING | review / inline 评论 | 汇总反馈 → 组织 follow-up prompt → **resume 同一会话** |
| REVISING→PR_OPEN | codex 完成修订 + commit + push | Orchestrator re-request review、评论"已按 review 更新：…" |
| PR_OPEN→MERGED | webhook `pull_request.closed (merged=true)`（你在网页手动 Approve + Merge） | 记录合并结果；issue 被 `Closes #` 自动关闭；**工具不执行合并** |
| MERGED→ARCHIVED | — | 归档记录、清工作区、Project 卡片移到 Done、贴归档小结 |

并发：多个 flow 并行（各自工作区）；单个 flow 内部事件串行。

**边界处理**：PR 被手动 close（未合并）→ CANCELLED；你自己往分支推了 commit → **暂停自动修订**、别覆盖你的改动；
base 前进致 PR 落后 / 冲突 → 工作区内 rebase base 后 push，冲突 → FAILED 并评论；CANCEL 要真正 **kill codex 进程 + 清工作区**。

**崩溃恢复**：状态与 job 全落库；daemon 启动时 reconcile——进行中 job 的进程已死 → 重驱动或转 FAILED；
push / 开 PR 等副作用前先查存在，保证幂等（见 §10-1）。

### 4.4 Codex Driver（已实测 resume ✅）

驱动 codex CLI 子进程（实测版本 `codex-cli 0.145.0`）。接口 `start(task, cwd) → session_id` /
`resume(session_id, followup)`，全程捕获 JSONL 日志入库。**这是未来 `CodeAgent` 端口的第一个 adapter（见 §9 第 4 条）。**

**已验证的调用形态**（2026-08-01 实测，会话上下文跨非交互调用保留）：

- 起始：`codex exec <PROMPT> --json -C <ws> -s workspace-write --skip-git-repo-check -o <last.txt> </dev/null`
  → 从 JSONL 的 `{"type":"thread.started","thread_id":"<UUID>"}` 事件取 `thread_id` 作为 session id。
- 续接：`codex exec resume <session_id> <PROMPT> --json --skip-git-repo-check -o <last.txt> </dev/null`
  → 实测第二轮能回忆起第一轮设定的内容，**上下文连续性成立**。
- 注意：`resume` **不接受** `-s` / `-C`（cwd 与 sandbox 从会话继承）。起始时用 `-C` 把工作区定死、
  `-s workspace-write` 给写权，正好契合"每 flow 一个工作区"的模型；resume 要改 sandbox 用 `-c sandbox_mode=...`。
- 坑：stdin 要重定向 `</dev/null`，否则 codex 会等 stdin 并附一个空 `<stdin>` 块。
- 输出：`--json` 给事件流（`thread.started` / `turn.started` / `item.completed` / `agent_message` / `turn.completed`）；
  `-o` 单独写最终回复文本，便于解析。

设计要点：

- **平面边界（见 §3）**：codex 负责**本地平面**——改码 + commit + branch + push（依 §4.6 的 **VCS 指引文本**，git / jj 通用），
  commit message 由 agent 自己决定，**分支名由 Orchestrator 定**（它来开 PR，指引文本把名字告诉 agent）；Orchestrator 只在 push 后走 GitHub API 开 PR（`Closes #`、reviewer、labels）。
  > 实测（2026-08-01）：`workspace-write` 下 **jj 的 `.jj` 可写、codex 能原生 `jj commit`**（已核实落库）；
  > 但 **git 的 `.git` 只读、`git commit` 被挡**（`.git/index.lock: Operation not permitted`）。这是**沙盒在漏 VCS 知识**——它守 `.git` 却不知守 `.jj`，jj 是「碰巧」能过（`.git` guard 是有意的安全启发式，不是 bug）。故这是 **sandbox 轴**的事，未来容器 Sandbox adapter 接管 FS 策略即解（§4.6 / 原 V4）；MVP 阶段 jj repo 已通、git repo 提交路径待定。codex 对 jj 不熟、需注入 jj 指引 / skill。
- **会话连续性**：resume 同一 codex 会话（已验证保留上下文）为主路径；
  兜底 = 每轮把 issue + 当前 diff + review 线程重新塞进 prompt。Driver 两条路都保留。
- **安全**：codex 在本机自动执行模型生成的代码 / 命令——每次 run 经 **Sandbox 端口**限权（MVP adapter = codex `workspace-write`），后续换容器 / 微VM adapter（§4.6 / §9-5）。
- **失控闸**：单次 run 超时、单 flow 最大修订轮数、token / 预算上限、kill switch；
  resume 多轮 context 无限累积 → 触发 re-seed / 压缩策略（见 §10-7）。

### 4.5 GitHub Client

- **认证：GitHub App（已定）**——独立 bot 身份，自带 webhook + 短时 installation token。接入细节见 **附录 A**。
- 库建议 **`githubkit`**（typed、async、内置 App 认证与 webhook 模型）。
- 能力：建 ref/PR、请求 reviewer、拉取带 diff hunk 的 review 评论、发评论、关 issue、更新 Project v2 卡片（GraphQL）、增删标签。**不含合并**（合并全程人工）。

### 4.6 本地平面：三条正交轴（workspace / sandbox / VCS）

本地平面**不是一个端口**，而是三条互不依赖的轴。此前把它们塞进一个「VCS 端口」是过度抽象
（薄包装、`head_commit`/`rebase_onto`/`cleanup` 全是死代码、只 jj 验过），且是那次 git-commit 故障的根因——
故 2026-08-02 拆开：

| 轴 | 是不是端口 | 干什么 | 载体 |
|---|---|---|---|
| **workspace** | 否（普通函数） | 每 flow 一个隔离目录，**VCS-无关** | `app/vcs.py`：`make_workspace` / `cleanup_workspace` |
| **sandbox** | **是（端口）** | 可写根 + 网络出口，**只认路径、不认 git/jj** | `ports.Sandbox` / `app/sandbox.py` |
| **VCS** | 否（指引文本） | branch / commit / push 由 agent 自己做 | `app/vcs.py`：`agent_guidance()`（git / jj） |

- **workspace（隔离）**：每 repo 一个主 clone（`~/.flowtool/repos/<owner>/<repo>`）；每 flow 一个独立工作区，
  做法是**VCS-无关的目录拷贝**（不跑 `git worktree` / `jj workspace`——那会把 workspace 绑死到具体 VCS）。
  分支名 `flow/issue-<n>` 由 Orchestrator 定（它来开 PR）、经指引文本告诉 agent。留这一步是为了：分支名权威、可观测、给 sandbox 圈范围、崩溃恢复。
- **sandbox（限权）**：一次 agent run 的**可写根 + 网络出口**策略，只认路径。MVP adapter = codex 原生 `workspace-write`
  （工作区可写、默认断网；push 时放开出口，见 §10-4）；容器 / 微VM 是后续 adapter（§9-5，M5）。
  按平面边界（§3），改码 + commit + push 都在这个沙盒里跑。
- **VCS（提交 / 推送）**：改动后 commit、（需要时）rebase base、push，**全由 code agent 自己做**，工具只出一段
  **指引文本**（探测 `.jj/` → jj 指引，否则 git 指引）注入 prompt。push 用 Orchestrator 现签的**仓库级短时 token**（见 §10-4）；
  Orchestrator 只在 push 后走 GitHub API 开 PR。冲突 / 归档 / 取消：清理工作区（`cleanup_workspace`）。

> 实测（2026-08-01）：`workspace-write` 下 **jj `.jj` 可写，codex 原生 `jj commit` 成功**（已核实落库）；
> **git `.git` 只读，`git commit` 被挡**。根因是 **sandbox 轴在漏 VCS 知识**（守 `.git` 却不知守 `.jj`），
> 不是 VCS 轴的问题——未来**容器 Sandbox adapter 接管整个工作区的 FS 策略**即消除，届时 git/jj 的差别塌成纯指引文本（解掉原 V4）。
> MVP 阶段：jj repo 提交路径已通；git repo 待定。

### 4.7 Store（SQLite）

- `flows`(repo, issue_number, state, installation_id, **current_attempt_id**, timestamps…)——**issue 级追踪 + 状态机**；`(repo, issue)` 上 active flow 唯一约束。**不再含 branch/pr/session**（已挪到 attempt）。
- `attempts`(flow_id, branch, codex_session_id, pr_number, revision_cycles, timestamps)——**一次候选解法**；一条 flow 挂多个（§9-2），MVP 一个，`flows.current_attempt_id` 指当前那个。**issue↔PR 不再 1:1 锁死。**
- `jobs`(flow_id, kind, state[queued/running/done/failed], attempt, pid, lease/heartbeat, timestamps)——**任务队列的真相**，支撑崩溃恢复与未来 `JobDispatch`（§10-1、§9-6）。
- `events`(delivery_id 去重 + 审计)
- `codex_runs`(**attempt_id**, kind[initial|revision], prompt, log_path, exit_code, timestamps)——`flows → attempts → runs` 一对多（§9-2）。
- `review_threads`(**attempt_id**, comment_id, path, line, original_sha, body, relayed / resolved 标记)——归属到具体 attempt（§9-2）。

归档即 `state=ARCHIVED`。持久化状态用于崩溃恢复（见 §4.3）。

### 4.8 交互面 = GitHub 本身（headless 的关键）

- 触发：给 issue 打 `flow` 标；取消：去标 或 `/flow stop`。
- 反馈：正常的 PR review（request changes / inline / 评论）。
- 合并：**全程人工**——你在 GitHub 网页上自己 Approve + 点 Merge，**工具从不自动合并**；它只监听 `pull_request.closed (merged=true)` 来触发收尾 / 归档。
- 状态可见性：工具贴 `flow:coding / in-review / revising / merged` 标签 + 进度评论——
  **这些就是 headless 的 "UI"**。

### 4.9 Context Builder（prompt / 上下文构建，最高杠杆）

工具效果的上限压在喂给 codex 的内容上，单列一个可测组件（喂给 §4.4 的 `start` / `resume`）。

- **首轮**：issue 标题 + 正文 + repo 约定（`AGENTS.md` / `CLAUDE.md`）+ 本仓库 **VCS 指引**（jj / git 各自的提交方式）+ 硬约束
  （"改码 → 用本仓库 VCS 提交 + push；**不要开 PR / 发评论**（GitHub 侧工具来做）、如何跑测试 / lint"）。
- **修订轮**：打包后的 review 反馈（正文 + inline，带文件 / hunk / 原文引用与**原 commit sha**，规避行号漂移）
  + 当前 diff + 未解决线程清单。
- issue / PR 正文当作**不可信输入**处理（防 prompt injection，见 §10-4）。

### 4.10 Judge / Triage（小型 agent，用 bub）

修订循环里"该不该改、改什么"由一个**轻量语义判定**决定（不是 codex 那套 agentic 改码）——独立的 `Judge` 端口。

- **MVP adapter = [bub](https://bub.build)**（`bubbuild/bub`）：hook-first 的小型 agent runtime，
  本就是为"多人 + agent 共处同一对话"设计的，PR 的 review 讨论正是这种场景。
- **用法**：以库方式起一次性 turn（`build_prompt` 注入判定指令 + 讨论快照，`run_model` 出结论），
  解析成结构化 verdict；模型经 `BUB_MODEL` / `BUB_API_KEY` 配（挑便宜快的）。
- **职责**：① 触发判定 `revise / wait / answer / ignore`；② `answer` 时直接以小 agent 身份在 PR 作答。
- 隔离：bub 尚在 0.x（0.3.9），API 可能变——包在 `Judge` 端口后面，随时可换直连模型或别的 runtime。
- 完整循环语义见 **附录 B**。

## 5. 端到端时序

1. 你建 issue、打 `flow` 标
2. webhook `issues.labeled`
3. 建工作区、codex 首轮编码
4. 推分支、开 PR（`Closes #N`）、@你审核
5. 你在 GitHub 上审
6. webhook 回传，汇总反馈 resume codex 修订
7. 再推、re-request review（5–7 循环）
8. 你在网页上 Approve + 点 Merge（工具不参与合并；CI 由仓库自身 CI 呈现在 PR 上，你据此判断）
9. webhook `pull_request.closed (merged=true)` → 记录合并、issue 被 `Closes #` 自动关闭
10. 归档、清理工作区、Project 卡片移 Done

## 6. 关键决策 & 推荐

| 决策 | 结论 | 说明 |
|---|---|---|
| 认证 | ✅ **GitHub App** | 独立 bot 身份；接入见附录 A |
| 合并 | ✅ **全程人工** | 你在网页 Approve + Merge，工具只监听合并事件收尾 |
| CI 门禁 | ✅ **交给仓库自身 CI** | 依赖 repo 的 CI + 分支保护呈现在 PR 上；工具不自建门禁，人工合并时你据此判断 |
| 合并方式 | ✅ **由你在网页选** | 工具不参与合并，方式随你 |
| 会话连续 | ✅ **resume 同会话** | 已实测（见 4.4），兜底 re-seed 保留 |
| 谁开 PR | ✅ **Orchestrator** | codex 只改码 + commit、工具建 PR：元数据可控、链路可靠 |

架构决策已全部锁定。

## 7. 头号风险 & 待核实

1. ~~codex CLI 非交互 / resume / 改码 / jj 提交~~ **已实测通过**（0.145.0，见 §4.4 / §4.6）：`codex exec` + `resume <id>` 上下文连续；`workspace-write` 下能改工作区，**jj `.jj` 可写、`jj commit` 成功**；**git `.git` 只读、`git commit` 被挡**。剩余待验：**git repo 提交路径**（这是 **sandbox 轴**的事——放宽 workspace-write 或换容器 Sandbox adapter，§4.6，非 VCS 层）、**push 联网 + scoped token**、多轮修订 diff/token 增长、并发多会话隔离。
2. codex 本机自动执行模型生成命令 → 限权 / 后续容器化。
3. webhook 本地投递 → 隧道或公网部署。
4. 并发写 base 冲突 → 每 flow 独立工作区 + push 前 rebase + 冲突转 FAILED。
5. webhook 长任务 vs 10s 超时 → 入队 + worker 异步跑 codex。

## 8. 建议目录 & 里程碑

```
flowtool/
  app/
    ingress.py        # FastAPI webhook 端点 + 验签
    router.py         # 事件归一 / 去重 / 路由
    engine.py         # Flow 状态机
    codex_driver.py   # CodexSession 抽象（CodeAgent 端口首个 adapter）
    context_builder.py # prompt / 上下文构建（§4.9）
    judge.py          # Judge / Triage 适配器（bub，§4.10）
    github_client.py  # githubkit 封装（App 认证）
    vcs.py            # workspace 隔离(VCS-无关) + VCS 指引文本(git / jj)，普通函数（§4.6）
    sandbox.py        # Sandbox 端口 adapter：codex workspace-write（§4.6 / §9-5）
    worker.py         # job 队列消费 + 崩溃恢复（§10-1）
    reconcile.py      # 对账 poller（§10-2）
    store.py          # SQLite 模型 + 仓储（含 jobs 表）
    config.py         # 配置（pydantic-settings）
    models.py         # 领域类型 / 枚举
    cli.py            # admin CLI（§10-8）
    plugins/          # 生命周期 hook 插件（§9-7）
  migrations/
  tests/
  pyproject.toml      # uv 管理
  README.md
```

- **M0（已验收）** 骨架 + webhook 验签 + SQLite（含 `jobs`）+ 配置 + worker / 崩溃恢复骨架；
  18 个自动化测试、Ruff、Mypy、CLI 与 `/healthz` runtime smoke 全部通过（2026-08-26）
- **M1** 触发（含 allowlist）→ 工作区 → codex 首轮 → 开 PR（单 flow happy path）
- **M2** 语义 Triage（bub）门 + 回喂循环（settle → revise/answer/wait + resume/兜底 + 自动 resolve）
- **M3** 人工合并检测（`pull_request.closed`）→ 归档 + 状态标签 / 评论
- **M4** 多 flow 并发 + reconcile 对账 + Project 看板 + 错误恢复
- **M5** 安全隔离（容器化 codex）+ 公网 / 隧道部署 + admin CLI

## 9. 未来扩展与架构预留（Roadmap）

> 以下是 MVP 之后的方向。列在这里是为了**现在就把接缝（ports）留好**，避免日后大改——
> core 只依赖抽象端口，扩展一律做成"换 adapter"或"挂插件"，不动 core。

1. **Web 控制台（Kanban）** —— 一个 webapp 打开项目看板，每张卡 = 一条被追踪的对话，可点进去实时参与对话。
   - 预留：core 状态经 store + 一层 API 暴露；headless（GitHub）与 webapp 是同一 core 的两个入口适配器。
     Driver 除"批量回喂 review"外，要留一个**实时注入消息**的入口，webapp 才能"插话"。

2. **多 agent 并行 / 多方案分支** —— 同一任务多个 agent 并行跑，或不同分支跑不同解法，再择优。
   - **数据模型接缝已就位（2026-08-02）**：引入 `attempts` 表，`issue ↔ PR` 不再 1:1 锁死——一条 flow 可挂多个 **attempt/candidate**
     （各自 branch / 工作区 / 会话 / PR）；`flows → attempts → runs` 一对多，`review_threads` 归属到具体 attempt，`flows.current_attempt_id` 指当前候选。
     待实现：真正并行起多个 attempt + 择优逻辑（engine 目前每 flow 只建一个 attempt）。

3. **完善 GitHub 协作** —— 覆盖常见协作动作（reviewer 指派、draft PR、labels、Projects 联动、review 线程 resolve 等）。
   - 预留：GitHub Client 保持薄封装，能力按需追加即可，不影响 core。

4. **Code Agent 标准接口** —— 把"与 code agent 交互"抽象成统一端口，方便接入新的底层 agent（不只 codex）。
   - 预留：把 §4.4 的 Codex Driver 收敛成 `CodeAgent` port（`start / resume / inject / 收集产物`）；
     codex 是它的**第一个 adapter**，后续 Claude Code 等按同一端口接入。

5. **更强隔离** —— 每次 agent 运行放进容器 / 微VM，隔离文件系统与网络。
   - 预留：`Sandbox` port **已是一等轴（§4.6，M0 就立）**，MVP adapter = codex `workspace-write`；容器 / 微VM 是后续 adapter，对应 M5。
     那个 adapter 由它**接管整个工作区的 FS 策略**，git/jj 的差别塌成纯指引文本、顺带解掉 git `.git` 只读（原 V4）。

6. **控制面 / 执行面分离** —— webapp + webhook 入口与底层 code agent 可分在不同机器，从而**调度**被追踪的 job。
   - 预留：engine 触发 codex 的那一步走一个 `JobDispatch` port（MVP 进程内直接调；日后换成队列 + 远程 worker）。
     core 不假设 worker 在本机，job 状态经 store 共享。

7. **端口插件扩展** —— 在生命周期端口挂插件。
   例：合并后总结插件——归纳上一段对话里的代码风格偏好、更新仓库的 skill 文件。
   - **已接线（2026-08-02）**：五个生命周期 hook（`on_flow_start / on_pr_opened / on_review / on_merged / on_archived`）已在 engine 的对应状态转移处触发，插件可订阅；插件异常被隔离（不掀翻 core）。
     待实现：示例插件 `post-merge-summary` 的插件体（读会话 + diff → 提炼风格偏好 → 提 PR 更新 repo 的 skill），目前只打日志。

**架构取向：hexagonal（ports & adapters）。** core（Flow Engine + 状态机 + store）只依赖端口：
`CodeAgent` / `Judge` / `Sandbox` / `JobDispatch` / `GitHubClient` / `Plugin hooks`。
（**VCS 不是端口**——它是 workspace 拷贝函数 + git/jj 指引文本，见 §4.6。）
MVP 给每个端口一个最简 adapter（codex / bub / workspace-write / 进程内 / githubkit / 空插件集），
上面 7 条都是"换 adapter 或加插件"，core 保持不变。

## 10. 待强化 / 已知短板

MVP 之前需要正视的短板。✅ 表示已回填进正文对应组件，⬜ 表示仍需定策略的开放项。

1. **持久化 job + 崩溃恢复** ✅ — 见 §4.2 持久化入队、§4.3 崩溃恢复、§4.7 `jobs` 表。
   codex 不再由 webhook task 直接拉起，`jobs` 表为真相，重启可 reconcile；副作用做幂等（防重复开 PR）。这也是 §9-6 `JobDispatch` 的种子。
2. **webhook 漏投 → reconcile 兜底** ✅ — 见 §4.2 对账兜底。webhook 主、周期轮询补漏。
3. **修订循环语义** ✅ — 见 **附录 B**（v3）：语义 Triage 门（`Judge` / bub）决定 revise/wait/answer/ignore；
   settle 窗口等会话停顿；行号漂移带 hunk + 原 sha；已处理线程自动 `resolveReviewThread`；并发合并排队、`/flow restart` 打断。
4. **触发授权 + 安全前移** ✅/⬜ — allowlist 与"issue 文本不可信"已入 §4.2 / §4.9；
   仍待定：**push 凭据** = Orchestrator 现签的**仓库级、短时、contents:write** token，经 git credential helper 注入、用完即弃、不落工作区（**App 私钥永不出 Orchestrator**）；**为 push 放开网络 egress**（由 **Sandbox 端口的 network 策略**控制，§4.6；尽量限到 github.com）。**打标 = 本机执行代码，安全是地基不是 M5。**
5. **Context Builder 一等组件** ✅ — 已单列 §4.9。
6. **状态机边界** ✅ — 见 §4.3 边界处理（PR 手动 close、外部 push、base 前进、CANCEL kill 进程）。
7. **成本 / 失控闸** ✅ — 见 §4.4：run 超时、最大修订轮数、预算上限、kill switch；resume context 累积 → re-seed / 压缩（待验）。
8. **上 webapp 前的可运维性** ⬜ — 补 admin CLI（重试失败 flow / 掐死 codex / 看日志）+ 结构化日志 + dry-run / mock 模式（录制 webhook fixtures + 假 codex）。

**优先级**：#1 / #3 / #4 最影响骨架、事后最难补，先做；#5 决定效果上限、边做边打磨；#8 在 webapp 之前补足。

---

## 附录 A：GitHub App 接入

GitHub App 与 PAT 的本质区别：它是一个**有独立身份的机器人**，认证不是一把固定 token，
而是"**私钥签发临时 token**"的两段式。工具的评论以 `flowtool[bot]` 名义发出，和你自己的 review 一眼分得清。

### A.1 三种 token

```
私钥(.pem) + App ID
   │  用私钥签 RS256 JWT（最长 10 分钟）
   ▼
App JWT ──POST /app/installations/{installation_id}/access_tokens──► Installation Token（1 小时，限定权限+repo）
                                                                          │
                                                          以 flowtool[bot] 身份调 API + git push
```

- **App JWT**：证明"我是这个 App"，只用来换下面的 token（管不了具体 repo）。
- **Installation Token**：真正干活的 token，1 小时过期、仅在你授权的 repo + 勾选权限内有效；工具缓存、快过期再换。
- **User token**（OAuth 代表某用户）——**本工具用不到**。

### A.2 一次性设置（网页操作一次）

1. **注册 App**：`github.com/settings/apps → New GitHub App`（个人账号即可）。填：
   - **Webhook URL**（事件推送地址）+ **Webhook secret**（自定，用于验签）
   - **Permissions**（细粒度，只给需要的）：
     - Contents: **Read & write**（push 分支 / 提交）
     - Issues: **Read & write**（评论、打标签、关闭）
     - Pull requests: **Read & write**（建 PR、评论、请求 reviewer）
     - Metadata: **Read**（强制）
     - Checks / Commit statuses: **Read**（想读 CI 状态才要）
   - **Subscribe to events**：Issues、Issue comment、Pull request、Pull request review、Pull request review comment
2. **生成并下载私钥**（`.pem`）；记下 **App ID**。
3. **安装到目标 repo**：App 页 → Install → 勾选 repo，得到 **installation id**（webhook payload 里也带 `installation.id`）。

产出四样：`App ID` · `私钥.pem` · `Webhook secret` · `installation id`。

### A.3 运行时流程

1. Webhook 进来 → 用 **Webhook secret** 验 `X-Hub-Signature-256`（HMAC-SHA256 原始 body）→ 读 `installation.id`。
2. 用 **App ID + 私钥** 签 JWT → 换 **installation token**（缓存 ~55 分钟）。
3. 用该 token 调 REST/GraphQL；**git push 也用它**：
   `https://x-access-token:<token>@github.com/<owner>/<repo>.git`

`githubkit` 内置 App 认证（给 `app_id / private_key / installation_id`，自动签 JWT、换 token、到期续），
并自带 webhook 验签与 typed 事件模型——不用手搓。

### A.4 本地开发的 webhook

本机无公网，用 **smee.io**：建 channel 得到公网 URL，填进 App 的 Webhook URL，
本地跑 `smee -u <channel> -t http://localhost:8000/webhook` 转发到 daemon。生产再换真实公网地址。

### A.5 密钥管理

`私钥.pem` / `Webhook secret` 走环境变量或本地 secret 文件，**不入库、不进 git**（`.gitignore` + `env.example` 占位）。

---

## 附录 B：修订循环语义（v3）

§3 里"你审 → 回喂 codex → 再改"这段闭环的完整语义。核心：**用语义判定决定要不要改、改什么**，不靠标记或点名。

### B.1 触发不靠语法，靠语义

所有非 bot 反馈只是"进入判定的素材"，不直接触发改码。判定由 `Judge`（§4.10，MVP 用 bub）做：

```
非 bot 反馈 → 缓冲 → settle 窗口(等会话停顿) → Judge 语义判定
        ├ revise → (贴"按 X 修改中") → codex 修订
        ├ answer → bub 以小 agent 身份在 PR 作答（不改码）
        ├ wait   → 继续缓冲，更多消息来了再判
        └ ignore → 略过
```

- **忽略 bot 自己的事件**（`sender == flowtool[bot]`），否则自触发死循环。
- **Approve** 是合并侧信号（人工网页合并），不进此循环。
- `settle` 窗口（~60–90s 静默）只为"等你们把话说完再判"，不再是触发器。

### B.2 Judge 的结构化产出

```json
{
  "verdict": "revise | wait | answer | ignore",
  "confidence": 0.0,
  "reason": "判断依据",
  "instruction": "verdict=revise：综合讨论得出的、要做的具体改动",
  "answer": "verdict=answer：回给提问者的话",
  "open_points": ["verdict=wait：还没定的点"]
}
```

- `revise` 且 `confidence ≥ 阈值` → 进 codex 修订，`instruction` 是权威指令、讨论原文作证据。
- 低于阈值 / 模棱两可 → 一律 `wait`（保守，宁可不动）。

### B.3 误判兜底

1. 保守默认 `wait`；2. 置信度阈值；3. `revise` 前贴一句意图、你可 `/flow stop` 否决；
4. 低风险（合并人工，错误只落在**未合并**的 PR 分支）；
5. 显式路径仍可用但非必需（`@flowtool 改…` 或 Request changes = 强信号，直接 revise、跳过判定）。

### B.4 一次 revise run

1. flush：取未回传反馈（review 正文 + inline + 讨论），标 `relaying`
2. ContextBuilder 组 prompt：`instruction` + 每条 inline 的 `path` + `diff_hunk` 原文 + `comment_id` + `original_commit_id`
   （按**内容**定位，避开行号漂移）+ VCS 指引 + 硬约束"用本仓库 VCS 提交 + push、别碰 GitHub API"
3. `codex exec resume <sid> <prompt> --output-schema out.json -o last.txt`（run 超时 + 轮数上限）
4. 成功：codex commit + push（push 前按 VCS 指引 rebase base，冲突 → FAILED）→ **Orchestrator** 开 / 更新 PR、按产出处理线程 → 标 `relayed` → re-request review + 贴 summary + 标签 `flow:in-review`
5. codex 产出（`--output-schema`）：
   ```json
   { "summary": "...",
     "addressed": [{"comment_id": 123, "note": "怎么改的"}],
     "deferred":  [{"comment_id": 456, "reason": "为何没改"}],
     "needs_clarification": ["歧义点…"] }
   ```
   - `addressed` → GraphQL `resolveReviewThread` **自动 resolve** + 回 note（可 re-open）
   - `deferred` → 回 reason，保持 open
   - `needs_clarification` 非空 → 发 PR 评论、转 IDLE 等你回复（不 re-request）
6. 失败 / 超时：反馈退回未回传、贴说明、计入失控闸

### B.5 并发

每 flow 至多一个 codex run 在飞；run 期间来的反馈攒到下一批（**不打断**）；`/flow restart` 才 kill 重开。

### B.6 循环状态机（PR_OPEN 内部）

```
        非bot反馈           settle到点 & Judge=revise
 IDLE ───────────► SETTLING ───────────────────► RUNNING (codex)
  ▲                 │  ▲                           │      │运行中来反馈
  │ 完成&无pending   │  │新反馈:重置 / Judge=wait:留   │      ▼ 存下批
  └──────────────────┘  └─────────── 完成&有pending ─┘
  /flow restart: kill→重开        /flow stop | 去标: CANCELLED
```

### B.7 参数（默认，可调）

| 参数 | 默认 | 含义 |
|---|---|---|
| settle 静默窗口 | 60–90s | 等会话停顿再判 |
| submit grace | 15s | Request-changes 后收齐 inline |
| confidence 阈值 | 0.6 | 低于当 `wait` |
| run 超时 | 20min | 单次 codex |
| 最大修订轮数 | 20 / flow | 失控闸 |

### B.8 未来（记一笔）

bub 本是"共处对话"的 runtime——日后可把 **GitHub PR 对话做成 bub 的一个 channel**（如同 Telegram），
让 agent 成为对话原生参与者，triage 与作答同处一个 runtime。属 §9-1 / §9-4 方向。
