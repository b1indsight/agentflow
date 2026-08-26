# 组件职责

## 4.1 Webhook Ingress（FastAPI + uvicorn）

`/webhook` 端点：校验 `X-Hub-Signature-256` HMAC → 按 `X-GitHub-Event` 解析 → **立即入队并秒回 2xx**
（GitHub 10s 超时，codex 长任务绝不能卡在这里）。

关注事件：`issues.labeled/unlabeled`（`flow` 标）、`pull_request_review.submitted`、
`pull_request_review_comment.created`、`issue_comment.created`（斜杠命令）、`pull_request.closed`（merged）。

## 4.2 Event Router

- **幂等**：用 `X-GitHub-Delivery` 去重（GitHub 会重投）。
- **定位**：event →(repo, issue/pr)→ 找到或新建 Flow；`(repo, issue)` 唯一 active flow（防重复打标重复起）。
- **触发授权**：仅 allowlist 内的用户打 `flow` 标才启动——因为触发 = 在本机执行代码（见 §10-4）。
- **持久化入队**：webhook → 落 `events` 表 → 派生**持久化 job**（`jobs` 表）交 worker 消费；**内存队列不作为真相**，重启可恢复（见 §4.3、§10-1）。
- **对账兜底**：另有 reconcile poller 启动时 + 定期从 GitHub 重新同步 open flows，补 webhook 漏投（见 §10-2）。
- **串行化**：同一 flow 的事件串行处理（按 flow id 加锁/单队列），避免两个 codex 进程同时写一个工作区；不同 flow 之间并行。

## 4.3 Flow Engine（状态机，核心）

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

## 4.4 Codex Driver（已实测 resume ✅）

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

## 4.5 GitHub Client

- **认证：GitHub App（已定）**——独立 bot 身份，自带 webhook + 短时 installation token。接入细节见 **附录 A**。
- 库建议 **`githubkit`**（typed、async、内置 App 认证与 webhook 模型）。
- 能力：建 ref/PR、请求 reviewer、拉取带 diff hunk 的 review 评论、发评论、关 issue、更新 Project v2 卡片（GraphQL）、增删标签。**不含合并**（合并全程人工）。

## 4.6 本地平面：三条正交轴（workspace / sandbox / VCS）

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

## 4.7 Store（SQLite）

- `flows`(repo, issue_number, state, installation_id, **current_attempt_id**, timestamps…)——**issue 级追踪 + 状态机**；`(repo, issue)` 上 active flow 唯一约束。**不再含 branch/pr/session**（已挪到 attempt）。
- `attempts`(flow_id, branch, codex_session_id, pr_number, revision_cycles, timestamps)——**一次候选解法**；一条 flow 挂多个（§9-2），MVP 一个，`flows.current_attempt_id` 指当前那个。**issue↔PR 不再 1:1 锁死。**
- `jobs`(flow_id, kind, state[queued/running/done/failed], attempt, pid, lease/heartbeat, timestamps)——**任务队列的真相**，支撑崩溃恢复与未来 `JobDispatch`（§10-1、§9-6）。
- `events`(delivery_id 去重 + 审计)
- `codex_runs`(**attempt_id**, kind[initial|revision], prompt, log_path, exit_code, timestamps)——`flows → attempts → runs` 一对多（§9-2）。
- `review_threads`(**attempt_id**, comment_id, path, line, original_sha, body, relayed / resolved 标记)——归属到具体 attempt（§9-2）。

归档即 `state=ARCHIVED`。持久化状态用于崩溃恢复（见 §4.3）。

## 4.8 交互面 = GitHub 本身（headless 的关键）

- 触发：给 issue 打 `flow` 标；取消：去标 或 `/flow stop`。
- 反馈：正常的 PR review（request changes / inline / 评论）。
- 合并：**全程人工**——你在 GitHub 网页上自己 Approve + 点 Merge，**工具从不自动合并**；它只监听 `pull_request.closed (merged=true)` 来触发收尾 / 归档。
- 状态可见性：工具贴 `flow:coding / in-review / revising / merged` 标签 + 进度评论——
  **这些就是 headless 的 "UI"**。

## 4.9 Context Builder（prompt / 上下文构建，最高杠杆）

工具效果的上限压在喂给 codex 的内容上，单列一个可测组件（喂给 §4.4 的 `start` / `resume`）。

- **首轮**：issue 标题 + 正文 + repo 约定（`AGENTS.md` / `CLAUDE.md`）+ 本仓库 **VCS 指引**（jj / git 各自的提交方式）+ 硬约束
  （"改码 → 用本仓库 VCS 提交 + push；**不要开 PR / 发评论**（GitHub 侧工具来做）、如何跑测试 / lint"）。
- **修订轮**：打包后的 review 反馈（正文 + inline，带文件 / hunk / 原文引用与**原 commit sha**，规避行号漂移）
  + 当前 diff + 未解决线程清单。
- issue / PR 正文当作**不可信输入**处理（防 prompt injection，见 §10-4）。

## 4.10 Judge / Triage（小型 agent，用 bub）

修订循环里"该不该改、改什么"由一个**轻量语义判定**决定（不是 codex 那套 agentic 改码）——独立的 `Judge` 端口。

- **MVP adapter = [bub](https://bub.build)**（`bubbuild/bub`）：hook-first 的小型 agent runtime，
  本就是为"多人 + agent 共处同一对话"设计的，PR 的 review 讨论正是这种场景。
- **用法**：以库方式起一次性 turn（`build_prompt` 注入判定指令 + 讨论快照，`run_model` 出结论），
  解析成结构化 verdict；模型经 `BUB_MODEL` / `BUB_API_KEY` 配（挑便宜快的）。
- **职责**：① 触发判定 `revise / wait / answer / ignore`；② `answer` 时直接以小 agent 身份在 PR 作答。
- 隔离：bub 尚在 0.x（0.3.9），API 可能变——包在 `Judge` 端口后面，随时可换直连模型或别的 runtime。
- 完整循环语义见 **附录 B**。
