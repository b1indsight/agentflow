# 组件职责

本页同时写目标职责和当前边界。组件的汇总状态以[架构入口](README.md#当前实现状态)为准。

## 4.1 Webhook Ingress（FastAPI + uvicorn）

职责：读取原始请求体，用 Webhook secret 校验 `X-Hub-Signature-256`，把有效事件交给 router，避免
Codex 等长任务占用 GitHub 的 Webhook 请求。

当前实现：

- `GET /healthz` 返回 `{"status": "ok"}`；它是存活探针，不检查数据库或外部依赖；
- `POST /webhook` 缺少或带错误签名时返回 401；签名正确时创建后台 handler 并返回 202；
- `WS /internal/workers/connect` 按 token map 鉴权，并为每个 worker identity 保持一条连接；
- FastAPI lifespan 初始化 PostgreSQL schema，启动 reconciler、lease sweeper 和 result inbox recovery，不启动进程内 worker；
- 自动化测试覆盖 health、错误签名、正确签名、202 交接和一条 signed label→job 路径。

边界：202 表示事件已交给进程内后台 task，不表示业务处理或持久化已经成功。进程在 handler 落库前
退出仍可能丢失事件，未来由可靠入站/对账语义补强。

## 4.2 Event Router

当前实现：

- 忽略 `sender.login == BOT_LOGIN` 的事件；
- 以 `X-GitHub-Delivery` 原子占有并持久化去重，只有成功插入的 handler 才继续处理，同时记录原始 payload；
- 仅允许 allowlist 用户通过 `issues.labeled(flow)` 创建 active flow 与 initial job；
- `issues.unlabeled(flow)` 或严格等于 `/flow stop` 的评论取消 active flow；
- review body、inline comment 和 PR conversation 归属到当前 attempt，settle 去抖后创建 TRIAGE job；
- `pull_request.closed (merged=true)` 驱动 MERGED；approve 本身不触发修订。

M0 自动化覆盖 delivery 去重、bot/allowlist、触发、取消和 inline review 入库。尚未实现：同一 flow
事件串行锁、未合并 PR close 的取消语义、后台 handler 失败后的 delivery 重试，以及真实 GitHub payload
的完整兼容性。Reconciler 仍是桩，不能作为当前补偿路径。

## 4.3 Control Service / Job Coordinator（状态机核心）

目标状态机：

```text
PENDING → CODING → PR_OPEN ⇄ REVISING
                      │
              人工 Approve + Merge
                      ▼
                    MERGED → ARCHIVED

任意活动态 → CANCELLED
运行失败   → FAILED
```

`ControlService` 负责触发、取消、合并和 retry；`JobCoordinator` 把五类持久 job 构造成版本化 task，
并把 worker 返回的结构化 result 应用为数据库、GitHub 与 plugin 副作用。worker 不能直接更新这些状态。
非零 Codex exit 会转 FAILED，revision 成功后会标记 addressed/deferred/clarification。

当前 coordinator 还依赖多处桩：

- `_fetch_issue()` 返回占位标题和空正文；
- `_thread_id_for()` 永远返回 `None`，无法真实 resolve review thread；
- `_pr_context()` 只有 repo、PR 和 issue 编号，没有 diff、CI 或未解决线程；
- base 固定为 `main`，没有从目标仓库配置解析默认分支；
- cancel 只更新数据库状态，不 kill Codex、不清工作区；
- 未合并 close、人工 push、rebase/冲突和外部副作用幂等尚未实现。

因此状态转换代码存在不等于目标闭环可用。

### 4.3.1 远程 Worker Gateway / Client / Executor

serve 的 `WorkerGateway` 校验每个 identity 的 bearer token，维护多会话 registry，并用随机 connection epoch 与
assignment token 对 Started、Heartbeat 和结果做四元组 fencing。每个 worker 是单槽，PostgreSQL 按 FIFO、
worker `last_assigned_at` 和 attempt affinity 事务化领取任务。worker 的 `WorkerClient` 主动连接并指数退避；
`WorkerExecutor` 只依赖 CodeAgent、Judge 和 Flow repo/VCS helpers，不导入 Store、FastAPI 或 GitHub。

不同 identity 可并发在线；同 identity 新连接取得新 epoch 并关闭旧 socket，数据库条件保证旧连接最终失效。
任务未 Started 断线后保留到 lease 到期再 requeue；Started job 进入 RECOVERING，只允许原 identity 在 grace 内恢复，
到期才把 job/flow 置 FAILED。worker socket 与 executor 解耦，root 内原子 journal 在 ack 前保存进度和终态结果；
进程重启可重放终态，但不会猜测恢复已经消失的 executor。同一 attempt 的后续任务仍只回到首次 Started 的 worker，
不支持跨 worker session 迁移。同机默认 root 按 identity 派生，并用持久 identity 标记与独占锁拒绝并发或顺序复用。

## 4.4 Codex Driver

`CodexAgent` 已实现 `CodeAgent` 端口的 `start` / `resume`：

- start 仅在显式 `FLOWTOOL_TRUSTED_HOST_EXECUTION=true` 时使用
  `codex exec <prompt> --json -C <flow-repo> -s danger-full-access`；
- start/resume 都要求 trusted-host 开关；worker 还必须声明专用 account/container/VM 隔离，且不得继承
  control-plane 环境变量；
- 从 JSONL 的 `thread.started.thread_id` 读取 session id；
- resume 使用 `codex exec resume <session_id>`，cwd 与 sandbox 继承自会话；
- `-o` 读取最终消息，INITIAL 和 revision 通过 `--output-schema` 解析结构化结果；
- 子进程只继承 Codex 认证、代理/CA 和基础运行环境，不继承 `FLOWTOOL_*` 或 GitHub token；
- 单次运行使用配置的 timeout，超时 kill 子进程并返回 exit 124。

2026-08-01 的独立 CLI 实测证明 `exec` / `resume` 上下文连续、workspace-write 可修改工作区、jj
`.jj` 可写且能 commit。它没有证明当前 driver 能在真实 flow 中完成 issue→commit→push→PR。

当前 worker 返回 exit/result，coordinator 把 kind 和 exit code 写入 `codex_runs`；跨协议尚未回传完整 prompt
或原始 JSONL，因此 `prompt`/`log_path` 为空。GitHub 限域、push 凭据和多轮 context 增长仍待验证。

## 4.5 GitHub Client

目标职责：以 GitHub App installation 身份创建 PR、评论、设置 labels、请求 reviewers、列出 review
threads、resolve thread，并签发 push 所需的短时 installation token。自动合并明确不在职责内。

`GitHubApp` 已写出 githubkit REST/GraphQL 调用形态，但全部属于未校准 adapter：真实认证方法、返回类型、
权限、分页、错误处理与 scoped token 都没有集成验证。当前代码也不支持 Project v2 更新、关闭 issue、
分支/ref 管理或远端状态查询。接入假设见 [GitHub App 文档](github-app.md)。

## 4.6 本地平面：Flow repo / execution policy / VCS

| 轴 | 是否端口 | 当前载体 | 当前边界 |
|---|---|---|---|
| Flow repo | 否 | `prepare_flow_repo/cleanup_flow_repo` | stable flow id 派生独占完整 repo；同 Flow 复用、不同 Flow 分离 |
| execution policy | 否 | worker config / `CodexAgent` | full access 需专用 account/container/VM；目录只表达所有权 |
| VCS | 否 | `agent_guidance/verify_vcs_outcome` | Agent 自探测；worker 用固定 remote 和净化、限时命令验证 ancestry/clean/remote SHA |

分支名由 control 定为 `flow/issue-<n>`。worker 内的 Code Agent 在 Flow repo 完成 branch/bookmark、
commit 和 push，worker 独立核验后，control 才把 INITIAL 视为成功。当前缺口包括：

- 每 Flow 容器/微 VM 和互不信任租户的强隔离仍属 M5；M1 至少要求 worker 与 control secret 分离；
- 仅允许 GitHub egress 的策略未实现；
- installation token 尚未安全注入 git credential helper；
- Flow 生命周期级 worker/repo binding 仍待 M1 Feature 3 持久化；
- 大 repo 的完整复制成本和 terminal cleanup 故障恢复仍待验证。

## 4.7 Store（PostgreSQL）

当前 Store 使用 Psycopg 3 的同步连接池访问 PostgreSQL：

`Flow`、`Attempt`、`Job` 和 `ReviewItem` 是 Pydantic 持久化模型。返回这些领域对象的查询在
cursor 上使用 Psycopg `class_row(Model)`，由模型统一校验枚举、时间和其他字段；其余轻量查询继续
使用 pool 的 `dict_row` 默认值。SQL、事务边界、JSONB 适配和并发协议仍由 Store 显式维护，没有引入
ORM。

- `flows`：issue 级状态，`(repo, issue)` 仅允许一个非终态 flow；
- `attempts`：每个候选解法的 branch/session/PR/worker_id/revision_cycles，当前每 flow 一个；
- `jobs`：queued/running/done/failed、worker/token/start、lease/recovery deadline、result inbox/digest 与尝试次数；
- `workers`：identity、协议版本、epoch、online/busy/recovering/offline/failed、当前 job、连接/心跳与断线原因；
- `events`：delivery 去重和 payload 审计；
- `codex_runs`：按 attempt 记录 prompt、kind、exit 与时间；
- `review_threads`：按 attempt 记录正文/inline/conversation 反馈及 relayed/resolved 状态。

自动化覆盖 active 唯一约束、flow/attempt/job 往返、review upsert、assignment start/heartbeat/fencing、
lease 到期 requeue、started grace/deadline、epoch fencing、result inbox 去重、跨 Store 可见性、delivery 去重和并发 assign。
`assign_job()` 使用事务内的 `FOR UPDATE SKIP LOCKED`，gateway 接受多个独立配置 worker。

Store 继续使用同步接口。assignment claim 会锁定公平候选 worker 和最早 eligible job，Started 在 token
条件下原子绑定 `attempt.worker_id`。result inbox 保证内部 coordinator 对已确认 digest 的重复结果不重复应用；
GitHub 等外部副作用与 `result_applied_at` 之间仍非原子，不能宣称 exactly-once。跨 worker session 迁移和多 serve HA
仍未实现。M0 的 SQLite 文件
不会自动导入 PostgreSQL。

## 4.8 GitHub 交互面

目标用户体验完全位于 GitHub：标签触发、PR review 反馈、网页人工合并、状态 labels 与评论。

当前 router 已识别触发、取消、review/comment 和 merged 事件；真正的 PR 创建、评论、reviewer、label、
thread resolve 与 Project 状态仍依赖未校准的 GitHub adapter。工具没有自动合并实现。

## 4.9 Context Builder

当前 `ContextBuilder` 是纯字符串组件：

- initial prompt 包含 issue 标题/正文、首个 `AGENTS.md` 或 `CLAUDE.md`、VCS 指引和硬约束；
- revision prompt 包含 Judge instruction、未 relay 的反馈、inline path/diff hunk/original commit id、
  VCS 指引和 `CodexResult` 输出约定。

但 coordinator 当前提供的 issue 正文是空占位；revision prompt 没有当前 diff、CI 状态或完整未解决线程清单。
issue/PR 文本是不可信输入，后续接入必须保留明确的数据/指令边界。

## 4.10 Judge / Triage

目标语义见 [Review 修订循环](review-loop.md)。当前 `BubJudge` 通过 subprocess 执行
`bub run <prompt>`，要求返回 JSON，再校验为 `TriageVerdict`；解析失败保守返回 `wait`。`answer()`
使用另一条简短 prompt。

实际 bub 命令、模型配置、退出码处理、结构化输出稳定性、置信度行为和真实 PR 讨论效果均未校准，
因此当前状态是桩，而不是已验证 adapter。
